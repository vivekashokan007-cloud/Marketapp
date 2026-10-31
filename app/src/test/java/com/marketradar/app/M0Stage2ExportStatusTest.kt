package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import java.io.OutputStream
import java.nio.file.Files
import java.security.MessageDigest
import java.time.LocalDate
import java.time.LocalDateTime
import java.util.zip.ZipInputStream

/**
 * M0 Stage 2 (CDX-STAGE2-BUILD-AUTH-1): CI-2 read-only research export and CI-4 capacity status.
 * Every test uses the real collector code paths to build evidence, then proves the export is byte-exact, read-only,
 * consistent under concurrent capture, bounded, and honest about corruption / missing references / IO failure.
 */
class M0Stage2ExportStatusTest {
    private lateinit var dir: File
    private lateinit var tmp: File
    private val S1 = "2026-10-22"
    private val ID = JSONObject().put("version_name", "2.6.85").put("version_code", 516).put("marketapp_source_head", "test")

    private fun ms(day: String, h: Int, m: Int, s: Int = 0): Long =
        LocalDateTime.of(LocalDate.parse(day), java.time.LocalTime.of(h, m, s)).atZone(ResearchCalendar.IST).toInstant().toEpochMilli()

    private fun chain(index: String, strikes: List<Int> = listOf(100, 200)): JSONObject {
        val data = JSONArray()
        for (k in strikes) {
            fun leg(t: String) = JSONObject().put("instrument_key", "NSE_FO|$index$k$t")
                .put("market_data", JSONObject().put("ltp", 10.0).put("bid_price", 9.5).put("ask_price", 10.5))
            data.put(JSONObject().put("strike_price", k.toDouble()).put("expiry", "2026-10-27").put("call_options", leg("CE")).put("put_options", leg("PE")))
        }
        return JSONObject().put("status", "success").put("data", data)
    }

    /** One real poll (NF+BNF attempts, captures, envelope) through the collector APIs. */
    private fun poll(ord: Int) {
        val now = ms(S1, 9, 15) + (ord - 1) * 300_000L + 5_000
        for (ix in listOf("NF", "BNF")) {
            val a = ResearchCollector.beginAttempt(dir, ix, "$S1|$ord", now) as ResearchCollector.Begin.Started
            val cap = ResearchCollector.captureFromChain(dir, ix, chain(ix), now - 800, now, "ml_option_chain_snapshots", now + 300, java.time.Instant.ofEpochMilli(now).toString(), now, a.attemptId)
            ResearchCollector.recordCapture(dir, a.slotId, a.attemptId, cap, now)
            ResearchCollector.closeIfOpen(dir, a.slotId, a.attemptId, now)
        }
        val raw = JSONObject().put("schema", "research_m0_raw_forecast_v1").put("status", "VALID").put("direction", "BULL")
            .put("confidence", 60.0).put("horizon", "UNDECLARED").put("brain_version", "2.6.85").put("input_last_poll_slot_key", "$S1|$ord")
        ResearchCollector.putEnvelope(dir, ResearchCollector.buildEnvelope("$S1|$ord", raw, now, now + 100, "2.6.85", 516, JSONObject()), now + 200)
    }

    private fun tree(d: File): Map<String, String> = d.walkTopDown().filter { it.isFile }.associate {
        it.relativeTo(d).invariantSeparatorsPath to (MessageDigest.getInstance("SHA-256").digest(it.readBytes()).joinToString("") { b -> "%02x".format(b) } + ":" + it.lastModified())
    }

    private class Doc { val out = ByteArrayOutputStream(); fun bytes() = out.toByteArray() }

    private fun run(doc: Doc, limits: ResearchExport.Limits = ResearchExport.Limits(retryBackoffMs = 1), cancel: () -> Boolean = { false },
                    sink: (() -> OutputStream)? = null, free: (File) -> Long = { Long.MAX_VALUE / 4 }): JSONObject =
        ResearchExport.export(dir, tmp, ID, ms(S1, 16, 0), sink ?: { doc.out }, { ByteArrayInputStream(doc.bytes()) },
            cancel, limits, freeSpace = free)

