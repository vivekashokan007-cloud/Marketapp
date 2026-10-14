package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.ZoneId

/**
 * Implied-EV v1.2 (Codex R2): the ONLY path from a native quote fetch to Rule B / acceptance quotes.
 *
 * Pure (no Android). Per frozen leg: the response must be an EXACT instrument-key match (never the legacy fuzzy
 * suffix lookup), must carry a parseable SOURCE time (the receipt time is never substituted), must not be a
 * duplicate source quote, must be within QUOTE_MAX_SOURCE_AGE_MS of the receipt (and not more than
 * QUOTE_MAX_FUTURE_SKEW_MS ahead), on the receipt's IST session date, and all legs within
 * QUOTE_MAX_INTER_LEG_SKEW_MS of each other. A leg that fails is handed to the policy with tsMs = null, so
 * RuleBExitPolicy.legQuoteValid rejects it; it is never "fresh because we just fetched it".
 *
 * Leg identity comes from the FROZEN entry record (entry_snapshot.entry_fill.legs), never from whatever the
 * open-trade cache currently says; a mismatch fails closed (POLICY_LEG_MISMATCH).
 */
internal object RuleBQuoteAdapter {
    const val CONTRACT = "ev12_rule_b_quote_adapter_v1_exact_key_source_time_frozen_legs"
    private val IST: ZoneId = ZoneId.of("Asia/Kolkata")

    data class LegCheck(val instrumentKey: String, val ok: Boolean, val reasons: List<String>, val sourceMs: Long?,
                        val bid: Double?, val ask: Double?)

    data class Result(val legs: List<LegCheck>, val reasons: List<String>, val receiptMs: Long) {
        val valid: Boolean get() = reasons.isEmpty() && legs.isNotEmpty() && legs.all { it.ok }

        /** Rule B tick quotes: an invalid leg carries tsMs = null (fails RuleBExitPolicy.legQuoteValid). */
        fun policyQuotes(): Map<String, RuleBExitPolicy.LegQuote> = legs.associate { l ->
            l.instrumentKey to RuleBExitPolicy.LegQuote(l.bid, l.ask, if (valid) l.sourceMs?.toDouble() else null)
        }

        /** Python acceptance quotes {key: {bid, ask, ts_ms(source), exact_key, source_time_valid}}. */
        fun acceptanceQuotesJson(): JSONObject = JSONObject().apply {
            legs.forEach { l ->
                put(l.instrumentKey, JSONObject()
                    .put("bid", l.bid ?: JSONObject.NULL).put("ask", l.ask ?: JSONObject.NULL)
                    .put("ts_ms", l.sourceMs ?: JSONObject.NULL)
                    .put("exact_key", !l.reasons.contains("key_match_inexact") && !l.reasons.contains("no_quote"))
                    .put("source_time_valid", l.ok && valid))
            }
        }

        fun toJson(): JSONObject = JSONObject().apply {
            put("contract", CONTRACT)
            put("valid", valid)
            put("reasons", JSONArray(reasons))
            put("receipt_ms", receiptMs)
            put("legs", JSONArray(legs.map { l ->
                JSONObject().put("instrument_key", l.instrumentKey).put("ok", l.ok).put("reasons", JSONArray(l.reasons))
                    .put("source_ms", l.sourceMs ?: JSONObject.NULL)
            }))
        }
    }

    fun adapt(
        legKeys: List<String>,
        sources: Map<String, LegSourceQuote>,
        receiptMs: Long,
        fetchStatus: String = "OK",
        maxSourceAgeMs: Long = QUOTE_MAX_SOURCE_AGE_MS,
        maxFutureSkewMs: Long = QUOTE_MAX_FUTURE_SKEW_MS,
        maxInterLegSkewMs: Long = QUOTE_MAX_INTER_LEG_SKEW_MS
    ): Result {
        val reasons = ArrayList<String>()
        if (fetchStatus != "OK") reasons.add("fetch:$fetchStatus")
        if (legKeys.isEmpty()) reasons.add("no_frozen_legs")
        val receiptDay = Instant.ofEpochMilli(receiptMs).atZone(IST).toLocalDate()
        val responseUse = HashMap<String, Int>()
        legKeys.forEach { k -> sources[k]?.takeIf { it.present }?.responseKey?.let { responseUse[it] = (responseUse[it] ?: 0) + 1 } }
        val legs = legKeys.map { key ->
            val q = sources[key]
            val r = ArrayList<String>()
            var srcMs: Long? = null
            if (q == null || !q.present) r.add("no_quote")
            else {
                if (!q.exactKeyMatch) r.add("key_match_inexact")
                if (q.responseKey != null && (responseUse[q.responseKey] ?: 0) > 1) r.add("duplicate_source_quote")
                val raw = q.sourceTs?.trim()?.takeIf { it.isNotEmpty() && it != "null" }
                val inst = parseParityInstantUtc(raw)
                when {
                    raw == null -> r.add("source_ts_missing")
                    inst == null -> r.add("source_ts_unparseable")
                    else -> {
                        srcMs = inst.toEpochMilli()
                        val age = receiptMs - inst.toEpochMilli()
                        if (age < -maxFutureSkewMs) r.add("source_ts_future")
                        else if (age > maxSourceAgeMs) r.add("source_stale")
                        if (inst.atZone(IST).toLocalDate() != receiptDay) r.add("source_different_session_date")
                    }
                }
                val b = q.bid; val a = q.ask
                if (b == null || a == null || !b.isFinite() || !a.isFinite() || b <= 0 || a <= 0 || a < b) r.add("book_invalid")
            }
            LegCheck(key, r.isEmpty(), r, srcMs, q?.bid, q?.ask)
        }
        val ms = legs.mapNotNull { it.sourceMs }
        if (ms.size == legs.size && ms.isNotEmpty() && ms.max() - ms.min() > maxInterLegSkewMs) reasons.add("inter_leg_skew")
        legs.filter { !it.ok }.forEach { l -> l.reasons.forEach { reasons.add("leg:${l.instrumentKey}:$it") } }
        return Result(legs, reasons, receiptMs)
    }

    /** Frozen legs of an accepted Rule B trade: entry_snapshot.entry_fill.legs [{instrument_key, qty}]. */
    fun frozenLegs(entrySnapshot: JSONObject): List<RuleBExitPolicy.Leg>? {
        val legs = entrySnapshot.optJSONObject("entry_fill")?.optJSONArray("legs") ?: return null
        if (legs.length() == 0) return null
        val out = ArrayList<RuleBExitPolicy.Leg>()
        for (i in 0 until legs.length()) {
            val l = legs.optJSONObject(i) ?: return null
            val key = l.optString("instrument_key").trim()
            val qty = l.optInt("qty", 0)
            if (key.isEmpty() || qty == 0) return null
            out.add(RuleBExitPolicy.Leg(qty, key))
        }
        return out
    }

    /** Same instruments with the same signs (the trade row may only differ in ordering). */
    fun legsMatch(frozen: List<RuleBExitPolicy.Leg>, trade: List<RuleBExitPolicy.Leg>): Boolean {
        fun norm(x: List<RuleBExitPolicy.Leg>) = x.map { it.instrumentKey to Integer.signum(it.qty) }.sortedBy { it.first + it.second }
        return frozen.size == trade.size && norm(frozen) == norm(trade)
    }
}
