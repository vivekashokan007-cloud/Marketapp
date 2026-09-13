package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.After
import org.junit.Test
import java.io.File

class CodexRectificationR3Test {
    private fun canonicalStatus(outcomes: Int = 1, snapshots: Int = 1): JSONObject = JSONObject()
        .put("status", "complete")
        .put("kind", "canonical_eval_export")
        .put("dataset_label", "canonical_evaluation_inputs")
        .put("live_training_eligible", false)
        .put("export_cutoff", "2026-09-13T00:00:00Z")
        .put("row_count", JSONObject().put("outcomes", outcomes).put("snapshots", snapshots))

    @After
    fun tearDown() {
        SupabaseClient.resetPageFetchSeam()
    }

    @Test
    fun r31TripletMismatchFailClosed() {
        val trade = JSONObject()
            .put("index_key", "NF")
            .put("strategy_type", "BEAR_CALL")
            .put("entry_snapshot", JSONObject()
                .put("contract_lot_size", 65)
                .put("number_of_lots", 2)
                .put("quantity_units", 100))
        assertNull(resolvePositionTickLotMeta(trade))
    }

    @Test
    fun r31MultiLotScalesQuantity() {
        for (n in listOf(1, 2, 4)) {
            val qty = 65 * n
            val trade = JSONObject()
                .put("index_key", "NF")
                .put("strategy_type", "BEAR_CALL")
                .put("lots", n)
                .put("entry_snapshot", JSONObject()
                    .put("contract_lot_size", 65)
                    .put("number_of_lots", n)
                    .put("quantity_units", qty)
                    .put("lot_size", qty))
            val resolved = resolvePositionTickLotMeta(trade)
            requireNotNull(resolved)
            assertEquals(qty.toDouble(), resolved.lotSize, 0.0001)
            assertEquals(n.toDouble(), resolved.numberOfLots, 0.0001)
            assertEquals(65.0, resolved.contractLotSize ?: -1.0, 0.0001)
            val pnl1 = computePositionTickCurrentPnl(20.0, 30.0, true, resolved.lotSize)
            if (n == 1) {
                // stash via companion-less local — compare ratios below
            }
            assertEquals(n * computePositionTickCurrentPnl(20.0, 30.0, true, 65.0), pnl1, 0.0001)
        }
    }

    @Test
    fun r32CounterexampleNotEligible() {
        val payload = JSONObject()
            .put("schema_version", ContractIdentityPayload.SCHEMA_VERSION)
            .put("identity_status", "verified")
            .put("index_key", "XYZ")
            .put("expiry", "not-a-date")
            .put("contract_lot_size", -65)
            .put("number_of_lots", 2)
            .put("quantity_units", 1)
            .put("calendar_dte", -3)
            .put("trading_dte", 1.5)
            .put("quantity_basis", "hypothetical_lots")
            .put("identity_complete", true)
        val v = ContractIdentityPayload.validate(payload)
        assertFalse(v.ok)
        assertFalse(v.eligibleForContractMetrics)
        assertTrue(v.errors.isNotEmpty())
    }

    @Test
    fun r34Generic404DoesNotFallback() {
        var tables = mutableListOf<String>()
        SupabaseClient.pageFetchSeam = { table, _, _, _, _ ->
            tables.add(table)
            SupabaseClient.PageResult(
                status = "http_error",
                httpCode = 404,
                error = "Not Found",
                body = """{"message":"not found"}"""
            )
        }
        val out = SupabaseClient.fetchRecentEvaluationOutcomesPaged(pageSize = 10, maxPages = 3)
        assertEquals("incomplete_error", out.optString("status"))
        assertEquals(1, tables.distinct().size)
    }

    @Test
    fun r34ExactMissingTableMayFallback() {
        var tables = mutableListOf<String>()
        SupabaseClient.pageFetchSeam = { table, _, _, _, _ ->
            tables.add(table)
            if (table.contains("s1")) {
                SupabaseClient.PageResult(
                    status = "http_error",
                    httpCode = 404,
                    error = "Not Found",
                    body = """{"code":"PGRST205","message":"table not in schema cache"}"""
                )
            } else {
                SupabaseClient.PageResult(
                    status = "success",
                    rows = JSONArray().put(JSONObject().put("id", "1").put("created_at", "2026-09-13T00:00:00Z")),
                    httpCode = 200
                )
            }
        }
        val out = SupabaseClient.fetchRecentEvaluationOutcomesPaged(pageSize = 10, maxPages = 3)
        assertTrue(tables.size >= 2)
        assertTrue(out.optString("status") in setOf("complete", "empty") || out.has("rows"))
    }

    @Test
    fun r34FreezeCutoffPresent() {
        SupabaseClient.pageFetchSeam = { _, filter, _, _, _ ->
            assertNotNull(filter)
            assertTrue(filter!!.contains("created_at=lte."))
            SupabaseClient.PageResult(status = "success", rows = JSONArray(), httpCode = 200)
        }
        val out = SupabaseClient.fetchRecentEvaluationOutcomesPaged(pageSize = 10, maxPages = 1)
        assertTrue(out.has("export_cutoff"))
    }

    @Test
    fun r36FaultInjectionNeverMixesGenerations() {
        val root = File.createTempFile("canon_export", "dir")
        root.delete()
        root.mkdirs()
        try {
            val status1 = canonicalStatus()
            val w1 = CanonicalExportStore.writeGeneration(
                root, """[{"id":"a"}]""".toByteArray(), """[{"id":"s1"}]""".toByteArray(), status1
            )
            assertTrue(w1.ok)
            val read1 = CanonicalExportStore.readCurrent(root)
            assertTrue(read1.ok)
            assertEquals("""[{"id":"a"}]""", String(read1.outcomes!!))

            val status2 = canonicalStatus()
            val w2 = CanonicalExportStore.writeGeneration(
                root,
                """[{"id":"b"}]""".toByteArray(),
                """[{"id":"s2"}]""".toByteArray(),
                status2,
                faultAfter = "after_outcomes"
            )
            assertFalse(w2.ok)
            val readAfterFault = CanonicalExportStore.readCurrent(root)
            assertTrue(readAfterFault.ok)
            // Still entire old generation
            assertEquals("""[{"id":"a"}]""", String(readAfterFault.outcomes!!))
            assertEquals("""[{"id":"s1"}]""", String(readAfterFault.snapshots!!))

            val lastAttempt = File(root, CanonicalExportStore.LAST_ATTEMPT_POINTER)
            assertTrue(lastAttempt.exists())
            val lastGood = File(root, CanonicalExportStore.LAST_GOOD_POINTER)
            assertTrue(lastGood.exists())
            assertTrue(JSONObject(lastGood.readText()).optString("generation_id") == w1.generationId)
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun r36IncompleteDoesNotOverwriteLastGood() {
        val root = File.createTempFile("canon_export2", "dir")
        root.delete(); root.mkdirs()
        try {
            val w1 = CanonicalExportStore.writeGeneration(
                root, """[1]""".toByteArray(), """[1]""".toByteArray(),
                canonicalStatus()
            )
            assertTrue(w1.ok)
            val w2 = CanonicalExportStore.writeGeneration(
                root, """[2]""".toByteArray(), """[2]""".toByteArray(),
                canonicalStatus().put("status", "incomplete_error")
            )
            assertFalse(w2.ok)
            val read = CanonicalExportStore.readCurrent(root)
            assertEquals("""[1]""", String(read.outcomes!!))
        } finally {
            root.deleteRecursively()
        }
    }
}
