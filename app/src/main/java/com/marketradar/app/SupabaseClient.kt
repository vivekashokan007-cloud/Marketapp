package com.marketradar.app

import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
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
     * Saves a 2pm/315pm chain snapshot to chain_snapshots table
     */
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

        val request = getBaseRequest("chain_snapshots")
            .header("Prefer", "resolution=merge-duplicates")
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
}
