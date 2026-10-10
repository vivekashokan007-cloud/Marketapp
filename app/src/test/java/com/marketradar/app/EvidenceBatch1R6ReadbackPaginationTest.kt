package com.marketradar.app

import okhttp3.OkHttpClient
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.net.InetAddress
import java.net.ServerSocket
import java.net.URI
import java.net.URLDecoder
import java.util.TreeMap
import java.util.concurrent.TimeUnit

/** Minimal HTTP/1.1 server (one request per connection, Connection: close) on a real loopback socket. */
class MiniHttpServer(private val handler: (MiniHttpServer.Exchange) -> Unit) {
    class Exchange(val method: String, val uri: URI, val headers: Map<String, String>, val body: ByteArray,
                   private val out: java.io.OutputStream) {
        fun header(name: String): String? = headers[name.lowercase()]
        fun respond(code: Int, body: String, extra: Map<String, String> = emptyMap()) {
            val b = body.toByteArray(Charsets.UTF_8)
            val sb = StringBuilder("HTTP/1.1 $code X\r\nContent-Type: application/json\r\nContent-Length: ${b.size}\r\nConnection: close\r\n")
            extra.forEach { (k, v) -> sb.append("$k: $v\r\n") }
            sb.append("\r\n")
            out.write(sb.toString().toByteArray(Charsets.ISO_8859_1)); out.write(b); out.flush()
        }
    }
    private val socket = ServerSocket(0, 50, InetAddress.getLoopbackAddress())
    val port get() = socket.localPort
    @Volatile private var running = true
    private val acceptor = Thread {
        while (running) {
            val c = runCatching { socket.accept() }.getOrNull() ?: break
            Thread {
                c.use { conn ->
                    runCatching {
                        val input = java.io.BufferedInputStream(conn.getInputStream())
                        fun line(): String { val sb = StringBuilder(); while (true) { val ch = input.read(); if (ch < 0 || ch == '\n'.code) break; if (ch != '\r'.code) sb.append(ch.toChar()) }; return sb.toString() }
                        val req = line().split(" ")
                        val headers = mutableMapOf<String, String>()
                        while (true) { val h = line(); if (h.isEmpty()) break; headers[h.substringBefore(":").trim().lowercase()] = h.substringAfter(":").trim() }
                        val len = headers["content-length"]?.toInt() ?: 0
                        val body = ByteArray(len); var r = 0; while (r < len) { val k = input.read(body, r, len - r); if (k < 0) break; r += k }
                        handler(Exchange(req[0], URI(req[1]), headers, body, conn.getOutputStream()))
                    }
                }
            }.apply { isDaemon = true }.start()
        }
    }.apply { isDaemon = true; start() }
    fun stop() { running = false; socket.close() }
}

/**
 * R2-review Blocker A: uploader + OkHttp transport against a local fake PostgREST over real HTTP.
 * The fake enforces a per-response row cap (db-max-rows) BELOW the requested limit, models the RPCs of
 * 20261009120000_paper_decision_menu_evidence.sql (manifest REPLAY, candidate replay, terminal finalize) and can
 * inject later-page timeouts, duplicate/non-progressing pages, omitted rows and changed hashes.
 */
class EvidenceBatch1R6ReadbackPaginationTest {
    @get:Rule val tmp = TemporaryFolder()

    private val key = "a".repeat(64)
    private val date = "2026-10-09"
    private val menuId = PaperMenuCapture.menuIdFor(date)

    /** What the fake server does to one readback GET (1-based call number across the whole test). */
    sealed class Fault {
        object Timeout : Fault()
        object Http503 : Fault()
        object IgnoreCursor : Fault()          // returns from the start again (duplicate / non-progressing page)
        data class OmitRank(val rank: Int) : Fault()
        data class ChangeHash(val rank: Int) : Fault()
    }

