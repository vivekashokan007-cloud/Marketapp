package com.marketradar.app

import com.marketradar.app.AnalysisAuthority.Outcome as O
import com.marketradar.app.ExpiryFallbackPublication.State as S
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.util.concurrent.CountDownLatch

/**
 * R4 (Codex R3 reply): the expiry-fallback attempt is decided at the app's FINAL publication gate. Every case goes
 * through the production [AnalysisAuthority.publish] (real identity checks, real monitor) with a fake result-store
 * commit, exactly as MarketWatchService calls it via [ExpiryFallbackPublication.publishThroughGate]. SYNTHETIC:
 * fake Python apply, deterministic clocks. The [Acks] list is what the service would queue to Python
 * (single_leg_expiry_fallback_reconcile); the Python side of the same sequences is tested in test_expiry_fallback_r4.py.
 */
class ExpiryFallbackFinalGateTest {
    @Before fun quiet() { PyTimeout.logger = { } }

    private fun result(): JSONObject {
        fun plan(did: String) = JSONObject().put("decision_id", did).put("reasons", JSONArray().put("EXPIRY_DAY_NO_NEXT_CHAIN")
            .put("EXPIRY_FALLBACK_EVIDENCE_PENDING")).put("status", "BLOCKED").put("admit", false)
            .put("expiry_fallback", JSONObject().put("status", "EVIDENCE_REQUIRED").put("decision_id", did)
                .put("trade_expiry", "2026-11-23").put("base_reasons", JSONArray().put("EXPIRY_DAY_NO_NEXT_CHAIN"))
                .put("base_status", "BLOCKED"))
        return JSONObject().put("single_leg_by_index", JSONObject().put("NF", plan("idx1_n")))
    }

    private fun patchFor(attempt: JSONObject): String = JSONObject().put("single_leg_by_index", JSONObject().put("NF",
        JSONObject().put("decision_id", "idx1_n").put("status", "ELIGIBLE_PENDING_SNAPSHOT")
            .put("instrument_key", "NSE_FO|NF2026120124000CE").put("reasons", JSONArray())
            .put("expiry_fallback", JSONObject().put("status", "APPLIED").put("attempt", attempt)))).put("attempt", attempt).toString()

    private data class Ack(val id: String, val gen: Long, val state: S, val reason: String?, val decidedMs: Long?)
    private class Acks { val list = mutableListOf<Ack>()
        @Synchronized fun add(d: ExpiryFallbackPublication.Decision) { list.add(Ack(d.attempt.id, d.attempt.generation, d.state, d.reason, d.decidedMs)) }
        @Synchronized fun add(a: ExpiryFallbackPublication.Attempt, s: S) { list.add(Ack(a.id, a.generation, s, null, null)) } }

    private class World {
        var rev = "rev-1"
        val auth = AnalysisAuthority().apply { attachRevisionSource { rev } }
        val pub = ExpiryFallbackPublication(ExpiryFallbackPublication.MemoryStore())
        val acks = Acks()
        var commits = 0
        var clock = 10_000L
        fun id(market: Long = 1_000L) = AnalysisAuthority.Identity("2026-11-23", rev, market, "POLL")
    }

    /** One poll's apply step: accepted in memory, nothing acknowledged. */
    private fun accept(w: World, r: JSONObject = result()): ExpiryFallbackPublication.Attempt {
        val d = ExpiryFallbackPublication.applyAndPublish(r, w.pub, "test.efb.gate." + System.nanoTime(), 2_000L,
            Long.MAX_VALUE, { 0L }, { a -> patchFor(a) }, w.acks::add)
        assertEquals(S.APPLY_ACCEPTED, d.state)
        assertEquals("NSE_FO|NF2026120124000CE", r.getJSONObject("single_leg_by_index").getJSONObject("NF").getString("instrument_key"))
        assertTrue("no acknowledgement before the final gate", w.acks.list.none { it.id == d.attempt.id })
        return d.attempt
    }

    private fun gate(w: World, a: ExpiryFallbackPublication.Attempt?, id: AnalysisAuthority.Identity,
                     commit: () -> Boolean = { w.commits++; true }): O =
        ExpiryFallbackPublication.publishThroughGate(w.pub, a, w.auth, id, { w.clock }, commit, w.acks::add)

