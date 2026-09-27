package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject

/*
 * B1.1 — independent, bounded, idempotency-ready pending-tick drain.
 *
 * Capture eligibility (quotes + policy work) and upload eligibility are separate:
 *   - capture: open trade(s) AND regular market session — unchanged, still owned by
 *     the capture service (PositionTickService.ensureRunning + its capture loop);
 *   - upload: non-empty pending queue AND network available — at ANY time of day,
 *     with or without open trades, with or without the capture service running.
 *
 * This file holds the pure, JVM-testable state machine. The Android runner that
 * wires it to SharedPreferences, SupabaseClient and connectivity lives in
 * PositionTickUploadRunner.kt. Nothing here fetches quotes or evaluates policy.
 */

/** Explicit request bound: never POST more than this many ticks at once. */
internal const val POSITION_TICK_UPLOAD_CHUNK_ROWS = 50

/**
 * Byte guard on one request body (1 MiB). Measured (PositionTickChunkBytesTest):
 * a synthetic worst-case 50-row chunk of the current producer shape is ~597 KiB
 * (~12.1 KB/row); the largest production row shape (24 Sep) gives ~277 KiB even
 * with widened values. If rows ever grow past the guard, the chunk shrinks
 * (minimum one row) instead of sending an oversized request.
 */
internal const val POSITION_TICK_UPLOAD_MAX_CHUNK_BYTES = 1024 * 1024

/** Chunks per drain pass (40 × 50 = 2000 ≥ the 1500-row queue cap). */
internal const val POSITION_TICK_UPLOAD_MAX_CHUNKS_PER_DRAIN = 40

/** Queue capacity (unchanged from the pre-B1.1 service constant). */
internal const val POSITION_TICK_MAX_PENDING = 1_500

/** Shared SharedPreferences keys (file "market_radar"); values unchanged from the service. */
internal const val PREF_POSITION_TICK_PENDING_QUEUE = "position_tick_pending_queue"
internal const val PREF_POSITION_TICK_LAST_FLUSH_MS = "position_tick_last_flush_ms"
internal const val PREF_POSITION_TICK_FLUSH_FAILURE_COUNT = "position_tick_flush_failure_count"
internal const val PREF_POSITION_TICK_FLUSH_LAST_CLASS = "position_tick_flush_last_class"

/** Minimum spacing between non-continuation attempts (storm guard). */
internal const val POSITION_TICK_DRAIN_MIN_SPACING_MS = 5_000L
/** Pre-B1.1 FLUSH_MIN_MS: after-enqueue flushes stay batched at ≥ 60 s. */
internal const val POSITION_TICK_AFTER_ENQUEUE_MIN_MS = 60_000L

/** Single process-wide lock for every read-modify-write of the pending queue. */
internal object PositionTickQueueLock

/** Drain triggers (log vocabulary; never carries payload data). */
internal object PositionTickDrainTrigger {
    const val APP_START = "app_start"
    const val PACKAGE_REPLACED = "package_replaced"
    const val BOOT = "boot"
    const val LIFECYCLE = "lifecycle"
    const val CONNECTIVITY = "connectivity"
    const val SERVICE_START = "service_start"
    const val SERVICE_DESTROY = "service_destroy"
    const val AFTER_ENQUEUE = "after_enqueue"
    const val SESSION_CLOSED = "session_closed"
    const val NO_OPEN_TRADES = "no_open_trades"
    const val ENSURE_RUNNING_NO_CAPTURE = "ensure_running_no_capture"
    const val CONTINUE = "continue"

    /** Triggers allowed to skip a *transient* failure backoff (network came back, app/service (re)started). */
    val RECOVERY: Set<String> = setOf(APP_START, PACKAGE_REPLACED, BOOT, LIFECYCLE, CONNECTIVITY, SERVICE_START)
}

/** Capture eligibility: quotes/policy only for open trades inside the regular session. */
internal fun positionTickCaptureEligible(openTradeCount: Int, marketSessionActive: Boolean): Boolean =
    openTradeCount > 0 && marketSessionActive

/** Upload eligibility: independent of trades and of the session. */
internal fun positionTickUploadEligible(pendingDepth: Int, networkAvailable: Boolean): Boolean =
    pendingDepth > 0 && networkAvailable

internal data class PositionTickDrainAttemptDecision(val allowed: Boolean, val waitMs: Long, val reason: String)

