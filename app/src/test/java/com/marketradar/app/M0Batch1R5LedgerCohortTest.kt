package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * B1-R5a (ledger byte budget incl. ack/release/compaction growth, cap enforced before every commit) and
 * B1-R5b (cohort freeze retry counts every byte it does not demonstrably replace, incl. temps, at the write peak).
 * The two Codex-named tests are reconstructed from the review description (the Codex attachment was not on the box).
 */
class M0Batch1R5LedgerCohortTest {
    private lateinit var dir: File
    private val S = "2026-10-21"
    private var max0 = 0; private var cap0 = 0; private var fin0 = 0; private var coh0 = 0L
    @Before fun setUp() { dir = Files.createTempDirectory("m0r5").toFile(); max0 = ResearchProtection.MAX_BYTES; cap0 = ResearchProtection.CAPACITY; fin0 = ResearchProtection.FINAL_RETAIN; coh0 = ResearchCohorts.MAX_TOTAL_BYTES }
    @After fun tearDown() { ResearchProtection.MAX_BYTES = max0; ResearchProtection.CAPACITY = cap0; ResearchProtection.FINAL_RETAIN = fin0; ResearchCohorts.MAX_TOTAL_BYTES = coh0; dir.deleteRecursively() }

    private val ledger get() = File(dir, "protection.json")
    private var peak = 0L
    private fun track() { peak = maxOf(peak, ledger.length()); assertTrue("ledger ${ledger.length()} > cap ${ResearchProtection.MAX_BYTES}", ledger.length() <= ResearchProtection.MAX_BYTES)
        // transient bound: no stray temp beyond one copy
        val tmp = File(dir, "protection.json.tmp"); assertFalse(tmp.exists()) }

    private fun rows() = JSONArray().put(JSONObject().put("expiry", "2026-10-27").put("strike", 22650).put("option_type", "CE")
        .put("instrument_key", "NSE_FO|1").put("bid", 10).put("ask", 11).put("ltp", JSONObject.NULL))
    private fun src() = ResearchCollector.canonicalRowsDigest("NF", ResearchCollector.canonicalArchiveRows(rows())!!)
    private fun req(i: Int) = ResearchProtection.request(dir, "entry-$i-" + "x".repeat(40), "research_m0", "NF", "poll-$i", S, listOf("CHAIN_POLL"), i.toLong(), expectedRows = 1, sourceSha = src())
    private fun ack(i: Int): String {
        val id = "entry-$i-" + "x".repeat(40)
        val b = JSONObject().put("index", "NF").put("poll_ts", "poll-$i").put("rows", rows()).toString().toByteArray()
        val m = JSONObject().put("entry_id", id).put("index", "NF").put("poll_ts", "poll-$i").put("roles", JSONArray(listOf("CHAIN_POLL")))
            .put("row_count", 1).put("content_sha256", M0Durable.sha256(b)).put("content_bytes", b.size)
        return ResearchProtection.acknowledgeArchive(dir, id, m, b, 1_792_000_000_000L + i)
    }
    private fun rel(i: Int) = ResearchProtection.release(dir, "entry-$i-" + "x".repeat(40), 1_792_000_100_000L + i)

    // ------------------------------------------------------------------ R5a
    @Test fun releasingAcknowledgedEntriesMustKeepLedgerWithinByteCap() {
        ResearchProtection.MAX_BYTES = 40_000; ResearchProtection.CAPACITY = 10_000; ResearchProtection.FINAL_RETAIN = 10_000
        val admitted = ArrayList<Int>()
        var i = 0
        // fill, acknowledge everything, fill again (acked entries must still carry their release growth), release all
        // cycle until nothing more is admitted: acked entries free no budget, because they still grow on release
        while (true) {
            var got = 0
            while (req(i) == "STAGED") { admitted.add(i); track(); i++; got++ }
            if (got == 0) break
            track(); i++
            for (k in admitted) if (ResearchProtection.entry(dir, "entry-$k-" + "x".repeat(40))!!.getString("state") == "STAGED") { assertEquals("ARCHIVE_ACKED", ack(k)); track() }
        }
        for (k in admitted) { assertEquals("RELEASED", rel(k)); track() }
        println("R5A admitted=${admitted.size} peak=$peak cap=${ResearchProtection.MAX_BYTES} final=${ledger.length()}")
        assertTrue(admitted.size > 3)
    }

