package com.marketradar.app

import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Durable PC2 upload queue.
 *
 * Codex review corrections R1-R3 (2026-09-30) supersede the first correction
 * pass. The governing rule is now explicit:
 *
 *   **Evidence leaves the pending queue only when the backend has acknowledged
 *   it, or when the payload itself has been proven permanently unacceptable.
 *   Nothing is ever removed because it is old, because it has been tried many
 *   times, or because a temporary file looked abandoned.**
 *
 *  - R1: [classifyPostFailure] quarantines only proven payload-specific
 *    rejections. Everything else - missing tables before the migration, expired
 *    auth, rate limits, gateway and schema-cache errors, outages - retries
 *    automatically and indefinitely. The attempt counter is diagnostic: past
 *    [DEFER_AFTER_ATTEMPTS] a batch stops *blocking* the queue but stays pending
 *    and is retried on every later pass.
 *  - R2: archive moves are collision-safe, their result is checked, the attempt
 *    marker survives a failed move, and the reason is persisted beside the
 *    retained bytes so it outlives a restart. A failed move is reported as a
 *    storage error, never as a successful quarantine.
 *  - R3: an orphaned `.tmp` may hold the only copy of a complete envelope
 *    (process death between `fd.sync()` and `renameTo`). It is verified by
 *    digest and promoted into the queue; only unverifiable bytes are archived,
 *    and neither is ever deleted on age.
 *
 * Work per drain pass is bounded by [MAX_UPLOADS_PER_PASS]. The object stays
 * free of Android dependencies so every rule above is unit-testable.
 */
object Pc2TelemetryOutbox {
    private const val DIRECTORY = "pc2_telemetry_outbox_v1"
    private const val QUARANTINE = "quarantine"
    private const val RECOVERY = "recovery"

    /** A `.tmp` younger than this may still be mid-write by a live enqueue. */
    private const val TMP_SETTLE_MS = 60L * 1000L

    /**
     * R1: diagnostic only. A batch past this many retryable attempts stops
     * holding up the batches behind it; it is NOT removed and is retried on
     * every subsequent pass.
     */
    internal const val DEFER_AFTER_ATTEMPTS = 20

    /** Bounded work per pass so a long queue cannot monopolise a poll. */
    internal const val MAX_UPLOADS_PER_PASS = 25

    private val draining = AtomicBoolean(false)

    /**
     * Test seam for R2. Archiving must behave correctly when the filesystem
     * refuses a move, and a JVM test running as root cannot provoke that with
     * permissions. Production always uses [File.renameTo]; only tests replace it.
     */
    internal var moveFile: (File, File) -> Boolean = { source, target -> source.renameTo(target) }

    /** Upload result contract. RETRY keeps evidence queued; QUARANTINE is terminal. */
    enum class Outcome { ACKNOWLEDGED, RETRY, QUARANTINE }

    /**
     * Postgres / PostgREST codes that prove THIS payload can never be accepted.
     * Anything absent from this list is treated as recoverable.
     *
     * Deliberately excluded:
     *  - `PGRST205` / `42P01` (table absent): the documented pre-migration state.
     *  - `PGRST204` (column absent / stale schema cache): a partial or
     *    not-yet-reloaded migration, recoverable without touching the payload.
     *  - `23503` (foreign key): the policy registry row may simply not be
     *    inserted yet; a later pass fixes it.
     *  - every unclassified 4xx, including gateway 404s, which say nothing
     *    about payload validity.
     */
    private val PERMANENT_PAYLOAD_CODES = listOf(
        "23505",   // unique_violation - the secondary constraint, not the on_conflict target
        "22P02",   // invalid_text_representation
        "22003",   // numeric_value_out_of_range
        "22001",   // string_data_right_truncation
        "23502",   // not_null_violation
        "23514",   // check_violation
        "PGRST102" // mixed key sets within a single bulk insert
    )

