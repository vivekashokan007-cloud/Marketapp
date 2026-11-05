package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Whole-state transitions for the bootstrap restore.
 *
 * The first version of this repair was tested with a correct helper and source-string
 * assertions, which could not see the three state-level faults that mattered: an undated local
 * row assumed to own the session, a `poll_count` preserved across a day boundary, and an
 * empty-remote branch deciding from pre-network state. Those are transitions between whole
 * prefs states, so they are tested as such here.
 *
 * Synthetic MOCK DATA.
 */
class PollHistoryRestoreTest {

    private val today = "2026-11-04"
    private val yesterday = "2026-11-03"

    private fun poll(t: String, nf: Double = 24000.0, bnf: Double = 52000.0, number: Int? = null): JSONObject =
        JSONObject().put("date", today).put("t", t).put("nf", nf).put("bnf", bnf)
            .put("nfSpot", nf).put("bnfSpot", bnf)
            .also { if (number != null) it.put("poll_number", number) }

    private fun arr(vararg rows: JSONObject) = JSONArray().also { a -> rows.forEach { a.put(it) } }

    private fun state(
        history: JSONArray = JSONArray(),
        pollCount: Int = 0,
        lastPollDate: String = today,
        assignedDate: String = today,
        assignedNumber: Int = 0,
        latestPollPresent: Boolean = false,
        lastPollTimePresent: Boolean = false
    ) = PollHistoryRestore.LocalState(
        historyJson = history.toString(), pollCount = pollCount, lastPollDate = lastPollDate,
        assignedDate = assignedDate, assignedNumber = assignedNumber,
        latestPollPresent = latestPollPresent, lastPollTimePresent = lastPollTimePresent
    )

    /**
     * Mirrors `MarketWatchService.reservePollNumberForToday()`:
     * `maxOf(committedCount, assignedCount) + 1`, where each prior value counts only when its
     * owning session header is today. The restore must leave state that makes this correct.
     */
    private fun nextPollNumberAfter(plan: PollHistoryRestore.Plan, prior: PollHistoryRestore.LocalState): Int {
        val lastPollDate = plan.lastPollDate ?: prior.lastPollDate
        val pollCount = plan.pollCount ?: prior.pollCount
        val assignedDate = plan.assignedDate ?: prior.assignedDate
        val assignedNumber = plan.assignedNumber ?: prior.assignedNumber
        val committed = if (lastPollDate == today) pollCount else 0
        val assigned = if (assignedDate == today) assignedNumber else 0
        return maxOf(committed, assigned) + 1
    }

    // ---- the counter fault (Codex 3B) --------------------------------------------------------

    @Test
    fun yesterdaysPollCountIsNotPromotedIntoTodaysSession() {
        // Codex's exact transition: yesterday's poll_count=78, today's restore has 3 rows.
        // The old caller wrote last_poll_date=today while preserving 78, so the next poll
        // number became 79 instead of 4.
        val prior = state(
            history = arr(poll("09:15")), pollCount = 78,
            lastPollDate = yesterday, assignedDate = yesterday, assignedNumber = 78
        )
        val plan = PollHistoryRestore.plan(
            prior, arr(poll("09:15"), poll("09:20"), poll("09:25")), today, today
        )
        assertEquals(PollHistoryRestore.Action.MERGE, plan.action)
        assertEquals(3, plan.kept)
        assertEquals("a previous session's count must not be inherited", 3, plan.pollCount)
        assertEquals(3, plan.assignedNumber)
        assertEquals(4, nextPollNumberAfter(plan, prior))
    }

    @Test
    fun todaysOwnCountersStayMonotonic() {
        // The date-scoping must not become a licence to rewind a counter that IS today's.
        val prior = state(pollCount = 9, lastPollDate = today, assignedDate = today, assignedNumber = 9)
        val plan = PollHistoryRestore.plan(prior, arr(poll("09:15"), poll("09:20")), today, today)
        assertEquals(9, plan.pollCount)
        assertEquals(9, plan.assignedNumber)
        assertEquals(10, nextPollNumberAfter(plan, prior))
    }

    @Test
    fun aSparseRestoreCannotReuseAnAlreadyAssignedPollNumber() {
        // Three rows come back, but they are polls 10, 11 and 12. Assigning from the row COUNT
        // would hand out number 4 again, duplicating an identity the session already used.
        val prior = state(lastPollDate = yesterday, assignedDate = yesterday, assignedNumber = 40)
        val plan = PollHistoryRestore.plan(
            prior,
            arr(poll("10:00", number = 10), poll("10:05", number = 11), poll("10:10", number = 12)),
            today, today
        )
        assertEquals(3, plan.kept)
        assertEquals("assigned identity comes from row metadata, not the row count", 12, plan.assignedNumber)
        assertEquals(13, nextPollNumberAfter(plan, prior))
        assertEquals("a counter must not imply captured slots that are absent", 3, plan.pollCount)
    }

