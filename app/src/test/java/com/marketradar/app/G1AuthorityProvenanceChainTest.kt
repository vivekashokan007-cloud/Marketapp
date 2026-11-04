package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.BeforeClass
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.util.concurrent.TimeUnit

/**
 * G1: index-authority provenance through the REAL release path, in one run:
 *   stage 1 (python3 tools/g1_authority_chain/chain.py produce): brain.analyze (index_direction_v1)
 *     -> brain.take_poll_snapshot(android_compact_v1) = the JSON CaptureEngine receives;
 *   stage 2 (this JVM test): the CaptureEngine re-parse hop -> EvaluationLocalCache.compactBrainSnapshotForPersistence
 *     -> appendFullSnapshotRow (local cache file) -> read back -> BrainSnapshotPayloadSanitizer (upload payload);
 *   stage 3 (chain.py teach): uploaded snapshots -> evaluation_job_prepare / evaluation_job_run_batch (teacher).
 * No in-memory candidate shortcut: the teacher only ever sees what survived compaction and upload sanitising.
 */
class G1AuthorityProvenanceChainTest {
    companion object {
        lateinit var root: File
        lateinit var out: File
        lateinit var index: JSONObject
        val uploaded = mutableMapOf<String, JSONObject>()
        val teacher = mutableMapOf<String, JSONObject>()

        private fun repoRoot(): File =
            listOf(File(".."), File(".")).map { it.canonicalFile }
                .first { File(it, "tools/g1_authority_chain/chain.py").isFile }

        private fun python(vararg args: String) {
            val p = ProcessBuilder(listOf("python3", "tools/g1_authority_chain/chain.py") + args)
                .directory(root).redirectErrorStream(true).start()
            val text = p.inputStream.bufferedReader().readText()
            assertTrue("python timed out", p.waitFor(600, TimeUnit.SECONDS))
            assertEquals("python ${args.toList()} failed:\n" + text.takeLast(4000), 0, p.exitValue())
        }

        /** CaptureEngine.build: JSONB string fields re-parsed before compaction (same as B510 hop). */
        fun captureHop(raw: JSONObject): JSONObject {
            for (field in listOf("verdict_json", "market_forces_json", "context_json", "primary_candidate_json")) {
                val v = raw.opt(field)
                if (v is String && v.isNotBlank()) raw.put(field, JSONObject(v))
            }
            val top = raw.opt("top_candidates_json")
            if (top is String && top.isNotBlank()) raw.put("top_candidates_json", JSONArray(top))
            return EvaluationLocalCache.compactBrainSnapshotForPersistence(raw)
        }

        @BeforeClass @JvmStatic fun runChain() {
            root = repoRoot()
            out = Files.createTempDirectory("g1chain").toFile()
            python("produce", out.absolutePath)
            index = JSONObject(File(out, "index.json").readText())
            val cache = File(out, "cache"); cache.mkdirs()
            for (name in index.keys()) {
                val raw = JSONObject(File(out, "$name.python.json").readText()).getJSONObject("snapshot")
                val compact = captureHop(raw)
                val session = compact.optString("session_date")
                val file = File(cache, "brain_snapshots_$name.jsonl")
                assertTrue(EvaluationLocalCache.appendFullSnapshotRow(file, File(cache, "$name.trim"), session, compact) { _, _ -> })
                val cached = JSONObject(file.readLines().last { it.isNotBlank() })
                val payload = JSONObject(BrainSnapshotPayloadSanitizer.sanitize(cached).toString())
                uploaded[name] = payload
                File(out, "$name.uploaded.json").writeText(payload.toString())
            }
            python("teach", out.absolutePath)
            for (name in index.keys()) teacher[name] = JSONObject(File(out, "$name.teacher.json").readText())
        }
    }

    private fun ctx(name: String) = uploaded.getValue(name).getJSONObject("context_json")
    private fun record(name: String) = ctx(name).getJSONObject("snapshot_index_authority_primary")
    private fun managed(name: String, ix: String) =
        ctx(name).getJSONObject("snapshot_managed_policy").getJSONObject("by_index").getJSONObject(ix)
    private fun outcomes(name: String): List<JSONObject> {
        val batch = teacher.getValue(name).getJSONObject("batch")
        assertTrue(batch.getBoolean("ok"))
        assertEquals("fatal snapshot error in $name: ${batch.optJSONArray("errors")}", 0, batch.getInt("fatal_snapshot_error_count"))
        val arr = batch.getJSONArray("outcomes")
        return (0 until arr.length()).map { arr.getJSONObject(it) }
    }
    private fun decisionId(name: String, ix: String) = index.getJSONObject(name).getJSONObject("decision_ids").getString(ix)

