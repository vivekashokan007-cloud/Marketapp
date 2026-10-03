package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * Review corrections C1–C4 and C6 for the DB-1 compact PC2 dual-write.
 *
 * Every test here is a counterexample to the pre-correction behaviour: each one
 * fails on the `work/db1-pc2-durable-dedupe-20260929` tip.
 */
class Pc2ReviewCorrectionsTest {

    private fun tempDir(): File = Files.createTempDirectory("pc2-corrections").toFile()

    private fun snapshot(pollTs: String, decisionCount: Int, marker: String): JSONObject {
        val decisions = JSONArray()
        repeat(decisionCount) { index ->
            decisions.put(
                JSONObject()
                    .put("variable_name", "vix")
                    .put("marker", marker)
                    .put("bucket", index % 3)
            )
        }
        val context = JSONObject()
            .put("snapshot_pc2_authority_decisions", decisions)
            .put("snapshot_pc2_authority_policy", JSONObject().put("version", "pc2_authority_policy_v1"))
            .put("snapshot_brain_version", "2.6.63")
        return JSONObject()
            .put("session_date", "2026-09-29")
            .put("poll_ts", pollTs)
            .put("context_json", context)
    }

    private fun built(pollTs: String, decisionCount: Int = 12, marker: String = "a") =
        requireNotNull(Pc2CompactBatch.build(snapshot(pollTs, decisionCount, marker)))

    // ---- C6: truncation is recorded, so `complete` is never misread -------------

    @Test
    fun envelopeRecordsThatTheSourceArrayIsATail() {
        val short = built("2026-09-29T04:00:00Z", decisionCount = 12)
        val grouped = requireNotNull(Pc2CompactBatch.groupedOf(short.batchRow))
        assertEquals("LOSSLESS_OF_SNAPSHOT_ARRAY", grouped.getString("envelope_completeness"))
        assertEquals(128, grouped.getInt("source_tail_cap"))
        assertEquals(
            Pc2CompactBatch.COMPACT_CONTRACT_VERSION,
            grouped.getString("compact_contract_version")
        )
        assertFalse(grouped.getBoolean("source_possibly_truncated"))
        assertFalse(short.snapshotRef.getBoolean("source_possibly_truncated"))

        val capped = built("2026-09-29T04:05:00Z", decisionCount = Pc2CompactBatch.SOURCE_TAIL_CAP)
        assertTrue(
            "a 128-row array is at brain.py's [-128:] cap and must be flagged",
            requireNotNull(Pc2CompactBatch.groupedOf(capped.batchRow))
                .getBoolean("source_possibly_truncated")
        )
        assertTrue(capped.snapshotRef.getBoolean("source_possibly_truncated"))
        // Losslessness of the envelope itself is unchanged.
        assertTrue(capped.batchRow.getBoolean("complete"))
    }

    // ---- C3: failure classification --------------------------------------------

    @Test
    fun missingTableRetriesAndConstraintViolationQuarantines() {
        assertEquals(
            "pre-migration state must keep evidence queued",
            Pc2TelemetryOutbox.Outcome.RETRY,
            Pc2TelemetryOutbox.classifyPostFailure(404, """{"code":"PGRST205"}""")
        )
        assertEquals(
            Pc2TelemetryOutbox.Outcome.RETRY,
            Pc2TelemetryOutbox.classifyPostFailure(400, """{"code":"42P01"}""")
        )
        assertEquals(
            "the secondary unique index is not the on_conflict target: permanent",
            Pc2TelemetryOutbox.Outcome.QUARANTINE,
            Pc2TelemetryOutbox.classifyPostFailure(409, """{"code":"23505"}""")
        )
        assertEquals(
            Pc2TelemetryOutbox.Outcome.QUARANTINE,
            Pc2TelemetryOutbox.classifyPostFailure(400, """{"code":"22P02"}""")
        )
        assertEquals(Pc2TelemetryOutbox.Outcome.RETRY, Pc2TelemetryOutbox.classifyPostFailure(503, ""))
        assertEquals(Pc2TelemetryOutbox.Outcome.RETRY, Pc2TelemetryOutbox.classifyPostFailure(429, ""))
        assertEquals(Pc2TelemetryOutbox.Outcome.RETRY, Pc2TelemetryOutbox.classifyPostFailure(401, ""))
        assertEquals(Pc2TelemetryOutbox.Outcome.RETRY, Pc2TelemetryOutbox.classifyPostFailure(null, ""))
    }

