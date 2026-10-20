package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.net.SocketTimeoutException

/**
 * Codex sandbox corrections G1-G3 (18 Oct 2026). Drives OrderExecutionService.runDebugAction only through its public
 * surface, with a FAKE transport and a fake persister: no network, no broker, no Supabase.
 */
class SandboxExecutionG1G3Test {
    private val now = 1_760_000_000_000L
    private val tokA = "NSE_FO|1001"
    private val tokB = "NSE_FO|1002"
    private val tokC = "NSE_FO|1003"

    private class FakeTransport(private val script: MutableList<() -> OrderExecutionService.TransportResponse>) :
        OrderExecutionService.Transport {
        val calls = mutableListOf<Triple<String, String, String?>>()
        override fun execute(method: String, url: String, body: String?, bearerToken: String): OrderExecutionService.TransportResponse {
            calls += Triple(method, url, body)
            check(script.isNotEmpty()) { "unexpected extra dispatch #${calls.size}" }
            return script.removeAt(0).invoke()
        }
    }

    private val persisted = mutableListOf<JSONObject>()

    @Before fun seams() {
        OrderExecutionService.persistRow = { persisted += it; true }
        OrderExecutionService.clock = { now }
    }

    @After fun reset() = OrderExecutionService.resetSeamsForTest()

    private fun fake(vararg steps: () -> OrderExecutionService.TransportResponse) =
        FakeTransport(steps.toMutableList()).also { OrderExecutionService.transport = it }

    private fun ok(code: Int, body: String) = { OrderExecutionService.TransportResponse(code, "", body) }

    private fun leg(token: String, side: String, extra: JSONObject.() -> Unit = {}) =
        JSONObject().put("instrument_key", token).put("action", side).put("lots", 1).put("lot_size", 75).apply(extra)

    private fun build(payload: JSONObject) = OrderExecutionService.runDebugAction(payload.put("action", "build"), "")

    private fun quote(token: String, vararg kv: Pair<String, Any>) =
        JSONObject().put("instrument_key", token).put("ts_ms", now - 500).apply { kv.forEach { put(it.first, it.second) } }

    private fun requested(token: String, side: String, price: Double, cid: String) =
        leg(token, side) { put("price", price); put("price_source", "requested_limit"); put("correlation_id", cid) }

    private fun placePayload(action: String, vararg legs: JSONObject) =
        JSONObject().put("action", action).put("trade_ref", "T-G").put("legs", JSONArray(legs.toList()))

    // ---------------------------------------------------------------- G1: no implicit LTP fallback
    @Test fun g1_buyMissingAsk_withPositiveLtp_refusesQuoteDerivedPricing() {
        val out = build(JSONObject().put("legs", JSONArray().put(leg(tokA, "BUY")))
            .put("quotes", JSONObject().put(tokA, quote(tokA, "ltp" to 100.0, "bid" to 99.0))))
        assertFalse("missing ask + positive LTP must not build: $out", out.getBoolean("ok"))
        assertEquals("QUOTE_SIDE_REQUIRED", out.getString("error_code"))
        assertEquals(0, out.getJSONArray("orders").length())
    }

    @Test fun g1_zeroOrInvalidRequiredSide_withPositiveLtp_refused() {
        for ((side, key) in listOf("BUY" to "ask", "SELL" to "bid")) for (bad in listOf<Any>(0.0, -1.0, "abc")) {
            val out = build(JSONObject().put("legs", JSONArray().put(leg(tokA, side)))
                .put("quotes", JSONObject().put(tokA, quote(tokA, "ltp" to 100.0, key to bad))))
            assertFalse("$side $key=$bad with LTP must be refused: $out", out.getBoolean("ok"))
            // R3: a negative side has its own explicit refusal reason
            assertEquals(if (bad == -1.0) "QUOTE_SIDE_NEGATIVE" else "QUOTE_SIDE_INVALID", out.getString("error_code"))
        }
    }

