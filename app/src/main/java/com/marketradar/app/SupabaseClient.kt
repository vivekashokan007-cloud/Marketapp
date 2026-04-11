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
    private const val URL = "https://fdynxkfxohbnlvayouje.supabase.co"
    private const val ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZkeW54a2Z4b2hibmx2YXlvdWplIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzMwMTc0NjQsImV4cCI6MjA4ODU5MzQ2NH0.1KbzYXtpuzUIDABCz9jKz4VjcuGeuyYOQAHkNLlndRE"

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
                    Log.e(TAG, "Request failed: ${response.code} ${response.message}")
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
     * Reads trades_v2 where status = CLOSED, limit 20
     */
    fun getClosedTrades(): JSONArray {
        val request = getBaseRequest("trades_v2?status=eq.CLOSED&select=*&order=exit_date.desc&limit=20")
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
     * Reads premium_history, order by date desc, limit 5
     */
    fun getPremiumHistory(): JSONArray {
        val request = getBaseRequest("premium_history?select=*&order=date.desc&limit=5")
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
}
