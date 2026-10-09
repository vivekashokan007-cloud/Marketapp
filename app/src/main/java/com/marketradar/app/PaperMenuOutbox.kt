package com.marketradar.app

import org.json.JSONObject
import java.io.File

/**
 * R2: durable per-menu outbox for Paper decision-menu evidence (CAPTURED menus and MISSED_SESSION manifests).
 *
 * Layout (one directory, e.g. filesDir/paper_decision_menus):
 *   <menu_id>.json           frozen CAPTURED payload bytes (written once, never rewritten)
 *   <menu_id>.missed.json    frozen MISSED_SESSION manifest bytes (written once, never rewritten)
 *   <menu_id>.outbox.json    per-menu delivery state (the only mutable file)
 *
 * Rules
 *  - Bytes are frozen at enqueue; payload_sha256 is the SHA-256 of exactly those bytes and every retry sends them.
 *  - Each menu has its own attempts/state/ack; nothing is keyed to "today" or "previous session", so any number of
 *    pending sessions survive rollover and process restarts (state lives only in files).
 *  - Only an explicit server acknowledgement moves an entry to ACKED. Only ACKED entries older than the retention
 *    window are pruned. EXHAUSTED / CONFLICT / MISMATCH / REJECTED / LOCAL_CORRUPT entries are retained and
 *    reported by [summary]; EXHAUSTED entries get one new recovery round per new session date.
 *  - Evidence only. Nothing here is read by ranking, gates, sizing, exits or primary selection.
 */
object PaperMenuOutbox {
    const val SCHEMA = "paper_menu_outbox_v1"
    const val MAX_ATTEMPTS_PER_ROUND = 12
    const val KIND_CAPTURED = "CAPTURED"
    const val KIND_MISSED = "MISSED"

    const val NOT_STARTED = "NOT_STARTED"
    const val RETRY_PENDING = "RETRY_PENDING"
    const val ACKED = "ACKED"
    const val EXHAUSTED = "EXHAUSTED"
    const val CONFLICT_RETAINED = "CONFLICT_RETAINED"
    const val MISMATCH_RETAINED = "MISMATCH_RETAINED"
    const val REJECTED_RETAINED = "REJECTED_RETAINED"
    const val LOCAL_CORRUPT_RETAINED = "LOCAL_CORRUPT_RETAINED"

    private val PENDING = setOf(NOT_STARTED, RETRY_PENDING)

    /** Server disposition from the uploader (SupabaseClient.PaperMenuUploadResult.disposition). */
    data class Delivery(val disposition: String, val outcome: String, val detail: String? = null)

    data class Entry(val menuId: String, val kind: String, val payloadSha256: String, val payloadFile: File, val state: JSONObject)

    data class DrainSummary(val attempted: Int, val acknowledged: Int, val retryPending: Int, val exhausted: Int,
                            val retained: Int, val requeued: Int)

    private fun stateFile(dir: File, menuId: String) = File(dir, "$menuId.outbox.json")
    private fun payloadFile(dir: File, menuId: String, kind: String) =
        File(dir, if (kind == KIND_MISSED) "$menuId.missed.json" else "$menuId.json")

    private fun atomicWrite(target: File, text: String): Boolean = runCatching {
        target.parentFile?.mkdirs()
        val tmp = File(target.parentFile, target.name + ".tmp")
        tmp.writeText(text, Charsets.UTF_8)
        if (!tmp.renameTo(target)) { tmp.delete(); false } else true
    }.getOrDefault(false)

    fun readState(dir: File, menuId: String): JSONObject? =
        stateFile(dir, menuId).takeIf { it.isFile }?.let { f -> runCatching { JSONObject(f.readText(Charsets.UTF_8)) }.getOrNull() }

    private fun enqueue(dir: File, menuId: String, kind: String, bytes: String, nowMs: Long, sessionDate: String): JSONObject? {
        readState(dir, menuId)?.let { existing ->
            // Freeze once: an existing entry (either kind) is never replaced. A CAPTURED menu wins over a later
            // MISSED for the same menu id only if CAPTURED was enqueued first; the reverse cannot happen because
            // capture refuses non-OPEN sessions.
            return existing
        }
        val pf = payloadFile(dir, menuId, kind)
        if (pf.isFile) {
            // Orphan payload from an interrupted enqueue: keep its bytes (they were frozen first).
            val text = runCatching { pf.readText(Charsets.UTF_8) }.getOrNull() ?: return null
            return writeNewState(dir, menuId, kind, pf, PaperMenuCapture.sha256Hex(text), nowMs, sessionDate)
        }
        if (!atomicWrite(pf, bytes)) return null
        return writeNewState(dir, menuId, kind, pf, PaperMenuCapture.sha256Hex(bytes), nowMs, sessionDate)
    }

    private fun writeNewState(dir: File, menuId: String, kind: String, pf: File, sha: String, nowMs: Long, sessionDate: String): JSONObject? {
        val st = JSONObject()
            .put("schema", SCHEMA).put("menu_id", menuId).put("kind", kind).put("session_date", sessionDate)
            .put("payload_file", pf.name).put("payload_sha256", sha)
            .put("state", NOT_STARTED).put("attempts", 0).put("total_attempts", 0).put("recovery_rounds", 0)
            .put("enqueued_at_ms", nowMs)
        return if (atomicWrite(stateFile(dir, menuId), st.toString())) st else null
    }

