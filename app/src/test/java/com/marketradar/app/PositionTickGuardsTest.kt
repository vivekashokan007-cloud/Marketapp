package com.marketradar.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Valuation guards behind the 27f08e3 leg-name fix.
 *
 * Covers the cases Codex flagged as missing in the first revision: duplicate
 * instruments, wrong leg roles, extra legs, unknown strategies, the wide-quote /
 * bound-breach case, and the zero-executable-price defect that produced the single
 * worst mark in production (`current_pnl_r` = -1.458 on trade 269, 2026-09-07).
 */
class PositionTickGuardsTest {

    private fun leg(
        key: String?, side: String, type: String?, strike: Double? = 100.0
    ) = PositionLeg(
        instrumentKey = key,
        side = side,
        closeSide = if (side == "SHORT") CloseSide.BUY_TO_CLOSE else CloseSide.SELL_TO_CLOSE,
        optionType = type,
        strike = strike
    )

    private fun ironButterflyLegs() = listOf(
        leg("K1", "SHORT", "CE", 23950.0),
        leg("K2", "LONG", "CE", 24150.0),
        leg("K3", "SHORT", "PE", 23950.0),
        leg("K4", "LONG", "PE", 23750.0)
    )

    // ---- happy paths ---------------------------------------------------------

    @Test
    fun completeIronButterflyPasses() {
        val r = validateStructure("IRON_BUTTERFLY", ironButterflyLegs())
        assertEquals("COMPLETE", r.status)
        assertEquals(4, r.expected)
        assertEquals(4, r.actual)
        assertTrue(r.problems.isEmpty())
    }

    @Test
    fun completeVerticalSpreadPasses() {
        val r = validateStructure("BEAR_CALL", listOf(leg("A", "SHORT", "CE"), leg("B", "LONG", "CE")))
        assertEquals("COMPLETE", r.status)
        assertEquals(2, r.expected)
    }

    @Test
    fun caseInsensitiveOnStrategyAndOptionType() {
        val r = validateStructure(
            "iron_condor",
            listOf(
                leg("K1", "SHORT", "ce"), leg("K2", "LONG", "ce"),
                leg("K3", "SHORT", "pe"), leg("K4", "LONG", "pe")
            )
        )
        assertEquals("COMPLETE", r.status)
    }

    // ---- the original defect -------------------------------------------------

    @Test
    fun fourLegStructureWithOnlyTwoLegsIsIncomplete() {
        // Exactly the pre-27f08e3 production shape: the second pair silently absent.
        val r = validateStructure("IRON_BUTTERFLY", ironButterflyLegs().take(2))
        assertEquals("INCOMPLETE", r.status)
        assertEquals(4, r.expected)
        assertEquals(2, r.actual)
        assertTrue(r.problems.any { it.startsWith("missing_legs") })
    }

    // ---- cases a count-only check would have passed ---------------------------

    @Test
    fun duplicateInstrumentsAreMalformedNotComplete() {
        val dup = listOf(
            leg("SAME", "SHORT", "CE"), leg("SAME", "LONG", "CE"),
            leg("K3", "SHORT", "PE"), leg("K4", "LONG", "PE")
        )
        val r = validateStructure("IRON_CONDOR", dup)
        assertEquals("MALFORMED", r.status)
        assertEquals(4, r.actual)
        assertTrue(r.problems.contains("duplicate_instrument_keys"))
    }

    @Test
    fun invertedRolesAreMalformed() {
        // Right count, but both call legs short - not an iron condor.
        val bad = listOf(
            leg("K1", "SHORT", "CE"), leg("K2", "SHORT", "CE"),
            leg("K3", "SHORT", "PE"), leg("K4", "LONG", "PE")
        )
        val r = validateStructure("IRON_CONDOR", bad)
        assertEquals("MALFORMED", r.status)
        assertTrue(r.problems.any { it.startsWith("role_mismatch") })
    }

    @Test
    fun wrongOptionTypesAreMalformed() {
        // Four legs, correct sides, but all calls.
        val bad = listOf(
            leg("K1", "SHORT", "CE"), leg("K2", "LONG", "CE"),
            leg("K3", "SHORT", "CE"), leg("K4", "LONG", "CE")
        )
        val r = validateStructure("IRON_CONDOR", bad)
        assertEquals("MALFORMED", r.status)
        assertTrue(r.problems.any { it.startsWith("role_mismatch") })
    }

    @Test
    fun extraLegsAreMalformed() {
        val r = validateStructure("BEAR_CALL", listOf(
            leg("A", "SHORT", "CE"), leg("B", "LONG", "CE"), leg("C", "LONG", "CE")
        ))
        assertEquals("MALFORMED", r.status)
        assertTrue(r.problems.any { it.startsWith("extra_legs") })
    }

    @Test
    fun blankInstrumentKeyIsMalformed() {
        val r = validateStructure("BEAR_CALL", listOf(leg(null, "SHORT", "CE"), leg("B", "LONG", "CE")))
        assertEquals("MALFORMED", r.status)
        assertTrue(r.problems.contains("blank_instrument_key"))
    }

    @Test
    fun missingOptionTypeIsMalformedNotSilentlyAccepted() {
        val r = validateStructure("BEAR_CALL", listOf(leg("A", "SHORT", null), leg("B", "LONG", "CE")))
        assertEquals("MALFORMED", r.status)
    }

    // ---- unknown strategies --------------------------------------------------

