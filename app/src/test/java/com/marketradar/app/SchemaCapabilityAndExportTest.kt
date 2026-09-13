package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.After
import org.junit.Test

class SchemaCapabilityAndExportTest {
    @After
    fun tearDown() {
        SupabaseClient.resetPageFetchSeam()
        SupabaseClient.setRemoteContractIdentityCapabilityForTest(false)
        SupabaseClient.markContractIdentityWriteSucceeded(false)
        SupabaseClient.markContractIdentityReadbackVerified(false)
    }

    @Test
    fun capabilityDefaultsFailClosed() {
        SupabaseClient.setRemoteContractIdentityCapabilityForTest(false)
        assertFalse(SupabaseClient.remoteContractIdentityCapability())
        assertFalse(SupabaseClient.remoteContractIdentityPersistedFlag())
        assertFalse(SupabaseClient.remoteContractIdentityReadbackVerifiedFlag())
    }

    @Test
    fun capabilityPerTableIndependent() {
        SupabaseClient.setRemoteContractIdentityCapabilityPerTableForTest(
            evaluation = true,
            recommendation = false
        )
        assertTrue(SupabaseClient.shouldIncludeContractIdentity("ml_evaluation_outcomes"))
        assertFalse(SupabaseClient.shouldIncludeContractIdentity("ml_recommendation_outcomes"))

        SupabaseClient.setRemoteContractIdentityCapabilityPerTableForTest(
            evaluation = false,
            recommendation = true
        )
        assertFalse(SupabaseClient.shouldIncludeContractIdentity("ml_evaluation_outcomes"))
        assertTrue(SupabaseClient.shouldIncludeContractIdentity("ml_recommendation_outcomes"))

        SupabaseClient.setRemoteContractIdentityCapabilityPerTableForTest(null, null)
        assertFalse(SupabaseClient.shouldIncludeContractIdentity("ml_evaluation_outcomes"))
        assertFalse(SupabaseClient.shouldIncludeContractIdentity("ml_recommendation_outcomes"))

        SupabaseClient.setRemoteContractIdentityCapabilityPerTableForTest(true, true)
        assertTrue(SupabaseClient.shouldIncludeContractIdentity("ml_evaluation_outcomes"))
        assertTrue(SupabaseClient.shouldIncludeContractIdentity("ml_recommendation_outcomes"))
    }

    @Test
    fun persistenceTruthSeparated() {
        SupabaseClient.setRemoteContractIdentityCapabilityForTest(true)
        // Payload may be included while write/readback remain false
        assertFalse(SupabaseClient.remoteContractIdentityPersistedFlag())
        SupabaseClient.markContractIdentityWriteSucceeded(true)
        assertTrue(SupabaseClient.remoteContractIdentityPersistedFlag())
        assertFalse(SupabaseClient.remoteContractIdentityReadbackVerifiedFlag())
        SupabaseClient.markContractIdentityReadbackVerified(true)
        assertTrue(SupabaseClient.remoteContractIdentityReadbackVerifiedFlag())
        val truth = SupabaseClient.persistenceTruth(
            payloadIncluded = true,
            writeSucceeded = true,
            readbackVerified = false
        )
        assertTrue(truth.payloadIncluded)
        assertTrue(truth.writeSucceeded)
        assertFalse(truth.readbackVerified)
    }

