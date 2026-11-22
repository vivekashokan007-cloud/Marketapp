package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.time.OffsetDateTime

/**
 * close_open_capture R3: a queued contract is ready ONLY after its exact saved close observation
 * and envelope are re-verified (section 1); receipt-based selection (section 2) including the
 * shared fixture v2. Every chain is SYNTHETIC; every clock is a declared IST instant.
 */
class CloseOpenCaptureR3Test {
    private val h = CloseOpenCaptureTest()
    private val IXS = listOf("NF", "BNF")
    private val CLOSE_D = "2026-11-25"; private val NEXT_D = "2026-11-26"
    private val NF_EXP = "2026-12-01"; private val BNF_EXP = "2026-12-29"
    private val EXP = mapOf("NF" to NF_EXP, "BNF" to BNF_EXP)
    private val ATM = mapOf("NF" to 24500.0, "BNF" to 55000.0)

    @After fun resetSeams() {
        CloseOpenCapture.testHookAfterSessionWrite = null
        CloseOpenCapture.testFailQueueWrite = false
    }

    private fun tmp(): File = Files.createTempDirectory("co_r3").toFile()
    private fun ce(ix: String) = h.key(ix, EXP.getValue(ix), ATM.getValue(ix), "CE")
    private fun pe(ix: String) = h.key(ix, EXP.getValue(ix), ATM.getValue(ix), "PE")
    private fun sessFile(fd: File, date: String, ix: String) = fd.resolve(CloseOpenCapture.DIR_NAME).resolve(date).resolve("$ix.json")
    private fun pollAt(fd: File, date: String, slot: String, sec: Int = 4, nfSpot: Double = 24500.0, bnfSpot: Double = 55000.0) =
        h.poll(fd, date, slot, h.chain("NF", nfSpot, NF_EXP), h.chain("BNF", bnfSpot, BNF_EXP), sec)
    private fun closeSlots(fd: File, slots: List<String>) { for (s in slots) pollAt(fd, CLOSE_D, s) }
    private fun morning(fd: File, slots: List<String> = h.REOPEN) { for (s in slots) pollAt(fd, NEXT_D, s, nfSpot = 24620.0, bnfSpot = 55210.0) }
    private fun settle(fd: File) = h.poll(fd, NEXT_D, "15:10", null, null)
    private fun closeEntries(fd: File, ix: String) = h.entries(fd, ix).filter { it.getString("close_session") == CLOSE_D }
    private fun reopenRecords(fd: File, ix: String): List<JSONObject> {
        val r = h.records(CloseOpenCapture.exportSection(fd, NEXT_D), ix)
        return (0 until r.length()).map { r.getJSONObject(it) }.filter { it.optString("role") == "REOPEN" }
    }
    private fun assertPositiveMarker(fd: File, slot: String) {
        for (ix in IXS) assertTrue(closeEntries(fd, ix).all {
            it.getString("state") == CloseOpenCapture.Q_QUEUED && it.getString("close_last_qualifying_slot") == slot &&
                it.getString("close_evidence") == "VERIFIED" })
    }

    /** Edit a durable close session file in place, the way storage damage or a bad write would. */
    private fun damage(fd: File, ix: String, op: String, slot: String = "15:30") {
        val f = sessFile(fd, CLOSE_D, ix)
        when (op) {
            "delete_close_file" -> assertTrue(f.delete())
            "corrupt_close_file" -> { val t = f.readText(); f.writeText(t.substring(0, t.length / 2)) }
            else -> {
                val s = JSONObject(f.readText())
                val obs = s.getJSONObject("observations")
                when (op) {
                    "remove_observation" -> assertTrue(obs.remove("$slot|${ce(ix)}") != null)
                    "remove_envelope" -> { val e = s.getJSONObject("envelopes")
                        e.keys().asSequence().toList().filter { e.getJSONObject(it).getString("slot") == slot }.also { assertEquals(1, it.size) }
                            .forEach { e.remove(it) } }
                    "make_one_sided" -> obs.getJSONObject("$slot|${ce(ix)}").getJSONObject("bbo").put("ask_raw", JSONObject().put("raw", JSONObject.NULL))
                    "replace_with_other_contract" -> obs.put("$slot|${ce(ix)}", JSONObject(obs.getJSONObject("$slot|${pe(ix)}").toString()))
                    else -> error(op)
                }
                f.writeText(s.toString())
            }
        }
    }

    // ───────────── 1. no pair on a marker alone ─────────────

