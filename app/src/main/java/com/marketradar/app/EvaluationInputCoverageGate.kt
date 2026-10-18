package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject

/**
 * b511 E1: the evaluation-input coverage window of MarketMLService.runDayEvaluation, extracted so the
 * JVM suite can drive it (no Robolectric). The service calls [verify] right after input preparation and
 * before the lease, Python scoring, labels, C3 or any DONE; [decideZeroSnapshots] is the only route to
 * the zero-snapshot DONE.
 */
internal object EvaluationInputCoverageGate {
    data class Verified(val ids: Set<Long>, val remoteEmpty: Boolean)

    interface Host {
        fun fetchRemoteIdentities(): JSONArray
        fun persistReplay(snapshot: JSONObject): Boolean
        /** Resolve every prepared row against [reconciler] (rewrites the prepared file in place). */
        fun resolvePreparedInput(reconciler: EvaluationIdentity.SnapshotReconciler): Set<Long>
        fun invalidatePreparedMarker()
        fun log(level: Char, message: String)
    }

    /** Remote identity lookup failed or hit its page cap: coverage is unknown, never "verified empty". */
    class InputUnverifiedException(message: String, cause: Throwable?) : IllegalStateException(message, cause)

    /**
     * Prove that the prepared input covers every remote snapshot identity of [sessionDate], for an empty
     * input as well (b511 R1). Throws InputIncompleteException (remote has identities the input lacks) or
     * [InputUnverifiedException] (lookup failed/capped); both invalidate the prepared-input marker so the
     * retry refetches Supabase, and leave the inputs and the selected date for that retry.
     */
    fun verify(sessionDate: String, preparedCount: Int, host: Host): Verified {
        require(preparedCount >= 0) { "EVAL_INPUT_INVALID_COUNT: $preparedCount" }
        val reconciler = try {
            EvaluationIdentity.SnapshotReconciler(sessionDate, host::fetchRemoteIdentities, host::persistReplay)
        } catch (e: Exception) {
            host.invalidatePreparedMarker()
            host.log('E', "EVAL_INPUT_UNVERIFIED: date=$sessionDate input=$preparedCount lookup=${e.message}")
            throw InputUnverifiedException(
                "EVAL_INPUT_UNVERIFIED: remote identity lookup did not complete for $sessionDate (${e.message}); " +
                    "inputs retained, prepared input invalidated, retry the same date", e
            )
        }
        val ids = if (preparedCount > 0) host.resolvePreparedInput(reconciler) else emptySet()
        try {
            reconciler.requireCoversRemote(ids)
        } catch (e: EvaluationIdentity.InputIncompleteException) {
            host.invalidatePreparedMarker()
            host.log('E', "EVAL_INPUT_INCOMPLETE: date=$sessionDate input=${ids.size} missing=${e.missing.size} first=${e.missing.take(5)}")
            throw e
        }
        // Covered and the input is empty => the complete remote identity list is empty.
        return Verified(ids, remoteEmpty = ids.isEmpty())
    }

    enum class ZeroSnapshotDecision { BLOCKED_INCOMPLETE_SESSION, FAIL_POLLED_WITHOUT_SNAPSHOTS, DONE_EMPTY }

    fun decideZeroSnapshots(
        sessionDate: String,
        verified: Verified,
        coverageIntegrity: String,
        forceAnyway: Boolean,
        lastPollDate: String,
        pollCount: Int
    ): ZeroSnapshotDecision {
        check(verified.remoteEmpty && verified.ids.isEmpty()) {
            "EVAL_INPUT_UNVERIFIED: zero snapshots for $sessionDate without a verified-empty remote identity lookup " +
                "(${verified.ids.size} covered identities); not marking DONE"
        }
        if (coverageIntegrity == "INTEGRITY_BROKEN" && !forceAnyway) return ZeroSnapshotDecision.BLOCKED_INCOMPLETE_SESSION
        if (lastPollDate == sessionDate && pollCount > 0) return ZeroSnapshotDecision.FAIL_POLLED_WITHOUT_SNAPSHOTS
        return ZeroSnapshotDecision.DONE_EMPTY
    }
}
