package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * G4: token + HTTPS proxy URL = configured, not verified-capable. The golden binds these exact Android outputs to the
 * PWA render test (tools/sandbox_exec/test_g4_pwa_infra_render.mjs, run against the paired MarketVivi app.js).
 */
class ExecutionInfraStatusG4Test {
    private fun root(): File = listOf(File("."), File("..")).first { File(it, "tools/sandbox_exec").isDirectory }
    private val goldenFile get() = File(root(), "tools/sandbox_exec/g4_infra_status_golden.json")

    private fun cases(): Map<String, JSONObject> = linkedMapOf(
        "token_and_https_proxy_only" to ExecutionInfraStatus.build(4, 4, tokenReady = true, sandboxTokenReady = false,
            sandboxEnabled = false, proxyUrlRaw = " https://orders.example.invalid/relay "),
        "absent_configuration" to ExecutionInfraStatus.build(0, 0, tokenReady = false, sandboxTokenReady = false,
            sandboxEnabled = false, proxyUrlRaw = null),
        "token_with_http_proxy" to ExecutionInfraStatus.build(4, 2, tokenReady = true, sandboxTokenReady = false,
            sandboxEnabled = false, proxyUrlRaw = "http://orders.example.invalid"),
        "sandbox_configured" to ExecutionInfraStatus.build(4, 4, tokenReady = true, sandboxTokenReady = true,
            sandboxEnabled = true, proxyUrlRaw = ""),
        "bridge_exception" to ExecutionInfraStatus.failure("boom")
    )

    @Test fun tokenPlusHttpsProxy_isConfiguredNotVerifiedCapable() {
        val s = cases().getValue("token_and_https_proxy_only")
        assertTrue(s.getBoolean("tokenReady"))
        assertTrue(s.getBoolean("proxyConfigured"))
        assertTrue("configuration is reported", s.getBoolean("liveConfigured"))
        assertFalse("configuration is not capability", s.getBoolean("liveReady"))
        assertFalse(s.getBoolean("liveCapabilityVerified"))
        assertEquals("UNVERIFIED", s.getString("liveCapability"))
        assertEquals("https://orders.example.invalid/relay", s.getString("proxyUrl"))
        assertTrue("Paper availability unaffected", s.getBoolean("paperReady"))
    }

    @Test fun absentConfiguration_isNotConfigured_paperStillReady() {
        val s = cases().getValue("absent_configuration")
        assertFalse(s.getBoolean("liveConfigured")); assertFalse(s.getBoolean("liveReady"))
        assertEquals("NOT_CONFIGURED", s.getString("liveCapability"))
        assertFalse(s.getBoolean("proxyConfigured"))
        assertTrue(s.getBoolean("paperReady"))
        val http = cases().getValue("token_with_http_proxy")
        assertFalse("plain http is not a configured proxy", http.getBoolean("proxyConfigured"))
        assertEquals("NOT_CONFIGURED", http.getString("liveCapability"))
    }

    @Test fun compatibleFields_sandboxAndExceptionPathPreserved() {
        val compat = listOf("instrumentKeyRows", "instrumentKeyPresentRows", "instrumentKeyFlowOk", "sandboxEnabled",
            "proxyConfigured", "proxyUrl", "tokenReady", "sandboxTokenReady", "paperReady", "sandboxReady", "liveReady")
        cases().filterKeys { it != "bridge_exception" }.values.forEach { s -> compat.forEach { assertTrue(it, s.has(it)) } }
        val sb = cases().getValue("sandbox_configured")
        assertTrue("sandboxReady meaning unchanged", sb.getBoolean("sandboxReady"))
        val ex = cases().getValue("bridge_exception")
        assertFalse(ex.getBoolean("liveReady")); assertTrue(ex.getBoolean("paperReady")); assertEquals("boom", ex.getString("error"))
    }

    @Test fun executionModeDerivationUnchanged() {
        // Real/Paper/sandbox mode selection is not altered by G4.
        assertEquals("live", ExecutionModeResolver.derive("", false, "https://orders.example.invalid"))
        assertEquals("paper", ExecutionModeResolver.derive("", false, ""))
        assertEquals("sandbox", ExecutionModeResolver.derive("", true, "https://orders.example.invalid"))
    }

    @Test fun nativeBridgeDelegatesToTheTruthfulBuilder() {
        val src = listOf(File("src/main/java/com/marketradar/app/NativeBridge.kt"), File("app/src/main/java/com/marketradar/app/NativeBridge.kt"))
            .first { it.exists() }.readText()
        val fn = src.substring(src.indexOf("fun getExecutionInfraStatus()"), src.indexOf("fun getLogBuffer("))
        assertTrue(fn.contains("ExecutionInfraStatus.build("))
        assertTrue(fn.contains("ExecutionInfraStatus.failure("))
        assertFalse("no capability derived from token+proxy", Regex("liveReady\\\"?,\\s*tokenReady\\s*&&\\s*proxyConfigured").containsMatchIn(src))
    }

    @Test fun goldenMatchesAndroidOutput() {
        val actual = JSONObject().apply { cases().forEach { (k, v) -> put(k, v) } }
        if (System.getenv("G4_UPDATE_GOLDEN") == "1") goldenFile.writeText(actual.toString(2) + "\n")
        val golden = JSONObject(goldenFile.readText())
        assertEquals(golden.keySet(), actual.keySet())
        for (k in actual.keySet()) assertTrue("golden drift in $k", golden.getJSONObject(k).similar(actual.getJSONObject(k)))
    }
}
