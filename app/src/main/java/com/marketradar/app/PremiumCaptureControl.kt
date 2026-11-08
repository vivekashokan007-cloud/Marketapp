package com.marketradar.app

import org.json.JSONObject
import java.io.File

/**
 * The explicit, local, **default-OFF** control for premium evidence capture.
 *
 * This is a temporary data-collection switch. It is **not** a trading mode, not a Paper lock, and
 * not an authority change: nothing in admission, ranking, sizing, exits, notifications or the
 * Paper/Real lock reads it, and turning it on cannot alter a trading result.
 *
 * Mirrors the existing `ResearchCollector` control deliberately — a small JSON meta file under the
 * capture directory, read with a default of `false` — rather than introducing a second mechanism
 * with different failure behaviour.
 *
 * When OFF, callers must not prepare evidence, write capture files, schedule work or make any
 * request. Export of already retained evidence stays available, because reading what is already on
 * disk is not capture.
 */
object PremiumCaptureControl {

    const val CONTROL_VERSION = "premium_capture_control_v1_local_default_off"
    const val DIR_NAME = "premium_capture"
    private const val META = "control.json"
    private const val LAST_RUN = "last_run.json"

    /** The capture root. Created only by an actual write, never by a status read. */
    fun root(filesDir: File) = File(filesDir, DIR_NAME)

    private fun metaFile(filesDir: File) = File(root(filesDir), META)

    private fun read(filesDir: File): JSONObject {
        val f = metaFile(filesDir)
        if (!f.isFile) return JSONObject()
        return runCatching { JSONObject(f.readText()) }.getOrElse { JSONObject() }
    }

    /*
     * IN-MEMORY EFFECTIVE STATE, ORDERED.
     *
     * `intent` is the authoritative admission gate once anyone has asked for a change in this
     * process. It is set synchronously and without IO, so OFF stops admitting capture AT ONCE: it
     * does not wait for the control file to be written and does not depend on that write
     * succeeding. An accepted persistence job already inside a blocking call cannot be cancelled,
     * so OFF stops new admission and the remainder is reported as draining rather than stopped.
     *
     * EVERY REQUEST CARRIES A MONOTONIC SEQUENCE NUMBER, and this is the part the previous version
     * got wrong. It re-assigned `intent` from inside the completed disk write, so:
     *
     *   1. request A asks ON; its save blocks on the store lock
     *   2. request B asks OFF; effective intent becomes false
     *   3. A unblocks and completes, and its success set intent back to TRUE
     *   4. capture could then be admitted again despite the newer explicit OFF
     *
     * Persistence now NEVER writes `intent`. A completed save records what reached disk, and may
     * only publish itself as the latest request's status when its own sequence is still the
     * newest. An older completion is recorded separately as a durable fact and cannot overwrite
     * newer effective intent or a newer request's pending/failed state.
     *
     * The persisted file is loaded ONCE per root, asynchronously, into an explicit init state.
     * Admission stays conservative (not admitted) until that completes, and a malformed file is an
     * ERROR that reads as OFF. A hand edit of the file mid-process is deliberately not observed
     * unless [refresh] is called.
     */
    private class MemState {
        /** Effective admission gate; null until a request is made in this process. */
        @Volatile var intent: Boolean? = null
        @Volatile var intentSeq: Long = 0L

        /** UNKNOWN -> LOADING -> READY | ERROR. Admission is conservative until READY. */
        @Volatile var initState: String = INIT_UNKNOWN
        @Volatile var initDetail: String? = null
        /** The last known DURABLE value, from the loaded file or a completed save. */
        @Volatile var cachedFile: Boolean? = null

        /** Status of the LATEST request only. */
        @Volatile var lastPersist: JSONObject? = null
        /** What actually reached disk, from whichever request got there. */
        @Volatile var lastDurable: JSONObject? = null

        @Volatile var lastRunSnapshot: JSONObject? = null
        @Volatile var globalBytesSnapshot: Long = -1L

        // guarded by `synchronized(this)`
        var seq: Long = 0L
        var desired: Boolean? = null
        var desiredSeq: Long = 0L
        var saving: Boolean = false
    }

