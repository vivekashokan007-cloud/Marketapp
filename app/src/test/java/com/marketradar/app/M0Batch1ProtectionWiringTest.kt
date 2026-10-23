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
 * M0 Batch 1 W4 (Codex R3): the REAL collector drives the protection ledger end to end:
 * capture -> restart -> archive/upload failure -> cap pressure -> acknowledgement -> deletion.
 */
class M0Batch1ProtectionWiringTest {
    private lateinit var dir: File
    private val S1 = "2026-10-21"; private val S2 = "2026-10-22"; private val S3 = "2026-10-23"
    private fun ms(d: String, h: Int, m: Int) = LocalDateTime.parse("${d}T%02d:%02d:00".format(h, m)).atZone(ResearchCalendar.IST).toInstant().toEpochMilli()
    private var cap0 = 0; private var man0 = 0; private var env0 = 0

    @Before fun setUp() { dir = Files.createTempDirectory("m0b1w4").toFile(); cap0 = ResearchProtection.CAPACITY; man0 = ResearchCollector.MAX_MANIFEST_SESSIONS; env0 = ResearchCollector.MAX_ENVELOPE_FILES }
    @After fun tearDown() { dir.deleteRecursively(); ResearchProtection.CAPACITY = cap0; ResearchCollector.MAX_MANIFEST_SESSIONS = man0; ResearchCollector.MAX_ENVELOPE_FILES = env0 }

    private fun chain() = JSONObject().put("data", JSONArray().apply {
        for (k in listOf(100, 200)) put(JSONObject().put("strike_price", k).put("expiry", "2026-10-27")
            .put("call_options", JSONObject().put("instrument_key", "NSE_FO|N${k}CE").put("market_data", JSONObject().put("bid_price", 1.0).put("ask_price", 1.1)))
            .put("put_options", JSONObject().put("instrument_key", "NSE_FO|N${k}PE").put("market_data", JSONObject().put("bid_price", 1.0).put("ask_price", 1.1))))
    })

    private fun capture(day: String, ord: Int): String {
        val t = ms(day, 12, 30) + 5000
        val b = ResearchCollector.beginAttempt(dir, "NF", "$day|$ord", t) as ResearchCollector.Begin.Started
        val c = ResearchCollector.captureFromChain(dir, "NF", chain(), t, t, "ml_option_chain_snapshots", t, "poll-$day-$ord", t, b.attemptId)
        assertEquals(ResearchCollector.RecordResult.RECORDED, ResearchCollector.recordCapture(dir, b.slotId, b.attemptId, c, t + 1000))
        return ResearchCollector.chainProtectionId(b.slotId, b.attemptId)   // B1-R2: per-attempt protection id
    }

    /** B1-R1: the archive carries the captured rows in the canonical schema (here: the genuine rows of chain()). */
    private fun genuineRows(): List<JSONObject> = listOf(100, 200).flatMap { k -> listOf("CE", "PE").map { t ->
        JSONObject().put("expiry", "2026-10-27").put("strike", k).put("option_type", t).put("instrument_key", "NSE_FO|N$k$t")
            .put("bid", 1.0).put("ask", 1.1).put("ltp", JSONObject.NULL) } }

    private fun chainAck(id: String, poll: String, rows: Int): Pair<JSONObject, ByteArray> {
        val content = JSONObject().put("index", "NF").put("poll_ts", poll).put("rows", JSONArray(genuineRows().take(rows))).toString().toByteArray()
        return JSONObject().put("entry_id", id).put("index", "NF").put("poll_ts", poll).put("roles", JSONArray(listOf("CHAIN_POLL")))
            .put("content_sha256", M0Durable.sha256(content)).put("content_bytes", content.size).put("row_count", rows) to content
    }

