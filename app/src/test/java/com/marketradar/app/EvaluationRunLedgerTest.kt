package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class EvaluationRunLedgerTest {
    private fun baseRun(): JSONObject =
        EvaluationRunLedger.newRun(
            "2026-09-10",
            JSONObject().put("snapshot_ids", JSONArray().put(1).put(2)).put("snapshot_count", 2)
        )

    @Test
    fun crashResumeReturnsCorrectStage() {
        var run = baseRun()
        run = EvaluationRunLedger.setStage(run, "input_coverage", "verified", verifiedCount = 2)
        run = EvaluationRunLedger.setStage(run, "outcome_computation", "running")
        assertEquals("outcome_computation", EvaluationRunLedger.nextResumableStage(run))
        run = EvaluationRunLedger.setStage(run, "outcome_computation", "verified")
        run = EvaluationRunLedger.setStage(run, "outcome_persistence", "failed", lastError = "net")
        assertEquals("outcome_persistence", EvaluationRunLedger.nextResumableStage(run))
    }

    @Test
    fun duplicateLeaseRejected() {
        val run = baseRun()
        val (a, okA, _) = EvaluationRunLedger.acquireLease(run, "device-a", nowMs = 1000L)
        assertTrue(okA)
        val (_, okB, reason) = EvaluationRunLedger.acquireLease(a, "device-b", nowMs = 2000L)
        assertFalse(okB)
        assertEquals(EvaluationRunLedger.REASON_DUPLICATE_LEASE, reason)
    }

    @Test
    fun failedC3NotFullSuccess() {
        var run = baseRun()
        for (name in listOf(
            "input_coverage", "outcome_computation", "outcome_persistence", "research_aggregation"
        )) {
            run = EvaluationRunLedger.setStage(run, name, "verified", verifiedCount = 1)
        }
        run = EvaluationRunLedger.setStage(run, "percentile_finalization", "failed", reasonCode = "WRITE_FAIL")
        assertTrue(run.optBoolean("labels_saved"))
        assertFalse(run.optBoolean("learning_complete"))
    }

    @Test
    fun cappedPopulationsFailProvenanceAndStayIneligible() {
        val frames = JSONArray()
            .put(JSONObject().put("candidate_population_verified", false).put("generated_capture_complete", false))
            .put(
                JSONObject()
                    .put("candidate_population_verified", false)
                    .put("generated_capture_complete", false)
                    .put("truncated_at_ranked_evidence", 4)
            )
        val assessment = EvaluationRunLedger.assessC3Frames(frames)
        assertFalse(assessment.optBoolean("eligible"))
        assertEquals(EvaluationRunLedger.REASON_CAPPED_POPULATION, assessment.optString("reason_code"))
        assertFalse(assessment.optBoolean("would_write_rows"))
        var run = baseRun()
        for (name in listOf(
            "input_coverage", "outcome_computation", "outcome_persistence", "research_aggregation"
        )) {
            run = EvaluationRunLedger.setStage(run, name, "verified")
        }
        run = EvaluationRunLedger.applyC3Assessment(run, assessment)
        assertEquals("ineligible", run.getJSONObject("stages").getJSONObject("percentile_finalization").optString("state"))
        assertTrue(run.optBoolean("labels_saved"))
        assertFalse(run.optBoolean("learning_complete")) // G6 metrics still pending
        run = EvaluationRunLedger.applyPerformanceMetricsResult(
            run,
            JSONObject()
                .put("state", "verified")
                .put("reason_code", "NO_ELIGIBLE_PREDICTIONS")
                .put("expected_count", 0)
                .put("written_count", 0)
                .put("verified_count", 0)
                .put("active_recommendation_unchanged", true)
        )
        assertTrue(run.optBoolean("learning_complete"))
    }

    @Test
    fun nonlabelableAccounted() {
        var run = baseRun()
        run = EvaluationRunLedger.setStage(
            run,
            "input_coverage",
            "verified",
            expectedCount = 10,
            verifiedCount = 7,
            nonlabelableCount = 3,
            reasonCode = "NONLABELABLE_SNAPSHOTS_ACCOUNTED"
        )
        assertEquals(3, run.getJSONObject("stages").getJSONObject("input_coverage").optInt("nonlabelable_count"))
    }

    @Test
    fun trainingPromotionDisabled() {
        val run = baseRun()
        assertEquals("disabled", run.getJSONObject("stages").getJSONObject("training").optString("state"))
        assertEquals(
            EvaluationRunLedger.REASON_TRAINING_FROZEN,
            run.getJSONObject("stages").getJSONObject("training").optString("reason_code")
        )
        assertEquals("disabled", run.getJSONObject("stages").getJSONObject("promotion").optString("state"))
        assertEquals("pending", run.getJSONObject("stages").getJSONObject("performance_metrics").optString("state"))
        assertEquals(
            EvaluationRunLedger.REASON_METRICS_READY_G6,
            run.getJSONObject("stages").getJSONObject("performance_metrics").optString("reason_code")
        )
    }

    @Test
    fun identityChangesWithManifest() {
        val a = EvaluationRunLedger.buildRunId("2026-09-10", inputManifestHash = EvaluationRunLedger.hashInputManifest(JSONObject().put("n", 1)))
        val b = EvaluationRunLedger.buildRunId("2026-09-10", inputManifestHash = EvaluationRunLedger.hashInputManifest(JSONObject().put("n", 2)))
        assertNotEquals(a, b)
        assertTrue(a.startsWith("erun_"))
    }
}
