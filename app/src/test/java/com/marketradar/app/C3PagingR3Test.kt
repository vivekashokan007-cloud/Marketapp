package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.BufferedReader
import java.io.IOException
import java.io.Reader
import java.io.StringReader
import java.net.URLDecoder
import java.time.OffsetDateTime

/**
 * C3 R3 (Codex REJECT follow-up): stable keyset paging with a fixed read
 * boundary, and strict local-cache completeness. Uses an in-memory PostgREST
 * emulator that really evaluates `id=lte`, the keyset `or=` filter,
 * `order=poll_ts.asc,id.asc` and `limit`, so tie/insert scenarios are real.
 */
class C3PagingR3Test {
    private val date = "2026-09-24"

    private data class Row(val id: Long, val pollTs: String, val withFrame: Boolean = true)

    /** Minimal PostgREST emulator for ml_brain_snapshots (narrow + context modes). */
    private class FakePostgrest(initial: List<Row>) {
        val table = initial.toMutableList()
        val pagePaths = mutableListOf<String>()
        var boundaryCalls = 0
        /** Hook run before every page request (index of page request). */
        var beforePage: (Int) -> Unit = {}
        /** Optional response tamper for page requests. */
        var tamper: (Int, JSONArray) -> JSONArray = { _, a -> a }

        fun insert(row: Row) { table += row }

        private fun render(r: Row, narrow: Boolean): JSONObject {
            val frame = if (r.withFrame) JSONObject().put("poll_ts", r.pollTs)
                .put("candidate_slices", JSONArray().put(JSONObject().put("k", r.id))) else null
            return JSONObject().put("id", r.id).put("session_date", "2026-09-24").put("poll_ts", r.pollTs).apply {
                if (narrow) put(C3SnapshotPager.FRAME_KEY, frame ?: JSONObject.NULL)
                else put("context_json", JSONObject().put("big", "x".repeat(32)).apply { if (frame != null) put(C3SnapshotPager.FRAME_KEY, frame) })
            }
        }

        private fun instant(ts: String) = OffsetDateTime.parse(ts).toInstant()

        fun fetch(path: String): C3PageFetch {
            // PostgREST would decode '+' as space: the pager must percent-encode.
            val query = path.substringAfter('?')
            val params = query.split('&').associate {
                val k = it.substringBefore('='); k to URLDecoder.decode(it.substringAfter('='), "UTF-8")
            }
            if (params["order"] == "id.desc") {
                boundaryCalls += 1
                val max = table.maxByOrNull { it.id } ?: return C3PageFetch.Ok("[]")
                return C3PageFetch.Ok(JSONArray().put(JSONObject().put("id", max.id)).toString())
            }
            val pageIdx = pagePaths.size
            pagePaths += path
            beforePage(pageIdx)
            // Emulates what PostgREST would do for whatever the pager asks
            // (also offset / poll_ts-only order, so the R2 behaviour can be fault-checked).
            val order = params.getValue("order")
            val maxId = params["id"]?.removePrefix("lte.")?.toLong() ?: Long.MAX_VALUE
            val limit = params.getValue("limit").toInt()
            val narrow = params.getValue("select").contains("context_json->")
            var rows = table.filter { it.id <= maxId }
            params["or"]?.let { expr ->
                val m = Regex("^\\(poll_ts\\.gt\\.\"([^\"]+)\",and\\(poll_ts\\.eq\\.\"([^\"]+)\",id\\.gt\\.(\\d+)\\)\\)$").find(expr)
                    ?: return C3PageFetch.Failed(400, "bad_or_filter")
                val ts = instant(m.groupValues[1]); val id = m.groupValues[3].toLong()
                assertEquals(m.groupValues[1], m.groupValues[2])
                rows = rows.filter { val t = instant(it.pollTs); t > ts || (t == ts && it.id > id) }
            }
            rows = if (order == "poll_ts.asc,id.asc") rows.sortedWith(compareBy<Row>({ instant(it.pollTs) }, { it.id }))
                else {
                    // No unique tie-breaker: tie order is unspecified; emulate a
                    // plan that flips it between requests.
                    val base = if (pageIdx % 2 == 0) rows else rows.reversed()
                    base.sortedWith(compareBy<Row> { instant(it.pollTs) })
                }
            rows = rows.drop(params["offset"]?.toInt() ?: 0).take(limit)
            val arr = JSONArray(); rows.forEach { arr.put(render(it, narrow)) }
            return C3PageFetch.Ok(tamper(pageIdx, arr).toString())
        }
    }

