package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.io.RandomAccessFile
import java.nio.file.Files
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneId

/**
 * close_open_capture R2: restart recovery (section 1), quote eligibility aligned with the matcher
 * (section 2, including the SHARED fixture), the single producer and independence (section 3).
 * Every chain is SYNTHETIC; every clock is a declared IST instant.
 */
class CloseOpenCaptureR2Test {
    private val h = CloseOpenCaptureTest()
    private val IXS = listOf("NF", "BNF")
    private val CLOSE_D = "2026-11-25"; private val NEXT_D = "2026-11-26"
    private val NF_EXP = "2026-12-01"; private val BNF_EXP = "2026-12-29"

    @After fun resetSeams() {
        CloseOpenCapture.testHookAfterSessionWrite = null
        CloseOpenCapture.testFailQueueWrite = false
    }

    private fun tmp(): File = Files.createTempDirectory("co_r2").toFile()
    private fun oneSided(k: Double, s: String) = CloseOpenCaptureTest.Book(10.0, null)
    private fun crashAt(kind: String, slot: String) {
        CloseOpenCapture.testHookAfterSessionWrite = { k, s, _ ->
            if (k == kind && s == slot) throw IllegalStateException("simulated process death after session write")
        }
    }
    private fun pollBoth(fd: File, date: String, slot: String, nfSpot: Double, bnfSpot: Double,
                         book: ((Double, String) -> CloseOpenCaptureTest.Book?)? = null): JSONObject {
        val nf = if (book == null) h.chain("NF", nfSpot, NF_EXP) else h.chain("NF", nfSpot, NF_EXP, book = book)
        val bnf = if (book == null) h.chain("BNF", bnfSpot, BNF_EXP) else h.chain("BNF", bnfSpot, BNF_EXP, book = book)
        return h.poll(fd, date, slot, nf, bnf)
    }
    private fun closeDay(fd: File, date: String = CLOSE_D, slots: List<String> = h.CLOSE,
                         book: ((String) -> ((Double, String) -> CloseOpenCaptureTest.Book?)?) = { null }) {
        for (s in slots) pollBoth(fd, date, s, 24500.0, 55000.0, book(s))
    }
    private fun reopenDay(fd: File, date: String = NEXT_D, slots: List<String> = h.REOPEN,
                          book: ((String) -> ((Double, String) -> CloseOpenCaptureTest.Book?)?) = { null }) {
        for (s in slots) pollBoth(fd, date, s, 24620.0, 55210.0, book(s))
    }
    private fun sample(fd: File, name: String, dates: List<String>) {
        val dir = System.getProperty("co.sample.dir") ?: System.getenv("CO_SAMPLE_DIR") ?: return
        val d = File(dir, "r2_recovery"); d.mkdirs()
        for (x in dates) File(d, "${name}_$x.json").writeText(JSONObject().put("session_date", x)
            .put(CloseOpenCapture.SECTION_KEY, CloseOpenCapture.exportSection(fd, x)).toString(1))
    }
    private fun stage(fd: File, date: String, ix: String) = CloseOpenCapture.exportSection(fd, date)
        .getJSONObject("close_open").getJSONObject("stages").getJSONObject(ix)
    private fun reopenRecords(fd: File, date: String, ix: String): List<JSONObject> {
        val r = h.records(CloseOpenCapture.exportSection(fd, date), ix)
        return (0 until r.length()).map { r.getJSONObject(it) }.filter { it.optString("role") == "REOPEN" }
    }
    private fun assertReconcileIdempotent(fd: File, date: String) {
        for (ix in IXS) {
            val q = CloseOpenCapture.loadQueue(fd, ix)
            assertEquals("second reconcile must change nothing ($ix)", 0,
                CloseOpenCapture.reconcile(fd, ix, LocalDate.parse(date), q).length())
        }
    }
    /** Each of the six contracts per index is carried exactly once and captured once. */
    private fun assertPairedOnce(fd: File, closeSlot: String, openSlot: String, recovered: String?, recoveredOpen: String? = null) {
        for (ix in IXS) {
            val es = h.entries(fd, ix).filter { it.getString("close_session") == CLOSE_D }
            assertEquals(6, es.size)
            assertEquals(6, es.map { it.getString("key") }.toSet().size)
            for (e in es) {
                assertEquals(e.toString(), CloseOpenCapture.Q_OPEN_CAPTURED, e.getString("state"))
                assertEquals(closeSlot, e.getString("close_last_qualifying_slot"))
                assertEquals(openSlot, e.getString("open_first_qualifying_slot"))
                if (recovered != null) assertEquals(recovered, e.optString("recovered"))
                if (recoveredOpen != null) assertEquals(recoveredOpen, e.optString("recovered_open"))
            }
        }
    }

