package com.marketradar.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.net.Uri
import android.os.Build
import androidx.core.app.NotificationCompat

object NotificationHelper {

    // Channel sound/vibration settings are locked by Android once created.
    // Versioned IDs ensure deterministic sound behaviour after app updates.
    private const val CHANNEL_PERFECT = "trade_perfect_v1"
    private const val CHANNEL_ENTRY = "trade_entry_v1"
    private const val CHANNEL_UPDATE = "trade_update_v1"
    private const val CHANNEL_WARNING = "trade_warning_v1"
    private const val CHANNEL_ROUTINE = "trade_routine_v1"
    private const val CHANNEL_URGENT = "trade_urgent_v1"

    private val lastNotifyTimes = mutableMapOf<String, Long>()
    private var notifIdBase = 3000

    fun createChannels(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java) ?: return

        val audioAttrs = AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_NOTIFICATION)
            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
            .build()

        fun uri(name: String): Uri = Uri.parse(
            "android.resource://${context.packageName}/raw/$name"
        )

        NotificationChannel(
            CHANNEL_PERFECT,
            "Perfect Trade Alert",
            NotificationManager.IMPORTANCE_HIGH
        ).apply {
            description = "All signals aligned at high confidence. Brain's strongest signal."
            enableVibration(true)
            vibrationPattern = longArrayOf(0, 120)
            setSound(uri("sound_perfect_alignment"), audioAttrs)
        }.also { manager.createNotificationChannel(it) }

        NotificationChannel(
            CHANNEL_ENTRY,
            "Trade Setup Ready",
            NotificationManager.IMPORTANCE_DEFAULT
        ).apply {
            description = "A setup is confirmed. Review and decide."
            enableVibration(true)
            vibrationPattern = longArrayOf(0, 100)
            setSound(uri("sound_entry_setup"), audioAttrs)
        }.also { manager.createNotificationChannel(it) }

        NotificationChannel(
            CHANNEL_UPDATE,
            "Conviction Update",
            NotificationManager.IMPORTANCE_DEFAULT
        ).apply {
            description = "Confidence on your current setup has significantly shifted."
            enableVibration(true)
            vibrationPattern = longArrayOf(0, 80)
            setSound(uri("sound_conviction_update"), audioAttrs)
        }.also { manager.createNotificationChannel(it) }

        NotificationChannel(
            CHANNEL_WARNING,
            "Market Warning",
            NotificationManager.IMPORTANCE_DEFAULT
        ).apply {
            description = "Market is choppy. Setup alerts are paused for 45 minutes."
            enableVibration(true)
            vibrationPattern = longArrayOf(0, 150, 80, 150)
            setSound(uri("sound_market_warning"), audioAttrs)
        }.also { manager.createNotificationChannel(it) }

        NotificationChannel(
            CHANNEL_ROUTINE,
            "Market Updates (silent)",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Routine status updates. Silent by default."
            enableVibration(false)
            setSound(null, null)
        }.also { manager.createNotificationChannel(it) }

        NotificationChannel(
            CHANNEL_URGENT,
            "Risk Alert - Exit Now",
            NotificationManager.IMPORTANCE_HIGH
        ).apply {
            description = "Stop-loss, target, and auth alerts. Always audible."
            enableVibration(true)
            vibrationPattern = longArrayOf(0, 200, 100, 200)
            setSound(uri("sound_urgent_risk"), audioAttrs)
            lockscreenVisibility = Notification.VISIBILITY_PUBLIC
        }.also { manager.createNotificationChannel(it) }
    }

    fun send(
        context: Context,
        title: String,
        body: String,
        type: String,
        tab: String? = null
    ) {
        // 30-second throttle per unique title prevents notification bursts.
        val now = System.currentTimeMillis()
        val lastTime = lastNotifyTimes[title] ?: 0L
        if (now - lastTime < 30_000L) return
        lastNotifyTimes[title] = now

        createChannels(context)

        val channelId = when (type) {
            "perfect" -> CHANNEL_PERFECT
            "entry", "important" -> CHANNEL_ENTRY
            "update" -> CHANNEL_UPDATE
            "warning" -> CHANNEL_WARNING
            "routine", "info", "INFO" -> CHANNEL_ROUTINE
            "urgent", "ERROR" -> CHANNEL_URGENT
            else -> CHANNEL_ROUTINE
        }

        val priority = when (type) {
            "perfect", "urgent", "ERROR" -> NotificationCompat.PRIORITY_HIGH
            "entry", "important", "update", "warning" -> NotificationCompat.PRIORITY_DEFAULT
            else -> NotificationCompat.PRIORITY_LOW
        }

        val intent = context.packageManager
            .getLaunchIntentForPackage(context.packageName)?.apply {
                if (tab != null) putExtra("openTab", tab)
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP
            }

        val currentId = notifIdBase++
        if (notifIdBase > 5000) notifIdBase = 3000

        val pending = PendingIntent.getActivity(
            context,
            currentId,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val builder = NotificationCompat.Builder(context, channelId)
            .setContentTitle(title)
            .setContentText(body)
            .setSmallIcon(android.R.drawable.ic_menu_manage)
            .setContentIntent(pending)
            .setPriority(priority)
            .setAutoCancel(true)

        if (type == "urgent" || type == "ERROR") {
            builder.setStyle(NotificationCompat.BigTextStyle().bigText(body))
        }

        val manager = context.getSystemService(NotificationManager::class.java)
        manager?.notify(currentId, builder.build())
    }
}
