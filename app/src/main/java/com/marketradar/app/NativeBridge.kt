package com.marketradar.app

import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.os.Build
import android.util.Log
import android.webkit.JavascriptInterface
import java.io.File
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONArray
import org.json.JSONObject
import com.marketradar.app.util.LogBuffer

class NativeBridge(private val context: Context) {
    private var lastScoredCandCount = -1
    private var lastScoredFirstCandId = ""
    private var lastScoredTotalLen = -1

    // Use applicationContext to guarantee same SharedPreferences instance as MarketWatchService
    private val prefs: SharedPreferences = context.applicationContext.getSharedPreferences("market_radar", Context.MODE_PRIVATE)

    @JavascriptInterface
    fun isNative(): Boolean = true

    @JavascriptInterface
    fun startMarketService() {
        val intent = Intent(context, MarketWatchService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.startForegroundService(intent)
        } else {
            context.startService(intent)
        }
    }

    @JavascriptInterface
    fun stopMarketService() {
        try {
            // NB1: Use stopService() instead of startService("STOP") to avoid background runtime exceptions
            val intent = Intent(context, MarketWatchService::class.java)
            context.stopService(intent)
            
            // Explicitly update running flag for immediate UI response
            prefs.edit().putBoolean("service_running", false).commit()
        } catch (e: Exception) {
            Log.e("NativeBridge", "stopMarketService failed: ${e.message}")
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
            prefs.edit()
                .putString("morning_input", obj.toString())
                .putString("morning_baseline", obj.toString())
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
            if (isMLModelReady()) {
                val ctxObj = JSONObject(json)
                val candsLite = ctxObj.optJSONArray("candsLite")
                if (candsLite != null && candsLite.length() > 0) {
                    val count = candsLite.length()
                    val firstId = candsLite.getJSONObject(0).optString("id", "")
                    
                    // b116/NB7: Enhanced change-guard (count + firstId + total length)
                    val totalLen = json.length
                    if (count != lastScoredCandCount || firstId != lastScoredFirstCandId || totalLen != lastScoredTotalLen) {
                        for (i in 0 until count) {
                            val cand = candsLite.getJSONObject(i)
                            try {
                                // NB3: Per-iteration try/catch — if one candidate fails, others still score
                                val mlScored = scoreCandidate(cand)
                                if (mlScored != null) {
                                    // NB2: Copy all ML fields, not just 4
                                    cand.put("p_ml", mlScored.optDouble("p_ml"))
                                    cand.put("mlAction", mlScored.optString("ml_action"))
                                    cand.put("mlEdge", mlScored.optDouble("ml_edge"))
                                    cand.put("mlOod", mlScored.optBoolean("ml_ood", false))
                                    cand.put("mlOodConf", mlScored.optDouble("ml_ood_conf", 1.0))
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
    fun setClosedTrades(json: String) {
        val last = prefs.getString("closed_trades", "")
        if (json == last) return
        prefs.edit().putString("closed_trades", json).commit()
    }

    // --- NEW: Data Pull (JS -> Kotlin) ---

    @JavascriptInterface
    fun getLatestPoll(): String {
        return prefs.getString("latest_poll", "null") ?: "null"
    }

    @JavascriptInterface
    fun getPollHistory(): String {
        return prefs.getString("poll_history", "[]") ?: "[]"
    }

    @JavascriptInterface
    fun getBrainResult(): String {
        return prefs.getString("brain_result", "null") ?: "null"
    }

    @JavascriptInterface
    fun getServiceStatus(): String {
        return try {
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

    private val TAG = "NativeBridge"

    private fun missingNumericFields(obj: JSONObject, fields: List<Pair<String, String>>): List<String> {
        return fields.mapNotNull { (key, label) ->
            val value = obj.optDouble(key, Double.NaN)
            if (!obj.has(key) || obj.isNull(key) || !java.lang.Double.isFinite(value)) label else null
        }
    }

    private fun bridgeOk(): String = JSONObject().put("ok", true).toString()

    private fun bridgeFail(error: String): String = JSONObject()
        .put("ok", false)
        .put("error", error)
        .toString()

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
}
