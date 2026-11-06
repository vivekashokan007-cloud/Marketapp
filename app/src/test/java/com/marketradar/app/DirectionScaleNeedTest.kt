package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * D9 repair, review round 2. Codex review of fb3b13f raised two defects in the Kotlin half:
 *
 *   A. a failed fetch could leave an earlier session's scale chain in the persisted brain context, so
 *      a later expiry-day poll could pair a NEW expiry with an OLD chain;
 *   B. the extra chain was fetched on every expiry-day poll (~78 requests) even after a usable scale
 *      had been frozen at the open reference and the brain had stopped reading the chain.
 */
class DirectionScaleNeedTest {
    private val TODAY = "2026-11-09"          // NF's own expiry day (Tue 10 Nov 2026 is an NSE holiday)
    private val NEXT = "2026-11-17"

    private fun state(vararg scales: Pair<String, JSONObject>): String {
        val s = JSONObject()
        for ((ix, sc) in scales) s.put(ix, sc)
        return JSONObject().put("scales", s).toString()
    }

    private fun ok(m: Double = 0.0095) =
        JSONObject().put("status", "OK").put("value", m).put("reason", JSONObject.NULL)

    private fun failed(reason: String) =
        JSONObject().put("status", "UNAVAILABLE").put("reason", reason)

    // ───────────────────────────── decision matrix ─────────────────────────────

    @Test fun ordinarySessionNeverFetches() {
        // front expiry still forward-dated: the trading chain prices the scale, as before the repair
        val d = DirectionScaleNeed.decide("NF", "2026-11-17", TODAY, null)
        assertFalse(d.fetch)
        assertEquals(DirectionScaleNeed.NOT_EXPIRY_DAY, d.reason)
    }

    @Test fun blankOrMissingExpiryIsNotAnExpiryDay() {
        for (e in listOf("", "   ", null)) {
            val d = DirectionScaleNeed.decide("NF", e, TODAY, null)
            assertFalse("expiry=$e", d.fetch)
            assertEquals(DirectionScaleNeed.NOT_EXPIRY_DAY, d.reason)
        }
    }

    @Test fun ownExpiryDayWithNoStateFetches() {
        val d = DirectionScaleNeed.decide("NF", TODAY, TODAY, null)
        assertTrue(d.fetch)
        assertEquals(DirectionScaleNeed.NO_SCALE_YET, d.reason)
    }

    @Test fun staleExpiryBeforeTodayAlsoCountsAsOwnExpiryDay() {
        val d = DirectionScaleNeed.decide("NF", "2026-10-27", TODAY, null)
        assertTrue(d.fetch)
    }

    @Test fun frozenUsableScaleStopsFetching() {
        val d = DirectionScaleNeed.decide("NF", TODAY, TODAY, state("NF" to ok()))
        assertFalse(d.fetch)
        assertEquals(DirectionScaleNeed.SCALE_FROZEN_OK, d.reason)
    }

    @Test fun recoverableFailureIsRetried() {
        for (reason in DirectionScaleNeed.RECOVERABLE_REASONS) {
            val d = DirectionScaleNeed.decide("NF", TODAY, TODAY, state("NF" to failed(reason)))
            assertTrue(reason, d.fetch)
            assertEquals(reason, DirectionScaleNeed.RECOVERABLE_FAILURE, d.reason)
        }
    }

    @Test fun unrecoverableFailureIsNotRetried() {
        for (reason in listOf("OPEN_REF_MISSING", "SPOT_INVALID", "SCALE_INVALID",
                              "NO_VALID_PARITY_PAIR", "EXPIRY_INVALID")) {
            val d = DirectionScaleNeed.decide("NF", TODAY, TODAY, state("NF" to failed(reason)))
            assertFalse(reason, d.fetch)
            assertEquals(reason, DirectionScaleNeed.UNRECOVERABLE_FAILURE, d.reason)
        }
    }

