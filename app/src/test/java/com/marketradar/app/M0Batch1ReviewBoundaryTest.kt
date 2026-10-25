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
 * Batch 1 review (B1-R1..R5) boundary regressions beyond the six Codex counterexamples. Every case drives the REAL
 * collector / ledger / map store / cohort hook code paths and inspects the bytes actually on disk.
 */
class M0Batch1ReviewBoundaryTest {
    private lateinit var dir: File
    private val day = "2026-10-23"
    private fun now() = LocalDateTime.parse("${day}T12:30:05").atZone(ResearchCalendar.IST).toInstant().toEpochMilli()
    private var maxBytes0 = 0; private var imap0 = 0L; private var imapFiles0 = 0; private var coh0 = 0L; private var cap0 = 0

    @Before fun setUp() {
        dir = Files.createTempDirectory("m0b1r").toFile()
        maxBytes0 = ResearchProtection.MAX_BYTES; imap0 = ResearchCollector.MAX_IMAP_BYTES; imapFiles0 = ResearchCollector.MAX_IMAP_FILES
        coh0 = ResearchCohorts.MAX_TOTAL_BYTES; cap0 = ResearchProtection.CAPACITY
        M0Durable.crashAt = null
    }
    @After fun tearDown() {
        ResearchProtection.MAX_BYTES = maxBytes0; ResearchCollector.MAX_IMAP_BYTES = imap0; ResearchCollector.MAX_IMAP_FILES = imapFiles0
        ResearchCohorts.MAX_TOTAL_BYTES = coh0; ResearchProtection.CAPACITY = cap0; M0Durable.crashAt = null
        dir.deleteRecursively()
    }

    private fun chain() = JSONObject().put("data", JSONArray().put(JSONObject()
        .put("strike_price", 22650).put("expiry", "2026-10-27")
        .put("call_options", JSONObject().put("instrument_key", "NSE_FO|12345")
            .put("market_data", JSONObject().put("bid_price", 10).put("ask_price", 11)))
        .put("put_options", JSONObject().put("instrument_key", "NSE_FO|12346")
            .put("market_data", JSONObject().put("bid_price", 12).put("ask_price", 13)))))
    private fun genuine() = listOf(
        JSONObject().put("expiry", "2026-10-27").put("strike", 22650).put("option_type", "CE").put("instrument_key", "NSE_FO|12345").put("bid", 10).put("ask", 11).put("ltp", JSONObject.NULL),
        JSONObject().put("expiry", "2026-10-27").put("strike", 22650).put("option_type", "PE").put("instrument_key", "NSE_FO|12346").put("bid", 12).put("ask", 13).put("ltp", JSONObject.NULL))

    private fun capture(poll: String, ord: Int = 40): String {
        val a = ResearchCollector.beginAttempt(dir, "NF", "$day|$ord", now()) as ResearchCollector.Begin.Started
        val c = ResearchCollector.captureFromChain(dir, "NF", chain(), now(), now(), ChainEvidenceRef.TABLE, now(), poll, now(), a.attemptId)
        assertEquals(ResearchCollector.RecordResult.RECORDED, ResearchCollector.recordCapture(dir, a.slotId, a.attemptId, c, now()))
        return ResearchCollector.chainProtectionId(a.slotId, a.attemptId)
    }
    private fun ack(id: String, rows: List<JSONObject>, poll: String = "p"): String {
        val bytes = JSONObject().put("index", "NF").put("poll_ts", poll).put("rows", JSONArray(rows)).toString().toByteArray(Charsets.UTF_8)
        val m = JSONObject().put("entry_id", id).put("index", "NF").put("poll_ts", poll).put("roles", JSONArray(listOf("CHAIN_POLL")))
            .put("row_count", rows.size).put("content_sha256", M0Durable.sha256(bytes)).put("content_bytes", bytes.size)
        return ResearchProtection.acknowledgeArchive(dir, id, m, bytes, now())
    }