    /**
     * R1: classify an upload rejection. The default is RETRY - a failure must
     * prove itself permanent before evidence is set aside.
     *
     * The one concrete permanent case preserved from the earlier pass: the batch
     * table carries `unique (poll_ts, policy_hash, authority_diagnostics_version)`,
     * which is NOT the `on_conflict=batch_id` target, so a second differing batch
     * for the same poll is rejected forever and would otherwise stall the queue.
     */
    fun classifyPostFailure(code: Int?, body: String): Outcome {
        if (PERMANENT_PAYLOAD_CODES.any { body.contains(it) }) return Outcome.QUARANTINE
        // PostgREST surfaces a unique violation as 409 even when the body is empty.
        if (code == 409) return Outcome.QUARANTINE
        return Outcome.RETRY
    }

    data class DrainResult(
        val attempted: Int,
        val acknowledged: Int,
        val quarantined: Int,
        val pending: Int,
        val pendingBytes: Long,
        val quarantinedTotal: Int,
        val recoveredTotal: Int = 0,
        val deferred: Int = 0,
        val storageErrors: Int = 0,
        /** "<batchId>:<reason>" for each batch archived during this pass. */
        val quarantineReasons: List<String> = emptyList(),
        /** "<name>:<reason>" for each orphaned temp file resolved this pass. */
        val recoveredReasons: List<String> = emptyList(),
        /** "<name>:<reason>" for each archive move that could not be completed. */
        val storageErrorReasons: List<String> = emptyList()
    )

    @Synchronized
    fun enqueue(filesDir: File, built: Pc2CompactBatch.Built): Boolean {
        val envelope = built.envelope()
        val batchId = built.batchRow.getString("batch_id")
        require(batchId.matches(Regex("[0-9a-f]{64}"))) { "Invalid PC2 batch id" }
        val dir = directory(filesDir)
        val canonical = Pc2CompactBatch.canonicalJson(envelope)

        val existingFile = fileForBatch(dir, batchId)
        if (existingFile != null) {
            val existing = runCatching { JSONObject(existingFile.readText(Charsets.UTF_8)) }.getOrNull()
            check(existing != null && Pc2CompactBatch.canonicalJson(existing) == canonical) {
                "PC2 outbox collision for $batchId"
            }
            return false
        }
        // R1: a previously quarantined batch is NOT refused here. If the builder
        // produces it again after a backend fix, it gets a fresh attempt; the
        // archive is collision-safe, so an unchanged rejection costs one archive
        // entry rather than the permanent loss of a recoverable batch.
        writeAtomically(dir, "${System.currentTimeMillis()}-$batchId.json", canonical)
        return true
    }

    @Synchronized
    fun pending(filesDir: File): List<JSONObject> = pendingFiles(filesDir).mapNotNull { file ->
        runCatching { JSONObject(file.readText(Charsets.UTF_8)) }.getOrNull()
    }

    @Synchronized
    fun acknowledge(filesDir: File, batchId: String): Boolean {
        if (!batchId.matches(Regex("[0-9a-f]{64}"))) return false
        val dir = directory(filesDir)
        val file = fileForBatch(dir, batchId) ?: return true
        val removed = file.delete()
        if (removed) attemptsFile(dir, file).delete()
        return removed
    }

