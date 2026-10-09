package com.marketradar.app

import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Locale
import java.util.TimeZone

/**
 * Evidence batch 1 (Item C): NF/BNF spot provenance from the Upstox v2 full-market-quote response.
 *
 * Clock semantics are kept separate and are never substituted for one another:
 *  - `feed_timestamp_raw` = v2 `timestamp`, documented by Upstox as the time the feed was updated
 *    (https://upstox.com/developer/api-documentation/get-full-market-quote/). Not an exchange time.
 *  - `last_trade_time_raw` = v2 `last_trade_time` ("time in milliseconds at which last trade happened").
 *    For a non-traded index it may be absent or not meaningful.
 *  - `fetch_started_at_ms` / `fetch_completed_at_ms` = device clock around the HTTP fetch.
 *
 * Session phase (NSE): continuous trading 09:15-15:15 for CAS-eligible constituents, Closing Auction
 * Session 15:15-15:35 (live from 2026-08-03, NSE/CMTR/75479), derivatives until 15:40. During CAS the
 * actual index uses constituents' continuous-session LTPs (NSE/CMTR/73362 s.6.5), so an unchanged index
 * with an advancing feed timestamp is FRESH_UNCHANGED, never stale. Phase is derived from the device
 * fetch-completion clock and labelled as such.
 *
 * Evidence only: nothing in ranking, gates, sizing or exits reads this object.
 */
object SpotProvenance {
    const val VERSION = "spot_provenance_v1"
    const val PROVIDER = "UPSTOX"
    const val ENDPOINT = "GET /v2/market-quote/quotes (full market quote)"
    const val ENDPOINT_VERSION = "v2"
    const val TIMESTAMP_MEANING = "upstox_v2_full_quote.timestamp = feed update time (provider-documented); not exchange time"
    const val LTT_MEANING = "upstox_v2_full_quote.last_trade_time = last trade time ms (provider-documented); may be absent for an index"
    /** Feed timestamp older than this, relative to fetch completion, is STALE_SOURCE (declared, evidence-only). */
    const val STALE_FEED_AGE_MS = 180_000L
    const val CAS_EFFECTIVE_DATE = "2026-08-03"

    val INDEX_KEYS = linkedMapOf("NF" to "NSE_INDEX:Nifty 50", "BNF" to "NSE_INDEX:Nifty Bank")

    /** Parse ISO-8601 with offset or an epoch-ms string. Returns null when absent/unparseable. */
    fun parseProviderTime(raw: Any?): Long? {
        val s = when (raw) {
            null, JSONObject.NULL -> return null
            is Number -> return raw.toLong().takeIf { it > 0 }
            else -> raw.toString().trim()
        }
        if (s.isEmpty() || s == "null") return null
        s.toLongOrNull()?.let { return it.takeIf { v -> v > 0 } }
        val patterns = listOf("yyyy-MM-dd'T'HH:mm:ss.SSSXXX", "yyyy-MM-dd'T'HH:mm:ssXXX", "yyyy-MM-dd'T'HH:mm:ss.SSSZ")
        for (p in patterns) {
            try {
                val f = SimpleDateFormat(p, Locale.US).apply { isLenient = false }
                return f.parse(s)?.time
            } catch (_: Exception) { }
        }
        return null
    }

