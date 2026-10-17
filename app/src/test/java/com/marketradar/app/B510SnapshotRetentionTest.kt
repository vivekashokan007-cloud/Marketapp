package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * b510 Batch A: Python producer -> CaptureEngine re-parse -> local compaction -> upload sanitizer
 * keeps the frozen market view and the explicit lane. The fixture was produced by the real
 * brain.take_poll_snapshot (see its _generated_by note).
 */
class B510SnapshotRetentionTest {
    private fun resource(name: String): JSONObject = JSONObject(requireNotNull(
        javaClass.classLoader?.getResourceAsStream("contracts/$name")).bufferedReader().readText())

    /** The CaptureEngine.build hop: string JSONB fields re-parsed, compacted, sanitized. */
    private fun persistHop(raw: JSONObject): JSONObject {
        for (field in listOf("verdict_json", "market_forces_json")) {
            val text = raw.optString(field, "")
            if (text.isNotBlank()) raw.put(field, JSONObject(text))
        }
        val compact = EvaluationLocalCache.compactBrainSnapshotForPersistence(raw)
        return JSONObject(BrainSnapshotPayloadSanitizer.sanitize(compact).toString())
    }

    @Test fun marketViewAndLaneSurviveCompactionAndUploadPayload() {
        val source = resource("b510_python_snapshot_market_view.json")
        val expected = JSONObject(source.getString("verdict_json"))
        val verdict = persistHop(JSONObject(source.toString())).getJSONObject("verdict_json")
        for (key in listOf("market_direction", "market_bull_score", "market_bear_score", "market_view_status",
                "market_view_source", "market_view_validated", "market_direction_scope", "direction_semantics",
                "direction_semantics_version", "market_thesis")) {
            assertTrue("missing $key", verdict.has(key))
            assertEquals("changed $key", expected.get(key).toString(), verdict.get(key).toString())
        }
        assertTrue(verdict.has("execution_lane_direction"))
        assertTrue(verdict.isNull("execution_lane_direction"))      // WAIT: no lane, not a fabricated neutral
        assertEquals("BULL", verdict.getString("market_direction"))
        assertEquals("NEUTRAL", verdict.getString("direction"))     // legacy WAIT value unchanged
        assertFalse(verdict.getBoolean("market_view_validated"))
    }
}
