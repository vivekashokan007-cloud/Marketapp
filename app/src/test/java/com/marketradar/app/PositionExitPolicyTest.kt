package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.charset.StandardCharsets

/**
 * G4 Kotlin monitor conformance against the shared fixture file.
 * Expected values match docs/contracts/fixtures/position_exit_policy_v1.json
 * (copied to test resources).
 */
class PositionExitPolicyTest {

    private val fixture: JSONObject by lazy {
        val stream = javaClass.classLoader!!.getResourceAsStream(
            "contracts/position_exit_policy_v1.json"
        ) ?: error("missing contracts/position_exit_policy_v1.json")
        val text = stream.bufferedReader(StandardCharsets.UTF_8).use { it.readText() }
        JSONObject(text)
    }

    private fun case(name: String): JSONObject {
        val cases = fixture.getJSONArray("cases")
        for (i in 0 until cases.length()) {
            val row = cases.getJSONObject(i)
            if (row.getString("name") == name) return row
        }
        error("missing fixture $name")
    }

    private fun eval(name: String) = PositionExitPolicy.evaluateCase(case(name), role = "monitor")

    @Test
    fun contractVersionsAlignWithLegacyReference() {
        assertEquals("position_exit_policy_v1_net_20260912", PositionExitPolicy.CONTRACT_VERSION)
        assertEquals(0.50, PositionExitPolicy.TP_MULT, 0.0)
        assertEquals(0.60, PositionExitPolicy.SL_MULT, 0.0)
        assertEquals("15:15", PositionExitPolicy.POLICY_EXIT_INTENT_HH_MM)
        assertEquals("15:40", PositionExitPolicy.NATIVE_MARKET_CLOSE_HH_MM)
        assertEquals("15:30", PositionExitPolicy.PYTHON_READINESS_CLOSE_HH_MM)
        assertEquals(PositionPolicyV1.VERSION, PositionExitPolicy.LEGACY_POSITION_POLICY_VERSION)
        assertEquals(PositionPolicyV1.TP_MULT, PositionExitPolicy.TP_MULT, 0.0)
        assertEquals(PositionPolicyV1.SL_MULT, PositionExitPolicy.SL_MULT, 0.0)
    }

    @Test
    fun teacherAndMonitorAgreeOnEveryFixture() {
        val cases = fixture.getJSONArray("cases")
        for (i in 0 until cases.length()) {
            val row = cases.getJSONObject(i)
            val teacher = PositionExitPolicy.evaluateCase(row, role = "teacher")
            val monitor = PositionExitPolicy.evaluateCase(row, role = "monitor")
            assertTrue(row.getString("name"), PositionExitPolicy.agree(teacher, monitor))
            assertEquals("COUNTERFACTUAL_TEACHER", teacher.evidenceGrade)
            assertEquals("OBSERVED_MONITOR", monitor.evidenceGrade)
        }
    }

    @Test
    fun positiveEodIsNetWinWithoutTp() {
        val r = eval("positive_eod_non_tp")
        assertTrue(r.entryValid)
        assertEquals("EOD", r.exitReason)
        assertEquals(120.0, r.netPnl!!, 0.001)
        assertEquals("WIN", r.learningResultNet)
        assertEquals(true, r.learningWonNet)
        assertFalse(r.tpHit)
    }

    @Test
    fun zeroNetIsExplicitFlat() {
        val r = eval("zero_net")
        assertEquals("FLAT", r.learningResultNet)
        assertEquals(0.0, r.netPnl!!, 0.001)
        assertEquals(true, r.learningFlatNet)
        assertEquals(false, r.learningWonNet)
    }

    @Test
    fun missingQuotesDoNotInventFill() {
        val r = eval("missing_quotes")
        assertEquals("MISSING_QUOTES", r.exitReason)
        assertNull(r.netPnl)
        assertNull(r.exitTs)
        assertEquals("UNAVAILABLE", r.learningResultNet)
    }

    @Test
    fun lateQuotesIgnored() {
        val r = eval("late_quotes")
        assertEquals("EOD", r.exitReason)
        assertEquals("2026-09-10T13:00:00+05:30", r.exitTs)
        assertEquals(80.0, r.netPnl!!, 0.001)
    }

    @Test
    fun gapThroughStopNotClipped() {
        val r = eval("gap_through_stop")
        assertEquals("SL", r.exitReason)
        assertEquals(-2580.0, r.netPnl!!, 0.001)
        assertTrue(r.netPnl!! < r.slThreshold!!)
    }

    @Test
    fun lotSizesAreTotalCurrency() {
        val r = eval("lot_sizes")
        assertEquals(240.0, r.netPnl!!, 0.001)
        assertEquals("WIN", r.learningResultNet)
    }

    @Test
    fun incompleteLegsRejectEntry() {
        val r = eval("incomplete_legs")
        assertFalse(r.entryValid)
        assertEquals("NO_ENTRY", r.exitReason)
        assertTrue(r.entryReasons.contains("incomplete_legs"))
    }

    @Test
    fun publishedThresholdsFireEarlier() {
        val r = eval("published_thresholds")
        assertEquals("TP", r.exitReason)
        assertTrue(r.tpThreshold!! < 460.0)
        assertEquals(220.0, r.netPnl!!, 0.001)
    }

    @Test
    fun noEntryAfterEodIntent() {
        val r = eval("no_entry_after_eod")
        assertFalse(r.entryValid)
        assertTrue(r.entryReasons.contains("no_entry_after_eod_intent"))
    }

    @Test
    fun overnightIsExplicitNotSameDayEod() {
        val r = eval("overnight_explicit")
        assertEquals("OVERNIGHT_HOLD", r.exitReason)
        assertEquals(90.0, r.netPnl!!, 0.001)
    }

    @Test
    fun sameMarkStopBeatsTarget() {
        val r = eval("sl_before_tp_same_mark")
        assertEquals("SL", r.exitReason)
        assertEquals("SL_BEFORE_TP", r.precedenceApplied)
        assertEquals(220.0, r.netPnl!!, 0.001)
    }

    @Test
    fun fiveMinuteReplayNotCertified() {
        val r = eval("five_minute_approximate")
        assertEquals("FIVE_MINUTE_APPROXIMATE", r.replayResolution)
        assertFalse(r.replayCertifiedTickEquivalent)
    }

    @Test
    fun perUnitQuantityRejected() {
        val r = eval("per_unit_quantity_rejected")
        assertFalse(r.entryValid)
        assertTrue(r.entryReasons.contains("quantity_unit_not_total_currency"))
    }
}
