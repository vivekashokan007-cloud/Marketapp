package com.marketradar.app

import com.marketradar.app.util.LogBuffer
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.Collections
import java.util.concurrent.Executor
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * C1 exact-contract Paper capture pilot (research only; opt-in, default OFF).
 *
 * On each PUBLISHED poll the latest Brain result is offered.  Python (brain.c1_capture_plan ->
 * c1_capture_pilot_v1) registers the denominator and returns a bounded fetch plan; this class performs exactly
 * those read-only requests through the existing exact-instrument V3 quote and SELL margin endpoints
 * ([ManagedReadAdapters]) and hands every outcome (success or an explicit failure) back to Python
 * (brain.c1_capture_record), which validates it with the released paper_broker_snapshot_v1 / margin_evidence_ok.
 *
 * Isolation: no admission call, no single_leg_v1 write (the executable quote cache), no order endpoint, no
 * re-arm of a fetch Python has fenced.  Queued work is coalesced (one pending run, latest result wins); an
 * identical fetch id is never executed twice concurrently.  Pure Kotlin (no Android types) for JVM tests.
 */
class C1CapturePilot(
    private val enabled: () -> Boolean,
    private val transport: () -> ManagedReadAdapters.Transport?,
    private val clocks: ManagedReadAdapters.Clocks,
    private val plan: (resultJson: String, nowMs: Long) -> String?,
    private val record: (fetchId: String, index: String, session: String, callJson: String) -> String?,
    private val executor: Executor,
) {
    companion object { const val MAX_ATTEMPTS = 2 }

    private val pending = AtomicBoolean(false)
    @Volatile private var latest: String = "null"
    private val inFlight: MutableSet<String> = Collections.synchronizedSet(HashSet())
    @Volatile var lastRun: String = "{}"
        private set

    /** Returns false (and does nothing at all) while the pilot is disabled. */
    fun offer(resultJson: String): Boolean {
        if (!enabled()) return false
        latest = resultJson
        if (pending.compareAndSet(false, true)) executor.execute { runOnce() }
        return true
    }

    internal fun runOnce() {
        pending.set(false)
        val res = latest
        if (!enabled()) return
        // plan first: the denominator is registered even when no request can be sent
        val p = try { JSONObject(plan(res, clocks.wallMs()) ?: return) } catch (e: Exception) { return }
        val tr = transport()
        val session = p.optString("session_date", "")
        val fetches = p.optJSONArray("fetches") ?: JSONArray()
        val outcomes = JSONArray()
        val seen = HashSet<String>()
        for (i in 0 until fetches.length()) {
            val f = fetches.optJSONObject(i) ?: continue
            val id = f.optString("fetch_id", "")
            if (id.isEmpty() || !seen.add(id) || !inFlight.add(id)) continue   // once per run, never concurrently
            try {
                val call = if (tr != null) execute(f, tr) else noTransport(f)
                val out = record(id, f.optString("index", ""), session, call.toString())
                outcomes.put(out ?: "TIMEOUT")
            } finally {
                inFlight.remove(id)
            }
        }
        lastRun = JSONObject().put("status", p.optString("status")).put("fetches", fetches.length())
            .put("budget_exhausted", p.optJSONArray("budget_exhausted") ?: JSONArray()).put("outcomes", outcomes).toString()
    }

    /** No token/transport: an explicit failure for every planned request (0 requests sent). */
    internal fun noTransport(f: JSONObject): JSONObject {
        val margins = JSONArray()
        val ma = f.optJSONArray("margins") ?: JSONArray()
        for (i in 0 until ma.length()) margins.put(JSONObject().put("target_id", ma.getJSONObject(i).optString("target_id"))
            .put("attempts", 0).put("failure", "NO_TRANSPORT"))
        return JSONObject().put("attempts", 0).put("requested", f.optJSONArray("keys") ?: JSONArray())
            .put("quote_failure", "NO_TRANSPORT").put("margins", margins)
    }

    internal fun execute(f: JSONObject, tr: ManagedReadAdapters.Transport): JSONObject {
        val ka = f.optJSONArray("keys") ?: JSONArray()
        val keys = (0 until ka.length()).map { ka.getString(it) }
        val maxAttempts = f.optInt("max_attempts", 1).coerceIn(1, MAX_ATTEMPTS)
        var attempts = 0
        var q: ManagedReadAdapters.QuoteOutcome
        do {
            attempts++
            q = ManagedReadAdapters.fetchQuotesOutcome(keys, tr, clocks)
        } while (q.fetch == null && q.retryable && attempts < maxAttempts)
        val requested = q.fetch?.let { JSONArray(it.requestedJson) } ?: JSONArray(keys.filter { it.isNotBlank() }.distinct().take(ManagedReadAdapters.MAX_QUOTE_KEYS))
        val call = JSONObject().put("attempts", attempts).put("requested", requested)
            .put("quote_failure", q.failure ?: JSONObject.NULL)
        q.fetch?.let {
            call.put("quote", JSONObject().put("body", it.body).put("request_start_ms", it.requestStartMs)
                .put("receipt_ms", it.receiptMs).put("receipt_mono_ms", it.receiptMonoMs))
        }
        val margins = JSONArray()
        val ma = f.optJSONArray("margins") ?: JSONArray()
        for (i in 0 until ma.length()) {
            val m = ma.getJSONObject(i)
            var n = 0
            var r: ManagedReadAdapters.MarginOutcome
            do {
                n++
                r = ManagedReadAdapters.fetchShortMarginOutcome("SELL", m.optString("instrument_key"),
                    m.optInt("quantity", 0), tr, clocks)
            } while (r.fetch == null && r.retryable && n < maxAttempts)
            margins.put(JSONObject().put("target_id", m.optString("target_id")).put("attempts", n)
                .put("request", r.fetch?.requestJson ?: JSONObject.NULL).put("body", r.fetch?.body ?: JSONObject.NULL)
                .put("receipt_ms", r.fetch?.receiptMs ?: JSONObject.NULL).put("failure", r.failure ?: JSONObject.NULL))
        }
        return call.put("margins", margins)
    }
}

