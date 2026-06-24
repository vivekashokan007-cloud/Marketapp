package com.marketradar.app

import android.util.Log
import com.marketradar.app.util.LogBuffer
import okhttp3.OkHttpClient
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.TimeUnit

object SupabaseClient {
    private const val TAG = "SupabaseClient"
    private const val URL = BuildConfig.SUPABASE_URL
    private const val ANON_KEY = BuildConfig.SUPABASE_ANON_KEY
    // Supabase REST caps page payloads at 1000 rows in this project, so using
    // a larger requested limit causes offset-based pagination gaps.
    private const val CHAIN_PAGE_SIZE = 1000

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()

    private data class PostResult(
        val success: Boolean,
        val table: String? = null,
        val code: Int? = null,
        val message: String? = null,
        val errorBody: String? = null,
        val exceptionMessage: String? = null
    )

    private fun getBaseRequest(path: String): Request.Builder {
        return Request.Builder()
            .url("$URL/rest/v1/$path")
            .addHeader("apikey", ANON_KEY)
            .addHeader("Authorization", "Bearer $ANON_KEY")
            .addHeader("Content-Type", "application/json")
    }

    private val shadowTeacherKeys = listOf(
        "managed_pnl",
        "managed_gross_pnl",
        "friction_cost",
        "exit_reason",
        "exit_step",
        "exit_ts",
        "r_multiple",
        "captured_pct",
        "is_success",
        "risk_at_entry",
        "regime_bucket",
        "label_version",
        "teacher_config_version",
        "tp_threshold",
        "sl_threshold",
        "break_even_win_rate_pct"
    )

    private fun fetchSync(request: Request): String? {
        return try {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    val errorBody = response.body?.string() ?: ""
                    Log.e(TAG, "Request failed: ${response.code} ${response.message} | URL: ${request.url} | Body: $errorBody")
                    null
                } else {
                    response.body?.string()
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Exception: ${e.message}")
            null
        }
    }

    // ML schema can differ across environments. Try multiple table names safely.
    private fun fetchArrayFromTables(paths: List<String>): JSONArray {
        for (path in paths) {
            val request = getBaseRequest(path).get().build()
            val json = fetchSync(request) ?: continue
            try {
                return JSONArray(json)
            } catch (_: Exception) {
            }
        }
        return JSONArray()
    }

    private fun filterRowsByIstSessionDate(rows: JSONArray, date: String): JSONArray {
        val out = JSONArray()
        for (i in 0 until rows.length()) {
            val obj = rows.optJSONObject(i) ?: continue
            if (rowBelongsToIstSessionDate(obj, date)) out.put(obj)
        }
        return out
    }

    private fun rowBelongsToIstSessionDate(obj: JSONObject, date: String): Boolean {
        val istTsFormats = listOf(
            SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssZ", Locale.US),
            SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSX", Locale.US),
            SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssX", Locale.US)
        ).onEach { fmt -> fmt.timeZone = TimeZone.getTimeZone("Asia/Kolkata") }
        val istDateFmt = SimpleDateFormat("yyyy-MM-dd", Locale.US).apply {
            timeZone = TimeZone.getTimeZone("Asia/Kolkata")
        }

        val sessionDate = obj.optString("session_date", "").trim()
        if (sessionDate == date) return true

        val pollTs = obj.optString("poll_ts", "").trim()
        if (pollTs.startsWith(date)) return true
        if (pollTs.isNotBlank()) {
            for (fmt in istTsFormats) {
                try {
                    if (istDateFmt.format(fmt.parse(pollTs) ?: continue) == date) return true
                } catch (_: Exception) {
                }
            }
        }

        val legacyDate = obj.optString("date", "").trim()
        return legacyDate == date
    }

    private fun postToFirstWorkingTableDetailed(
        tableNames: List<String>,
        body: String,
        preferHeader: String = "resolution=merge-duplicates"
    ): PostResult {
        var lastResult = PostResult(success = false)
        for (table in tableNames) {
            val request = getBaseRequest(table)
                .header("Prefer", preferHeader)
                .post(body.toRequestBody("application/json".toMediaTypeOrNull()))
                .build()
            try {
                client.newCall(request).execute().use { response ->
                    if (response.isSuccessful) {
                        return PostResult(
                            success = true,
                            table = table,
                            code = response.code,
                            message = response.message
                        )
                    }
                    val err = response.body?.string() ?: ""
                    lastResult = PostResult(
                        success = false,
                        table = table,
                        code = response.code,
                        message = response.message,
                        errorBody = err
                    )
                    when (response.code) {
                        404 -> {
                            Log.e(TAG, "Post missing ($table): ${response.code} ${response.message} | $err")
                        }
                        400 -> {
                            Log.e(TAG, "Post rejected ($table): ${response.code} ${response.message} | $err")
                        }
                        else -> {
                            Log.e(TAG, "Post failed ($table): ${response.code} ${response.message} | $err")
                        }
                    }
                }
            } catch (e: Exception) {
                lastResult = PostResult(
                    success = false,
                    table = table,
                    exceptionMessage = e.message
                )
                Log.e(TAG, "Post exception ($table): ${e.message}")
            }
        }
        return lastResult
    }

    private fun postToFirstWorkingTable(
        tableNames: List<String>,
        body: String,
        preferHeader: String = "resolution=merge-duplicates"
    ): Boolean {
        return postToFirstWorkingTableDetailed(tableNames, body, preferHeader).success
    }

    data class EvaluationSaveResult(
        val success: Boolean,
        val producedCount: Int,
        val persistedCount: Int,
        val primaryPersistedCount: Int,
        val evaluationPersistedCount: Int,
        val message: String
    )

    data class ChainFeedResult(
        val source: String,
        val rows: JSONArray
    )

    data class ChainStreamResult(
        val source: String,
        val rowCount: Int,
        val pageCount: Int,
        val capped: Boolean = false
    )

    private data class ChainSource(
        val path: String,
        val source: String,
        val filterDate: String?
    )

    data class EvaluationLegKey(
        val indexKey: String,
        val expiry: String,
        val strike: Double,
        val optionType: String
    )

