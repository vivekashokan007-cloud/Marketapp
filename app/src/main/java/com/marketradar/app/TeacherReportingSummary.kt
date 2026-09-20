package com.marketradar.app

import org.json.JSONObject
import java.util.Locale

/** Authoritative chosen-teacher metrics consumed by SupabaseClient teacher_summary. */
object TeacherReportingSummary {
    private fun number(value: Any?): Double? = when (value) {
        is Number -> value.toDouble()
        is String -> value.toDoubleOrNull()
        else -> null
    }

    private fun bool(value: Any?): Boolean? = when (value) {
        is Boolean -> value
        is Number -> when (value.toInt()) { 1 -> true; 0 -> false; else -> null }
        is String -> when (value.trim().lowercase(Locale.US)) {
            "1", "true", "yes" -> true
            "0", "false", "no" -> false
            else -> null
        }
        else -> null
    }

    fun isChosenTeacherRow(row: JSONObject): Boolean {
        val role = row.optString("role", "").trim().lowercase(Locale.US)
        val labelVersion = row.optString("label_version", "").trim()
        val integrity = row.optString("price_integrity", "").trim().uppercase(Locale.US)
        return role == "primary" &&
            labelVersion == TeacherTruthConfig.LABEL_VERSION &&
            integrity != "FAIL" &&
            number(row.opt("r_multiple")) != null
    }

    fun build(items: List<JSONObject>): JSONObject {
        var rowCount = 0
        var targetHits = 0
        var sumR = 0.0
        var sumCaptured = 0.0
        var capturedCount = 0
        var netProfitable = 0
        var tpCount = 0
        var slCount = 0
        var eodCount = 0
        var positiveEod = 0
        var sumPnl = 0.0
        var pnlCount = 0
        val sessions = linkedSetOf<String>()
        val winRs = mutableListOf<Double>()
        val lossRs = mutableListOf<Double>()
        for (row in items) {
            val r = number(row.opt("r_multiple")) ?: continue
            rowCount += 1
            sumR += r
            if (r > 0) winRs += r
            if (r < 0) lossRs += kotlin.math.abs(r)
            if (bool(row.opt("is_success")) == true) targetHits += 1
            number(row.opt("captured_pct"))?.let {
                sumCaptured += it
                capturedCount += 1
            }
            val session = row.optString("session_date", "").trim().take(10)
            if (session.isNotEmpty()) sessions.add(session)
            val pnl = number(if (!row.isNull("managed_pnl")) row.opt("managed_pnl") else row.opt("net_pnl"))
            if (pnl != null) {
                sumPnl += pnl
                pnlCount += 1
                if (pnl > 0) netProfitable += 1
            }
            when (row.optString("exit_reason", "").trim().uppercase(Locale.US)) {
                "TP", "TARGET", "TARGET_HIT" -> tpCount += 1
                "SL", "STOP", "STOP_LOSS" -> slCount += 1
                "EOD", "TIME", "SESSION_END", "FORCE_EOD", "DAY_END" -> {
                    eodCount += 1
                    if (pnl != null && pnl > 0) positiveEod += 1
                }
            }
        }
        val expectancyR = if (rowCount > 0) sumR / rowCount else 0.0
        val hitRate = if (rowCount > 0) targetHits * 100.0 / rowCount else 0.0
        val avgWinR = if (winRs.isNotEmpty()) winRs.average() else 0.0
        val avgLossR = if (lossRs.isNotEmpty()) lossRs.average() else 0.0
        val breakEven = if (avgWinR > 0.0 && avgLossR > 0.0) avgLossR * 100.0 / (avgLossR + avgWinR) else 0.0
        val avgCaptured = if (capturedCount > 0) sumCaptured * 100.0 / capturedCount else 0.0
        val distinctSessions = sessions.size
        val uncertain = rowCount == 0 || distinctSessions <= 1
        val profitableRate = if (rowCount > 0) netProfitable * 100.0 / rowCount else null
        fun rounded(value: Double, digits: String) = String.format(Locale.US, digits, value).toDouble()
        return JSONObject()
            .put("rows", rowCount)
            .put("successes", targetHits) // legacy JSON key, explicitly TP-target hits
            .put("teacherTargetHitCount", targetHits)
            .put("teacherTargetHitRatePct", rounded(hitRate, "%.2f"))
            .put("successRatePct", rounded(hitRate, "%.2f"))
            .put("expectancyR", rounded(expectancyR, "%.4f"))
            .put("mean_r_multiple", if (rowCount > 0) rounded(expectancyR, "%.4f") else JSONObject.NULL)
            .put("avgCapturedPct", rounded(avgCaptured, "%.2f"))
            .put("breakEvenWinRatePct", rounded(breakEven, "%.2f"))
            .put("worthTrading", !uncertain && rowCount >= 30 && expectancyR > 0.0 && hitRate > breakEven)
            .put("distinctSessionCount", distinctSessions)
            .put("distinct_session_count", distinctSessions)
            .put("netProfitableCount", netProfitable)
            .put("net_profitable_count", netProfitable)
            .put("netProfitableRatePct", if (profitableRate == null) JSONObject.NULL else rounded(profitableRate, "%.2f"))
            .put("net_profitable_rate", if (profitableRate == null) JSONObject.NULL else rounded(profitableRate, "%.2f"))
            .put("meanNetPnl", if (pnlCount > 0) rounded(sumPnl / pnlCount, "%.4f") else JSONObject.NULL)
            .put("tpCount", tpCount)
            .put("slCount", slCount)
            .put("eodCount", eodCount)
            .put("positiveEodCount", positiveEod)
            .put("positiveEodRatePct", if (eodCount > 0) rounded(positiveEod * 100.0 / eodCount, "%.2f") else JSONObject.NULL)
            .put("sampleUncertain", uncertain)
            .put("sample_uncertain", uncertain)
            .put(
                "profitabilityVerdict",
                if (uncertain) JSONObject.NULL
                else if ((profitableRate ?: 0.0) > 50.0) "net_profitable_majority"
                else "no_net_profitable_majority"
            )
            .put("labelSemantics", "is_success remains TP target hit for teacher_v1")
    }
}
