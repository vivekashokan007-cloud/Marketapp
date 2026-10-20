package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.time.Instant

/**
 * b511 E1: the 13 Oct 2026 shape. The remote identity list holds all 72 session
 * snapshots, but the evaluation input (bounded local fallback, newest-first trim)
 * holds only the newest 35. Every input row resolves, so before b511 the run was
 * frozen on 35 expected identities and could verify "complete". Coverage of the
 * remote identity set must now be proven before Python consumes the input.
 */
class EvaluationInputCoverageB511Test {
    private val date = "2026-10-13"
    private val start = Instant.parse("2026-10-13T03:45:04Z")
    private fun snap(i: Int, withId: Boolean, stepSeconds: Long = 300L) = JSONObject()
        .put("id", if (withId) 7066L + i else JSONObject.NULL)
        .put("session_date", date)
        .put("poll_ts", start.plusSeconds(stepSeconds * i).toString())
        .put("recommendation_id", "rec-$i")
    private val remote = JSONArray().apply { for (i in 0 until 72) put(snap(i, true)) }

    private fun reconciler(rows: JSONArray = remote) =
        EvaluationIdentity.SnapshotReconciler(date, fetch = { rows }, persist = { false })

    @Test fun truncatedLocalFallbackIsDetectedAndBlocked() {
        val r = reconciler()
        val resolved = (37 until 72).map { r.resolve(snap(it, false)) }.toSet()
        assertEquals(35, resolved.size) // every truncated row still resolves
        val missing = r.missingRemoteIds(resolved)
        assertEquals((0 until 37).map { 7066L + it }, missing)
        try {
            r.requireCoversRemote(resolved)
            fail("truncated input was accepted")
        } catch (e: EvaluationIdentity.InputIncompleteException) {
            assertEquals(37, e.missing.size)
            assertTrue(e.message!!.startsWith("EVAL_INPUT_INCOMPLETE: missing=37"))
        }
    }

    @Test fun completeInputPasses() {
        val r = reconciler()
        val resolved = (0 until 72).map { r.resolve(snap(it, false)) }.toSet()
        assertTrue(r.missingRemoteIds(resolved).isEmpty())
        r.requireCoversRemote(resolved)
    }

    @Test fun emptyRemoteWithEmptyInputIsNotIncomplete() {
        val r = reconciler(JSONArray())
        r.requireCoversRemote(emptySet())
    }

    @Test fun largeSessionCoverageIsBoundedAndExact() {
        val big = JSONArray().apply { for (i in 0 until 5000) put(snap(i, true, stepSeconds = 3L)) }
        val r = reconciler(big)
        val resolved = (0 until 5000).filter { it != 4321 }.map { 7066L + it }.toSet()
        assertEquals(listOf(7066L + 4321), r.missingRemoteIds(resolved))
    }
}
