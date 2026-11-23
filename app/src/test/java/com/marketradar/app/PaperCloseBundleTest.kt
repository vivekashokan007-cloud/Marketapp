package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * One accepted close bundle must price gross P&L, fees AND the saved evidence.
 *
 * The defect this closes, from production trade 320 (NIFTY 25600/25450 Bear Put, closed
 * 2026-11-23 12:31:50.093 by Book Profit):
 *
 *   * the booked exit was **87.50**, priced from a dedicated close request
 *     (`paper_close_quote_v1`, request 0d60f511-b6c5-4bf3-a0fd-e313568786a0, source quote
 *     12:31:46.921, 30 s validity, mark_trust TRUSTED);
 *   * the saved `exit_snapshot.close_leg_quotes` were long 132.05 / short 40.20 -> **91.85**,
 *     which reproduces the **12:29:44.749 periodic tick exactly**, 2 min 05 s older;
 *   * `close_turnover` 11,196.25 = (132.05 + 40.20) x 65, so the fee path used the stale pair
 *     while gross P&L used the fresh one.
 *
 * Cause: the close-quote payload published `executable_close_premium` but no legs, so whatever
 * persisted the outcome had to supply its own bundle and reached for the last card mark. The
 * fix is `TickValuation.closeLegQuotesJson()` plus the `legs` / `close_leg_quotes` /
 * `close_bundle_complete` fields on the payload.
 *
 * The 12:30:47 tick at 90.00 sits between the two, so the decline 91.85 -> 90.00 -> 87.50 is
 * monotone and the fresh quote is the correct one. These fixtures are those production legs.
 */
class PaperCloseBundleTest {

    private fun leg(key: String, side: String, type: String, strike: Double) = PositionLeg(
        instrumentKey = key, side = side,
        closeSide = if (side == "SHORT") CloseSide.BUY_TO_CLOSE else CloseSide.SELL_TO_CLOSE,
        optionType = type, strike = strike
    )

    private fun lv(
        leg: PositionLeg, bid: Double?, ask: Double?, ltp: Double? = null,
        executable: Double? = null, status: String = "OK"
    ) = LegValuation(
        leg = leg, bid = bid, ask = ask, ltp = ltp,
        mid = if (bid != null && ask != null) (bid + ask) / 2.0 else null,
        executablePrice = executable, priceBasis = "EXECUTABLE", quoteStatus = status
    )

    private fun valuation(legs: List<LegValuation>, mark: Double?) = TickValuation(
        legValuations = legs,
        structure = StructureCheck(
            status = "OK", expected = legs.size, actual = legs.size, problems = emptyList()
        ),
        valuationQuality = "OK", valuationAccepted = mark != null,
        executableMark = mark, midMark = null, ltpMark = null,
        rawExecutableMark = mark, rawMarkComplete = mark != null,
        currentPnl = null, currentPnlR = null,
        boundAnomaly = false, nonPositiveQuoteLegs = 0, crossedQuoteLegs = 0
    )

    // trade 320's own contracts
    private val long25600 = leg("NSE_FO|64486", "LONG", "PE", 25600.0)
    private val short25450 = leg("NSE_FO|64476", "SHORT", "PE", 25450.0)

    /** The stale 12:29:44.749 card mark: long 132.05/132.35, short 40.10/40.20 -> 91.85. */
    private val staleCardMark = valuation(
        listOf(
            lv(long25600, bid = 132.05, ask = 132.35, ltp = 132.45, executable = 132.05),
            lv(short25450, bid = 40.10, ask = 40.20, ltp = 39.65, executable = 40.20)
        ),
        mark = 91.85
    )

    /** The fresh 12:31:46.921 close quote the fill actually used -> 87.50. */
    private val freshCloseQuote = valuation(
        listOf(
            lv(long25600, bid = 126.40, ask = 126.70, ltp = 126.55, executable = 126.40),
            lv(short25450, bid = 38.80, ask = 38.90, ltp = 38.85, executable = 38.90)
        ),
        mark = 87.50
    )

    @Test
    fun theFreshCloseQuotePublishesItsOwnLegsSoFeesCannotUseTheStaleCard() {
        val fresh = freshCloseQuote.closeLegQuotesJson()
        assertNotNull("the accepted close quote must publish its legs", fresh)
        val buy = fresh!!.getJSONObject("buy")
        val sell = fresh.getJSONObject("sell")

        // The bundle is the FRESH one, not the card's.
        assertEquals(126.40, buy.getDouble("bid"), 1e-9)
        assertEquals(38.90, sell.getDouble("ask"), 1e-9)

        // Gross and fees now derive from the same pair.
        assertEquals(87.50, buy.getDouble("bid") - sell.getDouble("ask"), 1e-9)
        assertEquals(
            "fee turnover must come from the same legs as the fill",
            (126.40 + 38.90) * 65.0,
            (buy.getDouble("bid") + sell.getDouble("ask")) * 65.0, 1e-9
        )
        assertTrue(freshCloseQuote.closeBundleComplete())
    }

