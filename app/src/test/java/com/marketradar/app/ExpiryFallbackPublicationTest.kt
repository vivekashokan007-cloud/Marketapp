package com.marketradar.app

import com.marketradar.app.ExpiryFallbackPublication.State as S
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** R3 (Codex R2 reply s3), R4 (Codex R3 reply: apply success is APPLY_ACCEPTED, see ExpiryFallbackFinalGateTest): publication reconciliation of the expiry-fallback apply. SYNTHETIC: fake broker transport,
 *  fake Python apply (sleeps / returns), deterministic native clock where needed. */
class ExpiryFallbackPublicationTest {
    @Before fun quiet() { PyTimeout.logger = { } }

    private fun result(): JSONObject {
        fun dec(did: String, spot: Double) = JSONObject().put("decision_id", did).put("direction", JSONObject().put("inputs",
            JSONObject().put("spot", JSONObject().put("value", spot))))
        fun plan(did: String) = JSONObject().put("decision_id", did).put("reasons", JSONArray().put("EXPIRY_DAY_NO_NEXT_CHAIN")
            .put("EXPIRY_FALLBACK_EVIDENCE_PENDING")).put("status", "BLOCKED").put("admit", false)
            .put("expiry_fallback", JSONObject().put("status", "EVIDENCE_REQUIRED").put("decision_id", did)
                .put("trade_expiry", "2026-11-23").put("base_reasons", JSONArray().put("EXPIRY_DAY_NO_NEXT_CHAIN"))
                .put("base_status", "BLOCKED"))
        return JSONObject().put("by_index", JSONObject().put("NF", dec("idx1_n", 24010.0)).put("BNF", dec("idx1_b", 52010.0)))
            .put("single_leg_by_index", JSONObject().put("NF", plan("idx1_n")).put("BNF", plan("idx1_b")))
    }

    /** What Python returns for an attempt: an APPLIED eligible NF plan and an APPLIED BNF plan. */
    private fun patchFor(attempt: JSONObject): String {
        fun applied(ix: String, key: String) = JSONObject().put("decision_id", "idx1_" + ix.lowercase().first())
            .put("status", "ELIGIBLE_PENDING_SNAPSHOT").put("instrument_key", key).put("reasons", JSONArray())
            .put("expiry_fallback", JSONObject().put("status", "APPLIED").put("attempt", attempt))
        return JSONObject().put("single_leg_by_index", JSONObject().put("NF", applied("NF", "NSE_FO|NF2026120124000CE"))
            .put("BNF", applied("BNF", "NSE_FO|BNF2026122952000CE"))).put("attempt", attempt).toString()
    }

    private fun fb(r: JSONObject, ix: String) = r.getJSONObject("single_leg_by_index").getJSONObject(ix).getJSONObject("expiry_fallback")

    private fun assertWaited(r: JSONObject, code: String) {
        for (ix in listOf("NF", "BNF")) {
            val p = r.getJSONObject("single_leg_by_index").getJSONObject(ix)
            assertEquals("WAIT", fb(r, ix).getString("status"))
            assertEquals(code, fb(r, ix).getString("reason"))
            assertFalse(p.optBoolean("admit", true))
            assertFalse(p.has("instrument_key"))                  // nothing from the computed attempt was published
            assertEquals("EXPIRY_FALLBACK_WAIT:$code", p.getJSONArray("reasons").getString(1))
        }
    }

    /** Fake broker transport acquisition for both indices (the native side before apply). */
    private fun acquire(): JSONObject {
        val listing = JSONObject().put("status", "success").put("data", JSONArray()
            .put(JSONObject().put("expiry", "2026-11-23").put("underlying_key", "NSE_INDEX|Nifty 50"))
            .put(JSONObject().put("expiry", "2026-12-01").put("underlying_key", "NSE_INDEX|Nifty 50"))
            .put(JSONObject().put("expiry", "2026-11-23").put("underlying_key", "NSE_INDEX|Nifty Bank"))
            .put(JSONObject().put("expiry", "2026-12-29").put("underlying_key", "NSE_INDEX|Nifty Bank"))).toString()
        val ok = JSONObject().put("status", "success").put("data", JSONArray()).toString()
        val t = ManagedReadAdapters.Transport { _, url, _ ->
            when {
                url.contains("/v2/option/contract?") && !url.contains("expiry_date") -> ManagedReadAdapters.Response(200, listing)
                else -> ManagedReadAdapters.Response(200, ok)
            }
        }
        val d = Files.createTempDirectory("efbp").toFile().let { File(it, "single_leg_v1").apply { mkdirs() } }
        var wall = 1_795_420_800_000L
        val clocks = ManagedReadAdapters.Clocks({ wall.also { wall += 50 } }, { System.nanoTime() / 1_000_000L })
        return ExpiryFallbackAcquirer.acquire(ExpiryFallbackAcquirer.needs(result()), d, "2026-11-23", { t }, clocks,
            { ix, _, _ -> JSONObject().put("index", ix).put("expiry", if (ix == "NF") "2026-12-01" else "2026-12-29").toString() },
            { _, _ -> JSONObject().put("strikes", JSONObject().put("24000", JSONObject())) })
    }

