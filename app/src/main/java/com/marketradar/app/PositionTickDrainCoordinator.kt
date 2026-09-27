package com.marketradar.app

import org.json.JSONArray

/*
 * B1.1 R2 — one drain pass plus its DURABLE follow-up scheduling (Codex R2 §4).
 *
 * Pure JVM code: storage, transport, network, clock, scheduler and gate are
 * injected so every scheduling and concurrency rule is covered by executable
 * tests. The Android wiring (SharedPreferences, SupabaseClient, WorkManager)
 * is in PositionTickUploadRunner.kt / PositionTickDrainWorker.kt.
 *
 * Nothing here fetches quotes, evaluates policy, posts alerts or starts the
 * capture service.
 */

/** Unique WorkManager identity for the pending-tick drain. */
internal const val POSITION_TICK_DRAIN_WORK_NAME = "position_tick_pending_drain_v1"

/** Earliest required next attempt (epoch ms) of the pending durable job; 0 = none. */
internal const val PREF_POSITION_TICK_NEXT_DRAIN_AT_MS = "position_tick_next_drain_at_ms"

/**
 * Schema/auth/config/unverified-conflict failures: rows are retained and the
 * failure is logged; the only automatic retry is this deliberately long bounded
 * diagnostic retry (≤ 4/day). App start / update / connectivity triggers still
 * retry after the normal ≤ 5 min backoff.
 */
internal const val POSITION_TICK_DURABLE_FAILURE_RETRY_MS = 6 * 60 * 60 * 1000L

/** A stored next-attempt older than this is treated as lost and re-enqueued (never later than now). */
internal const val POSITION_TICK_SCHEDULE_LOST_AFTER_MS = 24 * 60 * 60 * 1000L

/** Minimal key-value store (SharedPreferences in the app, a map in tests). */
internal interface PositionTickKv {
    fun getString(key: String, def: String?): String?
    fun getInt(key: String, def: Int): Int
    fun getLong(key: String, def: Long): Long
    fun getBoolean(key: String, def: Boolean): Boolean
    /** Applies all puts in [block] as one edit; [durable] = commit() (else apply()). */
    fun edit(durable: Boolean = true, block: PositionTickKvEditor.() -> Unit)
}

internal interface PositionTickKvEditor {
    fun putString(key: String, value: String)
    fun putInt(key: String, value: Int)
    fun putLong(key: String, value: Long)
    fun putBoolean(key: String, value: Boolean)
}

/** Queue store backed by the same key-value store as the flags. */
internal class KvPositionTickQueueStore(private val kv: PositionTickKv) : PositionTickQueueStore {
    override fun load(): JSONArray = try {
        JSONArray(kv.getString(PREF_POSITION_TICK_PENDING_QUEUE, "[]") ?: "[]")
    } catch (_: Exception) {
        JSONArray()
    }

    override fun save(queue: JSONArray) {
        kv.edit(durable = true) { putString(PREF_POSITION_TICK_PENDING_QUEUE, queue.toString()) }
    }
}

/** Persistent one-off scheduler (unique work, CONNECTED constraint, REPLACE on enqueue). */
internal interface PositionTickWorkScheduler {
    /**
     * Replace the unique job with one that runs after [delayMs] once a network is
     * connected. May throw synchronously (e.g. WorkManager unavailable). Otherwise
     * [onResult] is invoked exactly once — possibly later, on another thread —
     * with true only when WorkManager reports the enqueue operation SUCCEEDED,
     * false when the operation failed (B1.1 R3, Codex Blocker A).
     */
    fun enqueue(delayMs: Long, onResult: (Boolean) -> Unit)
    fun cancel()
}

/**
 * Reservation of the unique job (B1.1 R3). [PREF_POSITION_TICK_NEXT_DRAIN_AT_MS]
 * holds the reserved time, [PREF_POSITION_TICK_NEXT_DRAIN_TOKEN] the token of the
 * enqueue attempt that owns it and [PREF_POSITION_TICK_NEXT_DRAIN_CONFIRMED]
 * whether WorkManager confirmed that attempt. Only a CONFIRMED reservation is
 * trusted; a pending one (in flight, or orphaned by process death) never
 * suppresses a real enqueue.
 */
