package com.marketradar.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * 2.6.59 source contracts for M1 (receiver NOT_EXPORTED) and H1 (poll/bridge
 * Python calls go through PyTimeout). There's no Robolectric in this project,
 * so the receiver flag is pinned at source level.
 */
class H1M1SourceContractTest {

    private fun src(name: String): String = listOf(
        File("src/main/java/com/marketradar/app/$name"),
        File("app/src/main/java/com/marketradar/app/$name")
    ).first { it.exists() }.readText()

    @Test
    fun pollReceiver_registeredNotExported_onAllApiLevels() {
        val s = src("MainActivity.kt")
        assertTrue(s.contains("ContextCompat.registerReceiver(this, pollReceiver, filter, ContextCompat.RECEIVER_NOT_EXPORTED)"))
        assertFalse(s.contains("RECEIVER_EXPORTED)"))
        assertFalse(s.contains("registerReceiver(pollReceiver, filter)"))
    }

    @Test
    fun pollTickSender_isSamePackage() {
        assertTrue(src("MarketWatchService.kt").contains("Intent(\"com.marketradar.POLL_TICK\").setPackage(packageName)"))
    }

    @Test
    fun pollAndBridgePythonCalls_useEffectiveTimeout_withUnchangedLimits() {
        val mw = src("MarketWatchService.kt")
        val nb = src("NativeBridge.kt")
        assertFalse(mw.contains("withTimeoutOrNull"))
        assertFalse(nb.contains("withTimeoutOrNull"))
        assertTrue(mw.contains("PyTimeout.callWithTimeout(PY_POLL_TIMEOUT_KEY, 10_000L)"))
        assertTrue(mw.contains("PyTimeout.callWithTimeout(PY_POLL_TIMEOUT_KEY, PY_SNAPSHOT_TIMEOUT_MS)"))
        assertTrue(mw.contains("PyTimeout.callWithTimeout(PY_POLL_TIMEOUT_KEY, PY_AGENT_TIMEOUT_MS)"))
        assertTrue(mw.contains("PY_SNAPSHOT_TIMEOUT_MS = 4_000L"))
        assertTrue(mw.contains("PY_AGENT_TIMEOUT_MS = 3_000L"))
        assertTrue(nb.contains("PyTimeout.callWithTimeout(\"bridge.validate_model\", PY_VALIDATE_TIMEOUT_MS)"))
        assertTrue(nb.contains("PyTimeout.callWithTimeout(\"bridge.compute_live_friction\", PY_SCORE_TIMEOUT_MS)"))
        assertTrue(nb.contains("PyTimeout.callWithTimeout(\"bridge.ml_score\", PY_SCORE_TIMEOUT_MS)"))
        assertTrue(nb.contains("PY_VALIDATE_TIMEOUT_MS = 8_000L"))
        assertTrue(nb.contains("PY_SCORE_TIMEOUT_MS = 2_500L"))
    }

    @Test
    fun postCloseLearningSites_deliberatelyUnchanged() {
        // No evidence of typical durations, so the post-close learning / eval / C3
        // timeouts are left exactly as before (see STOP_H1_M1_SHIP_2.6.59).
        val ml = src("MarketMLService.kt")
        assertEquals(7, Regex("withTimeoutOrNull\\(").findAll(ml).count())
        assertFalse(ml.contains("PyTimeout"))
    }
}
