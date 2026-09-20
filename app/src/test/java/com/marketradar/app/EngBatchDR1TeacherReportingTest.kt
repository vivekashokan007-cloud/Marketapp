package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class EngBatchDR1TeacherReportingTest {
    private fun row(
        session: String,
        role: String,
        success: Boolean,
        reason: String,
        pnl: Double,
        r: Double
    ) = JSONObject()
        .put("session_date", session)
        .put("lane", "NF_intraday")
        .put("role", role)
        .put("label_version", "teacher_v1")
        .put("price_integrity", "PASS")
        .put("is_success", success)
        .put("exit_reason", reason)
        .put("managed_pnl", pnl)
        .put("r_multiple", r)

    @Test
    fun mixedPrimarySecondaryUsesIdenticalChosenPopulation() {
        val all = listOf(
            row("2026-09-08", "primary", false, "EOD", 1200.0, 0.4),
            row("2026-09-09", "primary", true, "TP", -50.0, 1.0),
            row("2026-09-09", "secondary", true, "TP", 9999.0, 9.0)
        )
        val chosen = all.filter(TeacherReportingSummary::isChosenTeacherRow)
        val summary = TeacherReportingSummary.build(chosen)
        assertEquals(2, summary.getInt("rows"))
        assertEquals(2, summary.getInt("distinctSessionCount"))
        assertEquals(1, summary.getInt("teacherTargetHitCount"))
        assertEquals(50.0, summary.getDouble("teacherTargetHitRatePct"), 0.001)
        assertEquals(1, summary.getInt("netProfitableCount"))
        assertEquals(50.0, summary.getDouble("netProfitableRatePct"), 0.001)
        assertFalse(summary.getBoolean("sampleUncertain"))
    }

    @Test
    fun positiveEodIsProfitableButNotTargetHitAndLabelUnchanged() {
        val eod = row("2026-09-08", "primary", false, "EOD", 1200.0, 0.4)
        val summary = TeacherReportingSummary.build(listOf(eod))
        assertEquals(1, summary.getInt("netProfitableCount"))
        assertEquals(0, summary.getInt("teacherTargetHitCount"))
        assertEquals(1, summary.getInt("positiveEodCount"))
        assertFalse(eod.getBoolean("is_success"))
    }

    @Test
    fun oneSessionWithholdsProfitabilityVerdict() {
        val summary = TeacherReportingSummary.build(listOf(
            row("2026-09-08", "primary", true, "TP", 100.0, 1.0),
            row("2026-09-08", "primary", false, "EOD", 20.0, 0.1)
        ))
        assertTrue(summary.getBoolean("sampleUncertain"))
        assertTrue(summary.isNull("profitabilityVerdict"))
        assertFalse(summary.getBoolean("worthTrading"))
    }

    @Test
    fun thirtyPositiveRowsFromOneSessionStillCannotClaimWorthTrading() {
        val rows = (1..30).map {
            row("2026-09-08", "primary", true, "TP", 100.0, 1.0)
        }
        val summary = TeacherReportingSummary.build(rows)
        assertTrue(summary.getBoolean("sampleUncertain"))
        assertFalse(summary.getBoolean("worthTrading"))
        assertTrue(summary.isNull("profitabilityVerdict"))
    }
}
