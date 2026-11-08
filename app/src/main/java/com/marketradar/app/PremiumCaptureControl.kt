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

    /**
     * **Default false.** An unreadable or malformed control file is OFF, never ON: a capture that
     * cannot read its own switch must not run.
     */
    fun isEnabled(filesDir: File): Boolean = read(filesDir).optBoolean("enabled", false)

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
            if (w.persisted) SetResult(true, on)
            else SetResult(false, isEnabled(filesDir), "${w.status}: ${w.detail ?: ""}")
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
            PremiumCaptureStore(root(filesDir)).putControlFile(LAST_RUN, o)
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
    internal fun collectionState(enabled: Boolean, globalBytes: Long, last: JSONObject?): String {
        if (!enabled) return "OFF"
        val remaining = PremiumCaptureStore.MAX_BYTES_GLOBAL -
            PremiumCaptureStore.CONTROL_RESERVE_BYTES - globalBytes
        if (remaining <= 0L) return "ON_BUT_STOPPED_STORE_FULL"
        val s = last?.optString("status") ?: return "ON_NO_RUN_RECORDED_YET"
        return when {
            s == "CAPTURE_ERROR" -> "ON_LAST_RUN_ERRORED"
            s.startsWith("REFUSED") || s.contains("NOT_PERSISTED") -> "ON_LAST_RUN_REFUSED"
            last.optInt("written", -1) > 0 -> "ON_COLLECTING"
            else -> "ON_LAST_RUN_WROTE_NOTHING"
        }
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
            .put("collection_state", collectionState(enabled, global, lastRun(filesDir)))
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
