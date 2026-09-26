package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.LocalDate

/**
 * B3 + A1 (2026-09-26): executed tests of the shared quote validator, the A1 mark
 * trust classification, the recovery rule, trusted extrema and delivery
 * acknowledgement. Fixtures for trades 276 / 274 are the exact legs of the two
 * production SHADOW_SL ticks (15 and 16 Sep 2026, 09:15 IST, read-only SELECT).
 */
class B3QuoteValidityTrustTest {

    private fun leg(key: String, side: String, type: String, strike: Double) = PositionLeg(
        instrumentKey = key,
        side = side,
        closeSide = if (side == "SHORT") CloseSide.BUY_TO_CLOSE else CloseSide.SELL_TO_CLOSE,
        optionType = type,
        strike = strike
    )

    // Trade 276 — BNF IRON_BUTTERFLY, entry 864.15, qty 30, stored MP 25924 / ML 10076.
    private val legs276 = listOf(
        leg("NSE_FO|69795", "SHORT", "CE", 56200.0),
        leg("NSE_FO|69820", "LONG", "CE", 57400.0),
        leg("NSE_FO|69796", "SHORT", "PE", 56200.0),
        leg("NSE_FO|69768", "LONG", "PE", 55000.0)
    )
    private val quotes276 = mapOf(
        "NSE_FO|69795" to LegQuote(635.0, 1599.85, 1069.05),
        "NSE_FO|69820" to LegQuote(401.15, 679.85, 408.95),
        "NSE_FO|69796" to LegQuote(66.65, 509.95, 361.4),
        "NSE_FO|69768" to LegQuote(108.1, 126.85, 112.0)
    )

    // Trade 274 — BNF IRON_BUTTERFLY, entry 850.1, qty 30, stored MP 25503 / ML 10497.
    private val legs274 = listOf(
        leg("NSE_FO|69801", "SHORT", "CE", 56500.0),
        leg("NSE_FO|69826", "LONG", "CE", 57700.0),
        leg("NSE_FO|69802", "SHORT", "PE", 56500.0),
        leg("NSE_FO|69776", "LONG", "PE", 55300.0)
    )
    private val quotes274 = mapOf(
        "NSE_FO|69801" to LegQuote(454.95, 475.0, 459.75),
        "NSE_FO|69826" to LegQuote(97.75, 179.85, 128.6),
        "NSE_FO|69802" to LegQuote(740.0, 950.0, 855.6),
        "NSE_FO|69776" to LegQuote(225.05, 399.85, 355.1)
    )

    // Trade 274 at 16 Sep 2026 09:15:57 IST (production tick 27875, HOLD, exec +885,
    // mid +1,000.5, gap about 115 rupees): the normal book 57 s after the artifact.
    private val quotes274Normal = mapOf(
        "NSE_FO|69801" to LegQuote(522.85, 524.7, 523.05),
        "NSE_FO|69826" to LegQuote(148.95, 149.9, 149.0),
        "NSE_FO|69802" to LegQuote(709.95, 713.25, 708.3),
        "NSE_FO|69776" to LegQuote(268.4, 270.0, 269.1)
    )

    // Tuesday 15 Sep 2026 09:16:00 IST receipt.
    private val receipt = "2026-09-15T03:46:00.000Z"

    private fun sources(
        legs: List<PositionLeg>,
        quotes: Map<String, LegQuote>,
        ts: (String) -> String? = { "2026-09-15T09:15:59.4+05:30" },
        exact: (String) -> Boolean = { true },
        responseKey: (String) -> String = { "RESP:$it" }
    ): Map<String, LegSourceQuote> = legs.associate { l ->
        val k = l.instrumentKey!!
        val q = quotes[k]
        k to LegSourceQuote(k, q != null, exact(k), responseKey(k), q?.bid, q?.ask, ts(k), null)
    }

    private fun validity(
        legs: List<PositionLeg>,
        quotes: Map<String, LegQuote>,
        src: Map<String, LegSourceQuote> = sources(legs, quotes),
        receiptTs: String = receipt,
        strategy: String = "IRON_BUTTERFLY",
        expiry: LocalDate? = LocalDate.parse("2026-09-30"),
        qty: Double? = 30.0,
        authoritative: Boolean = true,
        fetchStatus: String = "OK"
    ): PositionQuoteValidity {
        val v = valuePositionTick(strategy, legs, quotes, true, 864.15, null, null, 30.0)
        return validatePositionQuotes(
            legs.map { it.instrumentKey }, v.structure.status,
            v.legValuations.associate { it.leg.instrumentKey!! to it.quoteStatus },
            src, receiptTs, expiry, qty, authoritative, fetchStatus
        )
    }

