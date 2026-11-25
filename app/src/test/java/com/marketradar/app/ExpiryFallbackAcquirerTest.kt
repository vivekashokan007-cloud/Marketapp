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

/** expiry_fallback_v1 native acquisition: SYNTHETIC fixture shapes, fake transport, deterministic clocks.
 *  R2 adds: total acquisition deadline (slow / failed fake transports across NF and BNF), markWait, weekly flags. */
class ExpiryFallbackAcquirerTest {
    private class Fake(val responses: (String) -> ManagedReadAdapters.Response) : ManagedReadAdapters.Transport {
        val calls = mutableListOf<String>()
        override fun call(method: String, url: String, body: String?): ManagedReadAdapters.Response {
            calls.add(url); return responses(url)
        }
    }

    private var wall = 1_795_420_800_000L
    private val clocks = ManagedReadAdapters.Clocks({ wall.also { wall += 100 } }, { 1L })
    private fun tmp(): File = Files.createTempDirectory("efb").toFile().let { File(it, "single_leg_v1").apply { mkdirs() } }

    private fun listingBody(): String {
        val rows = JSONArray()
        for (e in listOf("2026-11-23", "2026-12-29", "2026-12-29", "2026-12-29")) {
            rows.put(JSONObject().put("expiry", e).put("underlying_key", "NSE_INDEX|Nifty Bank"))
        }
        rows.put(JSONObject().put("expiry", "2026-11-25").put("underlying_key", "NSE_INDEX|Nifty 50"))  // foreign
        return JSONObject().put("status", "success").put("data", rows).toString()
    }
    private val contractsBody = JSONObject().put("status", "success").put("data", JSONArray()
        .put(JSONObject().put("instrument_key", "NSE_FO|1").put("expiry", "2026-12-29"))).toString()
    private val chainBody = JSONObject().put("status", "success").put("data", JSONArray()).toString()

    private fun fake() = Fake { url ->
        when {
            url.contains("/v2/option/contract?") && !url.contains("expiry_date") -> ManagedReadAdapters.Response(200, listingBody())
            url.contains("/v2/option/contract?") -> ManagedReadAdapters.Response(200, contractsBody)
            url.contains("/v2/option/chain?") -> ManagedReadAdapters.Response(200, chainBody)
            else -> ManagedReadAdapters.Response(404, null)
        }
    }

    private val fmt: (JSONObject, Double) -> JSONObject? = { _, _ ->
        JSONObject().put("strikes", JSONObject().put("52000", JSONObject())).put("atm", 52000)
    }

    private fun result(status: String = "EVIDENCE_REQUIRED", planDid: String = "idx1_a", decDid: String = "idx1_a"): JSONObject {
        val dec = JSONObject().put("decision_id", decDid).put("direction", JSONObject().put("inputs",
            JSONObject().put("spot", JSONObject().put("value", 52010.0))))
        val plan = JSONObject().put("decision_id", planDid).put("expiry_fallback",
            JSONObject().put("status", status).put("decision_id", planDid).put("trade_expiry", "2026-11-23"))
        return JSONObject().put("by_index", JSONObject().put("BNF", dec).put("NF", JSONObject().put("decision_id", "idx1_b")))
            .put("single_leg_by_index", JSONObject().put("BNF", plan).put("NF", JSONObject().put("decision_id", "idx1_b")))
    }

    @Test fun needsOnlyEvidenceRequiredWithMatchingDecision() {
        assertEquals(listOf("BNF"), ExpiryFallbackAcquirer.needs(result()).map { it.index })
        assertTrue(ExpiryFallbackAcquirer.needs(result(status = "WAIT")).isEmpty())
        assertTrue(ExpiryFallbackAcquirer.needs(result(decDid = "idx1_newer")).isEmpty())
        assertTrue(ExpiryFallbackAcquirer.needs(JSONObject()).isEmpty())            // ordinary day: nothing asked
    }

