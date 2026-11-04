package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.BeforeClass
import org.junit.Test

/**
 * G2: index_construction_v1 debit structures (BULL_CALL / BEAR_PUT) keep finite teacher P&L through the REAL
 * chain (python projection -> Kotlin compaction/local cache/upload sanitiser -> evaluation job teacher) AND the
 * uploader's evaluation + recommendation row builders, which strip teacher P&L whenever price_integrity == FAIL.
 * Claude's probe_debit_teacher_integrity.py was not available; this is the equivalent replay. Credit, IC and IB
 * rows must be graded and untouched.
 */
class G2DebitTeacherUploaderTest {
    companion object {
        val outcomes = mutableListOf<JSONObject>()
        lateinit var candidates: Map<String, JSONObject>

        @BeforeClass @JvmStatic fun runChain() {
            G1AuthorityProvenanceChainTest.runChain()
            for (name in listOf("bear_pair", "pair_both_admissible")) {
                val arr = G1AuthorityProvenanceChainTest.teacher.getValue(name).getJSONObject("batch").getJSONArray("outcomes")
                for (i in 0 until arr.length()) {
                    val o = arr.getJSONObject(i)
                    if (o.optString("candidate_id").endsWith("_index_construction_v1")) outcomes.add(o)
                }
            }
            val m = mutableMapOf<String, JSONObject>()
            for (name in listOf("bear_pair", "pair_both_admissible")) {
                val gen = G1AuthorityProvenanceChainTest.uploaded.getValue(name).getJSONObject("context_json")
                    .getJSONArray("snapshot_generated_candidates")
                for (i in 0 until gen.length()) gen.getJSONObject(i).let { m[it.getString("id")] = it }
            }
            candidates = m
        }

        private fun invoke(name: String, vararg args: Any): JSONArray {
            val types = args.map { if (it is String) String::class.java else JSONArray::class.java }.toTypedArray()
            val method = SupabaseClient::class.java.getDeclaredMethod(name, *types)
            method.isAccessible = true
            return method.invoke(SupabaseClient, *args) as JSONArray
        }
    }

    private val debit = setOf("BULL_CALL", "BEAR_PUT")

    @Test fun teacherGradesDebitWithPositiveCostAndSignedPremiumSurvives() {
        val d = outcomes.filter { it.getString("strategy_type") in debit }
        assertEquals(3, d.size)        // NF BULL_CALL, NF + BNF BEAR_PUT
        for (o in d) {
            val c = candidates.getValue(o.getString("candidate_id"))
            assertTrue(c.getDouble("netPremium") < 0)                   // signed premium still in the upload payload
            assertFalse(c.getBoolean("isCredit"))
            assertEquals("OK", o.getString("price_integrity"))
            assertEquals(Math.abs(c.getDouble("netPremium")), o.getDouble("h2_entry_basis_points"), 1e-4)
            assertEquals("index_construction_v1_debit_cost_abs_signed_net_premium", o.getString("h2_entry_basis_source"))
            assertTrue(o.getDouble("sim_pnl_h2").isFinite())
        }
    }

    @Test fun uploaderKeepsDebitTeacherPnlOnEvaluationAndRecommendationRows() {
        val body = JSONArray(outcomes.map { JSONObject(it.toString()) })
        val evalRows = invoke("buildEvaluationRows", body)
        val recRows = invoke("buildRecommendationRows", "2026-11-02", JSONArray(outcomes.map { JSONObject(it.toString()) }))
        for (rows in listOf(evalRows, recRows)) {
            assertEquals(outcomes.size, rows.length())
            for (i in 0 until rows.length()) {
                val r = rows.getJSONObject(i)
                assertEquals("${r.optString("candidate_id")}", "OK", r.optString("price_integrity"))
                for (k in listOf("managed_pnl", "r_multiple", "sim_pnl_h2")) {
                    assertTrue("$k stripped on ${r.optString("strategy_type")}", r.has(k) && !r.isNull(k))
                    assertTrue(r.getDouble(k).isFinite())
                }
            }
            val types = (0 until rows.length()).map { rows.getJSONObject(it).getString("strategy_type") }.toSet()
            assertTrue(types.containsAll(listOf("BULL_CALL", "BEAR_PUT", "BULL_PUT", "BEAR_CALL", "IRON_CONDOR", "IRON_BUTTERFLY")))
        }
    }

    @Test fun creditIcIbUnchanged() {
        val credit = outcomes.filter { it.getString("strategy_type") !in debit }
        assertEquals(5, credit.size)  // bear: NF + BNF BEAR_CALL; bull: NF BULL_PUT + BNF IC + IB
        for (o in credit) {
            val c = candidates.getValue(o.getString("candidate_id"))
            assertEquals("OK", o.getString("price_integrity"))
            assertFalse(o.has("h2_entry_basis_source"))
            assertEquals(c.getDouble("netPremium"), o.getDouble("h2_entry_basis_points"), 1e-4)
        }
    }

    /** Codex R3: h2_entry_basis_source is absent from the final server rows but recoverable via the G1 join. */
    @Test fun finalRowsOmitBasisSourceButJoinRecoversItExactly() {
        val snaps = listOf("bear_pair", "pair_both_admissible").associateWith { G1AuthorityProvenanceChainTest.uploaded.getValue(it) }
        for ((name, snap) in snaps) {
            val teacherRows = G1AuthorityProvenanceChainTest.teacher.getValue(name).getJSONObject("batch").getJSONArray("outcomes")
            val auth = (0 until teacherRows.length()).map { teacherRows.getJSONObject(it) }
                .filter { it.optString("candidate_id").endsWith("_index_construction_v1") }
            val built = listOf(
                invoke("buildEvaluationRows", JSONArray(auth.map { JSONObject(it.toString()) })),
                invoke("buildRecommendationRows", snap.getString("session_date"), JSONArray(auth.map { JSONObject(it.toString()) }))
            )
            for (rows in built) {
                assertEquals(auth.size, rows.length())
                for (i in 0 until rows.length()) {
                    val r = rows.getJSONObject(i)
                    assertFalse(r.has("h2_entry_basis_source"))
                    val res = OutcomeSnapshotProvenance.resolve(r, snap)
                    assertEquals(res.optString("detail"), "RESOLVED", res.getString("status"))
                    val teacherRow = auth.single { it.getString("candidate_id") == r.getString("candidate_id") }
                    val expected = teacherRow.optString("h2_entry_basis_source").ifBlank { null }
                    val recovered = res.optJSONObject("derived")?.optString("h2_entry_basis_source")
                    assertEquals(r.getString("candidate_id"), expected, recovered)
                }
            }
        }
    }
}