    const val INIT_UNKNOWN = "UNKNOWN"
    const val INIT_LOADING = "LOADING"
    const val INIT_READY = "READY"
    const val INIT_ERROR = "ERROR"

    const val ADMIT_INTENT_ON = "EFFECTIVE_INTENT_ON"
    const val ADMIT_INTENT_OFF = "EFFECTIVE_INTENT_OFF"
    const val ADMIT_PERSISTED_OFF = "PERSISTED_STATE_OFF"
    const val ADMIT_PERSISTED_ON = "PERSISTED_STATE_ON"
    const val ADMIT_INITIALIZING = "CONTROL_INITIALIZING_NOT_ADMITTED"
    const val ADMIT_UNREADABLE = "CONTROL_UNREADABLE_TREATED_AS_OFF"

    /**
     * KEYED BY CAPTURE ROOT, exactly as the store keys its locks and revisions.
     *
     * The first version held this state in process-global fields. A real app has exactly one
     * `filesDir`, so it would have behaved correctly in production — but the abstraction was
     * wrong: `isEnabled(someOtherDir)` answered from whatever directory had last been touched,
     * ignoring the one it was asked about. CI caught it immediately, across five tests.
     *
     * `absolutePath`, not `canonicalPath`: canonicalisation can touch the filesystem, and the
     * whole point of this state is to answer without IO. Keys only have to distinguish
     * directories within one process, which string paths already do.
     */
    private val STATES = HashMap<String, MemState>()

    private fun stateFor(filesDir: File): MemState {
        val key = File(filesDir, DIR_NAME).absolutePath
        return synchronized(STATES) { STATES.getOrPut(key) { MemState() } }
    }

    /**
     * BOUNDED control persistence: one running save, plus at most one coalesced latest desired
     * state per root.
     *
     * `newSingleThreadExecutor` has an UNBOUNDED queue, so every toggle while a write was blocked
     * added another Runnable and retained another obsolete state — the bounded evidence worker
     * does not bound this separate executor. The queue holds one task, the drain loop coalesces to
     * the latest desired value, and a redundant task is discarded because the loop will pick up
     * whatever the latest state is anyway. Repeated toggles therefore cannot accumulate.
     */
    private val controlExec: java.util.concurrent.ThreadPoolExecutor =
        java.util.concurrent.ThreadPoolExecutor(
            1, 1, 0L, java.util.concurrent.TimeUnit.MILLISECONDS,
            java.util.concurrent.ArrayBlockingQueue<Runnable>(1),
            { r -> Thread(r, "premium-capture-control").apply { isDaemon = true } },
            java.util.concurrent.ThreadPoolExecutor.DiscardPolicy(),
        )

    /**
     * A SEPARATE one-thread executor for the one-off load.
     *
     * Deliberately not the save executor: that one bounds its queue to a single coalesced task and
     * discards extras, so an init task submitted while a save was blocked would be silently
     * dropped and the state left LOADING for ever — conservative, but permanently stuck. The load
     * happens once per root (once in production), so a dedicated single thread bounds it.
     */
    private val initExec: java.util.concurrent.ThreadPoolExecutor =
        java.util.concurrent.ThreadPoolExecutor(
            1, 1, 0L, java.util.concurrent.TimeUnit.MILLISECONDS,
            java.util.concurrent.LinkedBlockingQueue<Runnable>(64),
            { r -> Thread(r, "premium-capture-control-init").apply { isDaemon = true } },
        )

    /** Starts the one-off asynchronous load of the persisted control for this root. */
    private fun ensureInitStarted(filesDir: File) {
        val st = stateFor(filesDir)
        synchronized(st) {
            if (st.initState != INIT_UNKNOWN) return
            st.initState = INIT_LOADING
        }
        runCatching {
            initExec.execute { loadPersisted(filesDir) }
        }.onFailure {
            // could not be queued at all: reset so a later call retries rather than leaving the
            // state LOADING for ever
            synchronized(st) { if (st.initState == INIT_LOADING) st.initState = INIT_UNKNOWN }
        }
    }