    private class Recon { val calls = mutableListOf<Pair<String, ExpiryFallbackPublication.State>>()
        @Synchronized fun add(a: ExpiryFallbackPublication.Attempt, s: ExpiryFallbackPublication.State) { calls.add(a.id to s) } }

    @Test fun pyTimeoutLateCompletionIsNeverPublishedThenNextPollPublishes() {
        val ev = acquire()
        assertTrue(ev.has("NF") && ev.has("BNF"))
        val pub = ExpiryFallbackPublication(ExpiryFallbackPublication.MemoryStore())
        val rec = Recon()
        val r = result()
        val finished = CountDownLatch(1)
        val key = "test.efb.late." + System.nanoTime()
        val d = ExpiryFallbackPublication.applyAndPublish(r, pub, key, 200L, Long.MAX_VALUE, { 0L },
            { a -> Thread.sleep(700); patchFor(a).also { finished.countDown() } }, rec::add)
        assertEquals(S.TIMED_OUT, d.state)
        assertWaited(r, "APPLY_TIMEOUT")
        assertEquals(listOf(d.attempt.id to S.TIMED_OUT), rec.calls)
        val before = r.toString()
        assertTrue(finished.await(3, TimeUnit.SECONDS))           // the Python call completes LATE
        Thread.sleep(100)
        assertEquals(before, r.toString())                         // its plans never reach the published result
        assertEquals(S.TIMED_OUT, pub.stateOf(d.attempt))
        assertEquals(S.TIMED_OUT, pub.tryAccept(d.attempt, 0L, Long.MAX_VALUE))   // a late accept cannot promote it
        // the following poll: a new attempt (generation + 1) is accepted and published
        val r2 = result()
        val d2 = ExpiryFallbackPublication.applyAndPublish(r2, pub, key, 2_000L, Long.MAX_VALUE, { 0L },
            { a -> patchFor(a) }, rec::add)
        assertEquals(S.APPLY_ACCEPTED, d2.state)                 // R4: accepted in memory, not yet published
        assertEquals(d.attempt.generation + 1, d2.attempt.generation)
        assertEquals("NSE_FO|NF2026120124000CE", r2.getJSONObject("single_leg_by_index").getJSONObject("NF").getString("instrument_key"))
        assertEquals(d2.attempt.id, fb(r2, "NF").getJSONObject("attempt").getString("attempt_id"))
        assertEquals(listOf(d.attempt.id to S.TIMED_OUT), rec.calls)  // R4: no acknowledgement before the final gate
        val auth = AnalysisAuthority().apply { attachRevisionSource { "rev-1" } }
        val out = ExpiryFallbackPublication.publishThroughGate(pub, d2.attempt, auth,
            AnalysisAuthority.Identity("2026-11-23", "rev-1", 1_000L, "POLL"), { 5L }, { true }) { rec.add(it.attempt, it.state) }
        assertEquals(AnalysisAuthority.Outcome.PUBLISHED, out)
        assertEquals(listOf(d.attempt.id to S.TIMED_OUT, d2.attempt.id to S.PUBLISHED), rec.calls)
    }

    @Test fun returnedAfterTheNativeDeadlineButBeforeAcceptIsTimedOut() {
        val pub = ExpiryFallbackPublication(ExpiryFallbackPublication.MemoryStore())
        val rec = Recon()
        val r = result()
        val deadline = 1_000L
        val d = ExpiryFallbackPublication.applyAndPublish(r, pub, "test.efb.gap." + System.nanoTime(), 2_000L, deadline,
            { deadline + 1 }, { a -> patchFor(a) }, rec::add)                  // Python returned; native too late
        assertEquals(S.TIMED_OUT, d.state)
        assertWaited(r, "APPLY_TIMEOUT")
        assertEquals(listOf(d.attempt.id to S.TIMED_OUT), rec.calls)
    }

