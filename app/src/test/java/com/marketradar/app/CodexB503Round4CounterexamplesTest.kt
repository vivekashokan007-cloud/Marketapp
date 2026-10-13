package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Instant

/**
 * Codex round-4 reproduction of 64e7424 (C3 parts A and B), ported verbatim in substance and inverted into the
 * required behaviour. On 64e7424 every mutated case below still returned verified=true (partial content digest and
 * status-specific-only NOT_EXPECTED checks), so these assertions FAIL there and PASS on the round-4 tip.
 * Only APIs that already existed at 64e7424 are used, so the same file compiles on both.
 */
class CodexB503Round4CounterexamplesTest {
    private fun capture(): Pair<JSONObject, List<JSONObject>> {
        val h = B503Harness()
        val source = B503Harness.result("2026-10-13T09:50:00+0530", 1, 0)
        source.getJSONObject("elephant_fact_pack").getJSONArray("candidates").getJSONObject(0)
            .put("economics", JSONObject().put("max_loss", 1000.25))
            .put("execution", JSONObject().put("execution_ready", true))
            .put("ml_overlay", JSONObject().put("ml_regime", "RANGE"))
        val b = h.engine.build(B503Harness.request(result = source, attempt = "content-test"), CaptureDelivery.LATE,
            CaptureCoordinator.Computed(B503Harness.pythonSnapshot("2026-10-13T09:50:42+0530"), 1, 1), 0, 0)
        val snap = JSONObject(b.channels.getString("snapshot"))
        val s = JSONObject().put("poll_ts_utc", "2026-10-13T04:20:42Z").put("session_date", snap.getString("session_date"))
            .put("recommendation_id", snap.opt("recommendation_id"))
            .put("capture_provenance", snap.getJSONObject("poll_summary_json").getJSONObject("capture_provenance"))
            .put("generated_cohort_manifest", snap.getJSONObject("poll_summary_json").getJSONObject("generated_cohort_manifest"))
        val rows = JSONObject(b.channels.getString("generated")).getJSONArray("rows")
        val g = (0 until rows.length()).map { i -> JSONObject(rows.getJSONObject(i).toString()).also {
            it.put("snapshot_poll_ts_utc", Instant.ofEpochSecond(Pc2ParityAudit.parseInstant(it.getString("snapshot_poll_ts"))!!.epochSecond).toString()) } }
        return s to g
    }

    private fun verified(s: JSONObject, g: List<JSONObject>) = CaptureCohortAudit.audit("2026-10-13", listOf(s), g)
    private fun mutated(g: List<JSONObject>, k: String, v: Any) = g.map { JSONObject(it.toString()) }.also { it[0].put(k, v) }

    @Test fun intactCaptureVerifies() {
        val (s, g) = capture()
        val r = verified(s, g); assertTrue(r.toJson().toString(), r.verified)
    }

    @Test fun A_maxLossMutationIsDetected() { val (s, g) = capture(); assertFalse(verified(s, mutated(g, "max_loss", 999999.75)).verified) }
    @Test fun A_executionReadyFlipIsDetected() { val (s, g) = capture(); assertFalse(verified(s, mutated(g, "execution_ready", false)).verified) }
    @Test fun A_mlRegimeMutationIsDetected() { val (s, g) = capture(); assertFalse(verified(s, mutated(g, "ml_regime", "MUTATED_REGIME")).verified) }

    @Test fun B_contradictoryNotExpectedWithRowsDoesNotVerify() {
        val (s, _) = capture()
        val m = s.getJSONObject("generated_cohort_manifest")
        m.put("status", "NOT_EXPECTED").put("reason", "zero_candidates") // 1-row entries left in place
        assertTrue(m.getJSONArray("rows").length() == 1)
        assertFalse(verified(s, emptyList<JSONObject>()).verified)
    }
}
