package com.marketradar.app

import org.json.JSONObject
import java.io.File
import java.security.MessageDigest

/**
 * Implied-EV v1.2 telemetry outbox (filesDir/ev_v12_outbox). One file per decision envelope or outcome
 * amendment holding the EXACT text Python produced plus its sha256. An entry is removed only when the server
 * ACK names the same id AND the same sha256 (ev_submit_decision / ev_submit_outcome); a CONFLICT is kept
 * aside (never retried); transport failures are retried with a finite attempt budget. Finite: at most
 * MAX_ENTRIES files; entries older than MAX_AGE_MS are dropped (recorded in the summary).
 */
object EvTelemetryOutbox {
    const val KIND_DECISION = "decision"
    const val KIND_OUTCOME = "outcome"
    const val MAX_ENTRIES = 40
    const val MAX_ATTEMPTS = 60
    const val MAX_AGE_MS = 12L * 24 * 3600 * 1000
    const val MAX_DECISION_BYTES = 24 * 1024
    const val MAX_OUTCOME_BYTES = 4 * 1024
    private val lock = Any()

    data class Entry(val kind: String, val id: String, val text: String, val sha256: String, val attempts: Int,
                     val createdMs: Long, val file: File)
    data class Ack(val status: String, val id: String?, val sha256: String?)
    data class DrainResult(var attempted: Int = 0, var acked: Int = 0, var conflicts: Int = 0, var retry: Int = 0,
                           var dropped: Int = 0)

    fun sha256(text: String): String = MessageDigest.getInstance("SHA-256").digest(text.toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }

    private fun safe(id: String) = id.replace(Regex("[^A-Za-z0-9_.-]"), "_").take(100)

    /** Enqueue exact bytes; refuses a digest that does not match the bytes or an oversize text. Idempotent. */
    fun enqueue(dir: File, kind: String, id: String, text: String, declaredSha: String, nowMs: Long): String =
        synchronized(lock) {
            val bytes = text.toByteArray(Charsets.UTF_8).size
            val cap = if (kind == KIND_DECISION) MAX_DECISION_BYTES else MAX_OUTCOME_BYTES
            if (bytes > cap) return "REFUSED_OVERSIZE"
            if (sha256(text) != declaredSha) return "REFUSED_DIGEST_MISMATCH"
            dir.mkdirs()
            val f = File(dir, "${kind}_${safe(id)}.json")
            val done = File(dir, "${kind}_${safe(id)}.acked")
            if (done.exists() && done.readText().trim() == declaredSha) return "ALREADY_ACKED"
            if (f.exists()) {
                val cur = try { JSONObject(f.readText()) } catch (e: Exception) { null }
                if (cur?.optString("sha256") == declaredSha) return "ALREADY_QUEUED"
                // a different text for the same id is never silently replaced
                File(dir, "${kind}_${safe(id)}.conflict.${nowMs}.json").writeText(
                    JSONObject().put("id", id).put("sha256", declaredSha).put("text", text).toString())
                return "LOCAL_CONFLICT_KEPT_ASIDE"
            }
            if ((dir.listFiles { x -> x.name.endsWith(".json") && !x.name.contains(".conflict.") }?.size ?: 0) >= MAX_ENTRIES)
                return "REFUSED_OUTBOX_FULL"
            val tmp = File(dir, f.name + ".tmp")
            tmp.writeText(JSONObject().put("kind", kind).put("id", id).put("text", text).put("sha256", declaredSha)
                .put("attempts", 0).put("created_ms", nowMs).toString())
            tmp.renameTo(f)
            return "QUEUED"
        }

    fun entries(dir: File): List<Entry> = synchronized(lock) {
        (dir.listFiles { x -> x.name.endsWith(".json") && !x.name.contains(".conflict.") } ?: emptyArray())
            .mapNotNull { f ->
                try {
                    val o = JSONObject(f.readText())
                    Entry(o.getString("kind"), o.getString("id"), o.getString("text"), o.getString("sha256"),
                        o.optInt("attempts"), o.optLong("created_ms"), f)
                } catch (e: Exception) { null }
            }.sortedWith(compareBy({ if (it.kind == KIND_DECISION) 0 else 1 }, { it.createdMs }))
    }

    fun ackMatches(e: Entry, ack: Ack?): Boolean =
        ack != null && (ack.status == "ACCEPTED" || ack.status == "DUPLICATE") && ack.id == e.id && ack.sha256 == e.sha256

    /** deliver returns the parsed server ACK, or null on a transport/server failure (retried). */
    fun drain(dir: File, nowMs: Long, deliver: (Entry) -> Ack?): DrainResult {
        val r = DrainResult()
        for (e in entries(dir)) {
            if (nowMs - e.createdMs > MAX_AGE_MS || e.attempts >= MAX_ATTEMPTS) {
                synchronized(lock) { e.file.renameTo(File(dir, e.file.name.removeSuffix(".json") + ".conflict.dropped.json")) }
                r.dropped++
                continue
            }
            r.attempted++
            val ack = try { deliver(e) } catch (x: Exception) { null }
            synchronized(lock) {
                when {
                    ackMatches(e, ack) -> {
                        File(dir, e.file.name.removeSuffix(".json") + ".acked").writeText(e.sha256)
                        e.file.delete(); r.acked++
                    }
                    ack?.status == "CONFLICT" -> {
                        e.file.renameTo(File(dir, e.file.name.removeSuffix(".json") + ".conflict.server.json")); r.conflicts++
                    }
                    else -> {
                        val o = JSONObject(e.file.readText()).put("attempts", e.attempts + 1).put("last_attempt_ms", nowMs)
                        e.file.writeText(o.toString()); r.retry++
                    }
                }
            }
        }
        pruneMarkers(dir, nowMs)
        return r
    }

    private fun pruneMarkers(dir: File, nowMs: Long) {
        (dir.listFiles() ?: emptyArray()).filter { !it.name.endsWith(".json") || it.name.contains(".conflict.") }
            .filter { nowMs - it.lastModified() > MAX_AGE_MS * 3 }.forEach { it.delete() }
    }

    fun parseAck(body: String?, kind: String): Ack? {
        if (body.isNullOrBlank()) return null
        return try {
            val o = JSONObject(body)
            Ack(o.optString("status"), o.optString("decision_id").ifBlank { null },
                (if (kind == KIND_DECISION) o.optString("envelope_sha256") else o.optString("outcome_sha256")).ifBlank { null })
        } catch (e: Exception) { null }
    }
}
