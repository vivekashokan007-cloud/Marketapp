package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * Codex b503 review (13 Oct 2026): the four executed counterexamples, ported verbatim in setup and
 * inverted into the REQUIRED behaviour. They failed on c2797c5 and must pass after R1/R2/R3/R5.
 */
class CodexB503CounterexamplesTest {
    private class Fixture(eventCapacity: Int = 2000, val root: File = Files.createTempDirectory("codex-b503").toFile()) {
        val journal = CaptureJournal(root)
        val events = FrozenEvidenceOutbox(root, "events", eventCapacity, 8L * 1024 * 1024)
        var healthText: String? = null
        val legacy = FrozenEvidenceOutbox(root, "legacy", 150, 64L * 1024 * 1024)
        val channels = object : CaptureChannels {
            override fun compactEnabled() = true
            override fun stageHealth() = JSONObject(healthText ?: "{}")
            override fun appendLocalCache(sessionDate: String, snapshotPayload: String) = true
            override fun enqueueSnapshot(payload: String) = BrainSnapshotOutbox.enqueuePayload(root, payload)
            override fun enqueueCompact(envelope: String): Boolean {
                val e = JSONObject(envelope)
                return Pc2TelemetryOutbox.enqueue(root, Pc2CompactBatch.Built(e.getJSONObject("policy_row"), e.getJSONObject("batch_row"), JSONObject()))
            }
            override fun enqueueGenerated(payload: String) = GeneratedCandidateOutbox.enqueuePayload(root, payload)
            override fun enqueueLegacy(pollTs: String, payload: String) = legacy.enqueue(pollTs, payload)
        }
        val engine = CaptureEngine(journal, channels, object : CaptureHealthStore {
            override fun read() = healthText
            override fun write(value: String): Boolean { healthText = value; return true }
        }, events, "run-A", 1, { _, _ -> })
        fun request() = FrozenCaptureRequest.freeze("2026-10-13|8", "2026-10-13", "09:50",
            JSONObject(), JSONObject().put("today_ist", "2026-10-13"), "[]", "2.6.72", 503, "2.6.72", "run-A", attemptId = "att")
    }

    @Test fun r1_uploadAckDoesNotResetOriginSessionHealth() {
        val h = Fixture()
        h.engine.recordHealth("attempt", "2026-10-13|8")
        h.engine.recordHealth("built", "2026-10-13|8")
        h.engine.recordHealth("upload_ack", "2026-10-13T09:50:42+0530")
        val health = JSONObject(h.healthText!!)
        assertEquals("2026-10-13", health.getString("session_date"))
        assertEquals(1, health.getJSONObject("counts").optInt("attempt"))
        assertEquals(1, health.getJSONObject("counts").optInt("built"))
    }

    @Test fun r2_fullEventQueueNeverDeletesTheOnlyDurableLossIdentity() {
        val f = Fixture(eventCapacity = 0); val req = f.request()
        f.engine.beforeSubmit(req)
        assertEquals(1, f.journal.inventory().intents)
        f.engine.onFailed(req, CaptureCoordinator.Failure.Exception(IllegalStateException("failed"), true), 0)
        assertEquals(0, f.events.status().pending)
        assertEquals("terminal marker must survive a full event ledger", 1, f.journal.inventory().intents)
        val again = f.engine.reconcile(emptySet())
        assertTrue("restart must still see the unresolved loss: $again", again.stillPending + again.lost + again.uncertain > 0)
        // Storage repaired: the SAME loss is delivered once, then ownership is released.
        val repaired = Fixture(eventCapacity = 2000, root = f.root)
        repaired.engine.reconcile(emptySet())
        val types = File(f.root, "events").listFiles()!!.filter { it.isFile && it.name.endsWith(".json") }
            .map { JSONObject(JSONObject(it.readText()).getString("payload")).getString("event_type") }
        assertEquals(listOf("LATE_EXCEPTION"), types)
        assertEquals(0, repaired.journal.inventory().intents)
    }

    @Test fun r3_compactionPressureNeverTurnsTheLegacyCohortIntoNotExpected() {
        val c = Fixture(); val cr = c.request()
        val decisions = JSONArray(); repeat(128) { decisions.put(JSONObject().put("variable_name", "v").put("bucket", 0).put("support_count", 1)) }
        val context = JSONObject().put("snapshot_brain_version", "2.6.72")
            .put("snapshot_pc2_authority_decisions", decisions)
            .put("snapshot_pc2_authority_policy", JSONObject().put("version", "pc2_authority_policy_v2").put("authority_diagnostics_version", "pc2_authority_diagnostics_v1"))
            .put("c3_finalization_frame", JSONObject().put("padding", "x".repeat(2200000)))
        val snap = JSONObject().put("poll_ts", "2026-10-13T09:50:42+0530").put("session_date", "2026-10-13")
            .put("context_json", context.toString()).put("top_candidates_json", "[]").put("poll_summary_json", "{}")
        val built = c.engine.build(cr, CaptureDelivery.LATE, CaptureCoordinator.Computed(snap.toString(), 4792, 1), 4000, 0)
        assertEquals("PENDING", built.states["compact"])
        assertEquals("PENDING", built.states["legacy"])
        assertEquals(128, JSONArray(built.channels.getString("legacy")).length())
    }

    @Test fun r5_eventAckRequiresTheSubmittedEventId() {
        assertNotEquals(FrozenEvidenceOutbox.Outcome.ACKNOWLEDGED,
            CaptureEventProtocol.classify(200, "{\"status\":\"INSERTED\",\"event_id\":\"different\"}"))
    }
}
