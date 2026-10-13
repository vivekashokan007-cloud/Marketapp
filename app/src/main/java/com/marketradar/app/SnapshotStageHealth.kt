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

    /**
     * b503 R1: how an event relates to the stored session.
     *  CURRENT  same validated session (or a newer one: the day rolled over -> fresh state)
     *  PRIOR    a validated OLDER session (late ACK/timing from yesterday): recorded separately,
     *           never resets or pollutes today's counters/histogram
     *  UNATTRIBUTED  no validated origin session (malformed key, ISO poll_ts, "pending=N"):
     *           counted separately, never resets anything
     */
    private enum class Relation { CURRENT, PRIOR, UNATTRIBUTED }

    private val SESSION = Regex("^20\\d{2}-\\d{2}-\\d{2}$")
    const val MAX_PRIOR_SESSIONS = 3

    fun isValidSession(session: String?): Boolean =
        session != null && SESSION.matches(session) && runCatching { java.time.LocalDate.parse(session) }.isSuccess

    private fun state(previous: String?, session: String?): Pair<JSONObject, Relation> {
        val old = runCatching { JSONObject(previous ?: "{}") }.getOrDefault(JSONObject())
        val current = old.optString("session_date", "")
        fun fresh(s: String) = JSONObject().put("schema_version", "snapshot_stage_health_v1").put("session_date", s)
        if (!isValidSession(session)) {
            return (if (old.has("session_date")) old else fresh("unknown")) to Relation.UNATTRIBUTED
        }
        return when {
            current == session -> old to Relation.CURRENT
            isValidSession(current) && session!! < current -> old to Relation.PRIOR
            else -> fresh(session!!) to Relation.CURRENT
        }
    }

    private fun priorBucket(state: JSONObject, session: String): JSONObject {
        val prior = state.optJSONObject("prior_sessions") ?: JSONObject().also { state.put("prior_sessions", it) }
        val bucket = prior.optJSONObject(session) ?: JSONObject().also { prior.put(session, it) }
        val keys = prior.keys().asSequence().toList().sorted()
        keys.dropLast(MAX_PRIOR_SESSIONS).forEach { prior.remove(it) }
        return bucket
    }

    fun record(previous: String?, session: String?, event: String, poll: String,
               elapsedMs: Long = 0, exceptionClass: String? = null): JSONObject {
        require(event in events)
        val (state, relation) = state(previous, session)
        val counts = when (relation) {
            Relation.CURRENT -> state.optJSONObject("counts") ?: JSONObject().also { state.put("counts", it) }
            Relation.PRIOR -> priorBucket(state, session!!).let { b -> b.optJSONObject("counts") ?: JSONObject().also { b.put("counts", it) } }
            Relation.UNATTRIBUTED -> state.optJSONObject("unattributed_counts") ?: JSONObject().also { state.put("unattributed_counts", it) }
        }
        counts.put(event, counts.optInt(event) + 1)
        if (relation == Relation.CURRENT) {
            state.put("last_event", event).put("last_poll_key", poll.take(100))
                .put("last_elapsed_ms", elapsedMs.coerceAtLeast(0))
            if (exceptionClass != null) state.put("last_exception_class", exceptionClass.take(100))
        } else {
            state.put("last_other_event", JSONObject().put("event", event).put("poll_key", poll.take(100))
                .put("relation", relation.name).put("session", session ?: JSONObject.NULL))
        }
        return state
    }

    /**
     * b503 timings for one completed computation. All values are diagnostics; -1 means
     * unavailable (e.g. thread CPU time unsupported). Never throws on odd input.
     */
    fun recordTiming(previous: String?, session: String?, poll: String, delivery: String,
                     elapsedMs: Long, freezeMs: Long, pythonMs: Long, pythonCpuMs: Long, postMs: Long): JSONObject {
        val (state, relation) = state(previous, session)
        if (relation != Relation.CURRENT) {
            // Late timing of another (older or unattributable) session: never touches today's histogram/max.
            val target = if (relation == Relation.PRIOR) priorBucket(state, session!!)
                else state.optJSONObject("unattributed_timing") ?: JSONObject().also { state.put("unattributed_timing", it) }
            target.put("timings", target.optInt("timings") + 1)
            if (elapsedMs > target.optLong("max_elapsed_ms", -1)) target.put("max_elapsed_ms", elapsedMs)
            return state
        }
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
