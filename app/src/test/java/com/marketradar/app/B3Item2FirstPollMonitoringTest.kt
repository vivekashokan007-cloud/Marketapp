package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.time.LocalDate

/**
 * B3 item 2 (2026-09-26): the 60-second tick path monitors a position from its
 * first usable mark (no poll/Brain warmup), and its per-position state behaves
 * correctly for overnight positions, restarts and two trades on the same index.
 */
class B3Item2FirstPollMonitoringTest {
    private val now = 1_790_000_000_000L

    // ------------------------------------------------------------ fixtures

    private fun leg(key: String, side: String, type: String, strike: Double) = PositionLeg(
        instrumentKey = key,
        side = side,
        closeSide = if (side == "SHORT") CloseSide.BUY_TO_CLOSE else CloseSide.SELL_TO_CLOSE,
        optionType = type,
        strike = strike
    )

    // Two BNF iron butterflies that share the long 55000 PE wing instrument.
    private val legsA = listOf(
        leg("NSE_FO|A_CE_S", "SHORT", "CE", 56200.0),
        leg("NSE_FO|A_CE_L", "LONG", "CE", 57400.0),
        leg("NSE_FO|A_PE_S", "SHORT", "PE", 56200.0),
        leg("NSE_FO|SHARED_PE_L", "LONG", "PE", 55000.0)
    )
    private val legsB = listOf(
        leg("NSE_FO|B_CE_S", "SHORT", "CE", 56000.0),
        leg("NSE_FO|B_CE_L", "LONG", "CE", 57000.0),
        leg("NSE_FO|B_PE_S", "SHORT", "PE", 56000.0),
        leg("NSE_FO|SHARED_PE_L", "LONG", "PE", 55000.0)
    )
    private val quotes = mapOf(
        "NSE_FO|A_CE_S" to LegQuote(455.0, 460.0, 457.0),
        "NSE_FO|A_CE_L" to LegQuote(98.0, 100.0, 99.0),
        "NSE_FO|A_PE_S" to LegQuote(740.0, 745.0, 742.0),
        "NSE_FO|SHARED_PE_L" to LegQuote(225.0, 228.0, 226.0),
        "NSE_FO|B_CE_S" to LegQuote(520.0, 524.0, 522.0),
        "NSE_FO|B_CE_L" to LegQuote(140.0, 143.0, 141.0),
        "NSE_FO|B_PE_S" to LegQuote(640.0, 645.0, 642.0)
    )
    private val receipt = "2026-09-15T03:46:00.000Z" // Tue 09:16:00 IST

    private fun sources(ts: (String) -> String? = { "2026-09-15T09:15:59.4+05:30" }) =
        quotes.keys.associateWith { k ->
            val q = quotes.getValue(k)
            LegSourceQuote(k, true, true, "RESP:$k", q.bid, q.ask, ts(k), null)
        }

    private fun validity(legs: List<PositionLeg>, src: Map<String, LegSourceQuote>, receiptTs: String = receipt): PositionQuoteValidity {
        val tradeQuotes = legs.associate { l -> val k = l.instrumentKey!!; k to quotes.getValue(k) }
        val v = valuePositionTick("IRON_BUTTERFLY", legs, tradeQuotes, true, 850.0, null, null, 30.0)
        return validatePositionQuotes(
            legs.map { it.instrumentKey }, v.structure.status,
            v.legValuations.associate { it.leg.instrumentKey!! to it.quoteStatus },
            src, receiptTs, LocalDate.parse("2026-09-30"), 30.0, true, "OK"
        )
    }

    private fun row(tradeId: String, pnl: Double, trust: JSONObject) = JSONObject().apply {
        put("trade_id", tradeId)
        put("tick_ts", "2026-09-28T04:00:00.000Z")
        put("valuation_quality", "OK")
        put("policy_action", "HOLD")
        put("policy_reason", "no shadow exit rule matched")
        put("source", "P1_REST_60S")
        put("current_pnl", pnl)
        put("executable_mark", 700.0)
        put("leg_count", 4)
        put("mark_basis", "EXECUTABLE")
        put("auth_source", "DAILY")
        put("index_key", "BNF")
        put("strategy_type", "IRON_BUTTERFLY")
        put("legs_json", JSONArray())
        put("policy_trace_json", JSONObject().put("mark_store_trust", trust))
    }

