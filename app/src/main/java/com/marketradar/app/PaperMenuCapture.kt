package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.security.MessageDigest

/**
 * Evidence batch 1 (Item B) once-per-session Paper decision-menu capture state machine (pure).
 *
 * Policy (HANDOFF_R2 decision 4): first successfully completed SCHEDULED Brain decision slot whose
 * scheduled start is in the inclusive 12:30-12:55 IST window (slot ordinals 40..45 of the 5-minute
 * grid from 09:15). The first successful menu is frozen; transport retries reuse that menu id and
 * payload. Earlier failed attempts are logged; no afternoon substitute; a window with no usable menu
 * becomes MISSED_SESSION. Evidence only: nothing here affects ranking, gates, sizing or exits.
 */
object PaperMenuCapture {
    const val POLICY_VERSION = "paper_menu_capture_v1_first_completed_1230_1255_ist"
    const val FIRST_SLOT_ORDINAL = 40   // 09:15 + 39*5 = 12:30
    const val LAST_SLOT_ORDINAL = 45    // 12:55
    const val MAX_UPLOAD_ATTEMPTS = 12
    const val CANDIDATE_CHUNK = 200

    enum class Action { WAIT_FOR_WINDOW, ATTEMPT_CAPTURE, ALREADY_CAPTURED, RECORD_MISSED, NOTHING }

    fun menuIdFor(sessionDate: String, policy: String = POLICY_VERSION): String {
        val d = MessageDigest.getInstance("SHA-256").digest("$sessionDate|$policy".toByteArray(Charsets.UTF_8))
        return "pdm_" + d.joinToString("") { "%02x".format(it) }.substring(0, 24)
    }

    fun slotScheduledHhmm(ordinal: Int): String {
        val m = 9 * 60 + 15 + (ordinal - 1) * 5
        return "%02d:%02d".format(m / 60, m % 60)
    }

    fun newState(sessionDate: String): JSONObject = JSONObject()
        .put("session_date", sessionDate)
        .put("policy", POLICY_VERSION)
        .put("menu_id", menuIdFor(sessionDate))
        .put("status", "OPEN")
        .put("attempts", JSONArray())
        .put("upload", JSONObject().put("attempts", 0).put("state", "NOT_STARTED"))

    /** Load-or-reset: a state from another session is discarded (never carried across sessions). */
    fun stateFor(raw: String?, sessionDate: String): JSONObject {
        val s = try { if (raw.isNullOrBlank()) null else JSONObject(raw) } catch (_: Exception) { null }
        return if (s != null && s.optString("session_date") == sessionDate && s.optString("policy") == POLICY_VERSION) s
        else newState(sessionDate)
    }

    /** slotOrdinal == null means an unscheduled/manual poll: never a capture slot. */
    fun decide(state: JSONObject, slotOrdinal: Int?): Action {
        val st = state.optString("status")
        if (st == "CAPTURED") return Action.ALREADY_CAPTURED
        if (st == "MISSED") return Action.NOTHING
        if (slotOrdinal == null) return Action.NOTHING
        return when {
            slotOrdinal < FIRST_SLOT_ORDINAL -> Action.WAIT_FOR_WINDOW
            slotOrdinal <= LAST_SLOT_ORDINAL -> Action.ATTEMPT_CAPTURE
            else -> Action.RECORD_MISSED
        }
    }

    fun captureRequest(state: JSONObject, slotKey: String, slotOrdinal: Int, dispatchAtIso: String): JSONObject =
        JSONObject()
            .put("menu_id", state.getString("menu_id"))
            .put("session_date", state.getString("session_date"))
            .put("policy", POLICY_VERSION)
            .put("slot_key", slotKey)
            .put("slot_scheduled_ist", "${state.getString("session_date")}T${slotScheduledHhmm(slotOrdinal)}:00+05:30")
            .put("dispatch_at", dispatchAtIso)
            .put("attempt_no", state.getJSONArray("attempts").length() + 1)

    fun recordAttempt(state: JSONObject, slotKey: String, outcome: String, detail: String?, atIso: String): JSONObject {
        state.getJSONArray("attempts").put(
            JSONObject().put("slot_key", slotKey).put("outcome", outcome).put("detail", detail ?: JSONObject.NULL).put("at", atIso)
        )
        return state
    }