    // ───────────── 1. restart recovery ─────────────

    @Test
    fun crashAfterTheLastCloseSlotsSessionWriteWithNoReplayPairsOnce() {
        val fd = tmp()
        crashAt("CLOSE", "15:30")
        // 15:10-15:25 one-sided (not qualifying); only the LAST slot qualifies, and its queue update is lost
        closeDay(fd, book = { s -> if (s == "15:30") null else ::oneSided })
        CloseOpenCapture.testHookAfterSessionWrite = null
        for (ix in IXS) {
            assertTrue(h.entries(fd, ix).all { it.getString("state") == CloseOpenCapture.Q_QUEUED && !it.has("close_last_qualifying_slot") })
            assertEquals(6, stage(fd, CLOSE_D, ix).getJSONObject("counters").getInt("durable_written") - 24)
        }
        // NO replay of 15:30: the next thing the phone does is the next session's open
        reopenDay(fd)
        assertPairedOnce(fd, "15:30", "09:15", "READY_MARKER")
        assertReconcileIdempotent(fd, NEXT_D)
        sample(fd, "T1_crash_after_last_close_write", listOf(CLOSE_D, NEXT_D))
    }

    @Test
    fun staleQueueMetadataIsBroughtUpToTheDurableCloseAndPairsOnce() {
        val fd = tmp()
        val root = fd.resolve(CloseOpenCapture.DIR_NAME)
        closeDay(fd, slots = listOf("15:10"))
        val snap = IXS.associateWith { root.resolve("queue_$it.json").readBytes() }   // marker 15:10
        closeDay(fd, slots = h.CLOSE.drop(1))
        for (ix in IXS) root.resolve("queue_$ix.json").writeBytes(snap.getValue(ix))       // restored stale snapshot
        for (ix in IXS) assertTrue(h.entries(fd, ix).all { it.getString("close_last_qualifying_slot") == "15:10" })
        reopenDay(fd)
        assertPairedOnce(fd, "15:30", "09:15", "READY_MARKER")
        assertReconcileIdempotent(fd, NEXT_D)
        sample(fd, "T2_stale_metadata", listOf(CLOSE_D, NEXT_D))
    }

    @Test
    fun aFullyMissingQueueWriteIsSurfacedThenRebuiltAndPairsOnce() {
        val fd = tmp()
        CloseOpenCapture.testFailQueueWrite = true
        closeDay(fd)
        CloseOpenCapture.testFailQueueWrite = false
        for (ix in IXS) {
            assertFalse(fd.resolve(CloseOpenCapture.DIR_NAME).resolve("queue_$ix.json").exists())
            val st = stage(fd, CLOSE_D, ix)
            assertEquals(5, st.getJSONObject("counters").getInt("queue_write_failed"))
            for (s in h.CLOSE) assertEquals("FAILED", st.getJSONObject("slots").getJSONObject(s).getString("queue_write"))
        }
        reopenDay(fd)
        assertPairedOnce(fd, "15:30", "09:15", "MISSING_ENTRY")
        val rec = stage(fd, NEXT_D, "NF").getJSONObject("slots").getJSONObject("09:15").getJSONObject("reconcile")
        assertEquals(6, rec.getInt("recovered_missing_entry"))
        assertReconcileIdempotent(fd, NEXT_D)
        sample(fd, "T3_missing_queue_write", listOf(CLOSE_D, NEXT_D))
    }

