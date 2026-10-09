package com.marketradar.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/** R3: Paper-only capture. Resolver semantics + service guard placement (source contract). */
class EvidenceBatch1R3PaperOnlyTest {
    private fun source(name: String): String = listOf(
        File("src/main/java/com/marketradar/app/$name"),
        File("app/src/main/java/com/marketradar/app/$name")
    ).first { it.isFile }.readText()

    @Test
    fun resolverKeepsTheServiceDerivationUnchanged() {
        assertEquals("paper", ExecutionModeResolver.derive("", false, ""))
        assertEquals("paper", ExecutionModeResolver.derive(null, false, null))
        assertEquals("sandbox", ExecutionModeResolver.derive("", true, "https://proxy"))
        assertEquals("live", ExecutionModeResolver.derive("", false, "https://proxy"))
        assertEquals("paper", ExecutionModeResolver.derive("", false, "http://proxy"))
        assertEquals("live", ExecutionModeResolver.derive(" LIVE ", true, ""))
        assertEquals("paper", ExecutionModeResolver.derive("paper", true, "https://proxy"))
        assertEquals("sandbox", ExecutionModeResolver.derive("bogus", true, ""))
        assertTrue(ExecutionModeResolver.isPaper("paper"))
        assertFalse(ExecutionModeResolver.isPaper("live"))
        assertFalse(ExecutionModeResolver.isPaper("sandbox"))
        assertFalse(ExecutionModeResolver.isPaper(""))
        assertFalse(ExecutionModeResolver.isPaper(null))
    }

    @Test
    fun serviceDerivesModeThroughTheResolver() {
        val svc = source("MarketWatchService.kt")
        assertTrue(svc.contains("val derivedExecutionMode = ExecutionModeResolver.derive("))
        assertTrue(svc.contains("ctxObj.put(\"executionMode\", derivedExecutionMode)"))
        // ctx mode is set before the capture request is prepared.
        assertTrue(svc.indexOf("ctxObj.put(\"executionMode\", derivedExecutionMode)") <
            svc.indexOf("preparePaperMenuCaptureRequest(poll.optString(\"slot_key\""))
    }
}
