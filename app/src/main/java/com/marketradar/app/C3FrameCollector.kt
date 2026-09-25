package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject

/**
 * C3 R2: turns a remote paging result (+ the pre-existing local-cache
 * fallback) into a finalization outcome. Pure JVM so the MarketMLService C3
 * decision is unit-testable.
 *
 * Local fallback semantics (unchanged from main / R1): main read remote via a
 * single request whose failure collapsed to an empty array, and an empty
 * remote result triggered EvaluationLocalCache.forEachBrainSnapshot. So remote
 * failure WAS already a local-fallback trigger and remains one. What changes:
 *  - FAILED remote frames are always discarded (never mixed with local frames,
 *    never finalized).
 *  - FAILED remote + no local frames => [C3CollectOutcome.RemoteFailed]
 *    (retryable FAILED), never SKIPPED_NO_FRAMES / ineligible, because absence
 *    of frames was not proven.
 *  - COMPLETE with zero frames or EMPTY + no local frames => existing
 *    evidence-based SKIPPED_NO_FRAMES handling.
 *
 * R3: the local fallback uses the strict reader
 * (EvaluationLocalCache.forEachBrainSnapshotStrict / [C3LocalSnapshotReader]).
 * Local compact frames are buffered and handed on only when the whole file
 * read is COMPLETE. A FAILED local read (malformed line / read error) discards
 * all local frames and yields [C3CollectOutcome.LocalFailed] => retryable
 * FAILED (C3_LOCAL_READ_INCOMPLETE). Local frames are never mixed with remote.
 *
 * R4: a COMPLETE local read is still only the *retained* cache. The local
 * fallback is used only if there is no trim marker ([C3TrimEvidence]), an
 * authoritative expected snapshot count exists ([C3ExpectedCountResolver]),
 * and the de-duplicated local row count and local frame count both equal it
 * exactly. Otherwise [C3CollectOutcome.LocalRejected] => retryable FAILED with
 * C3_LOCAL_CACHE_TRIMMED / C3_LOCAL_COUNT_UNVERIFIABLE / C3_LOCAL_COUNT_MISMATCH.
 */
sealed class C3CollectOutcome {
    abstract val remote: C3PagingResult

    data class Frames(
        val frames: JSONArray,
        val source: String,
        val snapshotCount: Int,
        val candidateSliceCount: Int,
        override val remote: C3PagingResult
    ) : C3CollectOutcome()

    data class NoFrames(
        val snapshotCount: Int,
        override val remote: C3PagingResult
    ) : C3CollectOutcome()

    data class RemoteFailed(
        override val remote: C3PagingResult.Failed,
        val localSnapshots: Int
    ) : C3CollectOutcome()

    /** R4: local cache trimmed / count unverifiable / count mismatch. */
    data class LocalRejected(
        override val remote: C3PagingResult,
        val reasonCode: String,
        val detail: String,
        val expected: Int?,
        val localRows: Int,
        val localFrames: Int
    ) : C3CollectOutcome()

    /** R3: strict local read FAILED while remote gave no usable frames. */
    data class LocalFailed(
        override val remote: C3PagingResult,
        val local: C3LocalReadResult.Failed
    ) : C3CollectOutcome()
}

/** Terminal state plan for a non-proceeding outcome; applied by MarketMLService. */
data class C3TerminalPlan(
    val c3Phase: String,
    val ledgerState: String,
    val reasonCode: String,
    val reason: String,
    val lastError: String,
    val runMetricsStage: Boolean
)

object C3FrameCollector {
    const val SOURCE_REMOTE = "remote"
    const val SOURCE_LOCAL = "local_fallback"
    const val REASON_REMOTE_READ_FAILED = "C3_REMOTE_READ_FAILED"
    const val REASON_LOCAL_READ_INCOMPLETE = "C3_LOCAL_READ_INCOMPLETE"
    const val REASON_LOCAL_CACHE_TRIMMED = "C3_LOCAL_CACHE_TRIMMED"
    const val REASON_LOCAL_COUNT_UNVERIFIABLE = "C3_LOCAL_COUNT_UNVERIFIABLE"
    const val REASON_LOCAL_COUNT_MISMATCH = "C3_LOCAL_COUNT_MISMATCH"

