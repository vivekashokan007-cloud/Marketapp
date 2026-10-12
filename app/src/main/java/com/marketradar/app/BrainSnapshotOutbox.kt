package com.marketradar.app

import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest
import java.util.concurrent.atomic.AtomicBoolean

/** Frozen snapshots awaiting acknowledgement; archived failures always retain their bytes. */
object BrainSnapshotOutbox {
    private const val DIRECTORY = "brain_snapshot_outbox_v1"
    private const val QUARANTINE = "quarantine"
    private const val MAX_PENDING = 150
    private const val MAX_PENDING_BYTES = 200L * 1024 * 1024
    private const val MAX_RETRYABLE_IN_A_ROW = 2
    private val POLL_TS = Regex("^20[0-9]{2}-[0-9]{2}-[0-9]{2}T[^ ]{8,40}$")
    private val draining = AtomicBoolean(false)
    private var inFlightName: String? = null // guarded by this monitor

    enum class Outcome { ACKNOWLEDGED, RETRY, QUARANTINE }
    /** Invalid snapshot payloads are set aside; auth, throttle and outages may recover. */
    fun classifyHttpFailure(code: Int?): Outcome =
        if (code != null && code in 400..499 && code !in listOf(401, 403, 408, 429)) {
            Outcome.QUARANTINE
        } else Outcome.RETRY
    data class Status(
        val attempted: Int, val acknowledged: Int, val pending: Int,
        val pendingBytes: Long, val faults: List<String>,
        val quarantined: Int = 0, val quarantinedTotal: Int = 0,
        val quarantinedBytes: Long = 0,
        val retried: Int = 0, val stopReason: String = "queue_empty",
        val acknowledgedPolls: Set<String> = emptySet()
    )

    private fun dir(filesDir: File) = File(filesDir, DIRECTORY)
    private fun sha(bytes: ByteArray) = MessageDigest.getInstance("SHA-256")
        .digest(bytes).joinToString("") { "%02x".format(it) }
    private fun fileFor(filesDir: File, pollTs: String): File =
        File(dir(filesDir), sha(pollTs.toByteArray(Charsets.UTF_8)) + ".json")
    private fun active(folder: File): List<File> = folder.listFiles()
        ?.filter { it.isFile && it.name.endsWith(".json") }
        ?.sortedWith(compareBy<File> { it.lastModified() }.thenBy { it.name }).orEmpty()
    private fun archives(folder: File): List<File> = File(folder, QUARANTINE)
        .listFiles()?.filter { it.isFile && it.name.endsWith(".json") }.orEmpty()

    private fun archive(folder: File, file: File, reason: String): Boolean {
        val targetDir = File(folder, QUARANTINE)
        if (!targetDir.isDirectory && !targetDir.mkdirs()) return false
        val base = "${System.currentTimeMillis()}-${file.nameWithoutExtension}--$reason"
        var target = File(targetDir, "$base.json")
        var suffix = 1
        while (target.exists()) target = File(targetDir, "$base-${suffix++}.json")
        return file.renameTo(target)
    }

    /** Keep the newest entry available for immediate upload; archive old excess without deleting bytes. */
    private fun enforceCap(folder: File, newest: File, faults: MutableList<String>) {
        val queue = active(folder).toMutableList()
        var bytes = queue.sumOf { it.length() }
        for (old in queue.toList()) {
            if (queue.size <= MAX_PENDING && bytes <= MAX_PENDING_BYTES) break
            if (old == newest || old.name == inFlightName) continue
            val length = old.length()
            if (archive(folder, old, "capacity")) {
                queue.remove(old)
                bytes -= length
                faults += "capacity_archived:${old.name.take(64)}"
            } else faults += "capacity_archive_failed:${old.name.take(64)}"
        }
        if (queue.size > MAX_PENDING || bytes > MAX_PENDING_BYTES) faults += "capacity_still_exceeded"
    }

    /** Freeze sanitized bytes before any network call. A conflicting poll is retained separately. */
    @Synchronized
    fun enqueue(filesDir: File, snapshot: JSONObject): Boolean {
        val pollTs = snapshot.optString("poll_ts", "")
        require(POLL_TS.matches(pollTs)) { "Invalid snapshot poll timestamp" }
        val payload = BrainSnapshotPayloadSanitizer.sanitize(snapshot).toString()
        val target = fileFor(filesDir, pollTs)
        val folder = dir(filesDir)
        check(folder.isDirectory || folder.mkdirs()) { "Cannot create snapshot outbox" }
        if (target.exists()) {
            if (target.readText(Charsets.UTF_8) != payload) {
                val collision = File(folder, "${target.nameWithoutExtension}-${System.nanoTime()}.json")
                val collisionTmp = File(folder, collision.name + ".tmp")
                writeFully(collisionTmp, payload)
                check(collisionTmp.renameTo(collision) && archive(folder, collision, "poll_collision")) {
                    "Could not archive conflicting poll"
                }
                throw IllegalStateException("Snapshot outbox poll collision")
            }
            return false
        }
        val tmp = File(folder, target.name + ".tmp")
        check(!tmp.exists()) { "Incomplete snapshot outbox enqueue retained" }
        writeFully(tmp, payload)
        check(tmp.renameTo(target)) { "Could not commit snapshot outbox entry" }
        enforceCap(folder, target, mutableListOf()) // archives are counted in drain status
        return true
    }

