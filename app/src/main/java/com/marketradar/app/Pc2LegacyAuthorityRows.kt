package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject

/**
 * b503: retained legacy PC2 authority rows (ml_pc2_authority_decisions), built exactly as
 * SupabaseClient.savePc2AuthorityDecisions always built them (moved verbatim), so the rows
 * can be frozen into a restart-safe outbox. Identity/upsert key unchanged:
 * (poll_ts, policy_version, authority_diagnostics_version, decision_index), id
 * "$pollTs|$policyVersion|$diagnosticsVersion|$index". The tail is NOT widened.
 */
object Pc2LegacyAuthorityRows {
    /** rows == null with ok=true: nothing expected; ok=false: identity missing (pre-b503 returned false). */
    class Built(val rows: JSONArray?, val ok: Boolean, val pollTs: String)

    fun build(snapshot: JSONObject): Built {
        val pollTsOuter = snapshot.optString("poll_ts", "").trim()
        val rowsOrSignal = buildRows(snapshot)
        return when {
            rowsOrSignal == null -> Built(null, true, pollTsOuter)
            rowsOrSignal === MISSING -> Built(null, false, pollTsOuter)
            else -> Built(rowsOrSignal.takeIf { it.length() > 0 }, true, pollTsOuter)
        }
    }

    private val MISSING = JSONArray()

    private fun buildRows(snapshot: JSONObject): JSONArray? {
        // b503 R3: absence is NOT_EXPECTED only when the source genuinely has no cohort. A present but
        // unparseable context or decisions value is an explicit build failure (MISSING), never "nothing expected".
        if (!snapshot.has("context_json") || snapshot.isNull("context_json")) return null
        val context = snapshot.optJSONObject("context_json") ?: try {
            val raw = snapshot.optString("context_json", "")
            if (raw.isBlank()) return null
            JSONObject(raw)
        } catch (_: Exception) {
            return MISSING
        }
        if (!context.has("snapshot_pc2_authority_decisions") || context.isNull("snapshot_pc2_authority_decisions")) return null
        val decisions = context.optJSONArray("snapshot_pc2_authority_decisions")
            ?: (context.opt("snapshot_pc2_authority_decisions") as? String)?.let { runCatching { JSONArray(it) }.getOrNull() }
            ?: return MISSING
        if (decisions.length() == 0) return null

        val policy = context.optJSONObject("snapshot_pc2_authority_policy") ?: JSONObject()
        val sessionDate = snapshot.optString("session_date", "").trim()
        val pollTs = snapshot.optString("poll_ts", "").trim()
        val brainVersion = context.optString("snapshot_brain_version", "unknown").trim()
        val policyVersion = policy.optString("version", "pc2_authority_policy_v1").trim()
        val policyDiagnosticsVersion = policy.optString(
            "authority_diagnostics_version",
            "pc2_authority_diagnostics_v1"
        ).trim()
        if (sessionDate.isEmpty() || pollTs.isEmpty()) return MISSING

        val rows = JSONArray()
        for (index in 0 until decisions.length()) {
            val decision = decisions.optJSONObject(index) ?: continue
            val variableName = decision.optString("variable_name", "").trim()
            if (variableName.isEmpty()) continue
            val diagnosticsVersion = decision.optString(
                "authority_diagnostics_version",
                policyDiagnosticsVersion
            ).trim().ifEmpty { "pc2_authority_diagnostics_v1" }
            rows.put(
                JSONObject()
                    .put("id", "$pollTs|$policyVersion|$diagnosticsVersion|$index")
                    .put("session_date", sessionDate)
                    .put("poll_ts", pollTs)
                    .put("decision_index", index)
                    .put("variable_name", variableName)
                    .put("constant", decision.opt("constant"))
                    .put("context_variable", decision.opt("context_variable"))
                    .put("slice_key", decision.opt("slice_key"))
                    .put("authority_kind", decision.opt("authority_kind"))
                    .put("authority_state", decision.optString("authority_state", "SHADOW"))
                    .put("authority_state_reason", decision.optString("authority_state_reason", "authority_contract_not_ready"))
                    .put("authority_diagnostics_version", diagnosticsVersion)
                    .put("provenance_policy", decision.optString("provenance_policy", "unknown"))
                    .put("support_count", decision.optInt("support_count", 0))
                    .put("stability_ratio", decision.opt("stability_ratio"))
                    .put("stability_bar", decision.opt("stability_bar"))
                    .put("stability_pass", decision.opt("stability_pass"))
                    .put("diversity_pass", decision.opt("diversity_pass"))
                    .put("diversity_status", decision.opt("diversity_status"))
                    .put("censor_guard_status", decision.opt("censor_guard_status"))
                    .put("censor_guard_flags", decision.opt("censor_guard_flags"))
                    .put("population_provenance_verified", decision.opt("population_provenance_verified"))
                    .put("population_provenance_scope", decision.opt("population_provenance_scope"))
                    .put("population_provenance_version", decision.opt("population_provenance_version"))
                    .put("neutrality_tick", decision.opt("neutrality_tick"))
                    .put("neutrality_delta", decision.opt("neutrality_delta"))
                    .put("neutrality_pass", decision.opt("neutrality_pass"))
                    .put("hard_threshold", decision.opt("hard_threshold_value"))
                    .put("percentile_threshold", decision.opt("percentile_threshold_value"))
                    .put("observed_value", decision.opt("observed_value"))
                    .put("hard_passed", decision.opt("hard_passed"))
                    .put("percentile_passed", decision.opt("percentile_passed"))
                    .put("counterfactual_behavior_differs", decision.opt("counterfactual_behavior_differs"))
                    .put("promotion_id", decision.opt("promotion_id"))
                    .put("promotion_valid_until", decision.opt("promotion_valid_until"))
                    .put("fallback_reason", decision.opt("fallback_reason"))
                    .put("brain_version", brainVersion)
                    .put("policy_version", policyVersion)
                    .put("policy_json", policy)
                    .put("decision_json", decision)
            )
        }
        return rows
    }
}
