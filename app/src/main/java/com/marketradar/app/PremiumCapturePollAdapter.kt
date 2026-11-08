package com.marketradar.app

import org.json.JSONObject
import java.util.Calendar
import java.util.TimeZone

/**
 * The mapping from one poll's raw results to capture inputs, as a PURE function.
 *
 * ## Why this exists as its own class
 *
 * The previous review found two integration faults that a passing JVM test had hidden, because
 * the test supplied values the service never supplies:
 *
 *  1. `x1*ChainFetchedAtMs` is assigned only when the chain response is non-null, so on a real
 *     chain failure the capture hook passed `responseReceivedMs = 0` and `receiptHhmm = null`.
 *     With no scheduler slot either, `observeSlot` could only answer NO_SLOT_IDENTITY, so the six
 *     `MISSING_REQUEST_FAILED` records were never written — exactly the gap capture exists to make
 *     visible.
 *  2. The hook sat below `if (quotesJson == null) return`, so a failure of the separate
 *     index-quotes request discarded BOTH good chains.
 *
 * A source-position assertion cannot catch either one. This adapter takes the same arguments the
 * service has in hand and returns exactly what is handed to the coordinator, so a test can assert
 * the real mapping for every failure shape.
 *
 * It is deliberately free of Android types so it runs under plain JUnit.
 */
object PremiumCapturePollAdapter {

    const val ADAPTER_VERSION = "premium_capture_poll_adapter_v1_capture_own_completion_clocks"

    /** 09:15, matching the service's poll grid. Duplicated as a constant, asserted in tests. */
    const val MARKET_OPEN_MINUTE = 9 * 60 + 15
    const val POLL_SLOT_MINUTES = 5

    /**
     * One index's raw poll result.
     *
     * `completedAtMs` is the capture-owned **request completion** clock and is set on success AND
     * on failure. It is NOT a vendor quote time and is not the existing `x1*FetchedAtMs`, whose
     * success-only semantics other code depends on and which this does not change.
     */
    data class IndexResult(
        val index: String,
        val chain: JSONObject?,
        val requestStartedMs: Long,
        val completedAtMs: Long,
        /** Nullable to match `ChainAcquisition.requestedUnderlyingKey`: an unknown index key is
         *  carried as absent rather than as a guess. */
        val underlyingKey: String?,
        val expiry: String?,
        val vendorSourceTs: String? = null,
    )

    /** IST HH:MM, or null for a clock that never ran. A non-positive stamp is unknown, never 0. */
    fun istHhmmOrNull(ms: Long): String? {
        if (ms <= 0L) return null
        val cal = Calendar.getInstance(TimeZone.getTimeZone("Asia/Kolkata"))
            .apply { timeInMillis = ms }
        return "%02d:%02d".format(cal.get(Calendar.HOUR_OF_DAY), cal.get(Calendar.MINUTE))
    }

    /**
     * The scheduler's INTENDED slot for this poll, derived from the service's `slotKey`
     * (`"YYYY-MM-DD|ordinal"`). Kept separate from the acquisition clock on purpose: it records
     * intent, and never relabels a response that completed in a different slot.
     */
    fun declaredSlotFromSlotKey(slotKey: String?): String? {
        val ordinal = slotKey?.substringAfter('|')?.toIntOrNull() ?: return null
        if (ordinal < 1) return null
        val m = MARKET_OPEN_MINUTE + (ordinal - 1) * POLL_SLOT_MINUTES
        if (m < 0 || m >= 24 * 60) return null
        return "%02d:%02d".format(m / 60, m % 60)
    }

    /**
     * Map poll results to coordinator inputs.
     *
     * A failed chain keeps its real completion clock, so its slot is honest and the per-contract
     * missing outcomes can be written once a cohort exists. Nothing is invented for it: the chain
     * stays null and only the clock it genuinely has is carried.
     */
    fun acquisitions(results: List<IndexResult>): List<PremiumCaptureCoordinator.ChainAcquisition> =
        results.map { r ->
            PremiumCaptureCoordinator.ChainAcquisition(
                index = r.index,
                rawChain = r.chain,
                // real clocks only; a stamp that never ran is unknown, not zero
                requestStartedMs = r.requestStartedMs.takeIf { it > 0L },
                responseReceivedMs = r.completedAtMs.takeIf { it > 0L },
                requestedUnderlyingKey = r.underlyingKey,
                expiry = r.expiry,
                // the capture-owned completion clock, present on success AND failure
                receiptHhmm = istHhmmOrNull(r.completedAtMs),
                vendorSourceTs = r.vendorSourceTs,
            )
        }
}
