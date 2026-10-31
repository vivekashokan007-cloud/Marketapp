package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject

/**
 * Delivery B (R5 review s4): managed-policy-only evidence, carried in a SEPARATE context object
 * (ctx.managedEvidence, schema managed_evidence_v1) keyed by exact Upstox instrument_key.
 *
 * OptionChainBrainFormatter (the X1 formatter) is NOT changed and never reads this object; this formatter
 * only READS the raw /v2/option/chain response (bid_qty / ask_qty under market_data) and never mutates it.
 * Contract metadata (/v2/option/contract) and margin (/v2/charges/margin) responses are carried raw for the
 * Python adapter (managed_evidence_v1) to parse; this build adds NO new network call (fixtures only).
 */
object ManagedEvidenceFormatter {
    const val SCHEMA = "managed_evidence_v1"
    const val KEY = "managedEvidence"

    /** Upstox option-contract tick_size is in paise (documented example: 5 for a Rs 0.05 tick). */
    fun tickRupeesFromUpstox(raw: Double?): Double? {
        if (raw == null || raw.isNaN() || raw <= 0.0 || raw != Math.floor(raw)) return null
        val r = Math.round(raw).toDouble() / 100.0
        return if (r > 0.0 && r <= 1.0) r else null
    }

    fun quantities(rawChain: JSONObject, receivedAtMs: Long): JSONObject {
        val out = JSONObject()
        val rows = rawChain.optJSONArray("data") ?: return out
        for (i in 0 until rows.length()) {
            val row = rows.optJSONObject(i) ?: continue
            for (side in arrayOf("call_options", "put_options")) {
                val o = row.optJSONObject(side) ?: continue
                val key = o.optString("instrument_key", "")
                if (key.isEmpty()) continue
                val md = o.optJSONObject("market_data") ?: JSONObject()
                val q = JSONObject()
                for ((src, dst) in arrayOf("bid_price" to "bid", "ask_price" to "ask", "bid_qty" to "bid_qty", "ask_qty" to "ask_qty")) {
                    if (md.has(src) && !md.isNull(src)) q.put(dst, md.optDouble(src))
                }
                q.put("snapshot_ts", receivedAtMs)
                q.put("snapshot_ts_basis", "DEVICE_RECEIPT_NOT_EXCHANGE_TIME")
                out.put(key, q)
            }
        }
        return out
    }

    private fun bucket(ctx: JSONObject): JSONObject {
        val ev = ctx.optJSONObject(KEY) ?: JSONObject().put("schema", SCHEMA).also { ctx.put(KEY, it) }
        return ev.optJSONObject("native_raw") ?: JSONObject()
            .put("quantities", JSONObject()).put("contracts", JSONObject()).put("margins", JSONArray())
            .also { ev.put("native_raw", it) }
    }

    /** Add this chain's quantities to ctx.managedEvidence (raw chain is only read). */
    fun attachQuantities(ctx: JSONObject, rawChain: JSONObject, receivedAtMs: Long) {
        val q = bucket(ctx).getJSONObject("quantities")
        val add = quantities(rawChain, receivedAtMs)
        val it = add.keys()
        while (it.hasNext()) { val k = it.next(); q.put(k, add.get(k)) }
    }

    fun attachContracts(ctx: JSONObject, index: String, response: JSONObject, fetchedAtMs: Long) {
        bucket(ctx).getJSONObject("contracts").put(index, JSONObject().put("response", response).put("fetched_at_ms", fetchedAtMs))
    }

    fun attachMargin(ctx: JSONObject, request: JSONObject, response: JSONObject, receivedAtMs: Long) {
        bucket(ctx).getJSONArray("margins").put(JSONObject().put("request", request).put("response", response)
            .put("received_at_ms", receivedAtMs))
    }
}