    private fun trust(state: String, srcMs: Long, fp: String = "fp1", cause: String? = null) = JSONObject().apply {
        put("paper", true)
        put("trust_state", state)
        put("trust_cause", cause ?: JSONObject.NULL)
        put("quote_validity_state", if (state == TRUST_TRUSTED) QV_VALID else QV_INVALID)
        put("earliest_source_ms", srcMs)
        put("earliest_source_ts", "2026-09-28T09:30:00+05:30")
        put("latest_source_ms", srcMs)
        put("book_fingerprint", fp)
    }

    private fun present(prefs: FakePrefs, id: String, at: Long) =
        JSONObject(PositionMarkStore.presentationJson(prefs, at)).getJSONObject(id)

    private fun restart(prefs: FakePrefs): FakePrefs = FakePrefs().also { it.map.putAll(prefs.map) }

    // ------------------------------------------------------------ first poll

    @Test
    fun firstTrustedTickIsLiveImmediately_noWarmupRequired() {
        val prefs = FakePrefs()
        PositionMarkStore.recordRows(prefs, JSONArray().put(row("284", -1596.0, trust(TRUST_TRUSTED, now - 1_000L))), now)
        val p = present(prefs, "284", now)
        assertEquals(PositionMarkStore.LIVE_FULL, p.getString("display_state"))
        assertEquals(-1596.0, p.getDouble("last_valid_current_pnl"), 0.0)
    }

    @Test
    fun tickServiceHasNoPollOrBrainWarmupGate() {
        val src = File("src/main/java/com/marketradar/app/PositionTickService.kt").readText()
        val capture = src.substringAfter("private fun captureOnce(): Boolean {").substringBefore("/** Capture one fresh")
        for (forbidden in listOf("poll_count", "poll_history", "last_brain_ms", "polls.size", "brainResult")) {
            assertFalse("captureOnce must not gate on $forbidden", capture.contains(forbidden))
        }
        assertFalse(src.contains("09:17"))
    }

    // ------------------------------------------------------------ overnight

    @Test
    fun overnightMarkFromPreviousSessionIsNotLiveAtTodaysFirstTick() {
        val prefs = FakePrefs()
        val yesterday = now - 18L * 3600 * 1000
        PositionMarkStore.recordRows(prefs, JSONArray().put(row("284", 2500.0, trust(TRUST_TRUSTED, yesterday - 1_000L))), yesterday)
        val before = present(prefs, "284", now)
        assertEquals(PositionMarkStore.STALE_LAST_VALID, before.getString("display_state"))
        assertFalse(before.getBoolean("display_is_actionable"))
        // Today's first trusted tick makes it live at once.
        PositionMarkStore.recordRows(prefs, JSONArray().put(row("284", 2600.0, trust(TRUST_TRUSTED, now - 800L, fp = "fp2"))), now)
        val after = present(prefs, "284", now)
        assertEquals(PositionMarkStore.LIVE_FULL, after.getString("display_state"))
        assertEquals(2600.0, after.getDouble("last_valid_current_pnl"), 0.0)
    }

    @Test
    fun previousSessionSourceTimeInvalidatesTodaysFirstQuote() {
        // Vendor returns yesterday's last quote for a leg at today's first fetch.
        val v = validity(legsA, sources { k ->
            if (k == "NSE_FO|A_CE_S") "2026-09-14T15:29:59+05:30" else "2026-09-15T09:15:59+05:30"
        })
        assertEquals(QV_INVALID, v.state)
        assertFalse(v.legFor("NSE_FO|A_CE_S")!!.ok)
        assertEquals(3, v.legs.count { it.ok })
    }

    // ------------------------------------------------------------ restart

    @Test
    fun markStoreAndRecoveryStateSurviveRestart() {
        val prefs = FakePrefs()
        PositionMarkStore.recordRows(prefs, JSONArray().put(row("274", -7000.0, trust(TRUST_TRUSTED, now - 2_000L))), now - 1_000L)
        PositionMarkStore.recordRows(prefs, JSONArray().put(
            row("274", -22000.0, trust(TRUST_UNTRUSTED, now - 500L, fp = "fpX", cause = CAUSE_WIDE_LIQUIDATION_BOOK))), now)
        val restarted = restart(prefs)
        assertEquals(PositionMarkStore.presentationJson(prefs, now), PositionMarkStore.presentationJson(restarted, now))
        assertEquals(now - 500L to "fpX", PositionMarkStore.trustRecoveryState(restarted, "274"))
        val p = present(restarted, "274", now)
        assertEquals(PositionMarkStore.STALE_LAST_VALID, p.getString("display_state"))
        assertTrue(p.getBoolean("untrusted_pending_revalidation"))
    }

