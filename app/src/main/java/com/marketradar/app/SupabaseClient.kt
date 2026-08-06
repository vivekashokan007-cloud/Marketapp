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
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.LinkedHashMap
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
    private const val EVALUATION_SNAPSHOT_PAGE_SIZE = 1
    private const val EVALUATION_CHAIN_EXACT_MAX_PAGES = 200
    private const val EVALUATION_CHAIN_RECENT_FALLBACK_MAX_PAGES = 30

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

    private data class ArrayPostResult(
        val success: Boolean,
        val table: String,
        val code: Int? = null,
        val message: String? = null,
        val errorBody: String? = null,
        val exceptionMessage: String? = null,
        val failedChunkIndex: Int? = null,
        val failedChunkRows: Int = 0,
        val totalChunks: Int = 0
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
        "break_even_win_rate_pct",
        "price_integrity",
        "h2_price_integrity_reason",
        "h2_later_value_points",
        "h2_entry_basis_points",
        "h2_bound_width_points",
        "h2_formula"
    )

    private val rejectedOutcomeColumns = setOf(
        "id", "snapshot_id", "session_date", "poll_ts", "candidate_id", "lane", "index_key",
        "trade_mode", "strategy_type", "role", "sim_pnl_h2", "outcome_h2", "canonical_won",
        "managed_pnl", "managed_gross_pnl", "friction_cost", "exit_reason", "exit_step",
        "exit_ts", "path_points_count", "r_multiple", "captured_pct", "is_success",
        "risk_at_entry", "regime_bucket", "label_version", "teacher_config_version",
        "tp_threshold", "sl_threshold", "break_even_win_rate_pct", "price_integrity",
        "h2_price_integrity_reason", "premium_edge", "credit_width_ratio", "sigma_otm",
        "rejection_stage", "rejection_reason", "gate_name", "gate_field", "observed_value",
        "threshold_value", "margin", "margin_pct", "rejected_rank_in_snapshot",
        "rejected_eval_rank", "rejected_eval_cap", "rejected_eval_source",
        "stage_sample_fraction", "stage_total_rejected", "stage_normalizable",
        "stage_skipped_not_evaluable", "rejected_eval_selection", "source_record_type",
        "outcome_json", "app_version", "created_at"
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

    private fun istSessionUtcWindow(date: String): UtcWindow? {
        return try {
            val sessionDate = LocalDate.parse(date, DateTimeFormatter.ISO_LOCAL_DATE)
            val istZone = ZoneId.of("Asia/Kolkata")
            UtcWindow(
                startIso = sessionDate.atStartOfDay(istZone).toInstant().toString(),
                endIso = sessionDate.plusDays(1).atStartOfDay(istZone).toInstant().toString()
            )
        } catch (_: Exception) {
            null
        }
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
        val message: String,
        val rejectedPersistedCount: Int = 0,
        val rejectedExpectedCount: Int = 0,
        val rejectedSaveMode: String = "not_attempted"
    )

    private data class OutcomePostResult(
        val success: Boolean,
        val expectedRows: Int,
        val mode: String
    )

    data class ChainFeedResult(
        val source: String,
        val rows: JSONArray
    )

    data class ChainStreamResult(
        val source: String,
        val rowCount: Int,
        val pageCount: Int,
        val capped: Boolean = false,
        val h2Required: Int = 0,
        val h2Present: Int = 0,
        val h2MissingPreview: String = ""
    )

    data class SnapshotStreamResult(
        val source: String,
        val count: Int,
        val pageCount: Int,
        val complete: Boolean,
        val completePath: String
    )

    private data class ChainSource(
        val path: String,
        val source: String,
        val filterDate: String?,
        val maxPages: Int,
        val reverseOutput: Boolean = false
    )

    data class EvaluationLegKey(
        val indexKey: String,
        val expiry: String,
        val strike: Double,
        val optionType: String
    )

    private data class UtcWindow(
        val startIso: String,
        val endIso: String
    )

    private fun postArrayToTableDetailed(table: String, body: JSONArray): ArrayPostResult {
        if (body.length() == 0) return ArrayPostResult(success = true, table = table)
        val request = getBaseRequest(table)
            .header("Prefer", "resolution=merge-duplicates")
            .post(body.toString().toRequestBody("application/json".toMediaTypeOrNull()))
            .build()
        return try {
            client.newCall(request).execute().use { response ->
                if (response.isSuccessful) {
                    ArrayPostResult(success = true, table = table, code = response.code, message = response.message)
                } else {
                    val err = response.body?.string() ?: ""
                    Log.e(TAG, "Post failed ($table): ${response.code} ${response.message} | $err")
                    LogBuffer.add('E', TAG, "POST_ARRAY_FAILED: table=$table status=${response.code} message=${response.message} body=${err.take(700)}")
                    ArrayPostResult(
                        success = false,
                        table = table,
                        code = response.code,
                        message = response.message,
                        errorBody = err
                    )
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Post exception ($table): ${e.message}")
            LogBuffer.add('E', TAG, "POST_ARRAY_EXCEPTION: table=$table error=${e.message}")
            ArrayPostResult(success = false, table = table, exceptionMessage = e.message)
        }
    }

    private fun postArrayToTable(table: String, body: JSONArray): Boolean {
        return postArrayToTableDetailed(table, body).success
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
        return postArrayToTableChunkedDetailed(table, body, chunkSize).success
    }

    private fun postArrayToTableChunkedDetailed(
        table: String,
        body: JSONArray,
        chunkSize: Int = 250
    ): ArrayPostResult {
        if (body.length() == 0) return ArrayPostResult(success = true, table = table)
        val chunks = splitJSONArray(body, chunkSize)
        if (chunks.isEmpty()) return ArrayPostResult(success = true, table = table)
        var chunkIndex = 0
        for (chunk in chunks) {
            chunkIndex += 1
            val result = postArrayToTableDetailed(table, chunk)
            if (!result.success) {
                Log.e(TAG, "Chunked post failed ($table) chunk=$chunkIndex/${chunks.size} rows=${chunk.length()}")
                LogBuffer.add(
                    'E',
                    TAG,
                    "CHUNKED_POST_FAILED: table=$table chunk=$chunkIndex/${chunks.size} rows=${chunk.length()} " +
                        "status=${result.code ?: "exception"} body=${result.errorBody?.take(700) ?: result.exceptionMessage.orEmpty()}"
                )
                return result.copy(
                    failedChunkIndex = chunkIndex,
                    failedChunkRows = chunk.length(),
                    totalChunks = chunks.size
                )
            }
        }
        return ArrayPostResult(success = true, table = table, totalChunks = chunks.size)
    }

    private fun countRows(table: String, filter: String? = null): Int {
        val queryParams = mutableListOf<String>()
        if (filter != null) queryParams.add(filter)
        queryParams.add("select=id")
        queryParams.add("limit=0")
        val url = "$table?${queryParams.joinToString("&")}"
        return try {
            val request = getBaseRequest(url)
                .addHeader("Prefer", "count=exact")
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
        } catch (oom: OutOfMemoryError) {
            Log.e(TAG, "FETCH_ARRAY_OOM: path=$path bytes=${json.length} error=${oom.message}")
            LogBuffer.add('E', TAG, "FETCH_ARRAY_OOM: path=${path.take(180)} bytes=${json.length}")
            System.gc()
            null
        } catch (_: Exception) {
            null
        } finally {
            System.gc()
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

    private fun fetchPagedArrayFromTables(
        paths: List<String>,
        pageSize: Int,
        maxPages: Int
    ): JSONArray {
        for (path in paths) {
            val rows = fetchPagedArray(path, pageSize = pageSize, maxPages = maxPages)
            if (rows.length() > 0) return rows
        }
        return JSONArray()
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
            val role = src.optString("role", "secondary").trim().lowercase(Locale.US)
            if (role == "rejected") continue
            val row = JSONObject()
            row.put("snapshot_id", src.opt("snapshot_id"))
            row.put("session_date", src.opt("session_date"))
            row.put("candidate_id", src.opt("candidate_id"))
            row.put("lane", src.opt("lane"))
            row.put("index_key", src.opt("index_key"))
            row.put("trade_mode", src.opt("trade_mode"))
            row.put("strategy_type", src.opt("strategy_type"))
            row.put("role", role.ifBlank { "secondary" })
            row.put("sim_pnl_h2", src.opt("sim_pnl_h2"))
            if (!src.isNull("outcome_h2")) row.put("outcome_h2", src.opt("outcome_h2"))
            if (!src.isNull("canonical_won")) row.put("canonical_won", src.opt("canonical_won"))
            shadowTeacherKeys.forEach { key ->
                if (!src.isNull(key)) row.put(key, src.opt(key))
            }
            sanitizeFailedIntegrityTeacherRow(row)
            row.put("created_at", nowIso)
            rows.put(row)
        }
        return rows
    }

    private fun hasValue(src: JSONObject, key: String): Boolean = src.has(key) && !src.isNull(key)

    private fun putIfPresent(row: JSONObject, src: JSONObject, key: String, dest: String = key) {
        if (hasValue(src, key)) row.put(dest, src.opt(key))
    }

    private fun putOptionalDouble(row: JSONObject, src: JSONObject, key: String, dest: String = key) {
        if (!hasValue(src, key)) return
        val value = src.optDouble(key, Double.NaN)
        if (java.lang.Double.isFinite(value)) row.put(dest, value)
    }

    private fun putOptionalInt(row: JSONObject, src: JSONObject, key: String, dest: String = key) {
        if (!hasValue(src, key)) return
        row.put(dest, src.optInt(key))
    }

    private fun optBooleanish(src: JSONObject, key: String): Boolean? {
        if (!hasValue(src, key)) return null
        return when (val raw = src.opt(key)) {
            is Boolean -> raw
            is Number -> raw.toInt() != 0
            is String -> when (raw.trim().lowercase(Locale.US)) {
                "true", "t", "yes", "y", "1", "win", "success" -> true
                "false", "f", "no", "n", "0", "loss", "fail" -> false
                else -> null
            }
            else -> null
        }
    }

    private fun putOptionalBoolean(row: JSONObject, src: JSONObject, key: String, dest: String = key) {
        optBooleanish(src, key)?.let { row.put(dest, it) }
    }

    private fun jsonKeys(obj: JSONObject): Set<String> {
        val keys = linkedSetOf<String>()
        val iterator = obj.keys()
        while (iterator.hasNext()) {
            keys.add(iterator.next())
        }
        return keys
    }

    private fun rejectedPayloadKeyDiff(rows: JSONArray): String {
        val keys = linkedSetOf<String>()
        for (i in 0 until rows.length()) {
            rows.optJSONObject(i)?.let { keys.addAll(jsonKeys(it)) }
        }
        val unknown = keys.filterNot(rejectedOutcomeColumns::contains).sorted()
        val missingRequired = listOf("id", "snapshot_id", "session_date", "candidate_id", "role")
            .filterNot(keys::contains)
        return "keys=${keys.size} unknown=$unknown missingRequired=$missingRequired"
    }

    private fun rejectedOutcomeId(sessionDate: String, src: JSONObject, rowIndex: Int): String {
        val snapshotId = src.optString("snapshot_id").ifBlank { "snapshot_unknown" }
        val candidateId = src.optString("candidate_id").ifBlank { "candidate_$rowIndex" }
        val labelVersion = src.optString("label_version").ifBlank { "teacher_v1" }
        return "$sessionDate:$snapshotId:$candidateId:$labelVersion"
            .replace(Regex("\\s+"), "_")
            .take(300)
    }

    private fun buildRejectedEvaluationRows(sessionDate: String, body: JSONArray): JSONArray {
        val nowIso = java.time.Instant.now().toString()
        val rows = JSONArray()
        for (i in 0 until body.length()) {
            val src = body.optJSONObject(i) ?: continue
            val role = src.optString("role", "secondary").trim().lowercase(Locale.US)
            if (role != "rejected") continue
            val row = JSONObject()
            row.put("id", rejectedOutcomeId(sessionDate, src, i))
            row.put("snapshot_id", src.optString("snapshot_id").ifBlank { "snapshot_unknown" })
            row.put("session_date", sessionDate)
            row.put("candidate_id", src.optString("candidate_id").ifBlank { "candidate_$i" })
            row.put("role", "rejected")

            listOf(
                "poll_ts",
                "lane",
                "index_key",
                "trade_mode",
                "strategy_type",
                "exit_reason",
                "exit_ts",
                "regime_bucket",
                "label_version",
                "teacher_config_version",
                "price_integrity",
                "h2_price_integrity_reason",
                "rejection_stage",
                "rejection_reason",
                "gate_name",
                "gate_field",
                "rejected_eval_source",
                "source_record_type",
                "app_version"
            ).forEach { key -> putIfPresent(row, src, key) }

            listOf(
                "sim_pnl_h2",
                "managed_pnl",
                "managed_gross_pnl",
                "friction_cost",
                "r_multiple",
                "captured_pct",
                "risk_at_entry",
                "tp_threshold",
                "sl_threshold",
                "break_even_win_rate_pct",
                "premium_edge",
                "credit_width_ratio",
                "sigma_otm",
                "observed_value",
                "threshold_value",
                "margin",
                "margin_pct",
                "stage_sample_fraction"
            ).forEach { key -> putOptionalDouble(row, src, key) }

            listOf(
                "exit_step",
                "path_points_count",
                "rejected_rank_in_snapshot",
                "rejected_eval_rank",
                "rejected_eval_cap",
                "stage_total_rejected",
                "stage_normalizable",
                "stage_skipped_not_evaluable"
            ).forEach { key -> putOptionalInt(row, src, key) }

            listOf("outcome_h2", "canonical_won", "is_success").forEach { key ->
                putOptionalBoolean(row, src, key)
            }

            val selection = src.optJSONObject("rejected_eval_selection")
            if (selection != null) row.put("rejected_eval_selection", selection)
            row.put("outcome_json", JSONObject(src.toString()))
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
            if (role == "rejected") continue
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
            sanitizeFailedIntegrityTeacherRow(row)
            row.put("created_at", nowIso)
            rows.put(row)
        }
        return rows
    }

    private fun sanitizeFailedIntegrityTeacherRow(row: JSONObject) {
        if (!row.optString("price_integrity").equals("FAIL", ignoreCase = true)) return
        listOf(
            "managed_pnl",
            "managed_gross_pnl",
            "friction_cost",
            "r_multiple",
            "captured_pct",
            "is_success"
        ).forEach(row::remove)
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
     * Reads daily-grain context percentile history and collapses it to one
     * enriched row per session date using the latest recorded value for each
     * variable on that day. This preserves the existing ctx premiumHistory
     * contract while making the persisted percentile-era variables available
     * to the live brain.
     *
     * Daily rows have poll_ts = NULL. Legacy poll-level backfills share the
     * same history_source label, so the reader must pin grain structurally.
     */
    fun getContextPercentileDailyHistory(maxDays: Int = 60): JSONArray {
        fun sourceRank(row: JSONObject): Int {
            return when (row.optString("history_source", "").trim().lowercase(Locale.US)) {
                "live" -> 2
                "backfill" -> 1
                else -> 0
            }
        }

        fun betterContextPercentileRow(candidate: JSONObject, existing: JSONObject?): Boolean {
            if (existing == null) return true
            val candidateRank = sourceRank(candidate)
            val existingRank = sourceRank(existing)
            if (candidateRank != existingRank) return candidateRank > existingRank
            val candidateWindow = candidate.optString("history_window_end", "")
            val existingWindow = existing.optString("history_window_end", "")
            return candidateWindow > existingWindow
        }

        val pageSize = 1000
        val maxPages = 8
        val allRows = JSONArray()
        for (page in 0 until maxPages) {
            val offset = page * pageSize
            val path =
                "ml_context_percentile_history" +
                    "?select=session_date,poll_ts,variable_name,value,history_window_end,history_source,pre_t_clean,source_table,source_quality,support_count,support_count_30,support_count_60" +
                    "&poll_ts=is.null" +
                    "&order=session_date.desc,history_window_end.desc,variable_name.asc" +
                    "&limit=$pageSize&offset=$offset"
            val request = getBaseRequest(path).get().build()
            val json = fetchSync(request) ?: break
            val pageRows = try {
                JSONArray(json)
            } catch (e: Exception) {
                Log.e(TAG, "Error parsing context percentile history page=$page: ${e.message}")
                break
            }
            if (pageRows.length() == 0) break
            for (i in 0 until pageRows.length()) {
                allRows.put(pageRows.optJSONObject(i) ?: continue)
            }
            if (pageRows.length() < pageSize) break
        }
        if (allRows.length() == 0) return JSONArray()

        val dayOrder = LinkedHashSet<String>()
        val selectedByDayVariable = LinkedHashMap<String, JSONObject>()
        for (i in 0 until allRows.length()) {
            val row = allRows.optJSONObject(i) ?: continue
            val sessionDate = row.optString("session_date", "").trim()
            val variableName = row.optString("variable_name", "").trim()
            if (sessionDate.isEmpty() || variableName.isEmpty()) continue
            if (!dayOrder.contains(sessionDate) && dayOrder.size >= maxDays) {
                continue
            }
            dayOrder.add(sessionDate)
            val key = "$sessionDate|$variableName"
            val existing = selectedByDayVariable[key]
            if (betterContextPercentileRow(row, existing)) {
                selectedByDayVariable[key] = row
            }
        }

        val dayRows = LinkedHashMap<String, JSONObject>()
        for ((_, row) in selectedByDayVariable) {
            val sessionDate = row.optString("session_date", "").trim()
            val variableName = row.optString("variable_name", "").trim()
            val dayObj = dayRows.getOrPut(sessionDate) {
                JSONObject().put("date", sessionDate).put("session_date", sessionDate)
            }
            if (!dayObj.has("pre_t_clean") && row.has("pre_t_clean")) {
                dayObj.put("pre_t_clean", row.optBoolean("pre_t_clean", false))
            }
            if (!dayObj.has("history_window_end")) {
                val historyWindowEnd = row.optString("history_window_end", "").trim()
                if (historyWindowEnd.isNotEmpty()) {
                    dayObj.put("history_window_end", historyWindowEnd)
                }
            }
            val percentileKey = "pct_$variableName"
            if (!dayObj.has(percentileKey) && row.has("value") && !row.isNull("value")) {
                dayObj.put(percentileKey, row.optDouble("value"))
                dayObj.put("${percentileKey}_history_source", row.optString("history_source", ""))
                dayObj.put("${percentileKey}_support_count", row.optInt("support_count", 0))
            }
        }

        val out = JSONArray()
        for ((_, value) in dayRows) {
            out.put(value)
            if (out.length() >= maxDays) break
        }
        return out
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
        val result = postToFirstWorkingTableDetailed(
            listOf("ml_generated_candidates?on_conflict=snapshot_poll_ts,candidate_id"),
            body,
            preferHeader = "resolution=merge-duplicates,return=minimal"
        )
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

    fun saveBuild3AbDecision(row: JSONObject): Boolean {
        val result = postToFirstWorkingTableDetailed(
            listOf("ab_week1_decisions?on_conflict=snapshot_poll_ts,experiment_name"),
            row.toString(),
            preferHeader = "resolution=merge-duplicates,return=minimal"
        )
        if (!result.success) {
            val details = buildString {
                append("BUILD3_AB_SAVE_HTTP: table=")
                append(result.table ?: "ab_week1_decisions")
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
                append(row.toString().toByteArray().size)
            }
            LogBuffer.add('W', TAG, details)
            Log.w(TAG, details)
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

    fun fetchEvaluationSnapshots(
        date: String,
        outputFile: File,
        onRow: ((JSONObject) -> Unit)? = null,
        maxPages: Int = 80
    ): SnapshotStreamResult {
        val select = "id,poll_ts,primary_candidate_json,context_json,top_candidates_json,is_labelable,session_date"
        val sources = listOf(
            "ml_brain_snapshots?session_date=eq.$date&select=$select&order=poll_ts.desc" to false,
            "ml_poll_sequences?session_date=eq.$date&select=$select&order=poll_ts.desc" to false,
            "ml_brain_snapshots?select=$select&order=poll_ts.desc" to true,
            "ml_poll_sequences?select=$select&order=poll_ts.desc" to true
        )

        for ((basePath, requiresDateFallbackFilter) in sources) {
            val writerFile = outputFile
            writerFile.parentFile?.mkdirs()
            var total = 0
            var pages = 0
            var offset = 0
            var reachedEnd = false
            var complete = false

            try {
                writerFile.writeText("")
            } catch (_: Exception) {
            }

            writerFile.bufferedWriter().use { writer ->
                writer.write("[")
                var first = true

                while (pages < maxPages) {
                    val separator = if (basePath.contains("?")) "&" else "?"
                    val page = fetchArray("$basePath${separator}limit=$EVALUATION_SNAPSHOT_PAGE_SIZE&offset=$offset") ?: break
                    if (page.length() == 0) {
                        reachedEnd = true
                        complete = true
                        break
                    }

                    pages += 1
                    for (i in 0 until page.length()) {
                        val row = page.optJSONObject(i) ?: continue
                        if (requiresDateFallbackFilter && !rowBelongsToIstSessionDate(row, date)) continue

                        if (!first) writer.write(",")
                        writer.write(row.toString())
                        writer.flush()
                        first = false
                        total += 1

                        try {
                            onRow?.invoke(row)
                        } catch (_: Exception) {
                        }
                    }

                    if (page.length() < EVALUATION_SNAPSHOT_PAGE_SIZE) {
                        reachedEnd = true
                        complete = true
                        break
                    }

                    offset += page.length()
                }

                writer.write("]")
            }

            if (reachedEnd && total > 0) {
                val finalPath = writerFile.absolutePath
                return SnapshotStreamResult(basePath, total, pages, complete, finalPath)
            }
        }

        return SnapshotStreamResult("none", 0, 0, false, "")
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
        val pollWindow = istSessionUtcWindow(date)
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

        if (pollWindow != null) {
            val windowPreferred = fetchPagedArray(
                "ml_option_chain_snapshots?poll_ts=gte.${pollWindow.startIso}&poll_ts=lt.${pollWindow.endIso}&order=poll_ts.asc",
                maxPages = EVALUATION_CHAIN_EXACT_MAX_PAGES
            )
            if (windowPreferred.length() > 0) {
                return ChainFeedResult("ml_option_chain_snapshots.poll_window", normalizeChainRows(windowPreferred))
            }

            val windowFallback = fetchPagedArray(
                "chain_slices?poll_ts=gte.${pollWindow.startIso}&poll_ts=lt.${pollWindow.endIso}&order=poll_ts.asc",
                maxPages = EVALUATION_CHAIN_EXACT_MAX_PAGES
            )
            if (windowFallback.length() > 0) {
                return ChainFeedResult("chain_slices.poll_window", normalizeChainRows(windowFallback))
            }
        }

        val recentPreferred = reverseJsonArray(
            filterRowsByIstSessionDate(
                fetchPagedArray(
                    "ml_option_chain_snapshots?select=*&order=poll_ts.desc",
                    maxPages = EVALUATION_CHAIN_RECENT_FALLBACK_MAX_PAGES
                ),
                date
            )
        )
        if (recentPreferred.length() > 0) {
            return ChainFeedResult("ml_option_chain_snapshots.filtered_recent", normalizeChainRows(recentPreferred))
        }

        val recentFallback = reverseJsonArray(
            filterRowsByIstSessionDate(
                fetchPagedArray(
                    "chain_slices?select=*&order=poll_ts.desc",
                    maxPages = EVALUATION_CHAIN_RECENT_FALLBACK_MAX_PAGES
                ),
                date
            )
        )
        return ChainFeedResult("chain_slices.filtered_recent", normalizeChainRows(recentFallback))
    }

    private fun reverseJsonArray(rows: JSONArray): JSONArray {
        val out = JSONArray()
        for (i in rows.length() - 1 downTo 0) {
            out.put(rows.opt(i))
        }
        return out
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

        val pollWindow = istSessionUtcWindow(date)
        val sources = mutableListOf(
            ChainSource(
                "ml_option_chain_snapshots?session_date=eq.$date&order=poll_ts.asc",
                "ml_option_chain_snapshots.exact.filtered_stream",
                null,
                EVALUATION_CHAIN_EXACT_MAX_PAGES
            ),
            ChainSource(
                "chain_slices?session_date=eq.$date&order=poll_ts.asc",
                "chain_slices.exact.filtered_stream",
                null,
                EVALUATION_CHAIN_EXACT_MAX_PAGES
            )
        )
        if (pollWindow != null) {
            sources.add(
                ChainSource(
                    "ml_option_chain_snapshots?poll_ts=gte.${pollWindow.startIso}&poll_ts=lt.${pollWindow.endIso}&order=poll_ts.asc",
                    "ml_option_chain_snapshots.poll_window.filtered_stream",
                    null,
                    EVALUATION_CHAIN_EXACT_MAX_PAGES
                )
            )
            sources.add(
                ChainSource(
                    "chain_slices?poll_ts=gte.${pollWindow.startIso}&poll_ts=lt.${pollWindow.endIso}&order=poll_ts.asc",
                    "chain_slices.poll_window.filtered_stream",
                    null,
                    EVALUATION_CHAIN_EXACT_MAX_PAGES
                )
            )
        }
        sources.addAll(
            listOf(
                ChainSource(
                    "ml_option_chain_snapshots?select=*&order=poll_ts.desc",
                    "ml_option_chain_snapshots.filtered_recent.filtered_stream",
                    date,
                    EVALUATION_CHAIN_RECENT_FALLBACK_MAX_PAGES,
                    reverseOutput = true
                ),
                ChainSource(
                    "chain_slices?select=*&order=poll_ts.desc",
                    "chain_slices.filtered_recent.filtered_stream",
                    date,
                    EVALUATION_CHAIN_RECENT_FALLBACK_MAX_PAGES,
                    reverseOutput = true
                )
            )
        )

        val tmpFile = File(outputFile.parentFile, "${outputFile.name}.tmp")
        var lastResult = ChainStreamResult("no_chain_source", 0, 0)
        var bestPartialResult: ChainStreamResult? = null
        var bestPartialFile: File? = null
        for (candidateSource in sources) {
            tmpFile.delete()
            val result = writePagedFilteredChain(
                basePath = candidateSource.path,
                source = candidateSource.source,
                legKeys = legKeys,
                outputFile = tmpFile,
                onPage = onPage,
                istSessionDate = candidateSource.filterDate,
                maxPages = candidateSource.maxPages,
                reverseOutput = candidateSource.reverseOutput
            )
            lastResult = result
            if (result.rowCount > 0) {
                if (result.h2Required == 0 || result.h2Present >= result.h2Required) {
                    replaceFile(tmpFile, outputFile)
                    return result
                }
                if (bestPartialResult == null || result.h2Present > (bestPartialResult?.h2Present ?: -1)) {
                    val partial = File(outputFile.parentFile, "${outputFile.name}.${candidateSource.source.hashCode()}.partial")
                    replaceFile(tmpFile, partial)
                    bestPartialFile?.delete()
                    bestPartialFile = partial
                    bestPartialResult = result
                }
                Log.w(TAG, "EVAL_CHAIN_SOURCE_H2_INCOMPLETE: source=${result.source} h2=${result.h2Present}/${result.h2Required}; trying next source")
                LogBuffer.add('W', TAG, "EVAL_CHAIN_SOURCE_H2_INCOMPLETE: source=${result.source} h2=${result.h2Present}/${result.h2Required}; trying next source")
                continue
            }
            if (result.capped) {
                Log.w(TAG, "EVAL_CHAIN_SOURCE_CAPPED_EMPTY: source=${result.source} pages=${result.pageCount}; trying next source")
                LogBuffer.add('W', TAG, "EVAL_CHAIN_SOURCE_CAPPED_EMPTY: source=${result.source} pages=${result.pageCount}; trying next source")
            }
            tmpFile.delete()
        }

        bestPartialResult?.let { result ->
            bestPartialFile?.let { file ->
                if (file.exists()) replaceFile(file, outputFile)
            }
            return result
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
        maxPages: Int = EVALUATION_CHAIN_EXACT_MAX_PAGES,
        reverseOutput: Boolean = false
    ): ChainStreamResult {
        outputFile.parentFile?.mkdirs()
        var offset = 0
        var pages = 0
        var rows = 0
        var reachedEnd = false
        var first = true
        val requiredH2Legs = legKeys.distinctBy { evaluationLegKeyToken(it) }
        val h2Present = linkedSetOf<String>()
        val bufferedRows = if (reverseOutput) mutableListOf<String>() else null

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
                    val rowText = normalized.toString()
                    if (isH2EvaluationWindow(normalized.optString("poll_ts"))) {
                        requiredH2Legs.firstOrNull { chainRowMatchesEvaluationLeg(normalized, it) }?.let { key ->
                            h2Present.add(evaluationLegKeyToken(key))
                        }
                    }
                    if (bufferedRows != null) {
                        bufferedRows.add(rowText)
                    } else {
                        if (!first) writer.write(",")
                        writer.write(rowText)
                        first = false
                    }
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
            if (bufferedRows != null) {
                for (i in bufferedRows.size - 1 downTo 0) {
                    if (!first) writer.write(",")
                    writer.write(bufferedRows[i])
                    first = false
                }
            }
            writer.write("]")
        }

        return ChainStreamResult(
            source = source,
            rowCount = rows,
            pageCount = pages,
            capped = !reachedEnd && pages >= maxPages,
            h2Required = requiredH2Legs.size,
            h2Present = h2Present.size,
            h2MissingPreview = requiredH2Legs
                .filterNot { h2Present.contains(evaluationLegKeyToken(it)) }
                .take(8)
                .joinToString(";") { evaluationLegKeyToken(it) }
        )
    }

    private fun matchesEvaluationLeg(row: JSONObject, legKeys: List<EvaluationLegKey>): Boolean {
        return legKeys.any { key -> chainRowMatchesEvaluationLeg(row, key) }
    }

    private fun chainRowMatchesEvaluationLeg(row: JSONObject, key: EvaluationLegKey): Boolean {
        val indexKey = row.optString("index_key").trim()
        val expiry = row.optString("expiry").trim()
        val optionType = normalizeOptionType(row.optString("option_type"))
        val strike = row.optDouble("strike", Double.NaN)
        if (indexKey.isBlank() || optionType.isBlank() || strike.isNaN()) return false

        return key.indexKey.equals(indexKey, ignoreCase = true) &&
            (key.expiry.isBlank() || expiry.isBlank() || key.expiry == expiry) &&
            normalizeOptionType(key.optionType) == optionType &&
            kotlin.math.abs(key.strike - strike) < 0.01
    }

    private fun evaluationLegKeyToken(key: EvaluationLegKey): String =
        listOf(
            key.indexKey.trim().uppercase(Locale.US),
            key.expiry.trim(),
            normalizeOptionType(key.optionType),
            "%.2f".format(Locale.US, key.strike)
        ).joinToString("|")

    private fun isH2EvaluationWindow(pollTs: String): Boolean {
        if (pollTs.isBlank()) return false
        return try {
            val zoned = OffsetDateTime.parse(pollTs).atZoneSameInstant(ZoneId.of("Asia/Kolkata"))
            zoned.hour == 15 && zoned.minute in 15..40
        } catch (_: Exception) {
            false
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
        val rejectedRows = buildRejectedEvaluationRows(sessionDate, body)

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

        fun stripRejectedResearch(rows: JSONArray): JSONArray {
            val legacy = JSONArray()
            for (i in 0 until rows.length()) {
                val src = rows.optJSONObject(i) ?: continue
                val role = src.optString("role", "secondary").trim().lowercase(Locale.US)
                if (role == "rejected") continue
                legacy.put(JSONObject(src.toString()))
            }
            return legacy
        }

        val evaluationRowsNoRejected = stripRejectedResearch(evaluationRows)
        val evaluationRowsNoShadow = stripShadowTeacher(evaluationRows)
        val evaluationRowsNoRejectedNoShadow = stripShadowTeacher(evaluationRowsNoRejected)
        val recommendationRowsNoShadow = stripShadowTeacher(recommendationRows)
        val evaluationRowsNoCanonical = stripCanonical(evaluationRowsNoShadow)
        val evaluationRowsNoRejectedNoCanonical = stripCanonical(evaluationRowsNoRejectedNoShadow)
        val recommendationRowsNoCanonical = stripCanonical(recommendationRowsNoShadow)
        val evaluationRowsLegacy = stripCanonical(stripAttribution(evaluationRowsNoShadow))
        val evaluationRowsNoRejectedLegacy = stripCanonical(stripAttribution(evaluationRowsNoRejectedNoShadow))
        val recommendationRowsLegacy = stripCanonical(stripAttribution(recommendationRowsNoShadow))

        fun postOutcomeRowsWithFallback(
            table: String,
            fullRows: JSONArray,
            noRejectedRows: JSONArray,
            noShadowRows: JSONArray,
            noRejectedNoShadowRows: JSONArray,
            noCanonicalRows: JSONArray,
            noRejectedNoCanonicalRows: JSONArray,
            legacyRows: JSONArray,
            noRejectedLegacyRows: JSONArray
        ): OutcomePostResult {
            val attempts = listOf(
                "full" to fullRows,
                "no_rejected_research" to noRejectedRows,
                "no_shadow" to noShadowRows,
                "no_rejected_research_no_shadow" to noRejectedNoShadowRows,
                "no_shadow_no_canonical" to noCanonicalRows,
                "no_rejected_research_no_shadow_no_canonical" to noRejectedNoCanonicalRows,
                "legacy" to legacyRows,
                "no_rejected_research_legacy" to noRejectedLegacyRows
            )
            for ((mode, rows) in attempts) {
                if (rows.length() == 0) {
                    if (mode != "full") {
                        LogBuffer.add(
                            'W',
                            TAG,
                            "OUTCOME_FALLBACK_EMPTY_PAYLOAD: table=$table mode=$mode fullRows=${fullRows.length()}"
                        )
                    }
                    return OutcomePostResult(success = true, expectedRows = 0, mode = mode)
                }
                if (!postArrayToTableChunked(table, rows)) continue
                if (mode == "full") return OutcomePostResult(success = true, expectedRows = rows.length(), mode = mode)
                LogBuffer.add(
                    'W',
                    TAG,
                    "S1_PRICE_INTEGRITY_FALLBACK_STRIPPED: table=$table mode=$mode rows=${fullRows.length()} persistedTarget=${rows.length()} migration_required_before_release"
                )
                return OutcomePostResult(success = true, expectedRows = rows.length(), mode = mode)
            }
            return OutcomePostResult(success = false, expectedRows = fullRows.length(), mode = "failed")
        }

        val evaluationWriteResult = postOutcomeRowsWithFallback(
            "ml_evaluation_outcomes",
            evaluationRows,
            evaluationRowsNoRejected,
            evaluationRowsNoShadow,
            evaluationRowsNoRejectedNoShadow,
            evaluationRowsNoCanonical,
            evaluationRowsNoRejectedNoCanonical,
            evaluationRowsLegacy,
            evaluationRowsNoRejectedLegacy
        )
        val recommendationWriteResult = postOutcomeRowsWithFallback(
            "ml_recommendation_outcomes",
            recommendationRows,
            recommendationRows,
            recommendationRowsNoShadow,
            recommendationRowsNoShadow,
            recommendationRowsNoCanonical,
            recommendationRowsNoCanonical,
            recommendationRowsLegacy,
            recommendationRowsLegacy
        )
        val rejectedWriteMode = if (rejectedRows.length() > 0) "separate_table" else "not_applicable"
        val rejectedWriteResult = if (rejectedRows.length() > 0) {
            val keyDiff = rejectedPayloadKeyDiff(rejectedRows)
            LogBuffer.add('I', TAG, "REJECTED_RESEARCH_PAYLOAD_AUDIT: rows=${rejectedRows.length()} $keyDiff")
            postArrayToTableChunkedDetailed(
                "ml_rejected_candidate_outcomes?on_conflict=id",
                rejectedRows,
                chunkSize = 100
            )
        } else {
            ArrayPostResult(success = true, table = "ml_rejected_candidate_outcomes")
        }

        val evaluationPersisted = countRows("ml_evaluation_outcomes", "session_date=eq.$sessionDate")
        val recommendationPersisted = countRows("ml_recommendation_outcomes", "session_date=eq.$sessionDate")
        val rejectedPersisted = if (rejectedRows.length() > 0) {
            countRows("ml_rejected_candidate_outcomes", "session_date=eq.$sessionDate")
        } else {
            0
        }
        val evaluationSaved = evaluationWriteResult.success && evaluationPersisted >= evaluationWriteResult.expectedRows
        val recommendationSaved = recommendationPersisted > 0 && recommendationWriteResult.success
        val rejectedSaved = rejectedRows.length() == 0 ||
            (rejectedWriteResult.success && rejectedPersisted >= rejectedRows.length())
        val success = evaluationSaved
        val baseMessage = when {
            evaluationSaved && recommendationSaved ->
                "Persisted $evaluationPersisted evaluation rows; $recommendationPersisted recommendation rows persisted separately. evalMode=${evaluationWriteResult.mode} recoMode=${recommendationWriteResult.mode}"
            evaluationSaved ->
                "Persisted $evaluationPersisted evaluation rows to Supabase; recommendation rows were not fully verified. evalMode=${evaluationWriteResult.mode} recoMode=${recommendationWriteResult.mode}"
            recommendationSaved ->
                "Persisted $recommendationPersisted recommendation rows to Supabase; evaluation rows were not saved. evalMode=${evaluationWriteResult.mode} recoMode=${recommendationWriteResult.mode}"
            else -> "Supabase persistence failed for evaluation outcomes."
        }
        val rejectedMessage = when {
            rejectedRows.length() == 0 ->
                " rejectedResearch=0/0 mode=not_applicable"
            rejectedSaved ->
                " rejectedResearch=$rejectedPersisted/${rejectedRows.length()} mode=$rejectedWriteMode"
            else -> {
                val status = rejectedWriteResult.code?.toString() ?: "exception"
                val detail = rejectedWriteResult.errorBody
                    ?.replace(Regex("\\s+"), " ")
                    ?.take(500)
                    ?: rejectedWriteResult.exceptionMessage?.take(500)
                    ?: "unknown_error"
                val keyDiff = rejectedPayloadKeyDiff(rejectedRows)
                val warning = " REJECTED_RESEARCH_SAVE_FAILED expected=${rejectedRows.length()} persisted=$rejectedPersisted " +
                    "table=ml_rejected_candidate_outcomes status=$status chunk=${rejectedWriteResult.failedChunkIndex ?: 0}/${rejectedWriteResult.totalChunks} " +
                    "$keyDiff error=$detail"
                LogBuffer.add('E', TAG, warning.trim())
                Log.e(TAG, warning.trim())
                warning
            }
        }
        val message = baseMessage + rejectedMessage

        return EvaluationSaveResult(
            success = success,
            producedCount = body.length(),
            persistedCount = evaluationPersisted + rejectedPersisted,
            primaryPersistedCount = recommendationPersisted,
            evaluationPersistedCount = evaluationPersisted,
            message = message,
            rejectedPersistedCount = rejectedPersisted,
            rejectedExpectedCount = rejectedRows.length(),
            rejectedSaveMode = if (rejectedSaved) rejectedWriteMode else "failed"
        )
    }

    fun fetchRecentEvaluationOutcomes(limit: Int = 1000): JSONArray {
        val shadowRows = normalizeShadowOutcomeRows(
            select("ml_evaluation_outcomes_s1", null, "effective_session_date.desc,created_at.desc", limit)
        )
        if (shadowRows.length() > 0) return shadowRows
        val evaluationRows = normalizeLegacyOutcomeRows(select("ml_evaluation_outcomes", null, "created_at.desc", limit))
        if (evaluationRows.length() > 0) return evaluationRows
        return normalizeLegacyOutcomeRows(select("ml_decisions", null, "created_at.desc", limit))
    }

    fun fetchEvaluationOutcomesForDate(sessionDate: String, limit: Int = 5000): JSONArray {
        val filter = "session_date=eq.$sessionDate"
        val shadowRows = normalizeShadowOutcomeRows(
            select("ml_evaluation_outcomes_s1", "effective_session_date=eq.$sessionDate", "created_at.desc", limit)
        )
        if (shadowRows.length() > 0) return shadowRows
        val evaluationRows = normalizeLegacyOutcomeRows(select("ml_evaluation_outcomes", filter, "created_at.desc", limit))
        if (evaluationRows.length() > 0) return evaluationRows
        val legacyRows = normalizeLegacyOutcomeRows(select("ml_decisions", filter, "created_at.desc", limit))
        if (legacyRows.length() > 0) return legacyRows
        return JSONArray()
    }

    private fun fetchAttributedRecommendationOutcomes(sessionDate: String, limit: Int = 1000): JSONArray {
        val filter = "session_date=eq.$sessionDate"
        val shadowRows = normalizeShadowOutcomeRows(
            select("ml_recommendation_outcomes_s1", "effective_session_date=eq.$sessionDate", "created_at.desc", limit)
        )
        if (shadowRows.length() > 0) return shadowRows
        val rows = normalizeLegacyOutcomeRows(select("ml_recommendation_outcomes", filter, "created_at.desc", limit))
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
        val evalRows = normalizeLegacyOutcomeRows(select("ml_evaluation_outcomes", filter, "created_at.desc", limit))
        if (evalRows.length() > 0) return evalRows
        return JSONArray()
    }

    private fun normalizeLegacyOutcomeRows(rows: JSONArray): JSONArray {
        for (i in 0 until rows.length()) {
            rows.optJSONObject(i)?.let(::sanitizeFailedIntegrityTeacherRow)
        }
        return rows
    }

    private fun normalizeShadowOutcomeRows(rows: JSONArray): JSONArray {
        val out = JSONArray()
        for (i in 0 until rows.length()) {
            val src = rows.optJSONObject(i) ?: continue
            val row = JSONObject(src.toString())
            if (!src.isNull("effective_session_date")) row.put("session_date", src.opt("effective_session_date"))
            if (!src.isNull("new_sim_pnl_h2")) row.put("sim_pnl_h2", src.opt("new_sim_pnl_h2"))
            if (!src.isNull("new_outcome_h2")) row.put("outcome_h2", src.opt("new_outcome_h2"))
            if (!src.isNull("new_canonical_won")) row.put("canonical_won", src.opt("new_canonical_won"))
            if (!src.isNull("new_price_integrity")) row.put("price_integrity", src.opt("new_price_integrity"))
            if (!src.isNull("new_h2_price_integrity_reason")) row.put("h2_price_integrity_reason", src.opt("new_h2_price_integrity_reason"))
            if (!src.isNull("new_raw_data_status")) row.put("raw_data_status", src.opt("new_raw_data_status"))
            if (!src.isNull("new_teacher_success")) row.put("is_success", src.opt("new_teacher_success"))
            if (!src.isNull("new_teacher_expectancy_r")) row.put("r_multiple", src.opt("new_teacher_expectancy_r"))
            row.put("source_table", "s1_shadow")
            out.put(row)
        }
        return out
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
        var rejectedResearchRows = 0
        for (i in 0 until todayRows.length()) {
            val row = todayRows.optJSONObject(i) ?: continue
            val role = row.optString("role", "").trim().lowercase(Locale.US)
            if (role == "rejected") {
                rejectedResearchRows += 1
                continue
            }
            val lane = row.optString("lane", "").trim()
            val bucket = lanes[lane] ?: continue
            bucket[0] += 1 // rows
            attributedRows += 1
            val mixBucket = recommendationMixLanes[lane]
            if (mixBucket != null) {
                mixBucket[0] += 1
                when (role) {
                    "primary" -> mixBucket[1] += 1
                    "secondary" -> mixBucket[2] += 1
                }
            }
            val isPrimaryLike = role != "secondary" && role != "rejected"
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
            .put("rejectedResearchRows", rejectedResearchRows)
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

    fun insertPositionTicks(rows: JSONArray): Boolean {
        if (rows.length() == 0) return true
        val request = getBaseRequest("position_ticks")
            .header("Prefer", "return=minimal")
            .post(rows.toString().toRequestBody("application/json".toMediaTypeOrNull()))
            .build()

        return try {
            client.newCall(request).execute().use { response ->
                if (response.isSuccessful) {
                    true
                } else {
                    val err = response.body?.string() ?: ""
                    Log.e(TAG, "Position tick insert failed: ${response.code} ${response.message} | $err")
                    false
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Position tick insert exception: ${e.message}")
            false
        }
    }

    fun insertSandboxOrder(row: JSONObject): Boolean {
        val request = getBaseRequest("sandbox_orders")
            .header("Prefer", "return=minimal")
            .post(row.toString().toRequestBody("application/json".toMediaTypeOrNull()))
            .build()

        return try {
            client.newCall(request).execute().use { response ->
                if (response.isSuccessful) {
                    true
                } else {
                    val err = response.body?.string() ?: ""
                    Log.e(TAG, "Sandbox order insert failed: ${response.code} ${response.message} | $err")
                    false
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Sandbox order insert exception: ${e.message}")
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
