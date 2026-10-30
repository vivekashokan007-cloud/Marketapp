package com.marketradar.app

import org.json.JSONObject
import java.io.File

/**
 * Delivery A (draft): context keys for the per-index direction authority (index_direction_v1).
 * Local app-private directories only. No network, no Supabase, no credentials.
 */
object IndexAuthorityContext {
    const val STATE_DIR_NAME = "index_authority_v1"
    const val REPORT_DIR_NAME = "self_study_v1"

    fun stateDir(filesDir: File) = File(filesDir, STATE_DIR_NAME)
    fun reportDir(filesDir: File) = File(filesDir, REPORT_DIR_NAME)

    fun apply(ctx: JSONObject, filesDir: File, isRefresh: Boolean, nowMs: Long) {
        ctx.put("indexAuthorityStateDir", stateDir(filesDir).absolutePath)
        // Receipt time of a live poll (analysis-start clock); a refresh re-analysis carries none.
        if (isRefresh) ctx.remove("pollReceiptMs") else ctx.put("pollReceiptMs", nowMs)
    }
}
