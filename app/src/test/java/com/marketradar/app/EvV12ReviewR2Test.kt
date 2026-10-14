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
import java.time.Instant

/** Codex review R2 (14 Oct 2026): native adapter, policy identity, close journal, telemetry classification. */
class EvV12ReviewR2Test {
    private fun root(): File = listOf(File("."), File("app")).first { File(it, "src/test/resources/ev_v12").isDirectory }

    private val receipt = Instant.parse("2026-10-12T09:46:00Z").toEpochMilli()     // 15:16 IST
    private fun iso(ms: Long) = Instant.ofEpochMilli(ms).toString()
    private fun src(key: String, ageMs: Long = 2_000, exact: Boolean = true, resp: String? = key, ts: String? = iso(receipt - ageMs),
                    bid: Double? = 10.0, ask: Double? = 10.5) =
        LegSourceQuote(key, true, exact, resp, bid, ask, ts, null)

    // ---------------- R2: quote adapter ----------------
    @Test
    fun adapterUsesSourceTimeNotReceipt() {
        val r = RuleBQuoteAdapter.adapt(listOf("A", "B"), mapOf("A" to src("A", 3_000), "B" to src("B", 1_000)), receipt)
        assertTrue(r.valid)
        assertEquals((receipt - 3_000).toDouble(), r.policyQuotes()["A"]!!.tsMs!!, 0.0)
        assertEquals(receipt - 3_000, r.acceptanceQuotesJson().getJSONObject("A").getLong("ts_ms"))
        assertTrue(r.acceptanceQuotesJson().getJSONObject("A").getBoolean("source_time_valid"))
    }

    @Test
    fun adapterRejectsFuzzyOldMissingFutureAndOtherDay() {
        fun bad(s: LegSourceQuote, reason: String) {
            val r = RuleBQuoteAdapter.adapt(listOf("A", "B"), mapOf("A" to s, "B" to src("B")), receipt)
            assertFalse(reason, r.valid)
            assertTrue(reason + " " + r.reasons, r.reasons.any { it.contains(reason) })
            // the policy never sees a fresh time for a rejected book
            assertNull(r.policyQuotes()["A"]!!.tsMs)
            assertFalse(r.acceptanceQuotesJson().getJSONObject("A").getBoolean("source_time_valid"))
            val step = RuleBExitPolicy.step(RuleBExitPolicy.Position(RuleBExitPolicy.POLICY_ID, "OPEN", "2026-10-12",
                "2026-10-13", listOf(RuleBExitPolicy.Leg(-1, "A"), RuleBExitPolicy.Leg(1, "B"))),
                RuleBExitPolicy.Tick("2026-10-12T15:16:00+05:30", r.policyQuotes()))
            assertEquals(reason, "LEG_QUOTE_INVALID", step.reason)
        }
        bad(src("A", exact = false), "key_match_inexact")
        bad(src("A", ageMs = 91_000), "source_stale")                // one stale wing
        bad(src("A", ts = null), "source_ts_missing")
        bad(src("A", ts = "garbage"), "source_ts_unparseable")
        bad(src("A", ageMs = -6_000), "source_ts_future")
        bad(src("A", ts = "2026-10-09T09:45:00Z"), "source_different_session_date")
        bad(src("A", resp = "B"), "duplicate_source_quote")
        bad(src("A", bid = 11.0, ask = 10.0), "book_invalid")
        // refreshed partial response: one leg absent entirely
        val partial = RuleBQuoteAdapter.adapt(listOf("A", "B"), mapOf("B" to src("B")), receipt)
        assertFalse(partial.valid); assertTrue(partial.reasons.any { it.contains("no_quote") })
        assertFalse(partial.acceptanceQuotesJson().getJSONObject("A").getBoolean("exact_key"))
        // inter-leg skew beyond 60 s
        val skew = RuleBQuoteAdapter.adapt(listOf("A", "B"), mapOf("A" to src("A", 1_000), "B" to src("B", 80_000)), receipt)
        assertFalse(skew.valid); assertTrue(skew.reasons.contains("inter_leg_skew"))
        // fetch failure
        assertFalse(RuleBQuoteAdapter.adapt(listOf("A"), mapOf("A" to src("A")), receipt, "HTTP_500").valid)
    }

