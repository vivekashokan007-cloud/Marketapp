package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.net.SocketTimeoutException
import java.util.concurrent.Executor

/**
 * C1 capture pilot, native side (fake Python callbacks, fake transport, virtual clocks).  FIXTURE results only:
 * fetch plans shaped like brain.c1_capture_plan output for the sanitized 9 Nov BNF proposals; bodies synthetic.
 */
class C1CapturePilotTest {
    private class Queue : Executor {
        val tasks = ArrayList<Runnable>()
        override fun execute(r: Runnable) { tasks.add(r) }
        fun drain() { while (tasks.isNotEmpty()) tasks.removeAt(0).run() }
    }

    private class FakeTransport(val script: MutableList<(String, String, String?) -> ManagedReadAdapters.Response>) :
        ManagedReadAdapters.Transport {
        val calls = ArrayList<Triple<String, String, String?>>()
        override fun call(method: String, url: String, body: String?): ManagedReadAdapters.Response {
            calls.add(Triple(method, url, body))
            val f = if (script.size > 1) script.removeAt(0) else script[0]
            return f(method, url, body)
        }
    }

    private var wall = 1_762_668_927_827L          // 2026-11-09T11:35:27.827+05:30
    private val clocks = ManagedReadAdapters.Clocks({ wall.also { wall += 100 } }, { 42L })
    private val sellKey = "FX|BNF|2026-11-23|59400PE"

    private fun planJson(vararg fetches: JSONObject) = JSONObject().put("status", "OK").put("session_date", "2026-11-09")
        .put("fetches", JSONArray(fetches.toList())).put("budget_exhausted", JSONArray()).toString()

    private fun entrySell(id: String = "c1f_a") = JSONObject().put("fetch_id", id).put("index", "BNF").put("purpose", "ENTRY")
        .put("keys", JSONArray().put(sellKey)).put("max_attempts", 2)
        .put("margins", JSONArray().put(JSONObject().put("target_id", "c1e_t").put("instrument_key", sellKey)
            .put("quantity", 30).put("product", "I").put("side", "SELL")))

    private val ok: (String, String, String?) -> ManagedReadAdapters.Response = { m, _, _ ->
        if (m == "GET") ManagedReadAdapters.Response(200, "{\"status\":\"success\",\"data\":{}}")
        else ManagedReadAdapters.Response(200, "{\"status\":\"success\",\"data\":{\"required_margin\":1}}")
    }

    private val calls = ArrayList<IntArray>()       // (dropped, discarded) per plan call
    private fun pilot(enabled: Boolean, tr: ManagedReadAdapters.Transport?, plans: MutableList<String>,
                      records: MutableList<JSONObject>, q: Queue, planOut: () -> String) =
        gated({ if (enabled) null else "PILOT_DISABLED" }, tr, plans, records, q, planOut)

    private fun gated(gate: () -> String?, tr: ManagedReadAdapters.Transport?, plans: MutableList<String>,
                      records: MutableList<JSONObject>, q: Queue, planOut: () -> String) = C1CapturePilot(
        gate = gate, transport = { tr }, clocks = clocks,
        plan = { r, _, dropped, discarded -> plans.add(r); calls.add(intArrayOf(dropped, discarded)); planOut() },
        record = { id, ix, s, c -> records.add(JSONObject(c).put("_id", id).put("_ix", ix).put("_s", s)); "{\"status\":\"RECORDED\"}" },
        executor = q)