    fun drain(filesDir: File, uploader: (JSONObject) -> Outcome): DrainResult {
        if (!draining.compareAndSet(false, true)) {
            return currentResult(filesDir, 0, 0, 0, emptyList(), emptyList(), emptyList())
        }
        return try {
            val dir = directory(filesDir)
            val recovery = recoverOrphanedTempFiles(dir)
            var attempted = 0
            var acknowledged = 0
            var deferred = 0
            val quarantineReasons = mutableListOf<String>()
            val storageErrors = recovery.storageErrors.toMutableList()

            for (file in pendingFiles(filesDir)) {
                if (attempted >= MAX_UPLOADS_PER_PASS) break
                val raw = runCatching { file.readText(Charsets.UTF_8) }.getOrNull()
                val envelope = raw?.let { runCatching { JSONObject(it) }.getOrNull() }
                if (envelope == null) {
                    // Corrupt bytes in the queue cannot be fixed by retrying, but
                    // they are archived rather than deleted.
                    archive(dir, quarantineDir(dir), file, "unreadable_envelope")
                        .fold(quarantineReasons, storageErrors)
                    continue
                }
                attempted += 1
                val batchId = envelope.optJSONObject("batch_row")?.optString("batch_id", "").orEmpty()
                when (uploader(envelope)) {
                    Outcome.ACKNOWLEDGED -> {
                        if (!acknowledge(filesDir, batchId)) {
                            storageErrors += "${file.name}:acknowledge_delete_failed"
                            break
                        }
                        acknowledged += 1
                    }
                    Outcome.QUARANTINE -> {
                        archive(dir, quarantineDir(dir), file, "permanent_rejection")
                            .fold(quarantineReasons, storageErrors)
                    }
                    Outcome.RETRY -> {
                        val attempts = bumpAttempts(dir, file)
                        if (attempts > DEFER_AFTER_ATTEMPTS) {
                            // R1: stop blocking, stay queued, keep being retried.
                            deferred += 1
                            continue
                        }
                        break // transient: preserve order and back off
                    }
                }
            }
            currentResult(
                filesDir, attempted, acknowledged, deferred,
                quarantineReasons, recovery.recovered, storageErrors
            )
        } finally {
            draining.set(false)
        }
    }

    /** Archived permanent rejections, retained for inspection and manual replay. */
    @Synchronized
    fun quarantinedFiles(filesDir: File): List<File> = jsonFiles(quarantineDir(directory(filesDir)))

    /** Orphaned temp bytes that could not be verified, retained for inspection. */
    @Synchronized
    fun recoveryFiles(filesDir: File): List<File> = jsonFiles(recoveryDir(directory(filesDir)))

    /** The persisted reason recorded beside an archived file, if any. */
    fun archivedReason(file: File): String? =
        File(file.parentFile, "${file.name}.reason")
            .takeIf { it.isFile }
            ?.let { runCatching { it.readText(Charsets.UTF_8).trim() }.getOrNull() }

    /**
     * Operator-driven replay: move every archived batch back into the queue.
     * Used after a backend-side fix; returns the number of files re-queued.
     */
    @Synchronized
    fun requeueQuarantined(filesDir: File): Int {
        val dir = directory(filesDir)
        var moved = 0
        for (file in quarantinedFiles(filesDir)) {
            val batchId = batchIdOf(file) ?: continue
            if (fileForBatch(dir, batchId) != null) continue
            val target = collisionSafeTarget(dir, "${System.currentTimeMillis()}-$batchId", "json")
            if (moveFile(file, target)) {
                File(file.parentFile, "${file.name}.reason").delete()
                moved += 1
            }
        }
        return moved
    }

    // ---- R3: orphaned temp recovery ------------------------------------------

    private class Recovery(val recovered: List<String>, val storageErrors: List<String>)

    /**
     * An enqueue writes the complete canonical envelope, fsyncs it, then renames.
     * A crash in between leaves a complete, recoverable envelope in a `.tmp`.
     * Verify it by digest and promote it; archive anything unverifiable. Age is
     * never on its own a reason to discard unacknowledged evidence.
     */
    private fun recoverOrphanedTempFiles(dir: File): Recovery {
        val recovered = mutableListOf<String>()
        val errors = mutableListOf<String>()
        val cutoff = System.currentTimeMillis() - TMP_SETTLE_MS
        val temps = dir.listFiles { f -> f.isFile && f.extension == "tmp" }
            ?.filter { it.lastModified() < cutoff }
            ?.sortedBy { it.name }
            .orEmpty()
        for (temp in temps) {
            val verdict = verifyOrphan(dir, temp)
            when (verdict.action) {
                OrphanAction.PROMOTE -> {
                    val target = collisionSafeTarget(
                        dir, "${temp.lastModified()}-${verdict.batchId}", "json"
                    )
                    if (moveFile(temp, target)) recovered += "${verdict.batchId}:promoted"
                    else errors += "${temp.name}:promote_move_failed"
                }
                OrphanAction.ARCHIVE -> {
                    val base = verdict.batchId ?: temp.nameWithoutExtension
                    archiveAs(dir, recoveryDir(dir), temp, base, verdict.reason)
                        .fold(recovered, errors)
                }
            }
        }
        return Recovery(recovered, errors)
    }

