package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone

/** R5: observations are separate from the freshness conclusion; CAS old/repeated feed time is UNKNOWN. */
class EvidenceBatch1R5SpotFreshnessTest {
    private val d = "2026-10-08"
    private fun ist(s: String): Long = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US)
        .apply { timeZone = TimeZone.getTimeZone("Asia/Kolkata") }.parse(s)!!.time
    private fun q(price: Double, ts: Any?) = JSONObject().put("last_price", price).apply { if (ts != null) put("timestamp", ts) }
    private fun data(nf: JSONObject, bnf: JSONObject = nf) = JSONObject().put("NSE_INDEX:Nifty 50", nf).put("NSE_INDEX:Nifty Bank", bnf)

    @Test fun ageOverThreeMinutesDuringCasIsUnknownNotStaleNorFresh() {
        val p = SpotProvenance.build(data(q(22216.0, "2026-10-08T15:15:00.000+05:30")), ist("${d}T15:19:59"), ist("${d}T15:20:00"), d)
        val nf = p.getJSONObject("NF")
        assertEquals("OLD_FEED_TS_DURING_CAS", nf.getString("status"))
        assertEquals("UNKNOWN", nf.getString("freshness"))
        val o = nf.getJSONObject("observations")
        assertEquals(300_000L, o.getLong("feed_age_ms"))
        assertTrue(o.getBoolean("feed_age_exceeds_threshold"))
        assertTrue(o.getBoolean("in_cas_phase"))
        // Same age outside CAS is concluded STALE.
        val c = SpotProvenance.build(data(q(22216.0, "2026-10-08T13:00:00.000+05:30")), ist("${d}T13:04:59"), ist("${d}T13:05:00"), d)
        assertEquals("STALE", c.getJSONObject("NF").getString("freshness"))
    }

    @Test fun repeatedFeedTimestampDuringCasIsUnknownWithObservationRecorded() {
        val p1 = SpotProvenance.build(data(q(22216.0, "2026-10-08T15:16:00.000+05:30")), ist("${d}T15:16:00"), ist("${d}T15:16:01"), d)
        val p2 = SpotProvenance.build(data(q(22216.0, "2026-10-08T15:16:00.000+05:30")), ist("${d}T15:17:30"), ist("${d}T15:17:31"), d, p1)
        val nf = p2.getJSONObject("NF")
        assertEquals("UNKNOWN", nf.getString("freshness"))
        assertTrue(nf.getJSONObject("observations").getBoolean("repeated_feed_timestamp"))
        assertTrue(nf.getJSONObject("observations").getBoolean("price_unchanged_vs_previous"))
    }

    @Test fun genuinelyFreshUnchangedPriceDuringCasIsFresh() {
        val p1 = SpotProvenance.build(data(q(22216.0, "2026-10-08T15:15:54.120+05:30")), ist("${d}T15:15:54"), ist("${d}T15:15:55"), d)
        val p2 = SpotProvenance.build(data(q(22216.0, "2026-10-08T15:20:46.900+05:30")), ist("${d}T15:20:46"), ist("${d}T15:20:47"), d, p1)
        val nf = p2.getJSONObject("NF")
        assertEquals("FRESH_UNCHANGED", nf.getString("status"))
        assertEquals("FRESH", nf.getString("freshness"))
        assertFalse(nf.getJSONObject("observations").getBoolean("repeated_feed_timestamp"))
        assertFalse(nf.getJSONObject("observations").getBoolean("feed_age_exceeds_threshold"))
    }

    @Test fun missingTimeIsUnknownInEveryPhase() {
        for (t in listOf("T12:30:00", "T15:20:00")) {
            val nf = SpotProvenance.build(data(q(22300.0, null)), ist(d + t), ist(d + t) + 1000, d).getJSONObject("NF")
            assertEquals("SOURCE_TS_MISSING", nf.getString("status"))
            assertEquals("UNKNOWN", nf.getString("freshness"))
            assertEquals("ABSENT", nf.getJSONObject("observations").getString("feed_timestamp_state"))
            assertTrue(nf.isNull("feed_age_ms"))
        }
    }

    @Test fun futureAndInvalidTimeAreUnknown() {
        val fut = SpotProvenance.build(data(q(22300.0, "2026-10-08T12:40:00.000+05:30")), ist("${d}T12:30:00"), ist("${d}T12:30:01"), d).getJSONObject("NF")
        assertEquals("SOURCE_TS_IN_FUTURE", fut.getString("status"))
        assertEquals("UNKNOWN", fut.getString("freshness"))
        assertTrue(fut.getJSONObject("observations").getBoolean("feed_timestamp_in_future"))
        val bad = SpotProvenance.build(data(q(22300.0, "not-a-time")), ist("${d}T12:30:00"), ist("${d}T12:30:01"), d).getJSONObject("NF")
        assertEquals("SOURCE_TS_INVALID", bad.getString("status"))
        assertEquals("UNKNOWN", bad.getString("freshness"))
        assertEquals("UNPARSEABLE", bad.getJSONObject("observations").getString("feed_timestamp_state"))
        // small forward skew inside tolerance is not "future"
        val skew = SpotProvenance.build(data(q(22300.0, "2026-10-08T12:30:30.000+05:30")), ist("${d}T12:30:00"), ist("${d}T12:30:01"), d).getJSONObject("NF")
        assertEquals("FRESH", skew.getString("freshness"))
    }

    @Test fun provenanceStaysOutOfTradingDecisions() {
        val roots = listOf(File("src/main"), File("app/src/main")).first { it.isDirectory }
        val readers = roots.walkTopDown().filter { it.isFile && (it.name.endsWith(".kt") || it.name.endsWith(".py")) }
            .filter { !it.path.contains("/tests/") }
            .filter { f -> f.readText().let { it.contains("spot_provenance") || it.contains("SpotProvenance") } }
            .map { it.name }.toSet()
        assertEquals(setOf("SpotProvenance.kt", "MarketWatchService.kt", "paper_menu_evidence.py", "brain.py"), readers)
        val py = File(roots, "python/paper_menu_evidence.py").readText()
        assertEquals(listOf("        'spot_provenance': ctx.get('spot_provenance'),"),
            py.lines().filter { it.contains("spot_provenance") })  // copied into the manifest only
        // brain.py: only the snapshot compactor's pass-through key list (no decision code reads it).
        val brainLines = File(roots, "python/brain.py").readLines().filter { it.contains("spot_provenance") }
        assertEquals(listOf("        'spot_provenance', 'chain_evidence_ref', 'marginShadowSummary',"), brainLines)
    }
}
