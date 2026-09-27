package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * B1.1 R2 — Codex verdict §4 acceptance tests (durable delayed retry).
 *
 * The coordinator is exercised with an in-memory key-value store (same keys as
 * SharedPreferences), a fake clock, a scripted transport and a fake durable
 * scheduler that models WorkManager unique one-time work with REPLACE: at most
 * one pending job, whose due time survives "process death" because it lives in
 * the scheduler/kv objects rather than in the coordinator instance.
 */
class PositionTickDrainSchedulerTest {

    // ---------------------------------------------------------------- fixtures

    private class MapKv : PositionTickKv {
        val map = HashMap<String, Any>()
        var commits = 0
        override fun getString(key: String, def: String?): String? = map[key] as? String ?: def
        override fun getInt(key: String, def: Int): Int = map[key] as? Int ?: def
        override fun getLong(key: String, def: Long): Long = map[key] as? Long ?: def
        override fun getBoolean(key: String, def: Boolean): Boolean = map[key] as? Boolean ?: def
        override fun edit(durable: Boolean, block: PositionTickKvEditor.() -> Unit) {
            val staged = HashMap<String, Any>()
            object : PositionTickKvEditor {
                override fun putString(key: String, value: String) { staged[key] = value }
                override fun putInt(key: String, value: Int) { staged[key] = value }
                override fun putLong(key: String, value: Long) { staged[key] = value }
                override fun putBoolean(key: String, value: Boolean) { staged[key] = value }
            }.block()
            map.putAll(staged)
            if (durable) commits += 1
        }
        fun queue(): JSONArray = JSONArray(getString(PREF_POSITION_TICK_PENDING_QUEUE, "[]"))
    }

    /** Unique one-time job with REPLACE semantics; delays are recorded relative to [clock]. */
    private class FakeWork(val clock: () -> Long, val deferDurable: Boolean = false) : PositionTickWorkScheduler {
        var pendingDueAt: Long? = null
        val enqueues = mutableListOf<Long>()
        var cancels = 0
        val deferred = mutableListOf<(Boolean) -> Unit>()
        override fun enqueue(delayMs: Long, onResult: (Boolean) -> Unit) {
            enqueues.add(delayMs)
            pendingDueAt = clock() + delayMs
            if (deferDurable) deferred.add(onResult) else onResult(true)
        }
        override fun cancel() { cancels += 1; pendingDueAt = null }
    }

    private class Clock(var now: Long = 1_790_000_000_000L) { fun get() = now }

    private class Server {
        val posted = mutableListOf<JSONObject>()
        val requests = mutableListOf<Int>()
        val script = ArrayDeque<Int>() // http status per request; 0 = transport timeout; 201 default
        fun insert(body: JSONArray): PositionTickInsertResult {
            requests.add(body.length())
            return when (val status = script.removeFirstOrNull() ?: 201) {
                0 -> classifyPositionTickFlushFailure(null, null, "java.net.SocketTimeoutException", "timeout", null, body.length())
                201 -> {
                    for (i in 0 until body.length()) posted.add(body.getJSONObject(i))
                    classifyPositionTickFlushFailure(201, "Created", null, null, null, body.length())
                }
                400 -> classifyPositionTickFlushFailure(400, "Bad", null, null, "PGRST204", body.length())
                401 -> classifyPositionTickFlushFailure(401, "Unauthorized", null, null, null, body.length())
                else -> classifyPositionTickFlushFailure(status, "Err", null, null, null, body.length())
            }
        }
    }

    private fun row(i: Int) = JSONObject().put("trade_id", "T1").put("tick_ts", "2026-09-24T04:%02d:%02d.000Z".format(i / 60, i % 60))
        .put("session_date", "2026-09-24").put("source", "P1_REST_60S")
        .put("current_pnl", -100.0 + i).put("status", "OPEN")

    private fun seed(kv: MapKv, n: Int) {
        kv.edit { putString(PREF_POSITION_TICK_PENDING_QUEUE, JSONArray().apply { repeat(n) { put(row(it)) } }.toString()) }
    }