    /** Reads and validates the persisted control. Never touches `intent`. */
    private fun loadPersisted(filesDir: File) {
        val st = stateFor(filesDir)
        val f = metaFile(filesDir)
        if (!f.isFile) {
            st.cachedFile = false
            st.initDetail = "no control file; default OFF"
            st.initState = INIT_READY
            return
        }
        val parsed = runCatching { JSONObject(f.readText()) }.getOrNull()
        if (parsed == null) {
            // a capture that cannot read its own switch must not run
            st.cachedFile = null
            st.initDetail = "control file present but unparseable"
            st.initState = INIT_ERROR
            return
        }
        st.cachedFile = parsed.optBoolean("enabled", false)
        st.initDetail = null
        st.initState = INIT_READY
    }

    /**
     * Re-read the persisted control for this root, synchronously.
     *
     * The documented policy is that a hand edit mid-process is NOT observed, so this is the
     * explicit escape hatch. It performs IO and must not be called from the UI or poll path.
     */
    fun refresh(filesDir: File) {
        val st = stateFor(filesDir)
        synchronized(st) { st.initState = INIT_LOADING }
        loadPersisted(filesDir)
    }

    /** Test/diagnostic helper: wait for the asynchronous load to settle. */
    fun awaitInitialized(filesDir: File, timeoutMs: Long): Boolean {
        ensureInitStarted(filesDir)
        val st = stateFor(filesDir)
        val deadline = System.nanoTime() + timeoutMs * 1_000_000L
        while (System.nanoTime() < deadline) {
            val s = st.initState
            if (s == INIT_READY || s == INIT_ERROR) return true
            if (s == INIT_UNKNOWN) ensureInitStarted(filesDir)
            Thread.sleep(5)
        }
        // Synchronous fallback: this helper is allowed to block, and settling the state here is
        // better than reporting a timeout that leaves it LOADING. It is never called from the UI
        // or poll path.
        if (st.initState != INIT_READY && st.initState != INIT_ERROR) loadPersisted(filesDir)
        return st.initState == INIT_READY || st.initState == INIT_ERROR
    }

    /**
     * The memory-only admission decision, for the poll path.
     *
     * The coordinator used to call `controlUnreadable` — which read and PARSED the file — on every
     * poll, even once the enabled flag was cached, so the claim that per-poll file reads were
     * removed was incomplete. This answers from memory and starts the load if it has not begun.
     */
    fun admission(filesDir: File): JSONObject {
        ensureInitStarted(filesDir)
        val st = stateFor(filesDir)
        val i = st.intent
        if (i != null) return JSONObject().put("admit", i)
            .put("reason", if (i) ADMIT_INTENT_ON else ADMIT_INTENT_OFF)
            .put("source", "REQUEST_SEQ_${st.intentSeq}")
        return when (st.initState) {
            INIT_READY -> {
                val v = st.cachedFile ?: false
                JSONObject().put("admit", v)
                    .put("reason", if (v) ADMIT_PERSISTED_ON else ADMIT_PERSISTED_OFF)
                    .put("source", "PERSISTED_CONTROL")
            }
            INIT_ERROR -> JSONObject().put("admit", false).put("reason", ADMIT_UNREADABLE)
                .put("source", "PERSISTED_CONTROL")
            else -> JSONObject().put("admit", false).put("reason", ADMIT_INITIALIZING)
                .put("source", "NOT_YET_LOADED")
        }
    }

    /**
     * **Default false.** An unreadable or malformed control file is OFF, never ON: a capture that
     * cannot read its own switch must not run. Conservative while the persisted state is still
     * loading.
     *
     * Memory-only, so neither the poll thread nor the UI thread pays a file read.
     */
    fun isEnabled(filesDir: File): Boolean = admission(filesDir).optBoolean("admit", false)

