package com.marketradar.app.util

import kotlinx.coroutines.*

object LogcatCaptureService {
    private var job: Job? = null
    private var lastSeenSignature: String? = null

    fun start(scope: CoroutineScope) {
        if (job?.isActive == true) return
        val pid = android.os.Process.myPid()
        job = scope.launch(Dispatchers.IO) {
            while (isActive) {
                try {
                    val cmd = arrayOf("logcat", "-d", "--pid=$pid", "-v", "time")
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
        var foundLastSeen = (lastSeenSignature == null)
        for (line in lines) {
            val sig = line.take(120)  // signature for dedup
            if (!foundLastSeen) {
                if (sig == lastSeenSignature) foundLastSeen = true
                continue
            }
            val m = pattern.matchEntire(line) ?: continue
            val (_, level, tag, msg) = m.destructured
            LogBuffer.add(level[0], tag.trim(), msg)
        }
        if (lines.isNotEmpty()) lastSeenSignature = lines.last().take(120)
    }
}
