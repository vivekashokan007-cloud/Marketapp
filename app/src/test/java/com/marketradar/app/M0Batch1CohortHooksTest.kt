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

/** M0 Batch 1 W8 (COH-01 hooks): preservation by copy survives raw pruning; slot counts never certify replay. */
class M0Batch1CohortHooksTest {
    private lateinit var dir: File
    private val S1 = "2026-10-21"; private val S2 = "2026-10-22"
    private fun ms(d: String, h: Int, m: Int) = LocalDateTime.parse("${d}T%02d:%02d:00".format(h, m)).atZone(ResearchCalendar.IST).toInstant().toEpochMilli()
    private var man0 = 0; private var env0 = 0
    @Before fun setUp() { dir = Files.createTempDirectory("m0b1w8").toFile(); man0 = ResearchCollector.MAX_MANIFEST_SESSIONS; env0 = ResearchCollector.MAX_ENVELOPE_FILES }
    @After fun tearDown() { dir.deleteRecursively(); ResearchCollector.MAX_MANIFEST_SESSIONS = man0; ResearchCollector.MAX_ENVELOPE_FILES = env0 }

    private val k = ResearchCohorts.Contract("NF", "2026-10-27", 24300, "CE")
    private fun items(env: String, manifest: String, exit: ResearchCohorts.Contract = k) = listOf(
        ResearchCohorts.Item("ENTRY_MARK", k, """{"bid":101.0,"ask":101.5}""", "poll:$S1|40"),
        ResearchCohorts.Item("ENTRY_QUOTE", k, """{"bid":101.0,"ask":101.5,"ts":"$S1 12:30:05"}""", "poll:$S1|40"),
        ResearchCohorts.Item("EXIT_MARK", exit, """{"bid":96.0,"ask":96.6}""", "poll:$S2|71"),
        ResearchCohorts.Item("UNDERLYING_REF", null, """{"spot":24310.5}""", "poll:$S1|40"),
        ResearchCohorts.Item("CHAIN_AT_OT", null, manifest, "manifest:$S1"),
        ResearchCohorts.Item("FORECAST_ENVELOPE", null, env, "env:$S1|40"),
        ResearchCohorts.Item("VERSIONS", null, JSONObject().put("calendar_version", ResearchCalendar.VERSION).put("lot_version", "lot_2026_v1")
            .put("fee_version", "fee_v3").put("rate_version", "rate_2026_10").toString(), "versions"))

    @Test fun pruning_raw_data_never_removes_cohort_copies() {
        ResearchCollector.setEnabled(dir, true, ms(S1, 9, 0))
        ResearchCollector.beginAttempt(dir, "NF", "$S1|40", ms(S1, 12, 30))
        assertEquals(ResearchCollector.EnvResult.STORED, ResearchCollector.putEnvelope(dir,
            ResearchCollector.buildEnvelope("$S1|40", null, 1L, 2L, "v", 1, JSONObject()), 3L))
        val envText = String(ResearchCollector.envelopeArchiveBytes(dir, "$S1|40")!!)
        val (manBytes, manSha) = ResearchCollector.manifestArchive(dir, S1)!!
        assertEquals("FROZEN", ResearchCohorts.freeze(dir, "c1", items(envText, String(manBytes)), 10L))
        assertEquals("REPLAYABLE", ResearchCohorts.replayable(dir, "c1").getString("status"))

        // archive-ack + guarded pruning removes the raw manifest and envelope
        ResearchCollector.reconcile(dir, ms(S2, 9, 0))
        val mid = ResearchProtection.sessionManifestId(S1)
        val cur = ResearchCollector.manifestArchive(dir, S1)!!
        assertEquals("ARCHIVE_ACKED", ResearchProtection.acknowledgeArchive(dir, mid, JSONObject().put("entry_id", mid).put("index", "SESSION")
            .put("poll_ts", S1).put("roles", JSONArray(listOf("SESSION_MANIFEST"))).put("content_sha256", cur.second).put("content_bytes", cur.first.size), cur.first, 1L, cur.second))
        val eid = ResearchCollector.envelopeEntryId("$S1|40")
        val eb = ResearchCollector.envelopeArchiveBytes(dir, "$S1|40")!!
        assertEquals("ARCHIVE_ACKED", ResearchProtection.acknowledgeArchive(dir, eid, JSONObject().put("entry_id", eid).put("index", "COMBINED")
            .put("poll_ts", "$S1|40").put("roles", JSONArray(listOf("FORECAST_ENVELOPE"))).put("content_sha256", M0Durable.sha256(eb)).put("content_bytes", eb.size), eb, 1L))
        ResearchCollector.MAX_MANIFEST_SESSIONS = 1; ResearchCollector.MAX_ENVELOPE_FILES = 1
        assertTrue(ResearchCollector.reclaimManifests(dir, 1))
        assertTrue(ResearchCollector.reclaimEnvelopes(dir, 0))
        assertNull(ResearchCollector.readManifest(dir, S1)); assertNull(ResearchCollector.readEnvelope(dir, "$S1|40"))
        // ... the cohort copy is untouched and still replayable, byte for byte
        assertEquals("REPLAYABLE", ResearchCohorts.replayable(dir, "c1").getString("status"))
        assertTrue(File(dir, "cohorts/c1/index.json").exists())
        assertNotEquals(manSha, "")
    }

