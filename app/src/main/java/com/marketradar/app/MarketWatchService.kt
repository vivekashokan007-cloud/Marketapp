package com.marketradar.app

import android.app.*
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.ServiceInfo
import android.os.*
import android.util.Log
import androidx.core.app.NotificationCompat
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlinx.coroutines.*
import okhttp3.OkHttpClient
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import okhttp3.logging.HttpLoggingInterceptor
import com.marketradar.app.util.LogBuffer
import java.net.URLEncoder
import java.text.SimpleDateFormat
import java.util.*
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.io.File

class MarketWatchService : Service() {

    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val bnfWebSocket by lazy { BnfWebSocketCapture(filesDir) }
    private var wakeLock: PowerManager.WakeLock? = null
    private var sessionWakeLock: PowerManager.WakeLock? = null
    private lateinit var prefs: SharedPreferences
    // R3: one publication fence for the expiry-fallback apply (generation persisted in prefs; survives restart)
    private val expiryFallbackPublication by lazy {
        ExpiryFallbackPublication(object : ExpiryFallbackPublication.GenerationStore {
            override fun load(): Long = prefs.getLong(EXPIRY_FALLBACK_GENERATION_PREF, 0L)
            override fun save(value: Long) { prefs.edit().putLong(EXPIRY_FALLBACK_GENERATION_PREF, value).commit() }
        })
    }
    // R3: publication decisions are sent to Python in order on one background thread, never blocking a poll
    private val expiryFallbackReconcileQueue = java.util.concurrent.Executors.newSingleThreadExecutor { r ->
        Thread(r, "expiry-fallback-reconcile").apply { isDaemon = true }
    }
    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .addInterceptor(HttpLoggingInterceptor { msg ->
            LogBuffer.add('I', "OkHttp", msg)
        }.apply { level = HttpLoggingInterceptor.Level.BASIC })
        .build()
    private val elephantHandoffClient = client.newBuilder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .writeTimeout(5, TimeUnit.SECONDS)
        .readTimeout(5, TimeUnit.SECONDS)
        .build()
    
    private var token401Counter = 0

    private var pollingJob: Job? = null
    private val serviceStartInProgress = AtomicBoolean(false)
    private val pollInProgress = AtomicBoolean(false)
    private val bnfUniverseInProgress = AtomicBoolean(false)
    private val pollSaveLock = Any()
    private val pollDispatchLock = Any()
    private val pollDispatchInFlightKeys = mutableSetOf<String>()
    private var pollLoopIteration = 0L
    private var marginQuoteCachePollKey = ""
    private val marginQuoteCache = mutableMapOf<String, MarginQuote>()
    // Evidence batch 1 (Item C): previous poll's spot provenance, for unchanged/repeated-feed detection.
    private var lastSpotProvenance: JSONObject? = null

    private data class BreadthStock(
        val symbol: String,
        val requestKey: String,
        val responseKey: String,
        val weight: Double
    )

    private data class MarginQuote(
        val status: String,
        val requestUrl: String,
        val requiredMargin: Double? = null,
        val finalMargin: Double? = null,
        val spanMargin: Double? = null,
        val exposureMargin: Double? = null,
        val netBuyPremium: Double? = null,
        val marginCount: Int = 0,
        val quotedAt: String? = null,
        val error: String? = null
    ) {
        fun toJson(): JSONObject = JSONObject().apply {
            put("status", status)
            put("request_url", requestUrl)
            if (requiredMargin != null) put("required_margin", requiredMargin)
            if (finalMargin != null) put("final_margin", finalMargin)
            if (spanMargin != null) put("span_margin", spanMargin)
            if (exposureMargin != null) put("exposure_margin", exposureMargin)
            if (netBuyPremium != null) put("net_buy_premium", netBuyPremium)
            put("margin_count", marginCount)
            if (!quotedAt.isNullOrBlank()) put("quoted_at", quotedAt)
            if (!error.isNullOrBlank()) put("error", error)
        }
    }

    private fun mergeHistoryRowsByDate(base: JSONArray, overlay: JSONArray): JSONArray {
        val merged = LinkedHashMap<String, JSONObject>()
        val protectedLiveKeys = setOf("vix", "pcr", "fii_short_pct")
        var protectedOverwriteCount = 0
        val protectedOverwriteKeys = linkedSetOf<String>()

        fun upsertAll(source: JSONArray, replaceExisting: Boolean) {
            for (i in 0 until source.length()) {
                val row = source.optJSONObject(i) ?: continue
                val day = row.optString("date", row.optString("session_date", "")).trim()
                if (day.isEmpty()) continue
                val current = merged[day] ?: JSONObject()
                val target = if (replaceExisting) JSONObject(current.toString()) else current
                val names = row.names() ?: JSONArray()
                for (j in 0 until names.length()) {
                    val key = names.optString(j).trim()
                    if (key.isEmpty()) continue
                    if (!replaceExisting && target.has(key)) continue
                    if (replaceExisting && target.has(key) && protectedLiveKeys.contains(key)) {
                        protectedOverwriteCount += 1
                        protectedOverwriteKeys.add(key)
                    }
                    target.put(key, row.opt(key))
                }
                if (!target.has("date")) target.put("date", day)
                if (!target.has("session_date")) target.put("session_date", day)
                merged[day] = target
            }
        }

        upsertAll(base, replaceExisting = false)
        upsertAll(overlay, replaceExisting = true)
        Log.i(
            TAG,
            "CTX_PERCENTILE_MERGE_COLLISION_CHECK: protectedOverwrites=$protectedOverwriteCount keys=${protectedOverwriteKeys.joinToString(",")}"
        )

        val out = JSONArray()
        for ((_, row) in merged) {
            out.put(row)
        }
        return out
    }

    private fun countPercentileEnrichedHistoryRows(history: JSONArray): Int {
        var count = 0
        for (i in 0 until history.length()) {
            val row = history.optJSONObject(i) ?: continue
            if (
                row.has("pct_iv_richness_menu_median") ||
                row.has("pct_credit_width_ratio_menu_median") ||
                row.has("pct_sigma_otm_menu_median") ||
                row.has("pct_premium_edge_menu_median") ||
                row.has("history_window_end")
            ) {
                count += 1
            }
        }
        return count
    }

    companion object {
        const val CHANNEL_ID = "market_radar_service"
        const val NOTIFICATION_ID = 1001
        const val TAG = "MarketWatchService"
        private const val ACTION_POLL_TICK = "com.marketradar.app.ACTION_POLL_TICK"
        const val ACTION_FORCE_POLL = "com.marketradar.app.ACTION_FORCE_POLL"
        /** b507: explicit analysis refresh after an acknowledged input save (no poll, no slot). */
        const val ACTION_ANALYSIS_REFRESH = "com.marketradar.app.ACTION_ANALYSIS_REFRESH"
        const val ANALYSIS_MODE_POLL = "POLL"
        const val ANALYSIS_MODE_REFRESH = "REFRESH"
        const val PREF_ANALYSIS_REFRESH_STATE = "analysis_refresh_state_v1"
        private const val POLL_ALARM_REQ_CODE = 74051
        private const val LAST_POLL_DISPATCH_SLOT_KEY = "last_poll_dispatch_slot"
        private const val LAST_SUCCESSFUL_POLL_SLOT_KEY = "last_successful_poll_slot"
        private const val LAST_ASSIGNED_POLL_NUMBER_KEY = "last_assigned_poll_number"
        private const val LAST_ASSIGNED_POLL_DATE_KEY = "last_assigned_poll_date"
        private const val COVERAGE_INTEGRITY_DATE_KEY = "coverage_integrity_date"
        private const val COVERAGE_INTEGRITY_KEY = "coverage_integrity"
        private const val COVERAGE_INTEGRITY_ISSUE_KEY = "coverage_integrity_issue"
        private const val LEASE_OWNER_PID_KEY = "lease_owner_pid"
        private const val LEASE_STARTED_MS_KEY = "lease_started_ms"
        private const val LEASE_HEARTBEAT_MS_KEY = "lease_heartbeat_ms"
        private const val LEASE_STALE_MS = 10 * 60 * 1000L
        private const val SESSION_POLL_GAP_WARNING_MS = 12 * 60 * 1000L
        // Live evidence on 2026-09-28 showed valid brain.analyze completions up
        // to ~80 seconds. Keep this below the five-minute poll cadence while
        // preserving PyTimeout's one-in-flight guard.
        private const val PY_POLL_ANALYZE_TIMEOUT_MS = 90_000L
        // b503: retained at 4 s. This is the caller's WAIT, not a loss boundary: a slower
        // capture is delivered LATE from the original call by CaptureCoordinator. No measured
        // on-device evidence yet justifies a different capture wait.
        private const val PY_SNAPSHOT_TIMEOUT_MS = 4_000L
        // b503: Paper-menu margin enrichment has its own constant (same value as before) and its
        // own retry-within-window path; it no longer shares the snapshot capture constant.
        private const val PY_PAPER_MENU_ENRICH_TIMEOUT_MS = 4_000L
        private const val PY_AGENT_TIMEOUT_MS = 3_000L
        private const val PY_POLL_TIMEOUT_KEY = "poll.brain"
        private const val PY_EXPIRY_FALLBACK_TIMEOUT_KEY = "poll.expiry_fallback"
        private const val PY_EXPIRY_FALLBACK_RESOLVE_KEY = "poll.expiry_fallback_resolve"
        private const val PY_EXPIRY_FALLBACK_RESOLVE_TIMEOUT_MS = 2_000L
        // R3 (Codex R2 reply s3): persisted attempt generation for expiry-fallback publication reconciliation
        private const val EXPIRY_FALLBACK_GENERATION_PREF = "expiry_fallback_publication_generation_v1"
        private const val PY_SNAPSHOT_TIMEOUT_KEY = "poll.snapshot"
        private const val PY_PAPER_MENU_TIMEOUT_KEY = "poll.paper_menu_capture"
        // Evidence batch 1 (Item B): once-per-session Paper decision-menu capture state.
        private const val PAPER_MENU_STATE_KEY = "paper_menu_capture_state_v1"
        private const val PAPER_MENU_OUTBOX_SUMMARY_KEY = "paper_menu_outbox_summary_v1"
        private const val PAPER_MENU_EXTRA_MARGIN_REQUESTS = 2
        private const val PAPER_MENU_LOCAL_RETENTION_DAYS = 30L
        private const val PREF_LAST_BRAIN_ANALYSIS_SUCCESS_MS = "last_brain_analysis_success_ms"
        private const val PREF_LAST_BRAIN_ANALYSIS_FAILURE_MS = "last_brain_analysis_failure_ms"
        private const val PREF_LAST_BRAIN_ANALYSIS_FAILURE_REASON = "last_brain_analysis_failure_reason"
        private const val PREF_LAST_BRAIN_ANALYSIS_DURATION_MS = "last_brain_analysis_duration_ms"
        private const val PREF_NOTIFICATION_TRANSPORT_MODE = "brain_notification_transport_mode"
        private const val PREF_LAST_BRAIN_NOTIFICATION = "last_brain_notification"
        private const val PREF_LAST_BRAIN_NOTIFICATION_META = "last_brain_notification_meta"
        private const val PREF_MORNING_INPUT_BLOCK_NOTIFIED_DATE = "morning_input_block_notified_date"
        // Shared with PositionTickService.PREF_POSITION_EXIT_THRESHOLDS.
        private const val PREF_POSITION_EXIT_THRESHOLDS = "position_exit_thresholds"
        private const val APPROVED_BRANCH_PROPOSALS_KEY = "approved_branch_proposals"
        private const val APPROVED_BRANCH_PROPOSALS_SYNC_MS_KEY = "approved_branch_proposals_sync_ms"
        private const val APPROVED_BRANCH_PROPOSALS_TTL_MS = 15 * 60 * 1000L
        private const val SIGNAL_RELIABILITY_CACHE_KEY = "signal_reliability_rows_json"
        private const val SIGNAL_RELIABILITY_CACHE_MONTH_KEY = "signal_reliability_cache_month"
        private const val EXPIRY_REFRESH_DATE_KEY = "expiry_refresh_date"
        private const val LAST_POLL_GAP_WARNING_SLOT_KEY = "last_poll_gap_warning_slot"
        private const val MARKET_OPEN_MINUTE = 9 * 60 + 15
        // Shared schedule (G4): native poll/session close = 15:40 IST.
        // Policy exit intent is 15:15 (PositionPolicyV1 / PositionExitPolicy).
        // Python readiness helper uses 15:30. Do not randomly change these.
        private const val MARKET_CLOSE_MINUTE = 15 * 60 + 40
        private const val SETTLED_EVALUATION_START_MINUTE = 16 * 60 + 30
        private const val POLL_SLOT_MINUTES = 5
        private const val POLL_FULL_DAY_SLOTS = ((MARKET_CLOSE_MINUTE - MARKET_OPEN_MINUTE) / POLL_SLOT_MINUTES) + 1
        private const val POLL_SLOT_TRIGGER_LAG_MS = 5_000L
        private const val SERVICE_SELF_HEAL_REQ_CODE = 74052
        private const val SERVICE_SELF_HEAL_DELAY_MS = 15_000L
        private const val FGS_BLOCKED_UNTIL_MS_KEY = "market_watch_fgs_blocked_until_ms"
        private const val FGS_BLOCKED_COUNT_KEY = "market_watch_fgs_blocked_count"
        private const val FGS_BLOCKED_BASE_BACKOFF_MS = 10 * 60 * 1000L
        private const val FGS_BLOCKED_MAX_BACKOFF_MS = 30 * 60 * 1000L
        private const val ELEPHANT_BASE_URL = "https://marketradar-oracle.online"
        private const val UPSTOX_MARGIN_URL = "https://api.upstox.com/v2/charges/margin"
        private const val MARGIN_SHADOW_CANDIDATE_CAP = 10
        private const val DAY_EVAL_HANDOFF_TS_KEY = "day_eval_handoff_ts"
        private const val DAY_EVAL_HANDOFF_DATE_KEY = "day_eval_handoff_date"
        private const val DAY_EVAL_HANDOFF_STALE_MS = 4 * 60 * 60 * 1000L
        
        private const val KOTAK_KEY_PREF = "kotak_instrument_key"
        private const val KOTAK_KEY_CURRENT = "NSE_EQ|INE237A01036"
        private const val KOTAK_KEY_LEGACY = "NSE_EQ|INE237A01028"

        private val BNF_STOCKS_BASE = listOf(
            BreadthStock("HDFCBANK", "NSE_EQ|INE040A01034", "NSE_EQ:HDFCBANK", 0.2522),
            BreadthStock("SBIN", "NSE_EQ|INE062A01020", "NSE_EQ:SBIN", 0.2042),
            BreadthStock("ICICIBANK", "NSE_EQ|INE009A01021", "NSE_EQ:ICICIBANK", 0.1977),
            BreadthStock("AXISBANK", "NSE_EQ|INE238A01034", "NSE_EQ:AXISBANK", 0.0865)
        )
    }

    private data class SessionIntegritySummary(
        val sessionDate: String,
        val pollCount: Int,
        val expectedFullDay: Int,
        val finalSlotOccurrences: Int,
        val snapshotRows: Int,
        val snapshotOverrun: Boolean,
        val coverageIntegrity: String,
        val integrityIssue: String
    )

    override fun onCreate() {
        super.onCreate()
        prefs = getSharedPreferences("market_radar", Context.MODE_PRIVATE)

        // Restore NotificationAgent state if available
        try {
            val savedState = prefs.getString("notification_agent_state", null)
            if (savedState != null) {
                val py = Python.getInstance()
                val brain = py.getModule("brain")
                brain.callAttr("reset_notification_agent", savedState)
                Log.i(TAG, "AGENT_STATE_RESTORED")
            }
        } catch (e: Exception) {
            Log.w(TAG, "AGENT_STATE_RESTORE_FAIL: ${e.message}")
        }

        Log.i(TAG, "BL_A_CHECK_ENTERED: site=Service.onCreate")
        val now = System.currentTimeMillis()
        val lastServiceCreate = prefs.getLong("last_service_create_ms", 0L)
        if (lastServiceCreate > 0L) {
            val gapMs = now - lastServiceCreate
            if (gapMs in 1 until 30_000L) {
                Log.w(TAG, "BL_A_SERVICE_RESTART_GAP_MS=$gapMs")
                LogBuffer.add('W', TAG, "BL_A_SERVICE_RESTART_GAP_MS=$gapMs")
            }
        }
        prefs.edit().putLong("last_service_create_ms", now).commit()
        LogBuffer.add('I', "MarketWatchService", "Service started, pid=${android.os.Process.myPid()}")
        createNotificationChannel()
    }

    private fun getCachedKotakKey(): String {
        val cached = prefs.getString(KOTAK_KEY_PREF, KOTAK_KEY_CURRENT) ?: KOTAK_KEY_CURRENT
        return if (cached.isNotBlank()) cached else KOTAK_KEY_CURRENT
    }

    private fun getBnfStocks(kotakKey: String = getCachedKotakKey()): List<BreadthStock> {
        return listOf(BreadthStock("KOTAKBANK", kotakKey, "NSE_EQ:KOTAKBANK", 0.0781)) + BNF_STOCKS_BASE
    }

    private suspend fun resolveKotakKey(token: String): String {
        val candidates = listOf(getCachedKotakKey(), KOTAK_KEY_CURRENT, KOTAK_KEY_LEGACY).distinct()
        for (candidate in candidates) {
            try {
                val url = "https://api.upstox.com/v2/market-quote/quotes?instrument_key=$candidate"
                val json = fetchSync(url, token) ?: continue
                val data = json.optJSONObject("data") ?: continue
                val hit = findQuoteByInstrument(data, candidate, "NSE_EQ:KOTAKBANK")
                if (hit != null) {
                    prefs.edit().putString(KOTAK_KEY_PREF, candidate).apply()
                    LogBuffer.add('I', TAG, "KOTAK_KEY_RESOLVED: $candidate")
                    return candidate
                }
            } catch (_: Exception) {
            }
        }
        return getCachedKotakKey()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!CloudRecovery.canMonitor() && intent?.action != "STOP") { stopSelf(); return START_NOT_STICKY }
        if (intent?.action == "STOP") {
            cancelPollAlarm()
            releaseLease()
            stopPolling()
            return START_NOT_STICKY
        }

        if (foregroundStartBackoffRemainingMs() > 0L) {
            val remainingMs = foregroundStartBackoffRemainingMs()
            Log.w(TAG, "SERVICE_START_BACKOFF_ACTIVE: remainingMs=$remainingMs")
            LogBuffer.add('W', TAG, "SERVICE_START_BACKOFF_ACTIVE: remainingMs=$remainingMs")
            stopSelf()
            return START_NOT_STICKY
        }

        val lease = claimLease()
        Log.w(TAG, "LEASE_RESULT: claimed=${lease.claimed} reason=${lease.reason} gapMs=${lease.gapMs}")
        LogBuffer.add('W', TAG, "LEASE_RESULT: claimed=${lease.claimed} reason=${lease.reason} gapMs=${lease.gapMs}")
        if (!lease.claimed) {
            stopSelf()
            return START_NOT_STICKY
        }

        try {
            startLiveForeground(NOTIFICATION_ID, createNotification("Service Starting", "Initializing poll loop..."))
            clearForegroundStartBackoff()
        } catch (t: Throwable) {
            Log.e(TAG, "SERVICE_START_FOREGROUND_BLOCKED: ${t.javaClass.simpleName}: ${t.message}")
            LogBuffer.add('E', TAG, "SERVICE_START_FOREGROUND_BLOCKED: ${t.javaClass.simpleName}: ${t.message}")
            recordForegroundStartBlocked(t)
            releaseLease()
            stopSelf()
            return START_NOT_STICKY
        }
        prefs.edit().putBoolean("service_running", true).commit() // NB5: use commit() for cross-process visibility
        PositionTickService.ensureRunning(this)
        maintainSessionWakeLock()
        ensurePollAlarmScheduled()
        serviceScope.launch(Dispatchers.IO) { drainPc2CompactOutbox() }
        serviceScope.launch(Dispatchers.IO) {
            try { drainBrainSnapshotOutbox() }
            catch (t: Throwable) { LogBuffer.add('E', TAG, "ML_BRAIN_SNAPSHOT_OUTBOX_DRAIN_THROW: ${t.javaClass.simpleName}") }
        }
        serviceScope.launch(Dispatchers.IO) {
            try { drainGeneratedCandidateOutbox() }
            catch (t: Throwable) { LogBuffer.add('E', TAG, "ML_GENERATED_OUTBOX_DRAIN_THROW: ${t.javaClass.simpleName}") }
        }
        // b503: reconcile capture journals FIRST (live owners are never treated as lost; a
        // service restart inside the same process is not process death), then drain the
        // retained legacy PC2 rows and capture events independently of the other channels.
        CaptureRuntime.ensure(applicationContext)
        CaptureRuntime.drainHook = { launchCaptureDrains() }
        launchCaptureDrains()

        if (intent?.action == ACTION_POLL_TICK) {
            serviceScope.launch { maybeRunPollFromAlarm() }
        } else if (intent?.action == ACTION_FORCE_POLL) {
            serviceScope.launch { maybeRunForcedPoll() }
        } else if (intent?.action == ACTION_ANALYSIS_REFRESH) {
            requestAnalysisRefresh("input_saved")
        }

        if (pollingJob?.isActive == true || !serviceStartInProgress.compareAndSet(false, true)) {
            Log.w(TAG, "SERVICE_START_IGNORED: polling already active or startup in progress")
            LogBuffer.add('W', TAG, "SERVICE_START_IGNORED: polling already active or startup in progress")
            return START_STICKY
        }
        
