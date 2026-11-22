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
import java.io.RandomAccessFile
import java.nio.file.Files
import java.time.LocalDateTime
import java.time.ZoneId

/**
 * close_open_capture_v1. Every chain here is SYNTHETIC (built below); every clock is a declared
 * IST instant. Fixture cases from the handoff: valid pair, restart, next-session rotation,
 * duplicate and failed polls, missing close and open quotes, stale and partial data, calendar
 * gap, expiry refusal — plus NF/BNF separation, pilot isolation, bounds and overlap.
 */
class CloseOpenCaptureTest {

    // ───────────── synthetic chains ─────────────
    data class Book(val bid: Double?, val ask: Double?)

    private fun step(ix: String) = if (ix == "NF") 50.0 else 100.0

    /**
     * A chain for [ix] around [spot]. Keys are stable per (ix, expiry, strike, side) unless
     * [keySuffix] changes them (a roll). [book] may override a contract's book; [drop] removes
     * contracts from the response.
     */
    fun chain(ix: String, spot: Double, expiry: String, keySuffix: String = "",
              book: (Double, String) -> Book? = { k, s -> Book(10.0 + (k % 1000) / 100.0, 10.5 + (k % 1000) / 100.0) },
              drop: Set<String> = emptySet(), strikes: Int = 9): JSONObject {
        val st = step(ix)
        val atm = Math.round(spot / st) * st
        val data = JSONArray()
        for (i in -(strikes / 2)..(strikes / 2)) {
            val k = atm + i * st
            val row = JSONObject().put("strike_price", k).put("expiry", expiry)
                .put("underlying_key", CloseOpenCapture.UNDERLYING[ix]).put("underlying_spot_price", spot)
            for ((side, field) in listOf("CE" to "call_options", "PE" to "put_options")) {
                val key = key(ix, expiry, k, side) + keySuffix
                if (key in drop) continue
                val b = book(k, side)
                val md = JSONObject()
                if (b?.bid != null) md.put("bid_price", b.bid)
                if (b?.ask != null) md.put("ask_price", b.ask)
                md.put("ltp", 10.2)
                row.put(field, JSONObject().put("instrument_key", key).put("market_data", md)
                    .put("option_greeks", JSONObject().put("iv", 14.1)))
            }
            data.put(row)
        }
        return JSONObject().put("status", "success").put("data", data)
    }

    fun key(ix: String, expiry: String, k: Double, side: String) = "NSE_FO|$ix-$expiry-${k.toInt()}-$side"

    fun ms(date: String, hhmm: String, sec: Int = 4): Long =
        LocalDateTime.parse("${date}T$hhmm:00").plusSeconds(sec.toLong())
            .atZone(ZoneId.of("Asia/Kolkata")).toInstant().toEpochMilli()

    fun acq(ix: String, date: String, slot: String, c: JSONObject?, sec: Int = 4) =
        CloseOpenCapture.Acquisition(ix, c, ms(date, slot, 1), if (sec < 0) 0L else ms(date, slot, sec))

    private fun tmp(): File = Files.createTempDirectory("co_cap").toFile()

    val CLOSE = listOf("15:10", "15:15", "15:20", "15:25", "15:30")
    val REOPEN = listOf("09:15", "09:20", "09:25", "09:30", "09:35", "09:40", "09:45")
    val NF_EXP = "2026-12-01"; val BNF_EXP = "2026-12-29"

    fun poll(fd: File, date: String, slot: String, nf: JSONObject?, bnf: JSONObject?, sec: Int = 4) =
        CloseOpenCapture.process(fd, date, slot, listOf(acq("BNF", date, slot, bnf, sec), acq("NF", date, slot, nf, sec)))

    fun closeDay(fd: File, date: String, nfSpot: Double = 24500.0, bnfSpot: Double = 55000.0) {
        for (s in CLOSE) poll(fd, date, s, chain("NF", nfSpot, NF_EXP), chain("BNF", bnfSpot, BNF_EXP))
    }
    fun reopenDay(fd: File, date: String, nfSpot: Double = 24620.0, bnfSpot: Double = 55210.0) {
        for (s in REOPEN) poll(fd, date, s, chain("NF", nfSpot, NF_EXP), chain("BNF", bnfSpot, BNF_EXP))
    }
    fun entries(fd: File, ix: String): List<JSONObject> {
        val a = CloseOpenCapture.loadQueue(fd, ix).getJSONArray("entries")
        return (0 until a.length()).map { a.getJSONObject(it) }
    }
    fun records(sec: JSONObject, ix: String): JSONArray =
        sec.getJSONObject("section").getJSONObject("indices").optJSONObject(ix)?.getJSONArray("records") ?: JSONArray()

