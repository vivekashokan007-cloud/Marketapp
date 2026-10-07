package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject

/** One evidence menu for batched and streamed quote requests. No trading authority. */
object EvaluationEvidenceMenu {
    val PROVENANCE_KEYS = arrayOf(
        "final_rank", "evidence_source", "ranked_population_size", "ranked_evidence_retention_cap",
        "sampling_rule_version", "sampling_frame", "sampling_frame_size", "sampling_frame_digest",
        "sampling_sample_cap", "sampling_selected_count", "sampling_sample_rank",
        "sampling_inclusion_probability", "sampling_included", "sampling_hash_prefix"
    )
    data class LegKey(val index: String, val expiry: String, val strike: Double, val optionType: String)

    private fun obj(raw: Any?): JSONObject? = when (raw) {
        is JSONObject -> raw
        is String -> runCatching { JSONObject(raw) }.getOrNull()
        else -> null
    }
    private fun array(raw: Any?): JSONArray? = when (raw) {
        is JSONArray -> raw
        is String -> runCatching { JSONArray(raw) }.getOrNull()
        else -> null
    }
    private fun text(row: JSONObject, vararg names: String): String = names.asSequence()
        .map { row.opt(it)?.takeUnless { v -> v == JSONObject.NULL }?.toString()?.trim().orEmpty() }
        .firstOrNull { it.isNotEmpty() } ?: ""

    fun candidates(snapshot: JSONObject): List<JSONObject> {
        val result = mutableListOf<JSONObject>()
        obj(snapshot.opt("primary_candidate_json"))?.let(result::add)
        val context = obj(snapshot.opt("context_json")) ?: JSONObject()
        fun add(rows: JSONArray?) {
            if (rows != null) for (i in 0 until rows.length()) obj(rows.opt(i))?.let(result::add)
        }
        fun nonempty(raw: Any?) = array(raw)?.takeIf { it.length() > 0 }
        add(nonempty(context.opt("snapshot_ranked_candidates_full"))
            ?: nonempty(context.opt("snapshot_generated_candidates"))
            ?: array(snapshot.opt("top_candidates_json")))
        add(array(context.opt("snapshot_ranked_below_cap_sample")))
        add(nonempty(context.opt("snapshot_rejected_candidates_full"))
            ?: array(context.opt("snapshot_rejected_candidates")))
        add(array(obj(context.opt("snapshot_pc2_supply_quality_shadow"))?.opt("sample_candidates")))
        return result
    }

    fun legKeys(snapshot: JSONObject): Set<LegKey> {
        val result = linkedSetOf<LegKey>()
        for (candidate in candidates(snapshot)) {
            val index = ContractLotTable.normalizeIndexKey(text(candidate, "index", "index_key", "underlying"))
                ?: continue
            val expiry = text(candidate, "expiry", "expiry_date")
            if (expiry.isBlank()) continue
            fun add(strikeText: String, typeText: String, legExpiry: String = expiry) {
                val strike = strikeText.toDoubleOrNull() ?: return
                val type = when (typeText.uppercase()) { "CE", "CALL" -> "CE"; "PE", "PUT" -> "PE"; else -> return }
                if (strike.isFinite() && strike > 0 && legExpiry.isNotBlank()) result.add(LegKey(index, legExpiry, strike, type))
            }
            for ((camel, snake) in listOf("sell" to "sell", "buy" to "buy", "sell2" to "sell", "buy2" to "buy")) {
                val suffix = if (camel.endsWith("2")) "2" else ""
                val prefix = camel.removeSuffix("2")
                add(text(candidate, "${prefix}Strike$suffix", "${snake}_strike$suffix"),
                    text(candidate, "${prefix}Type$suffix", "${snake}_type$suffix"))
            }
            val legs = array(candidate.opt("legs"))
            if (legs != null) for (i in 0 until legs.length()) {
                val leg = obj(legs.opt(i)) ?: continue
                add(text(leg, "strike"), text(leg, "option_type", "optionType", "type"),
                    text(leg, "expiry", "expiry_date").ifBlank { expiry })
            }
        }
        return result
    }

    fun copyCoverageEvidence(source: JSONObject, target: JSONObject, compact: (Any?, Int) -> JSONArray) {
        array(source.opt("snapshot_ranked_below_cap_sample"))?.let {
            target.put("snapshot_ranked_below_cap_sample", compact(it, 50))
        }
        for (key in listOf("snapshot_ranked_below_cap_sampling", "snapshot_android_compaction",
            "snapshot_capture_completeness", "snapshot_capture_failure_reason", "snapshot_capture_field_status")) {
            source.opt(key)?.takeUnless { it == JSONObject.NULL }?.let { target.put(key, it) }
        }
    }
}