    // ---- C2: no head-of-line blocking -------------------------------------------

    @Test
    fun aPermanentlyRejectedBatchDoesNotBlockLaterBatches() {
        val dir = tempDir()
        val poison = built("2026-09-29T04:00:00Z", marker = "poison")
        val good = built("2026-09-29T04:05:00Z", marker = "good")
        Pc2TelemetryOutbox.enqueue(dir, poison)
        Thread.sleep(2)
        Pc2TelemetryOutbox.enqueue(dir, good)

        val poisonId = poison.batchRow.getString("batch_id")
        val seen = mutableListOf<String>()
        val result = Pc2TelemetryOutbox.drain(dir) { envelope ->
            val id = envelope.getJSONObject("batch_row").getString("batch_id")
            seen += id
            if (id == poisonId) Pc2TelemetryOutbox.Outcome.QUARANTINE
            else Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED
        }

        assertEquals("both batches must be offered in one pass", 2, seen.size)
        assertEquals(1, result.acknowledged)
        assertEquals(1, result.quarantined)
        assertEquals("nothing may remain pending behind a rejected batch", 0, result.pending)
        assertEquals("rejected evidence is retained, never deleted", 1, result.quarantinedTotal)
    }

    @Test
    fun drainOrderIsChronologicalNotHashOrdered() {
        val dir = tempDir()
        val first = built("2026-09-29T04:00:00Z", marker = "first")
        val second = built("2026-09-29T04:05:00Z", marker = "second")
        val third = built("2026-09-29T04:10:00Z", marker = "third")
        Pc2TelemetryOutbox.enqueue(dir, first); Thread.sleep(2)
        Pc2TelemetryOutbox.enqueue(dir, second); Thread.sleep(2)
        Pc2TelemetryOutbox.enqueue(dir, third)

        val seen = mutableListOf<String>()
        Pc2TelemetryOutbox.drain(dir) { envelope ->
            seen += envelope.getJSONObject("batch_row").getString("poll_ts_text")
            Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED
        }
        assertEquals(
            listOf("2026-09-29T04:00:00Z", "2026-09-29T04:05:00Z", "2026-09-29T04:10:00Z"),
            seen
        )
    }