    /**
     * Ask for a change without blocking the caller on capture IO or its lock.
     *
     * Returns at once with the effective state already changed and persistence PENDING. The
     * durable outcome arrives later in [statusFast]'s `latest_request` block, so a failed write is
     * visible instead of being falsely acknowledged — and a newer request is never overwritten by
     * an older save completing.
     */
    fun requestEnabled(filesDir: File, on: Boolean): JSONObject {
        val st = stateFor(filesDir)
        ensureInitStarted(filesDir)
        var needSubmit = false
        val my: Long
        synchronized(st) {
            my = ++st.seq
            st.intent = on
            st.intentSeq = my
            st.desired = on
            st.desiredSeq = my
            st.lastPersist = JSONObject()
                .put("state", "PENDING").put("requested", on)
                .put("seq", my).put("requested_at_ms", System.currentTimeMillis())
            if (!st.saving) { st.saving = true; needSubmit = true }
        }
        var submitted = true
        if (needSubmit) {
            submitted = runCatching { controlExec.execute { drain(filesDir) }; true }
                .getOrElse {
                    synchronized(st) { st.saving = false }
                    false
                }
        }
        return JSONObject()
            .put("accepted", true)
            .put("effective_enabled", on)
            .put("effective_immediately", true)
            .put("seq", my)
            .put("persistence", if (submitted) "PENDING" else "NOT_SUBMITTED")
            .put("note", if (on) "capture may be admitted from the next poll"
                else "no further capture is admitted; work already accepted drains and " +
                    "cannot be cancelled")
    }

    /** Writes the latest desired state, coalescing anything that arrived while writing. */
    private fun drain(filesDir: File) {
        val st = stateFor(filesDir)
        while (true) {
            val d: Boolean
            val ds: Long
            synchronized(st) {
                val dd = st.desired
                if (dd == null) { st.saving = false; return }
                d = dd; ds = st.desiredSeq; st.desired = null
            }
            val r = runCatching { persistDurable(filesDir, d) }
                .getOrElse { SetResult(false, d, it.javaClass.simpleName) }
            synchronized(st) {
                if (r.ok) st.cachedFile = d
                st.lastDurable = JSONObject()
                    .put("value", d).put("ok", r.ok).put("seq", ds)
                    .put("settled_at_ms", System.currentTimeMillis())
                    .put("detail", r.detail ?: JSONObject.NULL)
                // ONLY the newest request may publish itself as the latest status, and NOTHING
                // here touches `intent`: an older ON completing must not re-enable capture.
                if (ds == st.seq) {
                    st.lastPersist = JSONObject()
                        .put("state", if (r.ok) "PERSISTED" else "FAILED")
                        .put("requested", d).put("seq", ds)
                        .put("settled_at_ms", System.currentTimeMillis())
                        .put("detail", r.detail ?: JSONObject.NULL)
                }
            }
        }
    }

    /**
     * True when the persisted control is present but could not be parsed — reported, not silently
     * OFF.
     *
     * MEMORY-BACKED. This used to read and parse the file on every call, and the coordinator
     * called it every poll, so per-poll control IO survived the caching of the enabled flag. It
     * now reflects the loaded init state; [refresh] re-reads deliberately.
     */
    fun controlUnreadable(filesDir: File): Boolean {
        ensureInitStarted(filesDir)
        return stateFor(filesDir).initState == INIT_ERROR
    }

    data class SetResult(val ok: Boolean, val enabled: Boolean, val detail: String? = null)

    /**
     * Turn capture on or off.
     *
     * Goes through [PremiumCaptureStore.putControlFile], so these bytes are admitted against the
     * declared control reserve under the store's root lock instead of being written around
     * admission — the previous version wrote the file and its temp directly and could therefore
     * take the store past the global limit every evidence write respects.
     *
     * Turning **OFF** is admitted even at capacity (`allowAtCapacity`). Refusing the stop action
     * because the store is full would be the worst available failure mode, and OFF only replaces
     * one small existing file.
     *
     * Replacement is atomic through the store's temp-then-rename path. The old code deleted the
     * destination *before* renaming, which left a window in which the control file did not exist
     * at all — and a missing control reads as OFF, so a crash in that window silently disabled
     * capture.
     */
    private fun persistDurable(filesDir: File, on: Boolean): SetResult = synchronized(this) {
        return runCatching {
            val prior = read(filesDir)
            val o = JSONObject()
                .put("control_version", CONTROL_VERSION)
                .put("enabled", on)
                .put("changed_at_ms", System.currentTimeMillis())
                .put("enabled_count", prior.optLong("enabled_count", 0L) + if (on) 1L else 0L)
                .put("is_trading_mode", false)
                .put("changes_paper_authority", false)
            val w = PremiumCaptureStore(root(filesDir))
                .putControlFile(META, o, allowAtCapacity = !on)
            // `persisted` already covers DUPLICATE_NO_OP and REPLACED
            if (w.persisted) SetResult(true, on)
            else SetResult(false, on, "${w.status}: ${w.detail ?: ""}")
        }.getOrElse { SetResult(false, on, it.javaClass.simpleName) }
    }