    @Test fun disabled_by_default_does_nothing() {
        val q = Queue(); val plans = ArrayList<String>(); val recs = ArrayList<JSONObject>()
        val tr = FakeTransport(mutableListOf(ok))
        val p = pilot(false, tr, plans, recs, q) { planJson(entrySell()) }
        assertFalse(p.offer("{}"))
        q.drain()
        assertTrue(plans.isEmpty()); assertTrue(recs.isEmpty()); assertTrue(tr.calls.isEmpty())
        val src = File("src/main/java/com/marketradar/app/C1CapturePilot.kt").readText()
        assertTrue(src.contains("prefs.getBoolean(PREF_ENABLED, false)"))
        val nb = File("src/main/java/com/marketradar/app/NativeBridge.kt").readText()
        assertTrue(nb.contains("fun getC1CapturePilotEnabled(): Boolean = BuildConfig.PWA_CANDIDATE && prefs.getBoolean(C1PilotIo.PREF_ENABLED, false)"))
        assertTrue(nb.contains("if (!BuildConfig.PWA_CANDIDATE) return\n        prefs.edit().putBoolean(C1PilotIo.PREF_ENABLED, enabled).apply()"))
    }

    @Test fun queued_offers_all_reach_plan_in_order() {
        // R1: three distinct publications before the worker runs -> all three slices registered (no latest-wins)
        val q = Queue(); val plans = ArrayList<String>(); val recs = ArrayList<JSONObject>()
        val p = pilot(true, FakeTransport(mutableListOf(ok)), plans, recs, q) { planJson() }
        assertTrue(p.offer("{\"poll\":1}")); assertTrue(p.offer("{\"poll\":2}")); assertTrue(p.offer("{\"poll\":3}"))
        assertEquals(1, q.tasks.size)
        q.drain()
        assertEquals(listOf("[{\"poll\":1},{\"poll\":2},{\"poll\":3}]"), plans)
        assertEquals(listOf(0, 0), calls.single().toList())
    }

    @Test fun exact_read_only_requests_and_raw_bodies_forwarded() {
        val q = Queue(); val recs = ArrayList<JSONObject>()
        val tr = FakeTransport(mutableListOf(ok))
        pilot(true, tr, ArrayList(), recs, q) { planJson(entrySell()) }.offer("{}"); q.drain()
        assertEquals(2, tr.calls.size)
        assertEquals("GET", tr.calls[0].first)
        assertTrue(tr.calls[0].second.startsWith("https://api.upstox.com/v3/market-quote/quotes?instrument_key="))
        assertEquals(Pair("POST", "https://api.upstox.com/v2/charges/margin"), Pair(tr.calls[1].first, tr.calls[1].second))
        val mreq = JSONObject(tr.calls[1].third!!).getJSONArray("instruments").getJSONObject(0)
        assertEquals(listOf(sellKey, 30, "I", "SELL"), listOf(mreq.getString("instrument_key"), mreq.getInt("quantity"),
            mreq.getString("product"), mreq.getString("transaction_type")))
        tr.calls.forEach { assertFalse(it.second.contains("/order")) }
        val c = recs.single()
        assertEquals(JSONArray().put(sellKey).toString(), c.getJSONArray("requested").toString())
        val quote = c.getJSONObject("quote")
        assertEquals("{\"status\":\"success\",\"data\":{}}", quote.getString("body"))
        assertTrue(quote.getLong("request_start_ms") < quote.getLong("receipt_ms"))
        assertEquals(42L, quote.getLong("receipt_mono_ms"))
        assertEquals(1, c.getInt("attempts"))
        assertTrue(c.isNull("quote_failure"))
        assertEquals(Triple("c1f_a", "BNF", "2026-11-09"), Triple(c.getString("_id"), c.getString("_ix"), c.getString("_s")))
    }

    @Test fun bounded_retry_on_timeout_then_success() {
        val q = Queue(); val recs = ArrayList<JSONObject>()
        val tr = FakeTransport(mutableListOf({ _, _, _ -> throw SocketTimeoutException() }, ok))
        pilot(true, tr, ArrayList(), recs, q) {
            planJson(entrySell().put("margins", JSONArray()))
        }.offer("{}"); q.drain()
        assertEquals(2, tr.calls.size)
        assertEquals(2, recs.single().getInt("attempts"))
        assertTrue(recs.single().has("quote"))
    }