    private fun unzip(b: ByteArray): Map<String, ByteArray> {
        val m = LinkedHashMap<String, ByteArray>()
        ZipInputStream(ByteArrayInputStream(b)).use { z -> while (true) { val e = z.nextEntry ?: break; m[e.name] = z.readBytes() } }
        return m
    }

    @Before fun setUp() {
        dir = Files.createTempDirectory("m0s2").toFile(); tmp = Files.createTempDirectory("m0s2tmp").toFile()
        ResearchExport.afterFileHook = null; ResearchExport.lastResult = null
        ResearchCollector.setEnabled(dir, true, ms(S1, 9, 0))
    }
    @After fun tearDown() { ResearchExport.afterFileHook = null; dir.deleteRecursively(); tmp.deleteRecursively() }

    // ------------------------------------------------------------- CI-2 success + independent readback
    @Test fun export_is_byte_exact_with_manifest_and_independent_readback_and_never_mutates_evidence() {
        for (o in 1..6) poll(o)
        val before = tree(dir)
        val doc = Doc()
        val r = run(doc)
        assertEquals(r.toString(), "COMPLETE_VERIFIED", r.getString("status"))
        assertEquals(before, tree(dir))                              // no byte or mtime changed, no file added/removed
        val z = unzip(doc.bytes())
        val man = JSONObject(String(z.getValue(ResearchExport.MANIFEST)))
        assertEquals(ResearchExport.SCHEMA, man.getString("schema"))
        assertEquals(ResearchCalendar.VERSION, man.getString("calendar_version"))
        assertEquals("special_session_protocol_v1", man.getJSONObject("protocol_ref").getString("name"))
        assertEquals(516, man.getJSONObject("build").getInt("version_code"))
        assertTrue(man.getString("created_ist").endsWith("+05:30"))
        assertEquals("NO_FINDINGS", man.getString("evidence_status"))
        // independent readback: each exported entry == source bytes, and == manifest hash/size
        val files = man.getJSONArray("files")
        assertEquals(before.size, files.length())
        for (i in 0 until files.length()) {
            val f = files.getJSONObject(i); val p = f.getString("path")
            val src = File(dir, p.removePrefix(ResearchExport.ROOT)).readBytes()
            assertTrue(p, src.contentEquals(z.getValue(p)))
            assertEquals(M0Durable.sha256(src), f.getString("sha256")); assertEquals(src.size.toLong(), f.getLong("size"))
        }
        // SHA256SUMS lists every file + manifest
        val sums = String(z.getValue(ResearchExport.SUMS)).trim().lines()
        assertEquals(files.length() + 1, sums.size)
        assertEquals(M0Durable.sha256(doc.bytes()), r.getString("zip_sha256"))
        assertTrue(r.getJSONObject("readback").getBoolean("ok"))
        assertTrue(tmp.listFiles()!!.isEmpty())                      // temp work cleaned
        // ledger/cohort/toggle state unchanged
        assertTrue(ResearchCollector.isEnabled(dir))
        writeMetrics("export_success", r, doc.bytes().size)
        // cross-language fixture for tools/research_m0_export (offline verify + B9 reproduction)
        File("build/m0_stage2_export_fixture.zip").writeBytes(doc.bytes())
    }

    @Test fun canonical_digest_matches_python_tool_vector() {
        // same vector is asserted by tools/research_m0_export/test_verify_export.py
        val rows = listOf(ResearchCollector.canonicalRow("2026-10-27", 100.0, "CE", "NSE_FO|NF100CE", 9.5, 10.5, 10.0),
            ResearchCollector.canonicalRow("2026-10-27", 100, "put", "NSE_FO|NF100PE", "-0.0", null, "1E+1"))
        assertEquals(listOf("2026-10-27|100|CE|NSE_FO|NF100CE|9.5|10.5|10", "2026-10-27|100|PE|NSE_FO|NF100PE|0|null|10"), rows)
        assertEquals(VECTOR_SHA, ResearchCollector.canonicalRowsDigest("NF", rows))
    }
    private val VECTOR_SHA = M0Durable.sha256("m0-chain-rows-v1\nNF\n2\n2026-10-27|100|CE|NSE_FO|NF100CE|9.5|10.5|10\n2026-10-27|100|PE|NSE_FO|NF100PE|0|null|10")