/**
 * Whether a drain attempt may run now. Schema/config/unverified-conflict failures
 * keep their backoff for every trigger (do not hammer a rejected payload);
 * transient failures may be retried early by a recovery trigger.
 */
internal fun decidePositionTickDrainAttempt(
    trigger: String,
    nowMs: Long,
    lastAttemptMs: Long,
    consecutiveFailures: Int,
    lastFailureClass: String?
): PositionTickDrainAttemptDecision {
    val since = if (lastAttemptMs <= 0L) Long.MAX_VALUE else nowMs - lastAttemptMs
    if (trigger != PositionTickDrainTrigger.CONTINUE && since < POSITION_TICK_DRAIN_MIN_SPACING_MS) {
        return PositionTickDrainAttemptDecision(false, POSITION_TICK_DRAIN_MIN_SPACING_MS - since, "min_spacing")
    }
    if (consecutiveFailures <= 0) {
        if (trigger == PositionTickDrainTrigger.AFTER_ENQUEUE && since < POSITION_TICK_AFTER_ENQUEUE_MIN_MS) {
            return PositionTickDrainAttemptDecision(false, POSITION_TICK_AFTER_ENQUEUE_MIN_MS - since, "after_enqueue_batching")
        }
        return PositionTickDrainAttemptDecision(true, 0L, "ok")
    }
    val cls = lastFailureClass ?: POSITION_TICK_FLUSH_UNKNOWN
    val backoff = computePositionTickFlushBackoffMs(consecutiveFailures, cls)
    val transient = cls == POSITION_TICK_FLUSH_TRANSIENT_NETWORK || cls == POSITION_TICK_FLUSH_TRANSPORT ||
        cls == POSITION_TICK_FLUSH_SERVER_5XX || cls == POSITION_TICK_FLUSH_UNKNOWN
    if (transient && trigger in PositionTickDrainTrigger.RECOVERY) {
        return PositionTickDrainAttemptDecision(true, 0L, "recovery_bypass")
    }
    if (trigger == PositionTickDrainTrigger.CONTINUE) {
        return PositionTickDrainAttemptDecision(true, 0L, "continue")
    }
    return if (since >= backoff) {
        PositionTickDrainAttemptDecision(true, 0L, "backoff_elapsed")
    } else {
        PositionTickDrainAttemptDecision(false, backoff - since, "backoff")
    }
}

/** Persistent queue (SharedPreferences in the app, in-memory in tests). */
internal interface PositionTickQueueStore {
    fun load(): JSONArray
    /** Must be durable before returning (the app uses commit()). */
    fun save(queue: JSONArray)
}

/** One POST of [rows] (already shaped for upload). */
internal fun interface PositionTickTransport {
    fun insert(rows: JSONArray): PositionTickInsertResult
}

internal data class PositionTickDrainConfig(
    val chunkRows: Int = POSITION_TICK_UPLOAD_CHUNK_ROWS,
    val maxChunkBytes: Int = POSITION_TICK_UPLOAD_MAX_CHUNK_BYTES,
    val maxChunksPerDrain: Int = POSITION_TICK_UPLOAD_MAX_CHUNKS_PER_DRAIN,
    /** Producer-key gate result; false => identity stripped from the request body. */
    val sendClientEventId: Boolean = false
)

internal object PositionTickDrainOutcome {
    const val EMPTY = "empty"
    const val DRAINED = "drained"
    const val FAILED = "failed"
    const val BUDGET_EXHAUSTED = "budget_exhausted"
}

internal data class PositionTickDrainReport(
    val outcome: String,
    val depthBefore: Int,
    val identitiesAssigned: Int,
    val exactDupDropped: Int,
    val contentConflicts: Int,
    val chunksAttempted: Int,
    val chunksAcked: Int,
    val rowsAcked: Int,
    /** Rows acknowledged because the server proved the identity already persisted. */
    val rowsIdempotent: Int,
    val requests: Int,
    val remaining: Int,
    val lastChunkRows: Int,
    val maxChunkBytesSent: Int,
    val failure: PositionTickInsertResult?,
    val identitySent: Boolean
) {
    val failureClass: String get() = failure?.failureClass ?: POSITION_TICK_FLUSH_OK
}

private data class SelectedChunk(val rows: List<JSONObject>, val ids: List<String>, val body: JSONArray, val bytes: Int)