    /**
     * The synchronous, durable API. It BLOCKS on the store lock and must not be called from the UI
     * or poll path; [requestEnabled] is the handler for those.
     *
     * It takes its own sequence number, so it participates in the same ordering as every other
     * request: it publishes effective intent and the latest-request status **only if no newer
     * request arrived while it was writing**. That is the fix for the defect where a completed
     * save re-assigned `intent` and an older ON could reactivate capture after a newer OFF.
     */
    fun setEnabled(filesDir: File, on: Boolean): SetResult {
        val st = stateFor(filesDir)
        ensureInitStarted(filesDir)
        val my = synchronized(st) { ++st.seq }
        val r = runCatching { persistDurable(filesDir, on) }
            .getOrElse { SetResult(false, on, it.javaClass.simpleName) }
        synchronized(st) {
            if (r.ok) st.cachedFile = on
            st.lastDurable = JSONObject()
                .put("value", on).put("ok", r.ok).put("seq", my)
                .put("settled_at_ms", System.currentTimeMillis())
                .put("detail", r.detail ?: JSONObject.NULL)
            if (my == st.seq) {
                st.intent = on
                st.intentSeq = my
                st.lastPersist = JSONObject()
                    .put("state", if (r.ok) "PERSISTED" else "FAILED")
                    .put("requested", on).put("seq", my)
                    .put("settled_at_ms", System.currentTimeMillis())
                    .put("detail", r.detail ?: JSONObject.NULL)
            }
        }
        return r
    }

    /**
     * Record the last run for status, as a **bounded structured object**.
     *
     * The old version wrote `run.toString().take(8192)`, which bounds CHARACTERS rather than UTF-8
     * bytes and can cut a JSON document mid-value — producing a status file that will not parse,
     * from code whose whole purpose is to report state. Nothing is truncated here: a small, fixed
     * set of scalar fields is projected out of the run, each individually length-capped, so the
     * result is valid JSON by construction and its size is known in advance.
     *
     * It is also admitted, so a full root that refused the records this describes also refuses to
     * grow further by writing about them.
     */
    fun recordRun(filesDir: File, run: JSONObject) {
        runCatching {
            fun cap(s: String?, n: Int): String? =
                s?.takeIf { it.isNotEmpty() }?.let { if (it.length <= n) it else it.take(n) }
            val summary = run.optJSONObject("summary_write")
            val o = JSONObject()
                .put("control_version", CONTROL_VERSION)
                .put("recorded_at_ms", System.currentTimeMillis())
                .put("index", cap(run.optString("index"), 8) ?: JSONObject.NULL)
                .put("status", cap(run.optString("status"), 64) ?: JSONObject.NULL)
                .put("acquisition_id", cap(run.optString("acquisition_id"), 32) ?: JSONObject.NULL)
                .put("written", run.optInt("written", -1))
                .put("duplicates", run.optInt("duplicates", -1))
                .put("refused", run.optInt("refused", -1))
                .put("conflicts", run.optInt("conflicts", -1))
                .put("batch_complete", run.optBoolean("batch_complete", false))
                .put("chain_present", run.optBoolean("chain_present", false))
                .put("persist_elapsed_ms", run.optLong("persist_elapsed_ms", -1L))
                .put("lot_authority_write",
                    cap(run.optString("lot_authority_write"), 48) ?: JSONObject.NULL)
                .put("error", cap(run.optString("error"), 64) ?: JSONObject.NULL)
                .put("summary_durably_recorded",
                    summary?.optBoolean("durably_recorded", false) ?: false)
                .put("summary_status", cap(summary?.optString("status"), 48) ?: JSONObject.NULL)
                .put("projection", "BOUNDED_STRUCTURED_FIELDS_NEVER_TRUNCATED_TEXT")
            val store = PremiumCaptureStore(root(filesDir))
            val w = store.putControlFile(LAST_RUN, o)
            // snapshot for the non-blocking status surface, so neither the UI nor a hot path has
            // to walk the store to answer "what is it doing and how much room is left"
            val st = stateFor(filesDir)
            st.lastRunSnapshot = JSONObject(o.toString())
                .put("durably_recorded", w.persisted)
                .put("write_status", w.status)
            st.globalBytesSnapshot = runCatching { store.globalBytes() }.getOrElse { -1L }
        }
    }

