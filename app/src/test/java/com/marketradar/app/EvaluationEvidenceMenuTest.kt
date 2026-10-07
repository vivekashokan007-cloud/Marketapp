package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class EvaluationEvidenceMenuTest {
    private fun candidate() = JSONObject().put("id", "sample-only").put("index", "NF")
        .put("expiry", "2026-10-13").put("sellStrike", 25000).put("sellType", "CE")
        .put("buyStrike", 25200).put("buyType", "CE").put("final_rank", 205)

    @Test fun sampledOnlyExpiryIsRequestedWithBothLegsAndDeduplicated() {
        val c = candidate()
        val context = JSONObject().put("snapshot_ranked_candidates_full", JSONArray())
            .put("snapshot_ranked_below_cap_sample", JSONArray().put(c).put(c))
        val keys = EvaluationEvidenceMenu.legKeys(JSONObject().put("context_json", context.toString()))
        assertEquals(setOf(25000.0, 25200.0), keys.map { it.strike }.toSet())
        assertTrue(keys.all { it.index == "NF" && it.expiry == "2026-10-13" })
        assertEquals(2, keys.size)
    }

    @Test fun emptyRankedMenuFallsBackAndLegArraysAreSupported() {
        val c = JSONObject().put("index", "BNF").put("expiry", "2026-10-27")
            .put("legs", JSONArray().put(JSONObject().put("strike", 56000).put("option_type", "PE")))
        val context = JSONObject().put("snapshot_ranked_candidates_full", JSONArray())
            .put("snapshot_generated_candidates", JSONArray().put(c))
        val keys = EvaluationEvidenceMenu.legKeys(JSONObject().put("context_json", context))
        assertEquals(setOf(EvaluationEvidenceMenu.LegKey("BNF", "2026-10-27", 56000.0, "PE")), keys)
    }

    @Test fun unknownIndexNeverDefaultsToBankNifty() {
        val c = candidate().put("index", "UNKNOWN")
        assertTrue(EvaluationEvidenceMenu.legKeys(JSONObject().put("primary_candidate_json", c)).isEmpty())
    }

    @Test fun reportRetainsSampleAndItsOriginalProvenance() {
        val metadata = JSONObject().put("sampling_selected_count", 1).put("sampling_rule_version", "fixture")
        val source = JSONObject().put("snapshot_ranked_below_cap_sample", JSONArray().put(candidate()))
            .put("snapshot_ranked_below_cap_sampling", metadata)
            .put("snapshot_capture_completeness", "bounded")
        val target = JSONObject()
        EvaluationEvidenceMenu.copyCoverageEvidence(source, target) { raw, _ -> raw as JSONArray }
        assertEquals(205, target.getJSONArray("snapshot_ranked_below_cap_sample").getJSONObject(0).getInt("final_rank"))
        assertEquals(metadata.toString(), target.getJSONObject("snapshot_ranked_below_cap_sampling").toString())
        assertEquals("bounded", target.getString("snapshot_capture_completeness"))
    }
}
