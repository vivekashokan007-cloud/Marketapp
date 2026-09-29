package com.marketradar.app

import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Durable PC2 upload queue.
 *
 * Review correction C2/C3/C4 (2026-09-29):
 *  - Files are named `<epochMillis>-<batchId>.json` so the drain order is
 *    chronological. SHA-256 filenames sorted lexicographically are effectively
 *    random, so an early-sorting permanent failure could starve every later
 *    batch indefinitely.
 *  - A permanently rejected batch is moved to `quarantine/` and the drain
 *    continues. Only retryable outcomes stop the pass, so ordering and backoff
 *    are preserved for transient faults without head-of-line blocking.
 *  - A batch that exhausts [MAX_ATTEMPTS] retryable attempts is also
 *    quarantined, so no single file can block the queue forever.
 *  - Orphaned `.tmp` files (process death between write and rename) are swept.
 *
 * Nothing is ever silently discarded: quarantined evidence stays on disk and the
 * batch id and reason are returned in [DrainResult] for the caller to log. The
 * object stays free of Android dependencies so every rule above is unit-testable.
 */
object Pc2TelemetryOutbox {
    private const val DIRECTORY = "pc2_telemetry_outbox_v1"
    private const val QUARANTINE = "quarantine"
    private const val TMP_SWEEP_AGE_MS = 60L * 60L * 1000L
    internal const val MAX_ATTEMPTS = 20
    private val draining = AtomicBoolean(false)

    /** Upload result contract. RETRY keeps order; QUARANTINE never blocks the queue. */
    enum class Outcome { ACKNOWLEDGED, RETRY, QUARANTINE }

    /**
     * Review correction C3: classify an upload rejection. Retry only what a later
     * attempt can fix.
     *
     * A missing table is the documented pre-migration state, so its evidence must
     * stay queued. A unique violation is permanent for this exact payload: the
     * batch table also carries `unique (poll_ts, policy_hash,
     * authority_diagnostics_version)`, which is NOT the `on_conflict=batch_id`
     * target, so a second batch for the same poll with different decisions is
     * rejected forever and would otherwise stall every later batch.
     */
    fun classifyPostFailure(code: Int?, body: String): Outcome {
        val missingTable = body.contains("PGRST205") || body.contains("42P01")
        if (missingTable) return Outcome.RETRY
        val uniqueViolation = code == 409 || body.contains("23505")
        val transientHttp = code == null || code == 401 || code == 403 ||
            code == 408 || code == 429 || code >= 500
        return if (uniqueViolation || !transientHttp) Outcome.QUARANTINE else Outcome.RETRY
    }

    data class DrainResult(
        val attempted: Int,
        val acknowledged: Int,
        val quarantined: Int,
        val pending: Int,
        val pendingBytes: Long,
        val quarantinedTotal: Int,
        /** "<batchId>:<reason>" for each batch set aside during this pass. */
        val quarantineReasons: List<String> = emptyList()
    )

    @Synchronized
    fun enqueue(filesDir: File, built: Pc2CompactBatch.Built): Boolean {
        val envelope = built.envelope()
        val batchId = built.batchRow.getString("batch_id")
        require(batchId.matches(Regex("[0-9a-f]{64}"))) { "Invalid PC2 batch id" }
        val dir = directory(filesDir)
        sweepStaleTemp(dir)
        val canonical = Pc2CompactBatch.canonicalJson(envelope)

        val existingFile = fileForBatch(dir, batchId)
        if (existingFile != null) {
            val existing = runCatching { JSONObject(existingFile.readText(Charsets.UTF_8)) }.getOrNull()
            check(existing != null && Pc2CompactBatch.canonicalJson(existing) == canonical) {
                "PC2 outbox collision for $batchId"
            }
            return false
        }
        if (File(quarantineDir(dir), "$batchId.json").exists()) return false

        val target = File(dir, "${System.currentTimeMillis()}-$batchId.json")
        val temp = File(dir, "$batchId.${System.nanoTime()}.tmp")
        FileOutputStream(temp).use { stream ->
            stream.write(canonical.toByteArray(Charsets.UTF_8))
            stream.fd.sync()
        }
        check(temp.renameTo(target)) { "PC2 outbox atomic rename failed for $batchId" }
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
        attemptsFile(dir, file).delete()
        return file.delete()
    }

