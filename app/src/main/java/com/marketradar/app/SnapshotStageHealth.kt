package com.marketradar.app

import org.json.JSONObject

/**
 * Bounded session counters; failures are visible in the next retained snapshot AND (b503) in
 * the capture-event ledger, which does not depend on a later snapshot succeeding.
 * Existing counters keep their meaning: "timeout" = the caller's wait expired (the call may
 * still complete and be delivered LATE), "built"/"queued"/"enqueue_failed" as before.
 */
object SnapshotStageHealth {
    private val events = setOf("attempt", "built", "timeout", "inflight_skip", "reservation_skip",
        "exception", "queued", "enqueue_failed", "upload_ack", "upload_unacknowledged",
        // b503 capture reliability
        "late_pending", "late_built", "late_queued", "late_exception", "capture_stuck",
        "duplicate_skip", "submit_rejected", "finisher_rejected", "identity_rejected",
        "channel_enqueue_failed", "partial_recovered", "uncertain_interrupted", "terminal_loss",
        "journal_fault", "event_ledger_fault")

    /** Fixed elapsed histogram (wall ms from submission to completion). */
    val BUCKETS = listOf(2_000L to "lt_2s", 3_000L to "2_3s", 4_000L to "3_4s", 6_000L to "4_6s",
        10_000L to "6_10s", 20_000L to "10_20s", Long.MAX_VALUE to "gt_20s")

    fun bucketOf(elapsedMs: Long): String = BUCKETS.first { elapsedMs < it.first }.second

    private fun state(previous: String?, session: String): JSONObject {
        val old = runCatching { JSONObject(previous ?: "{}") }.getOrDefault(JSONObject())
        return if (old.optString("session_date") == session) old else JSONObject()
            .put("schema_version", "snapshot_stage_health_v1").put("session_date", session)
    }

    fun record(previous: String?, session: String, event: String, poll: String,
               elapsedMs: Long = 0, exceptionClass: String? = null): JSONObject {
        require(event in events)
        val state = state(previous, session)
        val counts = state.optJSONObject("counts") ?: JSONObject().also { state.put("counts", it) }
        counts.put(event, counts.optInt(event) + 1)
        state.put("last_event", event).put("last_poll_key", poll.take(100))
            .put("last_elapsed_ms", elapsedMs.coerceAtLeast(0))
        if (exceptionClass != null) state.put("last_exception_class", exceptionClass.take(100))
        return state
    }

    /**
     * b503 timings for one completed computation. All values are diagnostics; -1 means
     * unavailable (e.g. thread CPU time unsupported). Never throws on odd input.
     */
    fun recordTiming(previous: String?, session: String, poll: String, delivery: String,
                     elapsedMs: Long, freezeMs: Long, pythonMs: Long, pythonCpuMs: Long, postMs: Long): JSONObject {
        val state = state(previous, session)
        state.put("timing_schema", "capture_timing_v1")
        val hist = state.optJSONObject("elapsed_histogram") ?: JSONObject().also { state.put("elapsed_histogram", it) }
        val b = bucketOf(elapsedMs.coerceAtLeast(0))
        hist.put(b, hist.optInt(b) + 1)
        if (elapsedMs > state.optLong("max_elapsed_ms", -1)) {
            state.put("max_elapsed_ms", elapsedMs).put("max_elapsed_poll_key", poll.take(100))
        }
        state.put("last_timing", JSONObject().put("poll_key", poll.take(100)).put("delivery", delivery)
            .put("elapsed_ms", elapsedMs).put("freeze_ms", freezeMs).put("python_ms", pythonMs)
            .put("python_thread_cpu_ms", pythonCpuMs).put("post_build_ms", postMs))
        if (pythonMs > 0 && pythonCpuMs >= 0) {
            state.put("last_python_cpu_to_wall", Math.round(pythonCpuMs * 1000.0 / pythonMs) / 1000.0)
        }
        return state
    }
}
