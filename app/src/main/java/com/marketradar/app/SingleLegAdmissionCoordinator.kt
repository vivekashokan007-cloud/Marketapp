package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.util.LinkedHashMap
import java.util.UUID
import java.util.concurrent.Executor
import java.util.concurrent.Executors

/**
 * Codex R7 review s2: explicit single-leg admission off the UI thread.
 *
 *  - every tap gets a stable request id; the JS page gets PENDING immediately and polls status(id);
 *  - the state copied at tap time is RE-READ after the quote/margin fetch (decision identity, opt-in, mode,
 *    selector, open managed exposure, capital/risk and the config revision); any change rejects with
 *    STATE_CHANGED_DURING_FETCH:<field> so an old proposal is never filled against a new quote;
 *  - Python receives a StateProbe and re-reads the same state again UNDER the store lock at commit, plus a
 *    commit deadline; after a PyTimeout the request stays PENDING/RECONCILING until a definitive outcome is
 *    read or fenced under the admission lock (single_leg_request_cancel).
 *  - Codex review 17 Nov 2026 (A1): a tap is bound to the proposal the page DISPLAYED.  submit() carries the
 *    displayed decision id and instrument key; the FIRST native read must match both or the request is rejected
 *    DISPLAYED_PROPOSAL_CHANGED:<field> (requires_new_tap) before any quote fetch or commit.  The later
 *    re-read-after-fetch and the Python commit-time probe are unchanged.
 *  - A3: uncovered short single-leg proposals (SELL_*) are not offered for admission from any card:
 *    UNCOVERED_SHORT_NOT_OFFERED before any broker call.  Plans, research capture and exits are untouched.
 * Pure Kotlin (no Android types) so the paused-transport integration tests run on the JVM.
 */