    // ───────────── 1. valid pair ─────────────
    @Test
    fun validPairQueuesTheExactContractsAndCapturesThemAtTheNextOpen() {
        val fd = tmp()
        closeDay(fd, "2026-11-25")                 // Wed
        val q = entries(fd, "NF")
        assertEquals(6, q.size)
        assertTrue(q.all { it.getString("state") == CloseOpenCapture.Q_QUEUED })
        assertTrue(q.all { it.getString("target_session") == "2026-11-26" })
        assertTrue(q.all { it.getString("close_last_qualifying_slot") == "15:30" })
        reopenDay(fd, "2026-11-26")                // Thu, spot moved
        val after = entries(fd, "NF")
        assertTrue(after.all { it.getString("state") == CloseOpenCapture.Q_OPEN_CAPTURED })
        assertTrue(after.all { it.getString("open_first_qualifying_slot") == "09:15" })
        val dExp = CloseOpenCapture.exportSection(fd, "2026-11-25")
        val tExp = CloseOpenCapture.exportSection(fd, "2026-11-26")
        assertEquals(30, records(dExp, "NF").length())          // 5 slots x 6 contracts
        assertEquals(42, records(tExp, "NF").length())          // 7 slots x 6 carried contracts
        assertEquals(30, records(dExp, "BNF").length())
        val r0 = records(tExp, "NF").getJSONObject(0)
        assertEquals("REOPEN", r0.getString("role"))
        assertEquals("2026-11-25", r0.getString("carried_from_session"))
        assertEquals(CloseOpenCapture.FRESHNESS_RECEIPT_ONLY, r0.getString("freshness"))
        assertEquals(true, r0.getJSONObject("identity").getBoolean("identity_verified"))
        val env = tExp.getJSONObject("section").getJSONObject("indices").getJSONObject("NF")
            .getJSONArray("slot_envelopes").getJSONObject(0)
        assertFalse(env.getJSONObject("clocks").getBoolean("quote_event_clock_verified"))
        assertTrue(env.getJSONObject("clocks").isNull("vendor_source_ts"))
        // per-stage outcomes are present and additive
        val st = dExp.getJSONObject("close_open").getJSONObject("stages").getJSONObject("NF")
        assertEquals(5, st.getJSONObject("counters").getInt("scheduled"))
        assertEquals(30, st.getJSONObject("counters").getInt("durable_written"))
        assertEquals(30, st.getInt("export_included_records"))
        val env0 = dExp.getJSONObject("section").getJSONObject("indices").getJSONObject("NF").getJSONArray("slot_envelopes").getJSONObject(0)
        assertEquals(CloseOpenCapture.QUOTE_SOURCE, env0.getString("quote_source"))
        val obs1510 = st.getJSONObject("slots").getJSONObject("15:10").getJSONArray("observations")
        assertTrue((0 until obs1510.length()).all { obs1510.getJSONObject(it).getString("durable") == "WRITTEN" })
        writeSample(fd, listOf("2026-11-25", "2026-11-26"), "valid_pair")
    }

    // ───────────── 2. restart ─────────────
    @Test
    fun aRestartBetweenCloseAndOpenLosesNothingBecauseTheQueueIsOnDisk() {
        val a = tmp(); closeDay(a, "2026-11-25")
        // "restart": a fresh files dir holding only the bytes on disk, nothing in memory carried
        val b = tmp(); a.resolve(CloseOpenCapture.DIR_NAME).copyRecursively(b.resolve(CloseOpenCapture.DIR_NAME))
        reopenDay(b, "2026-11-26")
        assertTrue(entries(b, "NF").all { it.getString("state") == CloseOpenCapture.Q_OPEN_CAPTURED })
        assertTrue(entries(b, "BNF").all { it.getString("state") == CloseOpenCapture.Q_OPEN_CAPTURED })
        // restart in the MIDDLE of the close window: the pinned close cohort is re-read, not reselected
        val c = tmp()
        poll(c, "2026-11-25", "15:10", chain("NF", 24500.0, NF_EXP), chain("BNF", 55000.0, BNF_EXP))
        val d = tmp(); c.resolve(CloseOpenCapture.DIR_NAME).copyRecursively(d.resolve(CloseOpenCapture.DIR_NAME))
        poll(d, "2026-11-25", "15:15", chain("NF", 24700.0, NF_EXP), chain("BNF", 55400.0, BNF_EXP))
        val keys = entries(d, "NF").map { it.getJSONObject("contract").getDouble("strike") }.toSet()
        assertEquals(setOf(24450.0, 24500.0, 24550.0), keys)
    }

