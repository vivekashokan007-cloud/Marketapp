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
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import com.marketradar.app.util.LogBuffer

class NativeBridge(private val context: Context) {
    companion object {
        private const val TAG = "NativeBridge"
        private const val PY_VALIDATE_TIMEOUT_MS = 8_000L
        private const val PY_SCORE_TIMEOUT_MS = 2_500L
        private const val PREF_SANDBOX_ENABLED = "execution_sandbox_enabled"
        private const val PREF_ORDER_PROXY_URL = "order_proxy_url"
        private const val PREF_APPROVED_BRANCH_PROPOSALS = "approved_branch_proposals"
        private const val PREF_APPROVED_BRANCH_PROPOSALS_SYNC_MS = "approved_branch_proposals_sync_ms"
        private const val PREF_LAST_EVALUATOR_JOB = "last_evaluator_job"
        private const val PREF_TRADE_MODE = "trade_mode"
        private const val PREF_TRADE_MODE_EXPLICIT = "trade_mode_explicit"
        private const val PREF_NOTIFICATION_TRANSPORT_MODE = "brain_notification_transport_mode"
        private const val ORACLE_BASE_URL = "https://marketradar-oracle.online"
        private const val APPROVED_BRANCH_PROPOSALS_TTL_MS = 2 * 60 * 1000L
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
            "id", "type", "strategy_type", "index", "lane", "expiry", "width",
            "premiumEdge", "creditWidthRatio", "sigmaOTM",
            "sellStrike", "buyStrike", "sellType", "buyType",
            "sellStrike2", "buyStrike2", "sellType2", "buyType2",
            "netPremium", "maxLoss", "entryAction", "reason_code", "reject_reason"
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

    private fun compactTeacherResearchCandidates(raw: Any?): JSONArray {
        val source = parseJsonArray(raw) ?: return JSONArray()
        val compact = JSONArray()
        for (i in 0 until source.length()) {
            compactTeacherResearchCandidate(source.opt(i))?.let(compact::put)
        }
        return compact
    }

    private fun compactTeacherResearchSnapshot(snapshot: JSONObject): JSONObject {
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
        val rejected = parseJsonArray(context.opt("snapshot_rejected_candidates_full"))
            ?: parseJsonArray(context.opt("snapshot_rejected_candidates"))

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

        val compactGenerated = compactTeacherResearchCandidates(generated)
        val compactRejected = compactTeacherResearchCandidates(rejected)
        compactContext.put("snapshot_generated_candidates", compactGenerated)
        if (compactRejected.length() > 0) {
            compactContext.put("snapshot_rejected_candidates", compactRejected)
        }
        parseJsonObject(context.opt("snapshot_rejected_candidate_stats"))?.let { stats ->
            compactContext.put("snapshot_rejected_candidate_stats", stats)
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

    private fun loadLocalSavedSnapshots(date: String, limit: Int = 200): JSONArray {
        val file = File(MarketMLService.evaluationSnapshotsPath(context, date))
        if (!file.exists() || file.length() <= 0L) return JSONArray()
        val rows = JSONArray()
        streamJsonArrayFile(file) { row ->
            if (rows.length() < limit) rows.put(row)
        }
        return rows
    }

    private fun rebuildTeacherResearchReportIfPossible(targetDate: String): JSONObject? {
        return try {
            val snapshotsFile = File(MarketMLService.evaluationSnapshotsPath(context, targetDate))
            val outcomesFile = File(MarketMLService.evaluationOutcomesPath(context, targetDate))
            if (!snapshotsFile.exists() || !outcomesFile.exists()) return null
            val compactSnapshots = buildTeacherResearchSnapshotPayload(snapshotsFile)
            val outcomes = readJsonArrayFile(outcomesFile)
            if (compactSnapshots.length() == 0 || outcomes.length() == 0) return null

            val py = Python.getInstance()
            val brain = py.getModule("brain")
            val reportRaw = brain.callAttr(
                "session_teacher_research_report",
                targetDate,
                compactSnapshots.toString(),
                outcomes.toString()
            ).toString()
            val report = JSONObject(reportRaw)
            if (!report.optBoolean("ok", false)) return null

            val outFile = File(MarketMLService.evaluationResearchReportPath(context, targetDate))
            outFile.parentFile?.mkdirs()
            outFile.writeText(report.toString())
            prefs.edit()
                .putString("teacher_research_report_date", targetDate)
                .putString("teacher_research_report", report.toString())
                .commit()
            report
        } catch (e: Exception) {
            Log.e(TAG, "rebuildTeacherResearchReportIfPossible failed", e)
            null
        }
    }

    private fun isTradeModeExplicit(): Boolean = prefs.getBoolean(PREF_TRADE_MODE_EXPLICIT, false)

    @JavascriptInterface
    fun isNative(): Boolean = true

    @JavascriptInterface
    fun startMarketService() {
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

            prefs.edit()
                .putString("morning_input", obj.toString())
                .putString("morning_baseline", obj.toString())
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
        prefs.edit().apply {
            putString("expiry_bnf", bnf)
            putString("expiry_nf", nf)
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
            val retryEvaluation = targetDate?.let { shouldRetryDayEvaluation(it) } == true
            val serviceRunning = isServiceRunning()
            val marketClock = MarketOpenScheduler.currentStatus()
            val coverage = currentPollCoverage(marketClock)
            val targetIsToday = targetDate == today
            val historicalReady = !targetDate.isNullOrBlank() && !targetIsToday
            val targetSessionComplete = historicalReady || (activeToday && coverage.missed == 0)
            val evaluationReady = !serviceRunning &&
                !marketClock.marketOpen &&
                !targetDate.isNullOrBlank() &&
                (historicalReady || coverage.actual > 0) &&
                runningDate != targetDate &&
                (retryEvaluation || (targetSessionComplete && doneDate != targetDate))
            val evaluationBlockedReason = when {
                retryEvaluation -> ""
                serviceRunning -> "WAIT_FOR_POST_CLOSE_HANDOFF"
                marketClock.marketOpen -> "MARKET_OPEN"
                targetDate.isNullOrBlank() -> "NO_SESSION"
                targetIsToday && (!activeToday || coverage.actual <= 0) -> "NO_SESSION"
                targetIsToday && coverage.missed > 0 -> "SESSION_PARTIAL"
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
            status.put("evaluationTargetDate", targetDate ?: "")
            status.put("evaluationTargetIsToday", targetIsToday)
            status.put("evaluationReady", evaluationReady)
            status.put("evaluationBlockedReason", evaluationBlockedReason)
            status.put("evaluationDoneToday", doneDate == today && !retryEvaluation)
            status.put("evaluationDoneForTarget", !targetDate.isNullOrBlank() && doneDate == targetDate && !retryEvaluation)
            status.put("evaluationDoneDate", doneDate)
            status.put("evaluationRunning", !targetDate.isNullOrBlank() && runningDate == targetDate)
            status.put("evaluationPhase", evaluationPhase)
            status.put("evaluationProgressCurrent", evaluationCompleted)
            status.put("evaluationProgressTotal", evaluationTotal)
            status.put("evaluationRetryRecommended", retryEvaluation)
            status.put("evaluationRetryable", retryEvaluation)
            status.put("evaluationLastError", prefs.getString("evaluation_last_error", "") ?: "")
            status.put("evaluationUpdatedAtMs", prefs.getLong("evaluation_job_updated_at_ms", 0L))
            status.put("lastEvaluationOutcomeCount", prefs.getInt("last_evaluation_outcome_count", 0))
            status.put("lastEvaluationProducedCount", prefs.getInt("last_evaluation_produced_count", 0))
            val lastEvaluationMessage = prefs.getString("last_evaluation_message", "") ?: ""
            status.put("lastEvaluationMessage", lastEvaluationMessage)
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
            if (targetDate == today && coverage.missed > 0 && !retryEvaluation) {
                return JSONObject().apply {
                    put("ok", false)
                    put("status", "blocked")
                    put("message", "Day evaluation is blocked because today's session is still partial (${coverage.actual}/${coverage.expectedFullDay} polls). Wait for the automatic post-close run or retry only after a completed session.")
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
                .putString(
                    "last_evaluation_message",
                    if (retryEvaluation) {
                        "Evaluation recovery queued for $targetDate. It will resume from the last completed batch if local progress exists."
                    } else {
                        "Evaluation queued for $targetDate..."
                    }
                )
                .commit()
            val intent = android.content.Intent(context, MarketMLService::class.java).apply {
                action = "ACTION_DAY_EVALUATION"
                putExtra("session_date", targetDate)
            }
            context.startForegroundService(intent)
            JSONObject().apply {
                put("ok", true)
                put("target_date", targetDate)
                put("status", if (retryEvaluation) "restarted" else "started")
                put("message", if (retryEvaluation) "Day evaluation recovery started for $targetDate." else "Day evaluation started for $targetDate.")
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
            out.put("paperReady", true)
            out.put("sandboxReady", tokenReady && sandboxEnabled)
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
        if (phase == "FAILED" || phase == "FAILED_SAVE" || phase == "STALLED") {
            return true
        }
        val doneDate = prefs.getString("evaluation_done_date", "") ?: ""
        if (doneDate != targetDate) return false
        val runningDate = prefs.getString("evaluation_running_date", "") ?: ""
        if (runningDate == targetDate) return false
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

    private fun currentPollCoverage(clock: MarketOpenScheduler.MarketClockStatus): PollCoverage {
        val rawActual = if (hasTodaySession()) prefs.getInt("poll_count", 0) else 0
        val expectedFullDay = 76
        if (!clock.marketDay) {
            return PollCoverage(0, expectedFullDay, rawActual.coerceAtMost(expectedFullDay), 0, clock.reason)
        }
        val actual = rawActual.coerceAtMost(expectedFullDay)

        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        val cal = Calendar.getInstance(ist)
        cal.add(Calendar.MINUTE, -2)
        val minutes = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE)
        val rawExpectedByNow = when {
            minutes < 555 -> 0
            minutes >= 930 -> expectedFullDay
            else -> ((minutes - 555) / 5) + 1
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
            slotOrdinalFromPollTime(first.optString("t", ""))
        } catch (_: Exception) {
            null
        }
    }

    private fun slotOrdinalFromPollTime(time: String): Int? {
        val parts = time.split(":")
        if (parts.size != 2) return null
        val hour = parts[0].toIntOrNull() ?: return null
        val minute = parts[1].toIntOrNull() ?: return null
        val totalMinutes = hour * 60 + minute
        if (totalMinutes < 555 || totalMinutes > 930) return null
        return ((totalMinutes - 555) / 5) + 1
    }

    private fun clearStaleSessionStateIfNeeded() {
        val today = todayIstDate()
        val lastPollDate = prefs.getString("last_poll_date", "") ?: ""
        val baselineIsToday = hasTodayBaseline()

        val hasStalePollState =
            lastPollDate != today && (
            prefs.getString("poll_history", "[]") != "[]" ||
            prefs.getInt("poll_count", 0) != 0 ||
            prefs.getString("latest_poll", "null") != "null" ||
            prefs.contains("last_poll_time"))
        val hasStaleDerivedState = !baselineIsToday && lastPollDate != today && (
            prefs.getString("brain_result", "null") != "null" ||
            prefs.getString("candidates", "[]") != "[]" ||
            prefs.getBoolean("service_running", false)
        )
        if (!hasStalePollState && !hasStaleDerivedState) return

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

    private fun resolveNearestExpiry(instrumentKey: String, token: String): String? {
        val today = todayIstDate()

        val existing = (if (instrumentKey.contains("Nifty Bank")) {
            prefs.getString("expiry_bnf", "")
        } else {
            prefs.getString("expiry_nf", "")
        } ?: "").trim()

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
        if (nearest != null) {
            return nearest
        }
        if (existing.isNotEmpty() && existing >= today) {
            Log.w("NativeBridge", "Using stored expiry fallback for $instrumentKey: $existing")
            return existing
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
        val targetDate = latestEligibleEvaluationDate(todayIstDate()) ?: todayIstDate()
        val cachedDate = prefs.getString("teacher_research_report_date", "") ?: ""
        val cached = prefs.getString("teacher_research_report", "") ?: ""
        if (cachedDate == targetDate && cached.trim().startsWith("{")) {
            try {
                val cachedObj = JSONObject(cached)
                if (cachedObj.optBoolean("ok", false)) {
                    return cached
                }
            } catch (_: Exception) {
            }
        }
        return try {
            val file = java.io.File(MarketMLService.evaluationResearchReportPath(context, targetDate))
            if (!file.exists()) {
                rebuildTeacherResearchReportIfPossible(targetDate)?.let { rebuilt ->
                    return rebuilt.toString()
                }
                return JSONObject()
                    .put("ok", false)
                    .put("session_date", targetDate)
                    .put("error", "REPORT_NOT_AVAILABLE")
                    .toString()
            }
            val raw = file.readText().trim()
            val obj = JSONObject(raw)
            prefs.edit()
                .putString("teacher_research_report_date", targetDate)
                .putString("teacher_research_report", obj.toString())
                .commit()
            obj.toString()
        } catch (e: Exception) {
            Log.e(TAG, "getMLTeacherResearchReport failed", e)
            JSONObject()
                .put("ok", false)
                .put("session_date", targetDate)
                .put("error", e.message ?: e.javaClass.simpleName)
                .toString()
        }
    }

    @JavascriptInterface
    fun getMLBrainSnapshots(limit: Int): String {
        val targetDate = latestEligibleEvaluationDate(todayIstDate()) ?: todayIstDate()
        return try {
            val remote = SupabaseClient.fetchBrainSnapshots(targetDate)
            if (remote.length() > 0) {
                remote.toString()
            } else {
                loadLocalSavedSnapshots(targetDate, limit.coerceAtLeast(1)).toString()
            }
        } catch (e: Exception) {
            Log.e(TAG, "getMLBrainSnapshots failed", e)
            loadLocalSavedSnapshots(targetDate, limit.coerceAtLeast(1)).toString()
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