    @Test fun acquiresListingContractsChainOnceAndCountsPerIndex() {
        val d = tmp()
        // the trading-expiry contracts cache must not be touched (ensureContracts prunes same-index expiries)
        val trading = File(d, ManagedReadAdapters.CONTRACT_CACHE_FILE)
        trading.writeText(JSONObject().put("BNF|2026-11-23|2026-11-23", JSONObject().put("fetches", 1)).toString())
        val before = trading.readText()
        val t = fake()
        val resolved = mutableListOf<JSONObject>()
        val resolve: (String, String, Long) -> String = { ix, li, _ ->
            resolved.add(JSONObject(li)); JSONObject().put("index", ix).put("expiry", "2026-12-29").toString()
        }
        val needs = ExpiryFallbackAcquirer.needs(result())
        val ev = ExpiryFallbackAcquirer.acquire(needs, d, "2026-11-23", { t }, clocks, resolve, fmt)
        val bnf = ev.getJSONObject("BNF")
        assertEquals(listOf("2026-11-23", "2026-12-29"),
            (0 until bnf.getJSONObject("listing").getJSONArray("expiries").length()).map { bnf.getJSONObject("listing").getJSONArray("expiries").getString(it) })
        assertEquals(1, bnf.getJSONObject("listing").getInt("foreign_rows"))
        assertEquals("2026-12-29", bnf.getJSONObject("contracts").getString("expiry"))
        val ch = bnf.getJSONObject("chain")
        assertEquals("2026-12-29", ch.getJSONObject("chain").getString("requested_expiry"))
        assertEquals("NSE_INDEX|Nifty Bank", ch.getJSONObject("chain").getString("requested_underlying_key"))
        assertTrue(ch.getLong("receipt_ms") >= ch.getLong("request_start_ms"))
        val req = bnf.getJSONObject("stats").getJSONObject("requests")
        assertEquals(listOf(1, 1, 1), listOf(req.getInt("listing"), req.getInt("contracts"), req.getInt("chain")))
        assertTrue(bnf.getJSONObject("stats").getJSONObject("response_bytes").getLong("listing") > 0)
        assertTrue(t.calls[2].contains("/v2/option/chain?instrument_key=NSE_INDEX%7CNifty+Bank&expiry_date=2026-12-29"))
        assertFalse(ev.has("NF"))
        assertEquals(before, trading.readText())
        // second poll: listing and contracts cached, only the chain is requested again
        val ev2 = ExpiryFallbackAcquirer.acquire(needs, d, "2026-11-23", { t }, clocks, resolve, fmt)
        val req2 = ev2.getJSONObject("BNF").getJSONObject("stats").getJSONObject("requests")
        assertEquals(listOf(0, 0, 1), listOf(req2.getInt("listing"), req2.getInt("contracts"), req2.getInt("chain")))
        assertEquals(4, t.calls.size)
    }

    @Test fun unresolvedExpiryMakesNoContractOrChainRequest() {
        val t = fake()
        val ev = ExpiryFallbackAcquirer.acquire(ExpiryFallbackAcquirer.needs(result()), tmp(), "2026-11-23", { t }, clocks,
            { _, _, _ -> JSONObject().put("expiry", JSONObject.NULL).put("reason", "BNF_MONTHLY_NOT_LISTED").toString() }, fmt)
        val bnf = ev.getJSONObject("BNF")
        assertFalse(bnf.has("contracts")); assertFalse(bnf.has("chain"))
        assertEquals(1, t.calls.size)
    }

