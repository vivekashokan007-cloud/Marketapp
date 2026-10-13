package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * b503 section D evidence: the 12 Oct menu (pdm_d0e25b719e65a2289bfa0588) was requeued at the 13 Oct 09:15 session
 * and kept failing (Oracle read timeouts / PT403 before designation). It must exhaust after
 * MAX_ATTEMPTS_PER_ROUND within the 13 Oct session, stay quiet for the rest of that session, and re-arm exactly
 * once on the 14 Oct session, where a successful delivery acknowledges it. No eviction, no new bytes.
 */
class B503PaperMenuRearmTest {
    @Test
    fun exhaustedMenuRearmsOnNextSessionAndAcknowledges() {
        val dir = Files.createTempDirectory("b503pdm").toFile()
        val menuId = "pdm_d0e25b719e65a2289bfa0588"
        val payload = JSONObject().put("menu_id", menuId).put("session_date", "2026-10-12").toString()
        PaperMenuOutbox.enqueueCaptured(dir, menuId, "2026-10-12", payload, 1L)!!
        val sent = mutableListOf<String>()
        val retry: (PaperMenuOutbox.Entry, String) -> PaperMenuOutbox.Delivery =
            { _, t -> sent.add(t); PaperMenuOutbox.Delivery("RETRY", "PT403") }

        // 12 Oct: one full round exhausts.
        repeat(PaperMenuOutbox.MAX_ATTEMPTS_PER_ROUND) { PaperMenuOutbox.drain(dir, "2026-10-12", 10L + it, retry) }
        assertEquals(PaperMenuOutbox.EXHAUSTED, state(dir))
        // 13 Oct 09:15: re-armed once, then exhausts again within the session.
        val first = PaperMenuOutbox.drain(dir, "2026-10-13", 100L, retry)
        assertEquals(1, first.requeued)
        repeat(PaperMenuOutbox.MAX_ATTEMPTS_PER_ROUND - 1) { PaperMenuOutbox.drain(dir, "2026-10-13", 101L + it, retry) }
        assertEquals(PaperMenuOutbox.EXHAUSTED, state(dir))
        assertEquals("2026-10-13", st(dir).optString("last_recovery_session"))
        val quiet = PaperMenuOutbox.drain(dir, "2026-10-13", 200L, retry)
        assertEquals(0, quiet.attempted); assertEquals(0, quiet.requeued)
        // 14 Oct: re-armed and delivered with the same frozen bytes.
        val next = PaperMenuOutbox.drain(dir, "2026-10-14", 300L) { _, t -> sent.add(t); PaperMenuOutbox.Delivery("ACKNOWLEDGED", "COMPLETE") }
        assertEquals(1, next.requeued); assertEquals(1, next.acknowledged)
        assertEquals(PaperMenuOutbox.ACKED, state(dir))
        assertEquals(2, st(dir).optInt("recovery_rounds"))
        assertEquals(setOf(payload), sent.toSet())
        assertEquals(PaperMenuOutbox.MAX_ATTEMPTS_PER_ROUND * 2 + 1, sent.size)
    }

    private fun st(dir: File): JSONObject = JSONObject(dir.listFiles { f -> f.name.endsWith(".outbox.json") }!!.single().readText())
    private fun state(dir: File): String = st(dir).optString("state")
}
