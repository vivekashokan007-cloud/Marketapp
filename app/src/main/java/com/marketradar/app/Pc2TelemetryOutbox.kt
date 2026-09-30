package com.marketradar.app

import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Durable PC2 upload queue.
 *
 * The governing rule, unchanged since round 2:
 *
 *   **Evidence leaves the pending queue only when the backend has acknowledged
 *   it, or when the payload itself has been proven permanently unacceptable.
 *   Nothing is ever removed because it is old, because it has been tried many
 *   times, or because a temporary file looked abandoned.**
 *
 * Round 3 (Codex round-2 review) corrects how that rule is scheduled and
 * recorded:
 *
 *  - B2, fairness. Round 2 walked the queue in enqueue order and spent the
 *    per-pass budget on it, so 25 permanently-retrying batches starved every
 *    batch behind them forever (reproduced in this class: 1,000 passes, zero
 *    attempts on the tail). The queue is now served least-recently-attempted
 *    first, on a clock-independent attempt sequence persisted beside each
 *    batch. A retried batch moves to the back; a never-attempted batch goes
 *    first. A pass stops after [MAX_UPLOADS_PER_PASS] attempts or after
 *    [MAX_CONSECUTIVE_RETRIES] retryable failures in a row, which bounds the
 *    cost of an outage without letting any prefix monopolise the queue.
 *    Upload order is not a correctness property: every batch is independently
 *    addressed by its content-bound id.
 *  - C1, reason persistence. The archive reason is now part of the archive
 *    file name, so it is recorded by the same rename that retains the bytes and
 *    cannot be lost to a full disk independently of them. The `.reason` sidecar
 *    carries detail only; failing to write it is reported as a storage error
 *    and the sidecar is backfilled on a later pass.
 *  - Metadata writes (attempt sequence, sidecars) never fail silently: every
 *    failure is counted in [DrainResult.storageErrors].
 *
 * The object stays free of Android dependencies so every rule is unit-testable.
 */
object Pc2TelemetryOutbox {
    private const val DIRECTORY = "pc2_telemetry_outbox_v1"
    private const val QUARANTINE = "quarantine"
    private const val RECOVERY = "recovery"
    private const val SEQUENCE_FILE = "attempt_sequence"
    private const val RETRY_SUFFIX = ".retry"
    private const val REASON_SUFFIX = ".reason"
    private const val ARCHIVE_REASON_SEPARATOR = "--"

    /** A `.tmp` younger than this may still be mid-write by a live enqueue. */
    private const val TMP_SETTLE_MS = 60L * 1000L

    /** Bounded work per pass so a long queue cannot monopolise a poll. */
    internal const val MAX_UPLOADS_PER_PASS = 25

    /**
     * Retryable failures in a row that end a pass. A backend outage fails every
     * batch; three in a row is strong evidence of one, so the pass stops and the
     * next pass resumes further round the rotation instead of at the same place.
     */
    internal const val MAX_CONSECUTIVE_RETRIES = 3

    private val draining = AtomicBoolean(false)

    /** Highest attempt sequence seen by this process; rebuilt from disk on restart. */
    @Volatile
    private var memorySequence = 0L

    /**
     * Test seams. A JVM test running as root cannot provoke a refused rename or
     * a failed file write with permissions, so both operations are injectable.
     * Production always uses [File.renameTo] and [File.writeText].
     */
    internal var moveFile: (File, File) -> Boolean = { source, target -> source.renameTo(target) }
    internal var writeMetadata: (File, String) -> Boolean = { file, text ->
        runCatching { file.writeText(text, Charsets.UTF_8) }.isSuccess
    }

    /** Test-only: forget in-process state, as a process restart would. */
    internal fun simulateProcessRestart() {
        memorySequence = 0L
    }

    /** Upload result contract. RETRY keeps evidence queued; QUARANTINE is terminal. */
    enum class Outcome { ACKNOWLEDGED, RETRY, QUARANTINE }

