package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * B1.1 R2 — Codex verdict §5: queue contents and the queue-derived tracking
 * flags are ONE atomic state transition under PositionTickQueueLock.
 *
 * Deterministic interleavings: the coordinator's post-drain seam
 * (afterDrainHook: drain has returned / chunks are acknowledged, tracking flags
 * not yet committed) starts a real second thread that performs the production
 * enqueue transition (admitAndCommitPositionTicksLocked under the shared lock)
 * and joins it, so the order "ack -> concurrent enqueue -> flag commit" is fixed.
 * The store also asserts that every read/write of the queue-derived flags
 * happens while the shared lock is held.
 */
class PositionTickTrackingAtomicityTest {

    private val max = POSITION_TICK_MAX_PENDING
    private val lock = Any()

    private class LockCheckedKv(private val lock: Any) : PositionTickKv {
        val map = HashMap<String, Any>()
        val violations = mutableListOf<String>()
        private val guarded = setOf(
            PREF_POSITION_TICK_OVERFLOW_ACTIVE, PREF_POSITION_TICK_TRACKING_COMPLETE,
            PREF_POSITION_TICK_OVERFLOW_REJECTED_COUNT
        )
        private fun check(key: String, op: String) {
            if (key in guarded && !Thread.holdsLock(lock)) violations.add("$op $key without PositionTickQueueLock")
        }
        @Synchronized override fun getString(key: String, def: String?): String? = map[key] as? String ?: def
        @Synchronized override fun getInt(key: String, def: Int): Int = map[key] as? Int ?: def
        @Synchronized override fun getLong(key: String, def: Long): Long { check(key, "read"); return map[key] as? Long ?: def }
        @Synchronized override fun getBoolean(key: String, def: Boolean): Boolean { check(key, "read"); return map[key] as? Boolean ?: def }
        override fun edit(durable: Boolean, block: PositionTickKvEditor.() -> Unit) {
            val staged = HashMap<String, Any>()
            object : PositionTickKvEditor {
                override fun putString(key: String, value: String) { staged[key] = value }
                override fun putInt(key: String, value: Int) { staged[key] = value }
                override fun putLong(key: String, value: Long) { check(key, "write"); staged[key] = value }
                override fun putBoolean(key: String, value: Boolean) { check(key, "write"); staged[key] = value }
            }.block()
            synchronized(this) { map.putAll(staged) }
        }
        fun queue(): JSONArray = JSONArray(getString(PREF_POSITION_TICK_PENDING_QUEUE, "[]"))
        fun flag(key: String, def: Boolean) = map[key] as? Boolean ?: def
        fun rejected() = map[PREF_POSITION_TICK_OVERFLOW_REJECTED_COUNT] as? Long ?: 0L
    }

    private class NullWork : PositionTickWorkScheduler {
        override fun enqueue(delayMs: Long, onResult: (Boolean) -> Unit) { onResult(true) }
        override fun cancel() {}
    }

    private fun row(tag: String, i: Int) = JSONObject().put("trade_id", tag).put("seq", i)
        .put("tick_ts", "2026-09-24T04:00:00.000Z").put("current_pnl", i * 1.5)

    private fun rows(tag: String, n: Int) = JSONArray().apply { repeat(n) { put(row(tag, it)) } }

    private fun seed(kv: LockCheckedKv, n: Int, overflow: Boolean, complete: Boolean, rejected: Long) {
        synchronized(lock) {
            kv.edit {
                putString(PREF_POSITION_TICK_PENDING_QUEUE, rows("OLD", n).toString())
                putBoolean(PREF_POSITION_TICK_OVERFLOW_ACTIVE, overflow)
                putBoolean(PREF_POSITION_TICK_TRACKING_COMPLETE, complete)
                putLong(PREF_POSITION_TICK_OVERFLOW_REJECTED_COUNT, rejected)
            }
        }
    }

    /** Production enqueue transition on a real second thread, joined (deterministic order). */
    private fun concurrentEnqueue(kv: LockCheckedKv, incoming: JSONArray): PositionTickEnqueueCommit {
        var out: PositionTickEnqueueCommit? = null
        val t = Thread { out = synchronized(lock) { admitAndCommitPositionTicksLocked(kv, incoming, max, durable = false) } }
        t.start(); t.join()
        return out!!
    }

