package com.marketradar.app

import org.junit.Assert.*
import org.junit.Test

class EvaluationAlarmContinuationTest {
    @Test
    fun historicalContinuationPreservesRequestedSessionDate() {
        val target = MarketMLService.resolveEvaluationAlarmSessionDate(
            isContinuation = true,
            requestedSessionDate = "2026-09-18",
            todayIst = "2026-09-19"
        )
        assertEquals("2026-09-18", target)
    }

    @Test
    fun ordinaryAlarmIgnoresStaleSessionDateExtraAndUsesToday() {
        val target = MarketMLService.resolveEvaluationAlarmSessionDate(
            isContinuation = false,
            requestedSessionDate = "2026-09-18",
            todayIst = "2026-09-19"
        )
        assertEquals("2026-09-19", target)
    }

    @Test
    fun continuationWithoutSessionDateFallsBackToToday() {
        val target = MarketMLService.resolveEvaluationAlarmSessionDate(
            isContinuation = true,
            requestedSessionDate = "  ",
            todayIst = "2026-09-19"
        )
        assertEquals("2026-09-19", target)
    }

    @Test
    fun onlyTrueContinuationBypassesReminderWindow() {
        assertTrue(MarketMLService.shouldBypassEvaluationReminderWindow(true))
        assertFalse(MarketMLService.shouldBypassEvaluationReminderWindow(false))
    }
}
