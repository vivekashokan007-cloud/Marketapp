package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class CodexRectificationR4Test {
    @After
    fun tearDown() {
        SupabaseClient.resetPageFetchSeam()
    }

    private fun canonicalStatus(outcomes: Int = 1, snapshots: Int = 1): JSONObject = JSONObject()
        .put("status", "complete")
        .put("kind", "canonical_eval_export")
        .put("dataset_label", "canonical_evaluation_inputs")
        .put("live_training_eligible", false)
        .put("export_cutoff", "2026-09-13T00:00:00Z")
        .put("row_count", JSONObject().put("outcomes", outcomes).put("snapshots", snapshots))

    @Test
    fun preferredSuccessfulEmptyIsAuthoritative() {
        val tables = mutableListOf<String>()
        SupabaseClient.pageFetchSeam = { table, _, _, _, _ ->
            tables.add(table)
            SupabaseClient.PageResult(status = "success", rows = JSONArray(), httpCode = 200)
        }
        val out = SupabaseClient.fetchRecentEvaluationOutcomesPaged(10, 3, "2026-09-13T00:00:00Z")
        assertEquals("empty", out.optString("status"))
        assertEquals(1, tables.size)
        assertTrue(out.optString("selection_reason").startsWith("authoritative_empty:"))
    }

    @Test
    fun onlyStructuredMissingTableCodesPermitFallback() {
        for (code in listOf("PGRST205", "42P01")) {
            val page = SupabaseClient.PageResult(
                status = "http_error",
                httpCode = 404,
                body = """{"code":"$code","message":"missing"}""",
            )
            assertTrue(code, SupabaseClient.isExactMissingTableError(page))
        }
        for (body in listOf(
            "relation ml_evaluation_outcomes_s1 does not exist",
            "PGRST205 table not found",
            """{"code":"42703","message":"column does not exist"}""",
        )) {
            val page = SupabaseClient.PageResult(status = "http_error", httpCode = 404, body = body)
            assertFalse(body, SupabaseClient.isExactMissingTableError(page))
        }
    }

    @Test
    fun compoundOrderUsesFrozenOffsetNotPartialKeyset() {
        val offsets = mutableListOf<Int>()
        val filters = mutableListOf<String>()
        SupabaseClient.pageFetchSeam = { _, filter, order, _, offset ->
            offsets.add(offset ?: -1)
            filters.add(filter ?: "")
            assertEquals("exit_date.asc,created_at.asc,id.asc", order)
            val rows = if ((offset ?: 0) == 0) {
                JSONArray()
                    .put(JSONObject().put("id", "a").put("exit_date", "2026-09-10").put("created_at", "2026-09-12T00:00:00Z"))
                    .put(JSONObject().put("id", "b").put("exit_date", "2026-09-11").put("created_at", "2026-09-10T00:00:00Z"))
            } else {
                JSONArray().put(JSONObject().put("id", "c").put("exit_date", "2026-09-12").put("created_at", "2026-09-09T00:00:00Z"))
            }
            SupabaseClient.PageResult(status = "success", rows = rows, httpCode = 200)
        }
        val out = SupabaseClient.selectAllPages(
            table = "trades_v2",
            order = "exit_date.asc,created_at.asc,id.asc",
            pageSize = 2,
            maxPages = 3,
            freezeCutoffIso = "2026-09-13T00:00:00Z",
        )
        assertEquals(3, out.getJSONArray("rows").length())
        assertEquals(listOf(0, 2), offsets)
        assertTrue(filters.all { !it.contains("or=(created_at") })
        assertEquals("frozen_offset", out.optString("cursor_mode"))
    }

    @Test
    fun callerSuppliedCutoffIsSharedExactly() {
        val cutoff = "2026-09-13T01:02:03Z"
        SupabaseClient.pageFetchSeam = { _, filter, _, _, _ ->
            assertTrue(filter?.contains("created_at=lte.$cutoff") == true)
            SupabaseClient.PageResult(status = "success", rows = JSONArray(), httpCode = 200)
        }
        val outcomes = SupabaseClient.fetchRecentEvaluationOutcomesPaged(10, 1, cutoff)
        val snapshots = SupabaseClient.fetchRecentBrainSnapshotsPaged(10, 1, cutoff)
        assertEquals(cutoff, outcomes.optString("export_cutoff"))
        assertEquals(cutoff, snapshots.optString("export_cutoff"))
    }

    @Test
    fun pointerFailureLeavesPriorCompleteGenerationVisible() {
        val root = createTempDir(prefix = "r4-canonical-")
        try {
            val first = CanonicalExportStore.writeGeneration(
                root,
                "[{\"id\":\"old\"}]".toByteArray(),
                "[{\"id\":\"s-old\"}]".toByteArray(),
                canonicalStatus(),
                cutoff = "2026-09-13T00:00:00Z",
            )
            assertTrue(first.ok)
            val second = CanonicalExportStore.writeGeneration(
                root,
                "[{\"id\":\"new\"}]".toByteArray(),
                "[{\"id\":\"s-new\"}]".toByteArray(),
                canonicalStatus(),
                cutoff = "2026-09-13T00:00:00Z",
                faultAfter = "pointer_failure",
            )
            assertFalse(second.ok)
            val visible = CanonicalExportStore.readCurrent(root)
            assertTrue(visible.ok)
            assertEquals("[{\"id\":\"old\"}]", String(visible.outcomes!!))
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun everyCanonicalWriteFaultExposesOnlyOneCompleteGeneration() {
        for (stage in listOf(
            "after_outcomes", "after_snapshots", "after_manifest",
            "before_current_pointer", "pointer_failure", "after_current_pointer",
        )) {
            val root = createTempDir(prefix = "r4-stage-")
            try {
                assertTrue(CanonicalExportStore.writeGeneration(
                    root,
                    "[{\"id\":\"old\"}]".toByteArray(),
                    "[{\"id\":\"s-old\"}]".toByteArray(),
                    canonicalStatus(),
                    cutoff = "2026-09-13T00:00:00Z",
                ).ok)
                val failed = CanonicalExportStore.writeGeneration(
                    root,
                    "[{\"id\":\"new\"}]".toByteArray(),
                    "[{\"id\":\"s-new\"}]".toByteArray(),
                    canonicalStatus(),
                    cutoff = "2026-09-13T00:00:00Z",
                    faultAfter = stage,
                )
                assertFalse(stage, failed.ok)
                val visible = CanonicalExportStore.readCurrent(root)
                assertTrue(stage, visible.ok)
                val pair = String(visible.outcomes!!) to String(visible.snapshots!!)
                assertTrue(
                    "$stage exposed mixed generation: $pair",
                    pair == ("[{\"id\":\"old\"}]" to "[{\"id\":\"s-old\"}]") ||
                        pair == ("[{\"id\":\"new\"}]" to "[{\"id\":\"s-new\"}]")
                )
                assertFalse(File(root, "canonical_eval_export_status.json").exists())
            } finally {
                root.deleteRecursively()
            }
        }
    }

    @Test
    fun corruptCurrentRequiresExplicitLastGoodRecovery() {
        val root = createTempDir(prefix = "r4-recovery-")
        try {
            assertTrue(CanonicalExportStore.writeGeneration(
                root,
                "[{\"id\":\"a\"}]".toByteArray(),
                "[{\"id\":\"s\"}]".toByteArray(),
                canonicalStatus(),
                cutoff = "2026-09-13T00:00:00Z",
            ).ok)
            File(root, CanonicalExportStore.CURRENT_POINTER).writeText("{}")
            assertFalse(CanonicalExportStore.readCurrent(root).ok)
            val recovered = CanonicalExportStore.readCurrent(root, allowLastGoodRecovery = true)
            assertTrue(recovered.ok)
            assertEquals(CanonicalExportStore.LAST_GOOD_POINTER, recovered.pointerSource)
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun malformedCompleteGenerationNeverReplacesPriorPointer() {
        val root = createTempDir(prefix = "r4-invalid-")
        try {
            val first = CanonicalExportStore.writeGeneration(
                root,
                "[{\"id\":\"old\"}]".toByteArray(),
                "[{\"id\":\"s-old\"}]".toByteArray(),
                canonicalStatus(),
                cutoff = "2026-09-13T00:00:00Z",
            )
            assertTrue(first.ok)
            val invalid = CanonicalExportStore.writeGeneration(
                root,
                "{not-array}".toByteArray(),
                "[]".toByteArray(),
                canonicalStatus(outcomes = 1, snapshots = 0),
                cutoff = "2026-09-13T00:00:00Z",
            )
            assertFalse(invalid.ok)
            assertEquals("generation_json_not_array", invalid.reason)
            val visible = CanonicalExportStore.readCurrent(root)
            assertTrue(visible.ok)
            assertEquals("[{\"id\":\"old\"}]", String(visible.outcomes!!))
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun corruptCanonicalGenerationRecoversPreviousOnlyWhenExplicit() {
        val root = createTempDir(prefix = "r4-canonical-recovery-")
        try {
            assertTrue(CanonicalExportStore.writeGeneration(
                root,
                "[{\"id\":\"old\"}]".toByteArray(),
                "[{\"id\":\"s-old\"}]".toByteArray(),
                canonicalStatus(),
                cutoff = "2026-09-13T00:00:00Z",
            ).ok)
            val second = CanonicalExportStore.writeGeneration(
                root,
                "[{\"id\":\"new\"}]".toByteArray(),
                "[{\"id\":\"s-new\"}]".toByteArray(),
                canonicalStatus(),
                cutoff = "2026-09-13T00:00:00Z",
            )
            assertTrue(second.ok)
            val secondId = requireNotNull(second.generationId)
            File(File(File(root, CanonicalExportStore.GENERATIONS_DIR), secondId), "outcomes.json")
                .writeText("[]")
            assertFalse(CanonicalExportStore.readCurrent(root).ok)
            val recovered = CanonicalExportStore.readCurrent(root, allowLastGoodRecovery = true)
            assertTrue(recovered.ok)
            assertEquals(CanonicalExportStore.LAST_GOOD_POINTER, recovered.pointerSource)
            assertEquals("[{\"id\":\"old\"}]", String(recovered.outcomes!!))
            assertEquals("[{\"id\":\"s-old\"}]", String(recovered.snapshots!!))
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun paperGenerationReaderRejectsTamperAndRecoversOnlyWhenExplicit() {
        val root = createTempDir(prefix = "r4-paper-")
        try {
            val status = JSONObject()
                .put("status", "complete")
                .put("export_cutoff", "2026-09-13T00:00:00Z")
                .put("row_count", 1)
            val first = CanonicalExportStore.writePaperResearchExport(
                root, "[{\"id\":\"paper-old\"}]", status
            )
            assertTrue(first.ok)
            val second = CanonicalExportStore.writePaperResearchExport(
                root, "[{\"id\":\"paper-new\"}]", status
            )
            assertTrue(second.ok)
            val secondId = requireNotNull(second.generationId)
            File(root, CanonicalExportStore.CURRENT_POINTER).writeText("{}")
            assertFalse(CanonicalExportStore.readCurrentPaper(root).ok)
            val recoveredPointer = CanonicalExportStore.readCurrentPaper(root, allowLastGoodRecovery = true)
            assertTrue(recoveredPointer.ok)
            assertEquals(CanonicalExportStore.LAST_GOOD_POINTER, recoveredPointer.pointerSource)
            assertEquals("[{\"id\":\"paper-old\"}]", String(recoveredPointer.data!!))

            // Restore the new current pointer, corrupt only its generation,
            // then prove explicit recovery returns the previous complete one.
            val newPointer = JSONObject()
                .put("schema_version", CanonicalExportStore.MANIFEST_SCHEMA_VERSION)
                .put("generation_id", secondId)
                .put("manifest_file", "${CanonicalExportStore.GENERATIONS_DIR}/$secondId/manifest.json")
                .put("paper_file", "${CanonicalExportStore.GENERATIONS_DIR}/$secondId/paper_trades.json")
                .put("checksum_sha256", second.manifest!!.getString("checksum_sha256"))
                .put("status", "complete")
                .put("export_cutoff", "2026-09-13T00:00:00Z")
            CanonicalExportStore.atomicWritePointer(File(root, CanonicalExportStore.CURRENT_POINTER), newPointer)
            File(File(File(root, CanonicalExportStore.GENERATIONS_DIR), secondId), "paper_trades.json")
                .writeText("[]")
            assertFalse(CanonicalExportStore.readCurrentPaper(root).ok)
            val recoveredGeneration = CanonicalExportStore.readCurrentPaper(root, allowLastGoodRecovery = true)
            assertTrue(recoveredGeneration.ok)
            assertEquals("[{\"id\":\"paper-old\"}]", String(recoveredGeneration.data!!))
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun fakeLotSourceAndMissingDteAreQuarantined() {
        val payload = JSONObject()
            .put("schema_version", ContractIdentityPayload.SCHEMA_VERSION)
            .put("identity_status", "verified")
            .put("identity_complete", true)
            .put("index_key", "NF")
            .put("expiry", "2026-09-17")
            .put("contract_lot_size", 65)
            .put("number_of_lots", 1)
            .put("quantity_units", 65)
            .put("quantity_basis", "hypothetical_lots")
            .put("lot_source", "banana")
        val result = ContractIdentityPayload.validate(payload)
        assertFalse(result.ok)
        assertFalse(result.eligibleForContractMetrics)
        assertTrue(result.errors.any { it.contains("invalid_lot_source") })
        assertTrue(result.errors.any { it.contains("dte_basis") })
        assertEquals("quarantine", result.payload?.optString("identity_status"))
    }

    @Test
    fun missingSchemaIsQuarantinedWithoutBeingInvented() {
        val payload = JSONObject()
            .put("identity_status", "verified")
            .put("identity_complete", true)
            .put("index_key", "NF")
            .put("expiry", "2026-09-17")
            .put("session_date", "2026-09-10")
            .put("contract_lot_size", 65)
            .put("number_of_lots", 1)
            .put("quantity_units", 65)
            .put("quantity_basis", "hypothetical_lots")
            .put("lot_source", "authoritative_contract_rule")
            .put("lot_table_version", ContractIdentityPayload.LOT_TABLE_VERSION)
            .put("source_ref", "NSE_FAOP_70616")
            .put("dte_basis", "explicit_calendar_dte")
            .put("calendar_dte", 7)
            .put("dte_source", "captured_contract_metadata")
        val result = ContractIdentityPayload.validate(payload)
        assertFalse(result.eligibleForContractMetrics)
        assertTrue(result.errors.contains("verified_requires_exact_schema_version"))
        assertFalse(result.payload!!.has("schema_version"))
    }

    @Test
    fun everyRecognizedVerifiedLotSourceAndDteBasisIsCovered() {
        val lotSources = mapOf(
            "authoritative_contract_rule" to mapOf(
                "lot_table_version" to ContractIdentityPayload.LOT_TABLE_VERSION,
                "source_ref" to "NSE_FAOP_70616",
            ),
            "captured_metadata_consistent" to mapOf(
                "lot_table_version" to ContractIdentityPayload.LOT_TABLE_VERSION,
                "source_ref" to "NSE_FAOP_70616",
            ),
            "captured_metadata" to mapOf(
                "source_ref" to "instrument_master_snapshot",
                "source_digest" to "sha256:test-fixture",
            ),
        )
        val dteBases = mapOf(
            "nse_trading_calendar" to mapOf<String, Any>(
                "calendar_dte" to 7,
                "trading_dte" to 5,
                "calendar_version" to "nse_holiday_years:2026",
                "calendar_coverage_ok" to true,
            ),
            "explicit_calendar_dte" to mapOf<String, Any>(
                "calendar_dte" to 7,
                "dte_source" to "captured_contract_metadata",
                "source_ref" to "instrument_master_snapshot",
            ),
            "explicit_trading_dte" to mapOf<String, Any>(
                "trading_dte" to 5,
                "dte_source" to "captured_contract_metadata",
                "source_ref" to "instrument_master_snapshot",
            ),
        )
        for ((lotSource, lotFields) in lotSources) {
            for ((dteBasis, dteFields) in dteBases) {
                val payload = JSONObject()
                    .put("schema_version", ContractIdentityPayload.SCHEMA_VERSION)
                    .put("identity_status", "verified")
                    .put("identity_complete", true)
                    .put("index_key", "NF")
                    .put("expiry", "2026-09-17")
                    .put("session_date", "2026-09-10")
                    .put("contract_lot_size", 65)
                    .put("number_of_lots", 1)
                    .put("quantity_units", 65)
                    .put("quantity_basis", "hypothetical_lots")
                    .put("lot_source", lotSource)
                    .put("dte_basis", dteBasis)
                lotFields.forEach { (key, value) -> payload.put(key, value) }
                dteFields.forEach { (key, value) -> payload.put(key, value) }
                val result = ContractIdentityPayload.validate(payload)
                assertTrue("$lotSource/$dteBasis: ${result.errors}", result.eligibleForContractMetrics)
            }
        }
    }
}