    @Test
    fun deliveryLedgerFailedAttemptStaysRetryableAcrossRestart() {
        var ledger = recordShadowDeliveryAttempt(JSONObject(), "284", "SHADOW_SL", "2026-09-15", true,
            DELIVERY_PERMISSION_DENIED, "PERMISSION_DENIED", "", consumed = false, nowMs = now)
        ledger = JSONObject(ledger.toString()) // process restart: reload from prefs
        val id = shadowDeliveryEventId("284", "SHADOW_SL", "2026-09-15")
        assertTrue(ledger.getJSONObject(id).getBoolean("retry_pending"))
        assertFalse(ledger.getJSONObject(id).getBoolean("posted_to_os"))
        ledger = recordShadowDeliveryAttempt(ledger, "284", "SHADOW_SL", "2026-09-15", true,
            DELIVERY_POSTED, "POSTED_TO_OS", "", consumed = shadowAlertAttemptConsumes(DELIVERY_POSTED), nowMs = now + 60_000L)
        ledger = JSONObject(ledger.toString())
        val e = ledger.getJSONObject(id)
        assertEquals(2, e.getInt("attempts"))
        assertEquals(now, e.getLong("first_decided_at_ms"))
        assertTrue(e.getBoolean("consumed"))
        assertFalse(e.getBoolean("retry_pending"))
    }

    // ------------------------------------------------------------ two trades, same index

    @Test
    fun twoSameIndexTradesSharingALegAreBothValid_noFalseDuplicate() {
        val src = sources()
        val a = validity(legsA, src)
        val b = validity(legsB, src)
        assertEquals(QV_VALID, a.state)
        assertEquals(QV_VALID, b.state)
        assertFalse(a.reasons.any { it.contains("duplicate_source_quote") })
        assertFalse(b.reasons.any { it.contains("duplicate_source_quote") })
        assertNotEquals(a.bookFingerprint, b.bookFingerprint)
    }

    @Test
    fun staleLegUniqueToOneTradeDoesNotInvalidateSibling_sharedStaleLegInvalidatesBoth() {
        val uniqueStale = sources { k -> if (k == "NSE_FO|A_PE_S") "2026-09-15T09:10:00+05:30" else "2026-09-15T09:15:59+05:30" }
        assertEquals(QV_INVALID, validity(legsA, uniqueStale).state)
        assertEquals(QV_VALID, validity(legsB, uniqueStale).state)
        val sharedStale = sources { k -> if (k == "NSE_FO|SHARED_PE_L") "2026-09-15T09:10:00+05:30" else "2026-09-15T09:15:59+05:30" }
        val a = validity(legsA, sharedStale)
        val b = validity(legsB, sharedStale)
        assertEquals(QV_INVALID, a.state)
        assertEquals(QV_INVALID, b.state)
        assertFalse(a.legFor("NSE_FO|SHARED_PE_L")!!.ok)
        assertFalse(b.legFor("NSE_FO|SHARED_PE_L")!!.ok)
    }

    @Test
    fun twoSameIndexMarksAreIsolatedInStoreAndLedger() {
        val prefs = FakePrefs()
        PositionMarkStore.recordRows(prefs, JSONArray()
            .put(row("284", -22000.0, trust(TRUST_UNTRUSTED, now - 1_000L, cause = CAUSE_BOUND_REFERENCE_MISMATCH)))
            .put(row("285", 1200.0, trust(TRUST_TRUSTED, now - 1_000L, fp = "fpB"))), now)
        assertEquals(PositionMarkStore.UNAVAILABLE, present(prefs, "284", now).getString("display_state"))
        assertEquals(PositionMarkStore.LIVE_FULL, present(prefs, "285", now).getString("display_state"))
        assertEquals(null to null, PositionMarkStore.trustRecoveryState(prefs, "285"))
        var ledger = recordShadowDeliveryAttempt(JSONObject(), "284", "SHADOW_SL", "2026-09-15", true,
            DELIVERY_THROTTLED, "THROTTLED", "", consumed = false, nowMs = now)
        ledger = recordShadowDeliveryAttempt(ledger, "285", "SHADOW_SL", "2026-09-15", true,
            DELIVERY_POSTED, "POSTED_TO_OS", "", consumed = true, nowMs = now)
        assertTrue(ledger.getJSONObject(shadowDeliveryEventId("284", "SHADOW_SL", "2026-09-15")).getBoolean("retry_pending"))
        assertFalse(ledger.getJSONObject(shadowDeliveryEventId("285", "SHADOW_SL", "2026-09-15")).getBoolean("retry_pending"))
        val pruned = pruneShadowDeliveryLedger(ledger, setOf("285"))
        assertEquals(1, pruned.length())
    }
}