    @Test
    fun frozenLegsEnforced() {
        val es = JSONObject().put("entry_fill", JSONObject().put("legs", JSONArray()
            .put(JSONObject().put("instrument_key", "A").put("qty", -1))
            .put(JSONObject().put("instrument_key", "B").put("qty", 1))))
        val frozen = RuleBQuoteAdapter.frozenLegs(es)!!
        assertTrue(RuleBQuoteAdapter.legsMatch(frozen, listOf(RuleBExitPolicy.Leg(1, "B"), RuleBExitPolicy.Leg(-1, "A"))))
        assertFalse(RuleBQuoteAdapter.legsMatch(frozen, listOf(RuleBExitPolicy.Leg(1, "A"), RuleBExitPolicy.Leg(-1, "B"))))
        assertFalse(RuleBQuoteAdapter.legsMatch(frozen, listOf(RuleBExitPolicy.Leg(-1, "A"), RuleBExitPolicy.Leg(1, "C"))))
        assertFalse(RuleBQuoteAdapter.legsMatch(frozen, listOf(RuleBExitPolicy.Leg(-1, "A"))))
        assertNull(RuleBQuoteAdapter.frozenLegs(JSONObject()))
        assertNull(RuleBQuoteAdapter.frozenLegs(JSONObject().put("entry_fill", JSONObject().put("legs", JSONArray()
            .put(JSONObject().put("instrument_key", "A").put("qty", 0))))))
    }

    // ---------------- R2: policy identity ----------------
    @Test
    fun policyClassFailsClosedForUnknownAndKeepsOlderTrades() {
        fun t(paper: Boolean, es: Any?) = JSONObject().put("paper", paper).apply { if (es != null) put("entry_snapshot", es) }
        assertEquals(RuleBExitPolicy.PolicyClass.RULE_B, RuleBExitPolicy.policyClass(t(true, JSONObject().put("position_policy_id", "paper_rule_b_v1"))))
        assertEquals(RuleBExitPolicy.PolicyClass.RULE_B, RuleBExitPolicy.policyClass(t(true, "{\"position_policy_id\":\"paper_rule_b_v1\"}")))
        assertEquals(RuleBExitPolicy.PolicyClass.UNKNOWN_EXPERIMENT, RuleBExitPolicy.policyClass(t(true, JSONObject().put("position_policy_id", "paper_rule_c_v9"))))
        // older non-experiment Paper trades and every Real trade stay on the legacy path
        assertEquals(RuleBExitPolicy.PolicyClass.LEGACY, RuleBExitPolicy.policyClass(t(true, JSONObject().put("selection_source", "pc2"))))
        assertEquals(RuleBExitPolicy.PolicyClass.LEGACY, RuleBExitPolicy.policyClass(t(true, null)))
        assertEquals(RuleBExitPolicy.PolicyClass.LEGACY, RuleBExitPolicy.policyClass(t(false, JSONObject().put("position_policy_id", "paper_rule_b_v1"))))
        assertEquals(RuleBExitPolicy.PolicyClass.LEGACY, RuleBExitPolicy.policyClass(t(true, "{bad")))
    }

    @Test
    fun publishedActionIsRuleBNeverShadow() {
        for (st in listOf(null, "OPEN", "LATE_EXIT_PENDING", "EXIT_FILLED", "UNRESOLVED")) {
            val a = RuleBExitPolicy.publishedAction(st?.let { RuleBExitPolicy.Step(it, "NONE", "x") })
            assertTrue(a.startsWith("RULE_B_")); assertFalse(a.startsWith("SHADOW_"))
        }
        // service wiring: experiment trades never reach the legacy SHADOW notifier
        val svc = File(root(), "src/main/java/com/marketradar/app/PositionTickService.kt").readText()
        assertEquals(1, Regex("maybeNotifyShadowExit\\(row, trade\\)").findAll(svc).count())
        assertTrue(svc.contains("RuleBExitPolicy.PolicyClass.LEGACY -> maybeNotifyShadowExit(row, trade)"))
        assertTrue(svc.contains("\"POLICY_UNKNOWN_FAIL_CLOSED\""))
    }

    // ---------------- R3: durable close journal ----------------
    private fun fillStep() = RuleBExitPolicy.Step("EXIT_FILLED", "FILL", "PLANNED_EXIT",
        RuleBExitPolicy.Fill("PLANNED_EXIT", "2026-10-12T15:16:00+05:30",
            listOf(RuleBExitPolicy.FillLeg("A", -1, 10.5, 10.0, 10.5, 1.0)), -10.5))

