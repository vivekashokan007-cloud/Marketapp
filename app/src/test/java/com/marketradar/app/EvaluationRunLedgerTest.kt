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
        val c3Stage = run.getJSONObject("stages").getJSONObject("percentile_finalization")
        assertEquals("ineligible", c3Stage.optString("state"))
        assertEquals(EvaluationRunLedger.REASON_CAPPED_POPULATION, c3Stage.optString("reason_code"))
        assertEquals("", c3Stage.optString("last_error"))
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

    @Test
    fun handledExitReleasesLeaseAndImmediateResumeSucceeds() {
        val run = baseRun()
        val holder = EvaluationRunLedger.stableLeaseHolder("Pixel 8", "2026-09-18")
        val (leased, ok, _) = EvaluationRunLedger.acquireLease(run, holder, nowMs = 1_000L)
        assertTrue(ok)
        assertEquals(holder, leased.optString("lease_holder"))
        val released = EvaluationRunLedger.releaseLease(leased, holder)
        assertTrue(released.isNull("lease_holder") || released.opt("lease_holder") == JSONObject.NULL)
        assertEquals(0L, released.optLong("lease_expires_at_ms"))
        val (resumed, okResume, reason) = EvaluationRunLedger.acquireLease(
            released,
            holder,
            nowMs = 2_000L
        )
        assertTrue(reason, okResume)
        assertEquals(holder, resumed.optString("lease_holder"))
    }

    @Test
    fun differentDeviceLeaseRemainsProtectedUntilExpiry() {
        val run = baseRun()
        val a = EvaluationRunLedger.stableLeaseHolder("Pixel 8", "2026-09-18")
        val b = EvaluationRunLedger.stableLeaseHolder("Samsung S24", "2026-09-18")
        val (held, okA, _) = EvaluationRunLedger.acquireLease(run, a, nowMs = 1_000L, leaseMs = 45 * 60_000L)
        assertTrue(okA)
        val (_, okB, reason) = EvaluationRunLedger.acquireLease(held, b, nowMs = 2_000L)
        assertFalse(okB)
        assertEquals(EvaluationRunLedger.REASON_DUPLICATE_LEASE, reason)
    }

    @Test
    fun wrongHolderCannotReleaseAnothersLease() {
        val run = baseRun()
        val owner = EvaluationRunLedger.stableLeaseHolder("Pixel 8", "2026-09-18")
        val other = EvaluationRunLedger.stableLeaseHolder("Samsung S24", "2026-09-18")
        val (held, ok, _) = EvaluationRunLedger.acquireLease(run, owner, nowMs = 1_000L)
        assertTrue(ok)
        val afterWrong = EvaluationRunLedger.releaseLease(held, other)
        assertEquals(owner, afterWrong.optString("lease_holder"))
        assertTrue(afterWrong.optLong("lease_expires_at_ms") > 0L)
        val afterOwner = EvaluationRunLedger.releaseLease(afterWrong, owner)
        assertTrue(afterOwner.isNull("lease_holder") || afterOwner.opt("lease_holder") == JSONObject.NULL)
    }

    @Test
    fun sameDeviceReclaimsAbandonedPerAttemptLease() {
        val run = baseRun()
        val abandoned = "device:Pixel 8:eval-2026-09-18-999"
        val stable = EvaluationRunLedger.stableLeaseHolder("Pixel 8", "2026-09-18")
        val (held, okA, _) = EvaluationRunLedger.acquireLease(run, abandoned, nowMs = 1_000L, leaseMs = 45 * 60_000L)
        assertTrue(okA)
        val (reclaimed, okB, reason) = EvaluationRunLedger.acquireLease(held, stable, nowMs = 2_000L)
        assertTrue(reason, okB)
        assertEquals(stable, reclaimed.optString("lease_holder"))
    }

    @Test
    fun stableLeaseHolderUsesDeviceAndSessionDate() {
        assertEquals(
            "device:Pixel 8:2026-09-18",
            EvaluationRunLedger.stableLeaseHolder("Pixel 8", "2026-09-18")
        )
        assertTrue(
            EvaluationRunLedger.sameDeviceLeaseHolders(
                "device:Pixel 8:eval-old",
                "device:Pixel 8:2026-09-18"
            )
        )
        assertFalse(
            EvaluationRunLedger.sameDeviceLeaseHolders(
                "device:Pixel 8:2026-09-18",
                "device:Samsung:2026-09-18"
            )
        )
    }
}