    private fun gapCase(op: String, gap: String, kind: String, allSix: Boolean) {
        val fd = tmp()
        closeSlots(fd, listOf("15:30"))                    // ONE close source per contract
        assertPositiveMarker(fd, "15:30")
        for (ix in IXS) damage(fd, ix, op)
        morning(fd, listOf("09:15"))
        for (ix in IXS) {
            val affected = closeEntries(fd, ix).filter { allSix || it.getJSONObject("contract").getString("instrument_key") == ce(ix) }
            assertEquals(if (allSix) 6 else 1, affected.size)
            // before 09:45: not ready, marker withdrawn, the gap SAID (a transient error may still recover)
            assertTrue(affected.all { it.getString("state") == CloseOpenCapture.Q_QUEUED && !it.has("close_last_qualifying_slot") &&
                it.getString("close_evidence") == "GAP" && it.getString("close_evidence_gap") == gap && it.getString("close_evidence_gap_kind") == kind })
        }
        morning(fd, h.REOPEN.drop(1)); settle(fd)
        for (ix in IXS) {
            val es = closeEntries(fd, ix)
            val bad = es.filter { allSix || it.getJSONObject("contract").getString("instrument_key") == ce(ix) }
            assertTrue(bad.all { it.getString("state") == CloseOpenCapture.Q_CLOSE_EVIDENCE_GAP && it.getString("reason") == gap &&
                it.getString("close_evidence_gap_kind") == kind && !it.has("open_first_qualifying_slot") })
            val badKeys = bad.map { it.getJSONObject("contract").getString("instrument_key") }.toSet()
            assertTrue("no opening lookup for a gap", reopenRecords(fd, ix).none { it.getString("instrument_key") in badKeys })
            assertTrue((es - bad.toSet()).all { it.getString("state") == CloseOpenCapture.Q_OPEN_CAPTURED })
            val gaps = CloseOpenCapture.statusSummary(fd, NEXT_D).getJSONObject("indices").getJSONObject(ix)
                .getJSONObject("queue").getJSONObject("evidence_gaps")
            assertEquals(bad.size, gaps.getInt(kind))
        }
    }

    @Test fun closeFileRemovedNeverQualifies() = gapCase("delete_close_file", CloseOpenCapture.GAP_FILE_MISSING, CloseOpenCapture.GAP_KIND_STORAGE, true)
    @Test fun closeFileCorruptedNeverQualifies() = gapCase("corrupt_close_file", CloseOpenCapture.GAP_FILE_UNREADABLE, CloseOpenCapture.GAP_KIND_STORAGE, true)
    @Test fun closeObservationRemovedNeverQualifies() = gapCase("remove_observation", CloseOpenCapture.GAP_OBS_MISSING, CloseOpenCapture.GAP_KIND_EVIDENCE, false)
    @Test fun closeEnvelopeRemovedNeverQualifies() = gapCase("remove_envelope", CloseOpenCapture.GAP_ENV_MISSING, CloseOpenCapture.GAP_KIND_EVIDENCE, true)
    @Test fun nonqualifyingCloseRecordNeverQualifies() = gapCase("make_one_sided", CloseOpenCapture.GAP_NOT_QUALIFYING, CloseOpenCapture.GAP_KIND_EVIDENCE, false)
    @Test fun mismatchedCloseRecordNeverQualifies() = gapCase("replace_with_other_contract", CloseOpenCapture.GAP_MISMATCH, CloseOpenCapture.GAP_KIND_EVIDENCE, false)

    @Test
    fun aTransientReadErrorIsAGapUntilTheFileReadsAgainAndThenPairsOnce() {
        val fd = tmp()
        closeSlots(fd, listOf("15:30"))
        val saved = IXS.associateWith { sessFile(fd, CLOSE_D, it).readBytes() }
        for (ix in IXS) damage(fd, ix, "corrupt_close_file")
        morning(fd, listOf("09:15"))
        for (ix in IXS) assertTrue(closeEntries(fd, ix).all { it.getString("close_evidence_gap_kind") == CloseOpenCapture.GAP_KIND_STORAGE })
        for (ix in IXS) sessFile(fd, CLOSE_D, ix).writeBytes(saved.getValue(ix))
        morning(fd, h.REOPEN.drop(1)); settle(fd)
        for (ix in IXS) {
            assertTrue(closeEntries(fd, ix).all { it.getString("state") == CloseOpenCapture.Q_OPEN_CAPTURED &&
                it.getString("open_first_qualifying_slot") == "09:20" && it.getString("close_evidence") == "VERIFIED" && !it.has("close_evidence_gap") })
            assertEquals(6 * 6, reopenRecords(fd, ix).size)                       // 09:20..09:45, none at 09:15
        }
    }