    @Test
    fun assignmentMetadataIsNeverInferredWhenAbsent() {
        val prior = state(lastPollDate = yesterday, assignedDate = yesterday, assignedNumber = 40)
        val plan = PollHistoryRestore.plan(prior, arr(poll("10:00"), poll("10:05")), today, today)
        assertEquals(2, plan.assignedNumber)
        assertEquals(2, plan.pollCount)
    }

    // ---- session ownership (Codex 3A) --------------------------------------------------------

    @Test
    fun anUndatedLocalRowFromAnEarlierSessionCannotWinAgainstTodaysRemoteRow() {
        val stale = JSONObject().put("t", "09:20").put("nf", 111.0).put("bnf", 222.0)
        val prior = state(history = arr(stale), pollCount = 1, lastPollDate = yesterday)
        val plan = PollHistoryRestore.plan(prior, arr(poll("09:20", nf = 24000.0)), today, today)
        assertEquals(1, plan.kept)
        assertEquals(1, plan.merge!!.unownedLocal)
        val row = JSONArray(plan.historyJson).getJSONObject(0)
        assertEquals(24000.0, row.optDouble("nf"), 1e-9)
    }

    @Test
    fun anUndatedLocalRowIsKeptWhenTheHeaderSaysItIsTodays() {
        val mine = JSONObject().put("t", "09:20").put("nf", 24111.0).put("bnf", 52111.0)
        val prior = state(history = arr(mine), pollCount = 1, lastPollDate = today)
        val plan = PollHistoryRestore.plan(prior, arr(poll("09:25")), today, today)
        assertEquals(2, plan.kept)
        assertEquals(0, plan.merge!!.unownedLocal)
        assertEquals(24111.0, JSONArray(plan.historyJson).getJSONObject(0).optDouble("nf"), 1e-9)
    }

    @Test
    fun aResponseThatArrivesAfterIstRolloverIsDiscardedEntirely() {
        val prior = state(history = arr(poll("09:15")), pollCount = 1, lastPollDate = today)
        val plan = PollHistoryRestore.plan(prior, arr(poll("09:20")), today, "2026-11-05")
        assertEquals(PollHistoryRestore.Action.ABORT_STALE_SESSION, plan.action)
        assertFalse("nothing may be written for a session that is no longer current", plan.writesHistory)
        assertNull(plan.pollCount)
        assertNull(plan.lastPollDate)
        assertNull(plan.assignedNumber)
    }

    @Test
    fun aBlankSessionIsRefusedRatherThanGuessed() {
        val prior = state()
        assertEquals(PollHistoryRestore.Action.ABORT_STALE_SESSION,
            PollHistoryRestore.plan(prior, arr(poll("09:20")), "", today).action)
        assertEquals(PollHistoryRestore.Action.ABORT_STALE_SESSION,
            PollHistoryRestore.plan(prior, arr(poll("09:20")), today, "").action)
    }

    // ---- empty / invalid remote (Codex 3C) ---------------------------------------------------

    @Test
    fun aPollSavedDuringTheFetchIsNotClearedByAStaleHeader() {
        // The pre-repair reset branch decided from a lastPollDate captured BEFORE the network
        // work. Here savePoll() ran during the fetch, so the CURRENT header is today and the
        // reset must not fire even though the remote copy came back empty.
        val prior = state(
            history = arr(poll("09:40")), pollCount = 1, lastPollDate = today,
            latestPollPresent = true, lastPollTimePresent = true
        )
        for (remote in listOf(JSONArray(), null)) {
            val plan = PollHistoryRestore.plan(prior, remote, today, today)
            assertEquals(PollHistoryRestore.Action.NO_OP, plan.action)
            assertFalse(plan.writesHistory)
        }
    }

    @Test
    fun genuinelyStaleStateIsStillResetWhenNothingUsableComesBack() {
        val prior = state(
            history = arr(JSONObject().put("t", "09:40")), pollCount = 12,
            lastPollDate = yesterday, assignedDate = yesterday, assignedNumber = 12,
            latestPollPresent = true, lastPollTimePresent = true
        )
        val plan = PollHistoryRestore.plan(prior, JSONArray(), today, today)
        assertEquals(PollHistoryRestore.Action.RESET, plan.action)
        assertEquals(today, plan.lastPollDate)
        assertFalse("a reset clears keys; it never writes a history payload", plan.writesHistory)
    }