    private fun ts(minute: Int, second: Int = 0) = "2026-09-24T04:%02d:%02d.123456+00:00".format(minute, second)

    private fun fetchAll(fake: FakePostgrest, sink: (JSONObject) -> Unit = {}, pageSize: Int = 3, maxPages: Int = 10) =
        C3SnapshotPager.fetchAll(date, fake::fetch, sink, narrowPageSize = pageSize, narrowMaxPages = maxPages)

    private fun ids(delivered: List<JSONObject>) = delivered.map { it.getLong("id") }

    // ---- Gap 1: stable paging ------------------------------------------------

    @Test
    fun tiedPollTsAcrossPageEdge_noDuplicatesNoSkips() {
        // 7 rows; ids 3,4,5,6 all share one poll_ts and straddle the 3-row page edge.
        val rows = listOf(
            Row(1, ts(0)), Row(2, ts(1)), Row(4, ts(2)), Row(3, ts(2)),
            Row(6, ts(2)), Row(5, ts(2)), Row(7, ts(3))
        )
        val fake = FakePostgrest(rows)
        val delivered = mutableListOf<JSONObject>()
        val r = fetchAll(fake, { delivered += it })
        assertTrue("got $r", r is C3PagingResult.Complete)
        assertEquals(listOf(1L, 2L, 3L, 4L, 5L, 6L, 7L), ids(delivered))
        assertEquals(7, (r as C3PagingResult.Complete).delivered)
        assertEquals(7L, r.boundaryMaxId)
        // No offset paging; page 2 resumes after the (poll_ts, id) cursor of row 3.
        assertTrue(fake.pagePaths.none { it.contains("offset=") })
        assertTrue(fake.pagePaths.all { it.contains("order=poll_ts.asc,id.asc") })
        val second = URLDecoder.decode(fake.pagePaths[1], "UTF-8")
        assertTrue(second, second.contains("(poll_ts.gt.\"${ts(2)}\",and(poll_ts.eq.\"${ts(2)}\",id.gt.3))"))
        assertFalse("'+' in timestamp must be percent-encoded", fake.pagePaths[1].substringAfter("or=").contains("+"))
    }

    @Test
    fun rowInsertedDuringRead_isExcludedByBoundary() {
        val fake = FakePostgrest((1L..7L).map { Row(it, ts(it.toInt())) })
        // A replayed/late snapshot lands mid-read with an earlier poll_ts
        // (would shift offset pages) and a newer identity id.
        fake.beforePage = { idx -> if (idx == 1) { fake.insert(Row(8, ts(0, 30))); fake.insert(Row(9, ts(9))) } }
        val delivered = mutableListOf<JSONObject>()
        val r = fetchAll(fake, { delivered += it })
        assertTrue("got $r", r is C3PagingResult.Complete)
        assertEquals((1L..7L).toList(), ids(delivered))
        assertEquals(7L, (r as C3PagingResult.Complete).boundaryMaxId)
        assertEquals(1, fake.boundaryCalls)
        assertTrue(fake.pagePaths.all { it.contains("&id=lte.7&") })
    }

    @Test
    fun rowAboveBoundaryReturnedByServer_isFailedBoundaryViolation() {
        val fake = FakePostgrest((1L..5L).map { Row(it, ts(it.toInt())) })
        fake.tamper = { idx, arr ->
            if (idx == 1) arr.put(JSONObject(arr.getJSONObject(0).toString()).put("id", 99).put("poll_ts", ts(59))) else arr
        }
        val r = fetchAll(fake, pageSize = 3)
        assertTrue("got $r", r is C3PagingResult.Failed)
        assertEquals(C3PagingFailure.PAGE_BOUNDARY_VIOLATION, (r as C3PagingResult.Failed).reason)
    }

