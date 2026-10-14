package com.marketradar.app

import android.content.SharedPreferences
import org.json.JSONObject
import java.io.File

/**
 * Implied-EV v1.2 acceptance transaction bridge (Codex R1). Every call into ev_v12_acceptance runs under ONE
 * process-wide [LOCK], so prepare / confirm / cancel / persisted / closed are serialized (double taps, the
 * service and the WebView thread cannot interleave). Execution mode, experiment mode, open trades and capital
 * are read HERE from native preferences - never accepted from the PWA.
 */
object EvAcceptance {
    val LOCK = Any()
    const val MAX_REQUESTS = 20
    const val REQUEST_TTL_MS = 24L * 3600 * 1000
    const val DEFAULT_CAPITAL = 250000

    fun executionMode(prefs: SharedPreferences): String = ExecutionModeResolver.derive(
        prefs.getString("execution_mode", ""),
        prefs.getBoolean("execution_sandbox_enabled", false),
        prefs.getString("order_proxy_url", "")
    )

    fun capitalTotal(prefs: SharedPreferences): Double = try {
        prefs.getInt("capital", DEFAULT_CAPITAL).toDouble()
    } catch (e: ClassCastException) {
        try { prefs.getFloat("capital", DEFAULT_CAPITAL.toFloat()).toDouble() } catch (x: Exception) { Double.NaN }
    }

    private fun mod() = com.chaquo.python.Python.getInstance().getModule("ev_v12_acceptance")

    private fun call(fn: String, vararg args: Any?): String = synchronized(LOCK) {
        try { mod().callAttr(fn, *args).toString() } catch (e: Exception) {
            JSONObject().put("status", "FAILED").put("reason", "ACCEPTANCE_INTERNAL_ERROR:${e.javaClass.simpleName}").toString()
        }
    }

    fun prepare(dir: File, sessionDate: String, decisionId: String, quotes: JSONObject, nowMs: Long,
                prefs: SharedPreferences): String = call("prepare_json", dir.absolutePath, sessionDate, decisionId,
        quotes.toString(), nowMs, executionMode(prefs), EvExperiment.mode(prefs),
        prefs.getString("open_trades", "[]") ?: "[]", capitalTotal(prefs).takeIf { it.isFinite() })

    fun confirm(dir: File, decisionId: String, reservationId: String, nowMs: Long, prefs: SharedPreferences): String =
        call("confirm_json", dir.absolutePath, decisionId, reservationId, nowMs, executionMode(prefs), EvExperiment.mode(prefs))

    fun cancel(dir: File, decisionId: String, reservationId: String, nowMs: Long): String =
        call("cancel_json", dir.absolutePath, decisionId, reservationId, nowMs)

    fun markPersisted(dir: File, decisionId: String, tradeId: String, tradeUid: String, nowMs: Long): String =
        call("mark_persisted_json", dir.absolutePath, decisionId, tradeId, tradeUid, nowMs)

    fun markClosed(dir: File, decisionId: String, kind: String, nowMs: Long): String =
        call("mark_closed_json", dir.absolutePath, decisionId, kind, nowMs)

    fun recovery(dir: File): String = call("recovery_json", dir.absolutePath)

    fun quarantineStatus(dir: File): String = call("quarantine_status_json", dir.absolutePath)

    /** Explicit reconciliation after the native open positions were checked (never automatic). */
    fun reconcileQuarantine(dir: File, nowMs: Long, note: String): String =
        call("reconcile_quarantine_json", dir.absolutePath, nowMs, note)

    fun closeEconomics(dir: File, decisionId: String, fillJson: String, entryFillJson: String?): String =
        call2("ev_v12_integration", "rule_b_close_economics_json", dir.absolutePath, decisionId, fillJson, entryFillJson)

    private fun call2(module: String, fn: String, vararg args: Any?): String = synchronized(LOCK) {
        try { com.chaquo.python.Python.getInstance().getModule(module).callAttr(fn, *args).toString() } catch (e: Exception) {
            JSONObject().put("status", "FAILED").put("reason", "ECONOMICS_INTERNAL_ERROR:${e.javaClass.simpleName}").toString()
        }
    }

    /** Bounded request cache: keys to drop (older than TTL, then oldest beyond MAX_REQUESTS). Pure. */
    fun prunePlan(entries: Map<String, String?>, nowMs: Long): List<String> {
        val created = entries.mapValues { (_, v) ->
            try { JSONObject(v ?: "{}").optLong("created_ms", 0L) } catch (e: Exception) { 0L }
        }
        val drop = created.filter { nowMs - it.value > REQUEST_TTL_MS }.keys.toMutableSet()
        val keep = created.filterKeys { it !in drop }.entries.sortedByDescending { it.value }
        keep.drop(MAX_REQUESTS).forEach { drop.add(it.key) }
        return drop.toList()
    }

    fun pruneRequests(store: SharedPreferences, nowMs: Long) {
        val all = store.all.mapValues { it.value as? String }
        val drop = prunePlan(all, nowMs)
        if (drop.isNotEmpty()) store.edit().apply { drop.forEach { remove(it) } }.commit()
    }
}
