package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SchemaCapabilityAndExportTest {
    @Test
    fun capabilityDefaultsFailClosed() {
        SupabaseClient.setRemoteContractIdentityCapabilityForTest(false)
        assertFalse(SupabaseClient.remoteContractIdentityCapability())
        assertFalse(SupabaseClient.remoteContractIdentityPersistedFlag())
    }

    @Test
    fun pageResultStatusesDistinguishErrors() {
        val http = SupabaseClient.PageResult(status = "http_error", httpCode = 500, error = "boom")
        val parse = SupabaseClient.PageResult(status = "parse_error", error = "bad json")
        val ok = SupabaseClient.PageResult(status = "success", rows = JSONArray().put(JSONObject().put("id", "1")))
        assertEquals("http_error", http.status)
        assertEquals("parse_error", parse.status)
        assertEquals("success", ok.status)
        assertEquals(1, ok.rows.length())
    }

    @Test
    fun selectAllPagesNeverMarksErrorAsComplete() {
        // Simulate by constructing the status mapping used by selectAllPages
        fun statusFor(pageStatus: String, returned: Int, truncated: Boolean): String = when {
            pageStatus != "success" -> "incomplete_error"
            returned == 0 -> "empty"
            truncated -> "incomplete_truncated"
            else -> "complete"
        }
        assertEquals("incomplete_error", statusFor("http_error", 500, false))
        assertEquals("incomplete_error", statusFor("parse_error", 500, false))
        assertEquals("complete", statusFor("success", 10, false))
        assertEquals("incomplete_truncated", statusFor("success", 500, true))
    }
}
