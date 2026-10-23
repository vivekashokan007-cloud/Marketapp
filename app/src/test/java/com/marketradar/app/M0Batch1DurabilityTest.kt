package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.time.LocalDateTime

/**
 * M0 Batch 1 - W5 (Codex R4 + CDX-G1R N8) and W4 (Codex R3) against the real collector/ledger code.
 * Equivalents of the Codex counterexamples (the original counterexample files were not in the handoff zip; names kept).
 */
class M0Batch1DurabilityTest {
    private lateinit var dir: File
    private val S = "2026-10-21"
    private fun ms(h: Int, m: Int) = LocalDateTime.parse("${S}T%02d:%02d:00".format(h, m)).atZone(ResearchCalendar.IST).toInstant().toEpochMilli()
    private val saved = IntArray(4)
    private var savedBytes = 0L; private var savedProtBytes = 0

    @Before fun setUp() {
        dir = Files.createTempDirectory("m0b1dur").toFile()
        saved[0] = ResearchCollector.MAX_IMAP_FILES; saved[1] = ResearchProtection.CAPACITY; saved[2] = ResearchProtection.FINAL_RETAIN
        saved[3] = ResearchCollector.MAX_MANIFEST_SESSIONS; savedBytes = ResearchCollector.MAX_IMAP_BYTES; savedProtBytes = ResearchProtection.MAX_BYTES
    }
    @After fun tearDown() {
        dir.deleteRecursively()
        ResearchCollector.MAX_IMAP_FILES = saved[0]; ResearchProtection.CAPACITY = saved[1]; ResearchProtection.FINAL_RETAIN = saved[2]
        ResearchCollector.MAX_MANIFEST_SESSIONS = saved[3]; ResearchCollector.MAX_IMAP_BYTES = savedBytes; ResearchProtection.MAX_BYTES = savedProtBytes
        M0Durable.crashAt = null; M0Durable.crashFile = null
    }

    private fun chain(index: String, keyTag: String, expiry: String = "2026-10-27") = JSONObject().put("data", JSONArray().apply {
        for (k in listOf(100, 200)) put(JSONObject().put("strike_price", k).put("expiry", expiry)
            .put("call_options", JSONObject().put("instrument_key", "NSE_FO|$keyTag$k" + "CE").put("market_data", JSONObject().put("bid_price", 1.0).put("ask_price", 1.1)))
            .put("put_options", JSONObject().put("instrument_key", "NSE_FO|$keyTag$k" + "PE").put("market_data", JSONObject().put("bid_price", 1.0).put("ask_price", 1.1))))
    })

    private fun captureSlot(ord: Int, keyTag: String, index: String = "NF"): JSONObject {
        val now = ResearchCalendar.slotGrid(java.time.LocalDate.parse(S)).first { it.ordinal == ord }.nominalMs + 5_000
        val b = ResearchCollector.beginAttempt(dir, index, "$S|$ord", now) as ResearchCollector.Begin.Started
        val c = ResearchCollector.captureFromChain(dir, index, chain(index, keyTag), now, now, "ml_option_chain_snapshots", now,
            "${S}T00:00:$ord", now, b.attemptId)
        ResearchCollector.recordCapture(dir, b.slotId, b.attemptId, c, now + 1000)
        return ResearchCollector.readManifest(dir, S)!!.getJSONObject("indices").getJSONObject(index).getJSONObject("slots").getJSONObject(ord.toString())
    }

    private fun imapCount() = dir.listFiles { f -> f.name.startsWith("imap_") && f.name.endsWith(".json") }!!.size

