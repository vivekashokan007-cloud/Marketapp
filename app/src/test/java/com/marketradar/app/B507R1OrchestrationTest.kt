package com.marketradar.app

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.yield
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.util.concurrent.CountDownLatch
import java.util.concurrent.atomic.AtomicInteger

/**
 * b507 R1 — Codex F2/F3/F4 behavioural regressions on the REAL AnalysisAuthority (the object MarketWatchService
 * and NativeBridge share), the REAL SessionInputs saves and the REAL BrainResultStore file+reference store.
 * [Harness.analyze] follows MarketWatchService.runBrainAnalysis's guarded order: poll-owned effects (history,
 * EV envelope) only in POLL mode -> enrichment barrier -> superseded => requestRefresh -> notification dispatch via
 * dispatchIfCurrent -> publication via publish{BrainResultStore.saveIn}. Deterministic barriers only (no sleeps).
 */
class B507R1OrchestrationTest {
    companion object {
        const val S = B507OvernightIntegrityTest.S
        fun ist(h: Int, m: Int) = B507OvernightIntegrityTest.ist(2026, 10, 15, h, m)
    }

    class Hooks(val enrichGate: CompletableDeferred<Unit>? = null, val enrichReached: CompletableDeferred<Unit>? = null,
                val commitEntered: CountDownLatch? = null, val commitRelease: CountDownLatch? = null)

    internal class Harness {
        val dir: File = Files.createTempDirectory("b507r1").toFile()
        val prefs = FakePrefs()
        val auth = AnalysisAuthority()
        val history = mutableListOf<Long>()
        val evEnvelopes = mutableListOf<Long>()
        val notifications = mutableListOf<String>()
        val refreshRevisions = mutableListOf<String>()
        var lastMarketMs = 0L
        var lastSession: String? = S
        var refreshFailures = 0
        var refreshTimeouts = 0
        var refreshHooks: Hooks = Hooks()
        val analyzing = AtomicInteger(0)
        var maxConcurrent = 0

        init {
            auth.attachRevisionSource { SessionInputs.inputRevision(state(), S) }
            auth.refresher = { rev ->
                refreshRevisions += rev
                when {
                    refreshFailures > 0 -> { refreshFailures--; AnalysisAuthority.Outcome.FAILED }
                    refreshTimeouts > 0 -> { refreshTimeouts--; AnalysisAuthority.Outcome.TIMED_OUT }
                    else -> AnalysisAuthority.refreshPrecondition(lastSession, lastMarketMs, S, ist(9, 31))
                        ?: analyze("REFRESH", lastMarketMs, refreshHooks)
                }
            }
            var st = SessionInputs.withEvening(JSONObject(), ist(8, 0), B507OvernightIntegrityTest.eve()).state!!
            st = SessionInputs.withMorning(st, ist(9, 5), B507OvernightIntegrityTest.mor()).state!!
            prefs.edit().putString(SessionInputs.PREF_KEY, st.toString()).commit()
        }

        fun state() = SessionInputs.read(prefs.getString(SessionInputs.PREF_KEY, null))
        fun rev() = SessionInputs.inputRevision(state(), S)

        /** NativeBridge.setEveningClose: one guarded commit (pair + result invalidation), then a refresh request. */
        fun saveEvening(dow: String, atMs: Long): String {
            val built = SessionInputs.withEvening(state(), atMs, B507OvernightIntegrityTest.eve(dow = dow))
            val ok = auth.commitInputs {
                prefs.edit().putString(SessionInputs.PREF_KEY, built.state!!.toString())
                    .also { BrainResultStore.clearIn(dir, it) }.commit()
            }
            assertTrue(ok)
            return auth.requestRefresh()
        }

        fun stored(): String = BrainResultStore.readBrainResultIn(dir, prefs)
        fun storedRevision(): String? = if (stored() == "null") null else JSONObject(stored()).getJSONObject("input_identity").getString("input_revision")
        fun storedMarket(): Long = JSONObject(stored()).getJSONObject("input_identity").getLong("market_collected_at_ms")

        suspend fun analyze(mode: String, marketMs: Long, hooks: Hooks = Hooks()): AnalysisAuthority.Outcome {
            maxConcurrent = maxOf(maxConcurrent, analyzing.incrementAndGet())
            try {
                val id = AnalysisAuthority.Identity(S, auth.currentRevision(), marketMs, mode)
                val poll = mode == "POLL"
                if (poll) { history += marketMs; evEnvelopes += marketMs; lastMarketMs = marketMs }
                hooks.enrichReached?.complete(Unit)
                hooks.enrichGate?.await()
                if (poll && !auth.isCurrent(id)) auth.requestRefresh()
                if (poll) auth.dispatchIfCurrent(id) { notifications += "${id.inputRevision}@$marketMs" }
                return auth.publish(id) {
                    hooks.commitEntered?.countDown()
                    hooks.commitRelease?.await()
                    BrainResultStore.saveIn(dir, prefs, JSONObject().put("input_identity", JSONObject()
                        .put("input_revision", id.inputRevision).put("market_collected_at_ms", marketMs).put("mode", mode)).toString(), "[]")
                }
            } finally { analyzing.decrementAndGet() }
        }