    // ------------------------------------------------------------------ B1-R1
    @Test fun r1_every_altered_field_is_rejected_and_a_genuine_archive_succeeds() {
        ResearchCollector.setEnabled(dir, true, now())
        val id = capture("p")
        val alterations = mapOf<String, (JSONObject) -> Unit>(
            "instrument" to { it.put("instrument_key", "NSE_FO|99999") }, "strike" to { it.put("strike", 22700) },
            "expiry" to { it.put("expiry", "2026-11-03") }, "type" to { it.put("option_type", "PE") },
            "bid" to { it.put("bid", 10.05) }, "ask" to { it.put("ask", 11.5) }, "ltp" to { it.put("ltp", 10.5) })
        for ((name, alter) in alterations) {
            val rows = genuine().map { JSONObject(it.toString()) }; alter(rows[0])
            assertEquals("altered $name", "REJECTED:source_rows_mismatch", ack(id, rows))
            assertTrue(ResearchProtection.isProtected(dir, "NF", "p"))
        }
        // same-count non-canonical rows (Codex counterexample shape) are rejected too
        assertEquals("REJECTED:chain_rows_not_canonical", ResearchProtection.acknowledgeArchive(dir, id,
            JSONObject().put("entry_id", id).put("index", "NF").put("poll_ts", "p").put("roles", JSONArray(listOf("CHAIN_POLL"))).put("row_count", 2)
                .put("content_sha256", M0Durable.sha256("{\"index\":\"NF\",\"poll_ts\":\"p\",\"rows\":[0,1]}".toByteArray())).put("content_bytes", "{\"index\":\"NF\",\"poll_ts\":\"p\",\"rows\":[0,1]}".toByteArray().size),
            "{\"index\":\"NF\",\"poll_ts\":\"p\",\"rows\":[0,1]}".toByteArray(), now()))
        // a genuine archive in another row order / numeric spelling (10 vs 10.0, CALL vs CE) is the same evidence
        val g = genuine().reversed().map { JSONObject(it.toString()) }
        g[0].put("bid", 12.0); g[1].put("option_type", "CALL")
        assertEquals("ARCHIVE_ACKED", ack(id, g))
        assertFalse(ResearchProtection.isProtected(dir, "NF", "p"))
    }

    @Test fun r1_without_source_proof_the_entry_stays_protected_with_a_reason() {
        assertEquals("STAGED", ResearchProtection.request(dir, "x", "o", "NF", "p", day, listOf("CHAIN_POLL"), 1L, expectedRows = 2))
        assertEquals("REJECTED:source_proof_unavailable", ack("x", genuine()))
        assertTrue(ResearchProtection.isProtected(dir, "NF", "p"))
        assertEquals("source_proof_unavailable", ResearchProtection.entry(dir, "x")!!.getString("last_ack_error"))
    }

    @Test fun r1_capture_records_the_source_digest_in_the_manifest_and_ledger_only_once() {
        ResearchCollector.setEnabled(dir, true, now())
        val id = capture("p")
        val src = ResearchProtection.entry(dir, id)!!.getString("source_rows_sha256")
        val slot = ResearchCollector.readManifest(dir, day)!!.getJSONObject("indices").getJSONObject("NF").getJSONObject("slots").getJSONObject("40")
        val cap = slot.getJSONArray("attempts").getJSONObject(0).getJSONObject("capture")
        assertEquals(src, cap.getString("source_rows_sha256"))
        assertEquals(64, src.length)
        // single-source design: no chain copy is stored locally (no file holds the quotes)
        assertTrue(dir.walkTopDown().filter { it.isFile }.none { it.readText().contains("NSE_FO|12345") && !it.name.startsWith("imap") })
    }

