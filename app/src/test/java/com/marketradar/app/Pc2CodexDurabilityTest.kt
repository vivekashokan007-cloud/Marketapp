package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * Codex review corrections R1-R4 (2026-09-30).
 *
 * Each test is one of the regression cases Codex named, and each one fails on
 * the previous corrections branch tip `13e1675`. Assertions are on actual file
 * bytes, names and counts rather than on uploader callback counts alone.
 */
class Pc2CodexDurabilityTest {

    private fun tempDir(): File = Files.createTempDirectory("pc2-codex").toFile()
    private fun outboxOf(dir: File) = File(dir, "pc2_telemetry_outbox_v1")

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

    private fun built(pollTs: String, marker: String = "a", decisionCount: Int = 12) =
        requireNotNull(Pc2CompactBatch.build(snapshot(pollTs, decisionCount, marker)))

    private fun <T> withFailingMoves(block: () -> T): T {
        val original = Pc2TelemetryOutbox.moveFile
        Pc2TelemetryOutbox.moveFile = { _, _ -> false }
        return try { block() } finally { Pc2TelemetryOutbox.moveFile = original }
    }

    // =========================================================================
    // R1 - recoverable failures stay automatically replayable
    // =========================================================================

    @Test
    fun classifierTreatsOnlyProvenPayloadFailuresAsPermanent() {
        val retryable = listOf(
            404 to """{"code":"PGRST205","message":"table not found"}""",
            400 to """{"code":"42P01"}""",
            404 to """{"code":"PGRST204","message":"column not found in schema cache"}""",
            404 to "",
            401 to "",
            403 to "",
            408 to "",
            429 to "",
            500 to "",
            502 to "",
            503 to "",
            400 to """{"code":"23503","message":"foreign key violation"}""",
            null to "network unreachable"
        )
        retryable.forEach { (code, body) ->
            assertEquals(
                "code=$code body=$body must stay automatically replayable",
                Pc2TelemetryOutbox.Outcome.RETRY,
                Pc2TelemetryOutbox.classifyPostFailure(code, body)
            )
        }

        val permanent = listOf(
            409 to """{"code":"23505"}""",
            409 to "",
            400 to """{"code":"22P02"}""",
            400 to """{"code":"22003"}""",
            400 to """{"code":"22001"}""",
            400 to """{"code":"23502"}""",
            400 to """{"code":"23514"}""",
            400 to """{"code":"PGRST102"}"""
        )
        permanent.forEach { (code, body) ->
            assertEquals(
                "code=$code body=$body is a proven payload rejection",
                Pc2TelemetryOutbox.Outcome.QUARANTINE,
                Pc2TelemetryOutbox.classifyPostFailure(code, body)
            )
        }
    }

