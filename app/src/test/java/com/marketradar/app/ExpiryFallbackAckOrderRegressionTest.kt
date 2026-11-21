package com.marketradar.app

import com.marketradar.app.ExpiryFallbackPublication.State as S
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Before
import org.junit.Test

/**
 * R4 regression (Codex R3 reply): uses ONLY API that exists on R3 (applyAndPublish + the production
 * AnalysisAuthority.publish, called in the service's order) so it compiles on R3 and fails there BEHAVIOURALLY:
 * R3 acknowledges PUBLISHED to Python from applyAndPublish, before the final gate runs, even when the gate then
 * refuses the result. R4 sends no acknowledgement before the gate.
 */
class ExpiryFallbackAckOrderRegressionTest {
    @Before fun quiet() { PyTimeout.logger = { } }

    private fun result(): JSONObject = JSONObject().put("single_leg_by_index", JSONObject().put("NF",
        JSONObject().put("decision_id", "idx1_n").put("reasons", JSONArray().put("EXPIRY_DAY_NO_NEXT_CHAIN")
            .put("EXPIRY_FALLBACK_EVIDENCE_PENDING")).put("status", "BLOCKED").put("admit", false)
            .put("expiry_fallback", JSONObject().put("status", "EVIDENCE_REQUIRED").put("decision_id", "idx1_n")
                .put("trade_expiry", "2026-11-23").put("base_reasons", JSONArray().put("EXPIRY_DAY_NO_NEXT_CHAIN"))
                .put("base_status", "BLOCKED"))))

    private fun patchFor(a: JSONObject): String = JSONObject().put("single_leg_by_index", JSONObject().put("NF",
        JSONObject().put("decision_id", "idx1_n").put("status", "ELIGIBLE_PENDING_SNAPSHOT")
            .put("instrument_key", "NSE_FO|NF2026120124000CE").put("reasons", JSONArray())
            .put("expiry_fallback", JSONObject().put("status", "APPLIED").put("attempt", a)))).put("attempt", a).toString()

    @Test fun noPublishedAcknowledgementBeforeTheFinalGate_refusedResultIsNeverAcknowledgedPublished() {
        var rev = "rev-1"
        val auth = AnalysisAuthority().apply { attachRevisionSource { rev } }
        val pub = ExpiryFallbackPublication(ExpiryFallbackPublication.MemoryStore())
        val events = mutableListOf<String>()
        val r = result()
        ExpiryFallbackPublication.applyAndPublish(r, pub, "test.efb.order." + System.nanoTime(), 2_000L, Long.MAX_VALUE,
            { 0L }, { a -> patchFor(a) }) { _, st -> events.add("ack:" + st) }
        val identity = AnalysisAuthority.Identity("2026-11-23", "rev-1", 1_000L, "POLL")
        rev = "rev-2"                                                // inputs change before the final gate
        val out = auth.publish(identity) { events.add("commit"); true }
        assertEquals(AnalysisAuthority.Outcome.SUPERSEDED_INPUT, out)
        assertFalse("a refused result must never be acknowledged PUBLISHED; events=$events", events.contains("ack:PUBLISHED"))
    }

    @Test fun successPath_theAcknowledgementNeverPrecedesTheCommit() {
        val auth = AnalysisAuthority().apply { attachRevisionSource { "rev-1" } }
        val pub = ExpiryFallbackPublication(ExpiryFallbackPublication.MemoryStore())
        val events = mutableListOf<String>()
        ExpiryFallbackPublication.applyAndPublish(result(), pub, "test.efb.order2." + System.nanoTime(), 2_000L,
            Long.MAX_VALUE, { 0L }, { a -> patchFor(a) }) { _, st -> events.add("ack:" + st) }
        assertEquals(AnalysisAuthority.Outcome.PUBLISHED,
            auth.publish(AnalysisAuthority.Identity("2026-11-23", "rev-1", 1_000L, "POLL")) { events.add("commit"); true })
        val ack = events.indexOf("ack:PUBLISHED")
        assertFalse("PUBLISHED acknowledged before the commit; events=$events", ack >= 0 && ack < events.indexOf("commit"))
    }
}