    // ------------------------------------------------------------------ B1-R2
    @Test fun r2_each_attempt_has_its_own_entry_retries_are_idempotent_and_identity_conflicts_are_explicit() {
        ResearchCollector.setEnabled(dir, true, now())
        val a = ResearchCollector.beginAttempt(dir, "NF", "$day|40", now()) as ResearchCollector.Begin.Started
        val c = ResearchCollector.captureFromChain(dir, "NF", chain(), now(), now(), ChainEvidenceRef.TABLE, now(), "first", now(), a.attemptId)
        assertEquals(ResearchCollector.RecordResult.RECORDED, ResearchCollector.recordCapture(dir, a.slotId, a.attemptId, c, now()))
        // retry of the same attempt (process restart re-delivers) -> idempotent, no second entry
        assertEquals(ResearchCollector.RecordResult.IDEMPOTENT, ResearchCollector.recordCapture(dir, a.slotId, a.attemptId, c, now()))
        val id2 = capture("second")
        val id3 = capture("third")
        val counts = ResearchProtection.summary(dir, day).getJSONObject("counts")
        assertEquals(3, counts.getInt("STAGED"))
        assertEquals(setOf(ResearchCollector.chainProtectionId(a.slotId, a.attemptId), id2, id3).size, 3)
        for (p in listOf("first", "second", "third")) assertTrue(ResearchProtection.isProtected(dir, "NF", p))
        // same id with different contents is an IDENTITY_CONFLICT; the original identity is untouched
        val e0 = ResearchProtection.entry(dir, id2)!!.toString()
        assertEquals("IDENTITY_CONFLICT", ResearchProtection.request(dir, id2, "research_m0", "NF", "other-poll", day, listOf("CHAIN_POLL"), now(), expectedRows = 2))
        assertEquals("IDENTITY_CONFLICT", ResearchProtection.request(dir, id2, "research_m0", "NF", "second", day, listOf("CHAIN_POLL"), now(), expectedRows = 2, sourceSha = "0".repeat(64)))
        assertEquals(e0, ResearchProtection.entry(dir, id2)!!.toString())
        assertEquals(2, ResearchProtection.summary(dir, day).getInt("IDENTITY_CONFLICT"))
        // identical re-request (restart) returns the state without a new entry
        val x = ResearchProtection.entry(dir, id2)!!
        assertEquals("STAGED", ResearchProtection.request(dir, id2, x.getString("owner"), "NF", "second", day, listOf("CHAIN_POLL"), now() + 99,
            expectedRows = x.getInt("expected_rows"), sourceSha = x.getString("source_rows_sha256")))
        assertEquals(3, ResearchProtection.summary(dir, day).getJSONObject("counts").getInt("STAGED"))
    }

    // ------------------------------------------------------------------ B1-R3
    @Test fun r3_byte_full_ledger_refuses_before_commit_and_count_full_too() {
        ResearchCollector.setEnabled(dir, true, now())
        ResearchProtection.MAX_BYTES = 700
        val env = ResearchCollector.buildEnvelope("$day|40", null, now(), now(), "t", 1, JSONObject())
        assertEquals(ResearchCollector.EnvResult.CAPACITY_BACKLOG, ResearchCollector.putEnvelope(dir, env, now()))
        assertNull(ResearchCollector.readEnvelope(dir, "$day|40"))
        ResearchProtection.MAX_BYTES = maxBytes0; ResearchProtection.CAPACITY = 0
        assertEquals(ResearchCollector.EnvResult.CAPACITY_BACKLOG, ResearchCollector.putEnvelope(dir, env, now()))
        ResearchProtection.CAPACITY = cap0
        assertEquals(ResearchCollector.EnvResult.STORED, ResearchCollector.putEnvelope(dir, env, now()))
        assertEquals("STAGED", ResearchProtection.entry(dir, ResearchCollector.envelopeEntryId("$day|40"))!!.getString("state"))
    }

    @Test fun r3_orphan_envelope_is_flagged_while_unprotectable_then_repaired_by_retry_or_reconcile() {
        ResearchCollector.setEnabled(dir, true, now())
        val key = "$day|40"
        val env = ResearchCollector.buildEnvelope(key, null, now(), now(), "t", 1, JSONObject())
        val name = "env_" + key.replace(Regex("[^A-Za-z0-9_.-]"), "_").take(80) + ".json"
        M0Durable.write(File(dir, name), JSONObject().put("sha256", ResearchCollector.sha256(env.toString())).put("stored_ms", now()).put("envelope_text", env.toString()).toString())
        ResearchProtection.MAX_BYTES = 700
        assertEquals(ResearchCollector.EnvResult.STORED_UNPROTECTED, ResearchCollector.putEnvelope(dir, env, now()))
        assertTrue(ResearchCollector.unprotectedEnvelopes(dir).has(key))
        assertEquals(1, ResearchCollector.status(dir, now()).getInt("envelopes_unprotected"))
        assertNotNull(ResearchCollector.readEnvelope(dir, key))          // bytes preserved
        ResearchProtection.MAX_BYTES = maxBytes0
        val rec = ResearchCollector.reconcile(dir, now())
        assertEquals(1, rec.getInt("envelopes_protection_repaired"))
        assertEquals("STAGED", ResearchProtection.entry(dir, ResearchCollector.envelopeEntryId(key))!!.getString("state"))
        assertFalse(ResearchCollector.unprotectedEnvelopes(dir).has(key))
        assertEquals(ResearchCollector.EnvResult.IDEMPOTENT, ResearchCollector.putEnvelope(dir, env, now()))
    }