    private fun postArrayToTable(table: String, body: JSONArray): Boolean {
        if (body.length() == 0) return true
        val request = getBaseRequest(table)
            .header("Prefer", "resolution=merge-duplicates")
            .post(body.toString().toRequestBody("application/json".toMediaTypeOrNull()))
            .build()
        return try {
            client.newCall(request).execute().use { response ->
                if (response.isSuccessful) {
                    true
                } else {
                    val err = response.body?.string() ?: ""
                    Log.e(TAG, "Post failed ($table): ${response.code} ${response.message} | $err")
                    false
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Post exception ($table): ${e.message}")
            false
        }
    }

    private fun splitJSONArray(body: JSONArray, chunkSize: Int): List<JSONArray> {
        if (body.length() == 0) return emptyList()
        val safeChunkSize = if (chunkSize > 0) chunkSize else 100
        val chunks = mutableListOf<JSONArray>()
        var index = 0
        while (index < body.length()) {
            val chunk = JSONArray()
            val end = minOf(body.length(), index + safeChunkSize)
            for (i in index until end) {
                body.optJSONObject(i)?.let(chunk::put)
            }
            if (chunk.length() > 0) chunks.add(chunk)
            index = end
        }
        return chunks
    }

    private fun postArrayToTableChunked(
        table: String,
        body: JSONArray,
        chunkSize: Int = 250
    ): Boolean {
        if (body.length() == 0) return true
        val chunks = splitJSONArray(body, chunkSize)
        if (chunks.isEmpty()) return true
        var chunkIndex = 0
        for (chunk in chunks) {
            chunkIndex += 1
            if (!postArrayToTable(table, chunk)) {
                Log.e(TAG, "Chunked post failed ($table) chunk=$chunkIndex/${chunks.size} rows=${chunk.length()}")
                return false
            }
        }
        return true
    }

    private fun countRows(table: String, filter: String? = null): Int {
        val queryParams = mutableListOf<String>()
        if (filter != null) queryParams.add(filter)
        queryParams.add("select=id")
        val url = "$table?${queryParams.joinToString("&")}"
        return try {
            val request = getBaseRequest(url)
                .addHeader("Prefer", "count=exact")
                .addHeader("Range-Unit", "items")
                .addHeader("Range", "0-0")
                .get()
                .build()
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    val errorBody = response.body?.string() ?: ""
                    Log.e(TAG, "Count request failed: ${response.code} ${response.message} | URL: ${request.url} | Body: $errorBody")
                    return@use 0
                }
                val contentRange = response.header("Content-Range")
                val exactCount = contentRange
                    ?.substringAfter("/", "")
                    ?.trim()
                    ?.takeIf { it != "*" }
                    ?.toIntOrNull()
                if (exactCount != null) {
                    return@use exactCount
                }
                Log.w(TAG, "Count request missing exact Content-Range for $table filter=$filter; falling back to body count")
                val json = response.body?.string() ?: "[]"
                JSONArray(json).length()
            }
        } catch (e: Exception) {
            Log.e(TAG, "Count rows failed for $table filter=$filter: ${e.message}")
            0
        }
    }

    private fun fetchArray(path: String): JSONArray? {
        val request = getBaseRequest(path).get().build()
        val json = fetchSync(request) ?: return null
        return try {
            JSONArray(json)
        } catch (_: Exception) {
            null
        }
    }

    private fun fetchPagedArray(
        basePath: String,
        pageSize: Int = CHAIN_PAGE_SIZE,
        maxPages: Int = 30
    ): JSONArray {
        val out = JSONArray()
        var offset = 0
        repeat(maxPages) {
            val separator = if (basePath.contains("?")) "&" else "?"
            val page = fetchArray("$basePath${separator}limit=$pageSize&offset=$offset") ?: return out
            if (page.length() == 0) return out
            for (i in 0 until page.length()) {
                page.optJSONObject(i)?.let(out::put)
            }
            if (page.length() < pageSize) return out
            offset += page.length()
        }
        return out
    }

    private fun normalizedChainRow(src: JSONObject): JSONObject? {
        val indexKey = src.optString("index_key")
            .ifBlank { src.optString("index") }
            .ifBlank { src.optString("symbol") }
            .trim()
        if (indexKey.isBlank()) return null

        val pollTs = src.optString("poll_ts")
            .ifBlank { src.optString("created_at") }
            .ifBlank { src.optString("timestamp") }
            .trim()
        if (pollTs.isBlank()) return null

        val expiry = src.optString("expiry")
            .ifBlank { src.optString("expiry_date") }
            .trim()

        val optionType = src.optString("option_type")
            .ifBlank { src.optString("type") }
            .ifBlank { src.optString("opt_type") }
            .trim()
        if (optionType.isBlank()) return null

        val strikeValue = when {
            src.has("strike") && !src.isNull("strike") -> src.opt("strike")
            src.has("strike_price") && !src.isNull("strike_price") -> src.opt("strike_price")
            else -> null
        } ?: return null

        val ltpValue = when {
            src.has("ltp") && !src.isNull("ltp") -> src.optDouble("ltp", Double.NaN)
            src.has("last_price") && !src.isNull("last_price") -> src.optDouble("last_price", Double.NaN)
            else -> Double.NaN
        }
        if (ltpValue.isNaN()) return null

        return JSONObject().apply {
            put("index_key", indexKey)
            put("strike", strikeValue)
            put("option_type", optionType)
            put("expiry", expiry)
            put("poll_ts", pollTs)
            put("ltp", ltpValue)
            if (src.has("session_date") && !src.isNull("session_date")) put("session_date", src.opt("session_date"))
        }
    }

    private fun buildEvaluationRows(body: JSONArray): JSONArray {
        val nowIso = java.time.Instant.now().toString()
        val rows = JSONArray()
        for (i in 0 until body.length()) {
            val src = body.optJSONObject(i) ?: continue
            val row = JSONObject()
            row.put("snapshot_id", src.opt("snapshot_id"))
            row.put("session_date", src.opt("session_date"))
            row.put("candidate_id", src.opt("candidate_id"))
            row.put("lane", src.opt("lane"))
            row.put("index_key", src.opt("index_key"))
            row.put("trade_mode", src.opt("trade_mode"))
            row.put("strategy_type", src.opt("strategy_type"))
            row.put("role", src.optString("role", "secondary"))
            row.put("sim_pnl_h2", src.opt("sim_pnl_h2"))
            if (!src.isNull("outcome_h2")) row.put("outcome_h2", src.opt("outcome_h2"))
            if (!src.isNull("canonical_won")) row.put("canonical_won", src.opt("canonical_won"))
            shadowTeacherKeys.forEach { key ->
                if (!src.isNull(key)) row.put(key, src.opt(key))
            }
            row.put("created_at", nowIso)
            rows.put(row)
        }
        return rows
    }

    private fun buildRecommendationRows(sessionDate: String, body: JSONArray): JSONArray {
        val nowIso = java.time.Instant.now().toString()
        val rows = JSONArray()
        for (i in 0 until body.length()) {
            val src = body.optJSONObject(i) ?: continue
            val role = src.optString("role", "secondary").ifBlank { "secondary" }.lowercase(Locale.US)
            val row = JSONObject()
            row.put("snapshot_id", src.opt("snapshot_id"))
            row.put("session_date", sessionDate)
            row.put("candidate_id", src.opt("candidate_id"))
            row.put("role", role)
            row.put("lane", src.opt("lane"))
            row.put("index_key", src.opt("index_key"))
            row.put("trade_mode", src.opt("trade_mode"))
            row.put("strategy_type", src.opt("strategy_type"))
            row.put("sim_pnl_h2", src.opt("sim_pnl_h2"))
            if (!src.isNull("outcome_h2")) row.put("outcome_h2", src.opt("outcome_h2"))
            if (!src.isNull("canonical_won")) row.put("canonical_won", src.opt("canonical_won"))
            shadowTeacherKeys.forEach { key ->
                if (!src.isNull(key)) row.put(key, src.opt(key))
            }
            row.put("created_at", nowIso)
            rows.put(row)
        }
        return rows
    }