    fun sessionPhase(epochMs: Long, sessionDate: String): String {
        val cal = Calendar.getInstance(TimeZone.getTimeZone("Asia/Kolkata")).apply { timeInMillis = epochMs }
        val m = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE)
        val casLive = sessionDate >= CAS_EFFECTIVE_DATE
        return when {
            m < 9 * 60 + 15 -> "PRE_OPEN"
            m < 15 * 60 + 15 -> "CONTINUOUS"
            casLive && m < 15 * 60 + 35 -> "CAS_15_15_15_35_INDEX_FROZEN_BY_DESIGN"
            !casLive && m < 15 * 60 + 30 -> "CONTINUOUS_PRE_CAS_REGIME"
            m < 15 * 60 + 40 -> "DERIVATIVES_ONLY_TO_15_40"
            else -> "POST_CLOSE"
        }
    }

    /**
     * @param data the `data` object of the quotes response (may be null when the fetch failed).
     * @param previous the previous poll's provenance (same session) for unchanged/repeat detection.
     */
    fun build(
        data: JSONObject?,
        fetchStartedAtMs: Long,
        fetchCompletedAtMs: Long,
        sessionDate: String,
        previous: JSONObject? = null
    ): JSONObject {
        val out = JSONObject()
            .put("version", VERSION)
            .put("provider", PROVIDER)
            .put("endpoint", ENDPOINT)
            .put("endpoint_version", ENDPOINT_VERSION)
            .put("timestamp_meaning", TIMESTAMP_MEANING)
            .put("last_trade_time_meaning", LTT_MEANING)
            .put("fetch_started_at_ms", fetchStartedAtMs)
            .put("fetch_completed_at_ms", fetchCompletedAtMs)
            .put("fetch_clock", "device_local")
            .put("session_date", sessionDate)
            .put("session_phase", sessionPhase(fetchCompletedAtMs, sessionDate))
            .put("session_phase_clock", "device_fetch_completed")
            .put("stale_feed_age_ms_threshold", STALE_FEED_AGE_MS)
        for ((label, key) in INDEX_KEYS) {
            out.put(label, buildOne(label, key, data, fetchCompletedAtMs, previous?.optJSONObject(label), sessionDate))
        }
        val statuses = INDEX_KEYS.keys.map { out.getJSONObject(it).optString("status") }
        out.put("all_identities_present", statuses.none { it == "IDENTITY_MISSING" })
        return out
    }

    private fun buildOne(label: String, key: String, data: JSONObject?, fetchedAt: Long, prev: JSONObject?, sessionDate: String): JSONObject {
        val o = JSONObject().put("index", label).put("instrument_key", key)
        val q = data?.optJSONObject(key) ?: data?.optJSONObject(key.replace(":", "|"))
        if (q == null) {
            return o.put("status", "IDENTITY_MISSING").put("available", false)
                .put("reason", if (data == null) "quotes_response_missing" else "instrument_key_absent_in_response")
                .put("last_price", JSONObject.NULL)
        }
        val price = q.optDouble("last_price", Double.NaN).takeIf { it.isFinite() && it > 0 }
        val tsRaw = q.opt("timestamp")
        val lttRaw = q.opt("last_trade_time")
        val ts = parseProviderTime(tsRaw)
        val ltt = parseProviderTime(lttRaw)
        o.put("last_price", price ?: JSONObject.NULL)
            .put("feed_timestamp_raw", if (tsRaw == null || tsRaw == JSONObject.NULL) JSONObject.NULL else tsRaw.toString())
            .put("feed_timestamp_ms", ts ?: JSONObject.NULL)
            .put("feed_timestamp_available", ts != null)
            .put("last_trade_time_raw", if (lttRaw == null || lttRaw == JSONObject.NULL) JSONObject.NULL else lttRaw.toString())
            .put("last_trade_time_ms", ltt ?: JSONObject.NULL)
            .put("last_trade_time_available", ltt != null)
            .put("available", price != null)
        if (price == null) return o.put("status", "PRICE_MISSING")
        if (ts == null) {
            // Never substitute fetch time for the missing feed time: freshness is UNKNOWN.
            return o.put("status", "SOURCE_TS_MISSING").put("freshness", "UNKNOWN").put("feed_age_ms", JSONObject.NULL)
        }
        val age = fetchedAt - ts
        o.put("feed_age_ms", age)
        val prevTs = prev?.optLong("feed_timestamp_ms", -1L)?.takeIf { it > 0 }
        val prevPrice = prev?.optDouble("last_price", Double.NaN)?.takeIf { it.isFinite() }
        val unchanged = prevPrice != null && prevPrice == price
        val prevFetchedAt = prev?.optLong("fetched_at_ms", 0L) ?: 0L
        val repeatedFeedTs = prevTs != null && prevTs == ts && (fetchedAt - prevFetchedAt) >= 60_000L
        val inCas = sessionPhase(fetchedAt, sessionDate).startsWith("CAS_")
        val status = when {
            age > STALE_FEED_AGE_MS -> "STALE_SOURCE"
            // Same feed timestamp a poll later: outside CAS the feed did not update (stale cache). During CAS
            // the provider may legitimately not refresh a frozen index, so freshness is only UNKNOWN there.
            repeatedFeedTs && inCas -> "REPEATED_FEED_TS_DURING_CAS"
            repeatedFeedTs -> "STALE_REPEATED_FEED_TS"
            unchanged -> "FRESH_UNCHANGED"
            else -> "FRESH"
        }
        val freshness = when {
            status.startsWith("FRESH") -> "FRESH"
            status == "REPEATED_FEED_TS_DURING_CAS" -> "UNKNOWN"
            else -> "STALE"
        }
        return o.put("status", status).put("freshness", freshness)
            .put("price_unchanged_vs_previous", if (prevPrice == null) JSONObject.NULL else unchanged)
            .put("fetched_at_ms", fetchedAt)
    }
}
