package com.marketradar.app

import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONArray
import org.json.JSONObject

/*
 * Owner decision 10 (26 Sep 2026): post-close ML timeouts — instrument first.
 *
 *  - Every post-close ML call (online_update, train_temporal) is timed and the
 *    sample is kept (bounded) with its session date; each call logs p50/p95 so
 *    five sessions of measurements can be read from logcat / LogBuffer.
 *  - train_temporal returns an explicit [TimedMlResult]: Completed(value?) vs
 *    TimedOut, so "Python returned None" is no longer indistinguishable from a
 *    timeout (both were `?: return` before).
 *  - NO limit is activated or changed here: the existing 30 s / 45 s bounds are
 *    passed through unchanged and reported as `limits_changed=false`. New limits
 *    are to be set later from the measurements.
 *
 * Semantics recorded for review (see PyTimeout / H1): withTimeoutOrNull never
 * fires around a blocking Chaquopy callAttr (the block never suspends). These
 * post-close bounds were therefore already inactive, and runTimedMl keeps them
 * exactly that way (no PyTimeout, no new effective limit) — "no limits
 * activated". Consequence: samples are the TRUE full durations of the Python
 * call, which is what is needed to choose limits later. TimedOut is still
 * modelled (it does fire if the block suspends) so callers handle it explicitly;
 * percentiles are over Completed samples and TimedOut is counted separately.
 */
internal const val POST_CLOSE_ML_TIMING_CONTRACT = "b3_decision10_post_close_ml_timing_v1"
internal const val POST_CLOSE_ML_TIMING_PREFS_KEY = "post_close_ml_timing_samples_v1"
internal const val POST_CLOSE_ML_TIMING_MAX_SAMPLES = 200
internal const val POST_CLOSE_ML_OUTCOME_COMPLETED = "COMPLETED"
internal const val POST_CLOSE_ML_OUTCOME_COMPLETED_NULL = "COMPLETED_NULL"
internal const val POST_CLOSE_ML_OUTCOME_TIMED_OUT = "TIMED_OUT"

internal sealed class TimedMlResult<out T> {
    abstract val elapsedMs: Long
    data class Completed<T>(val value: T?, override val elapsedMs: Long) : TimedMlResult<T>()
    data class TimedOut(val limitMs: Long, override val elapsedMs: Long) : TimedMlResult<Nothing>()
}

internal fun TimedMlResult<*>.outcomeLabel(): String = when (this) {
    is TimedMlResult.Completed -> if (value == null) POST_CLOSE_ML_OUTCOME_COMPLETED_NULL else POST_CLOSE_ML_OUTCOME_COMPLETED
    is TimedMlResult.TimedOut -> POST_CLOSE_ML_OUTCOME_TIMED_OUT
}

/** Runs [block] under the (unchanged) [limitMs]; never converts a null value into a timeout. */
internal suspend fun <T> runTimedMl(
    limitMs: Long,
    nanoClock: () -> Long = System::nanoTime,
    block: suspend () -> T?
): TimedMlResult<T> {
    val start = nanoClock()
    val done = withTimeoutOrNull(limitMs) { Box(block()) }
    val elapsed = (nanoClock() - start) / 1_000_000L
    return if (done != null) TimedMlResult.Completed(done.value, elapsed) else TimedMlResult.TimedOut(limitMs, elapsed)
}

private class Box<T>(val value: T?)

internal data class MlDurationSummary(
    val stage: String,
    val completedCount: Int,
    val timedOutCount: Int,
    val sessions: Int,
    val p50Ms: Long?,
    val p95Ms: Long?
)

/** Nearest-rank percentile over sorted values; null when empty. */
internal fun nearestRankPercentile(sorted: List<Long>, pct: Int): Long? {
    if (sorted.isEmpty()) return null
    val rank = Math.ceil(pct / 100.0 * sorted.size).toInt().coerceIn(1, sorted.size)
    return sorted[rank - 1]
}

/** Appends one sample to the stored JSON array, keeping the newest [maxSamples]. */
internal fun appendMlDurationSample(
    storedJson: String?, stage: String, sessionDate: String, elapsedMs: Long, outcome: String,
    limitMs: Long, maxSamples: Int = POST_CLOSE_ML_TIMING_MAX_SAMPLES
): String {
    val old = try { JSONArray(storedJson ?: "[]") } catch (_: Exception) { JSONArray() }
    val out = JSONArray()
    val start = maxOf(0, old.length() + 1 - maxSamples)
    for (i in start until old.length()) out.put(old.get(i))
    out.put(JSONObject().apply {
        put("stage", stage); put("session_date", sessionDate); put("elapsed_ms", elapsedMs)
        put("outcome", outcome); put("limit_ms", limitMs)
    })
    return out.toString()
}

internal fun summarizeMlDurations(storedJson: String?, stage: String): MlDurationSummary {
    val arr = try { JSONArray(storedJson ?: "[]") } catch (_: Exception) { JSONArray() }
    val completed = ArrayList<Long>()
    var timedOut = 0
    val sessions = HashSet<String>()
    for (i in 0 until arr.length()) {
        val o = arr.optJSONObject(i) ?: continue
        if (o.optString("stage") != stage) continue
        sessions.add(o.optString("session_date"))
        if (o.optString("outcome") == POST_CLOSE_ML_OUTCOME_TIMED_OUT) timedOut++
        else completed.add(o.optLong("elapsed_ms"))
    }
    completed.sort()
    return MlDurationSummary(stage, completed.size, timedOut, sessions.size,
        nearestRankPercentile(completed, 50), nearestRankPercentile(completed, 95))
}

internal fun mlDurationLogLine(result: TimedMlResult<*>, limitMs: Long, s: MlDurationSummary): String =
    "POST_CLOSE_ML_DURATION: stage=${s.stage} outcome=${result.outcomeLabel()} ms=${result.elapsedMs} " +
        "limit_ms=$limitMs limits_changed=false p50_ms=${s.p50Ms ?: "-"} p95_ms=${s.p95Ms ?: "-"} " +
        "completed=${s.completedCount} timed_out=${s.timedOutCount} sessions=${s.sessions} " +
        "contract=$POST_CLOSE_ML_TIMING_CONTRACT"