class SingleLegAdmissionCoordinator(
    private val readState: (String) -> State,
    private val transport: () -> ManagedReadAdapters.Transport?,
    private val clocks: ManagedReadAdapters.Clocks,
    private val admit: (Call) -> String?,            // null = PyTimeout
    private val reconcile: (String) -> String?,      // brain.single_leg_request_status (read-only)
    private val cancel: (String) -> String?,         // brain.single_leg_request_cancel (under the admission lock)
    private val executor: Executor = Executors.newSingleThreadExecutor(),
    private val commitWindowMs: Long = 15_000L,
    private val reconcileGraceMs: Long = 5_000L,
    private val sleeper: (Long) -> Unit = { Thread.sleep(it) },
    private val reconcileExecutor: Executor = Executors.newCachedThreadPool(),
    private val maxBackoffMs: Long = 5_000L,
    // Codex reply 20 Nov 2026: compact device trace (SingleLegTradeTraceStore.append).  Default no-op; a trace
    // failure never changes an admission outcome.
    private val trace: (JSONObject) -> Unit = {},
) {
    data class State(
        val resultJson: String?, val index: String, val decisionId: String?, val instrumentKey: String?,
        val action: String?, val lotSize: Int, val quoteNeeded: List<String>,
        val enabled: Boolean, val paper: Boolean, val selector: String, val openTrades: String,
        val capital: Double?, val maxRiskPct: Double?, val configRevision: Long,
    ) {
        fun gatesOpen() = enabled && paper && (selector == "BOTH" || selector == index)

        /** First field that differs (null = same). */
        fun diff(o: State): String? = when {
            decisionId != o.decisionId -> "decision_id"
            instrumentKey != o.instrumentKey -> "instrument_key"
            enabled != o.enabled -> "enabled"
            paper != o.paper -> "mode"
            selector != o.selector -> "selector"
            openTrades != o.openTrades -> "open_trades"
            capital != o.capital && !(capital?.isNaN() == true && o.capital?.isNaN() == true) -> "capital"
            maxRiskPct != o.maxRiskPct -> "max_risk_pct"
            configRevision != o.configRevision -> "config_revision"
            else -> null
        }

        fun probeJson(): String = JSONObject()
            .put("enabled", enabled).put("paper_mode", paper).put("selector", selector)
            .put("open_trades", try { JSONArray(openTrades) } catch (e: Exception) { JSONArray() })
            .put("capital", capital?.let { if (it.isFinite()) it else it.toString() } ?: JSONObject.NULL)
            .put("max_risk_pct", maxRiskPct ?: JSONObject.NULL)
            .put("config_revision", configRevision)
            .put("capital_source", "prefs.capital").put("risk_source", "brain._CONST.MAX_RISK_PCT")
            .put("decisions", JSONObject().put(index, JSONObject()
                .put("decision_id", decisionId ?: JSONObject.NULL).put("instrument_key", instrumentKey ?: JSONObject.NULL)))
            .toString()
    }

    /** Python sees this object; it calls current() under the store lock. */
    class StateProbe(private val f: () -> String) { fun current(): String = f() }

    data class Call(val state: State, val quote: ManagedReadAdapters.QuoteFetch?, val margin: ManagedReadAdapters.MarginFetch?,
                    val requestId: String, val deadlineMs: Long, val probe: StateProbe) {
        fun configJson(): String = JSONObject().put("revision", state.configRevision)
            .put("capital_source", "prefs.capital").put("risk_source", "brain._CONST.MAX_RISK_PCT").toString()
    }

    // in-flight requests are never evicted; only completed outcomes are pruned (oldest first) beyond 50
    private val statuses = LinkedHashMap<String, String>()
    private val completed = LinkedHashMap<String, Boolean>()
    // request ids whose exact-contract quote GET was started (20 Nov trace: refused before vs after the quote)
    private val quoteStarted = HashSet<String>()
    private val indexOf = HashMap<String, String>()

    private fun emit(kind: String, id: String?, f: (JSONObject) -> Unit = {}) {
        try {
            val j = JSONObject().put("kind", kind).put("source", "native").put("t_ms", clocks.wallMs())
            if (id != null) { j.put("request_id", id); synchronized(statuses) { indexOf[id] }?.let { j.put("index", it) } }
            f(j); trace(j)
        } catch (t: Throwable) { /* trace is evidence only */ }
    }

    private fun put(id: String, json: JSONObject) = synchronized(statuses) {
        statuses[id] = json.put("request_id", id).toString()
        if (json.optBoolean("completed")) completed[id] = true
        while (completed.size > 50) { val k = completed.keys.first(); completed.remove(k); statuses.remove(k) }
    }

    /** The identity the page displayed when the user tapped (A1). */
    data class Displayed(val decisionId: String?, val instrumentKey: String?)

    /**
     * Non-blocking: returns PENDING with the request id.  A tap without a complete displayed identity is rejected
     * immediately (DISPLAYED_IDENTITY_REQUIRED) and never queued.
     */
    fun submit(index: String, displayedDecisionId: String?, displayedInstrumentKey: String?,
               requestId: String = UUID.randomUUID().toString()): String {
        val ix = index.trim().uppercase()
        val shown = Displayed(displayedDecisionId?.trim()?.takeIf { it.isNotEmpty() },
                              displayedInstrumentKey?.trim()?.takeIf { it.isNotEmpty() })
        synchronized(statuses) { indexOf[requestId] = ix }
        emit("N_SUBMIT", requestId) { it.put("decision_id", shown.decisionId ?: JSONObject.NULL)
            .put("instrument_key", shown.instrumentKey ?: JSONObject.NULL) }
        if (shown.decisionId == null || shown.instrumentKey == null) {
            val j = rej("DISPLAYED_IDENTITY_REQUIRED").put("index", ix).put("requires_new_tap", true)
            done(requestId, j)
            return JSONObject(j.toString()).put("request_id", requestId).toString()
        }
        put(requestId, JSONObject().put("status", "PENDING").put("phase", "QUEUED").put("index", ix))
        executor.execute { run(ix, requestId, shown) }
        return JSONObject().put("status", "PENDING").put("request_id", requestId).put("index", ix).toString()
    }

    /**
     * Known in this process: its live status.  Unknown (e.g. after an app restart): no admission for it can be in
     * flight in this process, so reconcile DEFINITIVELY under the admission lock (recorded outcome, or a persisted
     * fence).  If the lock is busy the answer is OUTCOME_UNCONFIRMED (not completed) - never a guessed negative.
     */
    fun status(requestId: String): String {
        synchronized(statuses) { statuses[requestId] }?.let { return it }
        val c = cancel(requestId)?.let { try { JSONObject(it) } catch (e: Exception) { null } }
        if (c != null && c.optBoolean("found")) {
            emit("N_OUTCOME", requestId) { t -> t.put("status", c.optString("status")).put("reason", c.opt("reason") ?: JSONObject.NULL)
                .put("reconciled", true).put("recovered", true)
                c.optString("index").uppercase().takeIf { it == "NF" || it == "BNF" }?.let { t.put("index", it) } }
            return c.put("completed", true).put("reconciled", true).toString()
        }
        return JSONObject().put("status", "PENDING").put("phase", "OUTCOME_UNCONFIRMED").put("request_id", requestId).toString()
    }

    internal fun run(ix: String, id: String, shown: Displayed) {
        try {
            val s0 = readState(ix)                      // FIRST read: must be the proposal the user saw
            if (s0.resultJson == null) return done(id, rej("NO_CURRENT_RESULT"))
            if (s0.decisionId == null || s0.instrumentKey == null) return done(id, rej("NO_PROPOSAL"))
            val changed = when {
                s0.decisionId != shown.decisionId -> "decision_id"
                s0.instrumentKey != shown.instrumentKey -> "instrument_key"
                else -> null
            }
            if (changed != null) return done(id, rej("DISPLAYED_PROPOSAL_CHANGED:$changed").put("index", ix)
                .put("requires_new_tap", true)
                .put("displayed", JSONObject().put("decision_id", shown.decisionId).put("instrument_key", shown.instrumentKey))
                .put("current", JSONObject().put("decision_id", s0.decisionId).put("instrument_key", s0.instrumentKey)))
            if ((s0.action ?: "").trim().uppercase().startsWith("SELL"))
                return done(id, rej("UNCOVERED_SHORT_NOT_OFFERED").put("index", ix).put("action", s0.action))
            var q: ManagedReadAdapters.QuoteFetch? = null
            var m: ManagedReadAdapters.MarginFetch? = null
            val tr = transport()
            if (s0.gatesOpen() && tr != null) {
                put(id, JSONObject().put("status", "PENDING").put("phase", "FETCHING").put("index", ix))
                val keys = s0.quoteNeeded.take(1)
                val tracedTransport = ManagedReadAdapters.Transport { method, url, body ->
                    synchronized(statuses) { quoteStarted.add(id) }
                    emit("N_QUOTE_STARTED", id) { it.put("instrument_key", keys.first()) }
                    tr.call(method, url, body)
                }
                q = ManagedReadAdapters.fetchQuotes(keys, tracedTransport, clocks)
                m = ManagedReadAdapters.fetchShortMargin(s0.action ?: "", s0.instrumentKey, s0.lotSize, tr, clocks)
            }
            val s1 = readState(ix)                      // re-read AFTER the fetch
            s0.diff(s1)?.let { return done(id, rej("STATE_CHANGED_DURING_FETCH:$it")) }
            val deadline = clocks.wallMs() + commitWindowMs
            put(id, JSONObject().put("status", "PENDING").put("phase", "COMMITTING").put("index", ix))
            val probe = StateProbe { readState(ix).probeJson() }
            val out = admit(Call(s1, q, m, id, deadline, probe))
            if (out != null) return done(id, JSONObject(out))
            // PyTimeout: the Python call may still be running.  Report PENDING/RECONCILING until the outcome is
            // definitive; never NOT_ADMITTED because a timer elapsed.  Reconcile off the admission executor.
            put(id, JSONObject().put("status", "PENDING").put("phase", "RECONCILING").put("index", ix))
            reconcileExecutor.execute { reconcileLoop(ix, id, deadline) }
        } catch (e: Exception) {
            done(id, JSONObject().put("status", "ERROR").put("reason", e.javaClass.simpleName))
        }
    }

    internal fun reconcileLoop(ix: String, id: String, deadline: Long) {
        var wait = 250L
        while (true) {
            try {
                val r = reconcile(id)?.let { JSONObject(it) }
                if (r != null && r.optBoolean("found")) return done(id, r.put("reconciled", true))
                if (clocks.wallMs() > deadline + reconcileGraceMs) {
                    // definitive under the admission lock: the recorded outcome, or a persisted terminal fence that
                    // blocks any later commit for this id.  Lock busy -> still unconfirmed, keep reconciling.
                    val c = cancel(id)?.let { JSONObject(it) }
                    if (c != null && c.optBoolean("found")) return done(id, c.put("reconciled", true))
                    put(id, JSONObject().put("status", "PENDING").put("phase", "OUTCOME_UNCONFIRMED").put("index", ix))
                }
            } catch (e: Exception) {
                put(id, JSONObject().put("status", "PENDING").put("phase", "OUTCOME_UNCONFIRMED").put("index", ix)
                    .put("last_error", e.javaClass.simpleName))
            }
            sleeper(wait)
            wait = minOf(wait * 2, maxBackoffMs)
        }
    }

    private fun rej(why: String) = JSONObject().put("status", "REJECTED").put("reason", why)

    private fun done(id: String, j: JSONObject) {
        val quoted = synchronized(statuses) { quoteStarted.remove(id) }
        put(id, j.put("completed", true).put("quote_requested", quoted))
        synchronized(statuses) { indexOf.remove(id) }?.let { ix ->
            emit("N_OUTCOME", id) { t -> t.put("index", ix).put("status", j.optString("status"))
                .put("reason", j.opt("reason") ?: JSONObject.NULL).put("quote_requested", quoted)
                .put("requires_new_tap", j.optBoolean("requires_new_tap", false))
                j.optJSONArray("reasons")?.let { t.put("reasons", it) } }
        }
    }
}