    @Test
    fun duplicateKeyAcrossPages_isFailedKeyOrderViolation() {
        val fake = FakePostgrest((1L..8L).map { Row(it, ts(it.toInt())) })
        // Page 2 repeats the last row of page 1 (e.g. unstable server paging).
        fake.tamper = { idx, arr ->
            if (idx == 1) JSONArray().put(JSONObject().put("id", 3).put("session_date", date).put("poll_ts", ts(3))
                .put(C3SnapshotPager.FRAME_KEY, JSONObject.NULL)).also { out -> for (i in 0 until arr.length() - 1) out.put(arr.get(i)) }
            else arr
        }
        val (outcome, delivered) = collect(fake)
        assertEquals(3, delivered.size)
        assertTrue("got $outcome", outcome is C3CollectOutcome.RemoteFailed)
        val f = (outcome as C3CollectOutcome.RemoteFailed).remote
        assertEquals(C3PagingFailure.PAGE_KEY_ORDER_VIOLATION, f.reason)
        assertEquals(1, f.pageIndex)
        assertEquals("FAILED", C3FrameCollector.terminalPlan(outcome)!!.c3Phase)
    }

    @Test
    fun outOfOrderKeyAcrossPages_isFailedKeyOrderViolation() {
        val fake = FakePostgrest((1L..8L).map { Row(it, ts(it.toInt())) })
        fake.tamper = { idx, arr ->
            if (idx == 1) { arr.getJSONObject(0).put("poll_ts", ts(0, 10)); arr } else arr // older than page-1 cursor
        }
        val r = fetchAll(fake)
        assertTrue("got $r", r is C3PagingResult.Failed)
        r as C3PagingResult.Failed
        assertEquals(C3PagingFailure.PAGE_KEY_ORDER_VIOLATION, r.reason)
        assertTrue(r.detail.contains("non_increasing_key"))
    }

    @Test
    fun outOfOrderWithinPage_tiedTsDescendingId_isFailed() {
        val fake = FakePostgrest(listOf(Row(1, ts(1)), Row(2, ts(1))))
        fake.tamper = { _, arr -> if (arr.length() == 2) JSONArray().put(arr.get(1)).put(arr.get(0)) else arr }
        val r = C3SnapshotPager.pageMode(date, C3SnapshotPager.MODE_NARROW, 3, 4, 10L, fake::fetch) {}
        assertTrue(r is C3PagingResult.Failed)
        assertEquals(C3PagingFailure.PAGE_KEY_ORDER_VIOLATION, (r as C3PagingResult.Failed).reason)
    }

    @Test
    fun rowMissingIdOrPollTs_isFailedParse() {
        val fake = FakePostgrest((1L..2L).map { Row(it, ts(it.toInt())) })
        fake.tamper = { _, arr -> arr.getJSONObject(1).remove("id"); arr }
        val r = C3SnapshotPager.pageMode(date, C3SnapshotPager.MODE_NARROW, 3, 4, 10L, fake::fetch) {}
        assertTrue(r is C3PagingResult.Failed)
        assertTrue((r as C3PagingResult.Failed).detail.contains("row_id_missing"))
        fake.tamper = { _, arr -> arr.getJSONObject(0).put("poll_ts", "not-a-time"); arr }
        val r2 = C3SnapshotPager.pageMode(date, C3SnapshotPager.MODE_NARROW, 3, 4, 10L, fake::fetch) {}
        assertTrue((r2 as C3PagingResult.Failed).detail.contains("row_poll_ts_invalid"))
    }

    @Test
    fun boundaryFetchFailure_isFailed_noPageCalls() {
        val r = C3SnapshotPager.fetchAll(date, { p ->
            if (p.contains("order=id.desc")) C3PageFetch.Failed(503, "http_503") else error("no page calls expected")
        }, {})
        assertTrue(r is C3PagingResult.Failed)
        r as C3PagingResult.Failed
        assertEquals(C3PagingFailure.BOUNDARY_FETCH_FAILED, r.reason)
        assertEquals(C3SnapshotPager.MODE_BOUNDARY, r.mode)
    }

    @Test
    fun boundaryMalformed_isFailedBoundaryParse() {
        val r = C3SnapshotPager.fetchAll(date, { C3PageFetch.Ok("[{\"id\":\"abc\"}]") }, {})
        assertEquals(C3PagingFailure.BOUNDARY_PARSE_FAILED, (r as C3PagingResult.Failed).reason)
    }

    @Test
    fun pageLimitWithKeysetProbe_stillFailsUnconfirmed() {
        val fake = FakePostgrest((1L..13L).map { Row(it, ts(it.toInt())) })
        val r = fetchAll(fake, pageSize = 3, maxPages = 4)
        assertTrue(r is C3PagingResult.Failed)
        assertEquals(C3PagingFailure.PAGE_LIMIT_UNCONFIRMED, (r as C3PagingResult.Failed).reason)
        val probe = URLDecoder.decode(fake.pagePaths.last(), "UTF-8")
        assertTrue(probe.contains("limit=1") && probe.contains("id.gt.12"))
    }

