package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** 2 Nov 2026 ML eval count correction: chosen-primary training label contract. */
class MlEvalTrainingLabelCountContractTest {
    private val day = "2026-11-02"
    private val open = TrainingLabelCountContract.LedgerGate("LABELS_SAVED", true)
    private val researchOnly = TrainingLabelCountContract.LedgerGate("RESEARCH_ONLY", false)
    private val scope = TrainingLabelCountContract.SCOPE_SESSION_ATTRIBUTED

    private fun row(
        role: String, lane: String, snap: Any?, cand: String?, won: Any?,
        session: String = day, index: String? = null, wonKey: String = "canonical_won"
    ): JSONObject {
        val r = JSONObject().put("role", role).put("lane", lane).put("session_date", session)
        r.put("snapshot_id", snap ?: JSONObject.NULL).put("candidate_id", cand ?: JSONObject.NULL)
        r.put(wonKey, won ?: JSONObject.NULL)
        if (index != null) r.put("index_key", index)
        return r
    }

    /** Exact row shape established by read-only Supabase SELECT for 2 Nov 2026. */
    private fun rows1803(): JSONArray {
        val a = JSONArray()
        for (i in 0 until 46) a.put(row("secondary", "NF_intraday", 1000 + i, "nf$i", if (i < 11) 1 else 0, index = "NF"))
        for (i in 0 until 32) a.put(row("secondary", "BNF_intraday", 2000 + i, "bnf$i", if (i < 25) 0 else null, index = "BNF"))
        return a
    }

    private fun build(rows: JSONArray, gate: TrainingLabelCountContract.LedgerGate = open, s: String = scope) =
        TrainingLabelCountContract.build(rows, day, gate, s)

    @Test fun state1803ResearchOnlyGivesZeroTrainingLabelsAndUnknownWinRate() {
        val c = build(rows1803(), researchOnly)
        assertEquals(TrainingLabelCountContract.VERSION, c.getString("contract_version"))
        assertFalse(c.getBoolean("eligible"))
        assertEquals("RESEARCH_ONLY", c.getString("ineligible_reason"))
        assertEquals(0, c.getInt("verified_training_labels"))
        assertTrue(c.isNull("win_rate_pct"))
        assertEquals(78, c.getInt("secondary_research_rows"))
        assertEquals(71, c.getInt("secondary_research_labeled")) // origin of the false 71/500
        assertEquals(0, c.getInt("primary_rows"))
        assertEquals(46, c.getJSONObject("lanes").getJSONObject("NF_intraday").getInt("secondary_research_rows"))
        assertEquals(11, c.getJSONObject("lanes").getJSONObject("NF_intraday").getInt("secondary_research_wins"))
        assertEquals(32, c.getJSONObject("lanes").getJSONObject("BNF_intraday").getInt("secondary_research_rows"))
        assertEquals(0, c.getJSONObject("lanes").getJSONObject("BNF_intraday").getInt("secondary_research_wins"))
        assertFalse(c.has("ready_for_retrain_gate"))
    }

    @Test fun secondaryRowsNeverCountEvenWhenLedgerOpen() {
        val c = build(rows1803(), open)
        assertTrue(c.getBoolean("eligible"))
        assertEquals(0, c.getInt("verified_training_labels"))
        assertTrue(c.isNull("win_rate_pct"))
    }

    @Test fun state1643PendingEvalNoLedgerFailsClosed() {
        // Pre-evaluation: no attributed rows for the session; only recent fallback rows from 30 Oct.
        val a = JSONArray()
        for (i in 0 until 50) a.put(row("primary", "NF_intraday", i, "c$i", 1, session = "2026-10-30"))
        val c = build(a, TrainingLabelCountContract.LedgerGate("", null), TrainingLabelCountContract.SCOPE_RECENT_FALLBACK)
        assertFalse(c.getBoolean("eligible"))
        assertEquals("SOURCE_NOT_SESSION_ATTRIBUTED", c.getString("ineligible_reason"))
        assertEquals(0, c.getInt("verified_training_labels"))
        assertEquals(50, c.getInt("other_session_rows"))
        assertTrue(c.isNull("win_rate_pct"))
    }

    @Test fun ledgerUnavailableOrLabelsNotSavedFailsClosed() {
        val a = JSONArray().put(row("primary", "NF_intraday", 1, "a", 1))
        assertEquals("LEDGER_UNAVAILABLE", build(a, TrainingLabelCountContract.LedgerGate("DONE", null)).getString("ineligible_reason"))
        val c = build(a, TrainingLabelCountContract.LedgerGate("DONE", false))
        assertEquals("LABELS_NOT_SAVED", c.getString("ineligible_reason"))
        assertEquals(0, c.getInt("verified_training_labels"))
        assertEquals(1, c.getInt("primary_labeled_unverified"))
    }

