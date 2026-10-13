package com.marketradar.app

import android.content.Context
import android.content.SharedPreferences
import com.marketradar.app.util.LogBuffer
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicBoolean

/**
 * b503: application/process-owned capture runtime. Owns the coordinator, the bounded
 * finisher and the durable journal; never depends on a MarketWatchService instance or its
 * serviceScope. A live service registers [drainHook] so frozen evidence is uploaded promptly;
 * without one, the next service start drains the same outboxes.
 *
 * Resource bounds (documented in the handoff):
 *  - capture Python threads: 1 (dedicated executor, refuses rather than queues)
 *  - finisher: 1 thread + ArrayBlockingQueue(2); on-time overflow runs inline on the poll
 *    thread, late overflow is an explicit FINISHER_REJECTED loss event
 *  - control lane (diagnostics/events): 1 thread + 64 queued; overflow counted
 *  - journal: <= 64 intents, <= 256 MiB bundles (refused with JOURNAL_STORAGE_FAULT, never evicted)
 *  - legacy PC2 outbox: <= 150 entries / 64 MiB active; capture events: <= 2000 / 8 MiB active.
 *    Quarantine/collision archives are retained and reported separately (not under these caps).
 */
object CaptureRuntime {
    private const val TAG = "CaptureRuntime"
    const val LEGACY_DIR = "pc2_legacy_authority_outbox_v1"
    const val EVENTS_DIR = "capture_event_outbox_v1"
    private const val HEALTH_PREF = "snapshot_stage_health"

    @Volatile private var initialized = false
    private lateinit var appContext: Context
    private lateinit var prefs: SharedPreferences
    lateinit var engine: CaptureEngine; private set
    lateinit var coordinator: CaptureCoordinator; private set
    lateinit var journal: CaptureJournal; private set
    lateinit var legacyOutbox: FrozenEvidenceOutbox; private set
    lateinit var eventOutbox: FrozenEvidenceOutbox; private set
    private lateinit var eventDrainer: CaptureEventDrainer
    val finisher = CaptureFinisher("capture-finisher", 2)
    private val control = CaptureFinisher("capture-control", 64)
    private val reconciling = AtomicBoolean(false)

    /** Registered by the live service; called after durable fan-out. Cleared on destroy. */
    @Volatile var drainHook: (() -> Unit)? = null

    private fun log(level: Char, msg: String) { runCatching { LogBuffer.add(level, TAG, msg) } }

    @Synchronized
    fun ensure(context: Context): CaptureRuntime {
        if (initialized) return this
        appContext = context.applicationContext ?: context
        prefs = appContext.getSharedPreferences("market_radar", Context.MODE_PRIVATE)
        val filesDir = appContext.filesDir
        journal = CaptureJournal(filesDir)
        legacyOutbox = FrozenEvidenceOutbox(filesDir, LEGACY_DIR, 150, 64L * 1024 * 1024)
        eventOutbox = FrozenEvidenceOutbox(filesDir, EVENTS_DIR, 2000, 8L * 1024 * 1024)
        eventDrainer = CaptureEventDrainer(eventOutbox, java.io.File(filesDir, "capture_event_quota_cooldown_v1"))
        val channels = object : CaptureChannels {
            override fun compactEnabled() = prefs.getBoolean(Pc2IngestCredential.PREF_REGISTERED_CONFIRMED, false)
            override fun stageHealth(): JSONObject = engine.healthSnapshot()
            override fun appendLocalCache(sessionDate: String, snapshotPayload: String): Boolean =
                EvaluationLocalCache.appendBrainSnapshot(appContext, sessionDate, JSONObject(snapshotPayload)).also {
                    if (!it) log('E', "ML_BRAIN_SNAPSHOT_LOCAL_SAVE_FAIL: date=$sessionDate")
                }
            override fun enqueueSnapshot(payload: String) = BrainSnapshotOutbox.enqueuePayload(filesDir, payload)
            override fun enqueueCompact(envelope: String): Boolean {
                val env = JSONObject(envelope)
                val built = Pc2CompactBatch.Built(env.getJSONObject("policy_row"), env.getJSONObject("batch_row"), JSONObject())
                return Pc2TelemetryOutbox.enqueue(filesDir, built).also {
                    log('I', "PC2_COMPACT_OUTBOX_ENQUEUE: new=$it batchId=${built.batchRow.optString("batch_id")}")
                }
            }
            override fun enqueueGenerated(payload: String) = GeneratedCandidateOutbox.enqueuePayload(filesDir, payload)
            override fun enqueueLegacy(pollTs: String, payload: String) = legacyOutbox.enqueue(pollTs, payload)
        }
        val health = object : CaptureHealthStore {
            override fun read(): String? = prefs.getString(HEALTH_PREF, null)
            override fun write(value: String): Boolean = prefs.edit().putString(HEALTH_PREF, value).commit()
        }
        engine = CaptureEngine(journal, channels, health, eventOutbox, MarketRadarApp.PROCESS_START_UUID,
            android.os.Process.myPid(), ::log, onDurable = { runCatching { drainHook?.invoke() } })
        coordinator = CaptureCoordinator(
            worker = CaptureCoordinator.newWorker(), finisher = finisher, control = control, owner = engine,
            cpuClockMs = { android.os.Debug.threadCpuTimeNanos().let { if (it < 0) -1L else it / 1_000_000L } }
        )
        initialized = true
        return this
    }

