package com.marketradar.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test
import java.io.File

class OrderExecutionSafetyTest {
    @Test
    fun orderLayerIsSandboxLocked() {
        assertEquals("SANDBOX", OrderExecutionService.EXECUTION_MODE)
    }

    @Test
    fun orderLayerSourceDoesNotContainLiveOrderHosts() {
        val source = File("app/src/main/java/com/marketradar/app/OrderExecutionService.kt").readText()
        val forbidden = listOf(
            "api-" + "hft.upstox.com",
            "api.upstox.com/" + "v2/order",
            "api.upstox.com/" + "v3/order"
        )
        forbidden.forEach { needle ->
            assertFalse("Forbidden live order host/path found in OrderExecutionService.kt: $needle", source.contains(needle))
        }
    }
}
