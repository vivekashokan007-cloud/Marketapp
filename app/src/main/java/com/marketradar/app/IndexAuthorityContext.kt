package com.marketradar.app

import com.marketradar.app.util.LogBuffer
import org.json.JSONObject
import java.io.File
import java.time.Instant
import java.time.ZoneId
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * Delivery A: context keys for the per-index direction authority (index_direction_v1).
 * Local app-private directories only. No network, no Supabase, no credentials.
 *
 * R5 corrections:
 *  - R1: a trusted analysis clock (analysisClockMs) is stamped immediately before EVERY analyze (live poll and
 *    refresh); Python judges staleness against it and never relabels the source poll time.
 *  - s2: analyze performs no authority-state file I/O (indexAuthorityDeferredWrite); the state delta returned in
 *    the result is persisted by ONE single-thread IO worker, stamped with the publication time only when the
 *    native publisher reported PUBLISHED.
 *  - s2: the user's index selector (NF / BNF / BOTH) gates new managed admission only.
 *  - s2: the local Daily review runs on the same serialized worker after the session, once per session, with a
 *    cooperative budget; it is independent of the teacher.
 */
object IndexAuthorityContext {
    const val STATE_DIR_NAME = "index_authority_v1"
    const val REPORT_DIR_NAME = "self_study_v1"
    const val PREF_SELECTOR = "index_authority_selector_v1"
    const val PREF_REVIEW_DONE_SESSION = "index_authority_daily_review_done_session_v1"
    const val PERSIST_KEY = "index_authority_persist"
    const val DAILY_REVIEW_AFTER_MIN = 15 * 60 + 40          // 15:40 IST
    const val DAILY_REVIEW_BUDGET_S = 20.0
    private val IST: ZoneId = ZoneId.of("Asia/Kolkata")

    fun stateDir(filesDir: File) = File(filesDir, STATE_DIR_NAME)
    fun reportDir(filesDir: File) = File(filesDir, REPORT_DIR_NAME)
    const val SINGLE_LEG_DIR_NAME = "single_leg_v1"
    const val PREF_SINGLE_LEG_PAPER = "single_leg_paper_enabled_v1"   // default OFF; explicit user opt-in
    const val PREF_SINGLE_LEG_CONFIG_REV = "single_leg_config_revision_v1"
    fun singleLegDir(filesDir: File) = File(filesDir, SINGLE_LEG_DIR_NAME)

    fun normalizeSelector(raw: String?): String = when (raw?.trim()?.uppercase()) {
        "NF", "NIFTY" -> "NF"
        "BNF", "BANKNIFTY" -> "BNF"
        else -> "BOTH"
    }

    fun apply(ctx: JSONObject, filesDir: File, isRefresh: Boolean, nowMs: Long, selector: String? = null) {
        ctx.put("indexAuthorityStateDir", stateDir(filesDir).absolutePath)
        // Receipt time of a live poll; a refresh re-analysis carries none.
        if (isRefresh) ctx.remove("pollReceiptMs") else ctx.put("pollReceiptMs", nowMs)
        // R1: trusted analysis clock for every analysis (live and refresh)
        ctx.put("analysisClockMs", nowMs)
        ctx.put("indexAuthorityDeferredWrite", true)
        ctx.put("indexSelector", normalizeSelector(selector))
        // Delivery B: managed single-leg store (read-only during analyze; written by bridge admit / IO monitor)
        ctx.put("singleLegStateDir", singleLegDir(filesDir).absolutePath)
    }

    /** Detach the state delta from the result so it is never stored or shown as part of the brain result. */
    fun takePersist(result: JSONObject): JSONObject? {
        val p = result.optJSONObject(PERSIST_KEY) ?: return null
        result.remove(PERSIST_KEY)
        val session = p.optString("session_date", "")
        val delta = p.optJSONObject("delta")
        if (session.length < 10 || delta == null) return null
        return p
    }

    fun publishedMs(published: Boolean, nowMs: Long): Long = if (published) nowMs else 0L

    fun istSession(nowMs: Long): String = Instant.ofEpochMilli(nowMs).atZone(IST).toLocalDate().toString()

    fun dailyReviewDue(nowMs: Long, doneSession: String?): Boolean {
        val z = Instant.ofEpochMilli(nowMs).atZone(IST)
        val minutes = z.hour * 60 + z.minute
        if (minutes < DAILY_REVIEW_AFTER_MIN) return false
        val dow = z.dayOfWeek.value
        if (dow >= 6) return false
        return doneSession != istSession(nowMs)
    }

    /** PM1-2: KEPT_RICHER_REVISION finishes a run exactly like ALREADY_COMPLETE (same set as Python
     *  daily_review_v1.FINISHED_STATUSES and the catch-up ledger DONE tuple). */
    fun reviewFinished(status: String?): Boolean =
        status == "WRITTEN" || status == "ALREADY_COMPLETE" || status == "KEPT_RICHER_REVISION"

    const val REVIEW_TRADES_SOURCE = "prefs.closed_trades"

