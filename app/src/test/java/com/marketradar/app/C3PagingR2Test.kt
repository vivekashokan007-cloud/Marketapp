package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * C3 R2 (Codex REJECT follow-up): fail-closed paging contract and the
 * MarketMLService C3 outcome/ledger mapping. Behavioral tests with a fake
 * page transport (no network, no Android).
 */
class C3PagingR2Test {
    private val date = "2026-09-24"

    private fun frame(i: Int) = JSONObject()
        .put("poll_ts", "2026-09-24T10:%02d:00+05:30".format(i % 60))
        .put("candidate_slices", JSONArray().put(JSONObject().put("k", i)))

    private fun narrowRow(i: Int, withFrame: Boolean = true) = JSONObject()
        .put("id", i.toLong())
        .put("session_date", date)
        .put("poll_ts", "2026-09-24T10:%02d:%02d+05:30".format(i / 60, i % 60))
        .put(C3SnapshotPager.FRAME_KEY, if (withFrame) frame(i) else JSONObject.NULL)

    private fun contextRow(i: Int) = JSONObject()
        .put("id", i.toLong())
        .put("session_date", date)
        .put("poll_ts", "2026-09-24T11:%02d:%02d+05:30".format(i / 60, i % 60))
        .put("context_json", JSONObject().put("big", "x".repeat(64)).put(C3SnapshotPager.FRAME_KEY, frame(i)))

    private fun page(rows: List<JSONObject>) = C3PageFetch.Ok(JSONArray(rows).toString())

    /**
     * Scripted transport: responder(mode, offset, limit) -> response. R3: the
     * pager uses keyset (poll_ts,id) paging; fixture ids equal the row index and
     * poll_ts increases with it, so the virtual offset is `after.id + 1`.
     * The start-of-read boundary query is answered by [boundary].
     */
    private class FakeTransport(
        val boundary: C3PageFetch = C3PageFetch.Ok("[{\"id\":1000000}]"),
        val responder: (mode: String, offset: Int, limit: Int) -> C3PageFetch
    ) {
        val calls = mutableListOf<String>()
        val boundaryCalls = mutableListOf<String>()
        fun fetch(path: String): C3PageFetch {
            if (path.contains("order=id.desc")) {
                boundaryCalls += path
                return boundary
            }
            calls += path
            val mode = if (path.contains("context_json->")) C3SnapshotPager.MODE_NARROW else C3SnapshotPager.MODE_CONTEXT
            val decoded = java.net.URLDecoder.decode(path, "UTF-8")
            val offset = Regex("id\\.gt\\.(\\d+)").find(decoded)?.groupValues?.get(1)?.toInt()?.plus(1) ?: 0
            val limit = Regex("limit=(\\d+)").find(path)!!.groupValues[1].toInt()
            return responder(mode, offset, limit)
        }
    }

    private fun narrowTable(total: Int, override: (Int, Int) -> C3PageFetch? = { _, _ -> null }) =
        FakeTransport { mode, offset, limit ->
            if (mode != C3SnapshotPager.MODE_NARROW) C3PageFetch.Failed(500, "unexpected_context_mode")
            else override(offset, limit) ?: page((offset until minOf(total, offset + limit)).map { narrowRow(it) })
        }

    private fun collectWith(
        transport: FakeTransport,
        localRows: List<JSONObject> = emptyList(),
        narrowPageSize: Int = 3,
        narrowMaxPages: Int = 4
    ): Pair<C3CollectOutcome, MutableList<JSONObject>> {
        val delivered = mutableListOf<JSONObject>()
        val outcome = C3FrameCollector.collect(
            date,
            remoteFetch = { sink ->
                C3SnapshotPager.fetchAll(
                    date, transport::fetch, { delivered += it; sink(it) },
                    narrowPageSize = narrowPageSize, narrowMaxPages = narrowMaxPages,
                    contextPageSize = 1, contextMaxPages = 5
                )
            },
            localFetch = { sink ->
                localRows.forEach(sink)
                if (localRows.isEmpty()) C3LocalReadResult.Empty(C3LocalSnapshotReader.EMPTY_NO_FILE)
                else C3LocalReadResult.Complete(localRows.size, 0, 0)
            },
            // R4: untrimmed cache whose count matches the authoritative expected count.
            trimEvidence = { C3TrimEvidence.ABSENT },
            expectedCount = { C3ExpectedCount.Known(localRows.size, "test") }
        )
        return outcome to delivered
    }

