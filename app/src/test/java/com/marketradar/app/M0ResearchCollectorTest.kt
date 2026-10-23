package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.security.MessageDigest
import java.time.LocalDate
import java.time.LocalDateTime

/**
 * M0 full-day research collector (Codex corrected brief B7 items 1-8). COLLECTOR tests only; valuation/audit
 * cases (A2-01/02/04/13/14, F25/26/29/30/32) are later-stage specifications and are not exercised here.
 */
class M0ResearchCollectorTest {
    private lateinit var dir: File
    private val S1 = "2026-10-22"   // Thursday
    private val S2 = "2026-10-23"   // Friday
    private val S3 = "2026-10-26"   // Monday

    private fun ms(day: String, h: Int, m: Int, s: Int = 0): Long =
        LocalDateTime.of(LocalDate.parse(day), java.time.LocalTime.of(h, m, s)).atZone(ResearchCalendar.IST).toInstant().toEpochMilli()

    private fun slot(i: String, d: String, o: Int) = ResearchCollector.slotId(i, d, o)
    private fun state(d: String, i: String, o: Int): JSONObject =
        ResearchCollector.readManifest(dir, d)!!.getJSONObject("indices").getJSONObject(i).getJSONObject("slots").getJSONObject(o.toString())

    private fun chain(index: String, expiry: String, keyPrefix: String = "NSE_FO|", strikes: List<Int> = listOf(100, 200),
                      twoSided: Boolean = true): JSONObject {
        val data = JSONArray()
        for (k in strikes) {
            fun leg(t: String) = JSONObject().put("instrument_key", "$keyPrefix$index$k$t")
                .put("market_data", JSONObject().put("ltp", 10.0).put("bid_price", if (twoSided) 9.5 else 0.0).put("ask_price", 10.5))
            data.put(JSONObject().put("strike_price", k.toDouble()).put("expiry", expiry)
                .put("call_options", leg("CE")).put("put_options", leg("PE")))
        }
        return JSONObject().put("status", "success").put("data", data)
    }

    private fun begin(i: String, d: String, o: Int, now: Long) =
        ResearchCollector.beginAttempt(dir, i, "$d|$o", now) as ResearchCollector.Begin.Started

    private fun capture(i: String, ch: JSONObject?, receipt: Long, table: String? = "ml_option_chain_snapshots") =
        ResearchCollector.captureFromChain(dir, i, ch, receipt - 800, receipt, table, receipt + 300, "ts", receipt)

    private fun raw(status: String, wm: String?, dir0: String? = "BULL") = JSONObject().put("schema", "research_m0_raw_forecast_v1")
        .put("status", status).put("direction", dir0 ?: JSONObject.NULL).put("confidence", 62.0).put("horizon", "UNDECLARED")
        .put("brain_version", "2.6.83").put("input_last_poll_slot_key", wm ?: JSONObject.NULL)

    @Before fun setUp() { dir = Files.createTempDirectory("m0").toFile(); ResearchCollector.setEnabled(dir, true, ms(S1, 9, 0)) }
    @After fun tearDown() { dir.deleteRecursively() }

