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

/**
 * Codex review of PR #20 (19 Oct 2026): nine named counterexamples R1-R3, plus symmetric SELL and preservation cases.
 * Fakes only: no network, broker or Supabase.
 */
class Pr20CounterexampleTest {
    private val now = 1_760_000_000_000L
    private val tokA = "NSE_FO|2001"
    private val tokB = "NSE_FO|2002"

    @Before fun seams() {
        OrderExecutionService.persistRow = { true }
        OrderExecutionService.clock = { now }
    }

    @After fun reset() = OrderExecutionService.resetSeamsForTest()

    private fun order(cid: String, slice: Boolean = false) = JSONObject().put("correlation_id", cid).put("slice", slice)
    private val twoOrders get() = JSONArray().put(order("1")).put(order("2"))
    private fun parse(code: Int, body: String, req: JSONArray = twoOrders) =
        OrderExecutionService.parseMultiPlacement(code, JSONObject(body), req)
    private fun anomalies(p: JSONObject) = (0 until p.getJSONArray("anomalies").length()).map { p.getJSONArray("anomalies").getString(it) }
    private fun ids(a: JSONArray) = (0 until a.length()).map { a.getString(it) }

    private val successBody = """{"status":"success","data":[{"correlation_id":"1","order_id":"A"},{"correlation_id":"2","order_id":"B"}],
        "summary":{"total":2,"payload_error":0,"success":2,"error":0}}"""

    // ------------------------------------------------------------------ R1
    @Test fun g2_http503WithSuccessBodyIsUncertain() {
        val p = parse(503, successBody)
        assertEquals("UNKNOWN", p.getString("placement_status"))
        assertEquals("IDs kept as reconciliation evidence", listOf("A", "B"), ids(p.getJSONArray("accepted_order_ids")))
        assertTrue(anomalies(p).toString(), anomalies(p).isNotEmpty())
        for (code in listOf(408, 500, 502, 0)) assertEquals("HTTP $code", "UNKNOWN", parse(code, successBody).getString("placement_status"))
    }

    @Test fun g2_http400WithSuccessBodyIsContradictory() {
        val p = parse(400, successBody)
        assertEquals("UNKNOWN", p.getString("placement_status"))
        assertTrue(anomalies(p).toString(), anomalies(p).contains("REJECTION_STATUS_WITH_ORDER_IDS"))
        assertEquals(listOf("A", "B"), ids(p.getJSONArray("accepted_order_ids")))
    }

    @Test fun g2_publicMulti503MustNotReturnOkTrue() {
        OrderExecutionService.transport = OrderExecutionService.Transport { _, _, _, _ -> OrderExecutionService.TransportResponse(503, "", successBody) }
        val out = OrderExecutionService.runDebugAction(placePayload(), "tok")
        assertFalse(out.toString(), out.getBoolean("ok"))
        assertNotEquals("ALL_ACCEPTED", out.getString("placement_status"))
        assertEquals("HTTP_503", out.getString("error_code"))
        assertEquals(listOf("A", "B"), ids(out.getJSONArray("order_ids")))
    }

    // ------------------------------------------------------------------ R2
    @Test fun g2_duplicateBrokerIdAcrossLinesIsAnomaly() {
        val p = parse(200, """{"status":"success","data":[{"correlation_id":"1","order_id":"A"},{"correlation_id":"2","order_id":"A"}],
            "summary":{"total":2,"payload_error":0,"success":2,"error":0}}""")
        assertNotEquals("ALL_ACCEPTED", p.getString("placement_status"))
        assertTrue(anomalies(p).contains("DUPLICATE_BROKER_ORDER_ID"))
        assertEquals("a reused ID is one observed order, not two", listOf("A"), ids(p.getJSONArray("accepted_order_ids")))
        assertEquals(2, p.getJSONArray("raw_data_records").length())
    }

    @Test fun g2_multipleIdsForUnslicedLineIsAnomaly() {
        val p = parse(200, """{"status":"success","data":[{"correlation_id":"1","order_id":"A"},{"correlation_id":"1","order_id":"C"},
            {"correlation_id":"2","order_id":"B"}],"summary":{"total":2,"payload_error":0,"success":3,"error":0}}""")
        assertNotEquals("ALL_ACCEPTED", p.getString("placement_status"))
        assertTrue(anomalies(p).contains("MULTIPLE_IDS_FOR_UNSLICED_LINE"))
        val line1 = p.getJSONArray("lines").getJSONObject(0)
        assertEquals("UNKNOWN", line1.getString("outcome"))
        assertEquals(listOf("A", "C"), ids(line1.getJSONArray("order_ids")))
        assertEquals(listOf("A", "C", "B"), ids(p.getJSONArray("accepted_order_ids")))
    }

    @Test fun g2_unmatchedBatchErrorPreventsFullAcceptance() {
        val p = parse(200, """{"status":"success","data":[{"correlation_id":"1","order_id":"A"},{"correlation_id":"2","order_id":"B"}],
            "errors":[{"correlation_id":"zz","error_code":"UDAPI9","message":"?"}]}""")
        assertNotEquals("ALL_ACCEPTED", p.getString("placement_status"))
        assertTrue(anomalies(p).contains("UNMATCHED_OR_BATCH_ERROR"))
        assertEquals(1, p.getJSONArray("batch_errors").length())
        assertEquals(listOf("A", "B"), ids(p.getJSONArray("accepted_order_ids")))
    }