    @Test
    fun theStaleCardMarkIsTheProductionDefectAndIsNowDistinguishable() {
        val stale = staleCardMark.closeLegQuotesJson()!!
        val staleClose = stale.getJSONObject("buy").getDouble("bid") -
            stale.getJSONObject("sell").getDouble("ask")
        assertEquals("the 12:29:44.749 tick values", 91.85, staleClose, 1e-9)
        assertEquals("trade 320's stored close_turnover", 11196.25,
            (stale.getJSONObject("buy").getDouble("bid") +
                stale.getJSONObject("sell").getDouble("ask")) * 65.0, 1e-9)

        // 4.35 points apart; using the card for fees and the quote for P&L is the mismatch.
        val freshClose = freshCloseQuote.closeLegQuotesJson()!!.let {
            it.getJSONObject("buy").getDouble("bid") - it.getJSONObject("sell").getDouble("ask")
        }
        assertEquals(4.35, staleClose - freshClose, 1e-9)
    }

    @Test
    fun legsCarryContractIdentityAndSideSoTheBundleCanBeAudited() {
        val b = freshCloseQuote.closeLegQuotesJson()!!.getJSONObject("buy")
        assertEquals("NSE_FO|64486", b.getString("instrument_key"))
        assertEquals(25600.0, b.getDouble("strike"), 1e-9)
        assertEquals("PE", b.getString("option_type"))
        assertEquals("LONG", b.getString("side"))
        assertEquals("OK", b.getString("quote_status"))
    }

    @Test
    fun aFourLegStructureRefusesTheTwoSidedShapeRatherThanOverwritingALeg() {
        // An iron butterfly has two LONG and two SHORT legs: {buy, sell} cannot represent it,
        // and silently keeping one of each would misprice both P&L and fees.
        val ib = valuation(
            listOf(
                lv(leg("NSE_FO|69801", "SHORT", "CE", 56500.0), 454.95, 475.0, executable = 475.0),
                lv(leg("NSE_FO|69826", "LONG", "CE", 57700.0), 97.75, 179.85, executable = 97.75),
                lv(leg("NSE_FO|69802", "SHORT", "PE", 56500.0), 120.0, 130.0, executable = 130.0),
                lv(leg("NSE_FO|69776", "LONG", "PE", 55300.0), 40.0, 45.0, executable = 40.0)
            ),
            mark = 282.75
        )
        assertNull("a 4-leg structure must not be squeezed into {buy, sell}", ib.closeLegQuotesJson())
    }

    @Test
    fun aSingleLegClosePublishesOneSideAndIsComplete() {
        val single = valuation(
            listOf(lv(leg("NSE_FO|59956", "LONG", "PE", 64000.0), 1535.10, 1541.70,
                ltp = 1538.0, executable = 1535.10)),
            mark = 1535.10
        )
        val q = single.closeLegQuotesJson()!!
        assertTrue(q.has("buy"))
        assertFalse(q.has("sell"))
        assertEquals(1535.10, q.getJSONObject("buy").getDouble("bid"), 1e-9)
        assertTrue(single.closeBundleComplete())
    }

    @Test
    fun aMissingExecutablePriceMarksTheBundleIncompleteInsteadOfPricingIt() {
        val partial = valuation(
            listOf(
                lv(long25600, bid = 126.40, ask = 126.70, executable = 126.40),
                lv(short25450, bid = null, ask = null, executable = null, status = "MISSING")
            ),
            mark = null
        )
        assertNotNull(partial.closeLegQuotesJson())
        assertFalse(
            "an incomplete bundle must be reported, not silently completed from elsewhere",
            partial.closeBundleComplete()
        )
    }

    @Test
    fun emptyLegsYieldNoBundleRatherThanAnEmptyOne() {
        val none = valuation(emptyList(), mark = null)
        assertNull(none.closeLegQuotesJson())
        assertFalse(none.closeBundleComplete())
    }

    @Test
    fun twoLegsOnTheSameSideRefuseRatherThanCollide() {
        val both = valuation(
            listOf(
                lv(leg("NSE_FO|1", "LONG", "CE", 100.0), 10.0, 11.0, executable = 10.0),
                lv(leg("NSE_FO|2", "LONG", "CE", 200.0), 5.0, 6.0, executable = 5.0)
            ),
            mark = 5.0
        )
        assertNull(both.closeLegQuotesJson())
    }

    @Test
    fun theBundleContractIsNamedSoAReplayCanTellTheVersionsApart() {
        assertTrue(
            PositionTickService.PAPER_CLOSE_BUNDLE_CONTRACT.startsWith("paper_close_bundle_v1")
        )
    }
}
