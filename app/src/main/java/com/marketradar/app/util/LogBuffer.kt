package com.marketradar.app.util
import android.content.Context
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.ConcurrentLinkedDeque

object LogBuffer {
    private const val MAX_ENTRIES = 2000
    private const val MAX_AGE_MS = 30 * 60 * 1000L  // 30 minutes
    private const val MAX_PERSISTED_BYTES = 768 * 1024L
    private const val MAX_CRASH_LOG_ROWS = 160

    enum class CaptureMode { UNINITIALIZED, LOGCAT, LOGTAP }
    @Volatile var captureMode: CaptureMode = CaptureMode.UNINITIALIZED

    data class Entry(
        val timestampMs: Long,
        val level: Char,    // V D I W E F
        val tag: String,
        val message: String
    )

    private val buffer = ConcurrentLinkedDeque<Entry>()
    private val ioLock = Any()
    @Volatile private var appContext: Context? = null
    @Volatile private var logFile: File? = null
    @Volatile private var crashFile: File? = null
    @Volatile private var restoredPersisted = false

    fun init(context: Context) {
        if (appContext != null) return
        val ctx = context.applicationContext
        appContext = ctx
        logFile = File(ctx.filesDir, "forensics/logbuffer.jsonl")
        crashFile = File(ctx.filesDir, "forensics/last_crash.json")
        restorePersistedEntries()
    }

    fun add(level: Char, tag: String, message: String, mirrorToLogcat: Boolean = true) {
        val now = System.currentTimeMillis()
        val entry = Entry(now, level, tag, message)
        buffer.addFirst(entry)

        // Mirror to system logcat for ADB visibility unless caller disables it.
        // This is important for logcat ingestion paths to avoid feedback loops.
        if (mirrorToLogcat) {
            when (level) {
                'V' -> Log.v(tag, message)
                'D' -> Log.d(tag, message)
                'I' -> Log.i(tag, message)
                'W' -> Log.w(tag, message)
                'E' -> Log.e(tag, message)
                'F' -> Log.wtf(tag, message)
                else -> Log.i(tag, message)
            }
        }

        appendPersistent(entry)

        // Trim by count
        while (buffer.size > MAX_ENTRIES) buffer.pollLast()
        // Trim by age (best-effort, runs on every add)
        val cutoff = now - MAX_AGE_MS
        while (buffer.isNotEmpty() && (buffer.peekLast()?.timestampMs ?: 0L) < cutoff) {
            buffer.pollLast()
        }
    }

    fun wasRecentlySeen(level: Char, tag: String, message: String, withinMs: Long = 6_000L): Boolean {
        val cutoff = System.currentTimeMillis() - withinMs
        for (entry in buffer) {
            if (entry.timestampMs < cutoff) break
            if (entry.level == level && entry.tag == tag && entry.message == message) {
                return true
            }
        }
        return false
    }

    fun recordCrash(tag: String, message: String, throwable: Throwable? = null, extra: JSONObject? = null) {
        val now = System.currentTimeMillis()
        val fullMessage = buildString {
            append(message)
            if (throwable != null) {
                if (!message.endsWith("\n")) append('\n')
                append(throwable.stackTraceToString())
            }
        }
        val entry = Entry(now, 'F', tag, fullMessage)
        add(entry.level, entry.tag, entry.message)
        persistCrashArtifact(entry, throwable, extra)
    }

    fun snapshot(filter: String?): List<Entry> {
        val all = buffer.toList()  // snapshot copy
        return when (filter) {
            null, "ALL" -> all
            "Kotlin"   -> all.filter { it.tag.startsWith("MV_") || it.tag == "MarketWatchService" || it.tag == "NativeBridge" || it.tag == "MainActivity" || it.tag == "WebViewJS" }
            "Python"   -> all.filter { it.tag == "py.stdout" || it.tag == "py.stderr" || it.tag == "Chaquopy" }
            "OkHttp"   -> all.filter { it.tag.startsWith("OkHttp") || it.tag == "okhttp.OkHttpClient" }
            "Errors"   -> all.filter { it.level == 'E' || it.level == 'F' || it.level == 'W' }
            else       -> all
        }
    }

    fun lastCrashReport(): JSONObject? {
        val file = crashFile ?: return null
        if (!file.exists()) return null
        return try {
            JSONObject(file.readText())
        } catch (_: Exception) {
            null
        }
    }