    /** Safe at app start, service (re)start and every poll; never treats a live owner as lost. */
    fun reconcile(): CaptureEngine.ReconcileReport? {
        if (!initialized || !reconciling.compareAndSet(false, true)) return null
        return try {
            engine.reconcile { coordinator.isLive(it) }.also { r ->
                if (r.resumed + r.uncertain + r.lost + r.stillPending + r.faults.size > 0) {
                    val inv = journal.inventory()
                    log('W', "CAPTURE_RECONCILE: live=${r.live} resumed=${r.resumed} finalized=${r.finalized} " +
                        "uncertain=${r.uncertain} lost=${r.lost} pending=${r.stillPending} faults=${r.faults.size} " +
                        "intents=${inv.intents} bundles=${inv.bundles} bundleBytes=${inv.bundleBytes} " +
                        "journalQuarantine=${inv.quarantined}/${inv.quarantinedBytes}B " +
                        "finisherOverflow=${finisher.overflowCount()} controlOverflow=${control.overflowCount()}")
                }
            }
        } catch (t: Throwable) {
            log('E', "CAPTURE_RECONCILE_THROW: ${t.javaClass.simpleName}"); null
        } finally { reconciling.set(false) }
    }

    private fun deviceKey(): String? = prefs.getString(Pc2IngestCredential.PREF_DEVICE_KEY, null)
        ?.takeIf { Pc2IngestCredential.isValidKey(it) }

    /** Retained legacy PC2 rows: restart-safe, independent of snapshot/compact/generated drains. */
    fun drainLegacy(): FrozenEvidenceOutbox.Status? {
        if (!initialized) return null
        val s = legacyOutbox.drain { key, payload ->
            SupabaseClient.uploadPc2AuthorityDecisionRows(org.json.JSONArray(payload), key)
        }
        log(if (s.pending == 0 && s.quarantinedTotal == 0 && s.faults.isEmpty()) 'I' else 'W',
            "PC2_AUTHORITY_TELEMETRY_OUTBOX_DRAIN: attempted=${s.attempted} acknowledged=${s.acknowledged} " +
                "retried=${s.retried} quarantined=${s.quarantined} pending=${s.pending} pendingBytes=${s.pendingBytes} " +
                "quarantinedTotal=${s.quarantinedTotal} quarantinedBytes=${s.quarantinedBytes} stop=${s.stopReason} faults=${s.faults.size}")
        return s
    }

    fun drainEvents(): FrozenEvidenceOutbox.Status? {
        if (!initialized) return null
        val key = deviceKey() ?: return eventOutbox.status(stop = "no_device_key")
        val r = eventDrainer.drain(maxAttempts = 20) { _, payload -> SupabaseClient.recordCaptureEvent(key, payload) }
        val s = r.status
        if (s.attempted > 0 || s.pending > 0 || s.quarantinedTotal > 0) {
            log(if (s.pending == 0 && s.quarantinedTotal == 0) 'I' else 'W',
                "CAPTURE_EVENT_OUTBOX_DRAIN: attempted=${s.attempted} acknowledged=${s.acknowledged} retried=${s.retried} " +
                    "pending=${s.pending} quarantinedTotal=${s.quarantinedTotal} stop=${s.stopReason} " +
                    "quotaCooldownUntilMs=${r.cooldownUntilMs} skippedForCooldown=${r.skippedForCooldown}")
        }
        return s
    }
}
