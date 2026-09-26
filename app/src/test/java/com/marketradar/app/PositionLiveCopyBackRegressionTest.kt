package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class PositionLiveCopyBackRegressionTest {
    @Test fun omittedOrNullOptionalMetricsDoNotEraseStoredGrossExtrema() {
        val trade = JSONObject()
            .put("peak_pnl", 1800.0)
            .put("trough_pnl", -250.0)
            .put("peak_erosion", 50.0)
            .put("vix_change", 0.7)
            .put("current_premium", 120.0)

        applyOptionalPositionLiveMetrics(trade, JSONObject().put("current_pnl", -1596.0))
        applyOptionalPositionLiveMetrics(trade, JSONObject()
            .put("peak_pnl", JSONObject.NULL)
            .put("trough_pnl", "not-a-number")
            .put("current_net_premium", JSONObject.NULL))
        assertEquals(1800.0, trade.getDouble("peak_pnl"), 0.001)
        assertEquals(-250.0, trade.getDouble("trough_pnl"), 0.001)
        assertEquals(50.0, trade.getDouble("peak_erosion"), 0.001)
        assertEquals(0.7, trade.getDouble("vix_change"), 0.001)
        assertEquals(120.0, trade.getDouble("current_premium"), 0.001)
        assertFalse(trade.isNull("peak_pnl"))

        applyOptionalPositionLiveMetrics(trade, JSONObject()
            .put("peak_pnl", 2100.0)
            .put("trough_pnl", -1596.0)
            .put("peak_erosion", 176.0)
            .put("vix_change", 1.5)
            .put("current_net_premium", 130.0))
        assertEquals(2100.0, trade.getDouble("peak_pnl"), 0.001)
        assertEquals(-1596.0, trade.getDouble("trough_pnl"), 0.001)
        assertEquals(130.0, trade.getDouble("current_premium"), 0.001)
    }
}