    // ───────────── 3. next-session rotation ─────────────
    @Test
    fun nextSessionRotationKeepsYesterdaysContractsAndQueuesTodaysSeparately() {
        val fd = tmp()
        closeDay(fd, "2026-11-25", nfSpot = 24500.0)
        // spot moved 300 points overnight; the response still lists the old strikes (21 strikes wide)
        for (s in REOPEN) poll(fd, "2026-11-26", s, chain("NF", 24800.0, NF_EXP, strikes = 21), chain("BNF", 55210.0, BNF_EXP))
        closeDay(fd, "2026-11-26", nfSpot = 24900.0)          // Thu close: a NEW cohort for Fri
        val q = entries(fd, "NF")
        val fromWed = q.filter { it.getString("close_session") == "2026-11-25" }
        val fromThu = q.filter { it.getString("close_session") == "2026-11-26" }
        assertEquals(6, fromWed.size); assertEquals(6, fromThu.size)
        assertTrue(fromWed.all { it.getString("state") == CloseOpenCapture.Q_OPEN_CAPTURED })
        assertEquals(setOf(24450.0, 24500.0, 24550.0), fromWed.map { it.getJSONObject("contract").getDouble("strike") }.toSet())
        assertEquals(setOf(24850.0, 24900.0, 24950.0), fromThu.map { it.getJSONObject("contract").getDouble("strike") }.toSet())
        assertTrue(fromThu.all { it.getString("target_session") == "2026-11-27" })
        // Thu's export carries BOTH roles under one pinned list
        val pins = CloseOpenCapture.exportSection(fd, "2026-11-26").getJSONObject("section").getJSONObject("indices")
            .getJSONObject("NF").getJSONObject("cohort").getJSONArray("pinned_contracts")
        assertEquals(12, pins.length())
    }

    // ───────────── 4. duplicate and failed polls ─────────────
    @Test
    fun aRetriedPollIsANoOpAndAConflictingRetryKeepsTheFirstWithoutDoubleCounting() {
        val fd = tmp()
        val nf = chain("NF", 24500.0, NF_EXP); val bnf = chain("BNF", 55000.0, BNF_EXP)
        poll(fd, "2026-11-25", "15:10", nf, bnf)
        poll(fd, "2026-11-25", "15:10", nf, bnf)                       // identical retry
        poll(fd, "2026-11-25", "15:10", chain("NF", 24500.0, NF_EXP, book = { _, _ -> Book(1.0, 2.0) }), bnf)
        val exp = CloseOpenCapture.exportSection(fd, "2026-11-25")
        assertEquals(6, records(exp, "NF").length())
        val c = exp.getJSONObject("close_open").getJSONObject("stages").getJSONObject("NF").getJSONObject("counters")
        assertEquals(6, c.getInt("durable_written"))
        assertEquals(6, c.getInt("durable_duplicate_no_op"))
        assertEquals(6, c.getInt("durable_duplicate_conflict_kept_first"))
        assertEquals(3, exp.getJSONObject("close_open").getJSONObject("stages").getJSONObject("NF")
            .getJSONObject("slots").getJSONObject("15:10").getInt("polls_for_slot"))
        // the first observation is the one kept, not the conflicting retry's 1.0/2.0
        val bids = (0 until 6).map { records(exp, "NF").getJSONObject(it).getJSONObject("bbo").getJSONObject("bid_raw").getDouble("raw") }
        assertTrue(bids.none { it == 1.0 })
    }

    @Test
    fun aSingleFailedPollDoesNotDiscardTheSessionOrTheQueue() {
        val fd = tmp()
        poll(fd, "2026-11-25", "15:10", null, chain("BNF", 55000.0, BNF_EXP))          // NF chain failed
        poll(fd, "2026-11-25", "15:15", chain("NF", 24500.0, NF_EXP), chain("BNF", 55000.0, BNF_EXP))
        poll(fd, "2026-11-25", "15:20", chain("NF", 24500.0, NF_EXP), null)             // BNF chain failed
        val exp = CloseOpenCapture.exportSection(fd, "2026-11-25")
        val nfSt = exp.getJSONObject("close_open").getJSONObject("stages").getJSONObject("NF")
        assertEquals(CloseOpenCapture.R_CHAIN_MISSING, nfSt.getJSONObject("slots").getJSONObject("15:10").getString("quote_receipt"))
        assertEquals(12, records(exp, "NF").length())
        assertEquals(12, records(exp, "BNF").length())
        assertEquals(6, entries(fd, "NF").size)
        // a failed REOPEN poll followed by a good one still captures the open
        poll(fd, "2026-11-26", "09:15", null, null)
        poll(fd, "2026-11-26", "09:20", chain("NF", 24600.0, NF_EXP), chain("BNF", 55100.0, BNF_EXP))
        assertTrue(entries(fd, "NF").all { it.getString("state") == CloseOpenCapture.Q_OPEN_CAPTURED &&
            it.getString("open_first_qualifying_slot") == "09:20" && it.getInt("open_attempts") == 2 })
    }

