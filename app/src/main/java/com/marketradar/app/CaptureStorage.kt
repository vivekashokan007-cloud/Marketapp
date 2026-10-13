package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.IOException

/**
 * File primitives with injectable faults (disk full, failed fsync, failed rename) so the
 * b503 journal and outboxes can be tested against the real code path.
 */
open class CaptureFs {
    /** Writes [content] to [tmp] and fsyncs it. Throws IOException on any failure. */
    open fun writeSynced(tmp: File, content: String) {
        FileOutputStream(tmp).use { out ->
            out.write(content.toByteArray(Charsets.UTF_8))
            out.fd.sync()
        }
    }
    open fun rename(from: File, to: File): Boolean = from.renameTo(to)
    open fun delete(file: File): Boolean = !file.exists() || file.delete()
    open fun read(file: File): String = file.readText(Charsets.UTF_8)

    /** tmp + fsync + rename. On failure the target is untouched and the tmp is removed if possible. */
    fun atomicWrite(target: File, content: String) {
        val dir = target.parentFile
        if (dir != null && !dir.isDirectory && !dir.mkdirs()) throw IOException("mkdir_failed:${dir.name}")
        val tmp = File(dir, target.name + ".tmp")
        try {
            writeSynced(tmp, content)
        } catch (e: Throwable) {
            runCatching { tmp.delete() }
            throw if (e is IOException) e else IOException("write_failed:${e.javaClass.simpleName}", e)
        }
        if (!rename(tmp, target)) {
            runCatching { tmp.delete() }
            throw IOException("rename_failed:${target.name}")
        }
    }

    companion object { val REAL = CaptureFs() }
}

/**
 * Generic frozen-evidence outbox (b503) for channels that had no restart-safe queue:
 * the retained legacy PC2 rows and the capture-event ledger. One file per stable key;
 * identical replay is a no-op; different bytes under the same key are archived as an
 * explicit collision and the enqueue fails. Quarantine/capacity archives are never deleted
 * and are reported separately from the active cap (they are NOT bounded by it).
 */
class FrozenEvidenceOutbox(
    private val root: File,
    private val directory: String,
    private val maxPending: Int,
    private val maxPendingBytes: Long,
    private val fs: CaptureFs = CaptureFs.REAL,
    private val maxRetryableInARow: Int = 2
) {
    enum class Outcome { ACKNOWLEDGED, RETRY, QUARANTINE }

    data class Status(
        val attempted: Int, val acknowledged: Int, val retried: Int, val quarantined: Int,
        val pending: Int, val pendingBytes: Long, val quarantinedTotal: Int, val quarantinedBytes: Long,
        val faults: List<String>, val stopReason: String, val acknowledgedKeys: Set<String>
    )

    private val folder get() = File(root, directory)
    private val quarantine get() = File(folder, "quarantine")
    @Volatile private var draining = false
    private val drainLock = Any()

    private fun fileFor(key: String) = File(folder, FrozenCaptureRequest.sha256Hex(key).take(40) + ".json")
    private fun active(): List<File> = folder.listFiles()?.filter { it.isFile && it.name.endsWith(".json") }
        ?.sortedWith(compareBy<File> { it.lastModified() }.thenBy { it.name }).orEmpty()
    private fun archives(): List<File> = quarantine.listFiles()?.filter { it.isFile }.orEmpty()

    private fun envelope(key: String, payload: String) = JSONObject()
        .put("schema", "frozen_evidence_v1").put("key", key)
        .put("payload_sha256", FrozenCaptureRequest.sha256Hex(payload)).put("payload", payload).toString()

    private fun archive(file: File, reason: String): Boolean {
        if (!quarantine.isDirectory && !quarantine.mkdirs()) return false
        var target = File(quarantine, "${System.currentTimeMillis()}-${file.nameWithoutExtension}--$reason.json")
        var n = 1
        while (target.exists()) target = File(quarantine, "${System.currentTimeMillis()}-${file.nameWithoutExtension}--$reason-${n++}.json")
        return fs.rename(file, target)
    }

    /** true = newly frozen; false = identical bytes already pending. Throws on collision/storage fault. */
    @Synchronized
    fun enqueue(key: String, payload: String): Boolean {
        require(key.isNotBlank() && key.length <= 512) { "invalid_key" }
        val target = fileFor(key)
        val body = envelope(key, payload)
        if (target.exists()) {
            val existing = runCatching { JSONObject(fs.read(target)) }.getOrNull()
            if (existing != null && existing.optString("payload") == payload) return false
            val collision = File(folder, "${target.nameWithoutExtension}-c${System.nanoTime()}.json")
            fs.atomicWrite(collision, body)
            if (!archive(collision, "collision")) throw IOException("collision_archive_failed")
            throw IllegalStateException("frozen_evidence_collision:$directory")
        }
        if (active().size >= maxPending || active().sumOf { it.length() } + body.length > maxPendingBytes) {
            // Never evict unacknowledged evidence: refuse visibly; the caller keeps its journal.
            throw IOException("capacity_exceeded:$directory")
        }
        fs.atomicWrite(target, body)
        return true
    }

    @Synchronized
    fun isPending(key: String): Boolean = fileFor(key).exists()

    @Synchronized
    fun isQuarantined(key: String): Boolean {
        val name = fileFor(key).nameWithoutExtension
        return archives().any { it.name.contains(name) }
    }

    fun status(faults: List<String> = emptyList(), stop: String = "status"): Status = synchronized(this) {
        val a = active(); val q = archives()
        Status(0, 0, 0, 0, a.size, a.sumOf { it.length() }, q.size, q.sumOf { it.length() }, faults, stop, emptySet())
    }

    fun drain(maxAttempts: Int = 6, upload: (key: String, payload: String) -> Outcome): Status {
        synchronized(drainLock) { if (draining) return status(stop = "busy"); draining = true }
        try {
            var attempted = 0; var acked = 0; var retried = 0; var quarantined = 0; var inARow = 0
            var stop = "queue_empty"
            val faults = mutableListOf<String>()
            val keys = mutableSetOf<String>()
            // Orphan temp files are reported (bytes retained), never silently deleted.
            folder.listFiles()?.filter { it.name.endsWith(".tmp") }?.forEach { faults += "orphan_temp:${it.name.take(48)}" }
            for (file in synchronized(this) { active() }) {
                if (attempted >= maxAttempts) { stop = "budget"; break }
                if (inARow >= maxRetryableInARow) { stop = "consecutive_retries"; break }
                val env = runCatching { JSONObject(fs.read(file)) }.getOrNull()
                val key = env?.optString("key").orEmpty()
                val payload = env?.optString("payload")
                if (env == null || payload == null || key.isBlank() || fileFor(key).name != file.name ||
                    FrozenCaptureRequest.sha256Hex(payload) != env.optString("payload_sha256")) {
                    synchronized(this) { if (archive(file, "invalid_entry")) quarantined++ else faults += "invalid_archive_failed" }
                    continue
                }
                attempted++
                val outcome = try { upload(key, payload) } catch (_: Throwable) { Outcome.RETRY }
                synchronized(this) {
                    when (outcome) {
                        Outcome.ACKNOWLEDGED -> { inARow = 0; if (fs.delete(file)) { acked++; keys += key } else faults += "ack_delete_failed" }
                        Outcome.QUARANTINE -> { inARow = 0; if (archive(file, "permanent_rejection")) quarantined++ else faults += "permanent_archive_failed" }
                        Outcome.RETRY -> { retried++; inARow++; file.setLastModified(System.currentTimeMillis()) }
                    }
                }
            }
            val s = status()
            return s.copy(attempted = attempted, acknowledged = acked, retried = retried, quarantined = quarantined,
                faults = faults, stopReason = stop, acknowledgedKeys = keys)
        } finally { synchronized(drainLock) { draining = false } }
    }
}

