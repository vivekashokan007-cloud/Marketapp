package com.marketradar.app

import android.content.Context
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * G6 versioned performance ledger. Prefer Supabase `ml_evaluation_metrics`
 * (identity upsert) over date-only `ml_performance`. Shadow variants are
 * log-only and must not change the active recommendation.
 */
object EvaluationMetricsLedger {
    private const val TAG = "EvalMetricsLedger"
    const val METRICS_CONTRACT_VERSION = "evaluation_metrics_ledger_v1_20260912"
    const val FEATURE_SCHEMA_VERSION = "ml_feature_schema_v2_1_1_n38"
    const val MIRROR_DIR = "evaluation_metrics_ledger"

    fun runStage(
        context: Context,
        brain: com.chaquo.python.PyObject,
        runId: String,
        sessionDate: String,
        rows: JSONArray,
        modelHash: String = "model_hash_unknown",
        policySelectorVersion: String = "pc2_paper_primary_v7",
        netTargetVersion: String = "net_target_v1_gross_minus_costs_once_20260912",
        cohortExecutionMode: String = "paper_intraday",
        activeRecommendationId: String? = null,
        menu: JSONArray? = null,
        shadowFlags: JSONObject? = null
    ): JSONObject {
        val payload = JSONObject()
            .put("run_id", runId)
            .put("session_date", sessionDate)
            .put("rows", rows)
            .put("model_hash", modelHash)
            .put("feature_schema_version", FEATURE_SCHEMA_VERSION)
            .put("policy_selector_version", policySelectorVersion)
            .put("net_target_version", netTargetVersion)
            .put("cohort_execution_mode", cohortExecutionMode)
        if (activeRecommendationId != null) payload.put("active_recommendation_id", activeRecommendationId)
        if (menu != null) payload.put("menu", menu)
        if (shadowFlags != null) payload.put("shadow_flags", shadowFlags)

        val result = try {
            JSONObject(brain.callAttr("g6_compute_metrics_json", payload.toString()).toString())
        } catch (e: Exception) {
            Log.w(TAG, "G6 brain wrapper miss, using evaluation_metrics_ledger: ${e.message}")
            val mod = com.chaquo.python.Python.getInstance().getModule("evaluation_metrics_ledger")
            JSONObject(mod.callAttr("g6_compute_metrics_json", payload.toString()).toString())
        }

        // Persist local mirror of supabase rows (identity keyed)
        val rowsOut = result.optJSONArray("supabase_rows") ?: JSONArray()
        persistLocalMirror(context, sessionDate, rowsOut)

        // Best-effort remote upsert by metrics_id identity (not date-only)
        for (i in 0 until rowsOut.length()) {
            val row = rowsOut.optJSONObject(i) ?: continue
            try {
                val ok = SupabaseClient.upsert(
                    "ml_evaluation_metrics",
                    row,
                    onConflict = "run_id,session_date,model_hash,feature_schema_version,policy_selector_version,net_target_version,cohort_execution_mode,variant"
                )
                if (!ok) {
                    Log.w(TAG, "G6_METRICS_UPSERT_SOFT_FAIL: id=${row.optString("metrics_id")}")
                }
            } catch (e: Exception) {
                Log.w(TAG, "G6_METRICS_UPSERT_ERR: ${e.message}")
            }
        }
        if (!result.has("ok")) result.put("ok", true)
        return result
    }

    fun persistLocalMirror(context: Context, sessionDate: String, rows: JSONArray) {
        try {
            val dir = File(context.filesDir, MIRROR_DIR)
            if (!dir.exists()) dir.mkdirs()
            val file = File(dir, "$sessionDate.json")
            file.writeText(
                JSONObject()
                    .put("session_date", sessionDate)
                    .put("contract", METRICS_CONTRACT_VERSION)
                    .put("rows", rows)
                    .toString()
            )
        } catch (e: Exception) {
            Log.w(TAG, "G6_METRICS_MIRROR_FAIL: ${e.message}")
        }
    }
}
