package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * b513 fail-before probe. Uses ONLY API surface that already existed at fe0d97e (pageFetchSeam, PageResult(status,
 * rows, httpCode, error, body), readEvaluationOutcomeIdentityKeys(date, includeRejected), .error/.httpStatus/.failedPage)
 * so it compiles on the released head and demonstrates the 21 Oct blank diagnostic there.
 */
class B513ReadbackFailBeforeTest {
    @After fun reset() = SupabaseClient.resetPageFetchSeam()

    private fun install(failTable: String, blankReasonBody: String) {
        SupabaseClient.pageFetchSeam = { table, _, _, _, offset ->
            if (table == failTable && (offset ?: 0) == 0) {
                // what pre-b513 fetchSyncTyped produced for an HTTP/2 500: error = response.message = ""
                SupabaseClient.PageResult(status = "http_error", httpCode = 500, error = "", body = blankReasonBody)
            } else SupabaseClient.PageResult(status = "success", httpCode = 200, rows = JSONArray().put(
                JSONObject().put("snapshot_id", 1).put("candidate_id", "p1").put("role", if (table == "ml_rejected_candidate_outcomes") "rejected" else "primary")))
        }
    }

    @Test fun evaluationReaderBlankReasonIsNotBlankAndNamesStageAndCode() {
        install("ml_evaluation_outcomes", "{\"code\":\"57014\",\"message\":\"canceling statement due to statement timeout\"}")
        val rb = SupabaseClient.readEvaluationOutcomeIdentityKeys("2026-10-21", includeRejected = true)
        assertFalse(rb.ok)
        val err = rb.error.orEmpty()
        assertTrue("blank error reproduced: '$err'", err.isNotBlank())
        assertTrue(err, err.contains("stage=evaluation"))
        assertTrue(err, err.contains("57014"))
    }

    @Test fun rejectedReaderKeepsHttpStatusAndPage() {
        install("ml_rejected_candidate_outcomes", "{\"code\":\"57014\"}")
        val rb = SupabaseClient.readEvaluationOutcomeIdentityKeys("2026-10-21", includeRejected = true)
        assertFalse(rb.ok)
        assertNotNull("rejected httpStatus lost", rb.httpStatus)
        assertEquals(500, rb.httpStatus)
        assertEquals(0, rb.failedPage)
        assertTrue(rb.error.orEmpty(), rb.error.orEmpty().contains("stage=rejected"))
    }
}