    // ---------------------------------------------------------------- B7.1
    @Test fun b7_1_process_death_whole_session_and_late_upload_preserve_denominators() {
        val expected = ResearchCalendar.plan(S1, "NF").slots.size
        assertEquals(78, expected) // derived from the grid 09:15..15:40/5m = 78, not a constant in the collector
        // slot 1 starts, process dies before capture
        begin("NF", S1, 1, ms(S1, 9, 15, 5))
        // slot 3 captured 7 minutes late -> LATE, linked to slot 3
        val a3 = begin("NF", S1, 3, ms(S1, 9, 25, 5))
        assertEquals(ResearchCollector.RecordResult.RECORDED,
            ResearchCollector.recordCapture(dir, a3.slotId, a3.attemptId, capture("NF", chain("NF", "2026-10-27"), ms(S1, 9, 32)), ms(S1, 9, 32)))
        val r = ResearchCollector.reconcile(dir, ms(S1, 10, 0))
        assertEquals("OK", r.getString("status"))
        assertEquals("MISSING", state(S1, "NF", 1).getString("state"))
        assertEquals("attempt_abandoned_process_death_or_timeout", state(S1, "NF", 1).getString("reason"))
        assertEquals("no_attempt", state(S1, "NF", 2).getString("reason"))
        assertEquals("LATE", state(S1, "NF", 3).getString("state"))
        assertEquals(a3.slotId, state(S1, "NF", 3).getString("slot_id"))
        assertEquals("PENDING", state(S1, "NF", 78).getString("state")) // future slot is pending, not missing
        // a late upload for already-MISSING slot 2 stays linked to slot 2 and keeps history
        val a2 = begin("NF", S1, 2, ms(S1, 10, 1))
        ResearchCollector.recordCapture(dir, a2.slotId, a2.attemptId, capture("NF", chain("NF", "2026-10-27"), ms(S1, 10, 2)), ms(S1, 10, 2))
        assertEquals("LATE", state(S1, "NF", 2).getString("state"))
        assertTrue(state(S1, "NF", 2).getString("previous_state").startsWith("MISSING:"))
        // wholly missed session S2: reconstructed from the calendar on a later launch (S3)
        ResearchCollector.reconcile(dir, ms(S3, 9, 0))
        val m2 = ResearchCollector.readManifest(dir, S2)!!
        assertEquals("reconcile_missed_session", m2.getString("created_by"))
        for (i in listOf("NF", "BNF")) {
            val io = m2.getJSONObject("indices").getJSONObject(i)
            assertEquals(78, io.getInt("expected"))
            assertEquals("session_not_observed_by_collector", io.getJSONObject("slots").getJSONObject("40").getString("reason"))
        }
        // the denominator never shrinks: expected stays the calendar count and every slot is present
        val st = ResearchCollector.status(dir, ms(S3, 9, 0), 10)
        var found = false
        for (k in 0 until st.getJSONArray("sessions").length()) {
            val s = st.getJSONArray("sessions").getJSONObject(k)
            if (s.getString("session_date") == S1) {
                val nf = s.getJSONObject("NF"); found = true
                assertEquals(78, nf.getInt("expected"))
                assertEquals(78, ResearchCollector.STATES.sumOf { nf.getInt(it.lowercase()) })
            }
        }
        assertTrue(found)
        // weekends are not sessions; a 2027 date is UNKNOWN coverage, not zero slots
        assertNull(ResearchCollector.readManifest(dir, "2026-10-24"))
        assertEquals("CALENDAR_YEAR_NOT_COVERED", ResearchCalendar.plan("2027-01-04", "NF").reason)
        assertFalse(ResearchCalendar.plan("2027-01-04", "NF").available)
    }

    @Test fun b7_1_slots_before_collector_start_are_labelled() {
        val d2 = Files.createTempDirectory("m0b").toFile()
        try {
            ResearchCollector.setEnabled(d2, true, ms(S1, 11, 2))
            ResearchCollector.reconcile(d2, ms(S1, 12, 0))
            val sl = ResearchCollector.readManifest(d2, S1)!!.getJSONObject("indices").getJSONObject("BNF").getJSONObject("slots")
            assertEquals("before_collector_start", sl.getJSONObject("1").getString("reason"))
            assertEquals(78, ResearchCollector.readManifest(d2, S1)!!.getJSONObject("indices").getJSONObject("BNF").getInt("expected"))
        } finally { d2.deleteRecursively() }
    }

