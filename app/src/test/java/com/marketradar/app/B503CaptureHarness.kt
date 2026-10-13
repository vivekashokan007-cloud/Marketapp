package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.nio.file.Files
import java.util.concurrent.Executor
import java.util.concurrent.atomic.AtomicLong

/**
 * b503 deterministic harness around the REAL production coordinator, engine, journal and
 * outboxes. Only Python (a fake compute lambda), the Android local cache and the clocks are
 * substituted. Nothing here touches a network or a real device.
 */
class ManualExecutor : Executor {
    val queue = ArrayDeque<Runnable>()
    override fun execute(command: Runnable) { queue.addLast(command) }
    fun runNext(): Boolean { val r = queue.removeFirstOrNull() ?: return false; r.run(); return true }
    fun runAll(): Int { var n = 0; while (runNext()) n++; return n }
}

class ManualFinisher(name: String, capacity: Int) : CaptureFinisher(name, capacity, Executor { it.run() }) {
    val jobs = ArrayDeque<Runnable>()
    var reject = false
    override fun offer(job: Runnable): Boolean {
        if (reject || jobs.size >= capacity) { recordOverflow(); return false }
        jobs.addLast(job); return true
    }
    fun runAll(): Int { var n = 0; while (true) { val j = jobs.removeFirstOrNull() ?: break; try { j.run() } catch (_: Throwable) {}; n++ }; return n }
}

/** Runs diagnostics inline (control lane). */
class InlineFinisher : CaptureFinisher("inline", 1_000, Executor { it.run() })

class FaultyFs : CaptureFs() {
    var failWriteMatching: Regex? = null
    var failRenameMatching: Regex? = null
    override fun writeSynced(tmp: File, content: String) {
        if (failWriteMatching?.containsMatchIn(tmp.name) == true) {
            tmp.writeText(content.take(10)) // partial bytes, then the fsync "fails"
            throw java.io.IOException("ENOSPC simulated")
        }
        super.writeSynced(tmp, content)
    }
    override fun rename(from: File, to: File): Boolean {
        if (failRenameMatching?.containsMatchIn(to.name) == true) return false
        return super.rename(from, to)
    }
}