    @Test fun g1_pwaPreviewShape_legLtpOnly_isRefusedNotLabelledExecutable() {
        // The shape MarketVivi buildSandboxPreviewPayload sends: leg `ltp` only, notes.price_source=candidate_ltp_fallback.
        val payload = JSONObject().put("notes", JSONObject().put("price_source", "candidate_ltp_fallback"))
            .put("legs", JSONArray().put(leg(tokA, "BUY") { put("ltp", 120.5) }).put(leg(tokB, "SELL") { put("ltp", 80.0) }))
        val out = build(payload)
        assertFalse("LTP-only legs must not build an executable LIMIT: $out", out.getBoolean("ok"))
        assertEquals("EXECUTABLE_QUOTE_REQUIRED", out.getString("error_code"))
    }

    @Test fun g1_validBuyAsk_andSellBid_succeedWithQuoteProvenance() {
        val out = build(JSONObject()
            .put("legs", JSONArray().put(leg(tokA, "BUY") { put("correlation_id", "b1") }).put(leg(tokB, "SELL") { put("correlation_id", "s1") }))
            .put("quotes", JSONObject()
                .put(tokA, quote(tokA, "bid" to 99.5, "ask" to 100.25, "ltp" to 150.0))
                .put(tokB, quote(tokB, "bid" to 80.1, "ask" to 80.9, "ltp" to 10.0))))
        assertTrue(out.toString(), out.getBoolean("ok"))
        val orders = out.getJSONArray("orders")
        assertEquals("BUY", orders.getJSONObject(0).getString("transaction_type"))
        assertEquals(100.25, orders.getJSONObject(0).getDouble("price"), 1e-9)   // ask, never LTP 150
        assertEquals(80.1, orders.getJSONObject(1).getDouble("price"), 1e-9)     // bid, never LTP 10
        val pricing = out.getJSONArray("pricing")
        assertEquals(2, pricing.length())
        for (i in 0 until 2) {
            val p = pricing.getJSONObject(i)
            assertEquals("QUOTE_DERIVED_LIMIT", p.getString("price_source"))
            assertTrue(p.getBoolean("identity_checked"))
            assertFalse("a quote-derived LIMIT is not a fill claim", p.getBoolean("fill_claim"))
        }
        assertEquals("ask", pricing.getJSONObject(0).getString("quote_side"))
        assertEquals("bid", pricing.getJSONObject(1).getString("quote_side"))
    }

    @Test fun g1_explicitRequestedLimit_retainsRequestedProvenance_noQuoteOrFillClaim() {
        val out = build(JSONObject().put("legs", JSONArray().put(requested(tokA, "BUY", 101.5, "r1"))))
        assertTrue(out.toString(), out.getBoolean("ok"))
        assertEquals(101.5, out.getJSONArray("orders").getJSONObject(0).getDouble("price"), 1e-9)
        val p = out.getJSONArray("pricing").getJSONObject(0)
        assertEquals("REQUESTED_LIMIT", p.getString("price_source"))
        assertEquals(101.5, p.getDouble("requested_price"), 1e-9)
        assertFalse(p.getBoolean("executable_quote"))
        assertFalse(p.getBoolean("fill_claim"))
    }

    @Test fun g1_unlabelledPrice_cannotBeDistinguished_failsClosed() {
        val out = build(JSONObject().put("legs", JSONArray().put(leg(tokA, "BUY") { put("price", 101.5) })))
        assertFalse("an unlabelled leg price is ambiguous: $out", out.getBoolean("ok"))
        assertEquals("PRICE_SOURCE_AMBIGUOUS", out.getString("error_code"))
        val quoteLabelled = build(JSONObject().put("legs", JSONArray().put(leg(tokA, "BUY") { put("price", 101.5); put("price_source", "quote") })))
        assertEquals("PRICE_SOURCE_AMBIGUOUS", quoteLabelled.getString("error_code"))
        val unknownLabel = build(JSONObject().put("legs", JSONArray().put(leg(tokA, "BUY") { put("price", 101.5); put("price_source", "ltp") })))
        assertEquals("PRICE_SOURCE_UNKNOWN", unknownLabel.getString("error_code"))
    }