    // 1 ------------------------------------------------------------------------------------------------------------
    @Test fun s1_inputRevisionChangesBeforeFinalPublish_notPublishedSupersededInput_noCommit() {
        val w = World()
        val a = accept(w)
        val identity = w.id()
        w.rev = "rev-2"                                             // the user saved new inputs after apply
        assertEquals(O.SUPERSEDED_INPUT, gate(w, a, identity))
        assertEquals(0, w.commits)
        assertEquals(S.NOT_PUBLISHED, w.pub.stateOf(a))
        assertEquals(listOf(Ack(a.id, a.generation, S.NOT_PUBLISHED, "SUPERSEDED_INPUT", w.clock)), w.acks.list)
        assertNull(w.auth.lastPublished())
    }

    // 2 ------------------------------------------------------------------------------------------------------------
    @Test fun s2_marketIdentityRejectedOrStale_notPublishedWithTheAuthoritysReason() {
        val w = World()
        val a = accept(w)
        assertEquals(O.MARKET_IDENTITY_UNAVAILABLE, gate(w, a, w.id(market = 0L)))
        assertEquals(Ack(a.id, a.generation, S.NOT_PUBLISHED, "MARKET_IDENTITY_UNAVAILABLE", w.clock), w.acks.list.single())
        // stale: a result for a NEWER market snapshot was already published in this session
        val w2 = World()
        assertEquals(O.PUBLISHED, w2.auth.publish(w2.id(market = 2_000L)) { true })
        val b = accept(w2)
        assertEquals(O.STALE_MARKET, gate(w2, b, w2.id(market = 1_000L)))
        assertEquals(0, w2.commits)
        assertEquals(Ack(b.id, b.generation, S.NOT_PUBLISHED, "STALE_MARKET", w2.clock), w2.acks.list.single())
        assertEquals(2_000L, w2.auth.lastPublished()!!.marketCollectedAtMs)
    }

    // 3 ------------------------------------------------------------------------------------------------------------
    @Test fun s3_resultStoreCommitReturnsFalse_notPublishedPersistFailed() {
        val w = World()
        val a = accept(w)
        assertEquals(O.PERSIST_FAILED, gate(w, a, w.id()) { w.commits++; false })
        assertEquals(1, w.commits)
        assertEquals(Ack(a.id, a.generation, S.NOT_PUBLISHED, "PERSIST_FAILED", w.clock), w.acks.list.single())
        assertNull(w.auth.lastPublished())
    }

    // 4 ------------------------------------------------------------------------------------------------------------
    @Test fun s4_abortBeforeFinalPublishIsNotPublished_exceptionInsideTheCommitStaysUnconfirmed() {
        // (a) the analysis throws after apply, before the gate: the finalizer fences it (provably never committed)
        val w = World()
        val a = accept(w)
        assertTrue(ExpiryFallbackPublication.fenceUnfinished(w.pub, a, "ABORTED_BEFORE_FINAL_PUBLISH:IllegalStateException",
            { w.clock }, w.acks::add))
        assertEquals(Ack(a.id, a.generation, S.NOT_PUBLISHED, "ABORTED_BEFORE_FINAL_PUBLISH:IllegalStateException", w.clock),
            w.acks.list.single())
        assertEquals(0, w.commits)
        // (b) the commit itself throws inside the gate: it may or may not have been written -> no acknowledgement at
        // all (Python keeps UNCONFIRMED), the exception propagates, and the finalizer cannot overwrite that.
        val w2 = World()
        val b = accept(w2)
        try {
            gate(w2, b, w2.id()) { w2.commits++; throw IllegalStateException("disk") }
            fail("the gate must rethrow")
        } catch (e: IllegalStateException) { assertEquals("disk", e.message) }
        assertEquals(S.GATE_UNRESOLVED, w2.pub.stateOf(b))
        assertFalse(ExpiryFallbackPublication.fenceUnfinished(w2.pub, b, "ABORTED_BEFORE_FINAL_PUBLISH:IllegalStateException",
            { w2.clock }, w2.acks::add))
        assertTrue(w2.acks.list.isEmpty())
        assertEquals(S.GATE_UNRESOLVED, w2.pub.stateOf(b))
    }

