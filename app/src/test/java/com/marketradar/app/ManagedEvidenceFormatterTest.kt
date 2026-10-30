package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Delivery B (R5 review s4): the managed-only formatter is separate from the X1 formatter. Running it over the
 * same raw chains must leave the raw input and the X1 formatted output (golden) byte-identical; tick_size is
 * converted explicitly from Upstox paise to rupees.
 */
class ManagedEvidenceFormatterTest {
    private fun resource(name: String): String = requireNotNull(
        javaClass.classLoader?.getResourceAsStream("contracts/$name")).bufferedReader().readText()

    private fun canonical(v: Any?): Any? = when (v) {
        is JSONObject -> JSONObject().also { o -> v.keys().asSequence().sorted().forEach { o.put(it, canonical(v.get(it))) } }
        is JSONArray -> JSONArray().also { a -> for (i in 0 until v.length()) a.put(canonical(v.get(i))) }
        else -> v
    }

    @Test fun x1GoldenUnchangedWhenManagedFormatterRunsOnSameRawInput() {
        val snaps = JSONObject(resource("x1_upstox_raw_chains.json")).getJSONArray("snapshots")
        val out = JSONObject()
        for (i in 0 until snaps.length()) {
            val snap = snaps.getJSONObject(i)
            val raw = snap.getJSONObject("raw")
            val before = canonical(raw).toString()
            val ctx = JSONObject()
            ManagedEvidenceFormatter.attachQuantities(ctx, raw, 1_700_000_000_000L)   // managed first
            assertEquals("raw chain must not be mutated", before, canonical(raw).toString())
            val formatted = OptionChainBrainFormatter.format(raw, snap.getDouble("spot"))
            OptionChainBrainFormatter.stampRequest(formatted, snap.getString("requested_expiry"),
                snap.getString("requested_underlying_key"))
            ManagedEvidenceFormatter.attachQuantities(ctx, raw, 1_700_000_000_001L)   // and after
            assertTrue(ctx.getJSONObject("managedEvidence").getString("schema") == "managed_evidence_v1")
            out.put(snap.getString("id"), formatted)
        }
        val golden = JSONObject(resource("x1_upstox_formatted_golden.json"))
        assertEquals(canonical(golden).toString(), canonical(out).toString())
        // the X1 output never carries managed-only fields
        assertTrue(!out.toString().contains("bid_qty") && !out.toString().contains("managedEvidence"))
    }

    @Test fun tickSizeIsConvertedFromPaiseToRupees() {
        assertEquals(0.05, ManagedEvidenceFormatter.tickRupeesFromUpstox(5.0)!!, 1e-12)
        assertEquals(0.10, ManagedEvidenceFormatter.tickRupeesFromUpstox(10.0)!!, 1e-12)
        assertNull(ManagedEvidenceFormatter.tickRupeesFromUpstox(0.05))   // already-rupee / fractional: rejected
        assertNull(ManagedEvidenceFormatter.tickRupeesFromUpstox(0.0))
        assertNull(ManagedEvidenceFormatter.tickRupeesFromUpstox(null))
        assertNull(ManagedEvidenceFormatter.tickRupeesFromUpstox(500.0))  // > Rs 1 tick is implausible: rejected
    }

    @Test fun quantitiesAreKeyedByExactInstrumentKeyWithReceiptLabel() {
        val raw = JSONObject().put("data", JSONArray().put(JSONObject()
            .put("call_options", JSONObject().put("instrument_key", "NSE_FO|111")
                .put("market_data", JSONObject().put("bid_price", 10.5).put("ask_price", 10.7).put("bid_qty", 300).put("ask_qty", 450)))
            .put("put_options", JSONObject().put("instrument_key", "NSE_FO|222").put("market_data", JSONObject()))))
        val q = ManagedEvidenceFormatter.quantities(raw, 42L)
        assertEquals(300.0, q.getJSONObject("NSE_FO|111").getDouble("bid_qty"), 0.0)
        assertEquals("DEVICE_RECEIPT_NOT_EXCHANGE_TIME", q.getJSONObject("NSE_FO|111").getString("snapshot_ts_basis"))
        assertTrue(!q.getJSONObject("NSE_FO|222").has("bid_qty"))  // absent stays absent, never zero-filled
    }
}