    private fun trustFor(
        legs: List<PositionLeg>, quotes: Map<String, LegQuote>, entry: Double, qty: Double,
        storedMp: Double, storedMl: Double, v: PositionQuoteValidity = validity(legs, quotes)
    ): Pair<TickValuation, MarkTrust> {
        val tv = valuePositionTick("IRON_BUTTERFLY", legs, quotes, true, entry, storedMp, storedMl, qty)
        val expected = expectedStructuralBounds("IRON_BUTTERFLY", legs, entry, true, qty)
        val t = classifyPositionMarkTrust(v, tv.valuationAccepted, tv.currentPnl, tv.midMark, entry, true, qty,
            storedMp, storedMl, expected)
        return tv to t
    }

    // ---------------------------------------------------------------- validity

    @Test
    fun allLegsFreshExactCompleteIsValid_mixedTimezoneOffsets() {
        val src = sources(legs276, quotes276, ts = { k ->
            if (k.endsWith("95")) "2026-09-15T03:45:59.400Z" else "2026-09-15T09:15:59.900+05:30"
        })
        val v = validity(legs276, quotes276, src)
        assertEquals(QV_VALID, v.state)
        assertTrue(v.reasons.isEmpty())
        assertEquals(600L, v.maxSourceAgeMs)
        assertEquals(500L, v.interLegSkewMs)
        assertEquals("2026-09-15T03:45:59.400Z", v.earliestSourceTs)
    }

    @Test
    fun staleQuoteInFreshHttpResponseIsInvalid() {
        // Receipt is fresh (09:16:00), but the vendor source time is 5 minutes old.
        val v = validity(legs276, quotes276, sources(legs276, quotes276, ts = { "2026-09-15T09:11:00+05:30" }))
        assertEquals(QV_INVALID, v.state)
        assertTrue(v.legs.all { lv -> lv.reasons.any { it.startsWith("source_stale:") } })
    }

    @Test
    fun oneStaleLegInvalidatesTheMarkAndIsNamed() {
        val v = validity(legs276, quotes276, sources(legs276, quotes276, ts = { k ->
            if (k == "NSE_FO|69796") "2026-09-15T09:13:00+05:30" else "2026-09-15T09:15:59+05:30"
        }))
        assertEquals(QV_INVALID, v.state)
        assertFalse(v.legFor("NSE_FO|69796")!!.ok)
        assertEquals(3, v.legs.count { it.ok })
        assertTrue(v.reasons.any { it.startsWith("leg:NSE_FO|69796:source_stale") })
    }

    @Test
    fun missingSourceTimeStaysExplicitAndIsNeverReplacedByReceipt() {
        val v = validity(legs276, quotes276, sources(legs276, quotes276, ts = { k ->
            if (k == "NSE_FO|69820") null else "2026-09-15T09:15:59+05:30"
        }))
        assertEquals(QV_INVALID, v.state)
        val lv = v.legFor("NSE_FO|69820")!!
        assertNull(lv.sourceTs)
        assertNull(lv.sourceAgeMs)
        assertTrue(lv.reasons.contains("source_ts_missing"))
    }

    @Test
    fun futureSourceTimeBeyondToleranceIsInvalid_withinToleranceIsAccepted() {
        val far = validity(legs276, quotes276, sources(legs276, quotes276, ts = { "2026-09-15T09:16:30+05:30" }))
        assertEquals(QV_INVALID, far.state)
        assertTrue(far.legs.all { lv -> lv.reasons.any { it.startsWith("source_ts_future:") } })
        val near = validity(legs276, quotes276, sources(legs276, quotes276, ts = { "2026-09-15T09:16:03+05:30" }))
        assertEquals(QV_VALID, near.state)
    }