    @Test fun onePrimaryVerifiedLabel() {
        val c = build(JSONArray().put(row("primary", "BNF_swing", 5, "x", 1, index = "BNF")))
        assertEquals(1, c.getInt("verified_training_labels"))
        assertEquals(1, c.getInt("verified_training_wins"))
        assertEquals(100.0, c.getDouble("win_rate_pct"), 1e-9)
        assertEquals(1, c.getJSONObject("index_totals").getJSONObject("BNF").getInt("verified_training_labels"))
        assertEquals(0, c.getJSONObject("index_totals").getJSONObject("NF").getInt("verified_training_labels"))
    }

    @Test fun mixedPrimarySecondaryCountsOnlyPrimary() {
        val a = JSONArray()
            .put(row("primary", "NF_intraday", 1, "p", 0))
            .put(row("secondary", "NF_intraday", 1, "s1", 1))
            .put(row("secondary", "NF_intraday", 1, "s2", 1))
        val c = build(a)
        assertEquals(1, c.getInt("verified_training_labels"))
        assertEquals(0, c.getInt("verified_training_wins"))
        assertEquals(0.0, c.getDouble("win_rate_pct"), 1e-9)
        assertEquals(2, c.getInt("secondary_research_rows"))
    }

    @Test fun duplicatesOtherSessionRejectedAndMissingAttributionExcluded() {
        val a = JSONArray()
            .put(row("primary", "NF_intraday", 1, "p", 1))
            .put(row("primary", "NF_intraday", 1, "p", 1)) // duplicate identity
            .put(row("primary", "NF_intraday", 2, "q", 1, session = "2026-10-30"))
            .put(row("rejected", "NF_intraday", 3, "r", 1))
            .put(row("primary", "NF_intraday", null, "m", 1)) // missing snapshot
            .put(row("primary", "", 4, "n", 1)) // missing lane
            .put(row("", "NF_intraday", 5, "o", 1)) // missing role
        val c = build(a)
        assertEquals(1, c.getInt("verified_training_labels"))
        assertEquals(1, c.getInt("duplicate_rows"))
        assertEquals(1, c.getInt("other_session_rows"))
        assertEquals(1, c.getInt("rejected_research_rows"))
        assertEquals(3, c.getInt("attribution_pending_rows"))
    }

    @Test fun unknownCanonicalWonAndOutcomeH2AreNotLosses() {
        val a = JSONArray()
            .put(row("primary", "NF_intraday", 1, "a", null))
            .put(row("primary", "NF_intraday", 2, "b", "maybe"))
            .put(row("primary", "NF_intraday", 3, "c", 2, wonKey = "outcome_h2"))
            .put(row("primary", "NF_intraday", 4, "d", 1, wonKey = "outcome_h2"))
        val c = build(a)
        assertEquals(1, c.getInt("verified_training_labels"))
        assertEquals(3, c.getInt("primary_outcome_unknown"))
        assertEquals(100.0, c.getDouble("win_rate_pct"), 1e-9)
    }

    @Test fun nfAndBnfNeverCrossCount() {
        val a = JSONArray()
            .put(row("primary", "NF_intraday", 1, "a", 1, index = "NF"))
            .put(row("primary", "BNF_intraday", 2, "b", 0, index = "BNF"))
            .put(row("primary", "NF_intraday", 3, "c", 1, index = "BNF")) // mismatch
            .put(row("primary", "NF_swing", 4, "d", 0, index = "NIFTY"))
        val c = build(a)
        val nf = c.getJSONObject("index_totals").getJSONObject("NF")
        val bnf = c.getJSONObject("index_totals").getJSONObject("BNF")
        assertEquals(2, nf.getInt("verified_training_labels"))
        assertEquals(1, nf.getInt("verified_training_wins"))
        assertEquals(1, bnf.getInt("verified_training_labels"))
        assertEquals(0, bnf.getInt("verified_training_wins"))
        assertEquals(1, c.getInt("cross_index_mismatch_rows"))
        assertEquals(1, c.getJSONObject("lanes").getJSONObject("NF_swing").getInt("verified_training_labels"))
        assertEquals(1, c.getJSONObject("lanes").getJSONObject("NF_intraday").getInt("verified_training_labels"))
    }
}
