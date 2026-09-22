package com.marketradar.app

import android.content.SharedPreferences
import org.json.JSONObject

/**
 * Request-scoped, one-shot Paper close quotes.
 *
 * Periodic PositionMarkStore rows are deliberately display-only. This store is
 * the separate authority used by a manual Paper close after a fresh P1 quote
 * has been captured and validated.
 */
internal object PaperCloseQuoteStore {
    private const val PREF_KEY = "paper_close_quote_store_v1"
    private const val CONTRACT_VERSION = "paper_close_quote_v1"
    private const val RETAIN_MS = 10 * 60 * 1000L

    private val lock = Any()

    fun createPending(
        prefs: SharedPreferences,
        requestId: String,
        tradeId: String,
        requestedAtMs: Long
    ): JSONObject = synchronized(lock) {
        val all = readAll(prefs)
        val row = JSONObject().apply {
            put("contract_version", CONTRACT_VERSION)
            put("request_id", requestId)
            put("trade_id", tradeId)
            put("status", "PENDING")
            put("reason_code", "")
            put("requested_at_ms", requestedAtMs)
            put("quoted_at_ms", 0L)
            put("expires_at_ms", 0L)
            put("source", PositionTickService.PAPER_CLOSE_SOURCE)
        }
        all.put(requestId, row)
        writeAll(prefs, all)
        JSONObject(row.toString())
    }

    fun completeReady(
        prefs: SharedPreferences,
        requestId: String,
        payload: JSONObject
    ): JSONObject = synchronized(lock) {
        val all = readAll(prefs)
        val row = JSONObject(payload.toString()).apply {
            put("contract_version", CONTRACT_VERSION)
            put("request_id", requestId)
            put("status", "READY")
            put("reason_code", "")
        }
        all.put(requestId, row)
        writeAll(prefs, all)
        JSONObject(row.toString())
    }

    fun completeFailed(
        prefs: SharedPreferences,
        requestId: String,
        tradeId: String,
        reasonCode: String,
        detail: String? = null,
        nowMs: Long = System.currentTimeMillis()
    ): JSONObject = synchronized(lock) {
        val all = readAll(prefs)
        val pending = all.optJSONObject(requestId)
        val row = JSONObject().apply {
            put("contract_version", CONTRACT_VERSION)
            put("request_id", requestId)
            put("trade_id", tradeId)
            put("status", "FAILED")
            put("reason_code", reasonCode)
            put("requested_at_ms", pending?.optLong("requested_at_ms", nowMs) ?: nowMs)
            put("quoted_at_ms", 0L)
            put("expires_at_ms", 0L)
            put("source", PositionTickService.PAPER_CLOSE_SOURCE)
            if (!detail.isNullOrBlank()) put("detail", detail.take(180))
        }
        all.put(requestId, row)
        writeAll(prefs, all)
        JSONObject(row.toString())
    }

    fun read(
        prefs: SharedPreferences,
        requestId: String,
        nowMs: Long = System.currentTimeMillis()
    ): JSONObject = synchronized(lock) {
        if (requestId.isBlank()) return@synchronized failure(requestId, "REQUEST_MISMATCH")
        val all = readAll(prefs)
        val row = all.optJSONObject(requestId)
            ?: return@synchronized failure(requestId, "REQUEST_MISMATCH")
        val out = JSONObject(row.toString())
        if (out.optString("status") == "READY" && nowMs >= out.optLong("expires_at_ms", 0L)) {
            out.put("status", "EXPIRED")
            out.put("reason_code", "QUOTE_EXPIRED")
            clearActionableNumbers(out)
            all.put(requestId, out)
            writeAll(prefs, all)
        }
        JSONObject(out.toString())
    }

    fun prune(prefs: SharedPreferences, nowMs: Long = System.currentTimeMillis()) = synchronized(lock) {
        val all = readAll(prefs)
        val keys = all.keys()
        val stale = mutableListOf<String>()
        while (keys.hasNext()) {
            val key = keys.next()
            val row = all.optJSONObject(key) ?: continue
            val status = row.optString("status")
            if (status == "FAILED" || status == "EXPIRED") {
                val at = row.optLong("quoted_at_ms", 0L).takeIf { it > 0L }
                    ?: row.optLong("requested_at_ms", 0L)
                if (at > 0L && nowMs - at >= RETAIN_MS) stale += key
            }
        }
        stale.forEach(all::remove)
        if (stale.isNotEmpty()) writeAll(prefs, all)
    }

    private fun clearActionableNumbers(row: JSONObject) {
        listOf(
            "executable_close_premium",
            "gross_close_pnl",
            "leg_count",
            "expected_leg_count",
            "contract_lot_size",
            "number_of_lots",
            "quantity_units"
        ).forEach(row::remove)
        row.put("valuation_quality", JSONObject.NULL)
        row.put("mark_basis", JSONObject.NULL)
    }

    private fun failure(requestId: String, reason: String): JSONObject = JSONObject().apply {
        put("contract_version", CONTRACT_VERSION)
        put("request_id", requestId)
        put("trade_id", JSONObject.NULL)
        put("status", "FAILED")
        put("reason_code", reason)
        put("source", PositionTickService.PAPER_CLOSE_SOURCE)
    }

    private fun readAll(prefs: SharedPreferences): JSONObject = try {
        JSONObject(prefs.getString(PREF_KEY, "{}") ?: "{}")
    } catch (_: Exception) {
        JSONObject()
    }

    private fun writeAll(prefs: SharedPreferences, all: JSONObject) {
        prefs.edit().putString(PREF_KEY, all.toString()).commit()
    }
}
