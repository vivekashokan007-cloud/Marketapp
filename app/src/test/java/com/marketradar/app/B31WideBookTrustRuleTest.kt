package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.LocalDate

/**
 * B3.1 (2026-09-26): WIDE_EXECUTABLE_BOOK book-width validity and the Paper
 * MID_FALLBACK_WIDE_BOOK stop. Production fixtures are the exact legs of the
 * 274 / 276 09:15 ticks (read-only SELECT); crash fixtures are synthetic.
 *
 * The decision chain exercised here is the production one:
 *   classifyPositionMarkTrust -> midFallbackStopPnl -> decideShadowAction
 *   -> shadowExitNotificationContent
 * (PositionTickService.buildTickRow / evaluateShadowPolicy / maybeNotifyShadowExit
 * are thin adapters over these pure functions).
 */
class B31WideBookTrustRuleTest {

    private fun leg(key: String, side: String, type: String, strike: Double) = PositionLeg(
        instrumentKey = key, side = side,
        closeSide = if (side == "SHORT") CloseSide.BUY_TO_CLOSE else CloseSide.SELL_TO_CLOSE,
        optionType = type, strike = strike
    )

    // Trade 274 — BNF IRON_BUTTERFLY, entry 850.1 credit, qty 30, stored MP 25503 / ML 10497.
    private val legs274 = listOf(
        leg("NSE_FO|69801", "SHORT", "CE", 56500.0),
        leg("NSE_FO|69826", "LONG", "CE", 57700.0),
        leg("NSE_FO|69802", "SHORT", "PE", 56500.0),
        leg("NSE_FO|69776", "LONG", "PE", 55300.0)
    )
    private val quotes274At0915 = mapOf(          // production tick 27873, 16 Sep 09:15:00
        "NSE_FO|69801" to LegQuote(454.95, 475.0, 459.75),
        "NSE_FO|69826" to LegQuote(97.75, 179.85, 128.6),
        "NSE_FO|69802" to LegQuote(740.0, 950.0, 855.6),
        "NSE_FO|69776" to LegQuote(225.05, 399.85, 355.1)
    )
    private val quotes274At091557 = mapOf(        // production tick 27875, 16 Sep 09:15:57
        "NSE_FO|69801" to LegQuote(522.85, 524.7, 523.05),
        "NSE_FO|69826" to LegQuote(148.95, 149.9, 149.0),
        "NSE_FO|69802" to LegQuote(709.95, 713.25, 708.3),
        "NSE_FO|69776" to LegQuote(268.4, 270.0, 269.1)
    )
    // Trade 276 — BNF IRON_BUTTERFLY, entry 864.15, qty 30, stored MP 25924 / ML 10076.
    private val legs276 = listOf(
        leg("NSE_FO|69795", "SHORT", "CE", 56200.0),
        leg("NSE_FO|69820", "LONG", "CE", 57400.0),
        leg("NSE_FO|69796", "SHORT", "PE", 56200.0),
        leg("NSE_FO|69768", "LONG", "PE", 55000.0)
    )
    private val quotes276At0915 = mapOf(          // production tick 27075, 15 Sep 09:15:00
        "NSE_FO|69795" to LegQuote(635.0, 1599.85, 1069.05),
        "NSE_FO|69820" to LegQuote(401.15, 679.85, 408.95),
        "NSE_FO|69796" to LegQuote(66.65, 509.95, 361.4),
        "NSE_FO|69768" to LegQuote(108.1, 126.85, 112.0)
    )
    // Synthetic crash on 274 (spot ~55,400): executable beyond max loss, mid below SL.
    private val crashWideLiquidation = mapOf(
        "NSE_FO|69801" to LegQuote(5.0, 35.0, 20.0),
        "NSE_FO|69826" to LegQuote(0.5, 3.5, 2.0),
        "NSE_FO|69802" to LegQuote(1150.0, 1350.0, 1250.0),
        "NSE_FO|69776" to LegQuote(60.0, 240.0, 150.0)
    )
    // Synthetic crash on 274: executable INSIDE bounds, gap > 20% of ML, mid below SL.
    private val crashWideExecutable = mapOf(
        "NSE_FO|69801" to LegQuote(20.0, 40.0, 30.0),
        "NSE_FO|69826" to LegQuote(1.0, 5.0, 3.0),
        "NSE_FO|69802" to LegQuote(1140.0, 1240.0, 1190.0),
        "NSE_FO|69776" to LegQuote(120.0, 160.0, 140.0)
    )

