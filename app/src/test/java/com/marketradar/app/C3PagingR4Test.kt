package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * C3 R4 (Codex REJECT follow-up): the local-cache fallback fails closed when
 * the cache was trimmed or its count cannot be matched to the authoritative
 * session snapshot count. Uses the real EvaluationLocalCache append/trim path
 * on temp files, the real strict reader, the real expected-count resolver and
 * the real ledger.
 */
class C3PagingR4Test {
    private val date = "2026-09-24"

    private fun ts(i: Int) = "2026-09-24T%02d:%02d:00+05:30".format(9 + (15 + i * 5) / 60, (15 + i * 5) % 60)

    private fun snapshot(i: Int, withFrame: Boolean = true) = JSONObject()
        .put("id", "S$i").put("session_date", date).put("poll_ts", ts(i)).put("recommendation_id", "R$i")
        .put("context_json", JSONObject().put("pad", "p").apply {
            if (withFrame) put(C3SnapshotPager.FRAME_KEY, JSONObject().put("poll_ts", ts(i)).put("candidate_slices", JSONArray()))
        })

    private fun tempDir(): File = Files.createTempDirectory("c3r4").toFile().apply { deleteOnExit() }

    /** Cache written through the production append+trim path. */
    private fun cacheViaAppendPath(n: Int, dir: File = tempDir()): File {
        val file = File(dir, "brain_snapshots_$date.jsonl")
        val marker = EvaluationLocalCache.trimMarkerFileFor(file)
        for (i in 1..n) {
            assertTrue(EvaluationLocalCache.appendFullSnapshotRow(file, marker, date, snapshot(i)) { _, _ -> })
        }
        return file
    }

    /** Pre-R4 style cache file written directly (no marker), e.g. trimmed by an old build. */
    private fun unmarkedCache(ids: IntRange, frameless: Set<Int> = emptySet()): File {
        val file = File(tempDir(), "brain_snapshots_$date.jsonl")
        file.writeText(ids.joinToString("\n") { snapshot(it, it !in frameless).toString() } + "\n")
        return file
    }

    private fun ledger(count: Int, ids: Int = count): JSONObject = EvaluationRunLedger.newRun(
        sessionDate = date,
        inputManifest = JSONObject()
            .put("snapshot_count", count)
            .put("snapshot_ids", JSONArray((1..ids).map { 1000L + it }))
    )

    private fun prepareMeta(count: Int, source: String? = C3ExpectedCountResolver.SNAPSHOT_SOURCE_REMOTE_COMPLETE) =
        JSONObject().put("session_date", date).put("snapshot_count", count).apply { if (source != null) put("snapshot_source", source) }

    private fun collect(
        file: File,
        expected: C3ExpectedCount,
        trim: C3TrimEvidence = C3LocalCacheTrimMarker.evidence(EvaluationLocalCache.trimMarkerFileFor(file)),
        remote: (String) -> C3PageFetch = { C3PageFetch.Failed(503, "http_503") }
    ): C3CollectOutcome = C3FrameCollector.collect(
        date,
        remoteFetch = { sink -> C3SnapshotPager.fetchAll(date, remote, sink) },
        localFetch = { sink -> C3LocalSnapshotReader.readStrict({ if (file.exists()) file.bufferedReader() else null }, sink) },
        trimEvidence = { trim },
        expectedCount = { expected }
    )

    private fun assertFailedRetryable(outcome: C3CollectOutcome, reasonCode: String) {
        assertTrue("got $outcome", outcome is C3CollectOutcome.LocalRejected)
        assertEquals(reasonCode, (outcome as C3CollectOutcome.LocalRejected).reasonCode)
        val plan = C3FrameCollector.terminalPlan(outcome)!!
        assertEquals("FAILED", plan.c3Phase)
        assertEquals("failed", plan.ledgerState)
        assertEquals(reasonCode, plan.reasonCode)
        assertFalse("no metrics stage", plan.runMetricsStage)
        var run = ledger(78)
        for (name in EvaluationRunLedger.STAGE_ORDER) {
            if (name != "percentile_finalization") run = EvaluationRunLedger.setStage(run, name, "verified")
        }
        run = EvaluationRunLedger.setStage(run, "percentile_finalization", plan.ledgerState, reasonCode = plan.reasonCode, lastError = plan.lastError)
        assertFalse(run.getBoolean("learning_complete"))
        assertEquals("percentile_finalization", EvaluationRunLedger.nextResumableStage(run))
        assertFalse(plan.c3Phase in setOf("DONE", "INELIGIBLE", "SKIPPED_NO_FRAMES"))
    }

    // ---- Required regression -------------------------------------------------