    // ---------------------------------------------------------------- B7.2
    @Test fun b7_2_restart_update_retry_identities_and_conflicts() {
        val a = ResearchCollector.nextSeq(dir); val b = ResearchCollector.nextSeq(dir)
        assertEquals(a.epoch, b.epoch); assertEquals(a.seq + 1, b.seq)
        // "restart": state comes only from disk
        val c = ResearchCollector.nextSeq(dir); assertEquals(b.seq + 1, c.seq)
        // lost/corrupt sequence file -> new epoch, never a reused (epoch, seq)
        File(dir, "seq.json").writeText("{corrupt")
        val d = ResearchCollector.nextSeq(dir)
        assertNotEquals(a.epoch, d.epoch)
        assertNotEquals(a.id, d.id)

        val at = begin("BNF", S1, 5, ms(S1, 9, 35, 5))
        val cap = capture("BNF", chain("BNF", "2026-10-28"), ms(S1, 9, 35, 9))
        assertEquals(ResearchCollector.RecordResult.RECORDED, ResearchCollector.recordCapture(dir, at.slotId, at.attemptId, cap, ms(S1, 9, 36)))
        assertEquals(ResearchCollector.RecordResult.IDEMPOTENT, ResearchCollector.recordCapture(dir, at.slotId, at.attemptId, cap, ms(S1, 9, 37)))
        val other = capture("BNF", chain("BNF", "2026-10-28", strikes = listOf(300)), ms(S1, 9, 35, 9))
        assertEquals(ResearchCollector.RecordResult.CONFLICT, ResearchCollector.recordCapture(dir, at.slotId, at.attemptId, other, ms(S1, 9, 38)))
        assertEquals(1, ResearchCollector.conflicts(dir).getInt("total"))
        val kept = state(S1, "BNF", 5).getJSONArray("attempts").getJSONObject(0).getJSONObject("capture")
        assertEquals(4, kept.getInt("chain_rows"))
        // a retry is a new attempt on the SAME slot identity
        val at2 = begin("BNF", S1, 5, ms(S1, 9, 39))
        assertEquals(at.slotId, at2.slotId); assertEquals(2, at2.attemptNo); assertNotEquals(at.attemptId, at2.attemptId)

        // immutable envelope across "update" (different app version on retry -> conflict, original kept)
        val e1 = ResearchCollector.buildEnvelope("$S1|5", raw("VALID", "$S1|5"), ms(S1, 9, 36), ms(S1, 9, 36, 20), "2.6.83", 513, JSONObject())
        assertEquals(ResearchCollector.EnvResult.STORED, ResearchCollector.putEnvelope(dir, e1, ms(S1, 9, 37)))
        assertEquals(ResearchCollector.EnvResult.IDEMPOTENT, ResearchCollector.putEnvelope(dir, JSONObject(e1.toString()), ms(S1, 9, 38)))
        val e2 = ResearchCollector.buildEnvelope("$S1|5", raw("NEUTRAL", "$S1|5", "NEUTRAL"), ms(S1, 9, 36), ms(S1, 9, 36, 20), "2.6.83", 514, JSONObject())
        assertEquals(ResearchCollector.EnvResult.CONFLICT, ResearchCollector.putEnvelope(dir, e2, ms(S1, 9, 39)))
        assertEquals("VALID", ResearchCollector.readEnvelope(dir, "$S1|5")!!.getString("status"))
        assertEquals("2.6.83", ResearchCollector.readEnvelope(dir, "$S1|5")!!.getString("app_version"))
        assertEquals(2, ResearchCollector.conflicts(dir).getInt("total"))
    }

    // ---------------------------------------------------------------- B7.3
    @Test fun b7_3_late_brain_and_later_poll_contamination_never_become_earlier_forecast() {
        // brain for slot 11 completes after slot 12's nominal time: kept for slot 11 with true availability
        val late = ResearchCollector.buildEnvelope("$S1|11", raw("VALID", "$S1|11"), ms(S1, 10, 5, 6), ms(S1, 10, 10, 40), "2.6.83", 513, JSONObject())
        assertEquals("VALID", late.getString("status"))
        assertTrue(late.getBoolean("available_after_next_slot_nominal"))
        assertEquals(ms(S1, 10, 10, 40), late.getLong("available_at_ms"))
        assertEquals("$S1|11", late.getString("source_poll_slot_key"))
        // a result computed from slot 12 inputs offered as slot 11 -> refused
        val cont = ResearchCollector.buildEnvelope("$S1|11", raw("VALID", "$S1|12"), 1L, 2L, "2.6.83", 513, JSONObject())
        assertEquals("FORECAST_UNAVAILABLE", cont.getString("status"))
        assertTrue(cont.getString("reason").startsWith("input_watermark_mismatch"))
        val unk = ResearchCollector.buildEnvelope("$S1|11", raw("VALID", null), 1L, 2L, "2.6.83", 513, JSONObject())
        assertEquals("input_watermark_unknown", unk.getString("reason"))
        val noAvail = ResearchCollector.buildEnvelope("$S1|11", raw("VALID", "$S1|11"), 1L, null, "2.6.83", 513, JSONObject())
        assertEquals("availability_unknown", noAvail.getString("reason"))
        // the refused envelope occupies slot 11; a later "valid" retry cannot replace it
        ResearchCollector.putEnvelope(dir, cont, 3L)
        assertEquals(ResearchCollector.EnvResult.CONFLICT, ResearchCollector.putEnvelope(dir, late, 4L))
        assertEquals("FORECAST_UNAVAILABLE", ResearchCollector.readEnvelope(dir, "$S1|11")!!.getString("status"))
        // timeout -> MISSING (never neutral)
        val to = ResearchCollector.buildEnvelope("$S1|13", null, 1L, null, "2.6.83", 513, JSONObject(), "brain_timeout")
        assertEquals("MISSING", to.getString("status")); assertEquals("brain_timeout", to.getString("reason"))
    }

