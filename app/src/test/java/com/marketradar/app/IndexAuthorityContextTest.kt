package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.time.ZoneId
import java.time.ZonedDateTime

/** Delivery A R5: analysis clock on every analyze, deferred authority writes, selector, review schedule. */
class IndexAuthorityContextTest {
    private val ist = ZoneId.of("Asia/Kolkata")
    private fun ms(y: Int, mo: Int, d: Int, h: Int, mi: Int) =
        ZonedDateTime.of(y, mo, d, h, mi, 0, 0, ist).toInstant().toEpochMilli()

    @Test fun analysisClockStampedOnLiveAndRefresh() {
        val dir = File("/tmp/x")
        val live = JSONObject(); IndexAuthorityContext.apply(live, dir, false, 1000L, "nf")
        assertEquals(1000L, live.getLong("analysisClockMs"))
        assertEquals(1000L, live.getLong("pollReceiptMs"))
        assertTrue(live.getBoolean("indexAuthorityDeferredWrite"))
        assertEquals("NF", live.getString("indexSelector"))
        val refresh = JSONObject().put("pollReceiptMs", 5L)
        IndexAuthorityContext.apply(refresh, dir, true, 2000L, null)
        assertEquals(2000L, refresh.getLong("analysisClockMs"))
        assertFalse(refresh.has("pollReceiptMs"))
        assertEquals("BOTH", refresh.getString("indexSelector"))
    }

    @Test fun selectorNormalised() {
        assertEquals("BNF", IndexAuthorityContext.normalizeSelector("banknifty"))
        assertEquals("BOTH", IndexAuthorityContext.normalizeSelector("garbage"))
    }

    @Test fun persistDeltaDetachedFromResult() {
        val r = JSONObject().put("x", 1).put("index_authority_persist",
            JSONObject().put("session_date", "2026-11-02").put("delta", JSONObject().put("history", JSONObject())))
        val p = IndexAuthorityContext.takePersist(r)
        assertEquals("2026-11-02", p!!.getString("session_date"))
        assertFalse(r.has("index_authority_persist"))
        assertNull(IndexAuthorityContext.takePersist(JSONObject()))
        assertEquals(0L, IndexAuthorityContext.publishedMs(false, 99L))
        assertEquals(99L, IndexAuthorityContext.publishedMs(true, 99L))
    }

    @Test fun dailyReviewOncePerSessionAfterClose() {
        val before = ms(2026, 11, 2, 15, 39)
        val after = ms(2026, 11, 2, 15, 41)
        assertFalse(IndexAuthorityContext.dailyReviewDue(before, null))
        assertTrue(IndexAuthorityContext.dailyReviewDue(after, null))
        assertFalse(IndexAuthorityContext.dailyReviewDue(after, "2026-11-02"))
        assertTrue(IndexAuthorityContext.dailyReviewDue(after, "2026-10-30"))
        assertFalse(IndexAuthorityContext.dailyReviewDue(ms(2026, 10, 31, 16, 0), null))  // Saturday
        assertTrue(IndexAuthorityContext.reviewFinished("ALREADY_COMPLETE"))
        assertFalse(IndexAuthorityContext.reviewFinished("BUDGET_EXCEEDED"))
    }
}
