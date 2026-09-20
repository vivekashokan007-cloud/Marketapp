package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class EngBatchCR1IdentityCoverageTest {
    @After
    fun resetSeam() = SupabaseClient.resetPageFetchSeam()

    private fun outcomes(ids: IntRange): JSONArray = JSONArray().also { out ->
        ids.forEach { sid ->
            out.put(
                JSONObject()
                    .put("snapshot_id", sid)
                    .put("candidate_id", "candidate_$sid")
                    .put("role", "primary")
                    .put("session_date", "2026-09-17")
            )
        }
    }

    private fun key(sid: Int) = "$sid|candidate_$sid|primary"

    @Test
    fun expected2To76ServerOnly46To76BlocksEveryCompletionSideEffect() {
        val assessment = EvaluationIdentityCoverage.assess(
            expectedSnapshotIds = (2..76).map(Int::toString),
            producedOutcomes = outcomes(2..76),
            serverCompositeKeys = (46..76).map(::key),
            readbackOk = true
        )
        assertFalse(assessment.complete)
        assertEquals(44, assessment.missingCount)
        val transition = EvaluationIdentityCoverage.transitionFor(assessment)
        assertEquals("INCOMPLETE_IDENTITY", transition.phase)
        assertFalse(transition.labelsSaved)
        assertFalse(transition.writeEvaluationDoneDate)
        assertFalse(transition.publishLabelsSaved)
        assertFalse(transition.startC3)
        assertFalse(transition.cancelReminder)
        assertFalse(transition.logEvaluationComplete)
        assertFalse(transition.clearRecoveryState)
        assertTrue(transition.scheduleContinuation)
    }

    @Test
    fun aggregate75WithStaleReplacementFailsExactCompositeVerification() {
        val produced = outcomes(2..76)
        val server = (2..76).filter { it != 25 }.map(::key).toMutableList()
        server += "999|stale_candidate|primary"
        assertEquals(75, server.size) // plausible aggregate count
        val assessment = EvaluationIdentityCoverage.assess(
            expectedSnapshotIds = (2..76).map(Int::toString),
            producedOutcomes = produced,
            serverCompositeKeys = server,
            readbackOk = true
        )
        assertFalse(assessment.complete)
        assertTrue(assessment.missingSnapshotIds.contains("25"))
        assertTrue(assessment.unexpectedServerCompositeKeys.contains("999|stale_candidate|primary"))
        assertFalse(EvaluationIdentityCoverage.transitionFor(assessment).labelsSaved)
    }

    @Test
    fun readbackPageTwoFailureFailsClosed() {
        SupabaseClient.pageFetchSeam = { table, _, _, limit, offset ->
            if (table != "ml_evaluation_outcomes") {
                SupabaseClient.PageResult(status = "success", rows = JSONArray())
            } else if ((offset ?: 0) == 0) {
                val rows = JSONArray()
                repeat(limit ?: 500) { i ->
                    rows.put(JSONObject().put("snapshot_id", i + 2).put("candidate_id", "candidate_${i + 2}").put("role", "primary"))
                }
                SupabaseClient.PageResult(status = "success", rows = rows)
            } else {
                SupabaseClient.PageResult(status = "http_error", httpCode = 503, error = "page two unavailable")
            }
        }
        val readback = SupabaseClient.readEvaluationOutcomeIdentityKeys("2026-09-17")
        assertFalse(readback.ok)
        assertEquals("incomplete_error", readback.status)
        assertEquals(1, readback.failedPage)
    }

    @Test
    fun sameFrozenManifestCompletesOnlyAfterExactReadback() {
        val manifestExpected = (2..76).map(Int::toString)
        val produced = outcomes(2..76)
        val partial = EvaluationIdentityCoverage.assess(
            manifestExpected, produced, (46..76).map(::key), true
        )
        assertFalse(partial.complete)
        val complete = EvaluationIdentityCoverage.assess(
            manifestExpected, produced, (2..76).map(::key), true
        )
        assertTrue(complete.complete)
        val transition = EvaluationIdentityCoverage.transitionFor(complete)
        assertTrue(transition.labelsSaved)
        assertTrue(transition.writeEvaluationDoneDate)
        assertTrue(transition.startC3)
    }

    @Test
    fun parserReadbackFailureNeverLooksLikeEmptyComplete() {
        val produced = outcomes(2..3)
        val assessment = EvaluationIdentityCoverage.assess(
            expectedSnapshotIds = listOf("2", "3"),
            producedOutcomes = produced,
            serverCompositeKeys = emptyList(),
            readbackOk = false,
            readbackError = "parse_error"
        )
        assertFalse(assessment.complete)
        assertEquals("FAILED_IDENTITY_COVERAGE", assessment.phase)
        assertFalse(EvaluationIdentityCoverage.transitionFor(assessment).labelsSaved)
    }
}