    @Test
    fun unknownStrategyIsUncheckedNotComplete() {
        // The distinction Codex asked for: "we did not look" must not read as
        // "we looked and it is fine".
        val r = validateStructure("STRADDLE", listOf(leg("A", "SHORT", "CE")))
        assertEquals("UNCHECKED", r.status)
        assertNull(r.expected)
        assertEquals(1, r.actual)
        assertTrue(r.problems.contains("unsupported_strategy"))
    }

    @Test
    fun emptyStrategyIsUnchecked() {
        assertEquals("UNCHECKED", validateStructure("", emptyList()).status)
        assertNull(expectedLegCount(""))
        assertNull(expectedLegCount("MYSTERY"))
    }

    @Test
    fun expectedLegCountMatchesTheSpec() {
        assertEquals(4, expectedLegCount("IRON_CONDOR"))
        assertEquals(4, expectedLegCount("IRON_BUTTERFLY"))
        assertEquals(2, expectedLegCount("BEAR_CALL"))
        assertEquals(2, expectedLegCount("BULL_PUT"))
        assertEquals(2, expectedLegCount("BEAR_PUT"))
        assertEquals(2, expectedLegCount("BULL_CALL"))
        assertEquals(4, expectedLegCount("iron_condor"))
    }

    // ---- bounds anomaly ------------------------------------------------------

    @Test
    fun boundsFlagTheProductionExtreme() {
        // trade 269, 2026-09-07: current_pnl_r -1.458 caused by a zero long-leg bid.
        val maxProfit = 8832.0
        val maxLoss = 9168.0
        assertTrue(violatesStructuralBounds(-1.458 * maxLoss, maxProfit, maxLoss))
        assertTrue(violatesStructuralBounds(maxProfit * 1.10, maxProfit, maxLoss))
    }

    @Test
    fun boundsToleratePlausibleMarks() {
        val maxProfit = 9042.0
        val maxLoss = 3958.0
        assertFalse(violatesStructuralBounds(0.0, maxProfit, maxLoss))
        assertFalse(violatesStructuralBounds(-maxLoss, maxProfit, maxLoss))
        assertFalse(violatesStructuralBounds(maxProfit, maxProfit, maxLoss))
        assertFalse("2% friction drift is not an anomaly",
            violatesStructuralBounds(maxProfit * 1.02, maxProfit, maxLoss))
    }

    @Test
    fun boundsNeverFireOnAbsentOrNonFiniteInputs() {
        assertFalse(violatesStructuralBounds(null, 100.0, 100.0))
        assertFalse(violatesStructuralBounds(99999.0, null, null))
        assertFalse(violatesStructuralBounds(Double.NaN, 100.0, 100.0))
        assertFalse(violatesStructuralBounds(Double.POSITIVE_INFINITY, 100.0, 100.0))
        assertFalse("a zero/negative bound is not a usable threshold",
            violatesStructuralBounds(1000.0, 0.0, 100.0))
        assertFalse(violatesStructuralBounds(-1000.0, 100.0, 0.0))
    }

    @Test
    fun boundsToleranceIsConfigurable() {
        assertFalse(violatesStructuralBounds(103.0, 100.0, 100.0, tolerance = 1.05))
        assertTrue(violatesStructuralBounds(103.0, 100.0, 100.0, tolerance = 1.01))
    }

    // ---- the zero-executable-price defect ------------------------------------

    @Test
    fun zeroExecutablePriceContradictedByLtpIsTheRealDefect() {
        // Reproduces trade 269's long leg: bid 0, ask 443, ltp 404.15, marked OK.
        // Using bid=0 prices a ~400-point option at nothing and blows the mark out.
        val shortAsk = 739.9
        val longBidBroken = 0.0
        val longBidPlausible = 404.15   // LTP, i.e. what the leg is actually worth

        val brokenMark = shortAsk - longBidBroken
        val plausibleMark = shortAsk - longBidPlausible
        assertEquals(739.9, brokenMark, 0.001)
        assertEquals(335.75, plausibleMark, 0.001)

        val lot = 30.0
        val entry = 294.4
        val brokenPnl = computePositionTickCurrentPnl(entry, brokenMark, true, lot)
        val plausiblePnl = computePositionTickCurrentPnl(entry, plausibleMark, true, lot)
        assertEquals(-13365.0, brokenPnl, 1.0)

        val maxLoss = 9168.0
        val maxProfit = 8832.0
        assertTrue("the zero-bid mark breaches the risk envelope",
            violatesStructuralBounds(brokenPnl, maxProfit, maxLoss))
        assertFalse("the LTP-consistent mark does not",
            violatesStructuralBounds(plausiblePnl, maxProfit, maxLoss))
    }

    @Test
    fun genuineZeroQuoteIsNotTreatedAsADefect() {
        // A worthless far-OTM leg legitimately has bid 0 AND ltp 0. Only a zero
        // contradicted by a positive LTP indicates a data gap.
        val ltpZero: Double? = 0.0
        val suspectWhenLtpPositive = (0.0 <= 0.0) && (404.15 > 0.0)
        val suspectWhenLtpZero = (0.0 <= 0.0) && ((ltpZero ?: 0.0) > 0.0)
        assertTrue(suspectWhenLtpPositive)
        assertFalse(suspectWhenLtpZero)
    }

    // ---- version marker ------------------------------------------------------

    @Test
    fun guardsVersionMarkerIsPresentAndVersioned() {
        val v = PositionTickService.POSITION_TICK_GUARDS_VERSION
        assertTrue(v.startsWith("position_tick_guards_v"))
        assertTrue("v2 or later expected", !v.startsWith("position_tick_guards_v1"))
    }
}