    // ───────────── 5. missing close and open quotes ─────────────
    @Test
    fun missingCloseQuotesRefuseAtTheOpenAndMissingOpenQuotesEndOpenNotCaptured() {
        val fd = tmp()
        val oneSided = { _: Double, side: String -> if (side == "CE") Book(10.0, null) else Book(10.0, 10.5) }
        for (s in CLOSE) poll(fd, "2026-11-25", s, chain("NF", 24500.0, NF_EXP, book = oneSided), chain("BNF", 55000.0, BNF_EXP))
        // NF CE legs never had a two-sided close quote; NF PE legs did
        val nfDrop = setOf(key("NF", NF_EXP, 24500.0, "PE"))
        for (s in REOPEN) poll(fd, "2026-11-26", s, chain("NF", 24500.0, NF_EXP, drop = nfDrop),
            chain("BNF", 55000.0, BNF_EXP, book = { _, _ -> Book(null, null) }))
        poll(fd, "2026-11-26", "15:10", chain("NF", 24500.0, NF_EXP), chain("BNF", 55000.0, BNF_EXP))   // finalizes
        val nf = entries(fd, "NF").filter { it.getString("close_session") == "2026-11-25" }
        val ce = nf.filter { it.getJSONObject("contract").getString("option_type") == "CE" }
        assertTrue(ce.all { it.getString("state") == CloseOpenCapture.Q_REFUSED && it.getString("reason") == CloseOpenCapture.R_NO_QUALIFYING_CLOSE })
        val pe500 = nf.first { it.getJSONObject("contract").getString("instrument_key") == key("NF", NF_EXP, 24500.0, "PE") }
        assertEquals(CloseOpenCapture.Q_OPEN_NOT_CAPTURED, pe500.getString("state"))
        assertEquals(CloseOpenCapture.R_CONTRACT_ABSENT, pe500.getString("reason"))
        val bnf = entries(fd, "BNF").filter { it.getString("close_session") == "2026-11-25" }
        assertTrue(bnf.all { it.getString("state") == CloseOpenCapture.Q_OPEN_NOT_CAPTURED && it.getString("reason") == "book_no_book" })
    }

    @Test
    fun aRollAtTheOpenIsRefusedNotSubstituted() {
        val fd = tmp(); closeDay(fd, "2026-11-25")
        for (s in REOPEN) poll(fd, "2026-11-26", s, chain("NF", 24500.0, NF_EXP, keySuffix = "-R"), chain("BNF", 55000.0, BNF_EXP))
        poll(fd, "2026-11-26", "15:10", chain("NF", 24500.0, NF_EXP, keySuffix = "-R"), chain("BNF", 55000.0, BNF_EXP))
        val nf = entries(fd, "NF").filter { it.getString("close_session") == "2026-11-25" }
        assertTrue(nf.all { it.getString("state") == CloseOpenCapture.Q_OPEN_NOT_CAPTURED && it.getString("reason") == CloseOpenCapture.R_ROLL })
        writeSample(fd, listOf("2026-11-25", "2026-11-26"), "roll")
        assertEquals(0, records(CloseOpenCapture.exportSection(fd, "2026-11-26"), "NF").let { r ->
            (0 until r.length()).count { r.getJSONObject(it).getString("role") == "REOPEN" } })
    }

    // ───────────── 6. stale and partial data ─────────────
    @Test
    fun partialAndReceiptOnlyDataAreLabelledNotHidden() {
        val fd = tmp()
        val drop = setOf(key("NF", NF_EXP, 24450.0, "CE"), key("NF", NF_EXP, 24550.0, "PE"))
        poll(fd, "2026-11-25", "15:10", chain("NF", 24500.0, NF_EXP), chain("BNF", 55000.0, BNF_EXP))
        // partial response at 15:15, and a missing receipt clock (completedAtMs = 0) at 15:20
        poll(fd, "2026-11-25", "15:15", chain("NF", 24500.0, NF_EXP, drop = drop), chain("BNF", 55000.0, BNF_EXP))
        poll(fd, "2026-11-25", "15:20", chain("NF", 24500.0, NF_EXP), chain("BNF", 55000.0, BNF_EXP), sec = -1)
        val exp = CloseOpenCapture.exportSection(fd, "2026-11-25")
        val slots = exp.getJSONObject("close_open").getJSONObject("stages").getJSONObject("NF").getJSONObject("slots")
        assertEquals("QUALIFYING_4/6", slots.getJSONObject("15:15").getString("quote_receipt"))
        val envs = exp.getJSONObject("section").getJSONObject("indices").getJSONObject("NF").getJSONArray("slot_envelopes")
        val e1520 = (0 until envs.length()).map { envs.getJSONObject(it) }.first { it.getString("slot") == "15:20" }
        assertTrue(e1520.getJSONObject("clocks").isNull("acquisition_response_ts"))
        val recs = records(exp, "NF")
        assertTrue((0 until recs.length()).all { recs.getJSONObject(it).getString("freshness") == CloseOpenCapture.FRESHNESS_RECEIPT_ONLY })
        assertEquals(6 + 4 + 6, recs.length())
    }

