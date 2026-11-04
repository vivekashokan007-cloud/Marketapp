package com.marketradar.app

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Caller-boundary contract for the bootstrap poll-history restore.
 *
 * [PollHistoryMergeTest] proves the merge helper is correct, but a correct helper that the live
 * path never calls is the exact fault class this work exists to prevent. There is no Robolectric
 * in this project, so the bootstrap invocation is pinned at source level in the same way the
 * receiver flag and PyTimeout call sites are (see [H1M1SourceContractTest]).
 *
 * Deleting the merge invocation, or going back to replacing local history with the remote copy,
 * must fail here.
 */
class PollHistoryBootstrapContractTest {

    private fun src(name: String): String = listOf(
        File("src/main/java/com/marketradar/app/$name"),
        File("app/src/main/java/com/marketradar/app/$name")
    ).first { it.exists() }.readText()

    /**
     * The bootstrap restore region only. savePoll() legitimately writes
     * `poll_history` from its own local `history` variable, so these assertions must be
     * scoped rather than applied to the whole file.
     */
    private fun bootstrapRegion(s: String): String {
        val from = s.indexOf("SupabaseClient.getPollHistory(today)")
        assertTrue("bootstrap restore not found", from > 0)
        return s.substring(from, minOf(s.length, from + 2000))
    }

    @Test
    fun bootstrapMergesRemoteHistoryInsteadOfReplacingIt() {
        val s = src("MarketWatchService.kt")
        assertTrue(
            "bootstrap must merge the remote copy into retained local history",
            s.contains("PollHistoryMerge.merge(localHistory, history, today)")
        )
        assertFalse(
            "bootstrap must not overwrite local history with the raw remote copy",
            bootstrapRegion(s).contains("putString(\"poll_history\", history.toString())")
        )
        assertTrue(
            "bootstrap must write the merged history",
            bootstrapRegion(s).contains("putString(\"poll_history\", merged.history.toString())")
        )
    }

    @Test
    fun mergeAndWriteHappenUnderTheSameLockAsSavePoll() {
        val s = src("MarketWatchService.kt")
        val at = s.indexOf("PollHistoryMerge.merge(")
        assertTrue("merge invocation not found", at > 0)
        val before = s.substring(maxOf(0, at - 600), at)
        assertTrue(
            "the merge + prefs write must run under pollSaveLock so an in-flight savePoll is not erased",
            before.contains("synchronized(pollSaveLock)")
        )
        assertTrue(
            "local history must be re-read inside the lock, after the network fetch",
            before.contains("prefs.getString(\"poll_history\"")
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
    fun assignmentCountersAreNotRewoundToTheMergedRowCount() {
        val s = src("MarketWatchService.kt")
        assertTrue(s.contains("maxOf(prefs.getInt(\"poll_count\", 0), merged.kept)"))
        assertTrue(s.contains("maxOf(priorAssigned, merged.kept)"))
        assertFalse(
            "counters must never be set directly from the restored row count",
            s.contains("putInt(LAST_ASSIGNED_POLL_NUMBER_KEY, history.length())")
        )
        assertFalse(s.contains("putInt(\"poll_count\", history.length())"))
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
