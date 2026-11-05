package com.marketradar.app

import org.json.JSONArray

/**
 * The whole bootstrap poll-history restore decision, as one pure function over current state.
 *
 * The first version of this repair fixed the merge but left three state-level faults at the
 * caller, which string-level source assertions could not catch:
 *
 *  1. **Undated local rows were assumed to belong to the requested session.** The caller never
 *     consulted the header that actually owns the local history, so a legacy time-only row from
 *     a previous session could be re-interpreted as today's and win a conflict against a valid
 *     remote observation at the same clock time.
 *  2. **`poll_count` was preserved across a day boundary.** `reservePollNumberForToday()` reads
 *     `poll_count` only when `last_poll_date == today`. Writing today's date while preserving an
 *     undated prior count *promoted* a previous session's count into today's session:
 *     yesterday's `poll_count=78` plus a three-row restore yielded next poll number 79, not 4.
 *     The `maxOf` was not merely insufficient, it was the mechanism.
 *  3. **The empty/invalid-remote branch still used pre-network state.** Its reset decision read
 *     a `lastPollDate` captured before the fetch, so a poll saved while the request was in
 *     flight could be cleared by a header that was already stale.
 *
 * Everything here is date-scoped against the session that *owns* each piece of state, and the
 * active session is rechecked at apply time so a response requested before IST rollover cannot
 * overwrite a newer day's state. Cadence, caps, ER mathematics and slot semantics are untouched.
 */
object PollHistoryRestore {

    const val VERSION = "poll_history_restore_v1_session_scoped"

    /** The prefs state read under the poll-save lock, after the network work has finished. */
    data class LocalState(
        val historyJson: String?,
        val pollCount: Int,
        /** Session that owns [historyJson] and [pollCount] (`last_poll_date`). */
        val lastPollDate: String,
        /** Session that owns [assignedNumber]. */
        val assignedDate: String,
        val assignedNumber: Int,
        val latestPollPresent: Boolean,
        val lastPollTimePresent: Boolean
    )

    enum class Action {
        /** Usable remote observations existed; history and counters are rewritten together. */
        MERGE,

        /** Nothing usable was learned; local state is left exactly as it is. */
        NO_OP,

        /** Local state belongs to an earlier session and is cleared, as before this repair. */
        RESET,

        /** IST rolled over while the request was in flight; the response is discarded. */
        ABORT_STALE_SESSION
    }

    /**
     * What the caller should write. A null field means "do not write this key".
     * Applied as a single transaction so history, latest pointer, counters and the session
     * headers can never disagree with each other.
     */
    data class Plan(
        val action: Action,
        val historyJson: String? = null,
        val kept: Int = 0,
        val pollCount: Int? = null,
        val assignedDate: String? = null,
        val assignedNumber: Int? = null,
        val lastPollDate: String? = null,
        val coverageIntegrityDate: String? = null,
        val latestPollJson: String? = null,
        val lastPollTime: String? = null,
        val merge: PollHistoryMerge.Result? = null,
        val note: String = ""
    ) {
        val writesHistory: Boolean get() = historyJson != null
    }

    /**
     * @param local current prefs state, re-read under the lock after the fetch.
     * @param remote the fetched `poll_history_<requestedSession>` array (may be null/empty).
     * @param requestedSession the session the fetch asked for — the verified container context
     *   for remote time-only rows, since it is the server key the rows came back under.
     * @param activeSession the session that is current *now*, at apply time.
     */
    fun plan(
        local: LocalState,
        remote: JSONArray?,
        requestedSession: String,
        activeSession: String
    ): Plan {
        if (requestedSession.isBlank() || activeSession.isBlank() || requestedSession != activeSession) {
            // The fetch was for a session that is no longer current. Its rows would be stamped
            // into the wrong day and its counters compared against the wrong header.
            return Plan(
                action = Action.ABORT_STALE_SESSION,
                note = "requested=$requestedSession active=$activeSession"
            )
        }

        val localHistory = try {
            JSONArray(local.historyJson ?: "[]")
        } catch (_: Exception) { JSONArray() }

        val merged = PollHistoryMerge.merge(
            local = localHistory,
            remote = remote,
            sessionDate = requestedSession,
            localSessionDate = local.lastPollDate
        )

        if (merged.remoteUnusable) {
            // Nothing usable came back. Decide reset-vs-no-op from CURRENT state, not from a
            // header captured before the network work.
            val localOwnsToday = local.lastPollDate == requestedSession
            val hasStaleState = (local.historyJson ?: "[]") != "[]" ||
                local.pollCount != 0 ||
                local.latestPollPresent ||
                local.lastPollTimePresent
            return if (!localOwnsToday && hasStaleState) {
                Plan(
                    action = Action.RESET,
                    lastPollDate = requestedSession,
                    note = "stale session ${local.lastPollDate} cleared for $requestedSession"
                )
            } else {
                // Explicitly do NOT rewrite the history with a re-normalised copy of itself.
                Plan(action = Action.NO_OP, merge = merged, note = "no usable remote observation")
            }
        }

        // Counters: each prior value is honoured only by the session that owns it.
        val priorCount = if (local.lastPollDate == requestedSession) local.pollCount else 0
        val priorAssigned = if (local.assignedDate == requestedSession) local.assignedNumber else 0

        // Assigned identity must also clear any number the restored rows already used, so a
        // sparse restore cannot hand out a number a previous poll already holds. This is read
        // from validated row metadata only — never inferred from how many rows came back.
        var maxRestoredNumber = 0
        for (i in 0 until merged.history.length()) {
            PollHistoryMerge.assignedPollNumber(merged.history.optJSONObject(i))?.let {
                if (it > maxRestoredNumber) maxRestoredNumber = it
            }
        }

        val latestRow = PollHistoryMerge.latest(merged.history)

        return Plan(
            action = Action.MERGE,
            historyJson = merged.history.toString(),
            kept = merged.kept,
            // A count of retained observations, date-scoped. Deliberately NOT inflated to the
            // highest assigned number: a counter must not imply captured slots that are absent.
            pollCount = maxOf(priorCount, merged.kept),
            assignedDate = requestedSession,
            // Identity, not count: monotonic across the prior assignment, the numbers the
            // restored rows already used, and the retained row count.
            assignedNumber = maxOf(priorAssigned, maxRestoredNumber, merged.kept),
            lastPollDate = requestedSession,
            coverageIntegrityDate = requestedSession,
            latestPollJson = latestRow?.toString(),
            // Derived from the parsed identity: `optString("t")` blanks an ISO-only row.
            lastPollTime = PollHistoryMerge.latestClock(latestRow, requestedSession),
            merge = merged,
            note = "kept=${merged.kept} fromRemote=${merged.fromRemote} conflicts=${merged.conflicts} " +
                "unowned=${merged.unownedLocal} remoteWonUnusable=${merged.remoteWonUnusable} " +
                "maxRestoredNumber=$maxRestoredNumber"
        )
    }
}
