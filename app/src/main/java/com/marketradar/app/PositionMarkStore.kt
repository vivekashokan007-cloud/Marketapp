package com.marketradar.app

import android.content.SharedPreferences
import org.json.JSONArray
import org.json.JSONObject

/**
 * Local presentation cache for the strict executable marks produced by
 * [PositionTickService].
 *
 * A missing quote response is evidence that the current mark is not safe to
 * act on; it is not evidence that the preceding fully validated mark vanished.
 * This store therefore retains the last accepted mark separately from the most
 * recent observation. Consumers must treat STALE_LAST_VALID as display-only.
 */
internal object PositionMarkStore {
    private const val PREF_KEY = "position_mark_state_v1"
    private const val SCHEMA_VERSION = "position_mark_state_v1"
    internal const val LIVE_FULL = "LIVE_FULL"
    internal const val STALE_LAST_VALID = "STALE_LAST_VALID"
    internal const val UNAVAILABLE = "UNAVAILABLE"

    // Two missed 60-second ticks plus transport jitter. This is a display TTL,
    // not an order/exit permission TTL.
    internal const val FRESH_MARK_MAX_AGE_MS = 150_000L

    // B3: tolerance before a recorded/source instant counts as "in the future"
    // (device clock rollback). Beyond it the mark is never treated as fresh.
    internal const val CLOCK_ROLLBACK_TOLERANCE_MS = 5_000L
    internal const val FRESHNESS_BASIS_SOURCE = "SOURCE_QUOTE_TIME_AND_LOCAL_RECORD_TIME"
    internal const val FRESHNESS_BASIS_LEGACY = "LOCAL_RECORD_TIME"
    private val PAPER_TRUST_KEYS = listOf(
        "latest_paper", "latest_trust_state", "latest_trust_cause", "latest_quote_validity_state",
        "latest_source_quote_ms", "untrusted_pending_revalidation", "untrusted_latest_source_ms",
        "untrusted_book_fingerprint"
    )

    /**
     * Prior untrusted state used by the A1 recovery rule: the latest source time
     * and book fingerprint of the most recent UNTRUSTED mark, cleared on recovery.
     */
    fun trustRecoveryState(prefs: SharedPreferences, tradeId: String): Pair<Long?, String?> {
        if (tradeId.isBlank()) return null to null
        val raw = readAll(prefs).optJSONObject(tradeId) ?: return null to null
        if (!raw.optBoolean("untrusted_pending_revalidation", false)) return null to null
        val ms = if (raw.has("untrusted_latest_source_ms") && !raw.isNull("untrusted_latest_source_ms")) {
            raw.optLong("untrusted_latest_source_ms")
        } else null
        val fp = if (raw.has("untrusted_book_fingerprint") && !raw.isNull("untrusted_book_fingerprint")) {
            raw.optString("untrusted_book_fingerprint")
        } else null
        return ms to fp
    }