    @Test
    fun regression_78SnapshotSession_unmarked36FrameCache_remoteFails_isCountMismatch_notDone() {
        val expected = C3ExpectedCountResolver.resolve(date, ledger(78), prepareMeta(78))
        assertEquals(C3ExpectedCount.Known(78, C3ExpectedCountResolver.SOURCE), expected)
        val file = unmarkedCache(43..78) // the 36 most recent polls survived an (old, unmarked) trim
        val outcome = collect(file, expected)
        assertFailedRetryable(outcome, C3FrameCollector.REASON_LOCAL_COUNT_MISMATCH)
        outcome as C3CollectOutcome.LocalRejected
        assertEquals(78, outcome.expected)
        assertEquals(36, outcome.localRows)
        assertEquals(36, outcome.localFrames)
        assertTrue(outcome.detail.contains("expected=78 localRows=36 localFrames=36"))
        assertTrue(outcome.detail.contains("remote=FAILED:boundary_fetch_failed"))
    }

    @Test
    fun regression_78SnapshotSession_realTrimPath_remoteFails_isTrimmed_notDone() {
        // 61 appends cross the 60-row cap; production policy trims to 36.
        val file = cacheViaAppendPath(61)
        assertEquals("trim policy unchanged", 36, file.readLines().count { it.isNotBlank() })
        val evidence = C3LocalCacheTrimMarker.evidence(EvaluationLocalCache.trimMarkerFileFor(file))
        assertEquals(C3TrimEvidence.STATUS_TRIMMED, evidence.status)
        assertEquals(25, evidence.droppedRowsTotal)
        assertEquals(61, evidence.maxRowsBeforeTrim)
        assertEquals(1, evidence.trimEvents)
        val outcome = collect(file, C3ExpectedCountResolver.resolve(date, ledger(78), prepareMeta(78)))
        assertFailedRetryable(outcome, C3FrameCollector.REASON_LOCAL_CACHE_TRIMMED)
        assertTrue((outcome as C3CollectOutcome.LocalRejected).detail.contains("droppedRows=25"))
    }

    // ---- Trim marker ----------------------------------------------------------

    @Test
    fun trimMarkerPresent_evenWhenCountHappensToMatch_isRejected() {
        val file = cacheViaAppendPath(61) // 36 retained + marker
        val outcome = collect(file, C3ExpectedCount.Known(36, "coincidence"))
        assertFailedRetryable(outcome, C3FrameCollector.REASON_LOCAL_CACHE_TRIMMED)
    }

    @Test
    fun trimMarkerPersistsAcrossLaterAppends() {
        val dir = tempDir()
        val file = cacheViaAppendPath(61, dir)
        val marker = EvaluationLocalCache.trimMarkerFileFor(file)
        for (i in 62..70) EvaluationLocalCache.appendFullSnapshotRow(file, marker, date, snapshot(i)) { _, _ -> }
        assertEquals(45, file.readLines().count { it.isNotBlank() })
        assertEquals(C3TrimEvidence.STATUS_TRIMMED, C3LocalCacheTrimMarker.evidence(marker).status)
    }

    @Test
    fun unreadableTrimMarker_failsClosedAsTrimmed() {
        val file = unmarkedCache(1..10)
        EvaluationLocalCache.trimMarkerFileFor(file).writeText("{garbage")
        val outcome = collect(file, C3ExpectedCount.Known(10, "t"))
        assertFailedRetryable(outcome, C3FrameCollector.REASON_LOCAL_CACHE_TRIMMED)
        assertTrue((outcome as C3CollectOutcome.LocalRejected).detail.contains("marker=unreadable"))
    }

    @Test
    fun malformedLineDroppedByCompaction_writesMarker() {
        val file = unmarkedCache(1..5)
        file.appendText("{broken-line\n")
        val marker = EvaluationLocalCache.trimMarkerFileFor(file)
        EvaluationLocalCache.appendFullSnapshotRow(file, marker, date, snapshot(6)) { _, _ -> }
        val ev = C3LocalCacheTrimMarker.evidence(marker)
        assertEquals(C3TrimEvidence.STATUS_TRIMMED, ev.status)
        assertEquals(listOf(C3LocalCacheTrimMarker.REASON_MALFORMED_DROPPED), ev.reasons)
        assertEquals(1, ev.droppedRowsTotal)
    }

    @Test
    fun belowCap_noMarkerWritten() {
        val file = cacheViaAppendPath(60)
        assertEquals(60, file.readLines().count { it.isNotBlank() })
        assertFalse(EvaluationLocalCache.trimMarkerFileFor(file).exists())
    }

    // ---- Expected count --------------------------------------------------------

    @Test
    fun noExpectedCount_isRejectedUnverifiable() {
        val file = unmarkedCache(1..36)
        val outcome = collect(file, C3ExpectedCountResolver.resolve(date, null, null))
        assertFailedRetryable(outcome, C3FrameCollector.REASON_LOCAL_COUNT_UNVERIFIABLE)
        assertNull((outcome as C3CollectOutcome.LocalRejected).expected)
        assertTrue(outcome.detail.contains("no_evaluation_ledger"))
    }