    /**
     * PostgreSQL / PostgREST / ingestion codes that prove THIS payload can never
     * be accepted. Matched against the `code` field of the error body, not as a
     * free-text substring, so a digit sequence inside a message cannot turn a
     * recoverable failure into a permanent one.
     *
     * Deliberately absent, and therefore retried:
     *  - `PGRST205` / `42P01` / `PGRST202`: table or ingestion function not
     *    deployed yet - the documented pre-migration state;
     *  - `PGRST204`: column missing / stale schema cache;
     *  - `23503`: foreign key - the parent may simply not exist yet;
     *  - `PT403` (`PC2_DEVICE_NOT_AUTHORIZED`): recoverable by registering or
     *    re-activating the device;
     *  - `PT429` (`PC2_DAILY_QUOTA_EXCEEDED`): recoverable the next IST day;
     *  - every unclassified 4xx, 401/403/408/429 and 5xx, and transport failure.
     */
    private val PERMANENT_PAYLOAD_CODES = setOf(
        "23505",    // unique_violation
        "22P02",    // invalid_text_representation (includes invalid JSON)
        "22P05",    // untranslatable_character (e.g. \u0000 cast to jsonb)
        "22003",    // numeric_value_out_of_range
        "22001",    // string_data_right_truncation
        "23502",    // not_null_violation
        "23514",    // check_violation
        "PGRST102", // mixed key sets within a single bulk insert
        "PT413"     // PC2_PAYLOAD_TOO_LARGE from the ingestion function
    )

    private val CODE_FIELD = Regex("\"code\"\\s*:\\s*\"([^\"]+)\"")

    /** The PostgREST `code` field of an error body, if there is one. */
    fun errorCodeOf(body: String): String? = CODE_FIELD.find(body)?.groupValues?.get(1)

    /**
     * R1: the default is RETRY - a failure must prove itself permanent before
     * evidence is set aside. PostgREST surfaces a unique violation as 409 even
     * when the body is empty, so 409 alone is also permanent.
     */
    fun classifyPostFailure(code: Int?, body: String): Outcome {
        val errorCode = errorCodeOf(body)
        if (errorCode != null && errorCode in PERMANENT_PAYLOAD_CODES) return Outcome.QUARANTINE
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
        /** Retryable outcomes this pass; each moved its batch to the back. */
        val retried: Int = 0,
        val storageErrors: Int = 0,
        /** "<batchId>:<reason>" for each batch archived during this pass. */
        val quarantineReasons: List<String> = emptyList(),
        /** "<name>:<reason>" for each orphaned temp file resolved this pass. */
        val recoveredReasons: List<String> = emptyList(),
        /** "<name>:<fault>" for every file operation that did not complete. */
        val storageErrorReasons: List<String> = emptyList(),
        /** Why the pass ended: "queue_empty", "budget", or "consecutive_retries". */
        val stopReason: String = "queue_empty"
    )

    @Synchronized
    fun enqueue(filesDir: File, built: Pc2CompactBatch.Built): Boolean {
        val envelope = built.envelope()
        val batchId = built.batchRow.getString("batch_id")
        require(batchId.matches(BATCH_ID)) { "Invalid PC2 batch id" }
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
        // A previously archived batch is NOT refused: after a backend fix it gets
        // a fresh attempt, and the collision-safe archive keeps every rejection.
        writeAtomically(dir, "${System.currentTimeMillis()}-$batchId.json", canonical)
        return true
    }

    @Synchronized
    fun pending(filesDir: File): List<JSONObject> = pendingFiles(filesDir).mapNotNull { file ->
        runCatching { JSONObject(file.readText(Charsets.UTF_8)) }.getOrNull()
    }

    @Synchronized
    fun acknowledge(filesDir: File, batchId: String): Boolean {
        if (!batchId.matches(BATCH_ID)) return false
        val dir = directory(filesDir)
        val file = fileForBatch(dir, batchId) ?: return true
        val removed = file.delete()
        if (removed) retryFile(dir, file).delete()
        return removed
    }

