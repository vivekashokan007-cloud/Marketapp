package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * b513 regression suite for the 21 Oct 2026 evening identity-verification failure (Codex brief sections A–D).
 * Behavioural: a fake PostgREST (via SupabaseClient.pageFetchSeam) serves the real 21 Oct shape — 75 labelable
 * snapshots, 75 primary + 18675 secondary evaluation rows, 75 primary companions, 888 rejected rows = 19638 — through
 * the production readEvaluationOutcomeIdentityKeys / Bounded / EvaluationCertification code.
 */
class B513EvalIdentityReadbackTest {
    private val date = "2026-10-21"
    private val cutoff = "2026-10-21T12:26:00Z"
    private val SHA = "a".repeat(64)
    private val CONTRACT = "c".repeat(64)

    data class Call(val table: String, val filter: String, val offset: Int)

    /** Fake server. failures: (table, pageIndex, attempt#) -> PageResult to return instead of data. */
    inner class FakeServer {
        val tables = mutableMapOf<String, MutableList<JSONObject>>()
        val calls = mutableListOf<Call>()
        var failure: ((table: String, page: Int, call: Int) -> SupabaseClient.PageResult?)? = null
        private val perTablePageCalls = mutableMapOf<Pair<String, Int>, Int>()

        fun add(table: String, sid: Int, cid: String, role: String, createdAt: String = "2026-10-21T12:00:00Z") {
            tables.getOrPut(table) { mutableListOf() }.add(JSONObject().put("snapshot_id", sid).put("candidate_id", cid)
                .put("role", role).put("session_date", date).put("created_at", createdAt))
        }

        fun install() {
            SupabaseClient.pageFetchSeam = { table, filter, _, limit, offset ->
                val f = filter.orEmpty(); val off = offset ?: 0; val lim = limit ?: 500
                calls += Call(table, f, off)
                val page = off / lim
                val n = perTablePageCalls.merge(table to page, 1, Int::plus)!!
                failure?.invoke(table, page, n) ?: run {
                    val cut = Regex("created_at=lte\\.([^&]+)").find(f)?.groupValues?.get(1)
                    val roleEq = Regex("role=eq\\.([a-z]+)").find(f)?.groupValues?.get(1)
                    val rows = (tables[table] ?: mutableListOf())
                        .filter { roleEq == null || it.getString("role") == roleEq }
                        .filter { cut == null || it.getString("created_at") <= cut }
                        .sortedWith(compareBy({ it.getInt("snapshot_id") }, { it.getString("candidate_id") }, { it.getString("role") }))
                    val arr = JSONArray(); rows.drop(off).take(lim).forEach { arr.put(JSONObject().put("snapshot_id", it.get("snapshot_id")).put("candidate_id", it.get("candidate_id")).put("role", it.get("role"))) }
                    SupabaseClient.PageResult(status = "success", rows = arr, httpCode = 200)
                }
            }
        }
    }

    // ---- the 21 Oct shape ----
    private val expectedSnapshots = (1..75).map(Int::toString)
    private val primaryKeys = (1..75).map { "$it|p$it|primary" }
    private val producedKeys: Set<String> = buildSet {
        for (s in 1..75) {
            add("$s|p$s|primary")
            for (j in 0 until 249) add("$s|s${s}_${"%03d".format(j)}|secondary")
            val rej = if (s <= 63) 12 else 11
            for (j in 0 until rej) add("$s|r${s}_${"%02d".format(j)}|rejected")
        }
    }

    private fun fullServer(): FakeServer = FakeServer().also { srv ->
        producedKeys.forEach { k ->
            val (s, c, r) = k.split("|")
            val table = if (r == "rejected") "ml_rejected_candidate_outcomes" else "ml_evaluation_outcomes"
            srv.add(table, s.toInt(), c, r)
            if (r == "primary") srv.add("ml_recommendation_outcomes", s.toInt(), c, r)
        }
    }

    private val sleeps = mutableListOf<Long>()

