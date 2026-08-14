package com.marketradar.app

import android.content.Context
import com.marketradar.app.util.LogBuffer
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.RandomAccessFile
import java.time.LocalDate
import java.time.ZoneOffset
import java.time.format.DateTimeParseException
import java.time.temporal.ChronoUnit

object EvaluationLocalCache {
    private const val TAG = "EvaluationLocalCache"
    private const val DIR_NAME = "evaluation_local_cache"
    private const val RETENTION_DAYS = 45L
    private const val MAX_ROWS_PER_SESSION = 90
    private const val MAX_SUMMARY_ROWS_PER_SESSION = 120
    private const val MAX_SUMMARY_BYTES_PER_SESSION = 512L * 1024L
    // Local fallback snapshots do not need the full raw poll payload. Keep enough
    // compact evaluator-grade evidence for post-close replay without letting the
    // phone accumulate tens of MB during the live session.
    private const val MAX_BYTES_PER_SESSION = 16L * 1024L * 1024L

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

    private fun brainSnapshotSummaryFile(context: Context, sessionDate: String): File {
        return File(cacheDir(context), "brain_snapshot_summaries_${safeDate(sessionDate)}.jsonl")
    }

    private fun build3AbFile(context: Context, sessionDate: String): File {
        return File(cacheDir(context), "build3_ab_${safeDate(sessionDate)}.jsonl")
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
            file.isFile &&
                (
                    file.name.startsWith("brain_snapshots_") ||
                        file.name.startsWith("brain_snapshot_summaries_") ||
                        file.name.startsWith("build3_ab_")
                    ) &&
                file.name.endsWith(".jsonl")
        }?.forEach { file ->
            val sessionDate = file.name
                .removePrefix("brain_snapshot_summaries_")
                .removePrefix("brain_snapshots_")
                .removePrefix("build3_ab_")
                .removeSuffix(".jsonl")
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

    private fun trimToLimit(
        rows: LinkedHashMap<String, String>,
        totalBytesRef: LongArray,
        maxRows: Int,
        maxBytes: Long
    ): Boolean {
        var trimmed = false
        while (rows.size > maxRows) {
            val oldest = rows.entries.firstOrNull() ?: break
            rows.remove(oldest.key)
            totalBytesRef[0] -= rowBytes(oldest.value)
            if (totalBytesRef[0] < 0L) totalBytesRef[0] = 0L
            trimmed = true
        }
        while (rows.isNotEmpty() && totalBytesRef[0] > maxBytes) {
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
        val tmp = File(file.parentFile, "${file.name}.tmp")
        tmp.bufferedWriter(Charsets.UTF_8).use { writer ->
            rows.values.forEach { row ->
                writer.write(row)
                writer.newLine()
            }
        }
        if (!tmp.renameTo(file)) {
            tmp.copyTo(file, overwrite = true)
            tmp.delete()
        }
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

    private fun parseJsonObject(raw: Any?): JSONObject? {
        return when (raw) {
            is JSONObject -> raw
            is String -> {
                val trimmed = raw.trim()
                if (!trimmed.startsWith("{")) null else try { JSONObject(trimmed) } catch (_: Exception) { null }
            }
            else -> null
        }
    }

    private fun parseJsonArray(raw: Any?): JSONArray? {
        return when (raw) {
            is JSONArray -> raw
            is String -> {
                val trimmed = raw.trim()
                if (!trimmed.startsWith("[")) null else try { JSONArray(trimmed) } catch (_: Exception) { null }
            }
            else -> null
        }
    }

    private fun compactCandidate(raw: Any?): JSONObject? {
        val src = parseJsonObject(raw) ?: return null
        val out = JSONObject()
        val keys = arrayOf(
            "candidate_id",
            "id",
            "type",
            "strategy",
            "strategy_type",
            "index",
            "mode",
            "trade_mode",
            "lane",
            "role",
            "score",
            "probability",
            "probProfit",
            "expected_r",
            "ev",
            "premiumEdge",
            "marketConfidence",
            "entryConfidence",
            "entryEligible",
            "entryGate",
            "entryEligibility",
            "netPremium",
            "entry_credit",
            "max_profit",
            "maxProfit",
            "max_loss",
            "maxLoss",
            "width",
            "sell_strike",
            "sellStrike",
            "buy_strike",
            "buyStrike",
            "sell_strike2",
            "sellStrike2",
            "buy_strike2",
            "buyStrike2",
            "sigmaOTM",
            "ivRichness",
            "creditWidthRatio",
            "rank"
        )
        for (key in keys) {
            val value = src.opt(key)
            if (value != null && value != JSONObject.NULL) out.put(key, value)
        }
        return if (out.length() > 0) out else null
    }

    private fun compactCandidates(raw: Any?, limit: Int = Int.MAX_VALUE): JSONArray {
        val source = parseJsonArray(raw) ?: return JSONArray()
        val out = JSONArray()
        for (i in 0 until minOf(source.length(), limit)) {
            compactCandidate(source.opt(i))?.let(out::put)
        }
        return out
    }

    private fun compactBrainSnapshot(snapshot: JSONObject): JSONObject {
        val compact = JSONObject()
        val scalarKeys = arrayOf(
            "id",
            "recommendation_id",
            "session_date",
            "poll_ts",
            "action",
            "strategy",
            "direction",
            "confidence",
            "is_labelable",
            "brain_version",
            "app_version",
            "pre_alignment_action",
            "pre_alignment_strategy",
            "dominant_lane",
            "dominant_count",
            "execution_aligned"
        )
        for (key in scalarKeys) {
            val value = snapshot.opt(key)
            if (value != null && value != JSONObject.NULL) compact.put(key, value)
        }

        compactCandidate(snapshot.opt("primary_candidate_json"))?.let {
            compact.put("primary_candidate_json", it.toString())
        }

        val context = parseJsonObject(snapshot.opt("context_json")) ?: JSONObject()
        val generated = parseJsonArray(context.opt("snapshot_generated_candidates"))
            ?: parseJsonArray(snapshot.opt("top_candidates_json"))
            ?: JSONArray()
        val rankedFull = parseJsonArray(context.opt("snapshot_ranked_candidates_full"))
        val rejected = parseJsonArray(context.opt("snapshot_rejected_candidates_full"))
            ?: parseJsonArray(context.opt("snapshot_rejected_candidates"))

        val compactContext = JSONObject()
        val contextKeys = arrayOf(
            "vix",
            "bnfSpot",
            "nfSpot",
            "significant_move",
            "snapshot_generation_skip_reason"
        )
        for (key in contextKeys) {
            val value = context.opt(key)
            if (value != null && value != JSONObject.NULL) compactContext.put(key, value)
        }

        parseJsonArray(context.opt("snapshot_generation_skip_reasons"))?.let {
            if (it.length() > 0) compactContext.put("snapshot_generation_skip_reasons", it)
        }
        parseJsonObject(context.opt("snapshot_rejected_candidate_stats"))?.let {
            compactContext.put("snapshot_rejected_candidate_stats", it)
        }
        parseJsonObject(context.opt("snapshot_build3_gate"))?.let {
            compactContext.put("snapshot_build3_gate", it)
        }
        parseJsonObject(context.opt("snapshot_build3_lane_gate"))?.let {
            compactContext.put("snapshot_build3_lane_gate", it)
        }
        parseJsonObject(context.opt("snapshot_build3_flow"))?.let {
            compactContext.put("snapshot_build3_flow", it)
        }
        // This small, immutable frame is the post-close C3 source of truth.
        // Keep it even when the full context is compacted for local fallback.
        parseJsonObject(context.opt("c3_finalization_frame"))?.let {
            compactContext.put("c3_finalization_frame", it)
        }

        val gap = parseJsonObject(context.opt("gap"))
        if (gap != null) {
            val gapType = gap.opt("type")
            if (gapType != null && gapType != JSONObject.NULL) {
                compactContext.put("gap", JSONObject().put("type", gapType))
            }
        }

        val compactGenerated = compactCandidates(generated)
        val compactRankedFull = compactCandidates(rankedFull)
        val compactRejected = compactCandidates(rejected)

        compactContext.put("snapshot_generated_candidates", compactGenerated)
        if (compactRankedFull.length() > 0) {
            compactContext.put("snapshot_ranked_candidates_full", compactRankedFull)
        }
        if (compactRejected.length() > 0) {
            compactContext.put("snapshot_rejected_candidates", compactRejected)
        }

        compact.put("context_json", compactContext.toString())
        compact.put("top_candidates_json", compactGenerated.toString())
        return compact
    }

    private fun compactSnapshotSummary(snapshot: JSONObject): JSONObject {
        val compact = JSONObject()
        val scalarKeys = arrayOf(
            "id",
            "session_date",
            "poll_ts",
            "action",
            "strategy",
            "confidence",
            "is_labelable",
            "brain_version",
            "app_version"
        )
        for (key in scalarKeys) {
            val value = snapshot.opt(key)
            if (value != null && value != JSONObject.NULL) compact.put(key, value)
        }
        compactCandidate(snapshot.opt("primary_candidate_json"))?.let {
            compact.put("primary_candidate_json", it.toString())
        }

        val context = parseJsonObject(snapshot.opt("context_json")) ?: JSONObject()
        val generated = parseJsonArray(context.opt("snapshot_generated_candidates"))
            ?: parseJsonArray(snapshot.opt("top_candidates_json"))
            ?: JSONArray()
        val rankedFull = parseJsonArray(context.opt("snapshot_ranked_candidates_full"))
        val compactGenerated = compactCandidates(generated, 12)
        val compactRankedFull = compactCandidates(rankedFull, 50)
        val compactContext = JSONObject()
        val contextKeys = arrayOf(
            "vix",
            "bnfSpot",
            "nfSpot",
            "significant_move",
            "snapshot_generation_skip_reason"
        )
        for (key in contextKeys) {
            val value = context.opt(key)
            if (value != null && value != JSONObject.NULL) compactContext.put(key, value)
        }
        parseJsonArray(context.opt("snapshot_generation_skip_reasons"))?.let {
            if (it.length() > 0) compactContext.put("snapshot_generation_skip_reasons", it)
        }
        parseJsonObject(context.opt("snapshot_rejected_candidate_stats"))?.let {
            compactContext.put("snapshot_rejected_candidate_stats", it)
        }
        parseJsonObject(context.opt("snapshot_build3_gate"))?.let {
            compactContext.put("snapshot_build3_gate", it)
        }
        parseJsonObject(context.opt("snapshot_build3_lane_gate"))?.let {
            compactContext.put("snapshot_build3_lane_gate", it)
        }
        parseJsonObject(context.opt("snapshot_build3_flow"))?.let {
            compactContext.put("snapshot_build3_flow", it)
        }
        parseJsonObject(context.opt("c3_finalization_frame"))?.let {
            compactContext.put("c3_finalization_frame", it)
        }
        compactContext.put("snapshot_generated_candidates", compactGenerated)
        if (compactRankedFull.length() > 0) {
            compactContext.put("snapshot_ranked_candidates_full", compactRankedFull)
        }
        compact.put("context_json", compactContext.toString())
        compact.put("top_candidates_json", compactGenerated.toString())
        return compact
    }

    private fun appendSnapshotSummary(context: Context, sessionDate: String, snapshot: JSONObject): Boolean {
        val file = brainSnapshotSummaryFile(context, sessionDate)
        val state = loadSnapshotState(file)
        val summary = compactSnapshotSummary(snapshot)
        val key = snapshotKey(summary)
        val json = summary.toString()
        val previous = state.rows.put(key, json)
        if (previous != null) {
            state.totalBytes -= rowBytes(previous)
            if (state.totalBytes < 0L) state.totalBytes = 0L
        }
        state.totalBytes += rowBytes(json)
        val byteCounterRef = longArrayOf(state.totalBytes)
        val trimmed = trimToLimit(
            state.rows,
            byteCounterRef,
            MAX_SUMMARY_ROWS_PER_SESSION,
            MAX_SUMMARY_BYTES_PER_SESSION
        )
        state.totalBytes = byteCounterRef[0]
        rewriteCanonicalFile(file, state.rows)
        state.needsRewrite = false
        if (trimmed) {
            LogBuffer.add(
                'I',
                TAG,
                "LOCAL_SNAPSHOT_SUMMARY_TRIM: date=$sessionDate rows=${state.rows.size} bytes=${state.totalBytes} rowCap=$MAX_SUMMARY_ROWS_PER_SESSION byteCap=$MAX_SUMMARY_BYTES_PER_SESSION"
            )
        }
        LogBuffer.add('D', TAG, "LOCAL_SNAPSHOT_SUMMARY_APPEND: date=$sessionDate bytes=${file.length()}")
        return true
    }

    @Synchronized
    fun appendBrainSnapshot(context: Context, sessionDate: String, snapshot: JSONObject): Boolean {
        return try {
            pruneExpiredCacheFiles(context)
            val file = brainSnapshotFile(context, sessionDate)
            val state = loadSnapshotState(file)
            val compactSnapshot = compactBrainSnapshot(snapshot)
            val key = snapshotKey(compactSnapshot)
            if (state.rows.containsKey(key)) {
                LogBuffer.add('I', TAG, "LOCAL_SNAPSHOT_SKIP_DUP: date=$sessionDate key=$key")
                return true
            }
            val rawBytes = rowBytes(snapshot.toString())
            val json = compactSnapshot.toString()
            val compactBytes = rowBytes(json)
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
            LogBuffer.add(
                'I',
                TAG,
                "LOCAL_SNAPSHOT_COMPACTED: date=$sessionDate rawBytes=$rawBytes compactBytes=$compactBytes"
            )
            LogBuffer.add('D', TAG, "LOCAL_SNAPSHOT_APPEND: date=$sessionDate bytes=${file.length()}")
            try {
                appendSnapshotSummary(context, sessionDate, compactSnapshot)
            } catch (summaryError: Throwable) {
                LogBuffer.add('W', TAG, "LOCAL_SNAPSHOT_SUMMARY_APPEND_FAIL: date=$sessionDate error=${summaryError.message}")
            }
            true
        } catch (e: Throwable) {
            LogBuffer.add('W', TAG, "LOCAL_SNAPSHOT_APPEND_FAIL: date=$sessionDate error=${e.message}")
            false
        }
    }

    @Synchronized
    fun readBrainSnapshots(context: Context, sessionDate: String): JSONArray {
        val out = JSONArray()
        val seen = linkedSetOf<String>()

        try {
            pruneExpiredCacheFiles(context)
            val file = brainSnapshotFile(context, sessionDate)
            if (!file.exists()) return out
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
        } catch (e: Throwable) {
            LogBuffer.add('W', TAG, "LOCAL_SNAPSHOT_READ_FAIL: date=$sessionDate error=${e.message}")
        }
        return out
    }

    @Synchronized
    fun readRecentBrainSnapshotSummaries(
        context: Context,
        sessionDate: String,
        limit: Int,
        maxBytes: Long
    ): JSONArray {
        val out = JSONArray()
        val safeLimit = limit.coerceAtLeast(1)
        val safeMaxBytes = maxBytes.coerceAtLeast(1024L)
        val cappedRows = linkedMapOf<String, String>()
        var cappedBytes = 0L

        try {
            pruneExpiredCacheFiles(context)
            val file = brainSnapshotSummaryFile(context, sessionDate)
            if (!file.exists()) return out

            readRecentLinesFromEnd(file, safeLimit, safeMaxBytes).forEach { line ->
                val trimmed = line.trim()
                if (trimmed.isBlank()) return@forEach
                val row = try { JSONObject(trimmed) } catch (_: Exception) { return@forEach }
                val key = snapshotKey(row)
                val json = row.toString()
                val previous = cappedRows.remove(key)
                if (previous != null) {
                    cappedBytes -= rowBytes(previous)
                    if (cappedBytes < 0L) cappedBytes = 0L
                }
                cappedRows[key] = json
                cappedBytes += rowBytes(json)
                while (cappedRows.size > safeLimit || (cappedRows.size > 1 && cappedBytes > safeMaxBytes)) {
                    val oldest = cappedRows.entries.firstOrNull() ?: break
                    cappedRows.remove(oldest.key)
                    cappedBytes -= rowBytes(oldest.value)
                    if (cappedBytes < 0L) cappedBytes = 0L
                }
            }

            cappedRows.values.toList().asReversed().forEach { json ->
                val row = try { JSONObject(json) } catch (_: Exception) { return@forEach }
                out.put(row)
            }
            LogBuffer.add(
                'I',
                TAG,
                "LOCAL_SNAPSHOT_SUMMARY_READ_RECENT: date=$sessionDate rows=${out.length()} bytes=$cappedBytes fileBytes=${file.length()} limit=$safeLimit byteCap=$safeMaxBytes"
            )
        } catch (e: Exception) {
            LogBuffer.add('W', TAG, "LOCAL_SNAPSHOT_SUMMARY_READ_RECENT_FAIL: date=$sessionDate error=${e.message}")
        }
        return out
    }

    @Synchronized
    fun readRecentBrainSnapshots(
        context: Context,
        sessionDate: String,
        limit: Int,
        maxBytes: Long
    ): JSONArray {
        val out = JSONArray()
        val safeLimit = limit.coerceAtLeast(1)
        val safeMaxBytes = maxBytes.coerceAtLeast(1024L)
        val cappedRows = linkedMapOf<String, String>()
        var cappedBytes = 0L

        try {
            pruneExpiredCacheFiles(context)
            val file = brainSnapshotFile(context, sessionDate)
            if (!file.exists()) return out

            readRecentLinesFromEnd(file, safeLimit, safeMaxBytes).forEach { line ->
                val trimmed = line.trim()
                if (trimmed.isBlank()) return@forEach
                val row = try { JSONObject(trimmed) } catch (_: Exception) { return@forEach }
                val key = snapshotKey(row)
                val json = row.toString()
                val previous = cappedRows.remove(key)
                if (previous != null) {
                    cappedBytes -= rowBytes(previous)
                    if (cappedBytes < 0L) cappedBytes = 0L
                }
                cappedRows[key] = json
                cappedBytes += rowBytes(json)

                while (cappedRows.size > safeLimit || (cappedRows.size > 1 && cappedBytes > safeMaxBytes)) {
                    val oldest = cappedRows.entries.firstOrNull() ?: break
                    cappedRows.remove(oldest.key)
                    cappedBytes -= rowBytes(oldest.value)
                    if (cappedBytes < 0L) cappedBytes = 0L
                }
            }

            cappedRows.values.toList().asReversed().forEach { json ->
                val row = try { JSONObject(json) } catch (_: Exception) { return@forEach }
                out.put(row)
            }
            LogBuffer.add(
                'I',
                TAG,
                "LOCAL_SNAPSHOT_READ_RECENT: date=$sessionDate rows=${out.length()} bytes=$cappedBytes fileBytes=${file.length()} limit=$safeLimit byteCap=$safeMaxBytes"
            )
        } catch (e: Exception) {
            LogBuffer.add('W', TAG, "LOCAL_SNAPSHOT_READ_RECENT_FAIL: date=$sessionDate error=${e.message}")
        }
        return out
    }

    private fun readRecentLinesFromEnd(file: File, limit: Int, maxBytes: Long): List<String> {
        val lines = ArrayList<String>()
        if (!file.exists() || file.length() <= 0L) return lines

        RandomAccessFile(file, "r").use { raf ->
            var pointer = raf.length() - 1
            val line = StringBuilder()
            var collectedBytes = 0L

            while (pointer >= 0 && lines.size < limit && (lines.isEmpty() || collectedBytes < maxBytes)) {
                raf.seek(pointer)
                val ch = raf.read().toChar()
                if (ch == '\n') {
                    if (line.isNotEmpty()) {
                        val value = line.reverse().toString()
                        lines.add(value)
                        collectedBytes += rowBytes(value)
                        line.clear()
                    }
                } else {
                    line.append(ch)
                }
                pointer--
            }

            if (line.isNotEmpty() && lines.size < limit && (lines.isEmpty() || collectedBytes < maxBytes)) {
                lines.add(line.reverse().toString())
            }
        }

        return lines
    }

    @Synchronized
    fun appendBuild3AbDecision(context: Context, sessionDate: String, row: JSONObject): Boolean {
        return try {
            pruneExpiredCacheFiles(context)
            val file = build3AbFile(context, sessionDate)
            file.appendText(row.toString() + "\n")
            LogBuffer.add('D', TAG, "LOCAL_BUILD3_AB_APPEND: date=$sessionDate bytes=${file.length()}")
            true
        } catch (e: Exception) {
            LogBuffer.add('W', TAG, "LOCAL_BUILD3_AB_APPEND_FAIL: date=$sessionDate error=${e.message}")
            false
        }
    }
}
