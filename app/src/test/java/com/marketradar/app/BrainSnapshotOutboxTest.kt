package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

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
            val failed = BrainSnapshotOutbox.drain(root) { original.add(it); BrainSnapshotOutbox.Outcome.RETRY }
            assertEquals(1, failed.pending)
            assertEquals(0, failed.acknowledged)
            assertTrue(failed.pendingBytes > 0)
            assertTrue(BrainSnapshotOutbox.isPending(root, ts))
            val recovered = BrainSnapshotOutbox.drain(root) {
                assertEquals(original.single(), it)
                assertTrue(JSONObject(it).isNull("confidence"))
                assertEquals("retain", JSONObject(it).getJSONObject("context_json").getString("point_in_time"))
                BrainSnapshotOutbox.Outcome.ACKNOWLEDGED
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
            val result = BrainSnapshotOutbox.drain(root, maxAttempts = 0) { BrainSnapshotOutbox.Outcome.ACKNOWLEDGED }
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
            val result = BrainSnapshotOutbox.drain(root) { BrainSnapshotOutbox.Outcome.RETRY }
            assertEquals(2, result.attempted)
            assertEquals(2, result.pending)
            assertTrue(BrainSnapshotOutbox.isPending(root, second))
            assertEquals(2, BrainSnapshotOutbox.drain(root) { BrainSnapshotOutbox.Outcome.ACKNOWLEDGED }.acknowledged)
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
            val result = BrainSnapshotOutbox.drain(root) { BrainSnapshotOutbox.Outcome.ACKNOWLEDGED }
            assertEquals(1, result.acknowledged)
            assertEquals(0, result.pending)
            assertTrue(result.faults.isEmpty())
        } finally { root.deleteRecursively() }
    }

    @Test fun permanentRejectionIsArchivedWithoutReportingAcknowledgement() {
        val root = Files.createTempDirectory("snapshot-outbox").toFile()
        try {
            val ts = "2026-10-12T09:40:40+0530"
            BrainSnapshotOutbox.enqueue(root, snapshot(ts))
            val result = BrainSnapshotOutbox.drain(root) { BrainSnapshotOutbox.Outcome.QUARANTINE }
            assertEquals(0, result.acknowledged)
            assertEquals(1, result.quarantinedTotal)
            assertEquals(0, result.pending)
            assertTrue(BrainSnapshotOutbox.isUnacknowledged(root, ts))
            assertEquals(0, BrainSnapshotOutbox.drain(root) { error("Should not retry archive") }.attempted)
        } finally { root.deleteRecursively() }
    }

    @Test fun permanentAndTransientHttpFailuresAreSeparated() {
        assertEquals(BrainSnapshotOutbox.Outcome.QUARANTINE, BrainSnapshotOutbox.classifyHttpFailure(400))
        assertEquals(BrainSnapshotOutbox.Outcome.QUARANTINE, BrainSnapshotOutbox.classifyHttpFailure(413))
        for (code in listOf(null, 401, 403, 408, 429, 500, 503)) {
            assertEquals(BrainSnapshotOutbox.Outcome.RETRY, BrainSnapshotOutbox.classifyHttpFailure(code))
        }
    }

    @Test fun throwingUploaderRetainsBytesAndReleasesSingleFlight() {
        val root = Files.createTempDirectory("snapshot-outbox").toFile()
        try {
            val ts = "2026-10-12T09:40:40+0530"
            BrainSnapshotOutbox.enqueue(root, snapshot(ts))
            assertThrows(OutOfMemoryError::class.java) {
                BrainSnapshotOutbox.drain(root) { throw OutOfMemoryError("simulated") }
            }
            assertTrue(BrainSnapshotOutbox.isPending(root, ts))
            val result = BrainSnapshotOutbox.drain(root) { BrainSnapshotOutbox.Outcome.ACKNOWLEDGED }
            assertEquals(1, result.acknowledged)
        } finally { root.deleteRecursively() }
    }

    @Test fun concurrentDrainSkipsAndEnqueueNeverWaitsOnUpload() {
        val root = Files.createTempDirectory("snapshot-outbox").toFile()
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        try {
            BrainSnapshotOutbox.enqueue(root, snapshot("2026-10-12T09:40:40+0530"))
            val thread = Thread {
                BrainSnapshotOutbox.drain(root) {
                    entered.countDown()
                    release.await(3, TimeUnit.SECONDS)
                    BrainSnapshotOutbox.Outcome.ACKNOWLEDGED
                }
            }
            thread.start()
            assertTrue(entered.await(3, TimeUnit.SECONDS))
            assertEquals("busy", BrainSnapshotOutbox.drain(root) { error("Concurrent upload") }.stopReason)
            val start = System.nanoTime()
            BrainSnapshotOutbox.enqueue(root, snapshot("2026-10-12T09:45:40+0530"))
            assertTrue((System.nanoTime() - start) / 1_000_000 < 1000)
            release.countDown()
            thread.join(3000)
            assertFalse(thread.isAlive)
        } finally { release.countDown(); root.deleteRecursively() }
    }

    @Test fun overCapacityArchivesOldEntriesButKeepsNewestForUpload() {
        val root = Files.createTempDirectory("snapshot-outbox").toFile()
        try {
            for (i in 0..150) {
                BrainSnapshotOutbox.enqueue(root, snapshot("2026-10-12T10:%02d:%02d+0530".format(i / 60, i % 60)))
            }
            val result = BrainSnapshotOutbox.drain(root, maxAttempts = 0) { error("No upload") }
            assertEquals(150, result.pending)
            assertEquals(1, result.quarantinedTotal)
            assertTrue(BrainSnapshotOutbox.isPending(root, "2026-10-12T10:02:30+0530"))
        } finally { root.deleteRecursively() }
    }
}
