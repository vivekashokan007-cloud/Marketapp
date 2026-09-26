package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.time.LocalDate

/**
 * B3 item 3 (2026-09-26), Paper only: one capped re-fetch of only the invalid
 * legs, and one escalation notice per persistent mark-failure episode.
 */
class B3Item3RefreshEscalationTest {
    private fun leg(key: String, side: String, type: String, strike: Double) = PositionLeg(
        instrumentKey = key,
        side = side,
        closeSide = if (side == "SHORT") CloseSide.BUY_TO_CLOSE else CloseSide.SELL_TO_CLOSE,
        optionType = type,
        strike = strike
    )

    private val legsA = listOf(
        leg("K_A_CE_S", "SHORT", "CE", 56200.0),
        leg("K_A_CE_L", "LONG", "CE", 57400.0),
        leg("K_A_PE_S", "SHORT", "PE", 56200.0),
        leg("K_SHARED", "LONG", "PE", 55000.0)
    )
    private val legsB = listOf(
        leg("K_B_CE_S", "SHORT", "CE", 56000.0),
        leg("K_B_CE_L", "LONG", "CE", 57000.0),
        leg("K_B_PE_S", "SHORT", "PE", 56000.0),
        leg("K_SHARED", "LONG", "PE", 55000.0)
    )
    private val book = mapOf(
        "K_A_CE_S" to LegQuote(455.0, 460.0, 457.0),
        "K_A_CE_L" to LegQuote(98.0, 100.0, 99.0),
        "K_A_PE_S" to LegQuote(740.0, 745.0, 742.0),
        "K_SHARED" to LegQuote(225.0, 228.0, 226.0),
        "K_B_CE_S" to LegQuote(520.0, 524.0, 522.0),
        "K_B_CE_L" to LegQuote(140.0, 143.0, 141.0),
        "K_B_PE_S" to LegQuote(640.0, 645.0, 642.0)
    )
    private val receipt = "2026-09-15T03:46:00.000Z" // Tue 09:16:00 IST
    private val fresh = "2026-09-15T09:15:59.4+05:30"

    private fun src(
        quotes: Map<String, LegQuote> = book,
        ts: (String) -> String? = { fresh },
        exact: (String) -> Boolean = { true }
    ) = quotes.keys.associateWith { k ->
        val q = quotes.getValue(k)
        LegSourceQuote(k, true, exact(k), "RESP:$k", q.bid, q.ask, ts(k), null)
    }

    private fun validity(
        legs: List<PositionLeg>,
        source: Map<String, LegSourceQuote>,
        quotes: Map<String, LegQuote> = book,
        fetchStatus: String = "OK",
        expiry: LocalDate = LocalDate.parse("2026-09-30"),
        authoritative: Boolean = true
    ): PositionQuoteValidity {
        val tq = legs.mapNotNull { l -> val k = l.instrumentKey!!; quotes[k]?.let { k to it } }.toMap()
        val v = valuePositionTick("IRON_BUTTERFLY", legs, tq, true, 850.0, null, null, 30.0)
        return validatePositionQuotes(
            legs.map { it.instrumentKey }, v.structure.status,
            v.legValuations.associate { it.leg.instrumentKey!! to it.quoteStatus },
            source.filterKeys { k -> tq.containsKey(k) }, receipt, expiry, 30.0, authoritative, fetchStatus
        )
    }

    // ------------------------------------------------------------ refreshable legs

    @Test
    fun validMarkNeedsNoRefresh() {
        assertEquals(emptyList<String>(), refreshableInvalidLegKeys(validity(legsA, src())))
    }

    @Test
    fun onlyTheStaleLegIsRefreshed() {
        val v = validity(legsA, src(ts = { k -> if (k == "K_A_PE_S") "2026-09-15T09:12:00+05:30" else fresh }))
        assertEquals(QV_INVALID, v.state)
        assertEquals(listOf("K_A_PE_S"), refreshableInvalidLegKeys(v))
    }

    @Test
    fun missingSourceTimeCrossedAndMissingQuotesAreRefreshable() {
        val crossed = book + ("K_A_CE_L" to LegQuote(101.0, 99.0, 100.0))
        val noQuote = book - "K_A_CE_S"
        val v = validity(legsA, src(quotes = crossed, ts = { k -> if (k == "K_SHARED") null else fresh }), quotes = noQuote + ("K_A_CE_L" to LegQuote(101.0, 99.0, 100.0)))
        val keys = refreshableInvalidLegKeys(v).toSet()
        assertEquals(setOf("K_A_CE_S", "K_A_CE_L", "K_SHARED"), keys)
    }