    /** A brain result is a usable capture only when it carries a menu for THIS menu id that was built. */
    fun usableMenu(state: JSONObject, menu: JSONObject?): String? {
        if (menu == null) return "menu_not_returned_by_brain"
        val m = menu.optJSONObject("manifest") ?: return "manifest_missing"
        if (m.optString("menu_id") != state.optString("menu_id")) return "menu_id_mismatch"
        return when (m.optString("population_status")) {
            "MENU_BUILT", "EMPTY_MENU" -> null
            "" -> "population_status_missing"
            else -> "population_" + m.optString("population_status").lowercase()
        }
    }

    fun markCaptured(state: JSONObject, payloadSha256: String, frozenPath: String, capturedAtIso: String): JSONObject =
        state.put("status", "CAPTURED").put("payload_sha256", payloadSha256).put("frozen_path", frozenPath)
            .put("captured_at", capturedAtIso)

    fun missedManifest(state: JSONObject, reason: String, atIso: String): JSONObject {
        state.put("status", "MISSED").put("missed_at", atIso)
        return JSONObject()
            .put("menu_id", state.getString("menu_id"))
            .put("session_date", state.getString("session_date"))
            .put("capture_policy_version", POLICY_VERSION)
            .put("capture_status", "MISSED_SESSION")
            .put("population_status", JSONObject.NULL)
            .put("expected_candidate_rows", 0)
            .put("missed_reason", reason)
            .put("attempts", state.getJSONArray("attempts"))
    }

    fun sha256Hex(s: String): String =
        MessageDigest.getInstance("SHA-256").digest(s.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }

    /** Rows are idempotent on (menu_id, candidate_id); chunking never changes membership. */
    fun chunks(rows: JSONArray, size: Int = CANDIDATE_CHUNK): List<JSONArray> {
        val out = mutableListOf<JSONArray>()
        var cur = JSONArray()
        for (i in 0 until rows.length()) {
            cur.put(rows.get(i))
            if (cur.length() >= size) { out.add(cur); cur = JSONArray() }
        }
        if (cur.length() > 0) out.add(cur)
        return out
    }

    /** Upload bookkeeping. Completeness is declared only by the server-side reconciliation result. */
    fun recordUpload(state: JSONObject, outcome: String, completeness: String?, atIso: String): JSONObject {
        val u = state.optJSONObject("upload") ?: JSONObject().also { state.put("upload", it) }
        u.put("attempts", u.optInt("attempts") + 1).put("last_outcome", outcome).put("last_at", atIso)
        if (completeness != null) u.put("server_completeness", completeness)
        u.put("state", when {
            completeness == "COMPLETE" || completeness == "NO_ROWS_EXPECTED" -> "DONE"
            completeness == "MISMATCH" -> "MISMATCH_NO_RETRY"
            u.optInt("attempts") >= MAX_UPLOAD_ATTEMPTS -> "GAVE_UP"
            else -> "RETRY_PENDING"
        })
        return state
    }

    fun uploadPending(state: JSONObject): Boolean {
        val s = state.optString("status")
        if (s != "CAPTURED" && s != "MISSED") return false
        return state.optJSONObject("upload")?.optString("state") in setOf("NOT_STARTED", "RETRY_PENDING")
    }

    /**
     * Session rollover. Returns (state for [sessionDate], previous session's state if it ended OPEN, now
     * marked MISSED with its missed manifest attached) so a session whose app died before any post-window
     * poll is still recorded as MISSED_SESSION rather than silently absent.
     */
    fun rollover(raw: String?, sessionDate: String, atIso: String): Pair<JSONObject, JSONObject?> {
        val prev = try { if (raw.isNullOrBlank()) null else JSONObject(raw) } catch (_: Exception) { null }
        if (prev != null && prev.optString("session_date") == sessionDate && prev.optString("policy") == POLICY_VERSION) {
            return prev to null
        }
        val missedPrev = if (prev != null && prev.optString("policy") == POLICY_VERSION &&
            prev.optString("status") == "OPEN" && prev.optString("session_date") < sessionDate) {
            prev.put("missed_manifest", missedManifest(prev, "no_completed_scheduled_slot_in_window_before_session_end", atIso))
            prev.put("upload", JSONObject().put("attempts", 0).put("state", "NOT_STARTED"))
        } else null
        return newState(sessionDate) to missedPrev
    }