    @Before fun noSleep() { SupabaseClient.readbackSleepSeam = { sleeps += it } }
    @After fun reset() { SupabaseClient.resetPageFetchSeam(); SupabaseClient.readbackSleepSeam = null }

    private fun http500Blank(code: String) = SupabaseClient.PageResult(status = "http_error", httpCode = 500,
        error = EvaluationReadbackDiagnostics.describeHttpFailure(500, "", "{\"code\":\"$code\",\"message\":\"canceling statement due to statement timeout\"}"),
        body = "{\"code\":\"$code\",\"message\":\"canceling statement due to statement timeout\"}")

    private fun certify(rb: SupabaseClient.OutcomeIdentityReadback, produced: Collection<String> = producedKeys) =
        EvaluationCertification.certify(expectedSnapshots, produced, primaryKeys, rb)

    @Test fun shapeSanity() {
        assertEquals(19638, producedKeys.size)
        val srv = fullServer(); srv.install()
        val rb = SupabaseClient.readEvaluationOutcomeIdentityKeys(date, includeRejected = true, cutoffIso = cutoff)
        assertTrue(rb.error, rb.ok)
        assertEquals(19638, rb.compositeKeys.size)
        assertEquals(75, rb.recommendationCompositeKeys.size)
        assertEquals(38 + 1 + 2, rb.pagesFetched)
        assertTrue(certify(rb).complete)
    }

    // A1
    @Test fun httpErrorWithBlankReasonButStructuredCodeIsVisibleAndPrivacySafe() {
        val body = "{\"code\":\"57014\",\"message\":\"canceling statement\",\"details\":\"row 5|p5|primary secret-token\"}"
        val s = EvaluationReadbackDiagnostics.describeHttpFailure(500, "", body)
        assertEquals("HTTP_500 code=57014", s)
        assertFalse(s.contains("secret") || s.contains("p5"))
        assertEquals("HTTP_503", EvaluationReadbackDiagnostics.describeHttpFailure(503, null, "<html>gateway</html>"))

        val srv = fullServer(); srv.install()
        srv.failure = { t, p, _ -> if (t == "ml_evaluation_outcomes" && p == 36) http500Blank("57014") else null }
        val rb = SupabaseClient.readEvaluationOutcomeIdentityKeys(date, true, cutoff)
        assertFalse(rb.ok)
        val err = rb.error.orEmpty()
        assertTrue(err, err.isNotBlank())
        assertTrue(err, err.contains("class=query_statement_timeout_57014") && err.contains("http=500") && err.contains("code=57014"))
        assertFalse(err, Regex("secret|token|apikey|Bearer|https?://|\\|p\\d+\\|").containsMatchIn(err))
    }

    // A2
    @Test fun ioExceptionWithNullOrBlankMessageRetainsExceptionClass() {
        assertEquals("SocketTimeoutException:no_message", EvaluationReadbackDiagnostics.describeException(java.net.SocketTimeoutException()))
        assertEquals("IOException:no_message", EvaluationReadbackDiagnostics.describeException(java.io.IOException("   ")))
        assertEquals("UnknownHostException:Unable to resolve host x.supabase.co",
            EvaluationReadbackDiagnostics.describeException(java.net.UnknownHostException("Unable to resolve host \"x.supabase.co\"")))
        val srv = fullServer(); srv.install()
        srv.failure = { t, p, _ -> if (t == "ml_evaluation_outcomes" && p == 2)
            SupabaseClient.PageResult(status = "http_error", error = "SocketTimeoutException:no_message", exceptionClass = "SocketTimeoutException") else null }
        val rb = SupabaseClient.readEvaluationOutcomeIdentityKeys(date, true, cutoff)
        assertEquals("transport_SocketTimeoutException", rb.failure!!.classification)
        assertEquals("SocketTimeoutException", rb.failure!!.exceptionClass)
    }