    @Test fun recordAndManagedSummarySurviveEveryScenario() {
        for (name in index.keys()) {
            val rec = record(name)
            assertEquals("mr.snapshot_primary.index_direction_v1", rec.getString("record_schema"))
            assertTrue(rec.getString("coverage_note").contains("not chosen-trade labels"))
            for (ix in listOf("NF", "BNF")) {
                assertEquals(decisionId(name, ix), rec.getJSONObject("by_index").getJSONObject(ix).getString("decision_id"))
                val m = managed(name, ix)
                assertEquals("index_managed_v1", m.getString("policy_id"))
                assertEquals(index.getJSONObject(name).getJSONObject("managed").getJSONObject(ix).optString("reason"),
                    m.optString("reason"))
            }
        }
    }

    @Test fun candidateLineageAndLegFreshnessSurviveCompaction() {
        val gen = ctx("pair_both_admissible").getJSONArray("snapshot_generated_candidates")
        assertEquals(4, gen.length())
        for (i in 0 until gen.length()) {
            val c = gen.getJSONObject(i)
            assertEquals("index_direction_v1", c.getString("authorityPolicyVersion"))
            assertEquals("index_managed_v1", c.getString("policySelectorVersion"))
            assertEquals("index_construction_v1", c.getString("constructionVersion"))
            assertEquals("FIXED_PRESENTATION_ORDER_NOT_A_RANK", c.getString("authorityOrder"))
            assertEquals(decisionId("pair_both_admissible", c.getString("index")), c.getString("indexDecisionId"))
            val legs = c.getJSONArray("legs")
            for (j in 0 until legs.length()) {
                val leg = legs.getJSONObject(j)
                for (k in listOf("exchange_ts", "bid_qty", "ask_qty", "bid", "ask")) assertTrue("leg $k", leg.has(k))
            }
        }
    }

    @Test fun teacherGradesSnapshotSourcedAuthorityCandidatesAsIndexManaged() {
        var n = 0
        for (name in index.keys()) {
            for (o in outcomes(name)) {
                if (o.optString("constructionVersion") != "index_construction_v1") continue
                n++
                assertEquals("$name ${o.optString("candidate_id")}", "index_managed_v1", o.getString("policy_selector_version"))
                assertEquals("index_direction_v1", o.getString("authorityPolicyVersion"))
                assertEquals(decisionId(name, o.getString("index_key")), o.getString("indexDecisionId"))
                assertTrue(o.getString("evaluation_coverage").startsWith("HYPOTHETICAL_MULTI_LEG_TEACHER_V1"))
            }
            // no authority construction may fall back to the legacy selector (legacy rejected research rows,
            // still produced by the retired generator as evidence, legitimately keep pc2_paper_primary_v7)
            for (o in outcomes(name)) {
                if (o.optString("candidate_id").endsWith("_index_construction_v1"))
                    assertEquals(o.optString("candidate_id"), "index_managed_v1", o.optString("policy_selector_version"))
            }
        }
        assertTrue("teacher graded no authority candidate at all", n >= 8)
    }

    @Test fun bothAdmissiblePairHasNoPrimary() {
        for (ix in listOf("NF", "BNF")) {
            assertEquals("STRUCTURE_CHOICE_RULE_UNSPECIFIED", managed("pair_both_admissible", ix).getString("reason"))
            assertEquals(2, managed("pair_both_admissible", ix).getInt("admissible_count"))
        }
        assertNull(record("pair_both_admissible").opt("primary_policy").takeIf { it != JSONObject.NULL })
        assertTrue(outcomes("pair_both_admissible").none { it.optString("role") == "primary" })
    }

    @Test fun managedCapBlocksBothAndIsRecorded() {
        for (ix in listOf("NF", "BNF")) {
            val m = managed("managed_cap_both_blocked", ix)
            assertEquals("NO_ADMISSIBLE_CANDIDATE", m.getString("reason"))
            assertEquals(1, m.getInt("exposure_count"))
            val gates = m.getJSONArray("gates")
            assertEquals(2, gates.length())
            for (i in 0 until gates.length()) {
                assertFalse(gates.getJSONObject(i).getBoolean("admit"))
                assertTrue(gates.getJSONObject(i).getJSONArray("reasons").toString().contains("MANAGED_POLICY_CAP_ONE_PER_INDEX"))
            }
        }
        assertTrue(outcomes("managed_cap_both_blocked").none { it.optString("role") == "primary" })
    }

