package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.nio.file.Files

class GeneratedCandidateOutboxTest {
    private fun envelope(ts: String) = JSONObject().put("poll_ts", ts).put("rows", JSONArray()
        .put(JSONObject().put("snapshot_poll_ts", ts).put("candidate_id", "c1").put("net_premium", 40)))

    @Test fun restartRetryUsesTheExactFrozenRowsAndPermanentFailurePreservesBytes() {
        val root = Files.createTempDirectory("generated-outbox").toFile()
        try {
            val ts = "2026-10-12T12:00:44+0530"
            GeneratedCandidateOutbox.enqueue(root, envelope(ts))
            var frozen = ""
            val first = GeneratedCandidateOutbox.drain(root) { frozen = it; GeneratedCandidateOutbox.Outcome.RETRY }
            assertEquals(1, first.pending)
            val second = GeneratedCandidateOutbox.drain(root) {
                assertEquals(frozen, it)
                GeneratedCandidateOutbox.Outcome.QUARANTINE
            }
            assertEquals(0, second.pending)
            assertEquals(1, second.quarantinedTotal)
            assertTrue(second.quarantinedBytes > 0)
            assertTrue(GeneratedCandidateOutbox.isUnacknowledged(root, ts))
        } finally { root.deleteRecursively() }
    }

    @Test fun acknowledgedRowsLeaveNoPendingEntryAndMixedPollsAreRejected() {
        val root = Files.createTempDirectory("generated-outbox").toFile()
        try {
            val ts = "2026-10-12T12:05:44+0530"
            val bad = envelope(ts)
            bad.getJSONArray("rows").getJSONObject(0).put("snapshot_poll_ts", "other")
            assertThrows(IllegalArgumentException::class.java) { GeneratedCandidateOutbox.enqueue(root, bad) }
            GeneratedCandidateOutbox.enqueue(root, envelope(ts))
            val result = GeneratedCandidateOutbox.drain(root) { GeneratedCandidateOutbox.Outcome.ACKNOWLEDGED }
            assertEquals(1, result.acknowledged)
            assertFalse(GeneratedCandidateOutbox.isUnacknowledged(root, ts))
        } finally { root.deleteRecursively() }
    }
}