internal const val PREF_POSITION_TICK_NEXT_DRAIN_TOKEN = "position_tick_next_drain_token"
internal const val PREF_POSITION_TICK_NEXT_DRAIN_CONFIRMED = "position_tick_next_drain_confirmed"
/** Monotonic token source (never reset). */
internal const val PREF_POSITION_TICK_DRAIN_TOKEN_SEQ = "position_tick_drain_token_seq"

/** Enqueue result classes (fixed, privacy-safe log values). */
internal object PositionTickEnqueueResult {
    const val CONFIRMED = "confirmed"
    const val KEPT_CONFIRMED = "kept_confirmed"
    const val SYNC_FAILURE = "sync_failure"
    const val ASYNC_FAILURE = "async_failure"
}

private fun PositionTickKvEditor.clearReservation() {
    putLong(PREF_POSITION_TICK_NEXT_DRAIN_AT_MS, 0L)
    putLong(PREF_POSITION_TICK_NEXT_DRAIN_TOKEN, 0L)
    putBoolean(PREF_POSITION_TICK_NEXT_DRAIN_CONFIRMED, false)
}

/**
 * Settle one enqueue attempt: confirm on success, compare-and-clear on failure.
 * Only the reservation still owned by [token] is touched, so a late result of a
 * superseded attempt can neither confirm nor erase a newer reservation.
 * Returns true when the reservation belonged to [token].
 */
internal fun settlePositionTickReservation(kv: PositionTickKv, token: Long, succeeded: Boolean): Boolean =
    synchronized(PositionTickScheduleLock) {
        if (kv.getLong(PREF_POSITION_TICK_NEXT_DRAIN_TOKEN, 0L) != token) return@synchronized false
        kv.edit(durable = true) {
            if (succeeded) putBoolean(PREF_POSITION_TICK_NEXT_DRAIN_CONFIRMED, true) else clearReservation()
        }
        true
    }

/**
 * Request a durable attempt at [atMs] (clamped to now). A CONFIRMED earlier (or
 * equal) attempt is kept: later triggers never postpone it. A merely pending
 * reservation is not trusted: a fresh real enqueue is made at
 * min(pending, target), which also never postpones anything.
 *
 * [onComplete] is invoked exactly once with the outcome class
 * ([PositionTickEnqueueResult]); true in [onComplete] only for confirmed work.
 * Never throws for scheduler failures. Returns true when an enqueue was attempted.
 */
