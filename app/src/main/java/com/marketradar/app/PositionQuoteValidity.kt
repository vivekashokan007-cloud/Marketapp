package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.time.DayOfWeek
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import kotlin.math.abs
import kotlin.math.max

/*
 * B3 + A1 (2026-09-26): one shared quote-validity and mark-trust contract for
 * every Paper consumer of a P1 executable mark — native tick valuation,
 * PositionMarkStore presentation, the Brain P1 bridge (via the store), shadow
 * stop/target alerts, trusted extrema and Paper manual close.
 *
 * Everything in this file is pure (no Android, prefs, clock or I/O) so the
 * decision paths are executed by unit tests rather than asserted as source text.
 *
 * Vocabulary (kept deliberately separate):
 *  - raw observation  : what the venue returned (always preserved in legs_json).
 *  - quote validity   : per-leg book + source-time + identity checks (this file).
 *  - mark trust       : A1 — quote validity AND structural-bound consistency.
 *  - display mark     : PositionMarkStore last-valid mark (display-only when stale).
 *  - close permission : Paper close requires quote validity; bound trust is
 *                       recorded but a wide executable book remains closable.
 *
 * Measured fact (production position_ticks, 24 Sep 2026, 332 ticks): the Upstox
 * quote `timestamp` field sat 0.20–1.01 s AFTER the request start on every leg,
 * i.e. it behaves like a response/server time, not a book-update time. It is
 * therefore recorded as VENDOR_TIMESTAMP_SEMANTICS_UNVERIFIED. A stale vendor
 * time is proof of staleness; a fresh vendor time is NOT proof that the book
 * moved. Recovery from UNTRUSTED therefore also requires a changed book.
 */

internal const val QUOTE_VALIDITY_CONTRACT = "b3_quote_validity_v1_per_leg_source_time_exact_key_session_expiry"
internal const val MARK_TRUST_CONTRACT = "a1_mark_trust_v2_quote_validity_plus_structural_bounds_plus_b3_1_book_width_gross_basis"
internal const val SOURCE_TIME_KIND = "VENDOR_TIMESTAMP_SEMANTICS_UNVERIFIED"
internal const val SESSION_CALENDAR_BASIS = "regular_weekday_0915_1530_ist_no_holiday_or_special_session_calendar"

/** Validity parameters (data-quality limits, not trading policy). */
internal const val QUOTE_MAX_SOURCE_AGE_MS = 90_000L          // same limit as R5/R7 parity timing
internal const val QUOTE_MAX_FUTURE_SKEW_MS = 5_000L          // device/vendor clock tolerance
internal const val QUOTE_MAX_INTER_LEG_SKEW_MS = 60_000L      // all legs within one tick interval
internal const val BOUND_REFERENCE_REL_TOLERANCE = 0.05       // stored vs structure-derived bounds

internal const val QV_VALID = "VALID"
internal const val QV_INVALID = "INVALID"
internal const val TRUST_TRUSTED = "TRUSTED"
internal const val TRUST_UNTRUSTED = "UNTRUSTED"

internal const val CAUSE_QUOTE_INVALIDITY = "QUOTE_INVALIDITY"
internal const val CAUSE_BOUND_REFERENCE_MISMATCH = "BOUND_REFERENCE_MISMATCH"
internal const val CAUSE_WIDE_LIQUIDATION_BOOK = "WIDE_LIQUIDATION_BOOK"
internal const val CAUSE_UNRESOLVED = "UNRESOLVED"
internal const val CAUSE_AWAITING_REVALIDATION = "AWAITING_REVALIDATION"
internal const val CAUSE_NO_ACCEPTED_VALUATION = "NO_ACCEPTED_VALUATION"
internal const val CAUSE_WIDE_EXECUTABLE_BOOK = "WIDE_EXECUTABLE_BOOK"

/*
 * B3.1 (2026-09-26) book-width validity. One named, versioned place.
 *
 * A Paper mark whose executable (liquidation) P&L sits further from its mid P&L
 * than WIDE_EXECUTABLE_BOOK_MAX_GAP_FRACTION_OF_MAX_LOSS x stored max loss is
 * UNTRUSTED with cause WIDE_EXECUTABLE_BOOK, even when it lies inside the
 * structural bounds. Replay over every stored position_ticks row since
 * 10 Sep 2026 (10,351 valued ticks, read-only SELECT): normal ticks have
 * gap/max_loss p99 <= 0.021 per trade and max 0.144 (276, 17 Sep 09:15); the
 * opening-book artifacts are >= 0.362. 0.20 sits inside that empty band and
 * flags exactly six ticks (274: 11/15/16/17 Sep 09:15; 276: 15/16 Sep 09:15).
 */