    @Test
    fun nonCurableProblemsSkipTheRefreshEntirely() {
        val inexact = validity(legsA, src(ts = { k -> if (k == "K_A_PE_S") "2026-09-15T09:12:00+05:30" else fresh },
            exact = { k -> k != "K_A_CE_S" }))
        assertEquals(emptyList<String>(), refreshableInvalidLegKeys(inexact))
        val expired = validity(legsA, src(ts = { "2026-09-15T09:12:00+05:30" }), expiry = LocalDate.parse("2026-09-14"))
        assertEquals(emptyList<String>(), refreshableInvalidLegKeys(expired))
        val qty = validity(legsA, src(ts = { "2026-09-15T09:12:00+05:30" }), authoritative = false)
        assertEquals(emptyList<String>(), refreshableInvalidLegKeys(qty))
    }

    @Test
    fun authFailuresAreNotRefreshedButTransientFailuresRefreshAllLegs() {
        for (status in listOf("AUTH_REJECTED_401", "NO_TOKEN", "HTTP_400")) {
            val v = validity(legsA, emptyMap(), quotes = emptyMap(), fetchStatus = status)
            assertEquals(status, emptyList<String>(), refreshableInvalidLegKeys(v))
        }
        for (status in listOf("HTTP_503", "HTTP_429", "EXCEPTION_SocketTimeoutException")) {
            val v = validity(legsA, emptyMap(), quotes = emptyMap(), fetchStatus = status)
            assertEquals(status, legsA.map { it.instrumentKey }, refreshableInvalidLegKeys(v))
        }
    }

    @Test
    fun interLegSkewRefreshesOnlyTheOlderLegs() {
        val v = validity(legsA, src(ts = { k -> if (k == "K_A_CE_L") "2026-09-15T09:14:50+05:30" else fresh }))
        assertTrue(v.reasons.any { it.startsWith("inter_leg_skew:") })
        assertEquals(listOf("K_A_CE_L"), refreshableInvalidLegKeys(v))
    }

    // ------------------------------------------------------------ plan and merge

    @Test
    fun planDedupesSharedLegAcrossSameIndexTradesAndCaps() {
        val plan = planQuoteRefresh(linkedMapOf("284" to listOf("K_SHARED", "K_A_PE_S"), "285" to listOf("K_SHARED")))
        assertEquals(listOf("K_SHARED", "K_A_PE_S"), plan.keys)
        assertEquals(listOf("K_SHARED"), plan.requestedByTrade["285"])
        assertFalse(plan.truncated)
        val many = (1..30).associate { "t$it" to listOf("K$it") }
        val capped = planQuoteRefresh(many)
        assertEquals(QUOTE_REFRESH_MAX_KEYS, capped.keys.size)
        assertTrue(capped.truncated)
        assertEquals(QUOTE_REFRESH_MAX_KEYS, capped.requestedByTrade.size)
        assertEquals(1, QUOTE_REFRESH_MAX_REQUESTS_PER_TICK)
        assertEquals("NO_REFRESHABLE_INVALID_LEGS", planQuoteRefresh(emptyMap()).skipReason)
    }

    @Test
    fun mergeReplacesOnlyRequestedExactKeys() {
        val original = mapOf("K1" to "old1", "K2" to "old2", "REAL_ONLY" to "realQ")
        val refreshed = mapOf("K1" to "new1", "K2" to "new2fuzzy", "REAL_ONLY" to "realNew")
        val (q, exact, replaced) = mergeRefreshedLegQuotes(original, setOf("K2", "REAL_ONLY"), refreshed, setOf("K1", "REAL_ONLY"), listOf("K1", "K2"))
        assertEquals("new1", q["K1"])
        assertEquals("old2", q["K2"]) // refresh matched K2 only fuzzily → keep original
        assertEquals("realQ", q["REAL_ONLY"]) // not requested → untouched
        assertEquals(listOf("K1"), replaced)
        assertTrue(exact.containsAll(setOf("K1", "K2", "REAL_ONLY")))
    }

    @Test
    fun mergedFetchStatusCuresTransientFailureOnlyWhenAllTradeKeysReplaced() {
        assertEquals("OK", mergedFetchStatus("OK", null, listOf("a"), emptyList()))
        assertEquals("OK", mergedFetchStatus("HTTP_503", "OK", listOf("a", "b"), listOf("a", "b")))
        assertEquals("HTTP_503", mergedFetchStatus("HTTP_503", "OK", listOf("a", "b"), listOf("a")))
        assertEquals("HTTP_503", mergedFetchStatus("HTTP_503", "HTTP_503", listOf("a"), listOf("a")))
    }

