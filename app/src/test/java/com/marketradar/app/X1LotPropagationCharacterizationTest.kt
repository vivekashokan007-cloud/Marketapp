package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

/**
 * Codex 2026-10-29 task B2 (characterisation, no behaviour change; SYNTHETIC input).
 *
 * X1 reads the captured contract lot from the brain chain object (`chain.lotSize` / `chain.lot_size`,
 * paper_buying_research._lot). The native adapter that builds that object for the live Paper poll is
 * OptionChainBrainFormatter.format + stampRequest (MarketWatchService.mergeChain). This pins that the
 * adapter emits NO lot field at chain or leg level, even when the raw row carries one, so the X1
 * captured-vs-rule conflict guard receives no captured lot on the native path. A scoped fix is a
 * separate reviewed change; this test must be amended deliberately if that fix lands.
 */
class X1LotPropagationCharacterizationTest {
    private fun rawWithLot(): JSONObject {
        fun leg(key: String) = JSONObject().put("instrument_key", key).put("lot_size", 75)
            .put("market_data", JSONObject().put("ltp", 101.0).put("bid_price", 100.5).put("ask_price", 101.5)
                .put("oi", 1000.0))
        val row = JSONObject().put("strike_price", 24500).put("underlying_key", "NSE_INDEX|Nifty 50")
            .put("expiry", "2027-01-12").put("lot_size", 75)
            .put("call_options", leg("NSE_FO|1")).put("put_options", leg("NSE_FO|2"))
        return JSONObject().put("status", "success").put("lot_size", 75).put("data", JSONArray().put(row))
    }

    @Test fun formatterDropsEveryLotFieldSoX1SeesNoCapturedLot() {
        val formatted = OptionChainBrainFormatter.format(rawWithLot(), 24500.0)
        OptionChainBrainFormatter.stampRequest(formatted, "2027-01-12", "NSE_INDEX|Nifty 50")
        assertFalse(formatted.has("lotSize"))
        assertFalse(formatted.has("lot_size"))
        val strike = formatted.getJSONObject("strikes").getJSONObject("24500")
        for (side in listOf("CE", "PE")) {
            val leg = strike.getJSONObject(side)
            assertFalse(leg.has("lotSize"))
            assertFalse(leg.has("lot_size"))
        }
        assertFalse(formatted.toString().contains("lot"))
        assertEquals("2027-01-12", formatted.getString("requested_expiry"))
    }

    @Test fun goldenFormattedChainsConsumedByX1CarryNoLot() {
        val golden = JSONObject(requireNotNull(javaClass.classLoader?.getResourceAsStream(
            "contracts/x1_upstox_formatted_golden.json")).bufferedReader().readText())
        for (id in golden.keys()) {
            val chain = golden.getJSONObject(id)
            assertFalse(id, chain.has("lotSize") || chain.has("lot_size"))
        }
    }
}