    inner class FakePostgrest(val cap: Int, val sendExactCount: Boolean = true) {
        private val server = MiniHttpServer { ex -> runCatching { handle(ex) }.onFailure { ex.respond(500, "{\"message\":\"${it.javaClass.simpleName}\"}") } }
        val base get() = "http://127.0.0.1:${server.port}/rest/v1"
        var getCalls = 0
        val getFaults = mutableMapOf<Int, Fault>()      // by GET call number
        val persistentFaults = mutableListOf<Fault>()   // applied to every GET
        val requestedLimits = mutableListOf<Int>()
        val rpcPayloadShas = mutableListOf<String>()
        // storage
        var manifest: JSONObject? = null
        var completeness = "PENDING_ROWS"
        val rows = TreeMap<Int, Triple<String, String, String>>() // rank -> (candidate_id, canonical, sha)
        var finalizeResult: JSONObject? = null

        fun stop() = server.stop()

        private fun reply(ex: MiniHttpServer.Exchange, code: Int, body: String, contentRange: String? = null) =
            ex.respond(code, body, if (contentRange != null) mapOf("Content-Range" to contentRange) else emptyMap())

        private fun sha(s: String) = PaperMenuCapture.sha256Hex(s)

        private fun handle(ex: MiniHttpServer.Exchange) {
            val path = ex.uri.path.removePrefix("/rest/v1/")
            if (ex.method == "POST" && path.startsWith("rpc/")) {
                val body = JSONObject(ex.body.toString(Charsets.UTF_8))
                reply(ex, 200, rpc(path.removePrefix("rpc/"), body).toString()); return
            }
            if (ex.method == "GET" && path == "ml_paper_decision_menu_candidates") {
                getCalls++
                val q = ex.uri.rawQuery.split("&").associate { p ->
                    URLDecoder.decode(p.substringBefore("="), "UTF-8") to URLDecoder.decode(p.substringAfter("=", ""), "UTF-8") }
                val fault = getFaults[getCalls]
                if (fault is Fault.Timeout) { Thread.sleep(1500); reply(ex, 200, "[]"); return }
                if (fault is Fault.Http503) { reply(ex, 503, "{\"message\":\"unavailable\"}"); return }
                assertEquals("eq.$menuId", q["menu_id"])
                assertEquals("generated_rank.asc", q["order"])
                assertTrue("readback must not use offset", !q.containsKey("offset"))
                val limit = q.getValue("limit").toInt(); requestedLimits.add(limit)
                val faults = persistentFaults + listOfNotNull(fault)
                val gt = if (faults.any { it is Fault.IgnoreCursor }) 0 else q.getValue("generated_rank").removePrefix("gt.").toInt()
                var remaining = rows.tailMap(gt, false).entries.toList()
                val totalCount = remaining.size
                remaining = remaining.filter { e -> faults.none { it is Fault.OmitRank && it.rank == e.key } }
                val page = remaining.take(minOf(limit, cap))
                val arr = JSONArray()
                page.forEach { (rank, r) ->
                    val h = if (faults.any { it is Fault.ChangeHash && it.rank == rank }) sha("tampered") else r.third
                    arr.put(JSONObject().put("candidate_id", r.first).put("generated_rank", rank).put("content_sha256", h))
                }
                val exact = sendExactCount && ex.header("Prefer") == "count=exact"
                val total = if (exact) totalCount.toString() else "*"
                val range = if (page.isEmpty()) "*/$total" else "0-${page.size - 1}/$total"
                reply(ex, if (exact && page.size < totalCount) 206 else 200, arr.toString(), range); return
            }
            reply(ex, 404, "{}")
        }

        private fun rpc(fn: String, b: JSONObject): JSONObject = when (fn) {
            "paper_decision_menu_ingest_manifest" -> {
                val m = b.getJSONObject("p_manifest"); rpcPayloadShas.add(m.getString("payload_sha256"))
                val cur = manifest
                when {
                    cur == null -> { manifest = m; completeness = "PENDING_ROWS"; JSONObject().put("status", "ACCEPTED") }
                    cur.getString("payload_sha256") == m.getString("payload_sha256") -> JSONObject().put("status", "REPLAY")
                    else -> JSONObject().put("status", "REJECTED_CONFLICT").put("conflict", "PAYLOAD_CONFLICT")
                }
            }
            "paper_decision_menu_ingest_candidates" -> {
                val arr = b.getJSONArray("p_rows"); var out: JSONObject? = null
                for (i in 0 until arr.length()) {
                    val text = arr.getString(i); val d = JSONObject(text); val rank = d.getInt("generated_rank")
                    val existing = rows.values.firstOrNull { it.first == d.getString("candidate_id") }
                    if (existing != null) {
                        if (existing.third != sha(text)) { out = JSONObject().put("status", "REJECTED_CONFLICT").put("conflict", "CANDIDATE_CONTENT_CONFLICT"); break }
                        continue
                    }
                    if (completeness in setOf("COMPLETE", "NO_ROWS_EXPECTED", "MISMATCH")) { out = JSONObject().put("status", "REJECTED_TERMINAL"); break }
                    rows[rank] = Triple(d.getString("candidate_id"), text, sha(text))
                }
                out ?: JSONObject().put("status", "OK").put("durable_candidate_rows", rows.size)
            }
            "paper_decision_menu_finalize" -> {
                val m = manifest!!
                finalizeResult?.takeIf { completeness in setOf("COMPLETE", "NO_ROWS_EXPECTED", "MISMATCH") }?.let { JSONObject(it.toString()).put("already_reconciled", true) } ?: run {
                    val n = rows.size; val exp = m.getInt("expected_candidate_rows")
                    val ids = sha(rows.values.joinToString("\n") { it.first }); val content = sha(rows.values.joinToString("\n") { it.third })
                    completeness = when {
                        n > exp -> "MISMATCH"; n < exp -> "PARTIAL"
                        exp == 0 && m.getString("content_sha256") == content -> "NO_ROWS_EXPECTED"
                        exp == 0 -> "MISMATCH"
                        exp > 0 && m.optString("candidate_ids_sha256") != ids -> "MISMATCH"
                        m.getString("content_sha256") != content -> "MISMATCH"
                        else -> "COMPLETE"
                    }
                    JSONObject().put("status", "OK").put("completeness_state", completeness).put("durable_candidate_rows", n)
                        .put("expected_candidate_rows", exp).put("durable_ids_sha256", ids).put("durable_content_sha256", content)
                        .put("already_reconciled", false).also { finalizeResult = it }
                }
            }
            else -> JSONObject().put("status", "UNKNOWN")
        }
    }

