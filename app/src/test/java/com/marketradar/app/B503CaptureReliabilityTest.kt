package com.marketradar.app

import com.marketradar.app.B503Harness.Companion.SESSION
import com.marketradar.app.B503Harness.Companion.pythonSnapshot
import com.marketradar.app.B503Harness.Companion.request
import com.marketradar.app.B503Harness.Companion.result
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executor
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * b503 section 5: the real CaptureCoordinator + CaptureEngine (extracted production fan-out)
 * + CaptureJournal + real outboxes, with fake Python and virtual clocks.
 */
class B503CaptureReliabilityTest {
    private val pollTs = "${SESSION}T09:50:42+0530"   // Python's own poll_ts (original, end of call)

    @Before fun quietLogs() { GeneratedCandidateRows.logger = { _, _ -> } }

    private fun assertAllChannelsOnce(h: B503Harness, expectPollTs: String = pollTs) {
        val inv = h.inventory()
        assertEquals(inv.toString(), 1, inv.getInt("snapshot_pending"))
        assertEquals(expectPollTs, inv.getJSONArray("snapshot_poll_ts").getString(0))
        assertEquals(inv.toString(), 1, inv.getInt("generated_pending"))
        assertEquals(inv.toString(), 50, inv.getInt("generated_rows"))     // existing 50-row sample semantics
        assertEquals(inv.toString(), 1, inv.getInt("compact_pending"))
        assertEquals(inv.toString(), 1, inv.getInt("legacy_pending"))
        assertEquals(inv.toString(), 128, inv.getInt("legacy_rows"))       // retained tail, not widened
        assertEquals(inv.toString(), 1, inv.getInt("local_cache_entries"))
        assertEquals(inv.toString(), 0, inv.getInt("journal_intents"))
        assertEquals(inv.toString(), 0, inv.getInt("journal_bundles"))
    }

    private fun provenance(h: B503Harness): JSONObject =
        JSONObject(h.snapshotFiles().single().readText()).getJSONObject("poll_summary_json").getJSONObject("capture_provenance")

    // ------------------------------------------------------------ on-time / late
    @Test fun onTimeValue_exactlyOneDurableFanOut_withOriginalIdentityAndBytes() {
        val h = B503Harness()
        val worker = Executor { it.run() }            // completes before the wait
        val fin = ManualFinisher("fin", 2)
        val c = h.coordinator(worker = worker, finisher = fin)
        val calls = AtomicInteger()
        val out = c.submit(request(), 4_000L) { calls.incrementAndGet(); pythonSnapshot(pollTs) }
        assertEquals(CaptureCoordinator.Outcome.DELIVERED_ON_TIME, out)
        assertEquals(1, fin.runAll())
        assertEquals(1, calls.get())
        assertAllChannelsOnce(h)
        val p = provenance(h)
        assertEquals("ON_TIME", p.getString("delivery"))
        assertEquals("$SESSION|8", p.getString("capture_key"))
        assertEquals("${SESSION}T09:50:00+0530", p.getString("origin_poll_ts"))
        assertEquals("${SESSION}T09:50:00+0530", p.getString("slot_scheduled_ist"))
        assertEquals(1, h.count("built")); assertEquals(1, h.count("queued")); assertEquals(0, h.count("timeout"))
        assertTrue(h.engine.isCompleted("$SESSION|8"))
        assertEquals(emptyList<String>(), h.eventTypes())
    }

    @Test fun lateValue_deliversTheOriginalFutureResultOnce() {
        val h = B503Harness()
        val worker = ManualExecutor(); val fin = ManualFinisher("fin", 2)
        val c = h.coordinator(worker = worker, finisher = fin)
        val calls = AtomicInteger()
        assertEquals(CaptureCoordinator.Outcome.LATE_PENDING, c.submit(request(), 4_000L) { calls.incrementAndGet(); pythonSnapshot(pollTs) })
        assertEquals(1, h.journal.inventory().intents)       // durable intent while late
        assertEquals(0, h.snapshotFiles().size)
        worker.runAll()                                      // Python completes after the wait (4.8 s on 13 Oct)
        assertEquals(1, fin.runAll())
        assertEquals(1, calls.get())                          // never recomputed
        assertAllChannelsOnce(h)
        assertEquals("LATE", provenance(h).getString("delivery"))
        assertEquals(1, h.count("timeout")); assertEquals(1, h.count("late_pending"))
        assertEquals(1, h.count("late_built")); assertEquals(1, h.count("late_queued"))
        assertEquals(listOf("LATE_DELIVERED"), h.eventTypes())
        assertFalse(c.isComputationOutstanding()); assertEquals(0, c.pendingAdmissions())
    }

    @Test fun completionBeforeTimeoutRegistration_isClaimedByTheCallerRecheck() {
        val h = B503Harness()
        val worker = ManualExecutor(); val fin = ManualFinisher("fin", 2)
        // The wait expires, but the call completes in the gap before LATE is published:
        // done() sees RUNNING and does nothing, so the caller's isDone re-check must own it.
        val c = h.coordinator(worker = worker, finisher = fin, waiter = CaptureCoordinator.Waiter { _, _ ->
            worker.runAll(); throw java.util.concurrent.TimeoutException() })
        assertEquals(CaptureCoordinator.Outcome.LATE_PENDING, c.submit(request(), 4_000L) { pythonSnapshot(pollTs) })
        assertEquals(1, fin.jobs.size)
        fin.runAll()
        assertAllChannelsOnce(h)
    }

