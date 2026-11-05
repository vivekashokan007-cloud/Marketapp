package com.marketradar.app

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Caller-boundary guard for the bootstrap poll-history restore.
 *
 * [PollHistoryRestoreTest] owns the *behaviour*: it proves the whole state transition for
 * previous-day counters, sparse assignment metadata, undated local ownership, rollover during
 * the fetch, empty/invalid remote, the ISO-only latest pointer and unusable-vs-usable conflicts.
 * This file only checks that the live path actually consults and applies that plan, which no
 * pure-JVM test can see — there is no Robolectric here, so the wiring is pinned at source level
 * the same way the receiver flag and PyTimeout call sites already are.
 *
 * Deliberately NOT asserted: any particular counter arithmetic. The previous version of this
 * file pinned `maxOf(prefs.getInt("poll_count", 0), merged.kept)` as the definition of
 * correctness — and that very expression was the cross-day counter defect, because it preserved
 * an undated prior count while stamping today's date onto the session header. Arithmetic belongs
 * in the behavioural tests, where a wrong answer fails; a source string only proves a shape.
 */
class PollHistoryBootstrapContractTest {

    private fun src(name: String): String = listOf(
        File("src/main/java/com/marketradar/app/$name"),
        File("app/src/main/java/com/marketradar/app/$name")
    ).first { it.exists() }.readText()

    /**
     * The bootstrap restore region, so assertions cannot be satisfied by unrelated code.
     *
     * Bounded by the next distinct concern rather than a character count: CI run #207 failed
     * because a fixed 4000-char window stopped 532 characters short of the `ABORT_STALE_SESSION`
     * branch, which is a property of the window, not of the code under test.
     */
    private fun bootstrapRegion(s: String): String {
        val from = s.indexOf("SupabaseClient.getPollHistory(today)")
        assertTrue("bootstrap restore not found", from > 0)
        val next = s.indexOf("getContextPercentileDailyHistory", from)
        val to = if (next > from) next else minOf(s.length, from + 8000)
        return s.substring(from, to)
    }

    @Test
    fun bootstrapDecidesAndAppliesTheRestoreThroughTheSharedSeam() {
        val region = bootstrapRegion(src("MarketWatchService.kt"))
        assertTrue(
            "the restore decision must come from PollHistoryRestore.plan, not from inline logic",
            region.contains("PollHistoryRestore.plan(")
        )
        assertTrue(
            "the plan must be applied through the shared seam, so unit tests exercise the real " +
                "transaction including a RESET's removals",
            region.contains("PollHistoryRestore.apply(")
        )
        assertFalse(
            "the raw remote copy must never be written over local history",
            region.contains("putString(\"poll_history\", history.toString())")
        )
        assertFalse(
            "the call site must not re-implement the branch handling it delegates",
            region.contains("PollHistoryRestore.Action.MERGE ->")
        )
    }

    @Test
    fun theCallSitePassesItsOwnKeyNamesToTheSeam() {
        // apply() owns the transaction but not the key names that live in the service.
        val region = bootstrapRegion(src("MarketWatchService.kt"))
        for (arg in listOf(
            "assignedDateKey = LAST_ASSIGNED_POLL_DATE_KEY",
            "assignedNumberKey = LAST_ASSIGNED_POLL_NUMBER_KEY",
            "coverageDateKey = COVERAGE_INTEGRITY_DATE_KEY",
            "extraResetKeys = listOf("
        )) {
            assertTrue("the seam must be given $arg", region.contains(arg))
        }
        for (key in listOf(
            "LAST_POLL_DISPATCH_SLOT_KEY",
            "LAST_SUCCESSFUL_POLL_SLOT_KEY",
            "COVERAGE_INTEGRITY_KEY",
            "COVERAGE_INTEGRITY_ISSUE_KEY"
        )) {
            assertTrue("$key must still be cleared by a reset", region.contains(key))
        }
    }

    @Test
    fun theActiveSessionIsRecheckedAtApplyTimeRatherThanTrusted() {
        val region = bootstrapRegion(src("MarketWatchService.kt"))
        assertTrue(
            "the plan must be given the session that is current now, so a response arriving " +
                "after IST rollover can be discarded instead of applied",
            region.contains("activeSession = todayIstDate()")
        )
        assertTrue(
            "the requested session must be passed as such",
            region.contains("requestedSession = today")
        )
    }

    @Test
    fun localStateIsReReadInsideTheLockAfterTheFetch() {
        val s = src("MarketWatchService.kt")
        val at = s.indexOf("PollHistoryRestore.plan(")
        assertTrue("plan invocation not found", at > 0)
        val before = s.substring(maxOf(0, at - 400), at)
        assertTrue(
            "the plan + prefs write must run under pollSaveLock so an in-flight savePoll is not erased",
            before.contains("synchronized(pollSaveLock)")
        )
        val region = bootstrapRegion(s)
        assertTrue(
            "the owning session header must be read from CURRENT state, not captured pre-fetch",
            region.contains("lastPollDate = prefs.getString(\"last_poll_date\", \"\")")
        )
        assertTrue(
            "the local history must be re-read from current state",
            region.contains("historyJson = prefs.getString(\"poll_history\", \"[]\")")
        )
    }

    @Test
    fun remoteFetchStaysOutsideTheLock() {
        val s = src("MarketWatchService.kt")
        val fetch = s.indexOf("SupabaseClient.getPollHistory(today)")
        val lock = s.indexOf("synchronized(pollSaveLock)", fetch)
        assertTrue("remote fetch not found", fetch > 0)
        assertTrue("lock after fetch not found", lock > fetch)
        assertFalse(
            "the network call must not be inside the poll-save lock",
            s.substring(fetch, lock).contains("synchronized(pollSaveLock)")
        )
    }

    @Test
    fun theResetBranchNoLongerDecidesFromPreNetworkState() {
        val region = bootstrapRegion(src("MarketWatchService.kt"))
        // The old branch compared a `lastPollDate` captured before the fetch.
        assertFalse(
            "the reset decision must come from the plan, not from a pre-fetch header comparison",
            region.contains("lastPollDate != today")
        )
    }

    @Test
    fun remoteUpsertCadenceIsUnchangedByThisRepair() {
        // The repair is a non-destructive restore. It deliberately does NOT raise the sampled
        // upload cadence, force a shutdown upload, or add server/storage work as a workaround.
        val s = src("MarketWatchService.kt")
        assertTrue(s.contains("pollCount % 3 == 0"))
        assertFalse(
            "no shutdown-time upload may be introduced by this repair",
            s.contains("upsertPollHistory(todayIstDate(), history)\n") &&
                s.split("upsertPollHistory").size - 1 > 1
        )
    }
}
