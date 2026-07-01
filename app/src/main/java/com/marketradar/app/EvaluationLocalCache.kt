package com.marketradar.app

import android.content.Context
import com.marketradar.app.util.LogBuffer
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.time.LocalDate
import java.time.ZoneOffset
import java.time.format.DateTimeParseException
import java.time.temporal.ChronoUnit

object EvaluationLocalCache {
    private const val TAG = "EvaluationLocalCache"
    private const val DIR_NAME = "evaluation_local_cache"
    private const val RETENTION_DAYS = 45L
    private const val MAX_ROWS_PER_SESSION = 90
    private const val MAX_BYTES_PER_SESSION = 5L * 1024L * 1024L

    private data class SnapshotFileState(
        val rows: LinkedHashMap<String, String>,
        var totalBytes: Long,
        var needsRewrite: Boolean
    )

    private val snapshotStateByPath = mutableMapOf<String, SnapshotFileState>()

    private fun cacheDir(context: Context): File {
        return File(context.applicationContext.filesDir, DIR_NAME).apply { mkdirs() }
    }

    private fun safeDate(date: String): String {
        return date.filter { it.isDigit() || it == '-' }.ifBlank { "unknown" }
    }

    private fun brainSnapshotFile(context: Context, sessionDate: String): File {
        return File(cacheDir(context), "brain_snapshots_${safeDate(sessionDate)}.jsonl")
    }

    private fun snapshotKey(row: JSONObject): String {
        return row.optString("id").ifBlank {
            "${row.optString("poll_ts")}|${row.optString("recommendation_id")}"
        }.ifBlank {
            row.toString()
        }
    }

    private fun pruneExpiredCacheFiles(context: Context) {
        val today = LocalDate.now(ZoneOffset.UTC)
        cacheDir(context).listFiles { file ->
            file.isFile && file.name.startsWith("brain_snapshots_") && file.name.endsWith(".jsonl")
        }?.forEach { file ->
            val sessionDate = file.name.removePrefix("brain_snapshots_").removeSuffix(".jsonl")
            val keep = try {
                val ageDays = ChronoUnit.DAYS.between(LocalDate.parse(sessionDate), today)
                ageDays in 0..RETENTION_DAYS
            } catch (_: DateTimeParseException) {
                val ageMs = today.plusDays(1).atStartOfDay().toInstant(ZoneOffset.UTC).toEpochMilli() - file.lastModified()
                ageMs <= RETENTION_DAYS * 24L * 60L * 60L * 1000L
            }
            if (!keep && file.delete()) {
                snapshotStateByPath.remove(file.absolutePath)
                LogBuffer.add('I', TAG, "LOCAL_SNAPSHOT_PRUNE: removed=${file.name}")
            }
        }
    }

    private fun rowBytes(json: String): Long {
        return json.toByteArray(Charsets.UTF_8).size.toLong() + 1L
    }

    private fun trimToRecentLimit(rows: LinkedHashMap<String, String>, totalBytesRef: LongArray): Boolean {
        var trimmed = false
        while (rows.size > MAX_ROWS_PER_SESSION) {
            val oldest = rows.entries.firstOrNull() ?: break
            rows.remove(oldest.key)
            totalBytesRef[0] -= rowBytes(oldest.value)
            if (totalBytesRef[0] < 0L) totalBytesRef[0] = 0L
            trimmed = true
        }
        return trimmed
    }

    private fun trimToByteLimit(rows: LinkedHashMap<String, String>, totalBytesRef: LongArray): Boolean {
        var trimmed = false
        while (rows.isNotEmpty() && totalBytesRef[0] > MAX_BYTES_PER_SESSION) {
            val oldest = rows.entries.firstOrNull() ?: break
            rows.remove(oldest.key)
            totalBytesRef[0] -= rowBytes(oldest.value)
            if (totalBytesRef[0] < 0L) totalBytesRef[0] = 0L
            trimmed = true
        }
        return trimmed
    }

    private fun rewriteCanonicalFile(file: File, rows: LinkedHashMap<String, String>) {
        if (rows.isEmpty()) {
            if (file.exists()) file.writeText("")
            return
        }
        val content = buildString(rows.size * 256) {
            rows.values.forEach { append(it).append('\n') }
        }
        file.writeText(content)
    }