    private fun writeFully(file: File, content: String) {
        FileOutputStream(file).use { output ->
            output.write(content.toByteArray(Charsets.UTF_8))
            output.fd.sync()
        }
    }

    @Synchronized
    fun isPending(filesDir: File, pollTs: String): Boolean = fileFor(filesDir, pollTs).exists()

    @Synchronized
    fun isUnacknowledged(filesDir: File, pollTs: String): Boolean {
        val key = fileFor(filesDir, pollTs).nameWithoutExtension
        return isPending(filesDir, pollTs) || archives(dir(filesDir)).any {
            it.name.substringAfter('-').startsWith(key)
        }
    }

    @Synchronized
    private fun status(filesDir: File, attempted: Int, acknowledged: Int, quarantined: Int,
                       retried: Int, faults: List<String>, stop: String, polls: Set<String>): Status {
        val folder = dir(filesDir)
        val remaining = active(folder)
        val retained = archives(folder)
        return Status(attempted, acknowledged, remaining.size, remaining.sumOf { it.length() },
            faults, quarantined, retained.size, retained.sumOf { it.length() }, retried, stop, polls)
    }

    /** Single-flight drain. Network I/O never holds the enqueue monitor. */
    fun drain(filesDir: File, maxAttempts: Int = 3, upload: (String) -> Outcome): Status {
        if (!draining.compareAndSet(false, true)) {
            return status(filesDir, 0, 0, 0, 0, emptyList(), "busy", emptySet())
        }
        try {
            val folder = dir(filesDir)
            val faults = mutableListOf<String>()
            val files = synchronized(this) {
                folder.listFiles()?.filter { it.isFile && it.name.endsWith(".json.tmp") }?.forEach { tmp ->
                    if (System.currentTimeMillis() - tmp.lastModified() < 60_000L) return@forEach
                    val bytes = runCatching { tmp.readText(Charsets.UTF_8) }.getOrNull()
                    val ts = bytes?.let { runCatching { JSONObject(it).optString("poll_ts") }.getOrNull() }
                    val target = ts?.takeIf { POLL_TS.matches(it) }?.let { fileFor(filesDir, it) }
                    if (target == null || tmp.name != target.name + ".tmp") faults += "invalid_temp:${tmp.name.take(64)}"
                    else if (target.exists()) {
                        if (runCatching { target.readText(Charsets.UTF_8) == bytes }.getOrDefault(false)) {
                            if (!tmp.delete()) faults += "redundant_temp_delete_failed:${tmp.name.take(64)}"
                        } else faults += "temp_conflicts_with_pending:${tmp.name.take(64)}"
                    } else if (!tmp.renameTo(target)) faults += "temp_recovery_failed:${tmp.name.take(64)}"
                }
                folder.listFiles()?.filter { it.isFile && it.name.endsWith(".tmp") }?.forEach {
                    faults += "incomplete_enqueue:${it.name.take(64)}"
                }
                active(folder)
            }
            var attempted = 0
            var acknowledged = 0
            var quarantined = 0
            var retried = 0
            var consecutiveRetry = 0
            var stop = "queue_empty"
            val ackPolls = mutableSetOf<String>()
            for (file in files) {
                if (attempted >= maxAttempts) { stop = "budget"; break }
                if (consecutiveRetry >= MAX_RETRYABLE_IN_A_ROW) { stop = "consecutive_retries"; break }
                val payload = runCatching { file.readText(Charsets.UTF_8) }.getOrNull()
                val pollTs = payload?.let { runCatching { JSONObject(it).optString("poll_ts") }.getOrNull() }
                if (payload == null || pollTs == null || !POLL_TS.matches(pollTs) || fileFor(filesDir, pollTs).name != file.name) {
                    synchronized(this) {
                        if (archive(folder, file, "invalid_entry")) quarantined++
                        else faults += "invalid_entry_archive_failed:${file.name.take(64)}"
                    }
                    continue
                }
                synchronized(this) { inFlightName = file.name }
                try {
                    attempted++
                    when (upload(payload)) {
                        Outcome.ACKNOWLEDGED -> synchronized(this) {
                            consecutiveRetry = 0
                            if (file.delete()) { acknowledged++; ackPolls += pollTs }
                            else faults += "ack_delete_failed:${file.name.take(64)}"
                        }
                        Outcome.QUARANTINE -> synchronized(this) {
                            consecutiveRetry = 0
                            if (archive(folder, file, "permanent_rejection")) quarantined++
                            else faults += "permanent_archive_failed:${file.name.take(64)}"
                        }
                        Outcome.RETRY -> synchronized(this) {
                            retried++
                            consecutiveRetry++
                            if (!file.setLastModified(System.currentTimeMillis())) faults += "retry_order_write_failed:${file.name.take(64)}"
                        }
                    }
                } finally { synchronized(this) { inFlightName = null } }
            }
            return status(filesDir, attempted, acknowledged, quarantined, retried, faults, stop, ackPolls)
        } finally { draining.set(false) }
    }
}
