package com.marketradar.app

import com.marketradar.app.B503Harness.Companion.SESSION
import com.marketradar.app.B503Harness.Companion.pythonSnapshot
import com.marketradar.app.B503Harness.Companion.request
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.File
import java.util.concurrent.Executor

/**
 * b503 return artifact 5: reproducible harness results from the REAL coordinator/engine/journal/outboxes with
 * fake Python, fake transport and a faulty filesystem. Writes stable JSON (no wall-clock or temp paths) to
 * $B503_HARNESS_OUT when set, and always to stdout. Regenerate with:
 *   B503_HARNESS_OUT=/tmp/b503_harness.json ./gradlew :app:testDebugUnitTest --tests com.marketradar.app.B503HarnessReportTest
 */
class B503HarnessReportTest {
    private val pollTs = "${SESSION}T09:50:42+0530"

    private fun stable(h: B503Harness): JSONObject = h.inventory().put("health_counts", h.health().optJSONObject("counts") ?: JSONObject())

    @Test fun produceHarnessReport() {
        GeneratedCandidateRows.logger = { _, _ -> }
        val report = JSONObject().put("schema", "b503_harness_report_v1").put("brain_version", "2.6.72")

        // 1. Python completes after the 4 s wait (forced > wait), original Future delivered late, once.
        run {
            val h = B503Harness(); val worker = ManualExecutor(); val fin = ManualFinisher("fin", 2)
            val c = h.coordinator(worker = worker, finisher = fin)
            var calls = 0
            val out = c.submit(request(), 4_000L) { calls++; pythonSnapshot(pollTs) }
            val whilePending = stable(h)
            worker.runAll(); fin.runAll()
            report.put("forced_late_completion", JSONObject().put("submit_outcome", out.name).put("python_calls", calls)
                .put("while_pending", whilePending).put("after", stable(h)))
        }
        // 2. Exception early and late.
        run {
            val h = B503Harness(); val c = h.coordinator(worker = Executor { it.run() })
            val early = c.submit(request(), 4_000L) { throw IllegalStateException("py") }
            val h2 = B503Harness(); val w = ManualExecutor(); val f = ManualFinisher("fin", 2)
            val late = h2.coordinator(worker = w, finisher = f).submit(request(), 4_000L) { throw IllegalStateException("py-late") }
            w.runAll(); f.runAll()
            report.put("exception", JSONObject().put("early_outcome", early.name).put("early", stable(h))
                .put("late_outcome", late.name).put("late", stable(h2)))
        }
        // 3. Finisher queue saturation (late path) and inline on-time fallback.
        run {
            val h = B503Harness(); val w = ManualExecutor(); val fin = ManualFinisher("fin", 2).apply { reject = true }
            val out = h.coordinator(worker = w, finisher = fin).submit(request(), 4_000L) { pythonSnapshot(pollTs) }
            w.runAll()
            report.put("finisher_saturation_late", JSONObject().put("submit_outcome", out.name).put("overflow", fin.overflowCount()).put("state", stable(h)))
        }
        // 4. Process death after each local checkpoint, then restart in a new run.
        val crashes = JSONObject()
        for (point in listOf("intent", "bundle", "built", "local_cache", "snapshot", "compact", "legacy", "generated")) {
            val cache = mutableListOf<String>()
            val a = B503Harness(runId = "run-A", sharedLocalCache = cache)
            a.engine.crashPoint = { if (it == point) throw SimulatedProcessDeath(it) }
            val req = request()
            try {
                a.engine.beforeSubmit(req)
                a.engine.onBuilt(req, CaptureDelivery.LATE, CaptureCoordinator.Computed(pythonSnapshot(pollTs), 4792, 1200), 4000, 0)
            } catch (_: SimulatedProcessDeath) { }
            val b = B503Harness(root = a.root, runId = "run-B", sharedLocalCache = cache)
            val r = b.engine.reconcile(emptySet())
            crashes.put(point, JSONObject().put("reconcile", JSONObject().put("resumed", r.resumed).put("finalized", r.finalized)
                .put("uncertain", r.uncertain).put("lost", r.lost).put("still_pending", r.stillPending)).put("after_restart", stable(b)))
        }
        report.put("crash_after_checkpoint_then_restart", crashes)
        // 5. Upload outage, process restart, independent recovery of every channel.
        run {
            val h = B503Harness(); val fin = ManualFinisher("fin", 2)
            h.coordinator(worker = Executor { it.run() }, finisher = fin).submit(request(), 4_000L) { pythonSnapshot(pollTs) }; fin.runAll()
            BrainSnapshotOutbox.drain(h.root) { BrainSnapshotOutbox.Outcome.RETRY }
            GeneratedCandidateOutbox.drain(h.root) { GeneratedCandidateOutbox.Outcome.RETRY }
            Pc2TelemetryOutbox.drain(h.root) { Pc2TelemetryOutbox.Outcome.RETRY }
            h.legacy.drain { _, _ -> FrozenEvidenceOutbox.Outcome.RETRY }
            val during = stable(h)
            Pc2TelemetryOutbox.simulateProcessRestart()
            val acks = JSONObject()
                .put("snapshot", BrainSnapshotOutbox.drain(h.root) { BrainSnapshotOutbox.Outcome.ACKNOWLEDGED }.acknowledged)
                .put("generated", GeneratedCandidateOutbox.drain(h.root) { GeneratedCandidateOutbox.Outcome.ACKNOWLEDGED }.acknowledged)
                .put("compact", Pc2TelemetryOutbox.drain(h.root) { Pc2TelemetryOutbox.Outcome.ACKNOWLEDGED }.acknowledged)
                .put("legacy", FrozenEvidenceOutbox(h.root, CaptureRuntime.LEGACY_DIR, 150, 64L * 1024 * 1024)
                    .drain { _, _ -> FrozenEvidenceOutbox.Outcome.ACKNOWLEDGED }.acknowledged)
            report.put("upload_outage_then_restart", JSONObject().put("during_outage", during).put("acknowledged_after_restart", acks).put("after", stable(h)))
        }
        // 6. Event identities (stable across runs).
        report.put("event_ids", JSONObject()
            .put("LATE_DELIVERED", B503Harness().engine.eventIdFor("$SESSION|8", "att-8", "LATE_DELIVERED"))
            .put("LOST_PROCESS_ENDED_BEFORE_DURABLE_BUILD", B503Harness().engine.eventIdFor("$SESSION|8", "att-8", "LOST_PROCESS_ENDED_BEFORE_DURABLE_BUILD")))

        val text = report.toString(2)
        println(text)
        System.getenv("B503_HARNESS_OUT")?.let { File(it).writeText(text + "\n") }
        assertEquals("LATE_PENDING", report.getJSONObject("forced_late_completion").getString("submit_outcome"))
        assertEquals(1, report.getJSONObject("forced_late_completion").getInt("python_calls"))
        assertEquals(JSONArray(listOf("LOST_PROCESS_ENDED_BEFORE_DURABLE_BUILD")).toString(),
            crashes.getJSONObject("intent").getJSONObject("after_restart").getJSONArray("events").toString())
    }
}