internal const val WIDE_EXECUTABLE_BOOK_CONTRACT = "b3_1_wide_executable_book_v1_gap_vs_stored_max_loss"
internal const val WIDE_EXECUTABLE_BOOK_MAX_GAP_FRACTION_OF_MAX_LOSS = 0.20
internal const val BOOK_WIDTH_OK = "OK"
internal const val BOOK_WIDTH_WIDE = "WIDE"
internal const val BOOK_WIDTH_UNMEASURABLE = "UNMEASURABLE"

/*
 * B3.1 stop fallback on mid. A Paper mark untrusted ONLY because the book is
 * wide (WIDE_LIQUIDATION_BOOK / WIDE_EXECUTABLE_BOOK) with VALID current quotes
 * may still produce a stop when its mid P&L is at or below the SL threshold.
 * Never for quote invalidity, never for targets.
 */
internal const val MID_FALLBACK_CONTRACT = "b3_1_mid_fallback_stop_v1_wide_book_valid_quotes_sl_only"
internal const val STOP_BASIS_MID_FALLBACK_WIDE_BOOK = "MID_FALLBACK_WIDE_BOOK"
internal val MID_FALLBACK_ELIGIBLE_CAUSES = setOf(CAUSE_WIDE_LIQUIDATION_BOOK, CAUSE_WIDE_EXECUTABLE_BOOK)

private val IST_ZONE: ZoneId = ZoneId.of("Asia/Kolkata")
private const val SESSION_OPEN_MIN = 9 * 60 + 15
private const val SESSION_CLOSE_MIN = 15 * 60 + 30

/** One leg's quote as received, before any validity decision. */
internal data class LegSourceQuote(
    val instrumentKey: String,
    val present: Boolean,
    val exactKeyMatch: Boolean,
    val responseKey: String?,
    val bid: Double?,
    val ask: Double?,
    val sourceTs: String?,
    val lastTradeTs: String? = null
)

internal data class LegQuoteValidity(
    val instrumentKey: String,
    val ok: Boolean,
    val reasons: List<String>,
    val sourceTs: String?,
    val sourceMs: Long?,
    val sourceAgeMs: Long?,
    val lastTradeTs: String?,
    val exactKeyMatch: Boolean
)

internal data class PositionQuoteValidity(
    val state: String,
    val reasons: List<String>,
    val legs: List<LegQuoteValidity>,
    val receiptTs: String,
    val receiptMs: Long?,
    val earliestSourceMs: Long?,
    val latestSourceMs: Long?,
    val earliestSourceTs: String?,
    val maxSourceAgeMs: Long?,
    val interLegSkewMs: Long?,
    val bookFingerprint: String?,
    val fetchStatus: String
) {
    val valid: Boolean get() = state == QV_VALID

    fun toJson(): JSONObject = JSONObject().apply {
        put("contract", QUOTE_VALIDITY_CONTRACT)
        put("state", state)
        put("reasons", JSONArray(reasons))
        put("receipt_ts", receiptTs)
        put("source_time_kind", SOURCE_TIME_KIND)
        put("session_calendar_basis", SESSION_CALENDAR_BASIS)
        put("earliest_source_ts", earliestSourceTs ?: JSONObject.NULL)
        put("earliest_source_ms", earliestSourceMs ?: JSONObject.NULL)
        put("latest_source_ms", latestSourceMs ?: JSONObject.NULL)
        put("max_source_age_ms", maxSourceAgeMs ?: JSONObject.NULL)
        put("inter_leg_skew_ms", interLegSkewMs ?: JSONObject.NULL)
        put("book_fingerprint", bookFingerprint ?: JSONObject.NULL)
        put("fetch_status", fetchStatus)
        put("max_source_age_limit_ms", QUOTE_MAX_SOURCE_AGE_MS)
        put("max_future_skew_limit_ms", QUOTE_MAX_FUTURE_SKEW_MS)
        put("max_inter_leg_skew_limit_ms", QUOTE_MAX_INTER_LEG_SKEW_MS)
    }

    fun legFor(key: String?): LegQuoteValidity? = legs.firstOrNull { it.instrumentKey == key }
}