    // ---------------------------------------------------------------- W5a
    @Test fun r4_referenced_maps_cannot_bypass_map_capacity() {
        ResearchCollector.setEnabled(dir, true, ms(9, 0))
        ResearchCollector.MAX_IMAP_FILES = 3
        val okVersions = ArrayList<String>()
        for (ord in 1..8) {
            val sl = captureSlot(ord, "K$ord")
            assertTrue("map files ${imapCount()}", imapCount() <= 3)
            val cap = sl.getJSONArray("attempts").getJSONObject(0).getJSONObject("capture")
            if (!cap.isNull("imap_version")) okVersions.add(cap.getString("imap_version"))
            else {
                assertEquals("IMAP_CAPACITY", cap.getString("imap_unavailable"))
                assertEquals("instrument_map_unavailable:IMAP_CAPACITY", sl.getString("reason"))
            }
        }
        assertEquals(3, okVersions.size)   // all referenced -> cap holds, later maps rejected explicitly
        for (v in okVersions) assertNotNull("MapRef $v must resolve", ResearchCollector.readInstrumentMap(dir, v))
    }

    @Test fun admitted_map_lease_protects_it_until_the_capture_commits() {
        ResearchCollector.MAX_IMAP_FILES = 2
        val a = ResearchCollector.putInstrumentMap(dir, "NF", listOf("NF|e|1|CE|A"), 1L, "attemptA") as ResearchCollector.MapRef.Ok
        // another index admits maps in between (the old prune could delete A here)
        ResearchCollector.putInstrumentMap(dir, "BNF", listOf("BNF|e|1|CE|B"), 2L, null)
        ResearchCollector.putInstrumentMap(dir, "BNF", listOf("BNF|e|1|CE|C"), 3L, null)
        assertNotNull(ResearchCollector.readInstrumentMap(dir, a.version))
        assertTrue(imapCount() <= 2)
        // sustained map churn without references stays bounded
        repeat(50) { ResearchCollector.putInstrumentMap(dir, "BNF", listOf("BNF|e|1|CE|X$it"), 10L + it, null) }
        assertTrue(imapCount() <= 2); assertNotNull(ResearchCollector.readInstrumentMap(dir, a.version))
        // an orphan map file (crash between map write and admission) is never served and is cleaned
        File(dir, "imap_${"f".repeat(64)}.json").writeText("x")
        ResearchCollector.putInstrumentMap(dir, "BNF", listOf("BNF|e|1|CE|Y"), 99L, null)
        assertFalse(File(dir, "imap_${"f".repeat(64)}.json").exists())
    }

    // ---------------------------------------------------------------- W5b
    private fun chainArchive(id: String, idx: String, poll: String, rows: Int): Pair<JSONObject, ByteArray> {
        val content = JSONObject().put("index", idx).put("poll_ts", poll).put("rows", JSONArray().apply { repeat(rows) { put(JSONObject().put("r", it)) } })
            .toString().toByteArray()
        return JSONObject().put("entry_id", id).put("index", idx).put("poll_ts", poll).put("roles", JSONArray(listOf("CHAIN_POLL")))
            .put("content_sha256", M0Durable.sha256(content)).put("content_bytes", content.size).put("row_count", rows) to content
    }