internal fun requestDurablePositionTickDrain(
    kv: PositionTickKv,
    scheduler: PositionTickWorkScheduler,
    nowMs: Long,
    atMs: Long,
    log: (Char, String) -> Unit = { _, _ -> },
    onComplete: ((ok: Boolean, result: String) -> Unit)? = null
): Boolean {
    val token: Long
    val effective: Long
    synchronized(PositionTickScheduleLock) {
        val target = maxOf(atMs, nowMs)
        val existing = kv.getLong(PREF_POSITION_TICK_NEXT_DRAIN_AT_MS, 0L)
        val confirmed = existing > 0L && kv.getBoolean(PREF_POSITION_TICK_NEXT_DRAIN_CONFIRMED, false)
        val lost = existing > 0L && existing < nowMs - POSITION_TICK_SCHEDULE_LOST_AFTER_MS
        if (confirmed && existing <= target && !lost) {
            onComplete?.invoke(true, PositionTickEnqueueResult.KEPT_CONFIRMED)
            return false
        }
        effective = when {
            lost -> minOf(target, nowMs)
            existing > 0L && !confirmed -> maxOf(nowMs, minOf(existing, target))
            else -> target
        }
        token = kv.getLong(PREF_POSITION_TICK_DRAIN_TOKEN_SEQ, 0L) + 1
        // Reservation is PENDING (unconfirmed) until WorkManager reports success.
        kv.edit(durable = true) {
            putLong(PREF_POSITION_TICK_DRAIN_TOKEN_SEQ, token)
            putLong(PREF_POSITION_TICK_NEXT_DRAIN_AT_MS, effective)
            putLong(PREF_POSITION_TICK_NEXT_DRAIN_TOKEN, token)
            putBoolean(PREF_POSITION_TICK_NEXT_DRAIN_CONFIRMED, false)
        }
    }
    var reported = false
    val report = { ok: Boolean, result: String ->
        val first = synchronized(PositionTickScheduleLock) { if (reported) false else { reported = true; true } }
        if (first) onComplete?.invoke(ok, result)
    }
    try {
        scheduler.enqueue(effective - nowMs) { succeeded ->
            settlePositionTickReservation(kv, token, succeeded)
            if (!succeeded) {
                log('W', "POSITION_TICK_DURABLE_ENQUEUE_FAIL: stage=async delay_ms=${effective - nowMs} reservation_cleared=owned_only")
            }
            report(succeeded, if (succeeded) PositionTickEnqueueResult.CONFIRMED else PositionTickEnqueueResult.ASYNC_FAILURE)
        }
    } catch (e: Exception) {
        settlePositionTickReservation(kv, token, false)
        log('W', "POSITION_TICK_DURABLE_ENQUEUE_FAIL: stage=sync ex=${e.javaClass.simpleName} reservation_cleared=owned_only")
        report(false, PositionTickEnqueueResult.SYNC_FAILURE)
    }
    return true
}

/** Guards the stored reservation (receivers write it without the drain mutex). */
internal object PositionTickScheduleLock

/** Queue empty: cancel any pending job and forget the reservation. */
internal fun cancelDurablePositionTickDrain(
    kv: PositionTickKv,
    scheduler: PositionTickWorkScheduler,
    log: (Char, String) -> Unit = { _, _ -> }
) = synchronized(PositionTickScheduleLock) {
    if (kv.getLong(PREF_POSITION_TICK_NEXT_DRAIN_AT_MS, 0L) != 0L || kv.getLong(PREF_POSITION_TICK_NEXT_DRAIN_TOKEN, 0L) != 0L) {
        kv.edit(durable = true) { clearReservation() }
    }
    try {
        scheduler.cancel()
    } catch (e: Exception) {
        // A surviving job finds an empty queue and cancels itself.
        log('W', "POSITION_TICK_DURABLE_CANCEL_FAIL: ex=${e.javaClass.simpleName}")
    }
}

/**
 * Boot / package-replaced / time-change receivers: enqueue the durable job
 * directly and call [finish] (the receiver's PendingResult.finish) exactly once,
 * after the enqueue succeeded OR failed (sync or async) — no work continues on
 * an unprotected thread and a failure never leaves a trusted phantom slot.
 */
internal fun enqueuePositionTickDrainFromReceiver(
    kv: PositionTickKv,
    scheduler: PositionTickWorkScheduler,
    nowMs: Long,
    log: (Char, String) -> Unit = { _, _ -> },
    finish: () -> Unit
) {
    val finished = java.util.concurrent.atomic.AtomicBoolean(false)
    val once = { if (finished.compareAndSet(false, true)) finish() }
    try {
        requestDurablePositionTickDrain(kv, scheduler, nowMs, nowMs, log) { ok, result ->
            log(if (ok) 'I' else 'W', "POSITION_TICK_RECEIVER_ENQUEUE: result=$result")
            once()
        }
    } catch (e: Exception) {
        log('W', "POSITION_TICK_RECEIVER_ENQUEUE: result=error ex=${e.javaClass.simpleName}")
        once()
    }
}

/**
 * Completion of a WorkManager `Operation.result` future, called from its
 * listener once the future is done: success only if `get()` returns normally.
 */
