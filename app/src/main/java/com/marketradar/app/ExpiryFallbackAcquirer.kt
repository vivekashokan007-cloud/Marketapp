package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.URLEncoder

/**
 * Expiry-day Paper-BUY fallback (expiry_fallback_v1, REVIEW DRAFT r1): native acquisition of the evidence the
 * Python planner asked for, separate from the frozen direction scale (no `*ScaleChain` key, no
 * `scale_next_expiry_*` pref, no DirectionScaleNeed state is read or written here).
 *
 * Runs after analysis and before publication, only for indices whose plan says
 * `expiry_fallback.status == EVIDENCE_REQUIRED` (own expiry day + policy outright BUY + no expiry-independent
 * gate already refusing; decided in Python). Per index:
 *   1. listing: GET /v2/option/contract?instrument_key=U (no expiry filter) once per (index, session), cached in
 *      its own file; only the distinct expiry strings of rows whose underlying_key == U are kept;
 *   2. Python `single_leg_expiry_fallback_resolve` picks the later expiry (NF next distinct listed; BNF monthly);
 *   3. contract metadata for that expiry through [ManagedReadAdapters.ensureContracts] in a SEPARATE directory
 *      (its cache prunes other expiries of the same index, so sharing the trading cache would evict it);
 *   4. the chain for that expiry: GET /v2/option/chain, formatted by the unchanged OptionChainBrainFormatter
 *      and stamped with the requested expiry/underlying.
 * Read-only broker calls; never an order. Every request is counted per index (requests and response bytes).
 */
object ExpiryFallbackAcquirer {
    const val DIR = "expiry_fallback_v1"
    const val LISTING_CACHE_FILE = "listing_cache.json"
    const val LISTING_MAX_BODY_BYTES = 8 * 1024 * 1024       // UNPROVEN size of the unfiltered listing; bounded
    const val CHAIN_MAX_BODY_BYTES = 2 * 1024 * 1024
    const val LISTING_MAX_FETCHES_PER_SESSION = 3
    const val LISTING_RETRY_MS = 10 * 60_000L
    const val CHAIN_MAX_FETCHES_PER_SESSION = 80             // per index; an expiry-day session has <= ~75 polls
    const val CHAIN_COUNT_FILE = "chain_counts.json"
    const val STATUS_REQUIRED = "EVIDENCE_REQUIRED"
    private const val BASE = "https://api.upstox.com"

    data class Need(val index: String, val decisionId: String, val tradeExpiry: String, val spot: Double)

    /** Per-index request and byte counters for one acquisition. */
    class Stats {
        val requests = linkedMapOf("listing" to 0, "contracts" to 0, "chain" to 0)
        val bytes = linkedMapOf("listing" to 0L, "contracts" to 0L, "chain" to 0L)
        fun toJson(): JSONObject = JSONObject().put("requests", JSONObject(requests as Map<*, *>))
            .put("response_bytes", JSONObject(bytes as Map<*, *>))
    }

    /** Counts every call made through it under one label. */
    class CountingTransport(private val inner: ManagedReadAdapters.Transport, private val stats: Stats,
                            private val label: String) : ManagedReadAdapters.Transport {
        override fun call(method: String, url: String, body: String?): ManagedReadAdapters.Response {
            stats.requests[label] = (stats.requests[label] ?: 0) + 1
            val r = inner.call(method, url, body)
            stats.bytes[label] = (stats.bytes[label] ?: 0L) + (r.body?.toByteArray(Charsets.UTF_8)?.size ?: 0)
            return r
        }
    }

    private fun enc(s: String) = URLEncoder.encode(s, "UTF-8")

    /** Indices the result asks evidence for; decision id must match the plan's and the decision's. */
    fun needs(result: JSONObject): List<Need> {
        val sl = result.optJSONObject("single_leg_by_index") ?: return emptyList()
        val byIndex = result.optJSONObject("by_index")
        val out = mutableListOf<Need>()
        for (ix in listOf("NF", "BNF")) {
            val p = sl.optJSONObject(ix) ?: continue
            val fb = p.optJSONObject("expiry_fallback") ?: continue
            if (fb.optString("status") != STATUS_REQUIRED) continue
            val dec = byIndex?.optJSONObject(ix) ?: continue
            val did = dec.optString("decision_id", "")
            if (did.isEmpty() || did != fb.optString("decision_id") || did != p.optString("decision_id")) continue
            val spot = dec.optJSONObject("direction")?.optJSONObject("inputs")?.optJSONObject("spot")
                ?.optDouble("value", Double.NaN) ?: Double.NaN
            if (!spot.isFinite() || spot <= 0.0) continue
            out.add(Need(ix, did, fb.optString("trade_expiry"), spot))
        }
        return out
    }