    @Test
    fun aCapRefusedCloseNeverPairsAndSaysWhy() {
        val fd = tmp()
        val r = fd.resolve(CloseOpenCapture.DIR_NAME); r.mkdirs()
        RandomAccessFile(r.resolve("filler.bin"), "rw").use { it.setLength(CloseOpenCapture.MAX_STORE_BYTES - 1000) }
        closeDay(fd)
        reopenDay(fd)
        for (ix in IXS) {
            val es = h.entries(fd, ix)
            assertEquals(6, es.size)
            assertTrue(es.all { it.getString("state") == CloseOpenCapture.Q_REFUSED &&
                it.getString("reason") == CloseOpenCapture.R_NO_QUALIFYING_CLOSE && !it.has("recovered") })
            assertEquals(0, reopenRecords(fd, NEXT_D, ix).size)
        }
        assertReconcileIdempotent(fd, NEXT_D)
        sample(fd, "T4_cap_refused_close", listOf(CLOSE_D, NEXT_D))
    }

    @Test
    fun anOpenSavedButAwaitingItsQueueUpdateIsCapturedOnceAtTheNextSlot() {
        val fd = tmp()
        closeDay(fd)
        crashAt("REOPEN", "09:15")
        reopenDay(fd, slots = listOf("09:15"))
        CloseOpenCapture.testHookAfterSessionWrite = null
        for (ix in IXS) assertTrue(h.entries(fd, ix).all { it.getString("state") == CloseOpenCapture.Q_QUEUED })
        reopenDay(fd, slots = h.REOPEN.drop(1))
        assertPairedOnce(fd, "15:30", "09:15", null, "OPEN_SAVED")
        for (ix in IXS) {
            val recs = reopenRecords(fd, NEXT_D, ix)
            assertEquals(6 * 7, recs.size)                                         // one per contract per slot, none doubled
            assertEquals(42, recs.map { it.getString("slot") + "|" + it.getString("instrument_key") }.toSet().size)
        }
        assertReconcileIdempotent(fd, NEXT_D)
        sample(fd, "T5_open_saved_awaiting_queue", listOf(CLOSE_D, NEXT_D))
    }

    @Test
    fun anOpenSavedAtTheLastOpeningSlotIsRecoveredByTheNextPollOfTheDay() {
        val fd = tmp()
        closeDay(fd)
        crashAt("REOPEN", "09:45")
        reopenDay(fd, book = { s -> if (s == "09:45") null else ::oneSided })
        CloseOpenCapture.testHookAfterSessionWrite = null
        pollBoth(fd, NEXT_D, "15:10", 24700.0, 55300.0)    // the day's first close poll runs reconcile before finalize
        assertPairedOnce(fd, "15:30", "09:45", null, "OPEN_SAVED")
        assertReconcileIdempotent(fd, NEXT_D)
        sample(fd, "T5b_open_saved_last_slot", listOf(CLOSE_D, NEXT_D))
    }

    @Test
    fun aRebuiltEntryStillHonoursTheExpiryRefusal() {
        val fd = tmp()
        // Monday 30 Nov: the next session (Tue 1 Dec) is the NF weekly expiry -> refused; BNF carries
        CloseOpenCapture.testFailQueueWrite = true
        closeDay(fd, date = "2026-11-30")
        CloseOpenCapture.testFailQueueWrite = false
        reopenDay(fd, date = "2026-12-01")
        val nf = h.entries(fd, "NF"); val bnf = h.entries(fd, "BNF")
        assertEquals(6, nf.size); assertEquals(6, bnf.size)
        assertTrue(nf.all { it.getString("state") == CloseOpenCapture.Q_REFUSED &&
            it.getString("reason") == CloseOpenCapture.R_NEXT_ON_OR_AFTER_EXPIRY && it.getString("recovered") == "MISSING_ENTRY" })
        assertTrue(bnf.all { it.getString("state") == CloseOpenCapture.Q_OPEN_CAPTURED && it.getString("recovered") == "MISSING_ENTRY" })
        assertEquals(0, reopenRecords(fd, "2026-12-01", "NF").size)
        assertReconcileIdempotent(fd, "2026-12-01")
    }

