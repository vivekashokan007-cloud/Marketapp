package com.marketradar.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/** PM1-2: ONE manual closed-trade input for every Daily review run; KEPT_RICHER_REVISION finishes a run. */
class Pm1ReviewTradesTest {
    private fun prefs(v: Any?) = DiskPrefs(if (v == null) mutableMapOf() else mutableMapOf("closed_trades" to v))

    @Test fun missingBlankAndStored() {
        assertEquals(IndexAuthorityContext.ReviewTrades(null, "MISSING"), IndexAuthorityContext.reviewTrades(prefs(null)))
        assertEquals(IndexAuthorityContext.ReviewTrades(null, "MISSING"), IndexAuthorityContext.reviewTrades(prefs("  ")))
        assertEquals(IndexAuthorityContext.ReviewTrades("[]", "READ"), IndexAuthorityContext.reviewTrades(prefs("[]")))
        assertEquals("[{\"id\":1}]", IndexAuthorityContext.reviewTradesJson(prefs("[{\"id\":1}]")))
        assertNull(IndexAuthorityContext.reviewTradesJson(prefs("")))
        assertEquals("prefs.closed_trades", IndexAuthorityContext.REVIEW_TRADES_SOURCE)
    }

    @Test fun unreadablePreferenceIsDistinct() {
        val bad = object : android.content.SharedPreferences by prefs(null) {
            override fun getString(key: String?, defValue: String?): String? = throw ClassCastException("not a string")
        }
        assertEquals(IndexAuthorityContext.ReviewTrades(null, "UNREADABLE"), IndexAuthorityContext.reviewTrades(bad))
    }

    @Test fun keptRicherRevisionIsFinished() {
        assertTrue(IndexAuthorityContext.reviewFinished("KEPT_RICHER_REVISION"))
        assertTrue(IndexAuthorityContext.reviewFinished("ALREADY_COMPLETE"))
        assertFalse(IndexAuthorityContext.reviewFinished("INPUT_INVALID"))
    }

    @Test fun everyReviewCallerUsesTheHelperAndTheBridgeIgnoresThePwaArgument() {
        val src = listOf(File("src/main/java/com/marketradar/app"), File("app/src/main/java/com/marketradar/app")).first { it.isDirectory }
        val sched = File(src, "DailyReviewScheduler.kt").readText()
        assertTrue(sched.contains("IndexAuthorityContext.reviewTrades(prefs)"))
        assertFalse(sched.contains("prefs.getString(\"closed_trades\", null)"))
        val nb = File(src, "NativeBridge.kt").readText()
        val run = nb.substring(nb.indexOf("fun dailyReviewRun("), nb.indexOf("fun setIndexSelector("))
        assertTrue(run.contains("IndexAuthorityContext.reviewTrades(prefs)"))
        assertFalse(run.contains("pollsJson, paperTradesJson"))
        val svc = File(src, "MarketWatchService.kt").readText()
        assertTrue(svc.contains("maybeRunDailyReview(filesDir, prefs, System.currentTimeMillis())"))
    }
}
