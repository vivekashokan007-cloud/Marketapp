package com.marketradar.app

import org.json.JSONObject
import java.io.File
import java.time.LocalDate
import kotlin.math.abs

/** The live breadth universe comes from the archived official membership and weight publication. */
internal object BnfBreadthUniverse {
    data class Stock(val symbol: String, val instrumentKey: String, val weight: Double)

    /** Monthly report links need not arrive newest first. Never replace a newer publication with an older one. */
    fun saveLatest(dir: File, weights: JSONObject): Boolean {
        require(weights.getString("schema") == "bnf_monthly_weights_v1" && weights.getString("status") == "VALIDATED")
        val published = LocalDate.parse(weights.getString("as_of_date"))
        val latest = File(dir, "weights_latest.json")
        if (latest.exists()) {
            val existing = JSONObject(latest.readText())
            require(existing.getString("schema") == "bnf_monthly_weights_v1" && existing.getString("status") == "VALIDATED")
            if (LocalDate.parse(existing.getString("as_of_date")) > published) return false
        }
        val tmp = File.createTempFile("weights_", ".tmp", dir)
        try { tmp.writeText(weights.toString()); require(tmp.renameTo(latest)) } finally { tmp.delete() }
        return true
    }

    fun load(root: File, day: String): List<Stock> {
        val dir = File(root, "bnf_reference_v1")
        val daily = JSONObject(File(dir, "$day.json").readText())
        val weights = JSONObject(File(dir, "weights_latest.json").readText())
        require(weights.getString("schema") == "bnf_monthly_weights_v1" && weights.getString("status") == "VALIDATED")
        require(LocalDate.parse(weights.getString("as_of_date")) <= LocalDate.parse(day))
        val members = daily.getJSONArray("members")
        val weighted = weights.getJSONArray("members")
        require(members.length() >= 10 && members.length() == weighted.length()) { "BNF_MEMBERSHIP_WEIGHT_MISMATCH" }
        val byIsin = mutableMapOf<String, Stock>()
        for (i in 0 until weighted.length()) {
            val row = weighted.getJSONObject(i)
            val isin = row.getString("isin")
            val key = row.getString("instrument_key")
            val weight = row.getDouble("weight_pct") / 100.0
            require(key == "NSE_EQ|$isin" && weight.isFinite() && weight > 0.0 && weight <= 1.0)
            require(byIsin.put(isin, Stock(row.getString("symbol"), key, weight)) == null)
        }
        val result = (0 until members.length()).map { i ->
            val member = members.getJSONObject(i)
            val stock = byIsin.remove(member.getString("isin")) ?: error("BNF_MEMBER_WEIGHT_MISSING")
            require(member.getString("symbol") == stock.symbol && member.getString("instrument_key") == stock.instrumentKey)
            stock
        }
        require(byIsin.isEmpty() && abs(result.sumOf { it.weight } - 1.0) <= 0.001) { "BNF_WEIGHT_SUM_MISMATCH" }
        return result
    }
}
