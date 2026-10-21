package com.marketradar.app

import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/**
 * b512 diagnostics for "the app restarted when I came back" reports. Pure / Android-free so it is JVM-testable.
 * Distinguishes a same-process Activity re-creation (process alive, UI rebuilt from saved state) from a real process
 * restore (new process, saved state handed back by the system), a renderer-loss recovery, and a fresh launch.
 * Holds only strings and counters for the process: never an Activity, View or WebView.
 */
object LifecycleDiagnostics {
    enum class CreateKind { FRESH_PROCESS_LAUNCH, SAME_PROCESS_RELAUNCH, SAME_PROCESS_UI_RECREATION, PROCESS_RESTORE, RENDERER_RECOVERY }

    private val instanceSeq = AtomicInteger(0)
    @Volatile private var lastCreateProcessUuid: String? = null
    private val rendererRecoveryPending = AtomicBoolean(false)

    fun classifyCreate(restoredState: Boolean, processUuid: String, previousCreateUuid: String?, rendererRecovery: Boolean): CreateKind = when {
        rendererRecovery -> CreateKind.RENDERER_RECOVERY
        restoredState && previousCreateUuid == processUuid -> CreateKind.SAME_PROCESS_UI_RECREATION
        restoredState -> CreateKind.PROCESS_RESTORE
        previousCreateUuid == processUuid -> CreateKind.SAME_PROCESS_RELAUNCH
        else -> CreateKind.FRESH_PROCESS_LAUNCH
    }

    /** Called once per Activity onCreate; returns (instance seq, kind). */
    fun onActivityCreate(restoredState: Boolean, processUuid: String): Pair<Int, CreateKind> {
        val kind = classifyCreate(restoredState, processUuid, lastCreateProcessUuid, rendererRecoveryPending.getAndSet(false))
        lastCreateProcessUuid = processUuid
        return instanceSeq.incrementAndGet() to kind
    }

    fun markRendererRecovery() = rendererRecoveryPending.set(true)

    @Volatile var lastTrimLevel: Int = -1
    @Volatile var lastTrimElapsedMs: Long = 0L

    // ---------------- ApplicationExitInfo ----------------
    private val REASONS = mapOf(0 to "UNKNOWN", 1 to "EXIT_SELF", 2 to "SIGNALED", 3 to "LOW_MEMORY", 4 to "CRASH",
        5 to "CRASH_NATIVE", 6 to "ANR", 7 to "INITIALIZATION_FAILURE", 8 to "PERMISSION_CHANGE",
        9 to "EXCESSIVE_RESOURCE_USAGE", 10 to "USER_REQUESTED", 11 to "USER_STOPPED", 12 to "DEPENDENCY_DIED",
        13 to "OTHER", 14 to "FREEZER", 15 to "PACKAGE_STATE_CHANGE", 16 to "PACKAGE_UPDATED")
    private val IST: DateTimeFormatter = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss").withZone(ZoneId.of("Asia/Kolkata"))

    fun reasonName(code: Int): String = REASONS[code] ?: "REASON_$code"

    /** What the user would perceive; only a hint for the report, not a diagnosis. */
    fun reasonHint(code: Int): String = when (code) {
        3, 9, 14 -> "system_reclaimed"
        2 -> "signaled_os_or_oem_kill"
        4, 5, 7 -> "crash"
        6 -> "anr"
        10 -> "user_swipe_or_force_stop"
        11, 15, 16 -> "package_or_user_stop"
        1 -> "self_exit"
        else -> "other"
    }

    data class Exit(val reason: Int, val status: Int, val importance: Int, val pssKb: Long, val rssKb: Long, val timestampMs: Long, val description: String?)

    /** Bounded lines (max [limit]); entries newer than [lastSeenMs] are flagged new=true. No stack traces or tombstones. */
    fun formatExits(exits: List<Exit>, lastSeenMs: Long, limit: Int = 5): List<String> = exits.take(limit).mapIndexed { i, e ->
        val desc = e.description?.replace(Regex("[^A-Za-z0-9 _:.,()/-]"), "")?.take(80).orEmpty()
        "APP_EXIT_REASON[$i]: reason=${e.reason} name=${reasonName(e.reason)} hint=${reasonHint(e.reason)} " +
            "new=${e.timestampMs > lastSeenMs} status=${e.status} importance=${e.importance} pss=${e.pssKb} rss=${e.rssKb} " +
            "time=${e.timestampMs} ist=${IST.format(Instant.ofEpochMilli(e.timestampMs))} desc=\"$desc\""
    }

    fun newestTimestamp(exits: List<Exit>, lastSeenMs: Long): Long = maxOf(lastSeenMs, exits.maxOfOrNull { it.timestampMs } ?: 0L)
}