    @Test
    fun aRebuiltEntryStillHonoursTheCalendarRefusal() {
        val fd = tmp()
        // 31 Dec 2026: the next session is in 2027, which the calendar does not cover
        CloseOpenCapture.testFailQueueWrite = true
        closeDay(fd, date = "2026-12-31")
        CloseOpenCapture.testFailQueueWrite = false
        val q = CloseOpenCapture.loadQueue(fd, "BNF")
        val rec = CloseOpenCapture.reconcile(fd, "BNF", LocalDate.parse("2026-12-31"), q)
        val es = (0 until q.getJSONArray("entries").length()).map { q.getJSONArray("entries").getJSONObject(it) }
        assertEquals(6, rec.optInt("recovered_missing_entry"))
        assertTrue(es.all { it.getString("state") == CloseOpenCapture.Q_REFUSED && it.getString("reason") == CloseOpenCapture.R_CAL_NOT_COVERED })
    }

    // ───────────── 2. eligibility boundaries ─────────────

    private fun at(date: String, hms: String): Long = LocalDateTime.parse("${date}T$hms")
        .atZone(ZoneId.of("Asia/Kolkata")).toInstant().toEpochMilli()
    private fun elig(role: String, slot: String, ms: Long?, dis: JSONArray = JSONArray(), q: String = "TWO_SIDED") =
        CloseOpenCapture.eligibility(role, LocalDate.parse(CLOSE_D), slot, ms, dis, q)
    private fun reasons(o: JSONObject) = o.getJSONArray("reasons").let { a -> (0 until a.length()).map { a.getString(it) } }

    @Test
    fun theReceiptMinuteDecidesTheWindowInclusiveAtBothEnds() {
        val d = CLOSE_D
        assertEquals(listOf("receipt_before_close_window"), reasons(elig("CLOSE", "15:10", at(d, "15:05:02"))))
        assertEquals(listOf("receipt_before_close_window"), reasons(elig("CLOSE", "15:10", at(d, "15:09:59.999"))))
        assertTrue(elig("CLOSE", "15:10", at(d, "15:10:00")).getBoolean("quote_ready"))
        assertTrue(elig("CLOSE", "15:30", at(d, "15:30:59.999")).getBoolean("quote_ready"))
        assertEquals(listOf("receipt_after_close_window"), reasons(elig("CLOSE", "15:30", at(d, "15:31:00"))))
        assertEquals(listOf("receipt_before_open_window"), reasons(elig("REOPEN", "09:15", at(d, "09:14:59.999"))))
        assertTrue(elig("REOPEN", "09:15", at(d, "09:15:00")).getBoolean("quote_ready"))
        assertTrue(elig("REOPEN", "09:45", at(d, "09:45:59.999")).getBoolean("quote_ready"))
        assertEquals(listOf("receipt_after_open_window"), reasons(elig("REOPEN", "09:45", at(d, "09:46:00"))))
        assertEquals(listOf("receipt_after_open_window"), reasons(elig("REOPEN", "09:45", at(d, "09:46:01"))))
        assertEquals(listOf("receipt_date_not_session"), reasons(elig("CLOSE", "15:20", at("2026-11-26", "15:20:04"))))
        assertEquals(listOf("receipt_clock_missing"), reasons(elig("CLOSE", "15:20", null)))
        assertEquals(listOf("identity_disagreement", "book_one_sided"),
            reasons(elig("CLOSE", "15:20", at(d, "15:20:04"), JSONArray().put("strike"), "ONE_SIDED")))
        val e = elig("CLOSE", "15:10", at(d, "15:05:02"))
        assertEquals(CloseOpenCapture.FRESHNESS_RECEIPT_ONLY, e.getString("freshness"))
        assertEquals("15:05", e.getString("receipt_minute")); assertEquals("15:10", e.getString("slot_intent"))
        assertEquals(CloseOpenCapture.ELIGIBILITY_RULE, e.getString("rule"))
    }