internal fun completePositionTickEnqueueFuture(future: java.util.concurrent.Future<*>, onResult: (Boolean) -> Unit) {
    val ok = try {
        future.get()
        true
    } catch (_: Throwable) {
        false
    }
    onResult(ok)
}

/** Bounded explicit WorkManager backoff for an unexpected worker exception (Codex R3 Blocker B). */
internal const val POSITION_TICK_WORKER_RETRY_BACKOFF_MS = 60_000L
/** WorkManager caps exponential backoff at 5 h; this is the bound we rely on and document. */
internal const val POSITION_TICK_WORKER_RETRY_MAX_BACKOFF_MS = 5 * 60 * 60 * 1000L
/** Executor-pass unexpected exception: durable fallback attempt after this delay. */
internal const val POSITION_TICK_EXECUTOR_FAILURE_RETRY_MS = 60_000L

internal enum class PositionTickWorkerResult { SUCCESS, RETRY }

/** WorkManager's EXPONENTIAL backoff for [runAttemptCount] (0-based), capped. */
internal fun positionTickWorkerRetryDelayMs(runAttemptCount: Int): Long {
    val shift = runAttemptCount.coerceIn(0, 20)
    return minOf(POSITION_TICK_WORKER_RETRY_BACKOFF_MS shl shift, POSITION_TICK_WORKER_RETRY_MAX_BACKOFF_MS)
}

/**
 * Worker body (B1.1 R3): classified outcomes are handled (and durably followed
 * up) inside the pass → SUCCESS. An UNEXPECTED exception escaping the pass →
 * RETRY: WorkManager re-runs this same unique job after the explicit bounded
 * backoff. The queue is untouched here; the reservation records the retry so
 * later triggers keep the "never postpone an earlier attempt" rule.
 */
internal fun runPositionTickWorker(
    kv: PositionTickKv,
    nowMs: () -> Long,
    runAttemptCount: Int,
    log: (Char, String) -> Unit,
    pass: () -> Unit
): PositionTickWorkerResult = try {
    pass()
    PositionTickWorkerResult.SUCCESS
} catch (e: Exception) {
    val delay = positionTickWorkerRetryDelayMs(runAttemptCount)
    val now = nowMs()
    try {
        synchronized(PositionTickScheduleLock) {
            val existing = kv.getLong(PREF_POSITION_TICK_NEXT_DRAIN_AT_MS, 0L)
            val confirmed = existing > 0L && kv.getBoolean(PREF_POSITION_TICK_NEXT_DRAIN_CONFIRMED, false)
            if (!confirmed || existing > now + delay) {
                val token = kv.getLong(PREF_POSITION_TICK_DRAIN_TOKEN_SEQ, 0L) + 1
                kv.edit(durable = true) {
                    putLong(PREF_POSITION_TICK_DRAIN_TOKEN_SEQ, token)
                    putLong(PREF_POSITION_TICK_NEXT_DRAIN_AT_MS, now + delay)
                    putLong(PREF_POSITION_TICK_NEXT_DRAIN_TOKEN, token)
                    // WorkManager itself persists and re-runs a Result.retry() job.
                    putBoolean(PREF_POSITION_TICK_NEXT_DRAIN_CONFIRMED, true)
                }
            }
        }
    } catch (_: Exception) {
        // Reservation bookkeeping failed; WorkManager still retries the job.
    }
    log('E', "POSITION_TICK_DRAIN_WORKER_ERROR: ex=${e.javaClass.simpleName} attempt=$runAttemptCount result=retry backoff_ms=$delay")
    PositionTickWorkerResult.RETRY
}

/**
 * Opportunistic in-process executor pass (B1.1 R3): an unexpected exception
 * arms a durable fallback (coalesced by the reservation rules, so repeated
 * failures do not spin). If WorkManager is unavailable the attempt fails
 * honestly, its reservation is cleared and the next trigger re-enqueues.
 */