    @Test fun g1_quoteValidity_identityStaleCrossedAndMissingTimestamp_refused() {
        fun one(q: JSONObject) = build(JSONObject().put("legs", JSONArray().put(leg(tokA, "BUY"))).put("quotes", JSONObject().put(tokA, q)))
        assertEquals("QUOTE_IDENTITY_MISMATCH", one(quote(tokB, "ask" to 100.0)).getString("error_code"))
        assertEquals("QUOTE_STALE", one(quote(tokA, "ask" to 100.0, "bid" to 99.0).put("ts_ms", now - 60_000)).getString("error_code"))
        assertEquals("QUOTE_CROSSED", one(quote(tokA, "ask" to 100.0, "bid" to 101.0)).getString("error_code"))
        assertEquals("QUOTE_TIMESTAMP_REQUIRED", one(JSONObject().put("ask", 100.0).put("bid", 99.0)).getString("error_code"))
        assertEquals("QUOTE_INVALID", one(quote(tokA, "ask" to 100.0, "valid" to false)).getString("error_code"))
    }

    @Test fun g1_brokerOrderBodyCarriesNoProvenanceFields() {
        val out = build(JSONObject().put("legs", JSONArray().put(requested(tokA, "BUY", 101.5, "r1"))))
        val keys = out.getJSONArray("orders").getJSONObject(0).keys().asSequence().toSet()
        assertEquals(setOf("correlation_id", "quantity", "product", "validity", "price", "tag", "instrument_token", "order_type",
            "transaction_type", "disclosed_quantity", "trigger_price", "is_amo", "slice"), keys)
    }

    // ---------------------------------------------------------------- G2: multi-order body parsed per line
    private val twoLines get() = placePayload("place_multi", requested(tokA, "BUY", 10.0, "1"), requested(tokB, "SELL", 20.0, "2"))

    @Test fun g2_http207Partial_preservesAcceptedIdAndLineFailure_notSuccess() {
        val t = fake(ok(207, """{"status":"partial_success","data":[{"correlation_id":"1","order_id":"OID-1"}],
            "errors":[{"correlation_id":"2","error_code":"UDAPI100049","message":"insufficient margin"}],
            "summary":{"total":2,"payload_error":0,"success":1,"error":1}}"""))
        val out = OrderExecutionService.runDebugAction(twoLines, "tok")
        assertEquals(1, t.calls.size)
        assertFalse("207 partial is not full success: $out", out.getBoolean("ok"))
        assertEquals("PARTIAL", out.getString("placement_status"))
        assertEquals(listOf("OID-1"), out.getJSONArray("order_ids").toList())
        val lines = out.getJSONObject("placement").getJSONArray("lines")
        assertEquals("ACCEPTED", lines.getJSONObject(0).getString("outcome"))
        assertEquals("REJECTED", lines.getJSONObject(1).getString("outcome"))
        assertEquals("UDAPI100049", lines.getJSONObject(1).getJSONArray("errors").getJSONObject(0).getString("error_code"))
        assertNoFill(out)
    }

    @Test fun g2_completePlacement_isPlacementSuccessOnly() {
        fake(ok(200, """{"status":"success","data":[{"correlation_id":"1","order_id":"OID-1"},{"correlation_id":"2","order_id":"OID-2"}],
            "summary":{"total":2,"payload_error":0,"success":2,"error":0}}"""))
        val out = OrderExecutionService.runDebugAction(twoLines, "tok")
        assertTrue(out.toString(), out.getBoolean("ok"))
        assertEquals("ALL_ACCEPTED", out.getString("placement_status"))
        assertEquals(listOf("OID-1", "OID-2"), out.getJSONArray("order_ids").toList())
        assertNoFill(out)
    }

