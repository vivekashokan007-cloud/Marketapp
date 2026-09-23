package com.marketradar.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * R5: per-leg quote source timing must be validated for every required valued leg.
 * Missing / stale / future-dated never count as agreement.
 */
class ParitySourceQuoteTimingTest {

    private val event = "2026-09-23T10:00:00Z"

    @Test
    fun allLegsFresh_available() {
        val r = resolveParitySourceQuoteTiming(
            requiredLegKeys = listOf("L1", "L2"),
            sourceTsByInstrumentKey = mapOf(
                "L1" to "2026-09-23T09:59:30Z",
                "L2" to "2026-09-23T09:59:45Z"
            ),
            eventTsIso = event
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
            eventTsIso = event
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
            eventTsIso = event,
            maxQuoteAgeSeconds = 90L
        )
        assertFalse(r.available)
        assertTrue(r.reason.contains("leg_quote_stale_vs_event"))
    }

    @Test
    fun oneFutureDated_unavailable() {
        val r = resolveParitySourceQuoteTiming(
            requiredLegKeys = listOf("L1", "L2"),
            sourceTsByInstrumentKey = mapOf(
                "L1" to "2026-09-23T09:59:30Z",
                "L2" to "2026-09-23T10:01:00Z"
            ),
            eventTsIso = event
        )
        assertFalse(r.available)
        assertTrue(r.reason.contains("leg_quote_ts_after_event"))
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
            eventTsIso = event
        )
        assertTrue(r.available)
    }

    @Test
    fun eventCapturedBeforeLaterQuoteFetch_unavailable() {
        val r = resolveParitySourceQuoteTiming(
            requiredLegKeys = listOf("L1"),
            sourceTsByInstrumentKey = mapOf("L1" to "2026-09-23T10:00:05Z"),
            eventTsIso = event
        )
        assertFalse(r.available)
        assertTrue(r.reason.contains("leg_quote_ts_after_event"))
    }

    @Test
    fun parseParityInstantUtc_rejectsNaive() {
        assertNull(parseParityInstantUtc("2026-09-23T10:00:00"))
        assertNotNull(parseParityInstantUtc("2026-09-23T10:00:00Z"))
    }
}
