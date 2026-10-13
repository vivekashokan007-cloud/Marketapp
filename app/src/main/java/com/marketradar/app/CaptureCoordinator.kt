package com.marketradar.app

import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.Callable
import java.util.concurrent.CancellationException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ExecutionException
import java.util.concurrent.Executor
import java.util.concurrent.Future
import java.util.concurrent.FutureTask
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.ScheduledThreadPoolExecutor
import java.util.concurrent.SynchronousQueue
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

/**
 * b503: capture-specific late-delivery coordinator. Existing PyTimeout.callWithTimeout /
 * callWithOutcome semantics are untouched for every other caller.
 *
 * Contract (exactly one local completion owner; NOT global exactly-once):
 *  1. Registration happens BEFORE submission: the single computation slot ([running])
 *     and the per-capture-key admission map are claimed atomically first. A second
 *     submission while a computation is outstanding is refused explicitly
 *     (SKIPPED_INFLIGHT / SKIPPED_DUPLICATE) and never starts a second Python call.
 *  2. Per call, an atomic phase decides the owner of the single completion:
 *       RUNNING --caller got value/exception in time--> CALLER
 *       RUNNING --caller wait expired--> LATE --(done() XOR caller re-check)--> LATE_CLAIMED
 *     The caller re-checks isDone after publishing LATE, closing the
 *     completed-before-registration gap; done() and the caller race on one CAS.
 *  3. The owner hands the ORIGINAL Future's value or exception to the bounded,
 *     process-owned [CaptureFinisher]. FutureTask.done() only does CAS + queue offer;
 *     no JSON, persistence or network work runs on the Python worker thread.
 *  4. The computation slot is released when Python actually completes (value or
 *     exception). A still-running call keeps it: later polls are skipped and
 *     counted, one CAPTURE_STUCK diagnostic is emitted after [stuckAfterMs], and if
 *     the call ever completes it is delivered once (LATE). Python is never interrupted.
 *  5. The admission entry for a key is released only after its finisher job ends.
 */
