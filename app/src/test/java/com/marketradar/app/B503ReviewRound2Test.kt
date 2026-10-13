package com.marketradar.app

import com.marketradar.app.B503Harness.Companion.SESSION
import com.marketradar.app.B503Harness.Companion.pythonSnapshot
import com.marketradar.app.B503Harness.Companion.request
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.util.concurrent.Executor

/** Codex b503 review round 2 (R1, R2, R3, R4-client, R5, R7): every test listed per finding. */
class B503ReviewRound2Test {
    private val pollTs = "${SESSION}T09:50:42+0530"
    private fun computed(v: String = pythonSnapshot(pollTs)) = CaptureCoordinator.Computed(v, 4792, 1200)

    // ------------------------------------------------------------------ R1
    @Test fun r1_actualServiceInputs_neverResetSessionHealth() {
        val h = B503Harness()
        val c = h.coordinator(worker = Executor { it.run() }, finisher = ManualFinisher("fin", 2))
        c.submit(request(), 4_000L) { pythonSnapshot(pollTs) }
        // fin not run yet: attempt recorded; now deliver to get built + timing (histogram/max)
        val before = h.health()
        assertEquals(SESSION, before.getString("session_date"))
        val attempt = h.count("attempt")
        // Real drain inputs: ISO poll_ts ACK carrying its validated origin, a pre-b503 ACK without origin,
        // the pending summary, and a previous-day ACK.
        h.engine.recordUploadAck(pollTs, "$SESSION|8")
        h.engine.recordUploadAck("${SESSION}T09:55:03+0530", null)
        h.engine.recordUploadPending(3)
        h.engine.recordUploadAck("2026-10-12T15:25:04+0530", "2026-10-12|75")
        h.engine.recordHealth("upload_ack", "garbage|not-a-slot")
        val after = h.health()
        assertEquals(SESSION, after.getString("session_date"))
        assertEquals(attempt, h.count("attempt"))
        assertEquals(1, after.getJSONObject("counts").optInt("upload_ack"))                       // own-session ACK counted
        val un = after.getJSONObject("unattributed_counts")
        assertEquals(2, un.optInt("upload_ack")); assertEquals(1, un.optInt("upload_unacknowledged"))
        assertEquals(1, after.getJSONObject("prior_sessions").getJSONObject("2026-10-12").getJSONObject("counts").optInt("upload_ack"))
    }

    @Test fun r1_timingAndHistogramSurviveLatePriorSessionTiming() {
        val h = B503Harness()
        h.engine.beforeSubmit(request())
        h.engine.onBuilt(request(), CaptureDelivery.ON_TIME, computed(), 10, 0)
        val cur = h.health()
        val hist = cur.getJSONObject("elapsed_histogram").toString(); val max = cur.optLong("max_elapsed_ms")
        // Yesterday's LATE result finishing after midnight must not overwrite today's histogram or counters.
        val y = request(slot = "2026-10-12|78", ctxDate = "2026-10-12", pollTime = "15:25",
            result = B503Harness.result("2026-10-12T15:25:00+0530"), attempt = "att-y")
        h.engine.beforeSubmit(y)
        val ySnap = JSONObject(pythonSnapshot("2026-10-12T15:25:04+0530")).put("session_date", "2026-10-12").toString()
        h.engine.onBuilt(y, CaptureDelivery.LATE, computed(ySnap), 10, 0)
        val after = h.health()
        assertEquals(SESSION, after.getString("session_date"))
        assertEquals(hist, after.getJSONObject("elapsed_histogram").toString())
        assertEquals(max, after.optLong("max_elapsed_ms"))
        assertEquals(1, after.getJSONObject("counts").optInt("attempt"))
        assertEquals(1, after.getJSONObject("prior_sessions").getJSONObject("2026-10-12").getJSONObject("counts").optInt("attempt"))
    }