    @Test fun unreadableStateStillFetches() {
        // failing to read state must not silently suppress the acquisition that closes the outage
        val d = DirectionScaleNeed.decide("NF", TODAY, TODAY, "{not json")
        assertTrue(d.fetch)
        assertEquals(DirectionScaleNeed.STATE_UNREADABLE, d.reason)
    }

    @Test fun otherIndexScaleDoesNotSatisfyThisIndex() {
        // BNF frozen OK says nothing about NF
        val d = DirectionScaleNeed.decide("NF", TODAY, TODAY, state("BNF" to ok()))
        assertTrue(d.fetch)
        assertEquals(DirectionScaleNeed.NO_SCALE_YET, d.reason)
    }

    @Test fun scaleWithoutAStatusIsTreatedAsAbsent() {
        val d = DirectionScaleNeed.decide("NF", TODAY, TODAY,
            state("NF" to JSONObject().put("value", 0.009)))
        assertTrue(d.fetch)
        assertEquals(DirectionScaleNeed.NO_SCALE_YET, d.reason)
    }

    // ───────────────────────────── request count (finding B) ─────────────────────────────

    /**
     * A full expiry-day session is 78 polls. Before this bound the chain was fetched on every one of
     * them; now the scale is observed at the open reference and the acquisition stops.
     *
     * Scope, stated plainly: the saved OK state here is a FIXTURE, so this test shows what the gate
     * does once a usable scale is persisted — it does not show that the persistence happens. That
     * transition (a recoverable failure becoming a durable OK, and an OK then staying immutable) is
     * Python-side and is covered by `tests/test_expiry_day_scale_durability.py`, over both the direct
     * and the deferred write path. [frozenRecoveredScaleFromTheRealStoreStopsFetching] below uses the
     * exact shape that store actually writes, so the two sides meet on a real payload.
     */
    @Test fun requestCountOverAFullExpiryDaySessionIsBounded() {
        var stateJson: String? = null
        var fetches = 0
        for (poll in 1..78) {
            val d = DirectionScaleNeed.decide("NF", TODAY, TODAY, stateJson)
            if (d.fetch) fetches++
            // the open reference is the 09:20 poll (the 2nd of the session); the brain freezes a usable
            // scale from the chain fetched for it, and persists it to the authority state
            if (poll >= 2 && stateJson == null) stateJson = state("NF" to ok())
        }
        assertEquals("one fetch before the scale is frozen, none after", 2, fetches)
        assertTrue("must be far below the pre-fix 78", fetches < 5)
    }

    @Test fun aRecoverableFailureRetriesUntilItSucceeds() {
        var stateJson: String? = state("NF" to failed("NEXT_EXPIRY_CHAIN_MISSING"))
        var fetches = 0
        for (poll in 1..78) {
            val d = DirectionScaleNeed.decide("NF", TODAY, TODAY, stateJson)
            if (d.fetch) fetches++
            if (poll == 10) stateJson = state("NF" to ok())      // chain becomes available
        }
        assertEquals("retried while recoverable, stopped once recovered", 10, fetches)
    }

    @Test fun anUnrecoverableFailureStopsImmediately() {
        val stateJson = state("NF" to failed("SCALE_INVALID"))
        var fetches = 0
        for (poll in 1..78) {
            if (DirectionScaleNeed.decide("NF", TODAY, TODAY, stateJson).fetch) fetches++
        }
        assertEquals(0, fetches)
    }

    @Test fun ordinarySessionIssuesNoRequestsAtAll() {
        var fetches = 0
        for (poll in 1..78) {
            if (DirectionScaleNeed.decide("NF", NEXT, TODAY, null).fetch) fetches++
            if (DirectionScaleNeed.decide("BNF", "2026-11-23", TODAY, null).fetch) fetches++
        }
        assertEquals(0, fetches)
    }