private fun isRegularSessionInstant(instant: Instant): Boolean {
    val ist = instant.atZone(IST_ZONE)
    if (ist.dayOfWeek == DayOfWeek.SATURDAY || ist.dayOfWeek == DayOfWeek.SUNDAY) return false
    val minutes = ist.hour * 60 + ist.minute
    return minutes in SESSION_OPEN_MIN until SESSION_CLOSE_MIN
}

private fun Double.fp(): String = if (this.isFinite()) "%.4f".format(java.util.Locale.US, this) else "nan"

/**
 * Validates every required leg's quote for trusted Paper use.
 *
 * [bookStatusByKey] is the existing per-leg `quote_status` from valuePositionTick
 * (OK / NO_QUOTE / NO_DEPTH / CROSSED_QUOTE / NON_POSITIVE_QUOTE / KEY_MISSING) —
 * reused, not duplicated. [receiptTsIso] is the instant AFTER the HTTP response
 * (valuation_ts). It is never substituted for a missing source time.
 */
internal fun validatePositionQuotes(
    requiredLegKeys: List<String?>,
    structureStatus: String,
    bookStatusByKey: Map<String, String>,
    quotes: Map<String, LegSourceQuote>,
    receiptTsIso: String,
    expiry: LocalDate?,
    quantityUnits: Double?,
    quantityAuthoritative: Boolean,
    fetchStatus: String = "OK",
    maxSourceAgeMs: Long = QUOTE_MAX_SOURCE_AGE_MS,
    maxFutureSkewMs: Long = QUOTE_MAX_FUTURE_SKEW_MS,
    maxInterLegSkewMs: Long = QUOTE_MAX_INTER_LEG_SKEW_MS
): PositionQuoteValidity {
    val reasons = linkedSetOf<String>()
    val receiptInstant = parseParityInstantUtc(receiptTsIso)
    if (receiptInstant == null) reasons.add("receipt_ts_invalid")
    if (fetchStatus != "OK") reasons.add("fetch:$fetchStatus")
    if (structureStatus != STRUCTURE_ROLES_OK) reasons.add("structure:$structureStatus")
    if (requiredLegKeys.isEmpty()) reasons.add("no_required_legs")
    if (quantityUnits == null || !quantityUnits.isFinite() || quantityUnits <= 0.0) {
        reasons.add("quantity_unresolved")
    } else if (!quantityAuthoritative) {
        reasons.add("quantity_not_authoritative")
    }
    if (receiptInstant != null) {
        if (!isRegularSessionInstant(receiptInstant)) reasons.add("receipt_outside_regular_session")
        if (expiry != null && receiptInstant.atZone(IST_ZONE).toLocalDate().isAfter(expiry)) {
            reasons.add("contract_expired")
        }
    }

    val responseKeyUse = mutableMapOf<String, Int>()
    requiredLegKeys.filterNotNull().forEach { k ->
        quotes[k]?.takeIf { it.present }?.responseKey?.let { rk -> responseKeyUse[rk] = (responseKeyUse[rk] ?: 0) + 1 }
    }

    val legs = requiredLegKeys.map { rawKey ->
        val key = rawKey?.trim().orEmpty()
        val legReasons = mutableListOf<String>()
        if (key.isEmpty()) {
            legReasons.add("key_missing")
            return@map LegQuoteValidity("", false, legReasons, null, null, null, null, false)
        }
        val q = quotes[key]
        val book = bookStatusByKey[key] ?: "NO_QUOTE"
        if (q == null || !q.present) legReasons.add("no_quote")
        if (book != "OK") legReasons.add("book:$book")
        if (q != null && q.present && !q.exactKeyMatch) legReasons.add("key_match_inexact")
        if (q?.responseKey != null && (responseKeyUse[q.responseKey] ?: 0) > 1) legReasons.add("duplicate_source_quote")
        val srcRaw = q?.sourceTs?.trim()?.takeIf { it.isNotEmpty() && it != "null" }
        val srcInstant = parseParityInstantUtc(srcRaw)
        var ageMs: Long? = null
        when {
            q == null || !q.present -> Unit
            srcRaw == null -> legReasons.add("source_ts_missing")
            srcInstant == null -> legReasons.add("source_ts_unparseable")
            receiptInstant != null -> {
                val delta = receiptInstant.toEpochMilli() - srcInstant.toEpochMilli()
                ageMs = delta
                if (delta < -maxFutureSkewMs) legReasons.add("source_ts_future:${-delta}ms")
                else if (delta > maxSourceAgeMs) legReasons.add("source_stale:${delta}ms")
                if (!isRegularSessionInstant(srcInstant)) legReasons.add("source_outside_regular_session")
                if (srcInstant.atZone(IST_ZONE).toLocalDate() != receiptInstant.atZone(IST_ZONE).toLocalDate()) {
                    legReasons.add("source_different_session_date")
                }
            }
        }
        LegQuoteValidity(
            instrumentKey = key,
            ok = legReasons.isEmpty(),
            reasons = legReasons,
            sourceTs = srcRaw,
            sourceMs = srcInstant?.toEpochMilli(),
            sourceAgeMs = ageMs,
            lastTradeTs = q?.lastTradeTs,
            exactKeyMatch = q?.exactKeyMatch == true
        )
    }
    legs.filter { !it.ok }.forEach { lv ->
        lv.reasons.forEach { r -> reasons.add("leg:${lv.instrumentKey.ifBlank { "?" }}:$r") }
    }

    val sourceMs = legs.mapNotNull { it.sourceMs }
    val skew = if (sourceMs.size == legs.size && sourceMs.isNotEmpty()) sourceMs.max() - sourceMs.min() else null
    if (skew != null && skew > maxInterLegSkewMs) reasons.add("inter_leg_skew:${skew}ms")
    val earliest = sourceMs.minOrNull()
    val earliestTs = legs.filter { it.sourceMs == earliest && earliest != null }.firstOrNull()?.sourceTs
    val maxAge = legs.mapNotNull { it.sourceAgeMs }.maxOrNull()

    val fingerprint = if (legs.isNotEmpty() && legs.all { quotes[it.instrumentKey]?.present == true }) {
        legs.joinToString("|") { lv ->
            val q = quotes[lv.instrumentKey]
            "${lv.instrumentKey}=${q?.bid?.fp() ?: "na"}/${q?.ask?.fp() ?: "na"}"
        }
    } else null

    return PositionQuoteValidity(
        state = if (reasons.isEmpty()) QV_VALID else QV_INVALID,
        reasons = reasons.toList(),
        legs = legs,
        receiptTs = receiptTsIso,
        receiptMs = receiptInstant?.toEpochMilli(),
        earliestSourceMs = earliest,
        latestSourceMs = sourceMs.maxOrNull(),
        earliestSourceTs = earliestTs,
        maxSourceAgeMs = maxAge,
        interLegSkewMs = skew,
        bookFingerprint = fingerprint,
        fetchStatus = fetchStatus
    )
}