    @Test fun capture_restart_failure_cap_ack_deletion_lifecycle() {
        ResearchCollector.setEnabled(dir, true, ms(S1, 9, 0))
        // 1. capture -> protection requested by the collector itself
        val slot = capture(S1, 40)
        assertEquals("STAGED", ResearchProtection.entry(dir, slot)!!.getString("state"))
        assertEquals(4, ResearchProtection.entry(dir, slot)!!.getInt("expected_rows"))
        assertTrue(ResearchProtection.isProtected(dir, "NF", "poll-$S1-40"))
        assertEquals("STAGED", ResearchCollector.readManifest(dir, S1)!!.getJSONObject("indices").getJSONObject("NF")
            .getJSONObject("slots").getJSONObject("40").getString("protection"))

        // 2. restart (all state is on disk) + reconcile on the next day protects the past session manifest
        ResearchCollector.reconcile(dir, ms(S2, 9, 0))
        val manId = ResearchProtection.sessionManifestId(S1)
        assertEquals("STAGED", ResearchProtection.entry(dir, manId)!!.getString("state"))

        // 3. archive/upload failure: arbitrary bytes, wrong rows, stale manifest are all rejected; nothing deleted
        val junk = "x".toByteArray()
        val bad = JSONObject().put("entry_id", slot).put("index", "NF").put("poll_ts", "poll-$S1-40").put("roles", JSONArray(listOf("CHAIN_POLL")))
            .put("content_sha256", M0Durable.sha256(junk)).put("content_bytes", 1)
        assertTrue(ResearchProtection.acknowledgeArchive(dir, slot, bad, junk, 1L).startsWith("REJECTED:"))
        val (wm, wc) = chainAck(slot, "poll-$S1-40", 3)
        assertEquals("REJECTED:row_count_mismatch", ResearchProtection.acknowledgeArchive(dir, slot, wm, wc, 1L))
        val (om, oc) = chainAck(slot, "poll-OTHER", 4)
        assertEquals("REJECTED:manifest_poll_mismatch", ResearchProtection.acknowledgeArchive(dir, slot, om, oc, 1L))
        val (mb, msha) = ResearchCollector.manifestArchive(dir, S1)!!
        val stale = "{}".toByteArray()
        assertEquals("REJECTED:session_manifest_not_current", ResearchProtection.acknowledgeArchive(dir, manId,
            JSONObject().put("entry_id", manId).put("index", "SESSION").put("poll_ts", S1).put("roles", JSONArray(listOf("SESSION_MANIFEST")))
                .put("content_sha256", M0Durable.sha256(stale)).put("content_bytes", stale.size), stale, 1L, msha))
        assertEquals("BACKLOG", ResearchProtection.summary(dir, S2).getString("integrity"))

        // 4. cap pressure: manifest retention cap reached with nothing acknowledged -> admission refused, nothing deleted
        ResearchCollector.MAX_MANIFEST_SESSIONS = 2
        assertNotNull(ResearchCollector.readManifest(dir, S2))
        assertEquals(ResearchCollector.Begin.Unavailable("MANIFEST_CAPACITY_BACKLOG"), ResearchCollector.beginAttempt(dir, "NF", "$S3|40", ms(S3, 12, 30)))
        assertNotNull(ResearchCollector.readManifest(dir, S1))
        // protection cap -> further capture rejected visibly
        val active = ResearchProtection.summary(dir, S2).getJSONObject("counts").let { it.getInt("STAGED") + it.getInt("ARCHIVE_ACKED") + it.getInt("LAG_EXCEEDED") }
        ResearchProtection.CAPACITY = active
        assertEquals(ResearchCollector.Begin.Unavailable("PROTECTION_BACKLOG_AT_CAPACITY"), ResearchCollector.beginAttempt(dir, "NF", "$S2|41", ms(S2, 12, 35)))
        assertEquals("BACKLOG_AT_CAPACITY", ResearchCollector.status(dir, ms(S2, 12, 36)).getString("label"))
        ResearchProtection.CAPACITY = cap0

        // 5. acknowledgement against verified manifests
        val (gm, gc) = chainAck(slot, "poll-$S1-40", 4)
        assertEquals("ARCHIVE_ACKED", ResearchProtection.acknowledgeArchive(dir, slot, gm, gc, 2L))
        assertFalse(ResearchProtection.isProtected(dir, "NF", "poll-$S1-40"))
        assertEquals("ARCHIVE_ACKED", ResearchProtection.acknowledgeArchive(dir, manId,
            JSONObject().put("entry_id", manId).put("index", "SESSION").put("poll_ts", S1).put("roles", JSONArray(listOf("SESSION_MANIFEST")))
                .put("content_sha256", msha).put("content_bytes", mb.size), mb, 2L, ResearchCollector.manifestArchive(dir, S1)!!.second))

        // 6. deletion: only now may the oldest manifest go; its entries become RELEASED
        assertTrue(ResearchCollector.beginAttempt(dir, "NF", "$S3|40", ms(S3, 12, 30)) is ResearchCollector.Begin.Started)
        assertNull(ResearchCollector.readManifest(dir, S1))
        assertEquals("RELEASED", ResearchProtection.entry(dir, slot)!!.getString("state"))
        assertEquals("RELEASED", ResearchProtection.entry(dir, manId)!!.getString("state"))
        assertNotNull(ResearchCollector.readManifest(dir, S2))
    }

