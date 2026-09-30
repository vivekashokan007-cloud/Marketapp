package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * Codex round-2 finding B3: the parity audit must certify exact reconstruction
 * from stored bytes, and must make missing, extra, duplicate, mismatched,
 * capped and non-reconstructable evidence visible.
 *
 * Fixtures are built from a real production decision (29 Sep 2026, index 0,
 * `parameter_threshold`) with values varied per index, and every input goes
 * through a JSON-Lines text round trip exactly as the export does.
 */
class Pc2ParityAuditTest {

    /** A real decision_json from ml_pc2_authority_decisions, 29 Sep 2026. */
    private val template = JSONObject(
        """{"constant":"DOW_THRESHOLD","slice_key":"dow_pct|UNKNOWN|UNKNOWN|UNKNOWN","hard_passed":false,""" +
            """"promotion_id":null,"stability_bar":0.1,"support_count":0,"variable_name":"dow_pct",""" +
            """"authority_kind":"parameter_threshold","diversity_pass":false,"observed_value":0.13,""" +
            """"stability_pass":false,"authority_state":"SHADOW",""" +
            """"fallback_reason":"stability_or_history_not_ready|censor_guard_not_clear","neutrality_pass":false,""" +
            """"neutrality_tick":0.01,"stability_ratio":null,"context_variable":"dow_pct","diversity_status":"NO_HISTORY",""" +
            """"neutrality_delta":null,"percentile_passed":false,"provenance_policy":"market_scalar_not_required",""" +
            """"censor_guard_flags":["no_history"],"censor_guard_status":"NO_HISTORY","hard_threshold_value":0.5,""" +
            """"input_contract_valid":true,"promotion_valid_until":null,""" +
            """"authority_state_reason":"stability_or_history_not_ready|censor_guard_not_clear",""" +
            """"input_contract_reasons":[],"authority_policy_version":"pc2_authority_policy_v2",""" +
            """"percentile_threshold_value":null,"population_provenance_scope":"not_required_for_market_scalar",""" +
            """"authority_diagnostics_version":"pc2_authority_diagnostics_v1",""" +
            """"population_provenance_version":"pc2_history_population_provenance_v1",""" +
            """"population_provenance_verified":true,"counterfactual_behavior_differs":false}"""
    )
    private val policy = JSONObject()
        .put("version", "pc2_authority_policy_v2")
        .put("authority_diagnostics_version", "pc2_authority_diagnostics_v1")

    private class Session {
        val snapshots = mutableListOf<JSONObject>()
        val batches = mutableListOf<JSONObject>()
        val legacy = mutableListOf<JSONObject>()
    }

    private fun decisions(n: Int, salt: String, distinct: Int = 7): JSONArray = JSONArray().also { a ->
        repeat(n) { i ->
            a.put(JSONObject(template.toString())
                .put("observed_value", 0.13 + (i % distinct) / 100.0)
                .put("variable_name", "dow_pct_${i % distinct}")
                .put("slice_key", "$salt|${i % distinct}"))
        }
    }

    private fun addPoll(
        s: Session,
        minute: Int,
        n: Int = 20,
        salt: String = "s",
        keepSnapshotArray: Boolean = true,
        storeBatch: Boolean = true,
        writeLegacy: Boolean = true,
        array: JSONArray = decisions(n, salt)
    ): Pc2CompactBatch.Built {
        val pollUtc = "2026-09-30T04:%02d:00.000000Z".format(minute)
        val built = requireNotNull(Pc2CompactBatch.build(
            JSONObject()
                .put("session_date", "2026-09-30")
                .put("poll_ts", "2026-09-30T10:%02d:00+0530".format(minute))
                .put("context_json", JSONObject()
                    .put("snapshot_pc2_authority_decisions", array)
                    .put("snapshot_pc2_authority_policy", policy)
                    .put("snapshot_brain_version", "2.6.63"))
        ))
        s.snapshots += JSONObject()
            .put("poll_ts_utc", pollUtc)
            .put("session_date", "2026-09-30")
            .put("compact_ref", built.snapshotRef)
            .put("decisions", if (keepSnapshotArray) array else JSONObject.NULL)
            .put("policy", policy)
            .put("brain_version", "2.6.63")
        if (storeBatch) s.batches += JSONObject(built.batchRow.toString())
        if (writeLegacy) {
            for (i in 0 until array.length()) {
                s.legacy += JSONObject().put("poll_ts_utc", pollUtc).put("decision_index", i)
                    .put("decision_json", array.getJSONObject(i))
            }
        }
        return built
    }