    // ───────────── 7. calendar gap ─────────────
    @Test
    fun weekendAndHolidayGapsTargetTheNextRegularSession() {
        val fd = tmp()
        closeDay(fd, "2026-11-27")                                  // Fri -> Mon 30 Nov
        assertTrue(entries(fd, "NF").all { it.getString("target_session") == "2026-11-30" && it.getInt("calendar_gap_days") == 3 })
        reopenDay(fd, "2026-11-30")
        assertTrue(entries(fd, "NF").all { it.getString("state") == CloseOpenCapture.Q_OPEN_CAPTURED })
        writeSample(fd, listOf("2026-11-27", "2026-11-30"), "weekend_gap")
        val g = tmp()
        closeDay(g, "2026-11-23")                                   // Mon -> Wed 25 (24 Nov holiday)
        assertTrue(entries(g, "NF").all { it.getString("target_session") == "2026-11-25" && it.getInt("calendar_gap_days") == 2 })
        assertEquals("2026-11-25", CloseOpenCapture.exportSection(g, "2026-11-23").getJSONObject("close_open")
            .getJSONObject("calendar").getString("next_regular_session"))
    }

    @Test
    fun aTargetSessionWithNoPollEndsTargetPassedWithoutCapture() {
        val fd = tmp(); closeDay(fd, "2026-11-25")
        // the phone was off on Thu 26; the next poll is Fri 27 09:15
        poll(fd, "2026-11-27", "09:15", chain("NF", 24500.0, NF_EXP), chain("BNF", 55000.0, BNF_EXP))
        assertTrue(entries(fd, "NF").all { it.getString("state") == CloseOpenCapture.Q_TARGET_PASSED &&
            it.getString("reason") == CloseOpenCapture.R_NO_ATTEMPT })
        // and nothing was recorded on the 27th for contracts due on the 26th
        assertEquals(0, records(CloseOpenCapture.exportSection(fd, "2026-11-27"), "NF").length())
    }

    @Test
    fun uncoveredCalendarAndIrregularSessionsAreRefused() {
        val fd = tmp()
        closeDay(fd, "2026-12-31")                                  // next day is 2027: not covered
        assertEquals(6, entries(fd, "NF").size)
        assertTrue(entries(fd, "NF").all { it.getString("state") == CloseOpenCapture.Q_REFUSED &&
            it.getString("reason") == CloseOpenCapture.R_CAL_NOT_COVERED })
        val st = CloseOpenCapture.exportSection(fd, "2026-12-31").getJSONObject("close_open").getJSONObject("stages").getJSONObject("NF")
        assertEquals(6, st.getJSONObject("counters").getInt("queue_refused_" + CloseOpenCapture.R_CAL_NOT_COVERED))
        assertEquals("calendar_not_covered", CloseOpenCapture.exportSection(fd, "2026-12-31").getJSONObject("close_open")
            .getJSONObject("calendar").getString("next_reason"))
        // Muhurat 2026-11-08 is a Sunday special session: not regular
        val m = tmp(); poll(m, "2026-11-08", "15:10", chain("NF", 24500.0, NF_EXP), chain("BNF", 55000.0, BNF_EXP))
        assertTrue(entries(m, "NF").isEmpty())
        // a declared-but-untimed special session (an early close) is not regular either
        ResearchCalendar.PENDING_SPECIAL_SESSIONS.add("2026-11-25")
        try {
            val p = tmp(); closeDay(p, "2026-11-25")
            assertTrue(entries(p, "NF").isEmpty())
            assertEquals(5, CloseOpenCapture.exportSection(p, "2026-11-25").getJSONObject("close_open").getJSONObject("stages")
                .getJSONObject("NF").getJSONObject("refusals").getInt(CloseOpenCapture.R_NOT_REGULAR))
        } finally { ResearchCalendar.PENDING_SPECIAL_SESSIONS.remove("2026-11-25") }
    }