    /** Real ledger run with every non-C3 stage complete, so only C3 decides learning_complete. */
    private fun readyRun(): JSONObject {
        var run = EvaluationRunLedger.newRun(sessionDate = date, inputManifest = JSONObject().put("session_date", date))
        for (name in EvaluationRunLedger.STAGE_ORDER) {
            if (name == "percentile_finalization") continue
            run = EvaluationRunLedger.setStage(run, name, "verified")
        }
        return EvaluationRunLedger.setStage(run, "percentile_finalization", "running")
    }

    /** Apply the service's plan to a real ledger run, mirroring MarketMLService. */
    private fun ledgerAfter(outcome: C3CollectOutcome): JSONObject {
        val run = readyRun()
        val plan = C3FrameCollector.terminalPlan(outcome) ?: return run
        return EvaluationRunLedger.refreshCompletionFlags(
            EvaluationRunLedger.setStage(
                run, "percentile_finalization", plan.ledgerState,
                reasonCode = plan.reasonCode, expectedCount = 0, writtenCount = 0, verifiedCount = 0,
                lastError = plan.lastError
            )
        )
    }

    @Test
    fun ledgerControl_verifiedC3_makesLearningComplete_soFailedAssertionIsMeaningful() {
        val run = EvaluationRunLedger.refreshCompletionFlags(
            EvaluationRunLedger.setStage(readyRun(), "percentile_finalization", "verified")
        )
        assertTrue(run.getBoolean("learning_complete"))
    }

    private fun assertC3FailedRetryable(outcome: C3CollectOutcome, expectedReason: String) {
        assertTrue("expected RemoteFailed, got $outcome", outcome is C3CollectOutcome.RemoteFailed)
        val failed = (outcome as C3CollectOutcome.RemoteFailed).remote
        assertEquals(expectedReason, failed.reason)
        val plan = C3FrameCollector.terminalPlan(outcome)!!
        assertEquals("FAILED", plan.c3Phase)
        assertEquals("failed", plan.ledgerState)
        assertEquals(C3FrameCollector.REASON_REMOTE_READ_FAILED, plan.reasonCode)
        assertTrue(plan.lastError.contains(expectedReason))
        assertFalse("FAILED must not run the metrics (post-C3) stage", plan.runMetricsStage)
        val run = ledgerAfter(outcome)
        val stage = run.getJSONObject("stages").getJSONObject("percentile_finalization")
        assertEquals("failed", stage.getString("state"))
        assertFalse(run.optBoolean("learning_complete", true))
        // Retryable: not a terminal-ok ledger state and resumable for this session.
        assertFalse(stage.getString("state") in setOf("verified", "ineligible"))
        assertFalse(plan.c3Phase in setOf("DONE", "INELIGIBLE"))
        assertEquals("percentile_finalization", resumableFrom(run))
    }

    private fun resumableFrom(run: JSONObject): String? = EvaluationRunLedger.nextResumableStage(run)

    // 1. Page one OK, page two fetch fails -> FAILED, C3 not finalized, learning incomplete.
    @Test
    fun page2FetchFailure_isFailed_discardsPartialFrames_andC3StaysRetryable() {
        val t = narrowTable(10) { offset, _ -> if (offset == 3) C3PageFetch.Failed(503, "http_503") else null }
        val (outcome, delivered) = collectWith(t)
        assertEquals(3, delivered.size) // page 1 was streamed ...
        assertC3FailedRetryable(outcome, C3PagingFailure.PAGE_FETCH_FAILED)
        val failed = (outcome as C3CollectOutcome.RemoteFailed).remote
        assertEquals(1, failed.pageIndex)
        assertEquals(3, failed.discardedDelivered) // ... and discarded, never finalized
        assertEquals(503, failed.httpCode)
        // No mode mixing after a later-page narrow failure.
        assertTrue(t.calls.none { !it.contains("context_json->") })
    }