    fun recordRows(prefs: SharedPreferences, rows: JSONArray, nowMs: Long = System.currentTimeMillis()) {
        val all = readAll(prefs)
        for (i in 0 until rows.length()) {
            val row = rows.optJSONObject(i) ?: continue
            val tradeId = row.optString("trade_id", "").trim()
            if (tradeId.isEmpty()) continue

            val existing = all.optJSONObject(tradeId) ?: JSONObject()
            val next = JSONObject(existing.toString()).apply {
                put("schema_version", SCHEMA_VERSION)
                put("latest_tick_ts", row.optString("tick_ts", ""))
                put("latest_tick_ms", nowMs)
                put("latest_valuation_quality", row.optString("valuation_quality", "UNAVAILABLE"))
                put("latest_policy_action", row.optString("policy_action", ""))
                put("latest_policy_reason", row.optString("policy_reason", ""))
                put("latest_source", row.optString("source", ""))
            }

            val pnl = row.optFiniteDouble("current_pnl")
            // B3/A1: Paper rows carry a trust assessment. Legacy/Real rows (no
            // Paper trust object) keep the pre-B3 rule byte-for-byte.
            val trust = row.optJSONObject("policy_trace_json")?.optJSONObject("mark_store_trust")
            val paperTrust = trust != null && trust.optBoolean("paper", false)
            if (paperTrust) {
                val t = trust!!
                val trusted = t.optString("trust_state", "") == TRUST_TRUSTED
                next.put("latest_paper", true)
                next.put("latest_trust_state", t.optString("trust_state", TRUST_UNTRUSTED))
                next.put("latest_trust_cause", t.opt("trust_cause") ?: JSONObject.NULL)
                next.put("latest_quote_validity_state", t.optString("quote_validity_state", QV_INVALID))
                next.put("latest_source_quote_ms", t.opt("earliest_source_ms") ?: JSONObject.NULL)
                if (trusted) {
                    next.put("untrusted_pending_revalidation", false)
                    next.remove("untrusted_latest_source_ms")
                    next.remove("untrusted_book_fingerprint")
                } else {
                    next.put("untrusted_pending_revalidation", true)
                    val latestSrc = t.opt("latest_source_ms")
                    if (latestSrc is Number) {
                        val prior = if (next.has("untrusted_latest_source_ms") && !next.isNull("untrusted_latest_source_ms")) {
                            next.optLong("untrusted_latest_source_ms")
                        } else Long.MIN_VALUE
                        next.put("untrusted_latest_source_ms", maxOf(prior, latestSrc.toLong()))
                    }
                    val fp = t.opt("book_fingerprint")
                    if (fp is String && fp.isNotBlank()) next.put("untrusted_book_fingerprint", fp)
                }
            } else {
                PAPER_TRUST_KEYS.forEach { next.remove(it) }
            }
            val acceptTrust = !paperTrust || trust!!.optString("trust_state", "") == TRUST_TRUSTED
            if (row.optString("valuation_quality", "") == "OK" && pnl != null && acceptTrust) {
                next.put("last_valid_tick_ts", row.optString("tick_ts", ""))
                next.put("last_valid_tick_ms", nowMs)
                next.put("last_valid_current_pnl", pnl)
                next.put("last_valid_mark", row.opt("executable_mark"))
                next.put("last_valid_source", row.optString("source", ""))
                next.put("last_valid_leg_count", row.optInt("leg_count", 0))
                // Full accepted mark for Paper Brain valuation (not display-only).
                // Do not invent premiums; only persist what P1 already validated.
                if (row.has("legs_json") && !row.isNull("legs_json")) {
                    next.put("last_valid_legs_json", row.opt("legs_json"))
                }
                next.put("last_valid_mark_basis", row.optString("mark_basis", ""))
                next.put("last_valid_auth_source", row.optString("auth_source", ""))
                if (row.has("quantity_units") && !row.isNull("quantity_units")) {
                    next.put("last_valid_quantity_units", row.opt("quantity_units"))
                }
                if (row.has("contract_lot_size") && !row.isNull("contract_lot_size")) {
                    next.put("last_valid_contract_lot_size", row.opt("contract_lot_size"))
                }
                if (row.has("number_of_lots") && !row.isNull("number_of_lots")) {
                    next.put("last_valid_number_of_lots", row.opt("number_of_lots"))
                }
                if (row.has("lot_authoritative")) {
                    next.put("last_valid_lot_authoritative", row.optBoolean("lot_authoritative", false))
                }
                next.put("last_valid_index_key", row.optString("index_key", ""))
                next.put("last_valid_strategy_type", row.optString("strategy_type", ""))
                if (paperTrust) {
                    val t = trust!!
                    next.put("last_valid_trust_state", TRUST_TRUSTED)
                    next.put("last_valid_source_quote_ms", t.opt("earliest_source_ms") ?: JSONObject.NULL)
                    next.put("last_valid_source_quote_ts", t.opt("earliest_source_ts") ?: JSONObject.NULL)
                } else {
                    next.remove("last_valid_trust_state")
                    next.remove("last_valid_source_quote_ms")
                    next.remove("last_valid_source_quote_ts")
                }
            }
            all.put(tradeId, next)
        }
        prefs.edit().putString(PREF_KEY, all.toString()).commit()
    }

    fun presentationJson(prefs: SharedPreferences, nowMs: Long = System.currentTimeMillis()): String {
        val all = readAll(prefs)
        val tracking = readPositionTickTrackingStatus(prefs)
        val out = JSONObject()
        val keys = all.keys()
        while (keys.hasNext()) {
            val tradeId = keys.next()
            val raw = all.optJSONObject(tradeId) ?: continue
            out.put(tradeId, presentationFor(raw, nowMs, tracking))
        }
        return out.toString()
    }

