package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/** R2: durable per-menu outbox. R3: behavioral Paper-only coordinator checks. */
class EvidenceBatch1R2OutboxTest {
    @get:Rule val tmp = TemporaryFolder()

    private val day = 24L * 60 * 60 * 1000
    private val fri = "2026-10-09"; private val mon = "2026-10-12"; private val thu = "2026-10-08"
    private fun payload(date: String) = JSONObject().put("manifest", JSONObject().put("menu_id", PaperMenuCapture.menuIdFor(date))
        .put("session_date", date)).put("candidates", org.json.JSONArray()).toString()

    /** Fake server: scripted dispositions per menu; records the exact bytes it received. */
    private class FakeServer(val script: MutableMap<String, ArrayDeque<String>> = mutableMapOf()) {
        val received = mutableListOf<Triple<String, String, String>>() // menu, kind, bytes
        fun upload(e: PaperMenuOutbox.Entry, text: String): PaperMenuOutbox.Delivery {
            received.add(Triple(e.menuId, e.kind, text))
            val d = script[e.menuId]?.removeFirstOrNull() ?: "ACKNOWLEDGED"
            return PaperMenuOutbox.Delivery(d, if (d == "RETRY") "HTTP_503" else d)
        }
    }

    private fun st(dir: File, menu: String) = PaperMenuOutbox.readState(dir, menu)!!.getString("state")

    @Test fun partialFridayUploadSurvivesMondayRolloverAndCompletesWithSameBytes() {
        val dir = tmp.newFolder("o")
        val fridayId = PaperMenuCapture.menuIdFor(fri)
        val bytes = payload(fri)
        assertNotNull(PaperMenuOutbox.enqueueCaptured(dir, fridayId, fri, bytes, 0))
        val server = FakeServer(mutableMapOf(fridayId to ArrayDeque(listOf("RETRY"))))
        PaperMenuOutbox.drain(dir, fri, 1, server::upload)           // partial / failed on Friday
        assertEquals(PaperMenuOutbox.RETRY_PENDING, st(dir, fridayId))
        // Monday: session state rolls over (Prepared for a new day) - the outbox is untouched by that.
        val p = PaperMenuCapture.prepare("paper", PaperMenuCapture.markCaptured(PaperMenuCapture.newState(fri), PaperMenuCapture.sha256Hex(bytes), "x", "t").toString(),
            mon, null, null, "2026-10-12T09:20:00+0530")
        assertEquals(mon, JSONObject(p.stateToSave!!).getString("session_date"))
        assertTrue(p.missedToEnqueue.isEmpty())
        val s = PaperMenuOutbox.drain(dir, mon, 3 * day, server::upload)
        assertEquals(1, s.acknowledged)
        assertEquals(PaperMenuOutbox.ACKED, st(dir, fridayId))
        assertEquals(listOf(bytes, bytes), server.received.map { it.third })  // frozen bytes, both attempts
    }

    @Test fun twoPendingSessionsAreDeliveredIndependentlyOldestFirst() {
        val dir = tmp.newFolder("o")
        val thuId = PaperMenuCapture.menuIdFor(thu); val friId = PaperMenuCapture.menuIdFor(fri)
        PaperMenuOutbox.enqueueCaptured(dir, friId, fri, payload(fri), 10)
        PaperMenuOutbox.enqueueCaptured(dir, thuId, thu, payload(thu), 20)
        val server = FakeServer(mutableMapOf(thuId to ArrayDeque(listOf("RETRY"))))
        val s = PaperMenuOutbox.drain(dir, mon, 30, server::upload)
        assertEquals(listOf(thuId, friId), server.received.map { it.first })
        assertEquals(PaperMenuOutbox.RETRY_PENDING, st(dir, thuId))
        assertEquals(PaperMenuOutbox.ACKED, st(dir, friId))
        assertEquals(1, s.retryPending)
        PaperMenuOutbox.drain(dir, mon, 40, server::upload)
        assertEquals(PaperMenuOutbox.ACKED, st(dir, thuId))
        assertEquals(3, server.received.size) // the acked Friday menu is not re-sent
    }