    @Test fun b7_3_wiring_refresh_results_are_never_recorded_and_raw_is_detached() {
        val src = File("src/main/java/com/marketradar/app/MarketWatchService.kt").readText()
        val calls = Regex("(?<!fun )m0CaptureEnvelope\\(poll").findAll(src).map { m -> src.substring(maxOf(0, m.range.first - 40), m.range.first) }.toList()
        assertEquals(3, calls.size)
        calls.forEach { assertTrue(it, it.contains("if (!isRefresh)")) }
        assertTrue(src.contains("resultObj.remove(ResearchCollector.RAW_FORECAST_KEY)"))
        val py = File("src/main/python/research_forecast_capture.py").readText()
        assertTrue(py.contains("RESULT_KEY = '${ResearchCollector.RAW_FORECAST_KEY}'"))
    }

    // ---------------------------------------------------------------- B7.4
    @Test fun b7_4_neutral_and_missing_stay_distinct_missing_quotes_do_not_erase_neutral() {
        val at = begin("NF", S1, 20, ms(S1, 10, 50, 5))
        // baseline quotes missing (no two-sided legs) -> slot PARTIAL
        ResearchCollector.recordCapture(dir, at.slotId, at.attemptId, capture("NF", chain("NF", "2026-10-27", twoSided = false), ms(S1, 10, 50, 9)), ms(S1, 10, 51))
        assertEquals("PARTIAL", state(S1, "NF", 20).getString("state"))
        assertEquals("no_two_sided_quotes", state(S1, "NF", 20).getString("reason"))
        val n = ResearchCollector.buildEnvelope("$S1|20", raw("NEUTRAL", "$S1|20", "NEUTRAL"), 1L, 2L, "2.6.83", 513, JSONObject())
        ResearchCollector.putEnvelope(dir, n, 3L)
        ResearchCollector.putEnvelope(dir, ResearchCollector.buildEnvelope("$S1|21", null, 1L, null, "2.6.83", 513, JSONObject(), "brain_timeout"), 3L)
        assertEquals("NEUTRAL", ResearchCollector.readEnvelope(dir, "$S1|20")!!.getString("status"))
        val ev = ResearchCollector.status(dir, ms(S1, 11, 0)).getJSONObject("envelopes_today")
        assertEquals(1, ev.getInt("NEUTRAL")); assertEquals(1, ev.getInt("MISSING"))
    }