    /** PM1-2: the ONE manual closed-trade input for every Daily review run (service, catch-up, bridge),
     *  read inside the executing worker.  json == null with status MISSING = not stored / blank;
     *  UNREADABLE = the preference exists but cannot be read as a string.  Never reads any other key. */
    data class ReviewTrades(val json: String?, val status: String)

    fun reviewTrades(prefs: android.content.SharedPreferences): ReviewTrades = try {
        val v = prefs.getString("closed_trades", null)
        if (v == null || v.isBlank()) ReviewTrades(null, "MISSING") else ReviewTrades(v, "READ")
    } catch (e: Exception) {
        ReviewTrades(null, "UNREADABLE")
    }

    fun reviewTradesJson(prefs: android.content.SharedPreferences): String? = reviewTrades(prefs).json
}

/** ONE serialized local worker for authority state persistence and the Daily review (no network). */
object IndexAuthorityIo {
    private const val TAG = "IndexAuthorityIo"
    val executor: ExecutorService = Executors.newSingleThreadExecutor { r ->
        Thread(r, "index-authority-io").apply { isDaemon = true }
    }

    fun persist(filesDir: File, persist: JSONObject, publishedMs: Long) {
        val dir = IndexAuthorityContext.stateDir(filesDir).absolutePath
        val session = persist.optString("session_date", "")
        val delta = persist.optJSONObject("delta")?.toString() ?: return
        executor.execute {
            try {
                val brain = com.chaquo.python.Python.getInstance().getModule("brain")
                val out = brain.callAttr("index_authority_persist", dir, session, delta, publishedMs).toString()
                if (!out.contains("\"written\": true")) LogBuffer.add('W', TAG, "INDEX_AUTHORITY_PERSIST: ${out.take(160)}")
            } catch (e: Exception) {
                LogBuffer.add('W', TAG, "INDEX_AUTHORITY_PERSIST_FAIL: ${e.javaClass.simpleName}")
            }
        }
    }

    /** Delivery B s3 step 5: advance open single-leg Paper positions on a PUBLISHED poll (serialized IO). */
    private val slCoalescer = MonitorCoalescer()

    /**
     * R7 (Codex R6 s4): advance open single-leg Paper positions. Queued work is COALESCED (at most one pending
     * run); the run reads the session and clock WHEN IT EXECUTES (Python reads the wall clock itself), fetches
     * dedicated v3 quotes for OPEN positions first (exact instruments, bounded), stores them with their original
     * receipt times, then monitors. Direction observations are read from the persisted authority history.
     */
    fun monitorSingleLeg(filesDir: File, spotsJson: String, transport: ManagedReadAdapters.Transport?, onEvents: (String) -> Unit = {}) {
        val sl = IndexAuthorityContext.singleLegDir(filesDir)
        if (!File(sl, "positions.json").exists()) return
        if (!slCoalescer.offer(spotsJson)) return
        executor.execute {
            val spots = slCoalescer.begin()
            try {
                val brain = com.chaquo.python.Python.getInstance().getModule("brain")
                if (transport != null) {
                    val tgt = JSONObject(brain.callAttr("single_leg_quote_targets", sl.absolutePath, "null").toString())
                    val keys = (0 until (tgt.optJSONArray("open")?.length() ?: 0)).map { tgt.getJSONArray("open").getString(it) }
                    val research = tgt.optJSONArray("research")
                    val researchKeys = (0 until (research?.length() ?: 0)).mapNotNull { research?.optString(it) }
                    val clocks = ManagedReadAdapters.Clocks({ System.currentTimeMillis() }, { android.os.SystemClock.elapsedRealtime() })
                    ManagedReadAdapters.fetchQuotes(keys + researchKeys, transport, clocks)?.let { q ->
                        brain.callAttr("single_leg_put_quotes", sl.absolutePath, q.requestedJson, q.body,
                            q.requestStartMs, q.receiptMs, q.receiptMonoMs)
                    }
                }
                val session = IndexAuthorityContext.istSession(System.currentTimeMillis())   // at execution
                val out = brain.callAttr("single_leg_monitor", sl.absolutePath,
                    IndexAuthorityContext.stateDir(filesDir).absolutePath, session, spots).toString()
                val events = JSONObject(out).optJSONArray("events") ?: org.json.JSONArray()
                for (i in 0 until events.length()) {
                    val event = events.getJSONObject(i)
                    val account = event.optJSONObject("account")
                    SingleLegTradeTraceStore(filesDir).append(JSONObject().put("kind", "N_EXIT")
                        .put("position_id", event.optString("position_id"))
                        .put("index", account?.optString("index") ?: event.optString("index"))
                        .put("status", event.optString("event"))
                        .put("reason", account?.optString("exit_reason") ?: event.optString("reason"))
                        .put("source", "monitor").toString())
                }
                if (events.length() > 0) onEvents(out)
                LogBuffer.add('I', TAG, "SINGLE_LEG_MONITOR: ${out.take(160)}")
            } catch (e: Exception) {
                LogBuffer.add('W', TAG, "SINGLE_LEG_MONITOR_FAIL: ${e.javaClass.simpleName}")
            }
        }
    }