internal fun runPositionTickExecutorPass(
    kv: PositionTickKv,
    scheduler: PositionTickWorkScheduler,
    nowMs: () -> Long,
    trigger: String,
    log: (Char, String) -> Unit,
    pass: () -> Unit
) {
    try {
        pass()
    } catch (e: Exception) {
        log('E', "POSITION_TICK_DRAIN_ERROR: trigger=$trigger ex=${e.javaClass.simpleName} durable_fallback=armed")
        try {
            val now = nowMs()
            requestDurablePositionTickDrain(kv, scheduler, now, now + POSITION_TICK_EXECUTOR_FAILURE_RETRY_MS, log)
        } catch (e2: Exception) {
            log('E', "POSITION_TICK_DRAIN_FALLBACK_FAIL: ex=${e2.javaClass.simpleName}")
        }
    }
}

internal val POSITION_TICK_TRANSIENT_FAILURE_CLASSES: Set<String> = setOf(
    POSITION_TICK_FLUSH_TRANSIENT_NETWORK, POSITION_TICK_FLUSH_TRANSPORT,
    POSITION_TICK_FLUSH_SERVER_5XX, POSITION_TICK_FLUSH_UNKNOWN
)

/**
 * After a schema/auth/config/unverified-conflict failure only these triggers
 * (app start — which also follows boot and app update — plus explicit boot /
 * package-replaced) may retry before the long diagnostic retry is due.
 */
internal val POSITION_TICK_DURABLE_FAILURE_RETRY_TRIGGERS: Set<String> = setOf(
    PositionTickDrainTrigger.APP_START, PositionTickDrainTrigger.PACKAGE_REPLACED, PositionTickDrainTrigger.BOOT
)

internal fun isDurablePositionTickFailure(failures: Int, lastClass: String?): Boolean =
    failures > 0 && lastClass != null && lastClass != POSITION_TICK_FLUSH_OK &&
        lastClass !in POSITION_TICK_TRANSIENT_FAILURE_CLASSES

internal object PositionTickPassOutcome {
    const val EMPTY = "empty"
    const val NO_NETWORK = "no_network"
    const val DEFERRED = "deferred"
    const val RAN = "ran"
}

internal data class PositionTickPassResult(
    val outcome: String,
    val report: PositionTickDrainReport? = null,
    /** Absolute time of the durable follow-up requested by this pass, or null (none / cancelled). */
    val nextAttemptAtMs: Long? = null,
    val overflowActive: Boolean? = null,
    val trackingComplete: Boolean? = null
)

internal data class PositionTickTrackingSnapshot(
    val depth: Int,
    val overflowActive: Boolean,
    val trackingComplete: Boolean,
    val rejectedTotal: Long
)

internal fun readPositionTickTrackingSnapshot(kv: PositionTickKv, store: PositionTickQueueStore) =
    PositionTickTrackingSnapshot(
        depth = store.load().length(),
        overflowActive = kv.getBoolean(PREF_POSITION_TICK_OVERFLOW_ACTIVE, false),
        trackingComplete = kv.getBoolean(PREF_POSITION_TICK_TRACKING_COMPLETE, true),
        rejectedTotal = kv.getLong(PREF_POSITION_TICK_OVERFLOW_REJECTED_COUNT, 0L)
    )

/** Serialises every drain pass in the process (executor triggers and the worker). */
internal object PositionTickDrainMutex