    @Test
    fun closeJournalAcksOnlyAfterRemoteProofAndSurvivesRestart() {
        val dir = Files.createTempDirectory("rbj").toFile()
        assertTrue(RuleBFillLedger.record(dir, "t1", "d1", fillStep(), 1))
        assertEquals(RuleBFillLedger.PENDING_APPLY, RuleBFillLedger.entry(dir, "t1")!!.getString("delivery"))
        // no ACK without a journaled patch, and only remote states are accepted
        assertFalse(RuleBFillLedger.ack(dir, "t1", RuleBFillLedger.REMOTE_CONFIRMED, 2))
        assertFalse(RuleBFillLedger.ack(dir, "t1", "ACKED", 2))
        val patch = "{\"status\":\"CLOSED\",\"net_pnl\":-251.33}"
        assertEquals("JOURNALED", RuleBFillLedger.journal(dir, "t1", patch, 3))
        assertEquals("ALREADY_JOURNALED", RuleBFillLedger.journal(dir, "t1", patch, 4))
        assertEquals("JOURNALED_PATCH_DIFFERS", RuleBFillLedger.journal(dir, "t1", "{\"status\":\"CLOSED\"}", 4))
        assertTrue(RuleBFillLedger.noteRemoteAttempt(dir, "t1", 5, "updateTrade=false"))
        // "restart": a fresh read still offers the entry WITH the journaled patch
        val p = RuleBFillLedger.pending(dir)
        assertEquals(1, p.length())
        assertEquals(-251.33, p.getJSONObject(0).getJSONObject("journal_patch").getDouble("net_pnl"), 0.0)
        assertEquals(1, p.getJSONObject(0).getInt("remote_attempts"))
        assertTrue(RuleBFillLedger.ack(dir, "t1", RuleBFillLedger.REMOTE_CONFIRMED, 6))
        assertEquals(0, RuleBFillLedger.pending(dir).length())
        assertEquals("ALREADY_DELIVERED", RuleBFillLedger.journal(dir, "t1", patch, 7))
    }

    @Test
    fun unresolvedIsJournaledLikeAFill() {
        val dir = Files.createTempDirectory("rbu").toFile()
        RuleBFillLedger.record(dir, "t2", "d2", RuleBExitPolicy.Step("UNRESOLVED", "MARK_UNRESOLVED", "NO_VALID_QUOTE_BY_EXPIRY_DEADLINE"), 1)
        assertEquals(1, RuleBFillLedger.pending(dir).length())
        assertFalse(RuleBFillLedger.ack(dir, "t2", RuleBFillLedger.REMOTE_CONFIRMED, 2))
        RuleBFillLedger.journal(dir, "t2", "{\"rule_b_state\":\"UNRESOLVED\"}", 2)
        assertEquals(1, RuleBFillLedger.pending(dir).length())
        assertTrue(RuleBFillLedger.ack(dir, "t2", RuleBFillLedger.REMOTE_CONFIRMED, 3))
        assertEquals(0, RuleBFillLedger.pending(dir).length())
    }

    @Test
    fun manualCloseStopsRuleBOrConflicts() {
        val dir = Files.createTempDirectory("rbm").toFile()
        assertEquals(RuleBFillLedger.MANUAL_PENDING, RuleBFillLedger.noteManualClose(dir, "t3", "d3", 1))
        assertFalse(RuleBFillLedger.record(dir, "t3", "d3", fillStep(), 2))     // Rule B never fills after a manual close
        assertEquals("MANUAL_CLOSED", RuleBFillLedger.stateOf(dir, "t3"))
        assertEquals(0, RuleBFillLedger.pending(dir).length())                  // nothing journaled yet
        // racing fill: the conditional remote update decides
        RuleBFillLedger.record(dir, "t4", "d4", fillStep(), 3)
        assertEquals("RULE_B_TERMINAL_PENDING_DELIVERY", RuleBFillLedger.noteManualClose(dir, "t4", "d4", 4))
        RuleBFillLedger.journal(dir, "t4", "{}", 5)
        assertTrue(RuleBFillLedger.ack(dir, "t4", RuleBFillLedger.MANUAL_CLOSE_CONFLICT, 6))
        assertEquals(RuleBFillLedger.MANUAL_CLOSE_CONFLICT, RuleBFillLedger.entry(dir, "t4")!!.getString("delivery"))
    }

