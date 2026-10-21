package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * b512 acceptance for the ml_generated_candidates replay repair. Every server response below is a REAL PostgREST
 * 12.2.3 response recorded by tools/candidate_replay_db_suite against the live RLS shape (scratch DB, CI-like roles).
 */
class GeneratedCandidateReplayTest {
    private val golden: JSONObject = JSONObject(listOf(
        File("src/test/resources/candidate_replay/postgrest_golden.json"),
        File("app/src/test/resources/candidate_replay/postgrest_golden.json")).first { it.isFile }.readText())
    private val envelope: String = golden.getJSONObject("envelope").toString()
    private val pollTs = golden.getString("poll_ts")
    private fun resp(name: String): GeneratedCandidateReplay.Http {
        val r = golden.getJSONObject("responses").getJSONObject(name)
        val body = r.opt("body")
        return GeneratedCandidateReplay.Http(r.getInt("status"), if (body == null || body == JSONObject.NULL) "" else body.toString())
    }

    /** Scripted transport: answers in order, records what was sent. */
    private class Script(vararg val answers: GeneratedCandidateReplay.Http) : GeneratedCandidateReplay.Transport {
        var i = 0
        val inserted = mutableListOf<JSONArray>()
        val readPaths = mutableListOf<String>()
        override fun insert(rows: JSONArray): GeneratedCandidateReplay.Http { inserted += rows; return answers[i++] }
        override fun readBack(path: String): GeneratedCandidateReplay.Http { readPaths += path; return answers[i++] }
    }

    @Test fun goldenRecordsTheLiveFailureAndTheRepairPrimitives() {
        val repeat = golden.getJSONObject("responses").getJSONObject("upsert_repeat")
        assertEquals(401, repeat.getInt("status"))
        assertEquals("42501", repeat.getJSONObject("body").getString("code"))
        assertTrue(repeat.getJSONObject("body").getString("message").contains("(USING expression)"))
        assertEquals(401, resp("upsert_partial").code)
        assertEquals("23505", resp("insert_repeat").pgCode)
        assertEquals(409, resp("insert_partial").code)
        assertEquals(403, resp("auth_insert").code)
    }

    @Test fun readBackPathMatchesTheScratchSuiteAndReadsFloat8AsText() {
        assertEquals(golden.getString("readback_path"), GeneratedCandidateReplay.readBackPath(pollTs))
        assertTrue(GeneratedCandidateReplay.readBackPath(pollTs).contains("ev_per_1k::text"))
        assertTrue(GeneratedCandidateReplay.readBackPath(pollTs).contains("snapshot_poll_ts=eq.2026-10-19T10%3A15%3A00%2B05%3A30"))
    }

    @Test fun newCohortInserts() {
        val t = Script(resp("insert_new"))
        val a = GeneratedCandidateReplay.replay(envelope, t)
        assertEquals(GeneratedCandidateOutbox.Outcome.ACKNOWLEDGED, a.outcome)
        assertEquals(GeneratedCandidateReplay.State.INSERTED, a.state)
        assertEquals(1, t.inserted.size); assertEquals(50, t.inserted[0].length()); assertTrue(t.readPaths.isEmpty())
    }

    @Test fun lostSuccessRetryIsAcknowledgedOnlyAfterIdenticalReadBack() {
        val t = Script(resp("insert_repeat"), resp("readback_full"))
        val a = GeneratedCandidateReplay.replay(envelope, t)
        assertEquals(GeneratedCandidateOutbox.Outcome.ACKNOWLEDGED, a.outcome)
        assertEquals(GeneratedCandidateReplay.State.ALREADY_COMMITTED_IDENTICAL, a.state)
        assertEquals(50, a.present); assertEquals(0, a.absent); assertEquals(0, a.conflicting)
        assertEquals(1, t.inserted.size) // no UPDATE, no second write
    }

    @Test fun partialCohortInsertsOnlyTheAbsentRowsAndVerifiesBeforeAck() {
        val t = Script(resp("insert_partial"), resp("readback_partial"), resp("insert_missing"), resp("readback_after_completion"))
        val a = GeneratedCandidateReplay.replay(envelope, t)
        assertEquals(GeneratedCandidateReplay.State.PARTIAL_COMPLETED, a.state)
        assertEquals(GeneratedCandidateOutbox.Outcome.ACKNOWLEDGED, a.outcome)
        assertEquals(25, a.present); assertEquals(25, a.absent)
        val sent = (0 until t.inserted[1].length()).map { t.inserted[1].getJSONObject(it).getString("candidate_id") }.toSet()
        val stored = JSONArray(resp("readback_partial").body).let { s -> (0 until s.length()).map { s.getJSONObject(it).getString("candidate_id") }.toSet() }
        assertEquals(25, sent.size); assertTrue(sent.intersect(stored).isEmpty())
    }