    @Test fun envelopes_are_protected_and_only_deleted_after_verified_ack() {
        ResearchCollector.setEnabled(dir, true, ms(S1, 9, 0))
        ResearchCollector.MAX_ENVELOPE_FILES = 1
        val pol = JSONObject()
        val e1 = ResearchCollector.buildEnvelope("$S1|40", null, 1L, 2L, "v", 1, pol)
        assertEquals(ResearchCollector.EnvResult.STORED, ResearchCollector.putEnvelope(dir, e1, 3L))
        val id = ResearchCollector.envelopeEntryId("$S1|40")
        assertEquals("STAGED", ResearchProtection.entry(dir, id)!!.getString("state"))
        val e2 = ResearchCollector.buildEnvelope("$S1|41", null, 1L, 2L, "v", 1, pol)
        assertEquals(ResearchCollector.EnvResult.CAPACITY_BACKLOG, ResearchCollector.putEnvelope(dir, e2, 4L))
        assertNotNull(ResearchCollector.readEnvelope(dir, "$S1|40"))
        val bytes = ResearchCollector.envelopeArchiveBytes(dir, "$S1|40")!!
        val m = JSONObject().put("entry_id", id).put("index", "COMBINED").put("poll_ts", "$S1|40").put("roles", JSONArray(listOf("FORECAST_ENVELOPE")))
            .put("content_sha256", M0Durable.sha256(bytes)).put("content_bytes", bytes.size)
        val tampered = String(bytes).replace("MISSING", "VALID").toByteArray()
        assertTrue(ResearchProtection.acknowledgeArchive(dir, id, JSONObject(m.toString()).put("content_sha256", M0Durable.sha256(tampered)).put("content_bytes", tampered.size), tampered, 5L).startsWith("REJECTED:"))
        assertEquals("ARCHIVE_ACKED", ResearchProtection.acknowledgeArchive(dir, id, m, bytes, 5L))
        assertEquals(ResearchCollector.EnvResult.STORED, ResearchCollector.putEnvelope(dir, e2, 6L))
        assertNull(ResearchCollector.readEnvelope(dir, "$S1|40"))
        assertEquals("RELEASED", ResearchProtection.entry(dir, id)!!.getString("state"))
    }

    @Test fun runtime_wiring_reaches_protection_and_bounded_reconcile() {
        val src = File("src/main/java/com/marketradar/app/MarketWatchService.kt").readText()
        assertTrue(src.contains("leaseId = att.attemptId"))
        val close = src.substring(src.indexOf("private fun m0CloseOpenAttempts"), src.indexOf("private fun m0CaptureEnvelope"))
        assertTrue(close.contains("ResearchCollector.maybeReconcile(m0Dir(), now)"))
        assertTrue(close.contains("m0Safely("))
        // maybeReconcile is rate-limited (bounded runtime point)
        ResearchCollector.setEnabled(dir, true, ms(S1, 9, 0))
        assertNotNull(ResearchCollector.maybeReconcile(dir, ms(S1, 10, 0)))
        assertNull(ResearchCollector.maybeReconcile(dir, ms(S1, 10, 10)))
        assertNotNull(ResearchCollector.maybeReconcile(dir, ms(S1, 10, 31)))
    }
}