    @Test
    fun undeliveredEntriesAreNeverPruned() {
        val trades = JSONObject()
        for (i in 0 until RuleBFillLedger.MAX_ENTRIES + 5)
            trades.put("d$i", JSONObject().put("state", "EXIT_FILLED").put("delivery", RuleBFillLedger.REMOTE_CONFIRMED)
                .put("acked_at_ms", 0L).put("recorded_at_ms", i.toLong()))
        trades.put("live", JSONObject().put("state", "EXIT_FILLED").put("delivery", RuleBFillLedger.REMOTE_PENDING)
            .put("recorded_at_ms", 0L))
        RuleBFillLedger.prune(trades, RuleBFillLedger.RETAIN_MS + 1)
        assertTrue(trades.has("live"))
        assertEquals(1, trades.length())
    }

    // ---------------- R7: telemetry classification against captured PostgREST bodies ----------------
    @Test
    fun classificationMatchesCapturedServerBodies() {
        val cases = JSONObject(File(root(), "src/test/resources/ev_v12/rest_bodies.json").readText()).getJSONObject("cases")
        val expect = mapOf(
            "decision_quota" to EvTelemetryOutbox.Disposition.RETRY_QUOTA,
            "outcome_before_decision" to EvTelemetryOutbox.Disposition.RETRY_PREREQUISITE,
            "decision_oversize" to EvTelemetryOutbox.Disposition.PERMANENT_REJECTED,
            "decision_malformed" to EvTelemetryOutbox.Disposition.PERMANENT_REJECTED,
            "decision_too_old" to EvTelemetryOutbox.Disposition.PERMANENT_REJECTED,
            "decision_unregistered_device" to EvTelemetryOutbox.Disposition.RETRY_TRANSPORT,
            "builder_rpc_denied_anon" to EvTelemetryOutbox.Disposition.RETRY_TRANSPORT,
        )
        for ((name, want) in expect) {
            val c = cases.getJSONObject(name)
            val e = EvTelemetryOutbox.Entry("decision", "x", "t", "s", 0, 0, 0, File("x"))
            assertEquals(name, want, EvTelemetryOutbox.classify("decision", e,
                EvTelemetryOutbox.Response(c.getInt("http"), c.getString("body"))))
        }
        // 2xx ACKs: the exact id + sha must match; CONFLICT names the same id
        val acc = cases.getJSONObject("decision_accepted")
        val ackBody = JSONObject(acc.getString("body"))
        val mine = EvTelemetryOutbox.Entry("decision", ackBody.getString("decision_id"), "t",
            ackBody.getString("envelope_sha256"), 0, 0, 0, File("x"))
        assertEquals(EvTelemetryOutbox.Disposition.ACKED, EvTelemetryOutbox.classify("decision", mine,
            EvTelemetryOutbox.Response(200, acc.getString("body"))))
        assertEquals(EvTelemetryOutbox.Disposition.ACKED, EvTelemetryOutbox.classify("decision", mine,
            EvTelemetryOutbox.Response(200, cases.getJSONObject("decision_duplicate").getString("body"))))
        val conflictBody = cases.getJSONObject("decision_conflict").getString("body")
        val other = mine.copy(id = JSONObject(conflictBody).getString("decision_id"), sha256 = "1".repeat(64))
        assertEquals(EvTelemetryOutbox.Disposition.CONFLICT, EvTelemetryOutbox.classify("decision", other,
            EvTelemetryOutbox.Response(200, conflictBody)))
        assertEquals(EvTelemetryOutbox.Disposition.RETRY_TRANSPORT, EvTelemetryOutbox.classify("decision",
            mine.copy(sha256 = "0".repeat(64)), EvTelemetryOutbox.Response(200, acc.getString("body"))))
        assertEquals(EvTelemetryOutbox.Disposition.RETRY_TRANSPORT, EvTelemetryOutbox.classify("decision", mine, null))
        assertEquals(EvTelemetryOutbox.Disposition.RETRY_TRANSPORT, EvTelemetryOutbox.classify("decision", mine,
            EvTelemetryOutbox.Response(503, "{\"code\":\"PGRST002\"}")))
    }