    private val sl274 = -PositionPolicyV1.SL_MULT * 10497.0      // -6,298.2
    private val tp274 = PositionPolicyV1.TP_MULT * 25503.0       // 12,751.5
    private val sl276 = -PositionPolicyV1.SL_MULT * 10076.0
    private val tp276 = PositionPolicyV1.TP_MULT * 25924.0
    private val receipt = "2026-09-15T03:46:00.000Z"          // Tue 09:16:00 IST

    private fun validity(
        legs: List<PositionLeg>, quotes: Map<String, LegQuote>, entry: Double,
        ts: (String) -> String? = { "2026-09-15T09:15:59.4+05:30" }
    ): PositionQuoteValidity {
        val v = valuePositionTick("IRON_BUTTERFLY", legs, quotes, true, entry, null, null, 30.0)
        val src = legs.associate { l ->
            val k = l.instrumentKey!!
            val q = quotes[k]
            k to LegSourceQuote(k, q != null, true, "RESP:$k", q?.bid, q?.ask, ts(k), null)
        }
        return validatePositionQuotes(
            legs.map { it.instrumentKey }, v.structure.status,
            v.legValuations.associate { it.leg.instrumentKey!! to it.quoteStatus },
            src, receipt, LocalDate.parse("2026-09-30"), 30.0, true, "OK"
        )
    }

    private data class Paper(val tv: TickValuation, val v: PositionQuoteValidity, val trust: MarkTrust,
                             val midFallback: Double?, val action: String)

    /** The Paper decision chain as buildTickRow/evaluateShadowPolicy run it (priceGateApplied = !trusted). */
    private fun paper(
        legs: List<PositionLeg>, quotes: Map<String, LegQuote>, entry: Double, mp: Double, ml: Double,
        sl: Double, tp: Double, v: PositionQuoteValidity = validity(legs, quotes, entry)
    ): Paper {
        val tv = valuePositionTick("IRON_BUTTERFLY", legs, quotes, true, entry, mp, ml, 30.0)
        val expected = expectedStructuralBounds("IRON_BUTTERFLY", legs, entry, true, 30.0)
        val trust = classifyPositionMarkTrust(v, tv.valuationAccepted, tv.currentPnl, tv.midMark, entry, true,
            30.0, mp, ml, expected)
        val gate = !trust.trusted
        val mid = if (gate) midFallbackStopPnl(trust, v) else null
        val action = decideShadowAction(
            if (gate) null else tv.currentPnl, mid, sl, tp, false,
            if (gate) (trust.cause ?: CAUSE_UNRESOLVED) else null, tv.valuationQuality
        )
        return Paper(tv, v, trust, mid, action)
    }

    // --------------------------------------------------------- required cases

    @Test
    fun trade274At0915_noStop() {
        val p = paper(legs274, quotes274At0915, 850.1, 25503.0, 10497.0, sl274, tp274)
        assertEquals(-7563.0, p.tv.currentPnl!!, 0.5)
        assertTrue(p.tv.currentPnl!! <= sl274)                  // pre-B3.1 this fired SHADOW_SL
        assertEquals(CAUSE_WIDE_EXECUTABLE_BOOK, p.trust.cause)
        assertEquals(-259.0, p.midFallback!!, 1.0)               // eligible, but mid is far above SL
        assertEquals("SHADOW_DEGRADED", p.action)
    }

    @Test
    fun trade276At0915_noStop() {
        val p = paper(legs276, quotes276At0915, 864.15, 25924.0, 10076.0, sl276, tp276)
        assertEquals(-22092.0, p.tv.currentPnl!!, 0.5)
        assertEquals(CAUSE_WIDE_LIQUIDATION_BOOK, p.trust.cause)
        assertEquals(3492.0, p.midFallback!!, 1.0)
        assertEquals("SHADOW_DEGRADED", p.action)
    }

    @Test
    fun trade274From091557StaysTrustedAndHolds() {
        val p = paper(legs274, quotes274At091557, 850.1, 25503.0, 10497.0, sl274, tp274)
        assertEquals(TRUST_TRUSTED, p.trust.state)
        assertNull(p.midFallback)
        assertEquals("HOLD", p.action)
    }