    /**
     * Reads app_config where key = morning_baseline
     */
    fun getBaseline(): JSONObject? {
        val request = getBaseRequest("app_config?key=eq.morning_baseline&select=value")
            .get()
            .build()
        val json = fetchSync(request) ?: return null
        return try {
            val array = JSONArray(json)
            if (array.length() > 0) array.getJSONObject(0).optJSONObject("value") else null
        } catch (e: Exception) {
            Log.e(TAG, "Error parsing baseline: ${e.message}")
            null
        }
    }

    /**
     * Reads trades_v2 where status = OPEN
     */
    fun getOpenTrades(): JSONArray {
        val request = getBaseRequest("trades_v2?status=eq.OPEN&select=*&order=created_at.desc")
            .get()
            .build()
        val json = fetchSync(request) ?: return JSONArray()
        return try {
            JSONArray(json)
        } catch (e: Exception) {
            Log.e(TAG, "Error parsing open trades: ${e.message}")
            JSONArray()
        }
    }

    /**
     * Reads trades_v2 where status = CLOSED, limit 200 (SC1: increased from 20 for ML calibration)
     */
    fun getClosedTrades(): JSONArray {
        val request = getBaseRequest("trades_v2?status=eq.CLOSED&select=*&order=exit_date.desc&limit=200")
            .get()
            .build()
        val json = fetchSync(request) ?: return JSONArray()
        return try {
            JSONArray(json)
        } catch (e: Exception) {
            Log.e(TAG, "Error parsing closed trades: ${e.message}")
            JSONArray()
        }
    }

    /**
     * Reads app_config where key = poll_history_YYYY-MM-DD
     */
    fun getPollHistory(date: String): JSONArray {
        val request = getBaseRequest("app_config?key=eq.poll_history_$date&select=value")
            .get()
            .build()
        val json = fetchSync(request) ?: return JSONArray()
        return try {
            val array = JSONArray(json)
            if (array.length() > 0) array.getJSONObject(0).optJSONArray("value") ?: JSONArray() else JSONArray()
        } catch (e: Exception) {
            Log.e(TAG, "Error parsing poll history: ${e.message}")
            JSONArray()
        }
    }

    /**
     * Reads premium_history, order by date desc, limit 60
     */
    fun getPremiumHistory(): JSONArray {
        val request = getBaseRequest("premium_history?select=*&order=date.desc&limit=60")
            .get()
            .build()
        val json = fetchSync(request) ?: return JSONArray()
        return try {
            JSONArray(json)
        } catch (e: Exception) {
            Log.e(TAG, "Error parsing premium history: ${e.message}")
            JSONArray()
        }
    }
    
    /**
     * Reads yesterday's signal from chain_snapshots
     */
    fun getYesterdaySignal(date: String): JSONObject? {
        val request = getBaseRequest("chain_snapshots?date=eq.$date&session=eq.315pm&select=tomorrow_signal,signal_strength")
            .get()
            .build()
        val json = fetchSync(request) ?: return null
        return try {
            val array = JSONArray(json)
            if (array.length() > 0) array.getJSONObject(0) else null
        } catch (e: Exception) {
            Log.e(TAG, "Error parsing yesterday signal: ${e.message}")
            null
        }
    }

    /**
     * Saves a brain snapshot to ml_brain_snapshots (ML Arch V2).
     * Requires ALTER TABLE ml_brain_snapshots ADD COLUMN top_candidates_json JSONB;
     */
    fun saveBrainSnapshot(body: JSONObject): Boolean {
        val tables = listOf("ml_brain_snapshots", "ml_poll_sequences")
        val fullResult = postToFirstWorkingTableDetailed(tables, body.toString())
        if (fullResult.success) return true

        // Older schemas may not yet have the expanded context columns. Keep
        // capture alive with the original minimum payload rather than dropping
        // the whole poll snapshot.
        val minimal = JSONObject()
        listOf(
            "poll_ts",
            "session_date",
            "recommendation_id",
            "action",
            "strategy",
            "confidence",
            "primary_candidate_json",
            "top_candidates_json",
            "context_json",
            "verdict_json",
            "market_forces_json",
            "poll_summary_json",
            "is_labelable"
        ).forEach { key ->
            if (body.has(key)) minimal.put(key, body.get(key))
        }
        val minimalResult = postToFirstWorkingTableDetailed(tables, minimal.toString())
        if (!minimalResult.success) {
            val details = buildString {
                append("ML_BRAIN_SNAPSHOT_SAVE_FAIL: fullTable=")
                append(fullResult.table ?: "unknown")
                append(" fullCode=")
                append(fullResult.code ?: -1)
                append(" fullMessage=")
                append(fullResult.message ?: fullResult.exceptionMessage ?: "")
                if (!fullResult.errorBody.isNullOrBlank()) {
                    append(" fullBody=")
                    append(fullResult.errorBody.take(800))
                }
                append(" minimalTable=")
                append(minimalResult.table ?: "unknown")
                append(" minimalCode=")
                append(minimalResult.code ?: -1)
                append(" minimalMessage=")
                append(minimalResult.message ?: minimalResult.exceptionMessage ?: "")
                if (!minimalResult.errorBody.isNullOrBlank()) {
                    append(" minimalBody=")
                    append(minimalResult.errorBody.take(800))
                }
                append(" payloadBytes=")
                append(body.toString().toByteArray().size)
            }
            LogBuffer.add('E', TAG, details)
            Log.e(TAG, details)
        }
        return minimalResult.success
    }

    /**
     * Best-effort compact candidate persistence for offline ML evaluation breadth.
     *
     * The table may not exist yet in every environment; fail closed without
     * affecting the live poll path.
     */
    fun saveGeneratedCandidates(rows: JSONArray): Boolean {
        if (rows.length() == 0) return true
        val body = rows.toString()
        val result = postToFirstWorkingTableDetailed(listOf("ml_generated_candidates"), body)
        if (!result.success) {
            val details = buildString {
                append("ML_GENERATED_CANDIDATES_HTTP: table=")
                append(result.table ?: "ml_generated_candidates")
                append(" code=")
                append(result.code ?: -1)
                append(" message=")
                append(result.message ?: "")
                if (!result.errorBody.isNullOrBlank()) {
                    append(" body=")
                    append(result.errorBody.take(800))
                }
                if (!result.exceptionMessage.isNullOrBlank()) {
                    append(" exception=")
                    append(result.exceptionMessage)
                }
                append(" payloadBytes=")
                append(body.toByteArray().size)
                append(" rows=")
                append(rows.length())
            }
            LogBuffer.add('E', TAG, details)
            Log.e(TAG, details)
        }
        return result.success
    }

    fun fetchBrainSnapshots(date: String): JSONArray {
        val exact = fetchArrayFromTables(
            listOf(
                "ml_brain_snapshots?session_date=eq.$date&order=poll_ts.desc",
                "ml_poll_sequences?session_date=eq.$date&order=poll_ts.desc"
            )
        )
        if (exact.length() > 0) return exact

        // Recovery path: older or partially migrated rows may have a valid poll_ts
        // but a null/missing session_date, which makes the exact server-side filter
        // look empty and causes day evaluation to skip.
        val recent = fetchArrayFromTables(
            listOf(
                "ml_brain_snapshots?select=*&order=poll_ts.desc&limit=500",
                "ml_poll_sequences?select=*&order=poll_ts.desc&limit=500"
            )
        )
        return filterRowsByIstSessionDate(recent, date)
    }

