package com.marketradar.app

import android.content.Context
import com.marketradar.app.util.LogBuffer
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

object EvaluationLocalCache {
    private const val TAG = "EvaluationLocalCache"
    private const val DIR_NAME = "evaluation_local_cache"

    private fun cacheDir(context: Context): File {
        return File(context.applicationContext.filesDir, DIR_NAME).apply { mkdirs() }
    }

    private fun safeDate(date: String): String {
        return date.filter { it.isDigit() || it == '-' }.ifBlank { "unknown" }
    }

    private fun brainSnapshotFile(context: Context, sessionDate: String): File {
        return File(cacheDir(context), "brain_snapshots_${safeDate(sessionDate)}.jsonl")
    }

    @Synchronized
    fun appendBrainSnapshot(context: Context, sessionDate: String, snapshot: JSONObject): Boolean {
        return try {
            val file = brainSnapshotFile(context, sessionDate)
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
        val file = brainSnapshotFile(context, sessionDate)
        if (!file.exists()) return out

        try {
            file.forEachLine { line ->
                val trimmed = line.trim()
                if (trimmed.isBlank()) return@forEachLine
                val row = try { JSONObject(trimmed) } catch (_: Exception) { return@forEachLine }
                val key = row.optString("id").ifBlank {
                    "${row.optString("poll_ts")}|${row.optString("recommendation_id")}"
                }
                if (key.isBlank() || seen.add(key)) out.put(row)
            }
            LogBuffer.add('I', TAG, "LOCAL_SNAPSHOT_READ: date=$sessionDate rows=${out.length()} bytes=${file.length()}")
        } catch (e: Exception) {
            LogBuffer.add('W', TAG, "LOCAL_SNAPSHOT_READ_FAIL: date=$sessionDate error=${e.message}")
        }
        return out
    }
}