    // ---------------------------------------------------------------- B7.5
    @Test fun b7_5_mapping_change_keeps_old_and_new_versions() {
        val a1 = begin("NF", S1, 30, ms(S1, 11, 40, 5))
        val c1 = capture("NF", chain("NF", "2026-10-27", keyPrefix = "NSE_FO|A"), ms(S1, 11, 40, 9))
        ResearchCollector.recordCapture(dir, a1.slotId, a1.attemptId, c1, ms(S1, 11, 41))
        val a2 = begin("NF", S1, 31, ms(S1, 11, 45, 5))
        val c2 = capture("NF", chain("NF", "2026-10-27", keyPrefix = "NSE_FO|B"), ms(S1, 11, 45, 9))
        ResearchCollector.recordCapture(dir, a2.slotId, a2.attemptId, c2, ms(S1, 11, 46))
        assertNotNull(c1.imapVersion); assertNotNull(c2.imapVersion); assertNotEquals(c1.imapVersion, c2.imapVersion)
        assertTrue(ResearchCollector.readInstrumentMap(dir, c1.imapVersion!!)!!.all { it.contains("NSE_FO|A") })
        assertTrue(ResearchCollector.readInstrumentMap(dir, c2.imapVersion!!)!!.all { it.contains("NSE_FO|B") })
        // unchanged content dedups to the same immutable version
        val same = ResearchCollector.putInstrumentMap(dir, "NF", ResearchCollector.instrumentMapLines("NF", chain("NF", "2026-10-27", keyPrefix = "NSE_FO|A")), 9L)
        assertEquals(ResearchCollector.MapRef.Ok(c1.imapVersion!!, false), same)
        // a tampered stored map is refused, not silently re-referenced
        val f = File(dir, "imap_${c1.imapVersion}.json"); val o = JSONObject(f.readText()); o.put("body", JSONObject(o.getString("body")).put("lines", "NF|x").toString()); f.writeText(o.toString())
        assertEquals(ResearchCollector.MapRef.Unavailable("IMAP_STORE_CORRUPT"),
            ResearchCollector.putInstrumentMap(dir, "NF", ResearchCollector.instrumentMapLines("NF", chain("NF", "2026-10-27", keyPrefix = "NSE_FO|A")), 9L))
    }

    // ---------------------------------------------------------------- B7.6
    @Test fun b7_6_protection_archive_lag_and_capacity_are_bounded_and_visible() {
        val roles = listOf("K0_CE", "K0_PE", "P2_CE", "P2_PE", "PS_WING_CE", "PS_WING_PE")
        assertEquals("STAGED", ResearchProtection.request(dir, "c1", "offline_builder_m1", "NF", "t1", S1, roles, 1L, capacity = 2))
        assertEquals("STAGED", ResearchProtection.request(dir, "c2", "offline_builder_m1", "BNF", "t1", S1, roles, 1L, capacity = 2))
        assertEquals("CAPACITY_REJECTED", ResearchProtection.request(dir, "c3", "offline_builder_m1", "NF", "t2", S1, roles, 1L, capacity = 2))
        assertTrue(ResearchProtection.isProtected(dir, "NF", "t1"))
        assertEquals(roles, (0 until 6).map { ResearchProtection.entry(dir, "c1")!!.getJSONArray("roles").getString(it) })
        // Batch 1 W4: an arbitrary-bytes ack is rejected; only a verified manifest of the requested evidence is accepted
        val bytes = "archived chain rows".toByteArray()
        val sha = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
        val arbitrary = JSONObject().put("entry_id", "c1").put("index", "NF").put("poll_ts", "t1").put("roles", org.json.JSONArray(roles.sorted()))
            .put("content_sha256", sha).put("content_bytes", bytes.size)
        assertTrue(ResearchProtection.acknowledgeArchive(dir, "c1", arbitrary, bytes, 3L).startsWith("REJECTED:"))
        assertTrue(ResearchProtection.isProtected(dir, "NF", "t1"))
        // c2 never archived: lag limit converts it to a visible final state (no indefinite pin)
        assertEquals(0, ResearchProtection.enforceLag(dir, S2, lagLimit = 2))
        assertEquals(2, ResearchProtection.enforceLag(dir, "2026-10-29", lagLimit = 2))
        assertEquals("LAG_EXCEEDED", ResearchProtection.entry(dir, "c2")!!.getString("state"))
        val sum = ResearchProtection.summary(dir, S1)
        assertEquals(1, sum.getInt("CAPACITY_REJECTED")); assertEquals(2, sum.getInt("LAG_EXCEEDED"))
        assertTrue(ResearchProtection.isProtected(dir, "BNF", "t1"))   // LAG_EXCEEDED is still protected (no deletion)
        assertEquals("INCOMPLETE_REPLAY", sum.getString("integrity"))
        assertEquals("UNKNOWN_INDEX", ResearchProtection.request(dir, "c4", "o", "FINNIFTY", "t", S1, roles, 1L))
    }