        serviceScope.launch {
            try {
                bootstrapFromSupabase()
                // We need the token for OHLC/Futures Key resolution which are Startup tasks
                val token = prefs.getString("auth_token", "") ?: ""
                if (token.isNotEmpty()) {
                    bootstrapFromUpstox(token)
                }
                startPolling()
            } finally {
                serviceStartInProgress.set(false)
            }
        }
        return START_STICKY
    }

    private data class LeaseResult(val claimed: Boolean, val reason: String, val gapMs: Long = 0L)

    private fun claimLease(): LeaseResult {
        val myPid = android.os.Process.myPid()
        val now = System.currentTimeMillis()
        val ownerPid = prefs.getInt(LEASE_OWNER_PID_KEY, 0)
        val heartbeat = prefs.getLong(LEASE_HEARTBEAT_MS_KEY, 0L)
        val gapMs = if (heartbeat > 0L) now - heartbeat else Long.MAX_VALUE

        if (ownerPid == 0 || heartbeat == 0L) {
            prefs.edit()
                .putInt(LEASE_OWNER_PID_KEY, myPid)
                .putLong(LEASE_STARTED_MS_KEY, now)
                .putLong(LEASE_HEARTBEAT_MS_KEY, now)
                .commit()
            return LeaseResult(true, "fresh_claim")
        }

        if (ownerPid == myPid) {
            prefs.edit().putLong(LEASE_HEARTBEAT_MS_KEY, now).commit()
            return LeaseResult(true, "self_refresh", gapMs)
        }

        if (!isPidAlive(ownerPid)) {
            Log.w(TAG, "LEASE_STOLE_DEAD_PID: oldPid=$ownerPid oldHeartbeatAgeMs=$gapMs")
            LogBuffer.add('W', TAG, "LEASE_STOLE_DEAD_PID: oldPid=$ownerPid oldHeartbeatAgeMs=$gapMs")
            prefs.edit()
                .putInt(LEASE_OWNER_PID_KEY, myPid)
                .putLong(LEASE_STARTED_MS_KEY, now)
                .putLong(LEASE_HEARTBEAT_MS_KEY, now)
                .commit()
            return LeaseResult(true, "stole_dead", gapMs)
        }

        if (gapMs < LEASE_STALE_MS) {
            Log.w(TAG, "LEASE_REJECTED_LIVE_OWNER: ownerPid=$ownerPid heartbeatAgeMs=$gapMs")
            LogBuffer.add('W', TAG, "LEASE_REJECTED_LIVE_OWNER: ownerPid=$ownerPid heartbeatAgeMs=$gapMs")
            return LeaseResult(false, "active_owner_pid_$ownerPid", gapMs)
        }

        prefs.edit()
            .putInt(LEASE_OWNER_PID_KEY, myPid)
            .putLong(LEASE_STARTED_MS_KEY, now)
            .putLong(LEASE_HEARTBEAT_MS_KEY, now)
            .commit()
        return LeaseResult(true, "stole_stale", gapMs)
    }

    private fun isPidAlive(pid: Int): Boolean {
        if (pid <= 0 || pid == android.os.Process.myPid()) return false
        return File("/proc/$pid").exists()
    }

    private fun releaseLease() {
        prefs.edit()
            .remove(LEASE_OWNER_PID_KEY)
            .remove(LEASE_STARTED_MS_KEY)
            .remove(LEASE_HEARTBEAT_MS_KEY)
            .commit()
    }

    private fun startLiveForeground(id: Int, notification: Notification) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(id, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(id, notification)
        }
    }

    private fun foregroundStartBackoffRemainingMs(nowMs: Long = System.currentTimeMillis()): Long {
        val untilMs = prefs.getLong(FGS_BLOCKED_UNTIL_MS_KEY, 0L)
        return (untilMs - nowMs).coerceAtLeast(0L)
    }

    private fun clearForegroundStartBackoff() {
        if (!prefs.contains(FGS_BLOCKED_UNTIL_MS_KEY) && !prefs.contains(FGS_BLOCKED_COUNT_KEY)) return
        prefs.edit()
            .remove(FGS_BLOCKED_UNTIL_MS_KEY)
            .remove(FGS_BLOCKED_COUNT_KEY)
            .commit()
        LogBuffer.add('I', TAG, "SERVICE_START_BACKOFF_CLEARED")
    }

    private fun recordForegroundStartBlocked(t: Throwable) {
        val now = System.currentTimeMillis()
        val previousCount = prefs.getInt(FGS_BLOCKED_COUNT_KEY, 0)
        val count = (previousCount + 1).coerceAtMost(6)
        val backoffMs = (FGS_BLOCKED_BASE_BACKOFF_MS * count).coerceAtMost(FGS_BLOCKED_MAX_BACKOFF_MS)
        prefs.edit()
            .putInt(FGS_BLOCKED_COUNT_KEY, count)
            .putLong(FGS_BLOCKED_UNTIL_MS_KEY, now + backoffMs)
            .commit()
        Log.w(TAG, "SERVICE_START_FOREGROUND_BACKOFF: count=$count backoffMs=$backoffMs error=${t.javaClass.simpleName}")
        LogBuffer.add('W', TAG, "SERVICE_START_FOREGROUND_BACKOFF: count=$count backoffMs=$backoffMs error=${t.javaClass.simpleName}")
    }

    private suspend fun bootstrapFromSupabase() {
        Log.i(TAG, "BL_A_CHECK_ENTERED: site=bootstrapFromSupabase")
        val lastSync = prefs.getLong("last_bootstrap_time", 0L)
        val now = System.currentTimeMillis()
        val today = todayIstDate()
        val lastPollDate = prefs.getString("last_poll_date", "") ?: ""
        val shortGapMs = now - lastSync
        if (lastSync > 0L && shortGapMs in 1 until 30_000L) {
            Log.w(TAG, "BL_A_BOOTSTRAP_SHORT_GAP_SKIP_MS=$shortGapMs")
            LogBuffer.add('W', TAG, "BL_A_BOOTSTRAP_SHORT_GAP_SKIP_MS=$shortGapMs")
            return
        }
        
        // Only fetch if data is missing or older than 30 minutes
        val isStale = (now - lastSync) > 30 * 60 * 1000L
        val baselineRaw = prefs.getString("morning_baseline", null)
        val hasBaseline = try {
            if (baselineRaw.isNullOrBlank()) {
                false
            } else {
                val baseline = JSONObject(baselineRaw)
                baseline.optString("date", "") == today
            }
        } catch (_: Exception) {
            false
        }
        
        if (!isStale && hasBaseline && lastPollDate == today) {
            Log.d(TAG, "Bootstrap skipped: data is fresh")
            if (!prefs.contains(VixDailyHistory.PREF_KEY)) {
                // First start of a build that introduced the daily VIX history
                // on a day whose bootstrap already ran: fetch it once now.
                withContext(Dispatchers.IO) { refreshVixDailyHistory(today) }
            }
            val premHistory = JSONArray(prefs.getString("premium_history", "[]"))
            val historyLoadedLog = "HISTORY_LOADED_RESTART: vixCount=${premHistory.length()}, fiiCount=${extractFiiHistory().length()}"
            Log.d(TAG, historyLoadedLog)
            // Warm-upgrade: scrub retired afternoon-positioning prefs/context once
            sanitizeRetiredPositioningState()
            return
        }

        updateForegroundNotification("Bootstrapping", "Fetching data from Supabase...")
        Log.d(TAG, "Starting bootstrap from Supabase...")

        withContext(Dispatchers.IO) {
            try {
                // 1. Baseline
                SupabaseClient.getBaseline()?.let {
                    prefs.edit().putString("morning_baseline", it.toString()).apply()
                }

                // 2. Open Trades
                val open = SupabaseClient.getOpenTrades()
                prefs.edit().putString("open_trades", open.toString()).apply()

                // 3. Closed Trades
                SupabaseClient.getClosedTradesOrNull()?.let { closed ->
                    ClosedTradeLedger.reconcileRemote(
                        closed.toString(),
                        prefs.getString(ClosedTradeLedger.PENDING_CLOSED_TRADES_PREF, "[]")
                    )?.let { reconciled ->
                        prefs.edit()
                            .putString(ClosedTradeLedger.CLOSED_TRADES_PREF, reconciled.closedTradesJson)
                            .putString(ClosedTradeLedger.PENDING_CLOSED_TRADES_PREF, reconciled.pendingTradesJson)
                            .commit()
                    }
                } ?: LogBuffer.add('W', TAG, "CLOSED_TRADE_BOOTSTRAP_UNAVAILABLE: preserving local risk ledger")
                // Network fetch stays OUTSIDE pollSaveLock so savePoll() is never blocked on it.
                val history = SupabaseClient.getPollHistory(today)
                // The remote copy is sampled (every third assigned poll, best effort) and can lag the
                // device, so it is MERGED into the retained local history rather than replacing it.
                // Replacing could discard same-session observations the device already holds, and a
                // discarded stretch is not recoverable: the ER series is rebuilt from the open
                // reference every poll, so one gap over ER_MAX_GAP_MIN suppresses ER for the rest of
                // the session.
                //
                // BOTH outcomes (usable remote, and empty/invalid remote) are decided from state
                // re-read under the same lock savePoll() uses, by PollHistoryRestore.plan. That is
                // what makes the decision safe: a poll appended while the request was in flight
                // cannot be erased by a stale pre-fetch copy, undated local rows are not assumed to
                // own this session, prior counters are honoured only by the session that owns them,
                // and a response that arrives after IST rollover is discarded instead of applied.
                synchronized(pollSaveLock) {
                    val plan = PollHistoryRestore.plan(
                        local = PollHistoryRestore.LocalState(
                            historyJson = prefs.getString("poll_history", "[]"),
                            pollCount = prefs.getInt("poll_count", 0),
                            lastPollDate = prefs.getString("last_poll_date", "") ?: "",
                            assignedDate = prefs.getString(LAST_ASSIGNED_POLL_DATE_KEY, "") ?: "",
                            assignedNumber = prefs.getInt(LAST_ASSIGNED_POLL_NUMBER_KEY, 0),
                            latestPollPresent = prefs.getString("latest_poll", "null") != "null",
                            lastPollTimePresent = prefs.contains("last_poll_time")
                        ),
                        remote = history,
                        requestedSession = today,
                        activeSession = todayIstDate()
                    )
                    LogBuffer.add('I', TAG, "POLL_HISTORY_RESTORE: action=${plan.action}" +
                        " remote=${history.length()} ${plan.note}" +
                        " v=${PollHistoryRestore.VERSION}/${PollHistoryMerge.VERSION}")
                    // The plan is applied by PollHistoryRestore.apply, so the production
                    // transaction -- including the removals a RESET performs -- is the same code
                    // the unit tests drive through a map-backed StateWriter.
                    val editor = prefs.edit()
                    PollHistoryRestore.apply(
                        plan = plan,
                        state = object : PollHistoryRestore.StateWriter {
                            override fun putString(key: String, value: String) { editor.putString(key, value) }
                            override fun putInt(key: String, value: Int) { editor.putInt(key, value) }
                            override fun remove(key: String) { editor.remove(key) }
                        },
                        assignedDateKey = LAST_ASSIGNED_POLL_DATE_KEY,
                        assignedNumberKey = LAST_ASSIGNED_POLL_NUMBER_KEY,
                        coverageDateKey = COVERAGE_INTEGRITY_DATE_KEY,
                        extraResetKeys = listOf(
                            LAST_POLL_DISPATCH_SLOT_KEY,
                            LAST_SUCCESSFUL_POLL_SLOT_KEY,
                            COVERAGE_INTEGRITY_KEY,
                            COVERAGE_INTEGRITY_ISSUE_KEY
                        )
                    )
                    editor.commit()
                    if (plan.action == PollHistoryRestore.Action.RESET) {
                        Log.i(TAG, "DAILY_RESET_BOOTSTRAP: cleared stale poll state for $today" +
                            (if (plan.keepAssignment) " (assignment preserved)" else ""))
                    }
                }

                // 5. Historial Premium Data & Signal
                val percentileHistory = SupabaseClient.getContextPercentileDailyHistory()
                val premHistory = mergeHistoryRowsByDate(
                    SupabaseClient.getPremiumHistory(),
                    percentileHistory
                )
                if (premHistory.length() > 0) {
                    prefs.edit().putString("premium_history", premHistory.toString()).apply()
                }
                refreshVixDailyHistory(today)
                val supplyQualityHistory = SupabaseClient.getPc2SupplyQualityHistory(today)
                prefs.edit().putString("pc2_supply_quality_history", supplyQualityHistory.toString()).apply()
                
                // Retire afternoon positioning: no yesterday-signal bootstrap fetch.

                val approvedBranchRows = SupabaseClient.select("ai_branch_proposals", "status=eq.approved", "approved_at.desc", 50)
                prefs.edit()
                    .putString(APPROVED_BRANCH_PROPOSALS_KEY, approvedBranchRows.toString())
                    .putLong(APPROVED_BRANCH_PROPOSALS_SYNC_MS_KEY, now)
                    .apply()

                val historyLoadedLog = "HISTORY_LOADED: vixCount=${premHistory.length()}, ivPercentile=${calculateIvPercentile(18.0)}, fiiCount=${extractFiiHistory().length()}, percentileDays=${percentileHistory.length()}, supplySliceRows=${supplyQualityHistory.length()}"
                Log.d(TAG, historyLoadedLog)

                prefs.edit().putLong("last_bootstrap_time", now).commit()
                sanitizeRetiredPositioningState()
                Log.d(TAG, "Bootstrap complete")
            } catch (e: Exception) {
                Log.e(TAG, "Bootstrap failed: ${e.message}")
            }
        }
    }

    private fun getApprovedBranchProposalsCached(): JSONArray {
        return try {
            JSONArray(prefs.getString(APPROVED_BRANCH_PROPOSALS_KEY, "[]") ?: "[]")
        } catch (_: Exception) {
            JSONArray()
        }
    }

    private suspend fun refreshApprovedBranchProposals(force: Boolean = false): JSONArray {
        val now = System.currentTimeMillis()
        val lastSync = prefs.getLong(APPROVED_BRANCH_PROPOSALS_SYNC_MS_KEY, 0L)
        if (!force && (now - lastSync) < APPROVED_BRANCH_PROPOSALS_TTL_MS) {
            return getApprovedBranchProposalsCached()
        }
        return withContext(Dispatchers.IO) {
            try {
                val rows = SupabaseClient.select("ai_branch_proposals", "status=eq.approved", "approved_at.desc", 50)
                prefs.edit()
                    .putString(APPROVED_BRANCH_PROPOSALS_KEY, rows.toString())
                    .putLong(APPROVED_BRANCH_PROPOSALS_SYNC_MS_KEY, now)
                    .commit()
                rows
            } catch (e: Exception) {
                Log.e(TAG, "refreshApprovedBranchProposals failed: ${e.message}")
                getApprovedBranchProposalsCached()
            }
        }
    }

    private suspend fun bootstrapFromUpstox(token: String) {
        try {
            fetchYesterdayOHLC(token)
            // WS1: Ensure we have the near-month keys for both indices for breadth/fp
            val bnfFutKey = getFuturesKey("BNF")
            val nfFutKey = "" // retired; no NF futures request
            Log.d(TAG, "BOOTSTRAP_FUTURES: BNF=$bnfFutKey, NF=$nfFutKey")
            
            // Fetch initial futures quotes to prime the system
            val url = "https://api.upstox.com/v2/market-quote/quotes?instrument_key=$bnfFutKey"
            fetchSync(url, token)
        } catch (e: Exception) {
            Log.e(TAG, "Bootstrap from Upstox failed: ${e.message}")
        }
    }

    private suspend fun fetchYesterdayOHLC(token: String) {
        val today = todayIstDate()
        if (prefs.getString("ohlc_date", "") == today) return

        Log.d(TAG, "Fetching yesterday's OHLC...")
        val url = "https://api.upstox.com/v2/market-quote/ohlc?instrument_key=NSE_INDEX|Nifty Bank&interval=1d"
        val json = fetchSync(url, token)
        if (json != null) {
            prefs.edit().putString("yesterday_ohlc", json.toString())
                .putString("ohlc_date", today)
                .apply()
            Log.d(TAG, "OHLC_FETCHED: Success")
        }
    }

    private fun getYesterdayDate(): String {
        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        val cal = Calendar.getInstance(ist)
        
        // WS2: NSE Holiday support (2026 partial list - example)
        val holidays = setOf("2026-01-26", "2026-03-06", "2026-03-24", "2026-04-02", "2026-04-14", "2026-05-01")
        
        do {
            cal.add(Calendar.DATE, -1)
            val ds = SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = ist }.format(cal.time)
            val isWeekend = cal.get(Calendar.DAY_OF_WEEK) == Calendar.SUNDAY || cal.get(Calendar.DAY_OF_WEEK) == Calendar.SATURDAY
            val isHoliday = holidays.contains(ds)
        } while (isWeekend || isHoliday)
        
        return SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = ist }.format(cal.time)
    }

    private fun getFuturesKey(index: String): String {
        // WS6: Implement near-month futures rollover logic
        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        val cal = Calendar.getInstance(ist)
        val day = cal.get(Calendar.DAY_OF_MONTH)
        val month = cal.get(Calendar.MONTH) // 0-based
        val year = cal.get(Calendar.YEAR)
        
        // Find last Thursday of current month
        val temp = Calendar.getInstance(ist).apply {
            set(Calendar.YEAR, year)
            set(Calendar.MONTH, month)
            set(Calendar.DAY_OF_MONTH, getActualMaximum(Calendar.DAY_OF_MONTH))
        }
        while (temp.get(Calendar.DAY_OF_WEEK) != Calendar.THURSDAY) {
            temp.add(Calendar.DAY_OF_MONTH, -1)
        }
        val lastThursday = temp.get(Calendar.DAY_OF_MONTH)
        
        // If today is past last Thursday, roll to next month
        val targetMonthCal = Calendar.getInstance(ist)
        if (day > lastThursday) {
            targetMonthCal.add(Calendar.MONTH, 1)
        }
        
        val monthStr = SimpleDateFormat("MMM", Locale.US).format(targetMonthCal.time).uppercase()
        val yearShort = SimpleDateFormat("yy", Locale.US).format(targetMonthCal.time)
        
        val normalized = index.trim().uppercase(Locale.US)
        val symbol = when (normalized) {
            "NF", "NIFTY" -> "NIFTY"
            else -> "BANKNIFTY"
        }
        return "NSE_FO|$symbol$yearShort$monthStr" + "FUT"
    }

    private fun normalizeTradeMode(raw: String?): String {
        val m = (raw ?: "").trim().lowercase(Locale.US)
        return when (m) {
            "intraday", "intra", "day" -> "intraday"
            "swing", "carry", "positional" -> "swing"
            else -> ""
        }
    }

    private fun resolveTradeMode(ctxObj: JSONObject): String {
        // User preference is the source of truth. The persisted context is a
        // derived cache and can lag behind the UI after mode switches/rescans.
        val fromPrefs = normalizeTradeMode(prefs.getString("trade_mode", ""))
        val explicit = prefs.getBoolean("trade_mode_explicit", false)
        if (explicit && fromPrefs.isNotEmpty()) {
            LogBuffer.add('I', TAG, "TRADE_MODE_RESOLVED: mode=$fromPrefs source=prefs explicit=true")
            return fromPrefs
        }
        if (fromPrefs == "intraday") {
            LogBuffer.add('I', TAG, "TRADE_MODE_RESOLVED: mode=$fromPrefs source=prefs explicit=false")
            return fromPrefs
        }

        val morningInputStr = prefs.getString("morning_input", null)
        if (!morningInputStr.isNullOrBlank()) {
            try {
                val morning = JSONObject(morningInputStr)
                val fromMorning = normalizeTradeMode(
                    morning.optString("tradeMode", morning.optString("mode", ""))
                )
                if (fromMorning == "intraday") {
                    LogBuffer.add('I', TAG, "TRADE_MODE_RESOLVED: mode=$fromMorning source=morning_input")
                    return fromMorning
                }
            } catch (_: Exception) {
            }
        }

        val fromCtx = normalizeTradeMode(ctxObj.optString("tradeMode", ""))
        if (fromCtx == "intraday") {
            LogBuffer.add('I', TAG, "TRADE_MODE_RESOLVED: mode=$fromCtx source=context")
            return fromCtx
        }
        LogBuffer.add('I', TAG, "TRADE_MODE_RESOLVED: mode=intraday source=default")
        return "intraday"
    }

    private fun startPolling() {
        // M0: reconcile the research manifest after process death / reboot / update (collection only).
        m0Safely("reconcile", Unit) { if (ResearchCollector.isEnabled(m0Dir())) ResearchCollector.reconcile(m0Dir(), System.currentTimeMillis()) }
        if (pollingJob?.isActive == true) {
            Log.w(TAG, "POLL_LOOP_IGNORED: polling loop already active")
            LogBuffer.add('W', TAG, "POLL_LOOP_IGNORED: polling loop already active")
            return
        }
        pollingJob = serviceScope.launch {
            while (isActive) {
                if (!CloudRecovery.canMonitor()) { stopSelf(); return@launch }
                pollLoopIteration += 1
                val now = System.currentTimeMillis()
                val configuredPollIntervalMins = prefs.getInt("poll_frequency_mins", 5)
                val pollIntervalMins = if (configuredPollIntervalMins > 0) configuredPollIntervalMins else 5
                val slotKey = currentPollSlotKey(now) ?: "closed"
                Log.d(TAG, "POLL_LOOP_ITERATION: iter=$pollLoopIteration slot=$slotKey delayMins=$pollIntervalMins")
                LogBuffer.add('D', TAG, "POLL_LOOP_ITERATION: iter=$pollLoopIteration slot=$slotKey delayMins=$pollIntervalMins")
                if (isMarketOpen()) {
                    maintainSessionWakeLock()
                    maybeWarnOnPollGap()
                    // Re-read token dynamically on each poll cycle (Bug 1 Fix)
                    val appCtx = applicationContext
                    val currentPrefs = appCtx.getSharedPreferences("market_radar", Context.MODE_PRIVATE)
                    val currentToken = currentPrefs.getString("auth_token", "") ?: ""
                    
                    if (currentToken.isNotEmpty()) {
                        acquirePartialWakeLock()
                        try {
                            dispatchPollIfDue("loop", currentToken)
                        } catch (e: Exception) {
                            Log.e(TAG, "Poll failed: ${e.message}")
                            LogBuffer.add('E', "MarketWatchService", "POLL_LOOP_FAILURE: ${e.message}")
                        } finally {
                            releaseWakeLock()
                        }
                    } else {
                        Log.w(TAG, "No auth_token in prefs, skipping poll.")
                        updateForegroundNotification("Waiting for Token", "Open app to sync Upstox token")
                    }
                } else {
                    releaseSessionWakeLock()
                    if (handlePostCloseEvaluationHandoff(now)) return@launch
                    Log.d(TAG, "Market closed. Skipping poll.")
                    updateForegroundNotification("Market Closed", "Waiting for next session...")
                }

                // WS5: Read poll delay dynamically (default 5m)
                if (configuredPollIntervalMins <= 0) {
                    LogBuffer.add('W', TAG, "POLL_INTERVAL_INVALID: $configuredPollIntervalMins, using 5")
                }
                ensurePollAlarmScheduled()
                val waitMs = nextPollLoopDelayMs()
                val sleepStart = SystemClock.elapsedRealtime()
                delay(waitMs)
                val actualSleepMs = SystemClock.elapsedRealtime() - sleepStart
                if (actualSleepMs > waitMs + 60_000L) {
                    Log.w(TAG, "POLL_SLEEP_DRIFT: expectedMs=$waitMs actualMs=$actualSleepMs overMs=${actualSleepMs - waitMs}")
                    LogBuffer.add('W', TAG, "POLL_SLEEP_DRIFT: expectedMs=$waitMs actualMs=$actualSleepMs overMs=${actualSleepMs - waitMs}")
                }
            }
        }
    }

    private fun stopPolling() {
        prefs.edit().putBoolean("service_running", false).commit() 
        cancelPollAlarm()
        releaseLease()
        // WS27: Cancel job before stopping foreground to ensure no new notifications are triggered
        pollingJob?.cancel()
        serviceScope.cancel()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun handlePostCloseEvaluationHandoff(nowMs: Long = System.currentTimeMillis()): Boolean {
        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        val cal = Calendar.getInstance(ist).apply { timeInMillis = nowMs }
        val status = MarketOpenScheduler.currentStatus(cal)
        val minutes = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE)
        if (!status.marketDay || status.marketOpen || minutes <= MARKET_CLOSE_MINUTE) return false

        val today = todayIstDate()
        val integrity = computeSessionIntegrity(today)
        persistSessionIntegritySummary(integrity)
        Log.i(
            TAG,
            "SESSION_INTEGRITY: date=${integrity.sessionDate} coverage=${integrity.coverageIntegrity} issue=${integrity.integrityIssue} pollCount=${integrity.pollCount}/${integrity.expectedFullDay} finalSlotOccurrences=${integrity.finalSlotOccurrences} snapshots=${integrity.snapshotRows} snapshotOverrun=${integrity.snapshotOverrun}"
        )
        LogBuffer.add(
            'I',
            TAG,
            "SESSION_INTEGRITY: date=${integrity.sessionDate} coverage=${integrity.coverageIntegrity} issue=${integrity.integrityIssue} pollCount=${integrity.pollCount}/${integrity.expectedFullDay} finalSlotOccurrences=${integrity.finalSlotOccurrences} snapshots=${integrity.snapshotRows}"
        )
        if (integrity.coverageIntegrity == "INTEGRITY_BROKEN") {
            markIncompleteSession(today, integrity)
            MarketOpenScheduler.scheduleNextMarketOpen(this)
            Log.w(TAG, "POST_CLOSE_EVAL_BLOCKED: date=$today issue=${integrity.integrityIssue}")
            stopPolling()
            return true
        }
        val doneToday = prefs.getString("evaluation_done_date", "") == today
        var runningToday = prefs.getString("evaluation_running_date", "") == today
        val handoffTs = prefs.getLong(DAY_EVAL_HANDOFF_TS_KEY, 0L)
        val handoffDate = prefs.getString(DAY_EVAL_HANDOFF_DATE_KEY, "") ?: ""
        val alreadyHandedOff = prefs.getBoolean("hasDayEvalRun", false)
        val runningStartedMs = prefs.getLong("evaluation_job_updated_at_ms", 0L)
        val runningExpired = runningToday && (
            runningStartedMs <= 0L ||
            (nowMs - runningStartedMs) > MarketMLService.EVAL_STALE_AFTER_MS
        )
        if (runningExpired) {
            prefs.edit()
                .remove("evaluation_running_date")
                .remove("evaluation_job_updated_at_ms")
                .commit()
            runningToday = false
            Log.w(TAG, "DAY_EVAL_RUNNING_STATE_RESET: startedMs=$runningStartedMs staleMs=${if (runningStartedMs > 0L) nowMs - runningStartedMs else -1L}")
        }
        val handoffExpired = handoffTs > 0L && (nowMs - handoffTs) > DAY_EVAL_HANDOFF_STALE_MS
        val shouldRetryHandoff = alreadyHandedOff && !runningToday && (handoffExpired || handoffDate != today)
        if (shouldRetryHandoff) {
            prefs.edit()
                .putBoolean("hasDayEvalRun", false)
                .remove(DAY_EVAL_HANDOFF_TS_KEY)
                .remove(DAY_EVAL_HANDOFF_DATE_KEY)
                .commit()
            Log.w(TAG, "DAY_EVAL_HANDOFF_RESET: expired=$handoffExpired handoffDate=$handoffDate handoffTs=$handoffTs")
        }
        val currentHandedOff = if (alreadyHandedOff) !shouldRetryHandoff else false

        if (!currentHandedOff && !doneToday && !runningToday) {
            try {
                val targetMinute: String
                if (minutes >= SETTLED_EVALUATION_START_MINUTE) {
                    val mlService = Intent(this@MarketWatchService, MarketMLService::class.java).apply {
                        action = "ACTION_DAY_EVALUATION"
                        putExtra("session_date", today)
                    }
                    startForegroundService(mlService)
                    targetMinute = "immediate_settled_window"
                } else {
                    MarketMLService.scheduleDayEvaluationReminder(this)
                    targetMinute = "16:30+"
                }
                prefs.edit()
                    .putBoolean("hasDayEvalRun", true)
                    .putLong(DAY_EVAL_HANDOFF_TS_KEY, nowMs)
                    .putString(DAY_EVAL_HANDOFF_DATE_KEY, today)
                    .commit()
                Log.i(TAG, "DAY_EVAL_HANDOFF_DEFERRED: scheduled settled evaluation for $today target=$targetMinute")
                LogBuffer.add('I', TAG, "DAY_EVAL_HANDOFF_DEFERRED: session=$today handoffTs=$nowMs target=$targetMinute")
            } catch (e: Exception) {
                Log.e(TAG, "DAY_EVAL_HANDOFF_FAIL: ${e.message}")
                prefs.edit()
                    .putBoolean("hasDayEvalRun", false)
                    .remove(DAY_EVAL_HANDOFF_TS_KEY)
                    .remove(DAY_EVAL_HANDOFF_DATE_KEY)
                    .commit()
                LogBuffer.add('E', TAG, "DAY_EVAL_HANDOFF_FAIL: ${e.message}")
            }
        } else {
            val remainingMs = if (alreadyHandedOff && handoffTs > 0L) maxOf(0L, DAY_EVAL_HANDOFF_STALE_MS - (nowMs - handoffTs)) else 0L
            Log.i(
                TAG,
                "DAY_EVAL_HANDOFF_SKIP: hasDayEvalRun=$alreadyHandedOff doneToday=$doneToday runningToday=$runningToday handoffDate=$handoffDate remainingMs=$remainingMs"
            )
        }

        MarketOpenScheduler.scheduleNextMarketOpen(this)
        Log.i(TAG, "POST_CLOSE_SHUTDOWN: stopping watch service after market close")
        stopPolling()
        return true
    }

    private suspend fun performPoll(token: String) {
        if (!pollInProgress.compareAndSet(false, true)) {
            Log.w(TAG, "POLL_SKIPPED_OVERLAP: poll already running")
            LogBuffer.add('W', TAG, "POLL_SKIPPED_OVERLAP: poll already running")
            return
        }
        try {
            val slotKey = currentPollSlotKey()
            // M0 research collector: persist the scheduled identity + attempt BEFORE any request (collection only).
            m0Attempts = m0BeginAttempts(slotKey)
            val pollCount = slotKey?.let { slotOrdinalFromSlotKey(it) } ?: reservePollNumberForToday()
            Log.d(TAG, "POLL_START: performPoll() entered")
            LogBuffer.add('I', "MarketWatchService", "Poll #$pollCount starting${slotKey?.let { " slot=$it" } ?: ""}")

            val (bnfExpiry, nfExpiry) = refreshActiveExpiries(token)
            val today = todayIstDate()
            if (!isUsableExpiry(bnfExpiry, today)) {
                invalidateExpiryAuthority("poll_invalid_live_expiry", bnfExpiry, nfExpiry)
                Log.w(TAG, "POLL_SKIP_INVALID_EXPIRY: bnf='$bnfExpiry' nf='$nfExpiry' session=$today")
                LogBuffer.add('E', TAG, "POLL_SKIP_INVALID_EXPIRY: bnf='$bnfExpiry' nf='$nfExpiry' session=$today")
                NotificationHelper.send(
                    this,
                    "⚠️ Expiry refresh failed",
                    "Poll skipped. Live expiry authority is unavailable; open the app after connectivity stabilizes.",
                    "info"
                )
                return
            }

            // Expiry Rollover check
            val sdfUTC = SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = TimeZone.getTimeZone("Asia/Kolkata") }
            val todayCheck = sdfUTC.format(Date())

            if (bnfExpiry < todayCheck) {
                Log.w(TAG, "POLL_SKIP: Expiry passed: $bnfExpiry < $todayCheck")
                NotificationHelper.send(this, "⚠️ Expiry passed", "Poll skipped. Open app to refresh expiry dates.", "info")
                return
            }

            updateForegroundNotification("Polling Market", "Fetching Quotes...")

            // A8: Parallelize independent HTTP calls
            val kotakKey = resolveKotakKey(token)
            val bnfStocks = getBnfStocks(kotakKey).joinToString(",") { it.requestKey }
            val quotesUrl = "https://api.upstox.com/v2/market-quote/quotes?instrument_key=NSE_INDEX|Nifty Bank,NSE_INDEX|India VIX"
            val bnfUrl = "https://api.upstox.com/v2/option/chain?instrument_key=NSE_INDEX|Nifty Bank&expiry_date=$bnfExpiry"
            val bnfStocksUrl = "https://api.upstox.com/v2/market-quote/quotes?instrument_key=$bnfStocks"

            // Evidence batch 1 (Item C): device-local clock around the index quotes fetch only.
            var quotesFetchStartedAtMs = 0L
            var quotesFetchCompletedAtMs = 0L
            // b510 X1: per-index chain receipt times for THIS poll only (0 = fetch failed).
            x1NfChainFetchedAtMs = 0L
            x1BnfChainFetchedAtMs = 0L
            var m0BnfFetchStartMs = 0L
            var m0NfFetchStartMs = 0L
            // Capture-owned request-completion clocks, set on SUCCESS AND FAILURE.
            //
            // Deliberately separate from x1*ChainFetchedAtMs, whose success-only semantics other
            // code depends on and which are NOT changed here. Without these, a failed chain
            // reached capture with no clock and no slot, so the six per-contract
            // MISSING_REQUEST_FAILED records could never be written — the gap capture exists to
            // record was itself unrecordable. This is a completion time, not a vendor quote time.
            var capBnfCompletedAtMs = 0L
            val acquisition = MarketSnapshotIdentity.acquire(today) { coroutineScope {
                val quotes = async(Dispatchers.IO) {
                    quotesFetchStartedAtMs = System.currentTimeMillis()
                    fetchSync(quotesUrl, token).also { quotesFetchCompletedAtMs = System.currentTimeMillis() }
                }
                val chain = async(Dispatchers.IO) {
                    m0BnfFetchStartMs = System.currentTimeMillis()
                    fetchSync(bnfUrl, token).also {
                        capBnfCompletedAtMs = System.currentTimeMillis()
                        if (it != null) x1BnfChainFetchedAtMs = capBnfCompletedAtMs
                    }
                }
                val stocks = async(Dispatchers.IO) { fetchSync(bnfStocksUrl, token) }
                Triple(quotes.await(), chain.await(), stocks.await())
            } }
            val (quotesJson, chainResponse, stocksResponse) = acquisition.value
            // Independent daily full-expiry evidence pass. It never supplies the brain or Paper path.
            scheduleBnfUniverseCapture(today, token)
            val nf50Breadth = JSONObject()
            val bnfChainJson = chainResponse ?: JSONObject().put("data", JSONArray()).put("input_failure", "CHAIN_FETCH_FAILED")
            val bnfStocksJson = stocksResponse ?: JSONObject().put("data", JSONObject()).put("input_failure", "BNF_BREADTH_FETCH_FAILED")
            val nfChainJson = JSONObject().put("data", JSONArray()).put("retired", BnfScope.RETIRED)

            // Premium evidence capture, DEFAULT OFF. Placed deliberately here: immediately after
            // the already-fetched chain results are available, and BEFORE every early return
            // below.
            //
            // Two aborts follow — the quotes failure directly below, and the chain/stocks failure
            // further down — and BOTH fire on conditions unrelated to whether a chain succeeded.
            // The existing m0RecordCaptures hook sits after both, so M0 already loses good chains
            // to an unrelated failure. Capture must not repeat that: a quotes or breadth failure
            // with two good chains in hand would otherwise discard both.
            //
            // It makes no request of its own, reuses each chain exactly once, hands persistence to
            // a bounded off-poll worker, is wrapped so a capture failure cannot fail or stall this
            // poll, and returns a status only. Nothing it writes is read by admission, ranking,
            // sizing, exits, notifications or the Paper/Real lock. The live poll's own failure
            // decisions below are unchanged.
            premiumCaptureSafely {
                val status = PremiumCaptureCoordinator.onChainsAcquired(
                    filesDir, today,
                    PremiumCapturePollAdapter.declaredSlotFromSlotKey(slotKey),
                    PremiumCapturePollAdapter.acquisitions(listOf(
                        PremiumCapturePollAdapter.IndexResult(
                            index = "BNF", chain = chainResponse,
                            requestStartedMs = m0BnfFetchStartMs,
                            completedAtMs = capBnfCompletedAtMs,
                            underlyingKey = "NSE_INDEX|Nifty Bank", expiry = bnfExpiry),

                    )))
                if (status.optString("status") != PremiumCaptureCoordinator.DISABLED)
                    LogBuffer.add('I', TAG, "PREMIUM_CAPTURE: ${status.optString("status")}" +
                        " prepare=${status.optLong("prepare_elapsed_ms")}ms" +
                        " indices=${status.optJSONArray("indices")}")
            }

            // Close-to-next-session research capture (close_open_capture_v1), DEFAULT OFF behind its
            // own control. A separate path from the premium pilot above: it reuses the same two
            // already-fetched chains, makes no request, schedules nothing, never touches the
            // pilot's window, store or counters, and hands its work to its own bounded worker.
            closeOpenCaptureSafely {
                val coStatus = CloseOpenCapture.onPoll(filesDir, today,
                    PremiumCapturePollAdapter.declaredSlotFromSlotKey(slotKey),
                    listOf(
                        CloseOpenCapture.Acquisition("BNF", chainResponse, m0BnfFetchStartMs, capBnfCompletedAtMs)))
                val coS = coStatus.optString("status")
                if (coS != "DISABLED" && coS != "OUTSIDE_WINDOWS")
                    LogBuffer.add('I', TAG, "CLOSE_OPEN_CAPTURE: $coS kind=${coStatus.optString("kind")}")
            }

            // Persist each successful raw chain before unrelated shared-quote or breadth returns.
            val (chainEvidenceRef, chainEvidenceCoverage) = persistFetchedChainEvidenceBeforeBrain(bnfChainJson, nfChainJson)
            m0RecordCaptures(mapOf(
                "BNF" to Triple(chainResponse, m0BnfFetchStartMs, x1BnfChainFetchedAtMs)
            ), chainEvidenceRef)
            if (quotesJson == null) {
                prefs.edit().putBoolean("paper_shared_inputs_unavailable", true).apply()
                lastAnalysisInputs = null
                Log.e(TAG, "POLL_FAIL: SHARED_QUOTES_FETCH_FAILED")
                return
            }

            Log.d(TAG, "A8_PARALLEL_FETCH: quotes=${quotesJson != null} bnfChain=${bnfChainJson != null} bnfStocks=${bnfStocksJson != null}")
            LogBuffer.add('D', TAG, "BREADTH_HTTP_URL: $bnfStocksUrl")

            if (bnfStocksJson != null) {
                try {
                    val breadthData = bnfStocksJson.optJSONObject("data")
                    val kotakPresent = breadthData?.let {
                        findQuoteByInstrument(it, kotakKey, "NSE_EQ:KOTAKBANK") != null
                    } ?: false
                    if (!kotakPresent) {
                        val retryKey = if (kotakKey == KOTAK_KEY_CURRENT) KOTAK_KEY_LEGACY else KOTAK_KEY_CURRENT
                        val kotakUrl = "https://api.upstox.com/v2/market-quote/quotes?instrument_key=$kotakKey,$retryKey"
                        val kotakJson = fetchSync(kotakUrl, token)
                        val kotakData = kotakJson?.optJSONObject("data")
                        if (kotakData != null && breadthData != null) {
                            val mergedKotak = kotakData.optJSONObject(kotakKey)
                                ?: kotakData.optJSONObject(kotakKey.replace("|", ":"))
                                ?: kotakData.optJSONObject(retryKey)
                                ?: kotakData.optJSONObject(retryKey.replace("|", ":"))
                            if (mergedKotak != null) {
                                breadthData.put(kotakKey, mergedKotak)
                                LogBuffer.add('I', TAG, "BREADTH_KOTAK_RETRY_MERGED: key=$kotakKey")
                            } else {
                                LogBuffer.add('W', TAG, "BREADTH_KOTAK_RETRY_EMPTY: key=$kotakKey")
                            }
                        } else {
                            LogBuffer.add('W', TAG, "BREADTH_KOTAK_RETRY_NULL: key=$kotakKey")
                        }
                    }
                } catch (e: Exception) {
                    LogBuffer.add('W', TAG, "BREADTH_KOTAK_RETRY_ERROR: ${e.message}")
                }

                val breadthData = bnfStocksJson.optJSONObject("data")
                val breadthKeys = mutableListOf<String>()
                val breadthKeyIter = breadthData?.keys()
                while (breadthKeyIter != null && breadthKeyIter.hasNext() && breadthKeys.size < 5) {
                    breadthKeys.add(breadthKeyIter.next())
                }
                val bodyPrefix = bnfStocksJson.toString().take(500)
                LogBuffer.add(
                    'D',
                    TAG,
                    "BREADTH_HTTP_BODY: status=${bnfStocksJson.optString("status")} errors=${bnfStocksJson.opt("errors")} dataKeys=${breadthData?.length() ?: 0} first=${breadthKeys.joinToString("|")} bodyPrefix=$bodyPrefix"
                )
            } else {
                LogBuffer.add('E', TAG, "BREADTH_HTTP_BODY: null response for breadth stocks")
            }


            // Evidence batch 1 (Item C): per-index spot provenance. Evidence only; it never changes the
            // spot values, the abort behaviour below, ranking, gates, sizing or exits. An unchanged LTP is
            // not called stale (index frozen by design during CAS 15:15-15:35); freshness is judged on the
            // provider feed timestamp, and each clock is stored under its own name.
            val spotProvenance = try {
                val session = todayIstDate()
                SpotProvenance.build(
                    quotesJson.optJSONObject("data"),
                    quotesFetchStartedAtMs,
                    quotesFetchCompletedAtMs,
                    session,
                    lastSpotProvenance?.takeIf { it.optString("session_date") == session },
                    indices = BnfScope.indices
                ).also { prov ->
                    lastSpotProvenance = prov
                    LogBuffer.add(
                        if (prov.optBoolean("all_identities_present", false)) 'I' else 'E',
                        TAG,
                        "SPOT_PROVENANCE: phase=${prov.optString("session_phase")} BNF=${prov.optJSONObject("BNF")?.optString("status")}/${prov.optJSONObject("BNF")?.optString("freshness")}"
                    )
                }
            } catch (e: Exception) {
                LogBuffer.add('W', TAG, "SPOT_PROVENANCE_FAIL: ${e.message}")
                null
            }

            val data = quotesJson.optJSONObject("data")
            val missingShared = listOf("NSE_INDEX:Nifty Bank", "NSE_INDEX:India VIX")
                .filter { key -> val value = data?.optJSONObject(key)?.optDouble("last_price", Double.NaN)
                    value == null || !value.isFinite() || value <= 0.0 }
            if (data == null || missingShared.isNotEmpty()) {
                prefs.edit().putBoolean("paper_shared_inputs_unavailable", true).apply()
                lastAnalysisInputs = null
                LogBuffer.add('E', TAG, "POLL_SHARED_QUOTES_UNAVAILABLE: ${missingShared.joinToString()}")
                return
            }
            val bnfSpot = data.getJSONObject("NSE_INDEX:Nifty Bank").getDouble("last_price")
            val nfSpot = 0.0 // compatibility argument; never published as a live NF quote
            val vix = data.getJSONObject("NSE_INDEX:India VIX").getDouble("last_price")
            Log.d(TAG, "POLL_DATA_RECEIVED: BNF=$bnfSpot VIX=$vix")

            // Step 5: Build poll object and save
            Log.d(TAG, "POLL_STEP5: Saving poll object")
            val pollObj = parsePollData(quotesJson, bnfChainJson, nfChainJson, bnfStocksJson, bnfSpot)
            pollObj.put("poll_number", pollCount)
            if (!slotKey.isNullOrBlank()) pollObj.put("slot_key", slotKey)
            savePoll(pollObj, pollCount, slotKey)
            LogBuffer.add('I', "MarketWatchService", "Poll #$pollCount complete${slotKey?.let { " slot=$it" } ?: ""}, candidates=${pollObj.optJSONArray("candidates")?.length() ?: 0}")

            // b493: raw chain evidence belongs to the market-data fetch, not to
            // Brain success. Persist it synchronously before analyze() so a slow,
            // timed-out, or failed Python call cannot erase the 15:15-15:40 marks
            // required by the post-close teacher. The database write is
            // idempotent on (poll_ts,index_key,strike,option_type).

            // D9 repair: on an index's OWN expiry day the front (trading) chain cannot price a forward
            // session move, which removed that index's direction for the entire session (NF, 3 Nov 2026:
            // 77 of 78 polls SCALE_EXPIRY_DAY_CHAIN_ONLY). Fetch the next expiry's chain SEPARATELY for
            // the direction scale only.
            //
            // Placed here deliberately: AFTER persistFetchedChainEvidenceBeforeBrain, so this extra
            // request cannot delay the pre-Brain persistence of trading-chain evidence that the b493
            // gate exists to protect; and OUTSIDE the parallel acquisition above, so the trading chains,
            // the poll object and the market-snapshot identity stay byte-identical to before. It makes
            // no request at all on an ordinary session.
            val scaleChains = mutableMapOf<String, ScaleChainFetch>()
            fetchDirectionScaleChain("BNF", "NSE_INDEX|Nifty Bank", token, today, bnfExpiry)
                ?.let { scaleChains["BNF"] = it }

            // Step 6: Run Python Brain (nf50Breadth already fetched in parallel above)
            Log.d(TAG, "POLL_STEP6: Launching brain analysis")
            // b507 R1 (F2/F4): the poll's analysis owns the single analysis lane; on release the authority drains
            // any deferred recalculation for the newest acknowledged inputs.
            analysisAuthority.withPollLane { runBrainAnalysis(
                pollObj,
                bnfChainJson,
                nfChainJson,
                bnfSpot,
                nfSpot,
                vix,
                bnfStocksJson,
                nf50Breadth,
                bnfExpiry,
                nfExpiry,
                spotProvenance,
                chainEvidenceRef,
                chainEvidenceCoverage,
                marketSnapshot = acquisition.identity,
                scaleChains = scaleChains
            ) }
            val hbTs = System.currentTimeMillis()
            prefs.edit().putLong(LEASE_HEARTBEAT_MS_KEY, hbTs).commit()
            LogBuffer.add('D', TAG, "LEASE_HEARTBEAT_WRITTEN: pollNum=$pollCount ts=$hbTs")
        } finally {
            // M0: an attempt that ended before its capture was recorded (fetch failure, early return) is closed
            // against its ORIGINAL slot here, so it is never left looking like a live attempt.
            // Open positions use their own exact-contract quotes, including when analysis failed.
            try {
                IndexAuthorityIo.monitorSingleLeg(filesDir, "{}",
                    ManagedReadAdapters.HttpTransport { (prefs.getString("auth_token", "") ?: "").trim() }) { out ->
                    val events = JSONObject(out).optJSONArray("events") ?: JSONArray()
                    for (i in 0 until events.length()) {
                        val event = events.getJSONObject(i)
                        val account = event.optJSONObject("account")
                        val id = event.optString("position_id")
                        val status = event.optString("event")
                        val dedupe = "single_leg_exit_notice_$id"
                        if (prefs.getString(dedupe, "") != status) {
                            val delivery = NotificationHelper.send(this, "Paper exit: ${account?.optString("index") ?: event.optString("index")}",
                                if (status == "EXIT") "Closed simulated position $id · ${account?.optString("exit_reason")} · net ₹${account?.opt("net_pnl") ?: "see Trade"}"
                                else "Exit pending for $id · ${event.optString("reason")} · ${event.optString("pending")}. Open Trade to review.",
                                "urgent", "trade")
                            LogBuffer.add('I', TAG, "SINGLE_LEG_EXIT_NOTIFICATION: position=$id status=$status delivery=${delivery.outcome}")
                            if (delivery.postedToOs) prefs.edit().putString(dedupe, status).apply()
                        }
                    }
                }
            } catch (e: Exception) {
                LogBuffer.add('W', TAG, "SINGLE_LEG_MONITOR_ENQUEUE_FAIL: ${e.javaClass.simpleName}")
            }
            m0CloseOpenAttempts()
            pollInProgress.set(false)
        }
    }

    // ---------------------------------------------------------------- M0 research collector (collection only)
    @Volatile private var m0Attempts: Map<String, ResearchCollector.Begin.Started> = emptyMap()
    private fun m0Dir(): File = File(filesDir, ResearchCollector.DIR)
    /**
     * Premium capture can never fail a poll. Catches `Throwable`, not `Exception`, because an
     * `OutOfMemoryError` from a large chain snapshot is an `Error` and would otherwise escape.
     * Mirrors `m0Safely` below; kept separate so the two capture paths cannot mask each other.
     */
    private inline fun premiumCaptureSafely(block: () -> Unit) {
        try { block() } catch (t: Throwable) {
            LogBuffer.add('W', TAG, "PREMIUM_CAPTURE_FAIL: ${t.javaClass.simpleName}")
        }
    }

    /** IST HH:MM for a device-local epoch stamp, or null when the stamp is not a real time. */
    private fun istHhmmOrNull(ms: Long): String? {
        if (ms <= 0L) return null
        val cal = Calendar.getInstance(TimeZone.getTimeZone("Asia/Kolkata"))
            .apply { timeInMillis = ms }
        return "%02d:%02d".format(cal.get(Calendar.HOUR_OF_DAY), cal.get(Calendar.MINUTE))
    }

    /** Close-open research capture can never fail a poll; kept apart from premiumCaptureSafely. */
    private inline fun closeOpenCaptureSafely(block: () -> Unit) {
        try { block() } catch (t: Throwable) {
            LogBuffer.add('W', TAG, "CLOSE_OPEN_CAPTURE_FAIL: ${t.javaClass.simpleName}")
        }
    }

    private inline fun <T> m0Safely(where: String, fallback: T, block: () -> T): T = try { block() } catch (t: Throwable) {
        ResearchCollector.recordFailure(m0Dir(), where, t)
        LogBuffer.add('W', TAG, "M0_COLLECTOR_FAIL[$where]: ${t.javaClass.simpleName}")
        fallback
    }

    private fun m0BeginAttempts(slotKey: String?): Map<String, ResearchCollector.Begin.Started> = m0Safely("begin", emptyMap()) {
        val dir = m0Dir()
        if (slotKey.isNullOrBlank() || !ResearchCollector.isEnabled(dir)) return@m0Safely emptyMap()
        val now = System.currentTimeMillis()
        BnfScope.indices.mapNotNull { idx ->
            (ResearchCollector.beginAttempt(dir, idx, slotKey, now) as? ResearchCollector.Begin.Started)?.let { idx to it }
        }.toMap()
    }

    private fun m0RecordCaptures(chains: Map<String, Triple<JSONObject?, Long, Long>>, chainRef: JSONObject?) {
        val attempts = m0Attempts
        if (attempts.isEmpty()) return
        m0Safely("capture", Unit) {
            val dir = m0Dir()
            val now = System.currentTimeMillis()
            val table = chainRef?.takeIf { it.has("persist_table") && !it.isNull("persist_table") }?.optString("persist_table")
            val pollTs = chainRef?.optString("poll_ts")
            for ((idx, att) in attempts) {
                val (chain, start, end) = chains[idx] ?: continue
                val cap = ResearchCollector.captureFromChain(dir, idx, chain, start.takeIf { it > 0 }, end.takeIf { it > 0 },
                    table, now, pollTs, now, leaseId = att.attemptId)
                ResearchCollector.recordCapture(dir, att.slotId, att.attemptId, cap, now)
            }
        }
    }

    private fun m0CloseOpenAttempts() {
        val attempts = m0Attempts
        m0Attempts = emptyMap()
        if (attempts.isEmpty()) return
        m0Safely("close", Unit) {
            val now = System.currentTimeMillis()
            for ((_, att) in attempts) ResearchCollector.closeIfOpen(m0Dir(), att.slotId, att.attemptId, now)
            // W4: bounded runtime point for reconcile + protection lag (at most once per 30 min, after the poll).
            ResearchCollector.maybeReconcile(m0Dir(), now)
        }
    }

    private fun m0CaptureEnvelope(poll: JSONObject, raw: JSONObject?, startedMs: Long, completedMs: Long?,
                                  ctxObj: JSONObject?, failure: String?) {
        m0Safely("envelope", Unit) {
            val dir = m0Dir()
            val slot = poll.optString("slot_key", "")
            if (slot.isBlank() || !ResearchCollector.isEnabled(dir)) return@m0Safely
            val policy = JSONObject().put("execution_mode", ctxObj?.optString("executionMode", "") ?: "")
                .put("trade_mode", ctxObj?.optString("tradeMode", "") ?: "")
            val env = ResearchCollector.buildEnvelope(slot, raw, startedMs.takeIf { it > 0 }, completedMs,
                BuildConfig.VERSION_NAME, BuildConfig.VERSION_CODE, policy, failure)
            ResearchCollector.putEnvelope(dir, env, System.currentTimeMillis())
        }
    }


    private fun minutesSince(oldT: String, newT: String): Int {
        try {
            val sdf = SimpleDateFormat("HH:mm", Locale.getDefault())
            val d1 = sdf.parse(oldT)
            val d2 = sdf.parse(newT)
            return ((d2.time - d1.time) / (1000 * 60)).toInt()
        } catch (e: Exception) { return 99 }
    }

    private fun optionLtp(md: JSONObject?): Double = OptionChainBrainFormatter.optionLtp(md)

    private fun optionQuote(md: JSONObject?, key: String): Double? = OptionChainBrainFormatter.optionQuote(md, key)

    private fun optionMid(md: JSONObject?): Double = OptionChainBrainFormatter.optionMid(md)

    private fun optionIv(option: JSONObject?, md: JSONObject?): Double = OptionChainBrainFormatter.optionIv(option, md)

    private fun findQuoteByInstrument(data: JSONObject, requestKey: String, responseKey: String? = null): JSONObject? {
        if (!responseKey.isNullOrBlank()) data.optJSONObject(responseKey)?.let { return it }
        data.optJSONObject(requestKey)?.let { return it }
        data.optJSONObject(requestKey.replace("|", ":"))?.let { return it }
        val requestedSymbol = extractSymbolFromInstrumentKey(responseKey ?: requestKey)
        val keys = data.keys()
        while (keys.hasNext()) {
            val key = keys.next()
            val quote = data.optJSONObject(key)
            if (quote?.optString("instrument_token") == requestKey) return quote
            if (!requestedSymbol.isNullOrBlank()) {
                val keySymbol = extractSymbolFromInstrumentKey(key)
                if (keySymbol.equals(requestedSymbol, ignoreCase = true)) return quote
                val tradingSymbol = quote?.optString("trading_symbol", "") ?: ""
                if (tradingSymbol.equals(requestedSymbol, ignoreCase = true)) return quote
                val symbol = quote?.optString("symbol", "") ?: ""
                if (symbol.equals(requestedSymbol, ignoreCase = true)) return quote
            }
        }
        return null
    }

    private fun extractSymbolFromInstrumentKey(key: String?): String {
        if (key.isNullOrBlank()) return ""
        val normalized = key.trim()
        val delimiter = if (normalized.contains(":")) ":" else "|"
        return normalized.substringAfterLast(delimiter, "")
    }

    private fun quoteLtp(quote: JSONObject?): Double {
        if (quote == null) return 0.0
        val directLast = quote.optDouble("last_price", 0.0)
        if (directLast > 0.0) return directLast
        val directLtp = quote.optDouble("ltp", 0.0)
        if (directLtp > 0.0) return directLtp
        val ltpc = quote.optJSONObject("ltpc")
        val ltpcLtp = ltpc?.optDouble("ltp", 0.0) ?: 0.0
        if (ltpcLtp > 0.0) return ltpcLtp
        return 0.0
    }

    private fun quoteClose(quote: JSONObject?): Double {
        if (quote == null) return 0.0
        val ohlcClose = quote.optJSONObject("ohlc")?.optDouble("close", 0.0) ?: 0.0
        if (ohlcClose > 0.0) return ohlcClose
        val prevOhlcClose = quote.optJSONObject("prev_ohlc")?.optDouble("close", 0.0) ?: 0.0
        if (prevOhlcClose > 0.0) return prevOhlcClose
        val closePrice = quote.optDouble("close_price", 0.0)
        if (closePrice > 0.0) return closePrice
        val ltpcCp = quote.optJSONObject("ltpc")?.optDouble("cp", 0.0) ?: 0.0
        if (ltpcCp > 0.0) return ltpcCp
        return 0.0
    }

    private fun quotePrevCloseForBreadth(quote: JSONObject?): Double {
        if (quote == null) return 0.0
        val ltp = quoteLtp(quote)
        val netChange = quote.optDouble("net_change", Double.MIN_VALUE)
        if (ltp > 0.0 && netChange != Double.MIN_VALUE) {
            val derived = ltp - netChange
            if (derived > 0.0) return derived
        }
        val prevOhlcClose = quote.optJSONObject("prev_ohlc")?.optDouble("close", 0.0) ?: 0.0
        if (prevOhlcClose > 0.0) return prevOhlcClose
        return quoteClose(quote)
    }

    private fun extractLtpMap(chainJson: JSONObject): JSONObject {
        val map = JSONObject()
        val data = chainJson.optJSONArray("data") ?: return map
        for (i in 0 until data.length()) {
            val item = data.getJSONObject(i)
            val strike = item.optDouble("strike_price").toString()
            val pair = JSONObject()
            pair.put("CE", optionLtp(item.optJSONObject("call_options")?.optJSONObject("market_data")))
            pair.put("PE", optionLtp(item.optJSONObject("put_options")?.optJSONObject("market_data")))
            map.put(strike, pair)
        }
        return map
    }



    private fun parsePollData(quotes: JSONObject, bnfChain: JSONObject, nfChain: JSONObject, stocks: JSONObject, spot: Double): JSONObject {
        val data = quotes.getJSONObject("data")
        val sData = stocks.getJSONObject("data")
        val bnf = data.getJSONObject("NSE_INDEX:Nifty Bank").getDouble("last_price")
        val nf = 0.0 // retired input; stripped from the newly built poll
        val vix = data.getJSONObject("NSE_INDEX:India VIX").getDouble("last_price")
        
        val time = SimpleDateFormat("HH:mm", Locale.getDefault()).apply { timeZone = TimeZone.getTimeZone("Asia/Kolkata") }.format(Date())
        
        var maxCallOi = 0.0
        var maxPutOi = 0.0
        var cw = 0.0
        var pw = 0.0
        
        // Near-ATM PCR (±10 strikes)
        var nearAtmCOI = 0.0
        var nearAtmPOI = 0.0
        
        // Straddle Logic (ATM premiums)
        var atmCE = 0.0
        var atmPE = 0.0
        var nfAtmCE = 0.0
        var nfAtmPE = 0.0
        var atmDist = Double.MAX_VALUE
        var atmStrike = 0.0
        
        val chainData = bnfChain.optJSONArray("data") ?: JSONArray()
        for (i in 0 until chainData.length()) {
            val item = chainData.getJSONObject(i)
            val strikePrice = item.getDouble("strike_price")
            
            val callMd = item.optJSONObject("call_options")?.optJSONObject("market_data")
            val putMd = item.optJSONObject("put_options")?.optJSONObject("market_data")
            
            val coi = callMd?.optDouble("oi", 0.0) ?: 0.0
            val poi = putMd?.optDouble("oi", 0.0) ?: 0.0
            
            // Wall logic
            if (coi > maxCallOi) { maxCallOi = coi; cw = strikePrice }
            if (poi > maxPutOi) { maxPutOi = poi; pw = strikePrice }
            
            // Near-ATM Logic
            val gapS = bnf * (vix / 100.0) / Math.sqrt(252.0)
            if (gapS > 0 && Math.abs(strikePrice - bnf) <= gapS * 1.5) { // C6 partial: use sigma-based near-atm
                nearAtmCOI += coi
                nearAtmPOI += poi
            }
            
            // Straddle Logic
            val dist = Math.abs(strikePrice - bnf)
            if (dist < atmDist) {
                atmDist = dist
                atmStrike = strikePrice
                
                atmCE = optionMid(callMd)
                atmPE = optionMid(putMd)
            }
        }

        val nfChainData = nfChain.optJSONArray("data") ?: JSONArray()
        var nfAtmDist = Double.MAX_VALUE
        for (i in 0 until nfChainData.length()) {
            val item = nfChainData.optJSONObject(i) ?: continue
            val strikePrice = item.optDouble("strike_price", 0.0)
            val dist = Math.abs(strikePrice - nf)
            if (dist < nfAtmDist) {
                nfAtmDist = dist
                val callMd = item.optJSONObject("call_options")?.optJSONObject("market_data")
                val putMd = item.optJSONObject("put_options")?.optJSONObject("market_data")
                nfAtmCE = optionMid(callMd)
                nfAtmPE = optionMid(putMd)
            }
        }

        val poll = JSONObject()
        poll.put("date", todayIstDate())
        poll.put("t", time)
        poll.put("bnf", bnf)
        poll.put("nf", nf)
        poll.put("bnfSpot", bnf)
        poll.put("nfSpot", nf)
        poll.put("vix", vix)
        // Sigma Logic (Fix C2: correct daily sigma sqrt(252))
        val dailySigma = bnf * (vix / 100.0) / Math.sqrt(252.0)
        poll.put("dailySigma", dailySigma)
        poll.put("daily_sigma", dailySigma)
        
        // Phase E.1 — spotSigma / vixSigma wiring (port-first from app.js L5540-5552)
        // Required by runBrainAnalysis significantMove gate. Without these two fields
        // populated here, ctx.significant_move is always false and evaluate_alerts
        // skips WATCHLIST/POSITION/MARKET branches.
        val baselineStr = prefs.getString("morning_baseline", null)
        var spotSigma = 0.0
        var nfSpotSigma = 0.0
        var vixSigmaValue = 0.0
        if (baselineStr != null) {
            try {
                val baseline = JSONObject(baselineStr)
                val baselineSpot = baseline.optDouble("bnfSpot", 0.0)
                val baselineNfSpot = baseline.optDouble("nfSpot", 0.0)
                val baselineVix = baseline.optDouble("vix", 0.0)
                if (baselineSpot > 0 && baselineVix > 0) {
                    val spotDailySigma = baselineSpot * (baselineVix / 100.0) / Math.sqrt(252.0)
                    if (spotDailySigma > 0) {
                        spotSigma = (bnf - baselineSpot) / spotDailySigma
                    }
                    val nfDailySigma = baselineNfSpot * (baselineVix / 100.0) / Math.sqrt(252.0)
                    if (baselineNfSpot > 0 && nfDailySigma > 0) {
                        nfSpotSigma = (nf - baselineNfSpot) / nfDailySigma
                    }
                    val vixDailySigma = baselineVix * 0.10
                    if (vixDailySigma > 0) {
                        vixSigmaValue = (vix - baselineVix) / vixDailySigma
                    }
                }
            } catch (e: Exception) {
                Log.w(TAG, "spotSigma/vixSigma compute failed: ${e.message}")
            }
        }
        poll.put("spotSigma", spotSigma)
        poll.put("nfSpotSigma", nfSpotSigma)
        poll.put("vixSigma", vixSigmaValue)
        
        poll.put("cw", cw)
        poll.put("pw", pw)
        poll.put("cwOI", maxCallOi)
        poll.put("pwOI", maxPutOi)
        // Batch 3: OI velocity wiring for brain.py oi_velocity() and ML snapshots
        val bnfTotalCallOi = bnfChain.optDouble("totalCallOI", maxCallOi)
        val bnfTotalPutOi = bnfChain.optDouble("totalPutOI", maxPutOi)
        val nfTotalCallOi = nfChain.optDouble("totalCallOI", 0.0)
        val nfTotalPutOi = nfChain.optDouble("totalPutOI", 0.0)
        poll.put("bnfCOI", bnfTotalCallOi)
        poll.put("bnfPOI", bnfTotalPutOi)
        poll.put("nfCOI", nfTotalCallOi)
        poll.put("nfPOI", nfTotalPutOi)
        poll.put("bnfCallWall", cw)
        poll.put("bnfPutWall", pw)
        poll.put("bnfCallWallOI", maxCallOi)
        poll.put("bnfPutWallOI", maxPutOi)
        poll.put("straddle", atmCE + atmPE)
        poll.put("pcr", if (nearAtmCOI > 0) nearAtmPOI / nearAtmCOI else 1.0) // brain expects PE/CE
        poll.put("nearAtmPCR", poll.optDouble("pcr", 1.0))
        
        // Futures Premium
        val bnfFutKey = getFuturesKey("BANKNIFTY")
        Log.d(TAG, "FP_DEBUG: bnfFutKey=$bnfFutKey")
        var bnfFutLtp = findQuoteByInstrument(sData, bnfFutKey)?.optDouble("last_price", 0.0) ?: 0.0
        
        if (bnfFutLtp > 0) {
            Log.d(TAG, "FP_SOURCE: actual ($bnfFutLtp)")
        } else {
            // Synthetic Fallback: ATM_CE - ATM_PE + Spot
            bnfFutLtp = atmCE - atmPE + bnf
            Log.d(TAG, "FP_SOURCE: synthetic ($bnfFutLtp)")
        }
        
        val futuresPremium = if (bnf > 0.0) (bnfFutLtp - bnf) / bnf * 100.0 else 0.0
        poll.put("fp", futuresPremium)
        poll.put("futuresPremBnf", futuresPremium)

        val nfFutKey = getFuturesKey("NF")
        Log.d(TAG, "FP_DEBUG: nfFutKey=$nfFutKey")
        var nfFutLtp = findQuoteByInstrument(sData, nfFutKey)?.optDouble("last_price", 0.0) ?: 0.0
        if (nfFutLtp > 0) {
            Log.d(TAG, "FP_SOURCE_NF: actual ($nfFutLtp)")
        } else {
            nfFutLtp = nfAtmCE - nfAtmPE + nf
            Log.d(TAG, "FP_SOURCE_NF: synthetic ($nfFutLtp)")
        }
        val nfFuturesPremium = if (nf > 0.0) (nfFutLtp - nf) / nf * 100.0 else 0.0
        poll.put("futuresPremNf", nfFuturesPremium)
        
        // A5: Extract ATM IV from chain data
        fun extractAtmIv(chain: JSONObject, spot: Double): Double {
            val data = chain.optJSONArray("data") ?: return 0.0
            var minDist = Double.MAX_VALUE
            var atmCeIv = 0.0
            var atmPeIv = 0.0
            for (i in 0 until data.length()) {
                val item = data.getJSONObject(i)
                val strike = item.optDouble("strike_price", 0.0)
                val dist = Math.abs(strike - spot)
                if (dist < minDist) {
                    minDist = dist
                    val callOpt = item.optJSONObject("call_options")
                    val putOpt = item.optJSONObject("put_options")
                    val call = callOpt?.optJSONObject("market_data")
                    val put = putOpt?.optJSONObject("market_data")
                    atmCeIv = optionIv(callOpt, call)
                    atmPeIv = optionIv(putOpt, put)
                }
            }
            return if (atmCeIv > 0 && atmPeIv > 0) (atmCeIv + atmPeIv) / 2.0 else Math.max(atmCeIv, atmPeIv)
        }
        poll.put("bnfAtmIv", extractAtmIv(bnfChain, bnf))
        poll.put("nfAtmIv", extractAtmIv(nfChain, nf))

        // A6: Missing poll fields (dayRange, weekday, dayHigh/Low, prevClose, change)
        val bnfQuote = data.optJSONObject("NSE_INDEX:Nifty Bank")
        val nfQuote = data.optJSONObject("NSE_INDEX:Nifty 50")
        val bnfOhlc = bnfQuote?.optJSONObject("ohlc")
        val nfOhlc = nfQuote?.optJSONObject("ohlc")
        val bnfPrevClose = bnfOhlc?.optDouble("close", 0.0) ?: 0.0
        val nfPrevClose = nfOhlc?.optDouble("close", 0.0) ?: 0.0
        val bnfOpen = bnfOhlc?.optDouble("open", 0.0) ?: 0.0
        val overnightGapSigma = if (bnfPrevClose > 0.0 && bnfOpen > 0.0) {
            (((bnfOpen - bnfPrevClose) / bnfPrevClose) * 100.0) / 0.5
        } else {
            0.0
        }
        poll.put("gapSigma", overnightGapSigma)
        poll.put("gap_sigma", overnightGapSigma)
        poll.put("bnfPrevClose", bnfPrevClose)
        poll.put("nfPrevClose", nfPrevClose)
        poll.put("bnfChange", if (bnfPrevClose > 0) bnf - bnfPrevClose else 0.0)
        poll.put("nfChange", if (nfPrevClose > 0) nf - nfPrevClose else 0.0)
        poll.put("bnfHigh", bnfQuote?.optDouble("high", 0.0) ?: 0.0)
        poll.put("bnfLow", bnfQuote?.optDouble("low", 0.0) ?: 0.0)
        poll.put("nfHigh", nfQuote?.optDouble("high", 0.0) ?: 0.0)
        poll.put("nfLow", nfQuote?.optDouble("low", 0.0) ?: 0.0)
        val bnfDayRange = poll.optDouble("bnfHigh", 0.0) - poll.optDouble("bnfLow", 0.0)
        val nfDayRange = poll.optDouble("nfHigh", 0.0) - poll.optDouble("nfLow", 0.0)
        poll.put("bnfDayRange", bnfDayRange)
        poll.put("nfDayRange", nfDayRange)
        val moveSigma = if (dailySigma > 0.0 && bnfPrevClose > 0.0) (bnf - bnfPrevClose) / dailySigma else spotSigma
        val dayRangeSigma = if (dailySigma > 0.0) bnfDayRange / dailySigma else 0.0
        val dayDirection = when {
            moveSigma > 0.25 -> "UP"
            moveSigma < -0.25 -> "DOWN"
            else -> "FLAT"
        }
        var consecDays = 1
        try {
            val history = JSONArray(prefs.getString("poll_history", "[]") ?: "[]")
            val prev = if (history.length() > 0) history.optJSONObject(history.length() - 1) else null
            val prevDirection = if (prev != null) prev.optString("dayDirection", prev.optString("day_direction", "")) else ""
            val prevConsec = if (prev != null) prev.optInt("consecDays", prev.optInt("consec_days", 0)) else 0
            consecDays = if (dayDirection != "FLAT" && dayDirection == prevDirection) prevConsec + 1 else 1
        } catch (_: Exception) {
            consecDays = 1
        }
        poll.put("moveSigma", moveSigma)
        poll.put("move_sigma", moveSigma)
        poll.put("dayRangeSigma", dayRangeSigma)
        poll.put("day_range_sigma", dayRangeSigma)
        poll.put("dayDirection", dayDirection)
        poll.put("day_direction", dayDirection)
        poll.put("consecDays", consecDays)
        poll.put("consec_days", consecDays)
        val cal = Calendar.getInstance(TimeZone.getTimeZone("Asia/Kolkata"))
        poll.put("weekday", (cal.get(Calendar.DAY_OF_WEEK) + 5) % 7)
        poll.put("hour", cal.get(Calendar.HOUR_OF_DAY))
        poll.put("minute", cal.get(Calendar.MINUTE))

        Log.d("AUDIT_POLL", poll.toString())
        return BnfScope.stripNiftyFields(poll)
    }

    private fun savePoll(poll: JSONObject, assignedPollNumber: Int, slotKey: String?) {
        synchronized(pollSaveLock) {
            val ist = TimeZone.getTimeZone("Asia/Kolkata")
            val today = SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = ist }.format(Date())
            val lastPollDate = prefs.getString("last_poll_date", "") ?: ""

            var history = JSONArray(prefs.getString("poll_history", "[]"))
            var pollCount = prefs.getInt("poll_count", 0)

            // A1+A5: Daily Reset
            if (lastPollDate != today) {
                Log.i(TAG, "DAILY_RESET: New trading day detected ($today). Resetting history.")
                history = JSONArray()
                pollCount = 0
                prefs.edit()
                    .remove(LAST_POLL_DISPATCH_SLOT_KEY)
                    .remove(LAST_SUCCESSFUL_POLL_SLOT_KEY)
                    .remove(LAST_ASSIGNED_POLL_DATE_KEY)
                    .remove(LAST_ASSIGNED_POLL_NUMBER_KEY)
                    .remove(COVERAGE_INTEGRITY_DATE_KEY)
                    .remove(COVERAGE_INTEGRITY_KEY)
                    .remove(COVERAGE_INTEGRITY_ISSUE_KEY)
                    .apply()
            }

            // Append new poll
            history.put(poll)
            pollCount = maxOf(pollCount, assignedPollNumber)

            // Keep last 100 for memory
            if (history.length() > 100) {
                val trimmed = JSONArray()
                for (i in (history.length() - 100) until history.length()) {
                    trimmed.put(history.get(i))
                }
                history = trimmed
            }

            prefs.edit().apply {
                putString("last_poll_date", today)
                putString("latest_poll", poll.toString())
                putString("poll_history", history.toString())
                putString("last_poll_time", poll.getString("t"))
                putInt("poll_count", pollCount)
                putString(LAST_ASSIGNED_POLL_DATE_KEY, today)
                putInt(LAST_ASSIGNED_POLL_NUMBER_KEY, maxOf(assignedPollNumber, pollCount))
                putLong("last_successful_poll_ms", System.currentTimeMillis())
                remove(LAST_POLL_GAP_WARNING_SLOT_KEY)
                (slotKey ?: currentPollSlotKey(System.currentTimeMillis()))?.let { putString(LAST_SUCCESSFUL_POLL_SLOT_KEY, it) }
            }.commit()
            persistSessionIntegritySummary(computeSessionIntegrity(today))

            // Daily session reset (formerly inside checkInstitutionalPositioning).
            // Preserves hasDayEvalRun rollover; retired afternoon positioning capture removed.
            ensureDailySessionReset()

            // GAP 6: Upsert to Supabase every 3rd poll
            if (pollCount > 0 && pollCount % 3 == 0) {
                serviceScope.launch(Dispatchers.IO) {
                    SupabaseClient.upsertPollHistory(todayIstDate(), history)
                }
            }

            updateForegroundNotification(
                "Watching Market",
                "BNF: ${poll.getDouble("bnf")} | NF: ${poll.getDouble("nf")} | VIX: ${poll.getDouble("vix")}"
            )
        }
    }

    /**
     * Session-date rollover previously embedded in checkInstitutionalPositioning().
     * Clears hasDayEvalRun so post-close evaluation can run again, and scrubs
     * retired afternoon-positioning preference keys.
     */
    private fun ensureDailySessionReset() {
        val today = todayIstDate()
        val lastSavedDate = prefs.getString("positioning_date", "")
        if (lastSavedDate != today) {
            prefs.edit().apply {
                remove("afternoon_baseline")
                remove("tomorrow_signal")
                remove("yesterday_signal")
                remove("has2pmSnapshot")
                remove("has315pmSnapshot")
                remove("snap_2pm_today")
                remove("hasDayEvalRun")
                putString("positioning_date", today)
            }.apply()
            sanitizeRetiredPositioningState()
            Log.d(TAG, "DAILY_SESSION_RESET: cleared eval latch and retired positioning prefs for $today")
        }
    }

    /** Strip retired afternoon-positioning keys from prefs and persisted brain context. */
    private fun sanitizeRetiredPositioningState() {
        prefs.edit().apply {
            remove("has2pmSnapshot")
            remove("has315pmSnapshot")
            remove("snap_2pm_today")
            remove("tomorrow_signal")
            remove("yesterday_signal")
            remove("afternoon_baseline")
        }.apply()
        try {
            val raw = prefs.getString("context", null) ?: return
            val ctx = JSONObject(raw)
            val retired = listOf(
                "yesterdaySignal", "signalAccuracy", "snap_2pm_today",
                "positioning", "tomorrow_signal", "signalValidation",
                "chain_snapshot_now", "positioning_candidates", "positioning_bias"
            )
            var changed = false
            for (key in retired) {
                if (ctx.has(key)) {
                    ctx.remove(key)
                    changed = true
                }
            }
            if (changed) {
                prefs.edit().putString("context", ctx.toString()).apply()
                Log.d(TAG, "RETIRED_POSITIONING_CTX_SCRUB: removed stale context keys")
            }
        } catch (e: Exception) {
            Log.w(TAG, "RETIRED_POSITIONING_CTX_SCRUB_FAIL: ${e.message}")
        }
    }


    private fun calculateBreadth(stocks: JSONObject): JSONObject {
        val result = JSONObject()
        try {
            val data = stocks.getJSONObject("data")
            val quoteKeys = mutableListOf<String>()
            val keyIter = data.keys()
            while (keyIter.hasNext() && quoteKeys.size < 5) {
                quoteKeys.add(keyIter.next())
            }
            LogBuffer.add('D', TAG, "BREADTH_KEYS: first=${quoteKeys.joinToString("|")}")
            val bnfStocks = getBnfStocks()
            val configuredWeight = bnfStocks.sumOf { it.weight }
            var coveredWeight = 0.0
            var advancingWeight = 0.0
            var decliningWeight = 0.0
            var neutralWeight = 0.0
            var advancementScoreRaw = 0.0
            var weightedPctRaw = 0.0
            var advancing = 0
            var declining = 0
            var neutral = 0
            var considered = 0
            val results = JSONArray()
            
            for (stock in bnfStocks) {
                val stockData = findQuoteByInstrument(data, stock.requestKey, stock.responseKey)
                val ltp = quoteLtp(stockData)
                val close = quotePrevCloseForBreadth(stockData)
                if (ltp <= 0.0 || close <= 0.0) {
                    LogBuffer.add(
                        'W',
                        TAG,
                        "BREADTH_STOCK_MISSING: symbol=${stock.symbol} req=${stock.requestKey} hit=${stockData != null} ltp=$ltp close=$close"
                    )
                    continue
                }

                val change = if (close > 0) (ltp - close) else 0.0
                val pctChange = if (close > 0) (change / close * 100.0) else 0.0
                LogBuffer.add(
                    'D',
                    TAG,
                    "BREADTH_STOCK: symbol=${stock.symbol} req=${stock.requestKey} lookup=${stock.responseKey} hit=${stockData != null} ltp=$ltp close=$close pct=${Math.round(pctChange * 100.0) / 100.0}"
                )
                considered++
                coveredWeight += stock.weight
                
                if (ltp > close) {
                    advancing++
                    advancingWeight += stock.weight
                    advancementScoreRaw += stock.weight * 100.0
                } else if (ltp < close) {
                    declining++
                    decliningWeight += stock.weight
                    advancementScoreRaw += stock.weight * 0.0
                } else {
                    neutral++
                    neutralWeight += stock.weight
                    advancementScoreRaw += stock.weight * 50.0
                }

                weightedPctRaw += stock.weight * pctChange
                results.put(JSONObject()
                    .put("name", stock.symbol)
                    .put("change", Math.round(change * 100.0) / 100.0)
                    .put("pctChange", Math.round(pctChange * 100.0) / 100.0))
            }

            val normDivisor = if (coveredWeight > 0.0) coveredWeight else 1.0
            val pctNormalized = if (coveredWeight > 0.0) advancementScoreRaw / normDivisor else 50.0
            val weightedPctNormalized = if (coveredWeight > 0.0) weightedPctRaw / normDivisor else 0.0
            val coveragePct = if (configuredWeight > 0.0) (coveredWeight / configuredWeight) * 100.0 else 0.0

            result.put("pct", Math.round(pctNormalized * 10.0) / 10.0)
            result.put("weightedPct", Math.round(weightedPctNormalized * 100.0) / 100.0)
            result.put("pctRaw", Math.round(advancementScoreRaw * 10.0) / 10.0)
            result.put("weightedPctRaw", Math.round(weightedPctRaw * 100.0) / 100.0)
            result.put("coverageWeight", Math.round(coveredWeight * 1000.0) / 1000.0)
            result.put("configuredWeight", Math.round(configuredWeight * 1000.0) / 1000.0)
            result.put("coveragePct", Math.round(coveragePct * 10.0) / 10.0)
            result.put("advancing", advancing)
            result.put("declining", declining)
            result.put("neutral", neutral)
            result.put("considered", considered)
            result.put("advancingWeight", Math.round(advancingWeight * 1000.0) / 1000.0)
            result.put("decliningWeight", Math.round(decliningWeight * 1000.0) / 1000.0)
            result.put("neutralWeight", Math.round(neutralWeight * 1000.0) / 1000.0)
            result.put("results", results)
            LogBuffer.add(
                'D',
                TAG,
                "BREADTH_RESULT: pct=${result.optDouble("pct")} weighted=${result.optDouble("weightedPct")} " +
                    "coverage=${result.optDouble("coverageWeight")}/${result.optDouble("configuredWeight")} " +
                    "adv=$advancing dec=$declining neu=$neutral considered=$considered"
            )
        } catch (e: Exception) {
            Log.e(TAG, "Breadth calc failed: ${e.message}")
            LogBuffer.add('E', TAG, "BREADTH_ERROR: ${e.message}")
            result.put("pct", 50.0)
                .put("weightedPct", 0.0)
                .put("advancing", 0)
                .put("declining", 0)
                .put("neutral", 0)
                .put("considered", 0)
                .put("coverageWeight", 0.0)
                .put("configuredWeight", getBnfStocks().sumOf { it.weight })
                .put("coveragePct", 0.0)
                .put("results", JSONArray())
        }
        return result
    }

    /**
     * Fetches prior-session daily VIX closes and stores them for the brain.
     * On a failed request the previous history is kept; brain.py fails closed
     * if it is too old. Must run off the main thread.
     */
    private fun refreshVixDailyHistory(today: String) {
        val history = SupabaseClient.getVixDailyCloses(today)
        if (history == null) {
            val kept = try {
                JSONArray(prefs.getString(VixDailyHistory.PREF_KEY, "[]") ?: "[]").length()
            } catch (_: Exception) { 0 }
            LogBuffer.add('W', TAG, "VIX_DAILY_HISTORY_UNAVAILABLE: kept=$kept previous closes; brain fails closed if they are stale")
            return
        }
        prefs.edit().putString(VixDailyHistory.PREF_KEY, history.toString()).apply()
        val oldest = history.optJSONObject(0)?.optString("date", "") ?: ""
        val newest = history.optJSONObject(history.length() - 1)?.optString("date", "") ?: ""
        val line = "VIX_DAILY_HISTORY_LOADED: closes=${history.length()} oldest=$oldest newest=$newest"
        Log.i(TAG, line)
        LogBuffer.add('I', TAG, line)
    }

    private fun calculateIvPercentile(vix: Double, correctedPaper: Boolean = true): Int {
        // Legacy display/recording field. 50 is the pre-existing neutral value
        // when there is no usable history; the brain never treats this field
        // as regime authority (it carries no support count).
        return try {
            if (!correctedPaper) {
                val legacy = JSONArray(prefs.getString("premium_history", "[]") ?: "[]")
                if (legacy.length() < 10) return 50
                var lower = 0
                for (i in 0 until legacy.length()) {
                    if (vix > legacy.getJSONObject(i).optDouble("vix", 0.0)) lower++
                }
                return lower * 100 / legacy.length()
            }
            val hist = JSONArray(prefs.getString(VixDailyHistory.PREF_KEY, "[]") ?: "[]")
            VixDailyHistory.ivPercentile(vix, hist, todayIstDate()) ?: 50
        } catch (_: Exception) { 50 }
    }

    private fun extractFiiHistory(): JSONArray {
        val uniqueDays = LinkedHashMap<String, JSONObject>()
        try {
            val hist = JSONArray(prefs.getString("premium_history", "[]") ?: "[]")
            for (i in 0 until hist.length()) {
                val row = hist.getJSONObject(i)
                val date = row.optString("date", "")
                if (date.isEmpty()) continue
                
                if (!uniqueDays.containsKey(date)) {
                    val entry = JSONObject()
                    entry.put("fiiCash", row.optDouble("fii_cash", 0.0))
                    entry.put("fiiShort", row.optDouble("fii_short_pct", 0.0))
                    entry.put("vix", row.optDouble("vix", 0.0))
                    entry.put("date", date)
                    uniqueDays[date] = entry
                }
                if (uniqueDays.size >= 60) break
            }
        } catch (e: Exception) {
            Log.e(TAG, "extractFiiHistory error: ${e.message}")
        }
        
        val result = JSONArray()
        uniqueDays.values.forEach { result.put(it) }
        return result
    }

    private fun computeIndexGap(ohlc: JSONObject, pipeKey: String, colonKey: String): JSONObject {
        val gap = JSONObject()
        try {
            val data = ohlc.getJSONObject("data")
            val indexOhlc = (data.optJSONObject(colonKey) ?: data.optJSONObject(pipeKey))
                ?.getJSONObject("ohlc") ?: throw IllegalStateException("OHLC missing for $colonKey")
            val prevClose = indexOhlc.getDouble("close")
            val todayOpen = indexOhlc.getDouble("open")
            val points = todayOpen - prevClose
            
            val pct = points / prevClose * 100.0
            val sigma = pct / 0.5 // Simplified: 1 sigma = 0.5%
            
            gap.put("type", if (pct > 0.3) "GAP_UP" else if (pct < -0.3) "GAP_DOWN" else "FLAT")
            gap.put("points", Math.round(points))
            gap.put("pct", Math.round(pct * 100.0) / 100.0)
            gap.put("sigma", Math.round(sigma * 100.0) / 100.0)
            gap.put("open", todayOpen)
            gap.put("prevClose", prevClose)
        } catch (e: Exception) {
            gap.put("type", "FLAT").put("points", 0).put("pct", 0.0).put("sigma", 0.0)
        }
        return gap
    }

    /**
     * expiry_fallback_v1 (REVIEW DRAFT r1): only when the analysis result names an index whose own-expiry-day
     * policy BUY needs later-expiry evidence. Read-only broker requests through [ExpiryFallbackAcquirer]; the
     * Python planner re-plans at its own clock (the selection cutoff) and the result is patched in place.
     * Any failure leaves the analysis plan (already a named WAIT: EXPIRY_FALLBACK_EVIDENCE_PENDING).
     * R4 (Codex R3 reply): returns the attempt when it is APPLY_ACCEPTED (patch applied in memory, not yet
     * acknowledged); the caller decides it at the final publication gate ([ExpiryFallbackPublication.publishThroughGate])
     * or fences it when the analysis ends first ([ExpiryFallbackPublication.fenceUnfinished]). Otherwise null.
     */
    private fun applyExpiryFallback(brain: com.chaquo.python.PyObject, resultObj: JSONObject, ctxObj: JSONObject,
                                    openTradesJson: String): ExpiryFallbackPublication.Attempt? {
        var accepted: ExpiryFallbackPublication.Attempt? = null
        try {
            val needs = ExpiryFallbackAcquirer.needs(resultObj)
            if (needs.isEmpty()) { ExpiryFallbackAcquirer.markWait(resultObj, "NOT_ACQUIRED"); return null }
            val tok = (prefs.getString("auth_token", "") ?: "").trim()
            if (tok.isEmpty()) {
                ExpiryFallbackAcquirer.markWait(resultObj, "NO_TOKEN")
                LogBuffer.add('W', TAG, "EXPIRY_FALLBACK_SKIP: reason=NO_TOKEN")
                return null
            }
            val clocks = ManagedReadAdapters.Clocks({ System.currentTimeMillis() }, { android.os.SystemClock.elapsedRealtime() })
            val sess = IndexAuthorityContext.istSession(System.currentTimeMillis())
            val slDir = IndexAuthorityContext.singleLegDir(filesDir)
            val ev = ExpiryFallbackAcquirer.acquire(needs, slDir, sess,
                { cap -> ManagedReadAdapters.HttpTransport(cap) { tok } }, clocks,
                { ix, listing, cutoff ->
                    PyTimeout.callWithTimeout(PY_EXPIRY_FALLBACK_RESOLVE_KEY, PY_EXPIRY_FALLBACK_RESOLVE_TIMEOUT_MS) {
                        brain.callAttr("single_leg_expiry_fallback_resolve", ix, sess, listing, cutoff).toString()
                    } ?: "{\"expiry\":null,\"reason\":\"RESOLVE_TIMEOUT\"}"
                },
                { raw, spot -> formatChainForBrain(raw, spot) })
            val minCtx = JSONObject()
            for (k in listOf("capital", "singleLegPaperEnabled", "executionMode", "indexSelector", "nfExpiry",
                    "bnfExpiry", "nfDTE", "bnfDTE", "session_date")) {
                if (ctxObj.has(k)) minCtx.put(k, ctxObj.get(k))
            }
            // planning capital source (Codex R1 reply s3.5); admission separately requires configured capital
            minCtx.put("capitalSource", if (prefs.contains("capital")) "CONFIGURED" else "APP_DEFAULT")
            // Python refuses to retain or return plans after this instant (no late publication after a timeout).
            // R3 (Codex R2 reply s3): the apply is one attempt (id + persisted generation); native accepts and
            // publishes it only inside the deadline and only while it is the current attempt; every decision
            // (PUBLISHED / TIMED_OUT / CANCELLED) is reconciled into the retained outcome; a late completion is never
            // applied and never promotes a decided attempt.
            val applyDeadline = System.currentTimeMillis() + ExpiryFallbackAcquirer.APPLY_TIMEOUT_MS
            val resultStr = resultObj.toString()          // serialised before the worker starts (no shared mutation)
            val decision = ExpiryFallbackPublication.applyAndPublish(resultObj, expiryFallbackPublication,
                PY_EXPIRY_FALLBACK_TIMEOUT_KEY, ExpiryFallbackAcquirer.APPLY_TIMEOUT_MS, applyDeadline,
                { System.currentTimeMillis() },
                { attempt ->
                    brain.callAttr("single_leg_expiry_fallback_apply", slDir.absolutePath, resultStr,
                        minCtx.toString(), ev.toString(), null, openTradesJson, applyDeadline,
                        attempt.toString()).toString()
                },
                { attempt, state ->
                    // R4: only apply-step decisions arrive here (TIMED_OUT / CANCELLED); APPLY_ACCEPTED is not sent
                    queueExpiryFallbackReconcile(slDir, ExpiryFallbackPublication.Decision(attempt, state, null, null,
                        System.currentTimeMillis()))
                })
            if (decision.state != ExpiryFallbackPublication.State.APPLY_ACCEPTED) {
                LogBuffer.add('W', TAG, "EXPIRY_FALLBACK_FAIL: reason=${decision.state} gen=${decision.attempt.generation}")
                return null
            }
            accepted = decision.attempt
            val patch = decision.patch ?: JSONObject()
            val n = patch.optJSONObject("single_leg_by_index")?.length() ?: 0
            LogBuffer.add('I', TAG, "EXPIRY_FALLBACK: applied=$n ${ExpiryFallbackAcquirer.logLine(ev, patch)}")
        } catch (e: Exception) {
            ExpiryFallbackAcquirer.markWait(resultObj, "APPLY_ERROR")
            LogBuffer.add('W', TAG, "EXPIRY_FALLBACK_FAIL: ${e.javaClass.simpleName}")
        }
        return accepted
    }

    /** R3/R4: publication decisions go to Python in order on one background thread, never blocking a poll. Only
     *  terminal native decisions are sent (PUBLISHED / NOT_PUBLISHED with reason / TIMED_OUT / CANCELLED). */
    private fun queueExpiryFallbackReconcile(slDir: java.io.File, d: ExpiryFallbackPublication.Decision) {
        val st = d.state
        if (st != ExpiryFallbackPublication.State.PUBLISHED && st != ExpiryFallbackPublication.State.NOT_PUBLISHED &&
            st != ExpiryFallbackPublication.State.TIMED_OUT && st != ExpiryFallbackPublication.State.CANCELLED) return
        val now = d.decidedMs ?: System.currentTimeMillis()
        expiryFallbackReconcileQueue.execute {
            try {
                val brain = Python.getInstance().getModule("brain")
                brain.callAttr("single_leg_expiry_fallback_reconcile", slDir.absolutePath, d.attempt.id,
                    d.attempt.generation, st.name, now, d.reason)
            } catch (e: Exception) {
                LogBuffer.add('W', TAG, "EXPIRY_FALLBACK_RECONCILE_FAIL: ${e.javaClass.simpleName}")
            }
        }
    }

    private fun formatChainForBrain(chain: JSONObject, spot: Double): JSONObject =
        OptionChainBrainFormatter.format(chain, spot) { Log.e(TAG, it) }

    /**
     * D9 repair: the chain the DIRECTION SCALE reads for one index, used only when the front
     * (trading) expiry is the session date itself. `chain` is null when the fetch failed, which the
     * brain treats as fail-closed rather than as a usable scale.
     */
    private data class ScaleChainFetch(val expiry: String, val chain: JSONObject?)

    private data class AnalysisInputs(val marketSnapshot: MarketSnapshotIdentity, val poll: JSONObject, val bnfChain: JSONObject,
        val nfChain: JSONObject, val bnfSpot: Double, val nfSpot: Double, val vix: Double, val stocks: JSONObject,
        val nf50Breadth: JSONObject, val bnfExpiry: String, val nfExpiry: String, val spotProvenance: JSONObject?,
        val chainEvidenceRef: JSONObject?, val chainEvidenceCoverage: JSONObject?,
        val scaleChains: Map<String, ScaleChainFetch> = emptyMap())

    @Volatile private var lastAnalysisInputs: AnalysisInputs? = null
    @Volatile private var lastAnalysisStrikeOiJson: String? = null
    private val analysisAuthority = AnalysisAuthority.shared.also { auth ->
        auth.attachRevisionSource {
            SessionInputs.inputRevision(SessionInputs.read(prefs.getString(SessionInputs.PREF_KEY, null)), todayIstDate())
        }
        auth.refresher = { rev -> runAnalysisRefresh(rev) }
    }

    private fun writeRefreshState(state: String, detail: String) {
        prefs.edit().putString(PREF_ANALYSIS_REFRESH_STATE, JSONObject()
            .put("state", state).put("detail", detail).put("at_ms", System.currentTimeMillis()).toString()).apply()
    }

    /**
     * b507 R1 (F2): record the newest acknowledged revision as pending and drain it if the analysis lane is
     * free. A busy lane never consumes the request: its owner drains on release. Without a usable same-session
     * cached market snapshot the request stays pending (WAITING_FOR_POLL / MARKET_TOO_OLD) and the next poll
     * computes it; no forced poll is scheduled and no completed slot is re-run.
     */
    private fun requestAnalysisRefresh(reason: String) {
        val rev = analysisAuthority.requestRefresh()
        writeRefreshState("PENDING", rev)
        LogBuffer.add('I', TAG, "ANALYSIS_REFRESH_REQUESTED: reason=$reason rev=$rev")
        serviceScope.launch {
            val outcome = analysisAuthority.drain()
            if (outcome == null) LogBuffer.add('I', TAG, "ANALYSIS_REFRESH_DEFERRED_LANE_BUSY: rev=$rev")
        }
    }

    /** Runs only while holding the analysis lane (called by AnalysisAuthority.drain). */
    private suspend fun runAnalysisRefresh(rev: String): AnalysisAuthority.Outcome {
        val cached = lastAnalysisInputs
        val nowMs = System.currentTimeMillis()
        if (cached != null && !cached.marketSnapshot.usableFor(todayIstDate(), nowMs)) {
            writeRefreshState("MARKET_IDENTITY_UNAVAILABLE", rev)
            return AnalysisAuthority.Outcome.MARKET_IDENTITY_UNAVAILABLE
        }
        AnalysisAuthority.refreshPrecondition(cached?.marketSnapshot?.session, cached?.marketSnapshot?.collectedAtMs ?: 0L, todayIstDate(), nowMs)?.let { blocked ->
            writeRefreshState(blocked.name, rev)
            LogBuffer.add('I', TAG, "ANALYSIS_REFRESH_$blocked: rev=$rev collectedAtMs=${cached?.marketSnapshot?.collectedAtMs ?: 0L}")
            return blocked
        }
        cached!!
        writeRefreshState("RUNNING", rev)
        LogBuffer.add('I', TAG, "ANALYSIS_REFRESH_START: rev=$rev")
        val outcome = runBrainAnalysis(cached.poll, cached.bnfChain, cached.nfChain, cached.bnfSpot, cached.nfSpot,
            cached.vix, cached.stocks, cached.nf50Breadth, cached.bnfExpiry, cached.nfExpiry,
            cached.spotProvenance, cached.chainEvidenceRef, cached.chainEvidenceCoverage,
            marketSnapshot = cached.marketSnapshot, mode = ANALYSIS_MODE_REFRESH,
            scaleChains = cached.scaleChains)
        writeRefreshState(if (outcome == AnalysisAuthority.Outcome.PUBLISHED) "DONE" else outcome.name, rev)
        LogBuffer.add(if (outcome == AnalysisAuthority.Outcome.PUBLISHED) 'I' else 'W', TAG, "ANALYSIS_REFRESH_OUTCOME: rev=$rev outcome=$outcome")
        return outcome
    }

    private suspend fun runBrainAnalysis(poll: JSONObject, bnfChain: JSONObject, nfChain: JSONObject,
                                         bnfSpot: Double, nfSpot: Double, vix: Double, stocksJson: JSONObject,
                                         nf50Breadth: JSONObject,
                                         liveBnfExpiry: String,
                                         liveNfExpiry: String,
                                         spotProvenance: JSONObject? = null,
                                         chainEvidenceRef: JSONObject? = null,
                                         chainEvidenceCoverage: JSONObject? = null,
                                         marketSnapshot: MarketSnapshotIdentity,
                                         mode: String = ANALYSIS_MODE_POLL,
                                         scaleChains: Map<String, ScaleChainFetch> = emptyMap()): AnalysisAuthority.Outcome {
        var outcome = AnalysisAuthority.Outcome.FAILED
        // b507 (condition 7): REFRESH recomputes the analysis for the latest acknowledged inputs from the
        // last poll's cached market inputs, with NO poll-owned side effects (no poll history, strike-OI
        // history, Paper menu capture, EV envelopes, notifications, BUILD3 rows, ML snapshots, position
        // thresholds/live P&L, routine-alert stamp or candle upserts). Only the result + broadcast.
        val isRefresh = mode == ANALYSIS_MODE_REFRESH
        if (!isRefresh) {
            lastAnalysisInputs = AnalysisInputs(marketSnapshot, poll, bnfChain, nfChain, bnfSpot, nfSpot, vix,
                stocksJson, nf50Breadth, liveBnfExpiry, liveNfExpiry, spotProvenance, chainEvidenceRef, chainEvidenceCoverage,
                scaleChains)
        }
        // b507 R1 (F4): market-data identity is the collection time of the analysed poll, preserved by a refresh.
        val marketCollectedAtMs = if (marketSnapshot.usableFor(todayIstDate(), System.currentTimeMillis())) marketSnapshot.collectedAtMs else 0L
        var brainSuccess = false
        var brainAnalyzeStartedAtMs = 0L
        // Evidence batch 1 (Item B): slot key of an in-flight Paper decision-menu capture attempt.
        var paperMenuAttemptSlot: String? = null
        // R4 (Codex R3 reply): an APPLY_ACCEPTED expiry-fallback attempt carried by this result until the final gate
        var efbAttempt: ExpiryFallbackPublication.Attempt? = null
        var efbAbort: String? = null

        try {
            val ctxObj = JSONObject(prefs.getString("context", "{}") ?: "{}")
            val resolvedTradeMode = resolveTradeMode(ctxObj)
            ctxObj.put("tradeMode", resolvedTradeMode)
            prefs.edit()
                .putString("trade_mode", resolvedTradeMode)
                .apply()
            val approvedBranchRows = try {
                refreshApprovedBranchProposals(force = false)
            } catch (e: Exception) {
                Log.e(TAG, "approved branch refresh failed: ${e.message}")
                getApprovedBranchProposalsCached()
            }
            ctxObj.put("approvedProposals", approvedBranchRows)
            
            // C1: Calculate DTE (Days to Expiry) for brain context
            val sessionDate = todayIstDate()
            val sdf = SimpleDateFormat("yyyy-MM-dd", Locale.US).apply {
                isLenient = false
                timeZone = TimeZone.getTimeZone("Asia/Kolkata")
            }
            val todayDate = sdf.parse(sessionDate)

            val resolvedBnfExpiry = liveBnfExpiry.trim()
            val resolvedNfExpiry = liveNfExpiry.trim()
            if (!isUsableExpiry(resolvedBnfExpiry, sessionDate)) {
                invalidateExpiryAuthority("brain_invalid_live_expiry", resolvedBnfExpiry, resolvedNfExpiry)
                Log.w(
                    TAG,
                    "BRAIN_SKIP_INVALID_EXPIRY: bnf='$resolvedBnfExpiry' nf='$resolvedNfExpiry' session=$sessionDate"
                )
                LogBuffer.add(
                    'E',
                    TAG,
                    "BRAIN_SKIP_INVALID_EXPIRY: bnf='$resolvedBnfExpiry' nf='$resolvedNfExpiry' session=$sessionDate"
                )
                return AnalysisAuthority.Outcome.FAILED
            }
            val bnfExpDate = parseExpiryDate(resolvedBnfExpiry)
            val nfExpDate: java.util.Date? = null

            fun writeDteContext(prefix: String, rawExpiry: String, expDate: java.util.Date?): Long? {
                val dte = expDate?.let { (it.time - todayDate.time) / (24 * 60 * 60 * 1000L) }
                val source = if (expDate != null && rawExpiry.isNotBlank()) "COMPUTED" else "PARSE_FAILED"
                val meta = JSONObject()
                    .put("source", source)
                    .put("raw_expiry", rawExpiry)
                    .put("session_date", sessionDate)
                ctxObj.put("${prefix}DteSource", source)
                ctxObj.put("${prefix}DteMeta", meta)
                ctxObj.put("${prefix}DTE", dte ?: JSONObject.NULL)
                if (dte == null) {
                    LogBuffer.add('W', TAG, "DTE_PARSE_FAILED: index=$prefix rawExpiry='$rawExpiry'")
                }
                return dte
            }

            val bnfDTE = writeDteContext("bnf", resolvedBnfExpiry, bnfExpDate)
            val nfDTE: Long? = null // retired; no active NF DTE state

            Log.d(TAG, "BRAIN_START: Loading Chaquopy brain module")
            val py = Python.getInstance()
            val brain = py.getModule("brain")
            
            val pollsJson    = prefs.getString("poll_history",      "[]") ?: "[]"
            val baselineJson = prefs.getString("morning_baseline",  "{}") ?: "{}"
            val openTradesJson   = prefs.getString("open_trades",   "[]") ?: "[]"
            val closedTradesJson = prefs.getString("closed_trades", "[]") ?: "[]"
            // Persist the exact trade state used by this poll so historical replay
            // can reconstruct the same calibration and risk context later.
            ctxObj.put("snapshot_open_trades_json", openTradesJson)
            ctxObj.put("snapshot_closed_trades_json", closedTradesJson)
            // Paper Brain valuation source: P1 accepted marks (LIVE_FULL only is
            // applied in brain.py). Never clears valid P1 marks; bridge is read-only.
            try {
                val p1MarksRaw = PositionMarkStore.presentationJson(prefs)
                ctxObj.put("p1_position_marks", JSONObject(p1MarksRaw))
                ctxObj.put("p1_position_marks_contract", "p1_paper_brain_valuation_v1_20260924")
            } catch (e: Exception) {
                LogBuffer.add('W', TAG, "P1_MARKS_CTX_FAIL: ${e.message}")
            }
            
            // CHAIN MERGING (Phase C: Format raw chains for Python)
            fun mergeChain(key: String, liveChainRaw: JSONObject, spot: Double,
                           cwPoll: Double, pwPoll: Double, pcrPoll: Double, expiry: String,
                           requestedUnderlyingKey: String) {
                // b510 R1: stamp the request this chain answered (same values as the option/chain URL).
                val formattedLive = OptionChainBrainFormatter.stampRequest(
                    formatChainForBrain(liveChainRaw, spot), expiry, requestedUnderlyingKey)
                // Delivery B (R5 s4): managed-only evidence in a SEPARATE ctx object; the X1 formatter output above
                // is untouched and the raw chain is only read.
                try { ManagedEvidenceFormatter.attachQuantities(ctxObj, liveChainRaw, System.currentTimeMillis()) }
                catch (e: Exception) { LogBuffer.add('W', TAG, "MANAGED_EVIDENCE_QTY_FAIL: ${e.javaClass.simpleName}") }
                val liveCw = if (cwPoll > 0.0) cwPoll else formattedLive.optDouble("callWallStrike", 0.0)
                val livePw = if (pwPoll > 0.0) pwPoll else formattedLive.optDouble("putWallStrike", 0.0)
                val livePcr = if (pcrPoll > 0.0) pcrPoll else formattedLive.optDouble("pcr", 0.0)
                // A failed acquisition must erase inherited chain context, never reuse an older poll.
                if (liveChainRaw.has("input_failure")) {
                    ctxObj.put(key, JSONObject().put("strikes", JSONObject())
                        .put("input_failure", liveChainRaw.getString("input_failure")))
                    return
                }
                val existingChain = ctxObj.optJSONObject(key)
                
                if (existingChain != null && existingChain.has("atm")) {
                    // Rich chain exists from WebView — refresh only live intraday fields
                    existingChain.put("strikes",         formattedLive.optJSONObject("strikes"))
                    existingChain.put("atm",             formattedLive.optDouble("atm", 0.0))
                    existingChain.put("callWallStrike",  liveCw)
                    existingChain.put("putWallStrike",   livePw)
                    existingChain.put("atmIv",           formattedLive.optDouble("atmIv", 0.0))
                    existingChain.put("expiry",          expiry)
                    existingChain.put("requested_underlying_key", requestedUnderlyingKey)
                    existingChain.put("requested_expiry", expiry)
                    val summaryKeys = listOf(
                        "allStrikes", "totalCallOI", "totalPutOI", "nearTotalCallOI", "nearTotalPutOI",
                        "nearAtmPCR", "maxPain", "callWallOI", "putWallOI"
                    )
                    for (summaryKey in summaryKeys) {
                        if (formattedLive.has(summaryKey)) existingChain.put(summaryKey, formattedLive.opt(summaryKey))
                    }
                    existingChain.put("pcr", livePcr)
                } else {
                    // No rich data — use full formatted live chain
                    formattedLive.put("callWallStrike", liveCw)
                    formattedLive.put("putWallStrike",  livePw)
                    formattedLive.put("pcr",            livePcr)
                    ctxObj.put(key, formattedLive)
                }
            }
            
            // Calculate NF walls
            var nfCw = 0.0; var nfPw = 0.0; var nfMaxC = 0.0; var nfMaxP = 0.0
            val nfData = nfChain.optJSONArray("data")
            if (nfData != null) {
                for (i in 0 until nfData.length()) {
                    val item = nfData.getJSONObject(i)
                    val s = item.optDouble("strike_price", 0.0)
                    val coi = item.optJSONObject("call_options")?.optJSONObject("market_data")?.optDouble("oi", 0.0) ?: 0.0
                    val poi = item.optJSONObject("put_options")?.optJSONObject("market_data")?.optDouble("oi", 0.0) ?: 0.0
                    if (coi > nfMaxC) { nfMaxC = coi; nfCw = s }
                    if (poi > nfMaxP) { nfMaxP = poi; nfPw = s }
                }
            }

            mergeChain("bnfChain", bnfChain, bnfSpot, poll.optDouble("cw", 0.0), poll.optDouble("pw", 0.0), poll.optDouble("pcr", 0.0), resolvedBnfExpiry,
                "NSE_INDEX|Nifty Bank")
            ctxObj.optJSONObject("bnfChain")?.put("bnf_spot", bnfSpot)
            ctxObj.remove("nfChain")

            // D9 repair: the direction scale's own chain, in a SEPARATE ctx object. Deliberately not
            // routed through mergeChain - it must not inherit WebView-enriched trading fields, attach
            // managed trade evidence, or overwrite any part of nfChain/bnfChain. The brain reads it
            // only for compute_scale, and only when the front expiry is the session date itself.
            //
            // ctxObj is loaded from SharedPreferences above and saved back below, so these keys survive
            // between polls and between SESSIONS. Clear all four FIRST: otherwise a poll whose fetch
            // failed would write a fresh expiry while an earlier session's chain stayed behind, pairing
            // a new expiry with a stale chain. After clearing, a published pair always comes from one
            // poll, or is not published at all.
            DirectionScaleNeed.clearCtx(ctxObj)
            for ((scaleIx, fetched) in scaleChains) {
                val prefix = if (scaleIx == "NF") "nf" else "bnf"
                val spot = if (scaleIx == "NF") nfSpot else bnfSpot
                val underlying = if (scaleIx == "NF") "NSE_INDEX|Nifty 50" else "NSE_INDEX|Nifty Bank"
                val raw = fetched.chain
                if (raw == null) {
                    // fetch failed: publish the expiry but no chain, so the brain fails closed with
                    // NEXT_EXPIRY_CHAIN_EMPTY rather than silently falling back to the trading chain
                    ctxObj.put("${prefix}ScaleExpiry", fetched.expiry)
                    LogBuffer.add('W', TAG, "SCALE_CHAIN_CTX_EMPTY: index=$scaleIx expiry=${fetched.expiry}")
                    continue
                }
                try {
                    val formatted = OptionChainBrainFormatter.stampRequest(
                        formatChainForBrain(raw, spot), fetched.expiry, underlying)
                    ctxObj.put("${prefix}ScaleChain", formatted)
                    ctxObj.put("${prefix}ScaleExpiry", fetched.expiry)
                    LogBuffer.add('I', TAG, "SCALE_CHAIN_CTX: index=$scaleIx expiry=${fetched.expiry} " +
                        "strikes=${formatted.optJSONObject("strikes")?.length() ?: 0}")
                } catch (e: Exception) {
                    // never let the scale path break the poll; the brain fails closed on a missing chain
                    ctxObj.put("${prefix}ScaleExpiry", fetched.expiry)
                    LogBuffer.add('W', TAG, "SCALE_CHAIN_CTX_FAIL: index=$scaleIx ${e.javaClass.simpleName}")
                }
            }

            // Phase C: Inject OHLC for profile calculations
            val ohlcStr = prefs.getString("yesterday_ohlc", null)
            if (ohlcStr != null) {
                try {
                    val ohlcData = JSONObject(ohlcStr).optJSONObject("data")
                    if (ohlcData != null) {
                        ctxObj.put("bnfOHLC", ohlcData.optJSONObject("NSE_INDEX|Nifty Bank"))
                        ctxObj.put("nfOHLC",  ohlcData.optJSONObject("NSE_INDEX|Nifty 50"))
                    }
                } catch (e: Exception) { Log.e(TAG, "OHLC parse fail: ${e.message}") }
            }

            // Data Parity Overlays
            val breadth = if (stocksJson.has("input_failure")) JSONObject()
                .put("status", "UNAVAILABLE").put("reason", stocksJson.getString("input_failure"))
                else calculateBreadth(stocksJson)
            ctxObj.put("bnfBreadth", breadth)
            ctxObj.put("nf50Breadth", nf50Breadth)
            ctxObj.put("pollInputStatus", JSONObject()
                .put("BNF", bnfChain.optString("input_failure", "AVAILABLE"))
                .put("bnf_breadth", stocksJson.optString("input_failure", "AVAILABLE"))
                .put("breadth_used_in_direction", false))
            
            // Phase B: populate morning input + yesterday history for brain.py
            // b507 (conditions 1-3): scrub every inherited session input from the persisted context and inject
            // only the current session's acknowledged records; capture the input revision at analysis start.
            val inputSession = todayIstDate()
            val inputState = SessionInputs.read(prefs.getString(SessionInputs.PREF_KEY, null))
            val startInputRevision = SessionInputs.inputRevision(inputState, inputSession)
            val morningInputStr = prefs.getString("morning_input", null)
            val morningInputObj = morningInputStr?.let { try { JSONObject(it) } catch (_: Exception) { null } }
            SessionInputs.assemble(ctxObj, inputState, inputSession, System.currentTimeMillis(), morningInputObj,
                SessionInputs.read(prefs.getString(SessionInputs.PREF_LIVE_KEY, null)))
            val inputStatusAtStart = SessionInputs.status(inputState, inputSession, System.currentTimeMillis())
            val ydayHistStr = prefs.getString("premium_history", "[]") ?: "[]"
            ctxObj.put("yesterdayHistory", JSONArray(ydayHistStr))
            
            // Retire afternoon positioning: do not fetch/inject signalAccuracy.
            val signalReliabilityRows = loadSignalReliabilityRows()
            ctxObj.put("signalReliability", signalReliabilityRows)
            ctxObj.put("elephantObserveOnly", true)
            
            // b507: eveningClose / globalDirection are no longer read from loose prefs; the overnight
            // comparison uses ctx.overnightInputs (one acknowledged pair) injected above.
            
            val gapObj = JSONObject()
            if (ohlcStr != null) {
                val ohlc = JSONObject(ohlcStr)
                val bnfGap = computeIndexGap(ohlc, "NSE_INDEX|Nifty Bank", "NSE_INDEX:Nifty Bank")
                val nfGap = computeIndexGap(ohlc, "NSE_INDEX|Nifty 50", "NSE_INDEX:Nifty 50")
                gapObj.put("bnf", bnfGap)
                gapObj.put("nf", nfGap)
                gapObj.put("type", bnfGap.optString("type", "FLAT"))
                gapObj.put("points", bnfGap.optInt("points", 0))
                gapObj.put("pct", bnfGap.optDouble("pct", 0.0))
                gapObj.put("sigma", bnfGap.optDouble("sigma", 0.0))
            } else {
                val flatGap = JSONObject().put("type", "FLAT").put("points", 0).put("pct", 0.0).put("sigma", 0.0)
                gapObj.put("bnf", flatGap)
                gapObj.put("nf", JSONObject(flatGap.toString()))
                gapObj.put("type", "FLAT").put("points", 0).put("pct", 0.0).put("sigma", 0.0)
            }
            ctxObj.put("gap", gapObj)
            
            // Retire afternoon positioning: do not inject yesterdaySignal into brain context.

            val sandboxEnabled = prefs.getBoolean("execution_sandbox_enabled", false)
            val orderProxyUrl = (prefs.getString("order_proxy_url", "") ?: "").trim()
            val derivedExecutionMode = ExecutionModeResolver.derive(
                prefs.getString("execution_mode", ""), sandboxEnabled, orderProxyUrl
            )

            // Always overlay history from SharedPreferences for brain.py (Persistence)
            val premHist = JSONArray(prefs.getString("premium_history", "[]") ?: "[]")
            // Dated prior-session VIX closes (oldest first). The undated
            // vixHistory list is kept for offline tooling only; with today_ist
            // set, the brain trusts nothing but these dated closes.
            val vixDailyHist = try {
                JSONArray(prefs.getString(VixDailyHistory.PREF_KEY, "[]") ?: "[]")
            } catch (_: Exception) { JSONArray() }
            val correctedPaper = derivedExecutionMode == "paper"
            val vixHist = if (correctedPaper) {
                VixDailyHistory.vixValues(vixDailyHist)
            } else {
                // Retain the pre-fix input in sandbox/live execution modes.
                JSONArray().apply {
                    for (i in 0 until premHist.length()) {
                        val value = premHist.getJSONObject(i).optDouble("vix", 0.0)
                        if (value > 0) put(value)
                    }
                }
            }
            if (premHist.length() > 0) ctxObj.put("premiumHistory", premHist)
            if (correctedPaper && vixDailyHist.length() > 0) {
                ctxObj.put("vixDailyHistory", vixDailyHist)
            } else {
                ctxObj.remove("vixDailyHistory")
            }
            if (vixHist.length() > 0) ctxObj.put("vixHistory", vixHist) else ctxObj.remove("vixHistory")
            val supplyQualityHistory = JSONArray(prefs.getString("pc2_supply_quality_history", "[]") ?: "[]")
            if (supplyQualityHistory.length() > 0) {
                ctxObj.put("pc2SupplyQualityHistory", supplyQualityHistory)
            }
            val percentileHistoryRows = countPercentileEnrichedHistoryRows(premHist)
            val latestHistoryDay = premHist.optJSONObject(0)?.optString("date")
                ?: premHist.optJSONObject(0)?.optString("session_date")
                ?: ""
            Log.d(
                TAG,
                "CTX_PERCENTILE_HISTORY_BRIDGE: premiumRows=${premHist.length()}, enrichedRows=$percentileHistoryRows, latestDay=$latestHistoryDay"
            )
            
            val fiiHist = extractFiiHistory()
            if (fiiHist.length() > 0) ctxObj.put("fiiHistory", fiiHist)
            
            ctxObj.put("ivPercentile", calculateIvPercentile(vix, correctedPaper))

            ctxObj.put("capital",    prefs.getInt("capital", 250000))
            ctxObj.put("bnfExpiry",  resolvedBnfExpiry)
            ctxObj.put("nfExpiry",   resolvedNfExpiry)
            ctxObj.put("vix",        vix)
            ctxObj.put("bnfSpot",    bnfSpot)
            ctxObj.put("nfSpot",     nfSpot)
            // Evidence batch 1 (Item C): provenance travels beside the unchanged spot values.
            if (spotProvenance != null) ctxObj.put("spot_provenance", spotProvenance) else ctxObj.remove("spot_provenance")
            if (chainEvidenceRef != null) ctxObj.put("chain_evidence_ref", chainEvidenceRef) else ctxObj.remove("chain_evidence_ref")
            // Per-poll only (not kept by the snapshot compactor): the keys a quote_ref may resolve to.
            if (chainEvidenceCoverage != null) ctxObj.put("chain_evidence_coverage", chainEvidenceCoverage) else ctxObj.remove("chain_evidence_coverage")
            val authToken = (prefs.getString("auth_token", "") ?: "").trim()
            val stage2aMode = when ((prefs.getString("stage2a_guard_mode", "paper") ?: "paper").trim().lowercase(Locale.US)) {
                "off" -> "off"
                "live" -> "live"
                "shadow" -> "shadow"
                else -> "paper"
            }
            val stage2aTeacherTablePath = File(filesDir, "teacher_table_stage2a.json").absolutePath
            ctxObj.put("authTokenReady", authToken.isNotEmpty())
            ctxObj.put("sandboxEnabled", sandboxEnabled)
            ctxObj.put("orderProxyUrl", orderProxyUrl)
            ctxObj.put("executionMode", derivedExecutionMode)
            ctxObj.put("stage2a_mode", stage2aMode)
            ctxObj.put("stage2a_min_prior_bucket_n", 5)
            ctxObj.put("stage2a_teacher_table_path", stage2aTeacherTablePath)
            ctxObj.put("live", JSONObject().apply {
                put("bnfSpot", bnfSpot)
                put("nfSpot", nfSpot)
                put("vix", vix)
                put("spotSigma", poll.optDouble("spotSigma", 0.0))
                put("nfSpotSigma", poll.optDouble("nfSpotSigma", 0.0))
                put("vixSigma", poll.optDouble("vixSigma", 0.0))
            })
            ctxObj.put("bnfLtpMap",  extractLtpMap(bnfChain))
            ctxObj.put("nfLtpMap",   extractLtpMap(nfChain))

            // PHASE E: Populate context with new fields
            val ist = TimeZone.getTimeZone("Asia/Kolkata")
            val cal = Calendar.getInstance(ist)
            val minsSinceOpen = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE) - 555
            ctxObj.put("mins_since_open", minsSinceOpen)
            ctxObj.put("now_ms", System.currentTimeMillis())
            ctxObj.put("today_ist", todayIstDate())
            
            val lastRoutine = prefs.getLong("last_routine_dispatch_ms", 0L)
            ctxObj.put("last_routine_dispatch_ms", lastRoutine)
            
            // Phase E #27 — significantMove gate (port-first from app.js L5550-5552)
            val openTradesCount = org.json.JSONArray(prefs.getString("open_trades", "[]") ?: "[]").length()
            val sigmaThreshold = if (openTradesCount > 0) 1.0 else 1.5
            
            val bnfSpotSigma = poll.optDouble("spotSigma", 0.0)
            val nfSpotSigmaValue = poll.optDouble("nfSpotSigma", 0.0)
            val vixSigma = poll.optDouble("vixSigma", 0.0)
            
            val sigMove = Math.abs(bnfSpotSigma) > sigmaThreshold ||
                Math.abs(nfSpotSigmaValue) > sigmaThreshold ||
                Math.abs(vixSigma) > sigmaThreshold

            // b507: only a lock made for THIS session counts (legacy undated locks require re-entry).
            val hasMorningInput = ctxObj.optJSONObject("morning_input") != null
            val wallClockMinutes = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE)
            // Entry window matches cash-market hours (09:15–15:15 IST). Trading-window
            // telemetry keeps the historical 11:00 floor so ops charts stay comparable.
            val entryWindowActive = hasMorningInput && wallClockMinutes in 555..915
            if (!hasMorningInput && wallClockMinutes in 555..915) {
                val today = todayIstDate()
                val alreadyNotified = prefs.getString(PREF_MORNING_INPUT_BLOCK_NOTIFIED_DATE, "") == today
                if (!alreadyNotified) {
                    // Audible by design. This is the one notification whose whole
                    // job is to say that notifications are off for the session, and
                    // "info" routes to CHANNEL_ROUTINE — IMPORTANCE_LOW, no sound,
                    // no vibration — where the user would never notice it. A silent
                    // warning about silence is not a warning.
                    val delivery = NotificationHelper.send(
                        this@MarketWatchService,
                        "Entry alerts off",
                        "No entry alerts today — morning input is not set. " +
                            "Open the app and set it to turn them back on.",
                        "warning"
                    )
                    LogBuffer.add(
                        if (delivery.postedToOs) 'I' else 'W',
                        TAG,
                        "ENTRY_ALERTS_OFF: morning_input not set session=$today " +
                            "outcome=${delivery.outcome}"
                    )
                    // Stamp the session only once the OS actually accepted the
                    // notification. Same selected-vs-delivered rule the notification
                    // agent's acknowledge_delivery() already enforces: a denied
                    // permission, a disabled channel or the local throttle can
                    // suppress transport, and a session that never received the
                    // warning must be able to retry on the next poll instead of
                    // having silently consumed its single chance. commit(), not
                    // apply(), because the next poll reads this straight back.
                    if (delivery.postedToOs) {
                        prefs.edit()
                            .putString(PREF_MORNING_INPUT_BLOCK_NOTIFIED_DATE, today)
                            .commit()
                    }
                }
            }

            ctxObj.put("significant_move", sigMove)
            ctxObj.put("entry_window_active", entryWindowActive)
            ctxObj.put("trading_window_active", wallClockMinutes in 660..915)
            ctxObj.put("abs_spot_sigma", Math.abs(bnfSpotSigma))
            ctxObj.put("abs_nf_spot_sigma", Math.abs(nfSpotSigmaValue))
            ctxObj.put("abs_vix_sigma", Math.abs(vixSigma))

            // Retire afternoon positioning: do not inject snap_2pm_today.
            ctxObj.remove("yesterdaySignal")
            ctxObj.remove("signalAccuracy")
            ctxObj.remove("snap_2pm_today")
            ctxObj.remove("positioning")
            ctxObj.remove("tomorrow_signal")
            ctxObj.remove("signalValidation")
            ctxObj.remove("chain_snapshot_now")

            if (!ctxObj.has("ivPercentile")) ctxObj.put("ivPercentile", 50)
            
            Log.d("BRAIN_CTX_CHECK",
                "bnfChain.atm=${ctxObj.optJSONObject("bnfChain")?.opt("atm")}, " +
                "nfChain.atm=${ctxObj.optJSONObject("nfChain")?.opt("atm")}, " +
                "bnfProfile.maxPain=${ctxObj.optJSONObject("bnfProfile")?.opt("maxPain")}")
            Log.d("BRAIN_INPUT_SUMMARY",
                "polls=${JSONArray(pollsJson).length()}, " +
                "ivPercentile=${ctxObj.optDouble("ivPercentile")}, " +
                "vix=${ctxObj.optDouble("vix")}, " +
                "tradeMode=$resolvedTradeMode"
            )
            run {
                val bnfCtx = ctxObj.optJSONObject("bnfChain")
                val nfCtx = ctxObj.optJSONObject("nfChain")
                val bnfBreadth = ctxObj.optJSONObject("bnfBreadth")
                LogBuffer.add(
                    'D',
                    TAG,
                    "BRAIN_CTX: bnfStrikes=${bnfCtx?.optJSONObject("strikes")?.length() ?: -1} nfStrikes=${nfCtx?.optJSONObject("strikes")?.length() ?: -1} " +
                        "bnfAtm=${bnfCtx?.opt("atm")} nfAtm=${nfCtx?.opt("atm")} " +
                        "bnfExp=$resolvedBnfExpiry nfExp=$resolvedNfExpiry bnfDTE=$bnfDTE nfDTE=$nfDTE " +
                        "breadthPct=${bnfBreadth?.optDouble("pct")} breadthWeighted=${bnfBreadth?.optDouble("weightedPct")} " +
                        "breadthAdv=${bnfBreadth?.optInt("advancing")} breadthDec=${bnfBreadth?.optInt("declining")} " +
                        "nf50Pct=${nf50Breadth.optDouble("pct")} nf50Adv=${nf50Breadth.optInt("advancing")} " +
                        "tradeMode=$resolvedTradeMode"
                )
                LogBuffer.add('D', TAG, "BRAIN_CHAIN_SIZE: bnfBytes=${bnfChain.toString().length} nfBytes=${nfChain.toString().length}")
            }
            
            Log.d(TAG, "BRAIN_CALLING: analyze() with ${JSONArray(pollsJson).length()} polls")
            
            // Persistence: Must save merged context back to SharedPreferences for next poll cycle
            prefs.edit().putString("context", ctxObj.toString()).apply()

            // A4: Build per-trade strike OI history for open trade monitoring.
            // brain.py reads strike_oi_json by trade id and expects a short time series:
            // { trade_id: [{t, sellCOI, sellPOI, buyCOI, buyPOI}, ...] }
            fun buildStrikeOiJson(bnfChain: JSONObject, nfChain: JSONObject, openTradesJson: String, pollTime: String): String {
                val existing = try {
                    JSONObject(prefs.getString("strike_oi_history", "{}") ?: "{}")
                } catch (_: Exception) {
                    JSONObject()
                }
                val output = JSONObject()

                fun oiAt(chain: JSONObject, strike: Double): Pair<Double?, Double?> {
                    val data = chain.optJSONArray("data") ?: return Pair(null, null)
                    for (i in 0 until data.length()) {
                        val item = data.getJSONObject(i)
                        val chainStrike = item.optDouble("strike_price", 0.0)
                        if (Math.abs(chainStrike - strike) < 0.1) {
                            val callOi = item.optJSONObject("call_options")
                                ?.optJSONObject("market_data")
                                ?.optDouble("oi", 0.0)
                            val putOi = item.optJSONObject("put_options")
                                ?.optJSONObject("market_data")
                                ?.optDouble("oi", 0.0)
                            return Pair(callOi, putOi)
                        }
                    }
                    return Pair(null, null)
                }

                val openTrades = try { JSONArray(openTradesJson) } catch (_: Exception) { JSONArray() }
                for (i in 0 until openTrades.length()) {
                    val trade = openTrades.optJSONObject(i) ?: continue
                    val tradeId = trade.optString("id", "")
                    if (tradeId.isBlank()) continue

                    val indexKeyRaw = trade.optString("index_key", "").ifBlank { trade.optString("indexKey", "") }
                    val indexNorm = ContractLotTable.normalizeIndexKey(indexKeyRaw)
                    if (indexNorm == null) continue // fail-closed: never invent BNF
                    val chain = if (indexNorm == "NF") nfChain else bnfChain
                    val sellStrike = trade.optDouble("sell_strike", 0.0)
                    val buyStrike = trade.optDouble("buy_strike", 0.0)
                    val sellOi = if (sellStrike > 0.0) oiAt(chain, sellStrike) else Pair(null, null)
                    val buyOi = if (buyStrike > 0.0) oiAt(chain, buyStrike) else Pair(null, null)

                    val row = JSONObject()
                        .put("t", pollTime)
                        .put("sellCOI", sellOi.first)
                        .put("sellPOI", sellOi.second)
                        .put("buyCOI", buyOi.first)
                        .put("buyPOI", buyOi.second)

                    val history = existing.optJSONArray(tradeId) ?: JSONArray()
                    history.put(row)
                    val trimmed = JSONArray()
                    val start = Math.max(0, history.length() - 6)
                    for (j in start until history.length()) trimmed.put(history.getJSONObject(j))
                    existing.put(tradeId, trimmed)
                    output.put(tradeId, trimmed)
                }

                prefs.edit().putString("strike_oi_history", existing.toString()).apply()
                return output.toString()
            }
            val strikeOiJson = if (isRefresh) (lastAnalysisStrikeOiJson ?: "{}")
                else buildStrikeOiJson(bnfChain, nfChain, openTradesJson, poll.optString("t", "")).also { lastAnalysisStrikeOiJson = it }
            ctxObj.put("snapshot_strike_oi_json", strikeOiJson)

            // Call brain.analyze(poll_json, trades_json, baseline_json, open_trades_json, candidates_json, strike_oi_json, context_json)
            LogBuffer.add('I', TAG, "POLL_HEAP_BEFORE_BRAIN: ${heapLine()}")
            // H1: effective timeout (PyTimeout); the old coroutine timeout never fired around a blocking callAttr.
            // The poll path shares one key, so a timed-out poll call still running
            // in the background never overlaps a later poll's Python calls.
            // Evidence batch 1 (Item B): ask Brain for the frozen decision menu only on the capture slot.
            // Without the request, analyze() output is unchanged (tested in test_paper_menu_evidence.py).
            ctxObj.remove("paper_menu_capture_request")
            paperMenuAttemptSlot = if (isRefresh) null else try {
                preparePaperMenuCaptureRequest(poll.optString("slot_key", "").ifBlank { null }, ctxObj)
            } catch (e: Exception) {
                LogBuffer.add('W', TAG, "PAPER_MENU_REQUEST_FAIL: ${e.message}")
                null
            }
            // b510 X1: native-owned research keys; stripped before every call (refresh / persisted ctx
            // can never replay an entry), attached only for a live explicit-Paper poll.
            try {
                PaperBuyingResearch.applyContext(ctxObj, isRefresh, filesDir, System.currentTimeMillis(),
                    x1NfChainFetchedAtMs, x1BnfChainFetchedAtMs)
            } catch (e: Exception) {
                for (key in PaperBuyingResearch.CONTEXT_KEYS) ctxObj.remove(key)
                LogBuffer.add('W', TAG, "X1_CTX_FAIL: ${e.javaClass.simpleName}")
            }
            // Implied-EV v1.2: keys only for an explicit Paper poll with the experiment enabled (default off).
            val evMode = EvExperiment.mode(prefs)
            val evApplied = if (isRefresh) false else try {
                EvExperiment.applyContext(ctxObj, evMode, ctxObj.optString("executionMode", ""), filesDir,
                    System.currentTimeMillis(), evNfChainFetchedAtMs.takeIf { it > 0L },
                    prefs.getString(EvExperiment.PREF_CALIBRATION, null), BuildConfig.VERSION_NAME)
            } catch (e: Exception) {
                LogBuffer.add('W', TAG, "EV12_CTX_FAIL: ${e.message}"); false
            }
            if (!isRefresh) evRefreshCalibrationIfDue(evApplied, System.currentTimeMillis())
            // Delivery A: local durable state for index_direction_v1 (scale at the open reference, bounded
            // per-index decision history). Local files only; no network, no Supabase.
            try {
                IndexAuthorityContext.apply(ctxObj, filesDir, isRefresh, System.currentTimeMillis(),
                    prefs.getString(IndexAuthorityContext.PREF_SELECTOR, null))
                ctxObj.put("singleLegPaperEnabled", prefs.getBoolean(IndexAuthorityContext.PREF_SINGLE_LEG_PAPER, false))
                if (ManagedReadAdapters.contractsRequired(ctxObj.optString("executionMode", ""), isRefresh)) {
                    // Shared managed Paper metadata (multi-leg AND single-leg). This must not
                    // depend on the single-leg opt-in. X1's chain formatter stays unchanged.
                    // Cached once per (index, expiry, session), with bounded failure retries.
                    val tok = (prefs.getString("auth_token", "") ?: "").trim()
                    if (tok.isNotEmpty()) {
                        val tr = ManagedReadAdapters.HttpTransport { tok }
                        val clocks = ManagedReadAdapters.Clocks({ System.currentTimeMillis() }, { android.os.SystemClock.elapsedRealtime() })
                        val sess = IndexAuthorityContext.istSession(System.currentTimeMillis())
                        val slDir = IndexAuthorityContext.singleLegDir(filesDir)
                        for ((ix, exKey) in listOf("BNF" to "bnfExpiry")) {
                            val ex = ctxObj.optString(exKey, "")
                            ManagedReadAdapters.ensureContracts(slDir, ix, ex, sess, tr, clocks)?.let { e ->
                                ManagedEvidenceFormatter.attachContracts(ctxObj, ix, e.getJSONObject("response"), e.optLong("fetched_at_ms"))
                            }
                        }
                    }
                }
            } catch (e: Exception) {
                LogBuffer.add('W', TAG, "INDEX_AUTHORITY_CTX_FAIL: ${e.javaClass.simpleName}")
            }
            brainAnalyzeStartedAtMs = System.currentTimeMillis()
            val result = PyTimeout.callWithTimeout(PY_POLL_TIMEOUT_KEY, PY_POLL_ANALYZE_TIMEOUT_MS) {
                brain.callAttr("analyze",
                    pollsJson,
                    closedTradesJson,
                    baselineJson,
                    openTradesJson,
                    "[]",
                    strikeOiJson,
                    ctxObj.toString()
                ).toString()
            }
            
            if (result != null) {
                val brainAnalyzeCompletedAtMs = System.currentTimeMillis()
                val brainAnalyzeDurationMs = brainAnalyzeCompletedAtMs - brainAnalyzeStartedAtMs
                val resultObj = JSONObject(result)
                    .put("brain_result_started_at_ms", brainAnalyzeStartedAtMs)
                    .put("brain_result_completed_at_ms", brainAnalyzeCompletedAtMs)
                    .put("brain_result_duration_ms", brainAnalyzeDurationMs)
                // expiry_fallback_v1: own-expiry-day Paper BUY -> acquire the later-expiry evidence and re-plan
                // BEFORE the persist delta is detached and anything is published (never on a refresh).
                // R2: a refresh never acquires; a pending fallback plan is withdrawn with an explicit WAIT so a
                // displayed fallback proposal cannot be tapped against a refreshed decision (stale tap rejected by
                // admission's decision/instrument probe; the next qualifying poll re-plans from fresh evidence).
                if (!isRefresh) efbAttempt = applyExpiryFallback(brain, resultObj, ctxObj, openTradesJson)
                else ExpiryFallbackAcquirer.markWait(resultObj, "REFRESH_NOT_ACQUIRED")
                // R5 s2: authority state delta leaves the result; persisted by the serialized IO worker below
                val iaPersist = try { IndexAuthorityContext.takePersist(resultObj) } catch (e: Exception) { null }
                // Evidence batch 1 (Item B): detach the menu before anything else reads, caches,
                // broadcasts or snapshots the result; it is persisted only through its own path.
                val paperDecisionMenu = resultObj.optJSONObject("paper_decision_menu")
                resultObj.remove("paper_decision_menu")
                // M0 research collector: detach the raw pre-alignment forecast (never snapshotted/broadcast); a
                // REFRESH result is never recorded as any slot's forecast.
                val m0Raw = resultObj.optJSONObject(ResearchCollector.RAW_FORECAST_KEY)
                resultObj.remove(ResearchCollector.RAW_FORECAST_KEY)
                if (!isRefresh) m0CaptureEnvelope(poll, m0Raw, brainAnalyzeStartedAtMs, brainAnalyzeCompletedAtMs, ctxObj, null)
                // Implied-EV v1.2: envelopes/outcomes leave the result here (exact bytes -> outbox); only the
                // small recommendation view stays for the PWA.
                if (isRefresh) {
                    resultObj.remove(EvExperiment.RESULT_KEY)
                } else if (resultObj.has(EvExperiment.RESULT_KEY)) {
                    try {
                        val det = EvExperiment.detach(resultObj, EvExperiment.outboxDir(filesDir), System.currentTimeMillis())
                        val view = det.view
                        LogBuffer.add('I', TAG, "EV12_RESULT: status=${view?.optString("status")} " +
                            "wait=${view?.optString("wait_reason")} queued=${det.queued}")
                        launchEvUploadIfPending()
                    } catch (e: Exception) {
                        resultObj.remove(EvExperiment.RESULT_KEY)
                        LogBuffer.add('W', TAG, "EV12_DETACH_FAIL: ${e.message}")
                    }
                }
                LogBuffer.add(
                    'I',
                    TAG,
                    "BRAIN_ANALYZE_COMPLETE: elapsedMs=$brainAnalyzeDurationMs resultBytes=${result.toByteArray(Charsets.UTF_8).size} timeoutMs=$PY_POLL_ANALYZE_TIMEOUT_MS"
                )
                try {
                    val contextPercentiles = resultObj.optJSONObject("context_percentiles")
                    val variables = contextPercentiles?.optJSONObject("variables")
                    val premiumHistForLog = JSONArray(prefs.getString("premium_history", "[]") ?: "[]")
                    val ctxPercentileLog =
                        "CTX_PERCENTILE_RESULT: " +
                            "historySource=${contextPercentiles?.optString("history_source", "missing") ?: "missing"}, " +
                            "liveRanking=${contextPercentiles?.optBoolean("live_ranking_influence", false) ?: false}, " +
                            "variableCount=${variables?.length() ?: 0}, " +
                            "premiumRows=${premiumHistForLog.length()}, " +
                            "enrichedRows=${countPercentileEnrichedHistoryRows(premiumHistForLog)}"
                    Log.d(TAG, ctxPercentileLog)
                    LogBuffer.add('I', TAG, ctxPercentileLog)
                } catch (e: Exception) {
                    Log.w(TAG, "CTX_PERCENTILE_RESULT_LOG_FAIL: ${e.message}")
                }
                // b507 (condition 6): publish only if the acknowledged inputs are still at the start revision.
                // b507 R1 (F3): one frozen identity, re-validated by the shared authority at EVERY boundary
                // (margin enrichment, Paper menu, notification dispatch, BUILD3 row, publication).
                val identity = AnalysisAuthority.Identity(inputSession, startInputRevision, marketCollectedAtMs, mode)
                val sideEffects = !isRefresh
                val superseded = !analysisAuthority.isCurrent(identity)
                resultObj.put("input_identity", JSONObject()
                    .put("session", inputSession)
                    .put("input_revision", startInputRevision)
                    .put("status_at_start", inputStatusAtStart)
                    .put("pair_revision", ctxObj.optJSONObject("overnightInputs")?.optString("pair_revision", "")?.ifBlank { null } ?: JSONObject.NULL)
                    .put("mode", mode)
                    .put("computed_at_ms", brainAnalyzeCompletedAtMs)
                    .put("poll_t", poll.optString("t", poll.optString("time", "")))
                    .put("poll_slot", poll.optString("slot_key", ""))
                    .put("market_collected_at_ms", marketCollectedAtMs)
                    .put("market_acquisition_completed_at_ms", marketSnapshot.completedAtMs))
                if (superseded) {
                    LogBuffer.add('W', TAG, "ANALYSIS_SUPERSEDED: mode=$mode start=$startInputRevision; result not published")
                    if (!isRefresh) analysisAuthority.requestRefresh()   // drained when this lane is released
                }
                if (sideEffects && analysisAuthority.isCurrent(identity)) {
                    val marginPollKey = "${ctxObj.optString("today_ist", todayIstDate())}|${poll.optString("t", poll.optString("time", ""))}"
                    enrichTopCandidateMargin(resultObj, ctxObj, authToken, marginPollKey)
                }
                paperMenuAttemptSlot?.let { slot ->
                    paperMenuAttemptSlot = null
                    if (!analysisAuthority.isCurrent(identity)) {
                        recordPaperMenuAttemptFailure(slot, "superseded_input_revision")
                    } else try {
                        capturePaperDecisionMenu(slot, paperDecisionMenu, ctxObj, authToken, brainAnalyzeCompletedAtMs)
                    } catch (e: Exception) {
                        recordPaperMenuAttemptFailure(slot, "capture_exception:${e.javaClass.simpleName}:${e.message?.take(120)}")
                    }
                }
                if (sideEffects) {
                    val dispatched = analysisAuthority.dispatchIfCurrent(identity) { processUnifiedBrainNotifications(brain, resultObj, ctxObj) }
                    if (!dispatched) LogBuffer.add('I', TAG, "NOTIFICATIONS_SUPPRESSED_SUPERSEDED: rev=$startInputRevision")
                }

                try {
                    val build3Ab = resultObj.optJSONObject("build3_ab")
                    if (build3Ab != null && sideEffects && analysisAuthority.isCurrent(identity)) {
                        val sessionDate = build3Ab.optString(
                            "session_date",
                            ctxObj.optString("today_ist", todayIstDate())
                        ).ifBlank { todayIstDate() }
                        val row = JSONObject(build3Ab.toString())
                        if (!row.has("snapshot_poll_ts") || row.optString("snapshot_poll_ts").isBlank()) {
                            row.put("snapshot_poll_ts", currentIstIso())
                        }
                        row.put("session_date", sessionDate)
                        row.put("local_saved_at", currentIstIso())
                        EvaluationLocalCache.appendBuild3AbDecision(this@MarketWatchService, sessionDate, row)
                        serviceScope.launch(Dispatchers.IO) {
                            val saved = SupabaseClient.saveBuild3AbDecision(row)
                            LogBuffer.add(
                                if (saved) 'I' else 'W',
                                TAG,
                                "BUILD3_AB_SAVE: saved=$saved date=$sessionDate pollTs=${row.optString("snapshot_poll_ts")} old=${row.optString("old_pick_candidate_id")} new=${row.optString("new_pick_candidate_id")} gate=${row.optString("gate_reason")}"
                            )
                        }
                    }
                } catch (e: Exception) {
                    LogBuffer.add('W', TAG, "BUILD3_AB_PERSIST_FAIL: ${e.message}")
                    Log.w(TAG, "BUILD3_AB_PERSIST_FAIL: ${e.message}")
                }

                // ML Arch V2 / b503 capture reliability: the snapshot is captured through the
                // application-owned CaptureCoordinator. Inputs are frozen HERE, on the poll
                // thread, before submission and before the A1 ctx merge below mutates ctxObj.
                // A result that outlives PY_SNAPSHOT_TIMEOUT_MS is delivered LATE from the same
                // original Future (never recomputed); failures become explicit capture events.
                if (sideEffects) {
                val snapshotStageStarted = SystemClock.elapsedRealtime()
                var snapshotIdentity = poll.optString("slot_key", "").ifBlank { "unscheduled" }
                try {
                    // Batch A: lift marketPhase onto ctx before snapshot so
                    // producer→compact→upload can retain session phase evidence.
                    // (The later A1 ctx merge still runs for the next poll.)
                    try {
                        val marketPhaseForSnap = resultObj.optJSONObject("marketPhase")
                        if (marketPhaseForSnap != null) ctxObj.put("marketPhase", marketPhaseForSnap)
                    } catch (_: Exception) { /* non-fatal */ }
                    val capture = CaptureRuntime.ensure(applicationContext)
                    val frozen = try {
                        FrozenCaptureRequest.freeze(
                            pollSlotKey = poll.optString("slot_key", "").ifBlank { null },
                            ctxTodayIst = ctxObj.optString("today_ist", ""),
                            pollTime = poll.optString("t", poll.optString("time", "")),
                            result = resultObj,
                            ctx = ctxObj,
                            pollsJson = pollsJson,
                            appVersion = BuildConfig.VERSION_NAME,
                            versionCode = BuildConfig.VERSION_CODE,
                            brainVersion = resultObj.optString("brain_version", "unknown"),
                            runId = MarketRadarApp.PROCESS_START_UUID
                        )
                    } catch (e: CaptureIdentityException) {
                        capture.engine.recordHealth("identity_rejected", snapshotIdentity, 0, e.reason)
                        LogBuffer.add('E', TAG, "ML_SNAPSHOT_STAGE_ABORTED: reason=identity_rejected:${e.reason} poll=$snapshotIdentity")
                        null
                    }
                    if (frozen != null) {
                        snapshotIdentity = frozen.captureKey
                        if (capture.engine.isCompleted(frozen.captureKey)) {
                            recordSnapshotStage("reservation_skip", frozen.captureKey)
                            LogBuffer.add('W', TAG, "ML_PERSIST_DEDUP_SKIP: key=${frozen.captureKey}")
                        } else {
                            val captureOutcome = capture.coordinator.submit(frozen, PY_SNAPSHOT_TIMEOUT_MS) { req ->
                                brain.callAttr(
                                    "take_poll_snapshot",
                                    req.resultJson,
                                    req.ctxJson,
                                    req.pollsJson,
                                    req.persistenceMode
                                ).toString()
                            }
                            LogBuffer.add(
                                if (captureOutcome == CaptureCoordinator.Outcome.DELIVERED_ON_TIME) 'I' else 'W',
                                TAG,
                                "ML_SNAPSHOT_CAPTURE: outcome=$captureOutcome key=${frozen.captureKey} " +
                                    "attempt=${frozen.attemptId.take(8)} freezeMs=${frozen.freezeMs} " +
                                    "callerMs=${SystemClock.elapsedRealtime() - snapshotStageStarted}"
                            )
                            // Frozen evidence keeps draining even while a capture is late/stuck.
                            if (captureOutcome != CaptureCoordinator.Outcome.DELIVERED_ON_TIME) launchCaptureDrains()
                            launchElephantObserveOnly(resultObj)
                        }
                    }
                } catch (e: Exception) {
                    recordSnapshotStage("exception", snapshotIdentity,
                        SystemClock.elapsedRealtime() - snapshotStageStarted, e.javaClass.simpleName)
                    Log.w(TAG, "ML_SNAPSHOT_FAIL: ${e.message}")
                    LogBuffer.add('E', TAG, "ML_SNAPSHOT_STAGE_ABORTED: reason=${e.javaClass.simpleName}")
                }
                
                } // b507: ML snapshot only on real polls (kept for superseded polls: observation, not publication)

                // A1: Persist brain results back into ctxObj for next poll cycle
                try {
                    val eb = resultObj.optJSONObject("effective_bias")
                    if (eb != null) ctxObj.put("effective_bias", eb)
                    val regime = resultObj.optJSONObject("regime")
                    if (regime != null) ctxObj.put("regime", regime)
                    val marketPhase = resultObj.optJSONObject("marketPhase")
                    if (marketPhase != null) ctxObj.put("marketPhase", marketPhase)
                    val verdict = resultObj.optJSONObject("verdict")
                    if (verdict != null) ctxObj.put("prevVerdict", verdict)
                    val rangeSigma = resultObj.optString("rangeSigma", "")
                    if (rangeSigma.isNotEmpty()) ctxObj.put("rangeSigma", rangeSigma)
                    val positionLive = resultObj.optJSONObject("position_live")
                    if (positionLive != null) ctxObj.put("positionLive", positionLive)

                    // A1 COMPLETION: persist remaining brain outputs so next cycle
                    // has morning context, chain profiles, and institutional signals
                    for (key in listOf(
                        "morningBias",
                        "bnfProfile",
                        "nfProfile",
                        "pcrContext",
                        "institutionalRegime",
                        "overnightDelta",
                        "sessionTrajectory"
                    )) {
                        val v = resultObj.opt(key)
                        if (v != null && v.toString() != "null") ctxObj.put(key, v)
                    }

                    ctxObj.put("last_brain_ms", System.currentTimeMillis())
                } catch (e: Exception) {
                    Log.w(TAG, "BRAIN_CTX_MERGE_FAIL: ${e.message}")
                }

                // Retire afternoon positioning: captureChainSnapshots removed
                // (legacy chain_snapshots writer). Active ml_option_chain_snapshots
                // capture path is unchanged.

                // Decision #17/#18/#Issue9: Persist brain-computed P&L and metrics back to open_trades
                // Percentile-contextual exit levels resolved by brain.py for the
                // 60-second tick service, which owns those notifications since D4
                // but has no access to poll history or context percentiles. Stored
                // with commit() because PositionTickService reads it from its own
                // tick loop, which can run before an apply() has flushed.
                // Replace the whole threshold map after every successful Brain
                // result. Keeping an older non-empty map when the current result
                // intentionally has no thresholds leaves P1 evaluating days-old
                // levels. An empty map is the explicit, safe fallback signal.
                // b507: position management stays on the real poll path (also when superseded); never in REFRESH.
                if (sideEffects) {
                val thresholds = resultObj.optJSONObject("position_exit_thresholds") ?: JSONObject()
                prefs.edit()
                    .putString(PREF_POSITION_EXIT_THRESHOLDS, thresholds.toString())
                    .commit()
                LogBuffer.add(
                    'I',
                    TAG,
                    "POSITION_EXIT_THRESHOLDS_PUBLISHED: trades=${thresholds.length()}"
                )

                val posLive = resultObj.optJSONObject("position_live")
                if (posLive != null) {
                    val tradesStr = prefs.getString("open_trades", "[]") ?: "[]"
                    val trades = JSONArray(tradesStr)
                    var changed = false
                    var updatedCount = 0
                    for (i in 0 until trades.length()) {
                        val t = trades.getJSONObject(i)
                        val tid = t.optString("id")
                        val live = posLive.optJSONObject(tid)
                        if (live != null) {
                            t.putNullableFiniteNumber("current_pnl", live.optNullableFiniteDouble("current_pnl"))
                            t.putNullableFiniteNumber("current_spot", live.optNullableFiniteDouble("current_spot"))
                            applyOptionalPositionLiveMetrics(t, live)
                            if (live.has("journey")) {
                                t.put("journey", live.optJSONArray("journey"))
                            }
                            
                            val posData = resultObj.optJSONObject("positions")?.optJSONObject(tid)
                            if (posData != null) {
                                t.put("controlIndex", posData.optInt("controlIndex", 0))
                                t.put("wallDrift", posData.optJSONObject("wallDrift"))
                            }
                            changed = true
                            updatedCount += 1
                        }
                    }
                    Log.d(TAG, "POSITION_LIVE_APPLIED: live=${posLive.length()} open=${trades.length()} updated=$updatedCount")
                    if (changed) {
                        prefs.edit().putString("open_trades", trades.toString()).commit()
                    }
                }

                } // b507 sideEffects (position thresholds + position_live)

                // --- NEW Diagnostic Result Parsing ---
                try {
                    val candidateError = resultObj.optString("candidate_error", "")
                    if (candidateError.isNotEmpty()) {
                        Log.e("BRAIN_PHASE3_ERROR", "Phase 3 exception: $candidateError")
                    }
                    val generated = resultObj.optJSONArray("generated_candidates")
                    val watchlist = resultObj.optJSONArray("watchlist")
                    val actualCandidates = resultObj.optJSONObject("candidates")
                    val candidateStats = resultObj.optJSONObject("candidate_stats")
                    var generatedBnf = 0
                    var generatedNf = 0
                    var watchlistBnf = 0
                    var watchlistNf = 0
                    if (generated != null) {
                        for (i in 0 until generated.length()) {
                            when (generated.optJSONObject(i)?.optString("index", "")) {
                                "BNF" -> generatedBnf += 1
                                "NF" -> generatedNf += 1
                            }
                        }
                    }
                    if (watchlist != null) {
                        for (i in 0 until watchlist.length()) {
                            when (watchlist.optJSONObject(i)?.optString("index", "")) {
                                "BNF" -> watchlistBnf += 1
                                "NF" -> watchlistNf += 1
                            }
                        }
                    }
                    val statsTotal = candidateStats?.optInt("total", -1) ?: -1
                    val statsRejected = candidateStats?.optInt("rejected", -1) ?: -1
                    val statsRanked = candidateStats?.optInt("ranked", -1) ?: -1
                    val statsWatchlist = candidateStats?.optInt("watchlist", -1) ?: -1
                    val statsByIndex = candidateStats?.optJSONObject("by_index")
                    val statsRejectedByIndex = candidateStats?.optJSONObject("rejected_by_index")
                    Log.d("BRAIN_CANDIDATES_DETAIL",
                        "generated=${generated?.length() ?: 0}, " +
                        "watchlist=${watchlist?.length() ?: 0}, " +
                        "candidates_in_result=${actualCandidates?.length() ?: 0}, " +
                        "generated_bnf=$generatedBnf, generated_nf=$generatedNf, " +
                        "watchlist_bnf=$watchlistBnf, watchlist_nf=$watchlistNf, " +
                        "stats_total=$statsTotal, stats_rejected=$statsRejected, " +
                        "stats_ranked=$statsRanked, stats_watchlist=$statsWatchlist, " +
                        "stats_by_index_bnf=${statsByIndex?.optInt("BNF", -1)}, stats_by_index_nf=${statsByIndex?.optInt("NF", -1)}, " +
                        "stats_rejected_bnf=${statsRejectedByIndex?.optInt("BNF", -1)}, stats_rejected_nf=${statsRejectedByIndex?.optInt("NF", -1)}"
                    )
                    val resultKeys = mutableListOf<String>()
                    val resultIter = resultObj.keys()
                    while (resultIter.hasNext() && resultKeys.size < 20) {
                        resultKeys.add(resultIter.next())
                    }
                    LogBuffer.add(
                        'D',
                        TAG,
                        "BRAIN_RESULT: len=${result.length} keys=${resultKeys.joinToString("|")} generated=${generated?.length() ?: 0} watchlist=${watchlist?.length() ?: 0} generatedBNF=$generatedBnf generatedNF=$generatedNf watchlistBNF=$watchlistBnf watchlistNF=$watchlistNf statsTotal=$statsTotal statsRejected=$statsRejected statsByBNF=${statsByIndex?.optInt("BNF", -1)} statsByNF=${statsByIndex?.optInt("NF", -1)} candidateTrace=${resultObj.has("candidateTrace")}"
                    )
                } catch(e: Exception) {
                    Log.w("BRAIN_RESULT_PARSE", "Failed to parse candidate details: ${e.message}")
                    LogBuffer.add('W', TAG, "BRAIN_RESULT_PARSE_FAIL: ${e.message}")
                }

                // --- v2.2.6 ML Scoring Integration ---
                val generatedCands = resultObj.optJSONArray("generated_candidates")
                if (generatedCands != null && generatedCands.length() > 0 && isMLModelReady()) {
                    val py = Python.getInstance()
                    val brainMod = py.getModule("brain")
                    val mlDeadlineMs = System.currentTimeMillis() + 3_000L
                    var mlScoredCount = 0
                    for (i in 0 until generatedCands.length()) {
                        if (System.currentTimeMillis() > mlDeadlineMs) {
                            Log.w("BRAIN_ML_SCORING", "ML scoring timeout after $i/${generatedCands.length()} candidates")
                            LogBuffer.add('W', TAG, "ML_SCORING_TIMEOUT: scored=$mlScoredCount total=${generatedCands.length()}")
                            break
                        }
                        val cand = generatedCands.getJSONObject(i)
                        val mlResult = scoreCandidate(cand, brainMod)
                        if (mlResult != null) {
                            cand.put("p_ml", finiteDouble(mlResult, "p_ml", 0.0))
                            cand.put("mlAction", mlResult.optString("ml_action"))
                            cand.put("mlEdge", finiteDouble(mlResult, "ml_edge", 0.0))
                            cand.put("mlOod", mlResult.optBoolean("ml_ood", false))
                            cand.put("mlOodConf", finiteDouble(mlResult, "ml_ood_conf", 1.0))
                            cand.put("mlOodWarn", mlResult.optJSONArray("ml_ood_warn") ?: JSONArray())
                            cand.put("mlOodBlocked", mlResult.optBoolean("ml_ood_blocked", false))
                            cand.put("mlRegime", mlResult.optString("ml_regime", ""))
                            mlScoredCount++
                        }
                    }
                    Log.d("BRAIN_ML_SCORING", "Scored $mlScoredCount/${generatedCands.length()} background candidates")
                }


                Log.d(TAG, "SAVING_BRAIN_RESULT: result length=${result.length}")
                
                // WS19: resultObj was mutated with ML scoring, 'result' string was NOT.
                // Save resultObj to ensure ML-scored data persists in getBrainResult()
                val finalBrainString = resultObj.toString()
                
                val candidates = resultObj.optJSONArray("generated_candidates")
                    ?: resultObj.optJSONArray("candidates")
                // b507 R1 (F3/F4): validation and the result-reference commit are ONE guarded operation.
                // R4 (Codex R3 reply): the expiry-fallback attempt (if any) is decided HERE, after publish returns:
                // PUBLISHED only for a successful guarded commit, otherwise NOT_PUBLISHED with the authority's reason.
                outcome = ExpiryFallbackPublication.publishThroughGate(expiryFallbackPublication, efbAttempt,
                    analysisAuthority, identity, { System.currentTimeMillis() },
                    { BrainResultStore.save(this, prefs, finalBrainString, candidates?.toString()) },
                    { d -> queueExpiryFallbackReconcile(IndexAuthorityContext.singleLegDir(filesDir), d) })
                val publishNow = outcome == AnalysisAuthority.Outcome.PUBLISHED
                // R5 s2: persist the authority delta off the analysis path; published_at only when PUBLISHED
                if (iaPersist != null) {
                    IndexAuthorityIo.persist(filesDir, iaPersist,
                        IndexAuthorityContext.publishedMs(publishNow, System.currentTimeMillis()))
                }
                if (publishNow && sideEffects) {
                    prefs.edit().putBoolean("paper_shared_inputs_unavailable", false).apply()
                    // Research owns a bounded independent ledger; no admission/capture clock substitution.
                    val evidenceResult = JSONObject().put("market_scope", resultObj.optJSONObject("market_scope"))
                        .put("single_leg_by_index", resultObj.optJSONObject("single_leg_by_index"))
                        .put("input_identity", resultObj.optJSONObject("input_identity"))
                        .put("direction_authority", resultObj.optJSONObject("direction_authority"))
                        .put("brain_version", resultObj.opt("brain_version"))
                        .put("producer_versions", JSONObject().put("app", "${BuildConfig.VERSION_NAME}/${BuildConfig.VERSION_CODE}")
                            .put("native_source", BuildConfig.SOURCE_HEAD).put("bundled_pwa_source", BuildConfig.PWA_SOURCE_HEAD))
                    val evidenceChains = JSONObject().put("BNF", bnfChain).toString()
                    val evidenceClocks = JSONObject()
                        .put("BNF", JSONObject().put("receipt_ms", x1BnfChainFetchedAtMs)).toString()
                    IndexAuthorityIo.executor.execute {
                        try {
                            com.chaquo.python.Python.getInstance().getModule("proposal_evidence_v1").callAttr("record",
                                filesDir.absolutePath, evidenceResult.toString(), evidenceChains, evidenceClocks)
                        } catch (e: Exception) { LogBuffer.add('W', TAG, "PROPOSAL_EVIDENCE_FAIL: ${e.javaClass.simpleName}") }
                    }
                    val readinessToken = (prefs.getString("auth_token", "") ?: "").trim()
                    if (readinessToken.isNotEmpty()) IndexAuthorityIo.verifySingleLegReadiness(filesDir, prefs,
                        ManagedReadAdapters.HttpTransport { (prefs.getString("auth_token", "") ?: "").trim() })
                    // C1 capture pilot: opt-in research capture (default OFF); never admits or writes single_leg_v1
                    try {
                        C1PilotIo.offer(filesDir, prefs, resultObj, System.currentTimeMillis())
                    } catch (e: Exception) {
                        LogBuffer.add('W', TAG, "C1_PILOT_CTX_FAIL: ${e.javaClass.simpleName}")
                    }
                }
                if (sideEffects) IndexAuthorityIo.maybeRunDailyReview(filesDir, prefs, System.currentTimeMillis())
                if (outcome == AnalysisAuthority.Outcome.SUPERSEDED_INPUT && !isRefresh) analysisAuthority.requestRefresh()
                if (outcome == AnalysisAuthority.Outcome.SUPERSEDED_INPUT || outcome == AnalysisAuthority.Outcome.STALE_MARKET
                    || outcome == AnalysisAuthority.Outcome.MARKET_IDENTITY_UNAVAILABLE) {
                    LogBuffer.add('I', TAG, "BRAIN_RESULT_NOT_PUBLISHED: outcome=$outcome rev=$startInputRevision mode=$mode")
                } else if (publishNow) {
                    prefs.edit()
                        .putLong(PREF_LAST_BRAIN_ANALYSIS_SUCCESS_MS, brainAnalyzeCompletedAtMs)
                        .putLong(PREF_LAST_BRAIN_ANALYSIS_DURATION_MS, brainAnalyzeDurationMs)
                        .remove(PREF_LAST_BRAIN_ANALYSIS_FAILURE_REASON)
                        .apply()
                    brainSuccess = true
                } else {
                    val failureAtMs = System.currentTimeMillis()
                    prefs.edit()
                        .putLong(PREF_LAST_BRAIN_ANALYSIS_FAILURE_MS, failureAtMs)
                        .putLong(PREF_LAST_BRAIN_ANALYSIS_DURATION_MS, failureAtMs - brainAnalyzeStartedAtMs)
                        .putString(PREF_LAST_BRAIN_ANALYSIS_FAILURE_REASON, "RESULT_PERSISTENCE_FAILED")
                        .apply()
                    LogBuffer.add('E', TAG, "BRAIN_RESULT_NOT_CURRENT: persistence failed; previous result remains authoritative only as stale display")
                }
                LogBuffer.add('I', TAG, "POLL_HEAP_AFTER_PERSISTENCE: ${heapLine()}")
                Log.d(TAG, "BRAIN_COMPLETE: candidates=${candidates?.length() ?: 0}")
                
                // Phase E: Persist routine alert timestamp if routine alert was fired
                val alerts = resultObj.optJSONArray("alerts")
                if (alerts != null && sideEffects && publishNow) {
                    for (i in 0 until alerts.length()) {
                        if (alerts.getJSONObject(i).optString("category") == "ROUTINE") {
                            prefs.edit().putLong("last_routine_dispatch_ms", System.currentTimeMillis()).apply()
                            break
                        }
                    }
                }

                // Persist candlestick data to Supabase for historical review
                try {
                    val candleBnf = resultObj.optJSONObject("candle_bnf")
                    val candleNf = resultObj.optJSONObject("candle_nf")
                    if (sideEffects && (candleBnf != null || candleNf != null)) {
                        SupabaseClient.upsertCandleData(
                            todayIstDate(),
                            candleBnf ?: JSONObject(),
                            candleNf ?: JSONObject()
                        )
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Candle data persist failed: ${e.message}")
                }

            } else {
                val failureAtMs = System.currentTimeMillis()
                val elapsedMs = if (brainAnalyzeStartedAtMs > 0L) failureAtMs - brainAnalyzeStartedAtMs else 0L
                prefs.edit()
                    .putLong(PREF_LAST_BRAIN_ANALYSIS_FAILURE_MS, failureAtMs)
                    .putLong(PREF_LAST_BRAIN_ANALYSIS_DURATION_MS, elapsedMs)
                    .putString(PREF_LAST_BRAIN_ANALYSIS_FAILURE_REASON, "TIMEOUT")
                    .apply()
                Log.w(TAG, "BRAIN_TIMEOUT: brain.analyze timed out after ${PY_POLL_ANALYZE_TIMEOUT_MS}ms")
                outcome = AnalysisAuthority.Outcome.TIMED_OUT
                if (!isRefresh) m0CaptureEnvelope(poll, null, brainAnalyzeStartedAtMs, null, null, "brain_timeout")
                paperMenuAttemptSlot?.let { recordPaperMenuAttemptFailure(it, "brain_analyze_timeout") }
                paperMenuAttemptSlot = null
            }
            
        } catch (e: Exception) {
            val failureAtMs = System.currentTimeMillis()
            val elapsedMs = if (brainAnalyzeStartedAtMs > 0L) failureAtMs - brainAnalyzeStartedAtMs else 0L
            prefs.edit()
                .putLong(PREF_LAST_BRAIN_ANALYSIS_FAILURE_MS, failureAtMs)
                .putLong(PREF_LAST_BRAIN_ANALYSIS_DURATION_MS, elapsedMs)
                .putString(PREF_LAST_BRAIN_ANALYSIS_FAILURE_REASON, "EXCEPTION:${e.javaClass.simpleName}")
                .apply()
            if (!isRefresh) m0CaptureEnvelope(poll, null, brainAnalyzeStartedAtMs, null, null, "brain_exception:${e.javaClass.simpleName}")
            Log.e(TAG, "BRAIN_ERROR: ${e.message}\n${e.stackTraceToString()}")
            paperMenuAttemptSlot?.let { recordPaperMenuAttemptFailure(it, "brain_exception:${e.javaClass.simpleName}") }
            paperMenuAttemptSlot = null
            efbAbort = "ABORTED_BEFORE_FINAL_PUBLISH:${e.javaClass.simpleName}"
        } finally {
            // R4 finalizer: an attempt that never reached the final gate is fenced NOT_PUBLISHED; a decided attempt
            // (PUBLISHED / NOT_PUBLISHED / GATE_UNRESOLVED) is never touched.
            try {
                ExpiryFallbackPublication.fenceUnfinished(expiryFallbackPublication, efbAttempt,
                    efbAbort ?: "ANALYSIS_ENDED_BEFORE_FINAL_PUBLISH", { System.currentTimeMillis() },
                    { d -> queueExpiryFallbackReconcile(IndexAuthorityContext.singleLegDir(filesDir), d) })
            } catch (e: Exception) {
                LogBuffer.add('W', TAG, "EXPIRY_FALLBACK_FENCE_FAIL: ${e.javaClass.simpleName}")
            }
            // Keep the Binder payload empty. The WebView pulls the latest native
            // state directly, and sending the full brain payload here can exceed
            // Android's parcel limit and crash the poll loop.
            val tickIntent = Intent("com.marketradar.POLL_TICK").setPackage(packageName)
            sendBroadcast(tickIntent)
            val pollCount = prefs.getInt("poll_count", 0)
            Log.d(TAG, "BROADCAST_SENT: Poll #$pollCount (brain success=$brainSuccess)")
        }
        return outcome
    }

    @Volatile
    private var pc2UnregisteredLogged = false

    private fun drainPc2CompactOutbox() {
        val deviceKey = Pc2IngestCredential.loadOrCreate(
            read = { prefs.getString(Pc2IngestCredential.PREF_DEVICE_KEY, null) },
            // commit(), not apply(): the key must be durable before it is used.
            write = { key -> prefs.edit().putString(Pc2IngestCredential.PREF_DEVICE_KEY, key).commit() }
        )
        if (deviceKey == null) {
            LogBuffer.add('E', TAG, "PC2_INGEST_CREDENTIAL_UNAVAILABLE: compact channel stays off")
            return
        }
        if (!prefs.getBoolean(Pc2IngestCredential.PREF_REGISTERED_CONFIRMED, false)) {
            when (SupabaseClient.pc2IngestDeviceActive(deviceKey)) {
                true -> {
                    prefs.edit().putBoolean(Pc2IngestCredential.PREF_REGISTERED_CONFIRMED, true).commit()
                    LogBuffer.add('I', TAG, "PC2_INGEST_DEVICE_REGISTERED: compact channel enabled")
                }
                else -> {
                    if (!pc2UnregisteredLogged) {
                        pc2UnregisteredLogged = true
                        // The hash only - never the key - so the owner can register it.
                        LogBuffer.add(
                            'W',
                            TAG,
                            "PC2_INGEST_DEVICE_UNREGISTERED: key_hash=${Pc2IngestCredential.keyHash(deviceKey)} " +
                                "compact channel off; legacy PC2 remains authoritative"
                        )
                    }
                    return
                }
            }
        }
        val compactDrain = Pc2TelemetryOutbox.drain(filesDir) { envelope ->
            SupabaseClient.savePc2CompactBatch(envelope, deviceKey)
        }
        val clean = compactDrain.pending == 0 && compactDrain.quarantinedTotal == 0 &&
            compactDrain.recoveredTotal == 0 && compactDrain.storageErrors == 0
        LogBuffer.add(
            if (clean) 'I' else 'W',
            TAG,
            "PC2_COMPACT_OUTBOX_DRAIN: attempted=${compactDrain.attempted} " +
                "acknowledged=${compactDrain.acknowledged} quarantined=${compactDrain.quarantined} " +
                "retried=${compactDrain.retried} stop=${compactDrain.stopReason} " +
                "pending=${compactDrain.pending} " +
                "pendingBytes=${compactDrain.pendingBytes} " +
                "quarantinedTotal=${compactDrain.quarantinedTotal} " +
                "recoveredTotal=${compactDrain.recoveredTotal} " +
                "storageErrors=${compactDrain.storageErrors}"
        )
        compactDrain.quarantineReasons.forEach { reason ->
            LogBuffer.add('E', TAG, "PC2_COMPACT_OUTBOX_QUARANTINE: $reason")
        }
        // R3: an orphaned temp envelope that was verified and re-queued, or that
        // could not be verified and was retained for inspection.
        compactDrain.recoveredReasons.forEach { reason ->
            LogBuffer.add('W', TAG, "PC2_COMPACT_OUTBOX_TEMP_RECOVERY: $reason")
        }
        // R2: a failed archive move is a storage fault, not a successful
        // quarantine. The bytes are still pending and must be surfaced.
        compactDrain.storageErrorReasons.forEach { reason ->
            LogBuffer.add('E', TAG, "PC2_COMPACT_OUTBOX_STORAGE_ERROR: $reason")
        }
    }

    /**
     * b503: network drains after a durable capture fan-out. Every channel is independent and
     * bounded by its outbox budget; one failure never suppresses the others. If this service
     * instance is gone the frozen bytes simply wait for the next start (no stale dependency).
     */
    private fun launchCaptureDrains() {
        serviceScope.launch(Dispatchers.IO) {
            try { CaptureRuntime.reconcile() }
            catch (t: Throwable) { LogBuffer.add('E', TAG, "CAPTURE_RECONCILE_THROW: ${t.javaClass.simpleName}") }
            try {
                val drained = drainBrainSnapshotOutbox()
                // b503 R1: ACKs carry the payload's own validated origin key; a pending summary is unattributed.
                val captureEngine = CaptureRuntime.ensure(applicationContext).engine
                drained.acknowledgedPolls.forEach { captureEngine.recordUploadAck(it, drained.acknowledgedOrigins[it]) }
                if (drained.pending > 0) captureEngine.recordUploadPending(drained.pending)
            } catch (t: Throwable) { LogBuffer.add('E', TAG, "ML_BRAIN_SNAPSHOT_OUTBOX_DRAIN_THROW: ${t.javaClass.simpleName}") }
            runCatching { CaptureRuntime.drainLegacy() }
                .onFailure { LogBuffer.add('E', TAG, "PC2_AUTHORITY_TELEMETRY_THROW: ${it.javaClass.simpleName}") }
            runCatching { drainPc2CompactOutbox() }
                .onFailure { LogBuffer.add('E', TAG, "PC2_COMPACT_OUTBOX_DRAIN_THROW: ${it.javaClass.simpleName}") }
            runCatching { drainGeneratedCandidateOutbox() }
                .onFailure { LogBuffer.add('E', TAG, "ML_GENERATED_CANDIDATES_FAIL: ${it.javaClass.simpleName}") }
            runCatching { CaptureRuntime.drainEvents() }
                .onFailure { LogBuffer.add('E', TAG, "CAPTURE_EVENT_OUTBOX_DRAIN_THROW: ${it.javaClass.simpleName}") }
        }
    }

    private fun drainBrainSnapshotOutbox(): BrainSnapshotOutbox.Status {
        val status = BrainSnapshotOutbox.drain(filesDir) { payload ->
            SupabaseClient.uploadBrainSnapshotPayload(payload)
        }
        LogBuffer.add(
            if (status.pending == 0 && status.faults.isEmpty() && status.quarantinedTotal == 0) 'I' else 'W', TAG,
            "ML_BRAIN_SNAPSHOT_OUTBOX_DRAIN: attempted=${status.attempted} " +
                "acknowledged=${status.acknowledged} pending=${status.pending} " +
                "pendingBytes=${status.pendingBytes} quarantined=${status.quarantined} " +
                "quarantinedTotal=${status.quarantinedTotal} quarantinedBytes=${status.quarantinedBytes} retried=${status.retried} " +
                "stop=${status.stopReason} faults=${status.faults.size}"
        )
        status.faults.forEach { LogBuffer.add('E', TAG, "ML_BRAIN_SNAPSHOT_OUTBOX_FAULT: $it") }
        return status
    }

    @Volatile private var generatedRetainedLogged = false

    private fun drainGeneratedCandidateOutbox(): GeneratedCandidateOutbox.Status {
        if (!generatedRetainedLogged) {
            generatedRetainedLogged = true
            runCatching { GeneratedCandidateOutbox.describeRetained(filesDir) }.getOrDefault(emptyList())
                .forEach { LogBuffer.add('W', TAG, "ML_GENERATED_RETAINED: $it") }
        }
        // b512: plain INSERT + read-back reconciliation (no merge-duplicates upsert, which live RLS rejects 401/42501).
        val status = GeneratedCandidateOutbox.drainDetailed(filesDir) { payload ->
            SupabaseClient.replayGeneratedCandidateEnvelope(payload)
        }
        status.envelopes.forEach { LogBuffer.add('I', TAG, "ML_GENERATED_REPLAY: $it") }
        status.stuck.forEach { LogBuffer.add('E', TAG, "ML_GENERATED_RETAINED_STUCK: $it (bytes retained; retried with backoff)") }
        LogBuffer.add(if (status.pending == 0 && status.quarantinedTotal == 0 && status.faults.isEmpty()) 'I' else 'W',
            TAG, "ML_GENERATED_OUTBOX_DRAIN: acknowledged=${status.acknowledged} pending=${status.pending} " +
                "pendingBytes=${status.pendingBytes} quarantinedTotal=${status.quarantinedTotal} " +
                "quarantinedBytes=${status.quarantinedBytes} stop=${status.stopReason} faults=${status.faults.size} " +
                "retried=${status.retried} deferred=${status.deferred} conflicts=${status.conflicts} stuck=${status.stuck.size}")
        status.faults.forEach { LogBuffer.add('E', TAG, "ML_GENERATED_OUTBOX_FAULT: $it") }
        return status
    }

    // ---------------------------------------------------------------------------------------------
    // Evidence batch 1 (Item B / Item C margin): once-per-session Paper decision-menu capture.
    // Evidence only. Nothing below is read by ranking, gates, sizing, exits or primary selection.
    // ---------------------------------------------------------------------------------------------
    private val paperMenuStateLock = Any()
    private val paperMenuUploadInFlight = AtomicBoolean(false)

    // ---------------- Implied-EV v1.2 Paper experiment (off by default; Paper only) ----------------
    /** Completion time of the NF chain fetch: the quote timestamp (Upstox gives no per-contract time). */
    @Volatile private var evNfChainFetchedAtMs: Long = 0L
    @Volatile private var x1NfChainFetchedAtMs: Long = 0L
    @Volatile private var x1BnfChainFetchedAtMs: Long = 0L
    private val evUploadInFlight = AtomicBoolean(false)
    private val evCalibrationInFlight = AtomicBoolean(false)

    private fun evRefreshCalibrationIfDue(applied: Boolean, nowMs: Long) {
        val last = prefs.getLong(EvExperiment.PREF_CALIBRATION_FETCHED_MS, 0L)
        if (!EvExperiment.shouldRefreshCalibration(applied, nowMs, last)) return
        if (!evCalibrationInFlight.compareAndSet(false, true)) return
        serviceScope.launch(Dispatchers.IO) {
            try {
                val pub = SupabaseClient.fetchEvCalibration()
                val ed = prefs.edit().putLong(EvExperiment.PREF_CALIBRATION_FETCHED_MS, System.currentTimeMillis())
                if (pub != null) ed.putString(EvExperiment.PREF_CALIBRATION, pub)
                    .putLong(EvExperiment.PREF_CALIBRATION_STORED_MS, System.currentTimeMillis())
                ed.apply()
                LogBuffer.add(if (pub != null) 'I' else 'W', TAG, "EV12_CALIBRATION_FETCH: found=${pub != null}")
            } catch (e: Exception) {
                LogBuffer.add('W', TAG, "EV12_CALIBRATION_FETCH_FAIL: ${e.message}")
            } finally {
                evCalibrationInFlight.set(false)
            }
        }
    }

    private fun launchEvUploadIfPending() {
        val dir = EvExperiment.outboxDir(filesDir)
        if (!dir.isDirectory) return
        if (!evUploadInFlight.compareAndSet(false, true)) return
        serviceScope.launch(Dispatchers.IO) {
            try {
                val key = paperMenuDeviceKey()
                val r = EvTelemetryOutbox.drain(dir, System.currentTimeMillis()) { e ->
                    if (key == null) null
                    else if (e.kind == EvTelemetryOutbox.KIND_DECISION) SupabaseClient.evSubmitDecision(key, e.text)
                    else SupabaseClient.evSubmitOutcome(key, e.id, e.text)
                }
                LogBuffer.add(if (r.retry == 0 && r.conflicts == 0 && r.rejected == 0 && r.dropped == 0) 'I' else 'W', TAG,
                    "EV12_OUTBOX: attempted=${r.attempted} acked=${r.acked} retry=${r.retry} quota=${r.quota} " +
                    "prerequisite=${r.prerequisite} conflicts=${r.conflicts} rejected=${r.rejected} dropped=${r.dropped} deferred=${r.deferred}")
            } catch (e: Exception) {
                LogBuffer.add('W', TAG, "EV12_OUTBOX_FAIL: ${e.message}")
            } finally {
                evUploadInFlight.set(false)
            }
        }
    }

    private fun loadPaperMenuState(): JSONObject = synchronized(paperMenuStateLock) {
        PaperMenuCapture.stateFor(prefs.getString(PAPER_MENU_STATE_KEY, null), todayIstDate())
    }

    /** Returns whether the preference commit succeeded; a failure is logged as a storage error (Blocker B). */
    private fun savePaperMenuState(state: JSONObject): Boolean = synchronized(paperMenuStateLock) {
        prefs.edit().putString(PAPER_MENU_STATE_KEY, state.toString()).commit().also { ok ->
            if (!ok) LogBuffer.add('E', TAG, "PAPER_MENU_STORAGE_ERROR: capture_state_commit_failed menu=${state.optString("menu_id")}")
        }
    }

    /** R3: fresh derivation of the execution mode from preferences (same rule as runBrainAnalysis). */
    private fun currentExecutionMode(): String = ExecutionModeResolver.derive(
        prefs.getString("execution_mode", ""),
        prefs.getBoolean("execution_sandbox_enabled", false),
        prefs.getString("order_proxy_url", "")
    )

    /** Returns the slot key when this poll is a capture attempt (request placed into ctx), else null. */
    private fun preparePaperMenuCaptureRequest(slotKey: String?, ctxObj: JSONObject): String? {
        // R3: Paper only. Checked BEFORE any capture state is loaded, rolled over or written, so a live or
        // sandbox decision never creates a menu, consumes the session's capture slot or reaches the
        // evidence-only margin path. Recovery uploads of already-frozen Paper evidence still run.
        val populationMode = ctxObj.optString("executionMode", "")
        if (!ExecutionModeResolver.isPaper(populationMode)) {
            launchPaperMenuUploadIfPending()
            return null
        }
        val now = currentIstIso()
        val dir = paperMenuDir()
        // R2-review Blocker B: PaperMenuRecovery.preparePoll reconciles the outbox directory and the preference state
        // with already-frozen captures BEFORE rollover / MISSED generation; missed manifests are frozen into the
        // durable outbox BEFORE the rolled-over state is committed, and the commit result is checked.
        val poll = synchronized(paperMenuStateLock) {
            PaperMenuRecovery.preparePoll(
                dir, populationMode, prefs.getString(PAPER_MENU_STATE_KEY, null), todayIstDate(),
                slotKey, slotKey?.let { slotOrdinalFromSlotKey(it) }, now, System.currentTimeMillis()
            ) { st -> prefs.edit().putString(PAPER_MENU_STATE_KEY, st).commit() }
        }
        val prepared = poll.prepared
        poll.events.forEach { (level, msg) -> LogBuffer.add(level, TAG, msg) }
        prepared.request?.let { ctxObj.put("paper_menu_capture_request", it) }
        prunePaperMenuFiles()
        launchPaperMenuUploadIfPending()
        return prepared.attemptSlot
    }

    private fun paperMenuDir(): File = File(filesDir, "paper_decision_menus")

    /**
     * R2: device-local retention. Only outbox entries the server ACKNOWLEDGED more than
     * PAPER_MENU_LOCAL_RETENTION_DAYS ago are removed; unacknowledged / exhausted / conflict files are kept.
     */
    private fun prunePaperMenuFiles() {
        try {
            PaperMenuOutbox.prune(paperMenuDir(), System.currentTimeMillis(),
                PAPER_MENU_LOCAL_RETENTION_DAYS * 24L * 60L * 60L * 1000L)
                .forEach { LogBuffer.add('I', TAG, "PAPER_MENU_LOCAL_PRUNE: removed_acked=$it") }
        } catch (e: Exception) {
            LogBuffer.add('W', TAG, "PAPER_MENU_LOCAL_PRUNE_FAIL: ${e.message}")
        }
    }

    private fun recordPaperMenuAttemptFailure(slotKey: String, reason: String) {
        synchronized(paperMenuStateLock) {
            val state = loadPaperMenuState()
            if (state.optString("status") != "OPEN") return
            PaperMenuCapture.recordAttempt(state, slotKey, "FAILED", reason, currentIstIso())
            savePaperMenuState(state)
        }
        LogBuffer.add('W', TAG, "PAPER_MENU_CAPTURE_FAILED: slot=$slotKey reason=$reason")
    }

    /**
     * Item C (decision 3): broker margin for the PRIMARY and the REGISTERED_UNIFORM_PEER only. The poll's
     * margin cache (already filled by enrichTopCandidateMargin for up to MARGIN_SHADOW_CANDIDATE_CAP
     * candidates) is reused; at most PAPER_MENU_EXTRA_MARGIN_REQUESTS new broker requests are made.
     * Quantity is the dated authority lot recorded on the menu row (1 lot, product D, all legs).
     */
    private fun collectPaperMenuRoleMargins(menu: JSONObject, authToken: String): Pair<JSONObject, JSONObject> {
        val byCandidate = JSONObject()
        var extra = 0
        var reused = 0
        val rows = menu.optJSONArray("candidates") ?: JSONArray()
        for (i in 0 until rows.length()) {
            val row = rows.optJSONObject(i) ?: continue
            val role = row.optString("control_role", "")
            if (role != "PRIMARY" && role != "REGISTERED_UNIFORM_PEER") continue
            val id = row.optString("candidate_id")
            val lot = row.optInt("lot_size", 0)
            val payload = if (lot > 0) buildMarginPayload(JSONObject().put("lotSize", lot).put("legs", row.optJSONArray("legs") ?: JSONArray())) else null
            val entry = JSONObject()
            if (payload == null) {
                entry.put("origin", if (lot > 0) "PAYLOAD_UNAVAILABLE" else "LOT_AUTHORITY_UNRESOLVED")
            } else {
                val (cacheKey, instruments) = payload
                entry.put("instruments", instruments)
                val cached = marginQuoteCache[cacheKey]
                when {
                    cached != null -> { entry.put("origin", "POLL_CACHE_REUSE").put("quote", cached.toJson()); reused++ }
                    authToken.isBlank() -> entry.put("origin", "NO_BROKER_TOKEN")
                    extra >= PAPER_MENU_EXTRA_MARGIN_REQUESTS -> entry.put("origin", "EXTRA_REQUEST_CAP_REACHED")
                    else -> {
                        val q = fetchMarginQuote(authToken, instruments)
                        marginQuoteCache[cacheKey] = q
                        extra++
                        entry.put("origin", "EXTRA_REQUEST").put("quote", q.toJson())
                    }
                }
            }
            byCandidate.put(id, entry)
        }
        val stats = JSONObject()
            .put("extra_request_cap", PAPER_MENU_EXTRA_MARGIN_REQUESTS)
            .put("extra_requests", extra)
            .put("cache_reused", reused)
            .put("cache_poll_key", marginQuoteCachePollKey)
        return byCandidate to stats
    }

    private fun capturePaperDecisionMenu(
        slotKey: String,
        menu: JSONObject?,
        ctxObj: JSONObject,
        authToken: String,
        brainCompletedAtMs: Long
    ) {
        val now = currentIstIso()
        val state = loadPaperMenuState()
        if (state.optString("status") != "OPEN") return
        val unusable = PaperMenuCapture.usableMenu(state, menu)
        if (unusable != null) {
            recordPaperMenuAttemptFailure(slotKey, unusable)
            return
        }
        // R3: re-check before the expensive capture-side work (extra margin requests, file write). The
        // population must have been ranked in Paper AND the app must still be in Paper now.
        val modeNow = currentExecutionMode()
        val populationMode = menu!!.optJSONObject("manifest")?.optString("population_execution_mode", "") ?: ""
        if (!ExecutionModeResolver.isPaper(modeNow) || !ExecutionModeResolver.isPaper(populationMode)) {
            recordPaperMenuAttemptFailure(slotKey, "execution_mode_not_paper:now=$modeNow:population=$populationMode")
            return
        }
        val (margins, stats) = PaperMenuCapture.collectIfPaper(modeNow, populationMode) {
            collectPaperMenuRoleMargins(menu, authToken)
        } ?: return
        val enriched = PyTimeout.callWithTimeout(PY_PAPER_MENU_TIMEOUT_KEY, PY_PAPER_MENU_ENRICH_TIMEOUT_MS) {
            Python.getInstance().getModule("paper_menu_evidence")
                .callAttr("attach_margin_evidence_json", menu.toString(), margins.toString(), stats.toString())
                .toString()
        }
        if (enriched == null) {
            recordPaperMenuAttemptFailure(slotKey, "margin_attach_timeout")
            return
        }
        val frozen = JSONObject(enriched)
        val manifest = frozen.getJSONObject("manifest")
        manifest.put("brain_completed_at", SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssZ", Locale.US)
            .apply { timeZone = TimeZone.getTimeZone("Asia/Kolkata") }.format(Date(brainCompletedAtMs)))
        PaperMenuCapture.stampNativeVersion(manifest, BuildConfig.VERSION_NAME) // b503 R7 (Android NULL -> "null")
        manifest.put("execution_mode", ctxObj.optString("executionMode", "").ifBlank { JSONObject.NULL })
        synchronized(paperMenuStateLock) {
            // R2: freeze the bytes once into the durable per-menu outbox; every retry sends exactly these bytes.
            // R2-review Blocker B: an already-frozen capture for this session is kept (state reconciled to it);
            // the preference commit is checked and a failure is reported (the outbox evidence stays authoritative).
            val r = PaperMenuRecovery.commitCapture(paperMenuDir(), loadPaperMenuState(), slotKey, frozen, now,
                System.currentTimeMillis()) { st -> savePaperMenuState(st) }
            r.events.forEach { (level, msg) -> LogBuffer.add(level, TAG, msg) }
            if (r.status == "CAPTURED") {
                LogBuffer.add(
                    'I', TAG,
                    "PAPER_MENU_CAPTURED: menu=${manifest.optString("menu_id")} slot=$slotKey population=${manifest.optString("population_status")} rows=${manifest.optInt("expected_candidate_rows")} generated=${manifest.opt("generated_count")} control=${manifest.optJSONObject("control")?.optString("status")} marginExtra=${stats.optInt("extra_requests")} bytes=${r.payloadBytes} sha=${r.payloadSha256?.take(12)} state_saved=${r.stateSaved}"
                )
            }
        }
        launchPaperMenuUploadIfPending()
    }

    @Volatile
    private var paperMenuCollectorLogged = false

    /**
     * R4: the existing install credential (Pc2IngestCredential, same key as compact PC2). The server also
     * requires the key's hash to be the single designated collector (pdm_private.collectors). Only the hash
     * is ever logged, so the owner can register/designate it.
     */
    private fun paperMenuDeviceKey(): String? {
        val key = Pc2IngestCredential.loadOrCreate(
            read = { prefs.getString(Pc2IngestCredential.PREF_DEVICE_KEY, null) },
            write = { k -> prefs.edit().putString(Pc2IngestCredential.PREF_DEVICE_KEY, k).commit() }
        )
        if (key != null && !paperMenuCollectorLogged) {
            paperMenuCollectorLogged = true
            LogBuffer.add('I', TAG, "PAPER_MENU_COLLECTOR: key_hash=${Pc2IngestCredential.keyHash(key)} " +
                "(must be an active pc2_private.ingest_devices row AND the active pdm_private.collectors row)")
        }
        return key
    }

    /** R2: drains the durable per-menu outbox (any number of sessions, oldest first). */
    private fun launchPaperMenuUploadIfPending() {
        if (!paperMenuUploadInFlight.compareAndSet(false, true)) return
        serviceScope.launch(Dispatchers.IO) {
            try {
                val dir = paperMenuDir()
                if (!dir.isDirectory) return@launch
                val deviceKey = paperMenuDeviceKey()
                val drain = PaperMenuOutbox.drain(dir, todayIstDate(), System.currentTimeMillis()) { entry, text ->
                    if (deviceKey == null) return@drain PaperMenuOutbox.Delivery("RETRY", "DEVICE_CREDENTIAL_UNAVAILABLE")
                    val r = if (entry.kind == PaperMenuOutbox.KIND_CAPTURED) {
                        val frozen = JSONObject(text)
                        SupabaseClient.savePaperDecisionMenu(deviceKey, PaperMenuCapture.manifestRow(frozen, entry.payloadSha256),
                            PaperMenuCapture.canonicalCandidates(frozen).map { it.second }, entry.payloadSha256)
                    } else {
                        SupabaseClient.savePaperDecisionMenu(deviceKey, PaperMenuCapture.missedRow(JSONObject(text), entry.payloadSha256),
                            emptyList(), entry.payloadSha256)
                    }
                    LogBuffer.add(if (r.disposition == "ACKNOWLEDGED") 'I' else 'W', TAG,
                        "PAPER_MENU_UPLOAD: menu=${entry.menuId} kind=${entry.kind} disposition=${r.disposition} outcome=${r.outcome} completeness=${r.completeness ?: "unknown"}")
                    PaperMenuOutbox.Delivery(r.disposition, r.outcome, r.detail)
                }
                val summary = PaperMenuOutbox.summary(dir)
                summary.put("last_drain_storage_errors", org.json.JSONArray(drain.storageErrors))
                prefs.edit().putString(PAPER_MENU_OUTBOX_SUMMARY_KEY, summary.toString()).apply()
                drain.storageErrors.forEach { LogBuffer.add('E', TAG, "PAPER_MENU_STORAGE_ERROR: $it (delivery not counted as acknowledged)") }
                drain.reconcile.recovered.forEach { LogBuffer.add('W', TAG, "PAPER_MENU_OUTBOX_RECOVERED: $it (original frozen bytes kept)") }
                val clean = drain.retryPending == 0 && drain.exhausted == 0 && drain.retained == 0 && drain.storageErrors.isEmpty()
                LogBuffer.add(if (clean) 'I' else 'W', TAG,
                    "PAPER_MENU_OUTBOX: attempted=${drain.attempted} acked=${drain.acknowledged} retry_pending=${drain.retryPending} " +
                        "exhausted=${drain.exhausted} retained=${drain.retained} requeued=${drain.requeued} storage_errors=${drain.storageErrors.size} " +
                        "unacked=${summary.optJSONArray("unacked_menu_ids")}")
            } catch (e: Exception) {
                LogBuffer.add('W', TAG, "PAPER_MENU_UPLOAD_FAIL: ${e.message}")
            } finally {
                paperMenuUploadInFlight.set(false)
            }
        }
    }

    private suspend fun persistFetchedChainEvidenceBeforeBrain(
        bnfChain: JSONObject,
        nfChain: JSONObject
    ): Pair<JSONObject, JSONObject> {
        val pollTs = currentIstIso()
        val chainSliceRows = extractChainSlice(bnfChain, nfChain, pollTs)
        val chainRowsJson = JSONArray()
        chainSliceRows.forEach { chainRowsJson.put(it) }
        val persistTable = try {
            withContext(Dispatchers.IO) { SupabaseClient.saveChainRowsDetailed(chainRowsJson) }
        } catch (e: Exception) {
            Log.e(TAG, "ML_CHAIN_PRE_BRAIN_SAVE_EXCEPTION: ${e.message}", e)
            null
        }
        val saved = chainSliceRows.isEmpty() || persistTable != null
        LogBuffer.add(
            if (saved) 'I' else 'E',
            TAG,
            "ML_CHAIN_PRE_BRAIN_SAVE: rows=${chainSliceRows.size} saved=$saved table=${persistTable?.substringBefore('?') ?: "none"} pollTs=$pollTs"
        )
        // Evidence batch 1 (R1): one explicit schema (chain_evidence_ref_v1, ChainEvidenceRef.kt). poll_ts is
        // the exact join key written into every row; persist_status keeps a failed or fallback-table save
        // distinguishable from rows a quote reference can resolve; coverage lists the posted keys.
        return ChainEvidenceRef.build(pollTs, chainSliceRows, persistTable)
    }

    private fun extractChainSlice(
        bnfChain: JSONObject,
        nfChain: JSONObject,
        pollTs: String
    ): List<JSONObject> {
        val rows = mutableListOf<JSONObject>()
        val istTimeZone = TimeZone.getTimeZone("Asia/Kolkata")
        val sessionDate = SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = istTimeZone }.format(Date())
        appendFullChainSlice(rows, bnfChain, "BNF", sessionDate, pollTs)
        LogBuffer.add(
            'I',
            TAG,
            "ML_CHAIN_FETCH_EVIDENCE: rows=${rows.size} pollTs=$pollTs"
        )
        return rows
    }

    private fun appendFullChainSlice(
        rows: MutableList<JSONObject>,
        chainJson: JSONObject,
        indexKey: String,
        sessionDate: String,
        pollTs: String
    ) {
        val data = chainJson.optJSONArray("data") ?: return
        val defaultExpiry = chainJson.optString("expiry", "")
        for (i in 0 until data.length()) {
            val item = data.optJSONObject(i) ?: continue
            val strike = item.optDouble("strike_price", 0.0).toInt()
            if (strike <= 0) continue
            val expiry = item.optString("expiry", defaultExpiry)
            for (optionType in listOf("CE", "PE")) {
                val optKey = if (optionType == "CE") "call_options" else "put_options"
                val md = item.optJSONObject(optKey)?.optJSONObject("market_data") ?: continue
                val ltp = optionLtp(md)
                rows.add(JSONObject().apply {
                    put("poll_ts", pollTs)
                    put("session_date", sessionDate)
                    put("index_key", indexKey)
                    put("expiry", expiry)
                    put("strike", strike)
                    put("option_type", optionType)
                    put("ltp", ltp)
                    val option = item.optJSONObject(optKey) ?: JSONObject()
                    put("instrument_key", option.opt("instrument_key") ?: JSONObject.NULL)
                    put("market_data_json", JSONObject(md.toString()))
                    put("option_greeks_json", option.optJSONObject("option_greeks") ?: JSONObject.NULL)
                    put("source_received_at", pollTs)
                    putNullableFiniteNumber("bid", optionQuote(md, "bid_price"))
                    putNullableFiniteNumber("ask", optionQuote(md, "ask_price"))
                })
            }
        }
    }

    // b503: snapshot-stage health is owned by the application-level CaptureRuntime (one lock,
    // one prefs key) so a late finisher and the service record into the same counters. The
    // b502 wall-clock reservation key and single last_ml_persist_key were replaced by the
    // frozen poll.slot_key identity and the journal's bounded, monotonic completed-key set.
    private fun recordSnapshotStage(event: String, pollKey: String, elapsedMs: Long = 0,
                                    exceptionClass: String? = null) {
        try {
            CaptureRuntime.ensure(applicationContext).engine.recordHealth(event, pollKey, elapsedMs, exceptionClass)
        } catch (t: Throwable) {
            LogBuffer.add('E', TAG, "ML_SNAPSHOT_STAGE_HEALTH_FAIL: ${t.javaClass.simpleName}")
        }
    }

    private fun resolveStrikeFromSlice(chainJson: JSONObject, targetStrike: Int, optionType: String, indexKey: String): JSONObject? {
        val data = chainJson.optJSONArray("data") ?: return null
        for (i in 0 until data.length()) {
            val item = data.getJSONObject(i)
            val strike = item.optDouble("strike_price", 0.0).toInt()
            if (strike != targetStrike) continue
            val optKey = if (optionType.uppercase(Locale.US) == "CE") "call_options" else "put_options"
            val md = item.optJSONObject(optKey)?.optJSONObject("market_data") ?: continue
            val ltp = optionLtp(md)
            return JSONObject().apply {
                put("index", indexKey)
                put("ltp", ltp)
                putNullableFiniteNumber("bid", optionQuote(md, "bid_price"))
                putNullableFiniteNumber("ask", optionQuote(md, "ask_price"))
            }
        }
        return null
    }

    private fun processUnifiedBrainNotifications(brain: com.chaquo.python.PyObject, resultObj: JSONObject, ctxObj: JSONObject) {
        try {
            // H1: effective timeout (PyTimeout, shared poll key).
            val rawPayload = PyTimeout.callWithTimeout(PY_POLL_TIMEOUT_KEY, PY_AGENT_TIMEOUT_MS) {
                brain.callAttr(
                    "brain_notification_process",
                    resultObj.toString(),
                    ctxObj.toString()
                ).toString()
            }
            if (rawPayload == null) {
                Log.w(TAG, "BRAIN_NOTIFICATION_TIMEOUT: brain_notification_process exceeded ${PY_AGENT_TIMEOUT_MS}ms")
                return
            }
            if (rawPayload == "null" || rawPayload.isBlank()) return

            val payload = JSONObject(rawPayload)
            val brainNotification = payload.optJSONObject("brain_notification")
            if (brainNotification != null) {
                resultObj.put("brain_notification", brainNotification)
                LogBuffer.add(
                    'I',
                    TAG,
                    "BRAIN_NOTIFICATION_CONTRACT: type=${brainNotification.optString("decision_type")} notify=${brainNotification.optBoolean("notify_user", false)} reason=${brainNotification.optString("reason_code")} key=${brainNotification.optString("alert_key")}"
                )
            }

            val agentState = payload.optJSONObject("agent_state")

            val transportMode = prefs.getString(PREF_NOTIFICATION_TRANSPORT_MODE, "live") ?: "live"
            val brainNotifications = payload.optJSONArray("brain_notifications")
            var selectedCount = 0
            var attemptedCount = 0
            var postedCount = 0
            val postedContracts = JSONArray()
            val outcomes = linkedMapOf<String, Int>()
            fun deliver(contract: JSONObject?) {
                if (contract == null || !contract.optBoolean("notify_user", false)) return
                selectedCount += 1
                val delivery = dispatchUnifiedBrainNotification(contract, transportMode)
                if (delivery.attempted) attemptedCount += 1
                outcomes[delivery.outcome] = (outcomes[delivery.outcome] ?: 0) + 1
                if (delivery.postedToOs) {
                    postedCount += 1
                    postedContracts.put(contract)
                }
            }
            if (brainNotifications != null && brainNotifications.length() > 0) {
                for (i in 0 until brainNotifications.length()) {
                    deliver(brainNotifications.optJSONObject(i))
                }
            } else {
                deliver(brainNotification)
            }
            val acknowledgedState = if (postedContracts.length() > 0) {
                try {
                    JSONObject(brain.callAttr("brain_notification_ack_deliveries", postedContracts.toString()).toString())
                } catch (t: Throwable) {
                    Log.w(TAG, "BRAIN_NOTIFICATION_ACK_FAIL: ${t.message}")
                    agentState
                }
            } else {
                agentState
            }
            if (acknowledgedState != null) {
                prefs.edit().putString("notification_agent_state", acknowledgedState.toString()).commit()
            }
            val transportMeta = JSONObject().apply {
                put("mode", transportMode)
                put("selected_count", selectedCount)
                put("attempted_count", attemptedCount)
                put("posted_to_os_count", postedCount)
                put("dispatched", postedCount > 0)
                put("delivery_outcomes", JSONObject().apply {
                    outcomes.forEach { (outcome, count) -> put(outcome, count) }
                })
                put("contract_count", brainNotifications?.length() ?: if (brainNotification != null) 1 else 0)
                put("notify_user", brainNotification?.optBoolean("notify_user", false) ?: false)
                put("decision_type", brainNotification?.optString("decision_type", "WAIT") ?: "WAIT")
                put("reason_code", brainNotification?.optString("reason_code", "") ?: "")
                put("updated_at_ms", System.currentTimeMillis())
            }
            resultObj.put("brain_notification_meta", transportMeta)
            prefs.edit()
                .putString(PREF_LAST_BRAIN_NOTIFICATION, brainNotification?.toString() ?: "null")
                .putString(PREF_LAST_BRAIN_NOTIFICATION_META, transportMeta.toString())
                .commit()
            LogBuffer.add(
                'I',
                TAG,
                "BRAIN_NOTIFICATION_MODE: mode=$transportMode selected=$selectedCount attempted=$attemptedCount posted=$postedCount outcomes=$outcomes notify=${brainNotification?.optBoolean("notify_user", false)} type=${brainNotification?.optString("decision_type")}"
            )
        } catch (e: Exception) {
            Log.w(TAG, "BRAIN_NOTIFICATION_FAIL: ${e.message}")
        }
    }

    private fun dispatchUnifiedBrainNotification(
        contract: JSONObject?,
        transportMode: String
    ): NotificationHelper.DeliveryResult {
        if (contract == null || !contract.optBoolean("notify_user", false)) {
            return NotificationHelper.DeliveryResult(false, false, "NOT_SELECTED")
        }
        if (transportMode != "live") {
            LogBuffer.add(
                'I',
                TAG,
                "BRAIN_NOTIFICATION_SHADOW: type=${contract.optString("decision_type")} key=${contract.optString("alert_key")}"
            )
            return NotificationHelper.DeliveryResult(false, false, "SHADOW_MODE")
        }
        val notifType = notificationTypeFrom(
            soundClass = contract.optString("sound_class", ""),
            urgency = contract.optString("decision_type", "INFO")
        )
        val channel = if (contract.optString("decision_type").startsWith("POSITION")) "positions" else null
        if (channel != null) {
            return NotificationHelper.send(
                this,
                contract.optString("title"),
                contract.optString("body"),
                notifType,
                channel
            )
        } else {
            return NotificationHelper.send(
                this,
                contract.optString("title"),
                contract.optString("body"),
                notifType
            )
        }
    }

    private fun notificationTypeFrom(soundClass: String, urgency: String): String {
        return when {
            soundClass.isNotBlank() -> soundClass
            urgency == "HIGH" || urgency == "TRADE" -> "entry"
            urgency == "WARNING" -> "warning"
            urgency == "UPDATE" || urgency == "POSITION_UPDATE" -> "update"
            urgency == "ERROR" || urgency.startsWith("POSITION") -> "urgent"
            urgency == "INFO" -> "routine"
            else -> "routine"
        }
    }

    private fun currentIstMonthKey(): String = todayIstDate().take(7)

    private fun parseJsonArraySafe(raw: String?): JSONArray {
        return try {
            JSONArray(raw ?: "[]")
        } catch (_: Exception) {
            JSONArray()
        }
    }

    private fun loadSignalReliabilityRows(): JSONArray {
        val currentMonth = currentIstMonthKey()
        val cachedMonth = prefs.getString(SIGNAL_RELIABILITY_CACHE_MONTH_KEY, "") ?: ""
        val cachedRowsRaw = prefs.getString(SIGNAL_RELIABILITY_CACHE_KEY, "[]")
        val cachedRows = parseJsonArraySafe(cachedRowsRaw)
        if (cachedMonth == currentMonth && cachedRows.length() > 0) {
            return cachedRows
        }

        return try {
            val freshRows = SupabaseClient.getSignalReliabilityRows()
            if (freshRows.length() > 0) {
                prefs.edit()
                    .putString(SIGNAL_RELIABILITY_CACHE_KEY, freshRows.toString())
                    .putString(SIGNAL_RELIABILITY_CACHE_MONTH_KEY, currentMonth)
                    .apply()
                freshRows
            } else {
                cachedRows
            }
        } catch (e: Exception) {
            Log.w(TAG, "SIGNAL_RELIABILITY_FETCH_FAIL: ${e.message}")
            cachedRows
        }
    }

    private fun handoffElephantObserveOnly(payload: JSONObject): Boolean {
        val request = Request.Builder()
            .url("$ELEPHANT_BASE_URL/elephant")
            .addHeader("Accept", "application/json")
            .post(payload.toString().toRequestBody("application/json".toMediaTypeOrNull()))
            .build()

        return try {
            elephantHandoffClient.newCall(request).execute().use { response -> response.isSuccessful }
        } catch (e: Exception) {
            false
        }
    }

    private fun launchElephantObserveOnly(resultObj: JSONObject) {
        val factPack = resultObj.optJSONObject("elephant_fact_pack") ?: return
        if (!factPack.optBoolean("observe_only", false)) return
        val candidates = factPack.optJSONArray("candidates") ?: return
        if (candidates.length() == 0) return
        val pollTimestamp = factPack.optString("poll_timestamp", "").trim()
        if (pollTimestamp.isBlank()) return

        val grouped = linkedMapOf<String, MutableList<JSONObject>>()
        for (i in 0 until candidates.length()) {
            val candidate = candidates.optJSONObject(i) ?: continue
            val lane = candidate.optString("lane", "").trim()
            if (lane.isBlank()) continue
            grouped.getOrPut(lane) { mutableListOf() }.add(JSONObject(candidate.toString()))
        }
        if (grouped.isEmpty()) return

        for ((lane, laneCandidates) in grouped) {
            serviceScope.launch(Dispatchers.IO) {
                try {
                    val laneCandidatePayload = JSONArray()
                    laneCandidates.forEach { laneCandidatePayload.put(it) }
                    val lanePayload = JSONObject().apply {
                        put("poll_timestamp", pollTimestamp)
                        put("session_date", factPack.optString("session_date"))
                        put("lane", lane)
                        put("observe_only", true)
                        put("quality_tag", factPack.optString("quality_tag", "qualitative_prompt_v2"))
                        put("trade_mode", factPack.optString("trade_mode"))
                        put("decision_source", factPack.optString("decision_source"))
                        put("market_context", factPack.optJSONObject("market_context") ?: JSONObject())
                        put("verdict_context", factPack.optJSONObject("verdict_context") ?: JSONObject())
                        put("signal_independence", factPack.optJSONObject("signal_independence") ?: JSONObject())
                        put("coherence_signal", factPack.optJSONObject("coherence_signal") ?: JSONObject())
                        put("candidate_counts", factPack.optJSONObject("candidate_counts") ?: JSONObject())
                        put("candidates", laneCandidatePayload)
                    }
                    // b503 (G): timing/call-path evidence only. Observe-only research handoff;
                    // no order, broker or Real meaning. Request semantics unchanged.
                    val handoffStartedMs = System.currentTimeMillis()
                    val captureOutstanding = runCatching { CaptureRuntime.coordinator.isComputationOutstanding() }.getOrDefault(false)
                    val accepted = handoffElephantObserveOnly(lanePayload)
                    LogBuffer.add(
                        if (accepted) 'I' else 'W',
                        TAG,
                        "ELEPHANT_HANDOFF: lane=$lane accepted=$accepted candidates=${laneCandidates.size} " +
                            "elapsedMs=${System.currentTimeMillis() - handoffStartedMs} pollTs=$pollTimestamp " +
                            "captureComputationOutstandingAtStart=$captureOutstanding"
                    )
                } catch (e: Exception) {
                    Log.w(TAG, "ELEPHANT_OBSERVE_ONLY_FAIL lane=$lane: ${e.message}")
                }
            }
        }
    }

    private fun scheduleBnfUniverseCapture(day: String, token: String) {
        if (!bnfUniverseInProgress.compareAndSet(false, true)) return
        serviceScope.launch(Dispatchers.IO) {
            val pollStarted = System.currentTimeMillis()
            fun saveCoverage(coverage: JSONObject) {
                require(BnfRawCapture.record(filesDir, "marketradar://capture/coverage",
                    pollStarted, System.currentTimeMillis(), 200, coverage.toString().toByteArray(Charsets.UTF_8))) {
                    "COVERAGE_QUEUE_FAILED"
                }
                val dir = File(filesDir, BnfRawCapture.DIRECTORY).apply { mkdirs() }
                val marker = File(dir, "${day}_${pollStarted}_universe_coverage.json")
                val tmp = File.createTempFile("coverage_", ".tmp", dir)
                try {
                    tmp.writeText(coverage.toString())
                    require(tmp.renameTo(marker)) { "COVERAGE_SAVE_FAILED" }
                } finally { tmp.delete() }
                CloudRecovery.request(applicationContext)
            }
            try {
                val reference = runCatching { BnfReferenceCapture.daily(filesDir, day) }.getOrNull()
                val monthlyStatus = runCatching { BnfReferenceCapture.monthly(filesDir, day); "PUBLICATION_CHECKED" }
                    .getOrElse { "REFERENCE_CHECK_FAILED" }
                val key = URLEncoder.encode("NSE_INDEX|Nifty Bank", Charsets.UTF_8.name())
                val listing = fetchSync("https://api.upstox.com/v2/option/contract?instrument_key=$key", token)
                val contracts = listing?.optJSONArray("data")
                val expiries = sortedSetOf<String>()
                val optionKeys = sortedSetOf<String>()
                if (contracts != null) for (i in 0 until contracts.length()) {
                    val contract = contracts.optJSONObject(i) ?: continue
                    contract.optString("expiry").takeIf {
                        it.length == 10 && it >= day && parseExpiryDate(it) != null
                    }?.let(expiries::add)
                    contract.optString("instrument_key").takeIf { it.startsWith("NSE_FO|") }
                        ?.let(optionKeys::add)
                }
                val listedKeys = optionKeys.toSet()
                val masterOptions = reference?.optJSONArray("options") ?: JSONArray()
                val masterKeys = (0 until masterOptions.length()).map { masterOptions.getJSONObject(it).getString("instrument_key") }.toSet()
                for (mi in 0 until masterOptions.length()) {
                    BnfReferenceCapture.expiryDate(masterOptions.getJSONObject(mi).opt("expiry"))
                        ?.takeIf { it >= day }?.let(expiries::add)
                }
                optionKeys.addAll(masterKeys)
                var observedSpot = 0.0
                val chainKeys = sortedSetOf<String>()
                val coverage = JSONObject().put("schema", "bnf_universe_daily_v1")
                    .put("instrument_master_status", if (reference == null) "MISSING" else "RECEIVED")
                    .put("monthly_weights_status", monthlyStatus)
                    .put("master_option_count", masterKeys.size)
                    .put("master_only_keys", JSONArray((masterKeys - listedKeys).sorted()))
                    .put("listing_only_keys", JSONArray((listedKeys - masterKeys).sorted()))
                    .put("day", day).put("poll_started_ms", pollStarted).put("cadence", "each_poll_when_previous_complete")
                    .put("contract_count", contracts?.length() ?: 0)
                    .put("expected_expiries", JSONArray(expiries.toList()))
                val results = JSONArray()
                for (expiry in expiries) {
                    delay(150) // independent of the decision poll; requests remain bounded
                    val encodedExpiry = URLEncoder.encode(expiry, Charsets.UTF_8.name())
                    val chain = fetchSync("https://api.upstox.com/v2/option/chain?instrument_key=$key&expiry_date=$encodedExpiry", token)
                    val chainRows = chain?.optJSONArray("data")
                    if (chainRows != null && chainRows.length() > 0) observedSpot = chainRows.getJSONObject(0).optDouble("underlying_spot_price", observedSpot)
                    if (chainRows != null) for (ci in 0 until chainRows.length()) {
                        val cr = chainRows.optJSONObject(ci) ?: continue
                        for (side in listOf("call_options", "put_options"))
                            cr.optJSONObject(side)?.optString("instrument_key")?.takeIf { it.startsWith("NSE_FO|") }?.let(chainKeys::add)
                    }
                    val row = JSONObject().put("expiry", expiry)
                        .put("chain_status", if (chain?.optJSONArray("data") == null) "MISSING_DATA" else "RECEIVED")
                        .put("chain_rows", chain?.optJSONArray("data")?.length() ?: 0)
                    for ((label, suffix) in listOf(
                        "max_pain" to "max-pain?instrument_key=$key&expiry=$encodedExpiry&date=$day&bucket_interval=60",
                        "oi" to "oi?instrument_key=$key&expiry=$encodedExpiry&date=$day",
                        "change_oi" to "change-oi?instrument_key=$key&expiry=$encodedExpiry&date=$day&interval=2",
                        "pcr" to "pcr?instrument_key=$key&expiry=$encodedExpiry&date=$day&bucket_interval=60")) {
                        delay(150)
                        val response = fetchSync("https://api.upstox.com/v2/market/$suffix", token)
                        row.put("${label}_status", when {
                            response == null -> "REQUEST_FAILED"
                            response.isNull("data") -> "UPSTOX_DATA_NULL"
                            else -> "RECEIVED"
                        })
                    }
                    results.put(row)
                }
                coverage.put("chain_contract_count", chainKeys.size)
                    .put("chain_omitted_keys", JSONArray((optionKeys - chainKeys).sorted()))
                    .put("iv_units", JSONObject().put("v2_chain", "PERCENT").put("v3_option_greek", "DECIMAL"))
                // The full quote endpoint accepts at most 500 keys. Query the entire discovered
                // option universe, including contracts absent from the option-chain response.
                val quoteBatches = JSONArray()
                optionKeys.toList().chunked(250).forEachIndexed { index, keys ->
                    delay(150)
                    val joined = URLEncoder.encode(keys.joinToString(","), Charsets.UTF_8.name())
                    val response = fetchSync("https://api.upstox.com/v3/market-quote/quotes?instrument_key=$joined", token)
                    quoteBatches.put(JSONObject().put("batch", index).put("requested", keys.size)
                        .put("received", response?.optJSONObject("data")?.length() ?: 0)
                        .put("status", if (response?.optJSONObject("data")?.length() == keys.size) "RECEIVED" else "INCOMPLETE"))
                }
                val greekBatches = JSONArray()
                optionKeys.toList().chunked(50).forEachIndexed { index, keys ->
                    delay(150)
                    val joined = URLEncoder.encode(keys.joinToString(","), Charsets.UTF_8.name())
                    val response = fetchSync("https://api.upstox.com/v3/market-quote/option-greek?instrument_key=$joined", token)
                    greekBatches.put(JSONObject().put("batch", index).put("requested", keys.size)
                        .put("received", response?.optJSONObject("data")?.length() ?: 0)
                        .put("status", if (response?.optJSONObject("data")?.length() == keys.size) "RECEIVED" else "INCOMPLETE"))
                }
                val members = reference?.optJSONArray("members") ?: JSONArray()
                val futures = reference?.optJSONArray("futures") ?: JSONArray()
                val contextKeys = (0 until members.length()).map { members.getJSONObject(it).getString("instrument_key") } +
                    listOf("NSE_INDEX|Nifty Bank", "NSE_INDEX|Nifty 50", "NSE_INDEX|India VIX") +
                    (0 until futures.length()).map { futures.getJSONObject(it).getString("instrument_key") }
                val contractRows = (0 until (contracts?.length() ?: 0)).mapNotNull { contracts?.optJSONObject(it) }
                val front = expiries.firstOrNull()
                val nearOptions = if (observedSpot > 0) contractRows.filter { it.optString("expiry") == front }
                    .sortedBy { kotlin.math.abs(it.optDouble("strike_price", 0.0) - observedSpot) }
                    .take(4).map { it.getString("instrument_key") } else emptyList()
                bnfWebSocket.ensure(token, contextKeys + nearOptions)
                coverage.put("websocket_subscription_keys", JSONArray(contextKeys + nearOptions))
                    .put("websocket_option_count", nearOptions.size)
                val contextKey = URLEncoder.encode(contextKeys.joinToString(","), Charsets.UTF_8.name())
                val context = fetchSync("https://api.upstox.com/v3/market-quote/quotes?instrument_key=$contextKey", token)
                coverage.put("context_requested", contextKeys.size)
                    .put("context_received", context?.optJSONObject("data")?.length() ?: 0)
                    .put("context_manifest_as_of", reference?.optString("day") ?: JSONObject.NULL)
                    .put("quote_batches", quoteBatches)
                    .put("greek_batches", greekBatches)
                val nfoStatus = fetchSync("https://api.upstox.com/v2/market/status/NFO", token)
                val nseStatus = fetchSync("https://api.upstox.com/v2/market/status/NSE", token)
                val timings = fetchSync("https://api.upstox.com/v2/market/timings/$day", token)
                coverage.put("nfo_status", nfoStatus?.optJSONObject("data")?.optString("status") ?: "MISSING")
                    .put("nse_status", nseStatus?.optJSONObject("data")?.optString("status") ?: "MISSING")
                    .put("timings_status", if (timings?.optJSONArray("data") == null) "MISSING" else "RECEIVED")
                val fii = fetchSync("https://api.upstox.com/v2/market/fii?data_type=NSE_EQ%7CCASH,NSE_FO%7CINDEX_FUTURES,NSE_FO%7CINDEX_OPTIONS&interval=1D", token)
                val dii = fetchSync("https://api.upstox.com/v2/market/dii?data_type=NSE_EQ%7CCASH&interval=1D", token)
                coverage.put("fii_status", if (fii == null || fii.isNull("data")) "MISSING_DATA" else "RECEIVED")
                    .put("dii_status", if (dii == null || dii.isNull("data")) "MISSING_DATA" else "RECEIVED")
                coverage.put("results", results)
                    .put("status", if (reference == null || masterKeys != listedKeys || members.length() == 0 || futures.length() == 0 ||
                        monthlyStatus != "PUBLICATION_CHECKED" || nearOptions.size != 4 || contracts == null || expiries.isEmpty() ||
                        quoteBatches.length() == 0 ||
                        (0 until quoteBatches.length()).any { quoteBatches.getJSONObject(it).getString("status") != "RECEIVED" } ||
                        greekBatches.length() == 0 ||
                        (0 until greekBatches.length()).any { greekBatches.getJSONObject(it).getString("status") != "RECEIVED" } ||
                        context?.optJSONObject("data")?.length() != contextKeys.size ||
                        nfoStatus?.optJSONObject("data") == null || nseStatus?.optJSONObject("data") == null ||
                        timings?.optJSONArray("data") == null ||
                        (0 until results.length()).any { rowIndex ->
                            val row = results.getJSONObject(rowIndex)
                            listOf("chain", "max_pain", "oi", "change_oi", "pcr")
                                .any { row.optString("${it}_status") != "RECEIVED" }
                        })
                        "INCOMPLETE" else "RECEIVED_UNVERIFIED")
                // A local marker does not imply a source-table upload or verified readback.
                saveCoverage(coverage)
            } catch (e: Exception) {
                LogBuffer.add('E', TAG, "BNF_UNIVERSE_CAPTURE_FAILED: ${e.javaClass.simpleName}")
                try {
                    saveCoverage(JSONObject().put("schema", "bnf_universe_daily_v1")
                        .put("day", day).put("poll_started_ms", pollStarted)
                        .put("status", "INCOMPLETE").put("error", e.javaClass.simpleName))
                } catch (_: Exception) { LogBuffer.add('E', TAG, "BNF_COVERAGE_SAVE_FAILED") }
            } finally { bnfUniverseInProgress.set(false) }
        }
    }

    private fun fetchSync(url: String, token: String): JSONObject? {
        val startedMs = System.currentTimeMillis()
        val request = Request.Builder()
            .url(url)
            .addHeader("Authorization", "Bearer $token")
            .addHeader("Accept", "application/json")
            .build()

        return try {
            client.newCall(request).execute().use { response ->
                val body = response.body?.bytes()
                if (BnfRawCapture.eligible(url) && !BnfRawCapture.record(
                        filesDir, url, startedMs, System.currentTimeMillis(), response.code, body)) {
                    LogBuffer.add('E', TAG, "BNF_RAW_CAPTURE_FAILED: ${url.substringBefore('?')} status=${response.code}")
                }
                if (!response.isSuccessful) {
                    if (response.code == 401) {
                        token401Counter++
                        if (token401Counter == 3) {
                            // WS26: Only notify once on the 3rd fail to avoid spam
                            NotificationHelper.send(this, "🔑 Auth Expired", "Upstox token invalid. Open app to refresh.", "urgent")
                            stopPolling()
                        }
                    }
                    null
                } else {
                    token401Counter = 0
                    JSONObject(body?.toString(Charsets.UTF_8) ?: "{}")
                }
            }
        } catch (e: Exception) {
            if (BnfRawCapture.eligible(url)) BnfRawCapture.record(
                filesDir, url, startedMs, System.currentTimeMillis(), 0, null, e.javaClass.simpleName)
            null
        }
    }

    private fun resetMarginQuoteCacheIfNeeded(pollKey: String) {
        if (pollKey == marginQuoteCachePollKey) return
        marginQuoteCachePollKey = pollKey
        marginQuoteCache.clear()
    }

    private fun candidateKey(candidate: JSONObject): String =
        candidate.optString("id", "").ifBlank { candidate.optString("candidate_id", "") }.trim()

    private fun isMarginQuoteCandidate(candidate: JSONObject): Boolean {
        if (candidate.optBoolean("capitalBlocked", false)) return false
        if (candidate.has("executionReady") && !candidate.optBoolean("executionReady", true)) return false
        if (candidate.optBoolean("blocked", false)) return false
        if (candidate.optString("entryAction", "").equals("BLOCKED", ignoreCase = true)) return false
        if (candidate.has("directionSafe") && !candidate.optBoolean("directionSafe", true)) return false
        if (candidateKey(candidate).isBlank()) return false
        if (candidate.optString("type", "").isBlank()) return false
        return true
    }

    private fun collectMarginShadowCandidates(resultObj: JSONObject, cap: Int): List<JSONObject> {
        if (cap <= 0) return emptyList()
        val selected = mutableListOf<JSONObject>()
        val seen = linkedSetOf<String>()
        val arrays = listOf("watchlist", "generated_candidates", "candidates")
        for (name in arrays) {
            val arr = resultObj.optJSONArray(name) ?: continue
            for (i in 0 until arr.length()) {
                if (selected.size >= cap) return selected
                val candidate = arr.optJSONObject(i) ?: continue
                if (!isMarginQuoteCandidate(candidate)) continue
                val id = candidateKey(candidate)
                if (!seen.add(id)) continue
                selected.add(candidate)
            }
        }
        return selected
    }

    private fun buildMarginPayload(candidate: JSONObject): Pair<String, JSONArray>? {
        val lotSize = candidate.optInt("lotSize", 0)
        if (lotSize <= 0) return null
        val legs = candidate.optJSONArray("legs") ?: return null
        if (legs.length() == 0) return null

        data class PendingLeg(var action: String, var quantity: Int)
        val deduped = LinkedHashMap<String, PendingLeg>()

        for (i in 0 until legs.length()) {
            val leg = legs.optJSONObject(i) ?: continue
            val instrumentKey = leg.optString("instrument_key", "").trim()
            val action = leg.optString("action", "").trim().uppercase(Locale.US)
            if (instrumentKey.isBlank() || action !in setOf("BUY", "SELL")) {
                return null
            }
            val existing = deduped[instrumentKey]
            if (existing == null) {
                deduped[instrumentKey] = PendingLeg(action, lotSize)
            } else if (existing.action == action) {
                existing.quantity += lotSize
            } else {
                return null
            }
        }

        if (deduped.isEmpty() || deduped.size > 20) return null

        val instruments = JSONArray()
        val cacheKeyParts = mutableListOf<String>()
        for ((instrumentKey, leg) in deduped) {
            instruments.put(
                JSONObject()
                    .put("instrument_key", instrumentKey)
                    .put("quantity", leg.quantity)
                    .put("transaction_type", leg.action)
                    .put("product", "D")
            )
            cacheKeyParts += "${instrumentKey}:${leg.action}:${leg.quantity}:D"
        }
        return cacheKeyParts.joinToString("|") to instruments
    }

    private fun fetchMarginQuote(token: String, instruments: JSONArray): MarginQuote {
        val quotedAt = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssXXX", Locale.US).format(Date())
        val body = JSONObject().put("instruments", instruments).toString()
        val request = Request.Builder()
            .url(UPSTOX_MARGIN_URL)
            .addHeader("Authorization", "Bearer $token")
            .addHeader("Accept", "application/json")
            .addHeader("Content-Type", "application/json")
            .post(body.toRequestBody("application/json".toMediaTypeOrNull()))
            .build()
        return try {
            client.newCall(request).execute().use { response ->
                val raw = response.body?.string().orEmpty()
                val parsed = if (raw.isBlank()) JSONObject() else JSONObject(raw)
                if (!response.isSuccessful) {
                    val err = parsed.optString("message").ifBlank {
                        parsed.optString("errors").ifBlank { "http_${response.code}" }
                    }
                    return MarginQuote(
                        status = "ERROR",
                        requestUrl = UPSTOX_MARGIN_URL,
                        quotedAt = quotedAt,
                        error = err
                    )
                }
                val data = parsed.optJSONObject("data") ?: JSONObject()
                val margins = data.optJSONArray("margins") ?: JSONArray()
                var span = 0.0
                var exposure = 0.0
                var netBuyPremium = 0.0
                for (i in 0 until margins.length()) {
                    val row = margins.optJSONObject(i) ?: continue
                    span += row.optDouble("span_margin", 0.0)
                    exposure += row.optDouble("exposure_margin", 0.0)
                    netBuyPremium += row.optDouble("net_buy_premium", 0.0)
                }
                MarginQuote(
                    status = parsed.optString("status", "success").ifBlank { "success" },
                    requestUrl = UPSTOX_MARGIN_URL,
                    requiredMargin = data.optDouble("required_margin", Double.NaN).takeIf { it.isFinite() },
                    finalMargin = data.optDouble("final_margin", Double.NaN).takeIf { it.isFinite() },
                    spanMargin = span,
                    exposureMargin = exposure,
                    netBuyPremium = netBuyPremium,
                    marginCount = margins.length(),
                    quotedAt = quotedAt
                )
            }
        } catch (e: Exception) {
            MarginQuote(
                status = "ERROR",
                requestUrl = UPSTOX_MARGIN_URL,
                quotedAt = quotedAt,
                error = e.message ?: e.javaClass.simpleName
            )
        }
    }

    private fun annotateMarginQuote(candidate: JSONObject, quote: MarginQuote) {
        candidate.put("marginQuoteStatus", quote.status)
        candidate.put("marginQuoteSource", "UPSTOX_MARGIN_API")
        candidate.put("marginModelVersion", "margin_contract_v1")
        candidate.put("marginRequestUrl", quote.requestUrl)
        quote.quotedAt?.let { candidate.put("marginQuotedAt", it) }
        quote.error?.let { candidate.put("marginQuoteError", it) }
        if (quote.requiredMargin != null) candidate.put("upstoxRequiredMargin", quote.requiredMargin)
        if (quote.finalMargin != null) {
            candidate.put("upstoxFinalMargin", quote.finalMargin)
            candidate.put("realMargin", quote.finalMargin)
        }
        val normalizedMargin = quote.finalMargin ?: quote.requiredMargin
        if (normalizedMargin != null && normalizedMargin > 0.0) {
            candidate.put("marginRequired", normalizedMargin)
            candidate.put("marginForSizing", normalizedMargin)
            candidate.put("marginSource", "UPSTOX_MARGIN_API")
            candidate.put("marginFallbackUsed", false)
            candidate.put("marginSizingBehavior", "EVIDENCE_ONLY_DO_NOT_RANK")
        }
        if (quote.spanMargin != null) candidate.put("upstoxSpanMargin", quote.spanMargin)
        if (quote.exposureMargin != null) candidate.put("upstoxExposureMargin", quote.exposureMargin)
        if (quote.netBuyPremium != null) candidate.put("upstoxNetBuyPremium", quote.netBuyPremium)
        candidate.put("marginQuote", quote.toJson())
    }

    private fun annotateMarginQuoteSkipped(candidate: JSONObject, reason: String) {
        candidate.put("marginQuoteStatus", "SKIPPED")
        candidate.put("marginQuoteError", reason)
        candidate.put("marginSizingBehavior", "EVIDENCE_ONLY_DO_NOT_RANK")
        if (!candidate.has("marginSource")) candidate.put("marginSource", "STATIC_FALLBACK")
        if (!candidate.has("marginFallbackUsed")) candidate.put("marginFallbackUsed", true)
    }

    private fun annotateMatchingCandidates(resultObj: JSONObject, candidateId: String, quote: MarginQuote) {
        val arrays = listOf("watchlist", "generated_candidates", "candidates")
        for (name in arrays) {
            val arr = resultObj.optJSONArray(name) ?: continue
            for (i in 0 until arr.length()) {
                val candidate = arr.optJSONObject(i) ?: continue
                if (candidate.optString("id", "") == candidateId) {
                    annotateMarginQuote(candidate, quote)
                }
            }
        }
    }

    private fun enrichTopCandidateMargin(resultObj: JSONObject, ctxObj: JSONObject, authToken: String, pollKey: String) {
        resetMarginQuoteCacheIfNeeded(pollKey)
        if (authToken.isBlank()) {
            LogBuffer.add('W', TAG, "MARGIN_SKIP: token missing for read-only quote")
            return
        }

        val marginCandidates = collectMarginShadowCandidates(resultObj, MARGIN_SHADOW_CANDIDATE_CAP)
        if (marginCandidates.isEmpty()) {
            LogBuffer.add('I', TAG, "MARGIN_SKIP: no actionable candidate surfaced for shadow quote")
            return
        }

        val marginByCandidate = ctxObj.optJSONObject("marginByCandidate") ?: JSONObject()
        val summary = JSONObject()
            .put("mode", "shadow_evidence_only")
            .put("candidate_cap", MARGIN_SHADOW_CANDIDATE_CAP)
            .put("candidate_count", marginCandidates.size)
            .put("quoted", 0)
            .put("errors", 0)
            .put("skipped", 0)
            .put("cache_size", marginQuoteCache.size)
            .put("capital_gate_behavior", "UNCHANGED_MAX_LOSS_BASED")

        var topMarginJson: JSONObject? = null
        for (candidate in marginCandidates) {
            val candidateId = candidateKey(candidate)
            val payload = buildMarginPayload(candidate)
            if (payload == null) {
                annotateMarginQuoteSkipped(candidate, "candidate_missing_margin_inputs")
                summary.put("skipped", summary.optInt("skipped") + 1)
                continue
            }

            val (cacheKey, instruments) = payload
            val quote = marginQuoteCache.getOrPut(cacheKey) {
                fetchMarginQuote(authToken, instruments)
            }
            annotateMarginQuote(candidate, quote)
            annotateMatchingCandidates(resultObj, candidateId, quote)

            val marginJson = quote.toJson().apply {
                put("candidate_id", candidateId)
                put("candidate_type", candidate.optString("type"))
                put("candidate_index", candidate.optString("index"))
                put("brain_max_loss", candidate.optDouble("maxLoss", Double.NaN).takeIf { it.isFinite() })
                put("lot_size", candidate.optInt("lotSize", 0))
                put("instrument_count", instruments.length())
                put("request_payload", JSONObject().put("instruments", instruments))
                put("capital_gate_behavior", "UNCHANGED_MAX_LOSS_BASED")
            }
            marginByCandidate.put(candidateId, marginJson)
            if (topMarginJson == null) topMarginJson = marginJson
            if (quote.status == "ERROR") {
                summary.put("errors", summary.optInt("errors") + 1)
            } else {
                summary.put("quoted", summary.optInt("quoted") + 1)
            }
        }

        ctxObj.put("marginByCandidate", marginByCandidate)
        topMarginJson?.let {
            ctxObj.put("topCandidateMargin", it)
            resultObj.put("top_candidate_margin", it)
        }
        summary.put("cache_size", marginQuoteCache.size)
        ctxObj.put("marginShadowSummary", summary)
        resultObj.put("margin_shadow_summary", summary)

        LogBuffer.add(
            if (summary.optInt("errors") > 0) 'W' else 'I',
            TAG,
            buildString {
                append("[margin_shadow] candidates=").append(summary.optInt("candidate_count"))
                append(" quoted=").append(summary.optInt("quoted"))
                append(" errors=").append(summary.optInt("errors"))
                append(" skipped=").append(summary.optInt("skipped"))
                append(" cache=").append(summary.optInt("cache_size"))
                append(" behavior=").append(summary.optString("capital_gate_behavior"))
            }
        )
    }

    private fun parseExpiryDate(rawExpiry: String): Date? {
        val trimmed = rawExpiry.trim()
        if (!Regex("\\d{4}-\\d{2}-\\d{2}").matches(trimmed)) return null
        return try {
            SimpleDateFormat("yyyy-MM-dd", Locale.US).apply {
                isLenient = false
                timeZone = TimeZone.getTimeZone("Asia/Kolkata")
            }.parse(trimmed)
        } catch (_: Exception) {
            null
        }
    }

    private fun isUsableExpiry(rawExpiry: String, today: String = todayIstDate()): Boolean {
        val trimmed = rawExpiry.trim()
        return trimmed.isNotBlank() && trimmed >= today && parseExpiryDate(trimmed) != null
    }

    private fun invalidateExpiryAuthority(reason: String, bnfExpiry: String, nfExpiry: String) {
        val today = todayIstDate()
        val ctxObj = try {
            JSONObject(prefs.getString("context", "{}") ?: "{}")
        } catch (_: Exception) {
            JSONObject()
        }

        fun apply(prefix: String, rawExpiry: String) {
            ctxObj.put("${prefix}Expiry", JSONObject.NULL)
            ctxObj.put("${prefix}DTE", JSONObject.NULL)
            ctxObj.put("${prefix}DteSource", "INVALID_LIVE_EXPIRY")
            ctxObj.put(
                "${prefix}DteMeta",
                JSONObject()
                    .put("source", "INVALID_LIVE_EXPIRY")
                    .put("raw_expiry", rawExpiry)
                    .put("session_date", today)
                    .put("reason", reason)
            )
            ctxObj.optJSONObject("${prefix}Chain")?.put("expiry", JSONObject.NULL)
        }

        apply("bnf", bnfExpiry.trim())
        apply("nf", nfExpiry.trim())

        prefs.edit()
            .remove("expiry_bnf")
            .remove("expiry_nf")
            .remove(EXPIRY_REFRESH_DATE_KEY)
            .putString("context", ctxObj.toString())
            .commit()

        Log.w(TAG, "EXPIRY_AUTHORITY_INVALID: reason=$reason bnf='$bnfExpiry' nf='$nfExpiry'")
        LogBuffer.add('E', TAG, "EXPIRY_AUTHORITY_INVALID: reason=$reason bnf='$bnfExpiry' nf='$nfExpiry'")
    }

    private fun resolveNearestExpiryLive(instrumentKey: String, token: String): String {
        val today = todayIstDate()
        return try {
            val encodedKey = URLEncoder.encode(instrumentKey, Charsets.UTF_8.name())
            val url = "https://api.upstox.com/v2/option/contract?instrument_key=$encodedKey"
            val json = fetchSync(url, token)
            val arr = json?.optJSONArray("data")
            var nearest: String? = null
            if (arr != null) {
                for (i in 0 until arr.length()) {
                    val date = arr.optJSONObject(i)?.optString("expiry", "") ?: ""
                    if (date.length != 10) continue
                    if (date < today) continue
                    if (nearest == null || date < nearest) nearest = date
                }
            }
            nearest?.takeIf { isUsableExpiry(it, today) } ?: ""
        } catch (e: Exception) {
            ""
        }
    }

    private fun refreshActiveExpiries(token: String): Pair<String, String> {
        val today = todayIstDate()
        val stored = prefs.getString("expiry_bnf", "") ?: ""
        if (prefs.getString(EXPIRY_REFRESH_DATE_KEY, "") == today && isUsableExpiry(stored, today))
            return Pair(stored, "")
        val resolved = resolveNearestExpiryLive("NSE_INDEX|Nifty Bank", token)
        if (!isUsableExpiry(resolved, today)) {
            invalidateExpiryAuthority("expiry_refresh_failed", resolved, "")
            return Pair("", "")
        }
        prefs.edit().putString("expiry_bnf", resolved).putString(EXPIRY_REFRESH_DATE_KEY, today).commit()
        return Pair(resolved, "")
    }

    /**
     * D9 repair: the nearest expiry STRICTLY AFTER `afterDate`.
     *
     * [resolveNearestExpiryLive] accepts an expiry equal to today, which is correct for trading but
     * useless for the direction scale: on an index's own expiry day that contract cannot price a
     * forward session move, so compute_scale returns EXPIRY_DAY_CHAIN_ONLY and the index loses
     * direction for the whole session. Resolved once per session per index and cached, so an
     * expiry-day session does not re-resolve the contract list on every poll.
     *
     * Returns "" when no usable forward-dated expiry exists; the brain then fails closed.
     */
    private fun resolveNextExpiryAfter(index: String, instrumentKey: String, token: String,
                                       afterDate: String): String {
        val cacheKey = "scale_next_expiry_$index"
        val cacheDateKey = "${cacheKey}_on"
        val cached = (prefs.getString(cacheKey, "") ?: "").trim()
        if ((prefs.getString(cacheDateKey, "") ?: "") == afterDate &&
            cached.length == 10 && cached > afterDate && parseExpiryDate(cached) != null) {
            return cached
        }
        return try {
            val encodedKey = URLEncoder.encode(instrumentKey, Charsets.UTF_8.name())
            val json = fetchSync("https://api.upstox.com/v2/option/contract?instrument_key=$encodedKey", token)
            val arr = json?.optJSONArray("data")
            var next: String? = null
            if (arr != null) {
                for (i in 0 until arr.length()) {
                    val date = arr.optJSONObject(i)?.optString("expiry", "") ?: ""
                    if (date.length != 10) continue
                    if (date <= afterDate) continue          // STRICTLY after: an equal date is the failing case
                    if (next == null || date < next!!) next = date
                }
            }
            val resolved = next?.takeIf { it > afterDate && parseExpiryDate(it) != null } ?: ""
            if (resolved.isNotEmpty()) {
                prefs.edit().putString(cacheKey, resolved).putString(cacheDateKey, afterDate).commit()
            }
            resolved
        } catch (e: Exception) {
            ""
        }
    }

    /**
     * D9 repair: fetch the next-expiry chain for one index's direction scale, and only when needed.
     *
     * Returns null on the normal path (front expiry still forward-dated), so no extra request is
     * made on an ordinary session. The trading chain is never substituted, and this result never
     * enters the poll object, trade evidence or the market-snapshot identity.
     */
    private fun fetchDirectionScaleChain(index: String, instrumentKey: String, token: String,
                                         today: String, tradeExpiry: String): ScaleChainFetch? {
        // Acquisition bound: only fetch when the scale is actually needed. Decided from the authority's
        // own persisted scales state, so once a usable scale is frozen at the open reference this stops
        // issuing requests instead of repeating them on all ~78 polls of an expiry-day session.
        val need = DirectionScaleNeed.decide(
            index, tradeExpiry, today, DirectionScaleNeed.readState(filesDir, today))
        if (!need.fetch) {
            if (need.reason != DirectionScaleNeed.NOT_EXPIRY_DAY) {
                LogBuffer.add('D', TAG, "SCALE_CHAIN_SKIP: index=$index reason=${need.reason}")
            }
            return null
        }
        val trade = tradeExpiry.trim()
        val next = resolveNextExpiryAfter(index, instrumentKey, token, today)
        if (next.isEmpty()) {
            Log.w(TAG, "SCALE_CHAIN_NO_NEXT_EXPIRY: index=$index session=$today tradeExpiry=$trade")
            LogBuffer.add('W', TAG, "SCALE_CHAIN_NO_NEXT_EXPIRY: index=$index session=$today")
            return null
        }
        val encodedKey = URLEncoder.encode(instrumentKey, Charsets.UTF_8.name())
        val raw = fetchSync(
            "https://api.upstox.com/v2/option/chain?instrument_key=$encodedKey&expiry_date=$next", token)
        if (raw == null) {
            Log.w(TAG, "SCALE_CHAIN_FETCH_FAILED: index=$index scaleExpiry=$next")
            LogBuffer.add('W', TAG, "SCALE_CHAIN_FETCH_FAILED: index=$index scaleExpiry=$next")
            return ScaleChainFetch(next, null)
        }
        Log.i(TAG, "SCALE_CHAIN_FETCHED: index=$index session=$today tradeExpiry=$trade scaleExpiry=$next")
        LogBuffer.add('I', TAG, "SCALE_CHAIN_FETCHED: index=$index scaleExpiry=$next")
        return ScaleChainFetch(next, raw)
    }

    private val NSE_HOLIDAYS_2026 = setOf(
        "2026-01-26",  // Republic Day
        "2026-03-03",  // Maha Shivaratri
        "2026-03-26",  // Holi
        "2026-03-31",  // Id-Ul-Fitr
        "2026-04-03",  // Ram Navami
        "2026-04-14",  // Dr. Ambedkar Jayanti
        "2026-05-01",  // Maharashtra Day
        "2026-05-28",  // Buddha Purnima
        "2026-06-26",  // Bakri Id
        "2026-09-14",  // Milad-un-Nabi
        "2026-10-02",  // Mahatma Gandhi Jayanti
        "2026-10-20",  // Diwali (Laxmi Puja)
        "2026-11-10",  // Prakash Gurpurab Sri Guru Nanak Dev
        "2026-11-24",  // Guru Gobind Singh Jayanti
        "2026-12-25"   // Christmas
    )

    private fun isMarketDay(): Boolean {
        val today = todayIstDate()
        return !NSE_HOLIDAYS_2026.contains(today)
    }

    private fun isMarketOpen(): Boolean {
        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        val cal = Calendar.getInstance(ist)
        val day = cal.get(Calendar.DAY_OF_WEEK)
        if (day == Calendar.SATURDAY || day == Calendar.SUNDAY) return false
        if (!isMarketDay()) return false
        val mins = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE)
        return mins in MARKET_OPEN_MINUTE..MARKET_CLOSE_MINUTE
    }

    private fun createNotification(title: String, text: String): Notification {
        val intent = packageManager.getLaunchIntentForPackage(packageName)
        val pendingIntent = PendingIntent.getActivity(this, 0, intent, PendingIntent.FLAG_IMMUTABLE)
        
        val stopIntent = Intent(this, MarketWatchService::class.java).apply { action = "STOP" }
        val stopPending = PendingIntent.getService(this, 1, stopIntent, PendingIntent.FLAG_IMMUTABLE)

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_menu_manage)
            .setContentIntent(pendingIntent)
            .addAction(android.R.drawable.ic_media_pause, "Stop", stopPending)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }

    private fun updateForegroundNotification(title: String, text: String) {
        val manager = getSystemService(NotificationManager::class.java)
        manager?.notify(NOTIFICATION_ID, createNotification(title, text))
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(CHANNEL_ID, "Market Radar Background", NotificationManager.IMPORTANCE_LOW)
            getSystemService(NotificationManager::class.java)?.createNotificationChannel(channel)
        }
    }

    private fun acquirePartialWakeLock() {
        // C7: Release before acquire to prevent leak if previously held
        releaseWakeLock()
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "MarketRadar::Poll").apply { acquire(30*1000L) }
    }

    private fun releaseWakeLock() {
        wakeLock?.let { if (it.isHeld) it.release() }
    }

    private fun isMLModelReady(): Boolean {
        return File(filesDir, "ml_model.json").exists()
    }

    private fun scoreCandidate(cand: JSONObject, brain: com.chaquo.python.PyObject): JSONObject? {
        return try {
            val result = brain.callAttr("ml_score_bridge", cand.toString()).toString()
            JSONObject(result)
        } catch (e: Exception) {
            null
        }
    }

    private fun finiteDouble(obj: JSONObject, key: String, fallback: Double): Double {
        if (!obj.has(key) || obj.isNull(key)) return fallback
        val value = obj.optDouble(key, fallback)
        return if (value.isFinite()) value else fallback
    }

    private fun pollAlarmIntent(): PendingIntent {
        val alarmIntent = Intent(this, MarketWatchService::class.java).apply { action = ACTION_POLL_TICK }
        return PendingIntent.getForegroundService(
            this,
            POLL_ALARM_REQ_CODE,
            alarmIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
    }

    private fun selfHealIntent(): PendingIntent {
        val restartIntent = Intent(this, MarketWatchService::class.java)
        return PendingIntent.getForegroundService(
            this,
            SERVICE_SELF_HEAL_REQ_CODE,
            restartIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
    }

    private fun ensurePollAlarmScheduled() {
        try {
            val am = getSystemService(ALARM_SERVICE) as AlarmManager
            val triggerAt = SystemClock.elapsedRealtime() + nextPollLoopDelayMs()
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S || am.canScheduleExactAlarms()) {
                am.setExactAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, triggerAt, pollAlarmIntent())
            } else {
                am.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, triggerAt, pollAlarmIntent())
                LogBuffer.add('W', TAG, "POLL_ALARM_EXACT_UNAVAILABLE: using inexact fallback")
            }
        } catch (e: Exception) {
            Log.w(TAG, "POLL_ALARM_SCHEDULE_FAIL: ${e.message}")
            LogBuffer.add('W', TAG, "POLL_ALARM_SCHEDULE_FAIL: ${e.message}")
        }
    }

    private fun cancelPollAlarm() {
        try {
            val am = getSystemService(ALARM_SERVICE) as AlarmManager
            am.cancel(pollAlarmIntent())
            am.cancel(selfHealIntent())
        } catch (e: Exception) {
            LogBuffer.add('D', TAG, "POLL_ALARM_CANCEL_FAIL: ${e.message}")
        }
    }

    private suspend fun maybeRunPollFromAlarm() {
        if (!isMarketOpen()) {
            LogBuffer.add('D', TAG, "POLL_ALARM_SKIP: market_closed")
            return
        }
        val token = applicationContext.getSharedPreferences("market_radar", Context.MODE_PRIVATE)
            .getString("auth_token", "") ?: ""
        if (token.isBlank()) {
            LogBuffer.add('W', TAG, "POLL_ALARM_SKIP: missing_token")
            return
        }
        LogBuffer.add('I', TAG, "POLL_ALARM_TRIGGER: slot=${currentPollSlotKey() ?: "closed"}")
        acquirePartialWakeLock()
        try {
            dispatchPollIfDue("alarm", token)
        } catch (e: Exception) {
            Log.e(TAG, "POLL_ALARM_FAIL: ${e.message}")
            LogBuffer.add('E', TAG, "POLL_ALARM_FAIL: ${e.message}")
        } finally {
            releaseWakeLock()
        }
    }

    private suspend fun maybeRunForcedPoll() {
        if (!isMarketOpen()) {
            LogBuffer.add('D', TAG, "POLL_FORCE_SKIP: market_closed")
            return
        }
        val token = applicationContext.getSharedPreferences("market_radar", Context.MODE_PRIVATE)
            .getString("auth_token", "") ?: ""
        if (token.isBlank()) {
            LogBuffer.add('W', TAG, "POLL_FORCE_SKIP: missing_token")
            return
        }
        LogBuffer.add('I', TAG, "POLL_FORCE_TRIGGER")
        acquirePartialWakeLock()
        try {
            dispatchPollIfDue("force", token)
        } catch (e: Exception) {
            Log.e(TAG, "POLL_FORCE_FAIL: ${e.message}")
            LogBuffer.add('E', TAG, "POLL_FORCE_FAIL: ${e.message}")
        } finally {
            releaseWakeLock()
        }
    }

    private suspend fun dispatchPollIfDue(trigger: String, token: String) {
        val slotKey = currentPollSlotKey()
        if (slotKey == null) {
            LogBuffer.add('D', TAG, "POLL_DISPATCH_SKIP[$trigger]: no_open_slot")
            return
        }
        if (prefs.getString(LAST_SUCCESSFUL_POLL_SLOT_KEY, "") == slotKey) {
            LogBuffer.add('D', TAG, "POLL_DISPATCH_SKIP[$trigger]: slot_already_completed=$slotKey")
            return
        }
        val reserved = synchronized(pollDispatchLock) {
            if (prefs.getString(LAST_POLL_DISPATCH_SLOT_KEY, "") == slotKey) {
                false
            } else if (pollDispatchInFlightKeys.contains(slotKey)) {
                false
            } else {
                pollDispatchInFlightKeys.add(slotKey)
                prefs.edit().putString(LAST_POLL_DISPATCH_SLOT_KEY, slotKey).commit()
                true
            }
        }
        if (!reserved) {
            LogBuffer.add('D', TAG, "POLL_DISPATCH_DEDUP[$trigger]: slot_already_dispatched=$slotKey")
            return
        }

        LogBuffer.add('I', TAG, "POLL_DISPATCH[$trigger]: slot=$slotKey")
        try {
            performPoll(token)
        } finally {
            synchronized(pollDispatchLock) {
                pollDispatchInFlightKeys.remove(slotKey)
            }
        }
    }

    private fun nextPollLoopDelayMs(nowMs: Long = System.currentTimeMillis()): Long {
        if (!isMarketOpen()) return 60_000L
        val nextSlotEpochMs = nextPollSlotEpochMs(nowMs)
        return (nextSlotEpochMs - nowMs).coerceAtLeast(15_000L)
    }

    private fun nextPollSlotEpochMs(nowMs: Long = System.currentTimeMillis()): Long {
        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        val cal = Calendar.getInstance(ist).apply {
            timeInMillis = nowMs
            set(Calendar.SECOND, 0)
            set(Calendar.MILLISECOND, 0)
        }
        val minutes = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE)
        return when {
            minutes < MARKET_OPEN_MINUTE -> {
                cal.set(Calendar.HOUR_OF_DAY, 9)
                cal.set(Calendar.MINUTE, 15)
                cal.timeInMillis + POLL_SLOT_TRIGGER_LAG_MS
            }
            minutes >= MARKET_CLOSE_MINUTE -> nowMs + 60_000L
            else -> {
                val slotOffset = (minutes - MARKET_OPEN_MINUTE) / POLL_SLOT_MINUTES
                val nextMinute = MARKET_OPEN_MINUTE + ((slotOffset + 1) * POLL_SLOT_MINUTES)
                cal.set(Calendar.HOUR_OF_DAY, nextMinute / 60)
                cal.set(Calendar.MINUTE, nextMinute % 60)
                cal.timeInMillis + POLL_SLOT_TRIGGER_LAG_MS
            }
        }
    }

    private fun currentPollSlotKey(nowMs: Long = System.currentTimeMillis()): String? {
        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        val cal = Calendar.getInstance(ist).apply { timeInMillis = nowMs }
        val day = cal.get(Calendar.DAY_OF_WEEK)
        if (day == Calendar.SATURDAY || day == Calendar.SUNDAY || !isMarketDay()) return null
        val minutes = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE)
        if (minutes < MARKET_OPEN_MINUTE || minutes > MARKET_CLOSE_MINUTE) return null
        val slotOrdinal = ((minutes - MARKET_OPEN_MINUTE) / POLL_SLOT_MINUTES) + 1
        return "${todayIstDate()}|$slotOrdinal"
    }

    private fun slotOrdinalFromSlotKey(slotKey: String): Int? {
        return slotKey.substringAfterLast("|", "").toIntOrNull()
            ?.takeIf { it in 1..POLL_FULL_DAY_SLOTS }
    }

    private fun maintainSessionWakeLock() {
        if (!isMarketOpen()) {
            releaseSessionWakeLock()
            return
        }
        val held = sessionWakeLock?.isHeld == true
        if (held) return
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        sessionWakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "MarketRadar::Session").apply {
            setReferenceCounted(false)
            acquire()
        }
        LogBuffer.add('I', TAG, "SESSION_WAKELOCK_ACQUIRED")
    }

    private fun releaseSessionWakeLock() {
        sessionWakeLock?.let {
            if (it.isHeld) {
                it.release()
                LogBuffer.add('I', TAG, "SESSION_WAKELOCK_RELEASED")
            }
        }
        sessionWakeLock = null
    }

    private fun maybeWarnOnPollGap(nowMs: Long = System.currentTimeMillis()) {
        if (!isMarketOpen()) return
        val lastSuccessfulMs = prefs.getLong("last_successful_poll_ms", 0L)
        if (lastSuccessfulMs <= 0L) return
        val gapMs = nowMs - lastSuccessfulMs
        if (gapMs < SESSION_POLL_GAP_WARNING_MS) return
        val slotKey = currentPollSlotKey(nowMs) ?: return
        if (prefs.getString(LAST_POLL_GAP_WARNING_SLOT_KEY, "") == slotKey) return
        val gapMinutes = gapMs / 60_000L
        NotificationHelper.send(
            this,
            "Service health warning",
            "Polling gap detected during market hours: ${gapMinutes}m since last successful poll.",
            "warning"
        )
        prefs.edit().putString(LAST_POLL_GAP_WARNING_SLOT_KEY, slotKey).apply()
        LogBuffer.add('W', TAG, "SERVICE_HEALTH_WARNING: gapMin=$gapMinutes slot=$slotKey")
    }

    private fun scheduleSelfHealRestart() {
        if (!prefs.getBoolean("service_running", false)) return
        if (!isMarketOpen()) return
        try {
            val am = getSystemService(ALARM_SERVICE) as AlarmManager
            val delayMs = maxOf(SERVICE_SELF_HEAL_DELAY_MS, foregroundStartBackoffRemainingMs())
            val triggerAt = SystemClock.elapsedRealtime() + delayMs
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S || am.canScheduleExactAlarms()) {
                am.setExactAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, triggerAt, selfHealIntent())
            } else {
                am.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, triggerAt, selfHealIntent())
            }
            LogBuffer.add('W', TAG, "SERVICE_SELF_HEAL_SCHEDULED: delayMs=$delayMs")
        } catch (e: Exception) {
            LogBuffer.add('W', TAG, "SERVICE_SELF_HEAL_FAIL: ${e.message}")
        }
    }

    override fun onDestroy() {
        bnfWebSocket.close()
        // E5: Ensure wakelock is released when service is killed
        releaseWakeLock()
        releaseSessionWakeLock()
        scheduleSelfHealRestart()
        // b503: late captures keep finishing on the process-owned finisher; only the
        // prompt-upload hook of THIS instance is withdrawn.
        CaptureRuntime.drainHook = null
        serviceScope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?) = null

    // C4: Expiry fallback helper
    private fun getNextThursday(): String {
        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        val cal = Calendar.getInstance(ist)
        while (cal.get(Calendar.DAY_OF_WEEK) != Calendar.THURSDAY) {
            cal.add(Calendar.DATE, 1)
        }
        return SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = ist }.format(cal.time)
    }

    private fun todayIstDate(): String {
        return SimpleDateFormat("yyyy-MM-dd", Locale.US).apply {
            timeZone = TimeZone.getTimeZone("Asia/Kolkata")
        }.format(Date())
    }

    private fun currentIstIso(): String {
        return SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssZ", Locale.US).apply {
            timeZone = TimeZone.getTimeZone("Asia/Kolkata")
        }.format(Date())
    }

    private fun reservePollNumberForToday(): Int = synchronized(pollSaveLock) {
        val today = todayIstDate()
        val lastPollDate = prefs.getString("last_poll_date", "") ?: ""
        val assignedDate = prefs.getString(LAST_ASSIGNED_POLL_DATE_KEY, "") ?: ""
        val committedCount = if (lastPollDate == today) prefs.getInt("poll_count", 0) else 0
        val assignedCount = if (assignedDate == today) prefs.getInt(LAST_ASSIGNED_POLL_NUMBER_KEY, 0) else 0
        val next = maxOf(committedCount, assignedCount) + 1
        prefs.edit()
            .putString(LAST_ASSIGNED_POLL_DATE_KEY, today)
            .putInt(LAST_ASSIGNED_POLL_NUMBER_KEY, next)
            .commit()
        next
    }

    private fun persistSessionIntegritySummary(summary: SessionIntegritySummary) {
        prefs.edit()
            .putString(COVERAGE_INTEGRITY_DATE_KEY, summary.sessionDate)
            .putString(COVERAGE_INTEGRITY_KEY, summary.coverageIntegrity)
            .putString(COVERAGE_INTEGRITY_ISSUE_KEY, summary.integrityIssue)
            .commit()
    }

    private fun markIncompleteSession(sessionDate: String, summary: SessionIntegritySummary) {
        val message =
            "Evaluation blocked for $sessionDate: session integrity is broken (${summary.integrityIssue.lowercase(Locale.US)}). " +
                "Expected ${summary.expectedFullDay} full-day slots, recorded ${summary.pollCount} polls, final-slot count ${summary.finalSlotOccurrences}, snapshot rows ${summary.snapshotRows}."
        prefs.edit()
            .putString("evaluation_job_date", sessionDate)
            .putString("evaluation_phase", "INCOMPLETE_SESSION")
            .putString("last_evaluation_message", message)
            .putString("evaluation_running_date", "")
            .putLong("evaluation_started_at_ms", 0L)
            .putLong("evaluation_job_updated_at_ms", System.currentTimeMillis())
            .putInt("evaluation_total_snapshots", summary.snapshotRows)
            .putInt("evaluation_completed_snapshots", 0)
            .putInt("last_evaluation_produced_count", 0)
            .putInt("last_evaluation_outcome_count", 0)
            .putString("evaluation_last_error", summary.integrityIssue)
            .putString("teacher_research_report_status", "NOT_APPLICABLE")
            .putString("teacher_research_report_error", summary.integrityIssue)
            .remove("evaluation_done_date")
            .commit()
    }

    private fun computeSessionIntegrity(sessionDate: String): SessionIntegritySummary {
        val pollCount = if ((prefs.getString("last_poll_date", "") ?: "") == sessionDate) {
            countDistinctSlotOrdinalsFromHistory(sessionDate)
        } else {
            0
        }
        val rawPollCount = if ((prefs.getString("last_poll_date", "") ?: "") == sessionDate) {
            prefs.getInt("poll_count", 0)
        } else {
            0
        }
        val finalSlotOccurrences = countSlotOccurrencesFromHistory(sessionDate, POLL_FULL_DAY_SLOTS)
        val snapshotRows = countLocalSnapshotRows(sessionDate)
        val integrityIssue = when {
            finalSlotOccurrences > 1 -> "FINAL_SLOT_DUPLICATE"
            finalSlotOccurrences == 0 -> "FINAL_SLOT_MISSING"
            pollCount < POLL_FULL_DAY_SLOTS -> "MISSED_SLOTS"
            rawPollCount > POLL_FULL_DAY_SLOTS -> "COUNTER_DRIFT"
            snapshotRows > POLL_FULL_DAY_SLOTS -> "SNAPSHOT_OVERRUN"
            else -> "NONE"
        }
        val coverageIntegrity = when {
            integrityIssue in setOf("POLL_OVERRUN", "FINAL_SLOT_DUPLICATE", "FINAL_SLOT_MISSING") -> "INTEGRITY_BROKEN"
            integrityIssue == "MISSED_SLOTS" -> "PARTIAL"
            integrityIssue in setOf("COUNTER_DRIFT", "SNAPSHOT_OVERRUN") -> "COMPLETE_WITH_RETRIES"
            else -> "COMPLETE"
        }
        return SessionIntegritySummary(
            sessionDate = sessionDate,
            pollCount = pollCount,
            expectedFullDay = POLL_FULL_DAY_SLOTS,
            finalSlotOccurrences = finalSlotOccurrences,
            snapshotRows = snapshotRows,
            snapshotOverrun = snapshotRows > POLL_FULL_DAY_SLOTS,
            coverageIntegrity = coverageIntegrity,
            integrityIssue = integrityIssue
        )
    }

    private fun countSlotOccurrencesFromHistory(sessionDate: String, slotOrdinal: Int): Int {
        val lastPollDate = prefs.getString("last_poll_date", "") ?: ""
        if (lastPollDate != sessionDate) return 0
        return try {
            val history = JSONArray(prefs.getString("poll_history", "[]") ?: "[]")
            var count = 0
            for (i in 0 until history.length()) {
                val row = history.optJSONObject(i) ?: continue
                val time = pollSlotTime(row)
                if (slotOrdinalFromPollTime(time) == slotOrdinal) count++
            }
            count
        } catch (_: Exception) {
            0
        }
    }

    private fun countDistinctSlotOrdinalsFromHistory(sessionDate: String): Int {
        val lastPollDate = prefs.getString("last_poll_date", "") ?: ""
        if (lastPollDate != sessionDate) return 0
        return try {
            val history = JSONArray(prefs.getString("poll_history", "[]") ?: "[]")
            val slots = mutableSetOf<Int>()
            for (i in 0 until history.length()) {
                val row = history.optJSONObject(i) ?: continue
                val time = pollSlotTime(row)
                val slot = slotOrdinalFromPollTime(time) ?: continue
                slots.add(slot)
            }
            slots.size
        } catch (_: Exception) {
            0
        }
    }

    private fun slotOrdinalFromPollTime(time: String): Int? {
        val match = Regex("""(\d{1,2}):(\d{2})""").find(time) ?: return null
        val parts = match.value.split(":")
        if (parts.size != 2) return null
        val hour = parts[0].toIntOrNull() ?: return null
        val minute = parts[1].toIntOrNull() ?: return null
        val totalMinutes = hour * 60 + minute
        if (totalMinutes < MARKET_OPEN_MINUTE || totalMinutes > MARKET_CLOSE_MINUTE) return null
        return ((totalMinutes - MARKET_OPEN_MINUTE) / POLL_SLOT_MINUTES) + 1
    }

    private fun pollSlotTime(row: JSONObject): String {
        return row.optString("t", "").ifBlank {
            row.optString("time", "").ifBlank {
                row.optString("poll_time", "").ifBlank {
                    row.optString("poll_ts", "")
                }
            }
        }
    }

    private fun countLocalSnapshotRows(sessionDate: String): Int {
        val safeDate = sessionDate.filter { it.isDigit() || it == '-' }.ifBlank { "unknown" }
        val file = File(filesDir, "evaluation_local_cache/brain_snapshots_${safeDate}.jsonl")
        if (!file.exists()) return 0
        return try {
            file.useLines { lines -> lines.count { it.trim().isNotBlank() } }
        } catch (_: Exception) {
            0
        }
    }

    private fun heapLine(): String {
        val rt = Runtime.getRuntime()
        val used = rt.totalMemory() - rt.freeMemory()
        return "pid=${android.os.Process.myPid()} startUuid=${MarketRadarApp.PROCESS_START_UUID} javaUsed=$used javaMax=${rt.maxMemory()} nativeUsed=${Debug.getNativeHeapAllocatedSize()}"
    }
}

private fun JSONObject.optNullableFiniteDouble(name: String): Double? {
    if (!has(name) || isNull(name)) return null
    val raw = opt(name)
    return when (raw) {
        is Number -> raw.toDouble()
        is String -> raw.trim().toDoubleOrNull()
        else -> null
    }?.takeIf { it.isFinite() }
}

private fun JSONObject.putNullableFiniteNumber(name: String, value: Double?) {
    if (value == null || !value.isFinite()) {
        put(name, JSONObject.NULL)
    } else {
        put(name, value)
    }
}

/** Missing or nonfinite optional fields are not evidence that a stored gross
 * extreme vanished. Fresh current_pnl/current_spot retain their fail-closed
 * copy-back behaviour in the caller. */
internal fun applyOptionalPositionLiveMetrics(trade: JSONObject, live: JSONObject) {
    val premium = if (live.has("current_net_premium")) live.optNullableFiniteDouble("current_net_premium") else null
    if (premium != null) trade.put("current_premium", premium)
    for (key in arrayOf("peak_pnl", "trough_pnl", "peak_erosion", "vix_change")) {
        if (!live.has(key)) continue
        live.optNullableFiniteDouble(key)?.let { trade.put(key, it) }
    }
}