    private fun coordinator(
        kv: MapKv, server: Server, clock: Clock, work: FakeWork,
        network: () -> Boolean = { true }, maxChunks: Int = POSITION_TICK_UPLOAD_MAX_CHUNKS_PER_DRAIN
    ) = PositionTickDrainCoordinator(
        kv = kv,
        transport = PositionTickTransport { server.insert(it) },
        networkAvailable = network,
        clock = { clock.get() },
        scheduler = work,
        lock = Any(),
        config = PositionTickDrainConfig(maxChunksPerDrain = maxChunks)
    )

    private fun src(rel: String): String = listOf(File(rel), File("app/$rel")).first { it.isFile }.readText()
    /** Source without comments (banned-reference checks look at code only). */
    private fun code(rel: String): String =
        src(rel).replace(Regex("/\\*[\\s\\S]*?\\*/"), "").lines().joinToString("\n") { it.substringBefore("//") }
    private fun next(kv: MapKv) = kv.getLong(PREF_POSITION_TICK_NEXT_DRAIN_AT_MS, 0L)

    // ---------------------------------------------------------------- 1

    @Test fun t01_backoffDeniedAttempt_schedulesExactlyOneJob_atRequiredFutureTime() {
        val kv = MapKv(); val clock = Clock(); val work = FakeWork(clock::get); val server = Server()
        seed(kv, 10)
        kv.edit {
            putInt(PREF_POSITION_TICK_FLUSH_FAILURE_COUNT, 2)
            putString(PREF_POSITION_TICK_FLUSH_LAST_CLASS, POSITION_TICK_FLUSH_SERVER_5XX)
            putLong(PREF_POSITION_TICK_LAST_FLUSH_MS, clock.now - 30_000L)
        }
        val required = computePositionTickFlushBackoffMs(2, POSITION_TICK_FLUSH_SERVER_5XX) - 30_000L
        val r = coordinator(kv, server, clock, work).runPass(PositionTickDrainTrigger.AFTER_ENQUEUE)
        assertEquals(PositionTickPassOutcome.DEFERRED, r.outcome)
        assertEquals(listOf(required), work.enqueues)
        assertEquals(clock.now + required, next(kv))
        assertEquals(clock.now + required, work.pendingDueAt)
        assertTrue("denied attempt must not POST", server.requests.isEmpty())
        assertEquals(10, kv.queue().length())

        // min-spacing denial (no failures) is also scheduled, not just dropped.
        val kv2 = MapKv(); val work2 = FakeWork(clock::get)
        seed(kv2, 3)
        kv2.edit { putLong(PREF_POSITION_TICK_LAST_FLUSH_MS, clock.now - 2_000L) }
        coordinator(kv2, server, clock, work2).runPass(PositionTickDrainTrigger.APP_START)
        assertEquals(listOf(POSITION_TICK_DRAIN_MIN_SPACING_MS - 2_000L), work2.enqueues)

        // When the job fires (WORKER), the pass runs and completes the queue.
        clock.now += required
        val w = coordinator(kv, server, clock, work).runWorkerPass()
        assertEquals(PositionTickPassOutcome.RAN, w.outcome)
        assertEquals(0, kv.queue().length())
        assertEquals(0L, next(kv))
    }

    // ---------------------------------------------------------------- 2

    @Test fun t02_transient5xxAndTransportFailure_scheduleDurableRetry_withExistingBackoff() {
        for (status in listOf(503, 0)) {
            val kv = MapKv(); val clock = Clock(); val work = FakeWork(clock::get); val server = Server()
            seed(kv, 60)
            server.script.add(status)
            val r = coordinator(kv, server, clock, work).runPass(PositionTickDrainTrigger.APP_START)
            val cls = r.report!!.failureClass
            assertTrue(cls, cls in POSITION_TICK_TRANSIENT_FAILURE_CLASSES)
            val backoff = computePositionTickFlushBackoffMs(1, cls)
            assertEquals(listOf(backoff), work.enqueues)
            assertEquals(clock.now + backoff, next(kv))
            assertEquals(clock.now + backoff, r.nextAttemptAtMs)
            assertEquals("rows retained", 60, kv.queue().length())
            assertEquals(1, kv.getInt(PREF_POSITION_TICK_FLUSH_FAILURE_COUNT, 0))
        }
    }

