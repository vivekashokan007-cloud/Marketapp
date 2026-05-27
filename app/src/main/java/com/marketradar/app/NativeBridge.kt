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
import android.util.Log
import android.webkit.JavascriptInterface
import android.widget.Toast
import java.io.File
import java.net.URLEncoder
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
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
        NotificationHelper.send(context, title, body, type)
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
                return bridgeFail("Upstox token missing. Please paste token before Lock & Scan.")
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
            val modeFromPrefs = normalizeTradeMode(prefs.getString("trade_mode", ""))
            val resolvedMode = when {
                modeFromPrefs.isNotEmpty() -> modeFromPrefs
                modeFromCtx.isNotEmpty() -> modeFromCtx
                else -> "swing"
            }
            if (ctxObj.optString("tradeMode", "") != resolvedMode) {
                ctxObj.put("tradeMode", resolvedMode)
                finalJson = ctxObj.toString()
            }
            prefs.edit().putString("trade_mode", resolvedMode).commit()
            LogBuffer.add('I', TAG, "TRADE_MODE_SET_CONTEXT: mode=$resolvedMode pref=${modeFromPrefs.ifEmpty { "none" }} ctx=${modeFromCtx.ifEmpty { "none" }}")
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
        val normalized = normalizeTradeMode(mode).ifEmpty { "swing" }
        val editor = prefs.edit().putString("trade_mode", normalized)
        try {
            val ctxObj = JSONObject(prefs.getString("context", "{}") ?: "{}")
            ctxObj.put("tradeMode", normalized)
            editor.putString("context", ctxObj.toString())
        } catch (e: Exception) {
            Log.w(TAG, "setTradeMode context update failed: ${e.message}")
        }
        editor.commit()
        LogBuffer.add('I', TAG, "TRADE_MODE_SET: mode=$normalized")
    }

    @JavascriptInterface
    fun getTradeMode(): String {
        return normalizeTradeMode(prefs.getString("trade_mode", "")).ifEmpty { "swing" }
    }

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
        if (!hasTodayBaseline()) return "null"
        return prefs.getString("latest_poll", "null") ?: "null"
    }

    @JavascriptInterface
    fun getPollHistory(): String {
        clearStaleSessionStateIfNeeded()
        if (!hasTodayBaseline()) return "[]"
        return prefs.getString("poll_history", "[]") ?: "[]"
    }

    @JavascriptInterface
    fun getBrainResult(): String {
        clearStaleSessionStateIfNeeded()
        if (!hasTodayBaseline()) return "null"
        return prefs.getString("brain_result", "null") ?: "null"
    }

    @JavascriptInterface
    fun getServiceStatus(): String {
        return try {
            clearStaleSessionStateIfNeeded()
            // NB6: Build JSON using JSONObject to avoid injection/escaping issues
            val status = JSONObject()
            val activeToday = hasTodayBaseline()
            status.put("running", activeToday && isServiceRunning())
            status.put("lastPoll", if (activeToday) prefs.getString("last_poll_time", "Never") else "Never")
            status.put("polls", if (activeToday) prefs.getInt("poll_count", 0) else 0)
            status.toString()
        } catch (e: Exception) {
            "{\"running\": false, \"error\": \"Internal failure\"}"
        }
    }

    @JavascriptInterface
    fun getCandidates(): String {
        clearStaleSessionStateIfNeeded()
        if (!hasTodayBaseline()) return "[]"
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
            val latestPollRaw = prefs.getString("latest_poll", "null") ?: "null"
            val latestPoll = try { JSONObject(latestPollRaw) } catch (e: Exception) { JSONObject() }
            val keyStats = extractInstrumentKeyStats(latestPoll)

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
        val strikeArrays = listOf("bnfStrikes", "nfStrikes")
        var total = 0
        var withKey = 0
        for (name in strikeArrays) {
            val arr = poll.optJSONArray(name) ?: continue
            for (i in 0 until arr.length()) {
                val row = arr.optJSONObject(i) ?: continue
                total++
                val key = row.optString("instrument_key", "").trim()
                if (key.isNotEmpty()) withKey++
            }
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
        val hasStaleDerivedState = !baselineIsToday && (
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
        if (existing.isNotEmpty() && existing >= today) return existing

        val encodedKey = URLEncoder.encode(instrumentKey, Charsets.UTF_8.name())
        val url = "https://api.upstox.com/v2/option/contract?instrument_key=$encodedKey"
        val json = fetchJson(url, token) ?: return null
        val arr = json.optJSONArray("data") ?: return null

        var nearest: String? = null
        for (i in 0 until arr.length()) {
            val date = arr.optJSONObject(i)?.optString("expiry", "") ?: ""
            if (date.length != 10) continue
            if (date < today) continue
            if (nearest == null || date < nearest) nearest = date
        }
        if (nearest == null) {
            Log.w("NativeBridge", "No live expiry found for $instrumentKey; contracts=${arr.length()}, today=$today")
        }
        return nearest
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
            val baseline = prefs.getString("morning_baseline", "{}") ?: "{}"
            if (baselineDate(baseline) == todayIstDate()) baseline else "{}"
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
}
