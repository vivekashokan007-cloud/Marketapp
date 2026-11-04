package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Bootstrap must never shrink the retained poll history.
 *
 * The remote copy in app_config is sampled (every third assigned poll, best effort), so it can
 * lag the device. Replacing local history with it can drop same-session observations, and a
 * dropped stretch is unrecoverable because the ER series is rebuilt from the open reference on
 * every poll. These tests pin the merge contract and its safety boundaries.
 *
 * Synthetic MOCK DATA shaped to the real poll row contract (`date` + `t`); not captured market data.
 */
class PollHistoryMergeTest {

    private val session = "2026-11-04"

    private fun poll(t: String, nf: Double = 24000.0, bnf: Double = 52000.0, date: String? = session): JSONObject {
        val o = JSONObject().put("t", t).put("nf", nf).put("bnf", bnf)
            .put("nfSpot", nf).put("bnfSpot", bnf)
        if (date != null) o.put("date", date)
        return o
    }

    private fun arr(vararg rows: JSONObject) = JSONArray().also { a -> rows.forEach { a.put(it) } }
    private fun times(h: JSONArray) = (0 until h.length()).map { h.getJSONObject(it).optString("t") }

    // ---- the defect this repair exists for -------------------------------------------------

    @Test
    fun staleRemoteSubsetNeverShrinksLocalHistory() {
        val local = arr(poll("09:15"), poll("09:20"), poll("09:25"), poll("09:30"), poll("09:35"))
        val remote = arr(poll("09:15"), poll("09:20"), poll("09:25"))   // lagging by two polls
        val merged = PollHistoryMerge.merge(local, remote, session)
        assertEquals(5, merged.kept)
        assertEquals(listOf("09:15", "09:20", "09:25", "09:30", "09:35"), times(merged.history))
        assertEquals(0, merged.fromRemote)
    }

    @Test
    fun complementarySameSessionRowsAreUnioned() {
        val local = arr(poll("09:15"), poll("09:30"), poll("09:35"))
        val remote = arr(poll("09:20"), poll("09:25"), poll("09:30"))
        val merged = PollHistoryMerge.merge(local, remote, session)
        assertEquals(listOf("09:15", "09:20", "09:25", "09:30", "09:35"), times(merged.history))
        assertEquals(2, merged.fromRemote)
        assertEquals(1, merged.conflicts)
    }

    // ---- conflict, identity and fabrication boundaries ------------------------------------

    @Test
    fun localWinsOnConflictAndFieldsAreNeverSpliced() {
        val local = arr(poll("09:20", nf = 24111.0))
        val remote = arr(JSONObject().put("date", session).put("t", "09:20").put("nf", 99999.0).put("extra", "remote"))
        val merged = PollHistoryMerge.merge(local, remote, session)
        assertEquals(1, merged.kept)
        val row = merged.history.getJSONObject(0)
        assertEquals(24111.0, row.optDouble("nf"), 1e-9)
        assertTrue("remote-only field must not be spliced into the local row", row.isNull("extra"))
    }

    @Test
    fun previousDayAndMalformedRowsAreRejected() {
        val local = arr(poll("09:15"))
        val remote = arr(
            poll("09:20", date = "2026-11-03"),            // yesterday
            JSONObject().put("date", session).put("t", "not-a-time"),
            JSONObject().put("date", session),             // no timestamp at all
            poll("09:25")
        )
        val merged = PollHistoryMerge.merge(local, remote, session)
        assertEquals(listOf("09:15", "09:25"), times(merged.history))
        assertEquals(3, merged.rejected)
    }

    @Test
    fun explicitOtherDayIsoStampIsRejected() {
        val local = JSONArray()
        val remote = arr(JSONObject().put("poll_ts", "2026-11-03T09:20:00+05:30").put("t", "09:20"))
        assertEquals(0, PollHistoryMerge.merge(local, remote, session).kept)
    }

    @Test
    fun olderSchemaMissingOptionalFieldsIsStillKept() {
        val local = JSONArray()
        val remote = arr(JSONObject().put("date", session).put("t", "09:20").put("nf", 24000.0))  // no vix/sigma
        val merged = PollHistoryMerge.merge(local, remote, session)
        assertEquals(1, merged.kept)
    }