    @Test fun retries_never_exceed_cap_and_failures_are_explicit() {
        val q = Queue(); val recs = ArrayList<JSONObject>()
        val tr = FakeTransport(mutableListOf({ _, _, _ -> ManagedReadAdapters.Response(503, "x") }))
        pilot(true, tr, ArrayList(), recs, q) { planJson(entrySell().put("max_attempts", 9)) }.offer("{}"); q.drain()
        assertEquals(4, tr.calls.size)                                  // 2 quote + 2 margin, cap = 2 each
        val c = recs.single()
        assertEquals("HTTP_503", c.getString("quote_failure"))
        assertFalse(c.has("quote"))
        assertEquals("HTTP_503", c.getJSONArray("margins").getJSONObject(0).getString("failure"))
        val q2 = Queue(); val recs2 = ArrayList<JSONObject>()
        val tr2 = FakeTransport(mutableListOf({ _, _, _ -> ManagedReadAdapters.Response(401, "no") }))
        pilot(true, tr2, ArrayList(), recs2, q2) { planJson(entrySell().put("margins", JSONArray())) }.offer("{}"); q2.drain()
        assertEquals(1, tr2.calls.size)                                 // non-retryable
        assertEquals("HTTP_401", recs2.single().getString("quote_failure"))
    }

    @Test fun oversized_body_is_not_forwarded() {
        val q = Queue(); val recs = ArrayList<JSONObject>()
        val big = "x".repeat(ManagedReadAdapters.MAX_BODY_BYTES + 1)
        val tr = FakeTransport(mutableListOf({ _, _, _ -> ManagedReadAdapters.Response(200, big) }))
        pilot(true, tr, ArrayList(), recs, q) { planJson(entrySell().put("margins", JSONArray())) }.offer("{}"); q.drain()
        assertEquals("BODY_TOO_LARGE", recs.single().getString("quote_failure"))
        assertEquals(1, tr.calls.size)
    }

    @Test fun no_transport_still_plans_denominator_and_records_zero_requests() {
        val q = Queue(); val plans = ArrayList<String>(); val recs = ArrayList<JSONObject>()
        pilot(true, null, plans, recs, q) { planJson(entrySell()) }.offer("{}"); q.drain()
        assertEquals(1, plans.size)
        val c = recs.single()
        assertEquals(0, c.getInt("attempts"))
        assertEquals("NO_TRANSPORT", c.getString("quote_failure"))
        assertEquals("NO_TRANSPORT", c.getJSONArray("margins").getJSONObject(0).getString("failure"))
    }

    @Test fun identical_fetch_id_executes_once_per_run() {
        val q = Queue(); val recs = ArrayList<JSONObject>()
        val tr = FakeTransport(mutableListOf(ok))
        pilot(true, tr, ArrayList(), recs, q) { planJson(entrySell("dup").put("margins", JSONArray()), entrySell("dup").put("margins", JSONArray())) }
            .offer("{}"); q.drain()
        assertEquals(1, tr.calls.size)
        assertEquals(1, recs.size)
    }

    @Test fun exit_fetch_requests_at_most_four_exact_keys_in_one_call() {
        val q = Queue(); val recs = ArrayList<JSONObject>()
        val tr = FakeTransport(mutableListOf(ok))
        val keys = JSONArray(listOf("FX|BNF|2026-11-23|59300PE", "FX|BNF|2026-11-23|59400PE", "FX|BNF|2026-11-23|59500CE",
            "FX|BNF|2026-11-23|59500PE"))
        val f = JSONObject().put("fetch_id", "c1f_x").put("index", "BNF").put("purpose", "TIME_EXIT").put("keys", keys)
            .put("margins", JSONArray()).put("max_attempts", 2)
        pilot(true, tr, ArrayList(), recs, q) { planJson(f) }.offer("{}"); q.drain()
        assertEquals(1, tr.calls.size)
        assertEquals(keys.toString(), recs.single().getJSONArray("requested").toString())
    }

    @Test fun plan_failure_or_garbage_is_inert() {
        val q = Queue(); val recs = ArrayList<JSONObject>()
        val tr = FakeTransport(mutableListOf(ok))
        pilot(true, tr, ArrayList(), recs, q) { "not json" }.offer("{}"); q.drain()
        assertTrue(tr.calls.isEmpty()); assertTrue(recs.isEmpty())
    }

