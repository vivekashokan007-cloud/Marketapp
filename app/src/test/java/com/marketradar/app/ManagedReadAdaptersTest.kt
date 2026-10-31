package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/** R7: bounded read adapters with captured/redacted fixture shapes and deterministic clocks; no network. */
class ManagedReadAdaptersTest {
    private fun resource(name: String): String = requireNotNull(
        javaClass.classLoader?.getResourceAsStream("contracts/$name")).bufferedReader().readText()

    private class Fake(val responses: (String, String, String?) -> ManagedReadAdapters.Response) : ManagedReadAdapters.Transport {
        val calls = mutableListOf<Triple<String, String, String?>>()
        override fun call(method: String, url: String, body: String?): ManagedReadAdapters.Response {
            calls.add(Triple(method, url, body)); return responses(method, url, body)
        }
    }

    private var wall = 1_793_592_600_000L
    private var mono = 50_000L
    private val clocks = ManagedReadAdapters.Clocks({ wall.also { wall += 250 } }, { mono })

    private fun tmp(): File = Files.createTempDirectory("sl").toFile().let { File(it, "single_leg_v1") }

    @Test fun contractsFetchedOncePerSessionAndExpiryThenInvalidated() {
        val fx = JSONObject(resource("managed_evidence_upstox_fixtures.json")).getJSONObject("option_contracts_NF").toString()
        val t = Fake { _, _, _ -> ManagedReadAdapters.Response(200, fx) }
        val d = tmp()
        assertNotNull(ManagedReadAdapters.ensureContracts(d, "NF", "2026-10-27", "2026-10-26", t, clocks))
        assertNotNull(ManagedReadAdapters.ensureContracts(d, "NF", "2026-10-27", "2026-10-26", t, clocks))
        assertEquals(1, t.calls.size)                                       // cached
        assertTrue(t.calls[0].second.contains("/v2/option/contract?instrument_key=NSE_INDEX%7CNifty+50&expiry_date=2026-10-27"))
        ManagedReadAdapters.ensureContracts(d, "NF", "2026-11-03", "2026-10-26", t, clocks)   // expiry changed
        ManagedReadAdapters.ensureContracts(d, "NF", "2026-11-03", "2026-10-27", t, clocks)   // session changed
        assertEquals(3, t.calls.size)
        val cache = JSONObject(File(d, ManagedReadAdapters.CONTRACT_CACHE_FILE).readText())
        assertEquals(listOf("NF|2026-11-03|2026-10-27"), cache.keys().asSequence().toList())
    }

    @Test fun failedContractFetchIsRetriedOnlyAfterBackoffAndBounded() {
        val t = Fake { _, _, _ -> ManagedReadAdapters.Response(500, "{}") }
        val d = tmp()
        assertNull(ManagedReadAdapters.ensureContracts(d, "BNF", "2026-10-28", "2026-10-26", t, clocks))
        assertNull(ManagedReadAdapters.ensureContracts(d, "BNF", "2026-10-28", "2026-10-26", t, clocks))
        assertEquals(1, t.calls.size)                                       // within retry window
        repeat(10) { wall += ManagedReadAdapters.CONTRACT_RETRY_MS; ManagedReadAdapters.ensureContracts(d, "BNF", "2026-10-28", "2026-10-26", t, clocks) }
        assertEquals(ManagedReadAdapters.CONTRACT_MAX_FETCHES_PER_SESSION, t.calls.size)
        assertNull(ManagedReadAdapters.ensureContracts(d, "XX", "2026-10-28", "2026-10-26", t, clocks))
    }

    @Test fun quotesExactKeysBoundedWithRecordedClocks() {
        val body = resource("upstox_v3_full_quote_synthetic.json")
        val t = Fake { _, _, _ -> ManagedReadAdapters.Response(200, body) }
        val keys = listOf("SYNTH_FO|NF24100CE", "", "SYNTH_FO|NF24100CE", "K2", "K3", "K4", "K5")
        val q = ManagedReadAdapters.fetchQuotes(keys, t, clocks)!!
        assertEquals(JSONArray(listOf("SYNTH_FO|NF24100CE", "K2", "K3", "K4")).toString(), q.requestedJson)
        assertTrue(t.calls[0].second.startsWith("https://api.upstox.com/v3/market-quote/quotes?instrument_key=SYNTH_FO%7CNF24100CE,K2,K3,K4"))
        assertFalse(t.calls[0].second.contains("K5"))
        assertTrue(q.receiptMs > q.requestStartMs)
        assertEquals(50_000L, q.receiptMonoMs)
        assertNull(ManagedReadAdapters.fetchQuotes(emptyList(), t, clocks))
        assertNull(ManagedReadAdapters.fetchQuotes(listOf("K"), Fake { _, _, _ -> ManagedReadAdapters.Response(401, "{}") }, clocks))
    }

