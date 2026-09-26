package com.marketradar.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.time.LocalDate

/**
 * Owner decision 9 (26 Sep 2026): manual Paper close — one bounded refresh, then
 * allow the close with close_quote_quality=DEGRADED and the reason recorded.
 * Real-behaviour note: none (capturePaperCloseQuote rejects non-Paper trades first).
 */
class PaperCloseQualityDecision9Test {
    private fun leg(key: String, side: String, type: String, strike: Double) = PositionLeg(
        instrumentKey = key, side = side,
        closeSide = if (side == "SHORT") CloseSide.BUY_TO_CLOSE else CloseSide.SELL_TO_CLOSE,
        optionType = type, strike = strike
    )
    private val legs = listOf(
        leg("NSE_FO|69801", "SHORT", "CE", 56500.0), leg("NSE_FO|69826", "LONG", "CE", 57700.0),
        leg("NSE_FO|69802", "SHORT", "PE", 56500.0), leg("NSE_FO|69776", "LONG", "PE", 55300.0)
    )
    private val quotes = mapOf(
        "NSE_FO|69801" to LegQuote(522.85, 524.7, 523.05), "NSE_FO|69826" to LegQuote(148.95, 149.9, 149.0),
        "NSE_FO|69802" to LegQuote(709.95, 713.25, 708.3), "NSE_FO|69776" to LegQuote(268.4, 270.0, 269.1)
    )

    private fun validity(ts: (String) -> String? = { "2026-09-15T09:15:59.4+05:30" },
                         exact: (String) -> Boolean = { true }): PositionQuoteValidity {
        val v = valuePositionTick("IRON_BUTTERFLY", legs, quotes, true, 850.1, null, null, 30.0)
        val src = legs.associate { l ->
            val k = l.instrumentKey!!
            val q = quotes[k]
            k to LegSourceQuote(k, true, exact(k), "RESP:$k", q?.bid, q?.ask, ts(k), null)
        }
        return validatePositionQuotes(legs.map { it.instrumentKey }, v.structure.status,
            v.legValuations.associate { it.leg.instrumentKey!! to it.quoteStatus },
            src, "2026-09-15T03:46:00.000Z", LocalDate.parse("2026-09-30"), 30.0, true, "OK")
    }
    private val staleOneLeg = { k: String -> if (k == "NSE_FO|69802") "2026-09-15T09:13:00+05:30" else "2026-09-15T09:15:59.4+05:30" }

    @Test
    fun validQuotesCloseAsValidWithoutRefresh() {
        val v = validity()
        assertTrue(paperCloseRefreshKeys(v).isEmpty())
        val q = decidePaperCloseQuality(v, emptyList(), null)
        assertEquals(PAPER_CLOSE_QUOTE_QUALITY_VALID, q.quality)
        assertFalse(q.refreshAttempted)
        assertNull(q.reason)
    }

    @Test
    fun staleLegIsRefreshedOnceAndValidRefreshClosesValid() {
        val first = validity(staleOneLeg)
        val keys = paperCloseRefreshKeys(first)
        assertEquals(listOf("NSE_FO|69802"), keys)
        val q = decidePaperCloseQuality(first, keys, validity())
        assertEquals(PAPER_CLOSE_QUOTE_QUALITY_VALID, q.quality)
        assertTrue(q.refreshAttempted)
        assertTrue(q.useRefreshed)
    }

    @Test
    fun stillInvalidAfterOneRefreshClosesDegradedWithReason() {
        val first = validity(staleOneLeg)
        val keys = paperCloseRefreshKeys(first)
        val q = decidePaperCloseQuality(first, keys, validity(staleOneLeg))
        assertEquals(PAPER_CLOSE_QUOTE_QUALITY_DEGRADED, q.quality)
        assertTrue(q.refreshAttempted)
        assertTrue(q.reason!!.startsWith("quote_invalid_after_one_refresh:"))
        assertTrue(q.reason!!.contains("leg:NSE_FO|69802:source_stale"))
        assertTrue(q.reason!!.length <= 240)
    }

    @Test
    fun nonCurableInvalidityClosesDegradedWithoutARefresh() {
        val first = validity(exact = { it != "NSE_FO|69826" })        // inexact key match
        assertTrue(paperCloseRefreshKeys(first).isEmpty())
        val q = decidePaperCloseQuality(first, emptyList(), null)
        assertEquals(PAPER_CLOSE_QUOTE_QUALITY_DEGRADED, q.quality)
        assertFalse(q.refreshAttempted)
        assertTrue(q.reason!!.startsWith("quote_invalid_no_curable_refresh:"))
        assertTrue(q.reason!!.contains("key_match_inexact"))
    }

    @Test
    fun refreshedBookThatFailsExecutableGatesFallsBackToFirstReasons() {
        val first = validity(staleOneLeg)
        val q = decidePaperCloseQuality(first, paperCloseRefreshKeys(first), null)
        assertEquals(PAPER_CLOSE_QUOTE_QUALITY_DEGRADED, q.quality)
        assertFalse(q.useRefreshed)
    }

    @Test
    fun serviceNoLongerFailsTheCloseOnValidityAlone() {
        val src = listOf(File("src/main/java/com/marketradar/app/PositionTickService.kt"),
            File("app/src/main/java/com/marketradar/app/PositionTickService.kt")).first { it.isFile }.readText()
        val body = src.substringAfter("private fun capturePaperCloseQuote(").substringBefore("private fun getOpenTradesFromPrefs")
        assertFalse(body.contains("fail(\n                    PAPER_CLOSE_QUOTE_SOURCE_INVALID"))
        assertFalse(body.contains("PAPER_CLOSE_QUOTE_SOURCE_INVALID"))
        assertTrue(body.contains("decidePaperCloseQuality(firstValidity, refreshKeys, refreshedValidity)"))
        assertTrue(body.contains("put(\"close_quote_quality\", closeQuality.quality)"))
        assertTrue(body.contains("put(\"close_quote_degraded_reason\", closeQuality.reason ?: JSONObject.NULL)"))
        // Paper-only guard precedes any quote work.
        assertTrue(body.indexOf("return fail(PAPER_CLOSE_PAPER_ONLY)") < body.indexOf("fetchQuotesWithFallback(keys)"))
    }
}
