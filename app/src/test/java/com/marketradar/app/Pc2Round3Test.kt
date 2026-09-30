package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * Codex round-2 review (30 Sep 2026): B1, B2, C1, the classifier refinement,
 * the pinned canonicalisation contract, and the B4 client side.
 *
 * B2 and C1 were reproduced in the round-2 class before being fixed:
 *   B2  1,000 passes, 25 retrying batches ahead of a good one: 0 tail attempts.
 *   C1  reason sidecar unwritable: quarantined=1 storageErrors=0 reason=null.
 */
class Pc2Round3Test {

    private fun tempDir(): File = Files.createTempDirectory("pc2-r3").toFile()
    private fun outboxOf(dir: File) = File(dir, "pc2_telemetry_outbox_v1")

    private fun snapshot(pollTs: String, marker: String, policy: JSONObject? = null): JSONObject {
        val decisions = JSONArray()
        repeat(4) { i ->
            decisions.put(JSONObject().put("variable_name", "vix").put("marker", marker).put("bucket", i % 3))
        }
        return JSONObject()
            .put("session_date", "2026-09-30")
            .put("poll_ts", pollTs)
            .put("context_json", JSONObject()
                .put("snapshot_pc2_authority_decisions", decisions)
                .put("snapshot_pc2_authority_policy", policy ?: JSONObject().put("version", "pc2_authority_policy_v2"))
                .put("snapshot_brain_version", "2.6.63"))
    }

    private fun built(i: Int, marker: String = "m$i") =
        requireNotNull(Pc2CompactBatch.build(snapshot("2026-09-30T10:%02d:00+0530".format(i % 60), marker)))

    private fun <T> withSeams(
        move: ((File, File) -> Boolean)? = null,
        write: ((File, String) -> Boolean)? = null,
        block: () -> T
    ): T {
        val originalMove = Pc2TelemetryOutbox.moveFile
        val originalWrite = Pc2TelemetryOutbox.writeMetadata
        move?.let { Pc2TelemetryOutbox.moveFile = it }
        write?.let { Pc2TelemetryOutbox.writeMetadata = it }
        return try { block() } finally {
            Pc2TelemetryOutbox.moveFile = originalMove
            Pc2TelemetryOutbox.writeMetadata = originalWrite
        }
    }

    // =========================================================================
    // B2 - fair, bounded, restart-safe scheduling
    // =========================================================================

    private fun enqueueStarvationScenario(dir: File, failing: Int): Pair<Set<String>, String> {
        val failingIds = HashSet<String>()
        for (i in 0 until failing) {
            val b = built(i, "fail$i")
            failingIds += b.batchRow.getString("batch_id")
            Pc2TelemetryOutbox.enqueue(dir, b); Thread.sleep(2)
        }
        val good = built(59, "good-tail")
        Pc2TelemetryOutbox.enqueue(dir, good)
        return failingIds to good.batchRow.getString("batch_id")
    }