    fun fetchEvaluationSnapshots(date: String): JSONArray {
        val select = "id,poll_ts,primary_candidate_json,context_json,top_candidates_json,is_labelable,session_date"
        val exact = fetchArrayFromTables(
            listOf(
                "ml_brain_snapshots?session_date=eq.$date&select=$select&order=poll_ts.desc",
                "ml_poll_sequences?session_date=eq.$date&select=$select&order=poll_ts.desc"
            )
        )
        if (exact.length() > 0) return exact

        val recent = fetchArrayFromTables(
            listOf(
                "ml_brain_snapshots?select=$select&order=poll_ts.desc&limit=500",
                "ml_poll_sequences?select=$select&order=poll_ts.desc&limit=500"
            )
        )
        return filterRowsByIstSessionDate(recent, date)
    }

    fun fetchChainSlices(date: String): JSONArray {
        val exact = fetchArrayFromTables(
            listOf(
                "ml_option_chain_snapshots?session_date=eq.$date&order=poll_ts.desc",
                "chain_slices?session_date=eq.$date&order=poll_ts.desc",
                "chain_snapshots?date=eq.$date&order=created_at.desc"
            )
        )
        if (exact.length() > 0) return exact

        val recent = fetchArrayFromTables(
            listOf(
                "ml_option_chain_snapshots?select=*&order=poll_ts.desc&limit=3000",
                "chain_slices?select=*&order=poll_ts.desc&limit=3000",
                "chain_snapshots?select=*&order=created_at.desc&limit=3000"
            )
        )
        return filterRowsByIstSessionDate(recent, date)
    }

    fun fetchEvaluationChainSlices(date: String): JSONArray {
        return fetchEvaluationChainCandles(date).rows
    }

    fun saveChainSlice(body: JSONObject): Boolean {
        return postToFirstWorkingTable(
            listOf("ml_option_chain_snapshots", "chain_slices", "chain_snapshots"),
            body.toString()
        )
    }

    fun fetchEvaluationChainCandles(date: String): ChainFeedResult {
        val exactPreferred = fetchPagedArray(
            "ml_option_chain_snapshots?session_date=eq.$date&order=poll_ts.asc"
        )
        if (exactPreferred.length() > 0) {
            return ChainFeedResult("ml_option_chain_snapshots.exact", normalizeChainRows(exactPreferred))
        }

        val exactFallback = fetchPagedArray(
            "chain_slices?session_date=eq.$date&order=poll_ts.asc"
        )
        if (exactFallback.length() > 0) {
            return ChainFeedResult("chain_slices.exact", normalizeChainRows(exactFallback))
        }

        val recentPreferred = filterRowsByIstSessionDate(
            fetchPagedArray("ml_option_chain_snapshots?order=poll_ts.asc"),
            date
        )
        if (recentPreferred.length() > 0) {
            return ChainFeedResult("ml_option_chain_snapshots.filtered_recent", normalizeChainRows(recentPreferred))
        }

        val recentFallback = filterRowsByIstSessionDate(
            fetchPagedArray("chain_slices?order=poll_ts.asc"),
            date
        )
        return ChainFeedResult("chain_slices.filtered_recent", normalizeChainRows(recentFallback))
    }

    fun writeEvaluationChainCandlesForLegs(
        date: String,
        legKeys: List<EvaluationLegKey>,
        outputFile: File,
        onPage: ((source: String, pages: Int, rows: Int) -> Unit)? = null
    ): ChainStreamResult {
        outputFile.parentFile?.mkdirs()
        outputFile.delete()
        if (legKeys.isEmpty()) {
            outputFile.writeText("[]")
            return ChainStreamResult("no_candidate_legs", 0, 0)
        }

        val maxPages = 200
        val sources = listOf(
            ChainSource(
                "ml_option_chain_snapshots?session_date=eq.$date&order=poll_ts.asc",
                "ml_option_chain_snapshots.exact.filtered_stream",
                null
            ),
            ChainSource(
                "chain_slices?session_date=eq.$date&order=poll_ts.asc",
                "chain_slices.exact.filtered_stream",
                null
            ),
            ChainSource(
                "ml_option_chain_snapshots?order=poll_ts.asc",
                "ml_option_chain_snapshots.filtered_recent.filtered_stream",
                date
            ),
            ChainSource(
                "chain_slices?order=poll_ts.asc",
                "chain_slices.filtered_recent.filtered_stream",
                date
            )
        )

        val tmpFile = File(outputFile.parentFile, "${outputFile.name}.tmp")
        var lastResult = ChainStreamResult("no_chain_source", 0, 0)
        for (candidateSource in sources) {
            tmpFile.delete()
            val result = writePagedFilteredChain(
                basePath = candidateSource.path,
                source = candidateSource.source,
                legKeys = legKeys,
                outputFile = tmpFile,
                onPage = onPage,
                istSessionDate = candidateSource.filterDate,
                maxPages = maxPages
            )
            lastResult = result
            if (result.capped || result.rowCount > 0) {
                replaceFile(tmpFile, outputFile)
                return result
            }
            tmpFile.delete()
        }

        outputFile.writeText("[]")
        return lastResult
    }

    private fun replaceFile(source: File, target: File) {
        target.delete()
        if (!source.renameTo(target)) {
            source.copyTo(target, overwrite = true)
            source.delete()
        }
    }

    private fun writePagedFilteredChain(
        basePath: String,
        source: String,
        legKeys: List<EvaluationLegKey>,
        outputFile: File,
        onPage: ((source: String, pages: Int, rows: Int) -> Unit)? = null,
        istSessionDate: String? = null,
        pageSize: Int = CHAIN_PAGE_SIZE,
        maxPages: Int = 200
    ): ChainStreamResult {
        outputFile.parentFile?.mkdirs()
        var offset = 0
        var pages = 0
        var rows = 0
        var reachedEnd = false
        var first = true

        outputFile.bufferedWriter().use { writer ->
            writer.write("[")
            while (pages < maxPages) {
                val separator = if (basePath.contains("?")) "&" else "?"
                val page = fetchArray("$basePath${separator}limit=$pageSize&offset=$offset") ?: break
                if (page.length() == 0) {
                    reachedEnd = true
                    break
                }
                pages += 1
                for (i in 0 until page.length()) {
                    val raw = page.optJSONObject(i) ?: continue
                    if (istSessionDate != null && !rowBelongsToIstSessionDate(raw, istSessionDate)) continue
                    val normalized = normalizedChainRow(raw) ?: continue
                    if (!matchesEvaluationLeg(normalized, legKeys)) continue
                    if (!first) writer.write(",")
                    writer.write(normalized.toString())
                    first = false
                    rows += 1
                }
                writer.flush()
                onPage?.invoke(source, pages, rows)
                if (page.length() < pageSize) {
                    reachedEnd = true
                    break
                }
                offset += page.length()
            }
            writer.write("]")
        }

        return ChainStreamResult(
            source = source,
            rowCount = rows,
            pageCount = pages,
            capped = !reachedEnd && pages >= maxPages
        )
    }

