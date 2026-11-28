package com.marketradar.app

/** 28 Nov R3: bounds how often a recovery pass may do *expensive* work (full-inventory
 * SHA-256, upload, or a cloud restore download) when local changes keep arriving or a
 * prerequisite keeps failing.
 *
 * This is a SCHEDULING limit only. It never changes what a FULL pass verifies: SHA-256 of
 * every file before upload, the unchanged after-upload re-hash, manifest publication,
 * owner/device/revision claim, commit and head readback are all as before. Size/mtime is
 * never used as cryptographic proof of a new checkpoint. A deferred pass:
 *  - has already run the normal claim (so the lease is renewed and remote ownership/revision
 *    is re-checked every 30 s, independently of hashing or restore),
 *  - never commits, prunes as a side-effect of a new checkpoint, restores or deletes, and
 *    leaves every local file (pending work) intact,
 *  - is reported as LOCAL_CHANGES_PENDING_UPLOAD or RESTORE_RETRY_BACKOFF, never as a
 *    verified checkpoint.
 *
 * Rules (first match wins):
 *  1. Immediate safety only: a *change* in remote revision or head manifest SHA (another
 *     device, a lost commit, head drift) => RUN. These are cheap to detect (already done by
 *     the claim) and must not be delayed. Ownership/lease checks are never skipped to
 *     obtain a budget pass; they run before [decide] is consulted.
 *  2. No recorded expensive attempt (first ever) or a wall clock that went backwards => RUN.
 *  3. The current prerequisite token differs from the one recorded at the last expensive
 *     attempt (a readiness/web/restore flag flipped, or the revision/manifest changed
 *     underneath a still-failing reason) => RUN immediately, once, then the cooldown applies
 *     again if the same token persists.
 *  4. Cooldown: the next expensive attempt may start only [gapMs] after the previous one
 *     ended, where gap = clamp(previousDuration * (1 - DUTY) / DUTY, MIN_GAP_MS, MAX_GAP_MS).
 *     DUTY is a *target*, not a guarantee. MAX_GAP_MS caps the backup delay at 15 minutes, so
 *     a long pass (for example a ~166 s two-hash pass) realises a duty of about 15.6 %, not
 *     10 %. The history is persisted, so a process restart does not restart the clock.
 *     Persistent failures (WEB_CAPTURE_FAILED, NOT_READY, NOT_MONITOR_READY, RESTORE_PENDING,
 *     NOT_INITIALIZED after a first attempt) use this same backoff; they do not bypass it.
 *  5. Background: while the process is cached (no visible activity, no foreground service),
 *     only a WorkManager recovery job ("worker" trigger) may start expensive work. Android
 *     kills cached processes for sustained CPU. WorkManager's 15-minute interval is nominal:
 *     the system may run earlier, later, or retry, so this is not a hard four-passes-per-hour
 *     ceiling. In-process 30 s ticks still obey the cooldown and, when cached, do not hash
 *     except on a worker trigger.
 */
object RecoveryBudget {
    const val RUN = "RUN"
    const val DUTY = 0.10
    const val MIN_GAP_MS = 5 * 60_000L
    const val MAX_GAP_MS = 15 * 60_000L

    data class History(val lastFullEndWallMs: Long, val lastFullDurationMs: Long, val lastPrereq: String = "")
    data class Input(val correctnessReason: String?, val history: History?, val nowWallMs: Long,
                     val background: Boolean, val trigger: String, val prereq: String = "")

    fun gapMs(lastFullDurationMs: Long): Long =
        (lastFullDurationMs.coerceAtLeast(0L) * (1.0 - DUTY) / DUTY).toLong().coerceIn(MIN_GAP_MS, MAX_GAP_MS)

    /** Cheap safety checks that must run a pass promptly. Not used for expensive retries. */
    fun isImmediateSafety(reason: String?): Boolean =
        reason == "REVISION_CHANGED" || reason == "MANIFEST_CHANGED"

    fun prereq(reason: String?, revision: Long, manifestSha: String): String =
        if (reason == null) "OK" else reason + "|" + revision + "|" + manifestSha

    fun decide(i: Input): String {
        if (isImmediateSafety(i.correctnessReason)) return RUN
        val h = i.history ?: return RUN
        if (h.lastFullEndWallMs <= 0L || i.nowWallMs < h.lastFullEndWallMs) return RUN
        // Only when we have a recorded token: an upgrade with empty lastPrereq stays on the cooldown.
        if (h.lastPrereq.isNotEmpty() && i.prereq.isNotEmpty() && i.prereq != h.lastPrereq) return RUN
        val wait = h.lastFullEndWallMs + gapMs(h.lastFullDurationMs) - i.nowWallMs
        if (wait > 0) return "DEFER_COOLDOWN_${(wait + 999) / 1000}s"
        if (i.background && i.trigger != "worker") return "DEFER_BACKGROUND_UNTIL_WORKER"
        return RUN
    }

    /** Classifies the post-claim local/remote prerequisite. A non-null value is an *expensive*
     * retry reason unless [isImmediateSafety] says otherwise. */
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
