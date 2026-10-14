package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.time.Instant
import java.time.ZoneId

/**
 * Implied-EV v1.2 telemetry outbox (filesDir/ev_v12_outbox), Codex R7.
 *
 * One file per decision envelope / outcome amendment holding the EXACT text Python produced plus its sha256.
 * Responses are classified by [classify] (pure, pinned by PostgREST bodies captured from the scratch server):
 *   ACKED               200 ACK naming the same id AND sha256 -> removed, an .acked marker remembers the digest.
 *   CONFLICT            200 ACK status CONFLICT (the server kept and CHARGED the conflicting bytes) -> kept aside.
 *   PERMANENT_REJECTED  PT413 / 23514 / 22P02 (oversize, malformed, out-of-range) -> kept aside, never retried.
 *   RETRY_QUOTA         PT429 / HTTP 429 (receipt-day budget) -> retried after the next IST midnight.
 *   RETRY_PREREQUISITE  23503 (outcome before its decision is stored) -> retried after the decision.
 *   RETRY_TRANSPORT     network, 5xx, auth, unparseable or mismatched ACK -> exponential backoff.
 * A quota or prerequisite retry is NOT a conflict. Losses (age/attempt budget, outbox full) leave a .lost marker
 * that also blocks re-enqueue of the same id, and a bounded coverage record (coverage.json) so the loss is
 * visible. Finite: at most MAX_FILES files of every kind; markers are pruned oldest-first and by age.
 */
object EvTelemetryOutbox {
    const val KIND_DECISION = "decision"
    const val KIND_OUTCOME = "outcome"
    const val MAX_ENTRIES = 40
    const val MAX_FILES = 120
    const val MAX_ATTEMPTS = 60
    const val MAX_AGE_MS = 12L * 24 * 3600 * 1000
    const val MARKER_RETAIN_MS = MAX_AGE_MS * 3
    const val MAX_DECISION_BYTES = 24 * 1024
    const val MAX_OUTCOME_BYTES = 4 * 1024
    const val BACKOFF_BASE_MS = 60_000L
    const val BACKOFF_MAX_MS = 6L * 3600 * 1000
    const val PREREQUISITE_RETRY_MS = 10L * 60 * 1000
    const val COVERAGE_MAX_EVENTS = 30
    private val IST: ZoneId = ZoneId.of("Asia/Kolkata")
    private val lock = Any()
    private val CODE_FIELD = Regex("\"code\"\\s*:\\s*\"([^\"]+)\"")

    enum class Disposition { ACKED, CONFLICT, PERMANENT_REJECTED, RETRY_QUOTA, RETRY_PREREQUISITE, RETRY_TRANSPORT }

    data class Entry(val kind: String, val id: String, val text: String, val sha256: String, val attempts: Int,
                     val createdMs: Long, val nextAttemptMs: Long, val file: File)
    data class Ack(val status: String, val id: String?, val sha256: String?)
    data class Response(val httpCode: Int?, val body: String?)
    data class DrainResult(var attempted: Int = 0, var acked: Int = 0, var conflicts: Int = 0, var retry: Int = 0,
                           var quota: Int = 0, var prerequisite: Int = 0, var rejected: Int = 0, var dropped: Int = 0,
                           var deferred: Int = 0)

    fun sha256(text: String): String = MessageDigest.getInstance("SHA-256").digest(text.toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }

    private fun safe(id: String) = id.replace(Regex("[^A-Za-z0-9_.-]"), "_").take(100)
    private fun base(kind: String, id: String) = "${kind}_${safe(id)}"
    private fun isEntry(f: File) = f.name.endsWith(".json") && !f.name.contains(".conflict.") &&
        !f.name.contains(".rejected.") && f.name != "coverage.json"

    fun errorCodeOf(body: String?): String? = body?.let { CODE_FIELD.find(it)?.groupValues?.get(1) }

    /** Pure response classification (see the class comment). */
    fun classify(kind: String, entry: Entry, resp: Response?): Disposition {
        if (resp == null || resp.httpCode == null) return Disposition.RETRY_TRANSPORT
        val code = resp.httpCode
        if (code in 200..299) {
            val ack = parseAck(resp.body, kind)
            return when {
                ackMatches(entry, ack) -> Disposition.ACKED
                ack?.status == "CONFLICT" && ack.id == entry.id -> Disposition.CONFLICT
                else -> Disposition.RETRY_TRANSPORT
            }
        }
        return when (errorCodeOf(resp.body)) {
            "PT429" -> Disposition.RETRY_QUOTA
            "23503" -> Disposition.RETRY_PREREQUISITE
            "PT413", "23514", "22P02", "22023" -> Disposition.PERMANENT_REJECTED
            else -> if (code == 429) Disposition.RETRY_QUOTA else Disposition.RETRY_TRANSPORT
        }
    }

