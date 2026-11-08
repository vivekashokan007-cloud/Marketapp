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
     * IN-MEMORY EFFECTIVE STATE.
     *
     * `intent` is the authoritative admission gate once anyone has asked for a change in this
     * process. It is set synchronously and without IO, so pressing OFF stops admitting capture
     * AT ONCE — it does not wait for the control file to be written, and it does not depend on
     * that write succeeding. An accepted persistence job already inside a blocking call cannot be
     * cancelled, so OFF stops new admission and the remaining in-flight work is reported as
     * draining rather than described as stopped.
     *
     * `cachedFile` avoids re-reading the control file on the poll thread every poll. The file is
     * read once per process (or after a change) and then cached; a hand edit of the file while the
     * process is running is therefore not noticed, which is a deliberate trade for keeping
     * filesystem IO off the hot path.
     */
    private class MemState {
        @Volatile var intent: Boolean? = null
        @Volatile var cachedFile: Boolean? = null
        @Volatile var lastPersist: JSONObject? = null
        @Volatile var lastRunSnapshot: JSONObject? = null
        @Volatile var globalBytesSnapshot: Long = -1L
    }

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

    private val controlExec: java.util.concurrent.ExecutorService =
        java.util.concurrent.Executors.newSingleThreadExecutor { r ->
            Thread(r, "premium-capture-control").apply { isDaemon = true }
        }

    /**
     * **Default false.** An unreadable or malformed control file is OFF, never ON: a capture that
     * cannot read its own switch must not run.
     *
     * Answers from memory where possible, so neither the poll thread nor the UI thread pays a file
     * read for it.
     */
    fun isEnabled(filesDir: File): Boolean {
        val st = stateFor(filesDir)
        st.intent?.let { return it }
        st.cachedFile?.let { return it }
        val v = read(filesDir).optBoolean("enabled", false)
        st.cachedFile = v
        return v
    }

    /**
     * Ask for a change without blocking the caller on capture IO or its lock.
     *
     * The previous `setEnabled` ran `putControlFile` synchronously, which waits on the same root
     * lock a blocked persistence write holds — so pressing the native control could freeze the UI
     * even though worker submission is non-blocking. Submission being non-blocking says nothing
     * about this path, and it was wrong to treat the two as equivalent.
     *
     * Returns at once, with the effective state already changed and persistence reported as
     * PENDING. The durable outcome arrives later in [statusFast]'s `persistence` block, so a
     * failed write is visible instead of being falsely acknowledged.
     */
    fun requestEnabled(filesDir: File, on: Boolean): JSONObject {
        val st = stateFor(filesDir)
        st.intent = on
        st.lastPersist = JSONObject()
            .put("state", "PENDING")
            .put("requested", on)
            .put("requested_at_ms", System.currentTimeMillis())
        val submitted = runCatching {
            // explicit Runnable: `submit { }` with a Unit-returning lambda is ambiguous between
            // submit(Runnable) and submit(Callable<T>)
            controlExec.submit(Runnable {
                val r = runCatching { setEnabled(filesDir, on) }
                    .getOrElse { SetResult(false, on, it.javaClass.simpleName) }
                st.lastPersist = JSONObject()
                    .put("state", if (r.ok) "PERSISTED" else "FAILED")
                    .put("requested", on)
                    .put("settled_at_ms", System.currentTimeMillis())
                    .put("detail", r.detail ?: JSONObject.NULL)
            })
            true
        }.getOrElse { false }
        return JSONObject()
            .put("accepted", true)
            .put("effective_enabled", on)
            .put("effective_immediately", true)
            .put("persistence", if (submitted) "PENDING" else "NOT_SUBMITTED")
            .put("note", if (on) "capture may be admitted from the next poll"
                else "no further capture is admitted; work already accepted drains and " +
                    "cannot be cancelled")
    }

    /** True when the control file exists but could not be parsed — reported, not silently OFF. */
    fun controlUnreadable(filesDir: File): Boolean {
        val f = metaFile(filesDir)
        return f.isFile && runCatching { JSONObject(f.readText()) }.isFailure
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
    fun setEnabled(filesDir: File, on: Boolean): SetResult = synchronized(this) {
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
            if (w.persisted) {
                val st = stateFor(filesDir)
                st.cachedFile = on
                st.intent = on
                SetResult(true, on)
            } else SetResult(false, isEnabled(filesDir), "${w.status}: ${w.detail ?: ""}")
        }.getOrElse { SetResult(false, isEnabled(filesDir), it.javaClass.simpleName) }
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
        val enabled = st.intent ?: st.cachedFile ?: false
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