    @Test
    fun anAllInvalidRemoteCopyIsTreatedAsNothingLearnedNotAsAReplacement() {
        val prior = state(history = arr(poll("09:15"), poll("09:20")), pollCount = 2, lastPollDate = today)
        val remote = arr(
            JSONObject().put("t", "not-a-time"),
            JSONObject().put("poll_ts", "2026-11-04T09:20:00"),    // naive: consumer rejects
            JSONObject().put("date", yesterday).put("t", "09:25")
        )
        val plan = PollHistoryRestore.plan(prior, remote, today, today)
        assertEquals(PollHistoryRestore.Action.NO_OP, plan.action)
        assertFalse(plan.writesHistory)
        assertTrue(plan.merge!!.remoteUnusable)
    }

    @Test
    fun anEmptyLocalHistoryWithNothingUsableRemoteIsAQuietNoOp() {
        val plan = PollHistoryRestore.plan(state(), JSONArray(), today, today)
        assertEquals(PollHistoryRestore.Action.NO_OP, plan.action)
        assertFalse(plan.writesHistory)
    }

    @Test
    fun malformedLocalHistoryJsonDoesNotThrowAndDoesNotBlockTheRestore() {
        val prior = PollHistoryRestore.LocalState(
            historyJson = "{not json", pollCount = 0, lastPollDate = today,
            assignedDate = today, assignedNumber = 0,
            latestPollPresent = false, lastPollTimePresent = false
        )
        val plan = PollHistoryRestore.plan(prior, arr(poll("09:20")), today, today)
        assertEquals(PollHistoryRestore.Action.MERGE, plan.action)
        assertEquals(1, plan.kept)
    }

    // ---- latest pointer (Codex 3B) -----------------------------------------------------------

    @Test
    fun theLatestClockIsDerivedFromTheIdentityForAnIsoOnlyRow() {
        // `latest.optString("t", "")` blanked last_poll_time for a row carrying only poll_ts.
        val isoOnly = JSONObject().put("poll_ts", "2026-11-04T04:00:00Z")
            .put("nf", 24000.0).put("bnf", 52000.0)
        val plan = PollHistoryRestore.plan(state(), arr(isoOnly), today, today)
        assertEquals(PollHistoryRestore.Action.MERGE, plan.action)
        assertEquals("09:30:00", plan.lastPollTime)
        assertTrue(plan.latestPollJson!!.contains("poll_ts"))
    }

    @Test
    fun theLatestPointerIsTheChronologicallyLastRowNotTheLastReceived() {
        val prior = state(history = arr(poll("09:35")), pollCount = 1, lastPollDate = today)
        val plan = PollHistoryRestore.plan(prior, arr(poll("09:15"), poll("09:20")), today, today)
        assertEquals(3, plan.kept)
        assertEquals("09:35", plan.lastPollTime)
    }

    // ---- unusable local vs usable remote (Codex 3C) -------------------------------------------

    @Test
    fun anUnusableLocalRowDoesNotSuppressUsableRemoteEvidenceInTheRestore() {
        val unusable = JSONObject().put("date", today).put("t", "09:20").put("nf", 0.0)
        val prior = state(history = arr(unusable), pollCount = 1, lastPollDate = today)
        val plan = PollHistoryRestore.plan(prior, arr(poll("09:20", nf = 24000.0)), today, today)
        assertEquals(1, plan.kept)
        assertEquals(1, plan.merge!!.remoteWonUnusable)
        assertEquals(24000.0, JSONArray(plan.historyJson).getJSONObject(0).optDouble("nf"), 1e-9)
    }

    // ---- the defect the whole repair exists for ----------------------------------------------

    @Test
    fun aLaggingRemoteCopyNeverShrinksTodaysRetainedHistory() {
        val prior = state(
            history = arr(poll("09:15"), poll("09:20"), poll("09:25"), poll("09:30"), poll("09:35")),
            pollCount = 5, lastPollDate = today, assignedDate = today, assignedNumber = 5
        )
        val plan = PollHistoryRestore.plan(prior, arr(poll("09:15"), poll("09:20"), poll("09:25")), today, today)
        assertEquals(5, plan.kept)
        assertEquals(0, plan.merge!!.fromRemote)
        assertEquals(5, plan.pollCount)
        assertEquals(6, nextPollNumberAfter(plan, prior))
    }

    @Test
    fun genuineGapsAreNeverFilledByTheRestore() {
        val prior = state(history = arr(poll("09:15"), poll("09:20")), pollCount = 2, lastPollDate = today)
        val plan = PollHistoryRestore.plan(prior, arr(poll("10:30")), today, today)
        assertEquals(3, plan.kept)
        val times = JSONArray(plan.historyJson).let { a ->
            (0 until a.length()).map { a.getJSONObject(it).optString("t") }
        }
        assertEquals(listOf("09:15", "09:20", "10:30"), times)
    }
}