    @Test fun partialCohortIsNeverFullyAcknowledgedWithoutVerification() {
        // fill insert "succeeds" but the verification read-back still shows only the first half
        val t = Script(resp("insert_partial"), resp("readback_partial"), resp("insert_missing"), resp("readback_partial"))
        val a = GeneratedCandidateReplay.replay(envelope, t)
        assertEquals(GeneratedCandidateOutbox.Outcome.RETRY, a.outcome)
        // verify read-back fails outright -> RETRY, not ACK
        val b = GeneratedCandidateReplay.replay(envelope, Script(resp("insert_partial"), resp("readback_partial"), resp("insert_missing"),
            GeneratedCandidateReplay.Http(null, null, "SocketTimeoutException")))
        assertEquals(GeneratedCandidateOutbox.Outcome.RETRY, b.outcome)
        // a concurrent writer filled the gap first -> 409 on the subset -> RETRY and reconcile next time
        val c = GeneratedCandidateReplay.replay(envelope, Script(resp("insert_partial"), resp("readback_partial"), resp("insert_repeat")))
        assertEquals(GeneratedCandidateReplay.State.PARTIAL_RACE, c.state); assertEquals(GeneratedCandidateOutbox.Outcome.RETRY, c.outcome)
    }

    @Test fun sameKeyDifferentContentStaysAVisibleConflict() {
        val t = Script(resp("insert_repeat"), resp("readback_conflict"))
        val a = GeneratedCandidateReplay.replay(envelope, t)
        assertEquals(GeneratedCandidateOutbox.Outcome.CONFLICT, a.outcome)
        assertEquals(1, a.conflicting); assertEquals(listOf("ev_per_1k"), a.conflictColumns)
        assertFalse(a.describe().contains("99.5")) // names, never values
        assertEquals(1, t.inserted.size)
        // stored rows the frozen cohort does not contain are also a conflict, never a silent ack
        val extra = JSONArray(resp("readback_full").body).put(JSONObject(JSONArray(resp("readback_full").body).getJSONObject(0).toString()).put("candidate_id", "FX-ZZ-999"))
        val e = GeneratedCandidateReplay.replay(envelope, Script(resp("insert_repeat"), GeneratedCandidateReplay.Http(200, extra.toString())))
        assertEquals(GeneratedCandidateOutbox.Outcome.CONFLICT, e.outcome); assertEquals(1, e.extra)
    }

    @Test fun negativeZeroIsComparedExactlyNotAsAFalseConflict() {
        // FX-NF-002 has ev_per_1k = -0.0 in the frozen bytes; the golden read-back holds "-0" text.
        val frozen = JSONObject(envelope).getJSONArray("rows").let { r -> (0 until r.length()).map { r.getJSONObject(it) } }
            .single { it.getString("candidate_id") == "FX-NF-002" }
        assertEquals(java.lang.Double.doubleToRawLongBits(-0.0), java.lang.Double.doubleToRawLongBits(frozen.getDouble("ev_per_1k")))
        // and the reconciler really distinguishes the sign: a stored +0 for that row is a conflict
        val plus = JSONArray(resp("readback_full").body).also { s -> (0 until s.length()).map { s.getJSONObject(it) }
            .single { it.getString("candidate_id") == "FX-NF-002" }.put("ev_per_1k", "0") }
        assertEquals(GeneratedCandidateOutbox.Outcome.CONFLICT,
            GeneratedCandidateReplay.replay(envelope, Script(resp("insert_repeat"), GeneratedCandidateReplay.Http(200, plus.toString()))).outcome)
        val a = GeneratedCandidateReplay.replay(envelope, Script(resp("insert_repeat"), resp("readback_full")))
        assertEquals(GeneratedCandidateReplay.State.ALREADY_COMMITTED_IDENTICAL, a.state)
    }

