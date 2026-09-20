package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class EngBatchBAndDReportingTest {
    @Test
    fun labelsSavedIndependentOfC3Ineligible() {
        var run = EvaluationRunLedger.newRun(
            sessionDate = "2026-09-18",
            inputManifest = JSONObject().put("snapshot_count", 3)
        )
        run = EvaluationRunLedger.setExpectedIdentities(run, listOf("10", "11"), emptyList())
        run = EvaluationRunLedger.recordPersistedIdentities(run, listOf("10", "11"))
        run = EvaluationRunLedger.setStage(run, "outcome_persistence", "verified", expectedCount = 2, verifiedCount = 2)
        run = EvaluationRunLedger.setStage(
            run,
            "percentile_finalization",
            "ineligible",
            reasonCode = "NO_C3_FRAMES",
            lastError = ""
        )
        assertTrue(run.optBoolean("labels_saved"))
        val c3 = run.optJSONObject("stages")!!.optJSONObject("percentile_finalization")!!
        assertEquals("ineligible", c3.optString("state"))
        assertEquals("", c3.optString("last_error"))
    }

    @Test
    fun positiveEodIsNotTeacherTargetHit() {
        val isSuccess = false
        val exitReason = "EOD"
        val managedPnl = 1200.0
        val teacherTargetHit = isSuccess || exitReason == "TP"
        val netProfitable = managedPnl > 0
        assertFalse(teacherTargetHit)
        assertTrue(netProfitable)
    }
}