    @Test
    fun page2FetchFailure_pagerResultIsFailedNotRowCount() {
        val t = narrowTable(10) { offset, _ -> if (offset == 3) C3PageFetch.Failed(null, "SocketTimeoutException") else null }
        val r = C3SnapshotPager.fetchAll(date, t::fetch, {}, narrowPageSize = 3, narrowMaxPages = 4)
        assertTrue(r is C3PagingResult.Failed)
        assertEquals(C3PagingFailure.PAGE_FETCH_FAILED, (r as C3PagingResult.Failed).reason)
        assertEquals(1, r.pageIndex)
    }

    @Test
    fun remoteFailed_withLocalFrames_usesOnlyLocalFrames_existingFallbackSemantics() {
        val t = narrowTable(10) { offset, _ -> if (offset == 3) C3PageFetch.Failed(503) else null }
        val local = listOf(contextRow(100), contextRow(101))
        val (outcome, _) = collectWith(t, localRows = local)
        assertTrue(outcome is C3CollectOutcome.Frames)
        outcome as C3CollectOutcome.Frames
        assertEquals(C3FrameCollector.SOURCE_LOCAL, outcome.source)
        assertEquals(2, outcome.frames.length())
        val ids = (0 until outcome.frames.length()).map { outcome.frames.getJSONObject(it).getString("snapshot_id") }
        assertEquals(listOf("100", "101"), ids) // no remote partial frames mixed in
        assertTrue(outcome.remote is C3PagingResult.Failed)
    }

    // 2. Malformed page data -> FAILED.
    @Test
    fun badJsonOnLaterPage_isFailedParse() {
        val t = narrowTable(10) { offset, _ -> if (offset == 6) C3PageFetch.Ok("[{\"id\":") else null }
        val (outcome, _) = collectWith(t)
        assertC3FailedRetryable(outcome, C3PagingFailure.PAGE_PARSE_FAILED)
        assertEquals(2, (outcome as C3CollectOutcome.RemoteFailed).remote.pageIndex)
    }

    @Test
    fun missingFrameFieldOnLaterPage_isFailedParse_andNothingFromThatPageDelivered() {
        val t = narrowTable(10) { offset, limit ->
            if (offset == 3) page((3 until 3 + limit).map { i -> narrowRow(i).apply { if (i == 4) remove(C3SnapshotPager.FRAME_KEY) } })
            else null
        }
        val (outcome, delivered) = collectWith(t)
        assertC3FailedRetryable(outcome, C3PagingFailure.PAGE_PARSE_FAILED)
        assertEquals(3, delivered.size) // only page 1; malformed page 2 delivered nothing
        assertTrue((outcome as C3CollectOutcome.RemoteFailed).remote.detail.contains("frame_field_missing"))
    }

    @Test
    fun unparseableFrameValue_isFailedParse() {
        val t = narrowTable(2) { offset, _ ->
            if (offset == 0) page(listOf(narrowRow(0), narrowRow(1).put(C3SnapshotPager.FRAME_KEY, "{not json"))) else null
        }
        val (outcome, delivered) = collectWith(t)
        assertEquals(0, delivered.size)
        // page-0 narrow failure retries in context mode; context responder fails -> still FAILED
        assertTrue(outcome is C3CollectOutcome.RemoteFailed)
        val narrow = C3SnapshotPager.pageMode(date, C3SnapshotPager.MODE_NARROW, 3, 4, 1_000_000L, t::fetch) {}
        assertTrue(narrow is C3PagingResult.Failed)
        narrow as C3PagingResult.Failed
        assertEquals(C3PagingFailure.PAGE_PARSE_FAILED, narrow.reason)
        assertTrue(narrow.detail.contains("frame_unparseable"))
    }

    @Test
    fun nonArrayBody_isFailedParse() {
        val t = narrowTable(10) { offset, _ -> if (offset == 3) C3PageFetch.Ok("{\"message\":\"oops\"}") else null }
        val (outcome, _) = collectWith(t)
        assertC3FailedRetryable(outcome, C3PagingFailure.PAGE_PARSE_FAILED)
    }

