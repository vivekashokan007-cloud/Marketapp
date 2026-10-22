package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.time.Instant

/**
 * b513 R2 (Codex R1–R3) service-orchestration regressions. Drives the production EvaluationPersistenceOrchestrator
 * (the exact decision MarketMLService calls) with the production bounded reader over a fake PostgREST
 * (SupabaseClient.pageFetchSeam) whose rows carry created_at and honour created_at=lte.<cutoff>, and a save port
 * that stamps created_at from the same clock as the cutoff — exactly like buildEvaluationRows/buildRecommendationRows.
 */
class B513R2EvalRecoveryTest {
    private val date = "2026-10-21"
    private val snaps = (1..3).map(Int::toString)
    private val primaryKeys = snaps.map { "$it|p$it|primary" }
    private val produced: Set<String> = buildSet {
        snaps.forEach { s -> add("$s|p$s|primary"); add("$s|s${s}a|secondary"); add("$s|s${s}b|secondary"); add("$s|r$s|rejected") }
    }

    /** Monotonic wall clock shared by save stamping and read boundaries. */
    inner class Clock(var t: Instant = Instant.parse("2026-10-21T12:00:00Z")) {
        fun now(): String { t = t.plusSeconds(1); return t.toString() }
    }

    inner class Server {
        val rows = mutableMapOf<Pair<String, String>, JSONObject>() // (table,key) -> row
        val readCutoffs = mutableListOf<Pair<String, String>>()     // (table, cutoff)
        var fail: ((String) -> SupabaseClient.PageResult?)? = null
        var reads = 0
        fun upsert(table: String, key: String, createdAt: String) {
            val (s, c, r) = key.split("|")
            rows[table to key] = JSONObject().put("snapshot_id", s.toInt()).put("candidate_id", c).put("role", r)
                .put("created_at", createdAt)
        }
        fun upsertAll(keys: Collection<String>, createdAt: String) = keys.forEach { k ->
            val r = k.substringAfterLast('|')
            upsert(if (r == "rejected") "ml_rejected_candidate_outcomes" else "ml_evaluation_outcomes", k, createdAt)
            if (r == "primary") upsert("ml_recommendation_outcomes", k, createdAt)
        }
        fun install() {
            SupabaseClient.pageFetchSeam = { table, filter, _, limit, offset ->
                reads++
                val f = filter.orEmpty()
                val cut = Regex("created_at=lte\\.([^&]+)").find(f)?.groupValues?.get(1)
                if (cut != null) readCutoffs += table to cut
                fail?.invoke(table) ?: run {
                    val roleEq = Regex("role=eq\\.([a-z]+)").find(f)?.groupValues?.get(1)
                    val list = rows.filterKeys { it.first == table }.values
                        .filter { roleEq == null || it.getString("role") == roleEq }
                        .filter { cut == null || it.getString("created_at") <= cut }
                        .sortedBy { "${it.get("snapshot_id")}|${it.get("candidate_id")}|${it.get("role")}" }
                    val arr = JSONArray(); list.drop(offset ?: 0).take(limit ?: 500).forEach { arr.put(JSONObject(it.toString())) }
                    SupabaseClient.PageResult(status = "success", rows = arr, httpCode = 200)
                }
            }
        }
    }

    private lateinit var clock: Clock
    private lateinit var srv: Server
    private var writes = 0

    @Before fun setUp() {
        clock = Clock(); srv = Server().also { it.install() }; writes = 0
        SupabaseClient.readbackSleepSeam = { }
    }
    @After fun tearDown() { SupabaseClient.resetPageFetchSeam(); SupabaseClient.readbackSleepSeam = null }

    private fun ports(saveOk: Boolean = true) = EvaluationPersistenceOrchestrator.Ports(
        readback = { _, cutoff -> SupabaseClient.readEvaluationOutcomeIdentityKeysBounded(date, true, cutoff, maxAttempts = 3) },
        save = {
            writes++
            if (saveOk) srv.upsertAll(produced, clock.now()) // fresh / re-upserted created_at stamped now
            SupabaseClient.EvaluationSaveResult(saveOk, produced.size, if (saveOk) produced.size else 0, 3, 9, if (saveOk) "ok" else "boom", 3)
        },
        certify = { rb -> EvaluationCertification.certify(snaps, produced, primaryKeys, rb) },
        nowIso = { clock.now() }
    )

