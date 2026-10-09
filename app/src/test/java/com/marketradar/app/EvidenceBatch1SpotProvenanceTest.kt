package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone

class EvidenceBatch1SpotProvenanceTest {
    private fun ist(s: String): Long = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US)
        .apply { timeZone = TimeZone.getTimeZone("Asia/Kolkata") }.parse(s)!!.time

    private fun quote(price: Double, ts: Any?, ltt: Any? = null): JSONObject = JSONObject().apply {
        put("last_price", price)
        if (ts != null) put("timestamp", ts)
        if (ltt != null) put("last_trade_time", ltt)
    }

    private fun data(nf: JSONObject?, bnf: JSONObject?) = JSONObject().apply {
        if (nf != null) put("NSE_INDEX:Nifty 50", nf)
        if (bnf != null) put("NSE_INDEX:Nifty Bank", bnf)
    }

    @Test
    fun freshUnchangedSpotDuringCasIsNotStale() {
        val d = "2026-10-08"
        val p1 = SpotProvenance.build(
            data(quote(22216.0, "2026-10-08T15:15:54.120+05:30"), quote(54482.3, "2026-10-08T15:15:54.120+05:30")),
            ist("${d}T15:15:54"), ist("${d}T15:15:55"), d)
        val p2 = SpotProvenance.build(
            data(quote(22216.0, "2026-10-08T15:20:46.900+05:30"), quote(54482.3, "2026-10-08T15:20:46.900+05:30")),
            ist("${d}T15:20:46"), ist("${d}T15:20:47"), d, p1)
        assertEquals("CAS_15_15_15_35_INDEX_FROZEN_BY_DESIGN", p2.getString("session_phase"))
        for (ix in listOf("NF", "BNF")) {
            assertEquals("FRESH_UNCHANGED", p2.getJSONObject(ix).getString("status"))
            assertEquals("FRESH", p2.getJSONObject(ix).getString("freshness"))
            assertEquals(true, p2.getJSONObject(ix).get("price_unchanged_vs_previous"))
        }
    }

    @Test
    fun staleCachedSpotIsFlagged() {
        val d = "2026-10-08"
        // feed timestamp 10 minutes behind fetch completion
        val p = SpotProvenance.build(
            data(quote(22216.0, "2026-10-08T13:00:00.000+05:30"), quote(54482.3, "2026-10-08T13:09:59.000+05:30")),
            ist("${d}T13:09:59"), ist("${d}T13:10:00"), d)
        assertEquals("STALE_SOURCE", p.getJSONObject("NF").getString("status"))
        assertEquals("STALE", p.getJSONObject("NF").getString("freshness"))
        assertEquals("FRESH", p.getJSONObject("BNF").getString("status"))
        // same feed timestamp repeated a poll later outside CAS -> stale cache
        val first = SpotProvenance.build(data(quote(22200.0, "2026-10-08T13:10:00.000+05:30"), quote(54400.0, "2026-10-08T13:10:00.000+05:30")),
            ist("${d}T13:10:00"), ist("${d}T13:10:01"), d)
        val again = SpotProvenance.build(data(quote(22200.0, "2026-10-08T13:10:00.000+05:30"), quote(54410.0, "2026-10-08T13:12:30.000+05:30")),
            ist("${d}T13:12:30"), ist("${d}T13:12:31"), d, first)
        assertEquals("STALE_REPEATED_FEED_TS", again.getJSONObject("NF").getString("status"))
        assertEquals("FRESH", again.getJSONObject("BNF").getString("status"))
        // repeated feed ts during CAS -> unknown, never stale by price alone
        val c1 = SpotProvenance.build(data(quote(22216.0, "2026-10-08T15:16:00.000+05:30"), quote(1.0, "2026-10-08T15:16:00.000+05:30")),
            ist("${d}T15:16:00"), ist("${d}T15:16:01"), d)
        val c2 = SpotProvenance.build(data(quote(22216.0, "2026-10-08T15:16:00.000+05:30"), quote(1.0, "2026-10-08T15:17:30.000+05:30")),
            ist("${d}T15:17:30"), ist("${d}T15:17:31"), d, c1)
        assertEquals("REPEATED_FEED_TS_DURING_CAS", c2.getJSONObject("NF").getString("status"))
        assertEquals("UNKNOWN", c2.getJSONObject("NF").getString("freshness"))
    }

    @Test
    fun missingTimestampsNeverBorrowFetchTime() {
        val d = "2026-10-09"
        val p = SpotProvenance.build(data(quote(22300.0, null, null), quote(54000.0, "", "1697624972130")),
            ist("${d}T12:30:01"), ist("${d}T12:30:02"), d)
        val nf = p.getJSONObject("NF")
        assertEquals("SOURCE_TS_MISSING", nf.getString("status"))
        assertEquals("UNKNOWN", nf.getString("freshness"))
        assertTrue(nf.isNull("feed_timestamp_ms"))
        assertTrue(nf.isNull("feed_age_ms"))
        assertFalse(nf.getBoolean("feed_timestamp_available"))
        val bnf = p.getJSONObject("BNF")
        assertEquals("SOURCE_TS_MISSING", bnf.getString("status"))
        assertEquals(1697624972130L, bnf.getLong("last_trade_time_ms"))   // kept as its own clock, not promoted
        assertNotEquals(p.getLong("fetch_completed_at_ms"), bnf.optLong("feed_timestamp_ms", -1))
    }

    @Test
    fun missingIndexIdentityFailsClosedPerIndex() {
        val d = "2026-10-09"
        val p = SpotProvenance.build(data(quote(22300.0, "2026-10-09T12:30:01.000+05:30"), null),
            ist("${d}T12:30:01"), ist("${d}T12:30:02"), d)
        assertEquals("FRESH", p.getJSONObject("NF").getString("status"))
        assertEquals("IDENTITY_MISSING", p.getJSONObject("BNF").getString("status"))
        assertTrue(p.getJSONObject("BNF").isNull("last_price"))
        assertFalse(p.getBoolean("all_identities_present"))
        val none = SpotProvenance.build(null, 1L, 2L, d)
        assertEquals("quotes_response_missing", none.getJSONObject("NF").getString("reason"))
    }

    @Test
    fun sourceAndFetchClocksStaySeparateAndSemanticsDeclared() {
        val d = "2026-10-09"
        val p = SpotProvenance.build(data(quote(22300.0, "2026-10-09T12:30:00.500+05:30", "1760000000000"),
            quote(54000.0, "2026-10-09T12:30:00.500+05:30")), ist("${d}T12:30:01"), ist("${d}T12:30:02"), d)
        val nf = p.getJSONObject("NF")
        assertEquals(ist("${d}T12:30:00") + 500, nf.getLong("feed_timestamp_ms"))
        assertEquals(ist("${d}T12:30:02"), p.getLong("fetch_completed_at_ms"))
        assertEquals(1500L, nf.getLong("feed_age_ms"))
        assertEquals("v2", p.getString("endpoint_version"))
        assertTrue(p.getString("timestamp_meaning").contains("feed update time"))
        assertEquals("CONTINUOUS", p.getString("session_phase"))
        assertEquals("CONTINUOUS_PRE_CAS_REGIME", SpotProvenance.sessionPhase(ist("2026-07-01T15:20:00"), "2026-07-01"))
        assertEquals("DERIVATIVES_ONLY_TO_15_40", SpotProvenance.sessionPhase(ist("2026-10-09T15:37:00"), "2026-10-09"))
    }
}