    private fun matchesEvaluationLeg(row: JSONObject, legKeys: List<EvaluationLegKey>): Boolean {
        val indexKey = row.optString("index_key").trim()
        val expiry = row.optString("expiry").trim()
        val optionType = normalizeOptionType(row.optString("option_type"))
        val strike = row.optDouble("strike", Double.NaN)
        if (indexKey.isBlank() || optionType.isBlank() || strike.isNaN()) return false

        return legKeys.any { key ->
            key.indexKey.equals(indexKey, ignoreCase = true) &&
                (key.expiry.isBlank() || expiry.isBlank() || key.expiry == expiry) &&
                normalizeOptionType(key.optionType) == optionType &&
                kotlin.math.abs(key.strike - strike) < 0.01
        }
    }

    private fun normalizeOptionType(value: String): String {
        return when (value.trim().uppercase(Locale.US)) {
            "CALL", "C" -> "CE"
            "PUT", "P" -> "PE"
            else -> value.trim().uppercase(Locale.US)
        }
    }

    private fun normalizeChainRows(rows: JSONArray): JSONArray {
        val normalized = JSONArray()
        for (i in 0 until rows.length()) {
            val src = rows.optJSONObject(i) ?: continue
            normalizedChainRow(src)?.let(normalized::put)
        }
        return normalized
    }

    fun saveChainRows(rows: JSONArray): Boolean {
        if (rows.length() == 0) return true
        val payload = rows.toString()
        return postToFirstWorkingTable(
            listOf("ml_option_chain_snapshots", "chain_slices"),
            payload
        )
    }

    fun saveEvaluationOutcomes(sessionDate: String, body: JSONArray): EvaluationSaveResult {
        val evaluationRows = buildEvaluationRows(body)
        val recommendationRows = buildRecommendationRows(sessionDate, body)

        fun stripShadowTeacher(rows: JSONArray): JSONArray {
            val legacy = JSONArray()
            for (i in 0 until rows.length()) {
                val src = rows.optJSONObject(i) ?: continue
                val row = JSONObject(src.toString())
                shadowTeacherKeys.forEach { row.remove(it) }
                legacy.put(row)
            }
            return legacy
        }

        fun stripCanonical(rows: JSONArray): JSONArray {
            val legacy = JSONArray()
            for (i in 0 until rows.length()) {
                val src = rows.optJSONObject(i) ?: continue
                val row = JSONObject(src.toString())
                row.remove("canonical_won")
                legacy.put(row)
            }
            return legacy
        }

        fun stripAttribution(rows: JSONArray): JSONArray {
            val legacy = JSONArray()
            for (i in 0 until rows.length()) {
                val src = rows.optJSONObject(i) ?: continue
                val row = JSONObject(src.toString())
                row.remove("session_date")
                row.remove("lane")
                row.remove("index_key")
                row.remove("trade_mode")
                row.remove("strategy_type")
                legacy.put(row)
            }
            return legacy
        }

        val evaluationRowsNoShadow = stripShadowTeacher(evaluationRows)
        val recommendationRowsNoShadow = stripShadowTeacher(recommendationRows)
        val evaluationRowsLegacy = stripCanonical(stripAttribution(evaluationRowsNoShadow))
        val recommendationRowsLegacy = stripCanonical(stripAttribution(recommendationRowsNoShadow))

        val evaluationWriteAttempted = postArrayToTableChunked("ml_evaluation_outcomes", evaluationRows) ||
            postArrayToTableChunked("ml_evaluation_outcomes", evaluationRowsNoShadow) ||
            postArrayToTableChunked("ml_evaluation_outcomes", stripCanonical(evaluationRowsNoShadow)) ||
            postArrayToTableChunked("ml_evaluation_outcomes", evaluationRowsLegacy)
        val recommendationWriteAttempted = postArrayToTableChunked("ml_recommendation_outcomes", recommendationRows) ||
            postArrayToTableChunked("ml_recommendation_outcomes", recommendationRowsNoShadow) ||
            postArrayToTableChunked("ml_recommendation_outcomes", stripCanonical(recommendationRowsNoShadow)) ||
            postArrayToTableChunked("ml_recommendation_outcomes", recommendationRowsLegacy)

        val evaluationPersisted = countRows("ml_evaluation_outcomes", "session_date=eq.$sessionDate")
        val recommendationPersisted = countRows("ml_recommendation_outcomes", "session_date=eq.$sessionDate")
        val evaluationSaved = evaluationPersisted >= evaluationRows.length()
        val recommendationSaved = recommendationPersisted > 0 && recommendationWriteAttempted
        val success = evaluationSaved
        val message = when {
            evaluationSaved && recommendationSaved ->
                "Persisted $evaluationPersisted evaluation rows; $recommendationPersisted recommendation rows persisted separately."
            evaluationSaved ->
                "Persisted $evaluationPersisted evaluation rows to Supabase; recommendation rows were not fully verified."
            recommendationSaved ->
                "Persisted $recommendationPersisted recommendation rows to Supabase; evaluation rows were not saved."
            else -> "Supabase persistence failed for evaluation outcomes."
        }

        return EvaluationSaveResult(
            success = success,
            producedCount = evaluationRows.length(),
            persistedCount = evaluationPersisted,
            primaryPersistedCount = recommendationPersisted,
            evaluationPersistedCount = evaluationPersisted,
            message = message
        )
    }

    fun fetchRecentEvaluationOutcomes(limit: Int = 1000): JSONArray {
        val evaluationRows = select("ml_evaluation_outcomes", null, "created_at.desc", limit)
        if (evaluationRows.length() > 0) return evaluationRows
        return select("ml_decisions", null, "created_at.desc", limit)
    }

    private fun fetchAttributedRecommendationOutcomes(sessionDate: String, limit: Int = 1000): JSONArray {
        val filter = "session_date=eq.$sessionDate"
        val rows = select("ml_recommendation_outcomes", filter, "created_at.desc", limit)
        if (rows.length() > 0) {
            var hasNonPrimary = false
            for (i in 0 until rows.length()) {
                val role = rows.optJSONObject(i)?.optString("role", "primary") ?: "primary"
                if (!role.equals("primary", ignoreCase = true)) {
                    hasNonPrimary = true
                    break
                }
            }
            if (hasNonPrimary) return rows
        }
        val evalRows = select("ml_evaluation_outcomes", filter, "created_at.desc", limit)
        if (evalRows.length() > 0) return evalRows
        return JSONArray()
    }