    private val servers = mutableListOf<FakePostgrest>()
    private fun server(cap: Int, exact: Boolean = true) = FakePostgrest(cap, exact).also { servers.add(it) }
    @After fun stopServers() = servers.forEach { it.stop() }

    private val http = OkHttpClient.Builder().connectTimeout(2, TimeUnit.SECONDS).readTimeout(400, TimeUnit.MILLISECONDS).build()
    private fun transport(s: FakePostgrest) = PaperMenuUploader.OkHttpTransport(http, s.base) { mapOf("apikey" to "test") }

    /** A frozen CAPTURED payload with [n] candidates in the client's real shape (manifest + candidates). */
    private fun frozen(n: Int): String {
        val cands = JSONArray()
        for (i in 1..n) cands.put(JSONObject().put("menu_id", menuId).put("candidate_id", "c%05d".format(i)).put("generated_rank", i)
            .put("surfaced", i <= 3).put("selected_primary", i == 1).put("legs", JSONArray().put(JSONObject().put("strike", 52000 + i))))
        val ids = PaperMenuCapture.sha256Hex((1..n).joinToString("\n") { "c%05d".format(it) })
        val manifest = JSONObject().put("menu_id", menuId).put("session_date", date)
            .put("capture_policy_version", PaperMenuCapture.POLICY_VERSION)
            .put("population_status", if (n == 0) "EMPTY_MENU" else "MENU_BUILT")
            .put("generated_count", n).put("captured_count", n).put("duplicate_count", 0)
            .put("expected_candidate_rows", n).put("candidate_ids_sha256", ids)
        return JSONObject().put("manifest", manifest).put("candidates", cands).toString()
    }

    private fun uploadOnce(s: FakePostgrest, text: String): PaperMenuUploader.Result {
        val f = JSONObject(text); val sha = PaperMenuCapture.sha256Hex(text)
        return PaperMenuUploader.upload(transport(s), key, PaperMenuCapture.manifestRow(f, sha),
            PaperMenuCapture.canonicalCandidates(f).map { it.second }, sha)
    }

    /** Drives the real outbox with the real uploader, exactly as MarketWatchService.launchPaperMenuUploadIfPending does. */
    private fun drainVia(dir: java.io.File, s: FakePostgrest, nowMs: Long) = PaperMenuOutbox.drain(dir, date, nowMs) { _, text ->
        val r = uploadOnce(s, text); PaperMenuOutbox.Delivery(r.disposition, r.outcome, r.detail)
    }

