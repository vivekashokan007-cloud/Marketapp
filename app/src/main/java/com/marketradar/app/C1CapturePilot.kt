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
 * C1 exact-contract Paper capture pilot (research only; opt-in, default OFF).  R1.
 *
 * On each PUBLISHED poll a compact slice of the Brain result (single_leg_by_index + per-index decision id /
 * analysis time + publication time; CPU-only JSON on the publication thread, no disk, no network) is queued in
 * a bounded FIFO.  Every queued slice reaches Python (brain.c1_capture_plan -> c1_capture_pilot_v1), which
 * registers each proposal identity BEFORE any cap or coalescing; an overflow is counted and reported so the
 * slice is marked incomplete.  This class then performs exactly the planned read-only requests through the
 * existing exact-instrument V3 quote and SELL margin endpoints ([ManagedReadAdapters]) and hands every
 * outcome (success, failure or SKIPPED:<gate reason>) back to Python (brain.c1_capture_record).
 *
 * Gate ([gate], null = allowed): candidate build AND explicit opt-in AND active Paper mode.  It is rechecked
 * at offer, at worker start, and before EVERY quote attempt, margin attempt and retry (also after a queued
 * delay).  Denied -> zero new requests; a response already in flight is recorded with its own timestamps; the
 * remaining planned requests get explicit SKIPPED outcomes with the attempts actually made.  Work queued while
 * enabled is discarded (counted) when the gate closes, so re-enabling starts fresh.
 *
 * Isolation: no admission call, no single_leg_v1 write (the executable quote cache), no order endpoint, no
 * re-arm of a fetch Python has fenced; an identical fetch id is never executed twice.  Pure Kotlin (no Android
 * types) for JVM tests.
 */