    @Test fun readback_detects_a_tampered_document() {
        poll(1)
        val doc = Doc(); val r = run(doc)
        val b = doc.bytes().clone(); b[b.size / 3] = (b[b.size / 3] + 1).toByte()
        val v = try { ResearchExport.verifyStream(ByteArrayInputStream(b), r.getString("zip_sha256")) } catch (e: Exception) { JSONObject().put("ok", false) }
        assertFalse(v.optBoolean("ok"))
    }

    // ------------------------------------------------------------- cancellation / IO failure
    @Test fun cancellation_and_io_failure_leave_evidence_intact_and_are_visible() {
        for (o in 1..3) poll(o)
        val before = tree(dir)
        var n = 0
        val c = run(Doc(), cancel = { ++n > 3 })
        assertEquals("CANCELLED", c.getString("status")); assertEquals("NONE_WRITTEN", c.getString("partial_output"))
        assertEquals(before, tree(dir)); assertTrue(tmp.listFiles()!!.isEmpty())
        val failing = object : OutputStream() { var w = 0
            override fun write(b: Int) { if (++w > 100) throw IOException("ENOSPC") }
            override fun write(b: ByteArray, o: Int, l: Int) { w += l; if (w > 100) throw IOException("ENOSPC") } }
        val f = run(Doc(), sink = { failing })
        assertEquals("FAILED_IO", f.getString("status")); assertEquals("write_document", f.getString("stage"))
        assertTrue(f.getBoolean("sink_written"))
        assertEquals(before, tree(dir)); assertTrue(tmp.listFiles()!!.isEmpty())
        val s = run(Doc(), free = { 10L })
        assertEquals("REFUSED_INSUFFICIENT_SPACE", s.getString("status"))
        assertEquals(before, tree(dir))
    }

    // ------------------------------------------------------------- corruption + missing references
    @Test fun corrupt_and_missing_references_are_reported_and_bytes_preserved() {
        for (o in 1..2) poll(o)
        val env = dir.listFiles { f -> f.name.startsWith("env_") }!!.first()
        val corruptBytes = env.readBytes().also { it[it.size / 2] = 'X'.code.toByte() }
        env.writeBytes(corruptBytes)
        val imap = dir.listFiles { f -> f.name.startsWith("imap_") && f.name != "imapindex.json" }!!.first()
        val imapName = imap.name
        assertTrue(imap.renameTo(File(tmp, "moved_out")))           // reference target missing
        val before = tree(dir)
        val doc = Doc(); val r = run(doc)
        assertEquals("COMPLETE_VERIFIED", r.getString("status"))
        assertEquals("FINDINGS_REPORTED", r.getString("evidence_status"))
        val z = unzip(doc.bytes())
        val man = JSONObject(String(z.getValue(ResearchExport.MANIFEST)))
        assertTrue(man.getJSONArray("corrupt").toString().contains(env.name))
        assertTrue(man.getJSONArray("missing_references").toString(), man.getJSONArray("missing_references").toString().contains(imapName))
        assertTrue(corruptBytes.contentEquals(z.getValue(ResearchExport.ROOT + env.name)))   // exported as-is, no repair
        assertEquals(before, tree(dir))
        File(tmp, "moved_out").delete()
    }