        suspend fun poll(marketMs: Long, hooks: Hooks = Hooks()) = auth.withPollLane { analyze("POLL", marketMs, hooks) }
    }

    // ---------------- F2 ----------------

    @Test fun f2_saveDuringPollIsNotLost_bothDeferralPaths_exactlyOneRefreshForLatest() = runBlocking(Dispatchers.Default) {
        val h = Harness()
        val gate = CompletableDeferred<Unit>(); val reached = CompletableDeferred<Unit>()
        val pollA = async { h.poll(ist(9, 30), Hooks(gate, reached)) }
        reached.await()                                        // poll A is in flight, lane held
        val revB = h.saveEvening("46050", ist(9, 20))
        assertNull(h.auth.drain())                             // deferral path 1: busy lane, request NOT consumed
        assertEquals(revB, h.auth.pending())
        gate.complete(Unit)                                    // deferral path 2: poll sees superseded A
        assertEquals(AnalysisAuthority.Outcome.SUPERSEDED_INPUT, pollA.await())
        // the poll's lane release drained B without any user action or collection slot
        assertEquals(listOf(revB), h.refreshRevisions)
        assertEquals(revB, h.storedRevision())
        assertNull(h.auth.pending())
        assertEquals(listOf(ist(9, 30)), h.history)            // one collection only
        assertTrue(h.notifications.isEmpty())                  // A's notifications suppressed, refresh never notifies
        assertEquals(1, h.maxConcurrent)
    }

    @Test fun f2_rapidSavesBAndCDuringPollSettleOnC() = runBlocking(Dispatchers.Default) {
        val h = Harness()
        val gate = CompletableDeferred<Unit>(); val reached = CompletableDeferred<Unit>()
        val pollA = async { h.poll(ist(9, 30), Hooks(gate, reached)) }
        reached.await()
        h.saveEvening("46050", ist(9, 20)); h.auth.drain()
        val revC = h.saveEvening("46075", ist(9, 21)); h.auth.drain()
        gate.complete(Unit); pollA.await()
        assertEquals(listOf(revC), h.refreshRevisions)         // B never computed separately
        assertEquals(revC, h.storedRevision())
    }

    @Test fun f2_failedAndTimedOutRefreshStayPendingAndRecover() = runBlocking(Dispatchers.Default) {
        val h = Harness()
        h.poll(ist(9, 30))
        h.refreshFailures = 1; h.refreshTimeouts = 1
        val revB = h.saveEvening("46050", ist(9, 20))
        assertEquals(AnalysisAuthority.Outcome.FAILED, h.auth.drain())
        assertEquals(revB, h.auth.pending()); assertNull(h.storedRevision())      // honest: nothing current
        assertEquals(AnalysisAuthority.Outcome.TIMED_OUT, h.auth.drain())
        assertEquals(revB, h.auth.pending())
        assertEquals(AnalysisAuthority.Outcome.PUBLISHED, h.auth.drain())       // recovery on the next request
        assertEquals(revB, h.storedRevision()); assertNull(h.auth.pending())
        assertEquals(3, h.refreshRevisions.size)
    }

    // ---------------- F3 ----------------

    @Test fun f3a_saveWhileEnrichingSuppressesNotificationAndPublication() = runBlocking(Dispatchers.Default) {
        val h = Harness()
        val gate = CompletableDeferred<Unit>(); val reached = CompletableDeferred<Unit>()
        val revA = h.rev()
        val pollA = async { h.poll(ist(9, 30), Hooks(gate, reached)) }
        reached.await()
        val revB = h.saveEvening("46050", ist(9, 20))
        gate.complete(Unit)
        pollA.await()
        assertFalse(h.notifications.any { it.startsWith(revA) })          // no stale current notification
        assertEquals(revB, h.storedRevision())                            // only B's own result became current
    }

