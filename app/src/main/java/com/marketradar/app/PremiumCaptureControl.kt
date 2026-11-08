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

    fun setEnabled(filesDir: File, on: Boolean): SetResult = synchronized(this) {
        return runCatching {
            val r = root(filesDir)
            if (!r.exists() && !r.mkdirs() && !r.exists())
                return SetResult(false, isEnabled(filesDir), "cannot create ${r.name}")
            val prior = read(filesDir)
            val o = JSONObject()
                .put("control_version", CONTROL_VERSION)
                .put("enabled", on)
                .put("changed_at_ms", System.currentTimeMillis())
                .put("enabled_count", prior.optLong("enabled_count", 0L) + if (on) 1L else 0L)
                .put("is_trading_mode", false)
                .put("changes_paper_authority", false)
            val tmp = File(r, "$META.tmp")
            tmp.writeText(o.toString())
            val dest = metaFile(filesDir)
            if (dest.exists() && !dest.delete())
                return SetResult(false, isEnabled(filesDir), "cannot replace $META")
            if (!tmp.renameTo(dest)) {
                tmp.delete(); return SetResult(false, isEnabled(filesDir), "rename failed")
            }
            SetResult(true, on)
        }.getOrElse { SetResult(false, isEnabled(filesDir), it.javaClass.simpleName) }
    }

    /** The last coordinator run, for status. Bounded: one small object, replaced each time. */
    fun recordRun(filesDir: File, run: JSONObject) {
        runCatching {
            val r = root(filesDir)
            if (!r.exists() && !r.mkdirs() && !r.exists()) return
            val f = File(r, "last_run.json")
            val tmp = File(r, "last_run.json.tmp")
            tmp.writeText(run.toString().take(8192))
            if (f.exists()) f.delete()
            if (!tmp.renameTo(f)) tmp.delete()
        }
    }

    private fun lastRun(filesDir: File): JSONObject? {
        val f = File(root(filesDir), "last_run.json")
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
            .put("retention", JSONObject()
                .put("basis", "MEASURED_PER_SLOT_BYTES_AT_THE_CURRENT_RECORD_SHAPE")
                .put("deletion_policy", "NONE_CAPTURE_STOPS_CLEANLY_AT_THE_CAP")
                .put("note", "the global cap bounds total collection; it is not indefinite"))
            .put("last_run", lastRun(filesDir) ?: JSONObject.NULL)
    }
}