    @Test fun oversizeOrFailedListingIsBoundedAndRetriedOnlyAfterBackoff() {
        val big = Fake { ManagedReadAdapters.Response(200, "x".repeat(ExpiryFallbackAcquirer.LISTING_MAX_BODY_BYTES + 1)) }
        val d = File(tmp(), ExpiryFallbackAcquirer.DIR)
        assertNull(ExpiryFallbackAcquirer.listing(d, "BNF", "2026-11-23", big, clocks))
        assertNull(ExpiryFallbackAcquirer.listing(d, "BNF", "2026-11-23", big, clocks))
        assertEquals(1, big.calls.size)                          // inside the retry window
        wall += ExpiryFallbackAcquirer.LISTING_RETRY_MS + 1
        assertNull(ExpiryFallbackAcquirer.listing(d, "BNF", "2026-11-23", big, clocks))
        wall += ExpiryFallbackAcquirer.LISTING_RETRY_MS + 1
        assertNull(ExpiryFallbackAcquirer.listing(d, "BNF", "2026-11-23", big, clocks))
        wall += ExpiryFallbackAcquirer.LISTING_RETRY_MS + 1
        assertNull(ExpiryFallbackAcquirer.listing(d, "BNF", "2026-11-23", big, clocks))
        assertEquals(ExpiryFallbackAcquirer.LISTING_MAX_FETCHES_PER_SESSION, big.calls.size)
    }

    @Test fun chainRequestsAreCappedPerIndexPerSession() {
        val d = tmp()
        val t = fake()
        val resolve: (String, String, Long) -> String = { _, _, _ -> JSONObject().put("expiry", "2026-12-29").toString() }
        val needs = ExpiryFallbackAcquirer.needs(result())
        repeat(ExpiryFallbackAcquirer.CHAIN_MAX_FETCHES_PER_SESSION + 3) {
            ExpiryFallbackAcquirer.acquire(needs, d, "2026-11-23", { t }, clocks, resolve, fmt)
        }
        assertEquals(ExpiryFallbackAcquirer.CHAIN_MAX_FETCHES_PER_SESSION, t.calls.count { it.contains("/v2/option/chain?") })
    }

    @Test fun patchReplacesPlansAndOnlyExistingWhyRecords() {
        val r = result().put("index_authority_persist", JSONObject().put("delta",
            JSONObject().put("why", JSONObject().put("idx1_a", JSONObject().put("old", true)))))
        val patch = JSONObject().put("single_leg_by_index", JSONObject().put("NF", JSONObject().put("status", "ADMISSIBLE")))
            .put("why", JSONObject().put("idx1_a", JSONObject().put("new", true)).put("idx1_other", JSONObject()))
        assertEquals(1, ExpiryFallbackAcquirer.applyPatch(r, patch))
        assertEquals("ADMISSIBLE", r.getJSONObject("single_leg_by_index").getJSONObject("NF").getString("status"))
        val why = r.getJSONObject("index_authority_persist").getJSONObject("delta").getJSONObject("why")
        assertTrue(why.getJSONObject("idx1_a").has("new")); assertFalse(why.has("idx1_other"))
        assertEquals("idx1_b", r.getJSONObject("single_leg_by_index").getJSONObject("BNF").getString("decision_id"))
        assertEquals(0, ExpiryFallbackAcquirer.applyPatch(r, JSONObject().put("error", "X")))
        assertNotNull(r.optJSONObject("single_leg_by_index"))
    }

    // ---- R2 (Codex R1 reply s4): total acquisition deadline across NF and BNF -------------------------------
    private val realClocks = ManagedReadAdapters.Clocks({ System.currentTimeMillis() }, { System.nanoTime() / 1_000_000L })

