package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.util.concurrent.TimeUnit

/** CDX-S2-BUILD-R1 corrections: R2 status vocabulary/shapes, R3 bounded export resources, R4 protocol binding. */
class M0Stage2R1Test {
    private val root = generateSequence(File("").absoluteFile) { it.parentFile }.first { File(it, "app/src/main/java").isDirectory }

    private fun ledger(dir: File, o: JSONObject) = M0Durable.write(File(dir, "protection.json"), o.toString())
    private fun withDir(block: (File) -> Unit) { val d = Files.createTempDirectory("r1").toFile(); try { block(d) } finally { d.deleteRecursively() } }
    private fun entries(vararg st: Any?) = JSONObject().also { e -> st.forEachIndexed { i, s -> e.put("e$i", JSONObject().put("state", s ?: JSONObject.NULL)) } }

    // ---------------------------------------------------------------- R2
    @Test fun r2_unsupported_or_malformed_shapes_are_unknown_with_no_U() = withDir { d ->
        val cases = listOf(
            JSONObject().put("entries", entries("STAGED", "FUTURE_STATE")) to "entry_state_unsupported",
            JSONObject().put("entries", entries("staged")) to "entry_state_unsupported",
            JSONObject().put("entries", JSONObject().put("x", "STAGED")) to "entry_not_object",
            JSONObject().put("entries", JSONObject().put("x", JSONObject())) to "entry_state_not_string",
            JSONObject().put("entries", org.json.JSONArray()) to "entries_not_object",
            JSONObject().put("entries", entries("STAGED")).put("rejected_total", "0") to "rejected_total",
            JSONObject().put("entries", entries("STAGED")).put("rejected_total", -1) to "rejected_total",
            JSONObject().put("entries", entries("STAGED")).put("rejected_total", 1.5) to "rejected_total",
            JSONObject().put("entries", entries("STAGED")).put("identity_conflicts_total", JSONObject()) to "identity_conflicts_total",
            JSONObject().put("entries", entries("STAGED")).put("compacted", "x") to "compacted_not_object",
            JSONObject().put("entries", entries("STAGED")).put("compacted", JSONObject().put("total", "3")) to "compacted_total")
        for ((o, why) in cases) {
            ledger(d, o)
            val before = d.walkTopDown().filter { it.isFile }.associate { it.name to it.readBytes().toList() }
            val c = ResearchCapacity.read(d, 1L)
            assertFalse(o.toString(), c.getBoolean("known"))
            assertEquals(o.toString(), "MALFORMED", c.getString("integrity"))
            assertTrue(c.getString("reason"), c.getString("reason").contains(why))
            assertTrue(c.isNull("active")); assertTrue(c.isNull("u")); assertTrue(c.isNull("u_count")); assertTrue(c.isNull("u_bytes"))
            assertEquals(before, d.walkTopDown().filter { it.isFile }.associate { it.name to it.readBytes().toList() })  // read-only
        }
    }

    @Test fun r2_known_vocabulary_still_reads_and_first_use_is_no_evidence() = withDir { d ->
        assertEquals("NO_EVIDENCE", ResearchCapacity.read(d, 1L).getString("integrity"))
        assertTrue(ResearchCapacity.read(d, 1L).getBoolean("known"))
        ledger(d, JSONObject().put("entries", entries("STAGED", "LAG_EXCEEDED", "ARCHIVE_ACKED", "RELEASED")).put("rejected_total", 0)
            .put("compacted", JSONObject().put("total", 2).put("by_state", JSONObject()).put("chain", "")))
        val c = ResearchCapacity.read(d, 1L)
        assertTrue(c.getBoolean("known")); assertEquals(3, c.getInt("active")); assertEquals(2, c.getLong("compacted_total"))
        assertEquals("INCOMPLETE_REPLAY", c.getString("integrity"))
    }

    // ---------------------------------------------------------------- R4
    @Test fun r4_protocol_reference_is_bound_to_the_immutable_protocol_bytes() {
        val f = File(root, "tools/research_m0_freeze/freeze_inputs/" + ResearchExport.PROTOCOL_FILE)
        assertEquals(ResearchExport.PROTOCOL_SHA256, M0Durable.sha256(f.readBytes()))
        val rec = JSONObject(File(root, "tools/research_m0_freeze/freeze_inputs/SPECIAL_SESSION_DECISION_RECORD_APPROVED_20261023.json").readText())
        assertEquals(ResearchExport.PROTOCOL_SHA256, rec.getString("protocol_sha256"))
        assertEquals(ResearchExport.PROTOCOL_NAME, rec.getString("protocol_version"))
        withDir { d ->
            val t = Files.createTempDirectory("r1t").toFile()
            val out = java.io.ByteArrayOutputStream()
            val r = ResearchExport.export(d, t, JSONObject(), 1L, { out }, { java.io.ByteArrayInputStream(out.toByteArray()) },
                { false }, freeSpace = { Long.MAX_VALUE / 4 })
            assertEquals("COMPLETE_VERIFIED", r.getString("status"))
            java.util.zip.ZipInputStream(java.io.ByteArrayInputStream(out.toByteArray())).use { z ->
                while (true) { val e = z.nextEntry ?: break
                    if (e.name == ResearchExport.MANIFEST) {
                        val p = JSONObject(String(z.readBytes())).getJSONObject("protocol_ref")
                        assertEquals(ResearchExport.PROTOCOL_SHA256, p.getString("sha256")); assertEquals(ResearchExport.PROTOCOL_FILE, p.getString("file"))
                    } }
            }
            t.deleteRecursively()
        }
    }