    private fun nn(v: Any?): Any = v ?: JSONObject.NULL

    /** Manifest row for ml_paper_decision_menus from a frozen CAPTURED menu payload. */
    fun manifestRow(frozen: JSONObject, payloadSha256: String): JSONObject {
        val m = frozen.getJSONObject("manifest")
        val control = m.optJSONObject("control") ?: JSONObject()
        fun o(k: String): Any = if (m.has(k) && !m.isNull(k)) m.get(k) else JSONObject.NULL
        return JSONObject()
            .put("menu_id", m.getString("menu_id"))
            .put("session_date", m.getString("session_date"))
            .put("capture_policy_version", o("capture_policy_version"))
            .put("menu_schema_version", o("menu_schema_version"))
            .put("capture_status", "CAPTURED")
            .put("population_status", o("population_status"))
            .put("generated_count", o("generated_count"))
            .put("captured_count", o("captured_count"))
            .put("duplicate_count", o("duplicate_count"))
            .put("expected_candidate_rows", m.getInt("expected_candidate_rows"))
            .put("candidate_ids_sha256", o("candidate_ids_sha256"))
            .put("primary_candidate_id", o("primary_candidate_id"))
            .put("control_status", nn(control.optString("status").ifBlank { null }))
            .put("control_peer_candidate_id", nn(control.optString("peer_candidate_id").ifBlank { null }))
            .put("control_inclusion_probability", if (control.has("inclusion_probability")) control.get("inclusion_probability") else JSONObject.NULL)
            .put("slot_key", o("slot_key"))
            .put("slot_scheduled_ist", o("slot_scheduled_ist"))
            .put("dispatch_at", o("dispatch_at"))
            .put("brain_completed_at", o("brain_completed_at"))
            .put("chain_poll_ts", o("chain_poll_ts"))
            .put("execution_mode", o("execution_mode"))
            .put("app_version", o("app_version"))
            .put("brain_version", o("brain_version"))
            .put("attempts", frozen.optJSONArray("attempts") ?: JSONArray())
            .put("manifest", m)
            .put("payload_sha256", payloadSha256)
    }

    /** Manifest row for a MISSED_SESSION (no candidate rows; attempts carried). */
    fun missedRow(missed: JSONObject, payloadSha256: String): JSONObject = JSONObject()
        .put("menu_id", missed.getString("menu_id"))
        .put("session_date", missed.getString("session_date"))
        .put("capture_policy_version", missed.getString("capture_policy_version"))
        .put("capture_status", "MISSED_SESSION")
        .put("population_status", JSONObject.NULL)
        .put("expected_candidate_rows", 0)
        .put("attempts", missed.optJSONArray("attempts") ?: JSONArray())
        .put("manifest", missed)
        .put("payload_sha256", payloadSha256)

    private val CANDIDATE_COLUMNS = listOf(
        "menu_id", "candidate_id", "generated_rank", "watchlist_rank", "surfaced", "selected_primary",
        "control_role", "index_key", "expiry", "strategy_family", "lane", "lot_size", "control_eligible"
    )

    /** Candidate row: typed columns + legs + margin_evidence; everything else stays in row_json. */
    fun candidateRow(r: JSONObject): JSONObject {
        val row = JSONObject()
        CANDIDATE_COLUMNS.forEach { k -> row.put(k, if (r.has(k) && !r.isNull(k)) r.get(k) else JSONObject.NULL) }
        row.put("legs", r.optJSONArray("legs") ?: JSONArray())
        row.put("margin_evidence", if (r.has("margin_evidence") && !r.isNull("margin_evidence")) r.get("margin_evidence") else JSONObject.NULL)
        val rest = JSONObject(r.toString())
        (CANDIDATE_COLUMNS + listOf("legs", "margin_evidence")).forEach { rest.remove(it) }
        row.put("row_json", rest)
        return row
    }
}