    private val sha = "1".repeat(64)
    private val contract = EvaluationCertification.contractDigest(date, snaps, primaryKeys,
        mapOf("input_manifest_hash" to "m1", "evaluator_version" to "e1", "policy_label_contract" to "p1", "scope" to "full"))
    private fun ack(content: String = sha, c: String = contract) =
        EvaluationCertification.UploadAck(date, produced.size, 4096L, EvaluationCertification.keysDigest(produced), content, c)
    private fun priorAck() = ack().copy(persistedCount = produced.size)

    // ---------------- R1 ----------------
    @Test fun r1_noAck_freshSave_postWriteBoundaryIncludesNewRows_certifies() {
        val r = EvaluationPersistenceOrchestrator.run(null, ack(), produced.size, ports())
        assertEquals(EvaluationPersistenceOrchestrator.Outcome.SAVED_AND_VERIFIED, r.outcome)
        assertTrue(r.certified); assertEquals(1, writes)
        assertEquals(produced.size, r.assessment.verifiedCompositeKeys.size)
        assertNotNull(r.ackToStore)
        // every page of the post_save phase used one boundary, and it is >= every stamped created_at
        val c = r.phaseCutoffs.getValue("post_save")
        assertEquals(setOf(c), srv.readCutoffs.map { it.second }.toSet())
        assertTrue(srv.rows.values.all { it.getString("created_at") <= c })
    }

    @Test fun r1_reupsertedRowsGetLaterCreatedAtAndAreStillIncluded() {
        srv.upsertAll(produced, "2026-10-21T11:00:00Z") // b511 copy already on server, older stamp
        val r = EvaluationPersistenceOrchestrator.run(null, ack(), produced.size, ports())
        assertTrue(r.certified); assertEquals(1, writes)
        assertTrue(srv.rows.values.all { it.getString("created_at") > "2026-10-21T12:00:00Z" })
    }

    @Test fun r1_priorAck_readProvesMissingSubset_repairWrite_newBoundary_certifies() {
        srv.upsertAll(produced - setOf("2|s2a|secondary", "3|r3|rejected"), "2026-10-21T11:00:00Z")
        srv.rows.remove("ml_recommendation_outcomes" to "1|p1|primary") // missing companion too
        val r = EvaluationPersistenceOrchestrator.run(priorAck(), ack(), produced.size, ports())
        assertEquals(EvaluationPersistenceOrchestrator.Outcome.REPAIRED_AND_VERIFIED, r.outcome)
        assertTrue(r.certified); assertEquals(1, writes)
        val vf = r.phaseCutoffs.getValue("verify_first"); val pr = r.phaseCutoffs.getValue("post_repair")
        assertTrue(pr > vf)
        assertEquals(listOf(vf, pr), srv.readCutoffs.map { it.second }.distinct())
    }

    @Test fun r1_boundaryHeldFixedAcrossTransientRetriesWithinPostWritePhase() {
        var n = 0
        srv.fail = { t -> if (t == "ml_evaluation_outcomes" && n++ == 0) SupabaseClient.PageResult(status = "http_error",
            httpCode = 503, error = "HTTP_503", body = "") else null }
        val r = EvaluationPersistenceOrchestrator.run(null, ack(), produced.size, ports())
        assertTrue(r.certified); assertEquals(2, r.readback.attempts)
        assertEquals(1, srv.readCutoffs.map { it.second }.toSet().size)
    }