    private fun readJson(f: File): JSONObject = try { JSONObject(f.readText()) } catch (e: Exception) { JSONObject() }

    private fun save(f: File, obj: JSONObject) {
        try {
            f.parentFile?.mkdirs()
            val tmp = File(f.parentFile, f.name + ".tmp")
            tmp.writeText(obj.toString())
            tmp.renameTo(f)
        } catch (e: Exception) { /* optional cache */ }
    }

    /** Distinct listed expiries for (index, session); cached; bounded retries. Null when unavailable. */
    fun listing(dir: File, index: String, session: String, transport: ManagedReadAdapters.Transport,
                clocks: ManagedReadAdapters.Clocks): JSONObject? {
        val underlying = ManagedReadAdapters.UNDERLYING[index] ?: return null
        val f = File(dir, LISTING_CACHE_FILE)
        val cache = readJson(f)
        val pruned = JSONObject()
        cache.keys().forEach { k -> if (k.endsWith("|$session")) pruned.put(k, cache.get(k)) }
        val key = "$index|$session"
        val entry = pruned.optJSONObject(key) ?: JSONObject().put("fetches", 0)
        if (entry.optJSONArray("expiries") != null) { save(f, pruned); return entry }
        val now = clocks.wallMs()
        if (entry.optInt("fetches", 0) >= LISTING_MAX_FETCHES_PER_SESSION) { save(f, pruned); return null }
        if (entry.has("last_attempt_ms") && now - entry.optLong("last_attempt_ms") < LISTING_RETRY_MS) { save(f, pruned); return null }
        val start = clocks.wallMs()
        val r = try { transport.call("GET", "$BASE/v2/option/contract?instrument_key=${enc(underlying)}", null) }
                catch (e: Exception) { ManagedReadAdapters.Response(-1, null) }
        val receipt = clocks.wallMs()
        entry.put("fetches", entry.optInt("fetches", 0) + 1).put("last_attempt_ms", now).put("last_code", r.code)
        val body = r.body?.takeIf { it.length <= LISTING_MAX_BODY_BYTES }
        val parsed = try { if (r.code == 200 && body != null) JSONObject(body) else null } catch (e: Exception) { null }
        val rows = parsed?.takeIf { it.optString("status") == "success" }?.optJSONArray("data")
        if (rows != null) {
            val set = sortedSetOf<String>()
            var foreign = 0
            for (i in 0 until rows.length()) {
                val row = rows.optJSONObject(i) ?: continue
                if (row.optString("underlying_key") != underlying) { foreign++; continue }
                val e = row.optString("expiry", "")
                if (e.length >= 10) set.add(e.substring(0, 10))
            }
            if (set.isNotEmpty()) {
                entry.put("index", index).put("session", session).put("expiries", JSONArray(set.toList()))
                    .put("request_start_ms", start).put("receipt_ms", receipt).put("rows", rows.length())
                    .put("foreign_rows", foreign).put("source", "upstox_v2_option_contract_listing")
            }
        }
        pruned.put(key, entry)
        save(f, pruned)
        return if (entry.has("expiries")) entry else null
    }

    private fun chainBudgetOk(dir: File, index: String, session: String): Boolean {
        val f = File(dir, CHAIN_COUNT_FILE)
        val c = readJson(f)
        val key = "$index|$session"
        val n = c.optInt(key, 0)
        if (n >= CHAIN_MAX_FETCHES_PER_SESSION) return false
        val pruned = JSONObject()
        c.keys().forEach { k -> if (k.endsWith("|$session")) pruned.put(k, c.get(k)) }
        save(f, pruned.put(key, n + 1))
        return true
    }