/**
 * Durable per-attempt capture journal (b503 section D).
 *
 *  <attempt>.intent.json  small metadata; atomically rewritten on every monotonic stage change.
 *  <attempt>.bundle.json  original built channel bytes (written once after build, digest-checked).
 *
 * Stages: INTENT(1) -> BUILT(2) -> FINAL(3) | TERMINAL(3). A stage never moves backwards.
 * Channels (per BUILT intent): local_cache, snapshot, compact, legacy, generated, each
 * PENDING | ENQUEUED | NOT_EXPECTED | COLLISION_RETAINED | FAILED_RETRYABLE.
 * The bundle is deleted only after every channel is ENQUEUED / NOT_EXPECTED /
 * COLLISION_RETAINED (durable recovery ownership or explicit persisted terminal state).
 */
class CaptureJournal(
    private val root: File,
    private val fs: CaptureFs = CaptureFs.REAL,
    val maxIntents: Int = 64,
    val maxBundleBytes: Long = 256L * 1024 * 1024
) {
    companion object {
        const val DIRECTORY = "capture_journal_v1"
        const val COMPLETED_KEYS_FILE = "completed_keys.json"
        const val MAX_COMPLETED_KEYS = 128
        val CHANNELS = listOf("local_cache", "snapshot", "compact", "legacy", "generated")
        val RESOLVED = setOf("ENQUEUED", "NOT_EXPECTED", "COLLISION_RETAINED")
        private val STAGE_RANK = mapOf("INTENT" to 1, "BUILT" to 2, "FINAL" to 3, "TERMINAL" to 3)
    }

    val dir: File get() = File(root, DIRECTORY)
    private fun intentFile(attemptId: String) = File(dir, "${safe(attemptId)}.intent.json")
    private fun bundleFile(attemptId: String) = File(dir, "${safe(attemptId)}.bundle.json")
    private fun safe(id: String): String = id.replace(Regex("[^A-Za-z0-9_-]"), "_").take(64)

    @Synchronized
    fun writeIntent(req: FrozenCaptureRequest, pid: Int) {
        val count = dir.listFiles()?.count { it.name.endsWith(".intent.json") } ?: 0
        if (count >= maxIntents) throw IOException("journal_capacity_exceeded:$count")
        val st = req.identityJson().put("schema", "capture_intent_v1").put("pid", pid)
            .put("stage", "INTENT").put("stage_seq", 1).put("channels", JSONObject()).put("events", JSONArray())
        fs.atomicWrite(intentFile(req.attemptId), st.toString())
    }

    @Synchronized
    fun read(attemptId: String): JSONObject? = runCatching { JSONObject(fs.read(intentFile(attemptId))) }.getOrNull()

    /** Monotonic stage update; refuses regressions (returns false). */
    @Synchronized
    fun update(attemptId: String, mutate: (JSONObject) -> Unit): Boolean {
        val f = intentFile(attemptId)
        val cur = runCatching { JSONObject(fs.read(f)) }.getOrNull() ?: return false
        val before = STAGE_RANK[cur.optString("stage")] ?: 0
        val next = JSONObject(cur.toString())
        mutate(next)
        val after = STAGE_RANK[next.optString("stage")] ?: 0
        if (after < before) return false
        next.put("stage_seq", cur.optInt("stage_seq") + 1)
        fs.atomicWrite(f, next.toString())
        return true
    }

    @Synchronized
    fun writeBundle(attemptId: String, channels: JSONObject): String {
        val existing = dir.listFiles()?.filter { it.name.endsWith(".bundle.json") }?.sumOf { it.length() } ?: 0L
        val body = JSONObject().put("schema", "capture_bundle_v1").put("attempt_id", attemptId).put("channels", channels).toString()
        if (existing + body.length > maxBundleBytes) throw IOException("bundle_capacity_exceeded")
        fs.atomicWrite(bundleFile(attemptId), body)
        return FrozenCaptureRequest.sha256Hex(body)
    }

    /** Bundle channels if present AND its digest matches the intent's recorded digest. */
    @Synchronized
    fun readBundle(attemptId: String, expectedSha: String?): JSONObject? {
        val f = bundleFile(attemptId)
        if (!f.isFile) return null
        val text = runCatching { fs.read(f) }.getOrNull() ?: return null
        if (expectedSha != null && FrozenCaptureRequest.sha256Hex(text) != expectedSha) return null
        return runCatching { JSONObject(text).getJSONObject("channels") }.getOrNull()
    }

    @Synchronized fun hasBundle(attemptId: String) = bundleFile(attemptId).isFile
    @Synchronized fun hasBundleTemp(attemptId: String) = File(dir, bundleFile(attemptId).name + ".tmp").exists()

    /** Deletes the bundle, then the intent. Called only once every channel is resolved. */
    @Synchronized
    fun finalizeAttempt(attemptId: String): Boolean {
        update(attemptId) { it.put("stage", "FINAL") }
        val b = fs.delete(bundleFile(attemptId))
        return b && fs.delete(intentFile(attemptId))
    }

    @Synchronized
    fun terminal(attemptId: String, reason: String): Boolean {
        val ok = update(attemptId) { it.put("stage", "TERMINAL").put("terminal_reason", reason) }
        return ok && fs.delete(intentFile(attemptId))
    }

    @Synchronized
    fun quarantine(file: File, reason: String): Boolean {
        val q = File(dir, "quarantine"); if (!q.isDirectory && !q.mkdirs()) return false
        return fs.rename(file, File(q, "${System.currentTimeMillis()}-${file.name}--$reason"))
    }

    @Synchronized
    fun intents(): List<File> = dir.listFiles()?.filter { it.isFile && it.name.endsWith(".intent.json") }
        ?.sortedBy { it.name }.orEmpty()

    @Synchronized
    fun attemptIdOf(file: File) = file.name.removeSuffix(".intent.json")

    data class Inventory(val intents: Int, val bundles: Int, val bundleBytes: Long, val quarantined: Int, val quarantinedBytes: Long)

    @Synchronized
    fun inventory(): Inventory {
        val files = dir.listFiles()?.filter { it.isFile }.orEmpty()
        val q = File(dir, "quarantine").listFiles()?.filter { it.isFile }.orEmpty()
        return Inventory(files.count { it.name.endsWith(".intent.json") },
            files.count { it.name.endsWith(".bundle.json") },
            files.filter { it.name.endsWith(".bundle.json") }.sumOf { it.length() }, q.size, q.sumOf { it.length() })
    }

    // ---- monotonic dedup state: a bounded set of recent completed origin keys ----
    @Synchronized
    fun completedKeys(): List<String> = runCatching {
        val a = JSONArray(fs.read(File(dir, COMPLETED_KEYS_FILE)))
        (0 until a.length()).map { a.getString(it) }
    }.getOrDefault(emptyList())

    @Synchronized
    fun markCompleted(key: String) {
        val keys = completedKeys().toMutableList()
        if (key in keys) return
        keys += key
        val bounded = keys.sortedWith(compareBy({ it.substringBefore('|') }, { slotRank(it) })).takeLast(MAX_COMPLETED_KEYS)
        fs.atomicWrite(File(dir, COMPLETED_KEYS_FILE), JSONArray(bounded).toString())
    }

    fun slotRank(key: String): Int = key.substringAfter('|', "").toIntOrNull() ?: 10_000
}