    @Test
    fun crossedAndMissingBooksAreInvalid() {
        val crossed = quotes276 + ("NSE_FO|69795" to LegQuote(1700.0, 1599.85, 1069.05))
        val c = validity(legs276, crossed)
        assertTrue(c.legFor("NSE_FO|69795")!!.reasons.contains("book:CROSSED_QUOTE"))
        val noDepth = quotes276 + ("NSE_FO|69768" to LegQuote(null, 126.85, 112.0))
        val d = validity(legs276, noDepth)
        assertTrue(d.legFor("NSE_FO|69768")!!.reasons.contains("book:NO_DEPTH"))
        val missing = quotes276 - "NSE_FO|69820"
        val m = validity(legs276, missing, sources(legs276, missing))
        assertTrue(m.legFor("NSE_FO|69820")!!.reasons.contains("no_quote"))
        assertEquals(QV_INVALID, m.state)
    }

    @Test
    fun duplicateSourceQuoteAndInexactKeyMatchAreInvalid() {
        val src = sources(legs276, quotes276,
            exact = { it != "NSE_FO|69796" },
            responseKey = { if (it == "NSE_FO|69796") "RESP:NSE_FO|69795" else "RESP:$it" })
        val v = validity(legs276, quotes276, src)
        assertEquals(QV_INVALID, v.state)
        assertTrue(v.legFor("NSE_FO|69796")!!.reasons.contains("key_match_inexact"))
        assertTrue(v.legFor("NSE_FO|69796")!!.reasons.contains("duplicate_source_quote"))
        assertTrue(v.legFor("NSE_FO|69795")!!.reasons.contains("duplicate_source_quote"))
    }

    @Test
    fun afterCloseAndWeekendReceiptsAreNotActionableLiveQuotes() {
        val afterClose = validity(legs276, quotes276,
            sources(legs276, quotes276, ts = { "2026-09-15T15:34:59+05:30" }),
            receiptTs = "2026-09-15T15:35:00+05:30")
        assertTrue(afterClose.reasons.contains("receipt_outside_regular_session"))
        val saturday = validity(legs276, quotes276,
            sources(legs276, quotes276, ts = { "2026-09-19T10:00:00+05:30" }),
            receiptTs = "2026-09-19T10:00:01+05:30")
        assertTrue(saturday.reasons.contains("receipt_outside_regular_session"))
        assertEquals(QV_INVALID, saturday.state)
    }

    @Test
    fun expiredContractInterLegSkewQuantityAndFetchFailuresAreInvalid() {
        assertTrue(validity(legs276, quotes276, expiry = LocalDate.parse("2026-09-14"))
            .reasons.contains("contract_expired"))
        val skew = validity(legs276, quotes276, sources(legs276, quotes276, ts = { k ->
            if (k == "NSE_FO|69795") "2026-09-15T09:14:50+05:30" else "2026-09-15T09:15:59+05:30"
        }))
        assertTrue(skew.reasons.any { it.startsWith("inter_leg_skew:") })
        assertTrue(validity(legs276, quotes276, authoritative = false).reasons.contains("quantity_not_authoritative"))
        assertTrue(validity(legs276, quotes276, qty = null).reasons.contains("quantity_unresolved"))
        assertTrue(validity(legs276, quotes276, fetchStatus = "AUTH_REJECTED_401").reasons.contains("fetch:AUTH_REJECTED_401"))
    }

    // ------------------------------------------------------------------ A1 trust

    @Test
    fun structuralBoundsDeriveFromStrikesEntryAndQuantity() {
        val b = expectedStructuralBounds("IRON_BUTTERFLY", legs276, 864.15, true, 30.0)!!
        assertEquals(25924.5, b.maxProfit, 1e-6)
        assertEquals(10075.5, b.maxLoss, 1e-6)
        assertEquals(1200.0, b.width, 1e-9)
        assertNull(expectedStructuralBounds("IRON_BUTTERFLY", legs276, 1300.0, true, 30.0))
        assertNull(expectedStructuralBounds("IRON_BUTTERFLY", legs276, 864.15, true, null))
    }

    @Test
    fun trade276BoundedAnomalyWithValidQuotesIsUntrustedWideLiquidationBook() {
        // Original failing case: SHADOW_SL at 09:15 on a mark 2.2x beyond max loss.
        val (tv, t) = trustFor(legs276, quotes276, 864.15, 30.0, 25924.0, 10076.0)
        assertEquals(-22092.0, tv.currentPnl!!, 0.5)
        assertTrue(tv.boundAnomaly)
        assertEquals(TRUST_UNTRUSTED, t.state)
        assertEquals(CAUSE_WIDE_LIQUIDATION_BOOK, t.cause)
        assertEquals("CONSISTENT", t.boundReferenceStatus)
        assertTrue(t.midPnl!! > 0.0) // mid mark sits inside the envelope
    }

