package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.BeforeClass
import org.junit.Test

/**
 * G1 / Codex R3: proves the FINAL persistence boundary. Teacher outcomes from the real chain
 * (python projection -> Kotlin compaction / local cache / upload sanitiser -> evaluation teacher) are passed through
 * the REAL SupabaseClient.buildEvaluationRows / buildRecommendationRows (the exact server payload builders). The
 * resulting rows carry no lineage columns; every row must be traceable, via (snapshot_id, session_date,
 * candidate_id) only, to the uploaded snapshot payload, from which OutcomeSnapshotProvenance recovers the exact
 * policy / construction / decision metadata the teacher used.
 */
class G1OutcomeSnapshotJoinTest {
    companion object {
        /** scenario -> (teacher outcome, uploaded snapshot as the evaluator reads it back) */
        val cases = mutableListOf<Triple<String, JSONObject, JSONObject>>()
        val snapshots = mutableMapOf<String, JSONObject>()

        @BeforeClass @JvmStatic fun runChain() {
            G1AuthorityProvenanceChainTest.runChain()
            for (name in G1AuthorityProvenanceChainTest.index.keys()) {
                // The server row the evaluator reads back = uploaded payload + its ml_brain_snapshots id
                // (fetchEvaluationSnapshots selects id,poll_ts,primary_candidate_json,context_json,top_candidates_json,
                // is_labelable,session_date). The chain stamps the id before upload, so it is already present.
                val up = JSONObject(G1AuthorityProvenanceChainTest.uploaded.getValue(name).toString())
                val snap = JSONObject()
                for (k in listOf("id", "poll_ts", "primary_candidate_json", "context_json", "top_candidates_json", "is_labelable", "session_date"))
                    if (up.has(k)) snap.put(k, up.get(k))
                snapshots[name] = snap
                val arr = G1AuthorityProvenanceChainTest.teacher.getValue(name).getJSONObject("batch").getJSONArray("outcomes")
                for (i in 0 until arr.length()) {
                    val o = arr.getJSONObject(i)
                    if (o.optString("role").lowercase() in setOf("primary", "secondary")) cases += Triple(name, o, snap)
                }
            }
        }

        fun build(method: String, body: JSONArray, sessionDate: String? = null): JSONArray {
            val m = if (sessionDate == null) SupabaseClient::class.java.getDeclaredMethod(method, JSONArray::class.java)
            else SupabaseClient::class.java.getDeclaredMethod(method, String::class.java, JSONArray::class.java)
            m.isAccessible = true
            return (if (sessionDate == null) m.invoke(SupabaseClient, body) else m.invoke(SupabaseClient, sessionDate, body)) as JSONArray
        }

        /** Fields the teacher emits that the final server rows intentionally do NOT carry (recovered by the join). */
        val LOCAL_ONLY = EvaluationLocalCache.AUTHORITY_LINEAGE_KEYS.toList() +
            listOf("policy_selector_version", "evaluation_coverage", "h2_entry_basis_source")
    }

    private fun finalRows(): List<Pair<String, List<Pair<JSONObject, JSONObject>>>> {
        val out = mutableListOf<Pair<String, List<Pair<JSONObject, JSONObject>>>>()
        val byScenario = cases.groupBy { it.first }
        for (method in listOf("buildEvaluationRows", "buildRecommendationRows")) {
            val pairs = mutableListOf<Pair<JSONObject, JSONObject>>()
            for ((_, list) in byScenario) {
                val snap = list.first().third
                val body = JSONArray(list.map { JSONObject(it.second.toString()) })
                val rows = if (method == "buildEvaluationRows") build(method, body)
                else build(method, body, snap.getString("session_date"))
                assertEquals("$method dropped rows", list.size, rows.length())
                for (i in 0 until rows.length()) pairs += rows.getJSONObject(i) to snap
            }
            out += method to pairs
        }
        return out
    }

