package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.util.Collections
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicLong

/** Codex reply 20 Nov 2026: compact single-leg device trace (store bounds/privacy + coordinator native events). */
class SingleLegTradeTraceTest {
    @get:Rule val tmp = TemporaryFolder()
    private val t0 = 1_794_975_300_000L            // 2026-11-18 09:45:00 IST
    private val day = "2026-11-18"

    private fun rec(kind: String, vararg kv: Pair<String, Any?>) = JSONObject().put("kind", kind).put("t_ms", t0).also { j ->
        kv.forEach { (k, v) -> j.put(k, v ?: JSONObject.NULL) } }.toString()

    @Test fun allowListDropsCapitalTokensAndUnknownFields() {
        val s = SingleLegTradeTraceStore(tmp.root, { t0 })
        s.append(rec("CONFIG", "capital" to 250000, "capital_set" to true, "auth_token" to "abc", "pwa" to "b531",
            "reason" to "Bearer abc.def", "decision_id" to "idx1 <script>", "enabled" to "yes", "paper_mode" to true))
        val r = s.readSession(day)!!.getJSONArray("records").getJSONObject(0)
        assertFalse(r.has("capital")); assertFalse(r.has("auth_token"))
        assertEquals(true, r.getBoolean("capital_set")); assertEquals("b531", r.getString("pwa"))
        assertEquals("[REDACTED]", r.getString("reason")); assertEquals("[INVALID]", r.getString("decision_id"))
        assertFalse("non-boolean dropped", r.has("enabled"))
        assertFalse(s.readSession(day).toString().contains("250000"))
        assertEquals("REJECTED_NOT_A_TRACE_RECORD", JSONObject(s.append(rec("ANYTHING"))).getString("status"))
        assertEquals(1, s.readSession(day)!!.getInt("rejected_records"))
    }

    @Test fun renderDedupPerIndexAndDisplayState() {
        val s = SingleLegTradeTraceStore(tmp.root, { t0 })
        val nf = arrayOf("index" to "NF", "decision_id" to "idx1_a", "instrument_key" to "NSE_FO|64455", "trade_visible" to true,
            "row_state" to "ELIGIBLE", "action_exposed" to true)
        repeat(20) { s.append(rec("RENDER", *nf)) }
        s.append(rec("RENDER", "index" to "BNF", "row_state" to "ELIGIBLE", "trade_visible" to true))
        s.append(rec("RENDER", *nf.map { if (it.first == "trade_visible") "trade_visible" to false else it }.toTypedArray()))
        s.append(rec("TAP", "index" to "NF"))
        val d = s.readSession(day)!!
        assertEquals(4, d.getJSONArray("records").length())
        assertEquals(19, d.getInt("deduped_renders"))
    }

    @Test fun capEvictsRenderFirstThenOldConfigKeepsOutcomesAndCounts() {
        val s = SingleLegTradeTraceStore(tmp.root, { t0 }, maxRecords = 10)
        s.append(rec("CONFIG", "pwa" to "b531")); s.append(rec("CONFIG", "pwa" to "b532"))
        s.append(rec("TAP", "index" to "BNF")); s.append(rec("OUTCOME", "index" to "BNF", "request_id" to "r1", "status" to "ADMITTED"))
        for (i in 0 until 12) s.append(rec("RENDER", "index" to "NF", "row_state" to "S$i"))
        var d = s.readSession(day)!!
        assertEquals(10, d.getJSONArray("records").length()); assertTrue(d.getBoolean("truncated"))
        assertEquals(6, d.getInt("dropped_renders")); assertEquals(0, d.getInt("dropped_other"))
        for (i in 0 until 7) s.append(rec("OUTCOME", "index" to "NF", "request_id" to "q$i", "status" to "REJECTED"))
        d = s.readSession(day)!!
        val kinds = (0 until d.getJSONArray("records").length()).map { d.getJSONArray("records").getJSONObject(it) }
        assertEquals(0, kinds.count { it.getString("kind") == "RENDER" })
        assertEquals("latest configuration kept", listOf("b532"), kinds.filter { it.getString("kind") == "CONFIG" }.map { it.getString("pwa") })
        assertEquals(12, d.getInt("dropped_renders")); assertEquals(1, d.getInt("dropped_other"))
        assertTrue(kinds.any { it.optString("request_id") == "r1" })
    }