    @Test
    fun isoPollTsAndTimeOnlyResolveToTheSameIdentity() {
        val local = arr(poll("09:20"))
        val remote = arr(JSONObject().put("poll_ts", "$session" + "T09:20:00+05:30").put("t", "09:20"))
        val merged = PollHistoryMerge.merge(local, remote, session)
        assertEquals("same observation must not be duplicated", 1, merged.kept)
        assertEquals(1, merged.conflicts)
    }

    // ---- ordering, caps, degenerate input --------------------------------------------------

    @Test
    fun outputIsChronologicalNotLexicalArrivalOrder() {
        val local = arr(poll("9:05"), poll("10:00"))        // unpadded hour
        val remote = arr(poll("09:30"))
        val merged = PollHistoryMerge.merge(local, remote, session)
        assertEquals(listOf("9:05", "09:30", "10:00"), times(merged.history))
    }

    @Test
    fun existingHundredRowCapIsPreservedKeepingTheMostRecent() {
        val local = JSONArray()
        for (i in 0 until 120) local.put(poll(String.format("%02d:%02d", 9 + i / 60, i % 60)))
        val merged = PollHistoryMerge.merge(local, JSONArray(), session, PollHistoryMerge.MAX_ROWS)
        assertEquals(PollHistoryMerge.MAX_ROWS, merged.kept)
        assertEquals(20, merged.trimmed)
        assertEquals("09:20", merged.history.getJSONObject(0).optString("t"))
    }

    @Test
    fun unusableRemoteLeavesLocalUnchanged() {
        val local = arr(poll("09:15"), poll("09:20"))
        for (remote in listOf(JSONArray(), null)) {
            val merged = PollHistoryMerge.merge(local, remote, session)
            assertEquals(2, merged.kept)
            assertEquals(listOf("09:15", "09:20"), times(merged.history))
        }
    }

    @Test
    fun emptyLocalStillAcceptsRemote() {
        val merged = PollHistoryMerge.merge(JSONArray(), arr(poll("09:15"), poll("09:20")), session)
        assertEquals(2, merged.kept)
        assertEquals(2, merged.fromRemote)
    }

    @Test
    fun latestReturnsTheMostRecentMergedRow() {
        val merged = PollHistoryMerge.merge(arr(poll("09:35")), arr(poll("09:15")), session)
        assertEquals("09:35", PollHistoryMerge.latest(merged.history)?.optString("t"))
        assertNull(PollHistoryMerge.latest(JSONArray()))
    }

    // ---- the race the lock discipline exists for -------------------------------------------

    @Test
    fun aPollAppendedWhileTheRemoteFetchWasInFlightSurvives() {
        // remote snapshot taken before the fetch; local gained 09:40 while the request ran
        val remoteTakenEarlier = arr(poll("09:15"), poll("09:20"), poll("09:25"))
        val localNow = arr(poll("09:15"), poll("09:20"), poll("09:25"), poll("09:30"), poll("09:35"), poll("09:40"))
        val merged = PollHistoryMerge.merge(localNow, remoteTakenEarlier, session)
        assertEquals(6, merged.kept)
        assertTrue("the in-flight append must not be erased", times(merged.history).contains("09:40"))
        assertEquals("09:40", PollHistoryMerge.latest(merged.history)?.optString("t"))
    }

    // ---- genuine gaps stay genuine ---------------------------------------------------------

    @Test
    fun mergingNeverManufacturesMissingSlots() {
        val local = arr(poll("09:15"), poll("09:20"))
        val remote = arr(poll("10:30"))                      // real hole between 09:20 and 10:30
        val merged = PollHistoryMerge.merge(local, remote, session)
        assertEquals(3, merged.kept)
        assertEquals(listOf("09:15", "09:20", "10:30"), times(merged.history))
    }

    @Test
    fun canonicalKeyContract() {
        assertEquals("09:20:00", PollHistoryMerge.canonicalKey(poll("09:20"), session))
        assertEquals("09:20:00", PollHistoryMerge.canonicalKey(poll("9:20"), session))
        assertEquals("09:20:30", PollHistoryMerge.canonicalKey(poll("09:20:30"), session))
        assertNull(PollHistoryMerge.canonicalKey(poll("09:20", date = "2026-11-03"), session))
        assertNull(PollHistoryMerge.canonicalKey(JSONObject().put("t", "25:00"), session))
        assertNull(PollHistoryMerge.canonicalKey(null, session))
        assertNotNull(PollHistoryMerge.canonicalKey(JSONObject().put("t", "09:20"), session))  // no date field
    }
}