    @Test
    fun contextMode_unparseableContextJson_isFailedParse() {
        val t = FakeTransport { mode, offset, _ ->
            when {
                mode == C3SnapshotPager.MODE_NARROW -> C3PageFetch.Failed(400, "http_400")
                offset == 0 -> page(listOf(contextRow(0)))
                offset == 1 -> page(listOf(contextRow(1).put("context_json", "{broken")))
                else -> page(emptyList())
            }
        }
        val (outcome, _) = collectWith(t)
        assertC3FailedRetryable(outcome, C3PagingFailure.PAGE_PARSE_FAILED)
        val r = (outcome as C3CollectOutcome.RemoteFailed).remote
        assertEquals(C3SnapshotPager.MODE_CONTEXT, r.mode)
        assertEquals(1, r.pageIndex)
    }

    // 3. Page-limit exhaustion with full pages and no confirmed end.
    @Test
    fun pageLimitExhaustion_isFailedPageLimitUnconfirmed() {
        val t = narrowTable(1000) // always full pages
        val (outcome, delivered) = collectWith(t, narrowPageSize = 3, narrowMaxPages = 4)
        assertEquals(12, delivered.size)
        assertC3FailedRetryable(outcome, C3PagingFailure.PAGE_LIMIT_UNCONFIRMED)
        assertEquals(4, (outcome as C3CollectOutcome.RemoteFailed).remote.pageIndex)
        assertEquals(5, t.calls.size) // 4 pages + 1 limit probe
    }

    @Test
    fun pageLimitExactlyFilled_confirmedByEmptyProbe_isComplete() {
        val t = narrowTable(12)
        val r = C3SnapshotPager.fetchAll(date, t::fetch, {}, narrowPageSize = 3, narrowMaxPages = 4)
        assertTrue("got $r", r is C3PagingResult.Complete)
        r as C3PagingResult.Complete
        assertEquals(12, r.delivered)
        assertEquals("empty_limit_probe", r.confirmedBy)
    }

    @Test
    fun oversizePage_isFailedParse() {
        val t = narrowTable(10) { offset, _ -> if (offset == 0) page((0 until 5).map { narrowRow(it) }) else null }
        val r = C3SnapshotPager.pageMode(date, C3SnapshotPager.MODE_NARROW, 3, 4, 1_000_000L, t::fetch) {}
        assertTrue(r is C3PagingResult.Failed)
        assertEquals(C3PagingFailure.PAGE_PARSE_FAILED, (r as C3PagingResult.Failed).reason)
    }

    // 4. Normal multi-page COMPLETE with a short last page (R1 behavior).
    @Test
    fun multiPageShortLastPage_isComplete_andFramesMatchR1Capture() {
        val t = narrowTable(8)
        val (outcome, delivered) = collectWith(t)
        assertEquals(8, delivered.size)
        assertTrue(outcome is C3CollectOutcome.Frames)
        outcome as C3CollectOutcome.Frames
        assertEquals(C3FrameCollector.SOURCE_REMOTE, outcome.source)
        assertEquals(8, outcome.frames.length())
        assertEquals(8, outcome.candidateSliceCount)
        val remote = outcome.remote as C3PagingResult.Complete
        assertEquals("short_page", remote.confirmedBy)
        assertEquals(3, remote.pagesRead)
        val f0 = outcome.frames.getJSONObject(0)
        assertEquals("0", f0.getString("snapshot_id"))
        assertEquals(date, f0.getString("session_date"))
        assertEquals(narrowRow(0).getString("poll_ts"), f0.getString("poll_ts"))
        // Compact deliveries hold only the frame under context_json.
        val ctx = delivered[0].getJSONObject("context_json")
        assertEquals(setOf(C3SnapshotPager.FRAME_KEY), ctx.keySet())
        assertNull(C3FrameCollector.terminalPlan(outcome))
        assertEquals(3, t.calls.size)
    }

    @Test
    fun narrowRowsWithNullFrame_areSkippedNotFailed() {
        val t = narrowTable(5) { offset, limit ->
            page((offset until minOf(5, offset + limit)).map { narrowRow(it, withFrame = it % 2 == 0) })
        }
        val r = C3SnapshotPager.fetchAll(date, t::fetch, {}, narrowPageSize = 3, narrowMaxPages = 4)
        assertTrue(r is C3PagingResult.Complete)
        assertEquals(3, (r as C3PagingResult.Complete).delivered)
        assertEquals(5, r.rowsSeen)
    }