    @Test fun b7_6_local_stores_are_bounded() {
        // Batch 1 W4: count-only pruning is gone; at the cap, unacknowledged manifests are kept and admission refused
        var refused = 0
        repeat(ResearchCollector.MAX_MANIFEST_SESSIONS + 5) { i ->
            try { ResearchCollector.ensureManifest(dir, LocalDate.parse("2026-01-01").plusDays(i.toLong()).toString(), 1L) }
            catch (_: ResearchCollector.CapacityBacklog) { refused++ }
        }
        assertEquals(ResearchCollector.MAX_MANIFEST_SESSIONS, dir.listFiles { f -> f.name.startsWith("manifest_") && f.name.endsWith(".json") }!!.size)
        assertEquals(5, refused)
        dir.listFiles { f -> f.name.startsWith("manifest_") }!!.forEach { it.delete() }
        repeat(ResearchCollector.MAX_ATTEMPTS_PER_SLOT) { begin("NF", S1, 40, ms(S1, 12, 30)) }
        assertEquals(ResearchCollector.Begin.Unavailable("ATTEMPT_CAP"), ResearchCollector.beginAttempt(dir, "NF", "$S1|40", ms(S1, 12, 31)))
    }

    // ---------------------------------------------------------------- B7.7
    @Test fun b7_7_index_expiry_and_unknown_identity_never_cross_map() {
        assertEquals(ResearchCollector.Begin.Unavailable("UNKNOWN_INDEX"), ResearchCollector.beginAttempt(dir, "FINNIFTY", "$S1|5", 1L))
        assertEquals(ResearchCollector.MapRef.Unavailable("CROSS_INDEX_ENTRY"),
            ResearchCollector.putInstrumentMap(dir, "NF", ResearchCollector.instrumentMapLines("BNF", chain("BNF", "2026-10-28")), 1L))
        val nfPlan = ResearchCalendar.plan(S1, "NF"); val bnfPlan = ResearchCalendar.plan(S1, "BNF")
        assertEquals("2026-10-27", nfPlan.ruleExpiry)
        assertEquals("LIVE_CHAIN_ONLY", bnfPlan.expiryAuthority); assertNull(bnfPlan.ruleExpiry)
        assertEquals("2026-10-19", ResearchCalendar.nfRuleExpiry(LocalDate.parse("2026-10-14").plusDays(1))!!.toString()) // Tue 20 Oct holiday -> Mon 19
        // NF chain whose expiry disagrees with the dated rule -> PARTIAL, not silently accepted
        val a = begin("NF", S1, 50, ms(S1, 13, 20, 5))
        ResearchCollector.recordCapture(dir, a.slotId, a.attemptId, capture("NF", chain("NF", "2026-10-28"), ms(S1, 13, 20, 9)), ms(S1, 13, 21))
        assertEquals("expiry_calendar_mismatch", state(S1, "NF", 50).getString("reason"))
        // a second expiry inside one chain response -> PARTIAL
        val two = chain("NF", "2026-10-27"); two.getJSONArray("data").getJSONObject(1).put("expiry", "2026-11-03")
        val b = begin("NF", S1, 51, ms(S1, 13, 25, 5))
        ResearchCollector.recordCapture(dir, b.slotId, b.attemptId, capture("NF", two, ms(S1, 13, 25, 9)), ms(S1, 13, 26))
        assertEquals("multiple_expiries_in_chain", state(S1, "NF", 51).getString("reason"))
        // BNF accepts its own live expiry; NF and BNF slot identities are distinct
        val c = begin("BNF", S1, 50, ms(S1, 13, 20, 5))
        ResearchCollector.recordCapture(dir, c.slotId, c.attemptId, capture("BNF", chain("BNF", "2026-10-28"), ms(S1, 13, 20, 9)), ms(S1, 13, 21))
        assertEquals("CAPTURED", state(S1, "BNF", 50).getString("state"))
        assertNotEquals(a.slotId, c.slotId)
        // a non-primary-table save is not durable evidence
        val d = begin("BNF", S1, 52, ms(S1, 13, 30, 5))
        ResearchCollector.recordCapture(dir, d.slotId, d.attemptId, capture("BNF", chain("BNF", "2026-10-28"), ms(S1, 13, 30, 9), table = "chain_snapshots"), ms(S1, 13, 31))
        assertEquals("chain_not_durable:LOCAL_ONLY", state(S1, "BNF", 52).getString("reason"))
    }