    private fun parseJsonObject(value: Any?): JSONObject? = when (value) {
        is JSONObject -> value
        is String -> {
            val trimmed = value.trim()
            if (trimmed.startsWith("{")) {
                try { JSONObject(trimmed) } catch (_: Exception) { null }
            } else null
        }
        else -> null
    }

    private fun shallowCopy(src: JSONObject): JSONObject {
        val out = JSONObject()
        val keys = src.keys()
        while (keys.hasNext()) {
            val key = keys.next()
            out.put(key, src.get(key))
        }
        return out
    }

    /** Same capture rules as R1 MarketMLService.captureFrame. Returns slices added or -1. */
    fun captureFrame(snapshot: JSONObject, sessionDate: String, into: JSONArray): Int {
        val context = parseJsonObject(snapshot.opt("context_json")) ?: return -1
        val captured = context.optJSONObject("c3_finalization_frame") ?: return -1
        val frame = shallowCopy(captured)
        frame.put("snapshot_id", snapshot.optString("id", frame.optString("snapshot_id", "")))
        frame.put("session_date", snapshot.optString("session_date", sessionDate))
        frame.put("poll_ts", snapshot.optString("poll_ts", frame.optString("poll_ts", "")))
        if (frame.optString("poll_ts").isBlank()) return -1
        into.put(frame)
        return frame.optJSONArray("candidate_slices")?.length() ?: 0
    }

    fun collect(
        sessionDate: String,
        remoteFetch: (onCompact: (JSONObject) -> Unit) -> C3PagingResult,
        localFetch: (onRow: (JSONObject) -> Unit) -> C3LocalReadResult,
        trimEvidence: () -> C3TrimEvidence,
        expectedCount: () -> C3ExpectedCount,
        onPhase: (phase: String, detail: String) -> Unit = { _, _ -> }
    ): C3CollectOutcome {
        var frames = JSONArray()
        var slices = 0
        val remote = remoteFetch { compact ->
            val added = captureFrame(compact, sessionDate, frames)
            if (added >= 0) slices += added
        }
        onPhase("after_remote_fetch", "${C3SnapshotPager.describe(remote)} frames=${frames.length()}")
        if (remote is C3PagingResult.Complete && frames.length() > 0) {
            return C3CollectOutcome.Frames(frames, SOURCE_REMOTE, remote.delivered, slices, remote)
        }
        if (remote is C3PagingResult.Failed) {
            // Fail closed: drop every partially read remote frame.
            frames = JSONArray()
            slices = 0
            onPhase("remote_fetch_failed_discarded", "discarded=${remote.discardedDelivered}")
        }
        fun remoteTag(): String = when (remote) {
            is C3PagingResult.Failed -> "remote=FAILED:${remote.reason}"
            is C3PagingResult.Empty -> "remote=EMPTY"
            is C3PagingResult.Complete -> "remote=COMPLETE_NO_FRAMES"
        }
        // R4: durable trim evidence rejects the fallback before any local read.
        val trim = trimEvidence()
        if (trim.rejectsFallback) {
            onPhase("local_cache_trimmed", "status=${trim.status} dropped=${trim.droppedRowsTotal}")
            return C3CollectOutcome.LocalRejected(
                remote, REASON_LOCAL_CACHE_TRIMMED,
                "marker=${trim.status} droppedRows=${trim.droppedRowsTotal} maxRowsBeforeTrim=${trim.maxRowsBeforeTrim} " +
                    "trimEvents=${trim.trimEvents} reasons=${trim.reasons.joinToString("|")} ${remoteTag()}",
                expected = null, localRows = 0, localFrames = 0
            )
        }
        onPhase("before_local_stream_fallback", "")
        // Buffer compact local frames; they are released only on COMPLETE.
        var localFrames = JSONArray()
        var localSlices = 0
        val local = localFetch { row ->
            val added = captureFrame(row, sessionDate, localFrames)
            if (added >= 0) localSlices += added
        }
        onPhase("after_local_stream_fallback", "${C3LocalSnapshotReader.describe(local)} frames=${localFrames.length()}")
        if (local is C3LocalReadResult.Failed) {
            localFrames = JSONArray()
            onPhase("local_read_failed_discarded", "rowsBefore=${local.rowsBeforeFailure}")
            return C3CollectOutcome.LocalFailed(remote, local)
        }
        val localRows = (local as? C3LocalReadResult.Complete)?.rows ?: 0
        if (local is C3LocalReadResult.Complete) {
            // R4: the retained cache must match the authoritative session count.
            val frameCount = localFrames.length()
            fun reject(code: String, expected: Int?, why: String): C3CollectOutcome {
                localFrames = JSONArray()
                onPhase("local_cache_rejected", "reason=$code expected=${expected ?: "-"} rows=$localRows frames=$frameCount")
                return C3CollectOutcome.LocalRejected(
                    remote, code, "$why expected=${expected ?: "unavailable"} localRows=$localRows localFrames=$frameCount ${remoteTag()}",
                    expected, localRows, frameCount
                )
            }
            when (val exp = expectedCount()) {
                is C3ExpectedCount.Unavailable ->
                    return reject(REASON_LOCAL_COUNT_UNVERIFIABLE, null, "expected_count_unavailable:${exp.why}")
                is C3ExpectedCount.Known -> {
                    if (localRows != exp.count) return reject(REASON_LOCAL_COUNT_MISMATCH, exp.count, "row_count_mismatch")
                    if (frameCount > 0 && frameCount != exp.count) {
                        return reject(REASON_LOCAL_COUNT_MISMATCH, exp.count, "frame_count_mismatch")
                    }
                    if (frameCount > 0) {
                        onPhase("local_cache_verified", "expected=${exp.count} source=${exp.source}")
                        return C3CollectOutcome.Frames(localFrames, SOURCE_LOCAL, localRows, localSlices, remote)
                    }
                }
            }
        }
        if (remote is C3PagingResult.Failed) {
            return C3CollectOutcome.RemoteFailed(remote, localRows)
        }
        val remoteCount = (remote as? C3PagingResult.Complete)?.delivered ?: 0
        return C3CollectOutcome.NoFrames(if (remoteCount > 0) remoteCount else localRows, remote)
    }