    @Test
    fun manyMissingTableFailuresThenMigrationRecoveryLosesNothing() {
        val dir = tempDir()
        Pc2TelemetryOutbox.enqueue(dir, built("2026-09-29T04:00:00Z", "one")); Thread.sleep(2)
        Pc2TelemetryOutbox.enqueue(dir, built("2026-09-29T04:05:00Z", "two")); Thread.sleep(2)
        Pc2TelemetryOutbox.enqueue(dir, built("2026-09-29T04:10:00Z", "three"))

        repeat(25) {
            Pc2TelemetryOutbox.drain(dir) {
                Pc2TelemetryOutbox.classifyPostFailure(404, """{"code":"PGRST205"}""")
            }
        }
        assertEquals("pre-migration evidence must all still be queued", 3, outboxJson(dir).size)
        assertEquals(0, Pc2TelemetryOutbox.quarantinedFiles(dir).size)

        val recovered = Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED }
        assertEquals("the migration lands and every batch uploads", 3, recovered.acknowledged)
        assertEquals(0, recovered.pending)
        assertEquals(0, recovered.quarantined)
        assertEquals(0, Pc2TelemetryOutbox.quarantinedFiles(dir).size)
    }

    @Test
    fun manyAuthNetworkAndRateLimitFailuresThenSuccess() {
        val dir = tempDir()
        Pc2TelemetryOutbox.enqueue(dir, built("2026-09-29T04:00:00Z", "a")); Thread.sleep(2)
        Pc2TelemetryOutbox.enqueue(dir, built("2026-09-29T04:05:00Z", "b"))

        val cycle = listOf(401 to "", 429 to "", 503 to "", null to "connection reset")
        repeat(28) { pass ->
            val (code, body) = cycle[pass % cycle.size]
            Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.classifyPostFailure(code, body) }
        }
        assertEquals(2, outboxJson(dir).size)
        assertEquals(0, Pc2TelemetryOutbox.quarantinedFiles(dir).size)

        val after = Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED }
        assertEquals(2, after.acknowledged)
        assertEquals(0, after.pending)
    }

    @Test
    fun aGlobalSchemaCacheErrorThenCorrection() {
        val dir = tempDir()
        Pc2TelemetryOutbox.enqueue(dir, built("2026-09-29T04:00:00Z", "a")); Thread.sleep(2)
        Pc2TelemetryOutbox.enqueue(dir, built("2026-09-29T04:05:00Z", "b"))

        repeat(23) {
            Pc2TelemetryOutbox.drain(dir) {
                Pc2TelemetryOutbox.classifyPostFailure(
                    404,
                    """{"code":"PGRST204","message":"Could not find the 'grouped_decisions_json' column"}"""
                )
            }
        }
        assertEquals(
            "a schema error affects every envelope and proves nothing about one payload",
            0,
            Pc2TelemetryOutbox.quarantinedFiles(dir).size
        )
        val after = Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED }
        assertEquals(2, after.acknowledged)
    }

    @Test
    fun aProvenConstraintFailureIsIsolatedAndTheNextBatchStillUploads() {
        val dir = tempDir()
        val poison = built("2026-09-29T04:00:00Z", "poison")
        Pc2TelemetryOutbox.enqueue(dir, poison); Thread.sleep(2)
        Pc2TelemetryOutbox.enqueue(dir, built("2026-09-29T04:05:00Z", "good"))
        val poisonId = poison.batchRow.getString("batch_id")

        val result = Pc2TelemetryOutbox.drain(dir) { envelope ->
            val id = envelope.getJSONObject("batch_row").getString("batch_id")
            if (id == poisonId) Pc2TelemetryOutbox.classifyPostFailure(409, """{"code":"23505"}""")
            else Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED
        }
        assertEquals(1, result.acknowledged)
        assertEquals(1, result.quarantined)
        assertEquals(0, result.pending)
        assertEquals(1, Pc2TelemetryOutbox.quarantinedFiles(dir).size)
    }

    @Test
    fun anArchivedBatchIsNotRefusedByEnqueueAndCanBeRequeued() {
        val dir = tempDir()
        val batch = built("2026-09-29T04:00:00Z", "retryable-after-fix")
        Pc2TelemetryOutbox.enqueue(dir, batch)
        Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.QUARANTINE }
        assertEquals(1, Pc2TelemetryOutbox.quarantinedFiles(dir).size)

        assertTrue(
            "a later build of the same batch must not be silently refused",
            Pc2TelemetryOutbox.enqueue(dir, batch)
        )
        assertEquals(1, outboxJson(dir).size)
        Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED }

        // And an operator can replay the archive after a backend-side fix.
        assertEquals(1, Pc2TelemetryOutbox.requeueQuarantined(dir))
        assertEquals(0, Pc2TelemetryOutbox.quarantinedFiles(dir).size)
        assertEquals(1, Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED }.acknowledged)
    }

    // =========================================================================
    // R2 - archive moves preserve evidence and report what actually happened
    // =========================================================================

    @Test
    fun aFailedArchiveMoveIsReportedAsStorageErrorAndKeepsTheBytesPending() {
        val dir = tempDir()
        Pc2TelemetryOutbox.enqueue(dir, built("2026-09-29T04:00:00Z", "a"))
        val pendingFile = outboxJson(dir).single()
        val before = pendingFile.readText()

        val result = withFailingMoves {
            Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.QUARANTINE }
        }
        assertEquals("a move that did not happen is not a quarantine", 0, result.quarantined)
        assertEquals(1, result.storageErrors)
        assertTrue(result.storageErrorReasons.single().contains("archive_move_failed"))
        assertEquals("the evidence must still be pending", 1, result.pending)
        assertTrue(pendingFile.exists())
        assertEquals(before, pendingFile.readText())
        assertEquals(0, Pc2TelemetryOutbox.quarantinedFiles(dir).size)
    }

    @Test
    fun archivingNeverOverwritesAnEarlierArchiveWithDifferentBytes() {
        val dir = tempDir()
        Pc2TelemetryOutbox.enqueue(dir, built("2026-09-29T04:00:00Z", "a"))
        val pendingFile = outboxJson(dir).single()
        val batchId = pendingFile.nameWithoutExtension.substringAfterLast('-')

        // Pre-seed the exact destination the archiver would otherwise choose.
        val quarantineDir = File(outboxOf(dir), "quarantine").apply { mkdirs() }
        // The exact destination the archiver would otherwise choose (C1 naming).
        val decoy = File(quarantineDir, "${pendingFile.lastModified()}-$batchId--permanent_rejection.json")
        decoy.writeText("EARLIER ARCHIVED EVIDENCE")

        val result = Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.QUARANTINE }
        assertEquals(1, result.quarantined)
        assertEquals("the earlier archive must be byte-identical afterwards",
            "EARLIER ARCHIVED EVIDENCE", decoy.readText())
        val archived = Pc2TelemetryOutbox.quarantinedFiles(dir)
        assertEquals(2, archived.size)
        val fresh = archived.single { it.name != decoy.name }
        assertTrue("the new archive takes a collision-free name", fresh.name.contains("~"))
        assertNotEquals(decoy.readText(), fresh.readText())
    }

    @Test
    fun quarantiningTheSameBatchTwiceKeepsBothCopies() {
        val dir = tempDir()
        val batch = built("2026-09-29T04:00:00Z", "dup")
        Pc2TelemetryOutbox.enqueue(dir, batch)
        Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.QUARANTINE }
        Thread.sleep(3)
        Pc2TelemetryOutbox.enqueue(dir, batch)
        Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.QUARANTINE }

        assertEquals("neither copy may be lost", 2, Pc2TelemetryOutbox.quarantinedFiles(dir).size)
    }

    @Test
    fun theQuarantineReasonIsPersistedBesideTheEvidence() {
        val dir = tempDir()
        Pc2TelemetryOutbox.enqueue(dir, built("2026-09-29T04:00:00Z", "a"))
        Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.QUARANTINE }

        // A fresh read models a restart: nothing is held in memory.
        val archived = Pc2TelemetryOutbox.quarantinedFiles(dir).single()
        val reason = Pc2TelemetryOutbox.archivedReason(archived)
        assertNotNull("the reason must outlive the process and the log buffer", reason)
        assertTrue(reason!!.startsWith("permanent_rejection"))
        assertTrue(reason.contains("from="))
    }

    // =========================================================================
    // R3 - orphaned temp files may hold the only copy
    // =========================================================================

    private fun writeOrphanTemp(dir: File, content: String, ageMs: Long = 2L * 60 * 60 * 1000): File {
        val outbox = outboxOf(dir).apply { mkdirs() }
        val orphan = File(outbox, "orphan.${System.nanoTime()}.tmp")
        orphan.writeText(content)
        orphan.setLastModified(System.currentTimeMillis() - ageMs)
        return orphan
    }

    @Test
    fun aCompleteOrphanedTempIsVerifiedPromotedAndUploaded() {
        val dir = tempDir()
        outboxOf(dir).mkdirs()
        val batch = built("2026-09-29T04:00:00Z", "crash")
        val canonical = Pc2CompactBatch.canonicalJson(batch.envelope())
        val orphan = writeOrphanTemp(dir, canonical)

        val seen = mutableListOf<String>()
        val result = Pc2TelemetryOutbox.drain(dir) { envelope ->
            seen += envelope.getJSONObject("batch_row").getString("batch_id")
            Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED
        }
        assertFalse(orphan.exists())
        assertEquals(
            "a complete synced envelope must be recovered, not discarded",
            listOf(batch.batchRow.getString("batch_id")),
            seen
        )
        assertEquals(1, result.acknowledged)
        assertEquals(0, Pc2TelemetryOutbox.recoveryFiles(dir).size)
        assertEquals(listOf("${batch.batchRow.getString("batch_id")}:promoted"), result.recoveredReasons)
    }

    @Test
    fun anOrphanedTempDuplicatingAQueuedBatchIsRetainedNotDeleted() {
        val dir = tempDir()
        val batch = built("2026-09-29T04:00:00Z", "dup")
        Pc2TelemetryOutbox.enqueue(dir, batch)
        val canonical = Pc2CompactBatch.canonicalJson(batch.envelope())
        writeOrphanTemp(dir, canonical)

        val result = Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED }
        assertEquals("the queued copy still uploads exactly once", 1, result.acknowledged)
        val retained = Pc2TelemetryOutbox.recoveryFiles(dir).single()
        assertEquals(canonical, retained.readText())
        assertTrue(
            Pc2TelemetryOutbox.archivedReason(retained)?.startsWith("duplicate_of_pending") == true
        )
    }

    @Test
    fun anOrphanedTempConflictingWithAQueuedBatchIsRetained() {
        val dir = tempDir()
        val batch = built("2026-09-29T04:00:00Z", "conflict")
        Pc2TelemetryOutbox.enqueue(dir, batch)

        val altered = batch.envelope()
        altered.getJSONObject("policy_row").put("policy_version", "tampered_v9")
        val canonical = Pc2CompactBatch.canonicalJson(altered)
        writeOrphanTemp(dir, canonical)

        val result = Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED }
        assertEquals(1, result.acknowledged)
        val retained = Pc2TelemetryOutbox.recoveryFiles(dir).single()
        assertEquals(canonical, retained.readText())
        assertTrue(
            Pc2TelemetryOutbox.archivedReason(retained)?.startsWith("conflicts_with_pending") == true
        )
    }

    @Test
    fun anOrphanedTempWhoseDigestDoesNotVerifyIsNeverPromoted() {
        val dir = tempDir()
        outboxOf(dir).mkdirs()
        val batch = built("2026-09-29T04:00:00Z", "tampered")
        val envelope = batch.envelope()
        envelope.getJSONObject("batch_row").put("decision_digest", "f".repeat(64))
        val canonical = Pc2CompactBatch.canonicalJson(envelope)
        writeOrphanTemp(dir, canonical)

        val result = Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED }
        assertEquals("unverifiable bytes must never be uploaded as evidence", 0, result.acknowledged)
        val retained = Pc2TelemetryOutbox.recoveryFiles(dir).single()
        assertEquals(canonical, retained.readText())
        assertTrue(Pc2TelemetryOutbox.archivedReason(retained)?.startsWith("digest_mismatch") == true)
    }

    @Test
    fun aTempFileStillBeingWrittenIsLeftAlone() {
        val dir = tempDir()
        outboxOf(dir).mkdirs()
        writeOrphanTemp(dir, "{ half written", ageMs = 0)
        val result = Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED }
        assertEquals(0, result.recoveredTotal)
        assertEquals(1, outboxOf(dir).listFiles { f -> f.extension == "tmp" }!!.size)
    }

    // =========================================================================
    // R4 - completeness metadata is verified before acknowledgement
    // =========================================================================

    private fun groupedOf(batch: Pc2CompactBatch.Built): JSONObject =
        requireNotNull(Pc2CompactBatch.groupedOf(batch.batchRow))

    @Test
    fun consistentCompletenessMetadataVerifies() {
        val batch = built("2026-09-29T04:00:00Z", "meta")
        val expected = groupedOf(batch)
        val stored = JSONObject(expected.toString())
        assertTrue(Pc2CompactBatch.completenessMetadataMatches(expected, stored))
    }

    @Test
    fun everyMissingOrChangedCompletenessFieldFailsVerification() {
        val batch = built("2026-09-29T04:00:00Z", "meta")
        val expected = groupedOf(batch)
        Pc2CompactBatch.COMPLETENESS_FIELDS.forEach { field ->
            val missing = JSONObject(expected.toString())
            missing.remove(field)
            assertFalse(
                "a row missing $field must not be acknowledged",
                Pc2CompactBatch.completenessMetadataMatches(expected, missing)
            )
            val changed = JSONObject(expected.toString())
            changed.put(field, "CHANGED")
            assertFalse(
                "a row with a different $field must not be acknowledged",
                Pc2CompactBatch.completenessMetadataMatches(expected, changed)
            )
        }
    }

    @Test
    fun aCappedSourceIsNeverReadBackAsUncappedEvidence() {
        val capped = built("2026-09-29T04:00:00Z", "capped", Pc2CompactBatch.SOURCE_TAIL_CAP)
        val expected = groupedOf(capped)
        assertTrue(expected.getBoolean("source_possibly_truncated"))

        val relaxed = JSONObject(expected.toString()).put("source_possibly_truncated", false)
        assertFalse(
            "a stored row claiming the source was not capped is a mismatch",
            Pc2CompactBatch.completenessMetadataMatches(expected, relaxed)
        )
        val widened = JSONObject(expected.toString()).put("source_tail_cap", 4096)
        assertFalse(Pc2CompactBatch.completenessMetadataMatches(expected, widened))
    }

    @Test
    fun aLegacyEnvelopeOnlyVerifiesAgainstALegacyRow() {
        val batch = built("2026-09-29T04:00:00Z", "legacy")
        val legacy = JSONObject(groupedOf(batch).toString())
        Pc2CompactBatch.COMPLETENESS_FIELDS.forEach { legacy.remove(it) }

        assertTrue(
            "a pre-correction envelope verifies against a pre-correction row",
            Pc2CompactBatch.completenessMetadataMatches(legacy, JSONObject(legacy.toString()))
        )
        assertFalse(
            "a row that acquired metadata the envelope never sent is a mismatch",
            Pc2CompactBatch.completenessMetadataMatches(legacy, groupedOf(batch))
        )
        assertFalse(Pc2CompactBatch.completenessMetadataMatches(groupedOf(batch), legacy))
        assertFalse(Pc2CompactBatch.completenessMetadataMatches(null, legacy))
        assertFalse(Pc2CompactBatch.completenessMetadataMatches(legacy, null))
    }

    @Test
    fun theUploadPathVerifiesCompletenessBeforeAcknowledging() {
        val source = listOf(
            File("app/src/main/java/com/marketradar/app/SupabaseClient.kt"),
            File("src/main/java/com/marketradar/app/SupabaseClient.kt")
        ).first { it.exists() }.readText()
        val start = source.indexOf("fun savePc2CompactBatch(")
        assertTrue(start >= 0)
        val body = source.substring(start, source.indexOf("/** Delegates to the outbox contract", start))
        val check = body.indexOf("Pc2CompactBatch.completenessMetadataMatches(")
        val ack = body.indexOf("return Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED")
        assertTrue("completeness metadata must be verified on readback", check >= 0)
        assertTrue("it must be verified BEFORE the batch is acknowledged", ack > check)
    }

    private fun outboxJson(dir: File): List<File> =
        outboxOf(dir).listFiles { f -> f.isFile && f.extension == "json" }?.sortedBy { it.name }.orEmpty()
}