    @Test fun deadlineBoundaryStress_neverLosesOrDoublesACompletion() {
        // Real threads; completion released exactly at the caller's deadline. 4000 iterations.
        val iterations = 4_000
        var doubles = 0; var lost = 0
        repeat(iterations) { i ->
            val delivered = AtomicInteger(); val failed = AtomicInteger()
            val owner = object : CaptureCoordinator.Owner {
                override fun beforeSubmit(req: FrozenCaptureRequest) {}
                override fun onBuilt(req: FrozenCaptureRequest, delivery: CaptureDelivery, computed: CaptureCoordinator.Computed, waitedMs: Long, skippedWhileRunning: Int) { delivered.incrementAndGet() }
                override fun onFailed(req: FrozenCaptureRequest, failure: CaptureCoordinator.Failure, skippedWhileRunning: Int) { failed.incrementAndGet() }
                override fun onStuck(req: FrozenCaptureRequest, ageMs: Long) {}
                override fun onSkipped(req: FrozenCaptureRequest, outcome: CaptureCoordinator.Outcome, blockingKey: String?, blockingAgeMs: Long) {}
                override fun onLatePending(req: FrozenCaptureRequest, waitedMs: Long) {}
                override fun onFinisherRejected(req: FrozenCaptureRequest, delivery: CaptureDelivery) { failed.incrementAndGet() }
            }
            val done = CountDownLatch(1)
            val fin = object : CaptureFinisher("stress", 4, Executor { it.run() }) {
                override fun offer(job: Runnable): Boolean { job.run(); done.countDown(); return true }
            }
            val gate = CountDownLatch(1)
            val c = CaptureCoordinator(CaptureCoordinator.newWorker(), fin, InlineFinisher(), owner,
                waiter = CaptureCoordinator.Waiter { f, ms -> gate.countDown(); CaptureCoordinator.Waiter.REAL.await(f, ms) },
                scheduleStuckCheck = { _, _ -> })
            c.submit(request(attempt = "s$i"), (i % 2).toLong()) {
                gate.await(); if (i % 3 == 0) Thread.yield(); pythonSnapshot(pollTs)
            }
            assertTrue("iteration $i never completed", done.await(5, TimeUnit.SECONDS))
            val total = delivered.get() + failed.get()
            if (total > 1) doubles++
            if (total == 0) lost++
        }
        assertEquals(0, doubles); assertEquals(0, lost)
    }

    // ------------------------------------------------------------ exceptions / rejection
    @Test fun earlyException_isOneRecordedOutcome_andReleasesOwnership() {
        val h = B503Harness()
        val c = h.coordinator(worker = Executor { it.run() })
        assertEquals(CaptureCoordinator.Outcome.FAILED_EARLY, c.submit(request(), 4_000L) { throw IllegalStateException("py") })
        assertEquals(listOf("EARLY_EXCEPTION"), h.eventTypes())
        assertEquals(1, h.count("exception")); assertEquals(1, h.count("terminal_loss"))
        assertEquals(0, h.journal.inventory().intents)
        assertFalse(c.isComputationOutstanding()); assertEquals(0, c.pendingAdmissions())
        assertFalse(h.engine.isCompleted("$SESSION|8"))
        assertEquals(0, h.snapshotFiles().size)
    }

    @Test fun lateException_isRecordedOnce_withoutSuccess() {
        val h = B503Harness(); val worker = ManualExecutor(); val fin = ManualFinisher("fin", 2)
        val c = h.coordinator(worker = worker, finisher = fin)
        assertEquals(CaptureCoordinator.Outcome.LATE_PENDING, c.submit(request(), 4_000L) { throw OutOfMemoryError("py-late") })
        worker.runAll(); fin.runAll()
        assertEquals(listOf("LATE_EXCEPTION"), h.eventTypes())
        assertEquals(1, h.count("late_exception")); assertEquals(0, h.count("built"))
        assertEquals(0, h.snapshotFiles().size); assertEquals(0, h.journal.inventory().intents)
        assertEquals(0, c.pendingAdmissions())
    }

    @Test fun executorRejection_releasesTheSlotAndRecordsTheLoss() {
        val h = B503Harness()
        val c = h.coordinator(worker = Executor { throw RejectedExecutionException("busy") })
        assertEquals(CaptureCoordinator.Outcome.SUBMIT_REJECTED, c.submit(request(), 4_000L) { pythonSnapshot(pollTs) })
        assertEquals(listOf("SUBMIT_REJECTED"), h.eventTypes())
        assertFalse(c.isComputationOutstanding()); assertEquals(0, c.pendingAdmissions())
        assertEquals(0, h.journal.inventory().intents)
    }

    @Test fun handlerThrow_postBuildFailureIsTerminalAndExplicit() {
        val h = B503Harness(); val fin = ManualFinisher("fin", 2)
        val c = h.coordinator(worker = Executor { it.run() }, finisher = fin)
        c.submit(request(), 4_000L) { "this is not json" }
        fin.runAll()
        assertEquals(listOf("BUILD_FANOUT_FAILED"), h.eventTypes())
        assertEquals(0, c.pendingAdmissions()); assertEquals(0, h.journal.inventory().intents)
    }