/** Gross defined-risk bounds implied by the authoritative structure. */
internal data class StructuralBounds(val maxProfit: Double, val maxLoss: Double, val width: Double)

/**
 * Derives gross max profit / max loss from strikes, entry premium and total
 * units. Returns null when the structure cannot be derived honestly (unsupported
 * strategy, missing strikes, non-positive width or premium outside the width).
 */
internal fun expectedStructuralBounds(
    strategyType: String,
    legs: List<PositionLeg>,
    entryPremium: Double?,
    isCredit: Boolean,
    quantityUnits: Double?
): StructuralBounds? {
    if (entryPremium == null || !entryPremium.isFinite() || entryPremium <= 0.0) return null
    if (quantityUnits == null || !quantityUnits.isFinite() || quantityUnits <= 0.0) return null
    if (validateStructure(strategyType, legs).status != STRUCTURE_ROLES_OK) return null
    fun wing(type: String): Double? {
        val s = legs.firstOrNull { it.side == "SHORT" && it.optionType.equals(type, true) }?.strike
        val l = legs.firstOrNull { it.side == "LONG" && it.optionType.equals(type, true) }?.strike
        if (s == null || l == null || !s.isFinite() || !l.isFinite()) return null
        return abs(s - l)
    }
    val st = strategyType.uppercase()
    val width = when (st) {
        "IRON_CONDOR", "IRON_BUTTERFLY" -> {
            val ce = wing("CE") ?: return null
            val pe = wing("PE") ?: return null
            max(ce, pe)
        }
        "BEAR_CALL", "BULL_CALL" -> wing("CE") ?: return null
        "BULL_PUT", "BEAR_PUT" -> wing("PE") ?: return null
        else -> return null
    }
    if (width <= 0.0 || entryPremium >= width) return null
    return if (isCredit) {
        StructuralBounds(entryPremium * quantityUnits, (width - entryPremium) * quantityUnits, width)
    } else {
        if (st == "IRON_CONDOR" || st == "IRON_BUTTERFLY") return null
        StructuralBounds((width - entryPremium) * quantityUnits, entryPremium * quantityUnits, width)
    }
}

