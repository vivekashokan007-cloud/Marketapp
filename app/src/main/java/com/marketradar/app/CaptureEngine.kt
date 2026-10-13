package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** Durable local channels the capture fan-out writes to (real outboxes in production). */
interface CaptureChannels {
    fun compactEnabled(): Boolean
    fun stageHealth(): JSONObject
    fun appendLocalCache(sessionDate: String, snapshotPayload: String): Boolean
    /** Exact sanitized snapshot bytes; true new, false identical already pending. */
    fun enqueueSnapshot(payload: String): Boolean
    fun enqueueCompact(envelope: String): Boolean
    fun enqueueGenerated(payload: String): Boolean
    fun enqueueLegacy(pollTs: String, payload: String): Boolean
}

/** Persisted health counters (prefs on device, a String in tests). */
interface CaptureHealthStore {
    fun read(): String?
    fun write(value: String): Boolean
}

/** Simulated process death for crash-point tests: an Error, so no Exception handler swallows it. */
class SimulatedProcessDeath(point: String) : Error("simulated_process_death:$point")

/**
 * b503 capture owner: durable intent, extracted production post-build fan-out, per-channel
 * checkpoints, restart reconciliation and the capture-event ledger. Android-free and
 * driven by the real [CaptureCoordinator] in tests.
 */
