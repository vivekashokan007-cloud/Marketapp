package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/**
 * Delivery B R7 (Codex R6 review s2/s6): BOUNDED read adapters for the managed single-leg Paper flow only.
 * Read-only broker calls; never an order. Responses are handed raw to Python (managed_evidence_v1 /
 * paper_broker_snapshot_v1), which parses and validates them. X1 never sees any of this.
 *
 *  - contracts: GET /v2/option/contract once per (index, expiry, session); cached on disk; invalidated when the
 *    session or expiry changes or the response is not a success; failed fetches retried at most every
 *    CONTRACT_RETRY_MS and at most CONTRACT_MAX_FETCHES_PER_SESSION per index/expiry/session.
 *  - quotes: GET /v3/market-quote/quotes for EXACT instrument keys only (<= MAX_QUOTE_KEYS per call), recording
 *    request-start wall time, receipt wall time and receipt monotonic time from injected clocks.
 *  - margin: POST /v2/charges/margin only for SELL, one instrument, explicit product "I" and exact quantity.
 * Network access is behind [Transport] so tests use captured/redacted fixture shapes and deterministic clocks.
 */
object ManagedReadAdapters {
    const val MAX_QUOTE_KEYS = 4
    const val CONTRACT_RETRY_MS = 10 * 60_000L
    const val CONTRACT_MAX_FETCHES_PER_SESSION = 3
    const val CONNECT_TIMEOUT_MS = 4_000
    const val READ_TIMEOUT_MS = 5_000
    const val MAX_BODY_BYTES = 512 * 1024
    const val CONTRACT_CACHE_FILE = "contracts_cache.json"
    const val SHORT_PRODUCT = "I"
    private const val BASE = "https://api.upstox.com"
    val UNDERLYING = mapOf("NF" to "NSE_INDEX|Nifty 50", "BNF" to "NSE_INDEX|Nifty Bank")

    data class Response(val code: Int, val body: String?)
    fun interface Transport { fun call(method: String, url: String, body: String?): Response }
    data class Clocks(val wallMs: () -> Long, val monoMs: () -> Long)
    data class QuoteFetch(val requestedJson: String, val body: String, val requestStartMs: Long,
                          val receiptMs: Long, val receiptMonoMs: Long)
    data class MarginFetch(val requestJson: String, val body: String, val receiptMs: Long)

    private fun enc(s: String) = URLEncoder.encode(s, "UTF-8")

    // ── contracts (cached per session/expiry) ──────────────────────────────────────────────────────────
    fun contractCacheKey(index: String, expiry: String, session: String) = "$index|$expiry|$session"

    /** Returns the cached or freshly fetched success response for (index, expiry, session), or null. */
    fun ensureContracts(dir: File, index: String, expiry: String, session: String, transport: Transport,
                        clocks: Clocks): JSONObject? {
        val underlying = UNDERLYING[index] ?: return null
        if (expiry.length < 10 || session.length < 10) return null
        val f = File(dir, CONTRACT_CACHE_FILE)
        val cache = try { JSONObject(f.readText()) } catch (e: Exception) { JSONObject() }
        // invalidation: drop every entry of another session, and other expiries of this index
        val pruned = JSONObject()
        cache.keys().forEach { k ->
            val parts = k.split("|")
            if (parts.size == 3 && parts[2] == session && !(parts[0] == index && parts[1] != expiry)) pruned.put(k, cache.get(k))
        }
        val key = contractCacheKey(index, expiry, session)
        val entry = pruned.optJSONObject(key) ?: JSONObject().put("fetches", 0)
        entry.optJSONObject("response")?.let { if (it.optString("status") == "success") { save(f, pruned); return entry } }
        val now = clocks.wallMs()
        if (entry.optInt("fetches", 0) >= CONTRACT_MAX_FETCHES_PER_SESSION) { save(f, pruned); return null }
        if (now - entry.optLong("last_attempt_ms", 0L) < CONTRACT_RETRY_MS && entry.has("last_attempt_ms")) { save(f, pruned); return null }
        val url = "$BASE/v2/option/contract?instrument_key=${enc(underlying)}&expiry_date=${enc(expiry)}"
        val r = try { transport.call("GET", url, null) } catch (e: Exception) { Response(-1, null) }
        entry.put("fetches", entry.optInt("fetches", 0) + 1).put("last_attempt_ms", now).put("last_code", r.code)
        val body = r.body?.takeIf { it.length <= MAX_BODY_BYTES }
        val parsed = try { if (r.code == 200 && body != null) JSONObject(body) else null } catch (e: Exception) { null }
        if (parsed != null && parsed.optString("status") == "success") {
            entry.put("response", parsed).put("fetched_at_ms", clocks.wallMs())
        } else {
            entry.remove("response")
        }
        pruned.put(key, entry)
        save(f, pruned)
        return if (entry.has("response")) entry else null
    }

