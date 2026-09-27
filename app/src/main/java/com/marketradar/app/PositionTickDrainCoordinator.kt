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
     * connected. [onDurable] runs after the request is durably recorded.
     */
    fun enqueue(delayMs: Long, onDurable: (() -> Unit)? = null)
    fun cancel()
}

/**
 * Request a durable attempt at [atMs] (clamped to now). An already-scheduled
 * EARLIER (or equal) attempt is kept: later triggers never postpone it.
 * Returns true when a job was (re)enqueued.
 */
internal fun requestDurablePositionTickDrain(
    kv: PositionTickKv,
    scheduler: PositionTickWorkScheduler,
    nowMs: Long,
    atMs: Long,
    onDurable: (() -> Unit)? = null
): Boolean = synchronized(PositionTickScheduleLock) {
    val target = maxOf(atMs, nowMs)
    val existing = kv.getLong(PREF_POSITION_TICK_NEXT_DRAIN_AT_MS, 0L)
    val lost = existing > 0L && existing < nowMs - POSITION_TICK_SCHEDULE_LOST_AFTER_MS
    if (existing > 0L && existing <= target && !lost) {
        onDurable?.invoke()
        return@synchronized false
    }
    val effective = if (lost) minOf(target, nowMs) else target
    kv.edit(durable = true) { putLong(PREF_POSITION_TICK_NEXT_DRAIN_AT_MS, effective) }
    scheduler.enqueue(effective - nowMs, onDurable)
    true
}

/** Guards the stored next-attempt slot (receivers write it without the drain mutex). */
internal object PositionTickScheduleLock

/** Queue empty: cancel any pending job and forget the stored time. */
internal fun cancelDurablePositionTickDrain(kv: PositionTickKv, scheduler: PositionTickWorkScheduler) = synchronized(PositionTickScheduleLock) {
    if (kv.getLong(PREF_POSITION_TICK_NEXT_DRAIN_AT_MS, 0L) != 0L) {
        kv.edit(durable = true) { putLong(PREF_POSITION_TICK_NEXT_DRAIN_AT_MS, 0L) }
    }
    scheduler.cancel()
}

/**
 * Boot / package-replaced / time-change receivers: enqueue the durable job
 * directly and call [finish] (the receiver's PendingResult.finish) only once the
 * enqueue is durably recorded — no work continues on an unprotected thread.
 */
internal fun enqueuePositionTickDrainFromReceiver(
    kv: PositionTickKv,
    scheduler: PositionTickWorkScheduler,
    nowMs: Long,
    finish: () -> Unit
) {
    var finished = false
    val once = { if (!finished) { finished = true; finish() } }
    try {
        requestDurablePositionTickDrain(kv, scheduler, nowMs, nowMs, once)
    } catch (e: Exception) {
        once()
        throw e
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
                kv.edit(durable = true) { putLong(PREF_POSITION_TICK_NEXT_DRAIN_AT_MS, 0L) }
            }
        }
        runPassLocked(PositionTickDrainTrigger.WORKER)
    }

    private fun runPassLocked(trigger: String): PositionTickPassResult {
        val depth = synchronized(lock) { store.load().length() }
        if (depth == 0) {
            cancelDurablePositionTickDrain(kv, scheduler)
            return PositionTickPassResult(PositionTickPassOutcome.EMPTY)
        }
        val now = clock()
        if (!positionTickUploadEligible(depth, networkAvailable())) {
            // No POST and no spinning: the job's CONNECTED constraint waits for a network.
            requestDurablePositionTickDrain(kv, scheduler, now, now)
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
            requestDurablePositionTickDrain(kv, scheduler, now, at)
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
            requestDurablePositionTickDrain(kv, scheduler, now, at)
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

        // --- queue-derived tracking state (overflow_active / tracking_complete)
        val snapshot = readPositionTickTrackingSnapshot(kv, store)
        afterDrainHook?.invoke()
        val committed = run {
            val tracking = positionTickTrackingAfterDrain(
                report.copy(remaining = snapshot.depth), snapshot.overflowActive,
                snapshot.trackingComplete, snapshot.rejectedTotal
            )
            kv.edit(durable = true) {
                putBoolean(PREF_POSITION_TICK_OVERFLOW_ACTIVE, tracking.overflowActive)
                putBoolean(PREF_POSITION_TICK_TRACKING_COMPLETE, tracking.trackingComplete)
            }
            Pair(tracking, snapshot.rejectedTotal)
        }
        val tracking = committed.first
        val rejectedTotal = committed.second

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
            report.remaining > 0 -> after // budget exhausted or rows enqueued meanwhile: continue
            else -> null
        }
        if (next != null) {
            // Keeps any other, earlier pending attempt (never postpones it).
            requestDurablePositionTickDrain(kv, scheduler, after, next)
        } else {
            cancelDurablePositionTickDrain(kv, scheduler)
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
