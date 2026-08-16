package com.marketradar.app

import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.util.Base64
import android.util.JsonReader
import android.util.JsonToken
import android.util.Log
import android.webkit.JavascriptInterface
import android.widget.Toast
import com.chaquo.python.Python
import java.io.File
import java.net.URLEncoder
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import com.marketradar.app.util.LogBuffer
import java.util.concurrent.atomic.AtomicBoolean

class NativeBridge(private val context: Context) {
    companion object {
        private const val TAG = "NativeBridge"
        private const val PY_VALIDATE_TIMEOUT_MS = 8_000L
        private const val PY_SCORE_TIMEOUT_MS = 2_500L
        private const val PREF_SANDBOX_ENABLED = "execution_sandbox_enabled"
        private const val PREF_SANDBOX_TOKEN = "SANDBOX_TOKEN"
        private const val PREF_ORDER_PROXY_URL = "order_proxy_url"
        private const val PREF_APPROVED_BRANCH_PROPOSALS = "approved_branch_proposals"
        private const val PREF_APPROVED_BRANCH_PROPOSALS_SYNC_MS = "approved_branch_proposals_sync_ms"
        private const val PREF_LAST_EVALUATOR_JOB = "last_evaluator_job"
        private const val PREF_TRADE_MODE = "trade_mode"
        private const val PREF_TRADE_MODE_EXPLICIT = "trade_mode_explicit"
        private const val PREF_NOTIFICATION_TRANSPORT_MODE = "brain_notification_transport_mode"
        private const val PREF_STAGE2A_MODE = "stage2a_guard_mode"
        private const val ORACLE_BASE_URL = "https://marketradar-oracle.online"
        private const val APPROVED_BRANCH_PROPOSALS_TTL_MS = 2 * 60 * 1000L
        private const val ML_BRAIN_SNAPSHOT_JS_MAX_ROWS = 5
        private const val ML_BRAIN_SNAPSHOT_JS_MAX_BYTES = 2L * 1024L * 1024L
        private const val ML_BRAIN_SNAPSHOT_JS_CACHE_TTL_MS = 60_000L
        private const val MARKET_OPEN_MINUTE = 9 * 60 + 15
        private const val MARKET_CLOSE_MINUTE = 15 * 60 + 40
        private const val POLL_SLOT_MINUTES = 5
        private const val POLL_FULL_DAY_SLOTS = ((MARKET_CLOSE_MINUTE - MARKET_OPEN_MINUTE) / POLL_SLOT_MINUTES) + 1
        private const val TEACHER_RESEARCH_GENERATED_CANDIDATE_CAP = 20
        private const val TEACHER_RESEARCH_RANKED_CANDIDATE_CAP = 30
        private const val TEACHER_RESEARCH_REJECTED_CANDIDATE_CAP = 12
        private const val TEACHER_RESEARCH_REJECTED_OUTCOME_CAP = 500
        private val teacherResearchScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        private val teacherResearchRebuildInFlight = AtomicBoolean(false)
        @Volatile private var teacherResearchLastRebuildKey = ""
    }
    private var lastScoredCandCount = -1
    private var lastScoredFirstCandId = ""
    private var lastScoredTotalLen = -1
    private var openTradesCache = "[]"
    private var openTradesCacheMs = 0L
    private val openTradesCacheTtlMs = 30_000L
    private val morningSnapshotCache = mutableMapOf<String, String>()
    private var yesterdayHistoryCache = "[]"
    private var yesterdayHistoryCacheKey = ""
    private var exportSessionId = ""
    private var exportSessionName = ""
    private var exportSessionMime = ""
    private var exportSessionBase64 = StringBuilder()
    private var mlBrainSnapshotsBridgeCacheKey = ""
    private var mlBrainSnapshotsBridgeCacheValue = "[]"
    private var mlBrainSnapshotsBridgeCacheMs = 0L

    // Use applicationContext to guarantee same SharedPreferences instance as MarketWatchService
    private val prefs: SharedPreferences = context.applicationContext.getSharedPreferences("market_radar", Context.MODE_PRIVATE)
    private val httpClient = OkHttpClient()
    private val startDebounceMs = 15000L

    init {
        clearStaleSessionStateIfNeeded()
    }

    private fun defaultTradeMode(): String = "intraday"

    private fun readJsonArrayFile(file: File): JSONArray {
        if (!file.exists()) return JSONArray()
        val raw = try {
            file.readText().trim()
        } catch (_: Exception) {
            ""
        }
        if (raw.isBlank()) return JSONArray()
        return try {
            JSONArray(raw)
        } catch (_: Exception) {
            JSONArray()
        }
    }

    private fun parseJsonObject(value: Any?): JSONObject? {
        return when (value) {
            is JSONObject -> value
            is String -> {
                val trimmed = value.trim()
                if (!trimmed.startsWith("{")) null else try {
                    JSONObject(trimmed)
                } catch (_: Exception) {
                    null
                }
            }
            else -> null
        }
    }

    private fun parseJsonArray(value: Any?): JSONArray? {
        return when (value) {
            is JSONArray -> value
            is String -> {
                val trimmed = value.trim()
                if (!trimmed.startsWith("[")) null else try {
                    JSONArray(trimmed)
                } catch (_: Exception) {
                    null
                }
            }
            else -> null
        }
    }

    private fun streamJsonArrayFile(file: File, onRow: (JSONObject) -> Unit) {
        if (!file.exists() || file.length() <= 0L) return
        file.bufferedReader().use { buffered ->
            JsonReader(buffered).use { reader ->
                reader.beginArray()
                while (reader.hasNext()) {
                    onRow(readJsonObject(reader))
                }
                reader.endArray()
            }
        }
    }

    private fun readJsonObject(reader: JsonReader): JSONObject {
        val obj = JSONObject()
        reader.beginObject()
        while (reader.hasNext()) {
            obj.put(reader.nextName(), readJsonValue(reader))
        }
        reader.endObject()
        return obj
    }

    private fun readJsonArray(reader: JsonReader): JSONArray {
        val arr = JSONArray()
        reader.beginArray()
        while (reader.hasNext()) {
            arr.put(readJsonValue(reader))
        }
        reader.endArray()
        return arr
    }

    private fun readJsonValue(reader: JsonReader): Any? {
        return when (reader.peek()) {
            JsonToken.BEGIN_OBJECT -> readJsonObject(reader)
            JsonToken.BEGIN_ARRAY -> readJsonArray(reader)
            JsonToken.STRING -> reader.nextString()
            JsonToken.BOOLEAN -> reader.nextBoolean()
            JsonToken.NULL -> {
                reader.nextNull()
                JSONObject.NULL
            }
            JsonToken.NUMBER -> {
                val raw = reader.nextString()
                raw.toLongOrNull() ?: raw.toDoubleOrNull() ?: raw
            }
            else -> {
                reader.skipValue()
                JSONObject.NULL
            }
        }
    }

    private fun compactTeacherResearchCandidate(raw: Any?): JSONObject? {
        val cand = parseJsonObject(raw) ?: return null
        val compact = JSONObject()
        val keys = arrayOf(
            "id", "candidate_id", "rank", "watchlist_rank",
            "type", "strategy_type", "index", "lane", "trade_mode", "poll_ts", "expiry", "width", "tDTE",
            "premiumEdge", "ev", "evPer1k", "creditWidthRatio", "sigmaOTM", "ivRichness",
            "probProfit", "prob_source", "prob_status", "trueProb", "riskReward",
            "sellStrike", "buyStrike", "sellType", "buyType",
            "sellStrike2", "buyStrike2", "sellType2", "buyType2",
            "netPremium", "maxProfit", "maxLoss", "targetProfit", "stopLoss", "estCost", "isCredit",
            "capitalBlocked", "executionReady", "executionGate", "entryAction", "directionSafe",
            "marketConfidence", "entryConfidence", "entryEligible", "entryGate", "entryEligibility",
            "brainScore", "contextPercentileScore", "p_ml", "mlAction", "mlEdge", "mlRegime",
            "mlUnsure", "mlOodFlag", "deterministic_rank", "teacher_shadow_rank", "stage2a_live_rank",
            "pc2PaperRank", "pc2PaperResearchRank", "pc2PaperPrimaryEligible", "pc2PaperSelectorVersion", "pc2PaperMode",
            "pc2PaperSortComponents", "pc2PaperSortKey", "pc2PaperRandomControl", "pc2CompositeShadow",
            "pc2SupplyWidthSource", "pc2SupplyWidthExpanded", "pc2SupplyLadderVersion",
            "pc2BatchFCandleScore", "pc2BatchFCandleComponents", "pc2BatchFCandleExcludedPatterns",
            "pc2BatchFCandleScoringMethod",
            "reason_code", "reject_reason",
            "rejection_stage", "rejection_reason", "gate_name", "gate_field",
            "gate_basis", "pc2_gate_basis", "gate_basis_summary", "pct_target", "slice_key",
            "basis_support_count", "basis_stability_ratio", "basis_stability_bar",
            "basis_stability_pass", "counterfactual_basis",
            "observed_value", "threshold_value", "margin", "margin_pct",
            "marginRequired", "marginForSizing", "marginSource", "marginFallbackUsed",
            "marginFallbackValue", "marginFallbackReason", "marginModelVersion", "brainMaxLoss",
            "marginSizingBehavior", "marginQuoteStatus", "marginQuoteSource", "marginQuotedAt",
            "marginRequestUrl", "marginQuoteError", "realMargin", "upstoxRequiredMargin",
            "upstoxFinalMargin", "upstoxSpanMargin", "upstoxExposureMargin", "upstoxNetBuyPremium"
        )
        for (key in keys) {
            val value = cand.opt(key)
            if (value != null && value != JSONObject.NULL) {
                compact.put(key, value)
            }
        }
        parseJsonArray(cand.opt("legs"))?.let { legs ->
            if (legs.length() > 0) compact.put("legs", legs)
        }
        return if (compact.length() > 0) compact else null
    }

    private fun compactTeacherResearchCandidates(raw: Any?, limit: Int = Int.MAX_VALUE): JSONArray {
        val source = parseJsonArray(raw) ?: return JSONArray()
        val compact = JSONArray()
        val end = minOf(source.length(), limit)
        for (i in 0 until end) {
            compactTeacherResearchCandidate(source.opt(i))?.let(compact::put)
        }
        return compact
    }

    private fun compactTeacherResearchSnapshot(snapshot: JSONObject, includeRejectedCandidates: Boolean = true): JSONObject {
        val compact = JSONObject()
        compact.put("id", snapshot.opt("id"))
        compact.put("action", snapshot.opt("action"))
        compact.put("strategy", snapshot.opt("strategy"))
        compact.put("is_labelable", snapshot.opt("is_labelable"))

        compactTeacherResearchCandidate(snapshot.opt("primary_candidate_json"))?.let {
            compact.put("primary_candidate_json", it.toString())
        }

        val context = parseJsonObject(snapshot.opt("context_json")) ?: JSONObject()
        val generated = parseJsonArray(context.opt("snapshot_generated_candidates"))
            ?: parseJsonArray(snapshot.opt("top_candidates_json"))
            ?: JSONArray()
        val rankedFull = parseJsonArray(context.opt("snapshot_ranked_candidates_full"))

        val compactContext = JSONObject()
        val scalarKeys = arrayOf("vix", "bnfSpot", "nfSpot", "significant_move")
        for (key in scalarKeys) {
            val value = context.opt(key)
            if (value != null && value != JSONObject.NULL) {
                compactContext.put(key, value)
            }
        }
        parseJsonObject(context.opt("gap"))?.let { gap ->
            val gapType = gap.opt("type")
            if (gapType != null && gapType != JSONObject.NULL) {
                compactContext.put("gap", JSONObject().put("type", gapType))
            }
        }

        val compactGenerated = compactTeacherResearchCandidates(generated, TEACHER_RESEARCH_GENERATED_CANDIDATE_CAP)
        compactContext.put("snapshot_generated_candidates", compactGenerated)
        rankedFull?.let {
            val compactRankedFull = compactTeacherResearchCandidates(it, TEACHER_RESEARCH_RANKED_CANDIDATE_CAP)
            if (compactRankedFull.length() > 0) {
                compactContext.put("snapshot_ranked_candidates_full", compactRankedFull)
            }
        }
        if (includeRejectedCandidates) {
            val rejected = parseJsonArray(context.opt("snapshot_rejected_candidates_full"))
                ?: parseJsonArray(context.opt("snapshot_rejected_candidates"))
            val compactRejected = compactTeacherResearchCandidates(rejected, TEACHER_RESEARCH_REJECTED_CANDIDATE_CAP)
            if (compactRejected.length() > 0) {
                compactContext.put("snapshot_rejected_candidates", compactRejected)
            }
        }
        parseJsonObject(context.opt("snapshot_rejected_candidate_stats"))?.let { stats ->
            compactContext.put("snapshot_rejected_candidate_stats", stats)
        }
        parseJsonObject(context.opt("snapshot_build3_gate"))?.let { gate ->
            compactContext.put("snapshot_build3_gate", gate)
        }
        parseJsonObject(context.opt("snapshot_build3_lane_gate"))?.let { gate ->
            compactContext.put("snapshot_build3_lane_gate", gate)
        }
        parseJsonObject(context.opt("snapshot_build3_flow"))?.let { flow ->
            compactContext.put("snapshot_build3_flow", flow)
        }
        parseJsonObject(context.opt("snapshot_pc2_paper_primary"))?.let { policy ->
            compactContext.put("snapshot_pc2_paper_primary", policy)
        }
        parseJsonObject(context.opt("snapshot_pc2_composite_shadow"))?.let { shadow ->
            compactContext.put("snapshot_pc2_composite_shadow", shadow)
        }
        parseJsonObject(context.opt("snapshot_pc2_supply_quality_shadow"))?.let { shadow ->
            compactContext.put("snapshot_pc2_supply_quality_shadow", shadow)
        }
        parseJsonObject(context.opt("snapshot_pc2_batch_f_paper_context"))?.let { batchF ->
            compactContext.put("snapshot_pc2_batch_f_paper_context", batchF)
        }
        val skipReason = context.opt("snapshot_generation_skip_reason")
        if (skipReason != null && skipReason != JSONObject.NULL) {
            compactContext.put("snapshot_generation_skip_reason", skipReason)
        }
        val skipReasons = parseJsonArray(context.opt("snapshot_generation_skip_reasons"))
        if (skipReasons != null && skipReasons.length() > 0) {
            compactContext.put("snapshot_generation_skip_reasons", skipReasons)
        }

        compact.put("context_json", compactContext.toString())
        compact.put("top_candidates_json", compactGenerated.toString())
        return compact
    }

