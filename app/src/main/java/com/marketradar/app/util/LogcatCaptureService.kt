package com.marketradar.app.util

import kotlinx.coroutines.*

object LogcatCaptureService {
    private var job: Job? = null
    private var lastSeenSignature: String? = null

    private val LOGCAT_FILTERS = arrayOf(
        "-s", 
        "MarketRadarApp", "MarketWatchService", "NativeBridge",
        "LogProbe", "MainActivity", "OkHttp",
        "python.stdout", "python.stderr", "Chaquopy"
    )

    fun start(scope: CoroutineScope) {
        if (job?.isActive == true) return
        val pid = android.os.Process.myPid()
        job = scope.launch(Dispatchers.IO) {
            while (isActive) {
                try {
                    val cmd = arrayOf("logcat", "-d", "--pid=$pid", "-v", "time") + LOGCAT_FILTERS
                    val proc = Runtime.getRuntime().exec(cmd)
                    val reader = proc.inputStream.bufferedReader()
                    val lines = reader.readLines()
                    proc.destroy()
                    ingest(lines)
                } catch (e: Exception) {
                    LogBuffer.add('E', "LogcatCapture", "exec failed: ${e.message}")
                }
                delay(5000)
            }
        }
    }

    fun stop() { 
        job?.cancel()
        job = null 
    }

    private fun ingest(lines: List<String>) {
        // logcat -v time format: "MM-DD HH:MM:SS.mmm L/TAG  ( PID): message"
        val pattern = Regex("""^(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+([VDIWEF])/([^(]+)\(\s*\d+\):\s*(.*)$""")
        val startIndex = if (lastSeenSignature == null) {
            -1
        } else {
            lines.indexOfLast { it.take(200) == lastSeenSignature }
        }
        val newLines = if (startIndex >= 0 && startIndex < lines.size - 1) {
            lines.subList(startIndex + 1, lines.size)
        } else if (startIndex == lines.size - 1) {
            emptyList()
        } else {
            lines
        }

        for (line in newLines) {
            val m = pattern.matchEntire(line) ?: continue
            val (_, level, tag, msg) = m.destructured
            // Do not mirror captured logcat lines back into logcat.
            // Mirroring here creates self-amplifying feedback loops.
            LogBuffer.add(level[0], tag.trim(), msg, mirrorToLogcat = false)
        }
        if (lines.isNotEmpty()) lastSeenSignature = lines.last().take(200)
    }
}
