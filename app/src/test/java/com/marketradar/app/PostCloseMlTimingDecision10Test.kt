package com.marketradar.app

import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Owner decision 10 (26 Sep 2026): instrument post-close ML durations first;
 * Completed(value?) / TimedOut for train_temporal; no limit activated/changed.
 * Real-behaviour note: ML training is not an order path; Real and Paper alike
 * only gain a log line + bounded prefs sample.
 */
class PostCloseMlTimingDecision10Test {
    @Test
    fun completedWithNullIsDistinctFromTimedOut() = runBlocking {
        val nul = runTimedMl<String>(1_000L) { null }
        assertTrue(nul is TimedMlResult.Completed)
        assertNull((nul as TimedMlResult.Completed).value)
        assertEquals(POST_CLOSE_ML_OUTCOME_COMPLETED_NULL, nul.outcomeLabel())

        val ok = runTimedMl(1_000L) { "model" }
        assertEquals("model", (ok as TimedMlResult.Completed).value)
        assertEquals(POST_CLOSE_ML_OUTCOME_COMPLETED, ok.outcomeLabel())

        val slow = runTimedMl(50L) { delay(5_000L); "late" }
        assertTrue(slow is TimedMlResult.TimedOut)
        assertEquals(50L, (slow as TimedMlResult.TimedOut).limitMs)
        assertEquals(POST_CLOSE_ML_OUTCOME_TIMED_OUT, slow.outcomeLabel())
    }

    @Test
    fun elapsedIsMeasuredWithInjectedClock() = runBlocking {
        var t = 0L
        val r = runTimedMl(1_000L, nanoClock = { val v = t; t += 7_000_000L; v }) { 1 }
        assertEquals(7L, r.elapsedMs)
    }

    @Test
    fun percentilesAreNearestRankOverCompletedOnly() {
        var json: String? = null
        val days = listOf("2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25")
        for (i in 1..20) {
            json = appendMlDurationSample(json, "train_temporal", days[i % 5], i * 100L,
                POST_CLOSE_ML_OUTCOME_COMPLETED, 45_000L)
        }
        json = appendMlDurationSample(json, "train_temporal", "2026-09-25", 45_000L, POST_CLOSE_ML_OUTCOME_TIMED_OUT, 45_000L)
        json = appendMlDurationSample(json, "online_update", "2026-09-25", 9L, POST_CLOSE_ML_OUTCOME_COMPLETED, 30_000L)
        val s = summarizeMlDurations(json, "train_temporal")
        assertEquals(20, s.completedCount)
        assertEquals(1, s.timedOutCount)
        assertEquals(5, s.sessions)
        assertEquals(1_000L, s.p50Ms)   // rank ceil(0.5*20)=10 -> 1000
        assertEquals(1_900L, s.p95Ms)   // rank ceil(0.95*20)=19 -> 1900
        assertEquals(1, summarizeMlDurations(json, "online_update").completedCount)
        assertNull(summarizeMlDurations(null, "x").p50Ms)
    }

    @Test
    fun sampleStoreIsBounded() {
        var json: String? = "not json"
        for (i in 1..(POST_CLOSE_ML_TIMING_MAX_SAMPLES + 25)) {
            json = appendMlDurationSample(json, "online_update", "2026-09-25", i.toLong(), POST_CLOSE_ML_OUTCOME_COMPLETED, 30_000L)
        }
        val arr = JSONArray(json)
        assertEquals(POST_CLOSE_ML_TIMING_MAX_SAMPLES, arr.length())
        assertEquals(26L, arr.getJSONObject(0).getLong("elapsed_ms"))   // oldest dropped
    }

    @Test
    fun logLineStatesLimitsUnchanged() {
        val line = mlDurationLogLine(TimedMlResult.TimedOut(45_000L, 45_001L), 45_000L,
            MlDurationSummary("train_temporal", 3, 1, 2, 800L, 1200L))
        assertTrue(line.startsWith("POST_CLOSE_ML_DURATION: stage=train_temporal outcome=TIMED_OUT"))
        assertTrue(line.contains("limits_changed=false"))
        assertTrue(line.contains("p50_ms=800 p95_ms=1200"))
    }

    @Test
    fun serviceKeepsExistingBoundsAndUsesResultType() {
        assertEquals(30_000L, MarketMLService.ONLINE_UPDATE_TIMEOUT_MS)
        assertEquals(45_000L, MarketMLService.TEMPORAL_TRAIN_TIMEOUT_MS)
        val src = listOf(File("src/main/java/com/marketradar/app/MarketMLService.kt"),
            File("app/src/main/java/com/marketradar/app/MarketMLService.kt")).first { it.isFile }.readText()
        val temporal = src.substringAfter("private suspend fun runTemporalTraining()").substringBefore("// SUPABASE HELPERS")
        assertTrue(temporal.contains("runTimedMl(TEMPORAL_TRAIN_TIMEOUT_MS)"))
        assertFalse(temporal.contains("withTimeoutOrNull"))
        assertFalse(temporal.contains("} ?: return@withContext"))
        assertTrue(temporal.contains("is TimedMlResult.TimedOut ->"))
        assertTrue(temporal.contains("recordPostCloseMlDuration(\"train_temporal\", temporalTimed, TEMPORAL_TRAIN_TIMEOUT_MS)"))
        val online = src.substringAfter("private suspend fun runOnlineUpdate(").substringBefore("// TEMPORAL MODEL TRAINING")
        assertTrue(online.contains("runTimedMl(ONLINE_UPDATE_TIMEOUT_MS)"))
        assertTrue(online.contains("recordPostCloseMlDuration(\"online_update\", onlineTimed, ONLINE_UPDATE_TIMEOUT_MS)"))
    }
}
