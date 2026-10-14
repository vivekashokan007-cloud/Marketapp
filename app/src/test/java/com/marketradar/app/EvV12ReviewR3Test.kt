package com.marketradar.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files

/** Codex R2 re-review C2: manual and Rule B writers share one arbitration in the native ledger. */
class EvV12ReviewR3Test {
    private fun fillStep() = RuleBExitPolicy.Step("EXIT_FILLED", "FILL", "PLANNED_EXIT",
        RuleBExitPolicy.Fill("PLANNED_EXIT", "2026-10-12T15:16:00+05:30",
            listOf(RuleBExitPolicy.FillLeg("A", -1, 10.5, 10.0, 10.5, 1.0)), -10.5))

    @Test
    fun manualCloseIsPendingUntilRemoteProof() {
        val dir = Files.createTempDirectory("c2a").toFile()
        assertEquals(RuleBFillLedger.MANUAL_PENDING, RuleBFillLedger.noteManualClose(dir, "t1", "d1", 1))
        assertEquals(RuleBFillLedger.MANUAL_PENDING, RuleBFillLedger.entry(dir, "t1")!!.getString("delivery"))
        assertEquals("JOURNALED", RuleBFillLedger.manualJournal(dir, "t1", "{\"status\":\"CLOSED\"}", 2))
        assertEquals("ALREADY_JOURNALED", RuleBFillLedger.manualJournal(dir, "t1", "{\"status\":\"CLOSED\"}", 3))
        assertEquals("JOURNALED_PATCH_DIFFERS", RuleBFillLedger.manualJournal(dir, "t1", "{\"status\":\"X\"}", 3))
        assertEquals(1, RuleBFillLedger.pending(dir).length())   // retried after a restart / remote failure
        assertNull(RuleBFillLedger.manualResult(dir, "t1", RuleBFillLedger.RULE_B_WON, 4))   // no fill: refused
        assertEquals(RuleBFillLedger.REMOTE_CONFIRMED, RuleBFillLedger.manualResult(dir, "t1", RuleBFillLedger.MANUAL_WON, 5))
        assertEquals(0, RuleBFillLedger.pending(dir).length())
    }

    @Test
    fun competingWritersOneWinner() {
        val dir = Files.createTempDirectory("c2b").toFile()
        RuleBFillLedger.record(dir, "t2", "d2", fillStep(), 1)
        assertEquals("RULE_B_TERMINAL_PENDING_DELIVERY", RuleBFillLedger.noteManualClose(dir, "t2", "d2", 2))
        assertTrue(RuleBFillLedger.entry(dir, "t2")!!.getBoolean("manual_pending"))
        assertEquals("JOURNALED", RuleBFillLedger.manualJournal(dir, "t2", "{\"w\":\"m\"}", 3))
        // Rule B landed first: the manual writer stops, the fill stays undelivered until its own readback ACK
        assertEquals(RuleBFillLedger.PENDING_APPLY, RuleBFillLedger.manualResult(dir, "t2", RuleBFillLedger.RULE_B_WON, 4))
        assertFalse(RuleBFillLedger.entry(dir, "t2")!!.has("manual_pending"))
        RuleBFillLedger.journal(dir, "t2", "{\"w\":\"rb\"}", 5)
        assertTrue(RuleBFillLedger.ack(dir, "t2", RuleBFillLedger.REMOTE_CONFIRMED, 6))
        // a later manual close of a confirmed Rule B close must not write
        assertEquals("RULE_B_ALREADY_CONFIRMED", RuleBFillLedger.noteManualClose(dir, "t2", "d2", 7))
        assertNull(RuleBFillLedger.manualResult(dir, "t2", RuleBFillLedger.MANUAL_WON, 8))

        RuleBFillLedger.record(dir, "t3", "d3", fillStep(), 1)
        RuleBFillLedger.noteManualClose(dir, "t3", "d3", 2)
        assertEquals(RuleBFillLedger.MANUAL_CLOSE_CONFLICT, RuleBFillLedger.manualResult(dir, "t3", RuleBFillLedger.MANUAL_WON, 3))
        assertTrue(RuleBFillLedger.entry(dir, "t3")!!.has("fill"))   // superseded fill kept as evidence
        assertEquals(0, RuleBFillLedger.pending(dir).length())
    }
}
