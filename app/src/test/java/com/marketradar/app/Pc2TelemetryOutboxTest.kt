package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files

class Pc2TelemetryOutboxTest {
    @Test
    fun failedUploadSurvivesAndVerifiedRetryAcknowledges() {
        val root = Files.createTempDirectory("pc2-outbox-test").toFile()
        try {
            val built = requireNotNull(Pc2CompactBatch.build(snapshot()))
            assertTrue(Pc2TelemetryOutbox.enqueue(root, built))
            assertFalse("same batch is idempotent", Pc2TelemetryOutbox.enqueue(root, built))
            assertEquals(1, Pc2TelemetryOutbox.pending(root).size)

            val failed = Pc2TelemetryOutbox.drain(root) { Pc2TelemetryOutbox.Outcome.RETRY }
            assertEquals(1, failed.attempted)
            assertEquals(0, failed.acknowledged)
            assertEquals(1, failed.pending)

            // A new read from disk simulates process restart recovery.
            assertEquals(built.batchRow.getString("batch_id"), Pc2TelemetryOutbox.pending(root).single().getJSONObject("batch_row").getString("batch_id"))
            val succeeded = Pc2TelemetryOutbox.drain(root) { Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED }
            assertEquals(1, succeeded.acknowledged)
            assertEquals(0, succeeded.pending)
        } finally {
            root.deleteRecursively()
        }
    }

    private fun snapshot(): JSONObject = JSONObject()
        .put("session_date", "2026-09-29")
        .put("poll_ts", "2026-09-29T07:35:14Z")
        .put(
            "context_json",
            JSONObject()
                .put("snapshot_brain_version", "test")
                .put("snapshot_pc2_authority_policy", JSONObject().put("version", "v1"))
                .put(
                    "snapshot_pc2_authority_decisions",
                    JSONArray()
                        .put(JSONObject().put("variable_name", "a").put("observed_value", 1))
                        .put(JSONObject().put("variable_name", "a").put("observed_value", 1))
                )
        )
}