    // ───────────── 8. expiry refusal ─────────────
    @Test
    fun noFeasibleNextSessionExitBeforeExpiryIsRefusedAtEnqueue() {
        val fd = tmp()
        // Mon 30 Nov, NF expiry Tue 1 Dec: the next session IS the expiry day -> refused
        for (s in CLOSE) poll(fd, "2026-11-30", s, chain("NF", 24500.0, "2026-12-01"), chain("BNF", 55000.0, BNF_EXP))
        assertTrue(entries(fd, "NF").all { it.getString("state") == CloseOpenCapture.Q_REFUSED &&
            it.getString("reason") == CloseOpenCapture.R_NEXT_ON_OR_AFTER_EXPIRY })
        assertTrue(entries(fd, "BNF").all { it.getString("state") == CloseOpenCapture.Q_QUEUED })     // BNF independent
        // the real 20 Nov 2026 case: NF rule expiry is Mon 23 Nov (24 Nov holiday) and so is the next session
        assertEquals("2026-11-23", ResearchCalendar.nfRuleExpiry(java.time.LocalDate.parse("2026-11-20")).toString())
        val g = tmp()
        for (s in CLOSE) poll(g, "2026-11-20", s, chain("NF", 24500.0, "2026-11-23"), chain("BNF", 55000.0, "2026-11-23"))
        assertTrue(entries(g, "NF").all { it.getString("reason") == CloseOpenCapture.R_NEXT_ON_OR_AFTER_EXPIRY })
        // a refused entry is never captured at the open
        poll(fd, "2026-12-01", "09:15", chain("NF", 24500.0, "2026-12-01"), chain("BNF", 55000.0, BNF_EXP))
        assertTrue(entries(fd, "NF").all { it.getString("state") == CloseOpenCapture.Q_REFUSED })
    }

    // ───────────── separation, isolation, bounds, overlap ─────────────
    @Test
    fun nfAndBnfAreSeparateFilesAndTheIntradayPilotIsUntouched() {
        val fd = tmp(); closeDay(fd, "2026-11-25")
        val r = fd.resolve(CloseOpenCapture.DIR_NAME)
        assertTrue(r.resolve("2026-11-25/NF.json").isFile && r.resolve("2026-11-25/BNF.json").isFile)
        assertTrue(r.resolve("queue_NF.json").isFile && r.resolve("queue_BNF.json").isFile)
        // nothing was written into the premium pilot's store
        assertFalse(PremiumCaptureControl.root(fd).exists())
        // the pilot window constants are exactly the b533 values
        assertEquals(9 * 60 + 20, PremiumCaptureCohort.WINDOW_OPEN_MIN)
        assertEquals(15 * 60 + 5, PremiumCaptureCohort.WINDOW_CLOSE_MIN)
        // slots outside both windows do nothing at all
        val e = tmp(); val res = poll(e, "2026-11-25", "12:00", chain("NF", 24500.0, NF_EXP), chain("BNF", 55000.0, BNF_EXP))
        assertEquals("OUTSIDE_WINDOWS", res.getString("status"))
        assertFalse(e.resolve(CloseOpenCapture.DIR_NAME).exists())
    }

    @Test
    fun theSpliceIsAdditiveAndLeavesEveryExistingKeyByteIdentical() {
        val fd = tmp(); CloseOpenCapture.setEnabled(fd, true); closeDay(fd, "2026-11-25")
        val bundle = "{\"premium\":{\"state\":\"PRESENT\",\"x\":[1,2,3]},\"manifest\":{\"status\":\"OK\"}}"
        val r = CloseOpenCapture.spliceWithinCap(bundle.toByteArray(), fd, "2026-11-25", 4 * 1024 * 1024 + 65536)
        assertTrue(r.included)
        val out = String(r.bytes)
        assertTrue(out.endsWith(bundle.substring(1)))
        assertTrue(out.startsWith("{\"premium_followon\":"))
        val parsed = JSONObject(out)
        assertEquals(JSONObject(bundle).getJSONObject("premium").toString(), parsed.getJSONObject("premium").toString())
        // over the cap: left out and SAID, never truncated
        val tight = CloseOpenCapture.spliceWithinCap(bundle.toByteArray(), fd, "2026-11-25", bundle.length + 10)
        assertFalse(tight.included); assertEquals("OVER_COMBINED_CAP", tight.reason)
        assertEquals(bundle, String(tight.bytes))
    }

