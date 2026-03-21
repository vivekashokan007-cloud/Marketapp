package com.marketradar.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat

object NotificationHelper {

    private const val CHANNEL_URGENT = "trade_urgent"
    private const val CHANNEL_IMPORTANT = "trade_important"
    private const val CHANNEL_ROUTINE = "trade_routine"
    private var notifId = 2000

    fun createChannels(context: Context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val manager = context.getSystemService(NotificationManager::class.java) ?: return

            val urgent = NotificationChannel(CHANNEL_URGENT, "Urgent Alerts", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Exit signals, stop loss, target alerts"
                enableVibration(true)
                vibrationPattern = longArrayOf(0, 300, 200, 300)
            }

            val important = NotificationChannel(CHANNEL_IMPORTANT, "Important Alerts", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "Market moves, force alignment changes"
                enableVibration(true)
            }

            val routine = NotificationChannel(CHANNEL_ROUTINE, "Market Updates", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "Periodic market status"
            }

            manager.createNotificationChannel(urgent)
            manager.createNotificationChannel(important)
            manager.createNotificationChannel(routine)
        }
    }

    fun send(context: Context, title: String, body: String, type: String) {
        createChannels(context)

        val channelId = when (type) {
            "urgent" -> CHANNEL_URGENT
            "important", "entry" -> CHANNEL_IMPORTANT
            else -> CHANNEL_ROUTINE
        }
        val priority = when (type) {
            "urgent" -> NotificationCompat.PRIORITY_HIGH
            "important", "entry" -> NotificationCompat.PRIORITY_DEFAULT
            else -> NotificationCompat.PRIORITY_DEFAULT
        }

        val intent = context.packageManager.getLaunchIntentForPackage(context.packageName)
        val pending = PendingIntent.getActivity(
            context, 0, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val builder = NotificationCompat.Builder(context, channelId)
            .setContentTitle(title)
            .setContentText(body)
            .setSmallIcon(android.R.drawable.ic_menu_manage)
            .setContentIntent(pending)
            .setPriority(priority)
            .setAutoCancel(true)

        if (type == "urgent") {
            builder.setStyle(NotificationCompat.BigTextStyle().bigText(body))
        }

        val manager = context.getSystemService(NotificationManager::class.java)
        manager?.notify(notifId++, builder.build())
    }
}