    private fun enqueue(n: Int): Pair<java.io.File, String> {
        val dir = tmp.newFolder(); val text = frozen(n)
        PaperMenuOutbox.enqueueCaptured(dir, menuId, date, text, 0)!!
        return dir to text
    }

    private fun state(dir: java.io.File) = PaperMenuOutbox.readState(dir, menuId)!!

    @Test fun rows1100UnderServerCap1000AreFullyReadBackAndAcknowledged() {
        val s = server(cap = 1000)
        val (dir, _) = enqueue(1100)
        val d = drainVia(dir, s, 1)
        assertEquals(1, d.acknowledged)
        assertEquals(PaperMenuOutbox.ACKED, state(dir).getString("state"))
        assertEquals("VERIFIED", state(dir).getString("readback_state"))
        assertTrue(state(dir).getString("last_detail").startsWith("readback_rows=1100 "))
        assertTrue("requested page size stays bounded", s.requestedLimits.all { it == PaperMenuUploader.READBACK_PAGE_ROWS })
        assertEquals(3, s.getCalls) // 500 + 500 + 100 (exact remaining count proves the end)
    }

    @Test fun rows1100WithCap1000AndNoCountHeaderNeedEmptyPageAfterLast() {
        val s = server(cap = 1000, exact = false)
        val (dir, _) = enqueue(1100)
        assertEquals(1, drainVia(dir, s, 1).acknowledged)
        assertEquals(4, s.getCalls) // 500, 500, 100 (short page is NOT end), then empty page proves the end
    }

    @Test fun rows740WithCap100And200FetchEveryPage() {
        for ((cap, pages) in listOf(100 to 8, 200 to 4)) {
            for (exact in listOf(true, false)) {
                val s = server(cap, exact)
                val (dir, _) = enqueue(740)
                assertEquals("cap=$cap exact=$exact", 1, drainVia(dir, s, 1).acknowledged)
                assertEquals("cap=$cap exact=$exact", if (exact) pages else pages + 1, s.getCalls)
            }
        }
    }

    @Test fun exactPageBoundaryAndCapBoundary() {
        for ((n, cap) in listOf(500 to 1000, 1000 to 1000, 200 to 200, 400 to 200)) {
            for (exact in listOf(true, false)) {
                val s = server(cap, exact)
                val (dir, _) = enqueue(n)
                assertEquals("n=$n cap=$cap exact=$exact", 1, drainVia(dir, s, 1).acknowledged)
                val full = n / minOf(cap, PaperMenuUploader.READBACK_PAGE_ROWS)
                assertEquals("n=$n cap=$cap exact=$exact", if (exact) full else full + 1, s.getCalls)
            }
        }
    }

    @Test fun emptyMenuIsReadBackAsEmptyAndAcknowledged() {
        val s = server(cap = 1000)
        val (dir, _) = enqueue(0)
        assertEquals(1, drainVia(dir, s, 1).acknowledged)
        assertEquals(1, s.getCalls)
        assertEquals("NO_ROWS_EXPECTED", s.finalizeResult!!.getString("completeness_state"))
    }

    @Test fun timeoutOnLaterPageIsRetryThenIdenticalFrozenBytesAcknowledge() {
        for (fault in listOf(Fault.Timeout, Fault.Http503)) {
            val s = server(cap = 200)
            val (dir, text) = enqueue(740)
            s.getFaults[3] = fault
            val d1 = drainVia(dir, s, 1)
            assertEquals(0, d1.acknowledged)
            val st = state(dir)
            assertEquals(PaperMenuOutbox.RETRY_PENDING, st.getString("state"))
            assertEquals("INCOMPLETE", st.getString("readback_state"))
            assertTrue(st.getString("last_outcome"), st.getString("last_outcome").startsWith("READBACK_INCOMPLETE:page_3_http:"))
            assertEquals("COMPLETE", s.finalizeResult!!.getString("completeness_state"))   // server already complete
            val d2 = drainVia(dir, s, 2)
            assertEquals(1, d2.acknowledged)
            assertEquals(PaperMenuOutbox.ACKED, state(dir).getString("state"))
            assertEquals(listOf(PaperMenuCapture.sha256Hex(text), PaperMenuCapture.sha256Hex(text)), s.rpcPayloadShas)
            assertEquals(text, java.io.File(dir, "$menuId.json").readText())
        }
    }

