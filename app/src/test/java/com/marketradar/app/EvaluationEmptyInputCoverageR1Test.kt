package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.time.Instant

/**
 * b511 R1 (Codex P1): a zero-count prepared input (failed remote payload fetch + empty bounded local
 * fallback) must not skip the remote identity lookup and reach DONE "no brain snapshots found".
 *
 * There is no Robolectric here, so the Android Service cannot be instantiated. These tests drive the
 * service's real coverage window (EvaluationInputCoverageGate + the real SnapshotReconciler) through a
 * host with real files and a prefs map, in the service's order and with its catch semantics; the last
 * test pins that wiring/order in MarketMLService.kt itself.
 */
class EvaluationEmptyInputCoverageR1Test {
    @get:Rule val tmp = TemporaryFolder()
    private val date = "2026-10-13"
    private val start = Instant.parse("2026-10-13T03:45:04Z")
    private fun snap(i: Int, withId: Boolean = true) = JSONObject()
        .put("id", if (withId) 7066L + i else JSONObject.NULL)
        .put("session_date", date)
        .put("poll_ts", start.plusSeconds(300L * i).toString())
        .put("recommendation_id", "rec-$i")
    private fun remoteOf(n: Int) = JSONArray().apply { for (i in 0 until n) put(snap(i)) }

    /** Models MarketMLService.runDayEvaluation from input preparation to the first scoring call. */
    private inner class ServiceWindow(
        private val remote: () -> JSONArray,
        local: List<JSONObject>,
        lastPollDate: String = "2026-10-17",
        pollCount: Int = 78,
        private val coverageIntegrity: String = "OK"
    ) : EvaluationInputCoverageGate.Host {
        val dir: File = tmp.newFolder()
        val marker = File(dir, "evaluation_prepare_complete_$date.json").apply { writeText("{\"snapshot_count\":${local.size}}") }
        val snapshotsFile = File(dir, "evaluation_snapshots_$date.json").apply { writeText(JSONArray(local).toString()) }
        val chainFile = File(dir, "evaluation_chain_$date.json").apply { writeText("[]") }
        private val preparedCount = local.size
        val prefs = mutableMapOf<String, Any>("last_poll_date" to lastPollDate, "poll_count" to pollCount)
        var fetchCalls = 0; var pythonPrepareCalls = 0; var scoringCalls = 0; var labelsSaved = 0; var c3Runs = 0
        var phase = ""; var lastError: String? = null; var retryIntentDate: String? = null; var preserveRecoveryState = false
        val logs = mutableListOf<String>()

        override fun fetchRemoteIdentities(): JSONArray { fetchCalls += 1; return remote() }
        override fun persistReplay(snapshot: JSONObject) = false
        override fun resolvePreparedInput(reconciler: EvaluationIdentity.SnapshotReconciler): Set<Long> {
            val rows = JSONArray(snapshotsFile.readText())
            return (0 until rows.length()).map { reconciler.resolve(rows.getJSONObject(it)) }.toCollection(linkedSetOf())
        }
        override fun invalidatePreparedMarker() { marker.delete() }
        override fun log(level: Char, message: String) { logs += "$level $message" }

        fun run(forceAnyway: Boolean = false) {
            prefs["evaluation_job_date"] = date                        // updateEvaluationJobState(PREPARING)
            try {
                val coverage = EvaluationInputCoverageGate.verify(date, preparedCount, this)
                pythonPrepareCalls += 1                                // brain.evaluation_job_prepare
                val totalSnapshots = preparedCount                     // Python echoes the prepared rows (0 for [] input)
                if (totalSnapshots == 0) {
                    when (EvaluationInputCoverageGate.decideZeroSnapshots(
                        date, coverage, coverageIntegrity, forceAnyway, prefs["last_poll_date"] as String, prefs["poll_count"] as Int
                    )) {
                        EvaluationInputCoverageGate.ZeroSnapshotDecision.BLOCKED_INCOMPLETE_SESSION -> { phase = "INCOMPLETE_SESSION"; return }
                        EvaluationInputCoverageGate.ZeroSnapshotDecision.FAIL_POLLED_WITHOUT_SNAPSHOTS ->
                            throw IllegalStateException("EVAL_NO_SNAPSHOTS_AFTER_POLLING")
                        EvaluationInputCoverageGate.ZeroSnapshotDecision.DONE_EMPTY -> {
                            prefs["evaluation_done_date"] = date; phase = "DONE"; return  // "no brain snapshots found"
                        }
                    }
                }
                scoringCalls += 1; labelsSaved += 1; c3Runs += 1
                prefs["evaluation_done_date"] = date; phase = "DONE"
            } catch (e: Throwable) {                                   // service catch: retryable, state preserved
                preserveRecoveryState = true
                phase = "FAILED"; lastError = e.message
                retryIntentDate = date                                 // publishEvaluationStatus(sessionDate, allowRetry = true)
            }
        }

        fun assertStoppedBeforeScoring(code: String) {
            assertEquals("FAILED", phase)
            assertTrue("lastError=$lastError", lastError!!.startsWith(code))
            assertEquals(0, pythonPrepareCalls); assertEquals(0, scoringCalls); assertEquals(0, labelsSaved); assertEquals(0, c3Runs)
            assertNull("must not record DONE", prefs["evaluation_done_date"])
            assertFalse("prepared marker must be invalidated so retry refetches", marker.exists())
            assertTrue("recovery inputs retained", snapshotsFile.exists() && chainFile.exists())
            assertEquals(date, prefs["evaluation_job_date"]); assertEquals(date, retryIntentDate)
            assertTrue(preserveRecoveryState)
        }
    }