    private fun coordinator(kv: LockCheckedKv, acked: MutableList<JSONObject>, maxChunks: Int, hook: () -> Unit) =
        PositionTickDrainCoordinator(
            kv = kv,
            transport = PositionTickTransport { body ->
                for (i in 0 until body.length()) acked.add(body.getJSONObject(i))
                classifyPositionTickFlushFailure(201, "Created", null, null, null, body.length())
            },
            networkAvailable = { true },
            clock = { 1_790_000_000_000L },
            scheduler = NullWork(),
            lock = lock,
            config = PositionTickDrainConfig(maxChunksPerDrain = maxChunks),
            afterDrainHook = hook
        )

    private fun tags(a: JSONArray) = (0 until a.length()).map { a.getJSONObject(it).let { r -> r.getString("trade_id") + ":" + r.getInt("seq") } }

    @Test fun ackedChunk_thenConcurrentOverflowBeforeCommit_overflowStaysVisible_rowsIntactAndOrdered() {
        val kv = LockCheckedKv(lock)
        seed(kv, max, overflow = false, complete = true, rejected = 0L)
        val acked = mutableListOf<JSONObject>()
        var commit: PositionTickEnqueueCommit? = null
        val r = coordinator(kv, acked, maxChunks = 1) {
            // 1) drain acknowledged one chunk (50 rows) -> 1450 queued, capacity 50.
            assertEquals(max - 50, kv.queue().length())
            // 2) before the flag commit, a concurrent enqueue fills and overflows the queue.
            commit = concurrentEnqueue(kv, rows("NEW", 60))
        }.runPass(PositionTickDrainTrigger.APP_START)

        assertEquals(50, r.report!!.rowsAcked)
        assertEquals(50, commit!!.admitted); assertEquals(10, commit!!.rejected); assertTrue(commit!!.overflowed)
        // 3) final state keeps overflow_active=true, the increased rejected total, tracking_complete=false.
        assertTrue(kv.flag(PREF_POSITION_TICK_OVERFLOW_ACTIVE, false))
        assertEquals(10L, kv.rejected())
        assertFalse(kv.flag(PREF_POSITION_TICK_TRACKING_COMPLETE, true))
        assertEquals(true, r.overflowActive); assertEquals(false, r.trackingComplete)
        // 4) queued rows intact and ordered: OLD 50..1499, then NEW 0..49.
        val expected = (50 until max).map { "OLD:$it" } + (0 until 50).map { "NEW:$it" }
        assertEquals(expected, tags(kv.queue()))
        assertEquals((0 until 50).map { "OLD:$it" }, acked.map { it.getString("trade_id") + ":" + it.getInt("seq") })
        assertTrue(kv.violations.toString(), kv.violations.isEmpty())
    }

    @Test fun concurrentEnqueueWithoutOverflow_isNotLost_andDoesNotFalselyCompleteTracking() {
        val kv = LockCheckedKv(lock)
        seed(kv, 100, overflow = true, complete = false, rejected = 3L)
        val acked = mutableListOf<JSONObject>()
        val r = coordinator(kv, acked, maxChunks = 40) { concurrentEnqueue(kv, rows("NEW", 5)) }
            .runPass(PositionTickDrainTrigger.APP_START)
        assertEquals(PositionTickDrainOutcome.DRAINED, r.report!!.outcome)
        assertEquals((0 until 5).map { "NEW:$it" }, tags(kv.queue()))
        assertFalse(kv.flag(PREF_POSITION_TICK_OVERFLOW_ACTIVE, true))
        assertFalse("history keeps tracking incomplete", kv.flag(PREF_POSITION_TICK_TRACKING_COMPLETE, true))
        assertEquals(3L, kv.rejected())
        assertEquals("rows enqueued meanwhile get a durable continuation", 1_790_000_000_000L, r.nextAttemptAtMs)
        assertTrue(kv.violations.toString(), kv.violations.isEmpty())
    }

