package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.time.LocalDate
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlin.math.abs

object BnfWeightPublication {
    internal fun parse(text: String, members: JSONArray, today: String): JSONObject {
        val dateMatch = Regex("([A-Za-z]+ \\d{1,2}, \\d{4})").find(text) ?: error("WEIGHT_PUBLICATION_DATE_MISSING")
        val date = LocalDate.parse(dateMatch.value, DateTimeFormatter.ofPattern("MMMM d, yyyy", Locale.ENGLISH))
        require(date <= LocalDate.parse(today)) { "FUTURE_WEIGHT_PUBLICATION" }
        val rows = JSONArray()
        for (i in 0 until members.length()) {
            val member = members.getJSONObject(i)
            val symbol = member.getString("symbol")
            val line = Regex("(?m)^\\s*" + Regex.escape(symbol) + "\\s+([^\\r\\n]+)").find(text)
                ?: error("WEIGHT_MISSING_$symbol")
            val weight = Regex("(\\d+(?:\\.\\d+)?)\\s*$").find(line.value)?.groupValues?.get(1)?.toDouble()
                ?: error("WEIGHT_INVALID_$symbol")
            require(weight > 0 && weight <= 100)
            rows.put(JSONObject(member.toString()).put("weight_pct", weight))
        }
        val total = (0 until rows.length()).sumOf { rows.getJSONObject(it).getDouble("weight_pct") }
        require(abs(total - 100.0) <= 0.1) { "WEIGHT_SUM_MISMATCH" }
        return JSONObject().put("schema", "bnf_monthly_weights_v1").put("status", "VALIDATED")
            .put("as_of_date", date.toString()).put("total_weight_pct", total).put("members", rows)
    }
}