    @Test fun byteCapAndSessionRetention() {
        val clk = AtomicLong(t0)
        val s = SingleLegTradeTraceStore(tmp.root, { clk.get() }, maxBytes = 4000)
        for (i in 0 until 200) s.append(JSONObject().put("kind", "RENDER").put("index", "NF").put("row_state", "S$i").toString())
        assertTrue(s.readSession(day)!!.toString().toByteArray().size <= 4000)
        for (k in 1..9) { clk.set(t0 + k * 86_400_000L); s.append(JSONObject().put("kind", "TAP").put("index", "NF").toString()) }
        assertEquals(SingleLegTradeTraceStore.KEEP_SESSIONS, java.io.File(tmp.root, SingleLegTradeTraceStore.DIR).list()!!.count { it.endsWith(".json") })
        assertNull(s.readSession(day))
    }

    @Test fun corruptFileIsResetAndAppendNeverThrows() {
        val s = SingleLegTradeTraceStore(tmp.root, { t0 })
        java.io.File(tmp.root, SingleLegTradeTraceStore.DIR).mkdirs()
        java.io.File(java.io.File(tmp.root, SingleLegTradeTraceStore.DIR), "$day.json").writeText("{not json")
        assertEquals("APPENDED", JSONObject(s.append(rec("TAP", "index" to "NF"))).getString("status"))
        assertTrue(s.readSession(day)!!.optBoolean("reset_unreadable"))
        assertEquals("TRACE_ERROR", JSONObject(s.append("not json")).getString("status"))
    }

    @Test fun queueLossIsCountedAndFailedReplaceIsNotAcknowledged() {
        val store = SingleLegTradeTraceStore(tmp.root, { t0 })
        val dir = File(tmp.root, SingleLegTradeTraceStore.DIR).apply { mkdirs() }
        val blockedDestination = File(dir, "$day.json").apply { mkdir() }
        File(blockedDestination, "sentinel").writeText("original")
        val loss = rec("PAGE_QUEUE_LOSS", "dropped_count" to 3)
        assertFalse(JSONObject(store.append(loss)).getBoolean("ok"))
        assertEquals("original", File(blockedDestination, "sentinel").readText())
        assertTrue(blockedDestination.deleteRecursively())

        assertTrue(JSONObject(store.append(loss)).getBoolean("ok"))
        val trace = store.readSession(day)!!
        assertEquals(3, trace.getInt("page_queue_dropped"))
        assertTrue(trace.getBoolean("truncated"))
        assertEquals(0, trace.getJSONArray("records").length())
    }

    // ── coordinator native events ──
    private fun coord(events: MutableList<JSONObject>, key: String? = "NSE_FO|1", cancel: (String) -> String? = { null },
                      quoteNeeded: List<String> = listOf("NSE_FO|1"),
                      admitOut: (String) -> String = { id -> """{"status":"ADMITTED","position_id":"sl1","request_id":"$id"}""" }) =
        SingleLegAdmissionCoordinator(
            readState = { SingleLegAdmissionCoordinator.State("{}", "BNF", "idx1_a", key, "BUY_CE", 30, quoteNeeded,
                true, true, "BNF", "[]", 250000.0, 10.0, 1) },
            transport = { ManagedReadAdapters.Transport { _, _, _ -> ManagedReadAdapters.Response(500, "{}") } },
            clocks = ManagedReadAdapters.Clocks({ t0 }, { t0 }),
            admit = { c -> admitOut(c.requestId) },
            reconcile = { null }, cancel = cancel, executor = Executors.newSingleThreadExecutor(), sleeper = { },
            trace = { events.add(JSONObject(it.toString())) })

    private fun await(c: SingleLegAdmissionCoordinator, id: String): JSONObject {
        val end = System.currentTimeMillis() + 5000
        while (System.currentTimeMillis() < end) { val j = JSONObject(c.status(id)); if (j.optBoolean("completed")) return j; Thread.sleep(5) }
        throw AssertionError("not completed")
    }