    @Test
    fun page2HttpErrorIncompleteNoFallback() {
        var calls = 0
        SupabaseClient.pageFetchSeam = { table, _, _, limit, offset ->
            calls++
            when {
                offset == 0 || offset == null -> SupabaseClient.PageResult(
                    status = "success",
                    rows = JSONArray().put(JSONObject().put("id", "1")).put(JSONObject().put("id", "2")),
                    httpCode = 200
                )
                else -> SupabaseClient.PageResult(status = "http_error", httpCode = 500, error = "boom")
            }
        }
        // Force preferred table to have page-0 success then page-2 fail
        // Use small page size so page 0 is "full" and page 1 is requested.
        // Override: return pageSize-length page 0
        SupabaseClient.pageFetchSeam = { table, _, _, limit, offset ->
            val lim = limit ?: 2
            if ((offset ?: 0) == 0) {
                val rows = JSONArray()
                repeat(lim) { i -> rows.put(JSONObject().put("id", "p0-$i").put("created_at", "2026-09-13T0$i:00:00Z")) }
                SupabaseClient.PageResult(status = "success", rows = rows, httpCode = 200)
            } else {
                // Preferred source network failure — must not fall through
                SupabaseClient.PageResult(status = "http_error", httpCode = 503, error = "network")
            }
        }
        val out = SupabaseClient.fetchRecentEvaluationOutcomesPaged(pageSize = 2, maxPages = 5)
        assertEquals("incomplete_error", out.optString("status"))
        assertEquals("http_error", out.optString("page_error_status"))
        assertTrue(out.has("failed_page"))
    }

    @Test
    fun page2ParseErrorIncomplete() {
        SupabaseClient.pageFetchSeam = { _, _, _, limit, offset ->
            val lim = limit ?: 2
            if ((offset ?: 0) == 0) {
                val rows = JSONArray()
                repeat(lim) { i -> rows.put(JSONObject().put("id", "a$i")) }
                SupabaseClient.PageResult(status = "success", rows = rows)
            } else {
                SupabaseClient.PageResult(status = "parse_error", error = "bad json")
            }
        }
        val out = SupabaseClient.fetchRecentEvaluationOutcomesPaged(pageSize = 2, maxPages = 5)
        assertEquals("incomplete_error", out.optString("status"))
        assertEquals("parse_error", out.optString("page_error_status"))
    }

    @Test
    fun snapshotPageFailureIncomplete() {
        SupabaseClient.pageFetchSeam = { table, _, _, limit, offset ->
            if (table.contains("brain_snapshots") && (offset ?: 0) > 0) {
                SupabaseClient.PageResult(status = "http_error", httpCode = 500, error = "snap fail")
            } else if (table.contains("brain_snapshots")) {
                val rows = JSONArray()
                repeat(limit ?: 2) { i -> rows.put(JSONObject().put("id", "s$i")) }
                SupabaseClient.PageResult(status = "success", rows = rows)
            } else {
                SupabaseClient.PageResult(status = "success", rows = JSONArray())
            }
        }
        val snap = SupabaseClient.fetchRecentBrainSnapshotsPaged(pageSize = 2, maxPages = 5)
        assertEquals("incomplete_error", snap.optString("status"))
    }

    @Test
    fun preferredSourceNetworkFailureDoesNotFallThrough() {
        var tablesHit = mutableListOf<String>()
        SupabaseClient.pageFetchSeam = { table, _, _, _, _ ->
            tablesHit.add(table)
            SupabaseClient.PageResult(status = "http_error", httpCode = 500, error = "down")
        }
        val out = SupabaseClient.fetchRecentEvaluationOutcomesPaged(pageSize = 10, maxPages = 3)
        assertEquals("incomplete_error", out.optString("status"))
        assertEquals("preferred_source_error_no_fallback", out.optString("selection_reason"))
        // Only first preferred table attempted
        assertEquals(1, tablesHit.distinct().size)
        assertTrue(tablesHit.first().contains("ml_evaluation_outcomes_s1") || tablesHit.first().contains("evaluation"))
    }

    @Test
    fun pageResultStatusesDistinguishErrors() {
        val http = SupabaseClient.PageResult(status = "http_error", httpCode = 500, error = "boom")
        val parse = SupabaseClient.PageResult(status = "parse_error", error = "bad json")
        val ok = SupabaseClient.PageResult(status = "success", rows = JSONArray().put(JSONObject().put("id", "1")))
        assertEquals("http_error", http.status)
        assertEquals("parse_error", parse.status)
        assertEquals("success", ok.status)
        assertEquals(1, ok.rows.length())
    }
}
