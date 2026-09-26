package com.marketradar.app

import org.json.JSONObject

/*
 * B3 item 3 (2026-09-26), Paper only: bounded quote refresh and
 * persistent-failure escalation for invalid/untrusted marks.
 *
 * 1. Bounded refresh — when a Paper mark fails per-leg quote validity for a
 *    reason a re-fetch can plausibly cure (missing/stale/future/crossed quote,
 *    transient request failure), the tick service re-fetches ONLY those legs,
 *    at most once per tick and for at most QUOTE_REFRESH_MAX_KEYS keys. The
 *    refreshed legs are re-validated by the same validator; a refreshed quote
 *    whose vendor time is still stale stays invalid, so repeated receipts of
 *    an unchanged stale quote never restore trust. Real trades always value
 *    from the original response (Real inputs unchanged).
 *
 * 2. Escalation — a Paper position whose mark stays UNTRUSTED for
 *    MARK_FAILURE_ESCALATE_AFTER_TICKS consecutive ticks AND at least
 *    MARK_FAILURE_ESCALATE_AFTER_MS of observed failure time gets ONE
 *    escalation notice per failure episode, with a durable identity. A failed
 *    post stays retryable; only an OS-accepted post consumes it. A trusted
 *    tick closes the episode.
 *
 * These are monitoring/data-quality parameters. They do not change any stop,
 * target, EOD or ranking threshold, and they never produce a stop/target
 * instruction. Everything here is pure so it is executed by unit tests.
 */

internal const val QUOTE_REFRESH_CONTRACT = "b3_item3_bounded_invalid_leg_refresh_v1"
internal const val MARK_FAILURE_ESCALATION_CONTRACT = "b3_item3_mark_failure_escalation_v1"

internal const val QUOTE_REFRESH_MAX_REQUESTS_PER_TICK = 1
internal const val QUOTE_REFRESH_MAX_KEYS = 20
internal const val QUOTE_REFRESH_DELAY_MS = 1_000L

internal const val MARK_FAILURE_ESCALATE_AFTER_TICKS = 5
internal const val MARK_FAILURE_ESCALATE_AFTER_MS = 4 * 60_000L
/** A gap between ticks larger than this (service stopped, Doze) is not counted as observed failure time. */
internal const val MARK_FAILURE_MAX_COUNTED_GAP_MS = 120_000L
internal const val MARK_FAILURE_EPISODES_MAX = 50

private val REFRESHABLE_LEG_REASON_PREFIXES = listOf(
    "no_quote",
    "book:",
    "duplicate_source_quote",
    "source_ts_missing",
    "source_ts_unparseable",
    "source_ts_future:",
    "source_stale:",
    "source_outside_regular_session",
    "source_different_session_date"
)

/** Mark-level reasons a re-fetch cannot cure. */
private val NON_REFRESHABLE_GLOBAL_PREFIXES = listOf(
    "structure:",
    "no_required_legs",
    "quantity_",
    "receipt_ts_invalid",
    "receipt_outside_regular_session",
    "contract_expired"
)

/** Fetch failures a re-fetch cannot cure (auth/token), versus transient ones. */
internal fun isTransientFetchStatus(status: String): Boolean = when {
    status == "OK" -> true
    status == "NO_TOKEN" || status == "NO_KEYS" -> false
    status.startsWith("AUTH_REJECTED") -> false
    status.startsWith("HTTP_4") && status != "HTTP_408" && status != "HTTP_429" -> false
    else -> true // HTTP_5xx, HTTP_408/429, EXCEPTION_*
}

private fun isRefreshableLegReason(reason: String): Boolean =
    REFRESHABLE_LEG_REASON_PREFIXES.any { reason.startsWith(it) }

/**
 * Keys of the invalid legs of one Paper mark that a re-fetch may cure, or an
 * empty list when the mark is valid or cannot become valid by re-fetching
 * (identity/structure/quantity/expiry/auth problems, or an invalid leg with a
 * non-refreshable reason such as an inexact key match).
 */