    @Test fun pendingMissedManifestIsFrozenOnceAndDelivered() {
        val dir = tmp.newFolder("o")
        val prev = PaperMenuCapture.newState(thu)
        PaperMenuCapture.recordAttempt(prev, "$thu|40", "FAILED", "brain_analyze_timeout", "t")
        val p = PaperMenuCapture.prepare("paper", prev.toString(), fri, null, null, "2026-10-09T09:15:00+0530")
        assertEquals(1, p.missedToEnqueue.size)
        val m = p.missedToEnqueue[0]
        val e1 = PaperMenuOutbox.enqueueMissed(dir, m, 1)!!
        // A re-derived manifest with different content never replaces the frozen bytes.
        val e2 = PaperMenuOutbox.enqueueMissed(dir, JSONObject(m.toString()).put("missed_at", "later"), 2)!!
        assertEquals(e1.getString("payload_sha256"), e2.getString("payload_sha256"))
        assertEquals(PaperMenuCapture.sha256Hex(m.toString()), e1.getString("payload_sha256"))
        val server = FakeServer()
        PaperMenuOutbox.drain(dir, fri, 3, server::upload)
        assertEquals(PaperMenuOutbox.KIND_MISSED, server.received.single().second)
        assertEquals(m.toString(), server.received.single().third)
        assertEquals("MISSED_SESSION", JSONObject(server.received.single().third).getString("capture_status"))
    }

    @Test fun restartResumesFromFilesOnly() {
        val dir = tmp.newFolder("o")
        val id = PaperMenuCapture.menuIdFor(fri)
        PaperMenuOutbox.enqueueCaptured(dir, id, fri, payload(fri), 0)
        PaperMenuOutbox.drain(dir, fri, 1) { _, _ -> throw java.io.IOException("process killed mid-request") }
        // "Restart": nothing in memory; a fresh drain reads only the directory.
        val st = PaperMenuOutbox.readState(File(dir.path), id)!!
        assertEquals(PaperMenuOutbox.RETRY_PENDING, st.getString("state"))
        assertEquals(1, st.getInt("attempts"))
        assertTrue(st.getString("last_outcome").startsWith("EXCEPTION:"))
        assertEquals(1, PaperMenuOutbox.drain(File(dir.path), fri, 2, FakeServer()::upload).acknowledged)
    }

    @Test fun exhaustedRetriesAreRetainedObservableAndRecoveredNextSession() {
        val dir = tmp.newFolder("o")
        val id = PaperMenuCapture.menuIdFor(fri)
        PaperMenuOutbox.enqueueCaptured(dir, id, fri, payload(fri), 0)
        val always = FakeServer(mutableMapOf(id to ArrayDeque(List(100) { "RETRY" })))
        repeat(PaperMenuOutbox.MAX_ATTEMPTS_PER_ROUND + 3) { PaperMenuOutbox.drain(dir, fri, it.toLong(), always::upload) }
        assertEquals(PaperMenuOutbox.MAX_ATTEMPTS_PER_ROUND, always.received.size) // stops within the session
        assertEquals(PaperMenuOutbox.EXHAUSTED, st(dir, id))
        val sum = PaperMenuOutbox.summary(dir)
        assertEquals(1, sum.getJSONObject("by_state").getInt("EXHAUSTED"))
        assertEquals(fri, sum.getString("oldest_unacked_session"))
        // Retained through pruning, even when very old.
        assertTrue(PaperMenuOutbox.prune(dir, 400 * day, 30 * day).isEmpty())
        // New session: one recovery round.
        val s = PaperMenuOutbox.drain(dir, mon, 401 * day, FakeServer()::upload)
        assertEquals(1, s.requeued); assertEquals(1, s.acknowledged)
        assertEquals(1, PaperMenuOutbox.readState(dir, id)!!.getInt("recovery_rounds"))
    }