/** Exact duplicate proof: HTTP 409 + 23505 on the client_event_id unique index. */
internal fun isPositionTickClientEventIdDuplicate(result: PositionTickInsertResult): Boolean =
    result.httpStatus == 409 &&
        result.allowlistedServerCode == "23505" &&
        result.allowlistedConstraint == POSITION_TICK_CLIENT_EVENT_ID_UNIQUE_INDEX

/**
 * FIFO chunk drain. Invariants (each covered by PositionTickDrainTest):
 * 1. Rows are sent oldest-first in chunks of ≤ [PositionTickDrainConfig.chunkRows].
 * 2. Only rows of a chunk the server confirmed (2xx, or per-row proven identity
 *    duplicate) are removed — by identity, from the CURRENT stored queue, so rows
 *    enqueued concurrently are never lost.
 * 3. The first failing chunk stops the pass; it and every later row stay queued.
 * 4. The queue is never cleared, truncated or reordered; legacy rows only gain
 *    their computed `client_event_id` field.
 */
internal fun drainPositionTickQueue(
    store: PositionTickQueueStore,
    transport: PositionTickTransport,
    config: PositionTickDrainConfig,
    lock: Any = PositionTickQueueLock
): PositionTickDrainReport {
    var depthBefore: Int
    var identitiesAssigned = 0
    var exactDropped = 0
    var conflicts = 0
    synchronized(lock) {
        val queue = store.load()
        depthBefore = queue.length()
        if (depthBefore == 0) {
            return PositionTickDrainReport(
                PositionTickDrainOutcome.EMPTY, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, null, config.sendClientEventId
            )
        }
        val dedupe = dedupePositionTicksByTradeTs(queue)
        exactDropped = dedupe.exactDupDropped
        conflicts = dedupe.contentConflicts
        identitiesAssigned = ensurePositionTickIdentities(dedupe.queue)
        if (exactDropped > 0 || identitiesAssigned > 0) store.save(dedupe.queue)
    }

    var chunksAttempted = 0
    var chunksAcked = 0
    var rowsAcked = 0
    var rowsIdempotent = 0
    var requests = 0
    var lastChunkRows = 0
    var maxBytes = 0
    var failure: PositionTickInsertResult? = null
    var outcome = PositionTickDrainOutcome.BUDGET_EXHAUSTED

    while (chunksAttempted < config.maxChunksPerDrain) {
        val chunk = synchronized(lock) {
            val q = store.load()
            val late = ensurePositionTickIdentities(q)
            if (late > 0) {
                identitiesAssigned += late
                store.save(q)
            }
            selectChunk(q, config)
        }
        if (chunk.rows.isEmpty()) {
            outcome = PositionTickDrainOutcome.DRAINED
            break
        }
        chunksAttempted += 1
        lastChunkRows = chunk.rows.size
        maxBytes = maxOf(maxBytes, chunk.bytes)
        requests += 1
        val result = transport.insert(chunk.body)
        if (shouldDrainPositionTickQueue(result)) {
            removeAcked(store, lock, chunk.ids.toSet())
            chunksAcked += 1
            rowsAcked += chunk.rows.size
            continue
        }
        if (config.sendClientEventId && isPositionTickClientEventIdDuplicate(result)) {
            // At least one row is already persisted with identical content; the
            // statement was rolled back atomically. Resolve row-by-row.
            val ackIds = LinkedHashSet<String>()
            var rowFailure: PositionTickInsertResult? = null
            for ((i, row) in chunk.rows.withIndex()) {
                requests += 1
                val one = JSONArray().put(positionTickUploadRow(row, true))
                val r1 = transport.insert(one)
                when {
                    shouldDrainPositionTickQueue(r1) -> ackIds.add(chunk.ids[i])
                    isPositionTickClientEventIdDuplicate(r1) -> {
                        ackIds.add(chunk.ids[i]); rowsIdempotent += 1
                    }
                    else -> { rowFailure = r1; break }
                }
            }
            removeAcked(store, lock, ackIds)
            rowsAcked += ackIds.size
            if (rowFailure == null) {
                chunksAcked += 1
                continue
            }
            failure = rowFailure
            outcome = PositionTickDrainOutcome.FAILED
            break
        }
        failure = result
        outcome = PositionTickDrainOutcome.FAILED
        break
    }
    val remaining = synchronized(lock) { store.load().length() }
    if (outcome == PositionTickDrainOutcome.BUDGET_EXHAUSTED && remaining == 0) outcome = PositionTickDrainOutcome.DRAINED
    return PositionTickDrainReport(
        outcome = outcome,
        depthBefore = depthBefore,
        identitiesAssigned = identitiesAssigned,
        exactDupDropped = exactDropped,
        contentConflicts = conflicts,
        chunksAttempted = chunksAttempted,
        chunksAcked = chunksAcked,
        rowsAcked = rowsAcked,
        rowsIdempotent = rowsIdempotent,
        requests = requests,
        remaining = remaining,
        lastChunkRows = lastChunkRows,
        maxChunkBytesSent = maxBytes,
        failure = failure,
        identitySent = config.sendClientEventId
    )
}