    @Test
    fun narrowPage0Failure_fallsBackToContextMode_completeWithFrames() {
        val t = FakeTransport { mode, offset, _ ->
            when {
                mode == C3SnapshotPager.MODE_NARROW -> C3PageFetch.Failed(400, "http_400")
                offset < 3 -> page(listOf(contextRow(offset)))
                else -> page(emptyList())
            }
        }
        val (outcome, delivered) = collectWith(t)
        assertTrue(outcome is C3CollectOutcome.Frames)
        assertEquals(3, (outcome as C3CollectOutcome.Frames).frames.length())
        assertEquals(C3SnapshotPager.MODE_CONTEXT, outcome.remote.mode)
        assertEquals("empty_followup_page", (outcome.remote as C3PagingResult.Complete).confirmedBy)
        // full context dropped: only the frame key survives in compacts
        delivered.forEach { assertEquals(setOf(C3SnapshotPager.FRAME_KEY), it.getJSONObject("context_json").keySet()) }
    }

    // 5. EMPTY keeps existing terminal handling (SKIPPED_NO_FRAMES / ineligible NO_C3_FRAMES).
    @Test
    fun empty_keepsExistingNoFramesTerminalHandling() {
        val t = FakeTransport(boundary = page(emptyList())) { _, _, _ -> page(emptyList()) }
        val (outcome, delivered) = collectWith(t)
        assertEquals(0, delivered.size)
        assertTrue(outcome is C3CollectOutcome.NoFrames)
        assertTrue(outcome.remote is C3PagingResult.Empty)
        val plan = C3FrameCollector.terminalPlan(outcome)
        assertNotNull(plan)
        plan!!
        assertEquals("SKIPPED_NO_FRAMES", plan.c3Phase)
        assertEquals("ineligible", plan.ledgerState)
        assertEquals(EvaluationRunLedger.REASON_NO_FRAMES, plan.reasonCode)
        assertTrue(plan.runMetricsStage)
        val stage = ledgerAfter(outcome).getJSONObject("stages").getJSONObject("percentile_finalization")
        assertEquals("ineligible", stage.getString("state"))
        // R3: the boundary query proves the session has no rows; no page calls.
        assertEquals(1, t.boundaryCalls.size)
        assertEquals(0, t.calls.size)
    }

    @Test
    fun empty_withLocalFrames_usesLocalFallback() {
        val t = FakeTransport(boundary = page(emptyList())) { _, _, _ -> page(emptyList()) }
        val (outcome, _) = collectWith(t, localRows = listOf(contextRow(7)))
        assertTrue(outcome is C3CollectOutcome.Frames)
        assertEquals(C3FrameCollector.SOURCE_LOCAL, (outcome as C3CollectOutcome.Frames).source)
    }

    @Test
    fun completeWithOnlyNullFrames_andNoLocal_isNoFramesNotFailed() {
        val t = narrowTable(2) { offset, _ -> if (offset == 0) page(listOf(narrowRow(0, false), narrowRow(1, false))) else null }
        val (outcome, _) = collectWith(t)
        assertTrue(outcome is C3CollectOutcome.NoFrames)
        assertEquals("SKIPPED_NO_FRAMES", C3FrameCollector.terminalPlan(outcome)!!.c3Phase)
    }

    @Test
    fun describe_isPrivacySafe() {
        val t = narrowTable(10) { offset, _ -> if (offset == 3) C3PageFetch.Failed(503, "http_503") else null }
        val r = C3SnapshotPager.fetchAll(date, t::fetch, {}, narrowPageSize = 3, narrowMaxPages = 4)
        val line = C3SnapshotPager.describe(r)
        assertTrue(line.contains("status=FAILED"))
        assertTrue(line.contains("reason=page_fetch_failed"))
        assertTrue(line.contains("pageIndex=1"))
        assertFalse(line.contains("candidate_slices"))
    }
}