    // ------------------------------------------------------------- concurrent capture consistency
    @Test fun concurrent_capture_write_is_retried_then_refused_never_silently_dropped() {
        for (o in 1..2) poll(o)
        // a capture lands during the first copy only -> attempt 1 discarded, attempt 2 consistent and includes it
        var fired = 0
        ResearchExport.afterFileHook = { _, i -> if (i == 0 && fired++ == 0) poll(3) }
        val doc = Doc(); val r = run(doc)
        assertEquals(r.toString(), "COMPLETE_VERIFIED", r.getString("status"))
        assertEquals(2, r.getInt("snapshot_attempts"))
        assertTrue(r.getJSONArray("snapshot_failures").getJSONObject(0).getInt("changed_count") > 0)
        val man = JSONObject(String(unzip(doc.bytes()).getValue(ResearchExport.MANIFEST)))
        assertEquals(tree(dir).size, man.getInt("file_count"))       // the concurrently written files are all present
        for (i in 0 until man.getJSONArray("files").length()) { val f = man.getJSONArray("files").getJSONObject(i)
            assertEquals(f.getString("sha256"), M0Durable.sha256(File(dir, f.getString("path").removePrefix("research_m0/")).readBytes())) }
        // capture writes on EVERY attempt -> refused, nothing written to the document
        var ord = 4
        ResearchExport.afterFileHook = { _, i -> if (i == 0 && ord < 60) poll(ord++) }
        val doc2 = Doc(); val u = run(doc2)
        assertEquals("REFUSED_SNAPSHOT_UNSTABLE", u.getString("status"))
        assertFalse(u.getBoolean("sink_written")); assertEquals(0, doc2.bytes().size)
        assertEquals(3, u.getJSONArray("snapshot_failures").length())
        // the export itself performed no write in the research dir: with no capture, two exports leave it identical
        ResearchExport.afterFileHook = null
        val t0 = tree(dir); run(Doc()); run(Doc()); assertEquals(t0, tree(dir))
    }

    // ------------------------------------------------------------- bounded work
    @Test fun export_work_is_bounded_and_refuses_oversize() {
        for (o in 1..4) poll(o)
        assertEquals("REFUSED_TOO_LARGE", run(Doc(), ResearchExport.Limits(maxTotalBytes = 1000)).getString("status"))
        assertEquals("REFUSED_TOO_MANY_FILES", run(Doc(), ResearchExport.Limits(maxFiles = 3)).getString("status"))
        val r = run(Doc())
        assertTrue(r.getLong("peak_temp_bytes") > 0)
        assertTrue(r.getLong("peak_temp_bytes") <= r.getLong("source_bytes") + 64 * 1024)
    }

    // ------------------------------------------------------------- CI-4 capacity status
    @Test fun capacity_status_uses_accepted_formula_and_is_read_only() {
        for (o in 1..5) poll(o)
        val before = tree(dir)
        val c = ResearchCapacity.read(dir, ms(S1, 16, 0))
        assertEquals(before, tree(dir))
        assertTrue(c.getBoolean("known"))
        val cnt = c.getJSONObject("counts")
        val active = cnt.getInt("STAGED") + cnt.getInt("LAG_EXCEEDED") + cnt.getInt("ARCHIVE_ACKED")
        assertEquals(active, c.getInt("active")); assertTrue(active > 0)
        val ledger = File(dir, "protection.json").length()
        assertEquals(ledger, c.getLong("ledger_bytes"))
        val uc = active / 4000.0; val ub = (ledger + 448.0 * active + 128) / (2_500_000 - 768)
        assertEquals(uc, c.getDouble("u_count"), 1e-15); assertEquals(ub, c.getDouble("u_bytes"), 1e-15)
        assertEquals(maxOf(uc, ub), c.getDouble("u"), 1e-15)
        // Codex counterexample state as a formula check: 774 entries, 301,773 sealed bytes -> U = 0.2595409309739952
        val x = ResearchCapacity.util(JSONObject(), 774, 301_773L, 4000, 2_500_000L)
        assertEquals(0.2595409309739952, x.getDouble("u"), 1e-15)
        // also exposed on the existing status (no export needed)
        val st = ResearchCollector.status(dir, ms(S1, 16, 0))
        assertEquals(c.getInt("active"), st.getJSONObject("capacity").getInt("active"))
        assertTrue(st.getString("toggle_semantics").contains("not affected"))
    }

