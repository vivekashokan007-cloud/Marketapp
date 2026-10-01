package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class VixDailyHistoryTest {
    private fun poll(t: String, vix: Any?, date: String? = null): JSONObject {
        val o = JSONObject().put("t", t)
        if (vix != null) o.put("vix", vix)
        if (date != null) o.put("date", date)
        return o
    }

    private fun polls(vararg items: JSONObject): JSONArray = JSONArray().apply { items.forEach { put(it) } }

    private fun configRow(date: String, value: Any): JSONObject =
        JSONObject().put("key", "poll_history_$date").put("value", value)

    private fun source(path: String): String = listOf(File(path), File("app/$path"), File("../$path"))
        .first { it.isFile }.readText()

    @Test
    fun closeIsTheLastValidPollAtOrBefore1530() {
        val close = VixDailyHistory.closeFromPolls(
            "2026-09-30",
            polls(
                poll("09:15", 13.69, "2026-09-30"),
                poll("15:25", 13.44, "2026-09-30"),
                poll("15:30", 13.47, "2026-09-30"),
                poll("15:40", 13.51, "2026-09-30"), // post-close poll, ignored
            )
        )
        assertNotNull(close)
        assertEquals("2026-09-30", close!!.getString("date"))
        assertEquals(13.47, close.getDouble("vix"), 0.0)
        assertEquals("15:30", close.getString("t"))
        assertEquals(3, close.getInt("polls"))
        assertEquals(VixDailyHistory.SOURCE_LABEL, close.getString("source"))
    }

    @Test
    fun invalidPollsAreSkipped() {
        val close = VixDailyHistory.closeFromPolls(
            "2026-09-29",
            polls(
                poll("15:20", 13.41),
                poll("15:25", 0.0),
                poll("15:26", -1.0),
                poll("15:27", 150.0),
                poll("15:28", "NaN"),
                poll("15:29", true),
                poll("15:30", null),
                poll("9:30", 99.0),
                poll("24:00", 99.0),
                poll("15:30", 99.0, "2026-09-28"), // wrong session
                poll("09:10", 99.0),                // pre-open
            )
        )
        assertEquals(13.41, close!!.getDouble("vix"), 0.0)
        assertEquals("15:20", close.getString("t"))
        assertEquals(1, close.getInt("polls"))
    }

    @Test
    fun noValidPollMeansNoClose() {
        assertNull(VixDailyHistory.closeFromPolls("2026-09-29", polls(poll("15:40", 13.0))))
        assertNull(VixDailyHistory.closeFromPolls("2026-02-30", polls(poll("15:30", 13.0))))
        assertNull(VixDailyHistory.closeFromPolls("2026-09-29", JSONArray()))
    }

    @Test
    fun equalTimesKeepTheLaterWrite() {
        val close = VixDailyHistory.closeFromPolls("2026-09-29", polls(poll("15:30", 13.30), poll("15:30", 13.34)))
        assertEquals(13.34, close!!.getDouble("vix"), 0.0)
    }

    @Test
    fun historyIsChronologicalPriorOnlyAndWindowed() {
        val rows = JSONArray()
        // Supabase returns key.desc; include today, a future key, junk and duplicates.
        rows.put(configRow("2026-10-02", JSONArray().put(poll("15:30", 20.0))))
        rows.put(configRow("2026-10-01", JSONArray().put(poll("13:25", 15.08))))
        for (day in 30 downTo 1) {
            val date = "2026-09-%02d".format(day)
            rows.put(configRow(date, JSONArray().put(poll("15:30", 10.0 + day / 10.0, date))))
        }
        rows.put(JSONObject().put("key", "morning_inputs").put("value", JSONArray()))
        rows.put(configRow("2026-13-01", JSONArray().put(poll("15:30", 12.0))))
        rows.put(configRow("2026-09-15", JSONArray().put(poll("15:30", 99.0)))) // duplicate key, first wins
        val history = VixDailyHistory.fromAppConfigRows(rows, "2026-10-01", maxDays = 20)
        assertEquals(20, history.length())
        assertEquals("2026-09-11", history.getJSONObject(0).getString("date"))
        assertEquals("2026-09-30", history.getJSONObject(19).getString("date"))
        assertEquals(13.0, history.getJSONObject(19).getDouble("vix"), 1e-9)
        for (i in 0 until history.length()) assertTrue(history.getJSONObject(i).getDouble("vix") < 99.0)
        val values = VixDailyHistory.vixValues(history)
        assertEquals(20, values.length())
        assertEquals(13.0, values.getDouble(19), 1e-9)
    }

    @Test
    fun valueStoredAsJsonTextIsAccepted() {
        val rows = JSONArray().put(configRow("2026-09-30", "[{\"t\":\"15:30\",\"vix\":13.47}]"))
        val history = VixDailyHistory.fromAppConfigRows(rows, "2026-10-01")
        assertEquals(1, history.length())
        assertEquals(13.47, history.getJSONObject(0).getDouble("vix"), 0.0)
        assertEquals(0, VixDailyHistory.fromAppConfigRows(JSONArray().put(configRow("2026-09-30", "garbage")), "2026-10-01").length())
        assertEquals(0, VixDailyHistory.fromAppConfigRows(rows, "not-a-date").length())
    }

    @Test
    fun ivPercentileNeedsEnoughRecentHistory() {
        val history = JSONArray()
        for (i in 0 until 40) {
            history.put(JSONObject().put("date", "2026-08-%02d".format(1 + i % 28)).put("vix", 10.0 + i))
        }
        // Newest date 2026-08-28 is more than 7 calendar days before 2026-10-01.
        assertNull(VixDailyHistory.ivPercentile(30.0, history, "2026-10-01"))
        val recent = JSONArray()
        var day = java.time.LocalDate.parse("2026-08-01")
        var added = 0
        while (added < 40) {
            if (day.dayOfWeek.value <= 5) {
                recent.put(JSONObject().put("date", day.toString()).put("vix", 10.0 + added))
                added++
            }
            day = day.plusDays(1)
        }
        val today = day.toString()
        assertEquals(100, VixDailyHistory.ivPercentile(60.0, recent, today))
        assertEquals(0, VixDailyHistory.ivPercentile(10.0, recent, today))
        assertEquals(50, VixDailyHistory.ivPercentile(30.0, recent, today))
        val short = JSONArray()
        for (i in 0 until 29) short.put(recent.getJSONObject(i))
        assertNull(VixDailyHistory.ivPercentile(30.0, short, recent.getJSONObject(28).getString("date").let {
            java.time.LocalDate.parse(it).plusDays(1).toString()
        }))
        assertNull(VixDailyHistory.ivPercentile(Double.NaN, recent, today))
    }

    @Test
    fun serviceFeedsDatedClosesAndStopsDerivingVixFromPremiumHistory() {
        val service = source("src/main/java/com/marketradar/app/MarketWatchService.kt")
        assertTrue(service.contains("ctxObj.put(\"vixDailyHistory\", vixDailyHist)"))
        assertTrue(service.contains("val vixHist = VixDailyHistory.vixValues(vixDailyHist)"))
        assertFalse(service.contains("val v = premHist.getJSONObject(i).optDouble(\"vix\", 0.0)"))
        assertTrue(service.contains("VixDailyHistory.ivPercentile(vix, hist, todayIstDate())"))
        val bootstrap = service.indexOf("SupabaseClient.getPremiumHistory()")
        assertTrue(bootstrap >= 0 && service.indexOf("refreshVixDailyHistory(today)", bootstrap) > bootstrap)
        assertTrue(service.contains("if (!prefs.contains(VixDailyHistory.PREF_KEY))"))
        assertTrue(service.contains("VIX_DAILY_HISTORY_UNAVAILABLE"))
    }

    @Test
    fun fetchIsPriorSessionOnlyAndDistinguishesFailureFromEmpty() {
        val client = source("src/main/java/com/marketradar/app/SupabaseClient.kt")
        val start = client.indexOf("fun getVixDailyCloseSourceRows(")
        assertTrue(start >= 0)
        val body = client.substring(start, client.indexOf("\n    }\n", start))
        assertTrue(body.contains("\"&key=like.\$prefix*\""))
        assertTrue(body.contains("\"&key=lt.\$prefix\$today\""))
        assertTrue(body.contains("fetchSync(getBaseRequest(path).get().build()) ?: return null"))
    }
}