    // ---- Gap 2: strict local completeness -----------------------------------

    private fun localLine(id: Int, withFrame: Boolean = true): String = JSONObject()
        .put("id", "L$id").put("session_date", date).put("poll_ts", ts(id))
        .put("context_json", JSONObject().put("big", "y".repeat(16)).apply {
            if (withFrame) put(C3SnapshotPager.FRAME_KEY, JSONObject().put("poll_ts", ts(id)).put("candidate_slices", JSONArray()))
        }).toString()

    private fun reader(text: String) = { BufferedReader(StringReader(text)) }

    /** Reader that yields [okText] and then throws an IOException. */
    private fun failingReader(okText: String): () -> BufferedReader = {
        val inner = StringReader(okText)
        BufferedReader(object : Reader() {
            var done = false
            override fun read(cbuf: CharArray, off: Int, len: Int): Int {
                val n = inner.read(cbuf, off, len)
                if (n > 0) return n
                if (!done) { done = true; throw IOException("EIO simulated") }
                return -1
            }
            override fun close() {}
        }, 64)
    }

    private fun collect(
        fake: FakePostgrest? = null,
        remoteEmpty: Boolean = false,
        remoteFail: Boolean = false,
        local: (onRow: (JSONObject) -> Unit) -> C3LocalReadResult = { C3LocalReadResult.Empty(C3LocalSnapshotReader.EMPTY_NO_FILE) },
        // R4: defaults model an untrimmed cache whose count matches the ledger.
        trim: C3TrimEvidence = C3TrimEvidence.ABSENT,
        expected: C3ExpectedCount = C3ExpectedCount.Known(4, "test")
    ): Pair<C3CollectOutcome, MutableList<JSONObject>> {
        val delivered = mutableListOf<JSONObject>()
        val outcome = C3FrameCollector.collect(
            date,
            remoteFetch = { sink ->
                when {
                    fake != null -> fetchAll(fake, { delivered += it; sink(it) })
                    remoteEmpty -> C3SnapshotPager.fetchAll(date, { C3PageFetch.Ok("[]") }, sink)
                    remoteFail -> C3SnapshotPager.fetchAll(date, { C3PageFetch.Failed(503, "http_503") }, sink)
                    else -> error("configure remote")
                }
            },
            localFetch = local,
            trimEvidence = { trim },
            expectedCount = { expected }
        )
        return outcome to delivered
    }

    private fun assertLocalFailedRetryable(outcome: C3CollectOutcome, reason: String) {
        assertTrue("got $outcome", outcome is C3CollectOutcome.LocalFailed)
        assertEquals(reason, (outcome as C3CollectOutcome.LocalFailed).local.reason)
        val plan = C3FrameCollector.terminalPlan(outcome)!!
        assertEquals("FAILED", plan.c3Phase)
        assertEquals("failed", plan.ledgerState)
        assertEquals(C3FrameCollector.REASON_LOCAL_READ_INCOMPLETE, plan.reasonCode)
        assertFalse(plan.runMetricsStage)
        var run = EvaluationRunLedger.newRun(sessionDate = date, inputManifest = JSONObject().put("session_date", date))
        for (name in EvaluationRunLedger.STAGE_ORDER) {
            if (name != "percentile_finalization") run = EvaluationRunLedger.setStage(run, name, "verified")
        }
        run = EvaluationRunLedger.setStage(run, "percentile_finalization", plan.ledgerState, reasonCode = plan.reasonCode, lastError = plan.lastError)
        assertFalse(run.getBoolean("learning_complete"))
        assertEquals("percentile_finalization", EvaluationRunLedger.nextResumableStage(run))
    }

    @Test
    fun localMalformedLineInMiddle_isFailed_notFinalized() {
        val text = listOf(localLine(1), localLine(2), "{\"id\":\"L3\",\"poll_ts\":", localLine(4)).joinToString("\n")
        val direct = C3LocalSnapshotReader.readStrict(reader(text)) {}
        assertTrue(direct is C3LocalReadResult.Failed)
        direct as C3LocalReadResult.Failed
        assertEquals(C3LocalReadFailure.LOCAL_MALFORMED_LINE, direct.reason)
        assertEquals(3, direct.lineNumber)
        assertEquals(2, direct.rowsBeforeFailure)
        val (outcome, _) = collect(remoteEmpty = true, local = { sink -> C3LocalSnapshotReader.readStrict(reader(text), sink) })
        assertLocalFailedRetryable(outcome, C3LocalReadFailure.LOCAL_MALFORMED_LINE)
    }