    @Test fun remoteNonEmpty_emptyLocalFallback_lastPollOtherDate_failsBeforePythonLabelsC3AndDone() {
        val w = ServiceWindow(remote = { remoteOf(72) }, local = emptyList(), lastPollDate = "2026-10-17", pollCount = 78)
        w.run()
        assertTrue("remote identity lookup must run for empty input", w.fetchCalls >= 1)
        w.assertStoppedBeforeScoring("EVAL_INPUT_INCOMPLETE: missing=72")
        assertTrue(w.logs.any { it.contains("EVAL_INPUT_INCOMPLETE: date=$date input=0 missing=72") })
    }

    @Test fun identityLookupFailureOnEmptyInput_isUnverifiedAndRetryable() {
        val w = ServiceWindow(remote = { throw IllegalStateException("EVAL_IDENTITY_LOOKUP_FAILED: local inputs and results retained") }, local = emptyList())
        w.run()
        w.assertStoppedBeforeScoring("EVAL_INPUT_UNVERIFIED")
        assertTrue(w.lastError!!.contains("EVAL_IDENTITY_LOOKUP_FAILED"))
    }

    @Test fun identityLookupCappedOnEmptyInput_isUnverifiedAndRetryable() {
        val w = ServiceWindow(remote = { throw IllegalStateException("EVAL_IDENTITY_LOOKUP_CAPPED: local inputs and results retained") }, local = emptyList())
        w.run()
        w.assertStoppedBeforeScoring("EVAL_INPUT_UNVERIFIED")
        assertTrue(w.lastError!!.contains("EVAL_IDENTITY_LOOKUP_CAPPED"))
    }

    @Test fun identityLookupFailureOnNonEmptyInput_isUnverifiedAndRetryable() {
        val w = ServiceWindow(remote = { throw IllegalStateException("EVAL_IDENTITY_LOOKUP_FAILED: x") }, local = listOf(snap(0, false)))
        w.run()
        w.assertStoppedBeforeScoring("EVAL_INPUT_UNVERIFIED")
    }

    @Test fun verifiedRemoteEmpty_emptyLocal_completesNormally() {
        val w = ServiceWindow(remote = { JSONArray() }, local = emptyList())
        w.run()
        assertEquals("DONE", w.phase); assertEquals(1, w.fetchCalls); assertEquals(1, w.pythonPrepareCalls)
        assertEquals(0, w.scoringCalls); assertEquals(0, w.labelsSaved); assertEquals(0, w.c3Runs)
        assertEquals(date, w.prefs["evaluation_done_date"]); assertTrue(w.marker.exists())
    }