    private enum class OrphanAction { PROMOTE, ARCHIVE }

    private class OrphanVerdict(
        val action: OrphanAction,
        val reason: String,
        val batchId: String?
    )

    private fun verifyOrphan(dir: File, temp: File): OrphanVerdict {
        val raw = runCatching { temp.readText(Charsets.UTF_8) }.getOrNull()
            ?: return OrphanVerdict(OrphanAction.ARCHIVE, "unreadable_temp", null)
        val envelope = runCatching { JSONObject(raw) }.getOrNull()
            ?: return OrphanVerdict(OrphanAction.ARCHIVE, "partial_or_invalid_json", null)
        // Byte-equality with the re-canonicalised parse proves the write completed.
        if (Pc2CompactBatch.canonicalJson(envelope) != raw) {
            return OrphanVerdict(OrphanAction.ARCHIVE, "not_canonical_complete", null)
        }
        val batchRow = envelope.optJSONObject("batch_row")
            ?: return OrphanVerdict(OrphanAction.ARCHIVE, "missing_batch_row", null)
        val batchId = batchRow.optString("batch_id", "")
        if (!batchId.matches(Regex("[0-9a-f]{64}"))) {
            return OrphanVerdict(OrphanAction.ARCHIVE, "invalid_batch_id", null)
        }
        val digestOk = runCatching {
            val reconstructed = Pc2CompactBatch.reconstructOrderedDecisions(batchRow)
            Pc2CompactBatch.digestOrderedDecisions(reconstructed) ==
                batchRow.optString("decision_digest")
        }.getOrDefault(false)
        if (!digestOk) return OrphanVerdict(OrphanAction.ARCHIVE, "digest_mismatch", batchId)

        val queued = fileForBatch(dir, batchId)
        if (queued != null) {
            val queuedRaw = runCatching { queued.readText(Charsets.UTF_8) }.getOrNull()
            return if (queuedRaw == raw) {
                OrphanVerdict(OrphanAction.ARCHIVE, "duplicate_of_pending", batchId)
            } else {
                OrphanVerdict(OrphanAction.ARCHIVE, "conflicts_with_pending", batchId)
            }
        }
        return OrphanVerdict(OrphanAction.PROMOTE, "verified_complete", batchId)
    }

    // ---- R2: checked, collision-safe archiving --------------------------------

    private class ArchiveOutcome(val ok: Boolean, val note: String) {
        fun fold(success: MutableList<String>, failure: MutableList<String>) {
            if (ok) success += note else failure += note
        }
    }

    private fun archive(dir: File, targetDir: File, file: File, reason: String): ArchiveOutcome {
        val base = batchIdOf(file) ?: file.nameWithoutExtension
        return archiveAs(dir, targetDir, file, base, reason)
    }

    /**
     * R2: never overwrite an earlier archive, never report an unmoved file as
     * archived, and keep the attempt marker until the move has actually
     * succeeded so a storage failure is recoverable rather than silent.
     */
    private fun archiveAs(
        dir: File,
        targetDir: File,
        file: File,
        base: String,
        reason: String
    ): ArchiveOutcome {
        val stamp = file.lastModified().takeIf { it > 0 } ?: System.currentTimeMillis()
        val target = collisionSafeTarget(targetDir, "$stamp-$base", "json")
        if (!moveFile(file, target)) {
            return ArchiveOutcome(false, "${file.name}:archive_move_failed:$reason")
        }
        // Persist the reason beside the retained bytes: it must survive a restart
        // and log rotation, so a log line alone is not enough.
        runCatching {
            File(targetDir, "${target.name}.reason").writeText(
                "$reason at=${System.currentTimeMillis()} from=${file.name}",
                Charsets.UTF_8
            )
        }
        attemptsFile(dir, file).delete()
        return ArchiveOutcome(true, "$base:$reason")
    }

