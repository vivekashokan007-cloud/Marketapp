package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.util.concurrent.Executor

class PipelineQuoteFailuresTest {
    @Test fun retiredNfNeverReadsStateOrRequestsAQuote() {
        val c = SingleLegAdmissionCoordinator(
            readState = { error("retired index must stop before state access") },
            transport = { error("retired index must not request quotes") },
            clocks = ManagedReadAdapters.Clocks({ 1000L }, { 20L }),
            admit = { error("retired index must not admit") }, reconcile = { null }, cancel = { null },
            executor = Executor { error("retired index must not enqueue") })
        val out = JSONObject(c.submit("NF", "d1", "NSE_FO|1", "retired"))
        assertEquals("REJECTED", out.getString("status"))
        assertEquals(BnfScope.RETIRED, out.getString("reason"))
    }

    @Test fun knownHttpFailureNeverCallsAdmissionAndKeepsClocks() {
        var commits = 0
        val traces = mutableListOf<JSONObject>()
        val state = SingleLegAdmissionCoordinator.State("{}", "BNF", "d1", "NSE_FO|1", "BUY_CE", 30,
            listOf("NSE_FO|1"), true, true, "BNF", "[]", 250000.0, 10.0, 1)
        val c = SingleLegAdmissionCoordinator(readState = { state },
            transport = { ManagedReadAdapters.Transport { _, _, _ -> ManagedReadAdapters.Response(401, null) } },
            clocks = ManagedReadAdapters.Clocks({ 1000L }, { 20L }),
            admit = { commits++; """{"status":"ADMITTED"}""" }, reconcile = { null }, cancel = { null },
            executor = Executor { it.run() }, trace = { traces.add(it) })
        c.submit("BNF", "d1", "NSE_FO|1", "r1")
        val out = JSONObject(c.status("r1"))
        assertEquals("REJECTED", out.getString("status"))
        assertEquals("HTTP_401", out.getString("reason"))
        assertEquals(0, commits)
        val q = traces.single { it.optString("kind") == "N_QUOTE_RESULT" }
        assertEquals(401, q.getInt("http_status"))
        assertEquals(1000L, q.getLong("request_start_ms"))
        assertEquals(20L, q.getLong("receipt_mono_ms"))
        assertTrue(out.getBoolean("quote_requested"))
    }

    @Test fun timeoutHasItsOwnReasonAndClock() {
        val out = ManagedReadAdapters.fetchQuotesOutcome(listOf("NSE_FO|1"),
            ManagedReadAdapters.Transport { _, _, _ -> throw java.net.SocketTimeoutException() },
            ManagedReadAdapters.Clocks({ 1000L }, { 20L }))
        assertEquals("TIMEOUT", out.failure)
        assertEquals(1000L, out.requestStartMs)
        assertEquals(1000L, out.receiptMs)
        assertNull(out.fetch)
        assertTrue(out.retryable)
    }

    @Test fun emptyHttp200BodyIsResponseFailureNotUnrequested() {
        val out = ManagedReadAdapters.fetchQuotesOutcome(listOf("NSE_FO|1"),
            ManagedReadAdapters.Transport { _, _, _ -> ManagedReadAdapters.Response(200, null) },
            ManagedReadAdapters.Clocks({ 1000L }, { 20L }))
        assertEquals("BODY_MISSING:HTTP_200", out.failure)
        assertEquals(200, out.httpStatus)
        assertNull(out.fetch)
    }
}