    @Test fun capacity_unknown_when_corrupt_or_malformed_never_zero() {
        poll(1)
        val f = File(dir, "protection.json")
        val good = f.readBytes()
        f.writeBytes(good.copyOf(good.size - 5))
        val before = tree(dir)
        val c = ResearchCapacity.read(dir, 1L)
        assertFalse(c.getBoolean("known")); assertEquals("LEDGER_CORRUPT", c.getString("integrity"))
        assertTrue(c.isNull("u")); assertTrue(c.isNull("active"))
        assertEquals(before, tree(dir))                               // no ledgerstatus.json written by the read
        f.delete()                                                    // established ledger lost
        assertEquals("established_ledger_lost", ResearchCapacity.read(dir, 1L).getString("reason"))
        M0Durable.write(f, JSONObject().put("entries", JSONObject().put("a", JSONObject().put("state", 7))).toString())
        assertEquals("MALFORMED", ResearchCapacity.read(dir, 1L).getString("integrity"))
        val fresh = Files.createTempDirectory("m0fresh").toFile()
        val e = ResearchCapacity.read(fresh, 1L)
        assertEquals("NO_EVIDENCE", e.getString("integrity")); assertEquals(0, e.getInt("active")); fresh.deleteRecursively()
    }

    // ------------------------------------------------------------- toggle readback + independence
    @Test fun toggle_reads_back_and_only_touches_meta() {
        poll(1)
        val before = tree(dir)
        ResearchCollector.setEnabled(dir, false, ms(S1, 10, 0))
        val after = tree(dir)
        assertEquals(before.keys, after.keys)
        assertEquals(setOf("meta.json"), before.keys.filter { before[it] != after[it] }.toSet())
        assertFalse(ResearchCollector.status(dir, ms(S1, 10, 0)).getBoolean("enabled"))
        // an export neither toggles nor depends on the toggle
        val r = run(Doc()); assertEquals("COMPLETE_VERIFIED", r.getString("status"))
        assertFalse(ResearchCollector.isEnabled(dir))
    }

    @Test fun bridge_and_facade_wiring_is_declared() {
        val root = generateSequence(File("").absoluteFile) { it.parentFile }.first { File(it, "app/src/main/java").isDirectory }
        val nb = File(root, "app/src/main/java/com/marketradar/app/NativeBridge.kt").readText()
        val ma = File(root, "app/src/main/java/com/marketradar/app/MainActivity.kt").readText()
        for (m in listOf("exportResearchEvidence", "getResearchExportResult", "cancelResearchExport")) {
            assertTrue(m, Regex("@JavascriptInterface\\s+fun $m\\(").containsMatchIn(nb))
            assertTrue(m, ma.contains("$m: function("))
        }
        assertTrue(ma.contains("ActivityResultContracts.CreateDocument(\"application/zip\")"))
        assertTrue(ma.contains("contentResolver.openInputStream(uri)"))   // readback through the granted access
        // the export path never calls collection/ack/release/reset APIs
        val ex = File(root, "app/src/main/java/com/marketradar/app/ResearchExport.kt").readText()
        for (bad in listOf("setEnabled(", "acknowledgeArchive(", "release(", "releaseSession(", "M0Durable.write(", "writeAtomic(", "ResearchProtection.load(", ".delete()"))
            assertFalse(bad, ex.substringAfter("object ResearchExport").replace("tmp?.delete()", "").replace("t.delete()", "").contains(bad))
    }

    private fun writeMetrics(name: String, r: JSONObject, docBytes: Int) {
        val out = File("build/m0_stage2_export_metrics.json")
        val o = if (out.exists()) JSONObject(out.readText()) else JSONObject()
        o.put(name, JSONObject().put("elapsed_ms", r.getLong("elapsed_ms")).put("source_files", r.getInt("source_files"))
            .put("source_bytes", r.getLong("source_bytes")).put("zip_bytes", docBytes).put("peak_temp_bytes", r.getLong("peak_temp_bytes")).put("heap_delta_bytes", r.opt("heap_delta_bytes")))
        out.parentFile?.mkdirs(); out.writeText(o.toString(2))
    }

    @Test fun measured_export_at_realistic_session_size() {
        for (o in 1..78) poll(o)          // one full regular session, one attempt per slot, both indices + envelopes
        val rt = Runtime.getRuntime(); System.gc(); val m0 = rt.totalMemory() - rt.freeMemory()
        val doc = Doc(); val r = run(doc)
        val m1 = rt.totalMemory() - rt.freeMemory()
        assertEquals("COMPLETE_VERIFIED", r.getString("status"))
        writeMetrics("full_session_78_polls", r.put("heap_delta_bytes", m1 - m0), doc.bytes().size)
        assertNotNull(r.getString("zip_sha256"))
    }
}