    fun fetchEvaluationLaneSummary(sessionDate: String, limit: Int = 1000): JSONObject {
        val attributedRecommendationRows = fetchAttributedRecommendationOutcomes(sessionDate, limit)
        val rows = if (attributedRecommendationRows.length() > 0) {
            attributedRecommendationRows
        } else {
            fetchRecentEvaluationOutcomes(limit)
        }
        val todayRows = if (attributedRecommendationRows.length() > 0) {
            attributedRecommendationRows
        } else {
            filterRowsByIstSessionDate(rows, sessionDate)
        }
        val lanes = linkedMapOf(
            "NF_intraday" to intArrayOf(0, 0, 0),
            "NF_swing" to intArrayOf(0, 0, 0),
            "BNF_intraday" to intArrayOf(0, 0, 0),
            "BNF_swing" to intArrayOf(0, 0, 0)
        )
        val primaryLegacyLanes = linkedMapOf(
            "NF_intraday" to intArrayOf(0, 0, 0),
            "NF_swing" to intArrayOf(0, 0, 0),
            "BNF_intraday" to intArrayOf(0, 0, 0),
            "BNF_swing" to intArrayOf(0, 0, 0)
        )
        val teacherLaneAggregates = linkedMapOf(
            "NF_intraday" to mutableListOf<JSONObject>(),
            "NF_swing" to mutableListOf<JSONObject>(),
            "BNF_intraday" to mutableListOf<JSONObject>(),
            "BNF_swing" to mutableListOf<JSONObject>()
        )
        val teacherBucketAggregates = linkedMapOf<String, MutableList<JSONObject>>()
        val recommendationMixLanes = linkedMapOf(
            "NF_intraday" to intArrayOf(0, 0, 0),
            "NF_swing" to intArrayOf(0, 0, 0),
            "BNF_intraday" to intArrayOf(0, 0, 0),
            "BNF_swing" to intArrayOf(0, 0, 0)
        )

        fun normalizeWon(value: Any?): Int? = when (value) {
            is Boolean -> if (value) 1 else 0
            is Number -> {
                val n = value.toInt()
                if (n == 0 || n == 1) n else null
            }
            is String -> when (value.trim().lowercase(Locale.US)) {
                "1", "true", "yes" -> 1
                "0", "false", "no" -> 0
                else -> null
            }
            else -> null
        }

        fun normalizeBool(value: Any?): Boolean? = when (value) {
            is Boolean -> value
            is Number -> when (value.toInt()) {
                1 -> true
                0 -> false
                else -> null
            }
            is String -> when (value.trim().lowercase(Locale.US)) {
                "1", "true", "yes" -> true
                "0", "false", "no" -> false
                else -> null
            }
            else -> null
        }

        fun normalizeDouble(value: Any?): Double? = when (value) {
            is Number -> value.toDouble()
            is String -> value.toDoubleOrNull()
            else -> null
        }

        fun buildTeacherSummary(items: List<JSONObject>): JSONObject {
            var rowCount = 0
            var successCount = 0
            var sumR = 0.0
            var sumCaptured = 0.0
            var capturedCount = 0
            val winRs = mutableListOf<Double>()
            val lossRs = mutableListOf<Double>()
            for (row in items) {
                val r = normalizeDouble(row.opt("r_multiple")) ?: continue
                rowCount += 1
                sumR += r
                if (r > 0) winRs += r
                if (r < 0) lossRs += kotlin.math.abs(r)
                val success = normalizeBool(row.opt("is_success"))
                if (success == true) successCount += 1
                val captured = normalizeDouble(row.opt("captured_pct"))
                if (captured != null) {
                    sumCaptured += captured
                    capturedCount += 1
                }
            }
            val expectancyR = if (rowCount > 0) sumR / rowCount else 0.0
            val successRatePct = if (rowCount > 0) (successCount * 100.0) / rowCount else 0.0
            val avgWinR = if (winRs.isNotEmpty()) winRs.average() else 0.0
            val avgLossR = if (lossRs.isNotEmpty()) lossRs.average() else 0.0
            val breakEvenWinRatePct = if (avgWinR > 0.0 && avgLossR > 0.0) {
                (avgLossR / (avgLossR + avgWinR)) * 100.0
            } else {
                0.0
            }
            val avgCapturedPct = if (capturedCount > 0) (sumCaptured / capturedCount) * 100.0 else 0.0
            val worthTrading = rowCount >= 30 && expectancyR > 0.0 && successRatePct > breakEvenWinRatePct
            return JSONObject()
                .put("rows", rowCount)
                .put("successes", successCount)
                .put("successRatePct", String.format(Locale.US, "%.2f", successRatePct).toDouble())
                .put("expectancyR", String.format(Locale.US, "%.4f", expectancyR).toDouble())
                .put("avgCapturedPct", String.format(Locale.US, "%.2f", avgCapturedPct).toDouble())
                .put("breakEvenWinRatePct", String.format(Locale.US, "%.2f", breakEvenWinRatePct).toDouble())
                .put("worthTrading", worthTrading)
        }

        var attributedRows = 0
        var teacherRows = 0
        var primaryLegacyRows = 0
        var primaryLegacyLabeled = 0
        var primaryLegacyWins = 0
        for (i in 0 until todayRows.length()) {
            val row = todayRows.optJSONObject(i) ?: continue
            val lane = row.optString("lane", "").trim()
            val bucket = lanes[lane] ?: continue
            bucket[0] += 1 // rows
            attributedRows += 1
            val role = row.optString("role", "").trim().lowercase(Locale.US)
            val mixBucket = recommendationMixLanes[lane]
            if (mixBucket != null) {
                mixBucket[0] += 1
                when (role) {
                    "primary" -> mixBucket[1] += 1
                    "secondary" -> mixBucket[2] += 1
                }
            }
            val isPrimaryLike = role != "secondary"
            val won = normalizeWon(
                when {
                    !row.isNull("canonical_won") -> row.opt("canonical_won")
                    !row.isNull("outcome_h2") -> row.opt("outcome_h2")
                    !row.isNull("won") -> row.opt("won")
                    else -> null
                }
            )
            if (won == 0 || won == 1) {
                bucket[1] += 1 // labeled
                if (won == 1) bucket[2] += 1 // wins
            }
            if (isPrimaryLike) {
                val legacyBucket = primaryLegacyLanes[lane]
                if (legacyBucket != null) {
                    legacyBucket[0] += 1
                    primaryLegacyRows += 1
                    if (won == 0 || won == 1) {
                        legacyBucket[1] += 1
                        primaryLegacyLabeled += 1
                        if (won == 1) {
                            legacyBucket[2] += 1
                            primaryLegacyWins += 1
                        }
                    }
                }
            }

            val labelVersion = row.optString("label_version", "").trim()
            val rMultiple = normalizeDouble(row.opt("r_multiple"))
            val isPrimary = role == "primary"
            if (labelVersion == TeacherTruthConfig.LABEL_VERSION && rMultiple != null && isPrimary) {
                teacherRows += 1
                teacherLaneAggregates[lane]?.add(row)
                val strategyType = row.optString("strategy_type", "unknown").ifBlank { "unknown" }
                val regimeBucket = row.optString("regime_bucket", "unknown").ifBlank { "unknown" }
                val bucketKey = "$lane|$strategyType|$regimeBucket"
                teacherBucketAggregates.getOrPut(bucketKey) { mutableListOf() }.add(row)
            }
        }

        val lanesJson = JSONObject()
        for ((key, counts) in lanes) {
            lanesJson.put(
                key,
                JSONObject()
                    .put("rows", counts[0])
                    .put("labeled", counts[1])
                    .put("wins", counts[2])
            )
        }

        val primaryLegacyJson = JSONObject()
        for ((key, counts) in primaryLegacyLanes) {
            val winRatePct = if (counts[1] > 0) {
                String.format(Locale.US, "%.2f", (counts[2] * 100.0) / counts[1]).toDouble()
            } else {
                0.0
            }
            primaryLegacyJson.put(
                key,
                JSONObject()
                    .put("rows", counts[0])
                    .put("labeled", counts[1])
                    .put("wins", counts[2])
                    .put("winRatePct", winRatePct)
            )
        }

        val teacherLanesJson = JSONObject()
        for ((key, items) in teacherLaneAggregates) {
            teacherLanesJson.put(key, buildTeacherSummary(items))
        }

        val teacherBucketsJson = JSONObject()
        var tradeableBucketCount = 0
        for ((key, items) in teacherBucketAggregates) {
            val bucketSummary = buildTeacherSummary(items)
            if (bucketSummary.optBoolean("worthTrading", false)) tradeableBucketCount += 1
            teacherBucketsJson.put(key, bucketSummary)
        }

        val teacherSummary = buildTeacherSummary(
            teacherLaneAggregates.values.flatten()
        )
            .put("labelVersion", TeacherTruthConfig.LABEL_VERSION)
            .put("bucketCount", teacherBucketAggregates.size)
            .put("tradeableBucketCount", tradeableBucketCount)
            .put("scope", "primary_only_shadow")

        val comparisonLanesJson = JSONObject()
        for ((key, legacy) in primaryLegacyLanes) {
            val teacherLane = teacherLanesJson.optJSONObject(key) ?: JSONObject()
            val legacyWinRatePct = if (legacy[1] > 0) {
                String.format(Locale.US, "%.2f", (legacy[2] * 100.0) / legacy[1]).toDouble()
            } else {
                0.0
            }
            val teacherSuccessRatePct = teacherLane.optDouble("successRatePct", 0.0)
            comparisonLanesJson.put(
                key,
                JSONObject()
                    .put("legacyRows", legacy[0])
                    .put("legacyLabeled", legacy[1])
                    .put("legacyWins", legacy[2])
                    .put("legacyWinRatePct", legacyWinRatePct)
                    .put("teacherRows", teacherLane.optInt("rows", 0))
                    .put("teacherSuccesses", teacherLane.optInt("successes", 0))
                    .put("teacherSuccessRatePct", teacherSuccessRatePct)
                    .put("teacherExpectancyR", teacherLane.optDouble("expectancyR", 0.0))
                    .put("teacherBreakEvenWinRatePct", teacherLane.optDouble("breakEvenWinRatePct", 0.0))
                    .put("teacherWorthTrading", teacherLane.optBoolean("worthTrading", false))
                    .put(
                        "winRateDeltaPts",
                        String.format(Locale.US, "%.2f", teacherSuccessRatePct - legacyWinRatePct).toDouble()
                    )
            )
        }

        val primaryLegacyWinRatePct = if (primaryLegacyLabeled > 0) {
            String.format(Locale.US, "%.2f", (primaryLegacyWins * 100.0) / primaryLegacyLabeled).toDouble()
        } else {
            0.0
        }
        val comparisonSummaryJson = JSONObject()
            .put("legacyPrimaryRows", primaryLegacyRows)
            .put("legacyPrimaryLabeled", primaryLegacyLabeled)
            .put("legacyPrimaryWins", primaryLegacyWins)
            .put("legacyWinRatePct", primaryLegacyWinRatePct)
            .put("teacherPrimaryRows", teacherSummary.optInt("rows", 0))
            .put("teacherSuccesses", teacherSummary.optInt("successes", 0))
            .put("teacherSuccessRatePct", teacherSummary.optDouble("successRatePct", 0.0))
            .put("teacherExpectancyR", teacherSummary.optDouble("expectancyR", 0.0))
            .put("teacherBreakEvenWinRatePct", teacherSummary.optDouble("breakEvenWinRatePct", 0.0))
            .put("teacherWorthTrading", teacherSummary.optBoolean("worthTrading", false))
            .put(
                "winRateDeltaPts",
                String.format(
                    Locale.US,
                    "%.2f",
                    teacherSummary.optDouble("successRatePct", 0.0) - primaryLegacyWinRatePct
                ).toDouble()
            )
            .put("scope", "primary_only_old_vs_teacher_shadow")

        val recommendationMixJson = JSONObject()
        for ((key, counts) in recommendationMixLanes) {
            recommendationMixJson.put(
                key,
                JSONObject()
                    .put("rows", counts[0])
                    .put("primaryRows", counts[1])
                    .put("secondaryRows", counts[2])
            )
        }

        return JSONObject()
            .put("session_date", sessionDate)
            .put("rowsFetched", rows.length())
            .put("rowsToday", todayRows.length())
            .put("attributedRows", attributedRows)
            .put("primary_legacy_lanes", primaryLegacyJson)
            .put("recommendation_mix_lanes", recommendationMixJson)
            .put("teacherRows", teacherRows)
            .put("lanes", lanesJson)
            .put("teacher_lanes", teacherLanesJson)
            .put("teacher_summary", teacherSummary)
            .put("teacher_buckets", teacherBucketsJson)
            .put("comparison_lanes", comparisonLanesJson)
            .put("comparison_summary", comparisonSummaryJson)
    }