    @Test fun rlsAndAuthRejectionsRetainAndRetryNeverAck() {
        val rls = GeneratedCandidateReplay.replay(envelope, Script(resp("upsert_repeat")))
        assertEquals(GeneratedCandidateOutbox.Outcome.RETRY, rls.outcome)
        assertEquals(GeneratedCandidateReplay.State.RLS_OR_AUTH_REJECTED, rls.state)
        assertEquals(401, rls.http); assertEquals("42501", rls.pg)
        val auth = GeneratedCandidateReplay.replay(envelope, Script(resp("auth_insert")))
        assertEquals(GeneratedCandidateOutbox.Outcome.RETRY, auth.outcome); assertEquals(403, auth.http)
        // duplicate reported but the role cannot SELECT (authenticated read-back = []) -> never acknowledge
        val invisible = GeneratedCandidateReplay.replay(envelope, Script(resp("insert_repeat"), resp("auth_readback")))
        assertEquals(GeneratedCandidateReplay.State.READBACK_INVISIBLE, invisible.state)
        assertEquals(GeneratedCandidateOutbox.Outcome.RETRY, invisible.outcome)
        val net = GeneratedCandidateReplay.replay(envelope, Script(GeneratedCandidateReplay.Http(null, null, "UnknownHostException")))
        assertEquals(GeneratedCandidateOutbox.Outcome.RETRY, net.outcome)
        val rbFail = GeneratedCandidateReplay.replay(envelope, Script(resp("insert_repeat"), resp("upsert_repeat")))
        assertEquals(GeneratedCandidateOutbox.Outcome.RETRY, rbFail.outcome)
    }

    // ---------------- outbox integration (frozen bytes, meta, ordering, restart) ----------------

    private fun temp() = Files.createTempDirectory("candidate-replay").toFile()
    private fun pendingFile(root: File) = File(root, "generated_candidate_outbox_v1").listFiles()!!.single { it.name.endsWith(".json") }
    private fun fresh(ts: String) = JSONObject().put("poll_ts", ts).put("rows", JSONArray()
        .put(JSONObject().put("snapshot_poll_ts", ts).put("candidate_id", "fresh-1").put("was_surfaced", false))).toString()

    @Test fun rlsRejectionKeepsBytesAndEvidenceAndFreshPollsContinue() {
        val root = temp()
        try {
            assertTrue(GeneratedCandidateOutbox.enqueuePayload(root, envelope))
            val bytesBefore = pendingFile(root).readBytes()
            val s1 = GeneratedCandidateOutbox.drainDetailed(root, nowMs = 1_000L) { GeneratedCandidateReplay.replay(it, Script(resp("upsert_repeat"))) }
            assertEquals(1, s1.pending); assertEquals(1, s1.retried)
            assertArrayEquals(bytesBefore, pendingFile(root).readBytes())
            val meta = JSONObject(File(root, "generated_candidate_outbox_v1/${pendingFile(root).nameWithoutExtension}.meta").readText())
            assertEquals("RLS_OR_AUTH_REJECTED", meta.getString("last_state")); assertEquals(401, meta.getInt("last_http"))
            assertEquals("42501", meta.getString("last_pg")); assertEquals(1, meta.getInt("attempts"))
            assertEquals(GeneratedCandidateOutbox.identityOf(envelope).sha256, meta.getString("payload_sha256"))
            assertEquals(pollTs, meta.getString("poll_ts")); assertEquals(50, meta.getInt("rows"))
            assertFalse(meta.toString().contains("FX-NF-")) // identity + codes only, no row content
            assertTrue(s1.envelopes.single().contains("poll=$pollTs"))
            // a fresh poll is queued after; the retried old envelope was moved behind it
            Thread.sleep(15)
            val freshTs = "2026-10-21T10:35:00+05:30"
            GeneratedCandidateOutbox.enqueuePayload(root, fresh(freshTs))
            File(root, "generated_candidate_outbox_v1").listFiles()!!.first { it.name.endsWith(".json") && it.readText() == envelope }
                .setLastModified(System.currentTimeMillis() + 5_000)
            val order = mutableListOf<String>()
            val s2 = GeneratedCandidateOutbox.drainDetailed(root, nowMs = 2_000L) { p ->
                order += JSONObject(p).getString("poll_ts")
                if (p == envelope) GeneratedCandidateReplay.replay(p, Script(resp("upsert_repeat")))
                else GeneratedCandidateReplay.replay(p, Script(resp("insert_new")))
            }
            assertEquals(freshTs, order.first()); assertTrue(freshTs in s2.acknowledgedPolls); assertEquals(1, s2.pending)
            // then the server-side state is found already committed -> retained batch is acknowledged safely
            val s3 = GeneratedCandidateOutbox.drainDetailed(root, nowMs = 3_000L) { GeneratedCandidateReplay.replay(it, Script(resp("insert_repeat"), resp("readback_full"))) }
            assertEquals(0, s3.pending); assertTrue(pollTs in s3.acknowledgedPolls)
            assertFalse(GeneratedCandidateOutbox.isUnacknowledged(root, pollTs))
            val kept = File(root, "generated_candidate_outbox_v1/reconciled").listFiles()!!.filter { it.name.endsWith(".json") }
            assertEquals(1, kept.size); assertTrue(kept[0].name.endsWith("--ALREADY_COMMITTED_IDENTICAL.json"))
            assertArrayEquals(bytesBefore, kept[0].readBytes())
        } finally { root.deleteRecursively() }
    }