    /** Exactly what the export produces: one JSON text per line, re-parsed. */
    private fun roundTrip(rows: List<JSONObject>) = rows.map { JSONObject(it.toString()) }

    private fun audit(s: Session) = Pc2ParityAudit.audit(roundTrip(s.snapshots), roundTrip(s.batches), roundTrip(s.legacy))

    private fun Pc2ParityAudit.Report.categories() = findings.map { it.category }.toSet()

    /** Rewrites a stored row (and optionally its reference) so every hash is self-consistent. */
    private fun forge(row: JSONObject, grouped: String, decisionDigest: String): JSONObject {
        val groupedDigest = Pc2CompactBatch.sha256(grouped)
        val id = Pc2CompactBatch.batchIdOf(
            row.getString("session_date_text"), row.getString("poll_ts_text"), row.getString("brain_version"),
            row.getString("policy_hash"), row.getString("policy_version"),
            row.getString("authority_diagnostics_version"), decisionDigest, groupedDigest
        )
        return JSONObject(row.toString())
            .put("grouped_canonical", grouped).put("grouped_digest", groupedDigest)
            .put("decision_digest", decisionDigest).put("batch_id", id)
    }

    private fun refFrom(original: JSONObject, row: JSONObject): JSONObject = JSONObject(original.toString())
        .put("batch_id", row.getString("batch_id"))
        .put("grouped_digest", row.getString("grouped_digest"))
        .put("decision_digest", row.getString("decision_digest"))

    // ---- certification ------------------------------------------------------

    @Test
    fun aCleanSessionIsCertifiedAndCappingIsReportedSeparately() {
        val s = Session()
        addPoll(s, 0)
        addPoll(s, 5, n = 128, salt = "capped")          // at the tail cap
        addPoll(s, 10)
        val report = audit(s)
        assertTrue(report.findings.joinToString(), report.certified)
        assertEquals(3, report.certifiedPolls)
        assertEquals(1, report.cappedPolls)
        assertEquals(setOf("CAPPED_SOURCE"), report.categories())
        assertEquals(3, report.sourceFromSnapshot)
    }

    @Test
    fun theLegacyTableIsTheSourceWhenTheSnapshotDroppedTheArray() {
        val s = Session()
        addPoll(s, 0, keepSnapshotArray = false)
        val report = audit(s)
        assertTrue(report.findings.joinToString(), report.certified)
        assertEquals(1, report.sourceFromLegacy)
    }

    @Test
    fun charactersThatDivergedBetweenPlatformsNowCertify() {
        val s = Session()
        val array = JSONArray().put(JSONObject(template.toString())
            .put("fallback_reason", "n/a </x> \u0085   é 😀 tab\there"))
        addPoll(s, 0, array = array)
        val report = audit(s)
        assertTrue(report.findings.joinToString(), report.certified)
    }

    // ---- Codex's counterexample and its relatives ---------------------------