internal fun refreshableInvalidLegKeys(validity: PositionQuoteValidity): List<String> {
    if (validity.valid) return emptyList()
    if (!isTransientFetchStatus(validity.fetchStatus)) return emptyList()
    val global = validity.reasons.filterNot { it.startsWith("leg:") }
    if (global.any { g -> NON_REFRESHABLE_GLOBAL_PREFIXES.any { g.startsWith(it) } }) return emptyList()
    val keys = linkedSetOf<String>()
    for (leg in validity.legs) {
        if (leg.ok) continue
        if (leg.instrumentKey.isBlank()) return emptyList()
        if (leg.reasons.any { !isRefreshableLegReason(it) }) return emptyList()
        keys.add(leg.instrumentKey)
    }
    // Inter-leg skew alone: the legs older than the newest leg by more than the
    // skew limit are the ones to re-fetch.
    if (global.any { it.startsWith("inter_leg_skew:") }) {
        val latest = validity.legs.mapNotNull { it.sourceMs }.maxOrNull()
        if (latest != null) {
            validity.legs.forEach { lv ->
                val ms = lv.sourceMs
                if (ms != null && latest - ms > QUOTE_MAX_INTER_LEG_SKEW_MS && lv.instrumentKey.isNotBlank()) {
                    keys.add(lv.instrumentKey)
                }
            }
        }
    }
    return keys.toList()
}

internal data class QuoteRefreshPlan(
    val keys: List<String>,
    val requestedByTrade: Map<String, List<String>>,
    val truncated: Boolean,
    val skipReason: String?
)

/**
 * One request per tick, capped key count, in trade order, de-duplicated so a
 * leg shared by two same-index trades is fetched once.
 */
internal fun planQuoteRefresh(
    invalidKeysByTrade: Map<String, List<String>>,
    maxKeys: Int = QUOTE_REFRESH_MAX_KEYS
): QuoteRefreshPlan {
    val all = linkedSetOf<String>()
    invalidKeysByTrade.values.forEach { all.addAll(it) }
    if (all.isEmpty()) return QuoteRefreshPlan(emptyList(), emptyMap(), false, "NO_REFRESHABLE_INVALID_LEGS")
    val keys = all.take(maxKeys)
    val keySet = keys.toSet()
    val byTrade = invalidKeysByTrade
        .mapValues { (_, v) -> v.filter { it in keySet } }
        .filterValues { it.isNotEmpty() }
    return QuoteRefreshPlan(keys, byTrade, all.size > maxKeys, null)
}

/**
 * Replace ONLY [refreshKeys] that the refresh response matched exactly.
 * Every other key keeps the original quote and exactness.
 */
internal fun <Q> mergeRefreshedLegQuotes(
    original: Map<String, Q>,
    originalExact: Set<String>,
    refreshed: Map<String, Q>,
    refreshedExact: Set<String>,
    refreshKeys: List<String>
): Triple<Map<String, Q>, Set<String>, List<String>> {
    val quotes = original.toMutableMap()
    val exact = originalExact.toMutableSet()
    val replaced = mutableListOf<String>()
    for (k in refreshKeys) {
        if (k in refreshedExact) {
            val q = refreshed[k] ?: continue
            quotes[k] = q
            exact.add(k)
            replaced.add(k)
        }
    }
    return Triple(quotes, exact, replaced)
}

/**
 * Fetch status used for Paper validation after a refresh. A transient first
 * failure is cured only when the refresh succeeded AND replaced every key the
 * trade needed; otherwise the original status stands.
 */
internal fun mergedFetchStatus(
    originalStatus: String,
    refreshStatus: String?,
    tradeKeys: List<String>,
    replacedKeys: Collection<String>
): String {
    if (originalStatus == "OK") return "OK"
    if (refreshStatus != "OK") return originalStatus
    return if (tradeKeys.isNotEmpty() && replacedKeys.containsAll(tradeKeys)) "OK" else originalStatus
}

// ---------------------------------------------------------------------------
// Persistent-failure episodes
// ---------------------------------------------------------------------------

internal data class MarkFailureStep(
    val state: JSONObject?,
    val shouldEscalate: Boolean,
    val episodeClosed: Boolean
)

/**
 * Advances one Paper trade's failure episode by one tick.
 *
 * Observed failure time accumulates by at most MARK_FAILURE_MAX_COUNTED_GAP_MS
 * per tick (a stopped service/Doze gap is not evidence of failure) and never
 * by a negative amount (clock rollback cannot fabricate elapsed time).
 */
