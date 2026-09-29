package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class Pc2CompactBatchTest {
    @Test
    fun exactDedupReconstructsAllOrderedDecisions() {
        val snapshot = snapshot(decisionCount = 128, distinctCount = 39)
        val built = requireNotNull(Pc2CompactBatch.build(snapshot))

        assertEquals(128, built.batchRow.getInt("decision_count"))
        assertEquals(39, built.batchRow.getInt("distinct_decision_count"))
        val reconstructed = Pc2CompactBatch.reconstructOrderedDecisions(built.batchRow)
        val original = snapshot.getJSONObject("context_json")
            .getJSONArray("snapshot_pc2_authority_decisions")
        assertEquals(Pc2CompactBatch.digestOrderedDecisions(original), Pc2CompactBatch.digestOrderedDecisions(reconstructed))
        assertEquals(built.batchRow.getString("decision_digest"), Pc2CompactBatch.digestOrderedDecisions(reconstructed))
        for (index in 0 until original.length()) {
            assertEquals(
                Pc2CompactBatch.canonicalJson(original.getJSONObject(index)),
                Pc2CompactBatch.canonicalJson(reconstructed.getJSONObject(index))
            )
        }
    }

    @Test
    fun hashesAreStableAcrossObjectKeyOrderAndSensitiveToRealChanges() {
        val first = JSONObject().put("z", 2).put("a", JSONObject().put("y", 1).put("x", true))
        val reordered = JSONObject().put("a", JSONObject().put("x", true).put("y", 1.0)).put("z", 2.0)
        assertEquals(Pc2CompactBatch.canonicalJson(first), Pc2CompactBatch.canonicalJson(reordered))
        assertEquals(Pc2CompactBatch.sha256(Pc2CompactBatch.canonicalJson(first)), Pc2CompactBatch.sha256(Pc2CompactBatch.canonicalJson(reordered)))

        val baseline = requireNotNull(Pc2CompactBatch.build(snapshot(8, 3)))
        val policyChangedSnapshot = snapshot(8, 3).also {
            it.getJSONObject("context_json").getJSONObject("snapshot_pc2_authority_policy").put("minimum_support", 999)
        }
        val decisionChangedSnapshot = snapshot(8, 3).also {
            it.getJSONObject("context_json").getJSONArray("snapshot_pc2_authority_decisions")
                .getJSONObject(7).put("observed_value", 999)
        }
        assertNotEquals(baseline.policyRow.getString("policy_hash"), requireNotNull(Pc2CompactBatch.build(policyChangedSnapshot)).policyRow.getString("policy_hash"))
        assertNotEquals(baseline.batchRow.getString("decision_digest"), requireNotNull(Pc2CompactBatch.build(decisionChangedSnapshot)).batchRow.getString("decision_digest"))
    }

    @Test
    fun compactEnvelopeIsMateriallySmallerThanLegacyExpandedRows() {
        val snapshot = snapshot(128, 39)
        val context = snapshot.getJSONObject("context_json")
        val decisions = context.getJSONArray("snapshot_pc2_authority_decisions")
        val policy = context.getJSONObject("snapshot_pc2_authority_policy")
        val legacy = JSONArray()
        for (index in 0 until decisions.length()) {
            legacy.put(JSONObject().put("decision_index", index).put("policy_json", policy).put("decision_json", decisions.getJSONObject(index)))
        }
        val compact = requireNotNull(Pc2CompactBatch.build(snapshot)).envelope()
        val legacyBytes = legacy.toString().toByteArray().size
        val compactBytes = compact.toString().toByteArray().size
        assertTrue("expected at least 50% savings, legacy=$legacyBytes compact=$compactBytes", compactBytes * 2 < legacyBytes)
    }

    private fun snapshot(decisionCount: Int, distinctCount: Int): JSONObject {
        val policy = JSONObject()
            .put("version", "pc2_policy_test")
            .put("authority_diagnostics_version", "pc2_diag_test")
            .put("minimum_support", 30)
            .put("long_constant_registry", "x".repeat(700))
        val decisions = JSONArray()
        for (index in 0 until decisionCount) {
            val prototype = index % distinctCount
            decisions.put(
                JSONObject()
                    .put("variable_name", "variable_${prototype % 7}")
                    .put("constant", "constant_${prototype % 4}")
                    .put("slice_key", "slice_$prototype")
                    .put("authority_kind", if (prototype % 2 == 0) "parameter_threshold" else "ranking_context")
                    .put("authority_state", "SHADOW")
                    .put("authority_diagnostics_version", "pc2_diag_test")
                    .put("support_count", 100 + prototype)
                    .put("observed_value", prototype / 10.0)
                    .put("input_contract_reasons", JSONArray().put("fixture_$prototype"))
            )
        }
        return JSONObject()
            .put("session_date", "2026-09-29")
            .put("poll_ts", "2026-09-29T07:35:14Z")
            .put(
                "context_json",
                JSONObject()
                    .put("snapshot_brain_version", "test")
                    .put("snapshot_pc2_authority_policy", policy)
                    .put("snapshot_pc2_authority_decisions", decisions)
            )
    }
}