    @Test
    fun syntheticCrashBeyondBoundsWithMidBelowSl_stopsOnMidBasis() {
        val p = paper(legs274, crashWideLiquidation, 850.1, 25503.0, 10497.0, sl274, tp274)
        assertEquals(QV_VALID, p.v.state)
        assertTrue(p.tv.boundAnomaly)
        assertEquals(-14232.0, p.tv.currentPnl!!, 1.0)
        assertEquals(CAUSE_WIDE_LIQUIDATION_BOOK, p.trust.cause)
        assertEquals(-8037.0, p.midFallback!!, 1.0)
        assertTrue(isMidFallbackStop(null, p.midFallback, sl274))
        assertEquals("SHADOW_SL", p.action)
        val (title, body, channel) = shadowExitNotificationContent("SHADOW_SL", "BNF IRON_BUTTERFLY",
            fallbackRow(p.tv.currentPnl!!, p.midFallback!!, p.trust.cause!!))!!
        assertEquals("urgent", channel)
        assertEquals("🛑 Stop Loss Near (mid basis)", title)
        assertTrue(body, body.contains("expected fill (bid/ask) P&L ₹-14,232"))
        assertTrue(body, body.contains("mid P&L ₹-8,037 (indicative)"))
        assertTrue(body, body.contains("stop_basis=MID_FALLBACK_WIDE_BOOK"))
        // The expected fill cost comes before the indicative mid.
        assertTrue(body.indexOf("expected fill") < body.indexOf("mid P&L"))
    }

    @Test
    fun syntheticCrashInsideBoundsWideExecutableBook_stopsOnMidBasis() {
        val p = paper(legs274, crashWideExecutable, 850.1, 25503.0, 10497.0, sl274, tp274)
        assertFalse(p.tv.boundAnomaly)
        assertEquals(CAUSE_WIDE_EXECUTABLE_BOOK, p.trust.cause)
        assertTrue(p.midFallback!! <= sl274)
        assertEquals("SHADOW_SL", p.action)
    }

    @Test
    fun syntheticCrashWithOneStaleLeg_noStop_escalationPath() {
        val stale = validity(legs274, crashWideLiquidation, 850.1, ts = { k ->
            if (k == "NSE_FO|69802") "2026-09-15T09:13:00+05:30" else "2026-09-15T09:15:59.4+05:30"
        })
        val p = paper(legs274, crashWideLiquidation, 850.1, 25503.0, 10497.0, sl274, tp274, stale)
        assertEquals(QV_INVALID, p.v.state)
        assertEquals(CAUSE_QUOTE_INVALIDITY, p.trust.cause)
        assertNotNull(p.trust.midPnl)                           // a mid exists, but is not used
        assertNull(p.midFallback)
        assertEquals("SHADOW_DEGRADED", p.action)
        // Escalation path (item 3): 5 untrusted ticks over >= 4 minutes escalate once.
        var state: JSONObject? = null
        val t0 = 1_000_000L
        val steps = (0 until 6).map { i ->
            advanceMarkFailureEpisode(state, "274", "2026-09-15", true, p.trust.cause, t0 + i * 60_000L)
                .also { state = it.state }
        }
        assertEquals(listOf(false, false, false, false, true, true), steps.map { it.shouldEscalate })
    }

    @Test
    fun targetsNeverUseTheMidFallback() {
        // Wide-book untrusted mark whose mid is beyond the target: still no TP.
        val action = decideShadowAction(null, 20_000.0, sl274, tp274, false, CAUSE_WIDE_EXECUTABLE_BOOK, "OK")
        assertEquals("SHADOW_DEGRADED", action)
        // EOD precedence is unchanged when no stop applies.
        assertEquals("SHADOW_EOD",
            decideShadowAction(null, 20_000.0, sl274, tp274, true, CAUSE_WIDE_EXECUTABLE_BOOK, "OK"))
    }

