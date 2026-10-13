package com.marketradar.app

import android.content.SharedPreferences
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * Implied-EV v1.2 Paper experiment glue (pure; no Android services). Off by default.
 *
 * The ctx keys are added ONLY when the derived executionMode is "paper" AND the user preference
 * ev_experiment_mode is observe/active; a Real/sandbox poll's ctx is byte-identical to before.
 */
object EvExperiment {
    const val PREF_MODE = "ev_experiment_mode"           // off | observe | active (default off)
    const val PREF_CALIBRATION = "ev_calibration_cache"  // last fetched publication (validated by Python)
    const val PREF_CALIBRATION_FETCHED_MS = "ev_calibration_fetched_ms"
    const val CALIBRATION_REFRESH_MS = 30L * 60 * 1000
    const val RESULT_KEY = "implied_ev_v12"

    fun mode(prefs: SharedPreferences): String =
        when ((prefs.getString(PREF_MODE, "off") ?: "off").trim().lowercase()) {
            "observe" -> "observe"; "active" -> "active"; else -> "off"
        }

    fun stateDir(filesDir: File) = File(filesDir, "ev_v12")
    fun outboxDir(filesDir: File) = File(filesDir, "ev_v12_outbox")

    /** Returns true when keys were added. */
    fun applyContext(ctx: JSONObject, mode: String, executionMode: String, filesDir: File, pollMs: Long,
                     chainFetchedMs: Long?, calibration: String?, appVersion: String): Boolean {
        if (executionMode != "paper" || mode == "off") return false
        ctx.put("evExperimentMode", mode)
        ctx.put("evStateDir", stateDir(filesDir).absolutePath)
        ctx.put("evPollMs", pollMs)
        if (chainFetchedMs != null && chainFetchedMs > 0) ctx.put("evQuoteFetchedAtMs", chainFetchedMs)
        if (!calibration.isNullOrBlank()) {
            try { ctx.put("evCalibration", JSONObject(calibration)) } catch (e: Exception) { /* Python reports NO_CALIBRATION */ }
        }
        ctx.put("appVersion", appVersion)
        return true
    }

    data class Detached(val view: JSONObject?, val queued: List<String>)

    /**
     * Detach envelopes/outcomes from the brain result BEFORE anything else reads, caches, broadcasts or
     * snapshots it; enqueue the exact bytes. The remaining (small) view stays in the result for the PWA.
     */
    fun detach(result: JSONObject, outbox: File, nowMs: Long): Detached {
        val ev = result.optJSONObject(RESULT_KEY) ?: return Detached(null, emptyList())
        val out = ArrayList<String>()
        val envs = ev.optJSONArray("pending_envelopes") ?: JSONArray()
        for (i in 0 until envs.length()) {
            val e = envs.optJSONObject(i) ?: continue
            out += EvTelemetryOutbox.enqueue(outbox, EvTelemetryOutbox.KIND_DECISION, e.optString("decision_id"),
                e.optString("envelope_text"), e.optString("envelope_sha256"), nowMs)
        }
        val outs = ev.optJSONArray("pending_outcomes") ?: JSONArray()
        for (i in 0 until outs.length()) {
            val o = outs.optJSONObject(i) ?: continue
            out += EvTelemetryOutbox.enqueue(outbox, EvTelemetryOutbox.KIND_OUTCOME, o.optString("decision_id"),
                o.optString("outcome_text"), o.optString("outcome_sha256"), nowMs)
        }
        ev.remove("pending_envelopes")
        ev.remove("pending_outcomes")
        ev.put("outbox_pending", EvTelemetryOutbox.entries(outbox).size)
        return Detached(ev, out)
    }
}