    // ---------------- R2 ----------------
    private fun validated(pnl: Int): EvaluationOutcomeFile.Validated {
        val dir = createTempDirectoryCompat()
        val src = java.io.File(dir, "out.json")
        val arr = JSONArray()
        produced.sorted().forEach { k -> val (s, c, r) = k.split("|")
            arr.put(JSONObject().put("snapshot_id", s.toInt()).put("candidate_id", c).put("role", r)
                .put("session_date", date).put("managed_pnl", pnl)) }
        src.writeText(arr.toString())
        return EvaluationOutcomeFile.validate(date, src, java.io.File(dir, "out.validated.json"))
    }
    private fun createTempDirectoryCompat() = java.nio.file.Files.createTempDirectory("b513r2").toFile()

    @Test fun r2_equalSizeChangedContentDoesNotReuseAck_andUploads() {
        val v1 = validated(1); val v9 = validated(9)
        assertEquals(v1.file.length(), v9.file.length()); assertEquals(v1.compositeKeys, v9.compositeKeys)
        val a1 = EvaluationCertification.UploadAck.forStream(date, v1, contract).copy(persistedCount = produced.size)
        val a9 = EvaluationCertification.UploadAck.forStream(date, v9, contract)
        assertEquals(64, a1.contentSha256.length); assertNotEquals(a1.contentSha256, a9.contentSha256)
        assertEquals(java.security.MessageDigest.getInstance("SHA-256").digest(v1.file.readBytes())
            .joinToString("") { "%02x".format(it) }, a1.contentSha256)
        assertFalse(EvaluationCertification.shouldVerifyFirst(a1, a9))
        srv.upsertAll(produced, "2026-10-21T11:00:00Z")
        val r = EvaluationPersistenceOrchestrator.run(a1, a9, produced.size, ports())
        assertEquals(1, writes); assertTrue(r.certified); assertNull(r.phaseCutoffs["verify_first"])
    }

    @Test fun r2_changedContractOrManifestDoesNotSkipUpload() {
        val other = EvaluationCertification.contractDigest(date, snaps, primaryKeys,
            mapOf("input_manifest_hash" to "m2", "evaluator_version" to "e1", "policy_label_contract" to "p1", "scope" to "full"))
        assertNotEquals(contract, other)
        assertFalse(EvaluationCertification.shouldVerifyFirst(priorAck(), ack(c = other)))
        EvaluationPersistenceOrchestrator.run(priorAck(), ack(c = other), produced.size, ports())
        assertEquals(1, writes)
    }

    @Test fun r2_oldSchemaAckIsRejected() {
        val v1Json = JSONObject().put("v", 1).put("session_date", date).put("rows", produced.size).put("bytes", 4096L)
            .put("keys_digest", EvaluationCertification.keysDigest(produced)).put("persisted", produced.size).toString()
        assertNull(EvaluationCertification.UploadAck.fromJson(v1Json))
        assertFalse(EvaluationCertification.shouldVerifyFirst(priorAck().copy(schemaVersion = 1), ack()))
        srv.upsertAll(produced, "2026-10-21T11:00:00Z")
        EvaluationPersistenceOrchestrator.run(EvaluationCertification.UploadAck.fromJson(v1Json), ack(), produced.size, ports())
        assertEquals(1, writes)
    }

    @Test fun r2_byteIdenticalSameRunResume_verifiesFirst_noWrite_stillReadsServer() {
        val v = validated(1); val again = EvaluationOutcomeFile.validate(date, v.file.parentFile.resolve("out.json"),
            v.file.parentFile.resolve("again.validated.json"))
        val a = EvaluationCertification.UploadAck.forStream(date, v, contract).copy(persistedCount = produced.size)
        val b = EvaluationCertification.UploadAck.forStream(date, again, contract)
        assertTrue(EvaluationCertification.shouldVerifyFirst(EvaluationCertification.UploadAck.fromJson(a.toJson().toString()), b))
        srv.upsertAll(produced, "2026-10-21T11:00:00Z")
        val r = EvaluationPersistenceOrchestrator.run(a, b, produced.size, ports())
        assertEquals(EvaluationPersistenceOrchestrator.Outcome.VERIFIED_FROM_ACK, r.outcome)
        assertEquals(0, writes); assertTrue(srv.reads > 0); assertTrue(r.certified)
    }

