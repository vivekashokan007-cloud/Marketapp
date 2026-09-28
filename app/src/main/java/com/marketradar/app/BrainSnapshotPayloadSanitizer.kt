package com.marketradar.app

import org.json.JSONObject

/**
 * Normalizes only columns which the production ml_brain_snapshots schema
 * declares numeric. Chaquopy's Python-object string representation can expose
 * Python None as the token/string "None"; PostgREST must receive JSON null,
 * never that token, for numeric columns.
 */
object BrainSnapshotPayloadSanitizer {
    private val numericColumns = setOf(
        "confidence",
        "b1a_bnf_rv_to_iv_daily_ratio",
        "b1a_nf_rv_to_iv_daily_ratio"
    )

    fun sanitize(source: JSONObject): JSONObject {
        val payload = JSONObject(source.toString())
        for (column in numericColumns) {
            if (!payload.has(column) || payload.isNull(column)) continue
            val raw = payload.opt(column)
            val numeric = when (raw) {
                is Number -> raw.toDouble().takeIf { it.isFinite() }
                is String -> raw.trim().toDoubleOrNull()?.takeIf { it.isFinite() }
                else -> null
            }
            payload.put(column, numeric ?: JSONObject.NULL)
        }
        return payload
    }
}