    private fun result2(): JSONObject {
        fun dec(did: String, spot: Double) = JSONObject().put("decision_id", did).put("direction", JSONObject().put("inputs",
            JSONObject().put("spot", JSONObject().put("value", spot))))
        fun plan(did: String) = JSONObject().put("decision_id", did).put("reasons", JSONArray().put("EXPIRY_DAY_NO_NEXT_CHAIN")
            .put("EXPIRY_FALLBACK_EVIDENCE_PENDING")).put("status", "BLOCKED").put("admit", false)
            .put("expiry_fallback", JSONObject().put("status", "EVIDENCE_REQUIRED").put("decision_id", did)
                .put("trade_expiry", "2026-11-23").put("base_reasons", JSONArray().put("EXPIRY_DAY_NO_NEXT_CHAIN"))
                .put("base_status", "BLOCKED"))
        return JSONObject().put("by_index", JSONObject().put("NF", dec("idx1_n", 24010.0)).put("BNF", dec("idx1_b", 52010.0)))
            .put("single_leg_by_index", JSONObject().put("NF", plan("idx1_n")).put("BNF", plan("idx1_b")))
    }

    private fun slowFake(slowMs: Long, slowWhen: (String) -> Boolean, log: MutableList<String> = mutableListOf()) =
        ManagedReadAdapters.Transport { _, url, _ ->
            log.add(url)
            if (slowWhen(url)) Thread.sleep(slowMs)
            fake().call("GET", url, null)
        }

    private val resolveAny: (String, String, Long) -> String = { ix, _, _ ->
        JSONObject().put("index", ix).put("expiry", if (ix == "NF") "2026-12-01" else "2026-12-29").toString()
    }

    private fun acq(o: JSONObject, ix: String) = o.getJSONObject(ix).getJSONObject("stats").getJSONObject("acquisition")

    @Test fun retiredNfMakesNoRequestAndSlowBnfRespectsWholeBudget() {
        val budget = 1_200L
        val calls = mutableListOf<String>()
        val t = slowFake(3_000L, { it.contains("/v2/option/chain?") }, calls)
        val t0 = System.nanoTime()
        val ev = ExpiryFallbackAcquirer.acquire(ExpiryFallbackAcquirer.needs(result2()), tmp(), "2026-11-23", { t },
            realClocks, resolveAny, fmt, budget)
        val took = (System.nanoTime() - t0) / 1_000_000L
        assertTrue("acquire returned in ${took}ms", took < budget + 400)
        assertFalse(ev.has("NF"))
        assertFalse(calls.any { it.contains("Nifty+50") })
        assertEquals("DEADLINE_EXHAUSTED", acq(ev, "BNF").getString("status"))
        assertFalse(ev.getJSONObject("BNF").has("chain"))
        assertEquals(1, ev.getJSONObject("BNF").getJSONObject("stats").getJSONObject("requests").getInt("chain"))
        assertEquals(budget, acq(ev, "BNF").getLong("budget_ms"))
    }

    @Test fun slowListingTimesOutAndItsLateResultIsNeverCachedOrCounted() {
        val d = tmp()
        val t = slowFake(1_500L, { !it.contains("expiry_date") && it.contains("/v2/option/contract?") })
        val ev = ExpiryFallbackAcquirer.acquire(ExpiryFallbackAcquirer.needs(result2()), d, "2026-11-23", { t },
            realClocks, resolveAny, fmt, 600L)
        for (ix in listOf("BNF")) {
            assertEquals("DEADLINE_EXHAUSTED", acq(ev, ix).getString("status"))
            assertFalse(ev.getJSONObject(ix).has("listing"))
        }
        val bytesBefore = ev.getJSONObject("BNF").getJSONObject("stats").toString()
        Thread.sleep(1_800L)                                                     // the abandoned calls finish now
        val cache = JSONObject(File(File(d, ExpiryFallbackAcquirer.DIR), ExpiryFallbackAcquirer.LISTING_CACHE_FILE).readText())
        assertFalse(cache.getJSONObject("NF|2026-11-23").has("expiries"))
        assertEquals(-2, cache.getJSONObject("NF|2026-11-23").getInt("last_code"))
        assertEquals(bytesBefore, ev.getJSONObject("BNF").getJSONObject("stats").toString())
    }