    // A3
    @Test fun failureOnPageNRetainsSourcePageStatusAndNoCompletionFlags() {
        val srv = fullServer(); srv.install()
        srv.failure = { t, p, _ -> if (t == "ml_evaluation_outcomes" && p == 17) SupabaseClient.PageResult(status = "http_error", httpCode = 401,
            error = "HTTP_401 code=PGRST301", body = "{\"code\":\"PGRST301\"}") else null }
        val rb = SupabaseClient.readEvaluationOutcomeIdentityKeys(date, true, cutoff)
        val f = rb.failure!!
        assertEquals("evaluation", f.stage); assertEquals("ml_evaluation_outcomes", f.table)
        assertEquals(17, f.failedPage); assertEquals(8500, f.failedOffset); assertEquals(8500, f.rowsRead)
        assertEquals(18, f.pagesFetched); assertEquals(401, f.httpStatus); assertEquals("auth_jwt_PGRST301", f.classification)
        assertFalse(f.retryable)
        val a = certify(rb)
        assertFalse(a.complete)
        val t = EvaluationIdentityCoverage.transitionFor(a)
        assertFalse(t.labelsSaved || t.writeEvaluationDoneDate || t.publishLabelsSaved || t.startC3 || t.logEvaluationComplete || t.clearRecoveryState)
        assertEquals(EvaluationIdentityCoverage.PHASE_FAILED_IDENTITY_COVERAGE, t.phase)
        assertTrue(t.scheduleContinuation)
    }

    // A4
    @Test fun evaluationVsPrimaryCompanionVsRejectedFailuresAreDistinguishable() {
        val stages = listOf("ml_evaluation_outcomes", "ml_recommendation_outcomes", "ml_rejected_candidate_outcomes").map { table ->
            val srv = fullServer(); srv.install()
            srv.failure = { t, p, _ -> if (t == table && p == 0) http500Blank("57014") else null }
            val rb = SupabaseClient.readEvaluationOutcomeIdentityKeys(date, true, cutoff)
            assertFalse(rb.ok)
            assertEquals(500, rb.httpStatus); assertEquals(0, rb.failedPage)
            rb.failure!!.stage to rb.error!!
        }
        assertEquals(listOf("evaluation", "primary_companion", "rejected"), stages.map { it.first })
        assertTrue(stages[2].second.contains("table=ml_rejected_candidate_outcomes") && stages[2].second.contains("http=500"))
        assertEquals(3, stages.map { it.second }.toSet().size)
    }

    // B/C: same frozen boundary on every stage and attempt
    @Test fun sameDateResumeFrom78Of78UsesUnchangedLocalExpectedIdentities() {
        val srv = fullServer(); srv.install()
        val frozenProduced = producedKeys.toSet()
        val digestBefore = EvaluationCertification.keysDigest(frozenProduced)
        // two retries of the same session (17:56 and 18:15) against the same validated stream
        val first = EvaluationCertification.UploadAck(date, 19638, 148205716L, digestBefore, SHA, CONTRACT, persistedCount = 19638)
        val resumed = EvaluationCertification.UploadAck(date, 19638, 148205716L, EvaluationCertification.keysDigest(producedKeys.shuffled()), SHA, CONTRACT)
        assertTrue(EvaluationCertification.shouldVerifyFirst(first, resumed))
        assertFalse(EvaluationCertification.shouldVerifyFirst(first, resumed.copy(sessionDate = "2026-10-22")))
        assertFalse(EvaluationCertification.shouldVerifyFirst(first, resumed.copy(keysDigest = EvaluationCertification.keysDigest(producedKeys - "1|p1|primary"))))
        assertFalse(EvaluationCertification.shouldVerifyFirst(null, resumed))
        val rb = SupabaseClient.readEvaluationOutcomeIdentityKeysBounded(date, true, cutoff)
        assertTrue(certify(rb, frozenProduced).complete)
        assertEquals(digestBefore, EvaluationCertification.keysDigest(frozenProduced))
        assertTrue(srv.calls.all { it.filter.contains("session_date=eq.$date") && it.filter.contains("created_at=lte.$cutoff") })
    }

