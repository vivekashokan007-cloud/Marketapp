package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest
import java.util.concurrent.atomic.AtomicBoolean

/** Frozen generated-candidate rows. Uses the reviewed snapshot queue semantics in a separate directory. */
object GeneratedCandidateOutbox {
    private const val DIRECTORY = "generated_candidate_outbox_v1"
    private const val QUARANTINE = "quarantine"
    /** b512: bounded copies of envelopes acknowledged by reconciliation (not by a fresh insert). Never "unacknowledged". */
    private const val RECONCILED = "reconciled"
    private const val MAX_RECONCILED = 20
    /** b512: after this many failed attempts an envelope is reported STUCK and retried with backoff (never dropped). */
    const val STUCK_ATTEMPTS = 6
    private const val BACKOFF_BASE_MS = 2L * 60_000
    private const val BACKOFF_MAX_MS = 30L * 60_000
    private const val MAX_PENDING = 150
    private const val MAX_PENDING_BYTES = 200L * 1024 * 1024
    private const val MAX_RETRYABLE_IN_A_ROW = 2
    private val POLL_TS = Regex("^20[0-9]{2}-[0-9]{2}-[0-9]{2}T[^ ]{8,40}$")
    private val draining = AtomicBoolean(false)
    private var inFlightName: String? = null // guarded by this monitor