    fun clear() {
        buffer.clear()
        synchronized(ioLock) {
            try { logFile?.delete() } catch (_: Exception) {}
        }
    }

    fun clearCrashReport() {
        synchronized(ioLock) {
            try { crashFile?.delete() } catch (_: Exception) {}
        }
    }

    fun size(): Int = buffer.size

    private fun restorePersistedEntries() {
        if (restoredPersisted) return
        synchronized(ioLock) {
            if (restoredPersisted) return
            val file = logFile
            if (file != null && file.exists()) {
                try {
                    val lines = file.readLines()
                    for (line in lines.asReversed()) {
                        val obj = JSONObject(line)
                        buffer.addLast(
                            Entry(
                                timestampMs = obj.optLong("ts", 0L),
                                level = obj.optString("level", "I").firstOrNull() ?: 'I',
                                tag = obj.optString("tag", "RestoredLog"),
                                message = obj.optString("msg", "")
                            )
                        )
                    }
                } catch (_: Exception) {
                }
            }
            restoredPersisted = true
        }
    }

    private fun appendPersistent(entry: Entry) {
        synchronized(ioLock) {
            val file = logFile ?: return
            try {
                file.parentFile?.mkdirs()
                file.appendText(
                    JSONObject()
                        .put("ts", entry.timestampMs)
                        .put("level", entry.level.toString())
                        .put("tag", entry.tag)
                        .put("msg", entry.message)
                        .toString() + "\n"
                )
                trimPersistentFile(file)
            } catch (_: Exception) {
            }
        }
    }

    private fun trimPersistentFile(file: File) {
        if (!file.exists() || file.length() <= MAX_PERSISTED_BYTES) return
        try {
            val kept = file.readLines().takeLast(MAX_ENTRIES / 2)
            file.writeText(if (kept.isEmpty()) "" else kept.joinToString("\n", postfix = "\n"))
        } catch (_: Exception) {
        }
    }

    private fun persistCrashArtifact(entry: Entry, throwable: Throwable?, extra: JSONObject?) {
        synchronized(ioLock) {
            val file = crashFile ?: return
            try {
                file.parentFile?.mkdirs()
                val context = appContext
                val prefs = context?.getSharedPreferences("market_radar", Context.MODE_PRIVATE)
                val recentLogs = JSONArray()
                snapshot("ALL").take(MAX_CRASH_LOG_ROWS).forEach { row ->
                    recentLogs.put(
                        JSONObject()
                            .put("ts", row.timestampMs)
                            .put("level", row.level.toString())
                            .put("tag", row.tag)
                            .put("msg", row.message)
                    )
                }
                val report = JSONObject()
                    .put("captured_at_ms", entry.timestampMs)
                    .put("tag", entry.tag)
                    .put("message", entry.message)
                    .put("thread", Thread.currentThread().name)
                    .put("capture_mode", captureMode.name)
                    .put("process_id", android.os.Process.myPid())
                    .put("stack", throwable?.stackTraceToString() ?: entry.message)
                    .put("recent_logs", recentLogs)
                if (prefs != null) {
                    report.put(
                        "evaluation_context",
                        JSONObject()
                            .put("job_date", prefs.getString("evaluation_job_date", "") ?: "")
                            .put("running_date", prefs.getString("evaluation_running_date", "") ?: "")
                            .put("phase", prefs.getString("evaluation_phase", "") ?: "")
                            .put("completed_snapshots", prefs.getInt("evaluation_completed_snapshots", 0))
                            .put("total_snapshots", prefs.getInt("evaluation_total_snapshots", 0))
                            .put("produced_count", prefs.getInt("last_evaluation_produced_count", 0))
                            .put("persisted_count", prefs.getInt("last_evaluation_outcome_count", 0))
                            .put("last_message", prefs.getString("last_evaluation_message", "") ?: "")
                    )
                    report.put(
                        "service_context",
                        JSONObject()
                            .put("service_running", prefs.getBoolean("service_running", false))
                            .put("last_poll_time", prefs.getString("last_poll_time", "") ?: "")
                            .put("poll_count", prefs.getInt("poll_count", 0))
                    )
                }
                if (extra != null) {
                    report.put("extra", extra)
                }
                file.writeText(report.toString(2))
            } catch (_: Exception) {
            }
        }
    }
}
