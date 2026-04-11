package com.marketradar.app

import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.os.Build
import android.util.Log
import android.webkit.JavascriptInterface

class NativeBridge(private val context: Context) {

    private val prefs: SharedPreferences = context.getSharedPreferences("market_radar", Context.MODE_PRIVATE)

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
        prefs.edit().putString("auth_token", token).apply()
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
        prefs.edit().putString("context", json).apply()
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
}