    @Test
    fun refreshedFreshLegRevalidates_refreshedStaleLegStaysInvalid() {
        val stale = src(ts = { k -> if (k == "K_A_PE_S") "2026-09-15T09:12:00+05:30" else fresh })
        assertEquals(QV_INVALID, validity(legsA, stale).state)
        val cured = stale + ("K_A_PE_S" to stale.getValue("K_A_PE_S").copy(sourceTs = "2026-09-15T09:15:59.9+05:30"))
        assertEquals(QV_VALID, validity(legsA, cured).state)
        // A fresh HTTP receipt of the same stale vendor quote does not restore validity.
        val same = stale + ("K_A_PE_S" to stale.getValue("K_A_PE_S").copy(responseKey = "RESP2:K_A_PE_S"))
        assertEquals(QV_INVALID, validity(legsA, same).state)
    }

    @Test
    fun refreshCannotBypassRecoveryRuleAfterAnUntrustedTick() {
        val v = validity(legsA, src())
        val trusted = MarkTrust(TRUST_TRUSTED, null, false, false, "OK", null, null, emptyList())
        val r = applyTrustRecovery(trusted, v, v.latestSourceMs, v.bookFingerprint)
        assertEquals(TRUST_UNTRUSTED, r.state)
        assertEquals(CAUSE_AWAITING_REVALIDATION, r.cause)
    }

    // ------------------------------------------------------------ escalation

    private val t0 = 1_790_000_000_000L
    private fun run(ticks: List<Pair<Long, Boolean>>, start: JSONObject? = null, session: String = "2026-09-15"): List<MarkFailureStep> {
        var s = start
        return ticks.map { (at, untrusted) ->
            advanceMarkFailureEpisode(s, "284", session, untrusted, CAUSE_QUOTE_INVALIDITY, at).also { s = it.state }
        }
    }

    @Test
    fun escalatesOnceAfterFiveTicksAndFourMinutes() {
        val steps = run((0 until 5).map { t0 + it * 60_000L to true })
        assertEquals(listOf(false, false, false, false, true), steps.map { it.shouldEscalate })
        val posted = recordMarkFailureEscalationAttempt(steps.last().state!!, DELIVERY_POSTED, t0 + 240_000L)
        val next = run(listOf(t0 + 300_000L to true, t0 + 360_000L to true), posted)
        assertFalse(next.any { it.shouldEscalate })
        assertEquals(7, next.last().state!!.getInt("consecutive_ticks"))
    }

    @Test
    fun fiveQuickTicksDoNotEscalateBeforeFourMinutesObserved() {
        val steps = run((0 until 6).map { t0 + it * 20_000L to true })
        assertFalse(steps.any { it.shouldEscalate })
    }

    @Test
    fun fourMinutesObservedButFewerThanFiveTicksDoesNotEscalate() {
        val steps = run((0 until 4).map { t0 + it * 110_000L to true })
        assertTrue(steps.last().state!!.getLong("observed_failure_ms") >= MARK_FAILURE_ESCALATE_AFTER_MS)
        assertFalse(steps.any { it.shouldEscalate })
    }

    @Test
    fun failedPostStaysRetryableThenPostsOnce() {
        val due = run((0 until 5).map { t0 + it * 60_000L to true }).last()
        var s = recordMarkFailureEscalationAttempt(due.state!!, DELIVERY_PERMISSION_DENIED, t0)
        assertTrue(s.getBoolean("escalation_retry_pending"))
        s = JSONObject(s.toString()) // restart
        val retry = advanceMarkFailureEpisode(s, "284", "2026-09-15", true, CAUSE_QUOTE_INVALIDITY, t0 + 300_000L)
        assertTrue(retry.shouldEscalate)
        val posted = recordMarkFailureEscalationAttempt(retry.state!!, DELIVERY_POSTED, t0 + 300_000L)
        assertEquals(2, posted.getInt("escalation_attempts"))
        assertEquals("UNKNOWN_OS_POST_IS_NOT_USER_ACK", posted.getString("user_saw_notification"))
        assertFalse(advanceMarkFailureEpisode(posted, "284", "2026-09-15", true, null, t0 + 360_000L).shouldEscalate)
    }

