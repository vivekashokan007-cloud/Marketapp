package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * b510 R1: DOCUMENTED raw Upstox /v2/option/chain responses (no trading_symbol) through the REAL
 * native adapter (OptionChainBrainFormatter.format + stampRequest, exactly what mergeChain uses).
 * The output must equal x1_upstox_formatted_golden.json, which the Python X1 collector test
 * (test_b510_x1_raw_feed_identity.py) runs through reference capture, entry, control and exits.
 * Regenerate the golden only deliberately: X1_WRITE_GOLDEN=<abs path> ./gradlew ... --tests '*B510X1RawFeed*'
 */
class B510X1RawFeedFormatterTest {
    private fun resource(name: String): String = requireNotNull(
        javaClass.classLoader?.getResourceAsStream("contracts/$name")).bufferedReader().readText()

    private fun formatAll(): JSONObject {
        val raw = JSONObject(resource("x1_upstox_raw_chains.json")).getJSONArray("snapshots")
        val out = JSONObject()
        for (i in 0 until raw.length()) {
            val snap = raw.getJSONObject(i)
            val formatted = OptionChainBrainFormatter.format(snap.getJSONObject("raw"), snap.getDouble("spot"))
            OptionChainBrainFormatter.stampRequest(formatted, snap.getString("requested_expiry"),
                snap.getString("requested_underlying_key"))
            out.put(snap.getString("id"), formatted)
        }
        return out
    }

    @Test fun documentedFeedCarriesRowIdentityOnEveryLegWithoutSymbols() {
        val raw = JSONObject(resource("x1_upstox_raw_chains.json")).getJSONArray("snapshots")
        val formatted = formatAll()
        for (i in 0 until raw.length()) {
            val snap = raw.getJSONObject(i)
            assertFalse("fixture must be documented-only", snap.getJSONObject("raw").toString().contains("trading_symbol"))
            val chain = formatted.getJSONObject(snap.getString("id"))
            assertEquals(snap.getString("requested_underlying_key"), chain.getString("requested_underlying_key"))
            assertEquals(snap.getString("requested_expiry"), chain.getString("requested_expiry"))
            val rows = snap.getJSONObject("raw").getJSONArray("data")
            for (r in 0 until rows.length()) {
                val row = rows.getJSONObject(r)
                val strike = chain.getJSONObject("strikes").getJSONObject(row.getDouble("strike_price").toInt().toString())
                for ((side, rawSide) in listOf("CE" to "call_options", "PE" to "put_options")) {
                    val leg = strike.getJSONObject(side)
                    assertEquals(row.getString("underlying_key"), leg.getString("underlying_key"))
                    assertEquals(row.getString("expiry"), leg.getString("contract_expiry"))
                    assertEquals(row.getJSONObject(rawSide).getString("instrument_key"), leg.getString("instrument_key"))
                    assertEquals("", leg.getString("trading_symbol"))       // absent upstream, never invented
                    assertEquals(row.getJSONObject(rawSide).getJSONObject("market_data").getDouble("bid_price"),
                        leg.getDouble("bid"), 0.0)
                }
            }
        }
    }

    @Test fun formatterOutputEqualsTheGoldenTheCollectorConsumes() {
        val actual = formatAll()
        val writeTo = System.getenv("X1_WRITE_GOLDEN")
        if (!writeTo.isNullOrBlank()) File(writeTo).writeText(canonical(actual).toString() + "\n")
        val golden = JSONObject(resource("x1_upstox_formatted_golden.json"))
        assertEquals(canonical(golden).toString(), canonical(actual).toString())
    }

    @Test fun rowWithoutDocumentedIdentityYieldsEmptyIdentityNotAGuess() {
        val raw = JSONObject().put("data", JSONArray().put(JSONObject().put("strike_price", 24500)
            .put("call_options", JSONObject().put("instrument_key", "NSE_FO|1"))
            .put("put_options", JSONObject().put("instrument_key", "NSE_FO|2"))))
        val leg = OptionChainBrainFormatter.format(raw, 24500.0).getJSONObject("strikes").getJSONObject("24500").getJSONObject("CE")
        assertEquals("", leg.getString("underlying_key"))
        assertEquals("", leg.getString("contract_expiry"))
        assertTrue(leg.has("instrument_key"))
    }

    /** Sorted-key deep copy so comparisons do not depend on org.json key order. */
    private fun canonical(v: Any?): Any? = when (v) {
        is JSONObject -> JSONObject().also { o -> v.keys().asSequence().sorted().forEach { o.put(it, canonical(v.get(it))) } }
        is JSONArray -> JSONArray().also { a -> for (i in 0 until v.length()) a.put(canonical(v.get(i))) }
        else -> v
    }
}
