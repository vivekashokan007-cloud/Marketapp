package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.util.concurrent.CompletableFuture

/**
 * B1.1 R3 — Codex R3 verdict: Blocker A (phantom durable-job reservation) and
 * Blocker B (unexpected worker/executor exception), test groups 1–7.
 */
class PositionTickDurabilityR3Test {

    // ---------------------------------------------------------------- fixtures

    private class MapKv : PositionTickKv {
        val map = HashMap<String, Any>()
        @Synchronized override fun getString(key: String, def: String?): String? = map[key] as? String ?: def
        @Synchronized override fun getInt(key: String, def: Int): Int = map[key] as? Int ?: def
        @Synchronized override fun getLong(key: String, def: Long): Long = map[key] as? Long ?: def
        @Synchronized override fun getBoolean(key: String, def: Boolean): Boolean = map[key] as? Boolean ?: def
        override fun edit(durable: Boolean, block: PositionTickKvEditor.() -> Unit) {
            val staged = HashMap<String, Any>()
            object : PositionTickKvEditor {
                override fun putString(key: String, value: String) { staged[key] = value }
                override fun putInt(key: String, value: Int) { staged[key] = value }
                override fun putLong(key: String, value: Long) { staged[key] = value }
                override fun putBoolean(key: String, value: Boolean) { staged[key] = value }
            }.block()
            synchronized(this) { map.putAll(staged) }
        }
        fun queueRaw(): String = getString(PREF_POSITION_TICK_PENDING_QUEUE, "[]")!!
        fun at() = getLong(PREF_POSITION_TICK_NEXT_DRAIN_AT_MS, 0L)
        fun token() = getLong(PREF_POSITION_TICK_NEXT_DRAIN_TOKEN, 0L)
        fun confirmed() = getBoolean(PREF_POSITION_TICK_NEXT_DRAIN_CONFIRMED, false)
    }

    /** WorkManager model: SYNC_OK completes immediately; DEFER holds callbacks; THROW fails synchronously. */
    private class Work(var mode: String = "SYNC_OK") : PositionTickWorkScheduler {
        val enqueues = mutableListOf<Long>()
        val callbacks = mutableListOf<(Boolean) -> Unit>()
        var cancels = 0
        override fun enqueue(delayMs: Long, onResult: (Boolean) -> Unit) {
            if (mode == "THROW") { enqueues.add(-1L); throw IllegalStateException("WorkManager is not initialized") }
            enqueues.add(delayMs)
            if (mode == "DEFER") callbacks.add(onResult) else onResult(true)
        }
        override fun cancel() { cancels += 1 }
        fun realEnqueues() = enqueues.count { it >= 0 }
    }

    private class Clock(var now: Long = 1_790_000_000_000L)

    private val logs = mutableListOf<String>()
    private val log: (Char, String) -> Unit = { l, s -> logs.add("$l $s") }

    private fun seed(kv: MapKv, n: Int) = kv.edit {
        putString(PREF_POSITION_TICK_PENDING_QUEUE, JSONArray().apply {
            repeat(n) { put(JSONObject().put("trade_id", "T1").put("seq", it).put("tick_ts", "2026-09-24T04:00:00.000Z")) }
        }.toString())
    }

    private fun ok201(body: JSONArray) = classifyPositionTickFlushFailure(201, "Created", null, null, null, body.length())

    private fun coordinator(kv: MapKv, work: Work, clock: Clock, sendId: () -> Boolean = { false }) =
        PositionTickDrainCoordinator(
            kv = kv, transport = PositionTickTransport { ok201(it) }, networkAvailable = { true },
            clock = { clock.now }, scheduler = work, sendClientEventId = sendId, log = log, lock = Any()
        )

    private fun src(rel: String): String = listOf(File(rel), File("app/$rel")).first { it.isFile }.readText()

    // ------------------------------------------------ 1. synchronous enqueue throw