    @Test
    fun expectedCountFromLocalFallbackEvaluation_isNotIndependent_unverifiable() {
        val file = unmarkedCache(1..36)
        val exp = C3ExpectedCountResolver.resolve(date, ledger(36), prepareMeta(36, C3ExpectedCountResolver.SNAPSHOT_SOURCE_LOCAL_FALLBACK))
        assertEquals(C3ExpectedCount.Unavailable("evaluation_snapshot_source_local_fallback"), exp)
        assertFailedRetryable(collect(file, exp), C3FrameCollector.REASON_LOCAL_COUNT_UNVERIFIABLE)
    }

    @Test
    fun resolver_failsClosedOnEveryMissingOrInconsistentSource() {
        assertEquals(C3ExpectedCount.Unavailable("evaluation_snapshot_source_unknown"),
            C3ExpectedCountResolver.resolve(date, ledger(78), prepareMeta(78, source = null))) // pre-R4 meta
        assertEquals(C3ExpectedCount.Unavailable("ledger_snapshot_ids_count_mismatch"),
            C3ExpectedCountResolver.resolve(date, ledger(78, ids = 77), prepareMeta(78)))
        assertEquals(C3ExpectedCount.Unavailable("prepare_meta_count_mismatch"),
            C3ExpectedCountResolver.resolve(date, ledger(78), prepareMeta(77)))
        assertEquals(C3ExpectedCount.Unavailable("prepare_meta_missing"),
            C3ExpectedCountResolver.resolve(date, ledger(78), null))
        assertEquals(C3ExpectedCount.Unavailable("ledger_session_mismatch"),
            C3ExpectedCountResolver.resolve("2026-09-23", ledger(78), prepareMeta(78)))
        val noCount = EvaluationRunLedger.newRun(sessionDate = date, inputManifest = JSONObject())
        assertEquals(C3ExpectedCount.Unavailable("ledger_snapshot_count_missing"),
            C3ExpectedCountResolver.resolve(date, noCount, prepareMeta(78)))
    }

    @Test
    fun frameCountBelowExpected_isMismatch() {
        val file = unmarkedCache(1..78, frameless = setOf(40))
        val outcome = collect(file, C3ExpectedCountResolver.resolve(date, ledger(78), prepareMeta(78)))
        assertFailedRetryable(outcome, C3FrameCollector.REASON_LOCAL_COUNT_MISMATCH)
        outcome as C3CollectOutcome.LocalRejected
        assertEquals(78, outcome.localRows)
        assertEquals(77, outcome.localFrames)
    }

    @Test
    fun moreLocalRowsThanExpected_isMismatch() {
        val file = unmarkedCache(1..79)
        assertFailedRetryable(collect(file, C3ExpectedCount.Known(78, "t")), C3FrameCollector.REASON_LOCAL_COUNT_MISMATCH)
    }

    // ---- Acceptance unchanged for a clean full cache --------------------------

    @Test
    fun cleanUntrimmedFullCountCache_isAccepted() {
        val file = cacheViaAppendPath(58) // below the cap, written by the production path, no marker
        val outcome = collect(file, C3ExpectedCountResolver.resolve(date, ledger(58), prepareMeta(58)))
        assertTrue("got $outcome", outcome is C3CollectOutcome.Frames)
        outcome as C3CollectOutcome.Frames
        assertEquals(C3FrameCollector.SOURCE_LOCAL, outcome.source)
        assertEquals(58, outcome.frames.length())
        assertEquals(58, outcome.snapshotCount)
        assertNull(C3FrameCollector.terminalPlan(outcome))
    }

    @Test
    fun remoteCompleteWithFrames_ignoresLocalGuards() {
        var trimAsked = false
        val outcome = C3FrameCollector.collect(
            date,
            remoteFetch = { sink ->
                sink(C3SnapshotPager.compactSnapshot(1L, date, ts(1), JSONObject().put("poll_ts", ts(1))))
                C3PagingResult.Complete(C3SnapshotPager.MODE_NARROW, 1, 1, 1, 10, "short_page", 1L)
            },
            localFetch = { error("local must not be read") },
            trimEvidence = { trimAsked = true; C3TrimEvidence(C3TrimEvidence.STATUS_TRIMMED) },
            expectedCount = { C3ExpectedCount.Unavailable("x") }
        )
        assertTrue(outcome is C3CollectOutcome.Frames)
        assertFalse(trimAsked)
    }

    @Test
    fun noLocalFile_andRemoteEmpty_keepsNoFramesHandling() {
        val missing = File(tempDir(), "brain_snapshots_$date.jsonl")
        val outcome = collect(missing, C3ExpectedCount.Unavailable("x"), remote = { C3PageFetch.Ok("[]") })
        assertTrue("got $outcome", outcome is C3CollectOutcome.NoFrames)
    }
}