/** B3.1: executable-vs-mid gap measured against the position's own max loss. */
internal data class BookWidthCheck(
    val status: String,
    val gap: Double?,
    val maxLossRef: Double?,
    val maxLossSource: String,
    val threshold: Double?,
    val fraction: Double = WIDE_EXECUTABLE_BOOK_MAX_GAP_FRACTION_OF_MAX_LOSS
) {
    val gapFraction: Double? get() = if (gap != null && maxLossRef != null && maxLossRef > 0.0) gap / maxLossRef else null

    fun toJson(): JSONObject = JSONObject().apply {
        put("contract", WIDE_EXECUTABLE_BOOK_CONTRACT)
        put("status", status)
        put("max_gap_fraction_of_max_loss", fraction)
        put("threshold_rupees", threshold?.takeIf { it.isFinite() } ?: JSONObject.NULL)
        put("gap_rupees", gap?.takeIf { it.isFinite() } ?: JSONObject.NULL)
        put("gap_fraction_of_max_loss", gapFraction?.takeIf { it.isFinite() } ?: JSONObject.NULL)
        put("max_loss_ref", maxLossRef?.takeIf { it.isFinite() } ?: JSONObject.NULL)
        put("max_loss_source", maxLossSource)
    }
}

/**
 * B3.1: |executable P&L - mid P&L| vs WIDE_EXECUTABLE_BOOK_MAX_GAP_FRACTION_OF_MAX_LOSS
 * x max loss (stored first, structure-derived when stored is missing).
 * UNMEASURABLE when either P&L or a positive max loss is unavailable.
 */
internal fun assessBookWidth(
    executablePnl: Double?,
    midPnl: Double?,
    storedMaxLoss: Double?,
    expectedMaxLoss: Double?,
    fraction: Double = WIDE_EXECUTABLE_BOOK_MAX_GAP_FRACTION_OF_MAX_LOSS
): BookWidthCheck {
    val (ref, src) = when {
        storedMaxLoss != null && storedMaxLoss.isFinite() && storedMaxLoss > 0.0 -> storedMaxLoss to "STORED"
        expectedMaxLoss != null && expectedMaxLoss.isFinite() && expectedMaxLoss > 0.0 -> expectedMaxLoss to "STRUCTURAL"
        else -> null to "UNAVAILABLE"
    }
    if (executablePnl == null || !executablePnl.isFinite() || midPnl == null || !midPnl.isFinite() || ref == null) {
        return BookWidthCheck(BOOK_WIDTH_UNMEASURABLE, null, ref, src, ref?.let { it * fraction }, fraction)
    }
    val gap = abs(executablePnl - midPnl)
    val threshold = ref * fraction
    return BookWidthCheck(if (gap > threshold) BOOK_WIDTH_WIDE else BOOK_WIDTH_OK, gap, ref, src, threshold, fraction)
}

