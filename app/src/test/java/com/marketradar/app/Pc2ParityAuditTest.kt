package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.time.Instant

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
        /** Independent chain evidence: 11 s before each poll, as in production. */
        val inventory = mutableListOf<String>()
    }

    /** UTC instant of the client poll `2026-09-30T10:mm:00+0530`. */
    private fun utcOf(minute: Int) = "2026-09-30T04:%02d:00.000000Z".format(30 + minute)
    private fun chainOf(minute: Int) = "2026-09-30T04:%02d:49.000000Z".format(29 + minute)

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
        array: JSONArray = decisions(n, salt),
        inInventory: Boolean = true
    ): Pc2CompactBatch.Built {
        val pollUtc = utcOf(minute)
        if (inInventory) s.inventory += chainOf(minute)
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

    private fun evidence(s: Session, day: String = "2026-09-30", malformed: Int = 0) =
        Pc2ParityAudit.SessionEvidence(day, s.inventory.toList(), malformed)

    private fun audit(
        s: Session,
        evidence: Pc2ParityAudit.SessionEvidence? = evidence(s),
        exceptions: List<JSONObject> = emptyList()
    ) = Pc2ParityAudit.audit(evidence, roundTrip(s.snapshots), roundTrip(s.batches), roundTrip(s.legacy), exceptions)

    private fun Pc2ParityAudit.Report.fails() =
        findings.filter { it.severity == Pc2ParityAudit.Severity.FAIL }.map { it.category }.toSet()

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
    fun codexA2CounterexampleACopiedSnapshotOnTheNextMinuteFails() {
        val s = Session()
        addPoll(s, 0)
        // Exactly Codex's source counterexample: copy a valid snapshot, change only
        // its exported timestamp. Source, reference and stored batch are the old poll's.
        s.snapshots += JSONObject(s.snapshots[0].toString()).put("poll_ts_utc", utcOf(1))
        s.inventory += chainOf(1)
        val report = audit(s)
        assertFalse(report.certified)
        assertTrue(report.fails().toString(), "REFERENCE_POLL_MISMATCH" in report.fails())
        assertEquals("only the genuine poll verifies", 1, report.certifiedPolls)
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
        val dir = writeExport(s)
        val report = Pc2ParityAudit.run(dir)
        assertTrue(report.certified)
        val written = JSONObject(File(dir, "pc2_parity_report.json").readText())
        assertTrue(written.getBoolean("certified"))
        assertEquals(1, written.getInt("certified_polls"))
    }

    // =========================================================================
    // Export directory helper: exactly what tools/pc2_parity_export.sh writes
    // =========================================================================

    private fun sha256Hex(bytes: ByteArray) = java.security.MessageDigest.getInstance("SHA-256")
        .digest(bytes).joinToString("") { "%02x".format(it.toInt() and 0xff) }

    private fun writeExport(s: Session, day: String = "2026-09-30", malformed: Int = 0): File {
        val dir = Files.createTempDirectory("pc2-export").toFile()
        val files = JSONObject()
        val contents = mapOf(
            "snapshots.jsonl" to s.snapshots,
            "batches.jsonl" to s.batches,
            "legacy.jsonl" to s.legacy,
            "inventory.jsonl" to s.inventory.map { JSONObject().put("poll_ts_utc", it) }
        )
        contents.forEach { (name, rows) ->
            val text = rows.joinToString("") { it.toString() + "\n" }
            File(dir, name).writeText(text)
            files.put(name, JSONObject().put("rows", rows.size).put("sha256", sha256Hex(text.toByteArray())))
        }
        File(dir, "manifest.json").writeText(JSONObject().put("complete", true).put("ist_day", day)
            .put("malformed_batch_rows", malformed).put("files", files).toString())
        return dir
    }

    private fun manifestOf(dir: File) = JSONObject(File(dir, "manifest.json").readText())

    // =========================================================================
    // A1 - missing or empty inputs can never certify a session
    // =========================================================================

    @Test
    fun codexA1EmptyListsDoNotCertify() {
        val empty = Pc2ParityAudit.audit(Pc2ParityAudit.SessionEvidence("2026-09-30", emptyList()), emptyList(), emptyList(), emptyList())
        assertFalse(empty.certified)
        assertTrue(empty.fails().toString(), "EMPTY_INVENTORY" in empty.fails() && "NO_PC2_EVIDENCE_IN_SESSION" in empty.fails())
        val noEvidence = Pc2ParityAudit.audit(null, emptyList(), emptyList(), emptyList())
        assertFalse(noEvidence.certified)
        assertTrue("MISSING_SESSION_EVIDENCE" in noEvidence.fails())
    }

    @Test
    fun codexA1AnEmptyDirectoryDoesNotCertify() {
        val report = Pc2ParityAudit.run(Files.createTempDirectory("pc2-empty").toFile())
        assertFalse(report.certified)
        assertTrue("INPUT_INCOMPLETE" in report.fails())
    }

    @Test
    fun codexA1EachMissingFileFails() {
        val s = Session(); addPoll(s, 0)
        for (name in Pc2ParityAudit.DATA_FILES + Pc2ParityAudit.MANIFEST) {
            val dir = writeExport(s)
            File(dir, name).delete()
            val report = Pc2ParityAudit.run(dir)
            assertFalse("without $name", report.certified)
            assertTrue("without $name", "INPUT_INCOMPLETE" in report.fails())
        }
    }

    @Test
    fun codexA1ThreeEmptyFilesWithAHonestManifestDoNotCertify() {
        val report = Pc2ParityAudit.run(writeExport(Session()))
        assertFalse(report.certified)
        assertTrue(report.fails().toString(), "EMPTY_INVENTORY" in report.fails())
    }

    @Test
    fun codexA1LegacyOnlyInputsDoNotCertify() {
        val s = Session(); addPoll(s, 0)
        s.snapshots.clear(); s.batches.clear()
        val report = audit(s)
        assertFalse(report.certified)
        assertTrue(report.fails().toString(),
            "LEGACY_WITHOUT_SNAPSHOT" in report.fails() && "MISSING_SNAPSHOT_FOR_POLL" in report.fails())
    }

    @Test
    fun codexA1APartiallyExportedFileIsDetected() {
        val s = Session(); addPoll(s, 0)
        val dir = writeExport(s)
        val legacy = File(dir, "legacy.jsonl")
        legacy.writeText(legacy.readLines().dropLast(1).joinToString("") { it + "\n" })
        val report = Pc2ParityAudit.run(dir)
        assertFalse(report.certified)
        assertTrue(report.findings.any { it.category == "INPUT_INCOMPLETE" && it.detail.contains("legacy.jsonl") })
    }

    @Test
    fun codexA1AnIncompleteOrAlteredManifestIsDetected() {
        val s = Session(); addPoll(s, 0)
        val notComplete = writeExport(s)
        File(notComplete, "manifest.json").writeText(manifestOf(notComplete).put("complete", false).toString())
        assertFalse(Pc2ParityAudit.run(notComplete).certified)

        val tampered = writeExport(s)
        File(tampered, "snapshots.jsonl").appendText(" ")
        val report = Pc2ParityAudit.run(tampered)
        assertFalse(report.certified)
        assertTrue(report.findings.any { it.detail.contains("SHA-256") })
    }

    @Test
    fun codexA1MissingOneExpectedPollFails() {
        val s = Session(); addPoll(s, 0); addPoll(s, 5)
        s.inventory += chainOf(10)                     // the phone polled; nothing was captured
        val report = audit(s)
        assertFalse(report.certified)
        assertTrue("MISSING_SNAPSHOT_FOR_POLL" in report.fails())
    }

    @Test
    fun aSnapshotWithoutIndependentEvidenceFails() {
        val s = Session(); addPoll(s, 0); addPoll(s, 5, inInventory = false)
        val report = audit(s)
        assertFalse(report.certified)
        assertTrue("SNAPSHOT_WITHOUT_INVENTORY" in report.fails())
    }

    @Test
    fun aSessionWhosePollsCarryNoPc2CannotCount() {
        val s = Session()
        s.inventory += chainOf(0)
        s.snapshots += JSONObject().put("poll_ts_utc", utcOf(0)).put("session_date", "2026-09-30")
            .put("brain_version", "2.6.63")
        val report = audit(s)
        assertFalse(report.certified)
        assertTrue("NO_PC2_EVIDENCE_IN_SESSION" in report.fails())
    }

    @Test
    fun aNoPc2PollInsideARealSessionIsVisibleOnly() {
        val s = Session(); addPoll(s, 0)
        s.inventory += chainOf(5)
        s.snapshots += JSONObject().put("poll_ts_utc", utcOf(5)).put("session_date", "2026-09-30")
        val report = audit(s)
        assertTrue(report.findings.joinToString(), report.certified)
        assertEquals(1, report.expectedPc2Polls)
        assertEquals(2, report.snapshotPolls)
    }

    @Test
    fun malformedRowsAnywhereInTheTableBlockCertification() {
        val s = Session(); addPoll(s, 0)
        val report = audit(s, evidence(s, malformed = 1))
        assertFalse(report.certified)
        assertTrue("MALFORMED_TIMESTAMP_ROWS" in report.fails())
        val missingCount = writeExport(s)
        File(missingCount, "manifest.json").writeText(manifestOf(missingCount).apply { remove("malformed_batch_rows") }.toString())
        assertFalse(Pc2ParityAudit.run(missingCount).certified)
    }

    @Test
    fun aReviewedExceptionIsHonouredOnlyForContinuityGaps() {
        val s = Session(); addPoll(s, 0)
        s.inventory += chainOf(10)
        val gap = Instant.parse(chainOf(10)).toString()
        val ok = audit(s, exceptions = listOf(JSONObject().put("category", "MISSING_SNAPSHOT_FOR_POLL")
            .put("poll_ts_utc", chainOf(10)).put("reason", "phone restarted mid-poll").put("reviewed_by", "Codex")))
        assertTrue(ok.findings.joinToString(), ok.certified)
        assertEquals(1, ok.exceptionsApplied)
        assertTrue(ok.findings.any { it.key == gap && it.detail.startsWith("EXCEPTED (Codex)") })

        val integrity = audit(s, exceptions = listOf(JSONObject().put("category", "STORED_DIGEST_MISMATCH")
            .put("poll_ts_utc", chainOf(10)).put("reason", "x").put("reviewed_by", "y")))
        assertFalse("integrity failures can never be excepted", integrity.certified)
        assertTrue("INVALID_EXCEPTION" in integrity.fails())

        val unreviewed = audit(s, exceptions = listOf(JSONObject().put("category", "MISSING_SNAPSHOT_FOR_POLL")
            .put("poll_ts_utc", chainOf(10)).put("reason", "no reviewer")))
        assertFalse(unreviewed.certified)
    }

    // =========================================================================
    // A2 - a reference belongs to its actual snapshot poll and session
    // =========================================================================

    @Test
    fun changingOnlyTheSnapshotSessionDateFails() {
        val s = Session(); addPoll(s, 0)
        s.snapshots[0].put("session_date", "2026-10-01")
        val report = audit(s)
        assertFalse(report.certified)
        assertTrue("SESSION_MISMATCH" in report.fails())
    }

    @Test
    fun auditingTheWrongDayFails() {
        val s = Session(); addPoll(s, 0)
        val report = audit(s, evidence(s, day = "2026-10-01"))
        assertFalse(report.certified)
        assertTrue("SESSION_MISMATCH" in report.fails())
    }

    @Test
    fun aValidReferenceCopiedAcrossTwoGenuinePollsWithIdenticalArraysFails() {
        val s = Session()
        val same = decisions(20, "identical")
        addPoll(s, 0, array = same)
        addPoll(s, 5, array = same)                     // a genuinely distinct poll, same decisions
        s.snapshots[1].put("compact_ref", s.snapshots[0].getJSONObject("compact_ref"))
        val report = audit(s)
        assertFalse(report.certified)
        assertTrue(report.fails().toString(),
            "REFERENCE_POLL_MISMATCH" in report.fails() && "DUPLICATE_REFERENCE" in report.fails())
        assertEquals(1, report.certifiedPolls)
    }

    @Test
    fun aDuplicatedRowOfTheSamePollIsVisibleAndCountedOnce() {
        val s = Session(); addPoll(s, 0)
        s.snapshots += JSONObject(s.snapshots[0].toString())
        val report = audit(s)
        assertTrue(report.findings.joinToString(), report.certified)
        assertEquals(1, report.certifiedPolls)
        assertTrue(report.findings.any { it.category == "DUPLICATE_SNAPSHOT_POLL" })
    }

    @Test
    fun aDefectiveDuplicateSourceCannotHideBehindAValidFirstCopy() {
        val s = Session(); addPoll(s, 0)
        val defective = JSONObject(s.snapshots[0].toString())
        defective.getJSONArray("decisions").getJSONObject(0).put("observed_value", 9.99)
        s.snapshots += defective
        repeat(2) {
            val report = audit(s)
            assertFalse(report.findings.joinToString(), report.certified)
            assertEquals(0, report.certifiedPolls)
            assertTrue("SOURCE_DIGEST_MISMATCH" in report.fails())
            s.snapshots.reverse()
        }
    }

    @Test
    fun aDuplicateWithTheWrongSessionStillFailsIntegrity() {
        val s = Session(); addPoll(s, 0)
        s.snapshots += JSONObject(s.snapshots[0].toString()).put("session_date", "2026-10-01")
        val report = audit(s)
        assertFalse(report.findings.joinToString(), report.certified)
        assertEquals(0, report.certifiedPolls)
        assertTrue("SESSION_MISMATCH" in report.fails())
    }

    @Test
    fun anEquivalentTimestampOnAnIdenticalDuplicateStillCountsOnce() {
        val s = Session(); addPoll(s, 0)
        s.snapshots += JSONObject(s.snapshots[0].toString())
            .put("poll_ts_utc", "2026-09-30T10:00:00+05:30")
        val report = audit(s)
        assertTrue(report.findings.joinToString(), report.certified)
        assertEquals(1, report.certifiedPolls)
        assertTrue("DUPLICATE_REFERENCE" !in report.fails())
    }

    @Test
    fun equivalentUtcAndIstSpellingsOfOneInstantPass() {
        val a = Pc2ParityAudit.parseInstant("2026-09-30T04:30:00.000000Z")
        assertEquals(a, Pc2ParityAudit.parseInstant("2026-09-30T10:00:00+0530"))
        assertEquals(a, Pc2ParityAudit.parseInstant("2026-09-30T10:00:00+05:30"))
        assertEquals(a, Pc2ParityAudit.parseInstant("2026-09-30T04:30:00Z"))
        val s = Session(); addPoll(s, 0)
        s.snapshots[0].put("poll_ts_utc", "2026-09-30T10:00:00+05:30")
        assertTrue(audit(s).findings.joinToString(), audit(s).certified)
    }

    // =========================================================================
    // A3 - malformed timestamps are rejected and surfaced
    // =========================================================================

    @Test
    fun theInstantParserAcceptsOnlyRealInstantsWithAnExplicitOffset() {
        listOf("2026-99-99Tgarbage", "2026-09-30T10:00:00", "2026-02-30T10:00:00+0530",
            "2026-09-30T24:00:00+0530", "2026-09-30T10:00:60+0530", "2026-09-30 10:00:00+0530",
            "2026-09-30T10:00:00+0530x", "", "garbage").forEach {
            assertNull("'$it' must not parse", Pc2ParityAudit.parseInstant(it))
        }
    }

    @Test
    fun theInstantParserUsesTheDatabaseOffsetAndPrecisionLimits() {
        listOf("2026-09-30T10:00:00+1500", "2026-09-30T10:00:00+14:01",
            "2026-09-30T10:00:00-14:01", "2026-09-30T10:00:00.1234567Z",
            "2026-09-30t10:00:00Z", "2026-09-30T10:00:00z",
            "2026-09-30T10:00:00+05:30+0530", "0000-09-30T10:00:00Z").forEach {
            assertNull("'$it' must be rejected just as in PostgreSQL", Pc2ParityAudit.parseInstant(it))
        }
        listOf("2026-09-30T10:00:00+1400", "2026-09-30T10:00:00-14:00",
            "2026-09-30T10:00:00.123456Z").forEach {
            assertNotNull("'$it' is within the shared contract", Pc2ParityAudit.parseInstant(it))
        }
    }

    @Test
    fun aMalformedReferenceTimestampIsSurfaced() {
        val s = Session(); addPoll(s, 0)
        s.snapshots[0].getJSONObject("compact_ref").put("poll_ts_text", "2026-99-99Tgarbage")
        val report = audit(s)
        assertFalse(report.certified)
        assertTrue("MALFORMED_TIMESTAMP" in report.fails())
    }
}
