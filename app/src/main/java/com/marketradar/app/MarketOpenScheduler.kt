package com.marketradar.app

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.Log
import com.marketradar.app.util.LogBuffer
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale
import java.util.TimeZone

object MarketOpenScheduler {
    private const val TAG = "MarketOpenScheduler"
    private const val ACTION_MARKET_OPEN = "com.marketradar.app.ACTION_MARKET_OPEN"
    private const val REQ_MARKET_OPEN = 74052
    private const val PREF_NEXT_MARKET_OPEN_MS = "next_market_open_ms"

    private val IST: TimeZone = TimeZone.getTimeZone("Asia/Kolkata")
    private val HOLIDAYS_2026 = setOf(
        "2026-01-26",
        "2026-03-03",
        "2026-03-26",
        "2026-03-31",
        "2026-04-03",
        "2026-04-14",
        "2026-05-01",
        "2026-05-28",
        "2026-06-26",
        "2026-09-14",
        "2026-10-02",
        "2026-10-20",
        "2026-11-10",
        "2026-11-24",
        "2026-12-25"
    )

    data class MarketClockStatus(
        val marketDay: Boolean,
        val marketOpen: Boolean,
        val reason: String
    )

    fun currentStatus(now: Calendar = Calendar.getInstance(IST)): MarketClockStatus {
        val day = now.get(Calendar.DAY_OF_WEEK)
        if (day == Calendar.SATURDAY || day == Calendar.SUNDAY) {
            return MarketClockStatus(marketDay = false, marketOpen = false, reason = "WEEKEND")
        }
        if (HOLIDAYS_2026.contains(formatDate(now.timeInMillis))) {
            return MarketClockStatus(marketDay = false, marketOpen = false, reason = "HOLIDAY")
        }
        val minutes = now.get(Calendar.HOUR_OF_DAY) * 60 + now.get(Calendar.MINUTE)
        return if (minutes in 555..930) {
            MarketClockStatus(marketDay = true, marketOpen = true, reason = "OPEN")
        } else {
            MarketClockStatus(marketDay = true, marketOpen = false, reason = "OUT_OF_HOURS")
        }
    }

    fun scheduleNextMarketOpen(context: Context) {
        val next = computeNextMarketOpen()
        val prefs = context.getSharedPreferences("market_radar", Context.MODE_PRIVATE)
        prefs.edit().putLong(PREF_NEXT_MARKET_OPEN_MS, next.timeInMillis).commit()

        val am = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        val pending = marketOpenPendingIntent(context)
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S || am.canScheduleExactAlarms()) {
            am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, next.timeInMillis, pending)
        } else {
            am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, next.timeInMillis, pending)
            LogBuffer.add('W', TAG, "AUTO_START_EXACT_UNAVAILABLE: using inexact fallback")
        }
        LogBuffer.add('I', TAG, "AUTO_START_SCHEDULED: ${formatStamp(next.timeInMillis)}")
    }

    fun nextScheduledAtMs(context: Context): Long {
        return context.getSharedPreferences("market_radar", Context.MODE_PRIVATE)
            .getLong(PREF_NEXT_MARKET_OPEN_MS, 0L)
    }

    fun maybeStartIngestionNow(context: Context, reason: String) {
        scheduleNextMarketOpen(context)
        val status = currentStatus()
        if (!status.marketOpen) {
            LogBuffer.add('I', TAG, "AUTO_START_SKIP: reason=$reason market=${status.reason}")
            return
        }

        val prefs = context.getSharedPreferences("market_radar", Context.MODE_PRIVATE)
        val token = (prefs.getString("auth_token", "") ?: "").trim()
        if (token.isBlank()) {
            LogBuffer.add('W', TAG, "AUTO_START_SKIP: reason=$reason missing_token")
            return
        }

        val serviceIntent = Intent(context, MarketWatchService::class.java)
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(serviceIntent)
            } else {
                context.startService(serviceIntent)
            }
            LogBuffer.add('I', TAG, "AUTO_START_TRIGGERED: reason=$reason")
        } catch (e: Exception) {
            Log.e(TAG, "AUTO_START_FAIL: ${e.message}", e)
            LogBuffer.add('E', TAG, "AUTO_START_FAIL: reason=$reason ${e.message}")
        }
    }

    private fun computeNextMarketOpen(from: Calendar = Calendar.getInstance(IST)): Calendar {
        val next = Calendar.getInstance(IST).apply {
            timeInMillis = from.timeInMillis
            set(Calendar.SECOND, 0)
            set(Calendar.MILLISECOND, 0)
        }
        while (true) {
            val status = currentStatus(next)
            val minutes = next.get(Calendar.HOUR_OF_DAY) * 60 + next.get(Calendar.MINUTE)
            val beforeOpenToday = status.marketDay && minutes < 555
            if (beforeOpenToday) {
                next.set(Calendar.HOUR_OF_DAY, 9)
                next.set(Calendar.MINUTE, 15)
                return next
            }
            next.add(Calendar.DATE, 1)
            next.set(Calendar.HOUR_OF_DAY, 9)
            next.set(Calendar.MINUTE, 15)
            if (currentStatus(next).marketDay) return next
        }
    }

    private fun marketOpenPendingIntent(context: Context): PendingIntent {
        val intent = Intent(context, MarketOpenAlarmReceiver::class.java).apply {
            action = ACTION_MARKET_OPEN
        }
        return PendingIntent.getBroadcast(
            context,
            REQ_MARKET_OPEN,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
    }

    private fun formatDate(epochMs: Long): String {
        return SimpleDateFormat("yyyy-MM-dd", Locale.US).apply {
            timeZone = IST
        }.format(Date(epochMs))
    }

    private fun formatStamp(epochMs: Long): String {
        return SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.US).apply {
            timeZone = IST
        }.format(Date(epochMs))
    }
}

class MarketOpenAlarmReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        MarketOpenScheduler.maybeStartIngestionNow(context, "market_open_alarm")
    }
}

class MarketLifecycleReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        val action = intent?.action ?: "unknown"
        MarketOpenScheduler.maybeStartIngestionNow(context, action)
    }
}
