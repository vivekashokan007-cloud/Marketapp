package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class EngBatchCEvalCompletenessTest {
    private fun baseRun(): JSONObject {
        val run = EvaluationRunLedger.newRun(
            sessionDate = "2026-09-17",
            inputManifest = JSONObject().put("snapshot_count", 77)
        )
        val expected = (2..76).map { it.toString() }
        val nonlabelable = listOf("1", "77")
        return EvaluationRunLedger.setExpectedIdentities(run, expected, nonlabelable)
    }

    @Test
    fun suffixOnlyPersistedRemainsIncomplete() {
        var run = baseRun()
        run = EvaluationRunLedger.recordPersistedIdentities(run, (46..76).map { it.toString() })
        run = EvaluationRunLedger.setStage(
            run,
            "outcome_persistence",
            "verified",
            expectedCount = 75,
            verifiedCount = 31
        )
        assertEquals(44, run.optInt("missing_identity_count"))
        assertFalse(run.optBoolean("labels_saved"))
    }

    @Test
    fun resumeMissingThenCompleteWithoutDuplicates() {
        var run = baseRun()
        run = EvaluationRunLedger.recordPersistedIdentities(run, (46..76).map { it.toString() })
        val missing = EvaluationRunLedger.missingIdentities(run)
        assertEquals(44, missing.size)
        run = EvaluationRunLedger.recordPersistedIdentities(run, missing)
        val before = run.optJSONArray("persisted_identity_ids")!!.length()
        run = EvaluationRunLedger.recordPersistedIdentities(run, listOf("46", "47", "76"))
        assertEquals(before, run.optJSONArray("persisted_identity_ids")!!.length())
        run = EvaluationRunLedger.setStage(
            run,
            "outcome_persistence",
            "verified",
            expectedCount = 75,
            verifiedCount = 75
        )
        assertEquals(0, run.optInt("missing_identity_count"))
        assertTrue(run.optBoolean("labels_saved"))
        assertEquals("2026-09-17", run.optString("session_date"))
    }

    @Test
    fun endpointsExcludedDoNotBlockCompletion() {
        var run = baseRun()
        run = EvaluationRunLedger.recordPersistedIdentities(run, (2..76).map { it.toString() })
        run = EvaluationRunLedger.setStage(
            run,
            "outcome_persistence",
            "verified",
            expectedCount = 75,
            verifiedCount = 75
        )
        assertTrue(run.optBoolean("labels_saved"))
        assertEquals(0, run.optInt("missing_identity_count"))
    }
}
