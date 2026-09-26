package com.marketradar.app

/*
 * Owner decision 9 (26 Sep 2026): manual Paper close is never blocked by quote
 * validity alone. One bounded refresh (same curable-only rule, 1 s delay, one
 * request, <= QUOTE_REFRESH_MAX_KEYS keys as B3 item 3); if the quotes are still
 * invalid the close is allowed with close_quote_quality=DEGRADED and the reason
 * recorded. Pure — executed by PaperCloseQualityDecision9Test.
 *
 * Scope: this replaces only the B3 PAPER_CLOSE_QUOTE_SOURCE_INVALID block. The
 * pre-B3 book gates (crossed / non-positive / incomplete bid-ask / valuation not
 * accepted) still fail, because without both sides of every leg there is no
 * executable close premium to record. Real is never involved (Paper-only path).
 */
internal const val PAPER_CLOSE_QUALITY_CONTRACT = "b3_decision9_paper_close_one_refresh_then_degraded_v1"
internal const val PAPER_CLOSE_QUOTE_QUALITY_VALID = "VALID"
internal const val PAPER_CLOSE_QUOTE_QUALITY_DEGRADED = "DEGRADED"

internal data class PaperCloseQuality(
    val quality: String,
    val refreshAttempted: Boolean,
    val refreshKeys: List<String>,
    val reason: String?,
    /** Which validity the close is recorded against (refreshed when a refresh ran). */
    val useRefreshed: Boolean
)

/** Curable invalid legs to re-fetch once for a manual close (empty => no refresh). */
internal fun paperCloseRefreshKeys(first: PositionQuoteValidity): List<String> =
    refreshableInvalidLegKeys(first).take(QUOTE_REFRESH_MAX_KEYS)

/**
 * [refreshed] is the validity after the single refresh, or null when no refresh
 * ran (no curable key) or the refreshed book failed the executable gates.
 */
internal fun decidePaperCloseQuality(
    first: PositionQuoteValidity,
    refreshKeys: List<String>,
    refreshed: PositionQuoteValidity?
): PaperCloseQuality {
    if (first.valid) return PaperCloseQuality(PAPER_CLOSE_QUOTE_QUALITY_VALID, false, emptyList(), null, false)
    val attempted = refreshKeys.isNotEmpty()
    if (attempted && refreshed != null && refreshed.valid) {
        return PaperCloseQuality(PAPER_CLOSE_QUOTE_QUALITY_VALID, true, refreshKeys, null, true)
    }
    val basis = refreshed ?: first
    val prefix = if (attempted) "quote_invalid_after_one_refresh" else "quote_invalid_no_curable_refresh"
    val reason = (prefix + ":" + basis.reasons.joinToString(",")).take(240)
    return PaperCloseQuality(PAPER_CLOSE_QUOTE_QUALITY_DEGRADED, attempted, refreshKeys, reason, refreshed != null)
}