    @Test
    fun nonWideCausesAndInvalidQuotesNeverReachTheFallback() {
        val base = paper(legs274, crashWideLiquidation, 850.1, 25503.0, 10497.0, sl274, tp274)
        for (cause in listOf(CAUSE_QUOTE_INVALIDITY, CAUSE_BOUND_REFERENCE_MISMATCH, CAUSE_UNRESOLVED,
                CAUSE_AWAITING_REVALIDATION, CAUSE_NO_ACCEPTED_VALUATION)) {
            assertNull(cause, midFallbackStopPnl(base.trust.copy(cause = cause), base.v))
        }
        val invalid = validity(legs274, crashWideLiquidation, 850.1, ts = { "2026-09-15T09:10:00+05:30" })
        assertNull(midFallbackStopPnl(base.trust, invalid))       // wide cause but stale quotes
        assertNull(midFallbackStopPnl(base.trust.copy(state = TRUST_TRUSTED, cause = null), base.v))
        assertNull(midFallbackStopPnl(base.trust.copy(midPnl = null), base.v))
    }

    // ------------------------------------------ addendum A: fallback only adds

    // 274 with a wider-than-normal but still TRUSTED book (gap 6.4% of ML < 20%):
    // executable -6,597 is below SL -6,298.2, mid -5,922 is above it.
    private val wideButTrustedBelowSl = mapOf(
        "NSE_FO|69801" to LegQuote(30.0, 40.0, 35.0),
        "NSE_FO|69826" to LegQuote(3.0, 5.0, 4.0),
        "NSE_FO|69802" to LegQuote(1160.0, 1183.0, 1171.5),
        "NSE_FO|69776" to LegQuote(150.0, 160.0, 155.0)
    )

    @Test
    fun wideBookExecutableBelowSlMidAboveSl_firesOnExecutableBasis() {
        val p = paper(legs274, wideButTrustedBelowSl, 850.1, 25503.0, 10497.0, sl274, tp274)
        assertEquals(TRUST_TRUSTED, p.trust.state)
        assertTrue(p.tv.currentPnl!! <= sl274)
        assertTrue(p.trust.midPnl!! > sl274)
        assertTrue(p.trust.bookWidth!!.gapFraction!! > 0.05)   // wider than any normal stored tick
        assertNull(p.midFallback)                              // mid never consulted when trusted
        assertEquals("SHADOW_SL", p.action)
        assertFalse(isMidFallbackStop(p.tv.currentPnl, p.trust.midPnl, sl274))
        val row = JSONObject().put("current_pnl", p.tv.currentPnl!!).put("policy_reason", "x")
            .put("policy_trace_json", JSONObject().put("mark_trust", JSONObject().put("price_gate_applied", false)))
        val (title, body, channel) = shadowExitNotificationContent("SHADOW_SL", "L", row)!!
        assertEquals("🛑 Stop Loss Near", title)                // executable basis, no mid wording
        assertFalse(body.contains("mid"))
        assertEquals("urgent", channel)
    }

    @Test
    fun midFallbackNeverSuppressesOrReplacesAnExecutableStop() {
        // Whenever the executable P&L is present, any mid value is irrelevant.
        for (mid in listOf(null, -20_000.0, -6_298.2, 0.0, 20_000.0)) {
            assertEquals("SHADOW_SL", decideShadowAction(-7_000.0, mid, sl274, tp274, false, null, "OK"))
            assertEquals("SHADOW_SL", decideShadowAction(-7_000.0, mid, sl274, tp274, true, null, "OK"))
            assertEquals("SHADOW_TP", decideShadowAction(13_000.0, mid, sl274, tp274, false, null, "OK"))
            assertEquals("HOLD", decideShadowAction(0.0, mid, sl274, tp274, false, null, "OK"))
        }
        // With the executable withheld, the fallback only turns a no-stop into a stop.
        assertEquals("SHADOW_DEGRADED", decideShadowAction(null, null, sl274, tp274, false, CAUSE_WIDE_EXECUTABLE_BOOK, "OK"))
        assertEquals("SHADOW_SL", decideShadowAction(null, -7_000.0, sl274, tp274, false, CAUSE_WIDE_EXECUTABLE_BOOK, "OK"))
    }

    // ------------------------------------------------------------- Real parity