internal data class MarkTrust(
    val state: String,
    val cause: String?,
    val boundAnomalyStored: Boolean,
    val boundAnomalyStructural: Boolean?,
    val boundReferenceStatus: String,
    val expected: StructuralBounds?,
    val midPnl: Double?,
    val detail: List<String>,
    val bookWidth: BookWidthCheck? = null
) {
    val trusted: Boolean get() = state == TRUST_TRUSTED

    fun toJson(): JSONObject = JSONObject().apply {
        put("contract", MARK_TRUST_CONTRACT)
        put("state", state)
        put("cause", cause ?: JSONObject.NULL)
        put("basis", "GROSS_EXECUTABLE")
        put("bound_anomaly_stored_refs", boundAnomalyStored)
        put("bound_anomaly_structural_refs", boundAnomalyStructural ?: JSONObject.NULL)
        put("bound_reference_status", boundReferenceStatus)
        put("expected_max_profit", expected?.maxProfit ?: JSONObject.NULL)
        put("expected_max_loss", expected?.maxLoss ?: JSONObject.NULL)
        put("structure_width", expected?.width ?: JSONObject.NULL)
        put("mid_pnl_diagnostic", midPnl?.takeIf { it.isFinite() } ?: JSONObject.NULL)
        put("book_width", bookWidth?.toJson() ?: JSONObject.NULL)
        put("detail", JSONArray(detail))
    }
}

/**
 * A1: a mark is TRUSTED only when its quotes are valid AND its gross P&L lies
 * inside the structural tolerance of both the stored and the structure-derived
 * bounds. Exceeding the envelope does not prove the quote false — an immediate
 * multi-leg liquidation can cross a wide book outside the expiry payoff — so the
 * cause is recorded (quote invalidity / bound-reference mismatch / wide
 * liquidation book / unresolved) and the mark is simply excluded from trusted
 * price policy until revalidated.
 */
internal fun classifyPositionMarkTrust(
    validity: PositionQuoteValidity,
    valuationAccepted: Boolean,
    currentPnl: Double?,
    midMark: Double?,
    entryPremium: Double?,
    isCredit: Boolean,
    quantityUnits: Double?,
    storedMaxProfit: Double?,
    storedMaxLoss: Double?,
    expected: StructuralBounds?,
    tolerance: Double = STRUCTURAL_BOUND_TOLERANCE_VALUE
): MarkTrust {
    val detail = mutableListOf<String>()
    val anomStored = violatesStructuralBounds(currentPnl, storedMaxProfit, storedMaxLoss, tolerance)
    val anomStruct = expected?.let { violatesStructuralBounds(currentPnl, it.maxProfit, it.maxLoss, tolerance) }
    val refStatus = when {
        expected == null -> "UNDERIVABLE"
        storedMaxProfit == null || storedMaxLoss == null -> "STORED_MISSING"
        relDiff(storedMaxProfit, expected.maxProfit) > BOUND_REFERENCE_REL_TOLERANCE ||
            relDiff(storedMaxLoss, expected.maxLoss) > BOUND_REFERENCE_REL_TOLERANCE -> "MISMATCH"
        else -> "CONSISTENT"
    }
    if (refStatus == "MISMATCH") detail.add("stored_bounds_disagree_with_structure")
    val midPnl = if (midMark != null && midMark.isFinite() && entryPremium != null && quantityUnits != null) {
        computePositionTickCurrentPnl(entryPremium, midMark, isCredit, quantityUnits)
    } else null

    val bookWidth = assessBookWidth(currentPnl, midPnl, storedMaxLoss, expected?.maxLoss)

    fun result(state: String, cause: String?) =
        MarkTrust(state, cause, anomStored, anomStruct, refStatus, expected, midPnl, detail,
            if (validity.valid) bookWidth else null)

    if (!validity.valid) {
        detail.addAll(validity.reasons.take(8))
        return result(TRUST_UNTRUSTED, CAUSE_QUOTE_INVALIDITY)
    }
    if (!valuationAccepted || currentPnl == null || !currentPnl.isFinite()) {
        return result(TRUST_UNTRUSTED, CAUSE_NO_ACCEPTED_VALUATION)
    }
    if (!anomStored && anomStruct != true) {
        // B3.1: inside the bounds is not enough — a wide executable book (the
        // 09:15 opening-book artifact) is untrusted by its own measure.
        if (bookWidth.status == BOOK_WIDTH_WIDE) {
            detail.add("executable_mid_gap_exceeds_book_width_limit")
            return result(TRUST_UNTRUSTED, CAUSE_WIDE_EXECUTABLE_BOOK)
        }
        if (bookWidth.status == BOOK_WIDTH_UNMEASURABLE) detail.add("book_width_unmeasurable:${bookWidth.maxLossSource}")
        return result(TRUST_TRUSTED, null)
    }

    // Anomalous: explain, never assert the quote is false.
    if (refStatus == "MISMATCH" && (anomStored != (anomStruct == true))) {
        return result(TRUST_UNTRUSTED, CAUSE_BOUND_REFERENCE_MISMATCH)
    }
    val midInside = midPnl != null &&
        !violatesStructuralBounds(midPnl, storedMaxProfit, storedMaxLoss, tolerance) &&
        (expected == null || !violatesStructuralBounds(midPnl, expected.maxProfit, expected.maxLoss, tolerance))
    if (midInside) {
        detail.add("mid_mark_inside_bounds_executable_outside")
        return result(TRUST_UNTRUSTED, CAUSE_WIDE_LIQUIDATION_BOOK)
    }
    if (refStatus == "MISMATCH") return result(TRUST_UNTRUSTED, CAUSE_BOUND_REFERENCE_MISMATCH)
    return result(TRUST_UNTRUSTED, CAUSE_UNRESOLVED)
}