    @Test fun g1_syncEnqueueThrow_finishOnce_noConfirmedSlot_nextTriggerEnqueuesAgain() {
        val kv = MapKv(); val clock = Clock(); val work = Work("THROW")
        var finished = 0
        enqueuePositionTickDrainFromReceiver(kv, work, clock.now, log) { finished += 1 }
        assertEquals("PendingResult finished exactly once", 1, finished)
        assertEquals(0L, kv.at()); assertEquals(0L, kv.token()); assertFalse(kv.confirmed())
        assertTrue(logs.any { it.contains("POSITION_TICK_DURABLE_ENQUEUE_FAIL: stage=sync ex=IllegalStateException") })
        assertTrue(logs.any { it.contains("POSITION_TICK_RECEIVER_ENQUEUE: result=sync_failure") })
        // Next trigger: WorkManager healthy again -> a REAL enqueue happens.
        work.mode = "SYNC_OK"
        var outcome: String? = null
        assertTrue(requestDurablePositionTickDrain(kv, work, clock.now, clock.now + 30_000L, log) { _, r -> outcome = r })
        assertEquals(1, work.realEnqueues())
        assertEquals(PositionTickEnqueueResult.CONFIRMED, outcome)
        assertTrue(kv.confirmed()); assertEquals(clock.now + 30_000L, kv.at())
    }

    // ------------------------------------------ 2. asynchronous operation failure

    @Test fun g2_asyncOperationFailure_notDurable_reservationRemoved_finishOnce_nextTriggerEnqueues() {
        val kv = MapKv(); val clock = Clock(); val work = Work("DEFER")
        var finished = 0
        enqueuePositionTickDrainFromReceiver(kv, work, clock.now, log) { finished += 1 }
        assertEquals("finish waits for the operation result", 0, finished)
        assertFalse("pending reservation is not confirmed", kv.confirmed())
        val token = kv.token(); assertTrue(token > 0)
        work.callbacks.single().invoke(false)                 // Operation future failed
        assertEquals(1, finished)
        assertEquals(0L, kv.at()); assertEquals(0L, kv.token()); assertFalse(kv.confirmed())
        assertTrue(logs.any { it.contains("POSITION_TICK_DURABLE_ENQUEUE_FAIL: stage=async") })
        assertTrue(logs.any { it.contains("POSITION_TICK_RECEIVER_ENQUEUE: result=async_failure") })
        work.callbacks.single().invoke(false)                 // a duplicate completion cannot finish twice
        assertEquals(1, finished)
        // Next trigger performs a real enqueue.
        work.mode = "SYNC_OK"
        requestDurablePositionTickDrain(kv, work, clock.now, clock.now, log)
        assertEquals(2, work.realEnqueues()); assertTrue(kv.confirmed())
    }

    @Test fun g2_pendingReservation_isNeverTrusted_byLaterTrigger_orAfterProcessDeath() {
        val kv = MapKv(); val clock = Clock(); val work = Work("DEFER")
        requestDurablePositionTickDrain(kv, work, clock.now, clock.now + 10_000L, log)
        // No completion yet (or process died before it arrived): a later trigger must enqueue for real,
        // at the pending (earlier) time — never later.
        var outcome: String? = null
        assertTrue(requestDurablePositionTickDrain(kv, work, clock.now, clock.now + 60_000L, log) { _, r -> outcome = r })
        assertEquals(listOf(10_000L, 10_000L), work.enqueues)
        assertEquals(null, outcome)
        work.callbacks[1].invoke(true)
        assertEquals(PositionTickEnqueueResult.CONFIRMED, outcome)
        assertTrue(kv.confirmed())
    }

    @Test fun g2_operationFutureAdapter_successOnlyOnNormalCompletion() {
        val results = mutableListOf<Boolean>()
        completePositionTickEnqueueFuture(CompletableFuture.completedFuture("SUCCESS")) { results.add(it) }
        completePositionTickEnqueueFuture(CompletableFuture<Any>().apply { completeExceptionally(IllegalStateException("db")) }) { results.add(it) }
        completePositionTickEnqueueFuture(CompletableFuture<Any>().apply { cancel(true) }) { results.add(it) }
        assertEquals(listOf(true, false, false), results)
        val runner = src("src/main/java/com/marketradar/app/PositionTickUploadRunner.kt")
        assertTrue(runner.contains("future.addListener({ completePositionTickEnqueueFuture(future, onResult) }, Runnable::run)"))
        assertFalse("no terminal-state-only callback", runner.contains("onDurable"))
    }

