package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.time.LocalDateTime

/** Review regressions: assertions express required safe behaviour, not the current implementation. */
class Batch1ReviewCounterexamples {
    private val day = "2026-10-23"
    private fun now() = LocalDateTime.parse("${day}T12:30:05").atZone(ResearchCalendar.IST).toInstant().toEpochMilli()
    private inline fun fixture(block: (File) -> Unit) {
        val dir = Files.createTempDirectory("batch1-review-").toFile()
        val max = ResearchProtection.MAX_BYTES
        val maps = ResearchCollector.MAX_IMAP_BYTES
        try { ResearchCollector.setEnabled(dir, true, now()); block(dir) }
        finally { ResearchProtection.MAX_BYTES = max; ResearchCollector.MAX_IMAP_BYTES = maps; dir.deleteRecursively() }
    }
    private fun chain() = JSONObject().put("data", JSONArray().put(JSONObject()
        .put("strike_price", 22650).put("expiry", "2026-10-27")
        .put("call_options", JSONObject().put("instrument_key", "NSE_FO|12345")
            .put("market_data", JSONObject().put("bid_price", 10).put("ask_price", 11)))
        .put("put_options", JSONObject().put("instrument_key", "NSE_FO|12346")
            .put("market_data", JSONObject().put("bid_price", 12).put("ask_price", 13)))))
    private fun capture(dir: File, poll: String = "original-poll"): Pair<ResearchCollector.Begin.Started, ResearchCollector.Capture> {
        val a = ResearchCollector.beginAttempt(dir, "NF", "$day|40", now()) as ResearchCollector.Begin.Started
        val c = ResearchCollector.captureFromChain(dir, "NF", chain(), now(), now(), ChainEvidenceRef.TABLE, now(), poll, now(), a.attemptId)
        assertEquals(ResearchCollector.RecordResult.RECORDED, ResearchCollector.recordCapture(dir, a.slotId, a.attemptId, c, now()))
        return a to c
    }

    @Test fun unrelatedSameCountArchiveMustNotUnprotectOriginalChain() = fixture { dir ->
        val (a, _) = capture(dir)
        // These integers are not the captured bid/ask/instrument rows. Self-consistent metadata is not source proof.
        val bytes = JSONObject().put("index", "NF").put("poll_ts", "original-poll")
            .put("rows", JSONArray(listOf(0, 1))).toString().toByteArray(Charsets.UTF_8)
        // Fixture adapted to the B1-R2 API: protection is per durable attempt (id = slot@attempt).
        val pid = ResearchCollector.chainProtectionId(a.slotId, a.attemptId)
        val manifest = JSONObject().put("entry_id", pid).put("index", "NF").put("poll_ts", "original-poll")
            .put("roles", JSONArray(listOf("CHAIN_POLL"))).put("row_count", 2)
            .put("content_sha256", M0Durable.sha256(bytes)).put("content_bytes", bytes.size)
        val result = ResearchProtection.acknowledgeArchive(dir, pid, manifest, bytes, now())
        assertTrue("unrelated rows were accepted: $result", result.startsWith("REJECTED:"))
        assertTrue(ResearchProtection.isProtected(dir, "NF", "original-poll"))
    }

    @Test fun mapIndexLossMustNotDeleteManifestReferencedMap() = fixture { dir ->
        val (_, c) = capture(dir)
        val old = File(dir, "imap_${c.imapVersion}.json")
        assertTrue(old.exists())
        assertTrue(File(dir, "imapindex.json").delete())
        val result = ResearchCollector.putInstrumentMap(dir, "NF", listOf("NF|2026-10-27|22700|CE|NSE_FO|99999"), now(), "new-attempt")
        assertTrue("map returned $result but erased a map still referenced by the durable manifest", old.exists())
    }

    @Test fun byteFullProtectionMustNotReportEnvelopeStoredWithoutProtection() = fixture { dir ->
        ResearchProtection.MAX_BYTES = 700
        val key = "$day|40"
        val env = ResearchCollector.buildEnvelope(key, null, now(), now(), "review", 1, JSONObject())
        val result = ResearchCollector.putEnvelope(dir, env, now())
        val entry = ResearchProtection.entry(dir, ResearchCollector.envelopeEntryId(key))
        assertTrue("result=$result protection=$entry rejected=" + ResearchProtection.summary(dir, day).optInt("CAPACITY_REJECTED"),
            result != ResearchCollector.EnvResult.STORED || entry != null)
    }

    @Test fun envelopeRetryMustRepairOrFlagMissingProtection() = fixture { dir ->
        val key = "$day|40"
        val env = ResearchCollector.buildEnvelope(key, null, now(), now(), "review", 1, JSONObject())
        // Exact durable state after a process dies between putEnvelope's envelope commit and ledger request.
        val body = env.toString()
        val name = "env_" + key.replace(Regex("[^A-Za-z0-9_.-]"), "_").take(80) + ".json"
        M0Durable.write(File(dir, name), JSONObject().put("sha256", ResearchCollector.sha256(body))
            .put("stored_ms", now()).put("envelope_text", body).toString())
        val result = ResearchCollector.putEnvelope(dir, env, now())
        assertNotNull("retry reported $result but did not repair or flag orphan evidence", ResearchProtection.entry(dir, ResearchCollector.envelopeEntryId(key)))
    }

    @Test fun everyDurableRetryPollMustBeProtected() = fixture { dir ->
        capture(dir, "first-poll")
        capture(dir, "second-poll")
        assertTrue("second durable attempt is recorded but only first poll is protected", ResearchProtection.isProtected(dir, "NF", "second-poll"))
    }

    @Test fun mapByteCapMustMeasureWrittenSealedBytes() = fixture { dir ->
        val lines = (1..100).map { "NF|2026-10-27|${22000 + it * 50}|CE|NSE_FO|${100000 + it}" }
        ResearchCollector.MAX_IMAP_BYTES = lines.joinToString("\n").length.toLong() + 256
        val result = ResearchCollector.putInstrumentMap(dir, "NF", lines, now(), "lease")
        val actual = dir.listFiles()!!.filter { it.name.startsWith("imap_") }.sumOf { it.length() }
        assertTrue("result=$result actual_sealed_bytes=$actual cap=${ResearchCollector.MAX_IMAP_BYTES}", actual <= ResearchCollector.MAX_IMAP_BYTES)
    }
}