    @Test
    fun codexCounterexampleIsRejectedEvenWhenEveryHashIsSelfConsistent() {
        val s = Session()
        val built = addPoll(s, 0, n = 2, array = decisions(2, "cx", distinct = 1))
        // {"ordered_count":2,"prototypes":[{"decision":{"x":1},"indexes":[0,0]}]}, zero digest,
        // with the completeness fields present so only the structure is wrong.
        val grouped = Pc2CompactBatch.canonicalJson(JSONObject(Pc2CompactBatch.groupedOf(built.batchRow).toString())
            .put("prototypes", JSONArray().put(JSONObject().put("decision", JSONObject().put("x", 1))
                .put("indexes", JSONArray().put(0).put(0)))))
        val forged = forge(built.batchRow, grouped, "0".repeat(64))
        s.batches[0] = forged
        s.snapshots[0].put("compact_ref", refFrom(built.snapshotRef, forged))
        val report = audit(s)
        assertFalse(report.certified)
        val categories = report.categories()
        assertTrue(categories.toString(), "NOT_RECONSTRUCTABLE" in categories)
        assertTrue("the source proves the zero digest false", "SOURCE_DIGEST_MISMATCH" in categories)
        assertFalse("hashes were made consistent, so these are not what caught it",
            "GROUPED_BYTES_MISMATCH" in categories || "STORED_IDENTITY_INCONSISTENT" in categories)
    }

    @Test
    fun gappedAndOutOfRangeIndexesAreNotReconstructable() {
        val cases = listOf(3 to listOf(0, 2), 2 to listOf(1), 2 to listOf(0, 1, 2))   // gap, gap at 0, out of range
        for ((count, indexes) in cases) {
            val s = Session()
            val built = addPoll(s, 0, n = 2, array = decisions(2, "gap", distinct = 1))
            val grouped = Pc2CompactBatch.canonicalJson(JSONObject(Pc2CompactBatch.groupedOf(built.batchRow).toString())
                .put("ordered_count", count)
                .put("prototypes", JSONArray().put(JSONObject().put("decision", decisions(1, "gap").getJSONObject(0))
                    .put("indexes", JSONArray(indexes)))))
            val forged = forge(built.batchRow, grouped, built.batchRow.getString("decision_digest"))
            s.batches[0] = forged
            s.snapshots[0].put("compact_ref", refFrom(built.snapshotRef, forged))
            val report = audit(s)
            assertTrue("$indexes: ${report.categories()}", "NOT_RECONSTRUCTABLE" in report.categories())
            assertFalse(report.certified)
        }
    }

    @Test
    fun aWrongStoredDigestIsCaughtOnValidStructure() {
        val s = Session()
        val built = addPoll(s, 0)
        val forged = forge(built.batchRow, built.batchRow.getString("grouped_canonical"), "a".repeat(64))
        s.batches[0] = forged
        s.snapshots[0].put("compact_ref", refFrom(built.snapshotRef, forged))
        val report = audit(s)
        assertFalse(report.certified)
        assertTrue(report.categories().toString(), "STORED_DIGEST_MISMATCH" in report.categories())
        assertFalse("NOT_RECONSTRUCTABLE" in report.categories())
    }

    @Test
    fun aCappedSourceCannotBeStoredAsUncapped() {
        val s = Session()
        val built = addPoll(s, 0, n = 128, salt = "cap")
        val relaxed = JSONObject(Pc2CompactBatch.groupedOf(built.batchRow).toString())
            .put("source_possibly_truncated", false)
        s.batches[0] = forge(built.batchRow, Pc2CompactBatch.canonicalJson(relaxed), built.batchRow.getString("decision_digest"))
        s.snapshots[0].put("compact_ref", refFrom(built.snapshotRef, s.batches[0]))
        val report = audit(s)
        assertFalse(report.certified)
        assertTrue(report.categories().toString(), "COMPLETENESS_MISMATCH" in report.categories())
    }

    // ---- missing / extra / duplicate / unavailable --------------------------

    @Test
    fun aMissingBatchFails() {
        val s = Session()
        addPoll(s, 0, storeBatch = false)
        val report = audit(s)
        assertFalse(report.certified)
        assertEquals(setOf("MISSING_BATCH"), report.categories())
    }