    // -------------------------------------------- 3. failure must not erase newer work

    @Test fun g3_lateFailureOfStaleAttempt_cannotClearOrCancelNewerReservation() {
        val kv = MapKv(); val clock = Clock(); val work = Work("DEFER")
        requestDurablePositionTickDrain(kv, work, clock.now, clock.now + 60_000L, log)   // attempt A (pending)
        val tokenA = kv.token()
        requestDurablePositionTickDrain(kv, work, clock.now, clock.now + 10_000L, log)   // attempt B (earlier)
        val tokenB = kv.token()
        assertTrue(tokenB != tokenA)
        work.callbacks[0].invoke(false)                                                    // A fails late
        assertEquals("B's reservation survives", tokenB, kv.token())
        assertEquals(clock.now + 10_000L, kv.at())
        assertEquals("no cancel of B's work", 0, work.cancels)
        work.callbacks[0].invoke(true)                                                     // stale success can't confirm B
        assertFalse(kv.confirmed())
        work.callbacks[1].invoke(true)                                                     // B confirmed
        assertTrue(kv.confirmed()); assertEquals(tokenB, kv.token())
        // And settle() with a stale token is a no-op.
        assertFalse(settlePositionTickReservation(kv, tokenA, false))
        assertEquals(tokenB, kv.token())
    }

    // ------------------------------------------------ 4. earlier work not postponed

    @Test fun g4_confirmedEarlierWork_isNeverPostponed() {
        val kv = MapKv(); val clock = Clock(); val work = Work()
        requestDurablePositionTickDrain(kv, work, clock.now, clock.now + 10_000L, log)
        for (later in listOf(20_000L, 60_000L, 6 * 3_600_000L, 10_000L)) {
            var r: String? = null
            assertFalse(requestDurablePositionTickDrain(kv, work, clock.now, clock.now + later, log) { _, x -> r = x })
            assertEquals(PositionTickEnqueueResult.KEPT_CONFIRMED, r)
        }
        assertEquals(listOf(10_000L), work.enqueues)
        assertEquals(clock.now + 10_000L, kv.at())
    }

    // ------------------------------------------------ 5. unexpected worker exception

    @Test fun g5_unexpectedWorkerException_returnsRetry_queueKept_backoffBoundedNotZero() {
        val kv = MapKv(); val clock = Clock(); val work = Work()
        seed(kv, 25)
        requestDurablePositionTickDrain(kv, work, clock.now, clock.now, log)   // the job now running
        val before = kv.queueRaw()
        val c = coordinator(kv, work, clock, sendId = { throw IllegalStateException("boom") })
        val result = runPositionTickWorker(kv, { clock.now }, 0, log) { c.runWorkerPass() }
        assertEquals(PositionTickWorkerResult.RETRY, result)
        assertEquals("queue preserved on the exception path", before, kv.queueRaw())
        assertEquals("reservation records WorkManager's retry", clock.now + POSITION_TICK_WORKER_RETRY_BACKOFF_MS, kv.at())
        assertTrue(kv.confirmed())
        assertTrue(logs.any { it.contains("POSITION_TICK_DRAIN_WORKER_ERROR: ex=IllegalStateException attempt=0 result=retry") })
        // Bounded, never zero: >= WorkManager's 10 s minimum, <= 5 h cap, monotone.
        val delays = (0..30).map { positionTickWorkerRetryDelayMs(it) }
        assertTrue(delays.all { it >= 10_000L && it <= POSITION_TICK_WORKER_RETRY_MAX_BACKOFF_MS })
        assertEquals(delays, delays.sorted())
        assertEquals(POSITION_TICK_WORKER_RETRY_MAX_BACKOFF_MS, positionTickWorkerRetryDelayMs(1_000))
        // The worker maps RETRY to Result.retry() and the request carries the explicit backoff.
        val w = src("src/main/java/com/marketradar/app/PositionTickDrainWorker.kt")
        assertTrue(w.contains("PositionTickWorkerResult.RETRY -> Result.retry()"))
        assertFalse(w.contains("catch"))
        val runner = src("src/main/java/com/marketradar/app/PositionTickUploadRunner.kt")
        assertTrue(runner.contains("setBackoffCriteria(BackoffPolicy.EXPONENTIAL, POSITION_TICK_WORKER_RETRY_BACKOFF_MS, TimeUnit.MILLISECONDS)"))
    }

