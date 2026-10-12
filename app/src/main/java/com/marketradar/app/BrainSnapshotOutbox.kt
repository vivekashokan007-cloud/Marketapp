package com.marketradar.app

import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest

/** A frozen, retryable copy of each snapshot until Supabase acknowledges its insert. */
object BrainSnapshotOutbox {
    private const val DIRECTORY = "brain_snapshot_outbox_v1"
    private val POLL_TS = Regex("^20[0-9]{2}-[0-9]{2}-[0-9]{2}T[^ ]{8,40}$")

    data class Status(val attempted: Int, val acknowledged: Int, val pending: Int,
                      val pendingBytes: Long, val faults: List<String>)

    private fun dir(filesDir: File) = File(filesDir, DIRECTORY)
    private fun sha(bytes: ByteArray) = MessageDigest.getInstance("SHA-256")
        .digest(bytes).joinToString("") { "%02x".format(it) }
    private fun fileFor(filesDir: File, pollTs: String): File =
        File(dir(filesDir), sha(pollTs.toByteArray(Charsets.UTF_8)) + ".json")

    /** Freeze sanitized bytes before the first network call. Never replace an existing poll. */
    @Synchronized
    fun enqueue(filesDir: File, snapshot: JSONObject): Boolean {
        val pollTs = snapshot.optString("poll_ts", "")
        require(POLL_TS.matches(pollTs)) { "Invalid snapshot poll timestamp" }
        val payload = BrainSnapshotPayloadSanitizer.sanitize(snapshot).toString()
        val target = fileFor(filesDir, pollTs)
        if (target.exists()) {
            check(target.readText(Charsets.UTF_8) == payload) { "Snapshot outbox poll collision" }
            return false
        }
        val folder = dir(filesDir)
        check(folder.isDirectory || folder.mkdirs()) { "Cannot create snapshot outbox" }
        val tmp = File(folder, target.name + ".tmp")
        // An interrupted enqueue leaves an inspectable temp file. It is never silently overwritten.
        check(!tmp.exists()) { "Incomplete snapshot outbox enqueue retained" }
        FileOutputStream(tmp).use { output ->
            output.write(payload.toByteArray(Charsets.UTF_8))
            output.fd.sync()
        }
        check(tmp.renameTo(target)) { "Could not commit snapshot outbox entry" }
        return true
    }

    @Synchronized
    fun isPending(filesDir: File, pollTs: String): Boolean = fileFor(filesDir, pollTs).exists()

    /** Retry oldest first. A failed upload remains frozen for a later poll or process start. */
    @Synchronized
    fun drain(filesDir: File, maxAttempts: Int = 3, upload: (String) -> Boolean): Status {
        val folder = dir(filesDir)
        val faults = mutableListOf<String>()
        folder.listFiles()?.filter { it.name.endsWith(".json.tmp") }?.forEach { tmp ->
            // Do not inspect a write still in progress. A prior process's frozen
            // temp is recoverable when its payload and content-bound name agree.
            if (System.currentTimeMillis() - tmp.lastModified() < 60_000L) return@forEach
            val bytes = runCatching { tmp.readText(Charsets.UTF_8) }.getOrNull()
            val ts = bytes?.let { runCatching { JSONObject(it).optString("poll_ts") }.getOrNull() }
            val target = ts?.takeIf { POLL_TS.matches(it) }?.let { fileFor(filesDir, it) }
            if (target == null || tmp.name != target.name + ".tmp") {
                faults += "invalid_temp:${tmp.name.take(64)}"
            } else if (target.exists()) {
                if (runCatching { target.readText(Charsets.UTF_8) == bytes }.getOrDefault(false)) {
                    if (!tmp.delete()) faults += "redundant_temp_delete_failed:${tmp.name.take(64)}"
                } else faults += "temp_conflicts_with_pending:${tmp.name.take(64)}"
            } else if (!tmp.renameTo(target)) {
                faults += "temp_recovery_failed:${tmp.name.take(64)}"
            }
        }
        val files = folder.listFiles()?.filter { it.name.endsWith(".json") }?.sortedWith(
            compareBy<File> { it.lastModified() }.thenBy { it.name }
        ).orEmpty()
        folder.listFiles()?.filter { it.name.endsWith(".tmp") }?.forEach {
            faults += "incomplete_enqueue:${it.name.take(64)}"
        }
        var attempted = 0
        var acknowledged = 0
        for (file in files) {
            if (attempted >= maxAttempts) break
            val payload = runCatching { file.readText(Charsets.UTF_8) }.getOrNull()
            val pollTs = payload?.let { runCatching { JSONObject(it).optString("poll_ts") }.getOrNull() }
            if (pollTs == null || !POLL_TS.matches(pollTs) || fileFor(filesDir, pollTs).name != file.name) {
                faults += "invalid_entry:${file.name.take(64)}"
                continue
            }
            attempted++
            if (!upload(payload)) {
                // Move a failed poll behind untried work so a permanently failing
                // entry cannot prevent later snapshots from being attempted.
                if (!file.setLastModified(System.currentTimeMillis())) {
                    faults += "retry_order_write_failed:${file.name.take(64)}"
                }
                continue
            }
            if (file.delete()) acknowledged++ else faults += "ack_delete_failed:${file.name.take(64)}"
        }
        val remaining = folder.listFiles()?.filter { it.name.endsWith(".json") }.orEmpty()
        return Status(attempted, acknowledged, remaining.size, remaining.sumOf { it.length() }, faults)
    }
}