class CaptureCoordinator(
    private val worker: Executor,
    private val finisher: CaptureFinisher,
    private val control: CaptureFinisher,
    private val owner: Owner,
    private val waiter: Waiter = Waiter.REAL,
    private val monoClock: () -> Long = { System.nanoTime() / 1_000_000L },
    private val cpuClockMs: () -> Long = { -1L },
    private val stuckAfterMs: Long = DEFAULT_STUCK_AFTER_MS,
    private val scheduleStuckCheck: (delayMs: Long, check: () -> Unit) -> Unit = DEFAULT_SCHEDULER
) {
    companion object {
        /** Documented threshold: 120 s, well beyond the slowest observed capture (4.8 s). */
        const val DEFAULT_STUCK_AFTER_MS = 120_000L
        private val schedulerThread = AtomicInteger(0)
        private val sharedScheduler by lazy {
            ScheduledThreadPoolExecutor(1) { r ->
                Thread(r, "capture-stuck-timer-${schedulerThread.incrementAndGet()}").apply { isDaemon = true }
            }.apply { removeOnCancelPolicy = true }
        }
        val DEFAULT_SCHEDULER: (Long, () -> Unit) -> Unit = { delay, check ->
            try { sharedScheduler.schedule({ runCatching(check) }, delay, TimeUnit.MILLISECONDS) }
            catch (_: Throwable) { /* timer is diagnostic only; lazy checks on later submits remain */ }
        }

        /** Dedicated capture worker: at most one Python capture thread; refuses rather than queues. */
        fun newWorker(): ThreadPoolExecutor = ThreadPoolExecutor(
            0, 1, 60L, TimeUnit.SECONDS, SynchronousQueue()
        ) { r -> Thread(r, "capture-python").apply { isDaemon = true } }
    }

    /** Computation result as returned by the original Future. */
    class Computed(val value: String, val pythonMs: Long, val pythonCpuMs: Long)

    sealed class Failure(val reason: String) {
        class Exception(val error: Throwable, val late: Boolean) :
            Failure(if (late) "late_exception" else "early_exception")
        object SubmitRejected : Failure("submit_rejected")
        object Cancelled : Failure("cancelled")
    }

    interface Owner {
        /** Durable intent before submission. Must not throw for storage faults; report them. */
        fun beforeSubmit(req: FrozenCaptureRequest)
        /** Fan-out of a built value. Runs on the finisher (or inline caller fallback for ON_TIME). */
        fun onBuilt(req: FrozenCaptureRequest, delivery: CaptureDelivery, computed: Computed, waitedMs: Long, skippedWhileRunning: Int)
        /** Terminal failure of this attempt; records evidence and releases ownership. */
        fun onFailed(req: FrozenCaptureRequest, failure: Failure, skippedWhileRunning: Int)
        /** Light diagnostics (run on the control lane). */
        fun onStuck(req: FrozenCaptureRequest, ageMs: Long)
        fun onSkipped(req: FrozenCaptureRequest, outcome: Outcome, blockingKey: String?, blockingAgeMs: Long)
        fun onLatePending(req: FrozenCaptureRequest, waitedMs: Long)
        /** A late completion could not be queued (finisher saturated). Evidence only; bytes are lost. */
        fun onFinisherRejected(req: FrozenCaptureRequest, delivery: CaptureDelivery)
    }

    fun interface Waiter {
        /** Blocks up to [timeoutMs]; throws TimeoutException / ExecutionException like Future.get. */
        fun await(future: Future<Computed>, timeoutMs: Long): Computed

        companion object {
            val REAL = Waiter { future, timeoutMs ->
                var interrupted = false
                try {
                    val deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(timeoutMs)
                    while (true) {
                        try { return@Waiter future.get(deadline - System.nanoTime(), TimeUnit.NANOSECONDS) }
                        catch (_: InterruptedException) { interrupted = true }
                    }
                    @Suppress("UNREACHABLE_CODE") throw IllegalStateException()
                } finally { if (interrupted) Thread.currentThread().interrupt() }
            }
        }
    }

    enum class Outcome { DELIVERED_ON_TIME, LATE_PENDING, FAILED_EARLY, SUBMIT_REJECTED, SKIPPED_INFLIGHT, SKIPPED_DUPLICATE }

    private enum class Phase { RUNNING, CALLER, LATE, LATE_CLAIMED }

    private inner class Cell(val req: FrozenCaptureRequest, val startedMono: Long) {
        val phase = AtomicReference(Phase.RUNNING)
        val stuckReported = AtomicBoolean(false)
        val skipped = AtomicInteger(0)
        @Volatile var task: FutureTask<Computed>? = null
    }

    private val running = AtomicReference<Cell?>(null)
    private val admitted = ConcurrentHashMap<String, Cell>()

    /** Attempt ids whose computation or finishing is still owned in THIS process. */
    fun liveAttemptIds(): Set<String> = admitted.values.map { it.req.attemptId }.toSet() +
        listOfNotNull(running.get()?.req?.attemptId)

    /** R2: per-marker liveness for reconciliation (admission is registered before the intent is written). */
    fun isLive(attemptId: String): Boolean =
        admitted.values.any { it.req.attemptId == attemptId } || running.get()?.req?.attemptId == attemptId

    fun isComputationOutstanding(): Boolean = running.get() != null
    fun pendingAdmissions(): Int = admitted.size

    fun submit(req: FrozenCaptureRequest, waitMs: Long, compute: (FrozenCaptureRequest) -> String): Outcome {
        val cell = Cell(req, monoClock())
        // (1) admission of the capture key, then the single computation slot. Both before submission.
        val prior = admitted.putIfAbsent(req.captureKey, cell)
        if (prior != null) {
            lightly { owner.onSkipped(req, Outcome.SKIPPED_DUPLICATE, prior.req.captureKey, monoClock() - prior.startedMono) }
            return Outcome.SKIPPED_DUPLICATE
        }
        if (!running.compareAndSet(null, cell)) {
            admitted.remove(req.captureKey, cell)
            val blocking = running.get()
            if (blocking != null) {
                blocking.skipped.incrementAndGet()
                checkStuck(blocking)
            }
            durably {
                owner.onSkipped(req, Outcome.SKIPPED_INFLIGHT, blocking?.req?.captureKey,
                    blocking?.let { monoClock() - it.startedMono } ?: 0L)
            }
            return Outcome.SKIPPED_INFLIGHT
        }
        // (2) durable intent. The owner reports its own storage faults; it never blocks capture.
        try { owner.beforeSubmit(req) } catch (_: Throwable) { /* evidence path must not abort capture */ }

        val task = object : FutureTask<Computed>(Callable {
            val cpu0 = safeCpu()
            val t0 = monoClock()
            val value = compute(req)
            val cpu1 = safeCpu()
            Computed(value, (monoClock() - t0).coerceAtLeast(0), if (cpu0 >= 0 && cpu1 >= 0) cpu1 - cpu0 else -1L)
        }) {
            override fun done() {
                // Python worker thread: CAS + bounded offer only. Never throws.
                try {
                    running.compareAndSet(cell, null)
                    if (cell.phase.compareAndSet(Phase.LATE, Phase.LATE_CLAIMED)) dispatchLate(cell)
                } catch (_: Throwable) { }
            }
        }
        cell.task = task
        try {
            worker.execute(task)
        } catch (e: RejectedExecutionException) {
            running.compareAndSet(cell, null)
            cell.phase.set(Phase.CALLER)
            try { owner.onFailed(req, Failure.SubmitRejected, 0) } finally { admitted.remove(req.captureKey, cell) }
            return Outcome.SUBMIT_REJECTED
        }
        scheduleStuckCheck(stuckAfterMs) { checkStuck(cell) }

        val waitStart = monoClock()
        val computed: Computed = try {
            waiter.await(task, waitMs)
        } catch (e: TimeoutException) {
            val waited = monoClock() - waitStart
            cell.phase.set(Phase.LATE)  // only the caller leaves RUNNING
            // Completed in the gap between the wait expiring and LATE being published:
            // done() may already have run and seen RUNNING, so the caller claims it here.
            if (task.isDone && cell.phase.compareAndSet(Phase.LATE, Phase.LATE_CLAIMED)) dispatchLate(cell)
            lightly { owner.onLatePending(req, waited) }
            return Outcome.LATE_PENDING
        } catch (e: ExecutionException) {
            cell.phase.set(Phase.CALLER)
            try { owner.onFailed(req, Failure.Exception(e.cause ?: e, late = false), cell.skipped.get()) }
            finally { admitted.remove(req.captureKey, cell) }
            return Outcome.FAILED_EARLY
        } catch (e: CancellationException) {
            cell.phase.set(Phase.CALLER)
            try { owner.onFailed(req, Failure.Cancelled, cell.skipped.get()) } finally { admitted.remove(req.captureKey, cell) }
            return Outcome.FAILED_EARLY
        }
        cell.phase.set(Phase.CALLER)
        val waited = monoClock() - waitStart
        val job = Runnable {
            try { owner.onBuilt(req, CaptureDelivery.ON_TIME, computed, waited, cell.skipped.get()) }
            finally { admitted.remove(req.captureKey, cell) }
        }
        // On-time: the caller thread already owns the value; if the finisher is saturated it
        // runs the same fan-out inline (the pre-b503 behaviour) instead of dropping it.
        if (!finisher.offer(job)) job.run()
        return Outcome.DELIVERED_ON_TIME
    }

    private fun dispatchLate(cell: Cell) {
        val task = cell.task ?: return
        val job = Runnable {
            try {
                val computed = try { task.get() } catch (e: ExecutionException) {
                    owner.onFailed(cell.req, Failure.Exception(e.cause ?: e, late = true), cell.skipped.get()); null
                } catch (e: CancellationException) {
                    owner.onFailed(cell.req, Failure.Cancelled, cell.skipped.get()); null
                }
                if (computed != null) {
                    owner.onBuilt(cell.req, CaptureDelivery.LATE, computed,
                        monoClock() - cell.startedMono, cell.skipped.get())
                }
            } finally { admitted.remove(cell.req.captureKey, cell) }
        }
        if (!finisher.offer(job)) {
            // Saturated: an explicit terminal record, never silently. R2: ownership (admission) is released only
            // AFTER the owner has durably recorded the outcome, so reconciliation never sees an ownerless intent.
            val record = Runnable {
                try { owner.onFinisherRejected(cell.req, CaptureDelivery.LATE) } catch (_: Throwable) { }
                finally { admitted.remove(cell.req.captureKey, cell) }
            }
            if (!control.offer(record)) { control.recordOverflow(); record.run() }
        }
    }

    private fun checkStuck(cell: Cell) {
        val task = cell.task ?: return
        val age = monoClock() - cell.startedMono
        if (!task.isDone && age >= stuckAfterMs && cell.stuckReported.compareAndSet(false, true)) {
            lightly { owner.onStuck(cell.req, age) }
        }
    }

    /** Terminal evidence: control lane, or inline when the lane is saturated (never dropped). */
    private fun durably(block: () -> Unit) {
        val r = Runnable { runCatching(block) }
        if (!control.offer(r)) { control.recordOverflow(); r.run() }
    }

    private fun lightly(block: () -> Unit) {
        if (!control.offer(Runnable { runCatching(block) })) control.recordOverflow()
    }

    private fun safeCpu(): Long = try { cpuClockMs() } catch (_: Throwable) { -1L }
}

/**
 * Hard-bounded, process-owned serial worker with an explicit rejection policy:
 * at most [capacity] queued jobs plus one running. offer() never blocks and returns
 * false when full; the caller owns the rejected job's outcome.
 */
open class CaptureFinisher(private val name: String, val capacity: Int, executor: Executor? = null) {
    private val overflow = AtomicInteger(0)
    private val exec: Executor = executor ?: ThreadPoolExecutor(
        1, 1, 0L, TimeUnit.MILLISECONDS, ArrayBlockingQueue(capacity)
    ) { r -> Thread(r, name).apply { isDaemon = true } }

    open fun offer(job: Runnable): Boolean = try {
        exec.execute(Runnable { try { job.run() } catch (_: Throwable) { } })
        true
    } catch (_: RejectedExecutionException) { overflow.incrementAndGet(); false }

    fun recordOverflow() { overflow.incrementAndGet() }
    fun overflowCount(): Int = overflow.get()
    fun queued(): Int = (exec as? ThreadPoolExecutor)?.queue?.size ?: 0
}