    @Test fun g2_allLinesRejected() {
        fake(ok(400, """{"status":"error","errors":[{"correlation_id":"1","error_code":"UDAPI1","message":"a"},
            {"correlation_id":"2","error_code":"UDAPI2","message":"b"}],"summary":{"total":2,"payload_error":0,"success":0,"error":2}}"""))
        val out = OrderExecutionService.runDebugAction(twoLines, "tok")
        assertFalse(out.getBoolean("ok"))
        assertEquals("ALL_REJECTED", out.getString("placement_status"))
        val lines = out.getJSONObject("placement").getJSONArray("lines")
        assertEquals(listOf("REJECTED", "REJECTED"), (0 until 2).map { lines.getJSONObject(it).getString("outcome") })
        assertEquals(0, out.getJSONArray("order_ids").length())
        assertNoFill(out)
    }

    @Test fun g2_malformed2xx_isUnknownNotSuccess() {
        for (body in listOf("<html>gateway</html>", "{}", """{"status":"success"}""", "[1,2]")) {
            fake(ok(200, body))
            val out = OrderExecutionService.runDebugAction(twoLines, "tok")
            assertFalse("malformed 2xx '$body' must not be success: $out", out.getBoolean("ok"))
            assertEquals(body, "UNKNOWN", out.getString("placement_status"))
            val lines = out.getJSONObject("placement").getJSONArray("lines")
            assertEquals(listOf("UNKNOWN", "UNKNOWN"), (0 until 2).map { lines.getJSONObject(it).getString("outcome") })
            assertNoFill(out)
        }
    }

    @Test fun g2_successStatusWithMissingLine_keepsIdAndMarksLineUnknown() {
        fake(ok(200, """{"status":"success","data":[{"correlation_id":"1","order_id":"OID-1"}],"summary":{"total":2,"success":2,"error":0}}"""))
        val out = OrderExecutionService.runDebugAction(twoLines, "tok")
        assertFalse(out.getBoolean("ok"))
        assertEquals("PARTIAL", out.getString("placement_status"))
        assertEquals(listOf("OID-1"), out.getJSONArray("order_ids").toList())
        assertEquals("UNKNOWN", out.getJSONObject("placement").getJSONArray("lines").getJSONObject(1).getString("outcome"))
        assertTrue(out.getJSONObject("placement").getJSONArray("anomalies").toList().contains("SUMMARY_MISMATCH"))
    }

    @Test fun g2_unmatchedAcceptedIdIsPreserved_andTimeoutIsUnknown() {
        fake(ok(207, """{"status":"partial_success","data":[{"correlation_id":"zz","order_id":"OID-X"}],
            "errors":[{"correlation_id":"2","error_code":"E","message":"m"}]}"""))
        val out = OrderExecutionService.runDebugAction(twoLines, "tok")
        assertEquals(listOf("OID-X"), out.getJSONArray("order_ids").toList())
        assertEquals(listOf("OID-X"), out.getJSONObject("placement").getJSONArray("unmatched_accepted_order_ids").toList())
        assertEquals("PARTIAL", out.getString("placement_status"))

        val t = fake({ throw SocketTimeoutException("read timed out") })
        val timedOut = OrderExecutionService.runDebugAction(twoLines, "tok")
        assertEquals("one dispatch, no retry", 1, t.calls.size)
        assertEquals("UNKNOWN", timedOut.getString("placement_status"))
        assertFalse(timedOut.getBoolean("ok"))
    }

    // ---------------------------------------------------------------- G3: sequential stops after failure/uncertainty
    private val threeLegs get() = placePayload("place_sequential",
        requested(tokA, "BUY", 10.0, "1"), requested(tokB, "BUY", 11.0, "2"), requested(tokC, "SELL", 20.0, "3"))
    private fun placed(id: String) = ok(200, """{"status":"success","data":{"order_id":"$id"}}""")

    @Test fun g3_firstSubmissionRejected_noSecondDispatch() {
        val t = fake(ok(400, """{"status":"error","errors":[{"errorCode":"UDAPI100049","message":"margin"}]}"""))
        val out = OrderExecutionService.runDebugAction(threeLegs, "tok")
        assertEquals("no second dispatch after a rejection", 1, t.calls.size)
        assertFalse(out.getBoolean("ok"))
        assertEquals("STOPPED_AFTER_REJECTION", out.getString("placement_status"))
        assertEquals(listOf("2", "3"), out.getJSONArray("not_submitted_correlation_ids").toList())
        assertEquals(0, out.getJSONArray("accepted_order_ids").length())
        assertNoFill(out)
    }