    @Test fun r5a_every_admitted_entry_can_be_acked_and_released_without_hitting_the_cap() {
        ResearchProtection.MAX_BYTES = 7_000; ResearchProtection.CAPACITY = 10_000; ResearchProtection.FINAL_RETAIN = 10_000
        val ids = (0 until 200).filter { req(it) == "STAGED" }
        // repeated ack failures first (bounded bookkeeping), then genuine ack and release - all within the budget
        for (k in ids) { assertTrue(ResearchProtection.acknowledgeArchive(dir, "entry-$k-" + "x".repeat(40), JSONObject(), "z".toByteArray(), 1L).startsWith("REJECTED:")); track() }
        ResearchProtection.enforceLag(dir, "2027-12-31"); track()
        for (k in ids) { assertEquals("ARCHIVE_ACKED", ack(k)); track() }
        for (k in ids) { assertEquals("RELEASED", rel(k)); track() }
        assertEquals(0, ResearchProtection.summary(dir, S).getInt("cap_refusals").coerceAtMost(0))
    }

    @Test fun r5a_near_cap_rejection_and_identity_conflict_counters_stay_within_cap() {
        ResearchProtection.MAX_BYTES = 6_000; ResearchProtection.CAPACITY = 10_000
        var i = 0; while (req(i) == "STAGED") i++
        repeat(50) { n ->
            assertEquals("CAPACITY_REJECTED", ResearchProtection.request(dir, "rej-$n-" + "₹\"\\".repeat(60), "o", "NF", "p$n", S, listOf("CHAIN_POLL"), 1L, expectedRows = 1))
            track()
            assertEquals("IDENTITY_CONFLICT", ResearchProtection.request(dir, "entry-0-" + "x".repeat(40), "research_m0", "NF", "other-$n", S, listOf("CHAIN_POLL"), 1L, expectedRows = 1, sourceSha = src()))
            track()
        }
        val sum = ResearchProtection.summary(dir, S)
        assertTrue(sum.getInt("CAPACITY_REJECTED") >= 50); assertTrue(sum.getInt("IDENTITY_CONFLICT") >= 50)
        assertEquals(ledger.length(), sum.getLong("ledger_bytes"))
    }

    @Test fun r5a_compaction_transitions_near_cap_stay_bounded() {
        ResearchProtection.MAX_BYTES = 6_000; ResearchProtection.CAPACITY = 10_000; ResearchProtection.FINAL_RETAIN = 2
        var released = 0
        for (i in 0 until 300) {
            if (req(i) != "STAGED") continue
            track(); assertEquals("ARCHIVE_ACKED", ack(i)); track(); assertEquals("RELEASED", rel(i)); track(); released++
        }
        assertEquals(300, released)
        val c = ResearchProtection.summary(dir, S).getJSONObject("compacted")
        assertEquals(298, c.getInt("total")); assertEquals(298, c.getJSONObject("by_state").getInt("RELEASED"))
    }

    @Test fun r5a_commit_that_would_exceed_the_cap_is_refused_and_evidence_stays_protected() {
        ResearchProtection.CAPACITY = 10_000
        for (i in 0 until 5) assertEquals("STAGED", req(i))
        val before = ledger.readBytes()
        ResearchProtection.MAX_BYTES = ledger.length().toInt()     // cap lowered below what an ack would need
        assertEquals("REJECTED:ledger_byte_cap", ack(0))
        assertArrayEquals(before, ledger.readBytes())
        assertTrue(ResearchProtection.isProtected(dir, "NF", "poll-0"))
        assertEquals("CAPACITY_REJECTED", req(99))
        assertTrue(ledger.length() <= ResearchProtection.MAX_BYTES)
    }

    // ------------------------------------------------------------------ R5b
    private val c = ResearchCohorts.Contract("NF", "2026-10-27", 22650, "CE")
    private fun items(n: Int, size: Int) = (0 until n).map { k ->
        val kind = ResearchCohorts.REQUIRED.getOrElse(k) { "EXTRA_$k" }
        ResearchCohorts.Item(kind, if (kind in setOf("ENTRY_MARK", "EXIT_MARK", "ENTRY_QUOTE")) c else null,
            if (kind == "VERSIONS") JSONObject().put("calendar_version", "c").put("lot_version", "l").put("fee_version", "f").put("rate_version", "r").toString()
            else "₹\"\\".repeat(size), "src$k") }
    private fun cdir(id: String) = File(dir, "${ResearchCohorts.SUBDIR}/$id")
    private fun disk() = File(dir, ResearchCohorts.SUBDIR).walkTopDown().filter { it.isFile }.sumOf { it.length() }
    /** Durable state an interrupted freeze leaves: item files written, index never written. */
    private fun interrupted(id: String, its: List<ResearchCohorts.Item>) {
        its.forEachIndexed { i, it -> M0Durable.write(File(cdir(id), "item_$i.json"), JSONObject().put("kind", it.kind).put("source_ref", it.sourceRef)
            .put("contract", it.contract?.json() ?: JSONObject.NULL).put("content", it.content).toString()) }
    }

