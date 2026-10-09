package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class EvidenceBatch1PaperMenuCaptureTest {
    private val day = "2026-10-09"

    @Test
    fun menuIdMatchesPythonPaperMenuEvidence() {
        // python: paper_menu_evidence.menu_id_for('2026-10-09')
        assertEquals(PY_MENU_ID_20261009, PaperMenuCapture.menuIdFor(day))
        assertEquals("12:30", PaperMenuCapture.slotScheduledHhmm(40))
        assertEquals("12:55", PaperMenuCapture.slotScheduledHhmm(45))
    }

    @Test
    fun firstCompletedSlotInWindowIsFrozenAndLaterSlotsDoNotSubstitute() {
        val s = PaperMenuCapture.newState(day)
        assertEquals(PaperMenuCapture.Action.WAIT_FOR_WINDOW, PaperMenuCapture.decide(s, 39))
        assertEquals(PaperMenuCapture.Action.NOTHING, PaperMenuCapture.decide(s, null))   // manual poll
        assertEquals(PaperMenuCapture.Action.ATTEMPT_CAPTURE, PaperMenuCapture.decide(s, 40))
        // 12:30 attempt fails (brain timeout): logged, 12:35 retries
        PaperMenuCapture.recordAttempt(s, "$day|40", "FAILED", "brain_timeout", "t1")
        assertEquals(PaperMenuCapture.Action.ATTEMPT_CAPTURE, PaperMenuCapture.decide(s, 41))
        val req = PaperMenuCapture.captureRequest(s, "$day|41", 41, "t2")
        assertEquals(2, req.getInt("attempt_no"))
        assertEquals("2026-10-09T12:35:00+05:30", req.getString("slot_scheduled_ist"))
        val menu = JSONObject().put("manifest", JSONObject().put("menu_id", s.getString("menu_id")).put("population_status", "MENU_BUILT"))
        assertNull(PaperMenuCapture.usableMenu(s, menu))
        PaperMenuCapture.recordAttempt(s, "$day|41", "CAPTURED", null, "t2")
        PaperMenuCapture.markCaptured(s, "abc", "/f", "t2")
        assertEquals(PaperMenuCapture.Action.ALREADY_CAPTURED, PaperMenuCapture.decide(s, 42))
        assertEquals(PaperMenuCapture.Action.ALREADY_CAPTURED, PaperMenuCapture.decide(s, 70))
        assertEquals(2, s.getJSONArray("attempts").length())
        assertEquals("FAILED", s.getJSONArray("attempts").getJSONObject(0).getString("outcome"))
    }

    @Test
    fun windowWithoutUsableMenuBecomesMissedNotEmpty() {
        val s = PaperMenuCapture.newState(day)
        for (o in 40..45) PaperMenuCapture.recordAttempt(s, "$day|$o", "FAILED", "menu_not_returned_by_brain", "t$o")
        assertEquals(PaperMenuCapture.Action.RECORD_MISSED, PaperMenuCapture.decide(s, 46))
        val m = PaperMenuCapture.missedManifest(s, "no_usable_menu_in_window", "t46")
        assertEquals("MISSED_SESSION", m.getString("capture_status"))
        assertTrue(m.isNull("population_status"))
        assertEquals(0, m.getInt("expected_candidate_rows"))
        assertEquals(6, m.getJSONArray("attempts").length())
        assertEquals(PaperMenuCapture.Action.NOTHING, PaperMenuCapture.decide(s, 47))
        // an EMPTY generated menu is a usable capture, not a miss
        val s2 = PaperMenuCapture.newState(day)
        val empty = JSONObject().put("manifest", JSONObject().put("menu_id", s2.getString("menu_id")).put("population_status", "EMPTY_MENU"))
        assertNull(PaperMenuCapture.usableMenu(s2, empty))
        val unavailable = JSONObject().put("manifest", JSONObject().put("menu_id", s2.getString("menu_id")).put("population_status", "POPULATION_UNAVAILABLE"))
        assertEquals("population_population_unavailable", PaperMenuCapture.usableMenu(s2, unavailable))
        assertEquals("menu_id_mismatch", PaperMenuCapture.usableMenu(s2,
            JSONObject().put("manifest", JSONObject().put("menu_id", "pdm_other").put("population_status", "MENU_BUILT"))))
    }

    @Test
    fun stateNeverCarriesAcrossSessions() {
        val s = PaperMenuCapture.newState("2026-10-08")
        PaperMenuCapture.markCaptured(s, "x", "/f", "t")
        val today = PaperMenuCapture.stateFor(s.toString(), day)
        assertEquals("OPEN", today.getString("status"))
        assertEquals(PaperMenuCapture.menuIdFor(day), today.getString("menu_id"))
        assertEquals("CAPTURED", PaperMenuCapture.stateFor(s.toString(), "2026-10-08").getString("status"))
        assertEquals("OPEN", PaperMenuCapture.stateFor("{not json", day).getString("status"))
    }

    companion object {
        const val PY_MENU_ID_20261009 = "pdm_1e41b0da3cb95d88568ab210"
    }

    @Test
    fun rolloverRecordsPreviousOpenSessionAsMissed() {
        val prev = PaperMenuCapture.newState("2026-10-08")
        PaperMenuCapture.recordAttempt(prev, "2026-10-08|40", "FAILED", "brain_analyze_timeout", "t")
        val (today, missed) = PaperMenuCapture.rollover(prev.toString(), "2026-10-09", "2026-10-09T09:15:00+0530")
        assertEquals("2026-10-09", today.getString("session_date"))
        assertEquals("OPEN", today.getString("status"))
        assertNotNull(missed)
        assertEquals("MISSED", missed!!.getString("status"))
        val mm = missed.getJSONObject("missed_manifest")
        assertEquals("MISSED_SESSION", mm.getString("capture_status"))
        assertEquals(1, mm.getJSONArray("attempts").length())
        assertEquals(PaperMenuCapture.menuIdFor("2026-10-08"), mm.getString("menu_id"))
        // A captured previous session is not re-labelled missed.
        val cap = PaperMenuCapture.markCaptured(PaperMenuCapture.newState("2026-10-08"), "a".repeat(64), "/x", "t")
        assertNull(PaperMenuCapture.rollover(cap.toString(), "2026-10-09", "t").second)
        // Same session: state is kept.
        val (same, none) = PaperMenuCapture.rollover(today.toString(), "2026-10-09", "t")
        assertEquals(today.getString("menu_id"), same.getString("menu_id"))
        assertNull(none)
    }

    @Test
    fun rowMappingKeepsTypedColumnsAndNeverInventsMargin() {
        val manifest = JSONObject()
            .put("menu_id", PaperMenuCapture.menuIdFor("2026-10-09")).put("session_date", "2026-10-09")
            .put("capture_policy_version", PaperMenuCapture.POLICY_VERSION).put("population_status", "MENU_BUILT")
            .put("generated_count", 2).put("captured_count", 2).put("duplicate_count", 0)
            .put("expected_candidate_rows", 2).put("candidate_ids_sha256", "b".repeat(64))
            .put("control", JSONObject().put("status", "REGISTERED").put("peer_candidate_id", "C2").put("inclusion_probability", 0.5))
        val row1 = JSONObject().put("candidate_id", "C1").put("generated_rank", 1).put("selected_primary", true)
            .put("control_role", "PRIMARY").put("legs", JSONArray().put(JSONObject().put("leg_index", 0)))
            .put("margin_evidence", JSONObject().put("status", "QUOTED_OK")).put("gate_reasons", JSONArray())
        val row2 = JSONObject().put("candidate_id", "C3").put("generated_rank", 3).put("selected_primary", false)
            .put("control_role", JSONObject.NULL).put("legs", JSONArray()).put("margin_evidence", JSONObject.NULL)
        val frozen = JSONObject().put("manifest", manifest).put("candidates", JSONArray().put(row1).put(row2))
            .put("attempts", JSONArray().put(JSONObject().put("outcome", "CAPTURED")))
        val m = PaperMenuCapture.manifestRow(frozen, "c".repeat(64))
        assertEquals("CAPTURED", m.getString("capture_status"))
        assertEquals(2, m.getInt("expected_candidate_rows"))
        assertEquals("C2", m.getString("control_peer_candidate_id"))
        assertEquals(0.5, m.getDouble("control_inclusion_probability"), 0.0)
        assertEquals(1, m.getJSONArray("attempts").length())
        // R4: rows are stored as exact canonical bytes, ordered by generated_rank, digest declared in the manifest.
        val canon = PaperMenuCapture.canonicalCandidates(frozen)
        assertEquals(listOf(1, 3), canon.map { it.first })
        assertEquals(Pc2CompactBatch.canonicalJson(row1), canon[0].second)
        assertTrue(canon[0].second.contains("\"margin_evidence\":{\"status\":\"QUOTED_OK\"}"))
        assertTrue(canon[1].second.contains("\"margin_evidence\":null"))
        assertEquals(PaperMenuCapture.contentDigest(canon.map { it.second }), m.getString("content_sha256"))
        assertEquals("pc2_compact_contract_v4_pinned_canonical", m.getString("content_canonicalization"))
        val missed = PaperMenuCapture.missedManifest(PaperMenuCapture.newState("2026-10-09"), "r", "t")
        val mr = PaperMenuCapture.missedRow(missed, "d".repeat(64))
        assertEquals("MISSED_SESSION", mr.getString("capture_status"))
        assertTrue(mr.isNull("population_status"))
        assertEquals(0, mr.getInt("expected_candidate_rows"))
        assertTrue(mr.isNull("generated_count") && mr.isNull("captured_count") && mr.isNull("duplicate_count"))
        assertEquals(PaperMenuCapture.sha256Hex(""), mr.getString("content_sha256"))
    }
}