    @Test
    fun aLateFinalClosePollIsADiagnosticAndTheCloseRestsOnTheEarlierSlot() {
        val fd = tmp()
        pollBoth(fd, CLOSE_D, "15:25", 24500.0, 55000.0)
        h.poll(fd, CLOSE_D, "15:30", h.chain("NF", 24500.0, NF_EXP), h.chain("BNF", 55000.0, BNF_EXP), sec = 60)  // 15:31:00
        for (ix in IXS) {
            assertTrue(h.entries(fd, ix).all { it.getString("close_last_qualifying_slot") == "15:25" })
            val recs = h.records(CloseOpenCapture.exportSection(fd, CLOSE_D), ix)
            val late = (0 until recs.length()).map { recs.getJSONObject(it) }.filter { it.getString("slot") == "15:30" }
            assertEquals(6, late.size)
            assertTrue(late.all { !it.getJSONObject("eligibility").getBoolean("quote_ready") &&
                reasons(it.getJSONObject("eligibility")) == listOf("receipt_after_close_window") })
            assertEquals(6, stage(fd, CLOSE_D, ix).getJSONObject("counters").getInt("quote_not_qualifying"))
            assertEquals("QUALIFYING_0/6", stage(fd, CLOSE_D, ix).getJSONObject("slots").getJSONObject("15:30").getString("quote_receipt"))
        }
    }

    @Test
    fun theExportMarksLotAuthorityAbsentAndStatesTheRule() {
        val fd = tmp(); closeDay(fd)
        val ex = CloseOpenCapture.exportSection(fd, CLOSE_D)
        val co = ex.getJSONObject("close_open")
        assertEquals(CloseOpenCapture.ELIGIBILITY_RULE, co.getString("eligibility_rule"))
        assertEquals(CloseOpenCapture.LOT_AUTHORITY_STATE, co.getJSONObject("lot_authority").getString("state"))
        assertEquals("REFUSED_UNTIL_QUANTITY_EVIDENCE", co.getJSONObject("lot_authority").getString("after_cost_use"))
        for (ix in IXS) {
            assertEquals(CloseOpenCapture.LOT_AUTHORITY_STATE, ex.getJSONObject("section").getJSONObject("indices")
                .getJSONObject(ix).getJSONObject("lot_authority").getString("state"))
            assertEquals(30, co.getJSONObject("stages").getJSONObject(ix).getInt("export_qualifying_records"))
        }
    }

    // ───────────── 3. opt-in, single producer, independence ─────────────

    @Test
    fun statusSummaryIsReadOnlyAndShowsControlRuleCalendarAndQueue() {
        val fd = tmp()
        val off = CloseOpenCapture.statusSummary(fd, CLOSE_D)
        assertEquals("OFF", off.getString("default")); assertFalse(off.getBoolean("enabled"))
        assertFalse(fd.resolve(CloseOpenCapture.DIR_NAME).exists())                 // reading created nothing
        assertFalse(off.getBoolean("makes_requests"))
        assertEquals(CloseOpenCapture.SECTION_KEY, off.getString("producer_key"))
        assertTrue(off.getString("expiry_scope").startsWith("NEAREST_EXPIRY_ONLY"))
        assertEquals("2026-11-26", off.getJSONObject("calendar").getString("next_regular_session"))
        val y27 = CloseOpenCapture.statusSummary(fd, "2026-12-31").getJSONObject("calendar")
        assertEquals(CloseOpenCapture.R_CAL_NOT_COVERED, y27.getString("next_reason"))
        CloseOpenCapture.setEnabled(fd, true); closeDay(fd)
        val on = CloseOpenCapture.statusSummary(fd, CLOSE_D)
        assertTrue(on.getBoolean("enabled"))
        for (ix in IXS) {
            val b = on.getJSONObject("indices").getJSONObject(ix)
            assertEquals("PRESENT", b.getString("today_session_file"))
            assertEquals("15:30", b.getJSONObject("last_slot").getString("slot"))
            assertEquals("NO_FAILURE_RECORDED", b.getJSONObject("last_slot").getString("queue_write"))
            assertEquals(6, b.getJSONObject("queue").getJSONObject("by_state").getInt(CloseOpenCapture.Q_QUEUED))
        }
    }