    @Test fun r1_priorSessionsAreBounded() {
        var st: JSONObject? = null
        st = SnapshotStageHealth.record(null, SESSION, "attempt", "$SESSION|1", 0, null)
        for (d in 1..9) st = SnapshotStageHealth.record(st.toString(), "2026-10-0$d", "upload_ack", "2026-10-0$d|1", 0, null)
        assertEquals(SESSION, st!!.getString("session_date"))
        assertTrue(st.getJSONObject("prior_sessions").length() <= SnapshotStageHealth.MAX_PRIOR_SESSIONS)
    }

    @Test fun r1_drainCarriesThePayloadsOwnOrigin() {
        val s = JSONObject().put("poll_summary_json", JSONObject().put("capture_provenance",
            JSONObject().put("capture_key", "$SESSION|8")).toString()).toString()
        assertEquals("$SESSION|8", BrainSnapshotOutbox.originKeyOf(s))
        assertEquals(null, BrainSnapshotOutbox.originKeyOf("""{"poll_summary_json":{}}"""))
        assertEquals(null, BrainSnapshotOutbox.originKeyOf("""{"poll_summary_json":{"capture_provenance":{"capture_key":"x|99"}}}"""))
    }

    // ------------------------------------------------------------------ R2
    private enum class Path { INFLIGHT_SKIP, FAILED, FINISHER_REJECTED, BUILD_FAILED }
    private fun drive(h: B503Harness, p: Path) {
        val r = request()
        when (p) {
            Path.INFLIGHT_SKIP -> h.engine.onSkipped(r, CaptureCoordinator.Outcome.SKIPPED_INFLIGHT, "$SESSION|7", 5000)
            Path.FAILED -> { h.engine.beforeSubmit(r); h.engine.onFailed(r, CaptureCoordinator.Failure.Exception(IllegalStateException("x"), true), 0) }
            Path.FINISHER_REJECTED -> { h.engine.beforeSubmit(r); h.engine.onFinisherRejected(r, CaptureDelivery.LATE) }
            Path.BUILD_FAILED -> { h.engine.beforeSubmit(r); h.engine.onBuilt(r, CaptureDelivery.LATE, computed("not json"), 0, 0) }
        }
    }
    private val expected = mapOf(Path.INFLIGHT_SKIP to "INFLIGHT_SKIPPED_SLOT", Path.FAILED to "LATE_EXCEPTION",
        Path.FINISHER_REJECTED to "FINISHER_REJECTED", Path.BUILD_FAILED to null)

    @Test fun r2_everyTerminalPath_fullOrUnwritableLedger_keepsMarker_thenDeliversOnceAfterRepair() {
        for (p in Path.values()) for (mode in listOf("full", "unwritable")) {
            val bad = FaultyFs().apply { if (mode == "unwritable") failWriteMatching = Regex(".") }
            val a = B503Harness(eventCapacity = if (mode == "full") 0 else 2000, eventsFs = bad)
            drive(a, p)
            assertEquals("$p/$mode", 0, a.eventTypes().size)
            assertEquals("$p/$mode: marker must survive", 1, a.journal.inventory().intents)
            // Restart while still broken: no deletion, no false finalization.
            val still = B503Harness(root = a.root, runId = "run-B", eventCapacity = if (mode == "full") 0 else 2000, eventsFs = bad)
            still.engine.reconcile(emptySet())
            assertEquals("$p/$mode", 1, still.journal.inventory().intents)
            // Repaired.
            val b = B503Harness(root = a.root, runId = "run-C")
            b.engine.reconcile(emptySet()); b.engine.reconcile(emptySet())
            val types = b.eventTypes()
            assertEquals("$p/$mode: $types", 1, types.size)
            expected[p]?.let { assertEquals("$p/$mode", it, types.single()) }
            assertEquals("$p/$mode", 0, b.journal.inventory().intents)
        }
    }