    /** Null for [C3CollectOutcome.Frames] (proceed to G5 assessment / build). */
    fun terminalPlan(outcome: C3CollectOutcome): C3TerminalPlan? = when (outcome) {
        is C3CollectOutcome.Frames -> null
        is C3CollectOutcome.NoFrames -> C3TerminalPlan(
            c3Phase = "SKIPPED_NO_FRAMES",
            ledgerState = "ineligible",
            reasonCode = EvaluationRunLedger.REASON_NO_FRAMES,
            reason = "No C3 recording frames were captured for this session.",
            lastError = "",
            runMetricsStage = true
        )
        is C3CollectOutcome.RemoteFailed -> {
            val r = outcome.remote
            val detail = "${r.reason} page=${r.pageIndex} mode=${r.mode}"
            C3TerminalPlan(
                c3Phase = "FAILED",
                ledgerState = "failed",
                reasonCode = REASON_REMOTE_READ_FAILED,
                reason = "C3 remote snapshot read incomplete ($detail); partial frames discarded, retry allowed.",
                lastError = "C3 remote read failed: $detail",
                runMetricsStage = false
            )
        }
        is C3CollectOutcome.LocalRejected -> C3TerminalPlan(
            c3Phase = "FAILED",
            ledgerState = "failed",
            reasonCode = outcome.reasonCode,
            reason = "C3 local cache fallback rejected (${outcome.detail}); retry allowed.",
            lastError = "C3 local fallback rejected: ${outcome.reasonCode} ${outcome.detail}",
            runMetricsStage = false
        )
        is C3CollectOutcome.LocalFailed -> {
            val l = outcome.local
            val remoteTag = when (val r = outcome.remote) {
                is C3PagingResult.Failed -> "remote=FAILED:${r.reason}"
                is C3PagingResult.Empty -> "remote=EMPTY"
                is C3PagingResult.Complete -> "remote=COMPLETE_NO_FRAMES"
            }
            val detail = "${l.reason} line=${l.lineNumber} rowsBefore=${l.rowsBeforeFailure} $remoteTag"
            C3TerminalPlan(
                c3Phase = "FAILED",
                ledgerState = "failed",
                reasonCode = REASON_LOCAL_READ_INCOMPLETE,
                reason = "C3 local snapshot cache read incomplete ($detail); local frames discarded, retry allowed.",
                lastError = "C3 local read incomplete: $detail",
                runMetricsStage = false
            )
        }
    }
}