    @Test fun aNewerAttemptCancelsTheOlderOneAndOldWorkCannotOverwriteIt() {
        val pub = ExpiryFallbackPublication(ExpiryFallbackPublication.MemoryStore())
        val rec = Recon()
        val r = result()
        var newer: ExpiryFallbackPublication.Attempt? = null
        // while the first apply runs, a newer poll begins its own attempt (e.g. a refresh / next poll)
        val d = ExpiryFallbackPublication.applyAndPublish(r, pub, "test.efb.sup." + System.nanoTime(), 2_000L, Long.MAX_VALUE,
            { 0L }, { a -> val (n, cancelled) = pub.begin(); newer = n; cancelled.forEach { rec.add(it.attempt, it.state) }; patchFor(a) },
            rec::add)
        assertEquals(S.CANCELLED, d.state)
        assertWaited(r, "APPLY_SUPERSEDED")
        assertEquals(S.CANCELLED, pub.tryAccept(d.attempt, 0L, Long.MAX_VALUE))
        assertEquals(S.APPLY_ACCEPTED, pub.tryAccept(newer!!, 0L, Long.MAX_VALUE))
        assertTrue(newer!!.generation > d.attempt.generation)
        // the cancel was reconciled once (by begin), the old attempt's own decision repeats the same final state
        assertEquals(listOf(d.attempt.id to S.CANCELLED, d.attempt.id to S.CANCELLED), rec.calls)
    }

    @Test fun restartKeepsGenerationMonotonicAndAnOldAttemptIsNeverPublished() {
        val store = ExpiryFallbackPublication.MemoryStore()
        val before = ExpiryFallbackPublication(store)
        val (old, _) = before.begin()                             // process dies with this attempt pending
        val after = ExpiryFallbackPublication(store)               // restart: same persisted generation store
        assertEquals(S.UNKNOWN, after.tryAccept(old, 0L, Long.MAX_VALUE))
        val rec = Recon()
        val r = result()
        val d = ExpiryFallbackPublication.applyAndPublish(r, after, "test.efb.rst." + System.nanoTime(), 2_000L, Long.MAX_VALUE,
            { 0L }, { a -> patchFor(a) }, rec::add)
        assertEquals(S.APPLY_ACCEPTED, d.state)
        assertTrue(d.attempt.generation > old.generation)
        assertNotEquals(old.id, d.attempt.id)
    }

    @Test fun pythonDeadlineOrErrorIsNeverPublished() {
        for ((err, st, code) in listOf(Triple("APPLY_DEADLINE_EXCEEDED", S.TIMED_OUT, "APPLY_DEADLINE_EXCEEDED"),
                                       Triple("EXPIRY_FALLBACK_APPLY_ERROR:KeyError", S.CANCELLED, "APPLY_ERROR"))) {
            val pub = ExpiryFallbackPublication(ExpiryFallbackPublication.MemoryStore())
            val rec = Recon()
            val r = result()
            val d = ExpiryFallbackPublication.applyAndPublish(r, pub, "test.efb.err." + System.nanoTime(), 2_000L,
                Long.MAX_VALUE, { 0L }, { JSONObject().put("error", err).toString() }, rec::add)
            assertEquals(st, d.state)
            assertWaited(r, code)
            assertEquals(listOf(d.attempt.id to st), rec.calls)
        }
    }

    @Test fun aSecondCallWhileTheTimedOutOneIsStillRunningIsTimedOutWithoutAnotherPythonCall() {
        val pub = ExpiryFallbackPublication(ExpiryFallbackPublication.MemoryStore())
        val rec = Recon()
        val key = "test.efb.inflight." + System.nanoTime()
        var calls = 0
        val d1 = ExpiryFallbackPublication.applyAndPublish(result(), pub, key, 100L, Long.MAX_VALUE, { 0L },
            { a -> synchronized(this) { calls++ }; Thread.sleep(600); patchFor(a) }, rec::add)
        val r2 = result()
        val d2 = ExpiryFallbackPublication.applyAndPublish(r2, pub, key, 100L, Long.MAX_VALUE, { 0L },
            { a -> synchronized(this) { calls++ }; patchFor(a) }, rec::add)
        assertEquals(S.TIMED_OUT, d1.state)
        assertEquals(S.TIMED_OUT, d2.state)
        assertWaited(r2, "APPLY_TIMEOUT")
        assertEquals(1, synchronized(this) { calls })
        Thread.sleep(700)
    }
}
