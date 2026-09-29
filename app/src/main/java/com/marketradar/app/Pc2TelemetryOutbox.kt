package com.marketradar.app

import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.util.concurrent.atomic.AtomicBoolean

/** Durable, unbounded PC2 upload queue. Files are removed only after readback verification. */
object Pc2TelemetryOutbox {
    private const val DIRECTORY = "pc2_telemetry_outbox_v1"
    private val draining = AtomicBoolean(false)

    data class DrainResult(
        val attempted: Int,
        val acknowledged: Int,
        val pending: Int,
        val pendingBytes: Long
    )

    @Synchronized
    fun enqueue(filesDir: File, built: Pc2CompactBatch.Built): Boolean {
        val envelope = built.envelope()
        val batchId = built.batchRow.getString("batch_id")
        require(batchId.matches(Regex("[0-9a-f]{64}"))) { "Invalid PC2 batch id" }
        val dir = directory(filesDir)
        val target = File(dir, "$batchId.json")
        val canonical = Pc2CompactBatch.canonicalJson(envelope)
        if (target.exists()) {
            val existing = runCatching { JSONObject(target.readText(Charsets.UTF_8)) }.getOrNull()
            check(existing != null && Pc2CompactBatch.canonicalJson(existing) == canonical) {
                "PC2 outbox collision for $batchId"
            }
            return false
        }
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
        val file = File(directory(filesDir), "$batchId.json")
        return !file.exists() || file.delete()
    }

    fun drain(filesDir: File, uploader: (JSONObject) -> Boolean): DrainResult {
        if (!draining.compareAndSet(false, true)) return currentResult(filesDir, 0, 0)
        return try {
            var attempted = 0
            var acknowledged = 0
            for (file in pendingFiles(filesDir)) {
                val envelope = runCatching { JSONObject(file.readText(Charsets.UTF_8)) }.getOrNull()
                    ?: break // retain corrupt/unreadable evidence; never silently discard it
                attempted += 1
                if (!uploader(envelope)) break
                val batchId = envelope.optJSONObject("batch_row")?.optString("batch_id", "").orEmpty()
                if (!acknowledge(filesDir, batchId)) break
                acknowledged += 1
            }
            currentResult(filesDir, attempted, acknowledged)
        } finally {
            draining.set(false)
        }
    }

    private fun currentResult(filesDir: File, attempted: Int, acknowledged: Int): DrainResult {
        val remaining = pendingFiles(filesDir)
        return DrainResult(
            attempted = attempted,
            acknowledged = acknowledged,
            pending = remaining.size,
            pendingBytes = remaining.sumOf { it.length() }
        )
    }

    private fun directory(filesDir: File): File = File(filesDir, DIRECTORY).apply {
        check(exists() || mkdirs()) { "Could not create PC2 outbox" }
    }

    private fun pendingFiles(filesDir: File): List<File> = directory(filesDir)
        .listFiles { file -> file.isFile && file.extension == "json" }
        ?.sortedBy { it.name }
        .orEmpty()
}