    fun drain(filesDir: File, uploader: (JSONObject) -> Outcome): DrainResult {
        if (!draining.compareAndSet(false, true)) return currentResult(filesDir, 0, 0, emptyList())
        return try {
            val dir = directory(filesDir)
            sweepStaleTemp(dir)
            var attempted = 0
            var acknowledged = 0
            val reasons = mutableListOf<String>()
            for (file in pendingFiles(filesDir)) {
                val envelope = runCatching { JSONObject(file.readText(Charsets.UTF_8)) }.getOrNull()
                if (envelope == null) {
                    // Unreadable evidence is never deleted; move it aside so the
                    // rest of the queue still drains.
                    reasons += quarantine(dir, file, "unreadable_envelope")
                    continue
                }
                attempted += 1
                val batchId = envelope.optJSONObject("batch_row")?.optString("batch_id", "").orEmpty()
                when (uploader(envelope)) {
                    Outcome.ACKNOWLEDGED -> {
                        if (!acknowledge(filesDir, batchId)) break
                        acknowledged += 1
                    }
                    Outcome.QUARANTINE -> {
                        reasons += quarantine(dir, file, "permanent_rejection")
                    }
                    Outcome.RETRY -> {
                        val attempts = bumpAttempts(dir, file)
                        if (attempts >= MAX_ATTEMPTS) {
                            reasons += quarantine(dir, file, "max_attempts_$attempts")
                            continue
                        }
                        break // transient: preserve order and back off
                    }
                }
            }
            currentResult(filesDir, attempted, acknowledged, reasons)
        } finally {
            draining.set(false)
        }
    }

    /** Quarantined batches are retained for inspection and manual replay. */
    @Synchronized
    fun quarantinedFiles(filesDir: File): List<File> =
        quarantineDir(directory(filesDir)).listFiles { f -> f.isFile && f.extension == "json" }
            ?.sortedBy { it.name }.orEmpty()

    private fun quarantine(dir: File, file: File, reason: String): String {
        val batchId = batchIdOf(file) ?: file.nameWithoutExtension
        val target = File(quarantineDir(dir), "$batchId.json")
        attemptsFile(dir, file).delete()
        // Rename, never delete: rejected evidence stays inspectable and replayable.
        file.renameTo(target)
        return "$batchId:$reason"
    }

    private fun bumpAttempts(dir: File, file: File): Int {
        val marker = attemptsFile(dir, file)
        val current = runCatching { marker.readText(Charsets.UTF_8).trim().toInt() }.getOrDefault(0)
        val next = current + 1
        runCatching { marker.writeText(next.toString(), Charsets.UTF_8) }
        return next
    }

    private fun attemptsFile(dir: File, file: File): File = File(dir, "${file.name}.attempts")

    private fun currentResult(
        filesDir: File,
        attempted: Int,
        acknowledged: Int,
        quarantineReasons: List<String>
    ): DrainResult {
        val remaining = pendingFiles(filesDir)
        return DrainResult(
            attempted = attempted,
            acknowledged = acknowledged,
            quarantined = quarantineReasons.size,
            pending = remaining.size,
            pendingBytes = remaining.sumOf { it.length() },
            quarantinedTotal = quarantinedFiles(filesDir).size,
            quarantineReasons = quarantineReasons
        )
    }

    private fun directory(filesDir: File): File = File(filesDir, DIRECTORY).apply {
        check(exists() || mkdirs()) { "Could not create PC2 outbox" }
    }

    private fun quarantineDir(dir: File): File = File(dir, QUARANTINE).apply {
        check(exists() || mkdirs()) { "Could not create PC2 outbox quarantine" }
    }

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
        val base = file.nameWithoutExtension
        val candidate = base.substringAfterLast('-', base)
        return candidate.takeIf { it.matches(Regex("[0-9a-f]{64}")) }
    }

    private fun fileForBatch(dir: File, batchId: String): File? =
        dir.listFiles { f -> f.isFile && f.extension == "json" }
            ?.firstOrNull { batchIdOf(it) == batchId }

    private fun sweepStaleTemp(dir: File) {
        val cutoff = System.currentTimeMillis() - TMP_SWEEP_AGE_MS
        dir.listFiles { f -> f.isFile && f.extension == "tmp" }
            ?.filter { it.lastModified() < cutoff }
            ?.forEach { it.delete() }
    }
}