    @Test
    fun earlierCaptureHistoryIsKeptButMarkedNoLongerVerifiable() {
        val fd = tmp()
        closeSlots(fd, h.CLOSE); morning(fd)
        for (ix in IXS) assertTrue(sessFile(fd, CLOSE_D, ix).delete())
        settle(fd)
        for (ix in IXS) {
            assertTrue(closeEntries(fd, ix).all { it.getString("state") == CloseOpenCapture.Q_OPEN_CAPTURED &&
                it.getString("open_first_qualifying_slot") == "09:15" && it.getString("close_last_qualifying_slot") == "15:30" &&
                !it.getBoolean("close_verifiable") && it.getString("close_evidence_gap") == CloseOpenCapture.GAP_FILE_MISSING })
            assertEquals(42, reopenRecords(fd, ix).size)                          // the open history is untouched
        }
    }

    @Test
    fun aValidCloseStillRecoversOnceAcrossALastSlotRestartWithNoRepeatPoll() {
        val fd = tmp()
        CloseOpenCapture.testHookAfterSessionWrite = { k, s, _ -> if (k == "CLOSE" && s == "15:30") throw IllegalStateException("process death") }
        closeSlots(fd, h.CLOSE)
        CloseOpenCapture.testHookAfterSessionWrite = null
        for (ix in IXS) assertTrue(closeEntries(fd, ix).all { it.getString("close_last_qualifying_slot") == "15:25" })   // stale
        morning(fd); settle(fd)
        for (ix in IXS) {
            assertTrue(closeEntries(fd, ix).all { it.getString("state") == CloseOpenCapture.Q_OPEN_CAPTURED &&
                it.getString("close_last_qualifying_slot") == "15:30" && it.getString("close_evidence") == "VERIFIED" &&
                it.getString("recovered") == "READY_MARKER" && it.optBoolean("marker_corrected") })
            val recs = reopenRecords(fd, ix)
            assertEquals(42, recs.size); assertEquals(42, recs.map { it.getString("slot") + it.getString("instrument_key") }.toSet().size)
        }
        for (ix in IXS) assertEquals(0, CloseOpenCapture.reconcile(fd, ix, java.time.LocalDate.parse(NEXT_D), CloseOpenCapture.loadQueue(fd, ix)).length())
    }

    @Test
    fun aStaleMarkerIsCorrectedToTheLatestVerifiableClose() {
        val fd = tmp()
        closeSlots(fd, h.CLOSE)
        assertPositiveMarker(fd, "15:30")
        for (ix in IXS) damage(fd, ix, "remove_envelope", "15:30")
        morning(fd)
        for (ix in IXS) assertTrue(closeEntries(fd, ix).all { it.getString("state") == CloseOpenCapture.Q_OPEN_CAPTURED &&
            it.getString("close_last_qualifying_slot") == "15:25" && it.optBoolean("marker_corrected") })
    }

    // ───────────── 2. receipt-based selection ─────────────

    @Test
    fun aLateEarlierSlotDoesNotBlockALaterSlotReceivedInTime() {
        val fd = tmp(); closeSlots(fd, h.CLOSE)
        pollAt(fd, NEXT_D, "09:40", sec = 360)          // received 09:46:00 -> late diagnostic
        for (ix in IXS) assertTrue(closeEntries(fd, ix).all { it.getString("state") == CloseOpenCapture.Q_QUEUED &&
            it.getString("last_open_outcome") == "receipt_after_open_window" })
        pollAt(fd, NEXT_D, "09:45", sec = 1)            // received 09:45:01 -> eligible
        for (ix in IXS) assertTrue(closeEntries(fd, ix).all { it.getString("state") == CloseOpenCapture.Q_OPEN_CAPTURED &&
            it.getString("open_first_qualifying_slot") == "09:45" })
    }

    @Test
    fun theOpenMarkerFollowsTheEarliestEligibleReceiptNotProcessingOrder() {
        val fd = tmp(); closeSlots(fd, h.CLOSE)
        pollAt(fd, NEXT_D, "09:15", sec = 480)          // slot 09:15, received 09:23
        for (ix in IXS) assertTrue(closeEntries(fd, ix).all { it.getString("open_first_qualifying_slot") == "09:15" })
        pollAt(fd, NEXT_D, "09:20", sec = 60)           // slot 09:20, received 09:21 -> earlier receipt wins
        for (ix in IXS) assertTrue(closeEntries(fd, ix).all { it.getString("open_first_qualifying_slot") == "09:20" &&
            it.optBoolean("open_marker_corrected") })
    }

    @Test
    fun theCloseMarkerFollowsTheLatestEligibleReceiptNotTheLatestSlot() {
        val fd = tmp()
        pollAt(fd, CLOSE_D, "15:25", sec = 330)         // received 15:30:30
        pollAt(fd, CLOSE_D, "15:30", sec = 10)          // received 15:30:10
        for (ix in IXS) assertTrue(closeEntries(fd, ix).all { it.getString("close_last_qualifying_slot") == "15:25" &&
            it.getLong("close_selected_receipt_ms") == h.ms(CLOSE_D, "15:25", 330) })
    }