    @Test fun r2_matchingAckButServerMissingStillNotCertifiedWithoutProof() {
        val r = EvaluationPersistenceOrchestrator.run(priorAck(), ack(), produced.size, ports(saveOk = false))
        assertEquals(EvaluationPersistenceOrchestrator.Outcome.SAVE_FAILED, r.outcome)
        assertFalse(r.certified); assertNull(r.ackToStore)
    }

    // ---------------- R3 ----------------
    private fun r3(fail: SupabaseClient.PageResult, expectedAttempts: Int) {
        srv.upsertAll(produced, "2026-10-21T11:00:00Z")
        srv.fail = { fail }
        val r = EvaluationPersistenceOrchestrator.run(priorAck(), ack(), produced.size, ports())
        assertEquals(EvaluationPersistenceOrchestrator.Outcome.VERIFY_FIRST_READ_FAILED, r.outcome)
        assertEquals(0, writes); assertNull(r.ackToStore); assertFalse(r.certified)
        assertFalse(r.readback.ok); assertEquals(expectedAttempts, r.readback.attempts)
        assertNotNull(r.readback.failure)
        assertTrue(r.saveResult.message.contains("no re-upload"))
        val t = EvaluationIdentityCoverage.transitionFor(r.assessment)
        assertFalse(t.writeEvaluationDoneDate); assertFalse(t.labelsSaved)
        assertTrue(EvaluationCertification.honestStatusLine(date, r.saveResult.persistedCount, r.readback, r.assessment)
            .contains("verification=FAILED"))
    }

    @Test fun r3_persistentTransportFailure_noWrite_retryable() =
        r3(SupabaseClient.PageResult(status = "http_error", error = "SocketTimeoutException:no_message",
            exceptionClass = "SocketTimeoutException"), 3)

    @Test fun r3_authPermissionFailure_noWrite_notRetried() =
        r3(SupabaseClient.PageResult(status = "http_error", httpCode = 403, error = "HTTP_403 code=42501",
            body = "{\"code\":\"42501\",\"message\":\"permission denied\"}"), 1)

    @Test fun r3_parseFailure_noWrite() =
        r3(SupabaseClient.PageResult(status = "parse_error", httpCode = 200, error = "JSONException:bad",
            exceptionClass = "JSONException"), 1)

    @Test fun r3_successfulReadWithGenuineSubsetMissing_repairs() {
        srv.upsertAll(produced - "1|s1b|secondary", "2026-10-21T11:00:00Z")
        val r = EvaluationPersistenceOrchestrator.run(priorAck(), ack(), produced.size, ports())
        assertEquals(EvaluationPersistenceOrchestrator.Outcome.REPAIRED_AND_VERIFIED, r.outcome); assertEquals(1, writes)
    }

    @Test fun r3_exactVerifiedSuccess_noWrite() {
        srv.upsertAll(produced, "2026-10-21T11:00:00Z")
        val r = EvaluationPersistenceOrchestrator.run(priorAck(), ack(), produced.size, ports())
        assertEquals(EvaluationPersistenceOrchestrator.Outcome.VERIFIED_FROM_ACK, r.outcome)
        assertEquals(0, writes); assertTrue(r.certified)
        assertTrue(EvaluationIdentityCoverage.transitionFor(r.assessment).writeEvaluationDoneDate)
    }

    // ---------------- wiring: MarketMLService uses the orchestrator, not an inline decision ----------------
    @Test fun serviceDelegatesSaveVerifyDecisionToOrchestrator() {
        val src = java.io.File("src/main/java/com/marketradar/app/MarketMLService.kt").readText()
        assertTrue(src.contains("EvaluationPersistenceOrchestrator.run(priorAck, currentAck"))
        assertTrue(src.contains("val identityReadback = persistence.readback"))
        assertFalse(src.contains("readbackCutoff"))
        assertFalse(src.contains("verifiedFirst"))
        assertTrue(src.contains("UploadAck.forStream(sessionDate, validatedOutcomes, ackContract)"))
    }
}
