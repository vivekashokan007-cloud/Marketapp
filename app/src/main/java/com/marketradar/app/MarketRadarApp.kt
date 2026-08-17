package com.marketradar.app

import android.app.Application
import android.app.ActivityManager
import android.content.ComponentCallbacks2
import android.content.Context
import android.os.Build
import android.os.Debug
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import com.marketradar.app.util.LogBuffer
import com.marketradar.app.util.LogTap
import com.marketradar.app.util.LogcatCaptureService
import kotlinx.coroutines.*
import java.util.concurrent.TimeUnit

class MarketRadarApp : Application() {
    companion object {
        val PROCESS_START_UUID: String = java.util.UUID.randomUUID().toString()
    }

    private val applicationScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    override fun onCreate() {
        super.onCreate()
        try {
            LogBuffer.init(this)
            val prefs = getSharedPreferences("market_radar", MODE_PRIVATE)
            val now = System.currentTimeMillis()
            val lastProcessStart = prefs.getLong("last_process_start_ms", 0L)
            if (lastProcessStart > 0L) {
                val gapMs = now - lastProcessStart
                if (gapMs in 1 until 30_000L) {
                    Log.w("MarketRadarApp", "BL_A_PROCESS_RESTART_GAP_MS=$gapMs")
                    LogBuffer.add('W', "MarketRadarApp", "BL_A_PROCESS_RESTART_GAP_MS=$gapMs")
                }
            }
            prefs.edit().putLong("last_process_start_ms", now).commit()
            val pid = android.os.Process.myPid()
            Log.i("MarketRadarApp", "onCreate starting pid=$pid startUuid=$PROCESS_START_UUID")
            LogBuffer.add('I', "MarketRadarApp", "APP_PROCESS_START: pid=$pid startUuid=$PROCESS_START_UUID heap=${heapLine()}")
            logHistoricalExitReasons()
            
            // Initialize Python
            if (!Python.isStarted()) {
                Python.start(AndroidPlatform(this))
            }

            // Always install fatal crash capture regardless of log capture mode.
            LogTap.install(this)

            // ─── Log viewer probe ───
            val probeOk = try {
                val pid = android.os.Process.myPid()
                // No filters in probe — just check if we can read ANY process output
                val proc = Runtime.getRuntime().exec(arrayOf("logcat", "-d", "--pid=$pid", "-t", "5"))
                val out = proc.inputStream.bufferedReader().readText()
                proc.waitFor(2, TimeUnit.SECONDS)
                proc.destroy()
                out.isNotBlank()
            } catch (e: Exception) {
                LogBuffer.add('E', "LogProbe", "shell-out failed: ${e.message}")
                false
            }

            if (probeOk) {
                LogBuffer.captureMode = LogBuffer.CaptureMode.LOGCAT
                LogBuffer.add('I', "LogProbe",
                    "PROBE OK — capture mode = LOGCAT, " +
                    "API=${android.os.Build.VERSION.SDK_INT}, " +
                    "model=${android.os.Build.MODEL}")
                LogcatCaptureService.start(applicationScope)
            } else {
                LogBuffer.captureMode = LogBuffer.CaptureMode.LOGTAP
                LogBuffer.add('W', "LogProbe",
                    "PROBE FAILED — falling back to LOGTAP, " +
                    "API=${android.os.Build.VERSION.SDK_INT}, " +
                    "model=${android.os.Build.MODEL}")
                LogTap.installPythonStreams()
            }
            // Schedule day evaluation reminder at 4:30 PM IST (trading days)
            MarketMLService.scheduleDayEvaluationReminder(this)
            MarketOpenScheduler.scheduleNextMarketOpen(this)
            MarketOpenScheduler.maybeStartIngestionNow(this, "app_create")
            Log.i("MarketRadarApp", "onCreate complete")
        } catch (e: Exception) {
            Log.e("MarketRadarApp", "onCreate FAILED: ${e.message}", e)
            throw e
        }
    }

    override fun onTrimMemory(level: Int) {
        super.onTrimMemory(level)
        LogBuffer.add('W', "MarketRadarApp", "APP_TRIM_MEMORY: level=$level pid=${android.os.Process.myPid()} startUuid=$PROCESS_START_UUID heap=${heapLine()}")
        if (level >= ComponentCallbacks2.TRIM_MEMORY_BACKGROUND) {
            EvaluationLocalCache.releaseMemory()
        }
    }

    private fun heapLine(): String {
        val rt = Runtime.getRuntime()
        val used = rt.totalMemory() - rt.freeMemory()
        return "javaUsed=$used javaMax=${rt.maxMemory()} nativeUsed=${Debug.getNativeHeapAllocatedSize()}"
    }

    private fun logHistoricalExitReasons() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) return
        try {
            val am = getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
            am.getHistoricalProcessExitReasons(packageName, 0, 5).forEachIndexed { idx, info ->
                LogBuffer.add(
                    'I',
                    "MarketRadarApp",
                    "APP_EXIT_REASON[$idx]: reason=${info.reason} status=${info.status} importance=${info.importance} pss=${info.pss} rss=${info.rss} time=${info.timestamp}"
                )
            }
        } catch (e: Exception) {
            LogBuffer.add('W', "MarketRadarApp", "APP_EXIT_REASON_READ_FAIL: ${e.message}")
        }
    }
}