    private val readinessExecutor = Executors.newSingleThreadExecutor { r ->
        Thread(r, "paper-quote-readiness").apply { isDaemon = true }
    }
    private val readinessPending = java.util.concurrent.atomic.AtomicBoolean(false)

    /** Coalesced, read-only preflight on its own worker; open exit work suppresses candidate GETs. */
    fun verifySingleLegReadiness(filesDir: File, prefs: android.content.SharedPreferences,
                                transport: ManagedReadAdapters.Transport) {
        if (!prefs.getBoolean(IndexAuthorityContext.PREF_SINGLE_LEG_PAPER, false)
            || EvAcceptance.executionMode(prefs) != "paper") return
        if (!readinessPending.compareAndSet(false, true)) return
        readinessExecutor.execute {
            try {
                fun current(): String {
                    if (!prefs.getBoolean(IndexAuthorityContext.PREF_SINGLE_LEG_PAPER, false)
                        || EvAcceptance.executionMode(prefs) != "paper") return "{}"
                    if (prefs.getBoolean("paper_shared_inputs_unavailable", false)) return "{}"
                    return SessionInputs.currentResultOrNull(BrainResultStore.readBrainResultIn(File(filesDir, "brain_results"), prefs),
                        SessionInputs.inputRevision(SessionInputs.read(prefs.getString(SessionInputs.PREF_KEY, null)),
                            IndexAuthorityContext.istSession(System.currentTimeMillis()))) ?: "{}"
                }
                val module = com.chaquo.python.Python.getInstance().getModule("single_leg_readiness_v1")
                val directory = IndexAuthorityContext.singleLegDir(filesDir).absolutePath
                val request = module.callAttr("begin", directory, current()).toString()
                val body = JSONObject(request)
                val keys = body.getJSONArray("keys").let { a -> (0 until a.length()).map { a.getString(it) } }
                if (keys.isNotEmpty()) {
                    val clocks = ManagedReadAdapters.Clocks({ System.currentTimeMillis() },
                        { android.os.SystemClock.elapsedRealtime() })
                    val outcome = ManagedReadAdapters.fetchQuotesOutcome(keys, transport, clocks)
                    val quote = outcome.fetch
                    val recorded = JSONObject(module.callAttr("complete", directory, request, current(), quote?.body,
                        outcome.requestStartMs, outcome.receiptMs, outcome.receiptMonoMs, outcome.failure,
                        System.currentTimeMillis(), outcome.httpStatus).toString())
                    val rows = recorded.optJSONObject("by_index") ?: JSONObject()
                    for (ix in listOf("NF", "BNF")) rows.optJSONObject(ix)?.let { row ->
                        SingleLegTradeTraceStore(filesDir).append(JSONObject(row.toString())
                            .put("kind", "N_READINESS").put("index", ix).put("status", row.optString("state"))
                            .put("source", "readiness").toString())
                    }
                    LogBuffer.add('I', TAG, "SINGLE_LEG_READINESS: keys=${keys.size} transport=${outcome.failure ?: "RECEIVED"}")
                }
            } catch (e: Exception) {
                LogBuffer.add('W', TAG, "SINGLE_LEG_READINESS_FAIL: ${e.javaClass.simpleName}")
            } finally {
                readinessPending.set(false)
            }
        }
    }

    fun maybeRunDailyReview(filesDir: File, prefs: android.content.SharedPreferences, nowMs: Long) {
        if (!IndexAuthorityContext.dailyReviewDue(nowMs, prefs.getString(IndexAuthorityContext.PREF_REVIEW_DONE_SESSION, null))) return
        val session = IndexAuthorityContext.istSession(nowMs)
        executor.execute {
            try {
                if (!IndexAuthorityContext.dailyReviewDue(System.currentTimeMillis(),
                        prefs.getString(IndexAuthorityContext.PREF_REVIEW_DONE_SESSION, null))) return@execute
                val brain = com.chaquo.python.Python.getInstance().getModule("brain")
                val trades = IndexAuthorityContext.reviewTrades(prefs)   // PM1-2: read at run time, in the worker
                val out = brain.callAttr("daily_review_run",
                    IndexAuthorityContext.stateDir(filesDir).absolutePath,
                    IndexAuthorityContext.reportDir(filesDir).absolutePath,
                    session, "[]", trades.json, null, IndexAuthorityContext.DAILY_REVIEW_BUDGET_S, null,
                    IndexAuthorityContext.REVIEW_TRADES_SOURCE, trades.status).toString()
                val status = try { JSONObject(out).optString("status") } catch (e: Exception) { "" }
                if (IndexAuthorityContext.reviewFinished(status)) {
                    prefs.edit().putString(IndexAuthorityContext.PREF_REVIEW_DONE_SESSION, session).apply()
                }
                LogBuffer.add('I', TAG, "DAILY_REVIEW: session=$session status=$status")
            } catch (e: Exception) {
                LogBuffer.add('W', TAG, "DAILY_REVIEW_FAIL: ${e.javaClass.simpleName}")
            }
        }
    }
}