    @Test fun r4_final_entries_cannot_bypass_total_protection_capacity() {
        ResearchProtection.CAPACITY = 5; ResearchProtection.FINAL_RETAIN = 3
        var maxEntries = 0; var maxBytes = 0
        repeat(300) { i ->
            val id = "e$i"
            assertEquals("STAGED", ResearchProtection.request(dir, id, "o", "NF", "p$i", S, listOf("CHAIN_POLL"), i.toLong(), expectedRows = 2))
            val (m, c) = chainArchive(id, "NF", "p$i", 2)
            assertEquals("ARCHIVE_ACKED", ResearchProtection.acknowledgeArchive(dir, id, m, c, i.toLong()))
            assertEquals("RELEASED", ResearchProtection.release(dir, id, i.toLong()))
            val txt = (M0Durable.read(File(dir, "protection.json")) as M0Durable.Read.Ok).json
            maxEntries = maxOf(maxEntries, txt.getJSONObject("entries").length()); maxBytes = maxOf(maxBytes, txt.toString().length)
        }
        assertTrue("entries $maxEntries", maxEntries <= ResearchProtection.CAPACITY + ResearchProtection.FINAL_RETAIN)
        assertTrue("bytes $maxBytes", maxBytes < 4_000)
        val sum = ResearchProtection.summary(dir, S)
        assertEquals(297, sum.getJSONObject("compacted").getInt("total"))
        assertEquals(64, sum.getJSONObject("compacted").getString("chain").length)
        assertEquals("OK", sum.getString("integrity"))
        // acknowledged-but-unreleased entries still count against the cap (no bypass through final-ish states)
        repeat(5) { i -> ResearchProtection.request(dir, "h$i", "o", "NF", "q$i", S, listOf("CHAIN_POLL"), 1L, expectedRows = 1)
            val (m, c) = chainArchive("h$i", "NF", "q$i", 1); ResearchProtection.acknowledgeArchive(dir, "h$i", m, c, 1L) }
        assertEquals("CAPACITY_REJECTED", ResearchProtection.request(dir, "h9", "o", "NF", "q9", S, listOf("CHAIN_POLL"), 1L, expectedRows = 1))
        assertEquals("PROTECTION_BACKLOG_AT_CAPACITY", ResearchProtection.admission(dir))
        // byte bound (fresh ledger)
        dir.listFiles()!!.forEach { it.delete() }
        ResearchProtection.CAPACITY = 1000; ResearchProtection.MAX_BYTES = 3_000
        var rejected = false
        repeat(50) { i -> if (ResearchProtection.request(dir, "b$i".repeat(10), "o", "NF", "x$i", S, listOf("CHAIN_POLL"), 1L) == "CAPACITY_REJECTED") rejected = true }
        assertTrue(rejected)
        assertTrue((M0Durable.read(File(dir, "protection.json")) as M0Durable.Read.Ok).body.length <= 3_000)
    }

    // ---------------------------------------------------------------- W5d / N8
    @Test fun corrupt_ledger_is_not_reported_as_healthy() {
        // first use: absent ledger is NO_EVIDENCE, not OK
        assertEquals("NO_EVIDENCE", ResearchProtection.summary(dir, S).getString("integrity"))
        ResearchProtection.request(dir, "A", "o", "NF", "t1", S, listOf("CHAIN_POLL"), 1L, expectedRows = 1)
        File(dir, "protection.json").writeText("{garbage")
        assertEquals("LEDGER_CORRUPT", ResearchProtection.summary(dir, S).getString("integrity"))
        ResearchCollector.setEnabled(dir, true, ms(9, 0))
        assertEquals("INTEGRITY_FAULT", ResearchCollector.status(dir, ms(10, 0)).getString("label"))
        // persisted: even if the corrupt file disappears, the ledger is not silently reset to empty/healthy
        File(dir, "protection.json").delete()
        assertEquals("LEDGER_CORRUPT", ResearchProtection.summary(dir, S).getString("integrity"))
        assertTrue(ResearchProtection.isProtected(dir, "NF", "anything"))
    }

    @Test fun lost_established_ledger_is_corrupt_not_first_use() {
        ResearchProtection.request(dir, "A", "o", "NF", "t1", S, listOf("CHAIN_POLL"), 1L, expectedRows = 1)
        File(dir, "protection.json").delete()
        val s = ResearchProtection.summary(dir, S)
        assertEquals("LEDGER_CORRUPT", s.getString("integrity")); assertEquals("established_ledger_lost", s.getString("corrupt_reason"))
    }