    @Test fun inverse_progressAfterEarlierOverflow_clearsOnlyActiveFlag_whenCapacityGenuinelyFree() {
        // (a) capacity really freed -> active flag cleared; history + incomplete tracking kept.
        val kv = LockCheckedKv(lock)
        seed(kv, max, overflow = true, complete = false, rejected = 7L)
        coordinator(kv, mutableListOf(), maxChunks = 1) {}.runPass(PositionTickDrainTrigger.APP_START)
        assertEquals(max - 50, kv.queue().length())
        assertFalse(kv.flag(PREF_POSITION_TICK_OVERFLOW_ACTIVE, true))
        assertFalse(kv.flag(PREF_POSITION_TICK_TRACKING_COMPLETE, true))
        assertEquals(7L, kv.rejected())

        // (b) progress, but a concurrent enqueue refilled the freed capacity (no rejection)
        //     before the commit -> queue full again -> active flag must stay set.
        val kv2 = LockCheckedKv(lock)
        seed(kv2, max, overflow = true, complete = false, rejected = 7L)
        coordinator(kv2, mutableListOf(), maxChunks = 1) {
            val c = concurrentEnqueue(kv2, rows("NEW", 50))
            assertEquals(0, c.rejected)
        }.runPass(PositionTickDrainTrigger.APP_START)
        assertEquals(max, kv2.queue().length())
        assertTrue("capacity not genuinely free", kv2.flag(PREF_POSITION_TICK_OVERFLOW_ACTIVE, false))
        assertFalse(kv2.flag(PREF_POSITION_TICK_TRACKING_COMPLETE, true))
        assertEquals(7L, kv2.rejected())
        assertTrue(kv.violations.isEmpty() && kv2.violations.isEmpty())
    }

    @Test fun noHistory_progress_restoresTrackingComplete_andRejectedTotalNeverReduced() {
        val kv = LockCheckedKv(lock)
        seed(kv, 80, overflow = true, complete = false, rejected = 0L)
        coordinator(kv, mutableListOf(), maxChunks = 40) {}.runPass(PositionTickDrainTrigger.APP_START)
        assertFalse(kv.flag(PREF_POSITION_TICK_OVERFLOW_ACTIVE, true))
        assertTrue(kv.flag(PREF_POSITION_TICK_TRACKING_COMPLETE, false))
        assertEquals(0L, kv.rejected())
        // Rejected total only grows (enqueue) and is never written by the drain.
        val kv2 = LockCheckedKv(lock)
        seed(kv2, max, overflow = true, complete = false, rejected = 42L)
        coordinator(kv2, mutableListOf(), maxChunks = 40) { concurrentEnqueue(kv2, rows("NEW", 1)) }
            .runPass(PositionTickDrainTrigger.APP_START)
        assertEquals(42L, kv2.rejected())
    }

    @Test fun enqueueTransition_alone_isOneAtomicQueuePlusFlagsEdit() {
        val kv = LockCheckedKv(lock)
        seed(kv, max - 2, overflow = false, complete = true, rejected = 4L)
        val c = synchronized(lock) { admitAndCommitPositionTicksLocked(kv, rows("NEW", 5), max, durable = true) }
        assertEquals(2, c.admitted); assertEquals(3, c.rejected); assertEquals(7L, c.rejectedTotal)
        assertEquals(max, kv.queue().length())
        assertTrue(kv.flag(PREF_POSITION_TICK_OVERFLOW_ACTIVE, false))
        assertFalse(kv.flag(PREF_POSITION_TICK_TRACKING_COMPLETE, true))
        assertEquals(7L, kv.rejected())
        // Capacity available, no history -> active cleared and tracking restored.
        val kv2 = LockCheckedKv(lock)
        seed(kv2, 10, overflow = true, complete = false, rejected = 0L)
        synchronized(lock) { admitAndCommitPositionTicksLocked(kv2, rows("NEW", 1), max, durable = false) }
        assertFalse(kv2.flag(PREF_POSITION_TICK_OVERFLOW_ACTIVE, true))
        assertTrue(kv2.flag(PREF_POSITION_TICK_TRACKING_COMPLETE, false))
        assertEquals(11, kv2.queue().length())
        assertTrue(kv.violations.isEmpty() && kv2.violations.isEmpty())
    }
}