internal fun advanceMarkFailureEpisode(
    prev: JSONObject?,
    tradeId: String,
    sessionDate: String,
    untrusted: Boolean,
    cause: String?,
    nowMs: Long
): MarkFailureStep {
    if (!untrusted) return MarkFailureStep(null, false, prev != null)
    val continuing = prev != null && prev.optString("session_date", "") == sessionDate
    val state = if (!continuing) {
        JSONObject().apply {
            put("contract", MARK_FAILURE_ESCALATION_CONTRACT)
            put("trade_id", tradeId)
            put("session_date", sessionDate)
            put("episode_id", "$tradeId|$sessionDate|$nowMs")
            put("started_ms", nowMs)
            put("consecutive_ticks", 1)
            put("observed_failure_ms", 0L)
            put("last_tick_ms", nowMs)
            put("first_cause", cause ?: JSONObject.NULL)
            put("last_cause", cause ?: JSONObject.NULL)
            put("escalation_posted", false)
            put("escalation_attempts", 0)
            put("escalation_last_class", JSONObject.NULL)
            put("escalation_posted_ms", JSONObject.NULL)
        }
    } else {
        JSONObject(prev!!.toString()).apply {
            val gap = nowMs - optLong("last_tick_ms", nowMs)
            val counted = gap.coerceIn(0L, MARK_FAILURE_MAX_COUNTED_GAP_MS)
            put("consecutive_ticks", optInt("consecutive_ticks", 0) + 1)
            put("observed_failure_ms", optLong("observed_failure_ms", 0L) + counted)
            put("last_tick_ms", maxOf(nowMs, optLong("last_tick_ms", nowMs)))
            put("last_cause", cause ?: JSONObject.NULL)
        }
    }
    val due = !state.optBoolean("escalation_posted", false) &&
        state.optInt("consecutive_ticks", 0) >= MARK_FAILURE_ESCALATE_AFTER_TICKS &&
        state.optLong("observed_failure_ms", 0L) >= MARK_FAILURE_ESCALATE_AFTER_MS
    return MarkFailureStep(state, due, false)
}

/** Records one escalation delivery attempt; only an OS-accepted post consumes it. */
internal fun recordMarkFailureEscalationAttempt(state: JSONObject, deliveryClass: String, nowMs: Long): JSONObject =
    JSONObject(state.toString()).apply {
        put("escalation_attempts", optInt("escalation_attempts", 0) + 1)
        put("escalation_last_class", deliveryClass)
        put("escalation_last_attempt_ms", nowMs)
        if (deliveryClass == DELIVERY_POSTED) {
            put("escalation_posted", true)
            put("escalation_posted_ms", nowMs)
        }
        put("escalation_retry_pending", deliveryClass != DELIVERY_POSTED)
        put("user_saw_notification", "UNKNOWN_OS_POST_IS_NOT_USER_ACK")
    }

/** Keeps only open trades (when known) and caps the blob size, newest first. */
internal fun pruneMarkFailureEpisodes(all: JSONObject, liveTradeIds: Set<String>?): JSONObject {
    val entries = mutableListOf<Pair<String, JSONObject>>()
    all.keys().forEach { k -> all.optJSONObject(k)?.let { entries.add(k to it) } }
    val kept = entries
        .filter { (k, _) -> liveTradeIds == null || liveTradeIds.contains(k) }
        .sortedByDescending { (_, e) -> e.optLong("last_tick_ms", 0L) }
        .take(MARK_FAILURE_EPISODES_MAX)
    return JSONObject().apply { kept.forEach { (k, e) -> put(k, e) } }
}

internal fun markFailureEscalationText(label: String, state: JSONObject): Pair<String, String> {
    val minutes = state.optLong("observed_failure_ms", 0L) / 60_000L
    val cause = state.optString("last_cause", "").ifBlank { CAUSE_UNRESOLVED }
    val title = "🚨 Position Monitoring At Risk"
    val body = "$label · no trusted mark for ${state.optInt("consecutive_ticks", 0)} ticks (~${minutes} min, $cause) · " +
        "stop/target are not being evaluated · check the position directly."
    return title to body
}