    /** Freezes a CAPTURED payload. Returns the entry state (existing one if already enqueued), or null on I/O failure. */
    fun enqueueCaptured(dir: File, menuId: String, sessionDate: String, payload: String, nowMs: Long): JSONObject? =
        enqueue(dir, menuId, KIND_CAPTURED, payload, nowMs, sessionDate)

    /** Freezes a MISSED_SESSION manifest. Returns the entry state, or null on I/O failure. */
    fun enqueueMissed(dir: File, missedManifest: JSONObject, nowMs: Long): JSONObject? =
        enqueue(dir, missedManifest.getString("menu_id"), KIND_MISSED, missedManifest.toString(), nowMs,
            missedManifest.optString("session_date"))

    fun entries(dir: File): List<Entry> {
        val files = dir.listFiles { f -> f.name.endsWith(".outbox.json") } ?: return emptyList()
        return files.mapNotNull { f ->
            val st = runCatching { JSONObject(f.readText(Charsets.UTF_8)) }.getOrNull() ?: return@mapNotNull null
            Entry(st.optString("menu_id"), st.optString("kind"), st.optString("payload_sha256"),
                File(dir, st.optString("payload_file")), st)
        }.sortedWith(compareBy({ it.state.optString("session_date") }, { it.state.optLong("enqueued_at_ms") }, { it.menuId }))
    }

    /**
     * Sends every pending entry oldest-first. [upload] receives the frozen bytes (verified against the frozen sha).
     * [sessionDate] (IST) grants EXHAUSTED entries one new recovery round per new session.
     */
    fun drain(dir: File, sessionDate: String, nowMs: Long, upload: (Entry, String) -> Delivery): DrainSummary {
        var attempted = 0; var acked = 0; var requeued = 0
        for (e in entries(dir)) {
            val st = e.state
            if (st.optString("state") == EXHAUSTED && st.optString("last_recovery_session", st.optString("exhausted_session")) < sessionDate) {
                st.put("state", RETRY_PENDING).put("attempts", 0).put("recovery_rounds", st.optInt("recovery_rounds") + 1)
                    .put("last_recovery_session", sessionDate)
                atomicWrite(stateFile(dir, e.menuId), st.toString())
                requeued++
            }
            if (st.optString("state") !in PENDING) continue
            val text = if (e.payloadFile.isFile) runCatching { e.payloadFile.readText(Charsets.UTF_8) }.getOrNull() else null
            if (text == null || PaperMenuCapture.sha256Hex(text) != e.payloadSha256) {
                st.put("state", LOCAL_CORRUPT_RETAINED).put("last_outcome", if (text == null) "FROZEN_FILE_MISSING" else "FROZEN_FILE_SHA_MISMATCH")
                    .put("last_at_ms", nowMs)
                atomicWrite(stateFile(dir, e.menuId), st.toString())
                continue
            }
            attempted++
            val d = runCatching { upload(e, text) }.getOrElse { Delivery("RETRY", "EXCEPTION:${it.javaClass.simpleName}") }
            st.put("attempts", st.optInt("attempts") + 1).put("total_attempts", st.optInt("total_attempts") + 1)
                .put("last_outcome", d.outcome).put("last_detail", d.detail ?: JSONObject.NULL).put("last_at_ms", nowMs)
            st.put("state", when (d.disposition) {
                "ACKNOWLEDGED" -> { acked++; st.put("acked_at_ms", nowMs); ACKED }
                "CONFLICT" -> CONFLICT_RETAINED
                "MISMATCH" -> MISMATCH_RETAINED
                "REJECTED_PERMANENT" -> REJECTED_RETAINED
                else -> if (st.optInt("attempts") >= MAX_ATTEMPTS_PER_ROUND) {
                    st.put("exhausted_session", sessionDate).put("last_recovery_session", sessionDate); EXHAUSTED
                } else RETRY_PENDING
            })
            atomicWrite(stateFile(dir, e.menuId), st.toString())
        }
        val all = entries(dir).map { it.state.optString("state") }
        return DrainSummary(attempted, acked, all.count { it in PENDING }, all.count { it == EXHAUSTED },
            all.count { it !in PENDING && it != ACKED && it != EXHAUSTED }, requeued)
    }

    /** Deletes only ACKED entries (state + payload) acknowledged before nowMs - retentionMs. Returns removed menu ids. */
    fun prune(dir: File, nowMs: Long, retentionMs: Long): List<String> {
        val removed = mutableListOf<String>()
        for (e in entries(dir)) {
            if (e.state.optString("state") != ACKED) continue
            if (e.state.optLong("acked_at_ms", Long.MAX_VALUE) >= nowMs - retentionMs) continue
            // payload first: a crash between the two deletes leaves an ACKED state without payload, never the reverse.
            if (e.payloadFile.exists() && !e.payloadFile.delete()) continue
            if (stateFile(dir, e.menuId).delete()) removed.add(e.menuId)
        }
        return removed
    }

    /** Observable recovery summary (logged and stored for the PWA/diagnostics). */
    fun summary(dir: File): JSONObject {
        val es = entries(dir)
        val byState = JSONObject()
        es.groupBy { it.state.optString("state") }.forEach { (k, v) -> byState.put(k, v.size) }
        val unacked = es.filter { it.state.optString("state") != ACKED }
        return JSONObject().put("schema", SCHEMA).put("total", es.size).put("by_state", byState)
            .put("unacked_menu_ids", org.json.JSONArray(unacked.map { "${it.menuId}:${it.state.optString("state")}" }))
            .put("oldest_unacked_session", unacked.minOfOrNull { it.state.optString("session_date") } ?: JSONObject.NULL)
    }
}
