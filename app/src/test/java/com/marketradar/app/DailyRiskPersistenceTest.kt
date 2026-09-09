package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class DailyRiskPersistenceTest {
    private fun snapshot(realizedPnl: Double): JSONObject {
        val context = JSONObject()
            .put("snapshot_daily_risk_state", JSONObject().put("schema_version", "daily_risk_state_v1")
                .put("status", "OK").put("realized_pnl", realizedPnl)
                .put("daily_trade_count", if (realizedPnl == 0.0) 0 else 1))
            .put("snapshot_pc2_paper_primary", JSONObject()
                .put("deterministic_reference_source", "preserved_deterministic_rank"))
        return JSONObject().put("session_date", "2026-09-09")
            .put("poll_ts", "2026-09-09T09:35:00+05:30").put("context_json", context)
    }

    @Test fun compactPersistenceKeepsZeroAndLossDailyRiskState() {
        for (pnl in listOf(0.0, -3213.9)) {
            val compact = EvaluationLocalCache.compactBrainSnapshotForPersistence(snapshot(pnl))
            val context = compact.getJSONObject("context_json")
            assertTrue(context.has("snapshot_daily_risk_state"))
            assertEquals(pnl, context.getJSONObject("snapshot_daily_risk_state").getDouble("realized_pnl"), 0.001)
            assertTrue(context.has("snapshot_pc2_paper_primary"))
        }
    }
}