    // ---------------------------------------------------------------- 3

    @Test fun t03_repeatedTriggers_coalesce_andNeverPostponeEarlierJob() {
        val kv = MapKv(); val clock = Clock(); val work = FakeWork(clock::get)
        val t0 = clock.now
        assertTrue(requestDurablePositionTickDrain(kv, work, t0, t0 + 60_000L))
        for (later in listOf(120_000L, 300_000L, 60_000L, 6 * 3_600_000L)) {
            assertFalse(requestDurablePositionTickDrain(kv, work, t0, t0 + later))
        }
        assertEquals("one job only", listOf(60_000L), work.enqueues)
        assertEquals(t0 + 60_000L, work.pendingDueAt)
        // An EARLIER requirement does replace it.
        assertTrue(requestDurablePositionTickDrain(kv, work, t0, t0 + 10_000L))
        assertEquals(t0 + 10_000L, work.pendingDueAt)
        assertEquals(t0 + 10_000L, next(kv))

        // Coordinator level: a denied pass wanting +5 min keeps the earlier no-network job.
        val server = Server(); seed(kv, 5)
        kv.edit {
            putInt(PREF_POSITION_TICK_FLUSH_FAILURE_COUNT, 5)
            putString(PREF_POSITION_TICK_FLUSH_LAST_CLASS, POSITION_TICK_FLUSH_TRANSIENT_NETWORK)
            putLong(PREF_POSITION_TICK_LAST_FLUSH_MS, t0 - 10_000L)
        }
        val r = coordinator(kv, server, clock, work).runPass(PositionTickDrainTrigger.AFTER_ENQUEUE)
        assertEquals(PositionTickPassOutcome.DEFERRED, r.outcome)
        assertTrue("pass wanted a later time", r.nextAttemptAtMs!! > t0 + 10_000L)
        assertEquals(t0 + 10_000L, work.pendingDueAt)
        assertEquals(2, work.enqueues.size)
        assertTrue(server.requests.isEmpty())
    }

    // ---------------------------------------------------------------- 4

    @Test fun t04_processRecreation_executesPendingJob_andResumesFromCommittedQueue() {
        val kv = MapKv(); val clock = Clock(); val work = FakeWork(clock::get); val server = Server()
        seed(kv, 180)
        val original = kv.queue().let { a -> (0 until a.length()).map { a.getJSONObject(it).getString("tick_ts") } }
        server.script.addAll(listOf(201, 503))
        coordinator(kv, server, clock, work).runPass(PositionTickDrainTrigger.APP_START)
        assertEquals(130, kv.queue().length())
        val due = work.pendingDueAt!!
        // --- process death: executor, coordinator and in-memory state are gone. The
        // durable job (scheduler + persisted kv) survives; a NEW coordinator runs it.
        clock.now = due
        val r = coordinator(kv, server, clock, work).runWorkerPass()
        assertEquals(PositionTickPassOutcome.RAN, r.outcome)
        assertEquals(0, kv.queue().length())
        assertEquals("FIFO, no loss, no duplicate", original, server.posted.map { it.getString("tick_ts") })
        assertNull("empty queue cancels", work.pendingDueAt)
        assertEquals(0L, next(kv))
    }

    // ---------------------------------------------------------------- 5

    @Test fun t05_noNetwork_noPost_jobWaitsOnConnectedConstraint_noSpinning() {
        val kv = MapKv(); val clock = Clock(); val work = FakeWork(clock::get); val server = Server()
        seed(kv, 20)
        val c = coordinator(kv, server, clock, work, network = { false })
        repeat(5) { clock.now += 1_000L; assertEquals(PositionTickPassOutcome.NO_NETWORK, c.runPass(PositionTickDrainTrigger.CONNECTIVITY).outcome) }
        assertTrue("no POST without network", server.requests.isEmpty())
        assertEquals("one job, due immediately, gated by CONNECTED", listOf(0L), work.enqueues)
        assertEquals(20, kv.queue().length())
        val runner = src("src/main/java/com/marketradar/app/PositionTickUploadRunner.kt")
        assertTrue(runner.contains("setRequiredNetworkType(NetworkType.CONNECTED)"))
        assertTrue(runner.contains("enqueueUniqueWork(POSITION_TICK_DRAIN_WORK_NAME, ExistingWorkPolicy.REPLACE, request)"))
        assertEquals("position_tick_pending_drain_v1", POSITION_TICK_DRAIN_WORK_NAME)
    }