    @Test fun f3b_saveCannotInterleaveBetweenValidationAndResultCommit() {
        val h = Harness()
        val revA = h.rev()
        val entered = CountDownLatch(1); val release = CountDownLatch(1)
        var outcomeA: AnalysisAuthority.Outcome? = null
        val tA = Thread { outcomeA = runBlocking { h.poll(ist(9, 30), Hooks(commitEntered = entered, commitRelease = release)) } }
        h.auth.refresher = null                                            // observe the store only
        tA.start(); entered.await()                                        // A validated, inside the commit
        var revB: String? = null
        val tB = Thread { revB = h.saveEvening("46050", ist(9, 20)) }
        tB.start()
        while (tB.state != Thread.State.BLOCKED && tB.state != Thread.State.TERMINATED) Thread.onSpinWait()
        assertEquals(Thread.State.BLOCKED, tB.state)                      // the save waits for the guarded commit
        release.countDown(); tA.join(); tB.join()
        assertEquals(AnalysisAuthority.Outcome.PUBLISHED, outcomeA)
        assertEquals("null", h.stored())                                  // B's save invalidated A AFTER A committed
        assertEquals(revB, h.auth.pending())                              // B remains pending until its result
        assertFalse(revA == revB)
    }

    @Test fun f3b_reverseOrder_saveFirstThenPublishIsRejected_andReadbackGuard() = runBlocking(Dispatchers.Default) {
        val h = Harness()
        h.poll(ist(9, 30))
        val raw = h.stored()
        h.auth.refresher = null
        val revB = h.saveEvening("46050", ist(9, 20))
        val stale = AnalysisAuthority.Identity(S, JSONObject(raw).getJSONObject("input_identity").getString("input_revision"), ist(9, 30), "POLL")
        assertEquals(AnalysisAuthority.Outcome.SUPERSEDED_INPUT, h.auth.publish(stale) { BrainResultStore.saveIn(h.dir, h.prefs, raw, "[]") })
        assertEquals("null", h.stored())
        assertNull(SessionInputs.currentResultOrNull(raw, revB))           // native readback defence
    }

    // ---------------- F4 ----------------

    @Test fun f4_refreshOfP6AndPollP7NeverOverlap_P7StaysCurrent_noDuplicateEffects() = runBlocking(Dispatchers.Default) {
        val h = Harness()
        h.poll(ist(9, 25))                                                 // P6 collected
        val gate = CompletableDeferred<Unit>(); val reached = CompletableDeferred<Unit>()
        h.refreshHooks = Hooks(gate, reached)
        h.saveEvening("46050", ist(9, 20))
        val refresh = async { h.auth.drain() }                             // refresh of P6 holds the lane
        reached.await()
        val p7Started = CompletableDeferred<Unit>()
        val p7 = async { h.auth.withPollLane { p7Started.complete(Unit); h.analyze("POLL", ist(9, 30)) } }
        yield()
        assertFalse(p7Started.isCompleted)                                 // P7 waits for the lane
        gate.complete(Unit)
        assertEquals(AnalysisAuthority.Outcome.PUBLISHED, refresh.await())
        assertEquals(AnalysisAuthority.Outcome.PUBLISHED, p7.await())
        assertEquals(ist(9, 30), h.storedMarket())                         // newest market data is current
        assertEquals(1, h.maxConcurrent)
        assertEquals(listOf(ist(9, 25), ist(9, 30)), h.history)            // no duplicate collection/history
        assertEquals(listOf(ist(9, 25), ist(9, 30)), h.evEnvelopes)        // refresh queued no EV envelope
        assertEquals(2, h.notifications.size)                              // one per poll; refresh never notified
        // an old-market publication can never replace P7, even with the identical input revision
        val p6Late = AnalysisAuthority.Identity(S, h.rev(), ist(9, 25), "REFRESH")
        assertEquals(AnalysisAuthority.Outcome.STALE_MARKET, h.auth.publish(p6Late) { error("must not commit") })
        assertEquals(ist(9, 30), h.storedMarket())
    }

    @Test fun f4_cachedSnapshotTooOldAndRestartWithoutCacheStayPendingForNextPoll() = runBlocking(Dispatchers.Default) {
        val h = Harness()
        h.poll(ist(9, 20))                                                 // 11 min before refresh "now" (09:31)
        val revB = h.saveEvening("46050", ist(9, 21))
        assertEquals(AnalysisAuthority.Outcome.MARKET_TOO_OLD, h.auth.drain())
        assertEquals(revB, h.auth.pending()); assertNull(h.storedRevision())
        h.lastSession = null                                               // service restart: no in-memory cache
        assertEquals(AnalysisAuthority.Outcome.WAITING_FOR_POLL, h.auth.drain())
        assertEquals(revB, h.auth.pending())
        h.lastSession = S
        assertEquals(AnalysisAuthority.Outcome.PUBLISHED, h.poll(ist(9, 35)))   // the next poll computes B
        assertNull(h.auth.pending()); assertEquals(revB, h.storedRevision())
        assertEquals(1, h.history.count { it == ist(9, 35) })
        assertEquals(AnalysisAuthority.Outcome.WAITING_FOR_POLL,
            AnalysisAuthority.refreshPrecondition(null, 0L, S, ist(9, 31)))
        assertNull(AnalysisAuthority.refreshPrecondition(S, ist(9, 28), S, ist(9, 31)))
    }
}