    @Test fun pilot_source_has_no_admission_or_store_write_path() {
        val src = File("src/main/java/com/marketradar/app/C1CapturePilot.kt").readText()
        for (forbidden in listOf("single_leg_admit", "single_leg_put_quotes", "SingleLegAdmissionCoordinator",
                "positions.json", "snapshots.json", "/order", "setSingleLegPaperEnabled", "bumpSingleLegConfigRev")) {
            assertFalse(forbidden, src.contains(forbidden))
        }
        val svc = File("src/main/java/com/marketradar/app/MarketWatchService.kt").readText()
        assertEquals(1, Regex("C1PilotIo\\.offer\\(").findAll(svc).count())
        assertNull(Regex("C1PilotIo").find(File("src/main/java/com/marketradar/app/SingleLegAdmissionCoordinator.kt").readText()))
    }

    // ── R1 regressions ─────────────────────────────────────────────────────────────────────────────────

    @Test fun queue_overflow_is_counted_not_silent() {
        val q = Queue(); val plans = ArrayList<String>()
        val p = pilot(true, FakeTransport(mutableListOf(ok)), plans, ArrayList(), q) { planJson() }
        for (i in 1..(C1CapturePilot.MAX_QUEUE + 2)) p.offer("{\"poll\":$i}")
        q.drain()
        assertEquals(C1CapturePilot.MAX_QUEUE, JSONArray(plans.single()).length())
        assertEquals(listOf(2, 0), calls.single().toList())           // Python marks the slice INCOMPLETE
    }

    @Test fun gate_reasons_non_candidate_default_off_non_paper() {
        assertEquals("NOT_CANDIDATE_BUILD", C1PilotIo.gateReason(false, true, "paper"))
        assertEquals("PILOT_DISABLED", C1PilotIo.gateReason(true, false, "paper"))
        assertEquals("NOT_PAPER_MODE", C1PilotIo.gateReason(true, true, "real"))
        assertEquals("NOT_PAPER_MODE", C1PilotIo.gateReason(true, true, null))
        assertNull(C1PilotIo.gateReason(true, true, "paper"))
        for (reason in listOf("NOT_CANDIDATE_BUILD", "PILOT_DISABLED", "NOT_PAPER_MODE")) {
            val q = Queue(); val plans = ArrayList<String>(); val recs = ArrayList<JSONObject>()
            val tr = FakeTransport(mutableListOf(ok))
            assertFalse(gated({ reason }, tr, plans, recs, q) { planJson(entrySell()) }.offer("{}"))
            q.drain()
            assertTrue(plans.isEmpty()); assertTrue(recs.isEmpty()); assertTrue(tr.calls.isEmpty())
        }
        val src = File("src/main/java/com/marketradar/app/C1CapturePilot.kt").readText()
        assertTrue(src.contains("gateReason(BuildConfig.PWA_CANDIDATE,"))
        assertTrue(src.contains("EvAcceptance.executionMode(prefs)"))
    }

    @Test fun toggle_off_during_queued_run_discards_and_reenable_starts_fresh() {
        val q = Queue(); val plans = ArrayList<String>(); val recs = ArrayList<JSONObject>()
        var g: String? = null
        val tr = FakeTransport(mutableListOf(ok))
        val p = gated({ g }, tr, plans, recs, q) { planJson(entrySell()) }
        assertTrue(p.offer("{\"poll\":1}"))
        g = "PILOT_DISABLED"                                           // user turns it off while queued
        q.drain()
        assertTrue(plans.isEmpty()); assertTrue(tr.calls.isEmpty()); assertEquals(0, p.queued())
        g = null
        assertTrue(p.offer("{\"poll\":2}"))
        q.drain()
        assertEquals(listOf("[{\"poll\":2}]"), plans)                 // stale slice never runs
        assertEquals(listOf(0, 1), calls.single().toList())            // discarded count reported
    }