    private fun save(f: File, obj: JSONObject) {
        try {
            f.parentFile?.mkdirs()
            val tmp = File(f.parentFile, f.name + ".tmp")
            tmp.writeText(obj.toString())
            tmp.renameTo(f)
        } catch (e: Exception) { /* cache is optional; missing cache -> named unavailable in Python */ }
    }

    // ── quotes: exact instruments only ─────────────────────────────────────────────────────────────────
    fun quoteUrl(keys: List<String>) = "$BASE/v3/market-quote/quotes?instrument_key=" + keys.joinToString(",") { enc(it) }

    fun fetchQuotes(keys: List<String>, transport: Transport, clocks: Clocks): QuoteFetch? {
        val exact = keys.filter { it.isNotBlank() }.distinct().take(MAX_QUOTE_KEYS)
        if (exact.isEmpty()) return null
        val start = clocks.wallMs()
        val r = try { transport.call("GET", quoteUrl(exact), null) } catch (e: Exception) { return null }
        val receipt = clocks.wallMs()
        val mono = clocks.monoMs()
        val body = r.body ?: return null
        if (r.code != 200 || body.length > MAX_BODY_BYTES) return null
        return QuoteFetch(JSONArray(exact).toString(), body, start, receipt, mono)
    }

    // ── margin: SELL only, explicit product and request identity ───────────────────────────────────────
    fun marginRequest(instrumentKey: String, quantity: Int): JSONObject = JSONObject().put("instruments", JSONArray().put(
        JSONObject().put("instrument_key", instrumentKey).put("quantity", quantity).put("product", SHORT_PRODUCT)
            .put("transaction_type", "SELL")))

    fun fetchShortMargin(action: String, instrumentKey: String, quantity: Int, transport: Transport, clocks: Clocks): MarginFetch? {
        if (!action.startsWith("SELL") || instrumentKey.isBlank() || quantity <= 0) return null   // BUY never needs margin
        val req = marginRequest(instrumentKey, quantity).toString()
        val r = try { transport.call("POST", "$BASE/v2/charges/margin", req) } catch (e: Exception) { return null }
        val receipt = clocks.wallMs()
        val body = r.body ?: return null
        if (r.code != 200 || body.length > MAX_BODY_BYTES) return null
        return MarginFetch(req, body, receipt)
    }

    /** Production transport: bounded timeouts, bearer token per request, never logged. */
    class HttpTransport(private val token: () -> String) : Transport {
        override fun call(method: String, url: String, body: String?): Response {
            val c = URL(url).openConnection() as HttpURLConnection
            try {
                c.requestMethod = method
                c.connectTimeout = CONNECT_TIMEOUT_MS
                c.readTimeout = READ_TIMEOUT_MS
                c.setRequestProperty("Accept", "application/json")
                c.setRequestProperty("Authorization", "Bearer ${token()}")
                if (body != null) {
                    c.doOutput = true
                    c.setRequestProperty("Content-Type", "application/json")
                    c.outputStream.use { it.write(body.toByteArray()) }
                }
                val code = c.responseCode
                val stream = if (code in 200..299) c.inputStream else c.errorStream
                val text = stream?.use { s ->
                    val buf = s.readNBytesCompat(MAX_BODY_BYTES + 1)
                    String(buf)
                }
                return Response(code, text)
            } finally {
                c.disconnect()
            }
        }
    }

    private fun java.io.InputStream.readNBytesCompat(n: Int): ByteArray {
        val out = java.io.ByteArrayOutputStream()
        val b = ByteArray(8192)
        while (out.size() < n) {
            val r = read(b, 0, minOf(b.size, n - out.size()))
            if (r < 0) break
            out.write(b, 0, r)
        }
        return out.toByteArray()
    }
}

/** Coalesces queued single-leg monitor work: at most one pending run; the run reads its own clock. */
class MonitorCoalescer {
    private val pending = java.util.concurrent.atomic.AtomicBoolean(false)
    @Volatile var latestSpots: String = "{}"
        private set
    /** Returns true when the caller must enqueue a run (none pending); otherwise the queued run picks up the latest. */
    fun offer(spotsJson: String): Boolean { latestSpots = spotsJson; return pending.compareAndSet(false, true) }
    /** Called by the run when it starts executing; later offers enqueue a new run. */
    fun begin(): String { pending.set(false); return latestSpots }
}
