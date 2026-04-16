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

class NativeBridge(private val context: Context) {
    private var lastScoredCandCount = -1
    private var lastScoredFirstCandId = ""

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
        val intent = Intent(context, MarketWatchService::class.java).apply {
            action = "STOP"
        }
        context.startService(intent)
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
        prefs.edit().putString("open_trades", json).apply()
    }

    @JavascriptInterface
    fun setBaseline(json: String) {
        prefs.edit().putString("morning_baseline", json).apply()
    }

    @JavascriptInterface
    fun setExpiries(bnf: String, nf: String) {
        prefs.edit().apply {
            putString("expiry_bnf", bnf)
            putString("expiry_nf", nf)
        }.apply()
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
                    
                    // b116: Change guard to avoid redundant scoring
                    if (count != lastScoredCandCount || firstId != lastScoredFirstCandId) {
                        for (i in 0 until candsLite.length()) {
                            val cand = candsLite.getJSONObject(i)
                            val mlScored = scoreCandidate(cand)
                            if (mlScored != null) {
                                cand.put("p_ml", mlScored.optDouble("p_ml"))
                                cand.put("mlAction", mlScored.optString("ml_action"))
                                cand.put("mlEdge", mlScored.optDouble("ml_edge"))
                                cand.put("mlOod", mlScored.optBoolean("ml_ood", false))
                            }
                        }
                        lastScoredCandCount = count
                        lastScoredFirstCandId = firstId
                        finalJson = ctxObj.toString()
                        Log.d("NativeBridge", "Scored $count WebView candidates via setContext")
                    }
                }
            }
        } catch (e: Exception) {
            Log.w("NativeBridge", "setContext ML scoring failed: ${e.message}")
        }
        prefs.edit().putString("context", finalJson).apply()
    }

    @JavascriptInterface
    fun setClosedTrades(json: String) {
        prefs.edit().putString("closed_trades", json).apply()
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
    fun getServiceStatus(): String? {
        val isRunning = isServiceRunning()
        val lastPoll = prefs.getString("last_poll_time", "Never")
        val pollCount = prefs.getInt("poll_count", 0)
        return "{\"running\": $isRunning, \"lastPoll\": \"$lastPoll\", \"polls\": $pollCount}"
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
            val result = runBlocking {
                withTimeoutOrNull(10_000L) {
                    mod.callAttr("validate_model", modelPath).toString()
                }
            } ?: return "{\"ok\":false,\"error\":\"Python timeout\"}"
            result
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