    @Test fun g3_laterSubmissionFails_earlierIdsRetained_restNotSent() {
        val t = fake(placed("OID-1"), ok(503, """{"status":"error"}"""))
        val out = OrderExecutionService.runDebugAction(threeLegs, "tok")
        assertEquals(2, t.calls.size)
        assertEquals("STOPPED_AFTER_UNCERTAIN", out.getString("placement_status"))
        assertEquals(listOf("OID-1"), out.getJSONArray("accepted_order_ids").toList())
        assertEquals(listOf("3"), out.getJSONArray("not_submitted_correlation_ids").toList())
        assertEquals(1, out.getInt("stopped_at_index"))
        assertFalse(out.getBoolean("ok"))
    }

    @Test fun g3_timeout_isUncertain_noAutomaticRetry() {
        val t = fake({ throw SocketTimeoutException("read timed out") })
        val out = OrderExecutionService.runDebugAction(threeLegs, "tok")
        assertEquals("exactly one dispatch: no retry of the timed-out order and no next leg", 1, t.calls.size)
        assertEquals("STOPPED_AFTER_UNCERTAIN", out.getString("placement_status"))
        assertFalse(out.getBoolean("auto_retry"))
        assertEquals("UNKNOWN", out.getJSONArray("submissions").getJSONObject(0).getString("outcome"))
    }

    @Test fun g3_ackWithoutOrderId_isUncertainAndStops() {
        val t = fake(ok(200, """{"status":"success","data":{}}"""))
        val out = OrderExecutionService.runDebugAction(threeLegs, "tok")
        assertEquals(1, t.calls.size)
        assertEquals("STOPPED_AFTER_UNCERTAIN", out.getString("placement_status"))
    }

    @Test fun g3_allAccepted_isPlacementSuccessOnly_noDependentShortAuthority() {
        val t = fake(placed("OID-1"), placed("OID-2"), placed("OID-3"))
        val out = OrderExecutionService.runDebugAction(threeLegs, "tok")
        assertEquals(3, t.calls.size)
        assertTrue(t.calls.all { it.first == "POST" && it.second == "https://sandbox.upstox.com/v2/order/place" })
        assertEquals(listOf("BUY", "BUY", "SELL"), t.calls.map { JSONObject(it.third!!).getString("transaction_type") })
        assertTrue(out.getBoolean("ok"))
        assertEquals("ALL_ACCEPTED", out.getString("placement_status"))
        assertEquals(listOf("OID-1", "OID-2", "OID-3"), out.getJSONArray("accepted_order_ids").toList())
        assertNoFill(out)
        assertEquals(3, persisted.size)
    }

    @Test fun g3_clientDisablesSilentRetries_andSandboxLockUnchanged() {
        val src = listOf(File("src/main/java/com/marketradar/app/OrderExecutionService.kt"),
            File("app/src/main/java/com/marketradar/app/OrderExecutionService.kt")).first { it.exists() }.readText()
        assertTrue("OkHttp must not silently re-send an order", src.contains(".retryOnConnectionFailure(false)"))
        assertTrue(src.contains("private const val SANDBOX_BASE_URL = \"https://sandbox.upstox.com\""))
        assertEquals("SANDBOX", OrderExecutionService.EXECUTION_MODE)
    }

    private fun assertNoFill(out: JSONObject) {
        assertEquals("UNKNOWN_NOT_RECONCILED", out.getString("fill_status"))
        assertFalse(out.getBoolean("fill_implied"))
        assertFalse(out.getBoolean("dependent_live_short_entry_authorized"))
        assertNotEquals("FILLED", out.optString("placement_status"))
    }

    private fun JSONArray.toList(): List<Any> = (0 until length()).map { get(it) }
}