    private fun mainSources(): List<File> {
        val root = listOf(File("src/main/java"), File("app/src/main/java")).first { it.isDirectory }
        return root.walkTopDown().filter { it.isFile && (it.name.endsWith(".kt") || it.name.endsWith(".java")) }.toList()
    }

    @Test
    fun premiumFollowonHasExactlyOneProducer() {
        val src = mainSources()
        val literal = src.filter { f -> f.readLines().any { l -> !l.trimStart().startsWith("*") && !l.trimStart().startsWith("//") &&
            l.contains("\"premium_followon\"") } }
        assertEquals(listOf("CloseOpenCapture.kt"), literal.map { it.name })
        fun calls(token: String) = src.filter { it.name != "CloseOpenCapture.kt" }.sumOf { f -> Regex(Regex.escape(token)).findAll(f.readText()).count() }
        assertEquals(1, calls("CloseOpenCapture.spliceWithinCap("))   // the export, once
        assertEquals(1, calls("CloseOpenCapture.onPoll("))            // the live poll, once
        assertEquals(0, calls("CloseOpenCapture.exportSection("))     // nothing else builds the section
    }

    @Test
    fun theCaptureIsIndependentOfTheIntradayPilotAndPaper() {
        val code = mainSources().first { it.name == "CloseOpenCapture.kt" }.readLines()
            .filter { l -> val t = l.trimStart(); !t.startsWith("*") && !t.startsWith("//") && !t.startsWith("/**") }
            .joinToString("\n")
        for (tok in listOf("PremiumCaptureControl", "PremiumCaptureCohort", "PremiumCaptureStore", "Paper", "paper_", "Trading", "MarketWatchService"))
            assertFalse("CloseOpenCapture code references $tok", code.lines().any { it.contains(tok) && !it.contains("\"Paper controls\"") })
        val fd = tmp()
        CloseOpenCapture.setEnabled(fd, true); closeDay(fd); reopenDay(fd); CloseOpenCapture.setEnabled(fd, false)
        assertEquals(listOf(CloseOpenCapture.DIR_NAME), fd.list()!!.toList())   // nothing outside its own directory
    }

    // ───────────── 2b. the SHARED fixture, replayed through the capture ─────────────

    private fun fixtureFile(): File = listOf(File("src/test/resources/close_open_capture/shared_quote_eligibility_v1.json"),
        File("app/src/test/resources/close_open_capture/shared_quote_eligibility_v1.json")).first { it.isFile }

    private fun chainFrom(p: JSONObject): JSONObject {
        val byStrike = LinkedHashMap<Double, JSONObject>()
        val rows = p.getJSONArray("rows")
        for (i in 0 until rows.length()) {
            val r = rows.getJSONObject(i); val k = r.getDouble("strike")
            val row = byStrike.getOrPut(k) { JSONObject().put("strike_price", k).put("expiry", p.getString("expiry_in_response"))
                .put("underlying_key", p.getString("underlying_key")).put("underlying_spot_price", p.getDouble("spot")) }
            row.put(if (r.getString("side") == "CE") "call_options" else "put_options", JSONObject()
                .put("instrument_key", r.getString("key"))
                .put("market_data", JSONObject().put("bid_price", r.getDouble("bid")).put("ask_price", r.getDouble("ask")).put("ltp", r.getDouble("bid")))
                .put("option_greeks", JSONObject()))
        }
        return JSONObject().put("status", "success").put("data", JSONArray(byStrike.values.toList()))
    }

