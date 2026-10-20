package com.marketradar.app

import org.json.JSONObject

/**
 * G4 (Codex sandbox corrections, 18 Oct 2026): configuration is not verified capability. Android-free.
 *
 * A broker token plus an HTTPS order-proxy URL means the live path is CONFIGURED. It does not show that a live
 * dispatcher, the proxy, IP registration or order reconciliation works. No verification path exists in this build,
 * so the live capability is always UNVERIFIED and never true.
 *
 * Compatible fields:
 * - `liveReady` keeps its name. It now means verified-capable, so it is always false.
 * - `liveConfigured` carries the old token+proxy meaning.
 * - Paper and sandbox fields are unchanged.
 */
object ExecutionInfraStatus {
    const val CAPABILITY_UNVERIFIED = "UNVERIFIED"
    const val CAPABILITY_NOT_CONFIGURED = "NOT_CONFIGURED"
    const val CAPABILITY_UNKNOWN = "UNKNOWN"
    const val LIVE_UNVERIFIED_REASON =
        "configured only: no live dispatcher/reconciliation verification path exists; capability is not inferred from token + proxy URL"

    fun build(
        instrumentKeyRows: Int,
        instrumentKeyPresentRows: Int,
        tokenReady: Boolean,
        sandboxTokenReady: Boolean,
        sandboxEnabled: Boolean,
        proxyUrlRaw: String?
    ): JSONObject {
        val proxyUrl = (proxyUrlRaw ?: "").trim()
        val proxyConfigured = proxyUrl.startsWith("https://")
        val liveConfigured = tokenReady && proxyConfigured
        // No live dispatcher/reconciliation verification exists in this build. Do not derive true from configuration,
        // an IP registration, a route or a test order.
        val liveVerified = false
        return JSONObject()
            .put("instrumentKeyRows", instrumentKeyRows)
            .put("instrumentKeyPresentRows", instrumentKeyPresentRows)
            .put("instrumentKeyFlowOk", instrumentKeyRows > 0 && instrumentKeyPresentRows > 0)
            .put("sandboxEnabled", sandboxEnabled)
            .put("proxyConfigured", proxyConfigured)
            .put("proxyUrl", proxyUrl)
            .put("tokenReady", tokenReady)
            .put("sandboxTokenReady", sandboxTokenReady)
            .put("paperReady", true)
            .put("sandboxReady", sandboxTokenReady && sandboxEnabled)
            // compatible field, truthful meaning: verified-capable (never true without a verification path)
            .put("liveReady", liveVerified)
            .put("liveConfigured", liveConfigured)
            .put("liveCapabilityVerified", liveVerified)
            .put("liveCapability", if (liveConfigured) CAPABILITY_UNVERIFIED else CAPABILITY_NOT_CONFIGURED)
            .put("liveCapabilityReason", if (liveConfigured) LIVE_UNVERIFIED_REASON else "token and/or HTTPS order proxy not configured")
    }

    /** Exception fallback: compatible keys, nothing claimed. */
    fun failure(message: String?): JSONObject = JSONObject()
        .put("instrumentKeyFlowOk", false)
        .put("sandboxEnabled", false)
        .put("proxyConfigured", false)
        .put("tokenReady", false)
        .put("paperReady", true)
        .put("sandboxReady", false)
        .put("liveReady", false)
        .put("liveConfigured", false)
        .put("liveCapabilityVerified", false)
        .put("liveCapability", CAPABILITY_UNKNOWN)
        .put("error", message ?: "unknown")
}