/**
 * B3.1: the mid P&L a Paper stop may fall back on, or null. Eligible only when
 * the mark is UNTRUSTED for a wide-book cause AND the current quotes are VALID
 * (fresh, exact-key, complete) AND the mid P&L is finite. Quote invalidity,
 * bound-reference mismatch, unresolved and awaiting-revalidation never qualify.
 */
internal fun midFallbackStopPnl(trust: MarkTrust, validity: PositionQuoteValidity): Double? {
    if (trust.trusted) return null
    if (trust.cause !in MID_FALLBACK_ELIGIBLE_CAUSES) return null
    if (!validity.valid) return null
    return trust.midPnl?.takeIf { it.isFinite() }
}

private fun relDiff(stored: Double, expected: Double): Double {
    val denom = max(abs(expected), 1.0)
    return abs(stored - expected) / denom
}

/**
 * Recovery rule: after an UNTRUSTED mark, a TRUSTED classification only restores
 * trust when every leg's source time has advanced past the untrusted mark's
 * latest source time AND the book fingerprint changed. Repeated fresh HTTP
 * receipts of an unchanged quote therefore never restore trust.
 */
internal fun applyTrustRecovery(
    current: MarkTrust,
    validity: PositionQuoteValidity,
    priorUntrustedLatestSourceMs: Long?,
    priorUntrustedFingerprint: String?
): MarkTrust {
    if (!current.trusted) return current
    if (priorUntrustedLatestSourceMs == null && priorUntrustedFingerprint == null) return current
    val earliest = validity.earliestSourceMs
    val sourceAdvanced = earliest != null && (priorUntrustedLatestSourceMs == null || earliest > priorUntrustedLatestSourceMs)
    val bookChanged = priorUntrustedFingerprint == null || validity.bookFingerprint != priorUntrustedFingerprint
    if (sourceAdvanced && bookChanged) {
        return current.copy(detail = current.detail + "revalidated_after_untrusted")
    }
    val why = buildList {
        if (!sourceAdvanced) add("source_time_not_advanced")
        if (!bookChanged) add("book_unchanged_since_untrusted")
    }
    return current.copy(state = TRUST_UNTRUSTED, cause = CAUSE_AWAITING_REVALIDATION, detail = current.detail + why)
}

// ---------------------------------------------------------------------------
// Delivery acknowledgement (B3 item 7)
// ---------------------------------------------------------------------------

internal const val DELIVERY_POSTED = "POSTED"
internal const val DELIVERY_PERMISSION_DENIED = "PERMISSION_DENIED"
internal const val DELIVERY_CHANNEL_BLOCKED = "CHANNEL_BLOCKED"
internal const val DELIVERY_THROTTLED = "THROTTLED"
internal const val DELIVERY_EXCEPTION = "EXCEPTION"

/**
 * Maps NotificationHelper's transport outcome to the five B3 delivery classes.
 * POSTED means the OS accepted the notification — never that the user saw it.
 */
internal fun classifyNotificationDelivery(outcome: String, postedToOs: Boolean): String = when {
    postedToOs && outcome == "POSTED_TO_OS" -> DELIVERY_POSTED
    outcome == "PERMISSION_DENIED" || outcome == "APP_NOTIFICATIONS_DISABLED" -> DELIVERY_PERMISSION_DENIED
    outcome == "CHANNEL_DISABLED" -> DELIVERY_CHANNEL_BLOCKED
    outcome == "THROTTLED" -> DELIVERY_THROTTLED
    else -> DELIVERY_EXCEPTION
}