    @Test
    fun transientFailureStopsThePassAndKeepsEverythingQueued() {
        val dir = tempDir()
        Pc2TelemetryOutbox.enqueue(dir, built("2026-09-29T04:00:00Z", marker = "x")); Thread.sleep(2)
        Pc2TelemetryOutbox.enqueue(dir, built("2026-09-29T04:05:00Z", marker = "y"))

        val result = Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.RETRY }
        // Round 3 (B2): a pass keeps going through the rotation until
        // MAX_CONSECUTIVE_RETRIES in a row; with two batches both are tried once.
        assertEquals(2, result.attempted)
        assertEquals(2, result.retried)
        assertEquals(0, result.acknowledged)
        assertEquals(0, result.quarantined)
        assertEquals("transient faults keep everything queued", 2, result.pending)
    }

    /**
     * Codex R1 supersedes the first correction pass. A head that keeps failing
     * for a RECOVERABLE reason must stop blocking without ever leaving the
     * queue: the earlier version quarantined it after 20 attempts, which threw
     * away valid evidence the moment the backend recovered.
     */
    @Test
    fun aRecoverablyStuckHeadIsRotatedNeverQuarantined() {
        val dir = tempDir()
        val stuck = built("2026-09-29T04:00:00Z", marker = "stuck")
        Pc2TelemetryOutbox.enqueue(dir, stuck); Thread.sleep(2)
        Pc2TelemetryOutbox.enqueue(dir, built("2026-09-29T04:05:00Z", marker = "later"))
        val stuckId = stuck.batchRow.getString("batch_id")

        var quarantined = 0
        var laterAttempts = 0
        repeat(30) {
            val pass = Pc2TelemetryOutbox.drain(dir) { envelope ->
                if (envelope.getJSONObject("batch_row").getString("batch_id") != stuckId) laterAttempts += 1
                Pc2TelemetryOutbox.Outcome.RETRY
            }
            quarantined += pass.quarantined
        }
        assertEquals("a recoverable failure must never be quarantined", 0, quarantined)
        assertEquals(0, Pc2TelemetryOutbox.quarantinedFiles(dir).size)
        assertTrue("the batch behind a stuck head is still tried", laterAttempts >= 29)
        assertEquals("both batches are still queued", 2, Pc2TelemetryOutbox.pending(dir).size)

        val after = Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED }
        assertEquals("recovery drains everything automatically", 2, after.acknowledged)
        assertEquals(0, after.pending)
    }

    @Test
    fun unreadableEnvelopeIsQuarantinedNotDeletedAndDoesNotBlock() {
        val dir = tempDir()
        Pc2TelemetryOutbox.enqueue(dir, built("2026-09-29T04:05:00Z", marker = "ok"))
        val outbox = File(dir, "pc2_telemetry_outbox_v1")
        File(outbox, "1000000000000-${"c".repeat(64)}.json").writeText("{ not json")

        val result = Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED }
        assertEquals(1, result.acknowledged)
        assertEquals(1, result.quarantined)
        assertEquals(0, result.pending)
        assertEquals(1, Pc2TelemetryOutbox.quarantinedFiles(dir).size)
    }

    // ---- C4 / R3: orphaned temp files are resolved, never deleted ----------------

    /**
     * Codex R3 supersedes the first correction pass: an unparseable temp file is
     * retained for inspection instead of being deleted on age.
     */
    @Test
    fun aPartialOrphanedTempIsRetainedNotDeleted() {
        val dir = tempDir()
        Pc2TelemetryOutbox.enqueue(dir, built("2026-09-29T04:00:00Z"))
        val outbox = File(dir, "pc2_telemetry_outbox_v1")
        val orphan = File(outbox, "${"d".repeat(64)}.123456.tmp")
        orphan.writeText("partial")
        orphan.setLastModified(System.currentTimeMillis() - 2L * 60 * 60 * 1000)

        val result = Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED }
        assertFalse("the temp file must be moved out of the working set", orphan.exists())
        assertEquals("unverifiable bytes are retained, never discarded", 1, result.recoveredTotal)
        val retained = Pc2TelemetryOutbox.recoveryFiles(dir).single()
        assertEquals("partial", retained.readText())
        assertTrue(
            Pc2TelemetryOutbox.archivedReason(retained)?.startsWith("partial_or_invalid_json") == true
        )
    }

    // ---- C1: telemetry can never abort snapshot persistence ---------------------

    @Test
    fun compactBuildIsGuardedInThePollPersistencePath() {
        val source = listOf(
            File("app/src/main/java/com/marketradar/app/MarketWatchService.kt"),
            File("src/main/java/com/marketradar/app/MarketWatchService.kt"),
            File("../app/src/main/java/com/marketradar/app/MarketWatchService.kt")
        ).first { it.exists() }.readText()
        assertTrue(
            "Pc2CompactBatch.build must be wrapped: an uncaught throw is swallowed by the " +
                "outer ML_SNAPSHOT_FAIL catch and would abort snapshot + legacy PC2 persistence",
            source.contains("runCatching { Pc2CompactBatch.build(rawSnapObj) }")
        )
    }

    @Test
    fun aNonFiniteTokenInTelemetryDoesNotProduceANumberOrCrashTheBuilder() {
        // Reality check for C1: org.json rejects non-finite doubles in put() and
        // parses a bare `NaN` token as the STRING "NaN", so canonicalNumber() is
        // not reachable with a non-finite Double through either path. The guard in
        // MarketWatchService is defence in depth for any other unexpected throw,
        // not a fix for an observed NaN crash. Android ships a different org.json
        // implementation, which is one more reason to keep the guard.
        val snapshot = JSONObject(
            """{"session_date":"2026-09-29","poll_ts":"2026-09-29T04:00:00Z",""" +
                """"context_json":{"snapshot_pc2_authority_policy":{},""" +
                """"snapshot_pc2_authority_decisions":[{"observed_value":NaN}]}}"""
        )
        val built = runCatching { Pc2CompactBatch.build(snapshot) }
        assertTrue("the builder must not throw on a parsed NaN token", built.isSuccess)
        val batch = requireNotNull(built.getOrNull())
        // Round-trips losslessly as a string, so the digest still verifies.
        val reconstructed = Pc2CompactBatch.reconstructOrderedDecisions(batch.batchRow)
        assertEquals(
            batch.batchRow.getString("decision_digest"),
            Pc2CompactBatch.digestOrderedDecisions(reconstructed)
        )
    }
}