    @Test
    fun trade274InsideBoundsButWideExecutableBookIsUntrusted() {
        // B3.1 (replaces the pre-B3.1 assertion that this -7,563 mark was TRUSTED,
        // which let the false 16 Sep 09:15 SHADOW_SL through): inside the bounds,
        // valid quotes, but the executable-to-mid gap is 7,304 = 69.6% of max loss.
        val (tv, t) = trustFor(legs274, quotes274, 850.1, 30.0, 25503.0, 10497.0)
        assertEquals(-7563.0, tv.currentPnl!!, 0.5)
        assertFalse(tv.boundAnomaly)
        assertEquals(TRUST_UNTRUSTED, t.state)
        assertEquals(CAUSE_WIDE_EXECUTABLE_BOOK, t.cause)
        assertEquals(BOOK_WIDTH_WIDE, t.bookWidth!!.status)
        assertEquals(7304.25, t.bookWidth!!.gap!!, 0.5)
        assertEquals(10497.0 * WIDE_EXECUTABLE_BOOK_MAX_GAP_FRACTION_OF_MAX_LOSS, t.bookWidth!!.threshold!!, 1e-6)
    }

    @Test
    fun trade274NormalBookAt091557IsTrusted() {
        val (tv, t) = trustFor(legs274, quotes274Normal, 850.1, 30.0, 25503.0, 10497.0)
        assertEquals(885.0, tv.currentPnl!!, 0.5)
        assertEquals(TRUST_TRUSTED, t.state)
        assertNull(t.cause)
        assertEquals(BOOK_WIDTH_OK, t.bookWidth!!.status)
        assertTrue(t.bookWidth!!.gap!! < 150.0)
    }

    @Test
    fun wrongStoredBoundIsBoundReferenceMismatch() {
        // Stored max_loss was written for a smaller structure (wrong reference):
        // the executable P&L breaches it but sits inside the structure-derived bound.
        val (_, t) = trustFor(legs274, quotes274, 850.1, 30.0, 25503.0, 5000.0)
        assertEquals(TRUST_UNTRUSTED, t.state)
        assertEquals(CAUSE_BOUND_REFERENCE_MISMATCH, t.cause)
        assertEquals("MISMATCH", t.boundReferenceStatus)
    }

    @Test
    fun wrongQuantityIsBoundReferenceMismatch() {
        // Units doubled (60) against references written for 30 units.
        val (tv, t) = trustFor(legs274, quotes274, 850.1, 60.0, 25503.0, 10497.0)
        assertTrue(tv.boundAnomaly)
        assertEquals(TRUST_UNTRUSTED, t.state)
        assertEquals(CAUSE_BOUND_REFERENCE_MISMATCH, t.cause)
    }

    @Test
    fun invalidQuotesAreUntrustedQuoteInvalidityEvenInsideBounds() {
        val stale = validity(legs274, quotes274, sources(legs274, quotes274, ts = { "2026-09-15T09:10:00+05:30" }))
        val (_, t) = trustFor(legs274, quotes274, 850.1, 30.0, 25503.0, 10497.0, stale)
        assertEquals(TRUST_UNTRUSTED, t.state)
        assertEquals(CAUSE_QUOTE_INVALIDITY, t.cause)
        assertTrue(t.detail.isNotEmpty())
    }