    @Test fun r2_crashBeforeAndAfterTerminalEventEnqueue_exactlyOnceAfterRestart() {
        for (point in listOf("terminal_marked", "terminal_event_enqueued")) {
            val a = B503Harness()
            a.engine.crashPoint = { if (it == point) throw SimulatedProcessDeath(it) }
            val r = request(); a.engine.beforeSubmit(r)
            try { a.engine.onFailed(r, CaptureCoordinator.Failure.Exception(IllegalStateException("x"), true), 0) } catch (_: SimulatedProcessDeath) {}
            assertEquals(point, 1, a.journal.inventory().intents)
            val b = B503Harness(root = a.root, runId = "run-B")
            b.engine.reconcile(emptySet()); b.engine.reconcile(emptySet())
            assertEquals(point, listOf("LATE_EXCEPTION"), b.eventTypes())
            assertEquals(point, 0, b.journal.inventory().intents)
        }
    }

    @Test fun r2_bundleWithoutIntent_validIsResumed_corruptIsQuarantinedNotAbandoned() {
        for (corrupt in listOf(false, true)) {
            val fs = FaultyFs().apply { failWriteMatching = Regex("intent") }
            val a = B503Harness(journalFs = fs)
            a.engine.crashPoint = { if (it == "bundle") throw SimulatedProcessDeath(it) }
            val r = request(); a.engine.beforeSubmit(r)
            try { a.engine.onBuilt(r, CaptureDelivery.LATE, computed(), 0, 0) } catch (_: SimulatedProcessDeath) {}
            File(a.journal.dir, "${r.attemptId}.intent.json").delete()
            a.journal.dir.listFiles()!!.filter { it.name.contains(".tmp") }.forEach { it.delete() }
            assertEquals(0, a.journal.inventory().intents); assertEquals(1, a.journal.inventory().bundles)
            if (corrupt) {
                val f = File(a.journal.dir, "${r.attemptId}.bundle.json")
                val b = JSONObject(f.readText()); b.getJSONObject("identity").put("capture_key", "$SESSION|9"); f.writeText(b.toString())
            }
            val b = B503Harness(root = a.root, runId = "run-B")
            val rep = b.engine.reconcile(emptySet())
            if (!corrupt) {
                assertEquals(1, rep.resumed); assertEquals(1, b.snapshotFiles().size)
                assertEquals(listOf("JOURNAL_STORAGE_FAULT", "PARTIAL_RECOVERY_RESUMED"), b.eventTypes().sorted()) // the intent fault itself is evidence
                assertEquals(0, b.journal.inventory().bundles); assertEquals(0, b.journal.inventory().intents)
            } else {
                assertEquals(1, rep.uncertain); assertEquals(0, b.snapshotFiles().size)
                assertEquals(listOf("JOURNAL_STORAGE_FAULT", "UNCERTAIN_INTERRUPTED"), b.eventTypes().sorted())
                assertTrue("original bytes retained", b.journal.inventory().quarantined >= 1)
            }
        }
    }

    @Test fun r2_admissionDuringReconcile_isLiveNotUncertain() {
        val h = B503Harness()
        val worker = ManualExecutor()
        val c = h.coordinator(worker = worker, finisher = ManualFinisher("fin", 2))
        val snapshotBefore = c.liveAttemptIds()  // round-1 behaviour: snapshot taken before the scan
        h.engine.reconcileHook = { if (it == "scan_start") c.submit(request(), 0L) { pythonSnapshot(pollTs) } }
        val rep = h.engine.reconcile { c.isLive(it) }
        assertEquals(1, rep.live); assertEquals(0, rep.uncertain); assertEquals(0, rep.lost)
        assertEquals(emptyList<String>(), h.eventTypes())
        assertTrue(snapshotBefore.isEmpty()) // the stale set would have labelled this live attempt uncertain
        assertEquals(1, h.journal.inventory().intents)
    }

