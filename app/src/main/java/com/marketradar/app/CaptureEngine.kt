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

    /** R1: validated origin session of a capture key, or null (unattributed: never resets session health). */
    private fun sessionOf(key: String): String? = FrozenCaptureRequest.sessionOfKey(key)

    /**
     * R1: snapshot upload acknowledgement. [originKey] is the capture key carried by the acknowledged payload's own
     * capture_provenance (validated); without one (pre-b503 payloads) the ACK is counted as unattributed.
     */
    fun recordUploadAck(pollTs: String, originKey: String?) {
        val key = originKey?.takeIf { FrozenCaptureRequest.sessionOfKey(it) != null }
        recordHealth("upload_ack", key ?: "unattributed:${pollTs.take(40)}")
    }

    fun recordUploadPending(pending: Int) = recordHealth("upload_unacknowledged", "pending=$pending")

    fun healthSnapshot(): JSONObject = synchronized(healthLock) {
        runCatching { JSONObject(health.read() ?: "{}") }.getOrDefault(JSONObject())
    }

    // ------------------------------------------------------------------ events
    fun eventIdFor(captureKey: String, attemptId: String, type: String): String =
        "cev_" + FrozenCaptureRequest.sha256Hex("capture_event_v1|$captureKey|$attemptId|$type").take(32)

    /** Deterministic id + exact bytes for one event. */
    fun buildEvent(identity: JSONObject, type: String, reason: String, detail: JSONObject = JSONObject(),
                   occurredAtMs: Long = identity.optLong("requested_wall_ms")): Pair<String, String> {
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
        return id to event.toString()
    }

    /**
     * Enqueues exact event bytes. True when the event is durably owned by the outbox (new, identical copy
     * pending, or a same-id collision whose bytes the outbox archived to its quarantine for inspection).
     */
    private fun enqueueEvent(id: String, text: String): Boolean {
        val ev = runCatching { JSONObject(text) }.getOrNull()
        val type = ev?.optString("event_type") ?: "?"; val key = ev?.optString("capture_key") ?: "?"
        return try {
            if (events.isPending(id)) return true
            events.enqueue(id, text)
            log('W', "CAPTURE_EVENT: type=$type key=$key reason=${ev?.optString("reason")}")
            true
        } catch (e: IllegalStateException) {
            log('E', "CAPTURE_EVENT_COLLISION_RETAINED: type=$type key=$key id=$id (archived for inspection)")
            events.isQuarantined(id)
        } catch (t: Throwable) {
            recordHealth("event_ledger_fault", key, 0, t.javaClass.simpleName)
            log('E', "CAPTURE_EVENT_LEDGER_FAULT: type=$type key=$key ${t.javaClass.simpleName}:${t.message?.take(80)}")
            false
        }
    }

    /** Informational (non-terminal) event. Deterministic for a given (attempt, type). */
    fun recordEvent(identity: JSONObject, type: String, reason: String, detail: JSONObject = JSONObject(),
                    occurredAtMs: Long = identity.optLong("requested_wall_ms")): String? {
        val (id, text) = buildEvent(identity, type, reason, detail, occurredAtMs)
        return if (enqueueEvent(id, text)) id else null
    }

    /**
     * R2: a terminal outcome. The exact event bytes are persisted in the journal marker FIRST; the marker is
     * released only after the event outbox durably owns them. If the ledger is full or failing the marker stays,
     * and every reconciliation retries delivery idempotently. Returns true when the evidence is durable somewhere.
     */
    private fun terminalize(identity: JSONObject, attemptId: String, type: String, reason: String,
                            detail: JSONObject, terminalReason: String): Boolean {
        val (id, text) = buildEvent(identity, type, reason, detail)
        return terminalizeExact(identity, attemptId, id, text, terminalReason)
    }

    private fun terminalizeExact(identity: JSONObject, attemptId: String, id: String, text: String, terminalReason: String): Boolean {
        val type = runCatching { JSONObject(text).optString("event_type") }.getOrDefault("?")
        val marked = try { journal.markTerminal(attemptId, identity, terminalReason, id, text); true }
        catch (t: Throwable) {
            recordHealth("journal_fault", identity.optString("capture_key"), 0, t.javaClass.simpleName)
            log('E', "CAPTURE_TERMINAL_MARKER_WRITE_FAILED: key=${identity.optString("capture_key")} ${t.javaClass.simpleName}")
            false
        }
        crashPoint("terminal_marked")
        val delivered = enqueueEvent(id, text)
        crashPoint("terminal_event_enqueued")
        if (marked && delivered) {
            runCatching { journal.resolvePendingEvent(attemptId, id); journal.releaseTerminal(attemptId) }
        }
        if (!marked && !delivered) {
            log('E', "CAPTURE_TERMINAL_EVIDENCE_UNDURABLE: key=${identity.optString("capture_key")} type=$type (journal AND ledger failed)")
        }
        return marked || delivered
    }

    /** R2: delivers every pending event of an attempt; returns ids still pending (malformed entries -> "!id"). */
    private fun deliverPending(attemptId: String): List<String> {
        val remaining = mutableListOf<String>()
        for ((id, text) in journal.pendingEvents(attemptId)) {
            if (text == null) { remaining += "!$id"; continue }
            if (enqueueEvent(id, text)) runCatching { journal.resolvePendingEvent(attemptId, id) }
            else remaining += id
        }
        return remaining
    }

    /** R2: required (non-terminal) event, durable in the journal before it is enqueued. */
    private fun requireEvent(attemptId: String, identity: JSONObject, type: String, reason: String, detail: JSONObject) {
        val (id, text) = buildEvent(identity, type, reason, detail)
        val held = runCatching { journal.addPendingEvent(attemptId, id, text) }.getOrDefault(false)
        if (enqueueEvent(id, text)) { if (held) runCatching { journal.resolvePendingEvent(attemptId, id) }; return }
        // Neither the intent nor the ledger owns it: keep an evidence-only marker that reconciliation delivers.
        if (!held) runCatching { journal.markTerminal(attemptId, identity, "evidence_only", id, text) }
            .onFailure { log('E', "CAPTURE_REQUIRED_EVENT_UNDURABLE: type=$type ${it.javaClass.simpleName}") }
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

    private fun skippedEvent(req: FrozenCaptureRequest, blockingKey: String?, blockingAgeMs: Long): Pair<String, String> =
        buildEvent(req.identityJson(), "INFLIGHT_SKIPPED_SLOT", "prior_capture_inflight",
            JSONObject().put("blocking_capture_key", blockingKey ?: JSONObject.NULL).put("blocking_age_ms", blockingAgeMs)
                // C1: a skipped slot computed nothing: no NF/BNF generated cohort, compact or legacy rows exist for it.
                .put("expected_channels", "none_slot_not_computed")
                .put("generated_cohort", JSONObject().put("NF", "not_computed").put("BNF", "not_computed")))

    /** C1: durable terminal marker (exact event bytes) for a skipped origin, before the skip is returned. */
    override fun checkpointSkipped(req: FrozenCaptureRequest, blockingKey: String?, blockingAgeMs: Long): Boolean {
        val (id, text) = skippedEvent(req, blockingKey, blockingAgeMs)
        return try {
            journal.markTerminal(req.attemptId, req.identityJson(), "inflight_skip", id, text)
            crashPoint("skip_checkpointed")
            true
        } catch (e: java.io.IOException) {
            recordHealth("journal_fault", req.captureKey, 0, e.javaClass.simpleName)
            log('E', "CAPTURE_SKIP_CHECKPOINT_FAILED: key=${req.captureKey} ${e.message?.take(80)} (recording inline)")
            false
        } catch (e: RuntimeException) {
            recordHealth("journal_fault", req.captureKey, 0, e.javaClass.simpleName)
            log('E', "CAPTURE_SKIP_CHECKPOINT_FAILED: key=${req.captureKey} ${e.javaClass.simpleName} (recording inline)")
            false
        }
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
        val (id, text) = skippedEvent(req, blockingKey, blockingAgeMs)
        terminalizeExact(req.identityJson(), req.attemptId, id, text, "inflight_skip")
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
        terminalize(req.identityJson(), req.attemptId, type, failure.reason,
            JSONObject().put("exception_class", cls ?: JSONObject.NULL).put("skipped_slots_while_running", skippedWhileRunning),
            failure.reason)
        log('E', "ML_SNAPSHOT_STAGE_ABORTED: reason=${failure.reason} poll=${req.captureKey} class=${cls ?: "none"}")
    }

    override fun onFinisherRejected(req: FrozenCaptureRequest, delivery: CaptureDelivery) {
        recordHealth("finisher_rejected", req.captureKey)
        recordHealth("terminal_loss", req.captureKey)
        terminalize(req.identityJson(), req.attemptId, "FINISHER_REJECTED", "finisher_queue_full",
            JSONObject().put("delivery", delivery.name), "finisher_rejected")
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
            if (runCatching { journal.hasBundle(req.attemptId) }.getOrDefault(false)) {
                // Original bytes are durable: not a loss; reconciliation resumes the remaining channels.
                recordEvent(req.identityJson(), "BUILD_FANOUT_FAILED", "post_build_exception_bundle_retained",
                    JSONObject().put("exception_class", e.javaClass.simpleName).put("delivery", delivery.name))
            } else {
                recordHealth("terminal_loss", req.captureKey)
                terminalize(req.identityJson(), req.attemptId, "BUILD_FANOUT_FAILED", "post_build_exception",
                    JSONObject().put("exception_class", e.javaClass.simpleName).put("delivery", delivery.name), "post_build_exception")
            }
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
        // R3: freeze the retained legacy cohort from the ORIGINAL parsed snapshot, BEFORE compaction can trim
        // snapshot_pc2_authority_decisions to meet the local budget (identical rows otherwise: compaction copies
        // the array, poll_ts, session_date, policy and brain version unchanged).
        val legacy = runCatching { Pc2LegacyAuthorityRows.build(rawSnapObj) }.getOrNull()
        val snapObj = EvaluationLocalCache.compactBrainSnapshotForPersistence(rawSnapObj)
        val pollTs = snapObj.optString("poll_ts")
        val sessionDate = snapObj.optString("session_date", req.sessionDate).ifBlank { req.sessionDate }
        // Generated cohort from the FROZEN fact pack (never from the caller's mutable result).
        val factPack = req.factPackJson?.let { runCatching { JSONObject(it) }.getOrNull() }
        var genError: Throwable? = null
        // C3: a frozen fact pack that is present but unreadable is a generation FAILURE, never "no fact pack".
        val factPackBroken = req.factPackJson != null && factPack == null
        val rows = if (factPackBroken) { genError = IllegalStateException("fact_pack_unparseable"); log('E', "ML_GENERATED_BUILD_FAIL: fact_pack_unparseable"); null }
        else runCatching { factPack?.let { GeneratedCandidateRows.build(it, snapObj) } ?: JSONArray() }
            .onFailure { genError = it; log('E', "ML_GENERATED_BUILD_FAIL: ${it.javaClass.simpleName}") }.getOrNull()
        // C3: the authoritative expected sampled cohort, frozen into the snapshot itself before any upload.
        val factPollTs = factPack?.optString("poll_timestamp")?.ifBlank { null }
        val recId = snapObj.opt("recommendation_id")?.takeIf { it != JSONObject.NULL }
        val cohortManifest = runCatching {
            when {
                rows == null -> GeneratedCohort.buildFailed(if (factPackBroken) "fact_pack_unparseable" else genError?.javaClass?.simpleName ?: "build_failed", factPollTs, sessionDate, recId)
                rows.length() == 0 -> GeneratedCohort.notExpected(if (factPack == null) "no_fact_pack" else "zero_candidates", factPollTs, sessionDate, recId)
                else -> GeneratedCohort.expected(rows, sessionDate, recId)
            }
        }.getOrElse { GeneratedCohort.buildFailed("manifest_" + it.javaClass.simpleName, factPollTs, sessionDate, recId) }
        (snapObj.optJSONObject("poll_summary_json") ?: JSONObject().also { snapObj.put("poll_summary_json", it) })
            .put("generated_cohort_manifest", cohortManifest)
        val snapshotPayload = BrainSnapshotPayloadSanitizer.sanitize(snapObj).toString()
        ch.put("snapshot", snapshotPayload)
        states["local_cache"] = "PENDING"
        states["snapshot"] = "PENDING"
        when {
            rows == null -> states["generated"] = "BUILD_FAILED"
            rows.length() == 0 -> { states["generated"] = "NOT_EXPECTED"; ch.put("generated_reason", if (factPack == null) "no_fact_pack" else "zero_candidates") }
            else -> {
                ch.put("generated", JSONObject().put("poll_ts", rows.getJSONObject(0).getString("snapshot_poll_ts")).put("rows", rows).toString())
                states["generated"] = "PENDING"
            }
        }
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
            val sha = journal.writeBundle(req.attemptId, built.channels, req.identityJson())
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
        val identity = identityOf(attemptId, captureKey)
        val resolved = states.values.all { it in CaptureJournal.RESOLVED || it == "BUILD_FAILED" }
        val buildFailed = states.filterValues { it == "BUILD_FAILED" }
        val collided = states.filterValues { it == "COLLISION_RETAINED" }
        // R2: build-failure / collision outcomes are required evidence, durable before any finalization.
        if (buildFailed.isNotEmpty()) requireEvent(attemptId, identity, "CHANNEL_BUILD_FAILED", "build_failed",
            JSONObject(buildFailed as Map<*, *>))
        if (collided.isNotEmpty()) requireEvent(attemptId, identity, "CHANNEL_COLLISION_RETAINED", "collision_retained",
            JSONObject(collided as Map<*, *>))
        if (!resolved) {
            if (!durable) {
                // No durable copy: the unresolved channels are lost; say so explicitly and durably.
                recordHealth("terminal_loss", captureKey)
                terminalize(identity, attemptId, "CHANNEL_LOST_NO_DURABLE_COPY", "enqueue_failed_without_bundle",
                    JSONObject(states as Map<*, *>), "channels_lost_no_bundle")
            }
            return false
        }
        val intentExists = journal.read(attemptId) != null
        if (intentExists && deliverPending(attemptId).isNotEmpty()) return false
        runCatching { journal.markCompleted(captureKey) }.onFailure { recordHealth("journal_fault", captureKey, 0, it.javaClass.simpleName) }
        if (!intentExists) return true
        val stage = journal.read(attemptId)?.optString("stage")
        return if (stage == "TERMINAL") runCatching { journal.releaseTerminal(attemptId) }.getOrDefault(false)
        else runCatching { journal.finalizeAttempt(attemptId) }.getOrDefault(false)
    }

    // ------------------------------------------------------------------ reconciliation
    data class ReconcileReport(val live: Int, val resumed: Int, val finalized: Int, val uncertain: Int, val lost: Int,
                               val stillPending: Int, val faults: List<String>)

    /** Test seam: called at "scan_start" (after nothing has been read) to interleave admissions. */
    @Volatile var reconcileHook: (String) -> Unit = {}

    fun reconcile(liveAttemptIds: Set<String>): ReconcileReport = reconcile { it in liveAttemptIds }

    /**
     * Startup / service-restart / per-poll reconciliation. [isLive] is consulted PER MARKER, immediately before
     * the marker is read (R2): the coordinator registers an admission before its intent is written and releases it
     * only after the owner callback finished, so an attempt admitted during this scan is never mistaken for an
     * interrupted one. A leftover intent is never assumed to be a loss: live owners are skipped, built bundles are
     * resumed, and only a different-run intent with no durable built bytes is recorded as a loss.
     */
    fun reconcile(isLive: (String) -> Boolean): ReconcileReport {
        var live = 0; var resumed = 0; var finalized = 0; var uncertain = 0; var lost = 0; var pending = 0
        val faults = mutableListOf<String>()
        reconcileHook("scan_start")
        for (file in journal.intents()) {
            val attemptId = journal.attemptIdOf(file)
            if (isLive(attemptId)) { live++; continue }
            val intent = journal.read(attemptId)
            if (intent == null) {
                if (!file.exists()) continue // finished concurrently
                uncertain++
                val id = JSONObject().put("capture_key", "unknown").put("attempt_id", attemptId)
                recordEvent(id, "UNCERTAIN_INTERRUPTED", "journal_unreadable")
                // The quarantined file itself is the retained, inspectable evidence.
                if (!journal.quarantine(file, "unreadable")) faults += "quarantine_failed:${file.name.take(40)}"
                continue
            }
            val key = intent.optString("capture_key")
            val sameRun = intent.optString("run_id") == runId
            when (intent.optString("stage")) {
                "FINAL" -> {
                    if (deliverPending(attemptId).isEmpty() && journal.finalizeAttempt(attemptId)) finalized++ else pending++
                }
                "TERMINAL" -> {
                    val rem = deliverPending(attemptId)
                    when {
                        rem.any { it.startsWith("!") } -> {
                            // A permanently malformed ledger entry: retained for inspection, never silently dropped.
                            faults += "malformed_pending_event:${attemptId.take(40)}"
                            if (!journal.quarantine(file, "malformed_pending_event")) faults += "quarantine_failed:${file.name.take(40)}"
                        }
                        rem.isEmpty() -> if (journal.releaseTerminal(attemptId)) finalized++ else faults += "release_failed:${attemptId.take(40)}"
                        else -> pending++
                    }
                }
                "BUILT" -> {
                    val bundle = journal.readBundle(attemptId, intent.optString("bundle_sha256").ifBlank { null })
                    if (bundle == null) {
                        uncertain++
                        recordHealth("uncertain_interrupted", key)
                        terminalize(intent, attemptId, "UNCERTAIN_INTERRUPTED", "bundle_missing_or_digest_mismatch",
                            JSONObject().put("channels", intent.optJSONObject("channels") ?: JSONObject()), "bundle_missing")
                        continue
                    }
                    val chState = intent.optJSONObject("channels") ?: JSONObject()
                    val states = linkedMapOf<String, String>()
                    chState.keys().forEach { states[it] = chState.getString(it) }
                    val unresolvedBefore = states.filterValues { it == "PENDING" || it == "FAILED_RETRYABLE" }.keys
                    if (unresolvedBefore.isNotEmpty() && !sameRun) {
                        recordHealth("partial_recovered", key)
                        requireEvent(attemptId, intent, "PARTIAL_RECOVERY_RESUMED", "prior_run_built_bundle",
                            JSONObject().put("resumed_channels", JSONArray(unresolvedBefore.toList()))
                                .put("prior_run_id", intent.optString("run_id")).put("prior_pid", intent.optInt("pid")))
                    }
                    val after = fanOut(attemptId, key, bundle, states, true)
                    if (unresolvedBefore.isNotEmpty()) resumed++
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
                        recordHealth("partial_recovered", key)
                        requireEvent(attemptId, intent, "PARTIAL_RECOVERY_RESUMED", "bundle_before_built_checkpoint",
                            JSONObject().put("resumed_channels", JSONArray(states.keys.toList()))
                                .put("prior_run_id", intent.optString("run_id")).put("prior_pid", intent.optInt("pid")))
                        val after = fanOut(attemptId, key, orphanBundle, states, true)
                        resumed++
                        if (completeIfResolved(attemptId, key, after, true)) finalized++ else pending++
                        continue
                    }
                    if (sameRun) {
                        // Same process, no live owner, nothing built: ownership was lost without a
                        // recorded outcome. We cannot prove what happened -> uncertain.
                        uncertain++
                        recordHealth("uncertain_interrupted", key)
                        terminalize(intent, attemptId, "UNCERTAIN_INTERRUPTED", "same_run_owner_absent_no_bundle", JSONObject(), "reconciled_no_build")
                    } else if (journal.hasBundleTemp(attemptId)) {
                        uncertain++
                        recordHealth("uncertain_interrupted", key)
                        terminalize(intent, attemptId, "UNCERTAIN_INTERRUPTED", "bundle_write_interrupted",
                            JSONObject().put("prior_run_id", intent.optString("run_id")), "reconciled_no_build")
                    } else {
                        lost++
                        recordHealth("terminal_loss", key)
                        terminalize(intent, attemptId, "LOST_PROCESS_ENDED_BEFORE_DURABLE_BUILD", "prior_run_ended_no_built_bytes",
                            JSONObject().put("prior_run_id", intent.optString("run_id")).put("prior_pid", intent.optInt("pid")),
                            "reconciled_no_build")
                    }
                }
            }
        }
        // R2: a durable bundle whose intent was never written (intent storage fault, then a crash).
        for (attemptId in journal.bundlesWithoutIntent()) {
            if (isLive(attemptId)) { live++; continue }
            val read = journal.readBundleBody(attemptId)
            if (read == null) { if (journal.hasBundle(attemptId)) faults += "orphan_bundle_unreadable:${attemptId.take(40)}"; continue }
            val (body, sha) = read
            val identity = body.optJSONObject("identity")
            val channels = body.optJSONObject("channels")
            val problem = verifyOrphanBundle(attemptId, body, identity, channels)
            if (problem != null) {
                uncertain++
                val id = JSONObject().put("capture_key", identity?.optString("capture_key")?.ifBlank { null } ?: "unknown")
                    .put("attempt_id", attemptId)
                recordEvent(id, "UNCERTAIN_INTERRUPTED", "orphan_bundle_unverified:$problem")
                // Not abandoned: the original bytes are retained in the journal quarantine for inspection.
                if (!journal.quarantineBundle(attemptId, "unverified_$problem")) faults += "quarantine_failed:${attemptId.take(40)}"
                continue
            }
            val key = identity!!.getString("capture_key")
            val initial = channels!!.getJSONObject("initial_states")
            runCatching { journal.restoreIntent(attemptId, identity, initial, sha) }
                .onFailure { faults += "orphan_restore_failed:${attemptId.take(40)}" }
            val states = linkedMapOf<String, String>()
            initial.keys().forEach { states[it] = initial.getString(it) }
            recordHealth("partial_recovered", key)
            requireEvent(attemptId, identity, "PARTIAL_RECOVERY_RESUMED", "bundle_without_intent",
                JSONObject().put("resumed_channels", JSONArray(states.keys.toList())).put("prior_run_id", identity.optString("run_id")))
            val after = fanOut(attemptId, key, channels, states, true)
            resumed++
            if (completeIfResolved(attemptId, key, after, true)) finalized++ else pending++
        }
        return ReconcileReport(live, resumed, finalized, uncertain, lost, pending, faults)
    }

    /** null when the orphan bundle's frozen identity is verified against its own snapshot provenance. */
    private fun verifyOrphanBundle(attemptId: String, body: JSONObject, identity: JSONObject?, channels: JSONObject?): String? {
        if (body.optString("schema") != "capture_bundle_v1") return "schema"
        if (identity == null) return "identity_absent"
        if (channels == null || channels.optJSONObject("initial_states") == null) return "channels"
        val attempt = identity.optString("attempt_id")
        if (attempt.isBlank() || body.optString("attempt_id") != attempt) return "attempt_mismatch"
        if (attempt.replace(Regex("[^A-Za-z0-9_-]"), "_").take(64) != attemptId) return "file_name_mismatch"
        val key = identity.optString("capture_key")
        if (FrozenCaptureRequest.sessionOfKey(key) == null) return "capture_key"
        val snap = runCatching { JSONObject(channels.getString("snapshot")) }.getOrNull() ?: return "snapshot_unreadable"
        val summary = snap.optJSONObject("poll_summary_json")
            ?: runCatching { JSONObject(snap.optString("poll_summary_json")) }.getOrNull() ?: return "provenance_absent"
        val prov = summary.optJSONObject("capture_provenance") ?: return "provenance_absent"
        if (prov.optString("capture_key") != key || prov.optString("attempt_id") != attempt) return "provenance_mismatch"
        return null
    }

    fun isCompleted(key: String): Boolean = key in runCatching { journal.completedKeys() }.getOrDefault(emptyList())
}