    @Test
    fun recoveryRequiresSourceAdvanceAndChangedBook() {
        val v = validity(legs274, quotes274Normal)
        val (_, trusted) = trustFor(legs274, quotes274Normal, 850.1, 30.0, 25503.0, 10497.0, v)
        assertEquals(TRUST_TRUSTED, trusted.state)
        // Same source time as the untrusted mark (repeated receipt): not recovered.
        val sameSrc = applyTrustRecovery(trusted, v, v.latestSourceMs, "other-book")
        assertEquals(CAUSE_AWAITING_REVALIDATION, sameSrc.cause)
        assertTrue(sameSrc.detail.contains("source_time_not_advanced"))
        // Source advanced but identical book: fresh HTTP receipt of unchanged quote.
        val sameBook = applyTrustRecovery(trusted, v, v.earliestSourceMs!! - 60_000L, v.bookFingerprint)
        assertEquals(TRUST_UNTRUSTED, sameBook.state)
        assertTrue(sameBook.detail.contains("book_unchanged_since_untrusted"))
        // Both advanced and changed: recovered.
        val ok = applyTrustRecovery(trusted, v, v.earliestSourceMs!! - 60_000L, "older-book")
        assertEquals(TRUST_TRUSTED, ok.state)
        assertTrue(ok.detail.contains("revalidated_after_untrusted"))
        // No prior untrusted state: unchanged.
        assertEquals(trusted, applyTrustRecovery(trusted, v, null, null))
    }

    @Test
    fun anomalousMarkIsExcludedFromTrustedExtremaButRawExtremaStillMove() {
        var trusted = updateTrustedExtrema(null, null, -7563.0)
        assertEquals(-7563.0, trusted.first!!, 0.0)
        // Untrusted -22092 is passed as null to the trusted track.
        trusted = updateTrustedExtrema(trusted.first, trusted.second, null)
        assertEquals(-7563.0, trusted.first!!, 0.0)
        assertEquals(-7563.0, trusted.second!!, 0.0)
        trusted = updateTrustedExtrema(trusted.first, trusted.second, 1200.0)
        assertEquals(-7563.0, trusted.first!!, 0.0)
        assertEquals(1200.0, trusted.second!!, 0.0)
    }

    @Test
    fun legsJsonAnnotationAddsSourceCausesWithoutTouchingQuoteStatus() {
        val tv = valuePositionTick("IRON_BUTTERFLY", legs276, quotes276, true, 864.15, 25924.0, 10076.0, 30.0)
        val before = tv.legsJson().toString()
        val v = validity(legs276, quotes276, sources(legs276, quotes276, ts = { k ->
            if (k == "NSE_FO|69820") null else "2026-09-15T09:15:59+05:30"
        }))
        val annotated = annotateLegsWithSourceValidity(tv.legsJson(), v)
        val orig = JSONArray(before)
        for (i in 0 until annotated.length()) {
            val a = annotated.getJSONObject(i)
            assertEquals(orig.getJSONObject(i).getString("quote_status"), a.getString("quote_status"))
            assertTrue(a.has("source_ts"))
            assertTrue(a.has("source_validity"))
        }
        val missing = (0 until annotated.length()).map { annotated.getJSONObject(it) }
            .first { it.getString("instrument_key") == "NSE_FO|69820" }
        assertTrue(missing.isNull("source_ts"))
        assertEquals("source_ts_missing", missing.getString("source_validity"))
    }

    @Test
    fun paperTradeLotIdentityForLegacyLotSizeTrade() {
        // Documents what resolvePositionTickLotMeta yields for a 276-shaped Paper trade.
        val meta = resolvePositionTickLotMeta(JSONObject().apply {
            put("index_key", "BNF"); put("lot_size", 30); put("lots", 1)
            put("entry_date", "2026-09-14"); put("expiry", "2026-09-30"); put("paper", true)
        })
        assertNotNull(meta)
        assertEquals(30.0, meta!!.lotSize, 0.0)
        // Dated authority resolves for this shape, so A1 quantity validity passes.
        assertTrue(meta.authoritative)
    }

    // ------------------------------------------------------- delivery acknowledgement

    @Test
    fun deliveryOutcomesMapToFiveClasses() {
        assertEquals(DELIVERY_POSTED, classifyNotificationDelivery("POSTED_TO_OS", true))
        assertEquals(DELIVERY_PERMISSION_DENIED, classifyNotificationDelivery("PERMISSION_DENIED", false))
        assertEquals(DELIVERY_PERMISSION_DENIED, classifyNotificationDelivery("APP_NOTIFICATIONS_DISABLED", false))
        assertEquals(DELIVERY_CHANNEL_BLOCKED, classifyNotificationDelivery("CHANNEL_DISABLED", false))
        assertEquals(DELIVERY_THROTTLED, classifyNotificationDelivery("THROTTLED", false))
        assertEquals(DELIVERY_EXCEPTION, classifyNotificationDelivery("NOTIFY_FAILED", false))
        assertEquals(DELIVERY_EXCEPTION, classifyNotificationDelivery("MANAGER_UNAVAILABLE", false))
        // postedToOs=false can never be POSTED even with the posted label.
        assertEquals(DELIVERY_EXCEPTION, classifyNotificationDelivery("POSTED_TO_OS", false))
    }