    fun drain(filesDir: File, uploader: (JSONObject) -> Outcome): DrainResult {
        if (!draining.compareAndSet(false, true)) {
            return currentResult(filesDir, 0, 0, 0, emptyList(), emptyList(), emptyList(), "busy")
        }
        return try {
            val dir = directory(filesDir)
            val storageErrors = mutableListOf<String>()
            val recovery = recoverOrphanedTempFiles(dir)
            storageErrors += recovery.storageErrors
            storageErrors += backfillReasonSidecars(dir)

            val queue = scheduledQueue(dir)
            var attempted = 0
            var acknowledged = 0
            var retried = 0
            var consecutiveRetries = 0
            var stopReason = "queue_empty"
            val quarantineReasons = mutableListOf<String>()

            for (entry in queue) {
                if (attempted >= MAX_UPLOADS_PER_PASS) { stopReason = "budget"; break }
                if (consecutiveRetries >= MAX_CONSECUTIVE_RETRIES) {
                    stopReason = "consecutive_retries"; break
                }
                val file = entry.file
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
                        consecutiveRetries = 0
                        if (!acknowledge(filesDir, batchId)) {
                            storageErrors += "${file.name}:acknowledge_delete_failed"
                            continue
                        }
                        acknowledged += 1
                    }
                    Outcome.QUARANTINE -> {
                        consecutiveRetries = 0
                        archive(dir, quarantineDir(dir), file, "permanent_rejection")
                            .fold(quarantineReasons, storageErrors)
                    }
                    Outcome.RETRY -> {
                        retried += 1
                        consecutiveRetries += 1
                        // B2: move to the back of the rotation. The batch stays
                        // pending and is retried automatically, indefinitely.
                        recordRetry(dir, entry)?.let { storageErrors += it }
                    }
                }
            }
            currentResult(
                filesDir, attempted, acknowledged, retried,
                quarantineReasons, recovery.recovered, storageErrors, stopReason
            )
        } finally {
            draining.set(false)
        }
    }

    /** Archived permanent rejections, retained for inspection and manual replay. */
    @Synchronized
    fun quarantinedFiles(filesDir: File): List<File> = jsonFiles(quarantineDir(directory(filesDir)))

    /** Orphaned temp bytes that could not be promoted, retained for inspection. */
    @Synchronized
    fun recoveryFiles(filesDir: File): List<File> = jsonFiles(recoveryDir(directory(filesDir)))

    /**
     * The reason recorded for an archived file. The name always carries it (C1);
     * the sidecar, when present, adds when and from which file.
     */
    fun archivedReason(file: File): String? {
        val sidecar = File(file.parentFile, "${file.name}$REASON_SUFFIX")
        val detail = sidecar.takeIf { it.isFile }
            ?.let { runCatching { it.readText(Charsets.UTF_8).trim() }.getOrNull() }
        return detail?.takeIf { it.isNotEmpty() } ?: reasonFromArchiveName(file.name)
    }

    /** The reason encoded in an archive file name, independent of any sidecar. */
    fun reasonFromArchiveName(name: String): String? {
        val base = name.removeSuffix(".json").substringBefore('~')
        val at = base.indexOf(ARCHIVE_REASON_SEPARATOR)
        if (at < 0) return null
        return base.substring(at + ARCHIVE_REASON_SEPARATOR.length).takeIf { it.isNotEmpty() }
    }

    /**
     * Operator-driven replay: move every archived batch back into the queue.
     * Used after a backend-side fix; returns the number of files re-queued.
     */
    @Synchronized
    fun requeueQuarantined(filesDir: File): Int {
        val dir = directory(filesDir)
        var moved = 0
        for (file in quarantinedFiles(filesDir)) {
            val batchId = archiveBatchIdOf(file.name) ?: continue
            if (fileForBatch(dir, batchId) != null) continue
            val target = collisionSafeTarget(dir, "${System.currentTimeMillis()}-$batchId", "json")
            if (moveFile(file, target)) {
                File(file.parentFile, "${file.name}$REASON_SUFFIX").delete()
                moved += 1
            }
        }
        return moved
    }

    /** Diagnostic: retryable attempts recorded for a pending batch. */
    internal fun attemptsOf(filesDir: File, batchId: String): Int {
        val dir = directory(filesDir)
        val file = fileForBatch(dir, batchId) ?: return 0
        return readRetry(dir, file).attempts
    }

    // ---- B2: least-recently-attempted rotation ---------------------------------

    private data class RetryState(val attempts: Int, val lastSequence: Long)

    private class QueueEntry(val file: File, val state: RetryState, val order: String)

    /**
     * Never-attempted batches (sequence 0) first in enqueue order, then every
     * retried batch in the order it was last tried. The sequence is a counter,
     * not a clock, so a device clock change cannot reorder or starve anything.
     */
    private fun scheduledQueue(dir: File): List<QueueEntry> {
        val entries = pendingFilesIn(dir).map { file ->
            QueueEntry(file, readRetry(dir, file), sortKey(file))
        }
        // Self-healing: the next sequence is above every sequence on disk, so a
        // lost counter file can never send a just-retried batch to the front.
        val highestOnDisk = entries.maxOfOrNull { it.state.lastSequence } ?: 0L
        memorySequence = maxOf(memorySequence, highestOnDisk, readSequenceFile(dir))
        return entries.sortedWith(compareBy<QueueEntry>({ it.state.lastSequence }, { it.order }))
    }

    /** Returns a storage-error note when the rotation state could not be persisted. */
    private fun recordRetry(dir: File, entry: QueueEntry): String? {
        memorySequence += 1
        val next = memorySequence
        val state = RetryState(entry.state.attempts + 1, next)
        val retryOk = writeMetadata(
            retryFile(dir, entry.file),
            JSONObject().put("attempts", state.attempts).put("last_sequence", state.lastSequence).toString()
        )
        val sequenceOk = writeMetadata(File(dir, SEQUENCE_FILE), next.toString())
        return when {
            retryOk && sequenceOk -> null
            !retryOk -> "${entry.file.name}:retry_state_write_failed"
            else -> "$SEQUENCE_FILE:sequence_write_failed"
        }
    }

    private fun readRetry(dir: File, file: File): RetryState {
        val text = runCatching { retryFile(dir, file).readText(Charsets.UTF_8) }.getOrNull()
            ?: return RetryState(0, 0L)
        val json = runCatching { JSONObject(text) }.getOrNull() ?: return RetryState(0, 0L)
        return RetryState(json.optInt("attempts", 0), json.optLong("last_sequence", 0L))
    }

    private fun readSequenceFile(dir: File): Long =
        runCatching { File(dir, SEQUENCE_FILE).readText(Charsets.UTF_8).trim().toLong() }.getOrDefault(0L)

    private fun retryFile(dir: File, file: File): File = File(dir, "${file.name}$RETRY_SUFFIX")

    // ---- R3: orphaned temp recovery ---------------------------------------------

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
                    val base = verdict.batchId ?: temp.nameWithoutExtension.replace("-", "_")
                    archiveAs(dir, recoveryDir(dir), temp, base, verdict.reason)
                        .fold(recovered, errors)
                }
            }
        }
        return Recovery(recovered, errors)
    }

    private enum class OrphanAction { PROMOTE, ARCHIVE }

    private class OrphanVerdict(val action: OrphanAction, val reason: String, val batchId: String?)

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
        if (!batchId.matches(BATCH_ID)) {
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

    // ---- R2 / C1: checked, collision-safe, self-describing archives -------------

    private class ArchiveOutcome(val ok: Boolean, val note: String, val warnings: List<String>) {
        fun fold(success: MutableList<String>, failure: MutableList<String>) {
            if (ok) success += note else failure += note
            failure += warnings
        }
    }

    private fun archive(dir: File, targetDir: File, file: File, reason: String): ArchiveOutcome {
        val base = batchIdOf(file) ?: file.nameWithoutExtension.replace("-", "_")
        return archiveAs(dir, targetDir, file, base, reason)
    }

    /**
     * Never overwrite an earlier archive, never report an unmoved file as
     * archived, keep the retry state until the move has succeeded, and record
     * the reason in the name so it survives exactly as long as the bytes do.
     */
    private fun archiveAs(
        dir: File,
        targetDir: File,
        file: File,
        base: String,
        reason: String
    ): ArchiveOutcome {
        val stamp = file.lastModified().takeIf { it > 0 } ?: System.currentTimeMillis()
        val target = collisionSafeTarget(targetDir, "$stamp-$base$ARCHIVE_REASON_SEPARATOR$reason", "json")
        if (!moveFile(file, target)) {
            return ArchiveOutcome(false, "${file.name}:archive_move_failed:$reason", emptyList())
        }
        retryFile(dir, file).delete()
        val warnings = mutableListOf<String>()
        val detail = "$reason at=${System.currentTimeMillis()} from=${file.name}"
        if (!writeMetadata(File(targetDir, "${target.name}$REASON_SUFFIX"), detail)) {
            // C1: the reason is already durable in the name; the missing detail is
            // surfaced now and backfilled on a later pass.
            warnings += "${target.name}:reason_sidecar_write_failed"
        }
        return ArchiveOutcome(true, "$base:$reason", warnings)
    }

    /** C1 recovery: write any sidecar a full disk prevented earlier. */
    private fun backfillReasonSidecars(dir: File): List<String> {
        val errors = mutableListOf<String>()
        for (archiveDir in listOf(quarantineDir(dir), recoveryDir(dir))) {
            for (file in jsonFiles(archiveDir)) {
                val sidecar = File(archiveDir, "${file.name}$REASON_SUFFIX")
                if (sidecar.isFile) continue
                val reason = reasonFromArchiveName(file.name) ?: "unknown"
                if (!writeMetadata(sidecar, "$reason backfilled_at=${System.currentTimeMillis()}")) {
                    errors += "${file.name}:reason_sidecar_backfill_failed"
                }
            }
        }
        return errors
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

    private fun currentResult(
        filesDir: File,
        attempted: Int,
        acknowledged: Int,
        retried: Int,
        quarantineReasons: List<String>,
        recoveredReasons: List<String>,
        storageErrorReasons: List<String>,
        stopReason: String
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
            retried = retried,
            storageErrors = storageErrorReasons.size,
            quarantineReasons = quarantineReasons,
            recoveredReasons = recoveredReasons,
            storageErrorReasons = storageErrorReasons,
            stopReason = stopReason
        )
    }

    private val BATCH_ID = Regex("[0-9a-f]{64}")

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

    private fun pendingFiles(filesDir: File): List<File> = pendingFilesIn(directory(filesDir))

    /** Enqueue order by filename prefix; legacy `<batchId>.json` files sort first. */
    private fun pendingFilesIn(dir: File): List<File> =
        dir.listFiles { file -> file.isFile && file.extension == "json" }
            ?.sortedBy { sortKey(it) }
            .orEmpty()

    private fun sortKey(file: File): String {
        val name = file.name
        val dash = name.indexOf('-')
        if (dash <= 0) return "0000000000000-$name"
        val millis = name.substring(0, dash).toLongOrNull() ?: return "0000000000000-$name"
        return "%013d-%s".format(millis, name.substring(dash + 1))
    }

    /** Batch id of a pending file named `<millis>-<batchId>[~n].json`. */
    private fun batchIdOf(file: File): String? {
        val base = file.nameWithoutExtension.substringBefore('~')
        val candidate = base.substringAfterLast('-', base)
        return candidate.takeIf { it.matches(BATCH_ID) }
    }

    /** Batch id of an archive named `<stamp>-<batchId>--<reason>[~n].json`. */
    internal fun archiveBatchIdOf(name: String): String? {
        val base = name.removeSuffix(".json").substringBefore('~').substringBefore(ARCHIVE_REASON_SEPARATOR)
        val candidate = base.substringAfter('-', "")
        return candidate.takeIf { it.matches(BATCH_ID) }
    }

    private fun fileForBatch(dir: File, batchId: String): File? =
        dir.listFiles { f -> f.isFile && f.extension == "json" }
            ?.firstOrNull { batchIdOf(it) == batchId }
}