    @Test fun channelThrow_doesNotSuppressOtherChannels_andIsResumedFromFrozenBytes() {
        val h = B503Harness(); val fin = ManualFinisher("fin", 2)
        h.channelFaults["snapshot"] = { throw java.io.IOException("disk") }
        val c = h.coordinator(worker = Executor { it.run() }, finisher = fin)
        c.submit(request(), 4_000L) { pythonSnapshot(pollTs) }
        fin.runAll()
        assertEquals(0, h.snapshotFiles().size)
        assertEquals(1, h.generatedFiles().size); assertEquals(1, h.legacyStatus().pending); assertEquals(1, h.compactPending().size)
        assertEquals(1, h.count("enqueue_failed")); assertEquals(1, h.count("channel_enqueue_failed"))
        assertEquals(1, h.journal.inventory().bundles)          // retained: not every channel owned yet
        assertFalse(h.engine.isCompleted("$SESSION|8"))
        h.channelFaults.clear()
        val r = h.engine.reconcile(c.liveAttemptIds())
        assertEquals(1, r.resumed); assertEquals(0, r.lost); assertEquals(0, r.uncertain)
        assertAllChannelsOnce(h)
        assertEquals(1, h.enqueueCalls["generated"])            // only the missing channel was re-run
        assertTrue(h.engine.isCompleted("$SESSION|8"))
    }

    // ------------------------------------------------------------ concurrency / stuck
    @Test fun concurrentSameKeyAndOtherKey_atMostOneComputation_explicitSkips() {
        val h = B503Harness(); val worker = ManualExecutor(); val fin = ManualFinisher("fin", 2)
        val c = h.coordinator(worker = worker, finisher = fin)
        val calls = AtomicInteger()
        assertEquals(CaptureCoordinator.Outcome.LATE_PENDING, c.submit(request(attempt = "a1"), 4_000L) { calls.incrementAndGet(); pythonSnapshot(pollTs) })
        assertEquals(CaptureCoordinator.Outcome.SKIPPED_DUPLICATE, c.submit(request(attempt = "a2"), 4_000L) { calls.incrementAndGet(); "x" })
        val next = request(slot = "$SESSION|9", pollTime = "09:55", attempt = "b1")
        assertEquals(CaptureCoordinator.Outcome.SKIPPED_INFLIGHT, c.submit(next, 4_000L) { calls.incrementAndGet(); "x" })
        assertEquals(1, worker.queue.size)
        assertEquals(listOf("INFLIGHT_SKIPPED_SLOT"), h.eventTypes())
        val ev = JSONObject(JSONObject(h.files(CaptureRuntime.EVENTS_DIR).single().readText()).getString("payload"))
        assertEquals("$SESSION|9", ev.getString("capture_key")); assertEquals("$SESSION|8", ev.getJSONObject("detail").getString("blocking_capture_key"))
        worker.runAll(); fin.runAll()
        assertEquals(1, calls.get())
        assertEquals(1, h.snapshotFiles().size)
        assertEquals(1, JSONObject(h.snapshotFiles().single().readText()).getJSONObject("poll_summary_json")
            .getJSONObject("capture_provenance").getInt("skipped_slots_while_running"))
    }

    @Test fun stuckCall_oneDiagnostic_noReplacement_eventualOriginalDelivery() {
        val h = B503Harness(); val worker = ManualExecutor(); val fin = ManualFinisher("fin", 2)
        val checks = mutableListOf<() -> Unit>()
        val c = h.coordinator(worker = worker, finisher = fin, stuckChecks = checks)
        val calls = AtomicInteger()
        c.submit(request(), 4_000L) { calls.incrementAndGet(); pythonSnapshot(pollTs) }
        h.mono.addAndGet(119_999); checks.forEach { it() }
        assertFalse(h.eventTypes().contains("CAPTURE_STUCK"))
        h.mono.addAndGet(1); checks.forEach { it() }; checks.forEach { it() }
        for (slot in 10..12) {   // later polls are skipped and counted, never replaced
            h.mono.addAndGet(300_000)
            assertEquals(CaptureCoordinator.Outcome.SKIPPED_INFLIGHT,
                c.submit(request(slot = "$SESSION|$slot", attempt = "s$slot"), 4_000L) { calls.incrementAndGet(); "x" })
        }
        assertEquals(1, h.eventTypes().count { it == "CAPTURE_STUCK" })
        assertEquals(3, h.eventTypes().count { it == "INFLIGHT_SKIPPED_SLOT" })
        assertEquals(1, h.count("capture_stuck"))
        worker.runAll(); fin.runAll()                           // released after ~15 min
        assertEquals(1, calls.get())
        assertEquals(1, h.snapshotFiles().size)
        assertEquals("LATE", provenance(h).getString("delivery"))
        assertEquals(3, provenance(h).getInt("skipped_slots_while_running"))
        assertEquals(1, h.eventTypes().count { it == "LATE_DELIVERED" })
    }