    @Test
    fun paperConsumesOnlyOnPost_realKeepsLegacyConsumeOnSend() {
        listOf(DELIVERY_PERMISSION_DENIED, DELIVERY_CHANNEL_BLOCKED, DELIVERY_THROTTLED, DELIVERY_EXCEPTION)
            .forEach {
                assertFalse(shadowAlertAttemptConsumes(true, it))
                assertTrue(shadowAlertAttemptConsumes(false, it))
            }
        assertTrue(shadowAlertAttemptConsumes(true, DELIVERY_POSTED))
        assertTrue(shadowAlertAttemptConsumes(false, DELIVERY_POSTED))
    }

    @Test
    fun deniedThenPermissionRestoredRetriesAcrossRestartThenConsumes() {
        var ledgerBlob = "{}"
        fun attempt(cls: String, raw: String, now: Long): JSONObject {
            val consumed = shadowAlertAttemptConsumes(true, cls)
            // Serialize/parse each time: identical to SharedPreferences across restart.
            val next = recordShadowDeliveryAttempt(JSONObject(ledgerBlob), "281", "SHADOW_SL", "2026-09-28",
                true, cls, raw, "", consumed, now)
            ledgerBlob = next.toString()
            return next.getJSONObject(shadowDeliveryEventId("281", "SHADOW_SL", "2026-09-28"))
        }
        val e1 = attempt(DELIVERY_PERMISSION_DENIED, "PERMISSION_DENIED", 1_000L)
        assertTrue(e1.getBoolean("retry_pending"))
        assertFalse(e1.getBoolean("posted_to_os"))
        val e2 = attempt(DELIVERY_PERMISSION_DENIED, "PERMISSION_DENIED", 61_000L)
        assertEquals(2, e2.getInt("attempts"))
        assertTrue(e2.isNull("posted_at_ms"))
        val e3 = attempt(DELIVERY_POSTED, "POSTED_TO_OS", 121_000L)
        assertEquals(3, e3.getInt("attempts"))
        assertTrue(e3.getBoolean("consumed"))
        assertFalse(e3.getBoolean("retry_pending"))
        assertEquals(121_000L, e3.getLong("posted_at_ms"))
        assertEquals(1_000L, e3.getLong("first_decided_at_ms"))
        assertEquals("UNKNOWN_OS_POST_IS_NOT_USER_ACK", e3.getString("user_saw_notification"))
    }

    @Test
    fun twoSameIndexTradesHaveIndependentEventIdentities_andLedgerIsBounded() {
        var ledger = JSONObject()
        ledger = recordShadowDeliveryAttempt(ledger, "301", "SHADOW_SL", "2026-09-28", true,
            DELIVERY_POSTED, "POSTED_TO_OS", "", true, 10L)
        ledger = recordShadowDeliveryAttempt(ledger, "302", "SHADOW_SL", "2026-09-28", true,
            DELIVERY_PERMISSION_DENIED, "PERMISSION_DENIED", "", false, 11L)
        assertTrue(ledger.getJSONObject("301|SHADOW_SL|2026-09-28").getBoolean("consumed"))
        assertTrue(ledger.getJSONObject("302|SHADOW_SL|2026-09-28").getBoolean("retry_pending"))
        val pruned = pruneShadowDeliveryLedger(ledger, setOf("302"))
        assertEquals(1, pruned.length())
        var big = JSONObject()
        for (i in 0 until SHADOW_DELIVERY_LEDGER_MAX_ENTRIES + 25) {
            big = recordShadowDeliveryAttempt(big, "t$i", "SHADOW_TP", "2026-09-28", true,
                DELIVERY_POSTED, "POSTED_TO_OS", "", true, i.toLong())
        }
        assertEquals(SHADOW_DELIVERY_LEDGER_MAX_ENTRIES, big.length())
        assertFalse(big.has("t0|SHADOW_TP|2026-09-28"))
    }
}