class CaptureEngine(
    private val journal: CaptureJournal,
    private val channels: CaptureChannels,
    private val health: CaptureHealthStore,
    private val events: FrozenEvidenceOutbox,
    private val runId: String,
    private val pid: Int,
    private val log: (Char, String) -> Unit,
    private val wallClock: () -> Long = { System.currentTimeMillis() },
    private val monoClock: () -> Long = { System.nanoTime() / 1_000_000L },
    private val onDurable: () -> Unit = {}
) : CaptureCoordinator.Owner {

    /** Test seam: called after each durable checkpoint ("intent", "bundle", "built", "<channel>"). */
    @Volatile var crashPoint: (String) -> Unit = {}

    private val healthLock = Any()

    // ------------------------------------------------------------------ health
    fun recordHealth(event: String, key: String, elapsedMs: Long = 0, exceptionClass: String? = null) {
        try {
            synchronized(healthLock) {
                val st = SnapshotStageHealth.record(health.read(), sessionOf(key), event, key, elapsedMs, exceptionClass)
                if (!health.write(st.toString())) log('E', "ML_SNAPSHOT_STAGE_HEALTH_SAVE_FAIL")
            }
            log('I', "ML_SNAPSHOT_STAGE: event=$event poll=$key elapsedMs=$elapsedMs exception=${exceptionClass ?: "none"}")
        } catch (t: Throwable) {
            log('E', "ML_SNAPSHOT_STAGE_HEALTH_FAIL: ${t.javaClass.simpleName}")
        }
    }

    private fun recordTiming(req: FrozenCaptureRequest, delivery: CaptureDelivery, elapsed: Long, c: CaptureCoordinator.Computed, postMs: Long) {
        try {
            synchronized(healthLock) {
                val st = SnapshotStageHealth.recordTiming(health.read(), req.sessionDate, req.captureKey, delivery.name,
                    elapsed, req.freezeMs, c.pythonMs, c.pythonCpuMs, postMs)
                health.write(st.toString())
            }
        } catch (_: Throwable) { /* diagnostics never abort capture */ }
    }

    private fun sessionOf(key: String) = key.substringBefore('|').takeIf { it.matches(Regex("20\\d{2}-\\d{2}-\\d{2}")) } ?: "unknown"

    fun healthSnapshot(): JSONObject = synchronized(healthLock) {
        runCatching { JSONObject(health.read() ?: "{}") }.getOrDefault(JSONObject())
    }

    // ------------------------------------------------------------------ events
    fun eventIdFor(captureKey: String, attemptId: String, type: String): String =
        "cev_" + FrozenCaptureRequest.sha256Hex("capture_event_v1|$captureKey|$attemptId|$type").take(32)

    /** Deterministic for a given (attempt, type): replays after restarts are byte-identical. */
    fun recordEvent(identity: JSONObject, type: String, reason: String, detail: JSONObject = JSONObject(),
                    occurredAtMs: Long = identity.optLong("requested_wall_ms")): String? {
        val key = identity.optString("capture_key")
        val attempt = identity.optString("attempt_id")
        val id = eventIdFor(key, attempt, type)
        val event = JSONObject()
            .put("schema", "capture_event_v1").put("event_id", id).put("event_type", type)
            .put("capture_key", key).put("session_date", identity.optString("session_date"))
            .put("slot_ordinal", identity.opt("slot_ordinal") ?: JSONObject.NULL)
            .put("origin_poll_ts", identity.opt("origin_poll_ts") ?: JSONObject.NULL)
            .put("attempt_id", attempt).put("run_id", identity.optString("run_id"))
            .put("app_version", identity.optString("app_version")).put("version_code", identity.optInt("version_code"))
            .put("brain_version", identity.optString("brain_version"))
            .put("reason", reason.take(120)).put("occurred_at_ms", occurredAtMs)
            .put("detail", boundedDetail(detail))
        return try {
            if (events.isPending(id)) return id
            events.enqueue(id, event.toString())
            log('W', "CAPTURE_EVENT: type=$type key=$key attempt=${attempt.take(8)} reason=$reason")
            id
        } catch (t: Throwable) {
            recordHealth("event_ledger_fault", key, 0, t.javaClass.simpleName)
            log('E', "CAPTURE_EVENT_LEDGER_FAULT: type=$type key=$key ${t.javaClass.simpleName}:${t.message?.take(80)}")
            null
        }
    }

    private fun boundedDetail(d: JSONObject): JSONObject {
        val text = d.toString()
        return if (text.length <= 2048) d else JSONObject().put("truncated", true).put("detail_sha256", FrozenCaptureRequest.sha256Hex(text))
    }

    // ------------------------------------------------------------------ Owner
    override fun beforeSubmit(req: FrozenCaptureRequest) {
        recordHealth("attempt", req.captureKey)
        try {
            journal.writeIntent(req, pid)
            crashPoint("intent")
        } catch (e: Exception) {
            recordHealth("journal_fault", req.captureKey, 0, e.javaClass.simpleName)
            recordEvent(req.identityJson(), "JOURNAL_STORAGE_FAULT", "intent_write_failed:${e.message?.take(60)}")
        }
    }

    override fun onLatePending(req: FrozenCaptureRequest, waitedMs: Long) {
        recordHealth("timeout", req.captureKey, waitedMs)
        recordHealth("late_pending", req.captureKey, waitedMs)
        log('W', "CAPTURE_LATE_PENDING: key=${req.captureKey} waitedMs=$waitedMs original call retained; will be delivered once if it completes")
    }

    override fun onStuck(req: FrozenCaptureRequest, ageMs: Long) {
        recordHealth("capture_stuck", req.captureKey, ageMs)
        recordEvent(req.identityJson(), "CAPTURE_STUCK", "not_complete_after_threshold",
            JSONObject().put("age_ms", ageMs))
        log('E', "CAPTURE_STUCK: key=${req.captureKey} ageMs=$ageMs (original call kept; no replacement started)")
    }

    override fun onSkipped(req: FrozenCaptureRequest, outcome: CaptureCoordinator.Outcome, blockingKey: String?, blockingAgeMs: Long) {
        if (outcome == CaptureCoordinator.Outcome.SKIPPED_DUPLICATE) {
            recordHealth("duplicate_skip", req.captureKey, blockingAgeMs)
            log('W', "CAPTURE_DUPLICATE_SKIP: key=${req.captureKey} already admitted in this process")
            return
        }
        // A different origin slot could not be captured: an explicit terminal loss for THAT slot.
        recordHealth("inflight_skip", req.captureKey, blockingAgeMs)
        recordHealth("terminal_loss", req.captureKey)
        recordEvent(req.identityJson(), "INFLIGHT_SKIPPED_SLOT", "prior_capture_inflight",
            JSONObject().put("blocking_capture_key", blockingKey ?: JSONObject.NULL).put("blocking_age_ms", blockingAgeMs))
        log('E', "PY_TIMEOUT_INFLIGHT_SKIP: key=poll.snapshot capture=${req.captureKey} blockedBy=$blockingKey ageMs=$blockingAgeMs")
    }

    override fun onFailed(req: FrozenCaptureRequest, failure: CaptureCoordinator.Failure, skippedWhileRunning: Int) {
        val (healthEvent, type) = when (failure) {
            is CaptureCoordinator.Failure.Exception -> if (failure.late) "late_exception" to "LATE_EXCEPTION" else "exception" to "EARLY_EXCEPTION"
            CaptureCoordinator.Failure.SubmitRejected -> "submit_rejected" to "SUBMIT_REJECTED"
            CaptureCoordinator.Failure.Cancelled -> "exception" to "CANCELLED"
        }
        val cls = (failure as? CaptureCoordinator.Failure.Exception)?.error?.javaClass?.simpleName
        recordHealth(healthEvent, req.captureKey, 0, cls)
        recordHealth("terminal_loss", req.captureKey)
        recordEvent(req.identityJson(), type, failure.reason,
            JSONObject().put("exception_class", cls ?: JSONObject.NULL).put("skipped_slots_while_running", skippedWhileRunning))
        if (!runCatching { journal.terminal(req.attemptId, failure.reason) }.getOrDefault(false)) {
            log('W', "CAPTURE_JOURNAL_TERMINAL_WRITE_FAILED: key=${req.captureKey}")
        }
        log('E', "ML_SNAPSHOT_STAGE_ABORTED: reason=${failure.reason} poll=${req.captureKey} class=${cls ?: "none"}")
    }

    override fun onFinisherRejected(req: FrozenCaptureRequest, delivery: CaptureDelivery) {
        recordHealth("finisher_rejected", req.captureKey)
        recordHealth("terminal_loss", req.captureKey)
        recordEvent(req.identityJson(), "FINISHER_REJECTED", "finisher_queue_full", JSONObject().put("delivery", delivery.name))
        runCatching { journal.terminal(req.attemptId, "finisher_rejected") }
        log('E', "CAPTURE_FINISHER_REJECTED: key=${req.captureKey} delivery=$delivery (bounded queue full; explicit loss)")
    }

    override fun onBuilt(req: FrozenCaptureRequest, delivery: CaptureDelivery, computed: CaptureCoordinator.Computed,
                         waitedMs: Long, skippedWhileRunning: Int) {
        val elapsed = monoClock() - (req.requestedMonoMs)
        recordHealth("built", req.captureKey, elapsed)
        if (delivery == CaptureDelivery.LATE) recordHealth("late_built", req.captureKey, elapsed)
        val t0 = monoClock()
        val result = try {
            persist(req, delivery, computed, waitedMs, skippedWhileRunning)
        } catch (e: Exception) {
            recordHealth("exception", req.captureKey, elapsed, e.javaClass.simpleName)
            recordHealth("terminal_loss", req.captureKey)
            recordEvent(req.identityJson(), "BUILD_FANOUT_FAILED", "post_build_exception",
                JSONObject().put("exception_class", e.javaClass.simpleName).put("delivery", delivery.name))
            runCatching { journal.terminal(req.attemptId, "post_build_exception") }
            log('E', "ML_SNAPSHOT_STAGE_ABORTED: reason=post_build_${e.javaClass.simpleName} poll=${req.captureKey}")
            null
        }
        val postMs = monoClock() - t0
        recordTiming(req, delivery, elapsed, computed, postMs)
        if (result != null) {
            val snapQueued = result.states["snapshot"] == "ENQUEUED"
            recordHealth(if (snapQueued) "queued" else "enqueue_failed", req.captureKey)
            if (delivery == CaptureDelivery.LATE) {
                if (snapQueued) recordHealth("late_queued", req.captureKey)
                recordEvent(req.identityJson(), "LATE_DELIVERED", "original_future_result",
                    JSONObject().put("elapsed_ms", elapsed).put("waited_ms", waitedMs).put("python_ms", computed.pythonMs)
                        .put("skipped_slots_while_running", skippedWhileRunning).put("poll_ts", result.pollTs)
                        .put("channels", JSONObject(result.states as Map<*, *>)))
            }
        }
        runCatching(onDurable)
    }

    // ------------------------------------------------------------------ fan-out
    class FanoutResult(val pollTs: String, val states: Map<String, String>, val finalized: Boolean)

    class BuiltChannels(val pollTs: String, val sessionDate: String, val channels: JSONObject, val states: MutableMap<String, String>)

    /** The extracted production post-build path (parse -> health -> compact -> compaction -> cohort -> legacy rows). */
    fun build(req: FrozenCaptureRequest, delivery: CaptureDelivery, computed: CaptureCoordinator.Computed,
              waitedMs: Long, skippedWhileRunning: Int): BuiltChannels {
        val rawSnapObj = JSONObject(computed.value)
        // brain.py returns JSONB payload fields as pre-serialised strings; re-parse (unchanged behaviour).
        for (field in listOf("primary_candidate_json", "context_json", "verdict_json", "market_forces_json",
            "poll_summary_json", "b1a_intraday_rv_json")) {
            val raw = rawSnapObj.optString(field, "")
            if (raw.isNotBlank()) { try { rawSnapObj.put(field, JSONObject(raw)) } catch (_: Exception) { } }
        }
        val summary = rawSnapObj.optJSONObject("poll_summary_json") ?: JSONObject()
        summary.put("snapshot_stage_health", channels.stageHealth())
        summary.put("capture_provenance", req.identityJson()
            .put("schema", "capture_provenance_v1")
            .put("delivery", delivery.name)
            .put("completed_wall_ms", wallClock())
            .put("python_ms", computed.pythonMs).put("python_thread_cpu_ms", computed.pythonCpuMs)
            .put("freeze_ms", req.freezeMs).put("waited_ms", waitedMs)
            .put("skipped_slots_while_running", skippedWhileRunning)
            .put("python_poll_ts_is_original", true))
        rawSnapObj.put("poll_summary_json", summary)
        val raw = rawSnapObj.optString("top_candidates_json", "")
        if (raw.isNotBlank()) { try { rawSnapObj.put("top_candidates_json", JSONArray(raw)) } catch (_: Exception) { } }

        val states = linkedMapOf<String, String>()
        val ch = JSONObject()
        // Compact PC2 envelope, before compaction can remove embedded telemetry (unchanged order).
        val compact = if (!channels.compactEnabled()) { states["compact"] = "NOT_EXPECTED"; ch.put("compact_reason", "device_not_registered"); null }
        else runCatching { Pc2CompactBatch.build(rawSnapObj) }
            .onFailure { log('E', "PC2_COMPACT_BUILD_FAIL: ${it.message}"); states["compact"] = "BUILD_FAILED" }.getOrNull()
                .also { if (it == null && states["compact"] == null) { states["compact"] = "NOT_EXPECTED"; ch.put("compact_reason", "no_pc2_decisions") } }
        if (compact != null) {
            rawSnapObj.optJSONObject("context_json")?.put("snapshot_pc2_authority_compact_ref", compact.snapshotRef)
            ch.put("compact", Pc2CompactBatch.canonicalJson(compact.envelope()))
            states["compact"] = "PENDING"
        }
        val snapObj = EvaluationLocalCache.compactBrainSnapshotForPersistence(rawSnapObj)
        val pollTs = snapObj.optString("poll_ts")
        val sessionDate = snapObj.optString("session_date", req.sessionDate).ifBlank { req.sessionDate }
        val snapshotPayload = BrainSnapshotPayloadSanitizer.sanitize(snapObj).toString()
        ch.put("snapshot", snapshotPayload)
        states["local_cache"] = "PENDING"
        states["snapshot"] = "PENDING"
        // Generated cohort from the FROZEN fact pack (never from the caller's mutable result).
        val factPack = req.factPackJson?.let { runCatching { JSONObject(it) }.getOrNull() }
        val rows = runCatching { factPack?.let { GeneratedCandidateRows.build(it, snapObj) } ?: JSONArray() }
            .onFailure { log('E', "ML_GENERATED_BUILD_FAIL: ${it.javaClass.simpleName}") }.getOrNull()
        when {
            rows == null -> states["generated"] = "BUILD_FAILED"
            rows.length() == 0 -> { states["generated"] = "NOT_EXPECTED"; ch.put("generated_reason", if (factPack == null) "no_fact_pack" else "zero_candidates") }
            else -> {
                ch.put("generated", JSONObject().put("poll_ts", rows.getJSONObject(0).getString("snapshot_poll_ts")).put("rows", rows).toString())
                states["generated"] = "PENDING"
            }
        }
        val legacy = runCatching { Pc2LegacyAuthorityRows.build(snapObj) }.getOrNull()
        when {
            legacy == null || !legacy.ok -> states["legacy"] = "BUILD_FAILED"
            legacy.rows == null -> { states["legacy"] = "NOT_EXPECTED"; ch.put("legacy_reason", "no_retained_decisions") }
            else -> { ch.put("legacy", legacy.rows.toString()); ch.put("legacy_poll_ts", legacy.pollTs); states["legacy"] = "PENDING" }
        }
        ch.put("session_date", sessionDate).put("poll_ts", pollTs)
        // Initial channel expectations travel with the bytes, so a crash between the bundle
        // write and the BUILT stage update can still resume exactly the expected channels.
        ch.put("initial_states", JSONObject(states as Map<*, *>))
        return BuiltChannels(pollTs, sessionDate, ch, states)
    }

    fun persist(req: FrozenCaptureRequest, delivery: CaptureDelivery, computed: CaptureCoordinator.Computed,
                waitedMs: Long, skippedWhileRunning: Int): FanoutResult {
        val built = build(req, delivery, computed, waitedMs, skippedWhileRunning)
        var durable = true
        try {
            val sha = journal.writeBundle(req.attemptId, built.channels)
            crashPoint("bundle")
            val ok = journal.update(req.attemptId) {
                it.put("stage", "BUILT").put("bundle_sha256", sha).put("delivery", delivery.name)
                    .put("poll_ts", built.pollTs).put("channels", JSONObject(built.states as Map<*, *>))
            }
            if (!ok) throw java.io.IOException("intent_missing_or_regressed")
            crashPoint("built")
        } catch (e: Exception) {
            durable = false
            recordHealth("journal_fault", req.captureKey, 0, e.javaClass.simpleName)
            recordEvent(req.identityJson(), "JOURNAL_STORAGE_FAULT", "bundle_write_failed:${e.message?.take(60)}")
        }
        val states = fanOut(req.attemptId, req.captureKey, built.channels, built.states, durable)
        return FanoutResult(built.pollTs, states, completeIfResolved(req.attemptId, req.captureKey, states, durable))
    }

    /** Enqueue every unresolved channel from the frozen bytes; checkpoint each one. */
    private fun fanOut(attemptId: String, captureKey: String, ch: JSONObject, states: MutableMap<String, String>, durable: Boolean): Map<String, String> {
        for (name in CaptureJournal.CHANNELS) {
            val st = states[name] ?: continue
            if (st != "PENDING" && st != "FAILED_RETRYABLE") continue
            val next = try {
                val ok = when (name) {
                    "local_cache" -> channels.appendLocalCache(ch.optString("session_date"), ch.getString("snapshot"))
                    "snapshot" -> { channels.enqueueSnapshot(ch.getString("snapshot")); true }
                    "compact" -> { channels.enqueueCompact(ch.getString("compact")); true }
                    "generated" -> { channels.enqueueGenerated(ch.getString("generated")); true }
                    "legacy" -> { channels.enqueueLegacy(ch.optString("legacy_poll_ts", ch.optString("poll_ts")), ch.getString("legacy")); true }
                    else -> true
                }
                if (ok) "ENQUEUED" else "FAILED_RETRYABLE"
            } catch (e: Exception) {
                val collision = (e.message ?: "").contains("collision", ignoreCase = true)
                log('E', "CAPTURE_CHANNEL_ENQUEUE_FAIL: channel=$name key=$captureKey ${e.javaClass.simpleName}:${e.message?.take(80)}")
                if (collision) "COLLISION_RETAINED" else "FAILED_RETRYABLE"
            }
            states[name] = next
            if (next == "FAILED_RETRYABLE") recordHealth("channel_enqueue_failed", captureKey, 0, name)
            if (next == "COLLISION_RETAINED") recordEvent(identityOf(attemptId, captureKey), "CHANNEL_COLLISION_RETAINED", name)
            if (durable) {
                runCatching { journal.update(attemptId) { it.put("channels", JSONObject(states as Map<*, *>)) } }
                    .onFailure { recordHealth("journal_fault", captureKey, 0, it.javaClass.simpleName) }
            }
            crashPoint(name)
        }
        return states
    }

    private fun identityOf(attemptId: String, captureKey: String): JSONObject =
        journal.read(attemptId) ?: JSONObject().put("capture_key", captureKey).put("attempt_id", attemptId)

    private fun completeIfResolved(attemptId: String, captureKey: String, states: Map<String, String>, durable: Boolean): Boolean {
        val resolved = states.values.all { it in CaptureJournal.RESOLVED || it == "BUILD_FAILED" }
        if (states.values.any { it == "BUILD_FAILED" }) {
            recordEvent(identityOf(attemptId, captureKey), "CHANNEL_BUILD_FAILED", "build_failed",
                JSONObject(states.filterValues { it == "BUILD_FAILED" } as Map<*, *>))
        }
        if (!resolved) {
            if (!durable) {
                // No durable copy: the unresolved channels are lost; say so explicitly.
                recordHealth("terminal_loss", captureKey)
                recordEvent(identityOf(attemptId, captureKey), "CHANNEL_LOST_NO_DURABLE_COPY", "enqueue_failed_without_bundle",
                    JSONObject(states as Map<*, *>))
            }
            return false
        }
        runCatching { journal.markCompleted(captureKey) }.onFailure { recordHealth("journal_fault", captureKey, 0, it.javaClass.simpleName) }
        return runCatching { journal.finalizeAttempt(attemptId) }.getOrDefault(false)
    }

    // ------------------------------------------------------------------ reconciliation
    data class ReconcileReport(val live: Int, val resumed: Int, val finalized: Int, val uncertain: Int, val lost: Int,
                               val stillPending: Int, val faults: List<String>)

    /**
     * Startup / service-restart / per-poll reconciliation. [liveAttemptIds] are attempts still owned by
     * THIS process (coordinator). A leftover intent is never assumed to be a loss: live owners are
     * skipped, built bundles are resumed, and only a different-run intent with no durable built
     * bytes is recorded as a loss (process ended before durable build).
     */
    fun reconcile(liveAttemptIds: Set<String>): ReconcileReport {
        var live = 0; var resumed = 0; var finalized = 0; var uncertain = 0; var lost = 0; var pending = 0
        val faults = mutableListOf<String>()
        for (file in journal.intents()) {
            val attemptId = journal.attemptIdOf(file)
            if (attemptId in liveAttemptIds) { live++; continue }
            val intent = journal.read(attemptId)
            if (intent == null) {
                uncertain++
                val id = JSONObject().put("capture_key", "unknown").put("attempt_id", attemptId)
                recordEvent(id, "UNCERTAIN_INTERRUPTED", "journal_unreadable")
                if (!journal.quarantine(file, "unreadable")) faults += "quarantine_failed:${file.name.take(40)}"
                continue
            }
            val key = intent.optString("capture_key")
            val sameRun = intent.optString("run_id") == runId
            when (intent.optString("stage")) {
                "FINAL" -> { if (journal.finalizeAttempt(attemptId)) finalized++ }
                "TERMINAL" -> { journal.terminal(attemptId, intent.optString("terminal_reason", "terminal")); finalized++ }
                "BUILT" -> {
                    val bundle = journal.readBundle(attemptId, intent.optString("bundle_sha256").ifBlank { null })
                    if (bundle == null) {
                        uncertain++
                        recordHealth("uncertain_interrupted", key)
                        recordEvent(intent, "UNCERTAIN_INTERRUPTED", "bundle_missing_or_digest_mismatch",
                            JSONObject().put("channels", intent.optJSONObject("channels") ?: JSONObject()))
                        journal.quarantine(file, "bundle_missing")
                        continue
                    }
                    val chState = intent.optJSONObject("channels") ?: JSONObject()
                    val states = linkedMapOf<String, String>()
                    chState.keys().forEach { states[it] = chState.getString(it) }
                    val unresolvedBefore = states.filterValues { it == "PENDING" || it == "FAILED_RETRYABLE" }.keys
                    val after = fanOut(attemptId, key, bundle, states, true)
                    if (unresolvedBefore.isNotEmpty()) {
                        resumed++
                        if (!sameRun) {
                            recordHealth("partial_recovered", key)
                            recordEvent(intent, "PARTIAL_RECOVERY_RESUMED", "prior_run_built_bundle",
                                JSONObject().put("resumed_channels", JSONArray(unresolvedBefore.toList()))
                                    .put("prior_run_id", intent.optString("run_id")).put("prior_pid", intent.optInt("pid")))
                        }
                    }
                    if (completeIfResolved(attemptId, key, after, true)) finalized++ else pending++
                }
                else -> { // INTENT stage
                    val orphanBundle = if (journal.hasBundle(attemptId)) journal.readBundle(attemptId, null) else null
                    val initial = orphanBundle?.optJSONObject("initial_states")
                    if (orphanBundle != null && initial != null) {
                        // Crash after the bundle was durable but before the BUILT checkpoint: the
                        // original bytes exist, so resume every expected channel (idempotent enqueue).
                        val states = linkedMapOf<String, String>()
                        initial.keys().forEach { states[it] = initial.getString(it) }
                        runCatching { journal.update(attemptId) {
                            it.put("stage", "BUILT").put("channels", JSONObject(states as Map<*, *>))
                                .put("poll_ts", orphanBundle.optString("poll_ts")) } }
                        val after = fanOut(attemptId, key, orphanBundle, states, true)
                        resumed++
                        recordHealth("partial_recovered", key)
                        recordEvent(intent, "PARTIAL_RECOVERY_RESUMED", "bundle_before_built_checkpoint",
                            JSONObject().put("resumed_channels", JSONArray(states.keys.toList()))
                                .put("prior_run_id", intent.optString("run_id")).put("prior_pid", intent.optInt("pid")))
                        if (completeIfResolved(attemptId, key, after, true)) finalized++ else pending++
                        continue
                    }
                    if (sameRun) {
                        // Same process, no live owner, nothing built: ownership was lost without a
                        // recorded outcome. We cannot prove what happened -> uncertain.
                        uncertain++
                        recordHealth("uncertain_interrupted", key)
                        recordEvent(intent, "UNCERTAIN_INTERRUPTED", "same_run_owner_absent_no_bundle")
                    } else if (journal.hasBundleTemp(attemptId)) {
                        uncertain++
                        recordHealth("uncertain_interrupted", key)
                        recordEvent(intent, "UNCERTAIN_INTERRUPTED", "bundle_write_interrupted",
                            JSONObject().put("prior_run_id", intent.optString("run_id")))
                    } else {
                        lost++
                        recordHealth("terminal_loss", key)
                        recordEvent(intent, "LOST_PROCESS_ENDED_BEFORE_DURABLE_BUILD", "prior_run_ended_no_built_bytes",
                            JSONObject().put("prior_run_id", intent.optString("run_id")).put("prior_pid", intent.optInt("pid")))
                    }
                    journal.terminal(attemptId, "reconciled_no_build")
                }
            }
        }
        return ReconcileReport(live, resumed, finalized, uncertain, lost, pending, faults)
    }

    fun isCompleted(key: String): Boolean = key in runCatching { journal.completedKeys() }.getOrDefault(emptyList())
}