    @Test fun zeroBudgetIssuesNoRequestForEitherIndex() {
        val calls = mutableListOf<String>()
        val t = slowFake(0L, { false }, calls)
        val ev = ExpiryFallbackAcquirer.acquire(ExpiryFallbackAcquirer.needs(result2()), tmp(), "2026-11-23", { t },
            realClocks, resolveAny, fmt, 0L)
        assertTrue(calls.isEmpty())
        for (ix in listOf("BNF")) {
            assertEquals("DEADLINE_EXHAUSTED", acq(ev, ix).getString("status"))
            assertEquals(0, acq(ev, ix).getInt("requests"))
        }
    }

    @Test fun failedTransportIsNotADeadlineAndLeavesNamedGapsForPython() {
        val t = ManagedReadAdapters.Transport { _, _, _ -> ManagedReadAdapters.Response(503, null) }
        val ev = ExpiryFallbackAcquirer.acquire(ExpiryFallbackAcquirer.needs(result2()), tmp(), "2026-11-23", { t },
            realClocks, resolveAny, fmt, 2_000L)
        for (ix in listOf("BNF")) {
            assertEquals("COMPLETE", acq(ev, ix).getString("status"))
            assertFalse(ev.getJSONObject(ix).has("listing"))
            assertEquals(1, acq(ev, ix).getInt("requests"))
        }
    }

    @Test fun markWaitWithdrawsOnlyActivePendingPlansAndStopsAcquisition() {
        val r = result2()
        val nfBefore = r.getJSONObject("single_leg_by_index").getJSONObject("NF").toString()
        assertEquals(1, ExpiryFallbackAcquirer.markWait(r, "REFRESH_NOT_ACQUIRED"))
        val bnf = r.getJSONObject("single_leg_by_index").getJSONObject("BNF")
        assertEquals("WAIT", bnf.getJSONObject("expiry_fallback").getString("status"))
        assertEquals("REFRESH_NOT_ACQUIRED", bnf.getJSONObject("expiry_fallback").getString("reason"))
        assertEquals(listOf("EXPIRY_DAY_NO_NEXT_CHAIN", "EXPIRY_FALLBACK_WAIT:REFRESH_NOT_ACQUIRED"),
            (0 until bnf.getJSONArray("reasons").length()).map { bnf.getJSONArray("reasons").getString(it) })
        assertFalse(bnf.getBoolean("admit"))
        assertEquals(nfBefore, r.getJSONObject("single_leg_by_index").getJSONObject("NF").toString())
        assertTrue(ExpiryFallbackAcquirer.needs(r).isEmpty())
        assertEquals(0, ExpiryFallbackAcquirer.markWait(r, "NO_TOKEN"))
        val applied = result2()
        applied.getJSONObject("single_leg_by_index").getJSONObject("BNF").getJSONObject("expiry_fallback").put("status", "APPLIED")
        assertEquals(0, ExpiryFallbackAcquirer.markWait(applied, "NO_TOKEN"))
    }

    @Test fun listingKeepsWeeklyFlagsPerExpiry() {
        val rows = JSONArray()
        fun row(e: String, w: Any?) = JSONObject().put("expiry", e).put("underlying_key", "NSE_INDEX|Nifty Bank")
            .also { if (w != null) it.put("weekly", w) }
        rows.put(row("2026-11-23", false)).put(row("2026-12-29", false)).put(row("2026-12-29", false))
            .put(row("2027-01-26", true)).put(row("2027-01-26", false)).put(row("2027-02-23", null))
        val t = Fake { ManagedReadAdapters.Response(200, JSONObject().put("status", "success").put("data", rows).toString()) }
        val li = ExpiryFallbackAcquirer.listing(File(tmp(), ExpiryFallbackAcquirer.DIR), "BNF", "2026-11-23", t, clocks)!!
        val w = li.getJSONObject("weekly")
        assertEquals(false, w.get("2026-12-29")); assertEquals("MIXED", w.get("2027-01-26")); assertFalse(w.has("2027-02-23"))
    }
}