    @Test fun exactlyOneAdmittedStaleLegBecomesIndexManagedPrimary() {
        val name = "exactly_one_admitted_stale_leg"
        val m = managed(name, "NF")
        assertEquals(1, m.getInt("admissible_count"))
        val stale = index.getJSONObject(name).getJSONObject("stale_candidate").getString("NF")
        val gates = m.getJSONArray("gates")
        val staleGate = (0 until gates.length()).map { gates.getJSONObject(it) }.single { it.getString("candidate_id") == stale }
        assertEquals("STALE", staleGate.getString("freshness_status"))
        assertFalse(staleGate.getBoolean("admit"))
        val selected = m.getString("selected_candidate_id")
        val rec = record(name)
        assertEquals("index_managed_v1", rec.getString("primary_policy"))
        val prim = uploaded.getValue(name).getJSONObject("primary_candidate_json")
        assertEquals(selected, prim.getString("id"))
        assertEquals("index_managed_v1", prim.getString("policySelectorVersion"))
        assertEquals(decisionId(name, "NF"), prim.getString("indexDecisionId"))
        val p = outcomes(name).single { it.optString("role") == "primary" }
        assertEquals(selected, p.getString("candidate_id"))
        assertEquals("index_managed_v1", p.getString("policy_selector_version"))
        assertEquals(decisionId(name, "NF"), p.getString("indexDecisionId"))
        // selector excluded BNF from new admission; the reason survives
        assertTrue(managed(name, "BNF").getJSONArray("gates").toString().contains("INDEX_NOT_SELECTED"))
    }

    @Test fun twoIndicesSelectingIsMultipleWithoutPrimary() {
        val name = "two_indices_selecting"
        val rec = record(name)
        assertEquals("MULTIPLE_MANAGED_SELECTIONS", rec.getString("primary_reason"))
        assertEquals(2, rec.getJSONArray("selected_candidate_ids").length())
        for (ix in listOf("NF", "BNF")) assertNotNull(managed(name, ix).optString("selected_candidate_id").ifBlank { null })
        assertTrue(outcomes(name).none { it.optString("role") == "primary" })
    }

    @Test fun nfExpiryDayAndPost1515AreRecordedNotLost() {
        val e = record("nf_expiry_day").getJSONObject("by_index")
        assertEquals(0, e.getJSONObject("NF").getJSONArray("candidate_ids").length())
        assertEquals(2, e.getJSONObject("BNF").getJSONArray("candidate_ids").length())
        val p = record("post_1515").getJSONObject("by_index")
        for (ix in listOf("NF", "BNF")) {
            assertEquals("NO_NEW_ENTRY", p.getJSONObject(ix).getString("action"))
            assertEquals(0, managed("post_1515", ix).getInt("gate_count"))
        }
        assertTrue(outcomes("post_1515").isEmpty())
    }

    @Test fun authoritySummarySurvivesDocumentedBudgetTrimming() {
        val raw = JSONObject(File(out, "pair_both_admissible.python.json").readText()).getJSONObject("snapshot")
        val ctxRaw = raw.opt("context_json").let { if (it is String) JSONObject(it) else it as JSONObject }
        val pad = "x".repeat(3 * 1024 * 1024)
        ctxRaw.put("snapshot_phase3_expected_r_shadow", JSONObject().put("pad", pad))
        ctxRaw.put("snapshot_pc2_authority_decisions", JSONArray().put(JSONObject().put("pad", "y".repeat(256 * 1024))))
        raw.put("context_json", ctxRaw)
        val compact = captureHop(raw)
        val bytes = compact.toString().toByteArray(Charsets.UTF_8).size
        assertTrue("budget not enforced: $bytes", bytes <= 2 * 1024 * 1024)
        val c = compact.getJSONObject("context_json")
        assertFalse(c.has("snapshot_phase3_expected_r_shadow"))
        for (k in EvaluationLocalCache.IA_SNAPSHOT_CONTEXT_KEYS) assertTrue("trimmed $k", c.has(k))
        assertEquals(record("pair_both_admissible").toString(), c.getJSONObject("snapshot_index_authority_primary").toString())
    }
}
