package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.nio.file.Files

class BrainSnapshotOutboxTest {
    private fun snapshot(ts: String) = JSONObject()
        .put("session_date", "2026-10-12").put("poll_ts", ts)
        .put("confidence", "None")
        .put("context_json", JSONObject().put("point_in_time", "retain"))

    @Test fun networkFailureAndRestartKeepFrozenBytesUntilAcknowledged() {
        val root = Files.createTempDirectory("snapshot-outbox").toFile()
        try {
            val ts = "2026-10-12T09:40:40+0530"
            assertTrue(BrainSnapshotOutbox.enqueue(root, snapshot(ts)))
            val original = mutableListOf<String>()
            val failed = BrainSnapshotOutbox.drain(root) { original.add(it); false }
            assertEquals(1, failed.pending)
            assertEquals(0, failed.acknowledged)
            assertTrue(failed.pendingBytes > 0)
            assertTrue(BrainSnapshotOutbox.isPending(root, ts))
            val recovered = BrainSnapshotOutbox.drain(root) {
                assertEquals(original.single(), it)
                assertTrue(JSONObject(it).isNull("confidence"))
                assertEquals("retain", JSONObject(it).getJSONObject("context_json").getString("point_in_time"))
                true
            }
            assertEquals(1, recovered.acknowledged)
            assertEquals(0, recovered.pending)
            assertFalse(BrainSnapshotOutbox.isPending(root, ts))
        } finally { root.deleteRecursively() }
    }

    @Test fun samePollIsNeverOverwrittenAndBrokenTempIsVisible() {
        val root = Files.createTempDirectory("snapshot-outbox").toFile()
        try {
            val ts = "2026-10-12T09:45:31+0530"
            assertTrue(BrainSnapshotOutbox.enqueue(root, snapshot(ts)))
            assertFalse(BrainSnapshotOutbox.enqueue(root, snapshot(ts)))
            assertThrows(IllegalStateException::class.java) {
                BrainSnapshotOutbox.enqueue(root, snapshot(ts).put("action", "DIFFERENT"))
            }
            File(root, "brain_snapshot_outbox_v1/interrupted.json.tmp").writeText("frozen")
            val result = BrainSnapshotOutbox.drain(root, maxAttempts = 0) { true }
            assertEquals(1, result.pending)
            assertTrue(result.faults.any { it.startsWith("incomplete_enqueue:") })
        } finally { root.deleteRecursively() }
    }

    @Test fun failedOldPollDoesNotLoseTheNewPoll() {
        val root = Files.createTempDirectory("snapshot-outbox").toFile()
        try {
            val first = "2026-10-12T09:40:40+0530"
            val second = "2026-10-12T09:45:31+0530"
            BrainSnapshotOutbox.enqueue(root, snapshot(first))
            BrainSnapshotOutbox.enqueue(root, snapshot(second))
            val result = BrainSnapshotOutbox.drain(root) { false }
            assertEquals(2, result.attempted)
            assertEquals(2, result.pending)
            assertTrue(BrainSnapshotOutbox.isPending(root, second))
            assertEquals(2, BrainSnapshotOutbox.drain(root) { true }.acknowledged)
        } finally { root.deleteRecursively() }
    }

    @Test fun committedTempAfterProcessDeathIsRecovered() {
        val root = Files.createTempDirectory("snapshot-outbox").toFile()
        try {
            val ts = "2026-10-12T10:00:25+0530"
            BrainSnapshotOutbox.enqueue(root, snapshot(ts))
            val file = File(root, "brain_snapshot_outbox_v1").listFiles()!!.single()
            assertTrue(file.renameTo(File(file.parentFile, file.name + ".tmp")))
            val tmp = File(file.parentFile, file.name + ".tmp")
            assertTrue(tmp.setLastModified(System.currentTimeMillis() - 61_000))
            val result = BrainSnapshotOutbox.drain(root) { true }
            assertEquals(1, result.acknowledged)
            assertEquals(0, result.pending)
            assertTrue(result.faults.isEmpty())
        } finally { root.deleteRecursively() }
    }
}