    @Test fun r2_finisherRejection_recordsBeforeReleasingOwnership() {
        val h = B503Harness()
        val worker = ManualExecutor(); val fin = ManualFinisher("fin", 0).apply { reject = true }
        var liveDuringRecord: Boolean? = null
        lateinit var c: CaptureCoordinator
        val owner = object : CaptureCoordinator.Owner by h.engine {
            override fun onFinisherRejected(req: FrozenCaptureRequest, delivery: CaptureDelivery) {
                liveDuringRecord = c.isLive(req.attemptId) || c.pendingAdmissions() > 0
                h.engine.onFinisherRejected(req, delivery)
            }
        }
        c = CaptureCoordinator(worker, fin, InlineFinisher(), owner,
            CaptureCoordinator.Waiter { _, _ -> throw java.util.concurrent.TimeoutException() },
            monoClock = { 1L }, cpuClockMs = { 1L }, stuckAfterMs = 120_000L, scheduleStuckCheck = { _, _ -> })
        assertEquals(CaptureCoordinator.Outcome.LATE_PENDING, c.submit(request(), 1L) { pythonSnapshot(pollTs) })
        worker.runAll()
        assertEquals(true, liveDuringRecord)
        assertEquals(0, c.pendingAdmissions())
        assertEquals(listOf("FINISHER_REJECTED"), h.eventTypes())
        assertEquals(0, h.journal.inventory().intents)
    }

    // ------------------------------------------------------------------ R3
    private fun bigSnapshot(decisions: Int, ctxOverride: String? = null): String {
        val o = JSONObject(pythonSnapshot(pollTs, decisions = decisions))
        val ctx = JSONObject(o.getString("context_json")).put("c3_finalization_frame", JSONObject().put("padding", "x".repeat(2_200_000)))
        return o.put("context_json", ctxOverride ?: ctx.toString()).toString()
    }

    @Test fun r3_actualCompactorTrimsSnapshot_butCompactAndLegacyKeepAll128() {
        val h = B503Harness()
        val b = h.engine.build(request(), CaptureDelivery.LATE, computed(bigSnapshot(128)), 0, 0)
        val snap = JSONObject(b.channels.getString("snapshot"))
        val ctx = snap.optJSONObject("context_json") ?: JSONObject(snap.optString("context_json", "{}"))
        assertFalse("compactor really trimmed", ctx.has("snapshot_pc2_authority_decisions"))
        assertEquals("PENDING", b.states["compact"]); assertEquals("PENDING", b.states["legacy"])
        assertEquals(128, JSONArray(b.channels.getString("legacy")).length())
    }

    @Test fun r3_compactDisabled_stillFreezesLegacy() {
        val h = B503Harness(compactEnabled = false)
        val b = h.engine.build(request(), CaptureDelivery.LATE, computed(bigSnapshot(128)), 0, 0)
        assertEquals("NOT_EXPECTED", b.states["compact"]); assertEquals("PENDING", b.states["legacy"])
        assertEquals(128, JSONArray(b.channels.getString("legacy")).length())
    }

    @Test fun r3_emptyWarmupIsNotExpected_malformedIsBuildFailed() {
        val h = B503Harness()
        assertEquals("NOT_EXPECTED", h.engine.build(request(), CaptureDelivery.LATE, computed(bigSnapshot(0)), 0, 0).states["legacy"])
        assertEquals("BUILD_FAILED", h.engine.build(request(), CaptureDelivery.LATE, computed(bigSnapshot(0, "{not json")), 0, 0).states["legacy"])
        val badArray = JSONObject().put("snapshot_pc2_authority_decisions", "oops").toString()
        assertEquals("BUILD_FAILED", h.engine.build(request(), CaptureDelivery.LATE, computed(bigSnapshot(0, badArray)), 0, 0).states["legacy"])
    }

    // ------------------------------------------------------------------ R4 client / R5
    private class Clock(var ms: Long)