    @Test fun verifiedRemoteEmpty_butPolledToday_stillFailsClosed() {
        val w = ServiceWindow(remote = { JSONArray() }, local = emptyList(), lastPollDate = date, pollCount = 5)
        w.run()
        assertEquals("FAILED", w.phase); assertNull(w.prefs["evaluation_done_date"])
    }

    @Test fun nonEmptyCompleteInputProceedsToScoring_andTruncatedStillBlocks() {
        val ok = ServiceWindow(remote = { remoteOf(3) }, local = (0 until 3).map { snap(it, false) })
        ok.run()
        assertEquals("DONE", ok.phase); assertEquals(1, ok.scoringCalls); assertTrue(ok.marker.exists())
        val cut = ServiceWindow(remote = { remoteOf(72) }, local = (37 until 72).map { snap(it, false) })
        cut.run()
        cut.assertStoppedBeforeScoring("EVAL_INPUT_INCOMPLETE: missing=37")
    }

    @Test fun zeroSnapshotDoneRequiresVerifiedRemoteEmpty() {
        // Python reporting 0 snapshots for a non-empty, covered input must never finish empty.
        val notEmpty = EvaluationInputCoverageGate.Verified(setOf(7066L), remoteEmpty = false)
        try {
            EvaluationInputCoverageGate.decideZeroSnapshots(date, notEmpty, "OK", false, "2026-10-17", 0)
            fail("zero-snapshot DONE accepted without a verified-empty remote lookup")
        } catch (e: IllegalStateException) {
            assertTrue(e.message!!.startsWith("EVAL_INPUT_UNVERIFIED"))
        }
    }

    @Test fun serviceWiring_gateIsUnconditionalAndPrecedesScoringAndEveryEmptyDone() {
        val s = listOf(File("src/main/java/com/marketradar/app/MarketMLService.kt"),
            File("app/src/main/java/com/marketradar/app/MarketMLService.kt")).first { it.exists() }.readText()
        val prep = s.indexOf("val preparedInputs = ensureEvaluationInputFiles(sessionDate)")
        val gate = s.indexOf("EvaluationInputCoverageGate.verify(", prep)
        assertTrue(prep > 0 && gate > prep)
        val window = s.substring(prep, gate)
        assertFalse("gate must not be conditional on snapshotCount", Regex("""snapshotCount\s*>\s*0\)\s*\{\s*$""").containsMatchIn(window.lines().takeLast(4).joinToString("\n")))
        for (marker in listOf("EvaluationRunLedger.beginOrResume(", "\"evaluation_job_prepare\"", "EvaluationInputCoverageGate.decideZeroSnapshots(",
                "no brain snapshots found", "no evaluable candidate legs were captured")) {
            assertTrue("$marker must come after the coverage gate", s.indexOf(marker, prep) > gate)
        }
        val zeroDone = s.indexOf("no brain snapshots found")
        assertTrue(s.lastIndexOf("EvaluationInputCoverageGate.decideZeroSnapshots(", zeroDone) > s.lastIndexOf("if (totalSnapshots == 0) {", zeroDone))
        val catchBlock = s.substring(s.indexOf("} catch (e: Throwable) {\n            preserveRecoveryState = true"))
            .substringBefore("} finally {")
        assertTrue(catchBlock.contains("allowRetry = true") && catchBlock.contains("sessionDate = sessionDate"))
        assertFalse(catchBlock.contains("evaluation_done_date"))
        val host = s.substringAfter("private fun evaluationInputGateHost(").substringBefore("private fun resolveEvaluationSnapshotIds(")
        assertTrue(host.contains("SupabaseClient.fetchEvaluationSnapshotIdentities(sessionDate)"))
        assertTrue(host.contains("File(evaluationPrepareCompletePath(this@MarketMLService, sessionDate)).delete()"))
    }
}