    fun fetchRecentBrainSnapshots(limit: Int = 200): JSONArray {
        return fetchArrayFromTables(
            listOf(
                "ml_brain_snapshots?select=*&order=poll_ts.desc&limit=$limit",
                "ml_poll_sequences?select=*&order=poll_ts.desc&limit=$limit"
            )
        )
    }

    fun fetchNf50ConstituentRows(): JSONArray {
        val tableRows = fetchArrayFromTables(
            listOf(
                "config_nf50_constituents?select=*&active=eq.true&order=effective_date.desc,symbol.asc",
                "nf50_constituents?select=*&active=eq.true&order=effective_date.desc,symbol.asc"
            )
        )
        if (tableRows.length() > 0) return tableRows

        return try {
            val appConfig = select("app_config", "key=eq.nf50_constituents")
            if (appConfig.length() <= 0) return JSONArray()
            val value = appConfig.optJSONObject(0)?.opt("value")
            when (value) {
                is JSONArray -> value
                is String -> JSONArray(value)
                else -> JSONArray()
            }
        } catch (e: Exception) {
            Log.e(TAG, "fetchNf50ConstituentRows failed: ${e.message}")
            JSONArray()
        }
    }

    fun saveChainSnapshot(session: String, data: JSONObject): Boolean {
        // SC4: Standardization - snapshots use IST date to match trading days
        val ist = java.util.TimeZone.getTimeZone("Asia/Kolkata")
        val today = java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.US).apply {
            timeZone = ist
        }.format(java.util.Date())
        val body = JSONObject()
        body.put("date", today)
        body.put("session", session)
        body.put("data", data)

        // Do not rely on PostgREST upsert semantics here: older schemas did not
        // always have a unique (date, session) constraint, so a plain POST can
        // create duplicate 2pm/315pm snapshots. Patch the existing row first.
        val existing = select("chain_snapshots", "date=eq.$today&session=eq.$session", null, 1)
        if (existing.length() > 0 && update("chain_snapshots", body, "date=eq.$today&session=eq.$session")) {
            return true
        }

        val request = getBaseRequest("chain_snapshots")
            .post(body.toString().toRequestBody("application/json".toMediaTypeOrNull()))
            .build()