class B503Harness(
    val root: File = Files.createTempDirectory("b503").toFile(),
    val runId: String = "run-A",
    var compactEnabled: Boolean = true,
    val journalFs: CaptureFs = CaptureFs.REAL,
    val legacyFs: CaptureFs = CaptureFs.REAL,
    val sharedLocalCache: MutableList<String> = mutableListOf(),
    val mono: AtomicLong = AtomicLong(1_000_000L),
    val eventCapacity: Int = 2000,
    val eventsFs: CaptureFs = CaptureFs.REAL
) {
    val logs = mutableListOf<String>()
    var healthText: String? = null
    val channelFaults = mutableMapOf<String, () -> Unit>()
    val enqueueCalls = mutableMapOf<String, Int>()
    val legacy = FrozenEvidenceOutbox(root, CaptureRuntime.LEGACY_DIR, 150, 64L * 1024 * 1024, legacyFs)
    val events = FrozenEvidenceOutbox(root, CaptureRuntime.EVENTS_DIR, eventCapacity, 8L * 1024 * 1024, eventsFs)
    val journal = CaptureJournal(root, journalFs)

    private fun hit(ch: String) { enqueueCalls[ch] = (enqueueCalls[ch] ?: 0) + 1; channelFaults[ch]?.invoke() }

    val channels = object : CaptureChannels {
        override fun compactEnabled() = compactEnabled
        override fun stageHealth(): JSONObject = engine.healthSnapshot()
        override fun appendLocalCache(sessionDate: String, snapshotPayload: String): Boolean {
            hit("local_cache"); sharedLocalCache += snapshotPayload; return true
        }
        override fun enqueueSnapshot(payload: String): Boolean { hit("snapshot"); return BrainSnapshotOutbox.enqueuePayload(root, payload) }
        override fun enqueueCompact(envelope: String): Boolean {
            hit("compact")
            val env = JSONObject(envelope)
            return Pc2TelemetryOutbox.enqueue(root, Pc2CompactBatch.Built(env.getJSONObject("policy_row"), env.getJSONObject("batch_row"), JSONObject()))
        }
        override fun enqueueGenerated(payload: String): Boolean { hit("generated"); return GeneratedCandidateOutbox.enqueuePayload(root, payload) }
        override fun enqueueLegacy(pollTs: String, payload: String): Boolean { hit("legacy"); return legacy.enqueue(pollTs, payload) }
    }

    val engine: CaptureEngine = CaptureEngine(journal, channels, object : CaptureHealthStore {
        override fun read() = healthText
        override fun write(value: String): Boolean { healthText = value; return true }
    }, events, runId, 4242, { l, m -> synchronized(logs) { logs += "$l $m" } },
        wallClock = { 1_791_865_242_000L }, monoClock = { mono.get() })

    fun coordinator(
        worker: Executor = ManualExecutor(),
        finisher: CaptureFinisher = ManualFinisher("fin", 2),
        waiter: CaptureCoordinator.Waiter = CaptureCoordinator.Waiter { f, _ -> if (f.isDone) f.get() else throw java.util.concurrent.TimeoutException() },
        stuckChecks: MutableList<() -> Unit> = mutableListOf(),
        stuckAfterMs: Long = 120_000L
    ) = CaptureCoordinator(worker, finisher, InlineFinisher(), engine, waiter, monoClock = { mono.get() },
        cpuClockMs = { 7L }, stuckAfterMs = stuckAfterMs, scheduleStuckCheck = { _, c -> stuckChecks += c })

    // ---------------------------------------------------------------- inventory
    fun files(dir: String): List<File> = File(root, dir).listFiles()?.filter { it.isFile && it.name.endsWith(".json") }.orEmpty()
    fun snapshotFiles() = files("brain_snapshot_outbox_v1")
    fun generatedFiles() = files("generated_candidate_outbox_v1")
    fun compactPending() = Pc2TelemetryOutbox.pending(root)
    fun legacyStatus() = legacy.status()
    fun eventTypes(): List<String> = files(CaptureRuntime.EVENTS_DIR).map {
        JSONObject(JSONObject(it.readText()).getString("payload")).getString("event_type") }.sorted()
    fun health(): JSONObject = JSONObject(healthText ?: "{}")
    fun count(event: String) = health().optJSONObject("counts")?.optInt(event) ?: 0

    fun inventory(): JSONObject = JSONObject()
        .put("snapshot_pending", snapshotFiles().size)
        .put("snapshot_poll_ts", JSONArray(snapshotFiles().map { JSONObject(it.readText()).optString("poll_ts") }.sorted()))
        .put("generated_pending", generatedFiles().size)
        .put("generated_rows", generatedFiles().sumOf { JSONObject(it.readText()).getJSONArray("rows").length() })
        .put("compact_pending", compactPending().size)
        .put("legacy_pending", legacyStatus().pending)
        .put("legacy_rows", File(root, CaptureRuntime.LEGACY_DIR).listFiles()?.filter { it.isFile && it.name.endsWith(".json") }
            ?.sumOf { JSONArray(JSONObject(it.readText()).getString("payload")).length() } ?: 0)
        .put("local_cache_entries", sharedLocalCache.size)
        .put("journal_intents", journal.inventory().intents)
        .put("journal_bundles", journal.inventory().bundles)
        .put("events", JSONArray(eventTypes()))
        .put("completed_keys", JSONArray(journal.completedKeys()))

    companion object {
        const val SESSION = "2026-10-13"

        fun decisions(n: Int): JSONArray = JSONArray().also { a ->
            repeat(n) { i -> a.put(JSONObject().put("variable_name", "v${i % 7}").put("bucket", i % 3).put("support_count", i)) }
        }

        /** What take_poll_snapshot returns: JSONB fields pre-serialised as strings (brain.py contract). */
        fun pythonSnapshot(pollTs: String, decisions: Int = 128, recommendationId: String? = "rec-$pollTs"): String {
            val ctx = JSONObject().put("snapshot_brain_version", "2.6.72")
            if (decisions > 0) {
                ctx.put("snapshot_pc2_authority_decisions", decisions(decisions))
                    .put("snapshot_pc2_authority_policy", JSONObject().put("version", "pc2_authority_policy_v2"))
            }
            return JSONObject()
                .put("session_date", SESSION).put("poll_ts", pollTs)
                .put("recommendation_id", recommendationId ?: JSONObject.NULL)
                .put("action", "WAIT").put("confidence", "None")
                .put("context_json", ctx.toString())
                .put("poll_summary_json", JSONObject().put("poll_number", 8).toString())
                .put("top_candidates_json", "[]")
                .toString()
        }

        fun factPack(slotTs: String, nf: Int, bnf: Int): JSONObject {
            val c = JSONArray()
            repeat(nf) { i -> c.put(JSONObject().put("candidate_id", "NF-$i").put("lane", "NF_intraday").put("index", "NF").put("rank", i + 1)) }
            repeat(bnf) { i -> c.put(JSONObject().put("candidate_id", "BNF-$i").put("lane", "BNF_intraday").put("index", "BNF").put("rank", i + 1)) }
            return JSONObject().put("poll_timestamp", slotTs).put("session_date", SESSION).put("candidates", c)
                .put("candidate_counts", JSONObject().put("generated", nf + bnf)).put("brain_version", "2.6.72").put("app_version", "2.6.72")
        }

        fun result(slotTs: String?, nf: Int = 30, bnf: Int = 30): JSONObject {
            val r = JSONObject().put("brain_version", "2.6.72").put("watchlist", JSONArray())
            if (slotTs != null) r.put("elephant_fact_pack", factPack(slotTs, nf, bnf))
            return r
        }

        fun request(slot: String = "$SESSION|8", ctxDate: String = SESSION, pollTime: String = "09:50",
                    result: JSONObject = result("${SESSION}T09:50:00+0530"), ctx: JSONObject = JSONObject().put("today_ist", ctxDate),
                    runId: String = "run-A", attempt: String = "att-${slot.substringAfter('|')}", version: String = "2.6.72", code: Int = 503) =
            FrozenCaptureRequest.freeze(slot, ctxDate, pollTime, result, ctx, "[{\"t\":\"$pollTime\"}]", version, code, "2.6.72", runId,
                monoClock = { 1_000_000L }, wallClock = { 1_791_865_237_887L }, attemptId = attempt)
    }
}