    /** When to try again (pure). */
    fun nextAttemptMs(d: Disposition, attempts: Int, nowMs: Long): Long = when (d) {
        Disposition.RETRY_QUOTA -> Instant.ofEpochMilli(nowMs).atZone(IST).toLocalDate().plusDays(1)
            .atTime(0, 5).atZone(IST).toInstant().toEpochMilli()
        Disposition.RETRY_PREREQUISITE -> nowMs + PREREQUISITE_RETRY_MS
        else -> nowMs + minOf(BACKOFF_MAX_MS, BACKOFF_BASE_MS shl minOf(attempts, 12))
    }

    /** Enqueue exact bytes; refuses a digest that does not match the bytes or an oversize text. Idempotent. */
    fun enqueue(dir: File, kind: String, id: String, text: String, declaredSha: String, nowMs: Long): String =
        synchronized(lock) {
            val bytes = text.toByteArray(Charsets.UTF_8).size
            val cap = if (kind == KIND_DECISION) MAX_DECISION_BYTES else MAX_OUTCOME_BYTES
            if (bytes > cap) return "REFUSED_OVERSIZE"
            if (sha256(text) != declaredSha) return "REFUSED_DIGEST_MISMATCH"
            dir.mkdirs()
            val b = base(kind, id)
            val f = File(dir, "$b.json")
            val done = File(dir, "$b.acked")
            if (done.exists() && done.readText().trim() == declaredSha) return "ALREADY_ACKED"
            if (File(dir, "$b.lost").exists()) return "ALREADY_LOST"
            if (File(dir, "$b.rejected.json").exists()) return "ALREADY_REJECTED"
            if (f.exists()) {
                val cur = try { JSONObject(f.readText()) } catch (e: Exception) { null }
                if (cur?.optString("sha256") == declaredSha) return "ALREADY_QUEUED"
                if (File(dir, "$b.conflict.local.json").exists()) return "LOCAL_CONFLICT_ALREADY_KEPT"
                // a different text for the same id is never silently replaced
                writeAtomic(File(dir, "$b.conflict.local.json"),
                    JSONObject().put("id", id).put("sha256", declaredSha).put("text", text).toString())
                return "LOCAL_CONFLICT_KEPT_ASIDE"
            }
            val all = dir.listFiles() ?: emptyArray()
            if (all.count { isEntry(it) } >= MAX_ENTRIES || all.size >= MAX_FILES) {
                pruneMarkers(dir, nowMs, force = true)
                val after = dir.listFiles() ?: emptyArray()
                if (after.count { isEntry(it) } >= MAX_ENTRIES || after.size >= MAX_FILES) {
                    noteCoverage(dir, "OUTBOX_FULL", kind, id, nowMs)
                    return "REFUSED_OUTBOX_FULL"
                }
            }
            writeAtomic(f, JSONObject().put("kind", kind).put("id", id).put("text", text).put("sha256", declaredSha)
                .put("attempts", 0).put("created_ms", nowMs).put("next_attempt_ms", 0L).toString())
            return "QUEUED"
        }

    private fun writeAtomic(f: File, text: String) {
        val tmp = File(f.parentFile, f.name + ".tmp")
        tmp.writeText(text)
        if (!tmp.renameTo(f)) { f.delete(); tmp.renameTo(f) }
    }

    fun entries(dir: File): List<Entry> = synchronized(lock) {
        (dir.listFiles { x -> isEntry(x) } ?: emptyArray())
            .mapNotNull { f ->
                try {
                    val o = JSONObject(f.readText())
                    Entry(o.getString("kind"), o.getString("id"), o.getString("text"), o.getString("sha256"),
                        o.optInt("attempts"), o.optLong("created_ms"), o.optLong("next_attempt_ms"), f)
                } catch (e: Exception) { null }
            }.sortedWith(compareBy({ if (it.kind == KIND_DECISION) 0 else 1 }, { it.createdMs }))
    }

    fun ackMatches(e: Entry, ack: Ack?): Boolean =
        ack != null && (ack.status == "ACCEPTED" || ack.status == "DUPLICATE") && ack.id == e.id && ack.sha256 == e.sha256

    private fun lose(dir: File, e: Entry, reason: String, nowMs: Long) {
        val b = e.file.name.removeSuffix(".json")
        File(dir, "$b.lost").writeText(JSONObject().put("sha256", e.sha256).put("reason", reason).toString())
        e.file.delete()
        noteCoverage(dir, reason, e.kind, e.id, nowMs)
    }