    // B: exact composite, not counts
    @Test fun countsEqualButOneExpectedCompositeDiffersStillFails() {
        val srv = fullServer()
        srv.tables["ml_evaluation_outcomes"]!!.first { it.getString("candidate_id") == "s40_100" }.put("candidate_id", "s40_ZZZ")
        srv.install()
        val rb = SupabaseClient.readEvaluationOutcomeIdentityKeys(date, true, cutoff)
        assertTrue(rb.ok); assertEquals(19638, rb.compositeKeys.size)
        val a = certify(rb)
        assertFalse(a.complete)
        assertTrue(a.missingCompositeKeys.contains("40|s40_100|secondary"))
        assertFalse(EvaluationIdentityCoverage.transitionFor(a).labelsSaved)
    }

    @Test fun missingPrimaryCompanionStillFails() {
        val srv = fullServer(); srv.tables["ml_recommendation_outcomes"]!!.removeAt(10); srv.install()
        val a = certify(SupabaseClient.readEvaluationOutcomeIdentityKeys(date, true, cutoff))
        assertFalse(a.complete); assertTrue(a.missingPreview.any { it.startsWith("reco:") })
    }

    // B: hidden / partial / incomplete -> fails and keeps recovery state
    @Test fun readbackIncompleteHiddenOrPartialStillFailsAndRetainsRecovery() {
        // partial: rows created after the frozen cutoff are invisible (late/hidden) -> missing, not success
        val srv = fullServer()
        srv.tables["ml_rejected_candidate_outcomes"]!!.take(5).forEach { it.put("created_at", "2026-10-21T13:00:00Z") }
        srv.install()
        val rb = SupabaseClient.readEvaluationOutcomeIdentityKeys(date, true, cutoff)
        assertTrue(rb.ok)
        val a = certify(rb)
        assertFalse(a.complete); assertEquals(5, a.missingCount)
        val t = EvaluationIdentityCoverage.transitionFor(a)
        assertFalse(t.clearRecoveryState); assertFalse(t.writeEvaluationDoneDate); assertTrue(t.scheduleContinuation)
        // RLS-hidden: empty companion table while primaries exist
        val srv2 = fullServer(); srv2.tables.remove("ml_recommendation_outcomes"); srv2.install()
        val a2 = certify(SupabaseClient.readEvaluationOutcomeIdentityKeys(date, true, cutoff))
        assertFalse(a2.complete)
    }

    // C: transient failure recovers without recompute, same boundary
    @Test fun transientReadFailureRecoversWithoutRecomputeOrBoundaryChange() {
        val srv = fullServer(); srv.install()
        srv.failure = { t, p, n -> if (t == "ml_evaluation_outcomes" && p == 30 && n == 1) http500Blank("57014") else null }
        val logged = mutableListOf<String>()
        val rb = SupabaseClient.readEvaluationOutcomeIdentityKeysBounded(date, true, cutoff, maxAttempts = 3, backoffMs = 2000) { a, f -> logged += "$a:${f!!.classification}" }
        assertTrue(rb.error, rb.ok); assertEquals(2, rb.attempts); assertEquals(cutoff, rb.cutoffIso)
        assertEquals(listOf("1:query_statement_timeout_57014"), logged); assertEquals(listOf(2000L), sleeps)
        assertEquals(setOf(cutoff), srv.calls.map { Regex("created_at=lte\\.([^&]+)").find(it.filter)!!.groupValues[1] }.toSet())
        assertTrue(srv.calls.none { it.table.contains("insert") })
        assertTrue(certify(rb).complete)
    }

    @Test fun nonTransientOrPersistentFailureIsBoundedAndNotRetriedBlindly() {
        val srv = fullServer(); srv.install()
        srv.failure = { t, _, _ -> if (t == "ml_evaluation_outcomes") SupabaseClient.PageResult("http_error", httpCode = 403, error = "HTTP_403 code=42501", body = "{\"code\":\"42501\"}") else null }
        val rb = SupabaseClient.readEvaluationOutcomeIdentityKeysBounded(date, true, cutoff, maxAttempts = 3)
        assertEquals(1, rb.attempts); assertTrue(sleeps.isEmpty()); assertEquals("auth_permission_42501", rb.failure!!.classification)
        val srv2 = fullServer(); srv2.install()
        srv2.failure = { t, p, _ -> if (t == "ml_evaluation_outcomes" && p == 5) http500Blank("57014") else null }
        val rb2 = SupabaseClient.readEvaluationOutcomeIdentityKeysBounded(date, true, cutoff, maxAttempts = 3)
        assertFalse(rb2.ok); assertEquals(3, rb2.attempts); assertEquals(3, srv2.calls.count { it.table == "ml_evaluation_outcomes" && it.offset == 2500 })
    }