    /**
     * The gate must stop fetching for a scale that was RECOVERED, not only for one observed at the
     * open reference. This fixture is the exact payload `index_authority_store` writes after a
     * successful late recovery (captured from a real run of `brain.compute_index_authority`), so the
     * Kotlin gate is pinned against the shape Python actually persists rather than a minimal stub.
     */
    @Test fun frozenRecoveredScaleFromTheRealStoreStopsFetching() {
        val recovered = JSONObject()
            .put("index", "NF").put("status", "OK").put("reason", JSONObject.NULL)
            .put("value", 0.00789094)
            .put("method", "atm_straddle_sqrt_2_over_pi_v1")
            .put("expiry", NEXT).put("trade_expiry", TODAY)
            .put("k0", 24200).put("ce_mid", 112.5).put("pe_mid", 98.25)
            .put("forward", 24214.25).put("n_sessions", 5.986667)
            .put("observed_at", "2026-11-09T11:00:00+05:30")
            .put("calendar_rule_version", "ev_v12_nse_cal_nf_weekly_tue_prev_session_v1:2026")
            .put("chain_source", "NEXT_EXPIRY")
            .put("chain_source_version", "scale_chain_source_v1_next_expiry_for_own_expiry_day")
            .put("chain_source_reason", JSONObject.NULL)
            .put("open_ref_observation", false)
            .put("recovered", true)
            .put("recovered_from_reason", "NEXT_EXPIRY_CHAIN_MISSING")
            .put("recovered_at", "2026-11-09T11:00:00+05:30")
            .put("recovered_spot", 24180.0)

        val d = DirectionScaleNeed.decide("NF", TODAY, TODAY, state("NF" to recovered))
        assertFalse("a durable recovered scale needs no further chain", d.fetch)
        assertEquals(DirectionScaleNeed.SCALE_FROZEN_OK, d.reason)

        // and over a whole session it issues nothing further
        var fetches = 0
        for (poll in 1..78) {
            if (DirectionScaleNeed.decide("NF", TODAY, TODAY, state("NF" to recovered)).fetch) fetches++
        }
        assertEquals(0, fetches)
    }

    // ───────────────────────────── stale chain (finding A) ─────────────────────────────

    @Test fun clearCtxRemovesEveryScaleKey() {
        val ctx = JSONObject()
            .put("nfScaleChain", JSONObject().put("strikes", JSONObject()))
            .put("nfScaleExpiry", "2026-11-03")
            .put("bnfScaleChain", JSONObject().put("strikes", JSONObject()))
            .put("bnfScaleExpiry", "2026-11-03")
            .put("nfChain", JSONObject().put("atm", 24000))     // trading chain must survive
        DirectionScaleNeed.clearCtx(ctx)
        for (k in DirectionScaleNeed.CTX_KEYS) assertFalse(k, ctx.has(k))
        assertTrue("the trading chain is not touched", ctx.has("nfChain"))
    }

    /**
     * The defect: success in one session, fetch failure in a later session. The persisted context
     * carried session 1's chain; session 2 wrote only a fresh expiry, so the brain would have paired
     * a new expiry with a stale chain.
     */
    @Test fun successThenFailureInALaterSessionCannotPairANewExpiryWithAnOldChain() {
        // session 1: a successful fetch publishes a matching pair
        val ctx = JSONObject()
        DirectionScaleNeed.clearCtx(ctx)
        ctx.put("nfScaleChain", JSONObject().put("strikes", JSONObject().put("24000", JSONObject()))
            .put("expiry", "2026-11-09"))
        ctx.put("nfScaleExpiry", "2026-11-09")
        assertEquals("2026-11-09", ctx.optJSONObject("nfScaleChain")!!.optString("expiry"))

        // ctx is persisted to SharedPreferences and reloaded here, carrying session 1's chain.
        // session 2 is another expiry day, and its fetch FAILS: expiry only, no chain.
        DirectionScaleNeed.clearCtx(ctx)
        ctx.put("nfScaleExpiry", "2026-11-17")

        assertFalse("session 1's chain must not survive", ctx.has("nfScaleChain"))
        assertEquals("2026-11-17", ctx.optString("nfScaleExpiry"))
        // an expiry with no chain is what the brain fails closed on (NEXT_EXPIRY_CHAIN_EMPTY)
    }