    private fun collisionSafeTarget(dir: File, base: String, extension: String): File {
        var candidate = File(dir, "$base.$extension")
        var suffix = 1
        while (candidate.exists()) {
            candidate = File(dir, "$base~$suffix.$extension")
            suffix += 1
        }
        return candidate
    }

    private fun writeAtomically(dir: File, name: String, content: String) {
        val target = File(dir, name)
        val temp = File(dir, "${name.substringBeforeLast('.')}.${System.nanoTime()}.tmp")
        FileOutputStream(temp).use { stream ->
            stream.write(content.toByteArray(Charsets.UTF_8))
            stream.fd.sync()
        }
        check(temp.renameTo(target)) { "PC2 outbox atomic rename failed for $name" }
    }

    private fun bumpAttempts(dir: File, file: File): Int {
        val marker = attemptsFile(dir, file)
        val current = runCatching { marker.readText(Charsets.UTF_8).trim().toInt() }.getOrDefault(0)
        val next = current + 1
        runCatching { marker.writeText(next.toString(), Charsets.UTF_8) }
        return next
    }

    internal fun attemptsOf(filesDir: File, batchId: String): Int {
        val dir = directory(filesDir)
        val file = fileForBatch(dir, batchId) ?: return 0
        return runCatching {
            attemptsFile(dir, file).readText(Charsets.UTF_8).trim().toInt()
        }.getOrDefault(0)
    }

    private fun attemptsFile(dir: File, file: File): File = File(dir, "${file.name}.attempts")

    private fun currentResult(
        filesDir: File,
        attempted: Int,
        acknowledged: Int,
        deferred: Int,
        quarantineReasons: List<String>,
        recoveredReasons: List<String>,
        storageErrorReasons: List<String>
    ): DrainResult {
        val remaining = pendingFiles(filesDir)
        return DrainResult(
            attempted = attempted,
            acknowledged = acknowledged,
            quarantined = quarantineReasons.size,
            pending = remaining.size,
            pendingBytes = remaining.sumOf { it.length() },
            quarantinedTotal = quarantinedFiles(filesDir).size,
            recoveredTotal = recoveryFiles(filesDir).size,
            deferred = deferred,
            storageErrors = storageErrorReasons.size,
            quarantineReasons = quarantineReasons,
            recoveredReasons = recoveredReasons,
            storageErrorReasons = storageErrorReasons
        )
    }

    private fun directory(filesDir: File): File = File(filesDir, DIRECTORY).apply {
        check(exists() || mkdirs()) { "Could not create PC2 outbox" }
    }

    private fun quarantineDir(dir: File): File = File(dir, QUARANTINE).apply {
        check(exists() || mkdirs()) { "Could not create PC2 outbox quarantine" }
    }

    private fun recoveryDir(dir: File): File = File(dir, RECOVERY).apply {
        check(exists() || mkdirs()) { "Could not create PC2 outbox recovery" }
    }

    private fun jsonFiles(dir: File): List<File> =
        dir.listFiles { f -> f.isFile && f.extension == "json" }?.sortedBy { it.name }.orEmpty()

    /** Chronological by filename prefix; legacy `<batchId>.json` files sort first and still drain. */
    private fun pendingFiles(filesDir: File): List<File> = directory(filesDir)
        .listFiles { file -> file.isFile && file.extension == "json" }
        ?.sortedBy { sortKey(it) }
        .orEmpty()

    private fun sortKey(file: File): String {
        val name = file.name
        val dash = name.indexOf('-')
        if (dash <= 0) return "0000000000000-$name" // legacy name: drain first
        val millis = name.substring(0, dash).toLongOrNull() ?: return "0000000000000-$name"
        return "%013d-%s".format(millis, name.substring(dash + 1))
    }

    private fun batchIdOf(file: File): String? {
        val base = file.nameWithoutExtension.substringBefore('~')
        val candidate = base.substringAfterLast('-', base)
        return candidate.takeIf { it.matches(Regex("[0-9a-f]{64}")) }
    }

    private fun fileForBatch(dir: File, batchId: String): File? =
        dir.listFiles { f -> f.isFile && f.extension == "json" }
            ?.firstOrNull { batchIdOf(it) == batchId }
}