class C1CapturePilot(
    private val gate: () -> String?,
    private val transport: () -> ManagedReadAdapters.Transport?,
    private val clocks: ManagedReadAdapters.Clocks,
    private val plan: (slicesJson: String, nowMs: Long, dropped: Int, discarded: Int) -> String?,
    private val record: (fetchId: String, index: String, session: String, callJson: String) -> String?,
    private val executor: Executor,
) {
    companion object {
        const val MAX_ATTEMPTS = 2
        const val MAX_QUEUE = 32
        const val SKIPPED = "SKIPPED:"
    }

    private val pending = AtomicBoolean(false)
    private val queue = ArrayDeque<String>()
    private var dropped = 0
    private var discarded = 0
    private val inFlight: MutableSet<String> = Collections.synchronizedSet(HashSet())
    @Volatile var lastRun: String = "{}"
        private set

    /** Queue one publication slice.  Returns false (and does nothing at all) while the gate is closed. */
    fun offer(sliceJson: String): Boolean {
        if (gate() != null) return false
        synchronized(queue) {
            if (queue.size >= MAX_QUEUE) { queue.removeFirst(); dropped++ }   // counted -> slice INCOMPLETE
            queue.addLast(sliceJson)
        }
        if (pending.compareAndSet(false, true)) executor.execute { runOnce() }
        return true
    }

    internal fun queued(): Int = synchronized(queue) { queue.size }

    internal fun runOnce() {
        pending.set(false)
        val slices = JSONArray()
        var nDropped: Int
        var nDiscarded: Int
        synchronized(queue) {
            if (gate() != null) {                       // disabled / left Paper while queued: discard, start fresh
                discarded += queue.size; queue.clear()
                lastRun = JSONObject().put("status", "GATE_CLOSED").put("reason", gate()).toString()
                return
            }
            while (queue.isNotEmpty()) slices.put(JSONObject(queue.removeFirst()))
            nDropped = dropped; dropped = 0
            nDiscarded = discarded; discarded = 0
        }
        if (slices.length() == 0 && nDropped == 0) return
        // plan first: the denominator is registered even when no request can be sent
        val p = try { JSONObject(plan(slices.toString(), clocks.wallMs(), nDropped, nDiscarded) ?: return) } catch (e: Exception) { return }
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
                val g = gate()
                val call = when {
                    g != null -> skipped(f, SKIPPED + g)
                    tr == null -> skipped(f, "NO_TRANSPORT")
                    else -> execute(f, tr)
                }
                val out = record(id, f.optString("index", ""), session, call.toString())
                outcomes.put(out ?: "TIMEOUT")
            } finally {
                inFlight.remove(id)
            }
        }
        lastRun = JSONObject().put("status", p.optString("status")).put("fetches", fetches.length())
            .put("budget_exhausted", p.optJSONArray("budget_exhausted") ?: JSONArray()).put("outcomes", outcomes).toString()
    }

    /** Nothing sent: an explicit outcome for every planned request (0 attempts). */
    internal fun skipped(f: JSONObject, reason: String): JSONObject {
        val margins = JSONArray()
        val ma = f.optJSONArray("margins") ?: JSONArray()
        for (i in 0 until ma.length()) margins.put(JSONObject().put("target_id", ma.getJSONObject(i).optString("target_id"))
            .put("attempts", 0).put("failure", reason))
        return JSONObject().put("attempts", 0).put("requested", f.optJSONArray("keys") ?: JSONArray())
            .put("quote_failure", reason).put("margins", margins)
    }

    internal fun execute(f: JSONObject, tr: ManagedReadAdapters.Transport): JSONObject {
        val ka = f.optJSONArray("keys") ?: JSONArray()
        val keys = (0 until ka.length()).map { ka.getString(it) }
        val maxAttempts = f.optInt("max_attempts", 1).coerceIn(1, MAX_ATTEMPTS)
        var attempts = 0
        var q: ManagedReadAdapters.QuoteOutcome? = null
        var skip: String? = null
        while (true) {
            val g = gate()
            if (g != null) { skip = SKIPPED + g; break }                  // before the first try AND each retry
            attempts++
            q = ManagedReadAdapters.fetchQuotesOutcome(keys, tr, clocks)
            if (q.fetch != null || !q.retryable || attempts >= maxAttempts) break
        }
        val fetched = q?.fetch
        val requested = fetched?.let { JSONArray(it.requestedJson) } ?: JSONArray(keys.filter { it.isNotBlank() }.distinct().take(ManagedReadAdapters.MAX_QUOTE_KEYS))
        val failure: String? = if (fetched != null) null else (skip ?: q?.failure)
        val call = JSONObject().put("attempts", attempts).put("requested", requested)
            .put("quote_failure", failure ?: JSONObject.NULL)
        fetched?.let {   // an in-flight response is always recorded with its own timestamps
            call.put("quote", JSONObject().put("body", it.body).put("request_start_ms", it.requestStartMs)
                .put("receipt_ms", it.receiptMs).put("receipt_mono_ms", it.receiptMonoMs))
        }
        val margins = JSONArray()
        val ma = f.optJSONArray("margins") ?: JSONArray()
        for (i in 0 until ma.length()) {
            val m = ma.getJSONObject(i)
            var n = 0
            var r: ManagedReadAdapters.MarginOutcome? = null
            var mskip: String? = skip            // gate already closed during the quote: send nothing more
            while (mskip == null) {
                val g = gate()
                if (g != null) { mskip = SKIPPED + g; break }             // between quote and margin, and each retry
                n++
                r = ManagedReadAdapters.fetchShortMarginOutcome("SELL", m.optString("instrument_key"),
                    m.optInt("quantity", 0), tr, clocks)
                if (r.fetch != null || !r.retryable || n >= maxAttempts) break
            }
            val mf = r?.fetch
            val mfail: String? = if (mf != null) null else (mskip ?: r?.failure)
            margins.put(JSONObject().put("target_id", m.optString("target_id")).put("attempts", n)
                .put("request", mf?.requestJson ?: JSONObject.NULL).put("body", mf?.body ?: JSONObject.NULL)
                .put("receipt_ms", mf?.receiptMs ?: JSONObject.NULL).put("failure", mfail ?: JSONObject.NULL))
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

    /** null = allowed.  Candidate build AND explicit opt-in AND active Paper mode (same check as admission). */
    fun gateReason(candidate: Boolean, enabled: Boolean, executionMode: String?): String? = when {
        !candidate -> "NOT_CANDIDATE_BUILD"
        !enabled -> "PILOT_DISABLED"
        executionMode != "paper" -> "NOT_PAPER_MODE"
        else -> null
    }

    fun gate(prefs: android.content.SharedPreferences): String? = gateReason(BuildConfig.PWA_CANDIDATE,
        prefs.getBoolean(PREF_ENABLED, false), try { EvAcceptance.executionMode(prefs) } catch (e: Exception) { null })

    /** Compact publication slice: CPU-only JSON (no disk, no network) for the publication thread. */
    fun slice(resultObj: JSONObject, publishedMs: Long): String {
        val byIx = JSONObject()
        val src = resultObj.optJSONObject("by_index")
        for (ix in listOf("NF", "BNF")) {
            val d = src?.optJSONObject(ix) ?: continue
            byIx.put(ix, JSONObject().put("decision_id", d.opt("decision_id") ?: JSONObject.NULL)
                .put("analysis_ts", d.opt("analysis_ts") ?: JSONObject.NULL)
                .put("decision_poll_ts", d.opt("decision_poll_ts") ?: JSONObject.NULL))
        }
        return JSONObject().put("published_ms", publishedMs)
            .put("single_leg_by_index", resultObj.opt("single_leg_by_index") ?: JSONObject.NULL)
            .put("by_index", byIx).toString()
    }

    private val executor = Executors.newSingleThreadExecutor { r -> Thread(r, "c1-capture-pilot").apply { isDaemon = true } }
    @Volatile private var pilot: C1CapturePilot? = null

    fun offer(filesDir: File, prefs: android.content.SharedPreferences, resultObj: JSONObject, publishedMs: Long) {
        if (gate(prefs) != null) return
        val p = pilot ?: synchronized(this) { pilot ?: build(filesDir, prefs).also { pilot = it } }
        p.offer(slice(resultObj, publishedMs))
    }

    private fun build(filesDir: File, prefs: android.content.SharedPreferences): C1CapturePilot {
        val dir = dir(filesDir).absolutePath
        val sl = IndexAuthorityContext.singleLegDir(filesDir).absolutePath
        fun brain() = com.chaquo.python.Python.getInstance().getModule("brain")
        return C1CapturePilot(
            gate = { gate(prefs) },
            transport = {
                val tok = (prefs.getString("auth_token", "") ?: "").trim()
                if (tok.isNotEmpty()) ManagedReadAdapters.HttpTransport { (prefs.getString("auth_token", "") ?: "").trim() } else null
            },
            clocks = ManagedReadAdapters.Clocks({ System.currentTimeMillis() }, { android.os.SystemClock.elapsedRealtime() }),
            plan = { res, now, dropped, discarded ->
                try { brain().callAttr("c1_capture_plan", dir, res, now, true, sl, dropped, discarded).toString() } catch (e: Exception) {
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