    private fun buildTeacherResearchSnapshotPayload(file: File): JSONArray {
        val compact = JSONArray()
        streamJsonArrayFile(file) { row ->
            compact.put(compactTeacherResearchSnapshot(row))
        }
        return compact
    }

    private fun writeTeacherResearchBridgeInput(file: File, payload: JSONArray) {
        file.parentFile?.mkdirs()
        file.bufferedWriter().use { writer -> writer.write(payload.toString()) }
    }

    private fun compactTeacherResearchOutcomePayload(source: JSONArray): JSONArray {
        val out = JSONArray()
        var rejectedKept = 0
        val keys = arrayOf(
            "snapshot_id",
            "session_date",
            "poll_ts",
            "candidate_id",
            "lane",
            "index_key",
            "trade_mode",
            "strategy_type",
            "sim_pnl_h2",
            "outcome_h2",
            "canonical_won",
            "managed_pnl",
            "managed_gross_pnl",
            "friction_cost",
            "exit_reason",
            "exit_step",
            "exit_ts",
            "path_points_count",
            "r_multiple",
            "captured_pct",
            "is_success",
            "peak_pnl",
            "trough_pnl",
            "max_capture_pct",
            "near_target_pct",
            "target_gap_pnl",
            "time_to_peak_step",
            "target_was_reached",
            "risk_at_entry",
            "regime_bucket",
            "label_version",
            "teacher_config_version",
            "tp_threshold",
            "sl_threshold",
            "break_even_win_rate_pct",
            "price_integrity",
            "h2_price_integrity_reason",
            "premium_edge",
            "credit_width_ratio",
            "sigma_otm",
            "iv_richness",
            "width",
            "prob_profit",
            "rejection_stage",
            "rejection_reason",
            "gate_name",
            "gate_field",
            "observed_value",
            "threshold_value",
            "margin",
            "margin_pct",
            "rejected_rank_in_snapshot",
            "rejected_eval_rank",
            "rejected_eval_cap",
            "rejected_eval_source",
            "source_record_type"
        )
        for (i in 0 until source.length()) {
            val src = source.optJSONObject(i) ?: continue
            val role = src.optString("role", "secondary").trim().lowercase(Locale.US).ifBlank { "secondary" }
            if (role == "rejected") {
                if (rejectedKept >= TEACHER_RESEARCH_REJECTED_OUTCOME_CAP) continue
                rejectedKept += 1
            }
            val row = JSONObject()
            row.put("role", role)
            for (key in keys) {
                val value = src.opt(key)
                if (value != null && value != JSONObject.NULL) row.put(key, value)
            }
            if (row.optString("price_integrity").equals("FAIL", ignoreCase = true)) {
                listOf(
                    "managed_pnl",
                    "managed_gross_pnl",
                    "friction_cost",
                    "r_multiple",
                    "captured_pct",
                    "is_success"
                ).forEach(row::remove)
            }
            out.put(row)
        }
        Log.i(
            TAG,
            "teacher research outcome payload compacted input=${source.length()} output=${out.length()} rejectedKept=$rejectedKept rejectedCap=$TEACHER_RESEARCH_REJECTED_OUTCOME_CAP"
        )
        return out
    }

    private fun loadLocalSavedSnapshots(date: String, limit: Int = 200): JSONArray {
        val file = File(MarketMLService.evaluationSnapshotsPath(context, date))
        if (!file.exists() || file.length() <= 0L) return JSONArray()
        val rows = JSONArray()
        streamJsonArrayFile(file) { row ->
            if (rows.length() < limit) rows.put(row)
        }
        return rows
    }

    private fun logTeacherResearchThrowable(scope: String, t: Throwable) {
        val runtime = Runtime.getRuntime()
        val memory = "mem used=${(runtime.totalMemory() - runtime.freeMemory()) / 1048576}MB max=${runtime.maxMemory() / 1048576}MB"
        Log.e(TAG, "$scope failed throwable=${t.javaClass.name} message=${t.message ?: ""} $memory", t)
    }

    private fun teacherResearchRebuildKey(targetDate: String): String {
        val snapshotsFile = File(MarketMLService.evaluationSnapshotsPath(context, targetDate))
        val outcomesFile = File(MarketMLService.evaluationOutcomesPath(context, targetDate))
        return listOf(
            targetDate,
            snapshotsFile.length().toString(),
            snapshotsFile.lastModified().toString(),
            outcomesFile.length().toString(),
            outcomesFile.lastModified().toString()
        ).joinToString(":")
    }

    private fun scheduleTeacherResearchRebuild(targetDate: String) {
        val rebuildKey = teacherResearchRebuildKey(targetDate)
        if (teacherResearchLastRebuildKey == rebuildKey) return
        if (!teacherResearchRebuildInFlight.compareAndSet(false, true)) return
        teacherResearchLastRebuildKey = rebuildKey
        prefs.edit()
            .putString("teacher_research_report_status", "REBUILDING")
            .remove("teacher_research_report_error")
            .apply()
        teacherResearchScope.launch {
            try {
                Log.i(TAG, "teacher research local rebuild scheduled date=$targetDate")
                val report = rebuildTeacherResearchReportIfPossible(targetDate)
                if (report == null) {
                    teacherResearchLastRebuildKey = ""
                    prefs.edit()
                        .putString("teacher_research_report_status", "FAILED")
                        .putString("teacher_research_report_error", "LOCAL_REPORT_NOT_AVAILABLE")
                        .apply()
                }
            } catch (t: Throwable) {
                teacherResearchLastRebuildKey = ""
                logTeacherResearchThrowable("scheduleTeacherResearchRebuild", t)
                prefs.edit()
                    .putString("teacher_research_report_status", "FAILED")
                    .putString("teacher_research_report_error", t.javaClass.simpleName)
                    .apply()
            } finally {
                teacherResearchRebuildInFlight.set(false)
            }
        }
    }

    private fun rebuildTeacherResearchReportIfPossible(targetDate: String): JSONObject? {
        return try {
            val snapshotsFile = File(MarketMLService.evaluationSnapshotsPath(context, targetDate))
            val outcomesFile = File(MarketMLService.evaluationOutcomesPath(context, targetDate))
            if (!snapshotsFile.exists() || !outcomesFile.exists()) return null
            val compactSnapshots = buildTeacherResearchSnapshotPayload(snapshotsFile)
            val outcomes = compactTeacherResearchOutcomePayload(readJsonArrayFile(outcomesFile))
            if (compactSnapshots.length() == 0 || outcomes.length() == 0) return null

            val inputDir = File(context.cacheDir, "teacher_research_inputs").apply { mkdirs() }
            val snapshotsInput = File.createTempFile("${targetDate}_snapshots_", ".json", inputDir)
            val outcomesInput = File.createTempFile("${targetDate}_outcomes_", ".json", inputDir)
            writeTeacherResearchBridgeInput(snapshotsInput, compactSnapshots)
            writeTeacherResearchBridgeInput(outcomesInput, outcomes)

            val py = Python.getInstance()
            val brain = py.getModule("brain")
            val reportRaw = try {
                brain.callAttr(
                    "session_teacher_research_report",
                    targetDate,
                    snapshotsInput.absolutePath,
                    outcomesInput.absolutePath
                ).toString()
            } finally {
                snapshotsInput.delete()
                outcomesInput.delete()
            }
            val report = JSONObject(reportRaw)
            if (!report.optBoolean("ok", false)) return null

            val outFile = File(MarketMLService.evaluationResearchReportPath(context, targetDate))
            outFile.parentFile?.mkdirs()
            outFile.writeText(report.toString())
            prefs.edit()
                .putString("teacher_research_report_date", targetDate)
                .putString("teacher_research_report", report.toString())
                .putString("teacher_research_report_status", "READY")
                .remove("teacher_research_report_error")
                .commit()
            report
        } catch (e: Throwable) {
            logTeacherResearchThrowable("rebuildTeacherResearchReportIfPossible", e)
            null
        }
    }

    private fun rebuildTeacherResearchReportFromRemoteIfPossible(targetDate: String): JSONObject? {
        return try {
            val snapshots = SupabaseClient.fetchBrainSnapshots(targetDate)
            val outcomes = compactTeacherResearchOutcomePayload(SupabaseClient.fetchEvaluationOutcomesForDate(targetDate))
            if (snapshots.length() <= 0 || outcomes.length() <= 0) return null

            val compactSnapshots = JSONArray()
            for (i in 0 until snapshots.length()) {
                val row = snapshots.optJSONObject(i) ?: continue
                compactSnapshots.put(compactTeacherResearchSnapshot(row))
            }
            if (compactSnapshots.length() <= 0) return null

            val inputDir = File(context.cacheDir, "teacher_research_inputs").apply { mkdirs() }
            val snapshotsInput = File.createTempFile("${targetDate}_remote_snapshots_", ".json", inputDir)
            val outcomesInput = File.createTempFile("${targetDate}_remote_outcomes_", ".json", inputDir)
            writeTeacherResearchBridgeInput(snapshotsInput, compactSnapshots)
            writeTeacherResearchBridgeInput(outcomesInput, outcomes)

            val py = Python.getInstance()
            val brain = py.getModule("brain")
            val reportRaw = try {
                brain.callAttr(
                    "session_teacher_research_report",
                    targetDate,
                    snapshotsInput.absolutePath,
                    outcomesInput.absolutePath
                ).toString()
            } finally {
                snapshotsInput.delete()
                outcomesInput.delete()
            }
            val report = JSONObject(reportRaw)
            if (!report.optBoolean("ok", false)) return null

            val outFile = File(MarketMLService.evaluationResearchReportPath(context, targetDate))
            outFile.parentFile?.mkdirs()
            outFile.writeText(report.toString())
            prefs.edit()
                .putString("teacher_research_report_date", targetDate)
                .putString("teacher_research_report", report.toString())
                .putString("teacher_research_report_status", "READY")
                .remove("teacher_research_report_error")
                .commit()
            report
        } catch (e: Throwable) {
            logTeacherResearchThrowable("rebuildTeacherResearchReportFromRemoteIfPossible", e)
            null
        }
    }

    private fun isTradeModeExplicit(): Boolean = prefs.getBoolean(PREF_TRADE_MODE_EXPLICIT, false)

    @JavascriptInterface
    fun isNative(): Boolean = true

    @JavascriptInterface
    fun startMarketService() {
        clearStaleSessionStateIfNeeded()
        if (!hasTodayBaseline()) {
            Log.w(TAG, "startMarketService skipped: no baseline locked for today")
            LogBuffer.add('W', TAG, "startMarketService skipped: no baseline locked for today")
            prefs.edit().putBoolean("service_running", false).commit()
            return
        }
        val now = System.currentTimeMillis()
        val running = prefs.getBoolean("service_running", false)
        val lastStartReq = prefs.getLong("last_start_req_ms", 0L)
        val sinceLastReq = if (lastStartReq > 0L) now - lastStartReq else Long.MAX_VALUE
        if (running && sinceLastReq in 0 until startDebounceMs) {
            Log.w(TAG, "startMarketService debounced: running=$running sinceLastReqMs=$sinceLastReq")
            LogBuffer.add('W', TAG, "startMarketService debounced: running=$running sinceLastReqMs=$sinceLastReq")
            return
        }
        prefs.edit().putLong("last_start_req_ms", now).commit()
        Log.i(TAG, "startMarketService request: running=$running sinceLastReqMs=$sinceLastReq")
        LogBuffer.add('I', TAG, "startMarketService request: running=$running sinceLastReqMs=$sinceLastReq")
        val intent = Intent(context, MarketWatchService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.startForegroundService(intent)
        } else {
            context.startService(intent)
        }
    }