    private fun canonical(vs: List<String>): String {
        val g = vs.groupingBy { it }.eachCount()
        return if (g.size == 1) g.keys.first() else g.entries.sortedBy { it.key }.joinToString("; ") { "${it.key} x${it.value}" }
    }

    @Test
    fun theSharedFixtureReplaysToTheProposedRuleOnTheCapture() {
        val fx = JSONObject(fixtureFile().readText())
        val out = JSONObject().put("fixture_sha256", java.security.MessageDigest.getInstance("SHA-256")
            .digest(fixtureFile().readBytes()).joinToString("") { "%02x".format(it) }).put("cases", JSONObject())
        val mismatches = ArrayList<String>()
        val dir = (System.getProperty("co.sample.dir") ?: System.getenv("CO_SAMPLE_DIR"))?.let { File(it, "r2_fixture") }
        dir?.resolve("exports")?.mkdirs()
        val cases = fx.getJSONArray("cases")
        for (ci in 0 until cases.length()) {
            val c = cases.getJSONObject(ci); val id = c.getString("id")
            val closeD = c.getString("close_session"); val nextD = c.getString("next_session")
            val fd = tmp()
            val polls = c.getJSONArray("polls")
            val groups = LinkedHashMap<Pair<String, String>, MutableList<CloseOpenCapture.Acquisition>>()
            for (i in 0 until polls.length()) {
                val p = polls.getJSONObject(i)
                val rms = OffsetDateTime.parse(p.getString("receipt_ts")).toInstant().toEpochMilli()
                groups.getOrPut(p.getString("session_date") to p.getString("slot")) { ArrayList() }
                    .add(CloseOpenCapture.Acquisition(p.getString("index"), chainFrom(p), rms - 1000, rms))
            }
            for ((k, acqs) in groups) CloseOpenCapture.process(fd, k.first, k.second, acqs)
            // settle: the next session's first close poll runs reconcile + finalize; no chain, no record
            CloseOpenCapture.process(fd, nextD, "15:10", IXS.map { CloseOpenCapture.Acquisition(it, null, 0L, 0L) })
            val closeEx = CloseOpenCapture.exportSection(fd, closeD)
            val per = JSONObject()
            for (ix in IXS) {
                val recs = h.records(closeEx, ix).let { a -> (0 until a.length()).map { a.getJSONObject(it) } }
                val vs = h.entries(fd, ix).filter { it.getString("close_session") == closeD }.map { e ->
                    val key = e.getJSONObject("contract").getString("instrument_key")
                    when {
                        e.getString("state") == CloseOpenCapture.Q_OPEN_CAPTURED -> "PAIR@" + e.getString("close_last_qualifying_slot")
                        e.optString("reason") == CloseOpenCapture.R_NO_QUALIFYING_CLOSE -> {
                            val last = recs.filter { it.getString("instrument_key") == key && it.getString("role") == "CLOSE" }
                                .maxByOrNull { it.getString("slot") }
                            "REFUSED:" + (last?.let { reasons(it.getJSONObject("eligibility")).joinToString("+") } ?: CloseOpenCapture.R_NO_QUALIFYING_CLOSE)
                        }
                        else -> "REFUSED:" + e.optString("reason")
                    }
                }
                val got = canonical(vs)
                val want = c.getJSONObject("proposed_rule_expectation").getString(ix)
                per.put(ix, JSONObject().put("capture", got).put("expected", want).put("agrees", got == want)
                    .put("entries", vs.size).put("contracts", JSONArray(vs)))
                if (got != want) mismatches.add("$id $ix: capture=$got expected=$want")
            }
            out.getJSONObject("cases").put(id, per)
            if (dir != null) for (x in listOf(closeD, nextD)) dir.resolve("exports/${id}_$x.json").writeText(JSONObject()
                .put("session_date", x).put(CloseOpenCapture.SECTION_KEY, CloseOpenCapture.exportSection(fd, x)).toString())
        }
        dir?.resolve("capture_results.json")?.writeText(out.toString(1))
        assertTrue(mismatches.joinToString("\n"), mismatches.isEmpty())
    }
}