    // ----------------------------------------------- 6. unexpected executor exception

    @Test fun g6_executorException_armsDurableFallback_coalesces_queueUnchanged() {
        val kv = MapKv(); val clock = Clock(); val work = Work()
        seed(kv, 12)
        val before = kv.queueRaw()
        repeat(5) {
            clock.now += 1_000L
            runPositionTickExecutorPass(kv, work, { clock.now }, PositionTickDrainTrigger.AFTER_ENQUEUE, log) {
                throw IllegalStateException("bug")
            }
        }
        assertEquals("one coalesced fallback job", listOf(POSITION_TICK_EXECUTOR_FAILURE_RETRY_MS), work.enqueues)
        assertTrue(kv.confirmed())
        assertEquals(before, kv.queueRaw())
        assertEquals(5, logs.count { it.contains("POSITION_TICK_DRAIN_ERROR: trigger=after_enqueue ex=IllegalStateException durable_fallback=armed") })
    }

    @Test fun g6_workManagerUnavailable_failsHonestly_noPhantom_noSpin_nextTriggerRetries() {
        val kv = MapKv(); val clock = Clock(); val work = Work("THROW")
        seed(kv, 3)
        repeat(3) {
            runPositionTickExecutorPass(kv, work, { clock.now }, PositionTickDrainTrigger.SERVICE_START, log) { throw RuntimeException() }
        }
        assertEquals("exactly one attempt per failure, no loop", 3, work.enqueues.size)
        assertEquals(0L, kv.at()); assertFalse(kv.confirmed())
        work.mode = "SYNC_OK"
        runPositionTickExecutorPass(kv, work, { clock.now }, PositionTickDrainTrigger.APP_START, log) { throw RuntimeException() }
        assertEquals(1, work.realEnqueues()); assertTrue(kv.confirmed())
    }

    // --------------------------------------------------- 7. normal paths unchanged

    @Test fun g7_workerSuccessPaths_emptyAndDrained_returnSuccess() {
        val kv = MapKv(); val clock = Clock(); val work = Work()
        assertEquals(PositionTickWorkerResult.SUCCESS,
            runPositionTickWorker(kv, { clock.now }, 0, log) { coordinator(kv, work, clock).runWorkerPass() })
        assertEquals(1, work.cancels)
        seed(kv, 70)
        assertEquals(PositionTickWorkerResult.SUCCESS,
            runPositionTickWorker(kv, { clock.now }, 0, log) { coordinator(kv, work, clock).runWorkerPass() })
        assertEquals("[]", kv.queueRaw())
        assertEquals(0L, kv.at())
        // Classified failures are handled inside the pass (durable follow-up) -> still SUCCESS, not retry.
        seed(kv, 5)
        val failing = PositionTickDrainCoordinator(kv, PositionTickTransport {
            classifyPositionTickFlushFailure(503, "Unavailable", null, null, null, it.length())
        }, { true }, { clock.now }, work, log = log, lock = Any())
        clock.now += 10_000L
        assertEquals(PositionTickWorkerResult.SUCCESS, runPositionTickWorker(kv, { clock.now }, 0, log) { failing.runWorkerPass() })
        assertTrue(kv.confirmed())
        assertEquals(clock.now + computePositionTickFlushBackoffMs(1, POSITION_TICK_FLUSH_SERVER_5XX), kv.at())
    }
}