    @Test fun request_does_not_overwrite_corrupted_evidence() {
        ResearchProtection.request(dir, "A", "o", "NF", "t1", S, listOf("CHAIN_POLL"), 1L, expectedRows = 1)
        val f = File(dir, "protection.json")
        val sealed = JSONObject(f.readText()); sealed.put("body", sealed.getString("body").replace("t1", "tX"))   // digest mismatch
        f.writeText(sealed.toString())
        val before = f.readBytes()
        assertEquals("LEDGER_CORRUPT", ResearchProtection.request(dir, "B", "o", "NF", "t2", S, listOf("CHAIN_POLL"), 2L))
        val (m, c) = chainArchive("A", "NF", "t1", 1)
        assertEquals("LEDGER_CORRUPT", ResearchProtection.acknowledgeArchive(dir, "A", m, c, 3L))
        assertEquals("LEDGER_CORRUPT", ResearchProtection.release(dir, "A", 3L))
        assertEquals(0, ResearchProtection.enforceLag(dir, "2026-12-01", 1))
        assertArrayEquals(before, f.readBytes())
        ResearchCollector.setEnabled(dir, true, ms(9, 0))
        assertEquals(ResearchCollector.Begin.Unavailable("LEDGER_CORRUPT"), ResearchCollector.beginAttempt(dir, "NF", "$S|40", ms(12, 30)))
        assertArrayEquals(before, f.readBytes())
    }

    @Test fun corrupt_manifest_is_never_recreated_over() {
        ResearchCollector.setEnabled(dir, true, ms(9, 0))
        ResearchCollector.beginAttempt(dir, "NF", "$S|40", ms(12, 30))
        val f = File(dir, "manifest_$S.json"); f.writeText(f.readText().dropLast(20))
        val before = f.readBytes()
        assertEquals(ResearchCollector.Begin.Unavailable("MANIFEST_CORRUPT"), ResearchCollector.beginAttempt(dir, "NF", "$S|41", ms(12, 35)))
        val r = ResearchCollector.reconcile(dir, ms(23, 0))
        assertEquals(S, r.getJSONArray("corrupt_manifests").getString(0))
        assertArrayEquals(before, f.readBytes())
        assertEquals("INTEGRITY_FAULT", ResearchCollector.status(dir, ms(23, 1)).getString("label"))
    }

    // ---------------------------------------------------------------- W5c
    @Test fun replacement_is_fsync_and_atomic_rename_without_delete_then_rename() {
        val src = File("src/main/java/com/marketradar/app/M0Durable.kt").readText()
        assertTrue(src.contains("fd.sync()")); assertTrue(src.contains("ATOMIC_MOVE")); assertTrue(src.contains("fun fsyncDir"))
        val col = File("src/main/java/com/marketradar/app/ResearchCollector.kt").readText()
        assertFalse(col.contains("f.delete(); if (!tmp.renameTo(f))"))
        assertTrue(col.contains("internal fun writeAtomic(f: File, text: String) = M0Durable.write(f, text)"))
    }

    @Test fun leftover_temp_files_are_never_promoted_without_validation() {
        val good = M0Durable.seal("protection", JSONObject().put("entries", JSONObject()).toString())
        // target absent + valid temp -> promoted
        File(dir, "protection.json.tmp").writeText(good)
        // target present + valid temp -> committed old wins; temp quarantined
        M0Durable.write(File(dir, "meta.json"), JSONObject().put("enabled", false).toString())
        File(dir, "meta.json.tmp").writeText(M0Durable.seal("meta", JSONObject().put("enabled", true).toString()))
        // target absent + torn temp -> quarantined, not promoted
        File(dir, "seq.json.tmp").writeText(M0Durable.seal("seq", "{\"epoch\":\"x\"}").dropLast(5))
        // wrong owner / wrong kind -> quarantined
        File(dir, "conflicts.json.tmp").writeText(JSONObject(M0Durable.seal("conflicts", "{}")).put("owner", "evil").toString())
        File(dir, "integrity.json.tmp").writeText(M0Durable.seal("protection", "{}"))
        val r = M0Durable.recover(dir)
        assertEquals(listOf("protection.json"), (0 until r.getJSONArray("promoted").length()).map { r.getJSONArray("promoted").getString(it) })
        assertEquals(4, r.getJSONArray("quarantined").length())
        assertFalse(ResearchCollector.meta(dir).getBoolean("enabled"))
        assertFalse(File(dir, "seq.json").exists()); assertFalse(File(dir, "conflicts.json").exists()); assertFalse(File(dir, "integrity.json").exists())
        assertTrue(dir.listFiles { f -> f.name.endsWith(".tmp") }!!.isEmpty())
    }