    @JavascriptInterface
    fun requestImmediatePoll() {
        clearStaleSessionStateIfNeeded()
        if (!hasTodayBaseline()) {
            Log.w(TAG, "requestImmediatePoll skipped: no baseline locked for today")
            LogBuffer.add('W', TAG, "requestImmediatePoll skipped: no baseline locked for today")
            return
        }
        val intent = Intent(context, MarketWatchService::class.java).apply {
            action = MarketWatchService.ACTION_FORCE_POLL
        }
        Log.i(TAG, "requestImmediatePoll")
        LogBuffer.add('I', TAG, "requestImmediatePoll")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.startForegroundService(intent)
        } else {
            context.startService(intent)
        }
    }

    @JavascriptInterface
    fun stopMarketService() {
        try {
            val now = System.currentTimeMillis()
            val lastStopReq = prefs.getLong("last_stop_req_ms", 0L)
            val sinceLastReq = if (lastStopReq > 0L) now - lastStopReq else Long.MAX_VALUE
            if (sinceLastReq in 0 until 1500L) {
                Log.w(TAG, "stopMarketService debounced: sinceLastReqMs=$sinceLastReq")
                LogBuffer.add('W', TAG, "stopMarketService debounced: sinceLastReqMs=$sinceLastReq")
                return
            }
            prefs.edit().putLong("last_stop_req_ms", now).commit()
            // NB1: Use stopService() instead of startService("STOP") to avoid background runtime exceptions
            val intent = Intent(context, MarketWatchService::class.java)
            Log.i(TAG, "stopMarketService request")
            LogBuffer.add('I', TAG, "stopMarketService request")
            context.stopService(intent)
            
            // Explicitly update running flag for immediate UI response
            prefs.edit().putBoolean("service_running", false).commit()
        } catch (e: Exception) {
            Log.e(TAG, "stopMarketService failed: ${e.message}")
        }
    }

    @JavascriptInterface
    fun sendNotification(title: String, body: String, type: String) {
        if (!type.startsWith("ops_")) {
            Log.w(TAG, "sendNotification blocked for non-operational type=$type title=$title")
            LogBuffer.add('W', TAG, "JS_NOTIFICATION_BLOCKED: type=$type title=$title")
            return
        }
        NotificationHelper.send(context, title, body, type.removePrefix("ops_"))
    }

    // --- NEW: Data Push (JS -> Kotlin) ---

    @JavascriptInterface
    fun setApiToken(token: String) {
        // commit() not apply() — must be on disk before next poll reads it
        val ok = prefs.edit().putString("auth_token", token).commit()
        // Verify: re-read from a fresh SharedPreferences instance
        val verify = context.applicationContext
            .getSharedPreferences("market_radar", Context.MODE_PRIVATE)
            .getString("auth_token", null)
        Log.i("NativeBridge", "setApiToken: commit=$ok, stored=${token.length} chars, readback=${verify?.length ?: "NULL"}")
        MarketOpenScheduler.scheduleNextMarketOpen(context)
        MarketOpenScheduler.maybeStartIngestionNow(context, "token_update")
    }

    @JavascriptInterface
    fun setOpenTrades(json: String) {
        openTradesCache = json
        openTradesCacheMs = System.currentTimeMillis()
        val last = prefs.getString("open_trades", "")
        if (json == last) return
        prefs.edit().putString("open_trades", json).commit()
        PositionTickService.ensureRunning(context)
    }

    @JavascriptInterface
    fun setBaseline(json: String) {
        val normalized = try {
            val obj = JSONObject(json)
            if (obj.optString("date", "").isBlank()) {
                obj.put("date", todayIstDate())
            }
            obj.toString()
        } catch (e: Exception) {
            json
        }
        val last = prefs.getString("morning_baseline", "")
        if (normalized == last) return
        prefs.edit()
            .putString("morning_baseline", normalized)
            .remove("brain_result")
            .remove("candidates")
            .putString("last_poll_date", todayIstDate())
            .commit()
    }

    @JavascriptInterface
    fun setMorningInput(json: String): String {
        return try {
            val obj = JSONObject(json)
            val missing = missingNumericFields(
                obj,
                listOf(
                    "fiiCash" to "FII Cash",
                    "fiiShortPct" to "FII Short %",
                    "diiCash" to "DII Cash",
                    "fiiIdxFut" to "FII Idx Fut",
                    "fiiStkFut" to "FII Stk Fut",
                    "dowClose" to "Dow Close",
                    "crudeSettle" to "Crude Settle",
                    "giftSpot" to "GIFT Spot"
                )
            )
            if (missing.isNotEmpty()) return bridgeFail("Missing required morning input: ${missing.joinToString(", ")}")

            val token = (prefs.getString("auth_token", "") ?: "").trim()
            if (token.isEmpty()) {
                return bridgeFail("Upstox token missing. Please paste token to enable auto polling and morning baseline lock.")
            }

            val bnfExpiry = resolveNearestExpiry("NSE_INDEX|Nifty Bank", token)
            val nfExpiry = resolveNearestExpiry("NSE_INDEX|Nifty 50", token)
            if (bnfExpiry == null || nfExpiry == null) {
                return bridgeFail("Failed to discover valid expiries from Upstox. Check token/connectivity.")
            }

            val live = fetchLiveIndexQuotes(token)
                ?: return bridgeFail("Failed to fetch live quotes from Upstox.")
            if (live.bnfSpot <= 0.0 || live.nfSpot <= 0.0 || live.vix <= 0.0) {
                return bridgeFail("Invalid live quotes from Upstox.")
            }

            obj.put("bnfSpot", live.bnfSpot)
            obj.put("nfSpot", live.nfSpot)
            obj.put("vix", live.vix)
            obj.put("bnfExpiry", bnfExpiry)
            obj.put("nfExpiry", nfExpiry)
            obj.put("date", todayIstDate())

            val globalDirection = try {
                JSONObject(prefs.getString("global_direction", "{}") ?: "{}")
            } catch (_: Exception) {
                JSONObject()
            }
            globalDirection.put("dowClose", obj.optDouble("dowClose"))
            globalDirection.put("crudeSettle", obj.optDouble("crudeSettle"))
            globalDirection.put("_date", todayIstDate())

            prefs.edit()
                .putString("morning_input", obj.toString())
                .putString("morning_baseline", obj.toString())
                .putString("global_direction", globalDirection.toString())
                .putString("expiry_bnf", bnfExpiry)
                .putString("expiry_nf", nfExpiry)
                .remove("brain_result")
                .remove("candidates")
                .putString("last_poll_date", todayIstDate())
                .commit()
            bridgeOk()
        } catch (e: Exception) {
            bridgeFail("Invalid morning input: ${e.message}")
        }
    }

    @JavascriptInterface
    fun setEveningClose(json: String): String {
        return try {
            val obj = JSONObject(json)
            val missing = missingNumericFields(
                obj,
                listOf(
                    "dow" to "Dow Close",
                    "crude" to "Crude Settle",
                    "gift" to "GIFT Close"
                )
            )
            if (missing.isNotEmpty()) return bridgeFail("Missing required evening close: ${missing.joinToString(", ")}")
            prefs.edit().putString("evening_close_baseline", obj.toString()).commit()
            bridgeOk()
        } catch (e: Exception) {
            bridgeFail("Invalid evening close: ${e.message}")
        }
    }

    @JavascriptInterface
    fun setExpiries(bnf: String, nf: String) {
        val today = todayIstDate()
        val trimmedBnf = bnf.trim()
        val trimmedNf = nf.trim()
        if (!isUsableExpiry(trimmedBnf, today) || !isUsableExpiry(trimmedNf, today)) {
            Log.w(
                "NativeBridge",
                "IGNORING_INVALID_EXPIRIES: bnf='$trimmedBnf' nf='$trimmedNf' today=$today"
            )
            return
        }
        prefs.edit().apply {
            putString("expiry_bnf", trimmedBnf)
            putString("expiry_nf", trimmedNf)
        }.commit()
    }

    @JavascriptInterface
    fun setContext(json: String) {
        var finalJson = json
        try {
            val ctxObj = JSONObject(finalJson)
            val modeFromCtx = normalizeTradeMode(ctxObj.optString("tradeMode", ""))
            val modeFromPrefs = normalizeTradeMode(prefs.getString(PREF_TRADE_MODE, ""))
            val explicit = isTradeModeExplicit()
            val resolvedMode = when {
                explicit && modeFromPrefs.isNotEmpty() -> modeFromPrefs
                modeFromCtx == "intraday" -> modeFromCtx
                modeFromPrefs == "intraday" -> modeFromPrefs
                else -> defaultTradeMode()
            }
            if (ctxObj.optString("tradeMode", "") != resolvedMode) {
                ctxObj.put("tradeMode", resolvedMode)
                finalJson = ctxObj.toString()
            }
            prefs.edit()
                .putString(PREF_TRADE_MODE, resolvedMode)
                .putBoolean(PREF_TRADE_MODE_EXPLICIT, explicit)
                .commit()
            LogBuffer.add('I', TAG, "TRADE_MODE_SET_CONTEXT: mode=$resolvedMode explicit=$explicit pref=${modeFromPrefs.ifEmpty { "none" }} ctx=${modeFromCtx.ifEmpty { "none" }}")
        } catch (e: Exception) {
            Log.w("NativeBridge", "setContext tradeMode normalize failed: ${e.message}")
        }
        try {
            if (isMLModelReady()) {
                val ctxObj = JSONObject(finalJson)
                val candsLite = ctxObj.optJSONArray("candsLite")
                if (candsLite != null && candsLite.length() > 0) {
                    val count = candsLite.length()
                    val firstId = candsLite.getJSONObject(0).optString("id", "")
                    
                    // b116/NB7: Enhanced change-guard (count + firstId + total length)
                    val totalLen = finalJson.length
                    if (count != lastScoredCandCount || firstId != lastScoredFirstCandId || totalLen != lastScoredTotalLen) {
                        for (i in 0 until count) {
                            val cand = candsLite.getJSONObject(i)
                            try {
                                // NB3: Per-iteration try/catch — if one candidate fails, others still score
                                val mlScored = scoreCandidate(cand)
                                if (mlScored != null) {
                                    // NB2: Copy all ML fields, guarding non-finite numbers rejected by JSONObject.
                                    cand.put("p_ml", finiteDouble(mlScored, "p_ml", 0.0))
                                    cand.put("mlAction", mlScored.optString("ml_action"))
                                    cand.put("mlEdge", finiteDouble(mlScored, "ml_edge", 0.0))
                                    cand.put("mlOod", mlScored.optBoolean("ml_ood", false))
                                    cand.put("mlOodFlag", mlScored.optBoolean("ml_ood_flag", mlScored.optBoolean("ml_ood", false)))
                                    cand.put("mlOodConf", finiteDouble(mlScored, "ml_ood_conf", 1.0))
                                    cand.put("mlOodWarn", mlScored.optJSONArray("ml_ood_warn") ?: JSONArray())
                                    cand.put("mlOodBlocked", mlScored.optBoolean("ml_ood_blocked", false))
                                    cand.put("mlRegime", mlScored.optString("ml_regime", ""))
                                }
                            } catch (e: Exception) {
                                Log.w("NativeBridge", "ML scoring failed for cand $i: ${e.message}")
                            }
                        }
                        lastScoredCandCount = count
                        lastScoredFirstCandId = firstId
                        lastScoredTotalLen = totalLen
                        finalJson = ctxObj.toString()
                        Log.d("NativeBridge", "Scored $count WebView candidates via setContext")
                    }
                }
            }
        } catch (e: Exception) {
            Log.w("NativeBridge", "setContext ML scoring failed: ${e.message}")
        }
        val lastCtx = prefs.getString("context", "")
        if (finalJson == lastCtx) return
        prefs.edit().putString("context", finalJson).commit()
    }

    @JavascriptInterface
    fun setGlobalDirection(json: String) {
        try {
            val globalDirection = JSONObject(json)
            val ctxObj = JSONObject(prefs.getString("context", "{}") ?: "{}")
            ctxObj.put("globalDirection", globalDirection)
            prefs.edit()
                .putString("context", ctxObj.toString())
                .putString("global_direction", globalDirection.toString())
                .commit()
        } catch (e: Exception) {
            Log.w(TAG, "setGlobalDirection failed: ${e.message}")
        }
    }

    @JavascriptInterface
    fun setTradeMode(mode: String) {
        persistTradeMode(mode, explicit = true)
    }

    @JavascriptInterface
    fun setTradeModeDefault(mode: String) {
        persistTradeMode(mode, explicit = false)
    }

    private fun persistTradeMode(mode: String, explicit: Boolean) {
        val normalized = normalizeTradeMode(mode).ifEmpty { defaultTradeMode() }
        val editor = prefs.edit()
            .putString(PREF_TRADE_MODE, normalized)
            .putBoolean(PREF_TRADE_MODE_EXPLICIT, explicit)
        try {
            val ctxObj = JSONObject(prefs.getString("context", "{}") ?: "{}")
            ctxObj.put("tradeMode", normalized)
            editor.putString("context", ctxObj.toString())
        } catch (e: Exception) {
            Log.w(TAG, "setTradeMode context update failed: ${e.message}")
        }
        editor.commit()
        LogBuffer.add('I', TAG, "TRADE_MODE_SET: mode=$normalized explicit=$explicit")
    }

    @JavascriptInterface
    fun triggerEvaluationJob(payloadJson: String): String {
        return try {
            val payload = try {
                JSONObject(payloadJson.ifBlank { "{}" })
            } catch (_: Exception) {
                JSONObject()
            }
            if (!payload.has("date_to") || payload.optString("date_to").isBlank()) {
                payload.put("date_to", todayIstDate())
            }
            if (!payload.has("date_from") || payload.optString("date_from").isBlank()) {
                val cal = Calendar.getInstance(TimeZone.getTimeZone("Asia/Kolkata"))
                cal.add(Calendar.DAY_OF_MONTH, -29)
                payload.put(
                    "date_from",
                    SimpleDateFormat("yyyy-MM-dd", Locale.US).apply {
                        timeZone = TimeZone.getTimeZone("Asia/Kolkata")
                    }.format(cal.time)
                )
            }
            if (!payload.has("mode") || payload.optString("mode").isBlank()) {
                payload.put("mode", "branch_evaluation")
            }
            if (!payload.has("index_scope") || payload.optJSONArray("index_scope") == null || payload.optJSONArray("index_scope")?.length() == 0) {
                payload.put("index_scope", JSONArray().put("BNF").put("NF"))
            }
            val raw = oraclePost("/evaluation-jobs", payload.toString())
            val obj = JSONObject(raw)
            obj.put("ok", true)
            obj.put("request_payload", payload)
            obj.put("requested_at", System.currentTimeMillis())
            val jobId = obj.optString("job_id", "")
            if (jobId.isNotBlank()) {
                prefs.edit()
                    .putString("last_evaluator_job_id", jobId)
                    .putLong("last_evaluator_job_started_ms", System.currentTimeMillis())
                    .commit()
            }
            saveEvaluationJobObject(obj)
            obj.toString()
        } catch (e: Exception) {
            bridgeFail("Evaluator trigger failed: ${e.message}")
        }
    }

    @JavascriptInterface
    fun getEvaluationJobStatus(jobId: String): String {
        return try {
            val resolvedJobId = jobId.trim().ifBlank {
                getCachedEvaluationJobObject().optString("job_id", "")
            }
            if (resolvedJobId.isBlank()) return bridgeFail("No evaluator job id available")
            val raw = oracleGet("/evaluation-jobs/$resolvedJobId")
            val obj = JSONObject(raw)
            obj.put("ok", true)
            val cached = getCachedEvaluationJobObject()
            if (cached.has("request_payload") && !obj.has("request_payload")) {
                obj.put("request_payload", cached.optJSONObject("request_payload"))
            }
            if (cached.has("requested_at") && !obj.has("requested_at")) {
                obj.put("requested_at", cached.optLong("requested_at"))
            }
            obj.put("updated_at", System.currentTimeMillis())
            saveEvaluationJobObject(obj)
            obj.toString()
        } catch (e: Exception) {
            bridgeFail("Evaluator status failed: ${e.message}")
        }
    }

    @JavascriptInterface
    fun getEvaluationJobProposals(jobId: String): String {
        return try {
            val resolvedJobId = jobId.trim().ifBlank {
                getCachedEvaluationJobObject().optString("job_id", "")
            }
            if (resolvedJobId.isBlank()) return bridgeFail("No evaluator job id available")
            val raw = oracleGet("/evaluation-jobs/$resolvedJobId/proposals")
            val obj = JSONObject(raw)
            obj.put("ok", true)
            obj.toString()
        } catch (e: Exception) {
            bridgeFail("Evaluator proposals failed: ${e.message}")
        }
    }

    @JavascriptInterface
    fun getApprovedBranchProposals(): String {
        return try {
            readApprovedBranchProposals(force = false).toString()
        } catch (e: Exception) {
            Log.e(TAG, "getApprovedBranchProposals failed", e)
            prefs.getString(PREF_APPROVED_BRANCH_PROPOSALS, "[]") ?: "[]"
        }
    }

    @JavascriptInterface
    fun refreshApprovedBranchProposals(): String {
        return try {
            readApprovedBranchProposals(force = true).toString()
        } catch (e: Exception) {
            bridgeFail("Approved proposal refresh failed: ${e.message}")
        }
    }

    @JavascriptInterface
    fun getCachedEvaluationJob(): String {
        return getCachedEvaluationJobObject().toString()
    }

    @JavascriptInterface
    fun approveBranchProposal(rowId: String): String {
        return updateBranchProposalStatusInternal(rowId, "approved")
    }

    @JavascriptInterface
    fun rejectBranchProposal(rowId: String): String {
        return updateBranchProposalStatusInternal(rowId, "rejected")
    }

    @JavascriptInterface
    fun getTradeMode(): String {
        val stored = normalizeTradeMode(prefs.getString(PREF_TRADE_MODE, ""))
        return when {
            isTradeModeExplicit() && stored.isNotEmpty() -> stored
            stored == "intraday" -> stored
            else -> defaultTradeMode()
        }
    }

    @JavascriptInterface
    fun getTradeModeExplicit(): Boolean = isTradeModeExplicit()

    @JavascriptInterface
    fun setClosedTrades(json: String) {
        val last = prefs.getString("closed_trades", "")
        if (json == last) return
        prefs.edit().putString("closed_trades", json).commit()
    }

    // --- NEW: Data Pull (JS -> Kotlin) ---

    @JavascriptInterface
    fun getLatestPoll(): String {
        clearStaleSessionStateIfNeeded()
        if (!hasTodaySession()) return "null"
        return prefs.getString("latest_poll", "null") ?: "null"
    }

    @JavascriptInterface
    fun getPollHistory(): String {
        clearStaleSessionStateIfNeeded()
        if (!hasTodaySession()) return "[]"
        return prefs.getString("poll_history", "[]") ?: "[]"
    }

    @JavascriptInterface
    fun getBrainResult(): String {
        clearStaleSessionStateIfNeeded()
        if (!hasTodaySession()) return "null"
        return prefs.getString("brain_result", "null") ?: "null"
    }

    @JavascriptInterface
    fun getNotificationTransportMode(): String {
        return prefs.getString(PREF_NOTIFICATION_TRANSPORT_MODE, "live") ?: "live"
    }

    @JavascriptInterface
    fun setNotificationTransportMode(mode: String): Boolean {
        val normalized = when (mode.trim().lowercase(Locale.US)) {
            "shadow" -> "shadow"
            else -> "live"
        }
        return prefs.edit().putString(PREF_NOTIFICATION_TRANSPORT_MODE, normalized).commit()
    }

    @JavascriptInterface
    fun getStage2AGuardMode(): String {
        val stored = prefs.getString(PREF_STAGE2A_MODE, "paper") ?: "paper"
        return when (stored.trim().lowercase(Locale.US)) {
            "off" -> "off"
            "live" -> "live"
            "shadow" -> "shadow"
            else -> "paper"
        }
    }

    @JavascriptInterface
    fun setStage2AGuardMode(mode: String): Boolean {
        val normalized = when (mode.trim().lowercase(Locale.US)) {
            "off" -> "off"
            "live" -> "live"
            "shadow" -> "shadow"
            else -> "paper"
        }
        return prefs.edit().putString(PREF_STAGE2A_MODE, normalized).commit()
    }

    @JavascriptInterface
    fun getServiceStatus(): String {
        return try {
            clearStaleSessionStateIfNeeded()
            // NB6: Build JSON using JSONObject to avoid injection/escaping issues
            val status = JSONObject()
            val activeToday = hasTodaySession()
            val today = todayIstDate()
            val doneDate = prefs.getString("evaluation_done_date", "") ?: ""
            clearStaleEvaluationRunningIfNeeded()
            val runningDate = prefs.getString("evaluation_running_date", "") ?: ""
            val targetDate = latestEligibleEvaluationDate(today)
            val evaluationPhase = prefs.getString("evaluation_phase", "") ?: ""
            val evaluationCompleted = prefs.getInt("evaluation_completed_snapshots", 0)
            val evaluationTotal = prefs.getInt("evaluation_total_snapshots", 0)
            val marketClock = MarketOpenScheduler.currentStatus()
            val coverage = currentPollCoverage(marketClock)
            targetDate?.let { repairIncompleteSessionStateIfNeeded(it, coverage) }
            targetDate?.let { repairStaleResearchStateIfNeeded(it) }
            val retryEvaluation = targetDate?.let { shouldRetryDayEvaluation(it) } == true
            val serviceRunning = isServiceRunning()
            val coverageIntegrity = currentCoverageIntegrity(targetDate ?: today, coverage)
            val coverageIntegrityIssue = currentCoverageIntegrityIssue(targetDate ?: today)
            val integrityBroken = coverageIntegrity == "INTEGRITY_BROKEN"
            val forceEvaluationAllowed = !serviceRunning && !marketClock.marketOpen && !targetDate.isNullOrBlank() && integrityBroken
            val targetIsToday = targetDate == today
            val historicalReady = !targetDate.isNullOrBlank() && !targetIsToday
            val targetSessionRunnable = historicalReady || (activeToday && coverage.actual > 0 && !integrityBroken)
            val evaluationReady = !serviceRunning &&
                !marketClock.marketOpen &&
                !targetDate.isNullOrBlank() &&
                targetSessionRunnable &&
                runningDate != targetDate &&
                (retryEvaluation || (targetSessionRunnable && doneDate != targetDate))
            val evaluationBlockedReason = when {
                retryEvaluation -> ""
                serviceRunning -> "WAIT_FOR_POST_CLOSE_HANDOFF"
                marketClock.marketOpen -> "MARKET_OPEN"
                targetDate.isNullOrBlank() -> "NO_SESSION"
                targetIsToday && (!activeToday || coverage.actual <= 0) -> "NO_SESSION"
                integrityBroken -> "INCOMPLETE_SESSION"
                runningDate == targetDate -> "RUNNING"
                doneDate == targetDate -> "DONE"
                else -> ""
            }
            status.put("running", serviceRunning)
            status.put("sessionActive", activeToday)
            status.put("lastPoll", if (activeToday) prefs.getString("last_poll_time", "Never") else "Never")
            status.put("polls", coverage.actual.coerceAtMost(coverage.expectedFullDay))
            status.put("tokenReady", !(prefs.getString("auth_token", "") ?: "").isBlank())
            status.put("marketDay", marketClock.marketDay)
            status.put("marketOpen", marketClock.marketOpen)
            status.put("marketReason", marketClock.reason)
            status.put("autoStartAt", MarketOpenScheduler.nextScheduledAtMs(context))
            status.put("expectedPollsByNow", coverage.expectedByNow)
            status.put("expectedPollsFullDay", coverage.expectedFullDay)
            status.put("actualPollsToday", coverage.actual)
            status.put("missedPollsToday", coverage.missed)
            status.put("pollCoverageState", coverage.state)
            status.put("coverageIntegrity", coverageIntegrity)
            status.put("coverageIntegrityIssue", coverageIntegrityIssue)
            status.put("evaluationPromotionEligible", coverageIntegrity in setOf("COMPLETE", "COMPLETE_WITH_RETRIES", "PARTIAL"))
            status.put("evaluationTargetDate", targetDate ?: "")
            status.put("evaluationTargetIsToday", targetIsToday)
            status.put("evaluationReady", evaluationReady)
            status.put("evaluationBlockedReason", evaluationBlockedReason)
            status.put("evaluationDoneToday", doneDate == today)
            status.put("evaluationDoneForTarget", !targetDate.isNullOrBlank() && doneDate == targetDate)
            status.put("evaluationDoneDate", doneDate)
            status.put("evaluationRunning", !targetDate.isNullOrBlank() && runningDate == targetDate)
            status.put("evaluationPhase", evaluationPhase)
            status.put("evaluationProgressCurrent", evaluationCompleted)
            status.put("evaluationProgressTotal", evaluationTotal)
            status.put("evaluationRetryRecommended", retryEvaluation)
            status.put("evaluationRetryable", retryEvaluation)
            status.put("evaluationForceAllowed", forceEvaluationAllowed)
            status.put("evaluationLastError", prefs.getString("evaluation_last_error", "") ?: "")
            status.put("evaluationAlarmFiredDate", prefs.getString("evaluation_alarm_fired_date", "") ?: "")
            status.put("evaluationAlarmFiredAtMs", prefs.getLong("evaluation_alarm_fired_at_ms", 0L))
            status.put("evaluationAutoStartDate", prefs.getString("evaluation_auto_start_date", "") ?: "")
            status.put("evaluationAutoStartAtMs", prefs.getLong("evaluation_auto_start_at_ms", 0L))
            status.put("evaluationAutoStartStatus", prefs.getString("evaluation_auto_start_status", "") ?: "")
            status.put("evaluationAutoStartError", prefs.getString("evaluation_auto_start_error", "") ?: "")
            status.put("teacherResearchStatus", prefs.getString("teacher_research_report_status", "") ?: "")
            status.put("teacherResearchError", prefs.getString("teacher_research_report_error", "") ?: "")
            status.put("evaluationUpdatedAtMs", prefs.getLong("evaluation_job_updated_at_ms", 0L))
            status.put("lastEvaluationOutcomeCount", prefs.getInt("last_evaluation_outcome_count", 0))
            status.put("lastEvaluationProducedCount", prefs.getInt("last_evaluation_produced_count", 0))
            val lastEvaluationMessage = prefs.getString("last_evaluation_message", "") ?: ""
            status.put("lastEvaluationMessage", lastEvaluationMessage)
            status.put("c3FinalizationDate", prefs.getString("c3_finalization_date", "") ?: "")
            status.put("c3FinalizationPhase", prefs.getString("c3_finalization_phase", "") ?: "")
            status.put("c3FinalizationMessage", prefs.getString("c3_finalization_message", "") ?: "")
            status.put("c3FinalizationRunning", prefs.getBoolean("c3_finalization_running", false))
            status.put("c3FinalizationFrames", prefs.getInt("c3_finalization_frame_count", 0))
            status.put("c3FinalizationRows", prefs.getInt("c3_finalization_row_count", 0))
            status.put("c3FinalizationVerifiedRows", prefs.getInt("c3_finalization_verified_rows", 0))
            status.put("c3FinalizationError", prefs.getString("c3_finalization_last_error", "") ?: "")
            status.put("c3FinalizationUpdatedAtMs", prefs.getLong("c3_finalization_updated_at_ms", 0L))
            status.toString()
        } catch (e: Exception) {
            "{\"running\": false, \"error\": \"Internal failure\"}"
        }
    }

    @JavascriptInterface
    fun getCandidates(): String {
        clearStaleSessionStateIfNeeded()
        if (!hasTodaySession()) return "[]"
        return prefs.getString("candidates", "[]") ?: "[]"
    }

    private fun isServiceRunning(): Boolean {
        // This is a simplified check. A more robust check might query ActivityManager, 
        // but for now we'll rely on the service itself setting a flat in SharedPreferences.
        return prefs.getBoolean("service_running", false)
    }

    // Method 15: ML model status
    @JavascriptInterface
    fun getMLModelStatus(): String {
        return try {
            val py = com.chaquo.python.Python.getInstance()
            val mod = py.getModule("ml_train")
            val modelPath = File(context.filesDir, "ml_model.json").absolutePath
            val result = runBlocking {
                withTimeoutOrNull(PY_VALIDATE_TIMEOUT_MS) {
                    mod.callAttr("validate_model", modelPath).toString()
                }
            }
            if (result == null) {
                Log.w(TAG, "ML_VALIDATE_TIMEOUT: validate_model exceeded ${PY_VALIDATE_TIMEOUT_MS}ms")
                "{\"ok\":false,\"error\":\"validate_model timeout\"}"
            } else {
                result
            }
        } catch (e: Exception) {
            "{\"ok\":false,\"error\":\"${e.message}\"}"
        }
    }

    // Method 16: Trigger online update after trade closes
    @JavascriptInterface
    fun triggerMLOnlineUpdate(tradeJson: String) {
        try {
            val intent = android.content.Intent(context, MarketMLService::class.java).apply {
                action = "ACTION_ONLINE_UPDATE"
                putExtra("trade_json", tradeJson)
            }
            context.startForegroundService(intent)
        } catch (e: Exception) {
            android.util.Log.w("NativeBridge", "ML online update failed: ${e.message}")
        }
    }

    // Method 17: Check if model is loaded and ready
    @JavascriptInterface
    fun isMLModelReady(): Boolean {
        return File(context.filesDir, "ml_model.json").exists()
    }

    // Method 18: Manual ML retrain — checks trade count, shows confirmation notification
    @JavascriptInterface
    fun triggerMLRetrain() {
        try {
            val intent = android.content.Intent(context, MarketMLService::class.java).apply {
                action = "ACTION_CHECK_RETRAIN"
            }
            context.startForegroundService(intent)
        } catch (e: Exception) {
            android.util.Log.w("NativeBridge", "ML retrain trigger failed: ${e.message}")
        }
    }

    @JavascriptInterface
    fun triggerDayEvaluation(): String {
        return triggerDayEvaluationInternal(forceAnyway = false)
    }

    @JavascriptInterface
    fun triggerDayEvaluationForce(): String {
        return triggerDayEvaluationInternal(forceAnyway = true)
    }

    private fun triggerDayEvaluationInternal(forceAnyway: Boolean): String {
        return try {
            val today = todayIstDate()
            clearStaleEvaluationRunningIfNeeded()
            val targetDate = latestEligibleEvaluationDate(today)
                ?: return JSONObject().apply {
                    put("ok", false)
                    put("status", "blocked")
                    put("message", "No completed market session is available for teacher evaluation yet.")
                }.toString()
            val retryEvaluation = shouldRetryDayEvaluation(targetDate)
            val marketClock = MarketOpenScheduler.currentStatus()
            val coverage = currentPollCoverage(marketClock)
            val coverageIntegrity = currentCoverageIntegrity(targetDate, coverage)
            val coverageIntegrityIssue = currentCoverageIntegrityIssue(targetDate)
            val serviceRunning = isServiceRunning()
            if (serviceRunning) {
                return JSONObject().apply {
                    put("ok", false)
                    put("status", "blocked")
                    put("message", "Day evaluation is blocked while the live watch service is still active. Wait for the post-close handoff or retry after the service stops.")
                }.toString()
            }
            if (marketClock.marketOpen) {
                return JSONObject().apply {
                    put("ok", false)
                    put("status", "blocked")
                    put("message", "Day evaluation is available only after market close.")
                }.toString()
            }
            if (targetDate == today && coverage.actual <= 0) {
                return JSONObject().apply {
                    put("ok", false)
                    put("status", "blocked")
                    put("message", "No completed market session was found for today yet.")
                }.toString()
            }
            if (coverageIntegrity == "INTEGRITY_BROKEN" && !retryEvaluation && !forceAnyway) {
                return JSONObject().apply {
                    put("ok", false)
                    put("status", "blocked")
                    put("message", "Day evaluation is blocked because this session integrity is broken (${coverageIntegrityIssue.ifBlank { "UNKNOWN" }}). Normal teacher evaluation is disabled for incomplete sessions.")
                }.toString()
            }
            if (prefs.getString("evaluation_done_date", "") == targetDate && !retryEvaluation) {
                return JSONObject().apply {
                    put("ok", true)
                    put("status", "done")
                    put("target_date", targetDate)
                    put("message", if (targetDate == today) "Today's evaluation already done." else "Evaluation already done for $targetDate.")
                    put("outcomes", prefs.getInt("last_evaluation_outcome_count", 0))
                    put("produced", prefs.getInt("last_evaluation_produced_count", 0))
                }.toString()
            }
            if (prefs.getString("evaluation_running_date", "") == targetDate) {
                val phase = prefs.getString("evaluation_phase", "") ?: "RUNNING"
                val completed = prefs.getInt("evaluation_completed_snapshots", 0)
                val total = prefs.getInt("evaluation_total_snapshots", 0)
                return JSONObject().apply {
                    put("ok", true)
                    put("status", "running")
                    put("target_date", targetDate)
                    put("message", "Evaluation for $targetDate is already running (${phase.lowercase(Locale.US)} $completed/$total).")
                }.toString()
            }
            prefs.edit()
                .putString("evaluation_done_date", "")
                .putString("evaluation_running_date", targetDate)
                .putString("evaluation_phase", "QUEUED")
                .putString("evaluation_job_date", targetDate)
                .putLong("evaluation_job_updated_at_ms", System.currentTimeMillis())
                .putString("teacher_research_report_status", "PENDING")
                .remove("teacher_research_report_error")
                .putString(
                    "last_evaluation_message",
                    if (forceAnyway) {
                        "Forced evaluation queued for $targetDate. Results from incomplete sessions are advisory only and excluded from promotion gates."
                    } else if (retryEvaluation) {
                        "Evaluation recovery queued for $targetDate. It will resume from the last completed batch if local progress exists."
                    } else {
                        "Evaluation queued for $targetDate..."
                    }
                )
                .commit()
            val intent = android.content.Intent(context, MarketMLService::class.java).apply {
                action = "ACTION_DAY_EVALUATION"
                putExtra("session_date", targetDate)
                putExtra("force_anyway", forceAnyway)
            }
            context.startForegroundService(intent)
            JSONObject().apply {
                put("ok", true)
                put("target_date", targetDate)
                put("status", if (forceAnyway) "forced" else if (retryEvaluation) "restarted" else "started")
                put("message", if (forceAnyway) "Forced day evaluation started for $targetDate." else if (retryEvaluation) "Day evaluation recovery started for $targetDate." else "Day evaluation started for $targetDate.")
            }.toString()
        } catch (e: Exception) {
            prefs.edit()
                .putString("evaluation_running_date", "")
                .putString("last_evaluation_message", "Evaluation trigger failed: ${e.message}")
                .commit()
            android.util.Log.w("NativeBridge", "Day evaluation trigger failed: ${e.message}", e)
            JSONObject().apply {
                put("ok", false)
                put("status", "failed")
                put("message", "Day evaluation trigger failed: ${e.message}")
            }.toString()
        }
    }

    @JavascriptInterface
    fun setExecutionSandboxEnabled(enabled: Boolean): Boolean {
        val ok = prefs.edit().putBoolean(PREF_SANDBOX_ENABLED, enabled).commit()
        if (!ok) return false
        return persistDerivedExecutionMode()
    }

    @JavascriptInterface
    fun getExecutionSandboxEnabled(): Boolean {
        return prefs.getBoolean(PREF_SANDBOX_ENABLED, false)
    }

    @JavascriptInterface
    fun setSandboxToken(token: String): Boolean {
        return prefs.edit().putString(PREF_SANDBOX_TOKEN, token.trim()).commit()
    }

    @JavascriptInterface
    fun getSandboxTokenReady(): Boolean {
        return !(prefs.getString(PREF_SANDBOX_TOKEN, "") ?: "").isBlank()
    }

    @JavascriptInterface
    fun runSandboxOrderDebugAction(payloadJson: String): String {
        return try {
            val payload = if (payloadJson.isBlank()) JSONObject() else JSONObject(payloadJson)
            val token = prefs.getString(PREF_SANDBOX_TOKEN, "") ?: ""
            OrderExecutionService.runDebugAction(payload, token).toString()
        } catch (e: Exception) {
            JSONObject()
                .put("ok", false)
                .put("mode", OrderExecutionService.EXECUTION_MODE)
                .put("error_code", "BRIDGE_EXCEPTION")
                .put("error_message", e.message ?: e.javaClass.simpleName)
                .toString()
        }
    }

    @JavascriptInterface
    fun setOrderProxyUrl(url: String): Boolean {
        val cleaned = url.trim()
        val ok = prefs.edit().putString(PREF_ORDER_PROXY_URL, cleaned).commit()
        if (!ok) return false
        return persistDerivedExecutionMode()
    }

    @JavascriptInterface
    fun getOrderProxyUrl(): String {
        return prefs.getString(PREF_ORDER_PROXY_URL, "") ?: ""
    }

    @JavascriptInterface
    fun getExecutionInfraStatus(): String {
        return try {
            val brainRaw = prefs.getString("brain_result", "null") ?: "null"
            val brainResult = try { JSONObject(brainRaw) } catch (e: Exception) { JSONObject() }
            val keyStats = extractInstrumentKeyStats(brainResult)

            val tokenReady = !(prefs.getString("auth_token", "") ?: "").isBlank()
            val sandboxTokenReady = !(prefs.getString(PREF_SANDBOX_TOKEN, "") ?: "").isBlank()
            val sandboxEnabled = prefs.getBoolean(PREF_SANDBOX_ENABLED, false)
            val proxyUrl = (prefs.getString(PREF_ORDER_PROXY_URL, "") ?: "").trim()
            val proxyConfigured = proxyUrl.startsWith("https://")

            val out = JSONObject()
            out.put("instrumentKeyRows", keyStats.first)
            out.put("instrumentKeyPresentRows", keyStats.second)
            out.put("instrumentKeyFlowOk", keyStats.first > 0 && keyStats.second > 0)
            out.put("sandboxEnabled", sandboxEnabled)
            out.put("proxyConfigured", proxyConfigured)
            out.put("proxyUrl", proxyUrl)
            out.put("tokenReady", tokenReady)
            out.put("sandboxTokenReady", sandboxTokenReady)
            out.put("paperReady", true)
            out.put("sandboxReady", sandboxTokenReady && sandboxEnabled)
            out.put("liveReady", tokenReady && proxyConfigured)
            out.toString()
        } catch (e: Exception) {
            "{\"instrumentKeyFlowOk\":false,\"sandboxEnabled\":false,\"proxyConfigured\":false,\"tokenReady\":false,\"paperReady\":true,\"sandboxReady\":false,\"liveReady\":false,\"error\":\"${e.message}\"}"
        }
    }

    @JavascriptInterface
    fun getLogBuffer(filterJson: String?): String {
        val filter = if (filterJson.isNullOrBlank()) null else {
            try { JSONObject(filterJson).optString("filter", null) }
            catch (e: Exception) { null }
        }
        val entries = LogBuffer.snapshot(filter)
        val arr = JSONArray()
        for (e in entries) {
            arr.put(JSONObject().apply {
                put("ts", e.timestampMs)
                put("level", e.level.toString())
                put("tag", e.tag)
                put("msg", e.message)
            })
        }
        return arr.toString()
    }

    @JavascriptInterface
    fun getLastCrashReport(): String {
        return try {
            (LogBuffer.lastCrashReport() ?: JSONObject()).toString()
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: e.javaClass.simpleName).toString()
        }
    }

    @JavascriptInterface
    fun clearLastCrashReport(): Boolean {
        LogBuffer.clearCrashReport()
        return true
    }

    @JavascriptInterface
    fun reportJsCrash(payloadJson: String?): Boolean {
        return try {
            val payload = if (payloadJson.isNullOrBlank()) JSONObject() else JSONObject(payloadJson)
            val message = buildString {
                append(payload.optString("type", "JS_ERROR"))
                val msg = payload.optString("message", "")
                if (msg.isNotBlank()) append(": $msg")
                val source = payload.optString("source", "")
                if (source.isNotBlank()) append(" @ $source")
                val line = payload.optInt("line", -1)
                if (line >= 0) append(":$line")
                val col = payload.optInt("column", -1)
                if (col >= 0) append(":$col")
                val stack = payload.optString("stack", "")
                if (stack.isNotBlank()) append("\n$stack")
            }
            LogBuffer.recordCrash("WebViewJS", message, extra = payload)
            true
        } catch (e: Exception) {
            LogBuffer.add('E', TAG, "reportJsCrash failed: ${e.message}")
            false
        }
    }

    @JavascriptInterface
    fun clearLogBuffer(): Boolean {
        LogBuffer.clear()
        LogBuffer.add('I', "NativeBridge", "Log buffer cleared by user")
        return true
    }

    @JavascriptInterface
    fun getLogCaptureMode(): String = LogBuffer.captureMode.name

    @JavascriptInterface
    fun getSignalAccuracyStats(): String {
        return try {
            SupabaseClient.getSignalAccuracyStats().toString()
        } catch (e: Exception) {
            "{}"
        }
    }

    @JavascriptInterface
    fun computeLiveFriction(tradeJson: String): String {
        return try {
            val py = Python.getInstance()
            val brain = py.getModule("brain")
            runBlocking {
                withTimeoutOrNull(PY_SCORE_TIMEOUT_MS) {
                    brain.callAttr("compute_live_friction_bridge", tradeJson.ifBlank { "{}" }).toString()
                }
            } ?: JSONObject()
                .put("friction_cost", JSONObject.NULL)
                .put("friction_reason", "PYTHON_TIMEOUT")
                .put("friction_version", "G2_v1")
                .put("net_pnl", JSONObject.NULL)
                .put("net_won", JSONObject.NULL)
                .toString()
        } catch (e: Exception) {
            JSONObject()
                .put("friction_cost", JSONObject.NULL)
                .put("friction_reason", "BRIDGE_EXCEPTION:${e.message ?: e.javaClass.simpleName}")
                .put("friction_version", "G2_v1")
                .put("net_pnl", JSONObject.NULL)
                .put("net_won", JSONObject.NULL)
                .toString()
        }
    }

    private fun missingNumericFields(obj: JSONObject, fields: List<Pair<String, String>>): List<String> {
        return fields.mapNotNull { (key, label) ->
            val value = obj.optDouble(key, Double.NaN)
            if (!obj.has(key) || obj.isNull(key) || !java.lang.Double.isFinite(value)) label else null
        }
    }

    private fun extractInstrumentKeyStats(poll: JSONObject): Pair<Int, Int> {
        val candidateArrays = listOf("watchlist", "generated_candidates")
        var total = 0
        var withKey = 0
        for (name in candidateArrays) {
            val arr = poll.optJSONArray(name) ?: continue
            for (i in 0 until arr.length()) {
                val candidate = arr.optJSONObject(i) ?: continue
                val legKeys = listOf(
                    "sellInstrumentKey",
                    "buyInstrumentKey",
                    "sellInstrumentKey2",
                    "buyInstrumentKey2"
                )
                for (keyName in legKeys) {
                    if (!candidate.has(keyName) || candidate.isNull(keyName)) continue
                    total++
                    if (candidate.optString(keyName, "").trim().isNotEmpty()) withKey++
                }
            }
            if (total > 0) break
        }
        return Pair(total, withKey)
    }

    private fun persistDerivedExecutionMode(): Boolean {
        val sandboxEnabled = prefs.getBoolean(PREF_SANDBOX_ENABLED, false)
        val proxyUrl = (prefs.getString(PREF_ORDER_PROXY_URL, "") ?: "").trim()
        val mode = when {
            sandboxEnabled -> "sandbox"
            proxyUrl.startsWith("https://") -> "live"
            else -> "paper"
        }
        return prefs.edit().putString("execution_mode", mode).commit()
    }

    private data class LiveQuotes(val bnfSpot: Double, val nfSpot: Double, val vix: Double)

    private fun todayIstDate(): String {
        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        return SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = ist }.format(Date())
    }

    private fun baselineDate(rawBaseline: String = prefs.getString("morning_baseline", "{}") ?: "{}"): String {
        return try {
            JSONObject(rawBaseline).optString("date", "")
        } catch (e: Exception) {
            ""
        }
    }

    private fun baselineHasCoreFields(rawBaseline: String = prefs.getString("morning_baseline", "{}") ?: "{}"): Boolean {
        return try {
            val baseline = JSONObject(rawBaseline)
            baseline.has("bnfSpot") && baseline.has("nfSpot") && baseline.has("vix")
        } catch (_: Exception) {
            false
        }
    }

    private fun hasTodayBaseline(): Boolean {
        return baselineDate() == todayIstDate()
    }

    private fun hasTodaySession(): Boolean {
        if (hasTodayBaseline()) return true
        val today = todayIstDate()
        val lastPollDate = prefs.getString("last_poll_date", "") ?: ""
        val pollCount = prefs.getInt("poll_count", 0)
        return lastPollDate == today && pollCount > 0
    }

    private fun clearStaleEvaluationRunningIfNeeded(): Boolean {
        val runningDate = prefs.getString("evaluation_running_date", "") ?: ""
        if (runningDate.isBlank()) return false
        val phase = prefs.getString("evaluation_phase", "") ?: ""
        val startedAtMs = prefs.getLong("evaluation_started_at_ms", 0L)
        val updatedAtMs = prefs.getLong("evaluation_job_updated_at_ms", startedAtMs)
        val heartbeatMs = maxOf(startedAtMs, updatedAtMs)
        val ageMs = if (heartbeatMs > 0L) System.currentTimeMillis() - heartbeatMs else Long.MAX_VALUE
        val staleAfterMs = MarketMLService.EVAL_STALE_AFTER_MS
        if (ageMs in 0 until staleAfterMs) return false
        val completed = prefs.getInt("evaluation_completed_snapshots", 0)
        val total = prefs.getInt("evaluation_total_snapshots", 0)
        prefs.edit()
            .putString("evaluation_running_date", "")
            .putLong("evaluation_started_at_ms", 0L)
            .putString("evaluation_phase", "STALLED")
            .putLong("evaluation_job_updated_at_ms", System.currentTimeMillis())
            .putString(
                "last_evaluation_message",
                "Evaluation stalled during ${if (phase.isBlank()) "processing" else phase.lowercase(Locale.US)} at $completed/$total snapshots. Retry will resume from the last completed batch."
            )
            .commit()
        Log.i(TAG, "Cleared stale evaluation_running_date for $runningDate after ${ageMs}ms")
        return true
    }

    /**
     * Detects a session saved as DONE whose teacher research artifact is missing
     * or unreadable, then persists FAILED_RESEARCH so retry unlocks on the same render.
     */
    private fun repairStaleResearchStateIfNeeded(targetDate: String) {
        val phase = (prefs.getString("evaluation_phase", "") ?: "").uppercase(Locale.US)
        if (phase != "DONE") return

        val doneDate = prefs.getString("evaluation_done_date", "") ?: ""
        if (doneDate != targetDate) return

        val runningDate = prefs.getString("evaluation_running_date", "") ?: ""
        if (runningDate == targetDate) return

        val reportFile = File(MarketMLService.evaluationResearchReportPath(context, targetDate))
        val hasValidReport = reportFile.exists() && try {
            val obj = JSONObject(reportFile.readText().trim())
            obj.optBoolean("ok", false)
        } catch (_: Exception) {
            false
        }
        if (hasValidReport) return

        prefs.edit()
            .putString("evaluation_phase", "FAILED_RESEARCH")
            .putString("teacher_research_report_status", "FAILED")
            .putString("teacher_research_report_error", "REPORT_NOT_AVAILABLE_REPAIRED")
            .commit()

        Log.i(
            TAG,
            "repairStaleResearchStateIfNeeded: repaired stale DONE→FAILED_RESEARCH for $targetDate (teacher report missing)"
        )
    }

    private fun repairIncompleteSessionStateIfNeeded(targetDate: String, coverage: PollCoverage) {
        val integrity = currentCoverageIntegrity(targetDate, coverage)
        val phase = (prefs.getString("evaluation_phase", "") ?: "").uppercase(Locale.US)
        if (integrity != "INTEGRITY_BROKEN") {
            if (phase == "INCOMPLETE_SESSION") {
                prefs.edit()
                    .remove("evaluation_last_error")
                    .remove("teacher_research_report_error")
                    .putString("evaluation_phase", "READY")
                    .putString("teacher_research_report_status", "PENDING")
                    .putString(
                        "last_evaluation_message",
                        "Session integrity rechecked as $integrity for $targetDate. Post-close evaluation can be retried."
                    )
                    .putLong("evaluation_job_updated_at_ms", System.currentTimeMillis())
                    .commit()
                Log.i("NativeBridge", "repairIncompleteSessionStateIfNeeded: cleared stale incomplete state for $targetDate integrity=$integrity")
            }
            return
        }
        if (phase == "INCOMPLETE_SESSION") return
        val doneDate = prefs.getString("evaluation_done_date", "") ?: ""
        val runningDate = prefs.getString("evaluation_running_date", "") ?: ""
        if (runningDate == targetDate) return
        if (doneDate != targetDate && phase !in setOf("DONE", "FAILED_RESEARCH", "FAILED", "FAILED_SAVE", "STALLED", "")) return
        val issue = currentCoverageIntegrityIssue(targetDate).ifBlank { "INTEGRITY_BROKEN" }
        prefs.edit()
            .remove("evaluation_done_date")
            .putString("evaluation_job_date", targetDate)
            .putString("evaluation_phase", "INCOMPLETE_SESSION")
            .putString("teacher_research_report_status", "NOT_APPLICABLE")
            .putString("teacher_research_report_error", issue)
            .putString("evaluation_last_error", issue)
            .putString(
                "last_evaluation_message",
                "Evaluation blocked for $targetDate because session integrity is broken ($issue). Normal teacher evaluation is disabled for this session."
            )
            .putLong("evaluation_job_updated_at_ms", System.currentTimeMillis())
            .commit()
    }

    private fun latestEligibleEvaluationDate(today: String = todayIstDate()): String? {
        if (hasTodaySession()) return today
        val candidates = listOf(
            prefs.getString("evaluation_running_date", "") ?: "",
            prefs.getString("evaluation_job_date", "") ?: "",
            prefs.getString("last_poll_date", "") ?: "",
            baselineDate(),
            prefs.getString("evaluation_done_date", "") ?: ""
        )
        return candidates.firstOrNull { it.isNotBlank() && it <= today }
    }

    private fun shouldRetryDayEvaluation(targetDate: String): Boolean {
        clearStaleEvaluationRunningIfNeeded()
        val phase = (prefs.getString("evaluation_phase", "") ?: "").uppercase(Locale.US)
        if (phase == "FAILED" || phase == "FAILED_SAVE" || phase == "FAILED_RESEARCH" || phase == "STALLED") {
            return true
        }
        val doneDate = prefs.getString("evaluation_done_date", "") ?: ""
        if (doneDate != targetDate) return false
        val runningDate = prefs.getString("evaluation_running_date", "") ?: ""
        if (runningDate == targetDate) return false
        val teacherResearchStatus = (prefs.getString("teacher_research_report_status", "") ?: "").uppercase(Locale.US)
        if (teacherResearchStatus == "FAILED") return true
        val lastMessage = (prefs.getString("last_evaluation_message", "") ?: "").lowercase(Locale.US)
        val produced = prefs.getInt("last_evaluation_produced_count", 0)
        if (
            produced <= 0 &&
            (
                lastMessage.contains("0 evaluable shadow teacher outcomes") ||
                    lastMessage.contains("no evaluable shadow teacher labels")
            )
        ) {
            return true
        }
        if (!lastMessage.contains("no brain snapshots found")) return false
        val lastPollDate = prefs.getString("last_poll_date", "") ?: ""
        val pollCount = prefs.getInt("poll_count", 0)
        val latestPoll = prefs.getString("latest_poll", "null") ?: "null"
        return lastPollDate == targetDate && (pollCount > 0 || latestPoll != "null")
    }

    private data class PollCoverage(
        val expectedByNow: Int,
        val expectedFullDay: Int,
        val actual: Int,
        val missed: Int,
        val state: String
    )

    private fun currentCoverageIntegrity(targetDate: String, coverage: PollCoverage): String {
        val integrityDate = prefs.getString("coverage_integrity_date", "") ?: ""
        if (integrityDate == targetDate) {
            val stored = prefs.getString("coverage_integrity", "") ?: ""
            if (stored.isNotBlank()) {
                val issue = currentCoverageIntegrityIssue(targetDate)
                return when (stored.uppercase(Locale.US)) {
                    "CLEAN" -> "COMPLETE"
                    "PARTIAL_COVERAGE" -> "PARTIAL"
                    "INTEGRITY_BROKEN" -> normalizeBrokenCoverageIntegrity(issue)
                    else -> stored
                }
            }
        }
        val derivedIssue = deriveCoverageIntegrityIssue(targetDate)
        if (derivedIssue.isNotBlank()) {
            return when (derivedIssue) {
                "MISSED_SLOTS" -> "PARTIAL"
                "COUNTER_DRIFT" -> "COMPLETE_WITH_RETRIES"
                "SNAPSHOT_OVERRUN" -> "COMPLETE_WITH_RETRIES"
                "NONE" -> "COMPLETE"
                else -> "INTEGRITY_BROKEN"
            }
        }
        return when {
            coverage.actual <= 0 -> "NONE"
            coverage.missed > 0 -> "PARTIAL"
            else -> "COMPLETE"
        }
    }

    private fun currentCoverageIntegrityIssue(targetDate: String): String {
        val integrityDate = prefs.getString("coverage_integrity_date", "") ?: ""
        if (integrityDate == targetDate) {
            val storedIssue = prefs.getString("coverage_integrity_issue", "") ?: ""
            return storedIssue.ifBlank { deriveCoverageIntegrityIssue(targetDate) }
        }
        return deriveCoverageIntegrityIssue(targetDate)
    }

    private fun normalizeBrokenCoverageIntegrity(issue: String): String {
        return when (issue.uppercase(Locale.US)) {
            "", "NONE" -> "COMPLETE"
            "SNAPSHOT_OVERRUN" -> "COMPLETE_WITH_RETRIES"
            else -> "INTEGRITY_BROKEN"
        }
    }

    private fun deriveCoverageIntegrityIssue(targetDate: String): String {
        val lastPollDate = prefs.getString("last_poll_date", "") ?: ""
        if (lastPollDate != targetDate) return ""
        val pollCount = countDistinctSlotOrdinalsForDate(targetDate)
        val rawPollCount = prefs.getInt("poll_count", 0)
        val finalSlotCount = countSlotOccurrencesForDate(targetDate, POLL_FULL_DAY_SLOTS)
        val snapshotRows = countLocalSnapshotRows(targetDate)
        return when {
            finalSlotCount > 1 -> "FINAL_SLOT_DUPLICATE"
            finalSlotCount == 0 -> "FINAL_SLOT_MISSING"
            pollCount in 1 until POLL_FULL_DAY_SLOTS -> "MISSED_SLOTS"
            rawPollCount > POLL_FULL_DAY_SLOTS -> "COUNTER_DRIFT"
            snapshotRows > POLL_FULL_DAY_SLOTS -> "SNAPSHOT_OVERRUN"
            pollCount > 0 -> "NONE"
            else -> ""
        }
    }

    private fun countSlotOccurrencesForDate(targetDate: String, slotOrdinal: Int): Int {
        val lastPollDate = prefs.getString("last_poll_date", "") ?: ""
        if (lastPollDate != targetDate) return 0
        return try {
            val history = JSONArray(prefs.getString("poll_history", "[]") ?: "[]")
            var count = 0
            for (i in 0 until history.length()) {
                val row = history.optJSONObject(i) ?: continue
                if (slotOrdinalFromPollTime(pollSlotTime(row)) == slotOrdinal) count++
            }
            count
        } catch (_: Exception) {
            0
        }
    }

    private fun countDistinctSlotOrdinalsForDate(targetDate: String): Int {
        val lastPollDate = prefs.getString("last_poll_date", "") ?: ""
        if (lastPollDate != targetDate) return 0
        return try {
            val history = JSONArray(prefs.getString("poll_history", "[]") ?: "[]")
            val slots = mutableSetOf<Int>()
            for (i in 0 until history.length()) {
                val row = history.optJSONObject(i) ?: continue
                val slot = slotOrdinalFromPollTime(pollSlotTime(row)) ?: continue
                slots.add(slot)
            }
            slots.size
        } catch (_: Exception) {
            0
        }
    }

    private fun countLocalSnapshotRows(sessionDate: String): Int {
        val safeDate = sessionDate.filter { it.isDigit() || it == '-' }.ifBlank { "unknown" }
        val file = File(context.filesDir, "evaluation_local_cache/brain_snapshots_${safeDate}.jsonl")
        if (!file.exists()) return 0
        return try {
            file.useLines { lines -> lines.count { it.trim().isNotBlank() } }
        } catch (_: Exception) {
            0
        }
    }

    private fun currentPollCoverage(clock: MarketOpenScheduler.MarketClockStatus): PollCoverage {
        val rawActual = if (hasTodaySession()) prefs.getInt("poll_count", 0) else 0
        val expectedFullDay = POLL_FULL_DAY_SLOTS
        if (!clock.marketDay) {
            return PollCoverage(0, expectedFullDay, rawActual.coerceAtMost(expectedFullDay), 0, clock.reason)
        }
        val actual = rawActual.coerceAtMost(expectedFullDay)

        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        val cal = Calendar.getInstance(ist)
        val currentMinutes = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE)
        cal.add(Calendar.MINUTE, -2)
        val minutes = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE)
        val rawExpectedByNow = when {
            currentMinutes >= MARKET_CLOSE_MINUTE -> expectedFullDay
            minutes < MARKET_OPEN_MINUTE -> 0
            else -> ((minutes - MARKET_OPEN_MINUTE) / POLL_SLOT_MINUTES) + 1
        }.coerceIn(0, expectedFullDay)
        val firstSlot = firstPollSlotOrdinalToday()
        val expectedByNow = when {
            actual <= 0 -> rawExpectedByNow
            firstSlot != null -> (rawExpectedByNow - firstSlot + 1).coerceAtLeast(actual).coerceAtMost(expectedFullDay)
            else -> rawExpectedByNow
        }
        val missed = (expectedByNow - actual).coerceAtLeast(0)
        val state = when {
            actual <= 0 && expectedByNow <= 0 -> "PRE_OPEN"
            actual <= 0 && expectedByNow > 0 -> "NO_POLLS"
            missed == 0 -> "COMPLETE"
            actual > 0 -> "PARTIAL"
            else -> "NO_POLLS"
        }
        return PollCoverage(expectedByNow, expectedFullDay, actual, missed, state)
    }

    private fun firstPollSlotOrdinalToday(): Int? {
        val today = todayIstDate()
        val lastPollDate = prefs.getString("last_poll_date", "") ?: ""
        if (lastPollDate != today) return null
        return try {
            val history = JSONArray(prefs.getString("poll_history", "[]") ?: "[]")
            if (history.length() <= 0) return null
            val first = history.optJSONObject(0) ?: return null
            slotOrdinalFromPollTime(pollSlotTime(first))
        } catch (_: Exception) {
            null
        }
    }

    private fun slotOrdinalFromPollTime(time: String): Int? {
        val match = Regex("""(\d{1,2}):(\d{2})""").find(time) ?: return null
        val parts = match.value.split(":")
        if (parts.size != 2) return null
        val hour = parts[0].toIntOrNull() ?: return null
        val minute = parts[1].toIntOrNull() ?: return null
        val totalMinutes = hour * 60 + minute
        if (totalMinutes < MARKET_OPEN_MINUTE || totalMinutes > MARKET_CLOSE_MINUTE) return null
        return ((totalMinutes - MARKET_OPEN_MINUTE) / POLL_SLOT_MINUTES) + 1
    }

    private fun pollSlotTime(row: JSONObject): String {
        return row.optString("t", "").ifBlank {
            row.optString("time", "").ifBlank {
                row.optString("poll_time", "").ifBlank {
                    row.optString("poll_ts", "")
                }
            }
        }
    }

    private fun clearStaleSessionStateIfNeeded() {
        val today = todayIstDate()
        val lastPollDate = prefs.getString("last_poll_date", "") ?: ""
        val rawBaseline = prefs.getString("morning_baseline", "{}") ?: "{}"
        val baselineIsToday = baselineDate(rawBaseline) == today
        val baselineHasCoreFields = baselineHasCoreFields(rawBaseline)
        val hasActiveTodaySession = lastPollDate == today && prefs.getInt("poll_count", 0) > 0
        if (!baselineIsToday && baselineHasCoreFields && hasActiveTodaySession) {
            try {
                val healed = JSONObject(rawBaseline).put("date", today).toString()
                prefs.edit().putString("morning_baseline", healed).commit()
                Log.i(TAG, "BASELINE_HEALED_DURING_CLEAR: date=$today")
            } catch (_: Exception) {
            }
        }
        val effectiveBaselineIsToday = hasTodayBaseline()
        if (hasActiveTodaySession) return
        val hasStaleMorningState = !effectiveBaselineIsToday && (
            prefs.contains("morning_baseline") ||
            prefs.contains("morning_input") ||
            prefs.contains("expiry_bnf") ||
            prefs.contains("expiry_nf")
        )

        val hasStalePollState =
            lastPollDate != today && (
            prefs.getString("poll_history", "[]") != "[]" ||
            prefs.getInt("poll_count", 0) != 0 ||
            prefs.getString("latest_poll", "null") != "null" ||
            prefs.contains("last_poll_time"))
        val hasStaleDerivedState = !effectiveBaselineIsToday && lastPollDate != today && (
            prefs.getString("brain_result", "null") != "null" ||
            prefs.getString("candidates", "[]") != "[]" ||
            prefs.getBoolean("service_running", false)
        )
        if (!hasStalePollState && !hasStaleDerivedState && !hasStaleMorningState) return

        val editor = prefs.edit()
        if (hasStalePollState) {
            editor
                .remove("poll_history")
                .remove("poll_count")
                .remove("latest_poll")
                .remove("last_poll_time")
                .remove("last_poll_dispatch_slot")
                .remove("last_successful_poll_slot")
                .putString("last_poll_date", today)
        }
        if (hasStaleDerivedState) {
            editor
                .remove("brain_result")
                .remove("candidates")
                .putBoolean("service_running", false)
        }
        if (hasStaleMorningState && !effectiveBaselineIsToday) {
            editor
                .remove("morning_baseline")
                .remove("morning_input")
                .remove("expiry_bnf")
                .remove("expiry_nf")
        }
        editor.commit()
        Log.i("NativeBridge", "DAILY_RESET_BRIDGE: cleared stale session state for $today")
    }

    private fun clearDerivedSessionStateForToday() {
        prefs.edit()
            .remove("brain_result")
            .remove("candidates")
            .remove("poll_history")
            .remove("poll_count")
            .remove("latest_poll")
            .remove("last_poll_time")
            .remove("last_poll_dispatch_slot")
            .remove("last_successful_poll_slot")
            .putString("last_poll_date", todayIstDate())
            .commit()
    }

    private fun fetchJson(url: String, token: String): JSONObject? {
        val req = Request.Builder()
            .url(url)
            .addHeader("Authorization", "Bearer $token")
            .addHeader("Accept", "application/json")
            .build()
        return try {
            httpClient.newCall(req).execute().use { resp ->
                val body = resp.body?.string() ?: "{}"
                if (!resp.isSuccessful) {
                    Log.w("NativeBridge", "Upstox fetch failed: code=${resp.code}, url=$url, body=${body.take(240)}")
                    return null
                }
                JSONObject(body)
            }
        } catch (e: Exception) {
            Log.w("NativeBridge", "Upstox fetch exception: url=$url, error=${e.message}")
            null
        }
    }

    private fun parseExpiryDate(rawExpiry: String): Date? {
        val trimmed = rawExpiry.trim()
        if (!Regex("\\d{4}-\\d{2}-\\d{2}").matches(trimmed)) return null
        return try {
            SimpleDateFormat("yyyy-MM-dd", Locale.US).apply {
                isLenient = false
                timeZone = TimeZone.getTimeZone("Asia/Kolkata")
            }.parse(trimmed)
        } catch (_: Exception) {
            null
        }
    }

    private fun isUsableExpiry(rawExpiry: String, today: String = todayIstDate()): Boolean {
        val trimmed = rawExpiry.trim()
        return trimmed.isNotBlank() && trimmed >= today && parseExpiryDate(trimmed) != null
    }

    private fun resolveNearestExpiry(instrumentKey: String, token: String): String? {
        val today = todayIstDate()

        val encodedKey = URLEncoder.encode(instrumentKey, Charsets.UTF_8.name())
        val url = "https://api.upstox.com/v2/option/contract?instrument_key=$encodedKey"
        val json = fetchJson(url, token)
        val arr = json?.optJSONArray("data")

        var nearest: String? = null
        if (arr != null) {
            for (i in 0 until arr.length()) {
                val date = arr.optJSONObject(i)?.optString("expiry", "") ?: ""
                if (date.length != 10) continue
                if (date < today) continue
                if (nearest == null || date < nearest) nearest = date
            }
        }
        if (nearest != null && isUsableExpiry(nearest, today)) {
            return nearest
        }
        val contractCount = arr?.length() ?: -1
        Log.w("NativeBridge", "No live expiry found for $instrumentKey; contracts=$contractCount, today=$today")
        return null
    }

    private fun fetchLiveIndexQuotes(token: String): LiveQuotes? {
        val url = "https://api.upstox.com/v2/market-quote/quotes?instrument_key=NSE_INDEX|Nifty Bank,NSE_INDEX|Nifty 50,NSE_INDEX|India VIX"
        val json = fetchJson(url, token) ?: return null
        val data = json.optJSONObject("data") ?: return null

        val bnf = data.optJSONObject("NSE_INDEX:Nifty Bank")?.optDouble("last_price", 0.0) ?: 0.0
        val nf = data.optJSONObject("NSE_INDEX:Nifty 50")?.optDouble("last_price", 0.0) ?: 0.0
        val vix = data.optJSONObject("NSE_INDEX:India VIX")?.optDouble("last_price", 0.0) ?: 0.0
        return LiveQuotes(bnf, nf, vix)
    }

    private fun bridgeOk(): String = JSONObject().put("ok", true).toString()

    private fun bridgeFail(error: String): String = JSONObject()
        .put("ok", false)
        .put("error", error)
        .toString()

    private fun oracleGet(path: String): String {
        val request = Request.Builder()
            .url("$ORACLE_BASE_URL$path")
            .get()
            .build()
        return httpClient.newCall(request).execute().use { response ->
            val body = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                throw IllegalStateException("Oracle ${response.code}: ${body.ifBlank { response.message }}")
            }
            body
        }
    }

    private fun oraclePost(path: String, bodyJson: String): String {
        val request = Request.Builder()
            .url("$ORACLE_BASE_URL$path")
            .post(bodyJson.toRequestBody("application/json".toMediaTypeOrNull()))
            .build()
        return httpClient.newCall(request).execute().use { response ->
            val body = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                throw IllegalStateException("Oracle ${response.code}: ${body.ifBlank { response.message }}")
            }
            body
        }
    }

    private fun getCachedEvaluationJobObject(): JSONObject {
        val raw = prefs.getString(PREF_LAST_EVALUATOR_JOB, "{}") ?: "{}"
        return try {
            JSONObject(raw)
        } catch (_: Exception) {
            JSONObject()
        }
    }

    private fun saveEvaluationJobObject(job: JSONObject) {
        prefs.edit().putString(PREF_LAST_EVALUATOR_JOB, job.toString()).commit()
    }

    private fun readApprovedBranchProposals(force: Boolean = false): JSONArray {
        val now = System.currentTimeMillis()
        val cached = prefs.getString(PREF_APPROVED_BRANCH_PROPOSALS, "[]") ?: "[]"
        val lastSync = prefs.getLong(PREF_APPROVED_BRANCH_PROPOSALS_SYNC_MS, 0L)
        if (!force && (now - lastSync) < APPROVED_BRANCH_PROPOSALS_TTL_MS) {
            return try {
                JSONArray(cached)
            } catch (_: Exception) {
                JSONArray()
            }
        }
        val rows = SupabaseClient.select("ai_branch_proposals", "status=eq.approved", "approved_at.desc", 50)
        prefs.edit()
            .putString(PREF_APPROVED_BRANCH_PROPOSALS, rows.toString())
            .putLong(PREF_APPROVED_BRANCH_PROPOSALS_SYNC_MS, now)
            .commit()
        return rows
    }

    private fun updateBranchProposalStatusInternal(rowId: String, status: String): String {
        val trimmed = rowId.trim()
        if (trimmed.isBlank()) return bridgeFail("Proposal row id missing")
        return try {
            val body = JSONObject()
                .put("status", status)
                .put("approved_by", "market_radar_app")
                .put("approved_at", if (status == "approved") nowUtcIso() else JSONObject.NULL)
            val ok = SupabaseClient.update("ai_branch_proposals", body, "id=eq.$trimmed")
            if (!ok) return bridgeFail("Proposal status update failed")
            val rows = readApprovedBranchProposals(force = true)
            JSONObject()
                .put("ok", true)
                .put("status", status)
                .put("approvedCount", rows.length())
                .put("message", when (status) {
                    "approved" -> "Proposal approved and synced."
                    "rejected" -> "Proposal removed from the live brain."
                    else -> "Proposal status updated."
                })
                .toString()
        } catch (e: Exception) {
            bridgeFail("Proposal update failed: ${e.message}")
        }
    }

    private fun normalizeTradeMode(raw: String?): String {
        val m = (raw ?: "").trim().lowercase(Locale.US)
        return when (m) {
            "intraday", "intra", "day" -> "intraday"
            "swing", "carry", "positional" -> "swing"
            else -> ""
        }
    }

    @JavascriptInterface
    fun getOpenTrades(): String {
        val now = System.currentTimeMillis()
        val prefValue = prefs.getString("open_trades", "[]") ?: "[]"
        if (prefValue != openTradesCache) {
            openTradesCache = prefValue
            openTradesCacheMs = now
            return prefValue
        }
        // Live P&L/control fields are produced locally by brain.py and stored in
        // SharedPreferences. Supabase only has the entry snapshot, so polling it
        // here can overwrite current_pnl back to zero. Use Supabase only as a
        // bootstrap fallback when there is no local open-trades state.
        try {
            if (JSONArray(prefValue).length() > 0) {
                openTradesCache = prefValue
                openTradesCacheMs = now
                return prefValue
            }
        } catch (_: Exception) {
            // Fall through to cached/fallback path for corrupt local state.
        }
        if (now - openTradesCacheMs < openTradesCacheTtlMs) return openTradesCache
        return try {
            val result = SupabaseClient.getOpenTrades().toString()
            openTradesCache = result
            openTradesCacheMs = now
            prefs.edit().putString("open_trades", result).commit()
            result
        } catch (e: Exception) {
            Log.e(TAG, "getOpenTrades failed", e)
            openTradesCache
        }
    }

    @JavascriptInterface
    fun getClosedTrades(limit: Int): String {
        return try {
            SupabaseClient.select("trades_v2", "status=eq.CLOSED", "exit_date.desc", limit).toString()
        } catch (e: Exception) {
            Log.e(TAG, "getClosedTrades failed", e)
            "[]"
        }
    }

    @JavascriptInterface
    fun getPremiumHistory(days: Int): String {
        return try {
            SupabaseClient.select("premium_history", null, "date.desc", days * 5).toString()
        } catch (e: Exception) {
            Log.e(TAG, "getPremiumHistory failed", e)
            "[]"
        }
    }

    @JavascriptInterface
    fun getMorningSnapshot(date: String): String {
        val key = date.ifBlank { todayIsoDate() }
        morningSnapshotCache[key]?.let { return it }
        return try {
            val res = SupabaseClient.select("chain_snapshots", "date=eq.$key&session=eq.morning")
            val result = if (res.length() > 0) res.getJSONObject(0).toString() else "{}"
            morningSnapshotCache[key] = result
            result
        } catch (e: Exception) {
            Log.e(TAG, "getMorningSnapshot failed", e)
            morningSnapshotCache[key] ?: "{}"
        }
    }

    @JavascriptInterface
    fun getYesterdayHistory(days: Int): String {
        val safeDays = days.coerceAtLeast(1)
        val key = "${todayIsoDate()}:$safeDays"
        if (key == yesterdayHistoryCacheKey) return yesterdayHistoryCache
        return try {
            val result = SupabaseClient.select("chain_snapshots", null, "date.desc", safeDays).toString()
            yesterdayHistoryCacheKey = key
            yesterdayHistoryCache = result
            result
        } catch (e: Exception) {
            Log.e(TAG, "getYesterdayHistory failed", e)
            yesterdayHistoryCache
        }
    }

    @JavascriptInterface
    fun getChainSnapshot(date: String, session: String): String {
        return try {
            val res = SupabaseClient.select("chain_snapshots", "date=eq.$date&session=eq.$session")
            if (res.length() > 0) res.getJSONObject(0).toString() else "{}"
        } catch (e: Exception) {
            Log.e(TAG, "getChainSnapshot failed", e)
            "{}"
        }
    }

    @JavascriptInterface
    fun getBaseline(): String {
        return try {
            clearStaleSessionStateIfNeeded()
            val baselineRaw = prefs.getString("morning_baseline", "{}") ?: "{}"
            val today = todayIstDate()
            if (baselineDate(baselineRaw) == today) return baselineRaw

            // Auto-heal legacy sessions where morning_baseline exists but "date" is missing.
            // If today's session is active and baseline has core spot fields, stamp today's date.
            val baselineObj = try { JSONObject(baselineRaw) } catch (_: Exception) { JSONObject() }
            val hasCoreFields =
                baselineObj.has("bnfSpot") &&
                baselineObj.has("nfSpot") &&
                baselineObj.has("vix")
            if (hasCoreFields && hasTodaySession()) {
                baselineObj.put("date", today)
                val healed = baselineObj.toString()
                prefs.edit().putString("morning_baseline", healed).commit()
                LogBuffer.add('I', TAG, "BASELINE_HEALED_WITH_DATE: $today")
                return healed
            }
            "{}"
        } catch (e: Exception) {
            "{}"
        }
    }

    @JavascriptInterface
    fun getConfig(key: String): String {
        return try {
            val res = SupabaseClient.select("app_config", "key=eq.$key")
            if (res.length() > 0) res.getJSONObject(0).optString("value", "{}") else "{}"
        } catch (e: Exception) {
            Log.e(TAG, "getConfig failed", e)
            "{}"
        }
    }

    @JavascriptInterface
    fun getAllConfig(): String {
        return try {
            val res = SupabaseClient.selectAppConfigLite()
            val obj = JSONObject()
            for (i in 0 until res.length()) {
                val item = res.getJSONObject(i)
                obj.put(item.getString("key"), item.opt("value"))
            }
            obj.toString()
        } catch (e: Exception) {
            Log.e(TAG, "getAllConfig failed", e)
            "{}"
        }
    }

    @JavascriptInterface
    fun getTeacherTruthConfig(): String {
        return try {
            TeacherTruthConfig.toJson().toString()
        } catch (e: Exception) {
            Log.e(TAG, "getTeacherTruthConfig failed", e)
            "{}"
        }
    }

    @JavascriptInterface
    fun getBnfChain(): String {
        return try {
            val ctx = JSONObject(prefs.getString("context", "{}") ?: "{}")
            ctx.optJSONObject("bnfChain")?.toString() ?: "{}"
        } catch (e: Exception) {
            "{}"
        }
    }

    @JavascriptInterface
    fun getNfChain(): String {
        return try {
            val ctx = JSONObject(prefs.getString("context", "{}") ?: "{}")
            ctx.optJSONObject("nfChain")?.toString() ?: "{}"
        } catch (e: Exception) {
            "{}"
        }
    }

    @JavascriptInterface
    fun getBnfBreadth(): String {
        return try {
            val ctx = JSONObject(prefs.getString("context", "{}") ?: "{}")
            ctx.optJSONObject("bnfBreadth")?.toString() ?: "{}"
        } catch (e: Exception) {
            "{}"
        }
    }

    @JavascriptInterface
    fun getNf50Breadth(): String {
        return try {
            val ctx = JSONObject(prefs.getString("context", "{}") ?: "{}")
            ctx.optJSONObject("nf50Breadth")?.toString() ?: "{}"
        } catch (e: Exception) {
            "{}"
        }
    }

    @JavascriptInterface
    fun getGlobalDirection(): String {
        return try {
            val ctx = JSONObject(prefs.getString("context", "{}") ?: "{}")
            ctx.optJSONObject("globalDirection")?.toString()
                ?: prefs.getString("global_direction", "{}")
                ?: "{}"
        } catch (e: Exception) {
            "{}"
        }
    }

    @JavascriptInterface
    fun getRecentSignals(limit: Int): String {
        return try {
            SupabaseClient.getRecentSignals(limit).toString()
        } catch (e: Exception) {
            Log.e(TAG, "getRecentSignals failed", e)
            "[]"
        }
    }

    @JavascriptInterface
    fun getMLDecisions(limit: Int): String {
        return try {
            SupabaseClient.select("ml_decisions", null, "created_at.desc", limit).toString()
        } catch (e: Exception) {
            Log.e(TAG, "getMLDecisions failed", e)
            "[]"
        }
    }

    @JavascriptInterface
    fun getMLEvaluationOutcomes(limit: Int): String {
        return try {
            SupabaseClient.fetchRecentEvaluationOutcomes(limit).toString()
        } catch (e: Exception) {
            Log.e(TAG, "getMLEvaluationOutcomes failed", e)
            "[]"
        }
    }

    @JavascriptInterface
    fun getMLEvaluationLaneSummary(limit: Int): String {
        val targetDate = latestEligibleEvaluationDate(todayIstDate()) ?: todayIstDate()
        return try {
            SupabaseClient.fetchEvaluationLaneSummary(targetDate, limit).toString()
        } catch (e: Exception) {
            Log.e(TAG, "getMLEvaluationLaneSummary failed", e)
            JSONObject()
                .put("session_date", targetDate)
                .put("rowsFetched", 0)
                .put("rowsToday", 0)
                .put("attributedRows", 0)
                .put("lanes", JSONObject())
                .put("error", e.message ?: e.javaClass.simpleName)
                .toString()
        }
    }

    @JavascriptInterface
    fun getMLTeacherResearchReport(): String {
        var targetDate = "unknown"
        return try {
            targetDate = latestEligibleEvaluationDate(todayIstDate()) ?: todayIstDate()
            val cachedDate = prefs.getString("teacher_research_report_date", "") ?: ""
            val cached = prefs.getString("teacher_research_report", "") ?: ""
            if (cachedDate == targetDate && cached.trim().startsWith("{")) {
                try {
                    JSONObject(cached)
                    return cached
                } catch (_: Throwable) {
                }
            }
            val file = java.io.File(MarketMLService.evaluationResearchReportPath(context, targetDate))
            if (!file.exists()) {
                scheduleTeacherResearchRebuild(targetDate)
                val reportStatus = (prefs.getString("teacher_research_report_status", "") ?: "").uppercase(Locale.US)
                val status = when {
                    teacherResearchRebuildInFlight.get() -> "rebuilding"
                    reportStatus == "FAILED" -> "failed"
                    reportStatus == "REBUILDING" -> "rebuilding"
                    else -> "pending"
                }
                val error = if (status == "failed") {
                    prefs.getString("teacher_research_report_error", "REPORT_NOT_AVAILABLE") ?: "REPORT_NOT_AVAILABLE"
                } else {
                    "REPORT_REBUILDING"
                }
                return JSONObject()
                    .put("ok", false)
                    .put("session_date", targetDate)
                    .put("status", status)
                    .put("error", error)
                    .toString()
            }
            val raw = file.readText().trim()
            val obj = JSONObject(raw)
            prefs.edit()
                .putString("teacher_research_report_date", targetDate)
                .putString("teacher_research_report", obj.toString())
                .putString("teacher_research_report_status", "READY")
                .remove("teacher_research_report_error")
                .commit()
            obj.toString()
        } catch (e: Throwable) {
            logTeacherResearchThrowable("getMLTeacherResearchReport", e)
            try {
                val error = "${e.javaClass.simpleName}: ${e.message ?: e.javaClass.name}"
                JSONObject()
                    .put("ok", false)
                    .put("session_date", targetDate)
                    .put("error", error)
                    .toString()
            } catch (_: Throwable) {
                "{\"ok\":false,\"session_date\":\"unknown\",\"error\":\"getMLTeacherResearchReport failed\"}"
            }
        }
    }

    @Synchronized
    @JavascriptInterface
    fun getMLBrainSnapshots(limit: Int): String {
        var targetDate = "unknown"
        return try {
            targetDate = latestEligibleEvaluationDate(todayIstDate()) ?: todayIstDate()
            val maxRows = limit.coerceIn(1, ML_BRAIN_SNAPSHOT_JS_MAX_ROWS)
            val file = File(MarketMLService.evaluationSnapshotsPath(context, targetDate))
            val now = System.currentTimeMillis()
            val cacheKey = listOf(
                targetDate,
                maxRows.toString(),
                file.length().toString(),
                file.lastModified().toString()
            ).joinToString(":")
            if (
                cacheKey == mlBrainSnapshotsBridgeCacheKey &&
                now - mlBrainSnapshotsBridgeCacheMs < ML_BRAIN_SNAPSHOT_JS_CACHE_TTL_MS
            ) {
                return mlBrainSnapshotsBridgeCacheValue
            }
            val rows = EvaluationLocalCache.readRecentBrainSnapshotSummaries(
                context,
                targetDate,
                maxRows,
                ML_BRAIN_SNAPSHOT_JS_MAX_BYTES
            )
            val capped = JSONArray()
            for (i in 0 until minOf(rows.length(), maxRows)) {
                rows.optJSONObject(i)?.let(capped::put)
            }
            val payload = capped.toString()
            mlBrainSnapshotsBridgeCacheKey = cacheKey
            mlBrainSnapshotsBridgeCacheValue = payload
            mlBrainSnapshotsBridgeCacheMs = now
            LogBuffer.add('I', TAG, "ML_BRAIN_SNAPSHOTS_BRIDGE: date=$targetDate source=summary rows=${capped.length()} requested=$limit maxRows=$maxRows payloadBytes=${payload.toByteArray(Charsets.UTF_8).size}")
            payload
        } catch (e: Throwable) {
            logTeacherResearchThrowable("getMLBrainSnapshots", e)
            "[]"
        }
    }

    @JavascriptInterface
    fun saveExportFile(fileName: String, mimeType: String, base64Data: String): String {
        return try {
            saveExportBytes(fileName, mimeType, Base64.decode(base64Data, Base64.DEFAULT)).toString()
        } catch (e: Exception) {
            Log.e(TAG, "saveExportFile failed", e)
            LogBuffer.add('E', TAG, "saveExportFile failed: ${e.message}")
            JSONObject()
                .put("ok", false)
                .put("error", e.message ?: e.javaClass.simpleName)
                .toString()
        }
    }

    @JavascriptInterface
    fun beginExportFile(fileName: String, mimeType: String): String {
        return try {
            exportSessionId = "${System.currentTimeMillis()}_${fileName.hashCode()}"
            exportSessionName = fileName
            exportSessionMime = mimeType
            exportSessionBase64 = StringBuilder()
            Log.i(TAG, "beginExportFile ok: name=$fileName mime=$mimeType session=$exportSessionId")
            LogBuffer.add('I', TAG, "beginExportFile ok: name=$fileName mime=$mimeType session=$exportSessionId")
            JSONObject().put("ok", true).put("sessionId", exportSessionId).toString()
        } catch (e: Exception) {
            Log.e(TAG, "beginExportFile failed", e)
            LogBuffer.add('E', TAG, "beginExportFile failed: ${e.message}")
            JSONObject().put("ok", false).put("error", e.message ?: e.javaClass.simpleName).toString()
        }
    }

    @JavascriptInterface
    fun appendExportFileChunk(sessionId: String, base64Chunk: String): String {
        return try {
            if (sessionId != exportSessionId || sessionId.isBlank()) {
                throw IllegalStateException("Invalid export session")
            }
            exportSessionBase64.append(base64Chunk)
            JSONObject()
                .put("ok", true)
                .put("chars", exportSessionBase64.length)
                .toString()
        } catch (e: Exception) {
            Log.e(TAG, "appendExportFileChunk failed", e)
            LogBuffer.add('E', TAG, "appendExportFileChunk failed: ${e.message}")
            JSONObject().put("ok", false).put("error", e.message ?: e.javaClass.simpleName).toString()
        }
    }

    @JavascriptInterface
    fun finishExportFile(sessionId: String): String {
        return try {
            if (sessionId != exportSessionId || sessionId.isBlank()) {
                throw IllegalStateException("Invalid export session")
            }
            val bytes = Base64.decode(exportSessionBase64.toString(), Base64.DEFAULT)
            val result = saveExportBytes(exportSessionName, exportSessionMime, bytes)
            Log.i(TAG, "finishExportFile ok: name=$exportSessionName bytes=${bytes.size}")
            LogBuffer.add('I', TAG, "finishExportFile ok: name=$exportSessionName bytes=${bytes.size}")
            result.toString()
        } catch (e: Exception) {
            Log.e(TAG, "finishExportFile failed", e)
            LogBuffer.add('E', TAG, "finishExportFile failed: ${e.message}")
            JSONObject()
                .put("ok", false)
                .put("error", e.message ?: e.javaClass.simpleName)
                .toString()
        } finally {
            exportSessionId = ""
            exportSessionName = ""
            exportSessionMime = ""
            exportSessionBase64 = StringBuilder()
        }
    }

    private fun saveExportBytes(fileName: String, mimeType: String, bytes: ByteArray): JSONObject {
        val safeName = fileName
            .replace(Regex("""[\\/:*?"<>|]"""), "_")
            .ifBlank { "MarketRadar_Export.xlsx" }
        val resolver = context.applicationContext.contentResolver

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, safeName)
                put(MediaStore.Downloads.MIME_TYPE, mimeType.ifBlank { "application/octet-stream" })
                put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS)
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: throw IllegalStateException("Downloads insert returned empty URI")
            resolver.openOutputStream(uri)?.use { out ->
                out.write(bytes)
                out.flush()
            } ?: throw IllegalStateException("Unable to open Downloads output stream")
            values.clear()
            values.put(MediaStore.Downloads.IS_PENDING, 0)
            resolver.update(uri, values, null, null)
        } else {
            @Suppress("DEPRECATION")
            val downloads = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
            if (!downloads.exists() && !downloads.mkdirs()) {
                throw IllegalStateException("Unable to create Downloads directory")
            }
            File(downloads, safeName).writeBytes(bytes)
        }

        Log.i(TAG, "saveExportFile ok: name=$safeName bytes=${bytes.size}")
        LogBuffer.add('I', TAG, "saveExportFile ok: name=$safeName bytes=${bytes.size}")
        showToast("Saved to Downloads: $safeName")
        return JSONObject()
            .put("ok", true)
            .put("fileName", safeName)
            .put("bytes", bytes.size)
            .put("location", "Downloads")
    }

    private fun showToast(message: String) {
        Handler(Looper.getMainLooper()).post {
            Toast.makeText(context.applicationContext, message, Toast.LENGTH_SHORT).show()
        }
    }

    private fun scoreCandidate(cand: JSONObject): JSONObject? {
        return try {
            val py = com.chaquo.python.Python.getInstance()
            val brain = py.getModule("brain")
            val result = runBlocking {
                withTimeoutOrNull(PY_SCORE_TIMEOUT_MS) {
                    brain.callAttr("ml_score_bridge", cand.toString()).toString()
                }
            } ?: return null
            JSONObject(result)
        } catch (e: Exception) {
            null
        }
    }

    private fun finiteDouble(obj: JSONObject, key: String, fallback: Double): Double {
        if (!obj.has(key) || obj.isNull(key)) return fallback
        val value = obj.optDouble(key, fallback)
        return if (value.isFinite()) value else fallback
    }

    private fun todayIsoDate(): String =
        SimpleDateFormat("yyyy-MM-dd", Locale.US).apply {
            timeZone = TimeZone.getTimeZone("Asia/Kolkata")
        }.format(Date())

    private fun nowUtcIso(): String =
        SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US).apply {
            timeZone = TimeZone.getTimeZone("UTC")
        }.format(Date())
}
