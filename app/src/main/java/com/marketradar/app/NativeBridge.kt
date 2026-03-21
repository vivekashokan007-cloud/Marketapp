package com.marketradar.app

import android.content.Context
import android.content.Intent
import android.os.Build
import android.webkit.JavascriptInterface

class NativeBridge(private val context: Context) {

    @JavascriptInterface
    fun startService() {
        val intent = Intent(context, MarketRadarService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.startForegroundService(intent)
        } else {
            context.startService(intent)
        }
    }

    @JavascriptInterface
    fun stopService() {
        val intent = Intent(context, MarketRadarService::class.java).apply {
            action = "STOP"
        }
        context.startService(intent)
    }

    @JavascriptInterface
    fun sendNotification(title: String, body: String, type: String) {
        NotificationHelper.send(context, title, body, type)
    }
}