    @Test
    fun localReadErrorAfterSomeRows_isFailed() {
        val text = listOf(localLine(1), localLine(2), localLine(3)).joinToString("\n") + "\n"
        val seen = mutableListOf<JSONObject>()
        val direct = C3LocalSnapshotReader.readStrict(failingReader(text)) { seen += it }
        assertTrue("got $direct", direct is C3LocalReadResult.Failed)
        direct as C3LocalReadResult.Failed
        assertEquals(C3LocalReadFailure.LOCAL_READ_ERROR, direct.reason)
        assertEquals(3, direct.rowsBeforeFailure)
        assertEquals(3, seen.size) // rows were streamed, yet the read is rejected
        val (outcome, _) = collect(remoteFail = true, local = { sink -> C3LocalSnapshotReader.readStrict(failingReader(text), sink) })
        assertLocalFailedRetryable(outcome, C3LocalReadFailure.LOCAL_READ_ERROR)
        assertTrue(C3FrameCollector.terminalPlan(outcome)!!.lastError.contains("remote=FAILED:boundary_fetch_failed"))
    }

    @Test
    fun localOpenError_isFailed() {
        val r = C3LocalSnapshotReader.readStrict({ throw IOException("EACCES") }) {}
        assertEquals(C3LocalReadFailure.LOCAL_READ_ERROR, (r as C3LocalReadResult.Failed).reason)
    }

    @Test
    fun cleanLocalFile_isComplete_andFinalizesFromLocalLikeBefore() {
        val text = listOf(localLine(1), localLine(2), "", localLine(2), localLine(3), localLine(4)).joinToString("\n") + "\n"
        val direct = C3LocalSnapshotReader.readStrict(reader(text)) {}
        assertEquals(C3LocalReadResult.Complete(rows = 4, duplicatesSkipped = 1, blankLines = 1), direct)
        val (outcome, _) = collect(remoteEmpty = true, local = { sink -> C3LocalSnapshotReader.readStrict(reader(text), sink) })
        assertTrue("got $outcome", outcome is C3CollectOutcome.Frames)
        outcome as C3CollectOutcome.Frames
        assertEquals(C3FrameCollector.SOURCE_LOCAL, outcome.source)
        assertEquals(listOf("L1", "L2", "L3", "L4"), (0 until outcome.frames.length()).map { outcome.frames.getJSONObject(it).getString("snapshot_id") })
        assertEquals(4, outcome.snapshotCount)
    }

    @Test
    fun missingLocalFile_isEmpty_andKeepsNoFramesHandling() {
        assertEquals(C3LocalReadResult.Empty(C3LocalSnapshotReader.EMPTY_NO_FILE), C3LocalSnapshotReader.readStrict({ null }) {})
        val (outcome, _) = collect(remoteEmpty = true)
        assertTrue(outcome is C3CollectOutcome.NoFrames)
        assertEquals("SKIPPED_NO_FRAMES", C3FrameCollector.terminalPlan(outcome)!!.c3Phase)
    }

    @Test
    fun remoteFailed_andLocalEmpty_staysRemoteFailed() {
        val (outcome, _) = collect(remoteFail = true)
        assertTrue(outcome is C3CollectOutcome.RemoteFailed)
        assertEquals(C3FrameCollector.REASON_REMOTE_READ_FAILED, C3FrameCollector.terminalPlan(outcome)!!.reasonCode)
    }

    @Test
    fun remoteCompleteWithFrames_neverReadsLocal() {
        val fake = FakePostgrest((1L..4L).map { Row(it, ts(it.toInt())) })
        var localCalled = false
        val (outcome, _) = collect(fake, local = { localCalled = true; C3LocalReadResult.Empty("no_file") })
        assertTrue(outcome is C3CollectOutcome.Frames)
        assertEquals(C3FrameCollector.SOURCE_REMOTE, (outcome as C3CollectOutcome.Frames).source)
        assertFalse(localCalled)
    }
}