    @Test
    fun realParity_legacyDecisionAndTextUnchanged() {
        // Real never gets a price gate or a fallback pnl: decideShadowAction with the
        // executable P&L and no cause is the legacy precedence, byte-for-byte.
        val tv = valuePositionTick("IRON_BUTTERFLY", legs274, quotes274At0915, true, 850.1, 25503.0, 10497.0, 30.0)
        assertEquals("SHADOW_SL", decideShadowAction(tv.currentPnl, null, sl274, tp274, false, null, "OK"))
        assertEquals("SHADOW_TP", decideShadowAction(13_000.0, null, sl274, tp274, false, null, "OK"))
        assertEquals("SHADOW_SL", decideShadowAction(-7_000.0, null, sl274, tp274, true, null, "OK"))
        assertEquals("SHADOW_EOD", decideShadowAction(0.0, null, sl274, tp274, true, null, "OK"))
        assertEquals("SHADOW_DEGRADED", decideShadowAction(null, null, sl274, tp274, false, null, "DEGRADED"))
        assertEquals("HOLD", decideShadowAction(0.0, null, sl274, tp274, false, null, "OK"))
        // A mid pnl never matters while the executable P&L is present (trusted/Real).
        assertEquals("HOLD", decideShadowAction(0.0, -9_999.0, sl274, tp274, false, null, "OK"))

        val real = JSONObject().put("current_pnl", -7563.0).put("policy_reason", "x")
            .put("policy_trace_json", JSONObject().put("mark_trust", JSONObject().put("price_gate_applied", false)))
        assertEquals(Triple("🛑 Stop Loss Near", "L · P&L ₹-7,563 · Cut position.", "urgent"),
            shadowExitNotificationContent("SHADOW_SL", "L", real))
        assertEquals(Triple("💰 Target Near", "L · P&L ₹-7,563 · Book profit.", "urgent"),
            shadowExitNotificationContent("SHADOW_TP", "L", real))
        assertEquals(Triple("⏰ Exit — EOD", "L · P&L ₹-7,563 · Square off before close.", "urgent"),
            shadowExitNotificationContent("SHADOW_EOD", "L", real))
        assertEquals(Triple("🧪 Position Data Incomplete", "L · valuation degraded · review marks before trusting P&L.", "routine"),
            shadowExitNotificationContent("SHADOW_DEGRADED", "L", real))
        assertNull(shadowExitNotificationContent("HOLD", "L", real))
        val paperEod = JSONObject().put("current_pnl", -7563.0).put("policy_reason", "mark_untrusted:WIDE_EXECUTABLE_BOOK")
            .put("policy_trace_json", JSONObject().put("mark_trust", JSONObject().put("price_gate_applied", true)))
        assertEquals("L · P&L ₹-7,563 (untrusted mark) · Square off before close.",
            shadowExitNotificationContent("SHADOW_EOD", "L", paperEod)!!.second)
        assertEquals(Triple("⚠️ Stop/Target Unavailable",
            "L · mark untrusted (WIDE_EXECUTABLE_BOOK) · stop/target cannot be evaluated · review position.", "routine"),
            shadowExitNotificationContent("SHADOW_DEGRADED", "L", paperEod))
    }

    // ----------------------------------------------------------- book width

    @Test
    fun bookWidthThresholdIsRelativeToMaxLossStrictAndRecorded() {
        val at = assessBookWidth(-100.0, -100.0 + 2000.0, 10000.0, null)   // gap == 20% exactly
        assertEquals(BOOK_WIDTH_OK, at.status)
        assertEquals(BOOK_WIDTH_WIDE, assessBookWidth(-100.0, 1900.01, 10000.0, null).status)
        val structural = assessBookWidth(0.0, 3000.0, null, 10000.0)
        assertEquals("STRUCTURAL", structural.maxLossSource)
        assertEquals(BOOK_WIDTH_WIDE, structural.status)
        val none = assessBookWidth(0.0, 3000.0, null, null)
        assertEquals(BOOK_WIDTH_UNMEASURABLE, none.status)
        assertEquals(BOOK_WIDTH_UNMEASURABLE, assessBookWidth(0.0, null, 10000.0, null).status)
        val json = at.toJson()
        assertEquals(WIDE_EXECUTABLE_BOOK_CONTRACT, json.getString("contract"))
        assertEquals(0.20, json.getDouble("max_gap_fraction_of_max_loss"), 0.0)
        assertEquals(2000.0, json.getDouble("threshold_rupees"), 1e-9)
    }