    @Test fun marginOnlyForSellWithExplicitProductAndIdentity() {
        val fx = JSONObject(resource("managed_evidence_upstox_fixtures.json"))
        val t = Fake { _, _, _ -> ManagedReadAdapters.Response(200, fx.getJSONObject("margin_response").toString()) }
        assertNull(ManagedReadAdapters.fetchShortMargin("BUY_CE", "SYNTH_FO|1", 65, t, clocks))
        assertEquals(0, t.calls.size)                                       // BUY never asks for margin
        val m = ManagedReadAdapters.fetchShortMargin("SELL_PE", "SYNTH_FO|1", 65, t, clocks)!!
        val ins = JSONObject(m.requestJson).getJSONArray("instruments")
        assertEquals(1, ins.length())
        val r = ins.getJSONObject(0)
        assertEquals(listOf("SYNTH_FO|1", 65, "I", "SELL"),
            listOf(r.getString("instrument_key"), r.getInt("quantity"), r.getString("product"), r.getString("transaction_type")))
        assertEquals("POST", t.calls[0].first)
        assertTrue(t.calls[0].second.endsWith("/v2/charges/margin"))
    }

    @Test fun monitorWorkIsCoalescedAndRunsWithLatestInput() {
        val c = MonitorCoalescer()
        assertTrue(c.offer("{\"NF\":1}"))
        assertFalse(c.offer("{\"NF\":2}"))        // already queued: not enqueued again
        assertEquals("{\"NF\":2}", c.begin())     // the run takes the latest input when it executes
        assertTrue(c.offer("{\"NF\":3}"))         // after the run began, a new run may be queued
    }

    @Test fun dailyReviewWorkerOutcomeMapping() {
        assertEquals("SUCCESS", DailyReviewScheduler.outcome("{\"status\":\"OK\",\"retry\":false}", 0))
        assertEquals("RETRY", DailyReviewScheduler.outcome("{\"status\":\"OK\",\"retry\":true}", 0))
        assertEquals("SUCCESS", DailyReviewScheduler.outcome("{\"status\":\"OK\",\"retry\":true}", 9))
        assertEquals("RETRY", DailyReviewScheduler.outcome("{\"status\":\"LEDGER_BUSY\"}", 1))
        assertEquals("SUCCESS", DailyReviewScheduler.outcome("{\"status\":\"LEDGER_CORRUPT\"}", 0))
        assertEquals("RETRY", DailyReviewScheduler.outcome("not json", 0))
    }

    @Test fun x1GoldenUnchangedWithAllManagedCollectorsAttached() {
        val snaps = JSONObject(resource("x1_upstox_raw_chains.json")).getJSONArray("snapshots")
        val fx = JSONObject(resource("managed_evidence_upstox_fixtures.json"))
        val out = JSONObject()
        for (i in 0 until snaps.length()) {
            val snap = snaps.getJSONObject(i)
            val raw = snap.getJSONObject("raw")
            val ctx = JSONObject()
            ManagedEvidenceFormatter.attachQuantities(ctx, raw, 1L)
            ManagedEvidenceFormatter.attachContracts(ctx, "NF", fx.getJSONObject("option_contracts_NF"), 2L)
            ManagedEvidenceFormatter.attachMargin(ctx, fx.getJSONObject("margin_request"), fx.getJSONObject("margin_response"), 3L)
            val f = OptionChainBrainFormatter.format(raw, snap.getDouble("spot"))
            OptionChainBrainFormatter.stampRequest(f, snap.getString("requested_expiry"), snap.getString("requested_underlying_key"))
            out.put(snap.getString("id"), f)
        }
        val golden = JSONObject(resource("x1_upstox_formatted_golden.json"))
        fun canon(v: Any?): Any? = when (v) {
            is JSONObject -> JSONObject().also { o -> v.keys().asSequence().sorted().forEach { o.put(it, canon(v.get(it))) } }
            is JSONArray -> JSONArray().also { a -> for (j in 0 until v.length()) a.put(canon(v.get(j))) }
            else -> v
        }
        assertEquals(canon(golden).toString(), canon(out).toString())
    }
}