    // ---------------------------------------------------------------- R3
    @Test fun r3_up_front_memory_refusal_is_explicit_and_leaves_evidence() = withDir { d ->
        for (i in 0 until 50) M0Durable.write(File(d, "env_x_$i.json"), JSONObject().put("i", i).toString())
        val before = d.walkTopDown().filter { it.isFile }.associate { it.name to it.readBytes().toList() }
        val t = Files.createTempDirectory("r1t").toFile()
        val tiny = ResearchExport.Limits(maxHeapBytes = 8L * 1024 * 1024)
        val r = ResearchExport.export(d, t, JSONObject(), 1L, { error("sink must not open") }, null, { false }, tiny, freeSpace = { Long.MAX_VALUE / 4 })
        assertEquals("REFUSED_INSUFFICIENT_MEMORY", r.getString("status"))
        assertFalse(r.getBoolean("sink_written"))
        assertTrue(r.getJSONObject("memory").getLong("estimated_peak_bytes") > r.getJSONObject("memory").getLong("budget_bytes"))
        assertEquals(before, d.walkTopDown().filter { it.isFile }.associate { it.name to it.readBytes().toList() })
        assertTrue(t.listFiles()!!.isEmpty()); t.deleteRecursively()
        // the per-file validation cap scales with the heap; files above it are exported but labelled, never parsed
        assertEquals(2L * 1024 * 1024, ResearchExport.validateCap(ResearchExport.Limits(maxHeapBytes = 64L * 1024 * 1024)))
    }

    @Test fun r3_export_source_retains_no_parsed_bodies() {
        val src = File(root, "app/src/main/java/com/marketradar/app/ResearchExport.kt").readText()
        assertFalse(src.contains("sealedBodies"))
        assertFalse(Regex("HashMap<String, JSONObject>|Map<String, JSONObject>").containsMatchIn(src.substringAfter("object ResearchExport")))
    }

    private fun child(cls: String, xmx: String): Pair<Int, String> {
        val java = File(System.getProperty("java.home"), "bin/java").path
        val p = ProcessBuilder(java, "-Xmx$xmx", "-cp", System.getProperty("java.class.path"), cls).redirectErrorStream(true).start()
        val out = p.inputStream.bufferedReader().readText()
        assertTrue("child timeout", p.waitFor(10, TimeUnit.MINUTES))
        return p.exitValue() to out
    }

    private fun record(name: String, text: String) {
        val f = File(root, "app/build/m0_stage2_resource_probe.txt"); f.parentFile.mkdirs(); f.appendText("== $name\n$text\n")
    }

    /** Codex's Stage2ExportMemoryProbe, unmodified, in a child JVM at -Xmx64m: no heap exhaustion. */
    @Test fun r3_codex_memory_probe_in_child_jvm_64m_has_no_oom() {
        val (rc, out) = child("com.marketradar.app.Stage2ExportMemoryProbe", "64m")
        record("Stage2ExportMemoryProbe -Xmx64m exit=$rc", out)
        assertEquals(out, 0, rc)
        assertFalse(out, out.contains("OutOfMemoryError"))
        val res = JSONObject(out.lines().last { it.startsWith("{") })
        // readback is null in the probe -> WRITTEN_READBACK_FAILED(READBACK_UNAVAILABLE) is the expected terminal state;
        // the snapshot itself must complete (zip produced) or be an explicit documented refusal
        assertTrue(res.toString(), res.getString("status") in setOf("WRITTEN_READBACK_FAILED", "COMPLETE_VERIFIED", "REFUSED_INSUFFICIENT_MEMORY"))
        if (res.getString("status") == "WRITTEN_READBACK_FAILED") assertEquals("READBACK_UNAVAILABLE", res.getJSONObject("readback").getString("reason"))
    }

    /** Large admitted folder (~213 MB / 5,307 files): explicit refusal at -Xmx64m (estimate > budget); at -Xmx96m and
     *  -Xmx256m it exports and reads back with no heap exhaustion (96m: within a heap the estimate admits). */
    @Test fun r3_large_admitted_folder_measured_in_child_jvms() {
        for (xmx in listOf("64m", "96m", "256m")) {
            val (rc, out) = child("com.marketradar.app.Stage2ExportLargeFolderProbe", xmx)
            record("Stage2ExportLargeFolderProbe -Xmx$xmx exit=$rc", out)
            assertEquals(out, 0, rc); assertFalse(out, out.contains("OutOfMemoryError"))
            val j = JSONObject(out.lines().last { it.startsWith("LARGE_PROBE ") }.removePrefix("LARGE_PROBE "))
            assertTrue(j.toString(), j.getString("status") in setOf("COMPLETE_VERIFIED", "REFUSED_INSUFFICIENT_MEMORY"))
            if (xmx == "64m") assertEquals(j.toString(), "REFUSED_INSUFFICIENT_MEMORY", j.getString("status"))
            if (xmx != "64m") { assertEquals(j.toString(), "COMPLETE_VERIFIED", j.getString("status")); assertTrue(j.getBoolean("readback_ok")) }
            assertTrue(j.getLong("peak_heap_used_bytes_sampled_2ms") < j.getLong("max_heap"))
        }
    }
}
