package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
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
        // Needs a usable remote row: with nothing usable the merge is an accurate no-op and
        // deliberately does NOT re-normalise or re-trim local history (see the no-op test).
        val local = JSONArray()
        for (i in 0 until 120) local.put(poll(String.format("%02d:%02d", 9 + i / 60, i % 60)))
        // 09:00..09:59 and 10:00..10:59 locally; the remote row is an EARLIER new identity, so
        // the union is 121 and the oldest 21 are trimmed, leaving 09:20 first.
        val merged = PollHistoryMerge.merge(local, arr(poll("08:59")), session, session, PollHistoryMerge.MAX_ROWS)
        assertEquals(PollHistoryMerge.MAX_ROWS, merged.kept)
        assertEquals(21, merged.trimmed)
        assertEquals("09:20", merged.history.getJSONObject(0).optString("t"))
    }

    @Test
    fun unusableRemoteIsReportedAndKeepsEveryOwnedLocalObservation() {
        // An earlier version returned the raw local array by identity, which looked like a clean
        // no-op but meant the restore planner judged preserve-versus-reset from the array's stale
        // header alone. The merge now reports which local rows validated AND proved ownership, so
        // the planner can tell "nothing to learn" from "this row is independently current".
        val local = arr(poll("09:20"), poll("09:15"))          // deliberately out of order
        for (remote in listOf(JSONArray(), null, arr(JSONObject().put("t", "not-a-time")))) {
            val merged = PollHistoryMerge.merge(local, remote, session)
            assertTrue("no usable remote must be reported as such", merged.remoteUnusable)
            assertEquals("every owned local observation is retained", 2, merged.kept)
            assertEquals(2, merged.localRows)
            assertEquals(0, merged.fromRemote)
            assertEquals(listOf("09:15", "09:20"), times(merged.history))
        }
    }

    @Test
    fun unusableRemoteStillDropsUnownedLocalRowsWithoutReStampingThem() {
        val undated = JSONObject().put("t", "09:20").put("nf", 24000.0)
        val dated = poll("09:25", date = session)
        val merged = PollHistoryMerge.merge(
            arr(undated, dated), JSONArray(), session, localSessionDate = "2026-11-03"
        )
        assertTrue(merged.remoteUnusable)
        assertEquals("only the self-dating row proves it belongs to this session", 1, merged.kept)
        assertEquals(1, merged.unownedLocal)
        assertEquals(2, merged.localRows)
        assertEquals(listOf("09:25"), times(merged.history))
    }

    // ---- session ownership of undated local rows -------------------------------------------

    @Test
    fun undatedLocalRowsAreNotAssumedToOwnTheRequestedSession() {
        // A legacy time-only row whose owning header is yesterday must not be re-stamped onto
        // today, and must not beat a valid remote observation at the same clock time.
        val local = arr(JSONObject().put("t", "09:20").put("nf", 111.0).put("bnf", 222.0))
        val remote = arr(poll("09:20", nf = 24000.0))
        val merged = PollHistoryMerge.merge(local, remote, session, localSessionDate = "2026-11-03")
        assertEquals(1, merged.kept)
        assertEquals(1, merged.unownedLocal)
        assertEquals("the remote observation must own the identity", 24000.0,
            merged.history.getJSONObject(0).optDouble("nf"), 1e-9)
    }

    @Test
    fun undatedLocalRowsAreAdmittedWhenTheirContainerOwnsTheSession() {
        val local = arr(JSONObject().put("t", "09:20").put("nf", 111.0))
        val merged = PollHistoryMerge.merge(local, arr(poll("09:25")), session, localSessionDate = session)
        assertEquals(2, merged.kept)
        assertEquals(0, merged.unownedLocal)
    }

    @Test
    fun selfDatingLocalRowsSurviveAStaleContainerHeader() {
        // An explicitly dated or absolute-stamped row carries its own session, so a stale
        // container header must not discard it.
        val dated = arr(poll("09:20", date = session))
        val absolute = arr(JSONObject().put("poll_ts", "2026-11-04T09:20:00+05:30").put("nf", 1.0))
        for (local in listOf(dated, absolute)) {
            val merged = PollHistoryMerge.merge(local, arr(poll("09:25")), session, localSessionDate = "2026-11-03")
            assertEquals(2, merged.kept)
            assertEquals(0, merged.unownedLocal)
        }
    }

    @Test
    fun ownershipClassification() {
        assertEquals(PollHistoryMerge.Ownership.EXPLICIT,
            PollHistoryMerge.ownershipOf(poll("09:20", date = session), session, false))
        assertEquals(PollHistoryMerge.Ownership.EXPLICIT,
            PollHistoryMerge.ownershipOf(JSONObject().put("poll_ts", "2026-11-04T09:20:00Z"), session, false))
        assertEquals("an epoch number is self-dating", PollHistoryMerge.Ownership.EXPLICIT,
            PollHistoryMerge.ownershipOf(JSONObject().put("poll_ts", 1793764200000L), session, false))
        assertEquals("a clock string in an absolute field is still time-only",
            PollHistoryMerge.Ownership.UNKNOWN,
            PollHistoryMerge.ownershipOf(JSONObject().put("poll_ts", "09:20"), session, false))
        assertEquals(PollHistoryMerge.Ownership.VERIFIED_CONTAINER,
            PollHistoryMerge.ownershipOf(JSONObject().put("t", "09:20"), session, true))
        assertEquals(PollHistoryMerge.Ownership.UNKNOWN,
            PollHistoryMerge.ownershipOf(JSONObject().put("t", "09:20"), session, false))
        assertEquals("a row that resolves to nothing owns nothing",
            PollHistoryMerge.Ownership.UNKNOWN,
            PollHistoryMerge.ownershipOf(JSONObject().put("t", "nope"), session, true))
    }

    // ---- ownership must follow the field that ACTUALLY parsed ------------------------------

    @Test
    fun anUnparsedAbsoluteFieldDoesNotMakeAClockFallbackSelfDating() {
        // The naive absolute value is rejected by the consumer, so resolution genuinely falls
        // back to the container-dependent `t`. Treating the row as self-dating because some
        // field merely *looked* absolute let a previous session's undated row onto today.
        val row = JSONObject().put("poll_ts", "2026-11-03T09:20:00").put("t", "09:20").put("nf", 24000.0)
        val id = PollHistoryMerge.resolveIdentity(row, session)
        assertEquals("t", id!!.field)
        assertFalse("the clock fallback is not an absolute instant", id.absolute)
        assertEquals(PollHistoryMerge.Ownership.UNKNOWN,
            PollHistoryMerge.ownershipOf(row, session, false))
        assertEquals(PollHistoryMerge.Ownership.VERIFIED_CONTAINER,
            PollHistoryMerge.ownershipOf(row, session, true))
    }

    @Test
    fun malformedOutOfRangeAndNaiveAbsoluteFieldsAllFallBackWithoutOwnership() {
        for (bad in listOf("2026-11-03T09:20:00", "not-a-time", "2026-11-04T0920:00+05:30", "2026-11-04")) {
            val row = JSONObject().put("poll_ts", bad).put("t", "09:20").put("nf", 24000.0)
            assertEquals("resolution must fall through $bad to the clock field",
                "t", PollHistoryMerge.resolveIdentity(row, session)?.field)
            assertEquals("yesterday's container owns nothing of today: $bad",
                PollHistoryMerge.Ownership.UNKNOWN, PollHistoryMerge.ownershipOf(row, session, false))
        }
        // Non-positive and boolean numerics also fall through rather than self-dating.
        for (bad in listOf(0, -5, true)) {
            val row = JSONObject().put("poll_ts", bad).put("t", "09:20").put("nf", 24000.0)
            assertEquals(PollHistoryMerge.Ownership.UNKNOWN,
                PollHistoryMerge.ownershipOf(row, session, false))
        }
    }

    @Test
    fun aRowWithAnUnparsedAbsoluteFieldIsExcludedUnderAStaleHeader() {
        // The whole point: this row must not win a conflict against today's remote observation.
        val stale = JSONObject().put("poll_ts", "2026-11-03T09:20:00").put("t", "09:20")
            .put("nf", 111.0).put("bnf", 222.0)
        val merged = PollHistoryMerge.merge(
            arr(stale), arr(poll("09:20", nf = 24000.0)), session, localSessionDate = "2026-11-03"
        )
        assertEquals(1, merged.kept)
        assertEquals(1, merged.unownedLocal)
        assertEquals(24000.0, merged.history.getJSONObject(0).optDouble("nf"), 1e-9)
    }

    // ---- an unusable row must not suppress usable evidence ----------------------------------

    @Test
    fun timeValidLocalRowWithoutAnyUsableSpotDoesNotSuppressUsableRemote() {
        // The real spot consumer (poll_spot) drops a row with no finite>0 NF or BNF value, so
        // letting it hold the identity would delete the only usable observation at that instant.
        val local = arr(JSONObject().put("date", session).put("t", "09:20").put("nf", 0.0))
        val remote = arr(poll("09:20", nf = 24000.0, bnf = 52000.0))
        val merged = PollHistoryMerge.merge(local, remote, session)
        assertEquals(1, merged.kept)
        assertEquals(1, merged.remoteWonUnusable)
        assertEquals(24000.0, merged.history.getJSONObject(0).optDouble("nf"), 1e-9)
        assertEquals(52000.0, merged.history.getJSONObject(0).optDouble("bnf"), 1e-9)
    }

    @Test
    fun aValidPartialLocalRowIsPreservedAgainstAConflictingFullerRemoteRow() {
        // The narrow repair is ONLY about a local row the consumer drops entirely. A local row
        // that serves one index is real evidence the device holds, and must not be overwritten
        // with a different NF value merely because the remote row also carries BNF.
        val local = arr(JSONObject().put("date", session).put("t", "09:20").put("nfSpot", 24000.0))
        val remote = arr(poll("09:20", nf = 24111.0, bnf = 52111.0))
        val merged = PollHistoryMerge.merge(local, remote, session)
        assertEquals(1, merged.kept)
        assertEquals("one-usable local must not be replaced", 0, merged.remoteWonUnusable)
        val row = merged.history.getJSONObject(0)
        assertEquals("the local observation is kept whole", 24000.0, row.optDouble("nfSpot"), 1e-9)
        assertTrue("no remote field may be spliced in", row.isNull("bnf"))
        assertEquals("the conflict is recorded, not silently reconciled", 1, merged.conflicts)
    }

    @Test
    fun onlyAZeroUsableLocalRowIsReplaced() {
        // zero-usable local vs usable remote -> replaced
        val zero = arr(JSONObject().put("date", session).put("t", "09:20").put("nf", 0.0))
        val replaced = PollHistoryMerge.merge(zero, arr(poll("09:20", nf = 24000.0)), session)
        assertEquals(1, replaced.remoteWonUnusable)
        assertEquals(24000.0, replaced.history.getJSONObject(0).optDouble("nf"), 1e-9)

        // zero-usable local vs zero-usable remote -> local still wins, nothing gained
        val bothZero = PollHistoryMerge.merge(
            zero, arr(JSONObject().put("date", session).put("t", "09:20").put("bnf", -1.0)), session
        )
        assertEquals(0, bothZero.remoteWonUnusable)
        assertEquals(1, bothZero.kept)
    }

    @Test
    fun anEquallyUsableLocalRowStillWins() {
        val local = arr(poll("09:20", nf = 24111.0))
        val merged = PollHistoryMerge.merge(local, arr(poll("09:20", nf = 99999.0)), session)
        assertEquals(24111.0, merged.history.getJSONObject(0).optDouble("nf"), 1e-9)
        assertEquals(0, merged.remoteWonUnusable)
    }

    @Test
    fun usableIndexCountContract() {
        assertEquals(2, PollHistoryMerge.usableIndexCount(poll("09:20")))
        assertEquals(0, PollHistoryMerge.usableIndexCount(JSONObject().put("t", "09:20")))
        assertEquals(0, PollHistoryMerge.usableIndexCount(JSONObject().put("nf", 0.0).put("bnf", -1.0)))
        assertEquals(1, PollHistoryMerge.usableIndexCount(JSONObject().put("nf", 24000.0)))
        assertEquals("falls back from nfSpot to nf exactly as poll_spot does",
            1, PollHistoryMerge.usableIndexCount(JSONObject().put("bnfSpot", 52000.0)))
        assertEquals(0, PollHistoryMerge.usableIndexCount(null))
    }

    @Test
    fun assignedPollNumberIsReadOnlyFromValidMetadata() {
        assertEquals(12, PollHistoryMerge.assignedPollNumber(JSONObject().put("poll_number", 12)))
        assertEquals(12, PollHistoryMerge.assignedPollNumber(JSONObject().put("poll_number", "12")))
        assertNull(PollHistoryMerge.assignedPollNumber(JSONObject().put("poll_number", 0)))
        assertNull(PollHistoryMerge.assignedPollNumber(JSONObject().put("poll_number", "x")))
        assertNull(PollHistoryMerge.assignedPollNumber(JSONObject()))
        assertNull(PollHistoryMerge.assignedPollNumber(null))
    }

    @Test
    fun assignedPollNumberRejectsInvalidMetadataInsteadOfTruncatingIt() {
        // An earlier version called Number.toInt() BEFORE validating, so 12.9 silently became 12
        // and an out-of-range double saturated to Int.MAX_VALUE, poisoning the assignment maximum
        // so the next increment would overflow.
        assertNull("a fractional value is not an assignment number",
            PollHistoryMerge.assignedPollNumber(JSONObject().put("poll_number", 12.9)))
        assertNull(PollHistoryMerge.assignedPollNumber(JSONObject().put("poll_number", "12.9")))
        assertNull("must not saturate to Int.MAX_VALUE",
            PollHistoryMerge.assignedPollNumber(JSONObject().put("poll_number", 1e18)))
        assertNull(PollHistoryMerge.assignedPollNumber(JSONObject().put("poll_number", -3.0)))
        assertNull(PollHistoryMerge.assignedPollNumber(JSONObject().put("poll_number", true)))
        // The accepted ceiling leaves room for the `+ 1` a reservation performs.
        assertEquals(PollHistoryMerge.MAX_ASSIGNABLE_POLL_NUMBER,
            PollHistoryMerge.assignedPollNumber(
                JSONObject().put("poll_number", PollHistoryMerge.MAX_ASSIGNABLE_POLL_NUMBER)))
        assertNull(PollHistoryMerge.assignedPollNumber(JSONObject().put("poll_number", Int.MAX_VALUE)))
        assertEquals("the ceiling must not overflow when incremented",
            Int.MAX_VALUE, PollHistoryMerge.MAX_ASSIGNABLE_POLL_NUMBER + 1)
        // An exactly-integral double is still valid metadata.
        assertEquals(7, PollHistoryMerge.assignedPollNumber(JSONObject().put("poll_number", 7.0)))
    }

    @Test
    fun latestClockFallsBackToTheParsedIdentityForAnIsoOnlyRow() {
        val isoOnly = JSONObject().put("poll_ts", "2026-11-04T03:50:00Z").put("nf", 24000.0)
        assertEquals("09:20:00", PollHistoryMerge.latestClock(isoOnly, session))
        assertEquals("a present t is preserved verbatim", "09:20",
            PollHistoryMerge.latestClock(poll("09:20"), session))
        assertNull(PollHistoryMerge.latestClock(JSONObject().put("t", "nope"), session))
    }

    @Test
    fun aJunkClockFieldIsNotPublishedWhenTheIdentityCameFromAnotherField() {
        // Caught by CI run #207: latestClock returned `t` verbatim whenever it was non-empty, so
        // a row with a valid poll_ts and a junk `t` would have published the junk as
        // last_poll_time. The identity is authoritative; `t` is only preferred when it agrees.
        val row = JSONObject().put("poll_ts", "2026-11-04T03:50:00Z").put("t", "nope")
            .put("nf", 24000.0).put("bnf", 52000.0)
        assertEquals("09:20:00", PollHistoryMerge.latestClock(row, session))
        val disagreeing = JSONObject().put("poll_ts", "2026-11-04T03:50:00Z").put("t", "11:11")
        assertEquals("a disagreeing clock field is not published either",
            "09:20:00", PollHistoryMerge.latestClock(disagreeing, session))
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