    // ------------------------------------------------------------------ R3 (BUY and SELL)
    private fun quoteBuild(side: String, q: JSONObject): JSONObject {
        val leg = JSONObject().put("instrument_key", tokA).put("action", side).put("lots", 1).put("lot_size", 75)
        return OrderExecutionService.runDebugAction(JSONObject().put("action", "build")
            .put("legs", JSONArray().put(leg)).put("quotes", JSONObject().put(tokA, q.put("instrument_key", tokA).put("ts_ms", now - 500))), "")
    }
    private fun requiredAndOpposite(side: String) = if (side == "BUY") "ask" to "bid" else "bid" to "ask"

    @Test fun g1_negativeOppositeSideRefused() {
        for (side in listOf("BUY", "SELL")) {
            val (req, opp) = requiredAndOpposite(side)
            val out = quoteBuild(side, JSONObject().put(req, 100.0).put(opp, -1.0))
            assertFalse("$side $out", out.getBoolean("ok"))
            assertEquals(side, "QUOTE_SIDE_NEGATIVE", out.getString("error_code"))
            val reqNeg = quoteBuild(side, JSONObject().put(req, -5.0).put(opp, 100.0))
            assertEquals("$side required side negative", "QUOTE_SIDE_NEGATIVE", reqNeg.getString("error_code"))
        }
    }

    @Test fun g1_missingOppositeSideRefused() {
        for (side in listOf("BUY", "SELL")) {
            val (req, _) = requiredAndOpposite(side)
            val out = quoteBuild(side, JSONObject().put(req, 100.0).put("ltp", 100.0))
            assertFalse("$side $out", out.getBoolean("ok"))
            assertEquals(side, "QUOTE_OPPOSITE_SIDE_REQUIRED", out.getString("error_code"))
        }
    }

    @Test fun g1_zeroOppositeSideRefused() {
        for (side in listOf("BUY", "SELL")) {
            val (req, opp) = requiredAndOpposite(side)
            for (bad in listOf<Any>(0.0, "abc")) {
                val out = quoteBuild(side, JSONObject().put(req, 100.0).put(opp, bad))
                assertFalse("$side $opp=$bad $out", out.getBoolean("ok"))
                assertEquals("$side $opp=$bad", "QUOTE_OPPOSITE_SIDE_INVALID", out.getString("error_code"))
            }
        }
    }

    // ------------------------------------------------------------------ preservation / symmetry (not in Codex's nine)
    @Test fun extra_crossedBookRefusedBothSides_twoSidedBookSucceedsBothSides() {
        for (side in listOf("BUY", "SELL")) {
            assertEquals(side, "QUOTE_CROSSED", quoteBuild(side, JSONObject().put("ask", 99.0).put("bid", 100.0)).getString("error_code"))
            val okOut = quoteBuild(side, JSONObject().put("ask", 100.0).put("bid", 100.0))   // locked book: ask == bid allowed
            assertTrue("$side $okOut", okOut.getBoolean("ok"))
            assertEquals("QUOTE_DERIVED_LIMIT", okOut.getJSONArray("pricing").getJSONObject(0).getString("price_source"))
        }
    }

    @Test fun extra_requestedLimitStaysDistinct_noQuoteNeeded() {
        val leg = JSONObject().put("instrument_key", tokA).put("action", "SELL").put("lots", 1).put("lot_size", 75)
            .put("price", 55.5).put("price_source", "requested_limit")
        val out = OrderExecutionService.runDebugAction(JSONObject().put("action", "build").put("legs", JSONArray().put(leg)), "")
        assertTrue(out.toString(), out.getBoolean("ok"))
        val p = out.getJSONArray("pricing").getJSONObject(0)
        assertEquals("REQUESTED_LIMIT", p.getString("price_source"))
        assertFalse(p.getBoolean("executable_quote"))
    }

    @Test fun extra_normalCompletePlacementStillSucceeds_slicedStaysConservative() {
        OrderExecutionService.transport = OrderExecutionService.Transport { _, _, _, _ -> OrderExecutionService.TransportResponse(200, "", successBody) }
        val out = OrderExecutionService.runDebugAction(placePayload(), "tok")
        assertTrue(out.toString(), out.getBoolean("ok"))
        assertEquals("ALL_ACCEPTED", out.getString("placement_status"))
        assertEquals(listOf("A", "B"), ids(out.getJSONArray("order_ids")))
        val sliced = parse(200, """{"status":"success","data":[{"correlation_id":"1_1","order_id":"S1"},{"correlation_id":"1_2","order_id":"S2"}]}""",
            JSONArray().put(order("1", slice = true)))
        assertNotEquals("ALL_ACCEPTED", sliced.getString("placement_status"))
        assertTrue(anomalies(sliced).contains("SLICE_COUNT_UNVERIFIED"))
        assertEquals(listOf("S1", "S2"), ids(sliced.getJSONArray("accepted_order_ids")))
    }

    private fun placePayload() = JSONObject().put("action", "place_multi").put("trade_ref", "T-R").put("legs", JSONArray()
        .put(JSONObject().put("instrument_key", tokA).put("action", "BUY").put("lots", 1).put("lot_size", 75).put("price", 10.0)
            .put("price_source", "requested_limit").put("correlation_id", "1"))
        .put(JSONObject().put("instrument_key", tokB).put("action", "SELL").put("lots", 1).put("lot_size", 75).put("price", 20.0)
            .put("price_source", "requested_limit").put("correlation_id", "2")))
}
