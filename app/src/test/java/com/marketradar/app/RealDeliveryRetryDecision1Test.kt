package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Owner decision 1 (26 Sep 2026): the POSTED-only retry/ledger applies to Real.
 * Real-behaviour note: a Real shadow alert the OS did not accept is no longer
 * consumed on the first attempt; it stays retry_pending and consumes only when
 * POSTED. This is the ONLY Real change in this commit.
 */
class RealDeliveryRetryDecision1Test {

    private fun attempt(blob: String, cls: String, raw: String, now: Long, isPaper: Boolean = false): Pair<String, JSONObject> {
        val consumed = shadowAlertAttemptConsumes(cls)
        val next = recordShadowDeliveryAttempt(JSONObject(blob), "R42", "SHADOW_SL", "2026-09-28",
            isPaper, cls, raw, "", consumed, now)
        return next.toString() to next.getJSONObject(shadowDeliveryEventId("R42", "SHADOW_SL", "2026-09-28"))
    }

    @Test
    fun realDeniedStaysRetryableAcrossRestartThenPostsOnce() {
        var blob = "{}"
        val (b1, e1) = attempt(blob, DELIVERY_PERMISSION_DENIED, "PERMISSION_DENIED", 1_000L); blob = b1
        assertEquals("REAL", e1.getString("mode"))
        assertFalse(e1.getBoolean("consumed"))                // pre-decision-1 Real: true
        assertTrue(e1.getBoolean("retry_pending"))
        assertEquals(SHADOW_DELIVERY_CONSUME_RULE, e1.getString("consume_rule"))
        val (b2, e2) = attempt(blob, DELIVERY_THROTTLED, "THROTTLED", 61_000L); blob = b2
        assertEquals(2, e2.getInt("attempts"))
        assertFalse(e2.getBoolean("consumed"))
        val (_, e3) = attempt(blob, DELIVERY_POSTED, "POSTED_TO_OS", 121_000L)
        assertTrue(e3.getBoolean("consumed"))
        assertFalse(e3.getBoolean("retry_pending"))
        assertEquals(121_000L, e3.getLong("posted_at_ms"))
        assertEquals(1_000L, e3.getLong("first_decided_at_ms"))
    }

    @Test
    fun paperAndRealUseTheSameRule() {
        for (cls in listOf(DELIVERY_POSTED, DELIVERY_PERMISSION_DENIED, DELIVERY_CHANNEL_BLOCKED,
                DELIVERY_THROTTLED, DELIVERY_EXCEPTION)) {
            val (_, real) = attempt("{}", cls, cls, 5L, isPaper = false)
            val (_, paper) = attempt("{}", cls, cls, 5L, isPaper = true)
            assertEquals(cls, paper.getBoolean("consumed"), real.getBoolean("consumed"))
            assertEquals(cls == DELIVERY_POSTED, real.getBoolean("consumed"))
        }
    }
}