        return try {
            client.newCall(request).execute().use { it.isSuccessful }
        } catch (e: Exception) {
            Log.e(TAG, "Save chain snapshot failed: ${e.message}")
            false
        }
    }

    /**
     * Upserts poll history for a specific date to app_config
     */
    fun upsertPollHistory(date: String, history: JSONArray): Boolean {
        val body = JSONObject()
        body.put("key", "poll_history_$date")
        body.put("value", history)
        body.put("updated_at", java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", java.util.Locale.US).apply {
            timeZone = java.util.TimeZone.getTimeZone("UTC")
        }.format(java.util.Date()))

        val request = getBaseRequest("app_config")
            .header("Prefer", "resolution=merge-duplicates")
            .post(body.toString().toRequestBody("application/json".toMediaTypeOrNull()))
            .build()
        
        return try {
            client.newCall(request).execute().use { it.isSuccessful }
        } catch (e: Exception) {
            Log.e(TAG, "Upsert poll history failed: ${e.message}")
            false
        }
    }

    // --- Generic REST Methods ---

    fun upsert(table: String, body: JSONObject, onConflict: String? = null): Boolean {
        val path = if (onConflict != null) "$table?on_conflict=$onConflict" else table
        val request = getBaseRequest(path)
            .header("Prefer", "resolution=merge-duplicates")
            .post(body.toString().toRequestBody("application/json".toMediaTypeOrNull()))
            .build()
        
        return try {
            client.newCall(request).execute().use { it.isSuccessful }
        } catch (e: Exception) {
            Log.e(TAG, "Upsert to $table failed: ${e.message}")
            false
        }
    }

    fun update(table: String, body: JSONObject, filter: String): Boolean {
        // SC3: Use return=representation and check for empty array to detect 0 rows affected
        val request = getBaseRequest("$table?$filter")
            .header("Prefer", "return=representation")
            .patch(body.toString().toRequestBody("application/json".toMediaTypeOrNull()))
            .build()
        
        return try {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return@use false
                val respBody = response.body?.string() ?: "[]"
                // If representation is [], then 0 rows affected
                respBody.trim().length > 2
            }
        } catch (e: Exception) {
            Log.e(TAG, "Update to $table failed: ${e.message}")
            false
        }
    }

    fun select(table: String, filter: String? = null, order: String? = null, limit: Int? = null): JSONArray {
        val queryParams = mutableListOf<String>()
        if (filter != null) queryParams.add(filter)
        if (order != null) queryParams.add("order=$order")
        if (limit != null) queryParams.add("limit=$limit")
        
        val url = if (queryParams.isNotEmpty()) "$table?${queryParams.joinToString("&")}" else table
        val request = getBaseRequest(url).get().build()
        
        val json = fetchSync(request) ?: return JSONArray()
        return try {
            JSONArray(json)
        } catch (e: Exception) {
            Log.e(TAG, "Select from $table failed: ${e.message}")
            JSONArray()
        }
    }

    /**
     * Lightweight app_config fetch used by WebView boot.
     * Excludes large poll_history_* keys to keep startup payload small.
     */
    fun selectAppConfigLite(): JSONArray {
        val request = getBaseRequest(
            "app_config?key=not.like.poll_history_*&select=key,value,updated_at"
        ).get().build()
        val json = fetchSync(request) ?: return JSONArray()
        return try {
            JSONArray(json)
        } catch (e: Exception) {
            Log.e(TAG, "Select app_config lite failed: ${e.message}")
            JSONArray()
        }
    }

    /**
     * Phase B: fetch recent signals for accuracy tracking.
     * Mirrors db.js getRecentSignals(limit).
     */
    fun getRecentSignals(limit: Int = 20): JSONArray {
        val request = getBaseRequest(
            "chain_snapshots?session=eq.315pm" +
            "&select=date,tomorrow_signal,signal_strength,bnf_spot,vix" +
            "&order=date.desc" +
            "&limit=$limit"
        ).get().build()
        val json = fetchSync(request) ?: return JSONArray()
        return try {
            JSONArray(json)
        } catch (e: Exception) {
            Log.e(TAG, "Error parsing recent signals: ${e.message}")
            JSONArray()
        }
    }

    /**
     * Phase B: write yesterday's signal validation result.
     * Mirrors db.js updateSignalResult(date, correct, actualGap) — patches
     * chain_snapshots where date AND session=315pm.
     */
    fun updateSignalResult(date: String, correct: Boolean, actualGap: Double): Boolean {
        val body = JSONObject()
        body.put("signal_correct", correct)
        body.put("signal_actual_gap", actualGap)
        return update("chain_snapshots", body, "date=eq.$date&session=eq.315pm")
    }

    /**
     * Phase B: rolling 30-signal accuracy stats.
     * Mirrors db.js getSignalAccuracyStats() — chain_snapshots filter
     * session=315pm AND signal_correct IS NOT NULL, last 30, computes pct.
     */
    fun getSignalAccuracyStats(): JSONObject {
        val request = getBaseRequest(
            "chain_snapshots?session=eq.315pm" +
            "&signal_correct=not.is.null" +
            "&select=date,tomorrow_signal,signal_strength,signal_correct,signal_actual_gap" +
            "&order=date.desc" +
            "&limit=30"
        ).get().build()
        val result = JSONObject()
        result.put("correct", 0)
        result.put("total", 0)
        result.put("pct", 0)
        result.put("history", JSONArray())
        val json = fetchSync(request) ?: return result
        return try {
            val data = JSONArray(json)
            val total = data.length()
            var correctCount = 0
            for (i in 0 until total) {
                if (data.getJSONObject(i).optBoolean("signal_correct", false)) correctCount++
            }
            result.put("correct", correctCount)
            result.put("total", total)
            result.put("pct", if (total > 0) Math.round(correctCount.toDouble() / total * 100).toInt() else 0)
            result.put("history", data)
            result
        } catch (e: Exception) {
            Log.e(TAG, "Error parsing accuracy stats: ${e.message}")
            result
        }
    }

    fun getSignalReliabilityRows(): JSONArray {
        return select("signal_reliability", null, "lane.asc,signal_name.asc", 200)
    }

    /**
     * Saves candlestick pattern data for a trading date to app_config.
     * Key: candle_data_YYYY-MM-DD. Value: { bnf: {...}, nf: {...} }
     * Used for historical candlestick review.
     */
    fun upsertCandleData(date: String, bnfData: JSONObject, nfData: JSONObject): Boolean {
        val value = JSONObject()
        value.put("bnf", bnfData)
        value.put("nf", nfData)
        val body = JSONObject()
        body.put("key", "candle_data_$date")
        body.put("value", value)
        body.put("updated_at", java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", java.util.Locale.US).apply {
            timeZone = java.util.TimeZone.getTimeZone("UTC")
        }.format(java.util.Date()))
        val request = getBaseRequest("app_config")
            .header("Prefer", "resolution=merge-duplicates")
            .post(body.toString().toRequestBody("application/json".toMediaTypeOrNull()))
            .build()
        return try {
            client.newCall(request).execute().use { it.isSuccessful }
        } catch (e: Exception) {
            Log.e(TAG, "Upsert candle data failed: ${e.message}")
            false
        }
    }
}
