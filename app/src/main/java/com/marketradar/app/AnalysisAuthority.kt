package com.marketradar.app

import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/**
 * 2.6.76 / b507 R1 (Codex F2, F3, F4): the single authority that serialises acknowledged input changes with
 * acceptance of analysis results, owns the one analysis lane shared by polls and refreshes, and keeps the newest
 * deferred recalculation until it is actually computed.
 *
 * - [commitInputs] and [publish] / [dispatchIfCurrent] take the same monitor: an input save can never interleave
 *   between "is this result still current?" and committing the result reference (or dispatching its notifications).
 * - [lane] is held for the whole analysis of a poll ([withPollLane]) or a refresh ([drain]); the two never overlap.
 * - A refresh request only records the newest revision ([requestRefresh]); it is consumed only by a worker that
 *   owns the lane, and the lane owner drains it on release, so a busy lane can never discard it.
 * - Publication is monotonic in market-data collection time within a session: an older market snapshot can never
 *   replace a result for a newer one, even with an identical input revision.
 */
class AnalysisAuthority {
    enum class Outcome {
        PUBLISHED, SUPERSEDED_INPUT, STALE_MARKET, PERSIST_FAILED, FAILED, TIMED_OUT, WAITING_FOR_POLL, MARKET_TOO_OLD,
        MARKET_IDENTITY_UNAVAILABLE
    }

    data class Identity(val session: String, val inputRevision: String, val marketCollectedAtMs: Long, val mode: String)

    private val lock = Any()
    val lane = Mutex()
    @Volatile private var revisionSource: () -> String = { "" }
    private var deferred: String? = null
    private var lastPublished: Identity? = null
    @Volatile var refresher: (suspend (String) -> Outcome)? = null
    @Volatile var lastRefreshOutcome: Outcome? = null; private set
    @Volatile var lastRefreshRevision: String? = null; private set
    @Volatile var refreshRuns = 0; private set

    fun attachRevisionSource(src: () -> String) { revisionSource = src }

    fun currentRevision(): String = synchronized(lock) { revisionSource() }
    fun pending(): String? = synchronized(lock) { deferred }
    fun lastPublished(): Identity? = synchronized(lock) { lastPublished }

    /** Run an input write (which must also invalidate the stored result) atomically w.r.t. publication. */
    fun <T> commitInputs(write: () -> T): T = synchronized(lock) { write() }

    private fun currentLocked(id: Identity): Outcome? {
        if (id.marketCollectedAtMs <= 0L) return Outcome.MARKET_IDENTITY_UNAVAILABLE
        if (id.inputRevision != revisionSource()) return Outcome.SUPERSEDED_INPUT
        val last = lastPublished
        if (last != null && last.session == id.session && id.marketCollectedAtMs < last.marketCollectedAtMs) return Outcome.STALE_MARKET
        return null
    }

    fun isCurrent(id: Identity): Boolean = synchronized(lock) { currentLocked(id) == null }

    /** Validate at the real publication boundary and commit the result reference under the same monitor. */
    fun publish(id: Identity, commit: () -> Boolean): Outcome = synchronized(lock) {
        currentLocked(id)?.let { return it }
        if (!commit()) return Outcome.PERSIST_FAILED
        lastPublished = id
        if (deferred == id.inputRevision) deferred = null
        Outcome.PUBLISHED
    }

    /** Dispatch (notifications etc.) only while the identity is current; the save cannot interleave. */
    fun dispatchIfCurrent(id: Identity, action: () -> Unit): Boolean = synchronized(lock) {
        if (currentLocked(id) != null) return false
        action(); true
    }

    /** Record that the newest acknowledged revision needs a result. Never consumed here. */
    fun requestRefresh(): String = synchronized(lock) { revisionSource().also { deferred = it } }

    /** A poll's whole analysis owns the lane; on release any deferred recalculation is drained. */
    suspend fun <T> withPollLane(block: suspend () -> T): T {
        try {
            return lane.withLock { block() }
        } finally {
            drain()
        }
    }

    /**
     * Compute the newest deferred revision if the lane is free. Returns the last outcome (null when nothing ran).
     * Stops (keeping the revision pending) on WAITING_FOR_POLL / MARKET_TOO_OLD / FAILED / TIMED_OUT /
     * PERSIST_FAILED so a failure is recorded honestly and recovered by the next request or poll release.
     */
    suspend fun drain(): Outcome? {
        var last: Outcome? = null
        while (true) {
            if (pending() == null) return last
            if (!lane.tryLock()) return last          // the lane owner drains on release
            var stop: Boolean
            try {
                val rev = pending() ?: return last
                val run = refresher ?: return last
                refreshRuns++
                last = try { run(rev) } catch (_: Throwable) { Outcome.FAILED }
                lastRefreshOutcome = last; lastRefreshRevision = rev
                synchronized(lock) {
                    if (last == Outcome.STALE_MARKET && lastPublished?.inputRevision == rev && deferred == rev) deferred = null
                }
                stop = last !in setOf(Outcome.PUBLISHED, Outcome.SUPERSEDED_INPUT, Outcome.STALE_MARKET)
            } finally {
                lane.unlock()
            }
            if (stop) return last
        }
    }

    companion object {
        /** Process-wide instance shared by NativeBridge saves and MarketWatchService analyses (same process). */
        val shared = AnalysisAuthority()
        /** A refresh may reuse a cached market snapshot only while it is young enough to stand for "now". */
        const val MAX_REFRESH_MARKET_AGE_MS = 6 * 60_000L

        /**
         * Whether a refresh may run on the cached market snapshot: null = run; WAITING_FOR_POLL when there is no
         * same-session snapshot in memory (e.g. after a service restart); MARKET_TOO_OLD when the snapshot is too
         * old to stand for current market data. Both keep the revision pending for the next poll.
         */
        fun refreshPrecondition(cachedSession: String?, collectedAtMs: Long, today: String, nowMs: Long): Outcome? = when {
            cachedSession == null || cachedSession != today -> Outcome.WAITING_FOR_POLL
            nowMs - collectedAtMs > MAX_REFRESH_MARKET_AGE_MS -> Outcome.MARKET_TOO_OLD
            else -> null
        }
    }
}