    @Test
    fun moreThanABudgetOfRetryingBatchesCannotStarveTheTail() {
        val dir = tempDir()
        val (failing, goodId) = enqueueStarvationScenario(dir, failing = 30)
        var passes = 0
        var goodAcknowledged = false
        while (!goodAcknowledged && passes < 100) {
            passes += 1
            Pc2TelemetryOutbox.drain(dir) { env ->
                val id = env.getJSONObject("batch_row").getString("batch_id")
                if (id == goodId) { goodAcknowledged = true; Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED }
                else Pc2TelemetryOutbox.Outcome.RETRY
            }
        }
        assertTrue("the good tail must be acknowledged", goodAcknowledged)
        // 30 retrying batches, at most MAX_CONSECUTIVE_RETRIES per pass: the tail
        // is reached within ceil(31 / 3) = 11 passes. Bounded, not probabilistic.
        val bound = (failing.size + 1 + Pc2TelemetryOutbox.MAX_CONSECUTIVE_RETRIES - 1) /
            Pc2TelemetryOutbox.MAX_CONSECUTIVE_RETRIES
        assertTrue("tail reached in $passes passes, bound $bound", passes <= bound)
        assertEquals("every failing batch is still queued", failing.size, Pc2TelemetryOutbox.pending(dir).size)
        assertEquals(0, Pc2TelemetryOutbox.quarantinedFiles(dir).size)

        // The prefix recovers: every remaining envelope acknowledges.
        var acknowledged = 0
        repeat(5) { acknowledged += Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED }.acknowledged }
        assertEquals(failing.size, acknowledged)
        assertEquals(0, Pc2TelemetryOutbox.pending(dir).size)
    }

    @Test
    fun everyRetryingBatchIsAttemptedWithinBoundedPasses() {
        val dir = tempDir()
        repeat(40) { i -> Pc2TelemetryOutbox.enqueue(dir, built(i)); Thread.sleep(1) }
        val order = mutableListOf<String>()
        val passes = (40 + Pc2TelemetryOutbox.MAX_CONSECUTIVE_RETRIES - 1) / Pc2TelemetryOutbox.MAX_CONSECUTIVE_RETRIES
        repeat(passes) {
            Pc2TelemetryOutbox.drain(dir) { env ->
                order += env.getJSONObject("batch_row").getString("batch_id")
                Pc2TelemetryOutbox.Outcome.RETRY
            }
        }
        assertEquals("round robin: all 40 attempted within $passes passes", 40, order.toSet().size)
        assertEquals("no batch is tried twice before every batch is tried once", 40, order.take(40).toSet().size)
    }

    @Test
    fun anOutagePassIsCheapAndStopsOnConsecutiveRetries() {
        val dir = tempDir()
        repeat(10) { i -> Pc2TelemetryOutbox.enqueue(dir, built(i)); Thread.sleep(1) }
        val pass = Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.RETRY }
        assertEquals(Pc2TelemetryOutbox.MAX_CONSECUTIVE_RETRIES, pass.attempted)
        assertEquals("consecutive_retries", pass.stopReason)
        assertEquals(10, pass.pending)
    }

    @Test
    fun perPassWorkIsBoundedByTheBudget() {
        val dir = tempDir()
        repeat(Pc2TelemetryOutbox.MAX_UPLOADS_PER_PASS + 10) { i ->
            Pc2TelemetryOutbox.enqueue(dir, built(i)); Thread.sleep(1)
        }
        val pass = Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED }
        assertEquals(Pc2TelemetryOutbox.MAX_UPLOADS_PER_PASS, pass.attempted)
        assertEquals("budget", pass.stopReason)
        assertEquals(10, pass.pending)
    }

    @Test
    fun rotationSurvivesARestart() {
        val dir = tempDir()
        val (failing, goodId) = enqueueStarvationScenario(dir, failing = 30)
        // Run part of the rotation, then forget everything held in memory.
        repeat(4) { Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.RETRY } }
        Pc2TelemetryOutbox.simulateProcessRestart()

        val seenAfterRestart = mutableListOf<String>()
        var passes = 0
        while (goodId !in seenAfterRestart && passes < 100) {
            passes += 1
            Pc2TelemetryOutbox.drain(dir) { env ->
                val id = env.getJSONObject("batch_row").getString("batch_id")
                seenAfterRestart += id
                if (id == goodId) Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED else Pc2TelemetryOutbox.Outcome.RETRY
            }
        }
        assertTrue(goodId in seenAfterRestart)
        val retriedBeforeRestart = 4 * Pc2TelemetryOutbox.MAX_CONSECUTIVE_RETRIES
        assertTrue(
            "after restart the rotation resumes where it was: no batch retried before the " +
                "restart is tried again before the untried ones",
            seenAfterRestart.takeWhile { it != goodId }.size <= failing.size + 1 - retriedBeforeRestart
        )
    }

    @Test
    fun losingTheSequenceFileCannotSendARetriedBatchBackToTheFront() {
        val dir = tempDir()
        repeat(4) { i -> Pc2TelemetryOutbox.enqueue(dir, built(i)); Thread.sleep(2) }
        fun pass(): List<String> {
            val seen = mutableListOf<String>()
            Pc2TelemetryOutbox.drain(dir) { env ->
                seen += env.getJSONObject("batch_row").getString("batch_id"); Pc2TelemetryOutbox.Outcome.RETRY
            }
            return seen
        }
        val first = pass()                     // A, B, C retried; D untried
        File(outboxOf(dir), "attempt_sequence").delete()
        Pc2TelemetryOutbox.simulateProcessRestart()
        val second = pass()                    // D, A, B
        val third = pass()
        val c = first[2]
        assertFalse(c in second)
        assertEquals(
            "C, untouched since the first pass, must lead the third. Without rebuilding the " +
                "sequence from the per-batch state, the retries after the restart reuse low " +
                "numbers and the batches just tried jump back ahead of it",
            c,
            third.first()
        )
    }

    @Test
    fun aRetryStateWriteFailureIsReportedNotSilent() {
        val dir = tempDir()
        Pc2TelemetryOutbox.enqueue(dir, built(1))
        val pass = withSeams(write = { _, _ -> false }) {
            Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.RETRY }
        }
        assertEquals(1, pass.storageErrors)
        assertTrue(pass.storageErrorReasons.single().contains("retry_state_write_failed"))
        assertEquals("the evidence itself is untouched", 1, pass.pending)
    }

    // =========================================================================
    // C1 - the reason cannot be lost separately from the bytes
    // =========================================================================

    @Test
    fun aReasonSidecarWriteFailureIsSurfacedAndTheReasonStillPersists() {
        val dir = tempDir()
        val b = built(1)
        Pc2TelemetryOutbox.enqueue(dir, b)
        val before = outboxOf(dir).listFiles { f -> f.extension == "json" }!!.single().readText()

        val pass = withSeams(write = { file, _ -> !file.name.endsWith(".reason") }) {
            Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.QUARANTINE }
        }
        assertEquals("the bytes were retained, so the quarantine happened", 1, pass.quarantined)
        assertEquals("but the missing detail is reported", 1, pass.storageErrors)
        assertTrue(pass.storageErrorReasons.single().contains("reason_sidecar_write_failed"))

        val archived = Pc2TelemetryOutbox.quarantinedFiles(dir).single()
        assertEquals("retained bytes are exactly the queued bytes", before, archived.readText())
        assertFalse(File(archived.parentFile, "${archived.name}.reason").exists())
        assertEquals(
            "the reason survives in the archive name alone",
            "permanent_rejection",
            Pc2TelemetryOutbox.archivedReason(archived)
        )
        assertEquals(b.batchRow.getString("batch_id"), Pc2TelemetryOutbox.archiveBatchIdOf(archived.name))

        // Recovery: the next pass backfills the sidecar.
        val next = Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED }
        assertEquals(0, next.storageErrors)
        val sidecar = File(archived.parentFile, "${archived.name}.reason")
        assertTrue(sidecar.isFile)
        assertTrue(sidecar.readText().startsWith("permanent_rejection backfilled_at="))
    }

    @Test
    fun aFailedBackfillIsAlsoReported() {
        val dir = tempDir()
        Pc2TelemetryOutbox.enqueue(dir, built(1))
        withSeams(write = { file, _ -> !file.name.endsWith(".reason") }) {
            Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.QUARANTINE }
            val again = Pc2TelemetryOutbox.drain(dir) { Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED }
            assertEquals(1, again.storageErrors)
            assertTrue(again.storageErrorReasons.single().contains("reason_sidecar_backfill_failed"))
        }
    }

    // =========================================================================
    // Classifier: exact code field, not substrings
    // =========================================================================

    @Test
    fun theClassifierReadsTheCodeFieldNotDigitsInAMessage() {
        assertEquals(
            "a message mentioning 23505 is not a unique violation",
            Pc2TelemetryOutbox.Outcome.RETRY,
            Pc2TelemetryOutbox.classifyPostFailure(503, """{"code":"XX000","message":"retry 23505 later"}""")
        )
        assertEquals(Pc2TelemetryOutbox.Outcome.QUARANTINE,
            Pc2TelemetryOutbox.classifyPostFailure(400, """{"code": "23505", "message":"dup"}"""))
        assertEquals(Pc2TelemetryOutbox.Outcome.QUARANTINE,
            Pc2TelemetryOutbox.classifyPostFailure(400, """{"code":"22P05","message":"unsupported Unicode escape"}"""))
        assertEquals(Pc2TelemetryOutbox.Outcome.QUARANTINE,
            Pc2TelemetryOutbox.classifyPostFailure(413, """{"code":"PT413","message":"PC2_PAYLOAD_TOO_LARGE"}"""))
        // B4 recoverable states retry.
        assertEquals(Pc2TelemetryOutbox.Outcome.RETRY,
            Pc2TelemetryOutbox.classifyPostFailure(403, """{"code":"PT403","message":"PC2_DEVICE_NOT_AUTHORIZED"}"""))
        assertEquals(Pc2TelemetryOutbox.Outcome.RETRY,
            Pc2TelemetryOutbox.classifyPostFailure(429, """{"code":"PT429","message":"PC2_DAILY_QUOTA_EXCEEDED"}"""))
        assertEquals("function not deployed yet",
            Pc2TelemetryOutbox.Outcome.RETRY,
            Pc2TelemetryOutbox.classifyPostFailure(404, """{"code":"PGRST202","message":"Could not find the function"}"""))
    }

    // =========================================================================
    // B1 - one version-derivation rule
    // =========================================================================

    @Test
    fun policyVersionDerivationFollowsTheOneWrittenRule() {
        fun v(policy: JSONObject) = Pc2CompactBatch.derivePolicyVersion(policy)
        val d = Pc2CompactBatch.DEFAULT_POLICY_VERSION
        assertEquals("absent", d, v(JSONObject()))
        assertEquals("blank", d, v(JSONObject().put("version", "")))
        assertEquals("whitespace only", d, v(JSONObject().put("version", " \t\n\r\u000B\u000C ")))
        assertEquals("JSON null", d, v(JSONObject().put("version", JSONObject.NULL)))
        assertEquals("number", d, v(JSONObject().put("version", 2)))
        assertEquals("object", d, v(JSONObject().put("version", JSONObject())))
        assertEquals("pc2_authority_policy_v2", v(JSONObject().put("version", "pc2_authority_policy_v2")))
        assertEquals("ASCII whitespace stripped", "v2", v(JSONObject().put("version", "\t v2\n\u000B")))
        assertEquals("Unicode space kept", "\u00a0v2", v(JSONObject().put("version", "\u00a0v2")))
        assertEquals("interior kept", "v 2", v(JSONObject().put("version", " v 2 ")))
    }

    @Test
    fun theBuilderUsesTheDerivedVersionAndThePinnedSchema() {
        val b = requireNotNull(Pc2CompactBatch.build(
            snapshot("2026-09-30T10:00:00+0530", "b1", JSONObject().put("version", "  "))
        ))
        assertEquals(Pc2CompactBatch.DEFAULT_POLICY_VERSION, b.policyRow.getString("policy_version"))
        assertEquals(Pc2CompactBatch.DEFAULT_POLICY_VERSION, b.batchRow.getString("policy_version"))
        assertEquals(Pc2CompactBatch.GROUPING_SCHEMA_VERSION, b.policyRow.getString("schema_version"))
    }

    // =========================================================================
    // Pinned canonicalisation (found while building the B3 audit)
    // =========================================================================

    @Test
    fun canonicalStringsFollowTheJcsRuleOnEveryPlatform() {
        val c = Pc2CompactBatch::canonicalString
        assertEquals("slash is literal (Android's quote() writes \\/)", "\"n/a\"", c("n/a"))
        assertEquals("\"</x>\"", c("</x>"))
        assertEquals("\"a\\\"b\\\\c\"", c("a\"b\\c"))
        assertEquals("\"\\b\\f\\n\\r\\t\"", c("\b\u000C\n\r\t"))
        assertEquals("\"\\u0000\\u001f\"", c("\u0000\u001f"))
        assertEquals("C1 controls literal (reference quote() escapes them)", "\"\u0085\"", c("\u0085"))
        assertEquals("U+2028 literal (Android escapes it)", "\"\u2028\"", c("\u2028"))
        assertEquals("a valid pair is kept", "\"\uD83D\uDE00\"", c("\uD83D\uDE00"))
        assertEquals("a lone surrogate is escaped", "\"\\ud800x\"", c("\uD800x"))
        assertEquals("keys use the same rule", "{\"a/b\":\"c/d\"}",
            Pc2CompactBatch.canonicalJson(JSONObject().put("a/b", "c/d")))
    }

    @Test
    fun theCompactorNoLongerDelegatesEscapingToOrgJson() {
        val source = listOf(
            File("app/src/main/java/com/marketradar/app/Pc2CompactBatch.kt"),
            File("src/main/java/com/marketradar/app/Pc2CompactBatch.kt")
        ).first { it.exists() }.readText()
        val code = source.lines().filterNot { it.trimStart().startsWith("*") || it.trimStart().startsWith("//") }
            .joinToString("\n")
        assertFalse("JSONObject.quote differs between Android and the JVM", code.contains("JSONObject.quote("))
    }

    @Test
    fun theSnapshotReferenceCarriesTheFullIdentity() {
        val b = built(3)
        val ref = b.snapshotRef
        assertEquals(b.batchRow.getString("batch_id"), Pc2ParityAudit.identityOf(ref))
        assertEquals(b.batchRow.getString("batch_id"), Pc2ParityAudit.identityOf(b.batchRow))
    }

    @Test
    fun reconstructionRejectsEveryStructuralDefect() {
        fun row(grouped: String) = JSONObject().put("grouped_canonical", grouped)
        val base = """"schema_version":"pc2_exact_dedup_v1""""
        val defects = mapOf(
            "duplicate index (Codex counterexample)" to """{$base,"ordered_count":2,"prototypes":[{"decision":{"x":1},"indexes":[0,0]}]}""",
            "gap" to """{$base,"ordered_count":3,"prototypes":[{"decision":{"x":1},"indexes":[0,2]}]}""",
            "out of range" to """{$base,"ordered_count":1,"prototypes":[{"decision":{"x":1},"indexes":[1]}]}""",
            "negative" to """{$base,"ordered_count":1,"prototypes":[{"decision":{"x":1},"indexes":[-1]}]}""",
            "fractional index" to """{$base,"ordered_count":1,"prototypes":[{"decision":{"x":1},"indexes":[0.5]}]}""",
            "string index" to """{$base,"ordered_count":1,"prototypes":[{"decision":{"x":1},"indexes":["0"]}]}""",
            "missing count" to """{$base,"prototypes":[{"decision":{"x":1},"indexes":[0]}]}""",
            "zero count" to """{$base,"ordered_count":0,"prototypes":[]}""",
            "missing prototypes" to """{$base,"ordered_count":1}""",
            "empty object" to "{}",
            "empty indexes" to """{$base,"ordered_count":1,"prototypes":[{"decision":{"x":1},"indexes":[]},{"decision":{"y":1},"indexes":[0]}]}"""
        )
        defects.forEach { (name, grouped) ->
            val result = runCatching { Pc2CompactBatch.reconstructOrderedDecisions(row(grouped)) }
            assertTrue("$name must not reconstruct", result.isFailure)
        }
    }

    // =========================================================================
    // B4 - client side of authorised ingestion
    // =========================================================================

    @Test
    fun theCredentialIsCreatedOnceAndOnlyWhenDurable() {
        var stored: String? = null
        val key = Pc2IngestCredential.loadOrCreate(read = { stored }, write = { stored = it; true })
        assertTrue(Pc2IngestCredential.isValidKey(key))
        assertEquals("the same key on every later call", key,
            Pc2IngestCredential.loadOrCreate(read = { stored }, write = { error("must not rewrite") }))

        assertNull("a failed write yields no key",
            Pc2IngestCredential.loadOrCreate(read = { null }, write = { false }))
        assertNull("a write that does not read back yields no key",
            Pc2IngestCredential.loadOrCreate(read = { null }, write = { true }))
        assertNotEquals(Pc2IngestCredential.generateKey(), Pc2IngestCredential.generateKey())
    }

    @Test
    fun theKeyHashMatchesTheServerDerivation() {
        // Server: encode(sha256(convert_to(p_device_key,'UTF8')),'hex')
        val key = "0123456789abcdef".repeat(4)
        assertEquals(Pc2CompactBatch.sha256(key), Pc2IngestCredential.keyHash(key))
        assertTrue(Pc2IngestCredential.keyHash(key).matches(Regex("[0-9a-f]{64}")))
    }

    @Test
    fun onlyAnExplicitRegisteredAndActiveAnswerEnablesTheChannel() {
        assertTrue(Pc2IngestCredential.statusIsActive("""{"registered":true,"active":true}"""))
        assertFalse(Pc2IngestCredential.statusIsActive("""{"registered":true,"active":false}"""))
        assertFalse(Pc2IngestCredential.statusIsActive("""{"registered":false,"active":false}"""))
        assertFalse(Pc2IngestCredential.statusIsActive("""{}"""))
        assertFalse(Pc2IngestCredential.statusIsActive("not json"))
        assertFalse(Pc2IngestCredential.statusIsActive(null))
    }

    @Test
    fun theIngestBodyCarriesExactlyTheBuiltRows() {
        val b = built(4)
        val body = Pc2IngestCredential.ingestRequestBody("a".repeat(64), b.envelope())
        assertEquals(setOf("p_device_key", "p_policy", "p_batch"), body.keySet())
        assertEquals(Pc2CompactBatch.canonicalJson(b.policyRow), Pc2CompactBatch.canonicalJson(body.getJSONObject("p_policy")))
        assertEquals(Pc2CompactBatch.canonicalJson(b.batchRow), Pc2CompactBatch.canonicalJson(body.getJSONObject("p_batch")))
    }

    private fun source(name: String): String = listOf(
        File("app/src/main/java/com/marketradar/app/$name"),
        File("src/main/java/com/marketradar/app/$name")
    ).first { it.exists() }.readText()

    @Test
    fun theClientWritesOnlyThroughTheIngestionFunction() {
        val client = source("SupabaseClient.kt")
        val start = client.indexOf("fun savePc2CompactBatch(")
        val body = client.substring(start, client.indexOf("/** Delegates to the outbox contract", start))
        assertTrue(body.contains("\"rpc/pc2_ingest_compact_batch\""))
        assertFalse("no direct table insert", body.contains("ml_pc2_policy_registry?on_conflict"))
        assertFalse("no direct table insert", body.contains("ml_pc2_decision_batches?on_conflict"))
        assertTrue("B1: the derived version is checked before upload",
            body.contains("Pc2CompactBatch.derivePolicyVersion("))
    }

    @Test
    fun theServiceGatesTheChannelAndNeverLogsTheKey() {
        val service = source("MarketWatchService.kt")
        val gate = service.indexOf("Pc2IngestCredential.PREF_REGISTERED_CONFIRMED, false)")
        val build = service.indexOf("runCatching { Pc2CompactBatch.build(rawSnapObj) }")
        assertTrue("the build is gated on registration", gate in 0 until build)
        assertTrue(service.contains("key_hash=\${Pc2IngestCredential.keyHash(deviceKey)}"))
        assertFalse("the key itself must never be logged", Regex("LogBuffer\\.add\\([^)]*\\\$deviceKey").containsMatchIn(service))
        assertFalse(Regex("LogBuffer\\.add\\([^)]*\\{deviceKey\\}").containsMatchIn(service))
        assertTrue("the key is persisted synchronously", service.contains(".putString(Pc2IngestCredential.PREF_DEVICE_KEY, key).commit()"))
    }

    private fun assertNotEquals(a: Any?, b: Any?) = assertFalse("expected different values", a == b)
}
