package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PositionMarkStoreTest {
    @Test
    fun completeCurrentTickIsLiveForDisplayButNeverActionable() {
        val now = 1_000_000L
        val state = PositionMarkStore.presentationFor(
            JSONObject().apply {
                put("latest_valuation_quality", "OK")
                put("latest_tick_ms", now - 60_000L)
                put("last_valid_tick_ms", now - 60_000L)
                put("last_valid_current_pnl", 1677.0)
                put("last_valid_tick_ts", "2026-09-21T04:17:47.983Z")
            },
            now
        )

        assertEquals(PositionMarkStore.LIVE_FULL, state.getString("display_state"))
        assertEquals(1677.0, state.getDouble("last_valid_current_pnl"), 0.0001)
        assertFalse(state.getBoolean("display_is_actionable"))
    }

    @Test
    fun emptyCurrentQuoteKeepsLastValidOnlyAsStale() {
        val now = 1_000_000L
        val state = PositionMarkStore.presentationFor(
            JSONObject().apply {
                put("latest_valuation_quality", "UNAVAILABLE")
                put("latest_tick_ms", now)
                put("last_valid_tick_ms", now - 60_000L)
                put("last_valid_current_pnl", 1677.0)
            },
            now
        )

        assertEquals(PositionMarkStore.STALE_LAST_VALID, state.getString("display_state"))
        assertEquals(1677.0, state.getDouble("last_valid_current_pnl"), 0.0001)
        assertFalse(state.getBoolean("display_is_actionable"))
    }

    @Test
    fun noAcceptedMarkRemainsUnavailable() {
        val state = PositionMarkStore.presentationFor(
            JSONObject().apply {
                put("latest_valuation_quality", "UNAVAILABLE")
                put("latest_tick_ms", 1_000_000L)
            },
            1_000_000L
        )

        assertEquals(PositionMarkStore.UNAVAILABLE, state.getString("display_state"))
        assertTrue(state.isNull("last_valid_current_pnl"))
        assertFalse(state.getBoolean("display_is_actionable"))
    }

    @Test
    fun presentationIncludesTrackingCompleteFromOverflowStatus() {
        val now = 1_000_000L
        val incomplete = PositionTickTrackingStatus(
            trackingComplete = false,
            overflowActive = true,
            overflowRejectedCount = 3L
        )
        val state = PositionMarkStore.presentationFor(
            JSONObject().apply {
                put("latest_valuation_quality", "OK")
                put("latest_tick_ms", now - 60_000L)
                put("last_valid_tick_ms", now - 60_000L)
                put("last_valid_current_pnl", 10.0)
            },
            now,
            incomplete
        )
        assertFalse(state.getBoolean("tracking_complete"))
        assertTrue(state.getBoolean("overflow_active"))
        assertEquals(3L, state.getLong("overflow_rejected_count"))
    }
}
