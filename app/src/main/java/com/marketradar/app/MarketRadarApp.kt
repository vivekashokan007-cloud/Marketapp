package com.marketradar.app

import android.app.Application
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import com.marketradar.app.util.LogBuffer
import com.marketradar.app.util.LogTap
import com.marketradar.app.util.LogcatCaptureService
import kotlinx.coroutines.*
import java.util.concurrent.TimeUnit

class MarketRadarApp : Application() {
    private val applicationScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    override fun onCreate() {
        super.onCreate()
        try {
            Log.i("MarketRadarApp", "onCreate starting")
            
            // Initialize Python
            if (!Python.isStarted()) {
                Python.start(AndroidPlatform(this))
            }

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
                LogTap.install(this)
                LogTap.installPythonStreams()
            }
            Log.i("MarketRadarApp", "onCreate complete")
        } catch (e: Exception) {
            Log.e("MarketRadarApp", "onCreate FAILED: ${e.message}", e)
            throw e
        }
    }
}