    private fun loadSnapshotState(file: File): SnapshotFileState {
        snapshotStateByPath[file.absolutePath]?.let { return it }

        val rows = linkedMapOf<String, String>()
        val totalBytesRef = longArrayOf(0L)
        var needsRewrite = false
        if (file.exists()) {
            file.forEachLine { line ->
                val trimmed = line.trim()
                if (trimmed.isBlank()) {
                    needsRewrite = true
                    return@forEachLine
                }
                val row = try { JSONObject(trimmed) } catch (_: Exception) {
                    needsRewrite = true
                    return@forEachLine
                }
                val key = snapshotKey(row)
                if (rows.putIfAbsent(key, row.toString()) != null) {
                    needsRewrite = true
                } else {
                    totalBytesRef[0] += rowBytes(row.toString())
                }
            }
        }
        if (trimToRecentLimit(rows, totalBytesRef)) {
            needsRewrite = true
        }
        if (trimToByteLimit(rows, totalBytesRef)) {
            needsRewrite = true
        }
        val state = SnapshotFileState(rows, totalBytesRef[0], needsRewrite)
        snapshotStateByPath[file.absolutePath] = state
        return state
    }

    @Synchronized
    fun appendBrainSnapshot(context: Context, sessionDate: String, snapshot: JSONObject): Boolean {
        return try {
            pruneExpiredCacheFiles(context)
            val file = brainSnapshotFile(context, sessionDate)
            val state = loadSnapshotState(file)
            val key = snapshotKey(snapshot)
            if (state.rows.containsKey(key)) {
                LogBuffer.add('I', TAG, "LOCAL_SNAPSHOT_SKIP_DUP: date=$sessionDate key=$key")
                return true
            }
            val json = snapshot.toString()
            state.rows[key] = json
            state.totalBytes += rowBytes(json)
            val byteCounterRef = longArrayOf(state.totalBytes)
            val trimmedByRows = trimToRecentLimit(state.rows, byteCounterRef)
            val trimmedByBytes = trimToByteLimit(state.rows, byteCounterRef)
            state.totalBytes = byteCounterRef[0]
            if (state.needsRewrite || trimmedByRows || trimmedByBytes) {
                rewriteCanonicalFile(file, state.rows)
                state.needsRewrite = false
                if (trimmedByRows || trimmedByBytes) {
                    LogBuffer.add(
                        'I',
                        TAG,
                        "LOCAL_SNAPSHOT_TRIM: date=$sessionDate rows=${state.rows.size} bytes=${state.totalBytes} rowCap=$MAX_ROWS_PER_SESSION byteCap=$MAX_BYTES_PER_SESSION"
                    )
                } else {
                    LogBuffer.add('I', TAG, "LOCAL_SNAPSHOT_COMPACT_ONCE: file=${file.name} rows=${state.rows.size}")
                }
            } else {
                file.appendText(json + "\n")
            }
            LogBuffer.add('D', TAG, "LOCAL_SNAPSHOT_APPEND: date=$sessionDate bytes=${file.length()}")
            true
        } catch (e: Exception) {
            LogBuffer.add('W', TAG, "LOCAL_SNAPSHOT_APPEND_FAIL: date=$sessionDate error=${e.message}")
            false
        }
    }

    fun readBrainSnapshots(context: Context, sessionDate: String): JSONArray {
        val out = JSONArray()
        val seen = linkedSetOf<String>()
        pruneExpiredCacheFiles(context)
        val file = brainSnapshotFile(context, sessionDate)
        if (!file.exists()) return out

        try {
            val state = loadSnapshotState(file)
            if (state.needsRewrite) {
                rewriteCanonicalFile(file, state.rows)
                state.needsRewrite = false
                LogBuffer.add('I', TAG, "LOCAL_SNAPSHOT_COMPACT_ON_READ: file=${file.name} rows=${state.rows.size}")
            }
            state.rows.values.forEach { json ->
                val row = try { JSONObject(json) } catch (_: Exception) { return@forEach }
                val key = snapshotKey(row)
                if (key.isBlank() || seen.add(key)) out.put(row)
            }
            LogBuffer.add('I', TAG, "LOCAL_SNAPSHOT_READ: date=$sessionDate rows=${out.length()} bytes=${file.length()}")
        } catch (e: Exception) {
            LogBuffer.add('W', TAG, "LOCAL_SNAPSHOT_READ_FAIL: date=$sessionDate error=${e.message}")
        }
        return out
    }
}