    // 5 ------------------------------------------------------------------------------------------------------------
    @Test fun s5_successfulGuardedCommit_publishedOnlyAfterSuccess_orderingByLatch() {
        val w = World()
        val a = accept(w)
        val identity = w.id()
        val committed = CountDownLatch(1)
        val events = mutableListOf<String>()
        val out = ExpiryFallbackPublication.publishThroughGate(w.pub, a, w.auth, identity, { w.clock },
            {
                assertEquals("no acknowledgement may precede the commit", 0, w.acks.list.size)
                assertEquals(S.APPLY_ACCEPTED, w.pub.stateOf(a))
                events.add("commit"); w.commits++; w.clock = 12_345L; committed.countDown(); true
            }) { d ->
            assertEquals("the acknowledgement follows a completed commit", 0L, committed.count)
            assertEquals(identity, w.auth.lastPublished())         // publish has already returned PUBLISHED
            events.add("ack:" + d.state); w.acks.add(d)
        }
        assertEquals(O.PUBLISHED, out)
        assertEquals(listOf("commit", "ack:PUBLISHED"), events)
        assertEquals(listOf(Ack(a.id, a.generation, S.PUBLISHED, null, 12_345L)), w.acks.list)  // actual decision time
        // a later finalizer can never overwrite the successful terminal result
        assertFalse(ExpiryFallbackPublication.fenceUnfinished(w.pub, a, "ANALYSIS_ENDED_BEFORE_FINAL_PUBLISH", { 1L }, w.acks::add))
        assertFalse(w.pub.finish(a, S.NOT_PUBLISHED, "X"))
        assertEquals(S.PUBLISHED, w.pub.stateOf(a))
        assertEquals(1, w.acks.list.size)
    }

    // 6 ------------------------------------------------------------------------------------------------------------
    @Test fun s6_processDiesBeforeCommitOrBeforeAck_nothingIsEverInventedAfterRestart() {
        val store = ExpiryFallbackPublication.MemoryStore()
        val acks = Acks()
        val before = ExpiryFallbackPublication(store)
        val r = result()
        val a = ExpiryFallbackPublication.applyAndPublish(r, before, "test.efb.die." + System.nanoTime(), 2_000L,
            Long.MAX_VALUE, { 0L }, { x -> patchFor(x) }, acks::add).attempt
        assertEquals(S.APPLY_ACCEPTED, before.stateOf(a))
        // process dies here (before the gate): no acknowledgement was produced
        assertTrue(acks.list.isEmpty())
        val after = ExpiryFallbackPublication(store)                // restart: only the persisted generation remains
        assertEquals(S.UNKNOWN, after.stateOf(a))
        assertFalse(after.finish(a, S.PUBLISHED, null))
        assertFalse(ExpiryFallbackPublication.fenceUnfinished(after, a, "X", { 0L }, acks::add))
        val auth = AnalysisAuthority().apply { attachRevisionSource { "rev-1" } }
        // even if a result were published after restart while carrying the dead attempt, it is not acknowledged
        assertEquals(O.PUBLISHED, ExpiryFallbackPublication.publishThroughGate(after, a, auth,
            AnalysisAuthority.Identity("2026-11-23", "rev-1", 1_000L, "POLL"), { 0L }, { true }, acks::add))
        assertTrue(acks.list.isEmpty())                             // the old attempt stays UNCONFIRMED in Python
        // dies AFTER a successful commit but before the queued acknowledgement ran: the decision existed only in
        // memory; after restart nothing re-sends or infers it (Python keeps UNCONFIRMED; see the Python test)
        val lost = mutableListOf<ExpiryFallbackPublication.Decision>()
        val (b, _) = after.begin()
        assertEquals(S.APPLY_ACCEPTED, after.tryAccept(b, 0L, Long.MAX_VALUE))
        ExpiryFallbackPublication.publishThroughGate(after, b, auth,
            AnalysisAuthority.Identity("2026-11-23", "rev-1", 2_000L, "POLL"), { 0L }, { true }) { lost.add(it) }
        assertEquals(S.PUBLISHED, lost.single().state)               // queued, then lost with the process
        val again = ExpiryFallbackPublication(store)
        assertEquals(S.UNKNOWN, again.stateOf(b))
        assertTrue(again.begin().first.generation > b.generation)
    }

