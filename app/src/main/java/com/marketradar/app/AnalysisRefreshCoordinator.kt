package com.marketradar.app

/**
 * 2.6.76 / b507 (conditions 6, 7): in-flight revision guard and coalesced recalculation.
 * Pure state machine; MarketWatchService owns the thread and the side-effect gates.
 */
class AnalysisRefreshCoordinator {
    private var running = false
    private var pendingRevision: String? = null

    /** Request a recalculation for [revision]. Returns true when the caller should start one now. */
    @Synchronized fun request(revision: String): Boolean {
        pendingRevision = revision
        if (running) return false
        running = true
        return true
    }

    /** Take the revision to compute (latest requested); null when nothing is pending. */
    @Synchronized fun take(): String? { val r = pendingRevision; pendingRevision = null; return r }

    /** Called when one run finishes. Returns true if another run must follow (newer save arrived meanwhile). */
    @Synchronized fun finish(): Boolean {
        if (pendingRevision != null) return true
        running = false
        return false
    }

    @Synchronized fun isRunning() = running

    companion object {
        /** A result computed for [startRevision] may be published only if inputs are still at that revision. */
        fun mayPublish(startRevision: String, currentRevision: String) = startRevision == currentRevision
    }
}