    // ------------------------------------------------------------ freeze
    @Test fun mutationAfterFreeze_workerAndCohortSeeExactOriginalBytes() {
        val h = B503Harness(); val worker = ManualExecutor(); val fin = ManualFinisher("fin", 2)
        val c = h.coordinator(worker = worker, finisher = fin)
        val res = result("${SESSION}T09:50:00+0530", nf = 30, bnf = 30)
        val ctx = JSONObject().put("today_ist", SESSION).put("regime", "A")
        val req = request(result = res, ctx = ctx)
        val expectResult = FrozenCaptureRequest.sha256Hex(res.toString()); val expectCtx = FrozenCaptureRequest.sha256Hex(ctx.toString())
        var seenResult = ""; var seenCtx = ""
        c.submit(req, 4_000L) { r -> seenResult = FrozenCaptureRequest.sha256Hex(r.resultJson); seenCtx = FrozenCaptureRequest.sha256Hex(r.ctxJson); pythonSnapshot(pollTs) }
        // Caller continues: the A1 merge and position updates mutate the live objects.
        ctx.put("regime", "B").put("prevVerdict", JSONObject().put("x", 1))
        res.getJSONObject("elephant_fact_pack").put("candidates", JSONArray())
        res.put("position_live", JSONObject().put("mutated", true))
        worker.runAll(); fin.runAll()
        assertEquals(expectResult, seenResult); assertEquals(expectCtx, seenCtx)
        assertEquals(50, h.inventory().getInt("generated_rows"))   // cohort from frozen fact pack, not the mutated one
    }