    /** CONFLICT (b512): same poll key holds different stored content; bytes are kept, nothing is overwritten. */
    enum class Outcome { ACKNOWLEDGED, RETRY, QUARANTINE, CONFLICT }
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
        val acknowledgedPolls: Set<String> = emptySet(),
        /** b512: one safe line per attempted/deferred envelope (identity, digest, counts, codes; no row values). */
        val envelopes: List<String> = emptyList(),
        val stuck: List<String> = emptyList(),
        val deferred: Int = 0,
        val conflicts: Int = 0
    )

    /** b512 identity of a frozen envelope: stable across restarts because it is derived only from the frozen bytes. */
    data class Identity(val pollTs: String, val sessionDate: String, val sha256: String, val rows: Int, val bytes: Int) {
        fun describe() = "poll=$pollTs session=$sessionDate sha256=${sha256.take(16)} rows=$rows bytes=$bytes"
    }

    fun identityOf(payload: String): Identity {
        val o = JSONObject(payload)
        val rows = o.optJSONArray("rows")
        val session = rows?.optJSONObject(0)?.optString("session_date", "")?.takeIf { it.isNotBlank() }
            ?: o.optString("poll_ts", "").take(10)
        val bytes = payload.toByteArray(Charsets.UTF_8)
        return Identity(o.optString("poll_ts", ""), session, sha(bytes), rows?.length() ?: 0, bytes.size)
    }

    private fun metaFor(file: File) = File(file.parentFile, file.nameWithoutExtension + ".meta")

    private fun readMeta(file: File): JSONObject =
        runCatching { JSONObject(metaFor(file).readText(Charsets.UTF_8)) }.getOrDefault(JSONObject())

    private fun writeMeta(file: File, meta: JSONObject) {
        val target = metaFor(file)
        val part = File(target.parentFile, target.name + "part") // not ".tmp": that suffix means an incomplete enqueue
        runCatching { writeFully(part, meta.toString()); if (!part.renameTo(target)) part.delete() }
    }

    fun backoffMs(attempts: Int): Long =
        if (attempts < STUCK_ATTEMPTS) 0L
        else minOf(BACKOFF_MAX_MS, BACKOFF_BASE_MS shl minOf(8, attempts - STUCK_ATTEMPTS))

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
        val meta = metaFor(file)
        val moved = file.renameTo(target)
        if (moved && meta.exists()) meta.renameTo(File(targetDir, target.nameWithoutExtension + ".meta"))
        return moved
    }

    /** Ack after reconciliation: keep a bounded copy of the frozen bytes plus meta, then drop the oldest copies. */
    private fun keepReconciled(folder: File, file: File, state: String): Boolean {
        val targetDir = File(folder, RECONCILED)
        if (!targetDir.isDirectory && !targetDir.mkdirs()) return false
        val target = File(targetDir, "${System.currentTimeMillis()}-${file.nameWithoutExtension}--$state.json")
        val meta = metaFor(file)
        if (!file.renameTo(target)) return false
        if (meta.exists()) meta.renameTo(File(targetDir, target.nameWithoutExtension + ".meta"))
        targetDir.listFiles()?.filter { it.isFile && it.name.endsWith(".json") }?.sortedBy { it.name }
            ?.dropLast(MAX_RECONCILED)?.forEach { old -> old.delete(); File(targetDir, old.nameWithoutExtension + ".meta").delete() }
        return true
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
    fun enqueue(filesDir: File, snapshot: JSONObject): Boolean = enqueuePayload(filesDir, snapshot.toString())

    /** b503: freezes the exact cohort bytes built from the frozen fact pack (byte-identical on resume). */
    @Synchronized
    fun enqueuePayload(filesDir: File, payload: String): Boolean {
        val snapshot = JSONObject(payload)
        val rows = snapshot.getJSONArray("rows")
        require(rows.length() > 0) { "Empty generated-candidate envelope" }
        val pollTs = snapshot.optString("poll_ts", "")
        val ids = mutableSetOf<String>()
        for (i in 0 until rows.length()) {
            val row = rows.getJSONObject(i)
            require(row.optString("snapshot_poll_ts") == pollTs) { "Mixed generated-candidate polls" }
            require(row.optString("candidate_id").isNotBlank() && ids.add(row.optString("candidate_id"))) {
                "Missing or repeated generated candidate identity"
            }
        }
        require(POLL_TS.matches(pollTs)) { "Invalid snapshot poll timestamp" }
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

    /** b512: safe inventory of retained (pending) envelopes: identity + attempt meta; no row values. Bounded. */
    @Synchronized
    fun describeRetained(filesDir: File, limit: Int = 20): List<String> = active(dir(filesDir)).take(limit).map { f ->
        val id = runCatching { identityOf(f.readText(Charsets.UTF_8)).describe() }.getOrDefault("unreadable=${f.name.take(16)}")
        val m = readMeta(f)
        "$id attempts=${m.optInt("attempts", 0)} last=${m.optString("last_state", "none")} " +
            "http=${m.optInt("last_http", -1)} pg=${m.optString("last_pg", "")} ageMin=${(System.currentTimeMillis() - f.lastModified()) / 60_000}"
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

    /** Legacy shape: outcome-only uploads. */
    fun drain(filesDir: File, maxAttempts: Int = 3, upload: (String) -> Outcome): Status =
        drainDetailed(filesDir, maxAttempts, nowMs = System.currentTimeMillis()) { p ->
            val o = upload(p)
            GeneratedCandidateReplay.Attempt(o, when (o) {
                Outcome.ACKNOWLEDGED -> GeneratedCandidateReplay.State.INSERTED
                Outcome.QUARANTINE -> GeneratedCandidateReplay.State.PERMANENT_REJECTION
                Outcome.CONFLICT -> GeneratedCandidateReplay.State.CONFLICT
                Outcome.RETRY -> GeneratedCandidateReplay.State.TRANSIENT
            })
        }

    /** Single-flight drain. Network I/O never holds the enqueue monitor. */
    fun drainDetailed(filesDir: File, maxAttempts: Int = 3, nowMs: Long = System.currentTimeMillis(),
                      upload: (String) -> GeneratedCandidateReplay.Attempt): Status {
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
            var deferred = 0
            var conflicts = 0
            val ackPolls = mutableSetOf<String>()
            val lines = mutableListOf<String>()
            val stuck = mutableListOf<String>()
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
                val id = runCatching { identityOf(payload) }.getOrNull()
                val meta = readMeta(file)
                val priorAttempts = meta.optInt("attempts", 0)
                if (nowMs - meta.optLong("last_attempt_ms", 0L) < backoffMs(priorAttempts)) {
                    deferred++
                    lines += "deferred ${id?.describe()} attempts=$priorAttempts last=${meta.optString("last_state")}"
                    if (priorAttempts >= STUCK_ATTEMPTS) stuck += "${id?.describe()} attempts=$priorAttempts last=${meta.optString("last_state")} http=${meta.optString("last_http")} pg=${meta.optString("last_pg")}"
                    continue
                }
                synchronized(this) { inFlightName = file.name }
                try {
                    attempted++
                    val result = runCatching { upload(payload) }.getOrElse {
                        GeneratedCandidateReplay.Attempt(Outcome.RETRY, GeneratedCandidateReplay.State.TRANSIENT)
                    }
                    val attempts = priorAttempts + 1
                    if (id != null) {
                        if (!meta.has("first_seen_ms")) meta.put("first_seen_ms", nowMs)
                        meta.put("schema", "generated_outbox_meta_v1").put("poll_ts", id.pollTs).put("session_date", id.sessionDate)
                            .put("payload_sha256", id.sha256).put("rows", id.rows).put("bytes", id.bytes)
                    }
                    meta.put("attempts", attempts).put("last_attempt_ms", nowMs).put("last_state", result.state.name)
                        .put("last_http", result.http ?: -1).put("last_pg", result.pg ?: "")
                        .put("present", result.present).put("absent", result.absent).put("conflicting", result.conflicting)
                        .put("extra", result.extra)
                    lines += "attempt ${id?.describe()} attempts=$attempts ${result.describe()}"
                    when (result.outcome) {
                        Outcome.ACKNOWLEDGED -> synchronized(this) {
                            consecutiveRetry = 0
                            val ok = if (result.state == GeneratedCandidateReplay.State.INSERTED) {
                                file.delete().also { if (it) metaFor(file).delete() }
                            } else { writeMeta(file, meta); keepReconciled(folder, file, result.state.name) || file.delete() }
                            if (ok) { acknowledged++; ackPolls += pollTs }
                            else faults += "ack_delete_failed:${file.name.take(64)}"
                        }
                        Outcome.CONFLICT -> synchronized(this) {
                            consecutiveRetry = 0
                            conflicts++
                            writeMeta(file, meta.put("conflict_columns", JSONArray(result.conflictColumns)))
                            if (archive(folder, file, "content_conflict")) quarantined++
                            else faults += "conflict_archive_failed:${file.name.take(64)}"
                        }
                        Outcome.QUARANTINE -> synchronized(this) {
                            consecutiveRetry = 0
                            writeMeta(file, meta)
                            if (archive(folder, file, "permanent_rejection")) quarantined++
                            else faults += "permanent_archive_failed:${file.name.take(64)}"
                        }
                        Outcome.RETRY -> synchronized(this) {
                            retried++
                            consecutiveRetry++
                            writeMeta(file, meta)
                            if (attempts >= STUCK_ATTEMPTS) stuck += "${id?.describe()} attempts=$attempts ${result.describe()}"
                            if (!file.setLastModified(System.currentTimeMillis())) faults += "retry_order_write_failed:${file.name.take(64)}"
                        }
                    }
                } finally { synchronized(this) { inFlightName = null } }
            }
            return status(filesDir, attempted, acknowledged, quarantined, retried, faults, stop, ackPolls)
                .copy(envelopes = lines, stuck = stuck, deferred = deferred, conflicts = conflicts)
        } finally { draining.set(false) }
    }
}