    // C: exact success resumes later stages once
    @Test fun exactVerifyFirstSuccessResumesLaterStagesOnceWithoutReupload() {
        val srv = fullServer(); srv.install()
        val ack = EvaluationCertification.UploadAck(date, 19638, 148205716L, EvaluationCertification.keysDigest(producedKeys),
            SHA, CONTRACT, persistedCount = 19638, primaryPersistedCount = 75, evaluationPersistedCount = 18750, rejectedPersistedCount = 888)
        assertTrue(EvaluationCertification.shouldVerifyFirst(ack, ack.copy(persistedCount = 0)))
        val rb = SupabaseClient.readEvaluationOutcomeIdentityKeysBounded(date, true, cutoff)
        val a = certify(rb)
        assertTrue(a.complete)
        val save = EvaluationCertification.saveResultFromAck(ack, 19638)
        assertTrue(save.success); assertEquals(19638, save.persistedCount); assertTrue(save.message.contains("no re-upload"))
        val t = EvaluationIdentityCoverage.transitionFor(a)
        // later stages are entered exactly once: this transition writes evaluation_done_date, and runDayEvaluation
        // returns EVAL_SKIP for a session whose done date is already written (no retrain / no second notification).
        assertTrue(t.writeEvaluationDoneDate && t.labelsSaved && t.startC3)
        assertTrue(srv.calls.all { it.table in setOf("ml_evaluation_outcomes", "ml_recommendation_outcomes", "ml_rejected_candidate_outcomes") })
        val src = java.io.File("src/main/java/com/marketradar/app/MarketMLService.kt").readText()
        assertTrue(src.contains("prefs.getString(\"evaluation_done_date\", null) == sessionDate") && src.contains("EVAL_SKIP: already done"))
    }

    // budget
    @Test fun moreThanBudgetInputReportsTruncationHonestly() {
        val srv = fullServer()
        for (j in 0 until 1500) srv.add("ml_evaluation_outcomes", 76, "x_${"%04d".format(j)}", "secondary")
        srv.install()
        val rb = SupabaseClient.readEvaluationOutcomeIdentityKeysBounded(date, true, cutoff)
        assertFalse(rb.ok); assertEquals(1, rb.attempts)
        assertEquals("incomplete_truncated", rb.status)
        assertEquals("budget_truncated", rb.failure!!.classification)
        assertEquals(20000, rb.failure!!.rowsRead)
        assertTrue(rb.error!!.contains("class=budget_truncated"))
        assertFalse(certify(rb).complete)
    }

    @Test fun honestStatusSaysRowsWrittenButVerificationFailed() {
        val srv = fullServer(); srv.install()
        srv.failure = { t, p, _ -> if (t == "ml_evaluation_outcomes" && p == 1) http500Blank("57014") else null }
        val rb = SupabaseClient.readEvaluationOutcomeIdentityKeysBounded(date, true, cutoff, maxAttempts = 1)
        val line = EvaluationCertification.honestStatusLine(date, 19638, rb, certify(rb))
        assertTrue(line, line.contains("rowsWrittenAck=19638") && line.contains("verification=FAILED") &&
            line.contains("verifiedIdentities=unknown") && line.contains("labelsSaved=false"))
        assertFalse(line.contains("missing="))
        assertNotNull(rb.failure)
        srv.failure = null
        val ok = SupabaseClient.readEvaluationOutcomeIdentityKeys(date, true, cutoff)
        assertNull(ok.failure)
        assertTrue(EvaluationCertification.honestStatusLine(date, 19638, ok, certify(ok)).contains("verification=EXACT verified=19638"))
    }
}
