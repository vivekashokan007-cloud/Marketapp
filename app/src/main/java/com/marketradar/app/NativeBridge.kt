package com.marketradar.app

import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.os.Build
import android.util.Log
import android.webkit.JavascriptInterface
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
    private var lastScoredCandCount = -1
    private var lastScoredFirstCandId = ""
    private var lastScoredTotalLen = -1

    // Use applicationContext to guarantee same SharedPreferences instance as MarketWatchService
    private val prefs: SharedPreferences = context.applicationContext.getSharedPreferences("market_radar", Context.MODE_PRIVATE)
    private val httpClient = OkHttpClient()
    private val TAG = "NativeBridge"
    private val startDebounceMs = 15000L

    init {
        clearStalePollStateIfNeeded()
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
        val last = prefs.getString("open_trades", "")
        if (json == last) return
        prefs.edit().putString("open_trades", json).commit()
    }

    @JavascriptInterface
    fun setBaseline(json: String) {
        val last = prefs.getString("morning_baseline", "")
        if (json == last) return
        prefs.edit().putString("morning_baseline", json).commit()
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
        clearStalePollStateIfNeeded()
        return prefs.getString("latest_poll", "null") ?: "null"
    }

    @JavascriptInterface
    fun getPollHistory(): String {
        clearStalePollStateIfNeeded()
        return prefs.getString("poll_history", "[]") ?: "[]"
    }

    @JavascriptInterface
    fun getBrainResult(): String {
        return prefs.getString("brain_result", "null") ?: "null"
    }

    @JavascriptInterface
    fun getServiceStatus(): String {
        return try {
            clearStalePollStateIfNeeded()
            // NB6: Build JSON using JSONObject to avoid injection/escaping issues
            val status = JSONObject()
            status.put("running", isServiceRunning())
            status.put("lastPoll", prefs.getString("last_poll_time", "Never"))
            status.put("polls", prefs.getInt("poll_count", 0))
            status.toString()
        } catch (e: Exception) {
            "{\"running\": false, \"error\": \"Internal failure\"}"
        }
    }

    @JavascriptInterface
    fun getCandidates(): String {
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
            // NB4: Bridge calls are synchronous, runBlocking is redundant and risky
            mod.callAttr("validate_model", modelPath).toString()
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

    private data class LiveQuotes(val bnfSpot: Double, val nfSpot: Double, val vix: Double)

    private fun todayIstDate(): String {
        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        return SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = ist }.format(Date())
    }

    private fun clearStalePollStateIfNeeded() {
        val today = todayIstDate()
        val lastPollDate = prefs.getString("last_poll_date", "") ?: ""
        if (lastPollDate == today) return

        val hasStalePollState =
            prefs.getString("poll_history", "[]") != "[]" ||
            prefs.getInt("poll_count", 0) != 0 ||
            prefs.getString("latest_poll", "null") != "null" ||
            prefs.contains("last_poll_time")
        if (!hasStalePollState) return

        prefs.edit()
            .remove("poll_history")
            .remove("poll_count")
            .remove("latest_poll")
            .remove("last_poll_time")
            .commit()
        Log.i("NativeBridge", "DAILY_RESET_BRIDGE: cleared stale poll state for $today")
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
        return try {
            SupabaseClient.getOpenTrades().toString()
        } catch (e: Exception) {
            Log.e(TAG, "getOpenTrades failed", e)
            "[]"
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
        return try {
            val res = SupabaseClient.select("chain_snapshots", "date=eq.$date&session=eq.morning")
            if (res.length() > 0) res.getJSONObject(0).toString() else "{}"
        } catch (e: Exception) {
            Log.e(TAG, "getMorningSnapshot failed", e)
            "{}"
        }
    }

    @JavascriptInterface
    fun getYesterdayHistory(days: Int): String {
        return try {
            SupabaseClient.select("chain_snapshots", null, "date.desc", days).toString()
        } catch (e: Exception) {
            Log.e(TAG, "getYesterdayHistory failed", e)
            "[]"
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
            prefs.getString("morning_baseline", "{}") ?: "{}"
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
            val res = SupabaseClient.select("app_config")
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
            ctx.optJSONObject("globalDirection")?.toString() ?: "{}"
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

    private fun scoreCandidate(cand: JSONObject): JSONObject? {
        return try {
            val py = com.chaquo.python.Python.getInstance()
            val brain = py.getModule("brain")
            val result = brain.callAttr("ml_score_bridge", cand.toString()).toString()
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
}