    @Test fun conflictIsArchivedWithBytesAndStaysUnacknowledged() {
        val root = temp()
        try {
            GeneratedCandidateOutbox.enqueuePayload(root, envelope)
            val s = GeneratedCandidateOutbox.drainDetailed(root) { GeneratedCandidateReplay.replay(it, Script(resp("insert_repeat"), resp("readback_conflict"))) }
            assertEquals(1, s.conflicts); assertEquals(0, s.pending); assertEquals(0, s.acknowledged)
            assertTrue(GeneratedCandidateOutbox.isUnacknowledged(root, pollTs))
            val q = File(root, "generated_candidate_outbox_v1/quarantine").listFiles()!!
            val bytes = q.single { it.name.endsWith("--content_conflict.json") }
            assertEquals(envelope, bytes.readText())
            val meta = JSONObject(q.single { it.name.endsWith(".meta") }.readText())
            assertEquals("CONFLICT", meta.getString("last_state")); assertEquals("ev_per_1k", meta.getJSONArray("conflict_columns").getString(0))
        } finally { root.deleteRecursively() }
    }

    @Test fun restartDoesNotChangeFrozenIdentityAndRetriesAreBoundedNeverDropped() {
        val root = temp()
        try {
            GeneratedCandidateOutbox.enqueuePayload(root, envelope)
            val id = GeneratedCandidateOutbox.identityOf(pendingFile(root).readText())
            assertFalse(GeneratedCandidateOutbox.enqueuePayload(root, envelope)) // re-freeze after restart is a no-op
            var now = 10_000L
            var calls = 0
            repeat(GeneratedCandidateOutbox.STUCK_ATTEMPTS) {
                GeneratedCandidateOutbox.drainDetailed(root, nowMs = now) { calls++; GeneratedCandidateReplay.replay(it, Script(resp("upsert_repeat"))) }
                now += 1_000L
            }
            assertEquals(GeneratedCandidateOutbox.STUCK_ATTEMPTS, calls)
            // "restart": nothing in memory survives; identity is recomputed from the bytes on disk
            assertEquals(id, GeneratedCandidateOutbox.identityOf(pendingFile(root).readText()))
            val deferred = GeneratedCandidateOutbox.drainDetailed(root, nowMs = now) { calls++; error("must be backed off") }
            assertEquals(1, deferred.deferred); assertEquals(1, deferred.stuck.size); assertEquals(1, deferred.pending)
            assertTrue(deferred.stuck[0].contains("sha256=${id.sha256.take(16)}"))
            val later = GeneratedCandidateOutbox.drainDetailed(root, nowMs = now + GeneratedCandidateOutbox.backoffMs(GeneratedCandidateOutbox.STUCK_ATTEMPTS)) {
                calls++; GeneratedCandidateReplay.replay(it, Script(resp("insert_repeat"), resp("readback_full")))
            }
            assertEquals(GeneratedCandidateOutbox.STUCK_ATTEMPTS + 1, calls); assertEquals(0, later.pending)
            assertTrue(GeneratedCandidateOutbox.describeRetained(root).isEmpty())
            assertEquals(30L * 60_000, GeneratedCandidateOutbox.backoffMs(100))
        } finally { root.deleteRecursively() }
    }

    @Test fun retainedInventoryIsSafeAndIdentified() {
        val root = temp()
        try {
            GeneratedCandidateOutbox.enqueuePayload(root, envelope)
            val line = GeneratedCandidateOutbox.describeRetained(root).single()
            assertTrue(line.startsWith("poll=$pollTs session=2026-10-19 sha256=")); assertTrue(line.contains("rows=50"))
            assertFalse(line.contains("FX-"))
        } finally { root.deleteRecursively() }
    }

    @Test fun sourceNoLongerUpsertsGeneratedCandidatesAndUsesNoServiceKey() {
        fun src(n: String) = listOf(File("src/main/java/com/marketradar/app/$n"), File("app/src/main/java/com/marketradar/app/$n")).first { it.isFile }.readText()
        val supa = src("SupabaseClient.kt")
        assertFalse(supa.contains("ml_generated_candidates?on_conflict"))
        assertFalse(supa.contains("uploadGeneratedCandidateRows"))
        assertTrue(src("MarketWatchService.kt").contains("SupabaseClient.replayGeneratedCandidateEnvelope(payload)"))
        assertTrue(src("MarketWatchService.kt").contains("ML_GENERATED_RETAINED"))
        val replay = src("GeneratedCandidateReplay.kt")
        assertFalse(replay.contains("merge-duplicates\""))
        listOf(supa, replay).forEach { assertFalse(it.contains("service_role")) }
    }
}