    @Test
    fun atTheStoreCapObservationsAreRefusedButTheReasonIsStillDurable() {
        val fd = tmp()
        val r = fd.resolve(CloseOpenCapture.DIR_NAME); r.mkdirs()
        RandomAccessFile(r.resolve("filler.bin"), "rw").use { it.setLength(CloseOpenCapture.MAX_STORE_BYTES - 1000) }
        poll(fd, "2026-11-25", "15:10", chain("NF", 24500.0, NF_EXP), chain("BNF", 55000.0, BNF_EXP))
        val exp = CloseOpenCapture.exportSection(fd, "2026-11-25")
        for (ix in listOf("NF", "BNF")) {
            val st = exp.getJSONObject("close_open").getJSONObject("stages").getJSONObject(ix)
            assertEquals("PRESENT", st.getString("session_file"))
            assertEquals(6, st.getJSONObject("counters").getInt("durable_refused_cap"))
            assertTrue(st.getJSONObject("slots").getJSONObject("15:10").getString("durable_write").startsWith("OBSERVATIONS_REFUSED_CAP"))
            assertEquals(0, records(exp, ix).length())
            val o = st.getJSONObject("slots").getJSONObject("15:10").getJSONArray("observations")
            assertTrue((0 until o.length()).all { o.getJSONObject(it).getString("durable") == "REFUSED_CAP" })
            // R1 fix: a cap-refused close quote never becomes queue close evidence
            assertTrue(entries(fd, ix).none { it.has("close_last_qualifying_slot") })
        }
        // ...so at the next open the entries are refused for lack of a durable close quote
        poll(fd, "2026-11-26", "09:15", chain("NF", 24600.0, NF_EXP), chain("BNF", 55100.0, BNF_EXP))
        for (ix in listOf("NF", "BNF"))
            assertTrue(entries(fd, ix).all { it.getString("state") == CloseOpenCapture.Q_REFUSED &&
                it.getString("reason") == CloseOpenCapture.R_NO_QUALIFYING_CLOSE })
    }

    @Test
    fun aRestartBetweenTheSessionWriteAndTheQueueWriteIsRepairedByARetryWithoutDoubleCounting() {
        val fd = tmp()
        val nf = chain("NF", 24500.0, NF_EXP); val bnf = chain("BNF", 55000.0, BNF_EXP)
        poll(fd, "2026-11-25", "15:10", nf, bnf)
        // simulate a crash after the session file was written but before the queue's evidence was
        val qf = fd.resolve(CloseOpenCapture.DIR_NAME).resolve("queue_NF.json")
        val q = JSONObject(qf.readText())
        for (i in 0 until q.getJSONArray("entries").length()) q.getJSONArray("entries").getJSONObject(i).remove("close_last_qualifying_slot")
        qf.writeText(q.toString())
        assertTrue(entries(fd, "NF").none { it.has("close_last_qualifying_slot") })
        poll(fd, "2026-11-25", "15:10", nf, bnf)                       // identical retry after restart
        assertTrue(entries(fd, "NF").all { it.getString("close_last_qualifying_slot") == "15:10" })
        val c = CloseOpenCapture.exportSection(fd, "2026-11-25").getJSONObject("close_open")
            .getJSONObject("stages").getJSONObject("NF").getJSONObject("counters")
        assertEquals(6, c.getInt("durable_written"))                  // not 12
        assertEquals(6, c.getInt("queue_enqueued"))                   // not 12
        assertEquals(6, c.getInt("durable_duplicate_no_op"))
        // and the open is captured from it
        poll(fd, "2026-11-26", "09:15", chain("NF", 24600.0, NF_EXP), chain("BNF", 55100.0, BNF_EXP))
        assertTrue(entries(fd, "NF").all { it.getString("state") == CloseOpenCapture.Q_OPEN_CAPTURED })
    }

    @Test
    fun overlappingJobsAreBoundedToOneRunningAndOneWaiting() {
        val fd = tmp(); CloseOpenCapture.setEnabled(fd, true)
        val lockField = CloseOpenCapture::class.java.getDeclaredField("lock").apply { isAccessible = true }
        val lock = lockField.get(CloseOpenCapture)
        val statuses = ArrayList<String>()
        val acqs = listOf(acq("NF", "2026-11-25", "15:10", chain("NF", 24500.0, NF_EXP)))
        synchronized(lock) {
            repeat(4) { statuses.add(CloseOpenCapture.onPoll(fd, "2026-11-25", "15:10", acqs).getString("status")); Thread.sleep(50) }
        }
        assertEquals("SUBMITTED", statuses[0]); assertEquals("SUBMITTED", statuses[1])
        assertEquals("REFUSED_OVERLAP", statuses[2]); assertEquals("REFUSED_OVERLAP", statuses[3])
        Thread.sleep(500)
        val exp = CloseOpenCapture.exportSection(fd, "2026-11-25")
        assertEquals(6, records(exp, "NF").length())   // the two accepted jobs still produced ONE set
        assertEquals("DISABLED", CloseOpenCapture.onPoll(tmp(), "2026-11-25", "15:10", acqs).getString("status"))
        // outside both windows nothing is read or created, enabled or not
        val fresh = tmp()
        assertEquals("OUTSIDE_WINDOWS", CloseOpenCapture.onPoll(fresh, "2026-11-25", "12:00", acqs).getString("status"))
        assertFalse(fresh.resolve(CloseOpenCapture.DIR_NAME).exists())
    }