    @Test fun refusedBeforeQuoteIsMarkedQuoteRequestedFalse() {
        val ev = Collections.synchronizedList(mutableListOf<JSONObject>())
        val c = coord(ev)
        val out = JSONObject(c.submit("BNF", null, "NSE_FO|1", "r0"))
        assertEquals("DISPLAYED_IDENTITY_REQUIRED", out.getString("reason")); assertFalse(out.getBoolean("quote_requested"))
        assertEquals(listOf("N_SUBMIT", "N_OUTCOME"), ev.map { it.getString("kind") })
        assertEquals("BNF", ev[1].getString("index")); assertFalse(ev[1].getBoolean("quote_requested"))
    }

    @Test fun quoteFailureIsAfterQuoteWithOneQuoteStartedEvent() {
        val ev = Collections.synchronizedList(mutableListOf<JSONObject>())
        // HTTP 500 on the exact-contract GET; the Python admission (stubbed) then refuses for the missing snapshot
        val c = coord(ev, admitOut = { id -> """{"status":"REJECTED","reason":"BROKER_SNAPSHOT_UNAVAILABLE","request_id":"$id"}""" })
        c.submit("BNF", "idx1_a", "NSE_FO|1", "r1")
        val out = await(c, "r1")
        assertEquals("REJECTED", out.getString("status")); assertTrue(out.getBoolean("quote_requested"))
        Thread.sleep(20)
        val kinds = ev.map { it.getString("kind") }
        assertEquals(1, kinds.count { it == "N_QUOTE_STARTED" })
        assertEquals("NSE_FO|1", ev.first { it.getString("kind") == "N_QUOTE_STARTED" }.getString("instrument_key"))
        val o = ev.last { it.getString("kind") == "N_OUTCOME" }
        assertTrue(o.getBoolean("quote_requested")); assertEquals("r1", o.getString("request_id")); assertEquals("BNF", o.getString("index"))
    }

    @Test fun emptyQuoteKeyDoesNotProduceAQuoteStartedEvent() {
        val events = Collections.synchronizedList(mutableListOf<JSONObject>())
        val coordinator = coord(events, quoteNeeded = listOf(""))
        coordinator.submit("BNF", "idx1_a", "NSE_FO|1", "empty-key")
        assertFalse(await(coordinator, "empty-key").getBoolean("quote_requested"))
        synchronized(events) {
            assertFalse(events.any { it.getString("kind") == "N_QUOTE_STARTED" })
        }
    }

    @Test fun unknownIdAfterRestartReconciledOutcomeIsTraced() {
        val ev = Collections.synchronizedList(mutableListOf<JSONObject>())
        val c = coord(ev, cancel = { id -> """{"found":true,"status":"ADMITTED","position_id":"sl1","index":"BNF","request_id":"$id"}""" })
        val out = JSONObject(c.status("old"))
        assertEquals("ADMITTED", out.getString("status")); assertTrue(out.getBoolean("reconciled"))
        val o = ev.single()
        assertEquals("N_OUTCOME", o.getString("kind")); assertTrue(o.getBoolean("recovered")); assertEquals("BNF", o.getString("index"))
    }

    @Test fun traceFailureNeverChangesTheOutcome() {
        val c = SingleLegAdmissionCoordinator(
            readState = { SingleLegAdmissionCoordinator.State("{}", "BNF", "idx1_a", "NSE_FO|1", "BUY_CE", 30, listOf("NSE_FO|1"),
                true, true, "BNF", "[]", 250000.0, 10.0, 1) },
            transport = { null }, clocks = ManagedReadAdapters.Clocks({ t0 }, { t0 }),
            admit = { c -> """{"status":"ADMITTED","position_id":"sl1","request_id":"${c.requestId}"}""" },
            reconcile = { null }, cancel = { null }, executor = Executors.newSingleThreadExecutor(), sleeper = { },
            trace = { throw IllegalStateException("disk full") })
        c.submit("BNF", "idx1_a", "NSE_FO|1", "r2")
        assertTrue(await(c, "r2").has("status"))
    }
}
