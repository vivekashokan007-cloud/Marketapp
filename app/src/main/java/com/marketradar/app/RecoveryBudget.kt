package com.marketradar.app

/** 27 Nov R2: bounds how often a recovery pass may re-hash the whole portable inventory when local
 * changes keep arriving (the quiet-pass gate cannot help then: every post-close pass had a change).
 *
 * This is a SCHEDULING limit only. It never changes what a FULL pass verifies: SHA-256 of every
 * file before upload, the unchanged after-upload re-hash, manifest publication, owner/device/
 * revision claim, commit and head readback are all as before. A deferred pass:
 *  - has already run the normal claim (so the lease is renewed and remote ownership/revision is
 *    re-checked every 30 s, independently of hashing),
 *  - never commits, prunes, restores or deletes, and leaves every local file (pending work) intact,
 *  - is reported as LOCAL_CHANGES_PENDING_UPLOAD, never as a verified checkpoint.
 *
 * Rules (first match wins):
 *  1. Any correctness condition (not ready, not monitor-ready, restore pending, web capture failed,
 *     revision or head-manifest drift, no local verified checkpoint) => RUN. These are never deferred.
 *  2. No recorded FULL pass (first ever) or a wall clock that went backwards => RUN.
 *  3. Cooldown: next FULL may start only [gapMs] after the previous FULL ended, where
 *     gap = clamp(previousDuration * (1 - DUTY) / DUTY, MIN_GAP_MS, MAX_GAP_MS). With DUTY = 10 %
 *     hashing is <= ~10 % of wall time once passes are long, and never more often than every 5 min.
 *     The history is persisted, so a process restart does not restart the clock.
 *  4. Background: while the process is cached (no visible activity, no foreground service), only the
 *     WorkManager recovery job ("worker" trigger, every 15 min) may run a FULL pass. Android kills
 *     cached processes for sustained CPU (the four post-close EXCESSIVE_RESOURCE_USAGE exits).
 */
object RecoveryBudget {
    const val RUN = "RUN"
    const val DUTY = 0.10
    const val MIN_GAP_MS = 5 * 60_000L
    const val MAX_GAP_MS = 15 * 60_000L

    data class History(val lastFullEndWallMs: Long, val lastFullDurationMs: Long)
    data class Input(val correctnessReason: String?, val history: History?, val nowWallMs: Long,
                     val background: Boolean, val trigger: String)

    fun gapMs(lastFullDurationMs: Long): Long =
        (lastFullDurationMs.coerceAtLeast(0L) * (1.0 - DUTY) / DUTY).toLong().coerceIn(MIN_GAP_MS, MAX_GAP_MS)

    fun decide(i: Input): String {
        if (i.correctnessReason != null) return RUN
        val h = i.history ?: return RUN
        if (h.lastFullEndWallMs <= 0L || i.nowWallMs < h.lastFullEndWallMs) return RUN
        val wait = h.lastFullEndWallMs + gapMs(h.lastFullDurationMs) - i.nowWallMs
        if (wait > 0) return "DEFER_COOLDOWN_${(wait + 999) / 1000}s"
        if (i.background && i.trigger != "worker") return "DEFER_BACKGROUND_UNTIL_WORKER"
        return RUN
    }

    /** First correctness condition that forbids deferral, or null. */
    fun correctnessReason(ready: Boolean, canMonitor: Boolean, restorePending: Boolean, webCaptureFailed: Boolean,
                          initialized: Boolean, revision: Long, localRevision: Long,
                          headManifestSha256: String, localManifestSha256: String): String? = when {
        restorePending -> "RESTORE_PENDING"
        !initialized -> "NOT_INITIALIZED"
        !ready -> "NOT_READY"
        !canMonitor -> "NOT_MONITOR_READY"
        webCaptureFailed -> "WEB_CAPTURE_FAILED"
        revision != localRevision -> "REVISION_CHANGED"
        !CloudVault.validHash(headManifestSha256) || headManifestSha256 != localManifestSha256 -> "MANIFEST_CHANGED"
        else -> null
    }
}