    @Test fun a_passed_slot_count_never_certifies_replayability() {
        ResearchCollector.setEnabled(dir, true, ms(S1, 9, 0))
        // every slot captured-looking manifest exists, but no cohort copy -> NOT_REPLAYABLE
        ResearchCollector.ensureManifest(dir, S1, 1L)
        assertEquals("NO_COHORT_COPY", ResearchCohorts.replayable(dir, "c-none").getString("reason"))
        // missing exit mark / version fields / a different contract for the exit -> NOT_REPLAYABLE
        assertEquals("INCOMPLETE", ResearchCohorts.freeze(dir, "c2", items("{}", "{}").filter { it.kind != "EXIT_MARK" }, 1L))
        assertEquals("NOT_REPLAYABLE", ResearchCohorts.replayable(dir, "c2").getString("status"))
        assertEquals("INCOMPLETE", ResearchCohorts.freeze(dir, "c3", items("{}", "{}", exit = k.copy(strike = 24400)), 1L))
        assertTrue(ResearchCohorts.replayable(dir, "c3").getJSONObject("check").getJSONArray("problems").toString().contains("CONTRACT_MISMATCH:EXIT_MARK"))
        val noFee = items("{}", "{}").map { if (it.kind == "VERSIONS") it.copy(content = """{"calendar_version":"x","lot_version":"y","rate_version":"z"}""") else it }
        assertEquals("FROZEN", ResearchCohorts.freeze(dir, "c4", noFee, 1L))
        assertEquals("NOT_REPLAYABLE", ResearchCohorts.replayable(dir, "c4").getString("status"))
        // tampering a copy is detected
        assertEquals("FROZEN", ResearchCohorts.freeze(dir, "c5", items("{}", "{}"), 1L))
        val f = File(dir, "cohorts/c5/item_2.json"); val o = JSONObject(f.readText())
        o.put("body", JSONObject(o.getString("body")).put("content", """{"bid":1}""").toString()); o.put("sha256", M0Durable.sha256(o.getString("body"))); f.writeText(o.toString())
        assertEquals("NOT_REPLAYABLE", ResearchCohorts.replayable(dir, "c5").getString("status"))
        // bounded: no eviction at the cap
        val max0 = ResearchCohorts.MAX_COHORTS; ResearchCohorts.MAX_COHORTS = 4
        try { assertEquals("REJECTED_CAPACITY", ResearchCohorts.freeze(dir, "c6", items("{}", "{}"), 1L)) } finally { ResearchCohorts.MAX_COHORTS = max0 }
        assertEquals("EXISTS", ResearchCohorts.freeze(dir, "c5", items("{}", "{}"), 2L))
    }
}