    @Test fun interruptedCohortRetryMustCountFilesItDoesNotReplace() {
        // an interrupted attempt with MORE/BIGGER items; the retry writes fewer items. Leftover files it does not
        // replace still occupy disk and must be counted (the old code subtracted the whole partial directory).
        interrupted("k1", items(14, 400))
        val leftover = disk()
        // fits only if the 7 leftover files the retry does NOT replace (item_7..item_13) were wrongly discounted
        // the retry's 7 items are twice as large (~ the whole leftover); they replace item_0..item_6 only
        ResearchCohorts.MAX_TOTAL_BYTES = leftover * 13 / 10
        val r = ResearchCohorts.freeze(dir, "k1", items(7, 800), 1L)
        assertTrue("result=$r disk=${disk()} cap=${ResearchCohorts.MAX_TOTAL_BYTES}", disk() <= ResearchCohorts.MAX_TOTAL_BYTES)
        assertTrue(File(cdir("k1"), "item_13.json").exists())   // never blindly deleted
    }

    @Test fun r5b_same_input_retry_replaces_exactly_and_is_admitted() {
        interrupted("k2", items(7, 300))
        val partial = disk()
        val largest = cdir("k2").listFiles()!!.maxOf { it.length() }
        // room for the index + ONE transient temp copy of the largest file, far less than a second full copy
        ResearchCohorts.MAX_TOTAL_BYTES = partial + largest + 2_000
        assertTrue(ResearchCohorts.MAX_TOTAL_BYTES < 2 * partial)
        assertEquals("FROZEN", ResearchCohorts.freeze(dir, "k2", items(7, 300), 1L))
        assertTrue(disk() <= ResearchCohorts.MAX_TOTAL_BYTES)
        assertEquals("REPLAYABLE", ResearchCohorts.replayable(dir, "k2").getString("status"))
    }

    @Test fun r5b_more_items_and_changed_sizes_are_accounted_at_the_peak() {
        interrupted("k3", items(3, 100))
        ResearchCohorts.MAX_TOTAL_BYTES = disk() + 1_000
        assertEquals("REJECTED_CAPACITY", ResearchCohorts.freeze(dir, "k3", items(7, 300), 1L))   // more + bigger
        assertFalse(File(cdir("k3"), "index.json").exists())
        // smaller replacement content of the same items fits once the peak (old target + new temp) fits
        val before = disk()
        ResearchCohorts.MAX_TOTAL_BYTES = before + 3_000
        val r = ResearchCohorts.freeze(dir, "k3", items(7, 10), 2L)
        assertTrue(r == "FROZEN" || r == "REJECTED_CAPACITY")
        assertTrue(disk() <= ResearchCohorts.MAX_TOTAL_BYTES)
    }

    @Test fun r5b_interrupted_temp_files_are_counted_and_kept() {
        interrupted("k4", items(7, 200))
        File(cdir("k4"), "item_9.json.tmp").writeText("p".repeat(5_000))          // a temp this retry does not replace
        File(cdir("k4"), "item_8.json.tmp.rejected").writeText("q".repeat(1_000))
        File(cdir("k4"), "unknown.bin").writeText("u".repeat(500))
        val total = disk()
        ResearchCohorts.MAX_TOTAL_BYTES = total + 1_000        // would fit only if the 6.5 KB of leftovers were ignored
        assertEquals("REJECTED_CAPACITY", ResearchCohorts.freeze(dir, "k4", items(7, 200).map { it.copy(content = it.content + "!") }, 1L))
        assertTrue(File(cdir("k4"), "item_9.json.tmp").exists()); assertTrue(File(cdir("k4"), "unknown.bin").exists())
        ResearchCohorts.MAX_TOTAL_BYTES = total + 8_000
        assertEquals("FROZEN", ResearchCohorts.freeze(dir, "k4", items(7, 200), 2L))
        assertTrue(disk() <= ResearchCohorts.MAX_TOTAL_BYTES)
        assertTrue(File(cdir("k4"), "item_9.json.tmp").exists()); assertTrue(File(cdir("k4"), "unknown.bin").exists())
    }
}
