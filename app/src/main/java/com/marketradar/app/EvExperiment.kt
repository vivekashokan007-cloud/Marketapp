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
    /** Last fetch ATTEMPT (updated even when the fetch returned nothing): never proof of a successful refresh. */
    const val PREF_CALIBRATION_FETCHED_MS = "ev_calibration_fetched_ms"
    /** 2.6.74: time a fetched row was actually stored into the cache (written only when a row was returned). */
    const val PREF_CALIBRATION_STORED_MS = "ev_calibration_stored_ms"
    const val STATUS_SCHEMA = "ev_native_status_v1"
    const val CALIBRATION_REFRESH_MS = 30L * 60 * 1000
    const val RESULT_KEY = "implied_ev_v12"

    fun mode(prefs: SharedPreferences): String =
        when ((prefs.getString(PREF_MODE, "off") ?: "off").trim().lowercase()) {
            "observe" -> "observe"; "active" -> "active"; else -> "off"
        }

    /** Off and Observe may be selected in this batch; Active entry awaits its separate acceptance stage. */
    val SELECTABLE_MODES = setOf("off", "observe")

    /** Only a refresh for a poll the experiment actually applied to (Paper and not Off), on the existing cadence. */
    fun shouldRefreshCalibration(applied: Boolean, nowMs: Long, lastAttemptMs: Long): Boolean =
        applied && nowMs - lastAttemptMs >= CALIBRATION_REFRESH_MS

    data class SetResult(val mode: String, val written: Boolean, val reason: String)

    /**
     * Explicit user change. Native authority: refused unless the execution mode is Paper; Active cannot be newly
     * entered (an existing Active value is left as is, never silently rewritten). Unrecognized requests are refused.
     */
    fun setMode(prefs: SharedPreferences, requested: String, executionMode: String): SetResult {
        val cur = mode(prefs)
        val req = requested.trim().lowercase()
        if (!ExecutionModeResolver.isPaper(executionMode)) return SetResult(cur, false, "NOT_PAPER")
        if (req !in SELECTABLE_MODES) return SetResult(cur, false, if (req == "active") "ACTIVE_NOT_ENABLED" else "INVALID_MODE")
        prefs.edit().putString(PREF_MODE, req).commit()
        val back = mode(prefs)
        return SetResult(back, back == req, if (back == req) "OK" else "READBACK_MISMATCH")
    }

    private val SAFE = Regex("^[A-Za-z0-9_.:+\\-]{1,96}$")
    private fun safe(v: Any?): Any = if (v is String && SAFE.matches(v)) v else JSONObject.NULL

    /**
     * Bounded read-only status (2.6.74): the persisted preference, the native execution mode, cache presence and
     * the cached publication's version/available_at, the last fetch ATTEMPT and the last cache store time.
     * Never returns the publication body, quotes, keys or credentials. Native does not validate the publication:
     * validation is the brain's (Python) result for a poll.
     */
    fun status(prefs: SharedPreferences, executionMode: String, appVersion: String): JSONObject {
        val raw = prefs.getString(PREF_MODE, null)
        val m = mode(prefs)
        val stored = when {
            raw == null -> "UNSET"
            raw.trim().lowercase() in setOf("off", "observe", "active") -> "RECOGNIZED"
            else -> "UNRECOGNIZED"
        }
        val cache = prefs.getString(PREF_CALIBRATION, null)
        var parse = "ABSENT"; var ver: Any = JSONObject.NULL; var avail: Any = JSONObject.NULL
        if (!cache.isNullOrBlank()) {
            try {
                val o = JSONObject(cache)
                ver = safe(o.opt("calibration_version")); avail = safe(o.opt("available_at"))
                parse = if (ver == JSONObject.NULL) "UNREADABLE" else "PARSED"
            } catch (e: Exception) { parse = "UNREADABLE" }
        }
        val attempt = prefs.getLong(PREF_CALIBRATION_FETCHED_MS, 0L)
        val storedMs = prefs.getLong(PREF_CALIBRATION_STORED_MS, 0L)
        val exec = executionMode.trim().lowercase().ifBlank { "unknown" }
        return JSONObject()
            .put("schema", STATUS_SCHEMA)
            .put("mode", m)
            .put("mode_stored", stored)
            .put("execution_mode", exec)
            .put("experiment_applies", ExecutionModeResolver.isPaper(executionMode) && m != "off")
            .put("selectable_modes", org.json.JSONArray(SELECTABLE_MODES.toList()))
            .put("can_change", ExecutionModeResolver.isPaper(executionMode))
            .put("cache_present", parse != "ABSENT")
            .put("cache_parse", parse)
            .put("cached_calibration_version", ver)
            .put("cached_available_at", avail)
            .put("last_fetch_attempt_ms", if (attempt > 0) attempt else JSONObject.NULL)
            .put("cache_stored_ms", if (storedMs > 0) storedMs else JSONObject.NULL)
            .put("fetch_success_recorded", storedMs > 0)
            .put("native_validation", "NOT_PERFORMED")
            .put("app_version", safe(appVersion))
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
        ctx.put("evCaptureSource", "LIVE_PHONE_POLL")   // Codex C4 provenance: the live native poll path
        if (!calibration.isNullOrBlank()) {
            try { ctx.put("evCalibration", JSONObject(calibration)) } catch (e: Exception) { /* Python reports NO_CALIBRATION */ }
        }
        ctx.put("appVersion", appVersion)
        return true
    }

    /** Paper trade governed by an experiment exit policy (entry_snapshot.position_policy_id non-blank). */
    fun isExperimentTrade(t: JSONObject?): Boolean {
        if (t == null || !t.optBoolean("paper", false)) return false
        val es = t.opt("entry_snapshot") ?: t.opt("entrySnapshot")
        val o = when (es) {
            is JSONObject -> es
            is String -> try { JSONObject(es) } catch (e: Exception) { null }
            else -> null
        } ?: return false
        return o.optString("position_policy_id").isNotBlank()
    }

    /** Codex R4: experiment (Rule B) trades never enter v7 learning exports. Returns (kept rows, excluded). */
    fun excludeExperimentTrades(rows: JSONArray): Pair<JSONArray, Int> {
        val out = JSONArray(); var n = 0
        for (i in 0 until rows.length()) {
            val t = rows.optJSONObject(i)
            if (isExperimentTrade(t)) n++ else out.put(rows.get(i))
        }
        return out to n
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
