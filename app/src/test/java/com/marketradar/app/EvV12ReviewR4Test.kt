package com.marketradar.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files
import java.time.OffsetDateTime

/** Codex r4 D1: the b320910f native probe inverted (atomic manual request, patchless legacy intent, no throw). */
class EvV12ReviewR4Test {
    private val patch = "{\"status\":\"CLOSED\",\"exit_reason\":\"Manual\"}"
    private val iso = "2026-10-12T15:25:00+05:30"
    private val ms = OffsetDateTime.parse(iso).toInstant().toEpochMilli().toDouble()

    private fun pos(state: String?) = RuleBExitPolicy.Position(RuleBExitPolicy.POLICY_ID, state,
        "2026-10-12", "2026-10-13", listOf(RuleBExitPolicy.Leg(-1, "A")))

    @Test
    fun manualRequestRecordsIntentAndPatchAtomically() {
        val dir = Files.createTempDirectory("d1a").toFile()
        assertEquals(RuleBFillLedger.MANUAL_PENDING, RuleBFillLedger.manualCloseRequest(dir, "9001", "d", patch, 1))
        val e = RuleBFillLedger.entry(dir, "9001")!!
        assertEquals("MANUAL_CLOSED", e.getString("state"))
        assertTrue(e.has("manual_patch"))
        assertEquals(1, RuleBFillLedger.pending(dir).length())
        assertEquals("ALREADY_JOURNALED", RuleBFillLedger.manualCloseRequest(dir, "9001", "d", patch, 2))
        assertEquals("JOURNALED_PATCH_DIFFERS",
            RuleBFillLedger.manualCloseRequest(dir, "9001", "d", "{\"status\":\"CLOSED\",\"x\":1}", 3))
        assertEquals("JOURNALED_NOT_ABANDONED", RuleBFillLedger.abandonUnjournaledManual(dir, "9001", 4))
    }

    @Test
    fun refusedRequestChangesNothing() {
        val dir = Files.createTempDirectory("d1b").toFile()
        assertEquals("REFUSED_OVERSIZE", RuleBFillLedger.manualCloseRequest(dir, "9001", "d",
            "{\"status\":\"CLOSED\",\"p\":\"" + "x".repeat(200_000) + "\"}", 1))
        assertEquals("REFUSED_INVALID_JSON", RuleBFillLedger.manualCloseRequest(dir, "9001", "d", "{not json", 1))
        assertEquals("REFUSED_INVALID_JSON", RuleBFillLedger.manualCloseRequest(dir, "9001", "d", "{\"status\":\"OPEN\"}", 1))
        assertNull(RuleBFillLedger.entry(dir, "9001"))
        assertEquals(0, RuleBFillLedger.pending(dir).length())
        assertNull(RuleBFillLedger.stateOf(dir, "9001"))
    }

    @Test
    fun probeLegacyPatchlessIntentIsOfferedAndWithdrawable() {
        val dir = Files.createTempDirectory("d1c").toFile()
        assertEquals("MANUAL_PENDING", RuleBFillLedger.noteManualClose(dir, "9001", "ev12:NF:x", 1))
        // WebView interruption here (the legacy second call never happened): the intent is NOT hidden
        assertEquals(1, RuleBFillLedger.pending(dir).length())
        assertEquals("WITHDRAWN", RuleBFillLedger.abandonUnjournaledManual(dir, "9001", 2))
        assertEquals("OPEN", RuleBFillLedger.stateOf(dir, "9001"))
        assertEquals(0, RuleBFillLedger.pending(dir).length())
        assertEquals("NOTHING_TO_ABANDON", RuleBFillLedger.abandonUnjournaledManual(dir, "9001", 3))
        assertEquals("UNKNOWN_TRADE", RuleBFillLedger.abandonUnjournaledManual(dir, "nope", 3))
    }

    @Test
    fun legacyPatchlessManualCanBeJournaledByTheNewRequest() {
        val dir = Files.createTempDirectory("d1d").toFile()
        RuleBFillLedger.noteManualClose(dir, "9001", "d", 1)
        assertEquals(RuleBFillLedger.MANUAL_PENDING, RuleBFillLedger.manualCloseRequest(dir, "9001", "d", patch, 2))
        assertTrue(RuleBFillLedger.entry(dir, "9001")!!.has("manual_patch"))
    }

    @Test
    fun probeStepDoesNotThrowOnManualClosedWithValidOrInvalidQuotes() {
        val dir = Files.createTempDirectory("d1e").toFile()
        RuleBFillLedger.manualCloseRequest(dir, "9001", "d", patch, 1)
        val st = RuleBFillLedger.stateOf(dir, "9001")
        assertEquals("MANUAL_CLOSED", st)
        val valid = RuleBExitPolicy.Tick(iso, mapOf("A" to RuleBExitPolicy.LegQuote(10.0, 10.5, ms)))
        val crossed = RuleBExitPolicy.Tick(iso, mapOf("A" to RuleBExitPolicy.LegQuote(11.0, 10.5, ms)))
        val missing = RuleBExitPolicy.Tick(iso, emptyMap())
        for (t in listOf(valid, crossed, missing)) {
            val s = RuleBExitPolicy.step(pos(st), t)
            assertEquals("NONE", s.action)
            assertEquals("MANUAL_CLOSED", s.reason)
            assertNull(s.fill)
        }
        val u = RuleBExitPolicy.step(pos("SOMETHING_ELSE"), valid)
        assertEquals("STATE_UNKNOWN_FAIL_CLOSED", u.reason)
        assertFalse(u.action == "FILL")
        // a withdrawn intent returns the position to Rule B
        RuleBFillLedger.noteManualClose(dir, "9002", "d", 1)
        RuleBFillLedger.abandonUnjournaledManual(dir, "9002", 2)
        assertEquals("FILL", RuleBExitPolicy.step(pos(RuleBFillLedger.stateOf(dir, "9002")), valid).action)
    }
}