    /** deliver returns the raw HTTP response (null on a network failure). */
    fun drain(dir: File, nowMs: Long, deliver: (Entry) -> Response?): DrainResult {
        val r = DrainResult()
        for (e in entries(dir)) {
            if (nowMs - e.createdMs > MAX_AGE_MS || e.attempts >= MAX_ATTEMPTS) {
                synchronized(lock) { lose(dir, e, if (e.attempts >= MAX_ATTEMPTS) "ATTEMPTS_EXHAUSTED" else "AGE_EXCEEDED", nowMs) }
                r.dropped++
                continue
            }
            if (e.nextAttemptMs > nowMs) { r.deferred++; continue }
            r.attempted++
            val resp = try { deliver(e) } catch (x: Exception) { null }
            val d = classify(e.kind, e, resp)
            synchronized(lock) {
                val b = e.file.name.removeSuffix(".json")
                when (d) {
                    Disposition.ACKED -> {
                        File(dir, "$b.acked").writeText(e.sha256); e.file.delete(); r.acked++
                    }
                    Disposition.CONFLICT -> {
                        e.file.renameTo(File(dir, "$b.conflict.server.json")); r.conflicts++
                        noteCoverage(dir, "SERVER_CONFLICT", e.kind, e.id, nowMs)
                    }
                    Disposition.PERMANENT_REJECTED -> {
                        val o = JSONObject(e.file.readText()).put("rejected_code", errorCodeOf(resp?.body) ?: JSONObject.NULL)
                        writeAtomic(File(dir, "$b.rejected.json"), o.toString()); e.file.delete(); r.rejected++
                        noteCoverage(dir, "PERMANENT_REJECTED:" + (errorCodeOf(resp?.body) ?: "?"), e.kind, e.id, nowMs)
                    }
                    else -> {
                        val o = JSONObject(e.file.readText()).put("attempts", e.attempts + 1).put("last_attempt_ms", nowMs)
                            .put("last_disposition", d.name).put("next_attempt_ms", nextAttemptMs(d, e.attempts, nowMs))
                        writeAtomic(e.file, o.toString())
                        when (d) { Disposition.RETRY_QUOTA -> r.quota++; Disposition.RETRY_PREREQUISITE -> r.prerequisite++; else -> r.retry++ }
                    }
                }
            }
        }
        synchronized(lock) { pruneMarkers(dir, nowMs, force = false) }
        return r
    }

    private fun isMarker(f: File) = !isEntry(f) && f.name != "coverage.json" && !f.name.endsWith(".tmp")

    /** Age-prune markers; when [force], also drop the oldest markers until under MAX_FILES - MAX_ENTRIES. */
    private fun pruneMarkers(dir: File, nowMs: Long, force: Boolean) {
        val markers = (dir.listFiles() ?: emptyArray()).filter { isMarker(it) }
        markers.filter { nowMs - it.lastModified() > MARKER_RETAIN_MS }.forEach { it.delete() }
        val rest = (dir.listFiles() ?: emptyArray()).filter { isMarker(it) }.sortedBy { it.lastModified() }
        val cap = MAX_FILES - MAX_ENTRIES - 1
        if (force || rest.size > cap) rest.take(maxOf(0, rest.size - cap)).forEach { it.delete() }
    }

    /** Bounded coverage state: counts per reason plus the newest COVERAGE_MAX_EVENTS events. */
    fun noteCoverage(dir: File, reason: String, kind: String, id: String, nowMs: Long) {
        val f = File(dir, "coverage.json")
        val doc = try { if (f.exists()) JSONObject(f.readText()) else JSONObject() } catch (e: Exception) { JSONObject() }
        val counts = doc.optJSONObject("counts") ?: JSONObject()
        counts.put(reason, counts.optInt(reason) + 1)
        val ev = doc.optJSONArray("events") ?: JSONArray()
        ev.put(JSONObject().put("reason", reason).put("kind", kind).put("id", id.take(100)).put("ms", nowMs))
        val keep = JSONArray()
        for (i in maxOf(0, ev.length() - COVERAGE_MAX_EVENTS) until ev.length()) keep.put(ev.get(i))
        writeAtomic(f, JSONObject().put("counts", counts).put("events", keep).toString())
    }

    fun coverage(dir: File): JSONObject = try {
        val f = File(dir, "coverage.json"); if (f.exists()) JSONObject(f.readText()) else JSONObject()
    } catch (e: Exception) { JSONObject() }

    fun parseAck(body: String?, kind: String): Ack? {
        if (body.isNullOrBlank()) return null
        return try {
            val o = JSONObject(body)
            Ack(o.optString("status"), o.optString("decision_id").ifBlank { null },
                (if (kind == KIND_DECISION) o.optString("envelope_sha256") else o.optString("outcome_sha256")).ifBlank { null })
        } catch (e: Exception) { null }
    }
}
