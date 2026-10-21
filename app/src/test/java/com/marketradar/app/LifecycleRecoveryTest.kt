package com.marketradar.app

import org.junit.Assert.*
import org.junit.Test
import java.io.File

/** b512 B: lifecycle/recreation diagnostics and guards (pure helpers + source contracts; no device). */
class LifecycleRecoveryTest {
    private fun src(n: String) = listOf(File("src/main/java/com/marketradar/app/$n"), File("app/src/main/java/com/marketradar/app/$n")).first { it.isFile }.readText()
    private fun manifest() = listOf(File("src/main/AndroidManifest.xml"), File("app/src/main/AndroidManifest.xml")).first { it.isFile }.readText()
    private val K = LifecycleDiagnostics.CreateKind::class.java

    @Test fun createKindsSeparateUiRecreationFromProcessAndRendererRecovery() {
        val p = "uuid-A"
        assertEquals(LifecycleDiagnostics.CreateKind.FRESH_PROCESS_LAUNCH, LifecycleDiagnostics.classifyCreate(false, p, null, false))
        // background -> system destroyed the Activity only -> recreated from saved state in the SAME process (21 Oct pattern)
        assertEquals(LifecycleDiagnostics.CreateKind.SAME_PROCESS_UI_RECREATION, LifecycleDiagnostics.classifyCreate(true, p, p, false))
        // true process death: saved state handed to a NEW process
        assertEquals(LifecycleDiagnostics.CreateKind.PROCESS_RESTORE, LifecycleDiagnostics.classifyCreate(true, p, null, false))
        assertEquals(LifecycleDiagnostics.CreateKind.PROCESS_RESTORE, LifecycleDiagnostics.classifyCreate(true, p, "uuid-old", false))
        // renderer loss wins over everything (Activity recreate() after onRenderProcessGone)
        assertEquals(LifecycleDiagnostics.CreateKind.RENDERER_RECOVERY, LifecycleDiagnostics.classifyCreate(true, p, p, true))
        assertEquals(LifecycleDiagnostics.CreateKind.SAME_PROCESS_RELAUNCH, LifecycleDiagnostics.classifyCreate(false, p, p, false))
    }

    @Test fun stickyHelperCountsInstancesAndConsumesRendererFlagOnce() {
        val u = "proc-" + System.nanoTime()
        val (s1, k1) = LifecycleDiagnostics.onActivityCreate(false, u)
        assertEquals(LifecycleDiagnostics.CreateKind.FRESH_PROCESS_LAUNCH, k1)
        LifecycleDiagnostics.markRendererRecovery()
        val (s2, k2) = LifecycleDiagnostics.onActivityCreate(true, u)
        assertEquals(LifecycleDiagnostics.CreateKind.RENDERER_RECOVERY, k2)
        val (s3, k3) = LifecycleDiagnostics.onActivityCreate(true, u)
        assertEquals(LifecycleDiagnostics.CreateKind.SAME_PROCESS_UI_RECREATION, k3)
        assertTrue(s1 < s2 && s2 < s3)
    }

    @Test fun exitInfoLinesAreNamedIstBoundedAndFlagNewOnly() {
        val exits = listOf(
            LifecycleDiagnostics.Exit(10, 0, 400, 1, 2, 1_792_538_134_000L, "remove task\nstack:secret"),  // 21 Oct 04:45:34 IST
            LifecycleDiagnostics.Exit(16, 0, 100, 0, 0, 1_792_508_000_000L, null),
            LifecycleDiagnostics.Exit(3, 0, 400, 0, 0, 1_000L, null),
            LifecycleDiagnostics.Exit(2, 9, 400, 0, 0, 999L, null), LifecycleDiagnostics.Exit(6, 0, 100, 0, 0, 998L, null),
            LifecycleDiagnostics.Exit(4, 0, 100, 0, 0, 997L, null))
        val lines = LifecycleDiagnostics.formatExits(exits, lastSeenMs = 1_792_508_000_000L)
        assertEquals(5, lines.size)
        assertTrue(lines[0].contains("name=USER_REQUESTED hint=user_swipe_or_force_stop new=true"))
        assertTrue(lines[0].contains("ist=2026-10-21 04:45:34"))
        assertFalse(lines[0].contains("\n"))
        assertTrue(lines[1].contains("name=PACKAGE_UPDATED") && lines[1].contains("new=false"))
        assertTrue(lines[2].contains("name=LOW_MEMORY hint=system_reclaimed"))
        assertEquals("REASON_99", LifecycleDiagnostics.reasonName(99))
        assertEquals(1_792_538_134_000L, LifecycleDiagnostics.newestTimestamp(exits, 5L))
    }

    @Test fun webViewCallbacksAreGuardedAndTornDown() {
        val a = src("MainActivity.kt")
        assertFalse("raw webView.post must not be used", a.contains("webView.post {"))
        assertTrue(a.contains("uiAlive && !isDestroyed && !webViewRendererGone && webView === target"))
        val destroy = a.substring(a.indexOf("override fun onDestroy()"))
        assertTrue(destroy.indexOf("uiAlive = false") < destroy.indexOf("unregisterReceiver(pollReceiver)"))
        listOf("client.dispatcher.cancelAll()", "removeJavascriptInterface(\"AndroidBridge\")", "webView.stopLoading()",
            "webView.destroy()", "alwaysFinish=", "kind=\$createKind", "pid=").forEach { assertTrue(it, destroy.contains(it)) }
        assertEquals(3, Regex("if \\(!uiUsable\\(\\)\\) return@runOnUiThread").findAll(a).count())
        assertTrue(a.contains("if (savedInstanceState == null) handleIntent(intent)"))
        assertTrue(a.contains("LifecycleDiagnostics.markRendererRecovery()"))
    }

    @Test fun noStaticUiReferencesAndLifecycleIsNotSuppressed() {
        val diag = src("LifecycleDiagnostics.kt")
        listOf("Activity", "WebView", "View ").forEach { t ->
            assertFalse(t, Regex("(var|val) \\w+\\s*:\\s*[\\w.]*$t").containsMatchIn(diag))
        }
        val a = src("MainActivity.kt")
        val companion = a.substring(a.indexOf("companion object {"), a.indexOf("}", a.indexOf("companion object {")))
        assertFalse(companion.contains("WebView") || companion.contains("MainActivity"))
        listOf("onCreate", "onResume", "onStop", "onDestroy", "onSaveInstanceState", "onPause").forEach {
            assertTrue(a.contains("super.$it("))
        }
        val m = manifest()
        assertTrue(m.contains("android:launchMode=\"singleTask\""))
        assertFalse(m.contains("android:configChanges=\"orientation|screenSize|keyboard|keyboardHidden|uiMode"))
    }

    @Test fun collectionDoesNotDependOnTheUiAndHasASingleWriter() {
        val svc = src("MarketWatchService.kt")
        assertFalse(svc.contains("MainActivity."))
        assertEquals(1, Regex("GeneratedCandidateOutbox\\.drainDetailed\\(").findAll(svc).count())
        assertFalse(src("NativeBridge.kt").contains("GeneratedCandidateOutbox.drain"))
        assertFalse(src("MainActivity.kt").contains("GeneratedCandidateOutbox"))
        assertTrue(src("MarketRadarApp.kt").contains("LifecycleDiagnostics.formatExits"))
    }
}