    @Test fun anOrdinarySessionAfterAnExpiryDayClearsTheLeftovers() {
        val ctx = JSONObject()
            .put("nfScaleChain", JSONObject().put("strikes", JSONObject()))
            .put("nfScaleExpiry", "2026-11-09")
        DirectionScaleNeed.clearCtx(ctx)          // runs unconditionally, even with nothing fetched
        assertFalse(ctx.has("nfScaleChain"))
        assertFalse(ctx.has("nfScaleExpiry"))
    }

    // ───────────────────────────── state reading ─────────────────────────────

    @Test fun readStateReturnsNullWhenAbsentAndContentWhenPresent() {
        val files = Files.createTempDirectory("mr-scale-need").toFile()
        try {
            assertNull(DirectionScaleNeed.readState(files, TODAY))
            val dir = IndexAuthorityContext.stateDir(files)
            assertTrue(dir.mkdirs())
            val payload = state("NF" to ok())
            File(dir, "$TODAY.json").writeText(payload)
            assertEquals(payload, DirectionScaleNeed.readState(files, TODAY))
            // a different session's file is not read
            assertNull(DirectionScaleNeed.readState(files, "2026-11-10"))
        } finally {
            files.deleteRecursively()
        }
    }

    @Test fun readStateAndDecideComposeIntoNoFetchOnceFrozen() {
        val files = Files.createTempDirectory("mr-scale-need2").toFile()
        try {
            val dir = IndexAuthorityContext.stateDir(files)
            assertTrue(dir.mkdirs())
            File(dir, "$TODAY.json").writeText(state("NF" to ok()))
            val d = DirectionScaleNeed.decide("NF", TODAY, TODAY,
                DirectionScaleNeed.readState(files, TODAY))
            assertFalse(d.fetch)
            assertEquals(DirectionScaleNeed.SCALE_FROZEN_OK, d.reason)
        } finally {
            files.deleteRecursively()
        }
    }

    // ───────────────────────────── wiring contracts ─────────────────────────────

    private fun src(name: String): String = listOf(
        File("src/main/java/com/marketradar/app/$name"),
        File("app/src/main/java/com/marketradar/app/$name")
    ).first { it.isFile }.readText()

    @Test fun serviceGatesTheFetchAndClearsTheContext() {
        val s = src("MarketWatchService.kt")
        assertTrue(s.contains("DirectionScaleNeed.decide("))
        assertTrue(s.contains("DirectionScaleNeed.readState(filesDir, today)"))
        assertTrue(s.contains("DirectionScaleNeed.clearCtx(ctxObj)"))
        // the gate precedes both the contract lookup and the chain request
        val body = s.substring(s.indexOf("private fun fetchDirectionScaleChain("))
        val gate = body.indexOf("DirectionScaleNeed.decide(")
        assertTrue(gate >= 0)
        assertTrue(gate < body.indexOf("resolveNextExpiryAfter("))
        assertTrue(gate < body.indexOf("/v2/option/chain"))
        // the clear precedes this poll's inserts
        assertTrue(s.indexOf("DirectionScaleNeed.clearCtx(ctxObj)") <
                   s.indexOf("for ((scaleIx, fetched) in scaleChains)"))
    }

    @Test fun recoverableReasonsMatchThePythonContract() {
        val py = listOf(
            File("src/main/python/index_authority_v1.py"),
            File("app/src/main/python/index_authority_v1.py")
        ).first { it.isFile }.readText()
        val block = py.substringAfter("SCALE_RECOVERABLE_REASONS = (").substringBefore(")")
        val pyReasons = Regex("'([A-Z_]+)'").findAll(block).map { it.groupValues[1] }.toSet()
        assertEquals(pyReasons, DirectionScaleNeed.RECOVERABLE_REASONS)
    }
}