    private fun lastRun(filesDir: File): JSONObject? {
        val f = File(root(filesDir), LAST_RUN)
        if (!f.isFile) return null
        return runCatching { JSONObject(f.readText()) }.getOrNull()
    }

    /**
     * Operator status. Read-only: it never creates the capture directory, so asking for status
     * while OFF leaves the filesystem untouched.
     *
     * `retention` is stated because the global cap means collection is **not indefinite**: at the
     * measured per-slot cost, the 4 MiB global bound is reached after a bounded number of
     * index-sessions, and capture then stops cleanly rather than deleting anything.
     */
    /**
     * One honest word for what capture is actually doing, derived from the evidence budget and the
     * last recorded run rather than from the switch alone.
     */
    internal fun collectionState(enabled: Boolean, globalBytes: Long, last: JSONObject?,
                                 worker: JSONObject? = null): String {
        if (!enabled) return "OFF"
        if (globalBytes >= 0L) {
            val remaining = PremiumCaptureStore.MAX_BYTES_GLOBAL -
                PremiumCaptureStore.CONTROL_RESERVE_BYTES - globalBytes
            if (remaining <= 0L) return "ON_BUT_STOPPED_STORE_FULL"
        }
        // A REFUSED job never runs its closure, so it never records a run. Without this, a
        // successful last_run could keep showing ON_COLLECTING while every later poll was turned
        // away — the operator would see the last success, not the current state. A refusal newer
        // than the last recorded run therefore wins.
        if (worker != null) {
            val refusedAt = worker.optLong("last_refusal_at_ms", 0L)
            val lastRunAt = last?.optLong("recorded_at_ms", 0L) ?: 0L
            if (refusedAt > 0L && refusedAt >= lastRunAt)
                return "ON_BUT_REFUSING_" + worker.optString("last_refusal_reason", "UNKNOWN")
            if (worker.optBoolean("running", false) && worker.optBoolean("waiting", false))
                return "ON_WORKER_SATURATED"
        }
        val s = last?.optString("status") ?: return "ON_NO_RUN_RECORDED_YET"
        return when {
            s == "CAPTURE_ERROR" -> "ON_LAST_RUN_ERRORED"
            s.startsWith("REFUSED") || s.contains("NOT_PERSISTED") -> "ON_LAST_RUN_REFUSED"
            last.optInt("written", -1) > 0 -> "ON_COLLECTING"
            else -> "ON_LAST_RUN_WROTE_NOTHING"
        }
    }