    // ───────────── 2b. the SHARED fixture v2, replayed through the capture ─────────────

    private fun fixtureFile(): File = listOf(File("src/test/resources/close_open_capture/shared_quote_eligibility_v2.json"),
        File("app/src/test/resources/close_open_capture/shared_quote_eligibility_v2.json")).first { it.isFile }

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

    private fun reasonsOf(o: JSONObject) = o.getJSONArray("reasons").let { a -> (0 until a.length()).map { a.getString(it) } }

    @Test
    fun theSharedFixtureV2ReplaysToTheProposedRuleOnTheCapture() {
        val fx = JSONObject(fixtureFile().readText())
        val out = JSONObject().put("fixture_sha256", java.security.MessageDigest.getInstance("SHA-256")
            .digest(fixtureFile().readBytes()).joinToString("") { "%02x".format(it) }).put("cases", JSONObject())
        val mismatches = ArrayList<String>()
        val dir = (System.getProperty("co.sample.dir") ?: System.getenv("CO_SAMPLE_DIR"))?.let { File(it, "r3_fixture") }
        dir?.resolve("exports")?.mkdirs()
        val atm = fx.getJSONObject("atm_contracts")
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
            val mut = c.optJSONObject("storage_mutation")
            var damaged = false
            for ((k, acqs) in groups) {
                if (mut != null && !damaged && k.first == nextD) {
                    // a POSITIVE ready marker is in the queue right now; then the store is damaged
                    for (ix in IXS) assertTrue(closeEntries(fd, ix).all { it.has("close_last_qualifying_slot") })
                    for (ix in IXS) applyMutation(fd, closeD, ix, mut, atm.getJSONObject(ix))
                    damaged = true
                }
                CloseOpenCapture.process(fd, k.first, k.second, acqs)
            }
            CloseOpenCapture.process(fd, nextD, "15:10", IXS.map { CloseOpenCapture.Acquisition(it, null, 0L, 0L) })
            val closeEx = CloseOpenCapture.exportSection(fd, closeD)
            val per = JSONObject()
            for (ix in IXS) {
                val recs = h.records(closeEx, ix).let { a -> (0 until a.length()).map { a.getJSONObject(it) } }
                val vs = h.entries(fd, ix).filter { it.getString("close_session") == closeD }.map { e ->
                    val key = e.getJSONObject("contract").getString("instrument_key")
                    when {
                        e.getString("state") == CloseOpenCapture.Q_OPEN_CAPTURED ->
                            "PAIR@" + e.getString("close_last_qualifying_slot") + "/" + e.getString("open_first_qualifying_slot")
                        e.getString("state") == CloseOpenCapture.Q_CLOSE_EVIDENCE_GAP -> "REFUSED:" + e.getString("close_evidence_gap")
                        e.optString("reason") == CloseOpenCapture.R_NO_QUALIFYING_CLOSE -> {
                            val last = recs.filter { it.getString("instrument_key") == key && it.getString("role") == "CLOSE" }
                                .maxByOrNull { it.getString("slot") }
                            "REFUSED:" + (last?.let { reasonsOf(it.getJSONObject("eligibility")).joinToString("+") } ?: CloseOpenCapture.R_NO_QUALIFYING_CLOSE)
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

    private fun applyMutation(fd: File, closeD: String, ix: String, m: JSONObject, atm: JSONObject) {
        val f = sessFile(fd, closeD, ix)
        val slot = m.optString("slot", "15:30")
        val ceK = atm.optString(m.optString("contract", ""), ""); val other = atm.optString(m.optString("with", ""), "")
        when (val op = m.getString("op")) {
            "delete_close_file" -> assertTrue(f.delete())
            "corrupt_close_file" -> { val t = f.readText(); f.writeText(t.substring(0, t.length / 2)) }
            else -> {
                val s = JSONObject(f.readText()); val obs = s.getJSONObject("observations")
                when (op) {
                    "remove_observation" -> assertTrue(obs.remove("$slot|$ceK") != null)
                    "remove_envelope" -> { val e = s.getJSONObject("envelopes")
                        e.keys().asSequence().toList().filter { e.getJSONObject(it).getString("slot") == slot }.forEach { e.remove(it) } }
                    "make_one_sided" -> obs.getJSONObject("$slot|$ceK").getJSONObject("bbo").put("ask_raw", JSONObject().put("raw", JSONObject.NULL))
                    "replace_with_other_contract" -> obs.put("$slot|$ceK", JSONObject(obs.getJSONObject("$slot|$other").toString()))
                    else -> error(op)
                }
                f.writeText(s.toString())
            }
        }
    }
}