    /** The later-expiry chain, formatted + stamped; null on any failure. */
    fun chain(index: String, expiry: String, spot: Double, transport: ManagedReadAdapters.Transport,
              clocks: ManagedReadAdapters.Clocks, format: (JSONObject, Double) -> JSONObject?): JSONObject? {
        val underlying = ManagedReadAdapters.UNDERLYING[index] ?: return null
        val start = clocks.wallMs()
        val r = try {
            transport.call("GET", "$BASE/v2/option/chain?instrument_key=${enc(underlying)}&expiry_date=${enc(expiry)}", null)
        } catch (e: Exception) { return null }
        val receipt = clocks.wallMs()
        val body = r.body ?: return null
        if (r.code != 200 || body.length > CHAIN_MAX_BODY_BYTES) return null
        val raw = try { JSONObject(body) } catch (e: Exception) { return null }
        if (raw.optString("status") != "success") return null
        val formatted = format(raw, spot) ?: return null
        if ((formatted.optJSONObject("strikes")?.length() ?: 0) == 0) return null
        OptionChainBrainFormatter.stampRequest(formatted, expiry, underlying)
        return JSONObject().put("index", index).put("expiry", expiry).put("chain", formatted)
            .put("request_start_ms", start).put("receipt_ms", receipt)
    }

    /**
     * Acquire the evidence for every need. `transportFor(label, maxBytes)` builds the (counted) transport;
     * `resolve(index, listingJson, cutoffMs)` is the Python resolver. Returns {index: {listing, contracts?, chain?,
     * resolution, stats}}; an index without a usable listing still reports its counts (Python then WAITs).
     */
    fun acquire(needs: List<Need>, slDir: File, session: String,
                transportFor: (Int) -> ManagedReadAdapters.Transport, clocks: ManagedReadAdapters.Clocks,
                resolve: (String, String, Long) -> String, format: (JSONObject, Double) -> JSONObject?): JSONObject {
        val dir = File(slDir, DIR)
        val out = JSONObject()
        for (n in needs) {
            val stats = Stats()
            val e = JSONObject()
            val li = listing(dir, n.index, session, CountingTransport(transportFor(LISTING_MAX_BODY_BYTES), stats, "listing"), clocks)
            if (li != null) {
                e.put("listing", li)
                val res = try { JSONObject(resolve(n.index, li.toString(), clocks.wallMs())) } catch (x: Exception) { JSONObject() }
                e.put("resolution", res)
                val fx = res.optString("expiry", "")
                if (fx.length == 10 && !res.isNull("expiry")) {
                    ManagedReadAdapters.ensureContracts(dir, n.index, fx, session,
                        CountingTransport(transportFor(ManagedReadAdapters.MAX_BODY_BYTES), stats, "contracts"), clocks)?.let { c ->
                        e.put("contracts", JSONObject().put("index", n.index).put("expiry", fx)
                            .put("response", c.getJSONObject("response")).put("fetched_at_ms", c.optLong("fetched_at_ms")))
                    }
                    if (e.has("contracts") && chainBudgetOk(dir, n.index, session)) {
                        chain(n.index, fx, n.spot, CountingTransport(transportFor(CHAIN_MAX_BODY_BYTES), stats, "chain"),
                            clocks, format)?.let { e.put("chain", it) }
                    }
                }
            }
            e.put("stats", stats.toJson())
            out.put(n.index, e)
        }
        return out
    }

    /** Apply the Python patch to the result BEFORE publication: replacement plans and matching why records. */
    fun applyPatch(result: JSONObject, patch: JSONObject): Int {
        if (patch.has("error")) return 0
        val plans = patch.optJSONObject("single_leg_by_index") ?: return 0
        val sl = result.optJSONObject("single_leg_by_index") ?: return 0
        var n = 0
        plans.keys().forEach { ix -> sl.put(ix, plans.getJSONObject(ix)); n++ }
        val why = patch.optJSONObject("why")
        val target = result.optJSONObject("index_authority_persist")?.optJSONObject("delta")?.optJSONObject("why")
        if (why != null && target != null) why.keys().forEach { did -> if (target.has(did)) target.put(did, why.get(did)) }
        return n
    }

    /** Small log line: per index status/reason, request counts, response and retained bytes (no prices). */
    fun logLine(evidence: JSONObject, patch: JSONObject): String {
        val parts = mutableListOf<String>()
        evidence.keys().forEach { ix ->
            val st = evidence.optJSONObject(ix)?.optJSONObject("stats")
            val fb = patch.optJSONObject("single_leg_by_index")?.optJSONObject(ix)?.optJSONObject("expiry_fallback")
            val ret = patch.optJSONObject("retention")?.optJSONObject(ix)
            parts.add("$ix status=${fb?.optString("status")} reason=${fb?.optString("reason")} " +
                "exp=${fb?.optString("fallback_expiry")} req=${st?.optJSONObject("requests")} " +
                "bytes=${st?.optJSONObject("response_bytes")} retained=${ret?.optLong("index_bytes")}")
        }
        return parts.joinToString(" | ")
    }
}