    // ---------------------------------------------------------------- 6

    @Test fun t06_schemaAuthConfigFailure_noRapidRetryLoop() {
        for (status in listOf(400, 401)) {
            val kv = MapKv(); val clock = Clock(); val work = FakeWork(clock::get); val server = Server()
            seed(kv, 30)
            server.script.add(status)
            // A schema-class bulk rejection now gets one bounded singleton
            // proof. Keep that proof globally rejected too; otherwise the
            // fake server's default 201 would incorrectly model a row-specific
            // problem and drain the queue.
            if (status == 400) server.script.add(status)
            val c = coordinator(kv, server, clock, work)
            val r = c.runPass(PositionTickDrainTrigger.APP_START)
            assertFalse(r.report!!.failureClass in POSITION_TICK_TRANSIENT_FAILURE_CLASSES)
            assertEquals(listOf(POSITION_TICK_DURABLE_FAILURE_RETRY_MS), work.enqueues)
            // A capture-rate stream of triggers for 1 hour: no extra POST, no earlier job.
            repeat(60) {
                clock.now += 60_000L
                c.runPass(PositionTickDrainTrigger.AFTER_ENQUEUE)
                c.runPass(PositionTickDrainTrigger.CONNECTIVITY)
                c.runPass(PositionTickDrainTrigger.SERVICE_START)
            }
            val firstAttemptRequests = if (status == 400) 2 else 1
            assertEquals("only the original bounded attempt", firstAttemptRequests, server.requests.size)
            assertEquals(1, work.enqueues.size)
            assertEquals(30, kv.queue().length())
            // App start/update may retry after the ordinary backoff (bounded).
            c.runPass(PositionTickDrainTrigger.APP_START)
            assertEquals(firstAttemptRequests + 1, server.requests.size)
        }
    }

    // ---------------------------------------------------------------- 7

    @Test fun t07_budgetExhaustion_schedulesContinuation() {
        val kv = MapKv(); val clock = Clock(); val work = FakeWork(clock::get); val server = Server()
        seed(kv, 120)
        val r = coordinator(kv, server, clock, work, maxChunks = 1).runPass(PositionTickDrainTrigger.APP_START)
        assertEquals(PositionTickDrainOutcome.BUDGET_EXHAUSTED, r.report!!.outcome)
        assertEquals(listOf(0L), work.enqueues)
        assertEquals(clock.now, next(kv))
        var guard = 0
        while (work.pendingDueAt != null && guard++ < 10) {
            clock.now = work.pendingDueAt!!
            coordinator(kv, server, clock, work, maxChunks = 1).runWorkerPass()
        }
        assertEquals(0, kv.queue().length())
        assertEquals(listOf(50, 50, 20), server.requests)
    }

    // ---------------------------------------------------------------- 8

    @Test fun t08_emptyQueue_preventsUnnecessaryWork() {
        val kv = MapKv(); val clock = Clock(); val work = FakeWork(clock::get); val server = Server()
        requestDurablePositionTickDrain(kv, work, clock.now, clock.now + 60_000L)
        val r = coordinator(kv, server, clock, work).runPass(PositionTickDrainTrigger.APP_START)
        assertEquals(PositionTickPassOutcome.EMPTY, r.outcome)
        assertNull(work.pendingDueAt)
        assertEquals(0L, next(kv))
        assertEquals(1, work.enqueues.size)
        assertTrue(server.requests.isEmpty())
    }

    // ---------------------------------------------------------------- 9