    @Test fun pruningRemovesOnlyOldAcknowledgedEntries() {
        val dir = tmp.newFolder("o")
        val oldUnacked = PaperMenuCapture.menuIdFor("2026-08-01")
        val oldAcked = PaperMenuCapture.menuIdFor("2026-08-02")
        val newAcked = PaperMenuCapture.menuIdFor(fri)
        val conflict = PaperMenuCapture.menuIdFor("2026-08-03")
        PaperMenuOutbox.enqueueCaptured(dir, oldUnacked, "2026-08-01", payload("2026-08-01"), 0)
        PaperMenuOutbox.enqueueCaptured(dir, oldAcked, "2026-08-02", payload("2026-08-02"), 0)
        PaperMenuOutbox.enqueueCaptured(dir, conflict, "2026-08-03", payload("2026-08-03"), 0)
        val server = FakeServer(mutableMapOf(oldUnacked to ArrayDeque(listOf("RETRY")), conflict to ArrayDeque(listOf("CONFLICT"))))
        PaperMenuOutbox.drain(dir, "2026-08-03", 1 * day, server::upload)
        PaperMenuOutbox.enqueueCaptured(dir, newAcked, fri, payload(fri), 69 * day)
        PaperMenuOutbox.drain(dir, fri, 69 * day, FakeServer(mutableMapOf(oldUnacked to ArrayDeque(listOf("RETRY"))))::upload)
        val removed = PaperMenuOutbox.prune(dir, 70 * day, 30 * day)
        assertEquals(listOf(oldAcked), removed)
        assertFalse(File(dir, "$oldAcked.json").exists())
        assertTrue(File(dir, "$oldUnacked.json").exists())          // 70-day-old unacked file survives
        assertEquals(PaperMenuOutbox.CONFLICT_RETAINED, st(dir, conflict))
        assertTrue(File(dir, "$conflict.json").exists())
        assertTrue(File(dir, "$newAcked.json").exists())            // acked but inside retention
    }

    @Test fun corruptFrozenFileIsRetainedNotSent() {
        val dir = tmp.newFolder("o")
        val id = PaperMenuCapture.menuIdFor(fri)
        PaperMenuOutbox.enqueueCaptured(dir, id, fri, payload(fri), 0)
        File(dir, "$id.json").writeText("tampered")
        val server = FakeServer()
        PaperMenuOutbox.drain(dir, fri, 1, server::upload)
        assertTrue(server.received.isEmpty())
        assertEquals(PaperMenuOutbox.LOCAL_CORRUPT_RETAINED, st(dir, id))
    }

    // ---- R3 behavioral (pure coordinator; the service applies only this result) ----

    @Test fun liveAndSandboxCreateNoMenuConsumeNoSlotAndMakeNoMarginCalls() {
        val open = PaperMenuCapture.newState(fri)
        for (mode in listOf("live", "sandbox", "", null)) {
            val p = PaperMenuCapture.prepare(mode, open.toString(), fri, "$fri|40", 40, "2026-10-09T12:30:05+0530")
            assertNull("state must not be written for $mode", p.stateToSave)
            assertNull(p.request); assertNull(p.attemptSlot)
            assertTrue(p.missedToEnqueue.isEmpty())
            // Even a stale OPEN state from a previous day is not rolled over by a non-Paper poll.
            assertNull(PaperMenuCapture.prepare(mode, PaperMenuCapture.newState(thu).toString(), fri, null, null, "t").stateToSave)
            var marginCalls = 0
            assertNull(PaperMenuCapture.collectIfPaper(mode, "paper") { marginCalls++ })
            assertNull(PaperMenuCapture.collectIfPaper("paper", mode) { marginCalls++ })
            assertEquals(0, marginCalls)
        }
    }

    @Test fun switchingToPaperInsideTheWindowCaptures() {
        val open = PaperMenuCapture.newState(fri)
        // 12:30 slot polled in live: nothing consumed.
        val live = PaperMenuCapture.prepare("live", open.toString(), fri, "$fri|40", 40, "t1")
        assertNull(live.stateToSave)
        // 12:35 slot after switching to Paper: first attempt of the session.
        val paper = PaperMenuCapture.prepare("paper", open.toString(), fri, "$fri|41", 41, "t2")
        assertEquals("$fri|41", paper.attemptSlot)
        assertNotNull(paper.request)
        assertEquals(0, JSONObject(paper.stateToSave!!).getJSONArray("attempts").length())
        var marginCalls = 0
        assertEquals(1, PaperMenuCapture.collectIfPaper("paper", "paper") { ++marginCalls })
    }
}