    // ------------------------------------------------------------ lifecycle / restart
    @Test fun serviceCancellationWhileLate_durableFinishingContinuesWithoutStaleService() {
        val h = B503Harness(); val worker = ManualExecutor(); val fin = ManualFinisher("fin", 2)
        val c = h.coordinator(worker = worker, finisher = fin)
        val scope = kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.Unconfined + kotlinx.coroutines.SupervisorJob())
        var serviceDrains = 0
        var hook: (() -> Unit)? = { scope.launch { serviceDrains++ } }
        c.submit(request(), 4_000L) { pythonSnapshot(pollTs) }
        scope.cancel(); hook = null                            // onDestroy: scope cancelled, hook withdrawn
        worker.runAll(); fin.runAll()
        assertAllChannelsOnce(h)                                // durable without the service
        assertEquals(0, serviceDrains)
        hook?.invoke()
    }


    @Test fun processDeathBeforeBuild_isAnExplicitLoss_neverAnInventedSnapshot() {
        val a = B503Harness(runId = "run-A")
        a.coordinator().submit(request(), 4_000L) { pythonSnapshot(pollTs) }   // never completes: process dies
        val b = B503Harness(root = a.root, runId = "run-B")
        val r = b.engine.reconcile(emptySet())
        assertEquals(1, r.lost); assertEquals(0, r.uncertain)
        assertEquals(listOf("LOST_PROCESS_ENDED_BEFORE_DURABLE_BUILD"), b.eventTypes())
        assertEquals(0, b.snapshotFiles().size)
        b.engine.reconcile(emptySet()); B503Harness(root = a.root, runId = "run-C").engine.reconcile(emptySet())
        assertEquals(listOf("LOST_PROCESS_ENDED_BEFORE_DURABLE_BUILD"), b.eventTypes())    // idempotent
    }

    @Test fun interruptedBundleWrite_isUncertain_notAProvenLoss() {
        val fs = FaultyFs().apply { failWriteMatching = Regex("bundle") }
        val a = B503Harness(journalFs = fs, runId = "run-A")
        // keep the partial tmp: simulate the process dying mid-write by leaving it in place
        val req = request()
        a.journal.writeIntent(req, 1)
        File(a.journal.dir, "${req.attemptId}.bundle.json.tmp").writeText("{partial")
        val b = B503Harness(root = a.root, runId = "run-B")
        val r = b.engine.reconcile(emptySet())
        assertEquals(1, r.uncertain); assertEquals(0, r.lost)
        assertEquals(listOf("UNCERTAIN_INTERRUPTED"), b.eventTypes())
    }

    @Test fun crashAfterEachLocalCheckpoint_resumesOnlyMissingChannels_noFalseLoss_noDuplicates() {
        val points = listOf("bundle", "built", "local_cache", "snapshot", "compact", "legacy", "generated")
        for (point in points) {
            val cache = mutableListOf<String>()
            val a = B503Harness(runId = "run-A", sharedLocalCache = cache)
            a.engine.crashPoint = { if (it == point) throw SimulatedProcessDeath(it) }
            val req = request()
            a.engine.beforeSubmit(req)
            try {
                a.engine.onBuilt(req, CaptureDelivery.LATE, CaptureCoordinator.Computed(pythonSnapshot(pollTs), 4792, 1200), 4000, 0)
                fail("crash point $point not reached")
            } catch (_: SimulatedProcessDeath) { }
            val b = B503Harness(root = a.root, runId = "run-B", sharedLocalCache = cache)
            val r = b.engine.reconcile(emptySet())
            assertEquals("$point: $r", 0, r.lost); assertEquals("$point: $r", 0, r.uncertain)
            assertAllChannelsOnce(b)
            val types = b.eventTypes()
            assertFalse(point, types.contains("LOST_PROCESS_ENDED_BEFORE_DURABLE_BUILD"))
            if (point != "generated") assertTrue("$point $types", types.contains("PARTIAL_RECOVERY_RESUMED"))
            assertTrue(b.engine.isCompleted("$SESSION|8"))
            // A second restart is a no-op.
            val again = B503Harness(root = a.root, runId = "run-C", sharedLocalCache = cache).engine.reconcile(emptySet())
            assertEquals(0, again.resumed + again.lost + again.uncertain)
            assertAllChannelsOnce(b)
        }
    }

    @Test fun restartWhileSameProcessTaskStillLive_noFalseLossNoReplacement() {
        val h = B503Harness(); val worker = ManualExecutor(); val fin = ManualFinisher("fin", 2)
        val c = h.coordinator(worker = worker, finisher = fin)
        c.submit(request(), 4_000L) { pythonSnapshot(pollTs) }
        // Service onDestroy + onStartCommand inside the same process: reconcile with live owners.
        val r = h.engine.reconcile(c.liveAttemptIds())
        assertEquals(1, r.live); assertEquals(0, r.lost + r.uncertain + r.resumed)
        assertEquals(emptyList<String>(), h.eventTypes())
        worker.runAll(); fin.runAll()
        assertAllChannelsOnce(h)
    }

    // ------------------------------------------------------------ saturation / storage faults
    @Test fun finisherSaturation_lateIsExplicitLoss_onTimeRunsInline() {
        val h = B503Harness(); val worker = ManualExecutor()
        val fin = ManualFinisher("fin", 2).apply { reject = true }
        val c = h.coordinator(worker = worker, finisher = fin)
        c.submit(request(), 4_000L) { pythonSnapshot(pollTs) }
        worker.runAll()
        assertEquals(listOf("FINISHER_REJECTED"), h.eventTypes())
        assertEquals(1, h.count("finisher_rejected")); assertEquals(0, h.snapshotFiles().size)
        assertEquals(0, c.pendingAdmissions()); assertEquals(0, h.journal.inventory().intents)
        assertTrue(fin.overflowCount() >= 1)
        val h2 = B503Harness(); val c2 = h2.coordinator(worker = Executor { it.run() }, finisher = ManualFinisher("f", 2).apply { reject = true })
        assertEquals(CaptureCoordinator.Outcome.DELIVERED_ON_TIME, c2.submit(request(), 4_000L) { pythonSnapshot(pollTs) })
        assertAllChannelsOnce(h2)
    }

    @Test fun diskFullOnBundle_isVisible_channelsStillEnqueued_noFalseAck() {
        val fs = FaultyFs().apply { failWriteMatching = Regex("bundle") }
        val h = B503Harness(journalFs = fs); val fin = ManualFinisher("fin", 2)
        val c = h.coordinator(worker = Executor { it.run() }, finisher = fin)
        c.submit(request(), 4_000L) { pythonSnapshot(pollTs) }; fin.runAll()
        assertTrue(h.eventTypes().contains("JOURNAL_STORAGE_FAULT"))
        assertEquals(1, h.count("journal_fault"))
        assertEquals(1, h.snapshotFiles().size); assertEquals(1, h.legacyStatus().pending)
        assertEquals(0, h.journal.dir.listFiles()!!.count { it.name.endsWith(".tmp") })
    }

    @Test fun failedRenameInLegacyOutbox_isRetainedNotAcked_andRecoveredAfterRepair() {
        val lfs = FaultyFs().apply { failRenameMatching = Regex("^[0-9a-f]{40}\\.json$") }
        val h = B503Harness(legacyFs = lfs); val fin = ManualFinisher("fin", 2)
        val c = h.coordinator(worker = Executor { it.run() }, finisher = fin)
        c.submit(request(), 4_000L) { pythonSnapshot(pollTs) }; fin.runAll()
        assertEquals(0, h.legacyStatus().pending)
        assertEquals(1, h.journal.inventory().bundles)               // frozen bytes kept
        assertFalse(h.engine.isCompleted("$SESSION|8"))
        lfs.failRenameMatching = null
        h.engine.reconcile(c.liveAttemptIds())
        assertAllChannelsOnce(h)
    }

    @Test fun failedIntentFsync_isReported_andCaptureStillDelivered() {
        val fs = FaultyFs().apply { failWriteMatching = Regex("intent") }
        val h = B503Harness(journalFs = fs); val fin = ManualFinisher("fin", 2)
        val c = h.coordinator(worker = Executor { it.run() }, finisher = fin)
        c.submit(request(), 4_000L) { pythonSnapshot(pollTs) }; fin.runAll()
        assertTrue(h.eventTypes().contains("JOURNAL_STORAGE_FAULT"))
        assertEquals(1, h.snapshotFiles().size)
    }

    @Test fun journalCapacity_refusesVisibly_neverEvicts() {
        val h = B503Harness()
        val small = CaptureJournal(h.root, maxIntents = 2)
        small.writeIntent(request(attempt = "x1"), 1); small.writeIntent(request(attempt = "x2"), 1)
        try { small.writeIntent(request(attempt = "x3"), 1); fail() } catch (e: java.io.IOException) { assertTrue(e.message!!.contains("capacity")) }
        assertEquals(2, small.inventory().intents)
    }

    // ------------------------------------------------------------ ordering / identity
    @Test fun olderCompletionAfterNewerSuccess_neverRegressesOrReopens() {
        val h = B503Harness(); val worker = ManualExecutor(); val fin = ManualFinisher("fin", 2)
        val c = h.coordinator(worker = worker, finisher = fin)
        c.submit(request(), 4_000L) { pythonSnapshot(pollTs) }                     // slot 8 late
        worker.runAll()                                                             // completes, finisher queued
        val c9 = request(slot = "$SESSION|9", pollTime = "09:55", attempt = "n9")
        h.engine.onBuilt(c9, CaptureDelivery.ON_TIME, CaptureCoordinator.Computed(pythonSnapshot("${SESSION}T09:55:03+0530"), 3600, 3500), 3600, 0)
        fin.runAll()                                                                // older slot 8 lands after slot 9
        assertEquals(listOf("$SESSION|8", "$SESSION|9"), h.journal.completedKeys())
        assertTrue(h.engine.isCompleted("$SESSION|9")); assertTrue(h.engine.isCompleted("$SESSION|8"))
        assertEquals(2, h.snapshotFiles().size)
    }

    @Test fun completedKeySet_isBoundedAndOrderedBySessionAndOrdinal() {
        val h = B503Harness()
        for (o in 1..140) h.journal.markCompleted("$SESSION|$o")
        h.journal.markCompleted("2026-10-12|76")
        val keys = h.journal.completedKeys()
        assertEquals(CaptureJournal.MAX_COMPLETED_KEYS, keys.size)
        assertEquals("$SESSION|140", keys.last()); assertFalse(keys.contains("2026-10-12|76"))
    }

    @Test fun slotRollover_lateResultInLaterBucket_reconcilesToOriginSlot() {
        val h = B503Harness(); val fin = ManualFinisher("fin", 2); val worker = ManualExecutor()
        val c = h.coordinator(worker = worker, finisher = fin)
        val laterBucketTs = "${SESSION}T09:56:10+0530"   // completed after its own 09:50 bucket ended
        c.submit(request(), 4_000L) { pythonSnapshot(laterBucketTs) }; worker.runAll(); fin.runAll()
        val snap = JSONObject(h.snapshotFiles().single().readText())
        assertEquals(laterBucketTs, snap.getString("poll_ts"))             // never rewritten
        val origin = CaptureSlotReconciler.originOf(snap)!!
        assertEquals(8, origin.slotOrdinal); assertEquals("capture_provenance", origin.source)
        // Pre-b503 rows keep the historical bucket reconciliation.
        assertEquals(9, CaptureSlotReconciler.originOf(JSONObject().put("poll_ts", laterBucketTs))!!.slotOrdinal)
    }

    @Test fun midnight_lateDeliveryKeepsFrozenOriginDate() {
        val h = B503Harness(); val fin = ManualFinisher("fin", 2); val worker = ManualExecutor()
        val c = h.coordinator(worker = worker, finisher = fin)
        val req = request(slot = "", pollTime = "23:58", attempt = "night")      // unscheduled poll
        assertEquals("$SESSION|closed", req.captureKey)
        c.submit(req, 4_000L) { pythonSnapshot("2026-10-14T00:00:05+0530") }; worker.runAll(); fin.runAll()
        val p = provenance(h)
        assertEquals("$SESSION|closed", p.getString("capture_key")); assertEquals(SESSION, p.getString("session_date"))
        assertEquals(SESSION, CaptureSlotReconciler.originOf(JSONObject(h.snapshotFiles().single().readText()))!!.sessionDate)
    }

    @Test fun inconsistentIdentity_isRejectedExplicitly() {
        for ((slot, ctxDate, reason) in listOf(
            Triple("2026-10-12|8", SESSION, "slot_date_mismatch"),
            Triple("$SESSION|0", SESSION, "slot_ordinal_out_of_range"),
            Triple("$SESSION|$SESSION|8", SESSION, "slot_key_malformed"),
            Triple("$SESSION|8", "", "origin_date_invalid"))) {
            try { request(slot = slot, ctxDate = ctxDate); fail(reason) } catch (e: CaptureIdentityException) { assertEquals(reason, e.reason) }
        }
        assertEquals("$SESSION|8", FrozenCaptureRequest.normalizeLegacyKey("$SESSION|$SESSION|8"))
        assertEquals("09:50", FrozenCaptureRequest.slotStartHm(8)); assertEquals("15:40", FrozenCaptureRequest.slotStartHm(78))
        assertEquals(78, FrozenCaptureRequest.FULL_DAY_SLOTS)
    }

    @Test fun appUpgradeWithQueuedBuiltPayload_keepsOriginalVersionAndBytes() {
        val cache = mutableListOf<String>()
        val a = B503Harness(runId = "run-old", sharedLocalCache = cache)
        a.engine.crashPoint = { if (it == "built") throw SimulatedProcessDeath(it) }
        val req = request(version = "2.6.72", code = 503)
        a.engine.beforeSubmit(req)
        try { a.engine.onBuilt(req, CaptureDelivery.LATE, CaptureCoordinator.Computed(pythonSnapshot(pollTs), 1, 1), 1, 0) } catch (_: SimulatedProcessDeath) {}
        val bundled = JSONObject(File(a.journal.dir, "${req.attemptId}.bundle.json").readText()).getJSONObject("channels").getString("snapshot")
        val b = B503Harness(root = a.root, runId = "run-new-2.6.73", sharedLocalCache = cache)
        b.engine.reconcile(emptySet())
        val stored = b.snapshotFiles().single().readText()
        assertEquals(bundled, stored)                                                 // byte-identical
        val p = JSONObject(stored).getJSONObject("poll_summary_json").getJSONObject("capture_provenance")
        assertEquals("2.6.72", p.getString("app_version")); assertEquals(503, p.getInt("version_code"))
    }

    // ------------------------------------------------------------ replay / collision
    @Test fun sameArtifactRetry_isIdempotent_differentBytesSameIdentity_isRetainedCollision() {
        val h = B503Harness()
        val payload = JSONObject(pythonSnapshot(pollTs)).toString()
        assertTrue(BrainSnapshotOutbox.enqueuePayload(h.root, payload))
        assertFalse(BrainSnapshotOutbox.enqueuePayload(h.root, payload))
        val other = JSONObject(payload).put("action", "ENTER").toString()
        try { BrainSnapshotOutbox.enqueuePayload(h.root, other); fail() } catch (e: IllegalStateException) { assertTrue(e.message!!.contains("collision")) }
        assertEquals(1, File(h.root, "brain_snapshot_outbox_v1/quarantine").listFiles()!!.size)
        assertTrue(h.legacy.enqueue(pollTs, "[1]")); assertFalse(h.legacy.enqueue(pollTs, "[1]"))
        try { h.legacy.enqueue(pollTs, "[2]"); fail() } catch (e: IllegalStateException) { assertTrue(e.message!!.contains("collision")) }
        assertEquals(1, h.legacy.status().quarantinedTotal); assertEquals(1, h.legacy.status().pending)
    }

    @Test fun collisionThroughFanOut_isExplicitlyRetained() {
        val h = B503Harness(); val fin = ManualFinisher("fin", 2)
        BrainSnapshotOutbox.enqueuePayload(h.root, JSONObject().put("poll_ts", pollTs).put("other", 1).toString())
        val c = h.coordinator(worker = Executor { it.run() }, finisher = fin)
        c.submit(request(), 4_000L) { pythonSnapshot(pollTs) }; fin.runAll()
        assertTrue(h.eventTypes().contains("CHANNEL_COLLISION_RETAINED"))
        assertEquals(0, h.journal.inventory().intents)       // explicit terminal state persisted
    }

    // ------------------------------------------------------------ network failure / restart drain
    @Test fun networkFailureThenRestart_allFourChannelsRecoverIndependently() {
        val h = B503Harness(); val fin = ManualFinisher("fin", 2)
        val c = h.coordinator(worker = Executor { it.run() }, finisher = fin)
        c.submit(request(), 4_000L) { pythonSnapshot(pollTs) }; fin.runAll()
        // Outage: every channel retries; nothing is acknowledged or dropped.
        assertEquals(0, BrainSnapshotOutbox.drain(h.root) { BrainSnapshotOutbox.Outcome.RETRY }.acknowledged)
        assertEquals(0, GeneratedCandidateOutbox.drain(h.root) { GeneratedCandidateOutbox.Outcome.RETRY }.acknowledged)
        assertEquals(0, h.legacy.drain { _, _ -> FrozenEvidenceOutbox.Outcome.RETRY }.acknowledged)
        assertEquals(0, Pc2TelemetryOutbox.drain(h.root) { Pc2TelemetryOutbox.Outcome.RETRY }.acknowledged)
        assertAllChannelsOnce(h)
        // Restart (fresh process objects over the same files); legacy still failing, the rest recover.
        Pc2TelemetryOutbox.simulateProcessRestart()
        val legacyRows = mutableListOf<Int>()
        assertEquals(1, BrainSnapshotOutbox.drain(h.root) { BrainSnapshotOutbox.Outcome.ACKNOWLEDGED }.acknowledged)
        assertEquals(1, GeneratedCandidateOutbox.drain(h.root) { GeneratedCandidateOutbox.Outcome.ACKNOWLEDGED }.acknowledged)
        assertEquals(1, Pc2TelemetryOutbox.drain(h.root) { Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED }.acknowledged)
        assertEquals(0, h.legacy.drain { _, _ -> FrozenEvidenceOutbox.Outcome.RETRY }.acknowledged)
        val restarted = FrozenEvidenceOutbox(h.root, CaptureRuntime.LEGACY_DIR, 150, 64L * 1024 * 1024)
        val s = restarted.drain { key, payload -> assertEquals(pollTs, key); legacyRows += JSONArray(payload).length(); FrozenEvidenceOutbox.Outcome.ACKNOWLEDGED }
        assertEquals(1, s.acknowledged); assertEquals(listOf(128), legacyRows); assertEquals(0, s.pending)
        // Legacy identity contract unchanged: "$pollTs|$policy|$diag|$index".
        val rows = Pc2LegacyAuthorityRows.build(JSONObject(h.sharedLocalCache.single())).rows!!
        assertEquals("$pollTs|pc2_authority_policy_v2|pc2_authority_diagnostics_v1|0", rows.getJSONObject(0).getString("id"))
        assertEquals(127, rows.getJSONObject(127).getInt("decision_index"))
    }

    // ------------------------------------------------------------ empty / warmup / NF+BNF
    @Test fun warmupWithoutCohort_inventsNothing() {
        val h = B503Harness(); val fin = ManualFinisher("fin", 2)
        val c = h.coordinator(worker = Executor { it.run() }, finisher = fin)
        c.submit(request(slot = "$SESSION|1", pollTime = "09:15", result = result(null)), 4_000L) {
            pythonSnapshot("${SESSION}T09:15:04+0530", decisions = 0) }
        fin.runAll()
        val inv = h.inventory()
        assertEquals(1, inv.getInt("snapshot_pending")); assertEquals(0, inv.getInt("generated_pending"))
        assertEquals(0, inv.getInt("legacy_pending")); assertEquals(0, inv.getInt("compact_pending"))
        assertEquals(0, inv.getInt("journal_intents")); assertEquals(emptyList<String>(), h.eventTypes())
    }

    @Test fun zeroCandidatesPerIndex_andPopulatedNfBnf_reconcile() {
        val h = B503Harness(); val fin = ManualFinisher("fin", 2)
        val c = h.coordinator(worker = Executor { it.run() }, finisher = fin)
        c.submit(request(slot = "$SESSION|3", pollTime = "09:25", result = result("${SESSION}T09:25:00+0530", nf = 0, bnf = 0), attempt = "z"), 4_000L) {
            pythonSnapshot("${SESSION}T09:25:03+0530") }
        fin.runAll()
        assertEquals(0, h.generatedFiles().size); assertEquals(1, h.legacyStatus().pending)
        val h2 = B503Harness(); val fin2 = ManualFinisher("fin", 2)
        val c2 = h2.coordinator(worker = Executor { it.run() }, finisher = fin2)
        c2.submit(request(result = result("${SESSION}T09:50:00+0530", nf = 12, bnf = 7)), 4_000L) { pythonSnapshot(pollTs) }
        fin2.runAll()
        val rows = JSONObject(h2.generatedFiles().single().readText()).getJSONArray("rows")
        assertEquals(19, rows.length())
        val idx = (0 until rows.length()).map { rows.getJSONObject(it).getString("index_key") }
        assertEquals(12, idx.count { it == "NF" }); assertEquals(7, idx.count { it == "BNF" })
        assertTrue((0 until rows.length()).all { rows.getJSONObject(it).getString("recommendation_id") == "rec-$pollTs" })  // lineage kept
        assertTrue((0 until rows.length()).all { rows.getJSONObject(it).getString("snapshot_poll_ts") == "${SESSION}T09:50:00+0530" })
    }

    // ------------------------------------------------------------ event protocol / health
    @Test fun captureEventResponses_areClassifiedConservatively() {
        val ack = FrozenEvidenceOutbox.Outcome.ACKNOWLEDGED; val retry = FrozenEvidenceOutbox.Outcome.RETRY; val q = FrozenEvidenceOutbox.Outcome.QUARANTINE
        assertEquals(ack, CaptureEventProtocol.classify(200, """{"status":"INSERTED"}"""))
        assertEquals(ack, CaptureEventProtocol.classify(200, """{"status":"DUPLICATE"}"""))
        assertEquals(q, CaptureEventProtocol.classify(200, """{"status":"REJECTED_CONFLICT"}"""))
        assertEquals(retry, CaptureEventProtocol.classify(200, """{}"""))
        assertEquals(retry, CaptureEventProtocol.classify(403, """{"code":"PT403","message":"CAPTURE_DEVICE_NOT_AUTHORIZED"}"""))
        assertEquals(retry, CaptureEventProtocol.classify(404, """{"code":"PGRST202"}"""))       // capability absent: keep queued
        assertEquals(q, CaptureEventProtocol.classify(400, """{"code":"23514","message":"CAPTURE_EVENT_TOO_LARGE"}"""))
        assertEquals(retry, CaptureEventProtocol.classify(null, null))
        assertEquals(retry, CaptureEventProtocol.classify(503, "busy"))
    }

    @Test fun eventIdentity_isStableAndBounded() {
        val h = B503Harness()
        val id1 = h.engine.eventIdFor("$SESSION|8", "att-8", "LATE_DELIVERED")
        assertEquals(id1, h.engine.eventIdFor("$SESSION|8", "att-8", "LATE_DELIVERED"))
        assertTrue(id1.matches(Regex("^cev_[0-9a-f]{32}$")))
        val big = JSONObject().put("blob", "x".repeat(10_000))
        h.engine.recordEvent(request().identityJson(), "CAPTURE_STUCK", "r", big)
        val ev = JSONObject(JSONObject(h.files(CaptureRuntime.EVENTS_DIR).single().readText()).getString("payload"))
        assertTrue(ev.toString().length < 4_096); assertTrue(ev.getJSONObject("detail").getBoolean("truncated"))
        assertFalse(ev.toString().contains("result_json")); assertFalse(ev.toString().contains("p_device_key"))
    }

    @Test fun stageHealth_keepsLegacyCountersAndAddsHistogram() {
        var s = SnapshotStageHealth.record(null, SESSION, "attempt", "$SESSION|8").toString()
        s = SnapshotStageHealth.recordTiming(s, SESSION, "$SESSION|8", "LATE", 4_792, 120, 4_700, 900, 350).toString()
        s = SnapshotStageHealth.recordTiming(s, SESSION, "$SESSION|9", "ON_TIME", 3_589, 110, 3_500, 3_400, 300).toString()
        val j = JSONObject(s)
        assertEquals(1, j.getJSONObject("counts").getInt("attempt"))
        assertEquals(1, j.getJSONObject("elapsed_histogram").getInt("4_6s")); assertEquals(1, j.getJSONObject("elapsed_histogram").getInt("3_4s"))
        assertEquals(4_792, j.getLong("max_elapsed_ms")); assertEquals("$SESSION|8", j.getString("max_elapsed_poll_key"))
        assertEquals(0.971, j.getDouble("last_python_cpu_to_wall"), 0.001)
        assertEquals("gt_20s", SnapshotStageHealth.bucketOf(25_000)); assertEquals("lt_2s", SnapshotStageHealth.bucketOf(1_999))
    }

    @Test fun existingPyTimeoutSemantics_unchangedForOtherCallers() {
        val k = "b503.other.${System.nanoTime()}"
        assertEquals("ok", PyTimeout.callWithTimeout(k, 1_000L) { "ok" })
        assertEquals(PyTimeout.WaitState.COMPLETED, PyTimeout.callWithOutcome(k, 1_000L) { "x" }.state)
    }
}
