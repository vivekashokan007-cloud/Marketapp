package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Instant

/**
 * Codex round-3 review of 3bee8bb: the two executed reproductions, same setup, inverted into the REQUIRED
 * behaviour. Both fail on 3bee8bb and must pass after C1/C2.
 */
class CodexB503Round3CounterexamplesTest {
    @Test fun c2_validDelayedOriginIsMatchedToItsActualChainPoll() {
        val p = JSONObject().put("schema", "capture_provenance_v1").put("capture_key", "2026-10-13|8")
            .put("session_date", "2026-10-13").put("slot_ordinal", 8).put("delivery", "LATE").put("attempt_id", "origin-delay")
            .put("slot_scheduled_ist", "2026-10-13T09:50:00+0530").put("origin_poll_ts", "2026-10-13T09:52:00+0530")
        val completion = Instant.parse("2026-10-13T04:24:00Z")
        assertTrue(CaptureSlotReconciler.validate(p, "2026-10-13", completion.epochSecond) is CaptureSlotReconciler.Validation.Valid)
        val s = JSONObject().put("poll_ts_utc", completion.toString()).put("session_date", "2026-10-13").put("capture_provenance", p)
        val report = Pc2ParityAudit.audit(Pc2ParityAudit.SessionEvidence("2026-10-13", listOf("2026-10-13T04:22:31Z")),
            listOf(s), emptyList(), emptyList())
        assertEquals(report.toJson().toString(), 0, report.count("MISSING_INVENTORY_FOR_ORIGIN"))
        assertEquals(report.toJson().toString(), 0, report.count("MISSING_SNAPSHOT_FOR_POLL"))
    }

    @Test fun c1_skippedOriginIsDurableBeforeTheQueuedControlJobRuns() {
        val h = B503Harness()
        val worker = ManualExecutor()
        val fin = ManualFinisher("pending-finisher", 2)
        val control = ManualFinisher("pending-control", 64)
        val c = CaptureCoordinator(worker, fin, control, h.engine,
            waiter = CaptureCoordinator.Waiter { _, _ -> throw java.util.concurrent.TimeoutException() },
            scheduleStuckCheck = { _, _ -> })
        c.submit(B503Harness.request(attempt = "running-slot"), 0L) { B503Harness.pythonSnapshot("2026-10-13T09:50:42+0530") }
        val skipped = B503Harness.request(slot = "2026-10-13|9", pollTime = "09:55", attempt = "skipped-slot")
        assertEquals(CaptureCoordinator.Outcome.SKIPPED_INFLIGHT, c.submit(skipped, 0L) { error("must not execute") })
        // Process dies here: the control job never runs.
        assertNotNull("skipped origin must be journalled before the skip returns", h.journal.read(skipped.attemptId))
        val restarted = B503Harness(root = h.root, runId = "run-B")
        restarted.engine.reconcile(emptySet())
        restarted.engine.reconcile(emptySet())
        assertEquals(1, restarted.eventTypes().count { it == "INFLIGHT_SKIPPED_SLOT" })
    }
}
