package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class EvaluationMetricsLedgerTest {
    @Test
    fun newRunStartsMetricsPendingNotDeferred() {
        val run = EvaluationRunLedger.newRun("2026-09-12")
        val stage = run.getJSONObject("stages").getJSONObject("performance_metrics")
        assertEquals("pending", stage.optString("state"))
        assertEquals(EvaluationRunLedger.REASON_METRICS_READY_G6, stage.optString("reason_code"))
    }

    @Test
    fun applyMetricsResultMarksVerifiedAndAllowsLearningComplete() {
        var run = EvaluationRunLedger.newRun("2026-09-12")
        for (name in listOf(
            "input_coverage", "outcome_computation", "outcome_persistence",
            "research_aggregation", "percentile_finalization"
        )) {
            run = EvaluationRunLedger.setStage(run, name, "verified")
        }
        assertFalse(run.optBoolean("learning_complete"))
        run = EvaluationRunLedger.applyPerformanceMetricsResult(
            run,
            JSONObject()
                .put("state", "verified")
                .put("reason_code", "METRICS_WRITTEN")
                .put("expected_count", 3)
                .put("written_count", 3)
                .put("verified_count", 3)
                .put("active_recommendation_unchanged", true)
        )
        assertEquals(
            "verified",
            run.getJSONObject("stages").getJSONObject("performance_metrics").optString("state")
        )
        assertTrue(run.optBoolean("learning_complete"))
        assertTrue(
            run.getJSONObject("stages")
                .getJSONObject("performance_metrics")
                .getJSONObject("detail")
                .optBoolean("active_recommendation_unchanged")
        )
    }

    @Test
    fun contractVersionsStable() {
        assertEquals("evaluation_metrics_ledger_v1_20260912", EvaluationMetricsLedger.METRICS_CONTRACT_VERSION)
        assertEquals("ml_feature_schema_v2_1_1_n38", EvaluationMetricsLedger.FEATURE_SCHEMA_VERSION)
    }
}
