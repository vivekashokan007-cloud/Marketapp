package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.time.LocalDate
import java.time.format.DateTimeFormatter
import java.time.format.DateTimeParseException
import java.time.format.ResolverStyle
import java.time.temporal.ChronoUnit

/**
 * Daily India VIX closes for the brain's relative VIX regime.
 *
 * Source: the per-session poll history the app already upserts every poll into
 * `app_config` under `poll_history_<yyyy-MM-dd>`. The old source,
 * `premium_history`, lost its writer on 2026-05-03 and stopped at 2026-06-29,
 * so the brain ranked VIX against Feb-Jun closes from 2026-08-10 onward.
 *
 * A close is the last poll timed 09:15..15:30 IST with a valid VIX. This class
 * only extracts and orders closes; brain.py (`_pc2_vix_daily_history`) owns
 * the acceptance and freshness policy, so the two cannot drift.
 */
object VixDailyHistory {
    const val PREF_KEY = "vix_daily_history"
    const val KEY_PREFIX = "poll_history_"
    const val MARKET_OPEN = "09:15"
    const val CLOSE_CUTOFF = "15:30"
    const val DEFAULT_MAX_DAYS = 60
    /** Extra rows fetched so rejected (incomplete or malformed) days do not shrink the window. */
    const val FETCH_MARGIN_DAYS = 15
    const val SOURCE_LABEL = "app_config.poll_history"

    /** Mirrors the brain: percentile needs at least this many closes. */
    const val IV_PERCENTILE_MIN_ROWS = 30
    /** Coarse display guard only; the brain applies the exact NSE-session rule. */
    const val IV_PERCENTILE_MAX_CALENDAR_AGE_DAYS = 7L

    private val ISO_DATE: DateTimeFormatter =
        DateTimeFormatter.ofPattern("uuuu-MM-dd").withResolverStyle(ResolverStyle.STRICT)
    private val TIME_RE = Regex("""([01]\d|2[0-3]):[0-5]\d""")

    fun parseIsoDate(value: String?): LocalDate? {
        if (value == null || value.length != 10) return null
        return try {
            LocalDate.parse(value, ISO_DATE)
        } catch (_: DateTimeParseException) {
            null
        }
    }

    private fun validVix(obj: JSONObject): Double? {
        if (!obj.has("vix") || obj.isNull("vix")) return null
        val raw = obj.opt("vix")
        if (raw is Boolean) return null
        val value = obj.optDouble("vix", Double.NaN)
        if (value.isNaN() || value.isInfinite() || value <= 0.0 || value >= 100.0) return null
        return value
    }

    /**
     * The close of one session from its poll array, or null when no poll in
     * 09:15..15:30 carries a valid VIX for that date. Ties on time keep the
     * later array element (the most recent write of that slot).
     */
    fun closeFromPolls(date: String, polls: JSONArray): JSONObject? {
        if (parseIsoDate(date) == null) return null
        var bestTime: String? = null
        var bestVix = Double.NaN
        var validPolls = 0
        for (i in 0 until polls.length()) {
            val poll = polls.optJSONObject(i) ?: continue
            val time = poll.optString("t", "").trim()
            if (!TIME_RE.matches(time)) continue
            if (time < MARKET_OPEN || time > CLOSE_CUTOFF) continue
            val pollDate = poll.optString("date", "").trim()
            if (pollDate.isNotEmpty() && pollDate != date) continue
            val vix = validVix(poll) ?: continue
            validPolls += 1
            val currentBest = bestTime
            if (currentBest == null || time >= currentBest) {
                bestTime = time
                bestVix = vix
            }
        }
        val closeTime = bestTime ?: return null
        return JSONObject()
            .put("date", date)
            .put("vix", bestVix)
            .put("t", closeTime)
            .put("polls", validPolls)
            .put("source", SOURCE_LABEL)
    }

    private fun pollsOf(row: JSONObject): JSONArray? {
        row.optJSONArray("value")?.let { return it }
        val text = row.optString("value", "").trim()
        if (!text.startsWith("[")) return null
        return try {
            JSONArray(text)
        } catch (_: Exception) {
            null
        }
    }

    /**
     * Builds the chronological close history (oldest first) from `app_config`
     * rows `{key, value}`. Only sessions strictly before [today] are kept; the
     * newest [maxDays] survive.
     */
    fun fromAppConfigRows(rows: JSONArray, today: String, maxDays: Int = DEFAULT_MAX_DAYS): JSONArray {
        val todayDate = parseIsoDate(today) ?: return JSONArray()
        val byDate = java.util.TreeMap<LocalDate, JSONObject>()
        for (i in 0 until rows.length()) {
            val row = rows.optJSONObject(i) ?: continue
            val key = row.optString("key", "")
            if (!key.startsWith(KEY_PREFIX)) continue
            val dateText = key.substring(KEY_PREFIX.length)
            val date = parseIsoDate(dateText) ?: continue
            if (!date.isBefore(todayDate)) continue
            if (byDate.containsKey(date)) continue
            val polls = pollsOf(row) ?: continue
            val close = closeFromPolls(dateText, polls) ?: continue
            byDate[date] = close
        }
        val ordered = byDate.values.toList()
        val keep = if (maxDays > 0 && ordered.size > maxDays) ordered.subList(ordered.size - maxDays, ordered.size) else ordered
        val out = JSONArray()
        for (close in keep) out.put(close)
        return out
    }

    /** VIX values of a close history, in the same (chronological) order. */
    fun vixValues(history: JSONArray): JSONArray {
        val out = JSONArray()
        for (i in 0 until history.length()) {
            val row = history.optJSONObject(i) ?: continue
            val vix = validVix(row) ?: continue
            out.put(vix)
        }
        return out
    }

    /**
     * Legacy integer IV percentile (share of prior closes strictly below [vix]),
     * or null when the history is too short or too old to mean anything.
     */
    fun ivPercentile(vix: Double, history: JSONArray, today: String): Int? {
        if (vix.isNaN() || vix.isInfinite() || vix <= 0.0) return null
        val todayDate = parseIsoDate(today) ?: return null
        var newest: LocalDate? = null
        val values = ArrayList<Double>()
        for (i in 0 until history.length()) {
            val row = history.optJSONObject(i) ?: continue
            val date = parseIsoDate(row.optString("date", "")) ?: continue
            if (!date.isBefore(todayDate)) continue
            val value = validVix(row) ?: continue
            values.add(value)
            val currentNewest = newest
            if (currentNewest == null || date.isAfter(currentNewest)) newest = date
        }
        val newestDate = newest ?: return null
        if (values.size < IV_PERCENTILE_MIN_ROWS) return null
        if (ChronoUnit.DAYS.between(newestDate, todayDate) > IV_PERCENTILE_MAX_CALENDAR_AGE_DAYS) return null
        val window = if (values.size > DEFAULT_MAX_DAYS) values.subList(values.size - DEFAULT_MAX_DAYS, values.size) else values
        val lower = window.count { vix > it }
        return lower * 100 / window.size
    }
}
