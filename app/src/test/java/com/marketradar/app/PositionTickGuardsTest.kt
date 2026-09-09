package com.marketradar.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Valuation guards behind the 27f08e3 leg-name fix, revision 3.
 *
 * Codex's negative-control review of revision 2 disabled the executable-price
 * rejection in-place and found all revision-2 tests still passed — they only
 * asserted that certain strings existed in the source file, never executed the
 * actual decision. Revision 3 extracts the whole valuation into valuePositionTick(),
 * a pure function with no Android dependency, specifically so tests can call it and
 * assert on its real output. Section 2 below is the direct answer to that finding:
 * each test there constructs real leg/quote data and asserts on the TickValuation
 * that comes back - executableMark, currentPnl, valuationAccepted - not on source
 * text. This file was itself re-run against a deliberately weakened build (the same
 * negative-control technique) to confirm it fails when the behaviour is removed;
 * see NEGATIVE_CONTROL_20260907.md for that run.
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

    // =========================================================================
    // 1. validateStructure() - unchanged behaviour, renamed statuses (Codex Q4)
    // =========================================================================

    @Test
    fun completeIronButterflyPasses() {
        val r = validateStructure("IRON_BUTTERFLY", ironButterflyLegs())
        assertEquals(STRUCTURE_ROLES_OK, r.status)
        assertEquals(4, r.expected)
        assertEquals(4, r.actual)
        assertTrue(r.problems.isEmpty())
    }

    @Test
    fun completeVerticalSpreadPasses() {
        val r = validateStructure("BEAR_CALL", listOf(leg("A", "SHORT", "CE"), leg("B", "LONG", "CE")))
        assertEquals(STRUCTURE_ROLES_OK, r.status)
        assertEquals(2, r.expected)
    }

    @Test
    fun fourLegStructureWithOnlyTwoLegsIsIncomplete() {
        // Exactly the pre-27f08e3 production shape: the second pair silently absent.
        val r = validateStructure("IRON_BUTTERFLY", ironButterflyLegs().take(2))
        assertEquals(STRUCTURE_LEGS_MISSING, r.status)
        assertEquals(4, r.expected)
        assertEquals(2, r.actual)
        assertTrue(r.problems.any { it.startsWith("missing_legs") })
    }

    @Test
    fun duplicateInstrumentsAreInvalidNotOk() {
        val dup = listOf(
            leg("SAME", "SHORT", "CE"), leg("SAME", "LONG", "CE"),
            leg("K3", "SHORT", "PE"), leg("K4", "LONG", "PE")
        )
        val r = validateStructure("IRON_CONDOR", dup)
        assertEquals(STRUCTURE_ROLES_INVALID, r.status)
        assertTrue(r.problems.contains("duplicate_instrument_keys"))
    }

    @Test
    fun invertedRolesAreInvalid() {
        val bad = listOf(
            leg("K1", "SHORT", "CE"), leg("K2", "SHORT", "CE"),
            leg("K3", "SHORT", "PE"), leg("K4", "LONG", "PE")
        )
        val r = validateStructure("IRON_CONDOR", bad)
        assertEquals(STRUCTURE_ROLES_INVALID, r.status)
        assertTrue(r.problems.any { it.startsWith("role_mismatch") })
    }

    @Test
    fun unknownStrategyIsUnsupportedNotOk() {
        val r = validateStructure("STRADDLE", listOf(leg("A", "SHORT", "CE")))
        assertEquals(STRUCTURE_UNSUPPORTED, r.status)
        assertNull(r.expected)
        assertTrue(r.problems.contains("unsupported_strategy"))
    }

    @Test
    fun expectedLegCountMatchesTheSpec() {
        assertEquals(4, expectedLegCount("IRON_CONDOR"))
        assertEquals(4, expectedLegCount("IRON_BUTTERFLY"))
        assertEquals(2, expectedLegCount("BEAR_CALL"))
        assertNull(expectedLegCount("MYSTERY"))
    }

    // =========================================================================
    // 2. valuePositionTick() - real behaviour, not source-text assertions.
    //    Direct answer to Codex's negative-control finding.
    // =========================================================================

    @Test
    fun happyPathValuesAndPricesCorrectly() {
        // Simple 2-leg BEAR_CALL, credit trade, clean two-sided quotes.
        val legs = listOf(leg("S", "SHORT", "CE"), leg("L", "LONG", "CE"))
        val quotes = mapOf(
            "S" to LegQuote(bid = 100.0, ask = 105.0, ltp = 102.0),
            "L" to LegQuote(bid = 40.0, ask = 45.0, ltp = 42.0)
        )
        val v = valuePositionTick(
            strategyType = "BEAR_CALL", legs = legs, quotes = quotes, isCredit = true,
            entryPremium = 50.0, maxProfit = 1500.0, maxLoss = 3500.0, lotSize = 30.0
        )
        assertEquals("OK", v.valuationQuality)
        assertTrue(v.valuationAccepted)
        // Closing a credit spread: buy back short at ask (105), sell long at bid (40).
        assertEquals(65.0, v.executableMark!!, 0.001)
        assertNotNull(v.currentPnl)
        assertFalse(v.boundAnomaly)
    }

    @Test
    fun zeroExecutablePriceContradictedByLtpIsRejected_trade269() {
        // Reproduces trade 269, 2026-09-07: long leg bid=0, ask=443, ltp=404.15,
        // short leg ask=739.9. A rejection-disabled build would compute a real
        // (wrong) mark and P&L here instead of leaving both null - that is exactly
        // what Codex's negative control caught in revision 2's source-text tests.
        val legs = listOf(leg("SHORT_LEG", "SHORT", "CE"), leg("LONG_LEG", "LONG", "CE"))
        val quotes = mapOf(
            "SHORT_LEG" to LegQuote(bid = 520.1, ask = 739.9, ltp = 682.7),
            "LONG_LEG" to LegQuote(bid = 0.0, ask = 443.0, ltp = 404.15)
        )
        val v = valuePositionTick(
            strategyType = "BEAR_CALL", legs = legs, quotes = quotes, isCredit = true,
            entryPremium = 294.4, maxProfit = 8832.0, maxLoss = 9168.0, lotSize = 30.0
        )
        assertEquals("DEGRADED", v.valuationQuality)
        assertFalse("a valuation built on a data-gap quote must not be accepted", v.valuationAccepted)
        assertNull("no executable mark may publish from a rejected valuation", v.executableMark)
        assertNull("no P&L may publish from a rejected valuation", v.currentPnl)
        assertEquals(1, v.nonPositiveQuoteLegs)
        val longLegValuation = v.legValuations.first { it.leg.instrumentKey == "LONG_LEG" }
        assertEquals("NON_POSITIVE_QUOTE", longLegValuation.quoteStatus)
    }

    @Test
    fun zeroExecutablePriceIsRejectedEvenWhenLtpIsAlsoZero() {
        // v3 policy change from v2 (documented in QUOTE_CONTRACT): the executable
        // side must be strictly positive UNCONDITIONALLY, matching the Python
        // contract exactly, rather than only when LTP disagrees. Justified because
        // a zero executable price with a zero LTP has never once been observed
        // (47,338 legs checked) - the conditional carve-out cost nothing to remove
        // and removing it is simpler to reason about.
        val legs = listOf(leg("S", "SHORT", "CE"), leg("L", "LONG", "CE"))
        val quotes = mapOf(
            "S" to LegQuote(bid = 10.0, ask = 12.0, ltp = 11.0),
            "L" to LegQuote(bid = 0.0, ask = 0.05, ltp = 0.0)
        )
        val v = valuePositionTick(
            strategyType = "BEAR_CALL", legs = legs, quotes = quotes, isCredit = true,
            entryPremium = 5.0, maxProfit = 150.0, maxLoss = 350.0, lotSize = 30.0
        )
        assertFalse(v.valuationAccepted)
        assertNull(v.currentPnl)
    }

    @Test
    fun crossedQuoteIsRejectedAndCounted() {
        val legs = listOf(leg("S", "SHORT", "CE"), leg("L", "LONG", "CE"))
        val quotes = mapOf(
            "S" to LegQuote(bid = 100.0, ask = 90.0, ltp = 95.0), // crossed: bid > ask
            "L" to LegQuote(bid = 40.0, ask = 45.0, ltp = 42.0)
        )
        val v = valuePositionTick(
            strategyType = "BEAR_CALL", legs = legs, quotes = quotes, isCredit = true,
            entryPremium = 50.0, maxProfit = 1500.0, maxLoss = 3500.0, lotSize = 30.0
        )
        assertEquals(1, v.crossedQuoteLegs)
        assertFalse(v.valuationAccepted)
        val shortLegValuation = v.legValuations.first { it.leg.instrumentKey == "S" }
        assertEquals("CROSSED_QUOTE", shortLegValuation.quoteStatus)
        assertNull("a crossed quote must not produce a mid either", shortLegValuation.mid)
    }

    @Test
    fun oneSidedDepthIsDegradedEvenWhenClosingSidesArePresent() {
        val legs = listOf(leg("S", "SHORT", "CE"), leg("L", "LONG", "CE"))
        val quotes = mapOf(
            "S" to LegQuote(bid = null, ask = 105.0, ltp = 102.0),
            "L" to LegQuote(bid = 40.0, ask = null, ltp = 42.0)
        )
        val v = valuePositionTick(
            strategyType = "BEAR_CALL", legs = legs, quotes = quotes, isCredit = true,
            entryPremium = 50.0, maxProfit = 1500.0, maxLoss = 3500.0, lotSize = 30.0
        )
        assertEquals("DEGRADED", v.valuationQuality)
        assertFalse(v.valuationAccepted)
        assertNull(v.executableMark)
        assertNull(v.currentPnl)
        assertTrue(v.legValuations.all { it.quoteStatus == "NO_DEPTH" })
    }

    @Test
    fun structuralIncompletenessBlocksValuationRegardlessOfQuoteQuality() {
        // Only 2 of 4 iron-butterfly legs - the original 27f08e3 defect shape -
        // even though the two present legs have perfect quotes.
        val legs = ironButterflyLegs().take(2)
        val quotes = mapOf(
            "K1" to LegQuote(bid = 100.0, ask = 105.0, ltp = 102.0),
            "K2" to LegQuote(bid = 40.0, ask = 45.0, ltp = 42.0)
        )
        val v = valuePositionTick(
            strategyType = "IRON_BUTTERFLY", legs = legs, quotes = quotes, isCredit = true,
            entryPremium = 50.0, maxProfit = 8000.0, maxLoss = 9000.0, lotSize = 30.0
        )
        assertEquals("STRUCTURE_INCOMPLETE", v.valuationQuality)
        assertFalse(v.valuationAccepted)
        assertNull(v.currentPnl)
    }

    @Test
    fun unsupportedStrategyNeverReachesOk() {
        // Fixed in this revision: previously an UNSUPPORTED strategy (never role-
        // checked) could still reach valuationQuality=OK purely because the quotes
        // happened to be complete - "we did not look" reading as "we looked and it
        // is fine". Zero-cost fix: every strategy_type ever persisted in production
        // is in STRUCTURE_SPEC.
        val legs = listOf(leg("A", "SHORT", "CE"), leg("B", "LONG", "CE"))
        val quotes = mapOf(
            "A" to LegQuote(bid = 100.0, ask = 105.0, ltp = 102.0),
            "B" to LegQuote(bid = 40.0, ask = 45.0, ltp = 42.0)
        )
        val v = valuePositionTick(
            strategyType = "STRADDLE", legs = legs, quotes = quotes, isCredit = true,
            entryPremium = 50.0, maxProfit = 1500.0, maxLoss = 3500.0, lotSize = 30.0
        )
        assertEquals("STRUCTURE_UNCHECKED", v.valuationQuality)
        assertFalse(v.valuationAccepted)
        assertNull(v.currentPnl)
    }

    @Test
    fun boundAnomalyIsRecordedButDoesNotVetoPnl() {
        // Revision 1 nulled P&L on a bound breach, which would have suppressed
        // SHADOW_SL exactly when a position is deepest underwater. Revision 2 fixed
        // this at the service level; this test proves it directly against the pure
        // function. A deliberately extreme mark (short leg far ITM against a small
        // declared max_loss) breaches the tolerance band.
        val legs = listOf(leg("S", "SHORT", "CE"), leg("L", "LONG", "CE"))
        val quotes = mapOf(
            "S" to LegQuote(bid = 900.0, ask = 950.0, ltp = 925.0),
            "L" to LegQuote(bid = 5.0, ask = 8.0, ltp = 6.5)
        )
        val v = valuePositionTick(
            strategyType = "BEAR_CALL", legs = legs, quotes = quotes, isCredit = true,
            entryPremium = 50.0, maxProfit = 1500.0, maxLoss = 3500.0, lotSize = 30.0
        )
        assertTrue(v.valuationAccepted)
        assertTrue("mark is far outside the declared risk envelope", v.boundAnomaly)
        assertNotNull("bound anomaly must NOT null the P&L", v.currentPnl)
    }

    @Test
    fun runningExtremaContributeOnEveryAcceptedValuationIncludingAnomalies() {
        // EXTREMA_CONTRACT: revision 2 additionally excluded bound anomalies from
        // running MAE/MFE, which was an inconsistent second level of trust (Codex's
        // observation). Revision 3's rule is one rule: every accepted valuation
        // contributes, full stop. This test documents that boundAnomaly=true still
        // carries a real currentPnl fit for a caller to feed into running state -
        // the caller (buildTickRow) no longer branches on boundAnomaly before that
        // call.
        val legs = listOf(leg("S", "SHORT", "CE"), leg("L", "LONG", "CE"))
        val quotes = mapOf(
            "S" to LegQuote(bid = 900.0, ask = 950.0, ltp = 925.0),
            "L" to LegQuote(bid = 5.0, ask = 8.0, ltp = 6.5)
        )
        val v = valuePositionTick(
            strategyType = "BEAR_CALL", legs = legs, quotes = quotes, isCredit = true,
            entryPremium = 50.0, maxProfit = 1500.0, maxLoss = 3500.0, lotSize = 30.0
        )
        assertTrue(v.boundAnomaly)
        assertNotNull(v.currentPnl)
    }

    @Test
    fun rawExecutableMarkIsAllOrNothingNeverAPartialSum() {
        // Codex Q3: does raw_executable_mark ever sum only the legs that happened to
        // report one? No - it is null unless every leg supplied a raw executable
        // side, so a partial value can never masquerade as a full diagnostic mark.
        val legs = listOf(leg("S", "SHORT", "CE"), leg("L", "LONG", "CE"))
        val quotesOneMissing = mapOf(
            "S" to LegQuote(bid = 100.0, ask = 105.0, ltp = 102.0)
            // "L" has no quote at all - NO_QUOTE.
        )
        val vMissing = valuePositionTick(
            strategyType = "BEAR_CALL", legs = legs, quotes = quotesOneMissing, isCredit = true,
            entryPremium = 50.0, maxProfit = 1500.0, maxLoss = 3500.0, lotSize = 30.0
        )
        assertFalse(vMissing.rawMarkComplete)
        assertNull(vMissing.rawExecutableMark)

        // A raw price of exactly 0.0 (non-positive, but PRESENT) still counts toward
        // completeness - the raw mark is a diagnostic of what the venue quoted, not
        // a re-application of the executable-price filter.
        val quotesZeroButPresent = mapOf(
            "S" to LegQuote(bid = 100.0, ask = 105.0, ltp = 102.0),
            "L" to LegQuote(bid = 0.0, ask = 45.0, ltp = 42.0)
        )
        val vZero = valuePositionTick(
            strategyType = "BEAR_CALL", legs = legs, quotes = quotesZeroButPresent, isCredit = true,
            entryPremium = 50.0, maxProfit = 1500.0, maxLoss = 3500.0, lotSize = 30.0
        )
        assertTrue(vZero.rawMarkComplete)
        assertNotNull(vZero.rawExecutableMark)
        // Long leg closes by selling at bid=0.0; short leg closes by buying at ask=105.0.
        assertEquals(105.0, vZero.rawExecutableMark!!, 0.001)
    }

    @Test
    fun incompleteStructureNeverReportsCompleteRawPositionMark() {
        val legs = ironButterflyLegs().take(2)
        val quotes = mapOf(
            "K1" to LegQuote(bid = 100.0, ask = 105.0, ltp = 102.0),
            "K2" to LegQuote(bid = 40.0, ask = 45.0, ltp = 42.0)
        )
        val v = valuePositionTick(
            strategyType = "IRON_BUTTERFLY", legs = legs, quotes = quotes, isCredit = true,
            entryPremium = 50.0, maxProfit = 1500.0, maxLoss = 3500.0, lotSize = 30.0
        )
        assertEquals("STRUCTURE_INCOMPLETE", v.valuationQuality)
        assertFalse(v.rawMarkComplete)
        assertNull(v.rawExecutableMark)
    }

    // =========================================================================
    // 3. violatesStructuralBounds() - pure, unchanged from revision 2.
    // =========================================================================

    @Test
    fun boundsFlagTheProductionExtreme() {
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
    }

    // =========================================================================
    // 4. Version marker
    // =========================================================================

    @Test
    fun guardsVersionMarkerIsPresentAndV3() {
        val v = PositionTickService.POSITION_TICK_GUARDS_VERSION
        assertTrue(v.startsWith("position_tick_guards_v"))
        assertTrue("v3 or later expected", !v.startsWith("position_tick_guards_v1") && !v.startsWith("position_tick_guards_v2"))
    }
}
