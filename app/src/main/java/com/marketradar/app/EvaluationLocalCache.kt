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
                LogBuffer.add('I', TAG, "LOCAL_SNAPSHOT_PRUNE: removed=${file.name}")
            }
        }
    }

    private fun compactFileInPlace(file: File): Set<String> {
        if (!file.exists()) return emptySet()
        val uniqueRows = linkedMapOf<String, String>()
        var changed = false
        file.forEachLine { line ->
            val trimmed = line.trim()
            if (trimmed.isBlank()) {
                changed = true
                return@forEachLine
            }
            val row = try { JSONObject(trimmed) } catch (_: Exception) {
                changed = true
                return@forEachLine
            }
            val key = snapshotKey(row)
            if (uniqueRows.putIfAbsent(key, row.toString()) != null) {
                changed = true
            }
        }
        if (changed) {
            val content = buildString {
                uniqueRows.values.forEach { append(it).append('\n') }
            }
            file.writeText(content)
            LogBuffer.add('I', TAG, "LOCAL_SNAPSHOT_COMPACT: file=${file.name} rows=${uniqueRows.size}")
        }
        return uniqueRows.keys
    }

    @Synchronized
    fun appendBrainSnapshot(context: Context, sessionDate: String, snapshot: JSONObject): Boolean {
        return try {
            pruneExpiredCacheFiles(context)
            val file = brainSnapshotFile(context, sessionDate)
            val keys = compactFileInPlace(file)
            val key = snapshotKey(snapshot)
            if (keys.contains(key)) {
                LogBuffer.add('I', TAG, "LOCAL_SNAPSHOT_SKIP_DUP: date=$sessionDate key=$key")
                return true
            }
            file.appendText(snapshot.toString() + "\n")
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
            compactFileInPlace(file)
            file.forEachLine { line ->
                val trimmed = line.trim()
                if (trimmed.isBlank()) return@forEachLine
                val row = try { JSONObject(trimmed) } catch (_: Exception) { return@forEachLine }
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