    private fun runChild(scenario: String, boundary: String, target: String): Int {
        val java = File(System.getProperty("java.home"), "bin/java").path
        val p = ProcessBuilder(java, "-cp", System.getProperty("java.class.path"), "com.marketradar.app.M0CrashChild", dir.path, scenario, boundary, target)
            .redirectErrorStream(true).start()
        val out = p.inputStream.bufferedReader().readText(); val code = p.waitFor()
        assertTrue("child output: $out", code == 77 || out.contains("NO_CRASH"))
        return code
    }

    @Test fun r3_real_child_kill_between_envelope_commit_and_protection_request_is_reconciled() {
        val key = "2026-10-23|40"
        val envName = "env_" + key.replace(Regex("[^A-Za-z0-9_.-]"), "_") + ".json"
        val cases = listOf("env_committed_before_protection" to envName) +
            M0Durable.BOUNDARIES.map { it to envName } + M0Durable.BOUNDARIES.map { it to "protection.json" }
        var killedAtBoundary = 0
        for ((b, target) in cases) {
            dir.deleteRecursively(); dir.mkdirs()
            val code = runChild("envelope", b, target)
            if (code == 77) killedAtBoundary++
            if (b == "env_committed_before_protection") {
                assertEquals(77, code)
                // durable state exactly as Codex described: bytes committed, no ledger entry yet
                assertTrue(File(dir, envName).exists())
                assertNull(ResearchProtection.entry(dir, ResearchCollector.envelopeEntryId(key)))
            }
            ResearchCollector.reconcile(dir, 1_792_000_500_000L)   // restart
            val stored = ResearchCollector.readEnvelope(dir, key) != null
            val entry = ResearchProtection.entry(dir, ResearchCollector.envelopeEntryId(key))
            if (stored) assertEquals("[$b/$target] committed envelope must be protected after restart", "STAGED", entry?.getString("state"))
            // the producer's retry completes the admission idempotently
            val env = ResearchCollector.buildEnvelope(key, null, 1L, 2L, "crash", 1, JSONObject())
            val r = ResearchCollector.putEnvelope(dir, env, 4L)
            assertTrue("[$b/$target] $r", r == ResearchCollector.EnvResult.IDEMPOTENT || r == ResearchCollector.EnvResult.STORED)
            assertEquals("STAGED", ResearchProtection.entry(dir, ResearchCollector.envelopeEntryId(key))!!.getString("state"))
        }
        assertTrue(killedAtBoundary >= 6)
    }

    // ------------------------------------------------------------------ B1-R4
    @Test fun r4_corrupt_index_fails_closed_and_deletes_nothing() {
        ResearchCollector.setEnabled(dir, true, now())
        capture("p")
        val before = dir.listFiles()!!.filter { it.name.startsWith("imap_") }.map { it.name }.toSet()
        File(dir, "imapindex.json").writeText("{garbage")
        ResearchCollector.MAX_IMAP_FILES = 1
        val r = ResearchCollector.putInstrumentMap(dir, "NF", listOf("NF|2026-10-27|1|CE|Z"), now(), null)
        assertEquals(ResearchCollector.MapRef.Unavailable("IMAP_INDEX_CORRUPT"), r)
        assertEquals(before, dir.listFiles()!!.filter { it.name.startsWith("imap_") }.map { it.name }.toSet())
    }

