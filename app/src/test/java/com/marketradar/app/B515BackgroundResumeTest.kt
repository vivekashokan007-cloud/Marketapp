package com.marketradar.app

import com.marketradar.app.BackgroundResume.ResumeAction
import org.junit.Assert.*
import org.junit.Test
import java.io.File

/**
 * b515 (CDX-GROK-BACKGROUND-RESUME-1): background destroy -> same-process re-creation. Pure helpers driven through the
 * recorded 26 Oct lifecycle sequence, plus source contracts on MainActivity. JVM only: no device, no emulator, no
 * Robolectric (not used by this repo). The page-side behaviour is exercised in MarketVivi (vm + headless Chrome).
 */
class B515BackgroundResumeTest {
    private fun src(n: String) = listOf(File("src/main/java/com/marketradar/app/$n"), File("app/src/main/java/com/marketradar/app/$n")).first { it.isFile }.readText()
    private fun body(a: String, sig: String): String { val i = a.indexOf(sig); assertTrue(sig, i >= 0); val j = a.indexOf("\n    override fun ", i + sig.length); return a.substring(i, if (j < 0) a.length else j) }

    @Test fun recordedSequenceClassifiesAndNeverSyncsIntoALoadingRestoredPage() {
        val proc = "691864d2-c4c0-4040-b44f-7d6370b5322c-" + System.nanoTime()
        // 09:05:43 FRESH_PROCESS_LAUNCH seq 1: page loading at first onResume -> no duplicate inject/sync
        val (_, k1) = LifecycleDiagnostics.onActivityCreate(false, proc)
        assertEquals(LifecycleDiagnostics.CreateKind.FRESH_PROCESS_LAUNCH, k1)
        assertEquals(ResumeAction.SKIP_PAGE_NOT_READY, BackgroundResume.resumeAction(false, false, false, Long.MAX_VALUE))
        // 09:16:37 stop, 09:25:31 destroyed in background; 09:50:49 re-created from saved state, SAME process
        val (_, k2) = LifecycleDiagnostics.onActivityCreate(true, proc)
        assertEquals(LifecycleDiagnostics.CreateKind.SAME_PROCESS_UI_RECREATION, k2)
        // new instance: lastNativeSyncElapsedMs == 0 (passed as MAX) and restoreState() page still loading
        assertEquals(ResumeAction.SKIP_PAGE_NOT_READY, BackgroundResume.resumeAction(false, false, false, Long.MAX_VALUE))
        // after onPageFinished a later return syncs once; a second resume within 5 s does not duplicate it
        assertEquals(ResumeAction.SYNC, BackgroundResume.resumeAction(false, false, true, Long.MAX_VALUE))
        assertEquals(ResumeAction.SKIP_RECENT_SYNC, BackgroundResume.resumeAction(false, false, true, 1_200))
        assertEquals(ResumeAction.SYNC, BackgroundResume.resumeAction(false, false, true, 5_000))
        // 10:05:54 destroyed again, 10:19:19 re-created again: same classification, same gate
        val (_, k3) = LifecycleDiagnostics.onActivityCreate(true, proc)
        assertEquals(LifecycleDiagnostics.CreateKind.SAME_PROCESS_UI_RECREATION, k3)
        assertEquals(ResumeAction.SKIP_PAGE_NOT_READY, BackgroundResume.resumeAction(false, false, false, Long.MAX_VALUE))
    }

    @Test fun failedOrMissingLoadStillReloadsFirst() {
        assertEquals(ResumeAction.RELOAD, BackgroundResume.resumeAction(true, false, true, 0))
        assertEquals(ResumeAction.RELOAD, BackgroundResume.resumeAction(false, true, false, Long.MAX_VALUE))
        assertEquals(ResumeAction.RELOAD, BackgroundResume.resumeAction(true, true, false, 10))
    }

    @Test fun heapSummaryFlagsTheGcWatcherThresholdWithNumbersOnly() {
        val mb = 1024L * 1024L
        assertEquals("heapUsedMb=200 heapMaxMb=256 heapPct=78 gcWatcherThreshold=above_3_4", BackgroundResume.heapSummary(200 * mb, 256 * mb))
        assertEquals("heapUsedMb=96 heapMaxMb=256 heapPct=37 gcWatcherThreshold=below_3_4", BackgroundResume.heapSummary(96 * mb, 256 * mb))
        assertEquals("heapUsedMb=192 heapMaxMb=256 heapPct=75 gcWatcherThreshold=below_3_4", BackgroundResume.heapSummary(192 * mb, 256 * mb))
        assertTrue(BackgroundResume.heapSummary(1, 0).endsWith("gcWatcherThreshold=unknown"))
    }

    @Test fun flushScriptOnlyCallsThePageSaveAndIsGuarded() {
        val s = BackgroundResume.FLUSH_SCRIPT
        assertTrue(s.contains("typeof window.mrUiFlush==='function'"))
        assertTrue(s.contains("window.mrUiFlush('native_pause')"))
        assertTrue(s.contains("'absent'") && s.contains("catch(e)"))
        listOf("NativeBridge", "AndroidBridge", "lock", "scan", "submit", "Trade", "Mode", "click", "dispatchEvent", "reload", "location")
            .forEach { assertFalse(it, s.contains(it, ignoreCase = it.first().isLowerCase())) }
    }

    @Test fun mainActivityWiringFlushAtPauseGateAtResumeOneReceiverHeapLines() {
        val a = src("MainActivity.kt")
        val pause = body(a, "override fun onPause()")
        assertTrue(pause.contains("webView.evaluateJavascript(BackgroundResume.FLUSH_SCRIPT)"))
        assertTrue(pause.contains("uiAlive && ::webView.isInitialized && !webViewRendererGone"))
        assertTrue(pause.indexOf("super.onPause()") < pause.indexOf("FLUSH_SCRIPT"))
        val stop = body(a, "override fun onStop()")
        assertFalse("flush must not wait for onStop", stop.contains("FLUSH_SCRIPT"))
        assertTrue(stop.contains("\${heapNow()}"))
        val resume = body(a, "override fun onResume()")
        assertTrue(resume.contains("BackgroundResume.resumeAction(lastLoadFailedMainFrame, neverLoaded, pageFinishedThisInstance"))
        assertTrue(resume.indexOf("if (action != BackgroundResume.ResumeAction.SYNC) return@postToLiveWebView") < resume.indexOf("injectNativeBridge()"))
        assertTrue(a.contains("pageFinishedThisInstance = false\n                    injectNativeBridge()"))   // onPageStarted
        assertTrue(a.contains("pageFinishedThisInstance = true\n                    launchNav.onPageFinished()"))   // onPageFinished
        assertEquals(1, Regex("ContextCompat\\.registerReceiver\\(this, pollReceiver").findAll(a).count())
        assertTrue(a.contains("if (!pollReceiverRegistered) {"))
        val destroy = body(a, "override fun onDestroy()")
        assertTrue(destroy.contains("if (pollReceiverRegistered) { runCatching { unregisterReceiver(pollReceiver) }; pollReceiverRegistered = false }"))
        assertTrue(destroy.contains("\${heapNow()}"))
        // restoring the screen is the PWA's job; native adds no navigation/action on re-creation
        assertTrue(a.contains("if (savedInstanceState == null) handleIntent(intent)"))
        assertEquals(1, Regex("\\brecreate\\(\\)").findAll(a).count())   // renderer-gone recovery only
    }
}
