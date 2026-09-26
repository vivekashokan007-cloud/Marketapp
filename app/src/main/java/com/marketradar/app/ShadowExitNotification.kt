package com.marketradar.app

import org.json.JSONObject

/*
 * Pure shadow-exit decision helpers (no Android), extracted so the notification
 * text and channel for every shadow action — including the B3.1 mid-fallback
 * stop — are executed by unit tests. The non-fallback strings are the exact
 * pre-B3.1 strings from PositionTickService.maybeNotifyShadowExit (Real parity).
 */

/**
 * B3.1: SL fires on mid only when the executable mark was withheld and mid <= SL.
 * Addendum A: the fallback can only ADD a stop. It is consulted only when
 * pricePnl == null (untrusted Paper mark); whenever the executable P&L is
 * available (trusted mark, all Real) decideShadowAction evaluates the
 * executable stop first and the mid is never read.
 */
internal fun isMidFallbackStop(pricePnl: Double?, midFallbackPnl: Double?, slThreshold: Double?): Boolean =
    pricePnl == null && midFallbackPnl != null && midFallbackPnl.isFinite() &&
        slThreshold != null && midFallbackPnl <= slThreshold

/**
 * The shadow action precedence used by PositionTickService.evaluateShadowPolicy.
 * [pricePnl] is null when a Paper mark is untrusted (A1). Order: SL on the
 * trusted executable mark, then the B3.1 mid-fallback SL, TP (executable only —
 * targets never use mid), EOD, then data-quality states, else HOLD.
 */
internal fun decideShadowAction(
    pricePnl: Double?,
    midFallbackPnl: Double?,
    slThreshold: Double?,
    tpThreshold: Double?,
    eod: Boolean,
    priceUntrustedCause: String?,
    valuationQuality: String
): String = when {
    pricePnl != null && slThreshold != null && pricePnl <= slThreshold -> "SHADOW_SL"
    isMidFallbackStop(pricePnl, midFallbackPnl, slThreshold) -> "SHADOW_SL"
    pricePnl != null && tpThreshold != null && pricePnl >= tpThreshold -> "SHADOW_TP"
    eod -> "SHADOW_EOD"
    priceUntrustedCause != null -> "SHADOW_DEGRADED"
    valuationQuality != "OK" -> "SHADOW_DEGRADED"
    else -> "HOLD"
}

private fun rupees(v: Double): String = "₹${"%,.0f".format(kotlin.math.round(v))}"

/**
 * (title, body, channel) for a shadow action row, or null for a non-alert action.
 * Channel "urgent" for SL/TP/EOD (the B3.1 mid-fallback stop included), "routine"
 * for SHADOW_DEGRADED data-quality notices.
 */
internal fun shadowExitNotificationContent(action: String, label: String, row: JSONObject): Triple<String, String, String>? {
    val pnl = if (row.isNull("current_pnl")) Double.NaN else row.optDouble("current_pnl", Double.NaN)
    val trace = row.optJSONObject("policy_trace_json")
    // A1: a Paper deadline notice on an untrusted mark labels the raw P&L.
    val priceGateApplied = trace?.optJSONObject("mark_trust")?.optBoolean("price_gate_applied", false) == true
    val pnlText = when {
        pnl.isNaN() -> "P&L n/a"
        priceGateApplied -> "P&L ${rupees(pnl)} (untrusted mark)"
        else -> "P&L ${rupees(pnl)}"
    }
    // A1: an untrusted Paper mark produces an explicit monitoring-risk notice
    // instead of a stop/target instruction; never a silent HOLD.
    val untrustedReason = row.optString("policy_reason", "")
    val markUntrusted = untrustedReason.startsWith("mark_untrusted:")
    val degradedTitle = if (markUntrusted) "⚠️ Stop/Target Unavailable" else "🧪 Position Data Incomplete"
    val degradedBody = if (markUntrusted) {
        "$label · mark untrusted (${untrustedReason.removePrefix("mark_untrusted:")}) · " +
            "stop/target cannot be evaluated · review position."
    } else {
        "$label · valuation degraded · review marks before trusting P&L."
    }
    val midFallback = trace?.optString("stop_basis", "") == STOP_BASIS_MID_FALLBACK_WIDE_BOOK &&
        trace.optBoolean("mid_fallback_stop", false)
    return when (action) {
        "SHADOW_SL" -> if (midFallback) {
            val mid = trace!!.optDouble("mid_fallback_pnl", Double.NaN)
            val cause = trace.optString("price_policy_untrusted_cause", "WIDE_BOOK")
            // Addendum A (Codex): a wide book's mid is indicative, not a closing
            // price (buy-to-close pays the ask, sell-to-close gets the bid). The
            // executable P&L is shown FIRST as the expected fill cost so the
            // fallback can never make the position look safer than a close.
            Triple(
                "🛑 Stop Loss Near (mid basis)",
                "$label · expected fill (bid/ask) P&L ${if (pnl.isNaN()) "n/a" else rupees(pnl)} · " +
                    "mid P&L ${if (mid.isNaN()) "n/a" else rupees(mid)} (indicative) · wide book ($cause) · " +
                    "stop_basis=$STOP_BASIS_MID_FALLBACK_WIDE_BOOK · Cut position.",
                "urgent"
            )
        } else {
            Triple("🛑 Stop Loss Near", "$label · $pnlText · Cut position.", "urgent")
        }
        "SHADOW_TP" -> Triple("💰 Target Near", "$label · $pnlText · Book profit.", "urgent")
        "SHADOW_EOD" -> Triple("⏰ Exit — EOD", "$label · $pnlText · Square off before close.", "urgent")
        // Data-quality notice, not an exit signal: evaluateShadowPolicy only
        // reaches SHADOW_DEGRADED when no SL/TP/EOD rule matched, so nothing is
        // wrong with the position itself. Routed to the silent routine channel
        // so a transient quote gap cannot train the user to ignore the audible
        // position channels.
        "SHADOW_DEGRADED" -> Triple(degradedTitle, degradedBody, "routine")
        else -> null
    }
}