private fun selectChunk(queue: JSONArray, config: PositionTickDrainConfig): SelectedChunk {
    val rows = ArrayList<JSONObject>()
    val ids = ArrayList<String>()
    val body = JSONArray()
    var bytes = 2 // "[" + "]"
    val limit = config.chunkRows.coerceAtLeast(1)
    for (i in 0 until queue.length()) {
        if (rows.size >= limit) break
        val row = queue.optJSONObject(i) ?: continue
        val up = positionTickUploadRow(row, config.sendClientEventId)
        val rowBytes = up.toString().toByteArray(Charsets.UTF_8).size + if (rows.isEmpty()) 0 else 1
        if (rows.isNotEmpty() && bytes + rowBytes > config.maxChunkBytes) break
        rows.add(row)
        ids.add(positionTickRowIdentity(row))
        body.put(up)
        bytes += rowBytes
    }
    return SelectedChunk(rows, ids, body, if (rows.isEmpty()) 0 else bytes)
}

private fun removeAcked(store: PositionTickQueueStore, lock: Any, ids: Set<String>) {
    if (ids.isEmpty()) return
    synchronized(lock) {
        val q = store.load()
        val out = JSONArray()
        for (i in 0 until q.length()) {
            val row = q.optJSONObject(i)
            if (row != null && positionTickRowIdentity(row) in ids) continue
            out.put(q.opt(i))
        }
        store.save(out)
    }
}

/** Overflow / tracking flags after a drain. Rejected totals are history and never reset here. */
internal data class PositionTickTrackingAfterDrain(
    val overflowActive: Boolean,
    val trackingComplete: Boolean
)

internal fun positionTickTrackingAfterDrain(
    report: PositionTickDrainReport,
    overflowActiveFlag: Boolean,
    trackingCompletePref: Boolean,
    rejectedTotal: Long,
    maxPending: Int = POSITION_TICK_MAX_PENDING
): PositionTickTrackingAfterDrain {
    val progressed = report.rowsAcked > 0
    val overflow = if (progressed && report.remaining < maxPending) false else overflowActiveFlag
    val complete = if (progressed && !overflow && rejectedTotal == 0L) true else trackingCompletePref
    // derivePositionTickTrackingStatus still ANDs with rejectedTotal == 0 for display.
    return PositionTickTrackingAfterDrain(overflow, complete && rejectedTotal == 0L && !overflow)
}

/**
 * Privacy-safe one-line drain summary: counts, sizes, classes and flags only —
 * never trade ids, prices, P&L, payload fragments, response bodies or credentials.
 */
internal fun formatPositionTickDrainLog(
    trigger: String,
    report: PositionTickDrainReport,
    overflowActive: Boolean,
    overflowRejectedTotal: Long,
    trackingComplete: Boolean,
    elapsedMs: Long
): String {
    val f = report.failure
    return "POSITION_TICK_DRAIN: trigger=$trigger outcome=${report.outcome} " +
        "depth_before=${report.depthBefore} chunk_size=$POSITION_TICK_UPLOAD_CHUNK_ROWS " +
        "last_chunk_rows=${report.lastChunkRows} max_chunk_bytes=${report.maxChunkBytesSent} " +
        "chunks=${report.chunksAttempted} chunks_acked=${report.chunksAcked} " +
        "acked=${report.rowsAcked} idempotent=${report.rowsIdempotent} requests=${report.requests} " +
        "remaining=${report.remaining} class=${report.failureClass} status=${f?.httpStatus ?: -1} " +
        "server_code=${f?.allowlistedServerCode ?: "-"} ids_assigned=${report.identitiesAssigned} " +
        "exact_dup_dropped=${report.exactDupDropped} content_conflicts=${report.contentConflicts} " +
        "identity_sent=${report.identitySent} overflow_active=$overflowActive " +
        "overflow_rejected_total=$overflowRejectedTotal tracking_complete=$trackingComplete " +
        "elapsed_ms=$elapsedMs"
}
