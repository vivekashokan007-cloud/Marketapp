package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Codex follow-up review of PR #20 at 6e7abb3 (20 Oct 2026). payload_error > 0 together with any observed broker ID is a
 * batch contradiction: Upstox processes a batch only if it has no payload_error. Written from the review text, because
 * Codex's own SummaryContradictionTest.kt was not attached. Fakes only.
 */
class SummaryContradictionTest {
    @Before fun seams() { OrderExecutionService.persistRow = { true } }
    @After fun reset() = OrderExecutionService.resetSeamsForTest()

    /** Codex's exact HTTP 200 body. */
    private val codexBody = """{
  "status": "success",
  "data": [
    {"correlation_id": "1", "order_id": "A"},
    {"correlation_id": "2", "order_id": "B"}
  ],
  "summary": {"total": 2, "payload_error": 1, "success": 2, "error": 0}
}"""

    private fun req() = JSONArray().put(JSONObject().put("correlation_id", "1")).put(JSONObject().put("correlation_id", "2"))
    private fun strs(a: JSONArray) = (0 until a.length()).map { a.getString(it) }

    @Test fun parser_payloadErrorWithObservedIdsIsBatchContradiction() {
        val p = OrderExecutionService.parseMultiPlacement(200, JSONObject(codexBody), req())
        assertEquals(p.toString(), "UNKNOWN", p.getString("placement_status"))
        assertTrue(strs(p.getJSONArray("anomalies")).contains("PAYLOAD_ERROR_WITH_ORDER_IDS"))
        assertEquals(listOf("A", "B"), strs(p.getJSONArray("accepted_order_ids")))
        assertEquals(2, p.getJSONArray("raw_data_records").length())
        assertEquals(1, p.getJSONObject("summary_reported").getInt("payload_error"))
        // also for an unmatched-only ID
        val unmatched = OrderExecutionService.parseMultiPlacement(200, JSONObject("""{"status":"success",
            "data":[{"correlation_id":"zz","order_id":"X"}],"summary":{"total":2,"payload_error":1,"success":1,"error":0}}"""), req())
        assertEquals("UNKNOWN", unmatched.getString("placement_status"))
        assertTrue(strs(unmatched.getJSONArray("anomalies")).contains("PAYLOAD_ERROR_WITH_ORDER_IDS"))
        assertEquals(listOf("X"), strs(unmatched.getJSONArray("accepted_order_ids")))
    }

    @Test fun publicMulti_payloadErrorWithIdsReturnsOkFalseWithAnomaly() {
        OrderExecutionService.transport = OrderExecutionService.Transport { _, _, _, _ -> OrderExecutionService.TransportResponse(200, "", codexBody) }
        val legs = JSONArray()
            .put(JSONObject().put("instrument_key", "NSE_FO|3001").put("action", "BUY").put("lots", 1).put("lot_size", 75)
                .put("price", 10.0).put("price_source", "requested_limit").put("correlation_id", "1"))
            .put(JSONObject().put("instrument_key", "NSE_FO|3002").put("action", "SELL").put("lots", 1).put("lot_size", 75)
                .put("price", 20.0).put("price_source", "requested_limit").put("correlation_id", "2"))
        val out = OrderExecutionService.runDebugAction(JSONObject().put("action", "place_multi").put("trade_ref", "T-S").put("legs", legs), "tok")
        assertFalse(out.toString(), out.getBoolean("ok"))
        assertEquals("UNKNOWN", out.getString("placement_status"))
        assertEquals("PLACEMENT_UNKNOWN", out.getString("error_code"))
        assertTrue(strs(out.getJSONArray("placement_anomalies")).contains("PAYLOAD_ERROR_WITH_ORDER_IDS"))
        assertEquals(listOf("A", "B"), strs(out.getJSONArray("order_ids")))
        assertFalse(out.getBoolean("fill_implied"))
    }

    @Test fun coherentPayloadRejectionWithoutIds_keepsCurrentClassification() {
        // Payload validation rejects the batch and no ID is observed. Classified ALL_REJECTED at 6e7abb3; must stay so.
        val allErrors = """{"status":"error","errors":[{"correlation_id":"1","error_code":"UDAPI1115","message":"m"},
            {"correlation_id":"2","error_code":"UDAPI1115","message":"m"}],"summary":{"total":2,"payload_error":2,"success":0,"error":0}}"""
        val oneError = """{"status":"error","errors":[{"correlation_id":"2","error_code":"UDAPI1116","message":"m"}],
            "summary":{"total":2,"payload_error":1,"success":0,"error":0}}"""
        for ((code, body) in listOf(400 to allErrors, 400 to oneError, 200 to oneError)) {
            val p = OrderExecutionService.parseMultiPlacement(code, JSONObject(body), req())
            assertEquals("$code $body -> $p", "ALL_REJECTED", p.getString("placement_status"))
            assertEquals(0, p.getJSONArray("anomalies").length())
            assertEquals(0, p.getJSONArray("accepted_order_ids").length())
        }
    }
}
