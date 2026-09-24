package com.marketradar.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * R5/R7: per-leg quote source timing vs valuation_ts (post-fetch).
 * request_started_ts is diagnostic only. Quotes after request_started but
 * before valuation_ts must PASS when fresh.
 */
class ParitySourceQuoteTimingTest {

    private val requestStarted = "2026-09-23T10:00:00Z"
    private val valuation = "2026-09-23T10:00:00.500Z"

    @Test
    fun allLegsFresh_available() {
        val r = resolveParitySourceQuoteTiming(
            requiredLegKeys = listOf("L1", "L2"),
            sourceTsByInstrumentKey = mapOf(
                "L1" to "2026-09-23T09:59:30Z",
                "L2" to "2026-09-23T09:59:45Z"
            ),
            valuationTsIso = valuation,
            requestStartedTsIso = requestStarted
        )
        assertTrue(r.available)
        assertNotNull(r.quoteTs)
        assertEquals("all_required_legs_fresh", r.reason)
        assertTrue(r.legTimings.all { it.ok })
    }

    @Test
    fun oneMissing_unavailable() {
        val r = resolveParitySourceQuoteTiming(
            requiredLegKeys = listOf("L1", "L2"),
            sourceTsByInstrumentKey = mapOf(
                "L1" to "2026-09-23T09:59:30Z",
                "L2" to null
            ),
            valuationTsIso = valuation,
            requestStartedTsIso = requestStarted
        )
        assertFalse(r.available)
        assertNull(r.quoteTs)
        assertTrue(r.reason.contains("leg_source_ts_missing"))
    }

    @Test
    fun oneStale_unavailable() {
        val r = resolveParitySourceQuoteTiming(
            requiredLegKeys = listOf("L1", "L2"),
            sourceTsByInstrumentKey = mapOf(
                "L1" to "2026-09-23T09:59:30Z",
                "L2" to "2026-09-23T09:57:00Z"
            ),
            valuationTsIso = valuation,
            requestStartedTsIso = requestStarted,
            maxQuoteAgeSeconds = 90L
        )
        assertFalse(r.available)
        assertTrue(r.reason.contains("leg_quote_stale_vs_valuation"))
    }

    @Test
    fun quoteAfterValuation_unavailable() {
        val r = resolveParitySourceQuoteTiming(
            requiredLegKeys = listOf("L1", "L2"),
            sourceTsByInstrumentKey = mapOf(
                "L1" to "2026-09-23T09:59:30Z",
                "L2" to "2026-09-23T10:00:01Z"
            ),
            valuationTsIso = valuation,
            requestStartedTsIso = requestStarted
        )
        assertFalse(r.available)
        assertTrue(r.reason.contains("leg_quote_ts_after_valuation"))
    }

    @Test
    fun quote210msAfterRequestStarted_beforeValuation_acceptedWhenFresh() {
        // request_started=10:00:00.000, quote=10:00:00.210, valuation=10:00:00.500
        val r = resolveParitySourceQuoteTiming(
            requiredLegKeys = listOf("L1"),
            sourceTsByInstrumentKey = mapOf("L1" to "2026-09-23T10:00:00.210Z"),
            valuationTsIso = valuation,
            requestStartedTsIso = requestStarted
        )
        assertTrue("210ms post-request quote before valuation must pass", r.available)
        assertEquals("all_required_legs_fresh", r.reason)
    }

    @Test
    fun mixedTimestampFormatsAndOffsets_available() {
        val r = resolveParitySourceQuoteTiming(
            requiredLegKeys = listOf("L1", "L2", "L3"),
            sourceTsByInstrumentKey = mapOf(
                "L1" to "2026-09-23T09:59:30Z",
                "L2" to "2026-09-23T15:29:40+05:30",
                "L3" to "2026-09-23 09:59:50+00"
            ),
            valuationTsIso = valuation,
            requestStartedTsIso = requestStarted
        )
        assertTrue(r.available)
    }

    @Test
    fun everyRequiredLegIndependent_oneBadRejectsAll() {
        val r = resolveParitySourceQuoteTiming(
            requiredLegKeys = listOf("L1", "L2", "L3", "L4"),
            sourceTsByInstrumentKey = mapOf(
                "L1" to "2026-09-23T09:59:50Z",
                "L2" to "2026-09-23T09:59:51Z",
                "L3" to null,
                "L4" to "2026-09-23T09:59:52Z"
            ),
            valuationTsIso = valuation,
            requestStartedTsIso = requestStarted
        )
        assertFalse(r.available)
        assertTrue(r.reason.contains("leg_source_ts_missing:L3"))
    }

    @Test
    fun unparseableTime_unavailable() {
        val r = resolveParitySourceQuoteTiming(
            requiredLegKeys = listOf("L1"),
            sourceTsByInstrumentKey = mapOf("L1" to "not-a-timestamp"),
            valuationTsIso = valuation,
            requestStartedTsIso = requestStarted
        )
        assertFalse(r.available)
        assertTrue(r.reason.contains("leg_source_ts_unparseable"))
    }

    @Test
    fun parseParityInstantUtc_rejectsNaive() {
        assertNull(parseParityInstantUtc("2026-09-23T10:00:00"))
        assertNotNull(parseParityInstantUtc("2026-09-23T10:00:00Z"))
    }
}