    // 7 ------------------------------------------------------------------------------------------------------------
    @Test fun s7_lateCompletionOfAnOlderTimedOutOrCancelledAttemptCannotAlterTheNewerResult() {
        val w = World()
        val key = "test.efb.lategate." + System.nanoTime()
        val finished = CountDownLatch(1)
        val r1 = result()
        val old = ExpiryFallbackPublication.applyAndPublish(r1, w.pub, key, 150L, Long.MAX_VALUE, { 0L },
            { a -> Thread.sleep(600); patchFor(a).also { finished.countDown() } }, w.acks::add)
        assertEquals(S.TIMED_OUT, old.state)
        Thread.sleep(700)                                           // let the in-flight call drain before the next poll
        val r2 = result()
        val cur = accept(w, r2)
        assertEquals(O.PUBLISHED, gate(w, cur, w.id()))
        val published = r2.toString()
        assertTrue(finished.await(3, java.util.concurrent.TimeUnit.SECONDS))
        // the old attempt's late completion / late acceptance / late finalisation
        assertEquals(S.TIMED_OUT, w.pub.tryAccept(old.attempt, 0L, Long.MAX_VALUE))
        assertFalse(w.pub.finish(old.attempt, S.PUBLISHED, null))
        assertFalse(ExpiryFallbackPublication.fenceUnfinished(w.pub, old.attempt, "X", { 0L }, w.acks::add))
        assertEquals(published, r2.toString())
        assertEquals(S.PUBLISHED, w.pub.stateOf(cur))
        assertEquals(listOf(Ack(old.attempt.id, old.attempt.generation, S.TIMED_OUT, null, null),
            Ack(cur.id, cur.generation, S.PUBLISHED, null, w.clock)), w.acks.list)
        // a cancelled older attempt (newer begin while it ran) likewise can never be accepted or published
        val r3 = result()
        var newer: ExpiryFallbackPublication.Attempt? = null
        val c = ExpiryFallbackPublication.applyAndPublish(r3, w.pub, key + ".c", 2_000L, Long.MAX_VALUE, { 0L },
            { a -> newer = w.pub.begin().first; patchFor(a) }, w.acks::add)
        assertEquals(S.CANCELLED, c.state)
        assertFalse(w.pub.finish(c.attempt, S.PUBLISHED, null))
        assertEquals(S.APPLY_ACCEPTED, w.pub.tryAccept(newer!!, 0L, Long.MAX_VALUE))
    }

    // 8 ------------------------------------------------------------------------------------------------------------
    @Test fun s8_normalSuccessfulPollAfterAFailure_publishesAndReconcilesItsOwnAttemptOnce() {
        val w = World()
        val a = accept(w)
        val idA = w.id()
        w.rev = "rev-2"
        assertEquals(O.SUPERSEDED_INPUT, gate(w, a, idA))
        val b = accept(w)                                           // next poll, current inputs
        assertTrue(b.generation > a.generation)
        w.clock = 20_000L
        assertEquals(O.PUBLISHED, gate(w, b, w.id(market = 3_000L)))
        // the end-of-analysis finalizer runs for both; neither changes anything
        assertFalse(ExpiryFallbackPublication.fenceUnfinished(w.pub, a, "ANALYSIS_ENDED_BEFORE_FINAL_PUBLISH", { 0L }, w.acks::add))
        assertFalse(ExpiryFallbackPublication.fenceUnfinished(w.pub, b, "ANALYSIS_ENDED_BEFORE_FINAL_PUBLISH", { 0L }, w.acks::add))
        assertEquals(listOf(Ack(a.id, a.generation, S.NOT_PUBLISHED, "SUPERSEDED_INPUT", 10_000L),
            Ack(b.id, b.generation, S.PUBLISHED, null, 20_000L)), w.acks.list)
        assertEquals("rev-2", w.auth.lastPublished()!!.inputRevision)
        assertEquals(1, w.commits)
    }

    @Test fun aPollWithoutAnAcceptedAttemptIsExactlyTheAuthoritysPublish() {
        val w = World()
        assertEquals(O.PUBLISHED, gate(w, null, w.id()))
        w.rev = "rev-9"
        assertEquals(O.SUPERSEDED_INPUT, gate(w, null, w.id().copy(inputRevision = "rev-1")))
        assertTrue(w.acks.list.isEmpty())
        assertEquals(1, w.commits)
    }
}