    /**
     * Operator status with **no filesystem IO and no lock wait**, for the native dialog.
     *
     * [status] walks the store to total its bytes and so can block behind a stuck capture write.
     * That is acceptable off the main thread but not on it, so the UI reads this instead: effective
     * state from memory, capacity from the snapshot the last run recorded, and worker occupancy and
     * refusals from the worker's in-memory counters.
     *
     * `global_bytes` is `-1` when nothing has been recorded yet in this process. That is reported
     * as unknown rather than as zero, because an unknown total is not an empty store.
     */
    fun statusFast(filesDir: File): JSONObject {
        val st = stateFor(filesDir)
        val worker = PremiumCaptureWorker.stats()
        val adm = admission(filesDir)
        val enabled = adm.optBoolean("admit", false)
        val bytes = st.globalBytesSnapshot
        val remaining = if (bytes < 0) -1L
        else (PremiumCaptureStore.MAX_BYTES_GLOBAL -
            PremiumCaptureStore.CONTROL_RESERVE_BYTES - bytes).coerceAtLeast(0L)
        return JSONObject()
            .put("control_version", CONTROL_VERSION)
            .put("source", "IN_MEMORY_NO_FILESYSTEM_IO")
            .put("enabled", enabled)
            .put("enabled_is_effective_now", true)
            .put("is_trading_mode", false)
            .put("changes_paper_authority", false)
            .put("collection_state", collectionState(enabled, bytes, st.lastRunSnapshot, worker))
            .put("global_bytes", bytes)
            .put("global_bytes_known", bytes >= 0)
            .put("evidence_bytes_remaining", remaining)
            .put("control_reserve_bytes", PremiumCaptureStore.CONTROL_RESERVE_BYTES)
            // EFFECTIVE intent and DURABLE state are reported separately, because they can
            // legitimately disagree while a save is pending or has failed — and an unknown
            // persisted state must not be displayed as a confirmed OFF.
            .put("admission", adm)
            .put("control_init", st.initState)
            .put("control_init_detail", st.initDetail ?: JSONObject.NULL)
            .put("enabled_known", st.intent != null || st.initState == INIT_READY)
            .put("effective_intent", st.intent ?: JSONObject.NULL)
            .put("durable_value", st.cachedFile ?: JSONObject.NULL)
            .put("latest_request", st.lastPersist ?: JSONObject.NULL)
            .put("last_durable_write", st.lastDurable ?: JSONObject.NULL)
            .put("control_queue", synchronized(st) {
                JSONObject()
                    .put("saving", st.saving)
                    .put("pending_desired", st.desired ?: JSONObject.NULL)
                    .put("pending_seq", st.desiredSeq)
                    .put("latest_seq", st.seq)
                    .put("max_coalesced_pending", 1)
                    .put("policy", "ONE_RUNNING_SAVE_PLUS_ONE_COALESCED_LATEST_DESIRED_STATE")
            })
            // kept for compatibility with the earlier field name
            .put("persistence", st.lastPersist ?: JSONObject.NULL)
            .put("last_run", st.lastRunSnapshot ?: JSONObject.NULL)
            .put("worker", worker)
            .put("in_flight_cannot_be_cancelled", true)
    }

    fun status(filesDir: File): JSONObject {
        val store = PremiumCaptureStore(root(filesDir))
        val enabled = isEnabled(filesDir)
        val global = if (root(filesDir).exists()) store.globalBytes() else 0L
        return JSONObject()
            .put("control_version", CONTROL_VERSION)
            .put("enabled", enabled)
            .put("control_unreadable", controlUnreadable(filesDir))
            .put("is_trading_mode", false)
            .put("changes_paper_authority", false)
            .put("stop_mechanism", "SET_ENABLED_FALSE_TAKES_EFFECT_ON_THE_NEXT_POLL")
            .put("directory_exists", root(filesDir).exists())
            .put("global_bytes", global)
            .put("global_bytes_cap", PremiumCaptureStore.MAX_BYTES_GLOBAL)
            .put("global_bytes_remaining",
                (PremiumCaptureStore.MAX_BYTES_GLOBAL - global).coerceAtLeast(0L))
            .put("caps", store.capsJson())
            // The operational state an operator actually needs. An ON flag alone does not show
            // that a full store has STOPPED collecting, which is exactly the case where someone
            // would be looking at this.
            .put("collection_state",
                collectionState(enabled, global, lastRun(filesDir), PremiumCaptureWorker.stats()))
            .put("control_bytes", if (root(filesDir).exists()) store.controlBytes() else 0L)
            .put("control_reserve_bytes", PremiumCaptureStore.CONTROL_RESERVE_BYTES)
            .put("evidence_bytes_remaining",
                (PremiumCaptureStore.MAX_BYTES_GLOBAL -
                    PremiumCaptureStore.CONTROL_RESERVE_BYTES - global).coerceAtLeast(0L))
            .put("worker", PremiumCaptureWorker.stats())
            .put("retention", JSONObject()
                .put("basis", "MEASURED_PER_SLOT_BYTES_AT_THE_CURRENT_RECORD_SHAPE")
                .put("deletion_policy", "NONE_CAPTURE_STOPS_CLEANLY_AT_THE_CAP")
                .put("note", "the global cap bounds total collection; it is not indefinite"))
            .put("last_run", lastRun(filesDir) ?: JSONObject.NULL)
    }
}