internal class PositionTickDrainCoordinator(
    private val kv: PositionTickKv,
    private val transport: PositionTickTransport,
    private val networkAvailable: () -> Boolean,
    private val clock: () -> Long,
    private val scheduler: PositionTickWorkScheduler,
    private val sendClientEventId: () -> Boolean = { false },
    private val log: (Char, String) -> Unit = { _, _ -> },
    private val lock: Any = PositionTickQueueLock,
    private val config: PositionTickDrainConfig = PositionTickDrainConfig(),
    /** Test seam: runs after the drain returned and before tracking state is committed. */
    private val afterDrainHook: (() -> Unit)? = null
) {
    private val store = KvPositionTickQueueStore(kv)

    fun runPass(trigger: String): PositionTickPassResult = synchronized(PositionTickDrainMutex) { runPassLocked(trigger) }

    /**
     * Worker entry: the durable job that is running now releases its (due) slot
     * first, so this pass can schedule the next attempt. A slot that is still in
     * the future belongs to a newer, earlier-or-equal request and is kept.
     */
    fun runWorkerPass(): PositionTickPassResult = synchronized(PositionTickDrainMutex) {
        synchronized(PositionTickScheduleLock) {
            val stored = kv.getLong(PREF_POSITION_TICK_NEXT_DRAIN_AT_MS, 0L)
            if (stored != 0L && stored <= clock()) {
                kv.edit(durable = true) {
                    putLong(PREF_POSITION_TICK_NEXT_DRAIN_AT_MS, 0L)
                    putLong(PREF_POSITION_TICK_NEXT_DRAIN_TOKEN, 0L)
                    putBoolean(PREF_POSITION_TICK_NEXT_DRAIN_CONFIRMED, false)
                }
            }
        }
        runPassLocked(PositionTickDrainTrigger.WORKER)
    }

    private fun runPassLocked(trigger: String): PositionTickPassResult {
        val depth = synchronized(lock) { store.load().length() }
        if (depth == 0) {
            cancelDurablePositionTickDrain(kv, scheduler, log)
            return PositionTickPassResult(PositionTickPassOutcome.EMPTY)
        }
        val now = clock()
        if (!positionTickUploadEligible(depth, networkAvailable())) {
            // No POST and no spinning: the job's CONNECTED constraint waits for a network.
            requestDurablePositionTickDrain(kv, scheduler, now, now, log)
            log('D', "POSITION_TICK_DRAIN_SKIP: trigger=$trigger reason=no_network depth=$depth durable_retry=connected")
            return PositionTickPassResult(PositionTickPassOutcome.NO_NETWORK, nextAttemptAtMs = now)
        }
        val failures = kv.getInt(PREF_POSITION_TICK_FLUSH_FAILURE_COUNT, 0)
        val lastClass = kv.getString(PREF_POSITION_TICK_FLUSH_LAST_CLASS, POSITION_TICK_FLUSH_UNKNOWN)
        val lastFlush = kv.getLong(PREF_POSITION_TICK_LAST_FLUSH_MS, 0L)
        if (isDurablePositionTickFailure(failures, lastClass) &&
            trigger !in POSITION_TICK_DURABLE_FAILURE_RETRY_TRIGGERS &&
            now < lastFlush + POSITION_TICK_DURABLE_FAILURE_RETRY_MS
        ) {
            // No rapid loop: rows retained; wait for app start/update or the long diagnostic retry.
            val at = lastFlush + POSITION_TICK_DURABLE_FAILURE_RETRY_MS
            requestDurablePositionTickDrain(kv, scheduler, now, at, log)
            log(
                'D',
                "POSITION_TICK_FLUSH_BACKOFF: trigger=$trigger wait_ms=${at - now} reason=durable_failure " +
                    "consecutive=$failures class=$lastClass depth=$depth durable_retry=scheduled"
            )
            return PositionTickPassResult(PositionTickPassOutcome.DEFERRED, nextAttemptAtMs = at)
        }
        val decision = decidePositionTickDrainAttempt(trigger, now, lastFlush, failures, lastClass)
        if (!decision.allowed) {
            val at = now + decision.waitMs
            requestDurablePositionTickDrain(kv, scheduler, now, at, log)
            log(
                'D',
                "POSITION_TICK_FLUSH_BACKOFF: trigger=$trigger wait_ms=${decision.waitMs} " +
                    "reason=${decision.reason} consecutive=$failures class=$lastClass depth=$depth durable_retry=scheduled"
            )
            return PositionTickPassResult(PositionTickPassOutcome.DEFERRED, nextAttemptAtMs = at)
        }
        kv.edit(durable = true) { putLong(PREF_POSITION_TICK_LAST_FLUSH_MS, now) }

        val started = clock()
        val report = drainPositionTickQueue(store, transport, config.copy(sendClientEventId = sendClientEventId()), lock)
        val elapsed = clock() - started

        // --- queue-derived tracking state (overflow_active / tracking_complete): Codex §5.
        // One atomic transition under the SAME lock as enqueue: reload the current
        // queue, flags and rejected total, derive from those, commit durably. The
        // rejected total is history and is never written here.
        afterDrainHook?.invoke()
        val committed = synchronized(lock) {
            val snapshot = readPositionTickTrackingSnapshot(kv, store)
            val tracking = positionTickTrackingAfterDrain(
                report.copy(remaining = snapshot.depth), snapshot.overflowActive,
                snapshot.trackingComplete, snapshot.rejectedTotal
            )
            kv.edit(durable = true) {
                putBoolean(PREF_POSITION_TICK_OVERFLOW_ACTIVE, tracking.overflowActive)
                putBoolean(PREF_POSITION_TICK_TRACKING_COMPLETE, tracking.trackingComplete)
            }
            Triple(tracking, snapshot.rejectedTotal, snapshot.depth)
        }
        val tracking = committed.first
        val rejectedTotal = committed.second
        val committedDepth = committed.third

        // --- failure counters (serialised by PositionTickDrainMutex)
        val failure = report.failure
        kv.edit(durable = true) {
            if (failure == null) {
                putInt(PREF_POSITION_TICK_FLUSH_FAILURE_COUNT, 0)
                putString(PREF_POSITION_TICK_FLUSH_LAST_CLASS, POSITION_TICK_FLUSH_OK)
            } else {
                putInt(PREF_POSITION_TICK_FLUSH_FAILURE_COUNT, failures + 1)
                putString(PREF_POSITION_TICK_FLUSH_LAST_CLASS, failure.failureClass)
            }
        }

        // --- durable follow-up
        val after = clock()
        val next: Long? = when {
            failure != null && failure.failureClass in POSITION_TICK_TRANSIENT_FAILURE_CLASSES ->
                after + computePositionTickFlushBackoffMs(failures + 1, failure.failureClass)
            failure != null -> after + POSITION_TICK_DURABLE_FAILURE_RETRY_MS
            committedDepth > 0 -> after // budget exhausted or rows enqueued meanwhile: continue
            else -> null
        }
        if (next != null) {
            // Keeps any other, earlier pending attempt (never postpones it).
            requestDurablePositionTickDrain(kv, scheduler, after, next, log)
        } else {
            cancelDurablePositionTickDrain(kv, scheduler, log)
        }

        val line = formatPositionTickDrainLog(
            trigger, report, tracking.overflowActive, rejectedTotal, tracking.trackingComplete, elapsed
        ) + " next_attempt_in_ms=${next?.let { it - after } ?: -1}"
        if (failure == null) {
            log('I', line)
        } else {
            log('W', line)
            log(
                'W',
                formatPositionTickFlushFailLog(
                    consecutive = failures + 1,
                    pending = report.remaining,
                    result = failure,
                    backoffMs = (next ?: after) - after,
                    overflowActive = tracking.overflowActive,
                    trackingComplete = tracking.trackingComplete
                )
            )
            if (failure.failureClass !in POSITION_TICK_TRANSIENT_FAILURE_CLASSES) {
                log(
                    'W',
                    "POSITION_TICK_DRAIN_DURABLE_FAILURE: class=${failure.failureClass} " +
                        "status=${failure.httpStatus ?: -1} retained=${report.remaining} " +
                        "diagnostic_retry_ms=$POSITION_TICK_DURABLE_FAILURE_RETRY_MS"
                )
            }
        }
        if (report.contentConflicts > 0) {
            log(
                'W',
                "POSITION_TICK_QUEUE_CONTENT_CONFLICT: conflicts=${report.contentConflicts} " +
                    "pending=${report.remaining} retained_all=true"
            )
        }
        return PositionTickPassResult(
            PositionTickPassOutcome.RAN, report, next, tracking.overflowActive, tracking.trackingComplete
        )
    }
}