/** Android host: one pilot per process on its own serialized worker (never the admission executor). */
object C1PilotIo {
    private const val TAG = "C1PilotIo"
    const val DIR_NAME = "c1_capture_v1"
    const val PREF_ENABLED = "c1_capture_pilot_enabled_v1"      // default OFF; explicit opt-in

    fun dir(filesDir: File) = File(filesDir, DIR_NAME)

    private val executor = Executors.newSingleThreadExecutor { r -> Thread(r, "c1-capture-pilot").apply { isDaemon = true } }
    @Volatile private var pilot: C1CapturePilot? = null

    fun offer(filesDir: File, prefs: android.content.SharedPreferences, resultJson: String) {
        if (!prefs.getBoolean(PREF_ENABLED, false)) return
        val p = pilot ?: synchronized(this) { pilot ?: build(filesDir, prefs).also { pilot = it } }
        p.offer(resultJson)
    }

    private fun build(filesDir: File, prefs: android.content.SharedPreferences): C1CapturePilot {
        val dir = dir(filesDir).absolutePath
        val sl = IndexAuthorityContext.singleLegDir(filesDir).absolutePath
        fun brain() = com.chaquo.python.Python.getInstance().getModule("brain")
        return C1CapturePilot(
            enabled = { prefs.getBoolean(PREF_ENABLED, false) },
            transport = {
                val tok = (prefs.getString("auth_token", "") ?: "").trim()
                if (tok.isNotEmpty()) ManagedReadAdapters.HttpTransport { (prefs.getString("auth_token", "") ?: "").trim() } else null
            },
            clocks = ManagedReadAdapters.Clocks({ System.currentTimeMillis() }, { android.os.SystemClock.elapsedRealtime() }),
            plan = { res, now ->
                try { brain().callAttr("c1_capture_plan", dir, res, now, true, sl).toString() } catch (e: Exception) {
                    LogBuffer.add('W', TAG, "C1_PLAN_FAIL: ${e.javaClass.simpleName}"); null
                }
            },
            record = { id, ix, session, call ->
                try { brain().callAttr("c1_capture_record", dir, id, ix, session, call).toString() } catch (e: Exception) {
                    LogBuffer.add('W', TAG, "C1_RECORD_FAIL: ${e.javaClass.simpleName}"); null
                }
            },
            executor = executor,
        )
    }
}