    // ---------------------------------------------------------------- W5 PROCESS-CRASH recovery (fault injection)
    private fun runChild(scenario: String, boundary: String, target: String, extra: String? = null): Int {
        val java = File(System.getProperty("java.home"), "bin/java").path
        val cmd = mutableListOf(java, "-cp", System.getProperty("java.class.path"), "com.marketradar.app.M0CrashChild", dir.path, scenario, boundary, target)
        if (extra != null) cmd.add(extra)
        val p = ProcessBuilder(cmd).redirectErrorStream(true).start()
        val out = p.inputStream.bufferedReader().readText()
        val code = p.waitFor()
        assertTrue("child output: $out", code == 77 || out.contains("NO_CRASH"))
        return code
    }

    /**
     * PROCESS-CRASH RECOVERY (not hardware power-loss durability): a child JVM is killed (Runtime.halt) at each write
     * boundary of the protection ledger, a manifest and the map index; after recovery the committed state is the
     * complete OLD or complete NEW record, never empty-healthy and never a corrupt read.
     */
    @Test fun process_crash_recovery_at_every_write_boundary_yields_old_or_new() {
        for (b in M0Durable.BOUNDARIES) {
            // protection ledger with existing history
            dir.deleteRecursively(); dir.mkdirs()
            assertEquals("boundary $b", 77, runChild("protection", b, "protection.json"))
            M0Durable.recover(dir)
            val l = ResearchProtection.load(dir)
            assertTrue("protection@$b -> $l", l is ResearchProtection.Load.Ok)
            val ids = (l as ResearchProtection.Load.Ok).o.getJSONObject("entries").keys().asSequence().toSet()
            assertTrue("protection@$b ids=$ids", ids == setOf("A") || ids == setOf("A", "B"))
            assertNotEquals("NO_EVIDENCE", ResearchProtection.summary(dir, S).getString("integrity"))
            assertTrue(ResearchProtection.isProtected(dir, "NF", "t1"))

            // first-ever ledger write: old = genuine first use (NO_EVIDENCE, never OK), new = {B}
            dir.deleteRecursively(); dir.mkdirs()
            runChild("protection", b, "protection.json", "first")
            M0Durable.recover(dir)
            val f = ResearchProtection.load(dir)
            assertFalse("first@$b -> $f", f is ResearchProtection.Load.Corrupt)
            if (f is ResearchProtection.Load.Fresh) assertEquals("NO_EVIDENCE", ResearchProtection.summary(dir, S).getString("integrity"))
            else assertEquals(setOf("B"), (f as ResearchProtection.Load.Ok).o.getJSONObject("entries").keys().asSequence().toSet())

            // manifest: one or two attempts on slot 40, never a lost/empty manifest
            dir.deleteRecursively(); dir.mkdirs()
            assertEquals(77, runChild("manifest", b, "manifest_$S.json"))
            M0Durable.recover(dir)
            val m = ResearchCollector.readManifest(dir, S)!!
            val n = m.getJSONObject("indices").getJSONObject("NF").getJSONObject("slots").getJSONObject("40").getJSONArray("attempts").length()
            assertTrue("manifest@$b attempts=$n", n == 1 || n == 2)
            assertEquals(78, m.getJSONObject("indices").getJSONObject("NF").getInt("expected"))

            // instrument-map admission index: map 1 always admitted and resolvable
            dir.deleteRecursively(); dir.mkdirs()
            assertEquals(77, runChild("imap", b, "imapindex.json"))
            M0Durable.recover(dir)
            val adm = InstrumentMaps.admitted(dir)
            val v1 = M0Durable.sha256("NF|2026-10-27|100|CE|K1")
            assertTrue("imap@$b", v1 in adm && adm.size in 1..2)
            for (v in adm) assertNotNull(ResearchCollector.readInstrumentMap(dir, v))
        }
    }
}
