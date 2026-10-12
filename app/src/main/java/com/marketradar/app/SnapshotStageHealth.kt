package com.marketradar.app

import org.json.JSONObject

/** Bounded session counters; failures are visible in the next retained snapshot. */
object SnapshotStageHealth {
    private val events = setOf("attempt", "built", "timeout", "inflight_skip", "reservation_skip",
        "exception", "queued", "enqueue_failed", "upload_ack", "upload_unacknowledged")

    fun record(previous: String?, session: String, event: String, poll: String,
               elapsedMs: Long = 0, exceptionClass: String? = null): JSONObject {
        require(event in events)
        val old = runCatching { JSONObject(previous ?: "{}") }.getOrDefault(JSONObject())
        val state = if (old.optString("session_date") == session) old else JSONObject()
            .put("schema_version", "snapshot_stage_health_v1").put("session_date", session)
        val counts = state.optJSONObject("counts") ?: JSONObject().also { state.put("counts", it) }
        counts.put(event, counts.optInt(event) + 1)
        state.put("last_event", event).put("last_poll_key", poll.take(100))
            .put("last_elapsed_ms", elapsedMs.coerceAtLeast(0))
        if (exceptionClass != null) state.put("last_exception_class", exceptionClass.take(100))
        return state
    }
}