    @Test fun finalRowsCarryOnlyTheJoinIdentityNotLineage() {
        assertTrue("chain produced too few teacher rows: ${cases.size}", cases.size >= 8)
        for ((method, pairs) in finalRows()) for ((row, _) in pairs) {
            for (k in listOf("snapshot_id", "session_date", "candidate_id")) assertTrue("$method missing $k", row.optString(k).isNotBlank())
            for (k in LOCAL_ONLY) assertFalse("$method unexpectedly carries $k (would need a server column)", row.has(k))
        }
    }

    @Test fun everyFinalRowResolvesToItsUploadedSnapshotAndRecoversTeacherLineage() {
        val teacherById = cases.associate { (n, o, _) -> "$n|${o.getString("candidate_id")}|${o.optString("role")}" to o }
        var authority = 0
        for ((method, pairs) in finalRows()) for ((row, snap) in pairs) {
            val res = OutcomeSnapshotProvenance.resolve(row, snap)
            assertEquals("$method ${row.optString("candidate_id")}: ${res.optString("detail")}", "RESOLVED", res.getString("status"))
            val name = snapshots.entries.single { it.value === snap }.key
            val o = teacherById.getValue("$name|${row.getString("candidate_id")}|${row.getString("role")}")
            val lin = res.getJSONObject("candidate_lineage")
            for (k in EvaluationLocalCache.AUTHORITY_LINEAGE_KEYS) if (o.has(k) && !o.isNull(k))
                assertEquals("$method $k for ${row.getString("candidate_id")}", o.get(k).toString(), lin.opt(k)?.toString())
            assertEquals(row.getString("index_key"), lin.getString("index"))
            assertEquals(row.getString("strategy_type"), lin.getString("type"))
            if (lin.optString("constructionVersion") == "index_construction_v1") {
                authority++
                val ia = res.getJSONObject("index_authority")
                assertEquals("mr.snapshot_primary.index_direction_v1", ia.getString("record_schema"))
                assertTrue(ia.getBoolean("decision_id_matches_candidate"))
                assertEquals(o.getString("indexDecisionId"), ia.getString("decision_id"))
                assertTrue(ia.getBoolean("candidate_in_authority_menu"))
                val mp = res.getJSONObject("managed_policy")
                assertEquals(o.getString("policy_selector_version"), mp.getString("policy_id"))
                assertEquals(
                    G1AuthorityProvenanceChainTest.index.getJSONObject(name).getJSONObject("managed")
                        .getJSONObject(row.getString("index_key")).optString("reason"),
                    mp.optString("reason")
                )
                assertTrue("gate for candidate missing", mp.has("candidate_gate"))
            }
            assertEquals(row.getString("role") == "primary", res.getBoolean("is_snapshot_primary"))
        }
        assertTrue("no authority rows traced: $authority", authority >= 16) // >= 8 per builder
    }

    @Test fun joinFailsClosedOnWrongSnapshotSessionOrCandidate() {
        val (_, pairs) = finalRows().first()
        val (row, snap) = pairs.first { it.first.optString("role") == "secondary" }
        val other = snapshots.values.first { it.getString("id") != snap.getString("id") }
        assertEquals("SNAPSHOT_MISMATCH", OutcomeSnapshotProvenance.resolve(row, other).getString("status"))
        assertEquals("SESSION_MISMATCH", OutcomeSnapshotProvenance.resolve(JSONObject(row.toString()).put("session_date", "2026-10-30"), snap).getString("status"))
        assertEquals("CANDIDATE_NOT_FOUND", OutcomeSnapshotProvenance.resolve(JSONObject(row.toString()).put("candidate_id", "BULL_PUT_NF_X_index_construction_v1"), snap).getString("status"))
        assertEquals("ROW_SNAPSHOT_DISAGREE", OutcomeSnapshotProvenance.resolve(JSONObject(row.toString()).put("role", "primary"), snap).getString("status"))
        val flipped = if (row.getString("index_key") == "NF") "BNF" else "NF"
        assertEquals("ROW_SNAPSHOT_DISAGREE", OutcomeSnapshotProvenance.resolve(JSONObject(row.toString()).put("index_key", flipped), snap).getString("status"))
    }
}