    @Test fun r4_corrupt_manifest_under_cap_pressure_prunes_nothing() {
        ResearchCollector.setEnabled(dir, true, now())
        ResearchCollector.MAX_IMAP_FILES = 2
        val m1 = ResearchCollector.putInstrumentMap(dir, "BNF", listOf("BNF|e|1|CE|A"), 1L, null) as ResearchCollector.MapRef.Ok
        capture("p")   // creates the session manifest and an NF map referenced by it
        val mf = dir.listFiles()!!.first { it.name.startsWith("manifest_") }
        mf.writeText("{corrupt")
        val r = ResearchCollector.putInstrumentMap(dir, "BNF", listOf("BNF|e|1|CE|B"), 2L, null)
        assertEquals(ResearchCollector.MapRef.Unavailable("IMAP_CAPACITY_REFERENCES_UNKNOWN"), r)
        assertNotNull(ResearchCollector.readInstrumentMap(dir, m1.version))   // even the unreferenced map is kept
        assertEquals(2, dir.listFiles()!!.count { it.name.startsWith("imap_") })
    }

    @Test fun r4_index_loss_after_restart_is_rebuilt_by_validation_and_referenced_maps_survive() {
        ResearchCollector.setEnabled(dir, true, now())
        val id = capture("p")
        val v = ResearchCollector.readManifest(dir, day)!!.getJSONObject("indices").getJSONObject("NF").getJSONObject("slots")
            .getJSONObject("40").getJSONArray("attempts").getJSONObject(0).getJSONObject("capture").getString("imap_version")
        assertTrue(File(dir, "imap.established").exists())
        assertTrue(File(dir, "imapindex.json").delete())
        ResearchCollector.reconcile(dir, now() + 1000)   // restart
        ResearchCollector.MAX_IMAP_FILES = 1             // cap pressure: only one map may remain
        val r = ResearchCollector.putInstrumentMap(dir, "NF", listOf("NF|2026-10-27|22700|CE|NSE_FO|1"), now(), null)
        assertTrue("$r", r is ResearchCollector.MapRef.Unavailable)   // referenced map cannot be pruned -> capacity, visible
        assertNotNull(ResearchCollector.readInstrumentMap(dir, v))
        assertTrue(InstrumentMaps.admitted(dir).contains(v))
        assertTrue(ResearchProtection.isProtected(dir, "NF", "p")); assertNotNull(id)
        // with both the index AND marker gone but map files present, it is still not treated as first use
        File(dir, "imapindex.json").delete(); File(dir, "imap.established").delete()
        ResearchCollector.MAX_IMAP_FILES = imapFiles0
        assertTrue(ResearchCollector.putInstrumentMap(dir, "NF", listOf("NF|2026-10-27|22750|CE|NSE_FO|2"), now(), null) is ResearchCollector.MapRef.Ok)
        assertNotNull(ResearchCollector.readInstrumentMap(dir, v))
    }

    // ------------------------------------------------------------------ B1-R5
    private val nasty = "q\"uote\\back\\\\slash ₹ नमस्ते 漢字 \u0001 /</"

    @Test fun r5_map_cap_is_exact_sealed_utf8_bytes_including_escapes_and_multibyte() {
        val lines = (1..40).map { "NF|2026-10-27|${22000 + it}|CE|$nasty$it" }
        val text = lines.joinToString("\n")
        val v = M0Durable.sha256(text)
        val probe = File(Files.createTempDirectory("probe").toFile(), "x")
        ResearchCollector.MAX_IMAP_BYTES = 10_000_000
        val ok = ResearchCollector.putInstrumentMap(probe.parentFile, "NF", lines, 7L, null) as ResearchCollector.MapRef.Ok
        val exact = File(probe.parentFile, "imap_${ok.version}.json").length()
        assertTrue("utf-8 + escapes exceed char count", exact > text.length + 256)
        ResearchCollector.MAX_IMAP_BYTES = exact - 1
        assertTrue(ResearchCollector.putInstrumentMap(dir, "NF", lines, 7L, null) is ResearchCollector.MapRef.Unavailable)
        assertFalse(File(dir, "imap_$v.json").exists())
        ResearchCollector.MAX_IMAP_BYTES = exact
        assertTrue(ResearchCollector.putInstrumentMap(dir, "NF", lines, 7L, null) is ResearchCollector.MapRef.Ok)
        assertEquals(exact, File(dir, "imap_$v.json").length())
        // repeated admission of the same map is idempotent and adds no bytes
        repeat(5) { assertTrue(ResearchCollector.putInstrumentMap(dir, "NF", lines, 8L + it, "l$it") is ResearchCollector.MapRef.Ok) }
        assertTrue(dir.listFiles()!!.filter { it.name.startsWith("imap_") }.sumOf { it.length() } <= ResearchCollector.MAX_IMAP_BYTES)
        probe.parentFile.deleteRecursively()
    }

