package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.abs

/**
 * G2 fetch-boundary helper for closed-trade *learning* eligibility.
 *
 * Display / accounting paths must continue to see dirty rows
 * ([SupabaseClient.getClosedTradesOrNull], [ClosedTradeLedger]).
 * Call [filterLearningEligible] only when building brain/calibration payloads.
 *
 * Mirrors the Python `calibration_input` contract at a practical subset:
 * CLOSED + stable id + finite net with cost provenance; excludes
 * UNTRUSTED_INCOMPLETE_STRUCTURE, PNL_BASIS_DIVERGENT, UNKNOWN.
 * Full validation remains in Python.
 */
internal object CalibrationInputGate {
    const val CONTRACT_VERSION = "calibration_input_v1_net_eligible_20260912"
    private const val NET_COST_TOLERANCE = 1.0
    private val EXCLUDED_ENGINES = setOf(
        "UNTRUSTED_INCOMPLETE_STRUCTURE",
        "PNL_BASIS_DIVERGENT",
        "UNKNOWN",
    )

    data class Verdict(
        val eligible: Boolean,
        val reason: String,
        val netPnl: Double? = null,
        val cohort: String? = null,
    )

    fun validate(row: JSONObject): Verdict {
        val id = row.optString("id", row.optString("trade_id", "")).trim()
        if (id.isEmpty()) return Verdict(false, "missing_stable_trade_id")
        val status = row.optString("status", "").trim().uppercase()
        if (status != "CLOSED") return Verdict(false, "status_not_closed")

        val engine = row.optString("pnl_engine", row.optString("pnlEngine", "")).trim().uppercase()
        if (engine in EXCLUDED_ENGINES) return Verdict(false, "excluded_engine_$engine")

        val cohort = cohortOf(row)
        val net = finiteOrNull(firstPresent(row, "net_pnl", "netPnl"))
        val gross = finiteOrNull(firstPresent(row, "actual_pnl", "actualPnl", "gross_pnl", "grossPnl"))
        val costPresent = hasKey(row, "friction_cost", "frictionCost", "total_costs", "totalCosts")
        val cost = finiteOrNull(firstPresent(row, "friction_cost", "frictionCost", "total_costs", "totalCosts"))

        if (!costPresent) {
            val prefix = if (engine == "RECONCILED") "reconciled_but_" else if (engine.isEmpty()) "null_engine_" else ""
            return Verdict(false, prefix + "missing_cost_provenance", cohort = cohort)
        }
        if (cost == null) return Verdict(false, "nonfinite_or_invalid_cost", cohort = cohort)

        val resolvedNet = when {
            net != null && gross != null -> {
                if (abs(net - (gross - cost)) > NET_COST_TOLERANCE) {
                    return Verdict(false, "inconsistent_net_vs_gross_minus_cost", cohort = cohort)
                }
                net
            }
            net != null -> net
            gross != null -> gross - cost
            else -> return Verdict(false, "missing_net_and_gross", cohort = cohort)
        }

        if (engine == "RECONCILED" && (net == null && gross == null)) {
            return Verdict(false, "reconciled_but_missing_net", cohort = cohort)
        }
        return Verdict(true, "admitted", netPnl = resolvedNet, cohort = cohort)
    }

    /** Learning-only filter. Does not mutate the display ledger. */
    fun filterLearningEligible(closedJson: String, cohort: String = "paper"): String {
        val raw = try {
            JSONArray(closedJson)
        } catch (_: Exception) {
            return "[]"
        }
        val out = JSONArray()
        val seen = mutableSetOf<String>()
        for (i in 0 until raw.length()) {
            val row = raw.optJSONObject(i) ?: continue
            val verdict = validate(row)
            if (!verdict.eligible) continue
            if (cohort.isNotEmpty() && verdict.cohort != cohort) continue
            val id = row.optString("id", row.optString("trade_id", "")).trim()
            if (id.isEmpty() || !seen.add(id)) continue
            val copy = JSONObject(row.toString())
            copy.put("learning_pnl", verdict.netPnl)
            copy.put("calibration_cohort", verdict.cohort)
            copy.put("calibration_contract_version", CONTRACT_VERSION)
            out.put(copy)
        }
        return out.toString()
    }

    private fun cohortOf(row: JSONObject): String {
        if (row.has("paper") && !row.isNull("paper")) {
            return if (row.optBoolean("paper", true)) "paper" else "live"
        }
        val mode = (
            row.optString("execution_mode", "") + " " + row.optString("trade_mode", "")
            ).trim().lowercase()
        if (mode.contains("live") || mode.contains("real")) return "live"
        return "paper"
    }

    private fun hasKey(row: JSONObject, vararg keys: String): Boolean =
        keys.any { row.has(it) && !row.isNull(it) }

    private fun firstPresent(row: JSONObject, vararg keys: String): Any? {
        for (key in keys) {
            if (row.has(key) && !row.isNull(key)) return row.get(key)
        }
        return null
    }

    private fun finiteOrNull(value: Any?): Double? {
        val number = when (value) {
            null -> return null
            is Number -> value.toDouble()
            else -> value.toString().toDoubleOrNull() ?: return null
        }
        return if (number.isFinite()) number else null
    }
}