    @Test fun duplicateOrNonProgressingPageIsNeverAcknowledgedAndIsIncompleteNotMismatch() {
        for (exact in listOf(false, true)) {
            val s = server(cap = 200, exact = exact)
            val (dir, _) = enqueue(740)
            s.getFaults[2] = Fault.IgnoreCursor     // page 2 repeats page 1
            drainVia(dir, s, 1)
            val st = state(dir)
            assertEquals(PaperMenuOutbox.RETRY_PENDING, st.getString("state"))
            assertEquals("INCOMPLETE", st.getString("readback_state"))
            // Without a count the repeated ranks are caught; with an exact count the inconsistent remaining count is caught first.
            assertTrue(st.getString("last_outcome"), st.getString("last_outcome").contains(
                if (exact) "page_2_count_not_consistent" else "page_2_non_progressing_or_duplicate"))
            // A server that always ignores the cursor is never acknowledged; the round ends EXHAUSTED (retained, retried next session).
            s.persistentFaults.add(Fault.IgnoreCursor)
            repeat(PaperMenuOutbox.MAX_ATTEMPTS_PER_ROUND) { drainVia(dir, s, 2L + it) }
            assertEquals(PaperMenuOutbox.EXHAUSTED, state(dir).getString("state"))
            assertNotEquals(PaperMenuOutbox.MISMATCH_RETAINED, state(dir).getString("state"))
            assertEquals("INCOMPLETE", state(dir).getString("readback_state"))
        }
    }

    @Test fun omittedRowIsNeverAcknowledgedAndIsIncompleteWithOrWithoutCount() {
        for (exact in listOf(true, false)) {
            val s = server(cap = 1000, exact = exact)
            val (dir, _) = enqueue(1100)
            s.persistentFaults.add(Fault.OmitRank(777))
            val d = drainVia(dir, s, 1)
            assertEquals(0, d.acknowledged)
            val st = state(dir)
            assertEquals("exact=$exact", PaperMenuOutbox.RETRY_PENDING, st.getString("state"))
            assertEquals("INCOMPLETE", st.getString("readback_state"))
            assertTrue(st.getString("last_outcome"),
                if (exact) st.getString("last_outcome").contains("count_not_consistent")
                else st.getString("last_outcome").contains("readback_rows_1099_disagree_with_declared_1100"))
        }
    }

    @Test fun changedHashOnALaterPageIsAnEstablishedMismatchAndRetained() {
        val s = server(cap = 1000)
        val (dir, _) = enqueue(1100)
        s.persistentFaults.add(Fault.ChangeHash(1050))
        drainVia(dir, s, 1)
        val st = state(dir)
        assertEquals(PaperMenuOutbox.MISMATCH_RETAINED, st.getString("state"))
        assertEquals("MISMATCH", st.getString("readback_state"))
        assertEquals("READBACK_DIFFERS:row_1049:content_sha256", st.getString("last_outcome"))
        val calls = s.getCalls
        drainVia(dir, s, 2)                         // retained: never retried automatically, never acknowledged
        assertEquals(calls, s.getCalls)
        assertTrue(PaperMenuOutbox.prune(dir, 400L * 24 * 3600 * 1000, 30L * 24 * 3600 * 1000).isEmpty())
    }

    @Test fun readbackPageBudgetIsBoundedForACapOfOneRow() {
        val s = server(cap = 1, exact = false)
        val (dir, _) = enqueue(30)
        assertEquals(1, drainVia(dir, s, 1).acknowledged)
        assertEquals(31, s.getCalls)  // 30 single-row pages + the proving empty page; budget = rows + 2
    }

    @Test fun contentRangeParsing() {
        assertEquals(740, PaperMenuUploader.remainingFromContentRange("0-199/740"))
        assertEquals(0, PaperMenuUploader.remainingFromContentRange("*/0"))
        assertEquals(null, PaperMenuUploader.remainingFromContentRange("0-199/*"))
        assertEquals(null, PaperMenuUploader.remainingFromContentRange(null))
    }
}