    @Test fun r4_pt429CoolsDownUntilNextIstDay_withoutLosingEvents_andFullQueueStaysInJournal() {
        val root = Files.createTempDirectory("b503-drain").toFile()
        val out = FrozenEvidenceOutbox(root, "events", 2, 8L * 1024 * 1024)
        out.enqueue("cev_a", "{\"event_id\":\"cev_a\"}"); out.enqueue("cev_b", "{\"event_id\":\"cev_b\"}")
        val clock = Clock(1_791_865_242_000L) // 13 Oct 2026 ~11:10 IST
        val d = CaptureEventDrainer(out, File(root, "cooldown"), { clock.ms })
        var sends = 0
        val quota = { _: String, _: String -> sends++; CaptureEventProtocol.Response(400, """{"code":"PT429","message":"CAPTURE_EVENT_DAILY_QUOTA"}""") }
        val r1 = d.drain(20, quota)
        assertEquals(1, sends); assertEquals(2, r1.status.pending)
        assertEquals(CaptureEventDrainer.nextIstDayStartMs(clock.ms) + 300_000L, r1.cooldownUntilMs)
        clock.ms += 3_600_000L
        assertTrue(d.drain(20, quota).skippedForCooldown); assertEquals(1, sends)
        // Full queue while cooling down: a new terminal outcome stays durable in its journal marker.
        val h = B503Harness(root = root, eventCapacity = 0)
        drive(h, Path.FAILED); assertEquals(1, h.journal.inventory().intents)
        clock.ms = d.cooldownUntilMs() + 1
        val ack = { id: String, _: String -> sends++; CaptureEventProtocol.Response(200, """{"status":"INSERTED","event_id":"$id"}""") }
        assertEquals(0, d.drain(20, ack).status.pending)
        assertFalse(File(root, "cooldown").exists())
    }

    @Test fun r5_ackMustNameTheSubmittedEvent_viaDrainer() {
        val root = Files.createTempDirectory("b503-ack").toFile()
        val out = FrozenEvidenceOutbox(root, "events", 10, 1L shl 20)
        out.enqueue("cev_x", "{}")
        val d = CaptureEventDrainer(out, File(root, "cd"))
        d.drain(5) { _, _ -> CaptureEventProtocol.Response(200, """{"status":"INSERTED"}""") }
        d.drain(5) { _, _ -> CaptureEventProtocol.Response(200, """{"status":"DUPLICATE","event_id":"cev_other"}""") }
        assertEquals(1, out.status().pending)
        d.drain(5) { id, _ -> CaptureEventProtocol.Response(200, """{"status":"DUPLICATE","event_id":"$id"}""") }
        assertEquals(0, out.status().pending)
    }

    // ------------------------------------------------------------------ R7
    /** Android's org.json: optString on JSONObject.NULL yields the string "null" (JSONObject.toString(NULL)). */
    private fun androidOptString(o: JSONObject, k: String): String = if (o.has(k) && o.isNull(k)) "null" else o.optString(k)

    @Test fun r7_nativeVersionStamp_handlesAndroidNullSemantics() {
        val explicitNull = JSONObject().put("app_version", JSONObject.NULL)
        assertFalse("round-1 predicate on Android never fired", androidOptString(explicitNull, "app_version").isBlank())
        assertEquals("2.6.72", PaperMenuCapture.stampNativeVersion(explicitNull, "2.6.72").getString("app_version"))
        assertEquals("2.6.72", PaperMenuCapture.stampNativeVersion(JSONObject().put("app_version", "null"), "2.6.72").getString("app_version"))
        assertEquals("2.6.72", PaperMenuCapture.stampNativeVersion(JSONObject(), "2.6.72").getString("app_version"))
        assertEquals("2.6.72", PaperMenuCapture.stampNativeVersion(JSONObject().put("app_version", " "), "2.6.72").getString("app_version"))
        assertEquals("2.6.72", PaperMenuCapture.stampNativeVersion(JSONObject().put("app_version", 5), "2.6.72").getString("app_version"))
        assertEquals("2.6.71", PaperMenuCapture.stampNativeVersion(JSONObject().put("app_version", "2.6.71"), "2.6.72").getString("app_version"))
        assertNotEquals("null", PaperMenuCapture.stampNativeVersion(JSONObject(JSONObject().put("app_version", JSONObject.NULL).toString()), "2.6.72").optString("app_version"))
    }
}