    @Test
    fun quotaRetriesNextIstDayAndIsNotAConflict() {
        val dir = Files.createTempDirectory("evq").toFile()
        val text = "{\"q\":1}"; val sha = EvTelemetryOutbox.sha256(text)
        val now = Instant.parse("2026-10-14T12:00:00Z").toEpochMilli()      // 17:30 IST
        EvTelemetryOutbox.enqueue(dir, "decision", "q1", text, sha, now)
        val r = EvTelemetryOutbox.drain(dir, now) { EvTelemetryOutbox.Response(429, "{\"code\":\"PT429\",\"message\":\"EV_DAILY_QUOTA_EXCEEDED\"}") }
        assertEquals(1, r.quota); assertEquals(0, r.conflicts)
        val e = EvTelemetryOutbox.entries(dir).single()
        assertEquals(Instant.parse("2026-10-14T18:35:00Z").toEpochMilli(), e.nextAttemptMs)   // 00:05 IST next day
        assertEquals(1, EvTelemetryOutbox.drain(dir, e.nextAttemptMs - 1) { null }.deferred)
        val p = EvTelemetryOutbox.drain(dir, e.nextAttemptMs) { EvTelemetryOutbox.Response(409, "{\"code\":\"23503\"}") }
        assertEquals(1, p.prerequisite); assertEquals(1, EvTelemetryOutbox.entries(dir).size)
    }

    @Test
    fun lossesAreMarkedBoundedAndBlockReEnqueue() {
        val dir = Files.createTempDirectory("evl").toFile()
        val text = "{\"l\":1}"; val sha = EvTelemetryOutbox.sha256(text)
        EvTelemetryOutbox.enqueue(dir, "outcome", "l1", text, sha, 0)
        assertEquals(1, EvTelemetryOutbox.drain(dir, EvTelemetryOutbox.MAX_AGE_MS + 1) { null }.dropped)
        assertEquals("ALREADY_LOST", EvTelemetryOutbox.enqueue(dir, "outcome", "l1", text, sha, 1))
        assertEquals(1, EvTelemetryOutbox.coverage(dir).getJSONObject("counts").getInt("AGE_EXCEEDED"))
        // permanent rejection is kept aside once and never re-enqueued
        EvTelemetryOutbox.enqueue(dir, "decision", "r1", text, sha, 0)
        assertEquals(1, EvTelemetryOutbox.drain(dir, 1) { EvTelemetryOutbox.Response(413, "{\"code\":\"PT413\"}") }.rejected)
        assertEquals("ALREADY_REJECTED", EvTelemetryOutbox.enqueue(dir, "decision", "r1", text, sha, 2))
        // every file counts toward the cap; markers are pruned first
        for (i in 0 until 200) File(dir, "decision_m$i.acked").writeText("x")
        EvTelemetryOutbox.drain(dir, 3) { null }
        assertTrue((dir.listFiles()?.size ?: 0) <= EvTelemetryOutbox.MAX_FILES)
        for (i in 0 until 300) EvTelemetryOutbox.noteCoverage(dir, "X", "decision", "i$i", i.toLong())
        assertEquals(EvTelemetryOutbox.COVERAGE_MAX_EVENTS, EvTelemetryOutbox.coverage(dir).getJSONArray("events").length())
    }

    // ---------------- R1: request cache bound; R4: learning export exclusion ----------------
    @Test
    fun acceptanceRequestCacheIsBounded() {
        val now = 10L * 24 * 3600 * 1000
        val m = HashMap<String, String?>()
        for (i in 0 until 30) m["r$i"] = JSONObject().put("created_ms", now - i * 1000L).toString()
        m["old"] = JSONObject().put("created_ms", now - EvAcceptance.REQUEST_TTL_MS - 1).toString()
        m["junk"] = "{bad"
        val drop = EvAcceptance.prunePlan(m, now).toSet()
        assertTrue("old" in drop); assertTrue("junk" in drop)
        assertEquals(EvAcceptance.MAX_REQUESTS, m.size - drop.size)
        assertTrue((0 until EvAcceptance.MAX_REQUESTS).all { "r$it" !in drop })
    }

    @Test
    fun experimentTradesExcludedFromLearningExport() {
        val rows = JSONArray()
            .put(JSONObject().put("id", 1).put("paper", true).put("entry_snapshot", "{\"position_policy_id\":\"paper_rule_b_v1\"}"))
            .put(JSONObject().put("id", 2).put("paper", true).put("entry_snapshot", JSONObject().put("selection_source", "pc2")))
            .put(JSONObject().put("id", 3).put("paper", true))
        val (kept, n) = EvExperiment.excludeExperimentTrades(rows)
        assertEquals(1, n)
        assertEquals(listOf(2, 3), (0 until kept.length()).map { kept.getJSONObject(it).getInt("id") })
    }
}
