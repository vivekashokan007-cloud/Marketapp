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

    // ---- the real application seam -----------------------------------------------------------
    //
    // These are the production key names. The plan is applied by PollHistoryRestore.apply -- the
    // same code the service runs -- through a map-backed StateWriter, so removals are represented
    // exactly as a RESET performs them. An earlier helper modelled null plan fields as "reuse the
    // prior value", which does not describe the reset transaction at all.

    private val assignedDateKey = "last_assigned_poll_date"
    private val assignedNumberKey = "last_assigned_poll_number"
    private val coverageDateKey = "coverage_integrity_date"
    private val extraResetKeys = listOf(
        "last_poll_dispatch_slot", "last_successful_poll_slot",
        "coverage_integrity", "coverage_integrity_issue"
    )

    /** A map standing in for SharedPreferences, with the same get-or-default semantics. */
    private class Prefs(val map: MutableMap<String, Any> = LinkedHashMap()) :
        PollHistoryRestore.StateWriter {
        override fun putString(key: String, value: String) { map[key] = value }
        override fun putInt(key: String, value: Int) { map[key] = value }
        override fun remove(key: String) { map.remove(key) }
        fun str(key: String, def: String = "") = (map[key] as? String) ?: def
        fun int(key: String, def: Int = 0) = (map[key] as? Int) ?: def
        fun has(key: String) = map.containsKey(key)
    }

    private fun prefsFrom(local: PollHistoryRestore.LocalState): Prefs {
        val p = Prefs()
        p.map["poll_history"] = local.historyJson ?: "[]"
        p.map["poll_count"] = local.pollCount
        p.map["last_poll_date"] = local.lastPollDate
        p.map[assignedDateKey] = local.assignedDate
        p.map[assignedNumberKey] = local.assignedNumber
        if (local.latestPollPresent) p.map["latest_poll"] = "{}"
        if (local.lastPollTimePresent) p.map["last_poll_time"] = "09:00"
        return p
    }

    /** Runs plan + real application, returning the resulting state. */
    private fun applied(
        local: PollHistoryRestore.LocalState,
        remote: JSONArray?,
        requested: String = today,
        active: String = today
    ): Pair<PollHistoryRestore.Plan, Prefs> {
        val plan = PollHistoryRestore.plan(local, remote, requested, active)
        val prefs = prefsFrom(local)
        PollHistoryRestore.apply(plan, prefs, assignedDateKey, assignedNumberKey, coverageDateKey, extraResetKeys)
        return plan to prefs
    }

    /**
     * `MarketWatchService.reservePollNumberForToday()`, read off the APPLIED state:
     * `maxOf(committedCount, assignedCount) + 1`, each prior value counting only when its owning
     * session header is today.
     */
    private fun nextPollNumber(p: Prefs): Int {
        val committed = if (p.str("last_poll_date") == today) p.int("poll_count") else 0
        val assigned = if (p.str(assignedDateKey) == today) p.int(assignedNumberKey) else 0
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
        val (plan, p) = applied(prior, arr(poll("09:15"), poll("09:20"), poll("09:25")))
        assertEquals(PollHistoryRestore.Action.MERGE, plan.action)
        assertEquals(3, plan.kept)
        assertEquals("a previous session's count must not be inherited", 3, p.int("poll_count"))
        assertEquals(3, p.int(assignedNumberKey))
        assertEquals(today, p.str("last_poll_date"))
        assertEquals(4, nextPollNumber(p))
    }

    @Test
    fun todaysOwnCountersStayMonotonic() {
        // The date-scoping must not become a licence to rewind a counter that IS today's.
        val prior = state(pollCount = 9, lastPollDate = today, assignedDate = today, assignedNumber = 9)
        val (_, p) = applied(prior, arr(poll("09:15"), poll("09:20")))
        assertEquals(9, p.int("poll_count"))
        assertEquals(9, p.int(assignedNumberKey))
        assertEquals(10, nextPollNumber(p))
    }

    @Test
    fun aSparseRestoreCannotReuseAnAlreadyAssignedPollNumber() {
        // Three rows come back, but they are polls 10, 11 and 12. Assigning from the row COUNT
        // would hand out number 4 again, duplicating an identity the session already used.
        val prior = state(lastPollDate = yesterday, assignedDate = yesterday, assignedNumber = 40)
        val (plan, p) = applied(
            prior,
            arr(poll("10:00", number = 10), poll("10:05", number = 11), poll("10:10", number = 12))
        )
        assertEquals(3, plan.kept)
        assertEquals("assigned identity comes from row metadata, not the row count",
            12, p.int(assignedNumberKey))
        assertEquals(13, nextPollNumber(p))
        assertEquals("a counter must not imply captured slots that are absent", 3, p.int("poll_count"))
    }

    @Test
    fun assignmentMetadataIsNeverInferredWhenAbsent() {
        val prior = state(lastPollDate = yesterday, assignedDate = yesterday, assignedNumber = 40)
        val (_, p) = applied(prior, arr(poll("10:00"), poll("10:05")))
        assertEquals(2, p.int(assignedNumberKey))
        assertEquals(2, p.int("poll_count"))
    }

    @Test
    fun invalidAssignmentMetadataIsIgnoredRatherThanTruncatedOrSaturated() {
        // 12.9 must not become 12, and a huge double must not saturate to Int.MAX_VALUE and make
        // the next reservation overflow.
        val prior = state(lastPollDate = yesterday, assignedDate = yesterday, assignedNumber = 0)
        val fractional = arr(
            poll("10:00").put("poll_number", 12.9),
            poll("10:05").put("poll_number", 1e18)
        )
        val (_, p) = applied(prior, fractional)
        assertEquals("invalid metadata contributes nothing", 2, p.int(assignedNumberKey))
        assertEquals(3, nextPollNumber(p))
        assertTrue("the assignment must stay well inside Int range",
            p.int(assignedNumberKey) <= PollHistoryMerge.MAX_ASSIGNABLE_POLL_NUMBER)
    }

    // ---- session ownership (Codex 3A) --------------------------------------------------------

    @Test
    fun anUndatedLocalRowFromAnEarlierSessionCannotWinAgainstTodaysRemoteRow() {
        val stale = JSONObject().put("t", "09:20").put("nf", 111.0).put("bnf", 222.0)
        val prior = state(history = arr(stale), pollCount = 1, lastPollDate = yesterday)
        val (plan, p) = applied(prior, arr(poll("09:20", nf = 24000.0)))
        assertEquals(1, plan.kept)
        assertEquals(1, plan.merge!!.unownedLocal)
        val row = JSONArray(p.str("poll_history")).getJSONObject(0)
        assertEquals(24000.0, row.optDouble("nf"), 1e-9)
    }

    @Test
    fun anUndatedLocalRowIsKeptWhenTheHeaderSaysItIsTodays() {
        val mine = JSONObject().put("t", "09:20").put("nf", 24111.0).put("bnf", 52111.0)
        val prior = state(history = arr(mine), pollCount = 1, lastPollDate = today)
        val (plan, p) = applied(prior, arr(poll("09:25")))
        assertEquals(2, plan.kept)
        assertEquals(0, plan.merge!!.unownedLocal)
        assertEquals(24111.0, JSONArray(p.str("poll_history")).getJSONObject(0).optDouble("nf"), 1e-9)
    }

    @Test
    fun aResponseThatArrivesAfterIstRolloverIsDiscardedEntirely() {
        val prior = state(history = arr(poll("09:15")), pollCount = 1, lastPollDate = today)
        val (plan, p) = applied(prior, arr(poll("09:20")), active = "2026-11-05")
        assertEquals(PollHistoryRestore.Action.ABORT_STALE_SESSION, plan.action)
        assertFalse("nothing may be written for a session that is no longer current", plan.writesHistory)
        assertNull(plan.pollCount)
        assertNull(plan.lastPollDate)
        assertNull(plan.assignedNumber)
        // and the applied state is byte-identical to what it was
        assertEquals(1, JSONArray(p.str("poll_history")).length())
        assertEquals("09:15", JSONArray(p.str("poll_history")).getJSONObject(0).optString("t"))
        assertEquals(today, p.str("last_poll_date"))
        assertEquals(1, p.int("poll_count"))
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
            val (plan, p) = applied(prior, remote)
            assertEquals(PollHistoryRestore.Action.NO_OP, plan.action)
            assertEquals("09:40", JSONArray(p.str("poll_history")).getJSONObject(0).optString("t"))
            assertEquals(1, p.int("poll_count"))
        }
    }

    @Test
    fun aSelfDatedCurrentRowSurvivesAnEmptyRemoteUnderAStaleHeader() {
        // Remote availability must not decide whether independently self-dated local evidence is
        // real. Previously this row was deleted because the header still said yesterday, while
        // the same row survived if any unrelated valid remote row happened to exist.
        val prior = state(
            history = arr(poll("09:40", date = today)), pollCount = 1,
            lastPollDate = yesterday, assignedDate = yesterday, assignedNumber = 5,
            latestPollPresent = true, lastPollTimePresent = true
        )
        for (remote in listOf(JSONArray(), null)) {
            val (plan, p) = applied(prior, remote)
            assertEquals(PollHistoryRestore.Action.MERGE, plan.action)
            assertTrue("the row must still be present", p.has("poll_history"))
            val rows = JSONArray(p.str("poll_history"))
            assertEquals(1, rows.length())
            assertEquals("09:40", rows.getJSONObject(0).optString("t"))
            assertEquals("the header is corrected to the session the evidence proves",
                today, p.str("last_poll_date"))
        }
    }

    @Test
    fun anUndatedRowUnderAStaleHeaderIsStillNotReStampedOntoToday() {
        // The counterpart: preserving self-dated evidence must not become licence to adopt
        // clocks whose owning session is unknown.
        val prior = state(
            history = arr(JSONObject().put("t", "09:40").put("nf", 24000.0)), pollCount = 1,
            lastPollDate = yesterday, assignedDate = yesterday, assignedNumber = 5,
            latestPollPresent = true, lastPollTimePresent = true
        )
        val (plan, p) = applied(prior, JSONArray())
        assertEquals(PollHistoryRestore.Action.RESET, plan.action)
        assertFalse("the unowned clock is not adopted", p.has("poll_history"))
        assertEquals(today, p.str("last_poll_date"))
    }

    @Test
    fun genuinelyStaleStateIsStillResetWhenNothingUsableComesBack() {
        val prior = state(
            history = arr(JSONObject().put("t", "09:40")), pollCount = 12,
            lastPollDate = yesterday, assignedDate = yesterday, assignedNumber = 12,
            latestPollPresent = true, lastPollTimePresent = true
        )
        val (plan, p) = applied(prior, JSONArray())
        assertEquals(PollHistoryRestore.Action.RESET, plan.action)
        assertEquals(today, p.str("last_poll_date"))
        assertFalse(p.has("poll_history"))
        assertFalse(p.has("poll_count"))
        assertFalse(p.has("latest_poll"))
        assertFalse(p.has("last_poll_time"))
        assertFalse("yesterday's assignment is cleared with the rest", p.has(assignedNumberKey))
        assertEquals(1, nextPollNumber(p))
    }

    // ---- an independently current assignment survives a stale-history reset -------------------

    @Test
    fun aCurrentAssignmentIsPreservedWhenStaleHistoryIsReset() {
        // The history header and the assignment header are independent:
        // reservePollNumberForToday() can already have recorded today's number 3 while the
        // retained history still belongs to yesterday. Clearing it would reissue number 3.
        val prior = state(
            history = arr(JSONObject().put("t", "09:40")), pollCount = 12,
            lastPollDate = yesterday,
            assignedDate = today, assignedNumber = 3,
            latestPollPresent = true, lastPollTimePresent = true
        )
        val (plan, p) = applied(prior, JSONArray())
        assertEquals(PollHistoryRestore.Action.RESET, plan.action)
        assertTrue("the current assignment must survive", plan.keepAssignment)
        assertFalse("stale history is still cleared", p.has("poll_history"))
        assertEquals(today, p.str(assignedDateKey))
        assertEquals(3, p.int(assignedNumberKey))
        assertEquals("the pending number must not be reissued", 4, nextPollNumber(p))
    }

    @Test
    fun aStaleAssignmentIsNotPreservedByTheReset() {
        val prior = state(
            history = arr(JSONObject().put("t", "09:40")), pollCount = 12,
            lastPollDate = yesterday, assignedDate = yesterday, assignedNumber = 40,
            latestPollPresent = true
        )
        val (plan, p) = applied(prior, JSONArray())
        assertEquals(PollHistoryRestore.Action.RESET, plan.action)
        assertFalse(plan.keepAssignment)
        assertFalse(p.has(assignedDateKey))
        assertFalse(p.has(assignedNumberKey))
        assertEquals(1, nextPollNumber(p))
    }

    @Test
    fun aResetAlsoClearsSlotAndCoverageKeysItOwns() {
        val prior = state(
            history = arr(JSONObject().put("t", "09:40")), pollCount = 12,
            lastPollDate = yesterday, assignedDate = yesterday, assignedNumber = 12
        )
        val prefs = prefsFrom(prior)
        extraResetKeys.forEach { prefs.map[it] = "stale" }
        prefs.map[coverageDateKey] = yesterday
        val plan = PollHistoryRestore.plan(prior, JSONArray(), today, today)
        PollHistoryRestore.apply(plan, prefs, assignedDateKey, assignedNumberKey, coverageDateKey, extraResetKeys)
        assertEquals(PollHistoryRestore.Action.RESET, plan.action)
        extraResetKeys.forEach { assertFalse("$it must be cleared", prefs.has(it)) }
    }

    @Test
    fun anAllInvalidRemoteCopyIsTreatedAsNothingLearnedNotAsAReplacement() {
        val prior = state(history = arr(poll("09:15"), poll("09:20")), pollCount = 2, lastPollDate = today)
        val remote = arr(
            JSONObject().put("t", "not-a-time"),
            JSONObject().put("poll_ts", "2026-11-04T09:20:00"),    // naive: consumer rejects
            JSONObject().put("date", yesterday).put("t", "09:25")
        )
        val (plan, p) = applied(prior, remote)
        assertEquals(PollHistoryRestore.Action.NO_OP, plan.action)
        assertTrue(plan.merge!!.remoteUnusable)
        assertEquals(2, JSONArray(p.str("poll_history")).length())
    }

    @Test
    fun anEmptyLocalHistoryWithNothingUsableRemoteIsAQuietNoOp() {
        val (plan, p) = applied(state(), JSONArray())
        assertEquals(PollHistoryRestore.Action.NO_OP, plan.action)
        assertEquals("[]", p.str("poll_history"))
    }

    @Test
    fun malformedLocalHistoryJsonDoesNotThrowAndDoesNotBlockTheRestore() {
        val prior = PollHistoryRestore.LocalState(
            historyJson = "{not json", pollCount = 0, lastPollDate = today,
            assignedDate = today, assignedNumber = 0,
            latestPollPresent = false, lastPollTimePresent = false
        )
        val (plan, p) = applied(prior, arr(poll("09:20")))
        assertEquals(PollHistoryRestore.Action.MERGE, plan.action)
        assertEquals(1, plan.kept)
        assertEquals(1, JSONArray(p.str("poll_history")).length())
    }

    // ---- latest pointer (Codex 3B) -----------------------------------------------------------

    @Test
    fun theLatestClockIsDerivedFromTheIdentityForAnIsoOnlyRow() {
        // `latest.optString("t", "")` blanked last_poll_time for a row carrying only poll_ts.
        val isoOnly = JSONObject().put("poll_ts", "2026-11-04T04:00:00Z")
            .put("nf", 24000.0).put("bnf", 52000.0)
        val (plan, p) = applied(state(), arr(isoOnly))
        assertEquals(PollHistoryRestore.Action.MERGE, plan.action)
        assertEquals("09:30:00", p.str("last_poll_time"))
        assertTrue(p.str("latest_poll").contains("poll_ts"))
    }

    @Test
    fun theLatestPointerIsTheChronologicallyLastRowNotTheLastReceived() {
        val prior = state(history = arr(poll("09:35")), pollCount = 1, lastPollDate = today)
        val (plan, p) = applied(prior, arr(poll("09:15"), poll("09:20")))
        assertEquals(3, plan.kept)
        assertEquals("09:35", p.str("last_poll_time"))
    }

    // ---- unusable local vs usable remote (Codex 3C) -------------------------------------------

    @Test
    fun anUnusableLocalRowDoesNotSuppressUsableRemoteEvidenceInTheRestore() {
        val unusable = JSONObject().put("date", today).put("t", "09:20").put("nf", 0.0)
        val prior = state(history = arr(unusable), pollCount = 1, lastPollDate = today)
        val (plan, p) = applied(prior, arr(poll("09:20", nf = 24000.0)))
        assertEquals(1, plan.kept)
        assertEquals(1, plan.merge!!.remoteWonUnusable)
        assertEquals(24000.0, JSONArray(p.str("poll_history")).getJSONObject(0).optDouble("nf"), 1e-9)
    }

    // ---- the defect the whole repair exists for ----------------------------------------------

    @Test
    fun aLaggingRemoteCopyNeverShrinksTodaysRetainedHistory() {
        val prior = state(
            history = arr(poll("09:15"), poll("09:20"), poll("09:25"), poll("09:30"), poll("09:35")),
            pollCount = 5, lastPollDate = today, assignedDate = today, assignedNumber = 5
        )
        val (plan, p) = applied(prior, arr(poll("09:15"), poll("09:20"), poll("09:25")))
        assertEquals(5, plan.kept)
        assertEquals(0, plan.merge!!.fromRemote)
        assertEquals(5, p.int("poll_count"))
        assertEquals(6, nextPollNumber(p))
    }

    @Test
    fun genuineGapsAreNeverFilledByTheRestore() {
        val prior = state(history = arr(poll("09:15"), poll("09:20")), pollCount = 2, lastPollDate = today)
        val (plan, p) = applied(prior, arr(poll("10:30")))
        assertEquals(3, plan.kept)
        val times = JSONArray(p.str("poll_history")).let { a ->
            (0 until a.length()).map { a.getJSONObject(it).optString("t") }
        }
        assertEquals(listOf("09:15", "09:20", "10:30"), times)
    }
}