    internal fun presentationFor(
        raw: JSONObject,
        nowMs: Long,
        tracking: PositionTickTrackingStatus = PositionTickTrackingStatus(
            trackingComplete = true,
            overflowActive = false,
            overflowRejectedCount = 0L
        )
    ): JSONObject {
        val latestQuality = raw.optString("latest_valuation_quality", UNAVAILABLE)
        val latestMs = raw.optLong("latest_tick_ms", 0L)
        val lastValidPnl = raw.optFiniteDouble("last_valid_current_pnl")
        val lastValidMs = raw.optLong("last_valid_tick_ms", 0L)
        val paperTrust = raw.optBoolean("latest_paper", false) && raw.has("latest_trust_state")
        val latestAge: Long
        val validAge: Long
        val state: String
        if (!paperTrust) {
            // Legacy / Real: unchanged pre-B3 behaviour.
            latestAge = safeAge(nowMs, latestMs)
            validAge = safeAge(nowMs, lastValidMs)
            state = when {
                latestQuality == "OK" && lastValidPnl != null && latestAge <= FRESH_MARK_MAX_AGE_MS -> LIVE_FULL
                lastValidPnl != null -> STALE_LAST_VALID
                else -> UNAVAILABLE
            }
        } else {
            // B3 (Paper): freshness uses BOTH the source quote time and the local
            // record time; a clock rollback/future instant is never fresh.
            latestAge = strictAge(nowMs, latestMs)
            val sourceMs = if (raw.has("last_valid_source_quote_ms") && !raw.isNull("last_valid_source_quote_ms")) {
                raw.optLong("last_valid_source_quote_ms")
            } else 0L
            validAge = maxOf(strictAge(nowMs, lastValidMs), strictAge(nowMs, sourceMs))
            val latestTrusted = raw.optString("latest_trust_state", "") == TRUST_TRUSTED &&
                raw.optString("last_valid_trust_state", "") == TRUST_TRUSTED
            state = when {
                latestQuality == "OK" && latestTrusted && lastValidPnl != null &&
                    latestAge <= FRESH_MARK_MAX_AGE_MS && validAge <= FRESH_MARK_MAX_AGE_MS -> LIVE_FULL
                lastValidPnl != null -> STALE_LAST_VALID
                else -> UNAVAILABLE
            }
        }
        return JSONObject().apply {
            put("schema_version", SCHEMA_VERSION)
            put("display_state", state)
            put("display_is_actionable", false)
            put("latest_valuation_quality", latestQuality)
            put("latest_tick_ts", raw.optString("latest_tick_ts", ""))
            put("latest_age_ms", latestAge)
            put("last_valid_tick_ts", raw.optString("last_valid_tick_ts", ""))
            put("last_valid_age_ms", validAge)
            put("last_valid_current_pnl", lastValidPnl ?: JSONObject.NULL)
            put("last_valid_mark", raw.opt("last_valid_mark") ?: JSONObject.NULL)
            put("source", raw.optString("last_valid_source", raw.optString("latest_source", "")))
            put("leg_count", raw.optInt("last_valid_leg_count", 0))
            put("latest_policy_action", raw.optString("latest_policy_action", ""))
            put("latest_policy_reason", raw.optString("latest_policy_reason", ""))
            // Brain bridge fields (Paper valuation source). Present even when
            // STALE so diagnostics can explain rejection; apply path still
            // requires LIVE_FULL.
            put("last_valid_legs_json", raw.opt("last_valid_legs_json") ?: JSONObject.NULL)
            put("last_valid_mark_basis", raw.optString("last_valid_mark_basis", ""))
            put("last_valid_auth_source", raw.optString("last_valid_auth_source", ""))
            put("last_valid_quantity_units", raw.opt("last_valid_quantity_units") ?: JSONObject.NULL)
            put("last_valid_contract_lot_size", raw.opt("last_valid_contract_lot_size") ?: JSONObject.NULL)
            put("last_valid_number_of_lots", raw.opt("last_valid_number_of_lots") ?: JSONObject.NULL)
            put("last_valid_lot_authoritative", raw.optBoolean("last_valid_lot_authoritative", false))
            put("last_valid_index_key", raw.optString("last_valid_index_key", ""))
            put("last_valid_strategy_type", raw.optString("last_valid_strategy_type", ""))
            put("fresh_mark_max_age_ms", FRESH_MARK_MAX_AGE_MS)
            // R8: overflow / history-capture completeness for Position card UI.
            // Does not affect Brain valuation fail-closed (Brain ignores unknown fields).
            put("tracking_complete", tracking.trackingComplete)
            put("overflow_active", tracking.overflowActive)
            put("overflow_rejected_count", tracking.overflowRejectedCount)
            if (paperTrust) {
                // B3/A1 fields exist only for Paper rows carrying a trust object, so
                // Real/legacy presentation JSON is byte-identical to pre-B3.
                put("freshness_basis", FRESHNESS_BASIS_SOURCE)
                put("mark_trust_state", raw.optString("latest_trust_state", TRUST_UNTRUSTED))
                put("mark_trust_cause", raw.opt("latest_trust_cause") ?: JSONObject.NULL)
                put("quote_validity_state", raw.optString("latest_quote_validity_state", QV_INVALID))
                put("last_valid_source_quote_ts", raw.opt("last_valid_source_quote_ts") ?: JSONObject.NULL)
                put("untrusted_pending_revalidation", raw.optBoolean("untrusted_pending_revalidation", false))
            }
        }
    }

    private fun readAll(prefs: SharedPreferences): JSONObject = try {
        JSONObject(prefs.getString(PREF_KEY, "{}") ?: "{}")
    } catch (_: Exception) {
        JSONObject()
    }

    private fun safeAge(nowMs: Long, thenMs: Long): Long =
        if (thenMs <= 0L) Long.MAX_VALUE else (nowMs - thenMs).coerceAtLeast(0L)

    /** Like [safeAge] but a future instant beyond tolerance is never fresh. */
    private fun strictAge(nowMs: Long, thenMs: Long): Long = when {
        thenMs <= 0L -> Long.MAX_VALUE
        thenMs - nowMs > CLOCK_ROLLBACK_TOLERANCE_MS -> Long.MAX_VALUE
        else -> (nowMs - thenMs).coerceAtLeast(0L)
    }
}

private fun JSONObject.optFiniteDouble(name: String): Double? {
    if (!has(name) || isNull(name)) return null
    val value = when (val raw = opt(name)) {
        is Number -> raw.toDouble()
        is String -> raw.trim().toDoubleOrNull()
        else -> null
    }
    return value?.takeIf { it.isFinite() }
}