    @Test
    fun theServiceHookMakesNoRequestAndRunsBeforeTheQuotesAbort() {
        val f = File("src/main/java/com/marketradar/app/MarketWatchService.kt")
        if (!f.isFile) return           // plain-JVM runner from another cwd
        val src = f.readText()
        val block = src.substringAfter("closeOpenCaptureSafely {").substringBefore("if (quotesJson == null)")
        assertTrue(block.contains("CloseOpenCapture.onPoll("))
        for (banned in listOf("fetchSync(", "async(", "SupabaseClient", "AlarmManager", "WorkManager", "startService", "Thread("))
            assertFalse(banned, block.contains(banned))
        assertTrue(src.indexOf("CloseOpenCapture.onPoll(") < src.indexOf("POLL_FAIL: Quotes fetch returned null"))
        val guard = src.substringAfter("private inline fun closeOpenCaptureSafely(").substringBefore("\n    }\n")
        assertTrue(guard.contains("catch (t: Throwable)"))
    }

    // ───────────── capacity ─────────────
    @Test
    fun capacityOfOneFullCloseAndOpenCycle() {
        val fd = tmp()
        val times = ArrayList<Long>()
        for (s in CLOSE) { val r = poll(fd, "2026-11-25", s, chain("NF", 24500.0, NF_EXP, strikes = 81), chain("BNF", 55000.0, BNF_EXP, strikes = 81)); times.add(r.getLong("elapsed_us")) }
        for (s in REOPEN) { val r = poll(fd, "2026-11-26", s, chain("NF", 24600.0, NF_EXP, strikes = 81), chain("BNF", 55100.0, BNF_EXP, strikes = 81)); times.add(r.getLong("elapsed_us")) }
        // a retry of every slot (worst case): no growth in records
        val before = CloseOpenCapture.dirBytes(fd.resolve(CloseOpenCapture.DIR_NAME))
        for (s in CLOSE) poll(fd, "2026-11-25", s, chain("NF", 24500.0, NF_EXP, strikes = 81), chain("BNF", 55000.0, BNF_EXP, strikes = 81))
        val afterRetry = CloseOpenCapture.dirBytes(fd.resolve(CloseOpenCapture.DIR_NAME))
        val r = fd.resolve(CloseOpenCapture.DIR_NAME)
        val sorted = times.sorted()
        val e25 = CloseOpenCapture.exportSection(fd, "2026-11-25").toString().toByteArray().size
        val e26 = CloseOpenCapture.exportSection(fd, "2026-11-26").toString().toByteArray().size
        println("CAPACITY requests_added=0 polls_processed=${times.size} " +
            "elapsed_us_median=${sorted[sorted.size / 2]} elapsed_us_max=${sorted.last()} " +
            "nf_close_file=${r.resolve("2026-11-25/NF.json").length()} bnf_close_file=${r.resolve("2026-11-25/BNF.json").length()} " +
            "nf_open_file=${r.resolve("2026-11-26/NF.json").length()} bnf_open_file=${r.resolve("2026-11-26/BNF.json").length()} " +
            "queue_nf=${r.resolve("queue_NF.json").length()} queue_bnf=${r.resolve("queue_BNF.json").length()} " +
            "store_total=$before store_after_full_retry=$afterRetry export_close_day=$e25 export_open_day=$e26")
        assertTrue(r.resolve("2026-11-25/NF.json").length() < CloseOpenCapture.MAX_SESSION_FILE_BYTES)
        assertTrue(e25 < CloseOpenCapture.MAX_EXPORT_SECTION_BYTES)
    }

    /** Write sample exports for the report and the matcher run, when an output dir is given. */
    private fun writeSample(fd: File, dates: List<String>, name: String) {
        val dir = System.getProperty("co.sample.dir") ?: System.getenv("CO_SAMPLE_DIR") ?: return
        File(dir).mkdirs()
        for (d in dates) File(dir, "${name}_$d.json").writeText(
            JSONObject().put("session_date", d).put(CloseOpenCapture.SECTION_KEY, CloseOpenCapture.exportSection(fd, d)).toString(1))
    }
}
