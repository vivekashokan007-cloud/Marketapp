package com.marketradar.app

import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.TimeUnit

object SupabaseClient {
    private const val TAG = "SupabaseClient"
    private const val URL = BuildConfig.SUPABASE_URL
    private const val ANON_KEY = BuildConfig.SUPABASE_ANON_KEY

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()

    private fun getBaseRequest(path: String): Request.Builder {
        return Request.Builder()
            .url("$URL/rest/v1/$path")
            .addHeader("apikey", ANON_KEY)
            .addHeader("Authorization", "Bearer $ANON_KEY")
            .addHeader("Content-Type", "application/json")
    }

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
        val istTsFormats = listOf(
            SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssZ", Locale.US),
            SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSX", Locale.US),
            SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssX", Locale.US)
        ).onEach { fmt -> fmt.timeZone = TimeZone.getTimeZone("Asia/Kolkata") }
        val istDateFmt = SimpleDateFormat("yyyy-MM-dd", Locale.US).apply {
            timeZone = TimeZone.getTimeZone("Asia/Kolkata")
        }

        fun rowMatches(obj: JSONObject): Boolean {
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

        for (i in 0 until rows.length()) {
            val obj = rows.optJSONObject(i) ?: continue
            if (rowMatches(obj)) out.put(obj)
        }
        return out
    }

    private fun postToFirstWorkingTable(
        tableNames: List<String>,
        body: String,
        preferHeader: String = "resolution=merge-duplicates"
    ): Boolean {
        for (table in tableNames) {
            val request = getBaseRequest(table)
                .header("Prefer", preferHeader)
                .post(body.toRequestBody("application/json".toMediaTypeOrNull()))
                .build()
            try {
                client.newCall(request).execute().use { response ->
                    if (response.isSuccessful) return true
                    if (response.code !in listOf(404, 400)) {
                        val err = response.body?.string() ?: ""
                        Log.e(TAG, "Post failed ($table): ${response.code} ${response.message} | $err")
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Post exception ($table): ${e.message}")
            }
        }
        return false
    }

    data class EvaluationSaveResult(
        val success: Boolean,
        val producedCount: Int,
        val persistedCount: Int,
        val primaryPersistedCount: Int,
        val evaluationPersistedCount: Int,
        val message: String
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

    private fun buildEvaluationRows(body: JSONArray): JSONArray {
        val nowIso = java.time.Instant.now().toString()
        val rows = JSONArray()
        for (i in 0 until body.length()) {
            val src = body.optJSONObject(i) ?: continue
            val row = JSONObject()
            row.put("snapshot_id", src.opt("snapshot_id"))
            row.put("candidate_id", src.opt("candidate_id"))
            row.put("role", src.optString("role", "secondary"))
            row.put("sim_pnl_h2", src.opt("sim_pnl_h2"))
            if (!src.isNull("outcome_h2")) row.put("outcome_h2", src.opt("outcome_h2"))
            if (!src.isNull("canonical_won")) row.put("canonical_won", src.opt("canonical_won"))
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
            if (!src.optString("role", "secondary").equals("primary", ignoreCase = true)) continue
            val row = JSONObject()
            row.put("snapshot_id", src.opt("snapshot_id"))
            row.put("session_date", sessionDate)
            row.put("candidate_id", src.opt("candidate_id"))
            row.put("role", "primary")
            row.put("sim_pnl_h2", src.opt("sim_pnl_h2"))
            if (!src.isNull("outcome_h2")) row.put("outcome_h2", src.opt("outcome_h2"))
            if (!src.isNull("canonical_won")) row.put("canonical_won", src.opt("canonical_won"))
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
        if (postToFirstWorkingTable(tables, body.toString())) return true

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
        return postToFirstWorkingTable(tables, minimal.toString())
    }

    /**
     * Best-effort compact candidate persistence for offline ML evaluation breadth.
     *
     * The table may not exist yet in every environment; fail closed without
     * affecting the live poll path.
     */
    fun saveGeneratedCandidates(rows: JSONArray): Boolean {
        if (rows.length() == 0) return true
        return postToFirstWorkingTable(listOf("ml_generated_candidates"), rows.toString())
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
        val select = "id,poll_ts,primary_candidate_json,top_candidates_json,is_labelable,session_date"
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
        val select = "index_key,strike,option_type,expiry,poll_ts,ltp,session_date"
        val exact = fetchArrayFromTables(
            listOf(
                "ml_option_chain_snapshots?session_date=eq.$date&select=$select&order=poll_ts.desc",
                "chain_slices?session_date=eq.$date&select=$select&order=poll_ts.desc",
                "chain_snapshots?date=eq.$date&select=$select&order=created_at.desc"
            )
        )
        if (exact.length() > 0) return exact

        val recent = fetchArrayFromTables(
            listOf(
                "ml_option_chain_snapshots?select=$select&order=poll_ts.desc&limit=3000",
                "chain_slices?select=$select&order=poll_ts.desc&limit=3000",
                "chain_snapshots?select=$select&order=created_at.desc&limit=3000"
            )
        )
        return filterRowsByIstSessionDate(recent, date)
    }

    fun saveChainSlice(body: JSONObject): Boolean {
        return postToFirstWorkingTable(
            listOf("ml_option_chain_snapshots", "chain_slices", "chain_snapshots"),
            body.toString()
        )
    }

    fun saveEvaluationOutcomes(sessionDate: String, body: JSONArray): EvaluationSaveResult {
        val evaluationRows = buildEvaluationRows(body)
        val recommendationRows = buildRecommendationRows(sessionDate, body)

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

        val evaluationSaved = postArrayToTable("ml_evaluation_outcomes", evaluationRows) ||
            postArrayToTable("ml_evaluation_outcomes", stripCanonical(evaluationRows))
        val recommendationSaved = postArrayToTable("ml_recommendation_outcomes", recommendationRows) ||
            postArrayToTable("ml_recommendation_outcomes", stripCanonical(recommendationRows))

        val evaluationPersisted = if (evaluationSaved) evaluationRows.length() else 0
        val recommendationPersisted = if (recommendationSaved) recommendationRows.length() else 0
        val success = evaluationSaved || recommendationSaved
        val message = when {
            evaluationSaved && recommendationSaved ->
                "Persisted $evaluationPersisted evaluation rows; $recommendationPersisted primary recommendation rows persisted separately."
            evaluationSaved ->
                "Persisted $evaluationPersisted evaluation rows to Supabase."
            recommendationSaved ->
                "Persisted $recommendationPersisted primary recommendation rows to Supabase; evaluation rows were not saved."
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