    @Test fun b7_7_calendar_parity_with_app_and_python() {
        val svc = File("src/main/java/com/marketradar/app/MarketWatchService.kt").readText()
        val py = File("src/main/python/ev_v12_calendar.py").readText()
        for (h in ResearchCalendar.HOLIDAYS_2026) { assertTrue(h, svc.contains("\"$h\"")); assertTrue(h, py.contains("'$h'")) }
        assertEquals(15, Regex("\"2026-\\d\\d-\\d\\d\",?\\s*//").findAll(svc).count())
    }

    // ---------------------------------------------------------------- B7.8
    @Test fun b7_8_collector_failures_are_contained_and_touch_no_trading_or_evaluation_state() {
        // an unusable store (dir is a FILE) never throws out of the failure recorder
        val bad = File.createTempFile("m0bad", ".x")
        try { ResearchCollector.recordFailure(bad, "capture", RuntimeException("x")) } finally { bad.delete() }
        // collector source references no trading/evaluation/journal authority
        val forbidden = listOf("EvaluationLocalCache", "EvaluationCertification", "EvaluationRunLedger", "EvExperiment",
            "EvTelemetryOutbox", "OrderExecutionService", "ExecutionModeResolver", "PositionExitPolicy", "TeacherTruthConfig",
            "SupabaseClient", "PaperMenu", "prefs.edit", "SharedPreferences")
        for (f in listOf("ResearchCollector.kt", "ResearchCalendar.kt")) {
            val src = File("src/main/java/com/marketradar/app/$f").readText()
            forbidden.forEach { assertFalse("$f references $it", src.contains(it)) }
        }
        // every collector entry point in the poll service is wrapped by m0Safely (failure -> counter only)
        val svc = File("src/main/java/com/marketradar/app/MarketWatchService.kt").readText()
        val direct = Regex("ResearchCollector\\.(beginAttempt|recordCapture|captureFromChain|reconcile|putEnvelope|closeIfOpen)\\(")
            .findAll(svc).map { m -> svc.lastIndexOf("m0Safely(", m.range.first) to m.range.first }.toList()
        assertTrue(direct.isNotEmpty())
        direct.forEach { (w, at) -> assertTrue("unwrapped collector call at $at", w >= 0 && at - w < 900) }
        // the disabled collector creates nothing
        val off = Files.createTempDirectory("m0off").toFile()
        try {
            assertFalse(ResearchCollector.isEnabled(off))
            assertEquals("DISABLED", ResearchCollector.status(off, ms(S1, 10, 0)).getString("label"))
            assertEquals(0, off.listFiles()!!.size)
        } finally { off.deleteRecursively() }
    }

    @Test fun b6_label_requires_recent_durable_capture() {
        assertEquals("ENABLED_NOT_CAPTURING", ResearchCollector.status(dir, ms(S1, 10, 0)).getString("label"))
        val a = begin("NF", S1, 10, ms(S1, 10, 0, 5))
        ResearchCollector.recordCapture(dir, a.slotId, a.attemptId, capture("NF", chain("NF", "2026-10-27"), ms(S1, 10, 0, 9)), ms(S1, 10, 1))
        val st = ResearchCollector.status(dir, ms(S1, 10, 2))
        assertEquals("COLLECTING", st.getString("label"))
        assertEquals("Full-day research — collecting", st.getString("label_text"))
        assertTrue(st.getString("claims").startsWith("COLLECTION_ONLY"))
        assertEquals("ENABLED_NOT_CAPTURING", ResearchCollector.status(dir, ms(S1, 11, 0)).getString("label"))
        // local-only (fallback-table) save does not make it "collecting"
        val b = begin("NF", S1, 25, ms(S1, 11, 15, 5))
        ResearchCollector.recordCapture(dir, b.slotId, b.attemptId, capture("NF", chain("NF", "2026-10-27"), ms(S1, 11, 15, 9), table = null), ms(S1, 11, 16))
        assertEquals("ENABLED_NOT_CAPTURING", ResearchCollector.status(dir, ms(S1, 11, 16)).getString("label"))
    }
}