    @Test
    fun trustedTickClosesEpisodeAndANewEpisodeCanEscalateAgain() {
        val first = run((0 until 5).map { t0 + it * 60_000L to true })
        val posted = recordMarkFailureEscalationAttempt(first.last().state!!, DELIVERY_POSTED, t0)
        val close = advanceMarkFailureEpisode(posted, "284", "2026-09-15", false, null, t0 + 300_000L)
        assertNull(close.state)
        assertTrue(close.episodeClosed)
        val second = run((6 until 11).map { t0 + it * 60_000L to true })
        assertTrue(second.last().shouldEscalate)
        assertNotEquals(posted.getString("episode_id"), second.last().state!!.getString("episode_id"))
    }

    @Test
    fun flappingNeverEscalates() {
        val steps = run((0 until 20).map { t0 + it * 60_000L to (it % 3 != 2) })
        assertFalse(steps.any { it.shouldEscalate })
    }

    @Test
    fun serviceGapIsCappedAndClockRollbackCountsZero() {
        val gap = run(listOf(t0 to true, t0 + 30 * 60_000L to true))
        assertEquals(MARK_FAILURE_MAX_COUNTED_GAP_MS, gap.last().state!!.getLong("observed_failure_ms"))
        val rollback = run(listOf(t0 to true, t0 - 600_000L to true))
        assertEquals(0L, rollback.last().state!!.getLong("observed_failure_ms"))
        assertEquals(t0, rollback.last().state!!.getLong("last_tick_ms"))
    }

    @Test
    fun overnightNewSessionStartsANewEpisode() {
        val y = run((0 until 3).map { t0 + it * 60_000L to true }, session = "2026-09-14").last().state
        val today = advanceMarkFailureEpisode(y, "284", "2026-09-15", true, null, t0 + 18 * 3_600_000L)
        assertEquals(1, today.state!!.getInt("consecutive_ticks"))
        assertEquals(0L, today.state?.getLong("observed_failure_ms"))
        assertEquals("2026-09-15", today.state?.getString("session_date"))
    }

    @Test
    fun episodesAreIsolatedPerTradeAndPrunedWhenClosed() {
        val a = advanceMarkFailureEpisode(null, "284", "2026-09-15", true, CAUSE_WIDE_LIQUIDATION_BOOK, t0).state!!
        val b = advanceMarkFailureEpisode(null, "285", "2026-09-15", false, null, t0)
        assertNull(b.state)
        val all = JSONObject().put("284", a).put("999", a)
        assertEquals(setOf("284"), pruneMarkFailureEpisodes(all, setOf("284", "285")).keys().asSequence().toSet())
    }

    @Test
    fun escalationTextIsAMonitoringNoticeNotAStopOrTargetInstruction() {
        val s = run((0 until 5).map { t0 + it * 60_000L to true }).last().state!!
        val (title, body) = markFailureEscalationText("BNF IRON_BUTTERFLY", s)
        assertEquals("🚨 Position Monitoring At Risk", title)
        assertTrue(body.contains("stop/target are not being evaluated"))
        for (forbidden in listOf("Cut position", "Book profit", "Square off", "HOLD", "safe")) {
            assertFalse(body.contains(forbidden))
        }
    }

    // ------------------------------------------------------------ Real parity (source contract)

    @Test
    fun serviceWiringIsPaperOnly() {
        val svc = File("src/main/java/com/marketradar/app/PositionTickService.kt").readText()
        val capture = svc.substringAfter("private fun captureOnce(): Boolean {").substringBefore("/** Capture one fresh")
        assertTrue(capture.contains("val refreshed = isPaperTrade && refresh != null"))
        assertTrue(capture.contains("buildTickRow(trade, sessionDate, requestStartedTs, valuationTs, quoteFetch)"))
        assertTrue(capture.contains("if (isPaperTrade) maybeEscalatePersistentMarkFailure(row, trade, sessionDate)"))
        val refresh = svc.substringAfter("private fun maybeRefreshPaperInvalidLegs(").substringBefore("private fun maybeEscalatePersistentMarkFailure")
        assertTrue(refresh.contains("trades.filter { it.optBoolean(\"paper\", false) }"))
        val esc = svc.substringAfter("private fun maybeEscalatePersistentMarkFailure(").substringBefore("private fun readMarkFailureEpisodes")
        assertTrue(esc.contains("if (!trade.optBoolean(\"paper\", false)) return"))
        // Escalation never writes the stop/target transition keys or Brain-owned alerts.
        assertFalse(esc.contains("SHADOW_LAST_ACTION_PREFIX"))
        assertFalse(esc.contains("POS_VERDICT") || esc.contains("POS_BOOK"))
        assertFalse(svc.contains("09:17"))
    }
}