    @Test
    fun markTrustJsonCarriesTheBookWidthValueUsed() {
        val p = paper(legs274, quotes274At0915, 850.1, 25503.0, 10497.0, sl274, tp274)
        val bw = p.trust.toJson().getJSONObject("book_width")
        assertEquals("WIDE", bw.getString("status"))
        assertEquals(WIDE_EXECUTABLE_BOOK_MAX_GAP_FRACTION_OF_MAX_LOSS, bw.getDouble("max_gap_fraction_of_max_loss"), 0.0)
        assertEquals(2099.4, bw.getDouble("threshold_rupees"), 0.01)
        assertEquals(0.6958, bw.getDouble("gap_fraction_of_max_loss"), 0.0005)
        assertEquals(MARK_TRUST_CONTRACT, p.trust.toJson().getString("contract"))
    }

    /**
     * Replay fixture: every stored position_ticks row since 10 Sep 2026 whose
     * executable-to-mid gap exceeded 3% of max loss (20 of 10,351 valued ticks,
     * read-only SELECT 26 Sep 2026). Columns: id, current_pnl, executable_mark,
     * mid_mark, qty, max_loss_ref. All are credit iron butterflies/condors.
     */
    private val replay = listOf(
        doubleArrayOf(25837.0, 64.5, 847.95, 831.725, 30.0, 10497.0),
        doubleArrayOf(25839.0, -4327.5, 994.35, 650.175, 30.0, 10497.0),
        doubleArrayOf(26651.0, -864.0, 892.95, 881.575, 30.0, 10076.0),
        doubleArrayOf(27067.0, -1348.5, 909.1, 899.0, 30.0, 10076.0),
        doubleArrayOf(27073.0, -709.5, 873.75, 857.8, 30.0, 10497.0),
        doubleArrayOf(27071.0, -1728.0, 921.75, 897.775, 30.0, 10076.0),
        doubleArrayOf(27075.0, -22092.0, 1600.55, 747.75, 30.0, 10076.0),
        doubleArrayOf(27077.0, -3573.0, 969.2, 842.425, 30.0, 10497.0),
        doubleArrayOf(27076.0, -4426.5, 242.4, 238.175, 65.0, 8170.0),
        doubleArrayOf(27871.0, -123.0, 854.2, 839.725, 30.0, 10497.0),
        doubleArrayOf(27870.0, 571.5, 845.1, 833.0, 30.0, 10076.0),
        doubleArrayOf(27872.0, -5050.5, 1032.5, 799.575, 30.0, 10076.0),
        doubleArrayOf(27873.0, -7563.0, 1102.2, 858.725, 30.0, 10497.0),
        doubleArrayOf(28838.0, -4092.0, 986.5, 851.9, 30.0, 10497.0),
        doubleArrayOf(28837.0, -376.5, 876.7, 828.3, 30.0, 10076.0),
        doubleArrayOf(31282.0, 657.0, 772.4, 753.025, 30.0, 12171.0),
        doubleArrayOf(31286.0, 283.5, 784.85, 751.925, 30.0, 12171.0),
        doubleArrayOf(34504.0, 60.0, 472.25, 464.175, 30.0, 6772.0),
        doubleArrayOf(34506.0, 997.5, 441.0, 417.85, 30.0, 6772.0),
        doubleArrayOf(34507.0, 981.0, 507.65, 486.775, 30.0, 10790.0)
    )

    @Test
    fun replayOfStoredWideTicksFlagsExactlyTheSixOpeningArtifacts() {
        val flagged = replay.filter { r ->
            val mid = r[1] + (r[2] - r[3]) * r[4]          // credit: mid P&L = exec P&L + (exec - mid mark) x qty
            assessBookWidth(r[1], mid, r[5], null).status == BOOK_WIDTH_WIDE
        }.map { it[0].toLong() }.toSet()
        assertEquals(setOf(25839L, 27075L, 27077L, 27872L, 27873L, 28838L), flagged)
    }

    private fun fallbackRow(exec: Double, mid: Double, cause: String) = JSONObject()
        .put("current_pnl", exec)
        .put("policy_reason", "stop_basis=MID_FALLBACK_WIDE_BOOK: mid_pnl <= sl_threshold")
        .put("policy_trace_json", JSONObject()
            .put("mark_trust", JSONObject().put("price_gate_applied", true))
            .put("stop_basis", STOP_BASIS_MID_FALLBACK_WIDE_BOOK)
            .put("mid_fallback_stop", true)
            .put("mid_fallback_pnl", mid)
            .put("price_policy_untrusted_cause", cause))
}