    @Test fun r5_ledger_on_disk_bytes_never_exceed_max_with_escapes_multibyte_metadata_and_repeated_admission() {
        ResearchProtection.MAX_BYTES = 8_000
        var staged = 0; var rejected = 0; var maxDisk = 0L
        val f = File(dir, "protection.json")
        for (i in 0 until 60) {
            val id = "$nasty#$i"
            val r = ResearchProtection.request(dir, id, "own$nasty", "NF", "poll$nasty$i", day, listOf("CHAIN_POLL"), i.toLong(), expectedRows = 2, sourceSha = "a".repeat(64))
            if (r == "STAGED") staged++ else { assertEquals("CAPACITY_REJECTED", r); rejected++ }
            // repeated admission of the same id: no growth beyond the identical entry
            ResearchProtection.request(dir, id, "own$nasty", "NF", "poll$nasty$i", day, listOf("CHAIN_POLL"), i.toLong() + 1, expectedRows = 2, sourceSha = "a".repeat(64))
            maxDisk = maxOf(maxDisk, f.length())
            // ack failures and lag bookkeeping on every protected entry stay within the reserve
            if (r == "STAGED") {
                ResearchProtection.acknowledgeArchive(dir, id, JSONObject().put("entry_id", "wrong"), "x".toByteArray(), i.toLong())
                maxDisk = maxOf(maxDisk, f.length())
            }
        }
        ResearchProtection.enforceLag(dir, "2027-06-30")
        maxDisk = maxOf(maxDisk, f.length())
        assertTrue(staged > 0); assertTrue(rejected > 0)
        assertTrue("disk=$maxDisk max=${ResearchProtection.MAX_BYTES}", maxDisk <= ResearchProtection.MAX_BYTES)
        assertEquals(f.length(), ResearchProtection.summary(dir, day).getLong("ledger_bytes"))
    }

    @Test fun r5_cohort_hook_accounts_exact_sealed_bytes_and_temp_reserve() {
        val c = ResearchCohorts.Contract("NF", "2026-10-27", 22650, "CE")
        fun items(n: Int) = ResearchCohorts.REQUIRED.map { k ->
            ResearchCohorts.Item(k, if (k in setOf("ENTRY_MARK", "EXIT_MARK", "ENTRY_QUOTE")) c else null,
                if (k == "VERSIONS") JSONObject().put("calendar_version", "c").put("lot_version", "l").put("fee_version", "f").put("rate_version", "r").toString()
                else nasty.repeat(n), "src:$nasty") }
        ResearchCohorts.MAX_TOTAL_BYTES = 20_000
        var frozen = 0; var rejected = 0
        for (i in 0 until 30) {
            when (ResearchCohorts.freeze(dir, "coh$i$nasty", items(8), i.toLong())) { "FROZEN" -> frozen++; "REJECTED_CAPACITY" -> rejected++; else -> fail() }
            val disk = File(dir, ResearchCohorts.SUBDIR).walkTopDown().filter { it.isFile }.sumOf { it.length() }
            assertTrue("disk=$disk", disk <= ResearchCohorts.MAX_TOTAL_BYTES)
        }
        assertTrue(frozen > 0); assertTrue(rejected > 0)
        assertEquals("REPLAYABLE", ResearchCohorts.replayable(dir, "coh0$nasty").getString("status"))
    }
}