    @Test
    fun aRerunIsVisibleAndDoesNotBlockCertification() {
        val s = Session()
        addPoll(s, 0)
        // Same poll, different decisions, stored but not referenced.
        val rerun = Session()
        addPoll(rerun, 0, salt = "rerun")
        s.batches += rerun.batches
        val report = audit(s)
        assertTrue(report.findings.joinToString(), report.certified)
        assertEquals(setOf("EXTRA_BATCH"), report.categories())
    }

    @Test
    fun anUnreferencedBrokenRowStillBlocksCertification() {
        val s = Session()
        addPoll(s, 0)
        val other = Session()
        val b = addPoll(other, 5, n = 2, array = decisions(2, "x", distinct = 1))
        val grouped = Pc2CompactBatch.canonicalJson(JSONObject(Pc2CompactBatch.groupedOf(b.batchRow).toString())
            .put("prototypes", JSONArray().put(JSONObject().put("decision", JSONObject().put("x", 1))
                .put("indexes", JSONArray().put(0).put(0)))))
        s.batches += forge(b.batchRow, grouped, "0".repeat(64))
        val report = audit(s)
        assertFalse(report.certified)
        assertTrue("STORED_ROW_NOT_RECONSTRUCTABLE" in report.categories())
    }

    @Test
    fun aDuplicateReferenceIsVisible() {
        val s = Session()
        addPoll(s, 0)
        s.snapshots += JSONObject(s.snapshots[0].toString()).put("poll_ts_utc", "2026-09-30T04:01:00.000000Z")
        val report = audit(s)
        assertTrue("DUPLICATE_REFERENCE" in report.categories())
    }

    @Test
    fun disagreeingSourceCopiesFail() {
        val s = Session()
        addPoll(s, 0)
        s.legacy[3].put("decision_json", JSONObject(template.toString()).put("observed_value", 9.99))
        val report = audit(s)
        assertFalse(report.certified)
        assertTrue("LEGACY_SNAPSHOT_DISAGREE" in report.categories())
    }

    @Test
    fun noSourceAnywhereFails() {
        val s = Session()
        addPoll(s, 0, keepSnapshotArray = false, writeLegacy = false)
        val report = audit(s)
        assertFalse(report.certified)
        assertTrue("SOURCE_UNAVAILABLE" in report.categories())
    }

    @Test
    fun evidenceWithoutAReferenceFails() {
        val s = Session()
        addPoll(s, 0)
        s.snapshots[0].remove("compact_ref")
        val report = audit(s)
        assertFalse(report.certified)
        assertTrue("NO_COMPACT_REF" in report.categories())
    }

    @Test
    fun aDefectiveLegacySourceIsReported() {
        val s = Session()
        addPoll(s, 0, keepSnapshotArray = false)
        s.legacy[5].put("decision_index", 4)                       // duplicate 4, missing 5
        val report = audit(s)
        assertFalse(report.certified)
        assertTrue("LEGACY_INDEX_DEFECT" in report.categories())
    }

    @Test
    fun theRunnerWritesAReportAndMatchesTheExportFormat() {
        val s = Session()
        addPoll(s, 0)
        val dir = Files.createTempDirectory("pc2-audit-run").toFile()
        File(dir, "snapshots.jsonl").writeText(s.snapshots.joinToString("\n") { it.toString() })
        File(dir, "batches.jsonl").writeText(s.batches.joinToString("\n") { it.toString() })
        File(dir, "legacy.jsonl").writeText(s.legacy.joinToString("\n") { it.toString() })
        val report = Pc2ParityAudit.run(dir)
        assertTrue(report.certified)
        val written = JSONObject(File(dir, "pc2_parity_report.json").readText())
        assertTrue(written.getBoolean("certified"))
        assertEquals(1, written.getInt("certified_polls"))
    }
}