    @Test fun leaving_paper_between_quote_and_margin_skips_margin() {
        val q = Queue(); val recs = ArrayList<JSONObject>()
        var g: String? = null
        val tr = FakeTransport(mutableListOf({ m, u, b -> val r = ok(m, u, b); g = "NOT_PAPER_MODE"; r }))
        gated({ g }, tr, ArrayList(), recs, q) { planJson(entrySell()) }.offer("{}"); q.drain()
        assertEquals(1, tr.calls.size)                                 // GET only, no margin POST
        val c = recs.single()
        assertEquals(1, c.getInt("attempts"))
        assertTrue(c.has("quote"))                                     // in-flight response kept, own timestamps
        assertTrue(c.getJSONObject("quote").getLong("receipt_ms") > 0)
        val m = c.getJSONArray("margins").getJSONObject(0)
        assertEquals(Pair(0, "SKIPPED:NOT_PAPER_MODE"), Pair(m.getInt("attempts"), m.getString("failure")))
    }

    @Test fun disabled_before_retry_skips_retry_with_counted_attempts() {
        val q = Queue(); val recs = ArrayList<JSONObject>()
        var g: String? = null
        val tr = FakeTransport(mutableListOf({ _, _, _ -> g = "PILOT_DISABLED"; ManagedReadAdapters.Response(503, "x") }))
        gated({ g }, tr, ArrayList(), recs, q) { planJson(entrySell()) }.offer("{}"); q.drain()
        assertEquals(1, tr.calls.size)
        val c = recs.single()
        assertEquals(Pair(1, "SKIPPED:PILOT_DISABLED"), Pair(c.getInt("attempts"), c.getString("quote_failure")))
        val m = c.getJSONArray("margins").getJSONObject(0)
        assertEquals(Pair(0, "SKIPPED:PILOT_DISABLED"), Pair(m.getInt("attempts"), m.getString("failure")))
    }

    @Test fun gate_closing_mid_plan_skips_remaining_fetches_explicitly() {
        val q = Queue(); val recs = ArrayList<JSONObject>()
        var g: String? = null
        val tr = FakeTransport(mutableListOf({ m, u, b -> val r = ok(m, u, b); g = "NOT_PAPER_MODE"; r }))
        gated({ g }, tr, ArrayList(), recs, q) {
            planJson(entrySell("f1").put("margins", JSONArray()), entrySell("f2"))
        }.offer("{}"); q.drain()
        assertEquals(1, tr.calls.size)
        assertEquals(2, recs.size)
        assertEquals(Pair(0, "SKIPPED:NOT_PAPER_MODE"), Pair(recs[1].getInt("attempts"), recs[1].getString("quote_failure")))
        assertEquals("SKIPPED:NOT_PAPER_MODE", recs[1].getJSONArray("margins").getJSONObject(0).getString("failure"))
    }

    @Test fun publication_slice_is_compact_and_cpu_only() {
        val res = JSONObject().put("single_leg_by_index", JSONObject().put("BNF", JSONObject().put("status", "ADMISSIBLE")))
            .put("by_index", JSONObject().put("BNF", JSONObject().put("decision_id", "d1").put("analysis_ts", "t")
                .put("decision_poll_ts", "p").put("big", "x".repeat(5000))))
            .put("other", "y".repeat(5000))
        val s = JSONObject(C1PilotIo.slice(res, 123L))
        assertEquals(123L, s.getLong("published_ms"))
        assertEquals("d1", s.getJSONObject("by_index").getJSONObject("BNF").getString("decision_id"))
        assertFalse(s.getJSONObject("by_index").getJSONObject("BNF").has("big"))
        assertFalse(s.has("other"))
        val svc = File("src/main/java/com/marketradar/app/MarketWatchService.kt").readText()
        assertTrue(svc.contains("C1PilotIo.offer(filesDir, prefs, resultObj, System.currentTimeMillis())"))
    }
}
