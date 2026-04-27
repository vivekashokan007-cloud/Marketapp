package com.marketradar.app.util
import android.util.Log

import java.util.concurrent.ConcurrentLinkedDeque

object LogBuffer {
    private const val MAX_ENTRIES = 2000
    private const val MAX_AGE_MS = 30 * 60 * 1000L  // 30 minutes

    enum class CaptureMode { UNINITIALIZED, LOGCAT, LOGTAP }
    @Volatile var captureMode: CaptureMode = CaptureMode.UNINITIALIZED

    data class Entry(
        val timestampMs: Long,
        val level: Char,    // V D I W E F
        val tag: String,
        val message: String
    )

    private val buffer = ConcurrentLinkedDeque<Entry>()

    fun add(level: Char, tag: String, message: String) {
        val now = System.currentTimeMillis()
        buffer.addFirst(Entry(now, level, tag, message))
        
        // Mirror to system logcat for ADB visibility
        when (level) {
            'V' -> Log.v(tag, message)
            'D' -> Log.d(tag, message)
            'I' -> Log.i(tag, message)
            'W' -> Log.w(tag, message)
            'E' -> Log.e(tag, message)
            'F' -> Log.wtf(tag, message)
            else -> Log.i(tag, message)
        }

        // Trim by count
        while (buffer.size > MAX_ENTRIES) buffer.pollLast()
        // Trim by age (best-effort, runs on every add)
        val cutoff = now - MAX_AGE_MS
        while (buffer.isNotEmpty() && (buffer.peekLast()?.timestampMs ?: 0L) < cutoff) {
            buffer.pollLast()
        }
    }

    fun snapshot(filter: String?): List<Entry> {
        val all = buffer.toList()  // snapshot copy
        return when (filter) {
            null, "ALL" -> all
            "Kotlin"   -> all.filter { it.tag.startsWith("MV_") || it.tag == "MarketWatchService" || it.tag == "NativeBridge" || it.tag == "MainActivity" }
            "Python"   -> all.filter { it.tag == "py.stdout" || it.tag == "py.stderr" || it.tag == "Chaquopy" }
            "OkHttp"   -> all.filter { it.tag.startsWith("OkHttp") || it.tag == "okhttp.OkHttpClient" }
            "Errors"   -> all.filter { it.level == 'E' || it.level == 'F' || it.level == 'W' }
            else       -> all
        }
    }

    fun clear() { buffer.clear() }
    fun size(): Int = buffer.size
}