    @Test fun t09_workerPath_hasNoQuotePolicyAlertOrForegroundReferences() {
        for (f in listOf("PositionTickDrainWorker.kt", "PositionTickDrainCoordinator.kt", "PositionTickUploadRunner.kt", "PositionTickDrain.kt")) {
            val s = code("src/main/java/com/marketradar/app/$f")
            for (banned in listOf("fetchQuotes", "api.upstox.com", "market-quote", "captureOnce", "buildTickRow",
                "PositionPolicy", "maybeNotifyShadowExit", "NotificationManager", "notify(",
                "startForegroundService", "PositionTickService.", "PositionTickService::", "setForeground", "setExpedited", "ForegroundInfo")) {
                assertFalse("$f must not reference $banned", s.contains(banned))
            }
        }
        val w = src("src/main/java/com/marketradar/app/PositionTickDrainWorker.kt")
        assertTrue(w.contains("PositionTickUploadRunner.runWorkerPass(applicationContext, runAttemptCount)"))
        assertTrue(w.contains("PositionTickWorkerResult.SUCCESS -> Result.success()"))
    }

    // ---------------------------------------------------------------- 10

    @Test fun t10_bootAndPackageReplaced_finishOnlyAfterDurableEnqueue_noUnprotectedThread() {
        val kv = MapKv(); val clock = Clock(); val work = FakeWork(clock::get, deferDurable = true)
        var finished = 0
        enqueuePositionTickDrainFromReceiver(kv, work, clock.now) { finished += 1 }
        assertEquals(listOf(0L), work.enqueues)
        assertEquals("finish must wait for the durable enqueue", 0, finished)
        work.deferred.single().invoke(true)
        assertEquals(1, finished)
        // Already-scheduled earlier job: finish immediately, nothing re-enqueued.
        var finished2 = 0
        enqueuePositionTickDrainFromReceiver(kv, work, clock.now + 5L) { finished2 += 1 }
        assertEquals(1, finished2)
        assertEquals(1, work.enqueues.size)
        // Scheduler failure still finishes the PendingResult exactly once.
        val broken = object : PositionTickWorkScheduler {
            override fun enqueue(delayMs: Long, onResult: (Boolean) -> Unit) = throw IllegalStateException("x")
            override fun cancel() {}
        }
        var finished3 = 0
        try { enqueuePositionTickDrainFromReceiver(MapKv(), broken, clock.now) { finished3 += 1 } } catch (_: IllegalStateException) {}
        assertEquals(1, finished3)

        val life = src("src/main/java/com/marketradar/app/MarketOpenScheduler.kt")
        val recv = life.substringAfter("class MarketLifecycleReceiver : BroadcastReceiver() {")
        assertTrue(recv.contains("val pending = goAsync()"))
        assertTrue(recv.contains("PositionTickUploadRunner.enqueueDurableFromReceiver(context) { pending.finish() }"))
        assertFalse("no executor started from the receiver", recv.contains("PositionTickUploadRunner.request("))
        val runner = src("src/main/java/com/marketradar/app/PositionTickUploadRunner.kt")
        assertTrue(runner.contains("future.addListener({ completePositionTickEnqueueFuture(future, onResult) }, Runnable::run)"))
    }

    // ------------------------------------------------------------- extra

    @Test fun lostScheduleOlderThan24h_isReenqueuedNow_neverLater() {
        val kv = MapKv(); val clock = Clock(); val work = FakeWork(clock::get)
        kv.edit { putLong(PREF_POSITION_TICK_NEXT_DRAIN_AT_MS, clock.now - POSITION_TICK_SCHEDULE_LOST_AFTER_MS - 1) }
        assertTrue(requestDurablePositionTickDrain(kv, work, clock.now, clock.now + 60_000L))
        assertEquals(listOf(0L), work.enqueues)
    }

    @Test fun workerPass_releasesOnlyItsDueSlot_keepsNewerEarlierRequest() {
        val kv = MapKv(); val clock = Clock(); val work = FakeWork(clock::get); val server = Server()
        seed(kv, 5)
        server.script.add(503)
        // Slot still in the future (a newer earlier request) is kept by the worker pass.
        requestDurablePositionTickDrain(kv, work, clock.now, clock.now + 30_000L)
        coordinator(kv, server, clock, work).runWorkerPass()
        assertEquals(clock.now + 30_000L, next(kv))
        assertEquals(1, work.enqueues.size)
    }
}