/**
 * Whether a shadow alert attempt consumes its transition and cooldown anchor.
 * Only an OS-accepted post consumes; every failure (permission denied, channel
 * blocked, throttled, exception) stays retryable on the next tick.
 *
 * Owner decision 1 (26 Sep 2026): applies to Paper AND Real. It is delivery
 * reliability, not advice. Real-behaviour change: a Real shadow SL/TP/EOD/
 * DEGRADED notice that the OS did not accept is no longer consumed; it is
 * re-attempted on each following tick while the action persists (at most one
 * attempt per tick) and consumes the transition + cooldown anchor only when
 * posted. Before this, Real consumed on the first attempt whatever happened.
 */
internal const val SHADOW_DELIVERY_CONSUME_RULE = "POSTED_ONLY_ALL_MODES_OWNER_DECISION_1_20260926"

internal fun shadowAlertAttemptConsumes(deliveryClass: String): Boolean =
    deliveryClass == DELIVERY_POSTED

internal const val SHADOW_DELIVERY_LEDGER_VERSION = "shadow_delivery_ledger_v1"
internal const val SHADOW_DELIVERY_LEDGER_MAX_ENTRIES = 200

internal fun shadowDeliveryEventId(tradeId: String, action: String, sessionDate: String): String =
    "$tradeId|$action|$sessionDate"

/**
 * Durable per-(trade, event) attempt ledger. Pure: takes and returns the JSON
 * blob stored in SharedPreferences so restart behaviour is testable.
 */
internal fun recordShadowDeliveryAttempt(
    ledger: JSONObject,
    tradeId: String,
    action: String,
    sessionDate: String,
    isPaper: Boolean,
    deliveryClass: String,
    rawOutcome: String,
    detail: String,
    consumed: Boolean,
    nowMs: Long
): JSONObject {
    val out = JSONObject(ledger.toString())
    val id = shadowDeliveryEventId(tradeId, action, sessionDate)
    val prev = out.optJSONObject(id)
    val entry = JSONObject().apply {
        put("schema_version", SHADOW_DELIVERY_LEDGER_VERSION)
        put("event_id", id)
        put("trade_id", tradeId)
        put("action", action)
        put("session_date", sessionDate)
        put("mode", if (isPaper) "PAPER" else "REAL")
        put("first_decided_at_ms", prev?.optLong("first_decided_at_ms", nowMs) ?: nowMs)
        put("attempts", (prev?.optInt("attempts", 0) ?: 0) + 1)
        put("last_attempt_ms", nowMs)
        put("last_delivery_class", deliveryClass)
        put("last_outcome", rawOutcome)
        put("last_detail", detail.take(80))
        put("posted_to_os", (prev?.optBoolean("posted_to_os", false) ?: false) || deliveryClass == DELIVERY_POSTED)
        put("posted_at_ms", when {
            deliveryClass == DELIVERY_POSTED -> nowMs
            prev != null && !prev.isNull("posted_at_ms") && prev.has("posted_at_ms") -> prev.optLong("posted_at_ms")
            else -> JSONObject.NULL
        })
        put("consumed", consumed)
        put("consume_rule", SHADOW_DELIVERY_CONSUME_RULE)
        put("retry_pending", !consumed)
        put("user_saw_notification", "UNKNOWN_OS_POST_IS_NOT_USER_ACK")
    }
    out.put(id, entry)
    return pruneShadowDeliveryLedger(out, liveTradeIds = null)
}

/** Drops closed-trade entries (when [liveTradeIds] given) and caps size (oldest first). */
internal fun pruneShadowDeliveryLedger(ledger: JSONObject, liveTradeIds: Set<String>?): JSONObject {
    val entries = mutableListOf<Pair<String, JSONObject>>()
    ledger.keys().forEach { k -> ledger.optJSONObject(k)?.let { entries.add(k to it) } }
    val kept = entries
        .filter { (_, e) -> liveTradeIds == null || liveTradeIds.contains(e.optString("trade_id", "")) }
        .sortedByDescending { (_, e) -> e.optLong("last_attempt_ms", 0L) }
        .take(SHADOW_DELIVERY_LEDGER_MAX_ENTRIES)
    return JSONObject().apply { kept.forEach { (k, e) -> put(k, e) } }
}
