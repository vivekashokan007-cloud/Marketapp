package com.marketradar.app

/**
 * b515 (CDX-GROK-BACKGROUND-RESUME-1): pure, Android-free pieces of the background/resume contract so they are
 * JVM-testable. Holds no Activity, View or WebView.
 *
 * Contract: the PWA owns screen state (tab, scroll, allowlisted unsaved drafts) in DOM storage, which survives an
 * Activity/WebView re-creation inside the same process. Native only (1) asks the page to persist it at onPause,
 * before WebView.onPause()/pauseTimers() can defer page events, and (2) never pushes a sync into a document that has
 * not finished loading (the page's own boot already pulls native state). Neither path can submit, scan, switch
 * Paper/Real or replay anything: the flush script only calls the page's save function.
 */
object BackgroundResume {
    /** Evaluated at Activity.onPause. Returns the page's answer ('ok'), 'absent' on an older PWA, or 'error'. */
    const val FLUSH_SCRIPT =
        "(function(){try{return (typeof window.mrUiFlush==='function')?window.mrUiFlush('native_pause'):'absent';}catch(e){return 'error';}})()"

    enum class ResumeAction { RELOAD, SKIP_PAGE_NOT_READY, SKIP_RECENT_SYNC, SYNC }

    /**
     * What onResume may do to the WebView of THIS Activity instance.
     * - a failed or never-started main-frame load is reloaded (unchanged behaviour);
     * - a re-created WebView whose restored page is still loading is left alone: injecting/syncing into it is a
     *   duplicate of the page's own boot and races the restore;
     * - a sync within [minSyncGapMs] of the last one is skipped (unchanged behaviour).
     */
    fun resumeAction(lastLoadFailedMainFrame: Boolean, neverLoaded: Boolean, pageFinishedThisInstance: Boolean,
                     sinceLastSyncMs: Long, minSyncGapMs: Long = 5_000L): ResumeAction = when {
        lastLoadFailedMainFrame || neverLoaded -> ResumeAction.RELOAD
        !pageFinishedThisInstance -> ResumeAction.SKIP_PAGE_NOT_READY
        sinceLastSyncMs in 0 until minSyncGapMs -> ResumeAction.SKIP_RECENT_SYNC
        else -> ResumeAction.SYNC
    }

    /**
     * Java-heap snapshot for ACTIVITY_STOP / ACTIVITY_DESTROY. Android's ActivityThread GC watcher asks the system to
     * release stopped activities of this process when Java heap use exceeds 3/4 of the maximum
     * (ActivityTaskManager.releaseSomeActivities, reason "low-mem"). That is a HYPOTHESIS for the 26 Oct same-process
     * destructions; this line lets the next log confirm or exclude it. Numbers only.
     */
    fun heapSummary(usedBytes: Long, maxBytes: Long): String {
        val mb = 1024L * 1024L
        val pct = if (maxBytes > 0) (usedBytes * 100 / maxBytes) else -1
        val watcher = when { maxBytes <= 0 -> "unknown"; usedBytes * 4 > maxBytes * 3 -> "above_3_4"; else -> "below_3_4" }
        return "heapUsedMb=${usedBytes / mb} heapMaxMb=${maxBytes / mb} heapPct=$pct gcWatcherThreshold=$watcher"
    }
}
