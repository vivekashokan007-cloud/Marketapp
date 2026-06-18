package com.marketradar.app

import android.app.*
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
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
    private var wakeLock: PowerManager.WakeLock? = null
    private var sessionWakeLock: PowerManager.WakeLock? = null
    private lateinit var prefs: SharedPreferences
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
    private var lastPositionAlertKeys = mutableSetOf<String>()

    private var pollingJob: Job? = null
    private val serviceStartInProgress = AtomicBoolean(false)
    private val pollInProgress = AtomicBoolean(false)
    private val pollSaveLock = Any()
    private val mlPersistLock = Any()
    private var pollLoopIteration = 0L

    private data class BreadthStock(
        val symbol: String,
        val requestKey: String,
        val responseKey: String,
        val weight: Double
    )

    companion object {
        const val CHANNEL_ID = "market_radar_service"
        const val NOTIFICATION_ID = 1001
        const val TAG = "MarketWatchService"
        private const val ACTION_POLL_TICK = "com.marketradar.app.ACTION_POLL_TICK"
        const val ACTION_FORCE_POLL = "com.marketradar.app.ACTION_FORCE_POLL"
        private const val POLL_ALARM_REQ_CODE = 74051
        private const val LAST_POLL_DISPATCH_SLOT_KEY = "last_poll_dispatch_slot"
        private const val LAST_SUCCESSFUL_POLL_SLOT_KEY = "last_successful_poll_slot"
        private const val LEASE_OWNER_PID_KEY = "lease_owner_pid"
        private const val LEASE_STARTED_MS_KEY = "lease_started_ms"
        private const val LEASE_HEARTBEAT_MS_KEY = "lease_heartbeat_ms"
        private const val LEASE_STALE_MS = 10 * 60 * 1000L
        private const val SESSION_POLL_GAP_WARNING_MS = 12 * 60 * 1000L
        private const val PY_SNAPSHOT_TIMEOUT_MS = 4_000L
        private const val PY_AGENT_TIMEOUT_MS = 3_000L
        private const val APPROVED_BRANCH_PROPOSALS_KEY = "approved_branch_proposals"
        private const val APPROVED_BRANCH_PROPOSALS_SYNC_MS_KEY = "approved_branch_proposals_sync_ms"
        private const val APPROVED_BRANCH_PROPOSALS_TTL_MS = 15 * 60 * 1000L
        private const val NF50_REMOTE_CACHE_KEY = "nf50_remote_constituents_json"
        private const val NF50_REMOTE_CACHE_MS_KEY = "nf50_remote_constituents_sync_ms"
        private const val NF50_REMOTE_TTL_MS = 12 * 60 * 60 * 1000L
        private const val SIGNAL_RELIABILITY_CACHE_KEY = "signal_reliability_rows_json"
        private const val SIGNAL_RELIABILITY_CACHE_MONTH_KEY = "signal_reliability_cache_month"
        private const val EXPIRY_REFRESH_DATE_KEY = "expiry_refresh_date"
        private const val LAST_POLL_GAP_WARNING_SLOT_KEY = "last_poll_gap_warning_slot"
        private const val MARKET_OPEN_MINUTE = 9 * 60 + 15
        private const val MARKET_CLOSE_MINUTE = 15 * 60 + 30
        private const val POLL_SLOT_MINUTES = 5
        private const val POLL_FULL_DAY_SLOTS = ((MARKET_CLOSE_MINUTE - MARKET_OPEN_MINUTE) / POLL_SLOT_MINUTES) + 1
        private const val POLL_SLOT_TRIGGER_LAG_MS = 5_000L
        private const val SERVICE_SELF_HEAL_REQ_CODE = 74052
        private const val SERVICE_SELF_HEAL_DELAY_MS = 15_000L
        private const val ELEPHANT_BASE_URL = "https://marketradar-oracle.online"
        private const val GENERATED_CANDIDATE_PERSIST_CAP = 50
        
        private const val KOTAK_KEY_PREF = "kotak_instrument_key"
        private const val KOTAK_KEY_CURRENT = "NSE_EQ|INE237A01036"
        private const val KOTAK_KEY_LEGACY = "NSE_EQ|INE237A01028"

        private val BNF_STOCKS_BASE = listOf(
            BreadthStock("HDFCBANK", "NSE_EQ|INE040A01034", "NSE_EQ:HDFCBANK", 0.2522),
            BreadthStock("SBIN", "NSE_EQ|INE062A01020", "NSE_EQ:SBIN", 0.2042),
            BreadthStock("ICICIBANK", "NSE_EQ|INE009A01021", "NSE_EQ:ICICIBANK", 0.1977),
            BreadthStock("AXISBANK", "NSE_EQ|INE238A01034", "NSE_EQ:AXISBANK", 0.0865)
        )
        private val NF50_CONSTITUENTS_BASE = listOf(
            "NSE_EQ|INE423A01024", "NSE_EQ|INE742F01042", "NSE_EQ|INE437A01024", "NSE_EQ|INE021A01026",
            "NSE_EQ|INE238A01034", "NSE_EQ|INE917I01010", "NSE_EQ|INE918I01026", "NSE_EQ|INE296A01032",
            "NSE_EQ|INE263A01024", "NSE_EQ|INE397D01024", "NSE_EQ|INE059A01026", "NSE_EQ|INE522F01014",
            "NSE_EQ|INE089A01031", "NSE_EQ|INE066A01021", "NSE_EQ|INE758T01015", "NSE_EQ|INE047A01021",
            "NSE_EQ|INE860A01027", "NSE_EQ|INE040A01034", "NSE_EQ|INE795G01014", "NSE_EQ|INE038A01020",
            "NSE_EQ|INE030A01027", "NSE_EQ|INE090A01021", "NSE_EQ|INE646L01027", "NSE_EQ|INE009A01021",
            "NSE_EQ|INE154A01025", "NSE_EQ|INE758E01017", "NSE_EQ|INE019A01038", "NSE_EQ|INE018A01030",
            "NSE_EQ|INE101A01026", "NSE_EQ|INE585B01010", "NSE_EQ|INE027H01010", "NSE_EQ|INE239A01024",
            "NSE_EQ|INE733E01010", "NSE_EQ|INE213A01029", "NSE_EQ|INE752E01010", "NSE_EQ|INE002A01018",
            "NSE_EQ|INE123W01016", "NSE_EQ|INE062A01020", "NSE_EQ|INE721A01047", "NSE_EQ|INE044A01036",
            "NSE_EQ|INE192A01025", "NSE_EQ|INE081A01020", "NSE_EQ|INE467B01029", "NSE_EQ|INE669C01036",
            "NSE_EQ|INE280A01028", "NSE_EQ|INE155A01022", "NSE_EQ|INE849A01020", "NSE_EQ|INE481G01011",
            "NSE_EQ|INE075A01022"
        )
    }

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

    private fun getNf50Constituents(kotakKey: String = getCachedKotakKey()): List<String> {
        return NF50_CONSTITUENTS_BASE + listOf(kotakKey)
    }

    private fun parseRemoteNf50Constituents(rows: JSONArray, kotakKey: String = getCachedKotakKey()): List<String> {
        var latestEffectiveDate: String? = null
        for (i in 0 until rows.length()) {
            val row = rows.optJSONObject(i) ?: continue
            val active = row.optBoolean("active", true)
            if (!active) continue
            val effectiveDate = row.optString("effective_date")
            if (effectiveDate.isBlank()) continue
            if (latestEffectiveDate == null || effectiveDate > latestEffectiveDate) {
                latestEffectiveDate = effectiveDate
            }
        }
        val out = mutableListOf<String>()
        for (i in 0 until rows.length()) {
            val row = rows.optJSONObject(i) ?: continue
            val active = row.optBoolean("active", true)
            if (!active) continue
            if (!latestEffectiveDate.isNullOrBlank()) {
                val effectiveDate = row.optString("effective_date")
                if (effectiveDate != latestEffectiveDate) continue
            }
            val rawKey = listOf(
                row.optString("instrument_key"),
                row.optString("request_key"),
                row.optString("symbol")
            ).firstOrNull { it.isNotBlank() } ?: continue
            val normalized = when {
                rawKey.startsWith("NSE_EQ|") -> rawKey
                rawKey.equals("KOTAKBANK", ignoreCase = true) -> kotakKey
                else -> ""
            }
            if (normalized.isNotBlank()) out.add(normalized)
        }
        val unique = LinkedHashSet(out)
        if (!unique.contains(kotakKey)) unique.add(kotakKey)
        return unique.toList()
    }

    private fun getRemoteNf50Constituents(kotakKey: String = getCachedKotakKey()): List<String>? {
        val now = System.currentTimeMillis()
        val cachedAt = prefs.getLong(NF50_REMOTE_CACHE_MS_KEY, 0L)
        val cachedJson = prefs.getString(NF50_REMOTE_CACHE_KEY, "") ?: ""
        if (cachedJson.isNotBlank() && now - cachedAt < NF50_REMOTE_TTL_MS) {
            return try {
                val cached = JSONArray(cachedJson)
                val parsed = parseRemoteNf50Constituents(cached, kotakKey)
                if (parsed.isNotEmpty()) parsed else null
            } catch (_: Exception) {
                null
            }
        }

        return try {
            val rows = SupabaseClient.fetchNf50ConstituentRows()
            if (rows.length() <= 0) return null
            val parsed = parseRemoteNf50Constituents(rows, kotakKey)
            if (parsed.isEmpty()) return null
            prefs.edit()
                .putString(NF50_REMOTE_CACHE_KEY, rows.toString())
                .putLong(NF50_REMOTE_CACHE_MS_KEY, now)
                .apply()
            parsed
        } catch (e: Exception) {
            LogBuffer.add('W', TAG, "NF50_REMOTE_CONFIG_FAIL: ${e.message}")
            if (cachedJson.isNotBlank()) {
                try {
                    val cached = JSONArray(cachedJson)
                    val parsed = parseRemoteNf50Constituents(cached, kotakKey)
                    if (parsed.isNotEmpty()) return parsed
                } catch (_: Exception) {
                }
            }
            null
        }
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
        if (intent?.action == "STOP") {
            cancelPollAlarm()
            releaseLease()
            stopPolling()
            return START_NOT_STICKY
        }

        val lease = claimLease()
        Log.w(TAG, "LEASE_RESULT: claimed=${lease.claimed} reason=${lease.reason} gapMs=${lease.gapMs}")
        LogBuffer.add('W', TAG, "LEASE_RESULT: claimed=${lease.claimed} reason=${lease.reason} gapMs=${lease.gapMs}")
        if (!lease.claimed) {
            stopSelf()
            return START_NOT_STICKY
        }

        startForeground(NOTIFICATION_ID, createNotification("Service Starting", "Initializing poll loop..."))
        prefs.edit().putBoolean("service_running", true).commit() // NB5: use commit() for cross-process visibility
        maintainSessionWakeLock()
        ensurePollAlarmScheduled()

        if (intent?.action == ACTION_POLL_TICK) {
            serviceScope.launch { maybeRunPollFromAlarm() }
        } else if (intent?.action == ACTION_FORCE_POLL) {
            serviceScope.launch { maybeRunForcedPoll() }
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
        val hasBaseline = prefs.contains("morning_baseline")
        
        if (!isStale && hasBaseline && lastPollDate == today) {
            Log.d(TAG, "Bootstrap skipped: data is fresh")
            val premHistory = JSONArray(prefs.getString("premium_history", "[]"))
            val ySig = prefs.getString("yesterday_signal", "null")
            val historyLoadedLog = "HISTORY_LOADED_RESTART: vixCount=${premHistory.length()}, fiiCount=${extractFiiHistory().length()}, ySignal=$ySig"
            Log.d(TAG, historyLoadedLog)
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
                val closed = SupabaseClient.getClosedTrades()
                prefs.edit().putString("closed_trades", closed.toString()).apply()
                val history = SupabaseClient.getPollHistory(today)
                if (history.length() > 0) {
                    val latest = history.optJSONObject(history.length() - 1)
                    prefs.edit().apply {
                        putString("poll_history", history.toString())
                        putInt("poll_count", history.length())
                        putString("last_poll_date", today)
                        if (latest != null) {
                            putString("latest_poll", latest.toString())
                            putString("last_poll_time", latest.optString("t", ""))
                        }
                    }.apply()
                } else if (
                    lastPollDate != today &&
                    (prefs.getString("poll_history", "[]") != "[]" ||
                        prefs.getInt("poll_count", 0) != 0 ||
                        prefs.getString("latest_poll", "null") != "null" ||
                        prefs.contains("last_poll_time"))
                ) {
                    prefs.edit().apply {
                        remove("poll_history")
                        remove("poll_count")
                        remove("latest_poll")
                        remove("last_poll_time")
                        remove(LAST_POLL_DISPATCH_SLOT_KEY)
                        remove(LAST_SUCCESSFUL_POLL_SLOT_KEY)
                        putString("last_poll_date", today)
                    }.apply()
                    Log.i(TAG, "DAILY_RESET_BOOTSTRAP: cleared stale poll state for $today")
                }

                // 5. Historial Premium Data & Signal
                val premHistory = SupabaseClient.getPremiumHistory()
                if (premHistory.length() > 0) {
                    prefs.edit().putString("premium_history", premHistory.toString()).apply()
                }
                
                val yesterday = getYesterdayDate()
                SupabaseClient.getYesterdaySignal(yesterday)?.let {
                    prefs.edit().putString("yesterday_signal", it.toString()).apply()
                    Log.d(TAG, "SIGNAL_PRIOR_LOADED: $it")
                }

                val approvedBranchRows = SupabaseClient.select("ai_branch_proposals", "status=eq.approved", "approved_at.desc", 50)
                prefs.edit()
                    .putString(APPROVED_BRANCH_PROPOSALS_KEY, approvedBranchRows.toString())
                    .putLong(APPROVED_BRANCH_PROPOSALS_SYNC_MS_KEY, now)
                    .apply()

                val historyLoadedLog = "HISTORY_LOADED: vixCount=${premHistory.length()}, ivPercentile=${calculateIvPercentile(18.0)}, fiiCount=${extractFiiHistory().length()}"
                Log.d(TAG, historyLoadedLog)

                prefs.edit().putLong("last_bootstrap_time", now).commit()
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
            val nfFutKey = getFuturesKey("NF")
            Log.d(TAG, "BOOTSTRAP_FUTURES: BNF=$bnfFutKey, NF=$nfFutKey")
            
            // Fetch initial futures quotes to prime the system
            val url = "https://api.upstox.com/v2/market-quote/quotes?instrument_key=$bnfFutKey,$nfFutKey"
            fetchSync(url, token)
        } catch (e: Exception) {
            Log.e(TAG, "Bootstrap from Upstox failed: ${e.message}")
        }
    }

    private suspend fun fetchYesterdayOHLC(token: String) {
        val today = todayIstDate()
        if (prefs.getString("ohlc_date", "") == today) return

        Log.d(TAG, "Fetching yesterday's OHLC...")
        val url = "https://api.upstox.com/v2/market-quote/ohlc?instrument_key=NSE_INDEX|Nifty Bank,NSE_INDEX|Nifty 50&interval=1d"
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
        if (pollingJob?.isActive == true) {
            Log.w(TAG, "POLL_LOOP_IGNORED: polling loop already active")
            LogBuffer.add('W', TAG, "POLL_LOOP_IGNORED: polling loop already active")
            return
        }
        pollingJob = serviceScope.launch {
            while (isActive) {
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
                            LogBuffer.add('E', "MarketWatchService", "Poll #${nextPollNumberForToday()} FAILED: ${e.message}")
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
        val doneToday = prefs.getString("evaluation_done_date", "") == today
        val runningToday = prefs.getString("evaluation_running_date", "") == today
        val alreadyHandedOff = prefs.getBoolean("hasDayEvalRun", false)

        if (!alreadyHandedOff && !doneToday && !runningToday) {
            try {
                val mlService = Intent(this@MarketWatchService, MarketMLService::class.java).apply {
                    action = "ACTION_DAY_EVALUATION"
                }
                startForegroundService(mlService)
                prefs.edit().putBoolean("hasDayEvalRun", true).apply()
                Log.i(TAG, "DAY_EVAL_HANDOFF: launched post-close evaluation")
            } catch (e: Exception) {
                Log.e(TAG, "DAY_EVAL_HANDOFF_FAIL: ${e.message}")
            }
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
            val pollCount = nextPollNumberForToday()
            Log.d(TAG, "POLL_START: performPoll() entered")
            LogBuffer.add('I', "MarketWatchService", "Poll #$pollCount starting")

            val (bnfExpiry, nfExpiry) = refreshActiveExpiries(token)

            // Expiry Rollover check
            val sdfUTC = SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = TimeZone.getTimeZone("Asia/Kolkata") }
            val today = sdfUTC.format(Date())

            if (bnfExpiry < today) {
                Log.w(TAG, "POLL_SKIP: Expiry passed: $bnfExpiry < $today")
                NotificationHelper.send(this, "⚠️ Expiry passed", "Poll skipped. Open app to refresh expiry dates.", "info")
                return
            }

            updateForegroundNotification("Polling Market", "Fetching Quotes...")

            // A8: Parallelize independent HTTP calls
            val kotakKey = resolveKotakKey(token)
            val bnfStocks = getBnfStocks(kotakKey).joinToString(",") { it.requestKey }
            val quotesUrl = "https://api.upstox.com/v2/market-quote/quotes?instrument_key=NSE_INDEX|Nifty Bank,NSE_INDEX|Nifty 50,NSE_INDEX|India VIX"
            val bnfUrl = "https://api.upstox.com/v2/option/chain?instrument_key=NSE_INDEX|Nifty Bank&expiry_date=$bnfExpiry"
            val bnfStocksUrl = "https://api.upstox.com/v2/market-quote/quotes?instrument_key=$bnfStocks"
            val nfUrl = "https://api.upstox.com/v2/option/chain?instrument_key=NSE_INDEX|Nifty 50&expiry_date=$nfExpiry"

            val parallel = coroutineScope {
                listOf(
                    async(Dispatchers.IO) { fetchNf50Breadth(token) },
                    async(Dispatchers.IO) { fetchSync(quotesUrl, token) },
                    async(Dispatchers.IO) { fetchSync(bnfUrl, token) },
                    async(Dispatchers.IO) { fetchSync(bnfStocksUrl, token) },
                    async(Dispatchers.IO) { fetchSync(nfUrl, token) }
                ).map { it.await() }
            }
            val nf50Breadth = parallel[0]!!
            val quotesJson = parallel[1]
            val bnfChainJson = parallel[2]
            val bnfStocksJson = parallel[3]
            val nfChainJson = parallel[4]

            if (quotesJson == null) {
                Log.e(TAG, "POLL_FAIL: Quotes fetch returned null — network or auth error")
                return
            }

            Log.d(TAG, "A8_PARALLEL_FETCH: quotes=${quotesJson != null} bnfChain=${bnfChainJson != null} bnfStocks=${bnfStocksJson != null} nfChain=${nfChainJson != null} nf50=${nf50Breadth != null}")
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

            if (bnfChainJson == null || bnfStocksJson == null || nfChainJson == null) {
                Log.e(TAG, "POLL_FAIL: Chain or stocks fetch returned null")
                return
            }

            val data = quotesJson.getJSONObject("data")
            val bnfSpot = data.getJSONObject("NSE_INDEX:Nifty Bank").getDouble("last_price")
            val nfSpot = data.getJSONObject("NSE_INDEX:Nifty 50").getDouble("last_price")
            val vix = data.getJSONObject("NSE_INDEX:India VIX").getDouble("last_price")
            Log.d(TAG, "POLL_DATA_RECEIVED: BNF=$bnfSpot NF=$nfSpot VIX=$vix")

            // Step 5: Build poll object and save
            Log.d(TAG, "POLL_STEP5: Saving poll object")
            val pollObj = parsePollData(quotesJson, bnfChainJson, nfChainJson, bnfStocksJson, bnfSpot)
            savePoll(pollObj)
            LogBuffer.add('I', "MarketWatchService", "Poll #$pollCount complete, candidates=${pollObj.optJSONArray("candidates")?.length() ?: 0}")

            // Step 6: Run Python Brain (nf50Breadth already fetched in parallel above)
            Log.d(TAG, "POLL_STEP6: Launching brain analysis")
            runBrainAnalysis(pollObj, bnfChainJson, nfChainJson, bnfSpot, nfSpot, vix, bnfStocksJson, nf50Breadth)
            val hbTs = System.currentTimeMillis()
            prefs.edit().putLong(LEASE_HEARTBEAT_MS_KEY, hbTs).commit()
            Log.d(TAG, "LEASE_HEARTBEAT_WRITTEN: pollNum=$pollCount ts=$hbTs")
            LogBuffer.add('D', TAG, "LEASE_HEARTBEAT_WRITTEN: pollNum=$pollCount ts=$hbTs")
        } finally {
            pollInProgress.set(false)
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

    private fun optionLtp(md: JSONObject?): Double {
        if (md == null) return 0.0
        val ltp = md.optDouble("ltp", 0.0)
        return if (ltp > 0.0) ltp else md.optDouble("last_price", 0.0)
    }

    private fun optionMid(md: JSONObject?): Double {
        if (md == null) return 0.0
        val bid = md.optDouble("bid_price", 0.0)
        val ask = md.optDouble("ask_price", 0.0)
        return if (bid > 0.0 && ask > 0.0) {
            (bid + ask) / 2.0
        } else {
            val ltp = optionLtp(md)
            if (ltp > 0.0) ltp else Math.max(bid, ask)
        }
    }

    private fun optionIv(option: JSONObject?, md: JSONObject?): Double {
        val greekIv = option?.optJSONObject("option_greeks")?.optDouble("iv", 0.0) ?: 0.0
        return if (greekIv > 0.0) greekIv else (md?.optDouble("iv", 0.0) ?: 0.0)
    }

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
        val nf = data.getJSONObject("NSE_INDEX:Nifty 50").getDouble("last_price")
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
        var atmDist = Double.MAX_VALUE
        var atmStrike = 0.0
        
        val chainData = bnfChain.getJSONArray("data")
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
        poll.put("gap_sigma", dailySigma)
        
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
        return poll
    }

    private fun savePoll(poll: JSONObject) {
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
                    .apply()
            }

            // Append new poll
            history.put(poll)
            pollCount++ // A5: Monotonic increment

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
                putLong("last_successful_poll_ms", System.currentTimeMillis())
                remove(LAST_POLL_GAP_WARNING_SLOT_KEY)
                currentPollSlotKey(System.currentTimeMillis())?.let { putString(LAST_SUCCESSFUL_POLL_SLOT_KEY, it) }
            }.commit()

            // GAP 12: Institutional Positioning
            checkInstitutionalPositioning(poll)

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

    private fun checkInstitutionalPositioning(poll: JSONObject) {
        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        val cal = Calendar.getInstance(ist)
        val mins = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE)
        val today = todayIstDate()
        val lastSavedDate = prefs.getString("positioning_date", "")

        // Reset if new day
        if (lastSavedDate != today) {
            prefs.edit().apply {
                remove("afternoon_baseline")
                remove("tomorrow_signal")
                remove("has2pmSnapshot")
                remove("has315pmSnapshot")
                remove("hasDayEvalRun")
                putString("positioning_date", today)
            }.apply()
        }

        // At 2:00 PM (840 mins) to 2:10 PM
        if (mins in 840..850) {
            if (!prefs.contains("afternoon_baseline")) {
                prefs.edit().putString("afternoon_baseline", poll.toString()).apply()
                Log.d(TAG, "Captured 2PM institutional baseline")
            }
        }

        // At 3:15 PM (915 mins) to 3:30 PM
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

    private suspend fun fetchNf50Breadth(token: String): JSONObject {
        return try {
            val kotakKey = getCachedKotakKey()
            val remoteConstituents = getRemoteNf50Constituents(kotakKey)
            val nf50Constituents = remoteConstituents ?: getNf50Constituents(kotakKey)
            val keys = nf50Constituents.joinToString(",") { java.net.URLEncoder.encode(it, "UTF-8") }
            val url = "https://api.upstox.com/v2/market-quote/quotes?instrument_key=$keys"
            val response = fetchSync(url, token) ?: return defaultNf50Breadth()
            val data = response.optJSONObject("data") ?: return defaultNf50Breadth()

            var advancing = 0
            var declining = 0
            var neutral = 0
            var considered = 0
            val missingKeys = mutableListOf<String>()
            val configuredTotal = nf50Constituents.size

            for (key in nf50Constituents) {
                val quote = findQuoteByInstrument(data, key)
                val ltp = quoteLtp(quote)
                val netChange = quote?.optDouble("net_change", Double.MIN_VALUE) ?: Double.MIN_VALUE
                if (ltp <= 0.0 || netChange == Double.MIN_VALUE) {
                    missingKeys.add(key)
                    continue
                }
                considered++
                when {
                    netChange > 0.05 -> advancing++
                    netChange < -0.05 -> declining++
                    else -> neutral++
                }
            }

            val score = advancing * 100.0 + neutral * 50.0
            val pct = if (considered > 0) (score / considered).coerceIn(0.0, 100.0) else 50.0
            val advPct = if (configuredTotal > 0) {
                (advancing * 100.0 / configuredTotal).coerceIn(0.0, 100.0)
            } else 0.0
            val coverage = if (nf50Constituents.isNotEmpty()) {
                considered.toDouble() / nf50Constituents.size.toDouble()
            } else 0.0

            LogBuffer.add(
                'I',
                TAG,
                "NF50_BREADTH_RESULT: pct=${Math.round(pct * 10.0) / 10.0} adv=$advancing dec=$declining neu=$neutral considered=$considered coverage=${String.format(Locale.US, "%.3f", coverage)} missing=${missingKeys.size} source=${if (remoteConstituents != null) "remote_or_cached" else "bundled"} sample=${missingKeys.take(3).joinToString("|")}"
            )
            JSONObject().apply {
                val pctRounded = Math.round(pct * 10.0) / 10.0
                put("pct", pctRounded)
                put("advancing", advancing)
                put("declining", declining)
                put("neutral", neutral)
                put("considered", considered)
                put("total", configuredTotal)
                put("coverage", coverage)
                put("advPct", Math.round(advPct * 10.0) / 10.0)
                put("scaled", pctRounded)
                put("source", if (remoteConstituents != null) "remote_or_cached" else "bundled")
                put("missingCount", missingKeys.size)
                put("missingKeys", JSONArray(missingKeys.take(10)))
            }
        } catch (e: Exception) {
            LogBuffer.add('E', TAG, "NF50_BREADTH_ERROR: ${e.message}")
            defaultNf50Breadth()
        }
    }

    private fun defaultNf50Breadth(): JSONObject {
        return JSONObject().apply {
            put("pct", 50.0)
            put("advancing", 0)
            put("declining", 0)
            put("neutral", 0)
            put("considered", 0)
            put("total", getNf50Constituents().size)
            put("coverage", 0.0)
            put("advPct", 0.0)
            put("scaled", 50.0)
            put("source", "bundled")
            put("missingCount", 0)
            put("missingKeys", JSONArray())
        }
    }

    private fun calculateIvPercentile(vix: Double): Int {
        try {
            val histStr = prefs.getString("premium_history", "[]") ?: "[]"
            val hist = JSONArray(histStr)
            if (hist.length() < 10) return 50
            var lower = 0
            for (i in 0 until hist.length()) {
                if (vix > hist.getJSONObject(i).optDouble("vix", 0.0)) lower++
            }
            return (lower * 100 / hist.length())
        } catch (e: Exception) { return 50 }
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
                if (uniqueDays.size >= 5) break
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

    private fun formatChainForBrain(chain: JSONObject, spot: Double): JSONObject {
        val result = JSONObject()
        try {
            val data = chain.optJSONArray("data") ?: return result
            val strikesObj = JSONObject()
            val allStrikesArr = JSONArray()
            val oiRows = mutableListOf<DoubleArray>()
            
            var atmStrike = 0.0
            var atmMinDist = Double.MAX_VALUE
            var atmCEIv = 0.0
            var atmPEIv = 0.0
            var totalCallOI = 0.0
            var totalPutOI = 0.0
            var maxCallOI = 0.0
            var maxPutOI = 0.0
            var callWallStrike = 0.0
            var putWallStrike = 0.0

            for (i in 0 until data.length()) {
                val item = data.getJSONObject(i)
                val strike = item.getDouble("strike_price")
                allStrikesArr.put(strike)
                
                val call = item.optJSONObject("call_options")
                val put = item.optJSONObject("put_options")
                val callMd = call?.optJSONObject("market_data")
                val putMd = put?.optJSONObject("market_data")
                val callInstrumentKey = call?.optString("instrument_key", "")?.trim().orEmpty()
                val putInstrumentKey = put?.optString("instrument_key", "")?.trim().orEmpty()
                val callSymbol = call?.optString("trading_symbol", "")?.trim().orEmpty()
                val putSymbol = put?.optString("trading_symbol", "")?.trim().orEmpty()
                val callOI = callMd?.optDouble("oi", 0.0) ?: 0.0
                val putOI = putMd?.optDouble("oi", 0.0) ?: 0.0
                totalCallOI += callOI
                totalPutOI += putOI
                oiRows.add(doubleArrayOf(strike, callOI, putOI))
                if (callOI > maxCallOI) {
                    maxCallOI = callOI
                    callWallStrike = strike
                }
                if (putOI > maxPutOI) {
                    maxPutOI = putOI
                    putWallStrike = strike
                }
                
                val dist = Math.abs(strike - spot)
                if (dist < atmMinDist) {
                    atmMinDist = dist
                    atmStrike = strike
                    atmCEIv = optionIv(call, callMd)
                    atmPEIv = optionIv(put, putMd)
                }
                
                val strikeObj = JSONObject()
                strikeObj.put("CE", JSONObject().apply {
                    val md = callMd
                    val bid = md?.optDouble("bid_price", 0.0) ?: 0.0
                    val ask = md?.optDouble("ask_price", 0.0) ?: 0.0
                    put("ltp", optionLtp(md))
                    put("bid", bid)
                    put("ask", ask)
                    put("mid", optionMid(md))
                    put("oi", callOI)
                    put("volume", md?.optDouble("volume", 0.0) ?: 0.0)
                    put("iv", optionIv(call, md))
                    put("prev_oi", md?.optDouble("prev_oi", 0.0) ?: 0.0)  // PHASE C STEP 7.0
                    put("instrument_key", callInstrumentKey)
                    put("instrumentKey", callInstrumentKey)
                    put("trading_symbol", callSymbol)
                    put("symbol", callSymbol)
                    
                    val gr = call?.optJSONObject("option_greeks")
                    put("delta", gr?.optDouble("delta", 0.0) ?: 0.0)
                    put("gamma", gr?.optDouble("gamma", 0.0) ?: 0.0)
                    put("theta", gr?.optDouble("theta", 0.0) ?: 0.0)
                    put("vega", gr?.optDouble("vega", 0.0) ?: 0.0)        // PHASE C STEP 7.0
                    put("pop", gr?.optDouble("pop", 0.0) ?: 0.0)          // PHASE C STEP 7.0
                })
                strikeObj.put("PE", JSONObject().apply {
                    val md = putMd
                    val bid = md?.optDouble("bid_price", 0.0) ?: 0.0
                    val ask = md?.optDouble("ask_price", 0.0) ?: 0.0
                    put("ltp", optionLtp(md))
                    put("bid", bid)
                    put("ask", ask)
                    put("mid", optionMid(md))
                    put("oi", putOI)
                    put("volume", md?.optDouble("volume", 0.0) ?: 0.0)
                    put("iv", optionIv(put, md))
                    put("prev_oi", md?.optDouble("prev_oi", 0.0) ?: 0.0)  // PHASE C STEP 7.0
                    put("instrument_key", putInstrumentKey)
                    put("instrumentKey", putInstrumentKey)
                    put("trading_symbol", putSymbol)
                    put("symbol", putSymbol)
                    
                    val gr = put?.optJSONObject("option_greeks")
                    put("delta", gr?.optDouble("delta", 0.0) ?: 0.0)
                    put("gamma", gr?.optDouble("gamma", 0.0) ?: 0.0)
                    put("theta", gr?.optDouble("theta", 0.0) ?: 0.0)
                    put("vega", gr?.optDouble("vega", 0.0) ?: 0.0)        // PHASE C STEP 7.0
                    put("pop", gr?.optDouble("pop", 0.0) ?: 0.0)          // PHASE C STEP 7.0
                })
                strikesObj.put(strike.toInt().toString(), strikeObj)
            }

            var nearTotalCallOI = 0.0
            var nearTotalPutOI = 0.0
            val nearWindow = Math.max(spot * 0.015, 1.0)
            for (row in oiRows) {
                if (Math.abs(row[0] - atmStrike) <= nearWindow) {
                    nearTotalCallOI += row[1]
                    nearTotalPutOI += row[2]
                }
            }

            var maxPain = 0.0
            var minPain = Double.MAX_VALUE
            for (candidate in oiRows) {
                var pain = 0.0
                for (row in oiRows) {
                    pain += row[1] * Math.max(candidate[0] - row[0], 0.0)
                    pain += row[2] * Math.max(row[0] - candidate[0], 0.0)
                }
                if (pain < minPain) {
                    minPain = pain
                    maxPain = candidate[0]
                }
            }
            
            result.put("strikes", strikesObj)
            result.put("allStrikes", allStrikesArr)
            result.put("atm", atmStrike)
            result.put("atmIv", if (atmCEIv > 0 && atmPEIv > 0) (atmCEIv + atmPEIv) / 2.0 else Math.max(atmCEIv, atmPEIv))
            result.put("totalCallOI", totalCallOI)
            result.put("totalPutOI", totalPutOI)
            result.put("nearTotalCallOI", nearTotalCallOI)
            result.put("nearTotalPutOI", nearTotalPutOI)
            result.put("pcr", if (totalCallOI > 0.0) totalPutOI / totalCallOI else 0.0)
            result.put("nearAtmPCR", if (nearTotalCallOI > 0.0) nearTotalPutOI / nearTotalCallOI else 0.0)
            result.put("maxPain", maxPain)
            result.put("callWallStrike", callWallStrike)
            result.put("callWallOI", maxCallOI)
            result.put("putWallStrike", putWallStrike)
            result.put("putWallOI", maxPutOI)
            
        } catch (e: Exception) {
            Log.e(TAG, "Error formatting chain for brain: ${e.message}")
        }
        return result
    }

    private suspend fun runBrainAnalysis(poll: JSONObject, bnfChain: JSONObject, nfChain: JSONObject,
                                         bnfSpot: Double, nfSpot: Double, vix: Double, stocksJson: JSONObject,
                                         nf50Breadth: JSONObject) {
        var brainSuccess = false

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
            val sdf = SimpleDateFormat("yyyy-MM-dd", Locale.US)
            val todayDate = sdf.parse(todayIstDate())
            
            val bnfExpiryPref = prefs.getString("expiry_bnf", "") ?: ""
            val nfExpiryPref = prefs.getString("expiry_nf", "") ?: ""
            val bnfExpDate = try { sdf.parse(bnfExpiryPref) } catch(e: Exception) { null }
            val nfExpDate = try { sdf.parse(nfExpiryPref) } catch(e: Exception) { null }
            
            val bnfDTE = bnfExpDate?.let { (it.time - todayDate.time) / (24 * 60 * 60 * 1000L) } ?: 3
            val nfDTE = nfExpDate?.let { (it.time - todayDate.time) / (24 * 60 * 60 * 1000L) } ?: 3
            
            ctxObj.put("bnfDTE", bnfDTE)
            ctxObj.put("nfDTE",  nfDTE)

            Log.d(TAG, "BRAIN_START: Loading Chaquopy brain module")
            val py = Python.getInstance()
            val brain = py.getModule("brain")
            
            val pollsJson    = prefs.getString("poll_history",      "[]") ?: "[]"
            val baselineJson = prefs.getString("morning_baseline",  "{}") ?: "{}"
            val openTradesJson   = prefs.getString("open_trades",   "[]") ?: "[]"
            val closedTradesJson = prefs.getString("closed_trades", "[]") ?: "[]"
            
            // CHAIN MERGING (Phase C: Format raw chains for Python)
            fun mergeChain(key: String, liveChainRaw: JSONObject, spot: Double,
                           cwPoll: Double, pwPoll: Double, pcrPoll: Double, expiry: String) {
                val formattedLive = formatChainForBrain(liveChainRaw, spot)
                formattedLive.put("expiry", expiry)
                val liveCw = if (cwPoll > 0.0) cwPoll else formattedLive.optDouble("callWallStrike", 0.0)
                val livePw = if (pwPoll > 0.0) pwPoll else formattedLive.optDouble("putWallStrike", 0.0)
                val livePcr = if (pcrPoll > 0.0) pcrPoll else formattedLive.optDouble("pcr", 0.0)
                val existingChain = ctxObj.optJSONObject(key)
                
                if (existingChain != null && existingChain.has("atm")) {
                    // Rich chain exists from WebView — refresh only live intraday fields
                    existingChain.put("strikes",         formattedLive.optJSONObject("strikes"))
                    existingChain.put("atm",             formattedLive.optDouble("atm", 0.0))
                    existingChain.put("callWallStrike",  liveCw)
                    existingChain.put("putWallStrike",   livePw)
                    existingChain.put("atmIv",           formattedLive.optDouble("atmIv", 0.0))
                    existingChain.put("expiry",          expiry)
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

            mergeChain("bnfChain", bnfChain, bnfSpot, poll.optDouble("cw", 0.0), poll.optDouble("pw", 0.0), poll.optDouble("pcr", 0.0), bnfExpiryPref)
            ctxObj.optJSONObject("bnfChain")?.put("bnf_spot", bnfSpot)
            mergeChain("nfChain",  nfChain,  nfSpot,  nfCw, nfPw, 0.0, nfExpiryPref)
            ctxObj.optJSONObject("nfChain")?.put("nf_spot", nfSpot)

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
            val breadth = calculateBreadth(stocksJson)
            ctxObj.put("bnfBreadth", breadth)
            ctxObj.put("nf50Breadth", nf50Breadth)
            
            // Phase B: populate morning input + yesterday history for brain.py
            val morningInputStr = prefs.getString("morning_input", null)
            if (morningInputStr != null) {
                ctxObj.put("morning_input", JSONObject(morningInputStr))
            }
            val ydayHistStr = prefs.getString("premium_history", "[]") ?: "[]"
            ctxObj.put("yesterdayHistory", JSONArray(ydayHistStr))
            
            // Phase B: accuracy stats from Supabase (refresh once per day or per session)
            val accuracyStats = SupabaseClient.getSignalAccuracyStats()
            ctxObj.put("signalAccuracy", accuracyStats)
            val signalReliabilityRows = loadSignalReliabilityRows()
            ctxObj.put("signalReliability", signalReliabilityRows)
            ctxObj.put("elephantObserveOnly", true)
            
            // Phase B: evening close data for overnight delta
            val eveningCloseStr = prefs.getString("evening_close_baseline", null)
            if (eveningCloseStr != null) {
                ctxObj.put("eveningClose", JSONObject(eveningCloseStr))
            }
            
            // Phase B: global direction for overnight delta
            val globalDirStr = prefs.getString("global_direction", null)
            if (globalDirStr != null) {
                ctxObj.put("globalDirection", JSONObject(globalDirStr))
            }
            
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
            
            val ySigStr = prefs.getString("yesterday_signal", null)
            if (ySigStr != null) {
                val ySig = JSONObject(ySigStr)
                val sigObj = JSONObject()
                sigObj.put("signal", ySig.optString("tomorrow_signal", "NEUTRAL").replace("Tomorrow: ", "").split(" ")[0])
                sigObj.put("strength", ySig.optString("tomorrow_signal", "").let { if (it.contains("4/5")) 4 else 2 })
                ctxObj.put("yesterdaySignal", sigObj)
            }
            
            // Always overlay history from SharedPreferences for brain.py (Persistence)
            val premHist = JSONArray(prefs.getString("premium_history", "[]") ?: "[]")
            val vixHist = JSONArray()
            for (i in 0 until premHist.length()) {
                val v = premHist.getJSONObject(i).optDouble("vix", 0.0)
                if (v > 0) vixHist.put(v)
            }
            if (vixHist.length() > 0) ctxObj.put("vixHistory", vixHist)
            
            val fiiHist = extractFiiHistory()
            if (fiiHist.length() > 0) ctxObj.put("fiiHistory", fiiHist)
            
            ctxObj.put("ivPercentile", calculateIvPercentile(vix))

            ctxObj.put("capital",    prefs.getInt("capital", 250000))
            ctxObj.put("bnfExpiry",  bnfExpiryPref)
            ctxObj.put("nfExpiry",   nfExpiryPref)
            ctxObj.put("vix",        vix)
            ctxObj.put("bnfSpot",    bnfSpot)
            ctxObj.put("nfSpot",     nfSpot)
            val authToken = (prefs.getString("auth_token", "") ?: "").trim()
            val sandboxEnabled = prefs.getBoolean("execution_sandbox_enabled", false)
            val orderProxyUrl = (prefs.getString("order_proxy_url", "") ?: "").trim()
            val explicitExecutionMode = (prefs.getString("execution_mode", "") ?: "").trim().lowercase(Locale.US)
            val derivedExecutionMode = when {
                explicitExecutionMode in setOf("paper", "sandbox", "live") -> explicitExecutionMode
                sandboxEnabled -> "sandbox"
                orderProxyUrl.startsWith("https://") -> "live"
                else -> "paper"
            }
            ctxObj.put("authTokenReady", authToken.isNotEmpty())
            ctxObj.put("sandboxEnabled", sandboxEnabled)
            ctxObj.put("orderProxyUrl", orderProxyUrl)
            ctxObj.put("executionMode", derivedExecutionMode)
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

            val hasMorningInput = !morningInputStr.isNullOrBlank()
            val wallClockMinutes = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE)
            val entryWindowActive = hasMorningInput && wallClockMinutes in 660..915

            ctxObj.put("significant_move", sigMove)
            ctxObj.put("entry_window_active", entryWindowActive)
            ctxObj.put("trading_window_active", wallClockMinutes in 660..915)
            ctxObj.put("abs_spot_sigma", Math.abs(bnfSpotSigma))
            ctxObj.put("abs_nf_spot_sigma", Math.abs(nfSpotSigmaValue))
            ctxObj.put("abs_vix_sigma", Math.abs(vixSigma))

            val snap2pmStr = prefs.getString("snap_2pm_today", null)
            if (snap2pmStr != null) {
                ctxObj.put("snap_2pm_today", JSONObject(snap2pmStr))
            }

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
                        "bnfExp=$bnfExpiryPref nfExp=$nfExpiryPref bnfDTE=$bnfDTE nfDTE=$nfDTE " +
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

                    val chain = if (trade.optString("index_key", "BNF") == "NF") nfChain else bnfChain
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
            val strikeOiJson = buildStrikeOiJson(bnfChain, nfChain, openTradesJson, poll.optString("t", ""))

            // Call brain.analyze(poll_json, trades_json, baseline_json, open_trades_json, candidates_json, strike_oi_json, context_json)
            val result = runBlocking {
                withTimeoutOrNull(10_000L) {
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
            }
            
            if (result != null) {
                val resultObj = JSONObject(result)
                brainSuccess = true

                // ML Arch V2: Chain slice extraction + brain snapshot
                try {
                    val mlPersistKey = mlPersistKeyForPoll(poll)
                    if (markMlPollPersistAttempt(mlPersistKey)) {
                        val chainSliceRows = extractChainSlice(bnfChain, nfChain, resultObj)
                        val chainRowsJson = JSONArray()
                        chainSliceRows.forEach { chainRowsJson.put(it) }
                        serviceScope.launch(Dispatchers.IO) {
                            val saved = SupabaseClient.saveChainRows(chainRowsJson)
                            LogBuffer.add(
                                if (saved) 'I' else 'E',
                                TAG,
                                "ML_CHAIN_SAVE: rows=${chainSliceRows.size} saved=$saved"
                            )
                        }

                        // Take poll snapshot and save to ml_brain_snapshots
                        val snapResult = runBlocking {
                            withTimeoutOrNull(PY_SNAPSHOT_TIMEOUT_MS) {
                                brain.callAttr(
                                    "take_poll_snapshot",
                                    result,
                                    ctxObj.toString(),
                                    pollsJson
                                ).toString()
                            }
                        }
                        if (snapResult == null) {
                            Log.w(TAG, "ML_SNAPSHOT_TIMEOUT: take_poll_snapshot exceeded ${PY_SNAPSHOT_TIMEOUT_MS}ms")
                        } else {
                            val snapObj = JSONObject(snapResult)
                            // FIX: brain.py returns JSONB payload fields as pre-serialised json.dumps()
                            // strings. Re-parse each into a proper JSONObject/JSONArray so PostgREST
                            // stores them as JSONB objects, not JSONB strings. Without this, every
                            // ->> extraction on these columns returns NULL.
                            val jsonObjectFields = listOf(
                                "primary_candidate_json",
                                "context_json",
                                "verdict_json",
                                "market_forces_json",
                                "poll_summary_json"
                            )
                            val jsonArrayFields = listOf("top_candidates_json")
                            for (field in jsonObjectFields) {
                                val raw = snapObj.optString(field, "")
                                if (raw.isNotBlank()) {
                                    try { snapObj.put(field, JSONObject(raw)) }
                                    catch (_: Exception) { /* leave as-is — don't drop the row */ }
                                }
                            }
                            for (field in jsonArrayFields) {
                                val raw = snapObj.optString(field, "")
                                if (raw.isNotBlank()) {
                                    try { snapObj.put(field, JSONArray(raw)) }
                                    catch (_: Exception) { /* leave as-is — don't drop the row */ }
                                }
                            }
                            serviceScope.launch(Dispatchers.IO) {
                                val snapshotSaved = SupabaseClient.saveBrainSnapshot(snapObj)
                                if (snapshotSaved) {
                                    try {
                                        val factPack = resultObj.optJSONObject("elephant_fact_pack")
                                        if (factPack != null) {
                                            persistCompactGeneratedCandidates(factPack, snapObj)
                                        }
                                    } catch (e: Exception) {
                                        Log.w(TAG, "ML_GENERATED_CANDIDATES_FAIL: ${e.message}")
                                    }
                                }
                            }
                        }

                        launchElephantObserveOnly(resultObj)
                    } else {
                        LogBuffer.add('W', TAG, "ML_PERSIST_DEDUP_SKIP: key=$mlPersistKey")
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "ML_SNAPSHOT_FAIL: ${e.message}")
                }

                // Notification Agent: gate setup commentary (position-risk handled separately below)
                try {
                    val agentResult = runBlocking {
                        withTimeoutOrNull(PY_AGENT_TIMEOUT_MS) {
                            brain.callAttr(
                                "notification_agent_process",
                                result,
                                ctxObj.toString()
                            ).toString()
                        }
                    }
                    if (agentResult == null) {
                        Log.w(TAG, "AGENT_TIMEOUT: notification_agent_process exceeded ${PY_AGENT_TIMEOUT_MS}ms")
                    } else {
                    if (agentResult != "null" && agentResult.isNotBlank()) {
                        val agentAlert = JSONObject(agentResult)
                        val urgency = agentAlert.optString("urgency", "INFO")
                        val soundClass = agentAlert.optString("sound_class", "")
                        // Prefer explicit sound_class from brain.py; Kotlin only routes presentation.
                        val notifType = when {
                            soundClass.isNotBlank() -> soundClass
                            urgency == "HIGH" -> "entry"
                            urgency == "WARNING" -> "warning"
                            urgency == "UPDATE" -> "update"
                            urgency == "ERROR" -> "urgent"
                            urgency == "INFO" -> "routine"
                            else -> "routine"
                        }
                        NotificationHelper.send(this,
                            agentAlert.optString("title"),
                            agentAlert.optString("message"),
                            notifType
                        )
                        Log.d(TAG, "AGENT_NOTIFICATION: ${agentAlert.optString("title")}")
                    }
                    // Persist full agent state (including verdict_history) for restart survival
                    val agentStatePython = brain.callAttr("notification_agent_state_json").toString()
                    if (agentStatePython != "null" && agentStatePython.isNotBlank()) {
                        prefs.edit().putString("notification_agent_state", agentStatePython).commit()
                    }
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "AGENT_NOTIFICATION_FAIL: ${e.message}")
                }
                
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

                // Phase E: Capture snapshots using Python-computed data
                captureChainSnapshots(ctxObj, py)

                // Decision #17/#18/#Issue9: Persist brain-computed P&L and metrics back to open_trades
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
                            t.put("current_pnl", live.optDouble("current_pnl"))
                            t.put("current_spot", live.optDouble("current_spot"))
                            t.put("current_premium", live.optDouble("current_net_premium"))
                            t.put("peak_pnl", live.optDouble("peak_pnl"))
                            t.put("trough_pnl", live.optDouble("trough_pnl"))
                            t.put("peak_erosion", live.optDouble("peak_erosion"))
                            t.put("vix_change", live.optDouble("vix_change"))
                            t.put("journey", live.optJSONArray("journey"))
                            
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
                        "BRAIN_RESULT: len=${resultObj.toString().length} keys=${resultKeys.joinToString("|")} generated=${generated?.length() ?: 0} watchlist=${watchlist?.length() ?: 0} generatedBNF=$generatedBnf generatedNF=$generatedNf watchlistBNF=$watchlistBnf watchlistNF=$watchlistNf statsTotal=$statsTotal statsRejected=$statsRejected statsByBNF=${statsByIndex?.optInt("BNF", -1)} statsByNF=${statsByIndex?.optInt("NF", -1)} candidateTrace=${resultObj.has("candidateTrace")}"
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
                prefs.edit().putString("brain_result", finalBrainString).commit()
                
                val candidates = resultObj.optJSONArray("generated_candidates")
                    ?: resultObj.optJSONArray("candidates")
                if (candidates != null) {
                    prefs.edit().putString("candidates", candidates.toString()).commit()
                }
                Log.d(TAG, "BRAIN_COMPLETE: candidates=${candidates?.length() ?: 0}")
                
                sendPositionRiskAlerts(resultObj)
                
                // Phase E: Persist routine alert timestamp if routine alert was fired
                val alerts = resultObj.optJSONArray("alerts")
                if (alerts != null) {
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
                    if (candleBnf != null || candleNf != null) {
                        SupabaseClient.upsertCandleData(
                            todayIstDate(),
                            candleBnf ?: JSONObject(),
                            candleNf ?: JSONObject()
                        )
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Candle data persist failed: ${e.message}")
                }

                brainSuccess = true
            } else {
                Log.w(TAG, "BRAIN_TIMEOUT: brain.analyze timed out after 10s")
            }
            
        } catch (e: Exception) {
            Log.e(TAG, "BRAIN_ERROR: ${e.message}\n${e.stackTraceToString()}")
        } finally {
            // Keep the Binder payload empty. The WebView pulls the latest native
            // state directly, and sending the full brain payload here can exceed
            // Android's parcel limit and crash the poll loop.
            val tickIntent = Intent("com.marketradar.POLL_TICK")
            sendBroadcast(tickIntent)
            val pollCount = prefs.getInt("poll_count", 0)
            Log.d(TAG, "BROADCAST_SENT: Poll #$pollCount (brain success=$brainSuccess)")
        }
    }

    private fun extractChainSlice(bnfChain: JSONObject, nfChain: JSONObject, brainResult: JSONObject): List<JSONObject> {
        val rows = mutableListOf<JSONObject>()
        val now = System.currentTimeMillis()
        val istTimeZone = TimeZone.getTimeZone("Asia/Kolkata")
        val pollTs = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssZ", Locale.US).apply { timeZone = istTimeZone }.format(Date(now))
        val sessionDate = SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = istTimeZone }.format(Date(now))
        appendFullChainSlice(rows, bnfChain, "BNF", sessionDate, pollTs)
        appendFullChainSlice(rows, nfChain, "NF", sessionDate, pollTs)
        LogBuffer.add(
            'I',
            TAG,
            "ML_CHAIN_FULL: rows=${rows.size} pollTs=$pollTs generated=${brainResult.optJSONArray("generated_candidates")?.length() ?: 0} watchlist=${brainResult.optJSONArray("watchlist")?.length() ?: 0}"
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
                    put("bid", md.optDouble("bid_price", 0.0))
                    put("ask", md.optDouble("ask_price", 0.0))
                })
            }
        }
    }

    private fun mlPersistKeyForPoll(poll: JSONObject): String {
        val date = todayIstDate()
        val pollCount = prefs.getInt("poll_count", 0)
        val pollTime = poll.optString("t", "")
        val bnf = poll.optDouble("bnf", 0.0)
        val nf = poll.optDouble("nf", 0.0)
        return "$date|$pollCount|$pollTime|$bnf|$nf"
    }

    private fun markMlPollPersistAttempt(key: String): Boolean = synchronized(mlPersistLock) {
        val lastKey = prefs.getString("last_ml_persist_key", "") ?: ""
        if (key.isBlank() || key == lastKey) return@synchronized false
        prefs.edit().putString("last_ml_persist_key", key).commit()
        true
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
                put("bid", md.optDouble("bid_price", 0.0))
                put("ask", md.optDouble("ask_price", 0.0))
            }
        }
        return null
    }

    private fun sendPositionRiskAlerts(result: JSONObject) {
        val alerts = result.optJSONArray("alerts") ?: return
        val currentPositionKeys = mutableSetOf<String>()

        for (i in 0 until alerts.length()) {
            val alert = alerts.getJSONObject(i)
            if (alert.optString("category") != "POSITION") continue
            val key = alert.optString("key").ifBlank {
                "${alert.optString("title")}|${alert.optString("body")}|${alert.optString("priority", "urgent")}"
            }
            currentPositionKeys.add(key)
            if (lastPositionAlertKeys.contains(key)) continue
            // Critical position-risk alerts bypass notification agent entirely
            NotificationHelper.send(this,
                alert.optString("title"),
                alert.optString("body"),
                alert.optString("priority", "urgent"),
                "positions"
            )
        }

        lastPositionAlertKeys.clear()
        lastPositionAlertKeys.addAll(currentPositionKeys)
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
                    val accepted = handoffElephantObserveOnly(lanePayload)
                    LogBuffer.add(
                        if (accepted) 'I' else 'W',
                        TAG,
                        "ELEPHANT_HANDOFF: lane=$lane accepted=$accepted candidates=${laneCandidates.size}"
                    )
                } catch (e: Exception) {
                    Log.w(TAG, "ELEPHANT_OBSERVE_ONLY_FAIL lane=$lane: ${e.message}")
                }
            }
        }
    }

    private fun persistCompactGeneratedCandidates(factPack: JSONObject, snapObj: JSONObject) {
        val candidates = factPack.optJSONArray("candidates") ?: return
        val candidateCounts = factPack.optJSONObject("candidate_counts") ?: JSONObject()
        val generationSkip = factPack.optJSONObject("generation_skip_reason")
        val generationSkipSummary = generationSkip?.optString("detail")
            ?: factPack.optString("generation_skip_reason", "")
        if (candidates.length() == 0) {
            val snapshotContext = snapObj.optJSONObject("context_json")
            val rejectedRows = factPack.optJSONArray("rejected_candidates")
                ?: snapshotContext?.optJSONArray("snapshot_rejected_candidates")
            val rejectedStats = snapshotContext?.optJSONObject("snapshot_rejected_candidate_stats")
                ?: factPack.optJSONObject("rejected_candidate_stats")
            val trace = snapshotContext?.optJSONObject("candidate_generation_trace")
            val rejectedCount = rejectedStats?.optInt("total", 0) ?: 0
            val traceAccepted = trace?.optInt("accepted_count", 0) ?: 0
            val traceRejected = trace?.optInt("rejected_count", 0) ?: 0
            val byStage = rejectedStats?.optJSONObject("by_stage")
            val topStage = byStage?.keys()?.asSequence()?.map { key ->
                "$key:${byStage.optInt(key, 0)}"
            }?.take(3)?.joinToString(",") ?: ""
            val pollTs = factPack.optString("poll_timestamp", snapObj.optString("poll_ts", "")).trim()
            val diag = buildString {
                append("ML_GENERATED_CANDIDATES_SKIP: generated=0")
                append(" rejected=")
                append(rejectedCount)
                append(" traceAccepted=")
                append(traceAccepted)
                append(" traceRejected=")
                append(traceRejected)
                if (topStage.isNotBlank()) {
                    append(" byStage=")
                    append(topStage)
                }
                if (!generationSkipSummary.isNullOrBlank()) {
                    append(" skip=")
                    append(generationSkipSummary)
                }
                if (pollTs.isNotBlank()) {
                    append(" pollTs=")
                    append(pollTs)
                }
            }
            LogBuffer.add('W', TAG, diag)
            Log.w(TAG, diag)
            if (rejectedRows == null || rejectedRows.length() == 0) return

            val syntheticRows = buildRejectedCandidateRows(
                rejectedRows = rejectedRows,
                factPack = factPack,
                snapObj = snapObj,
                candidateCounts = candidateCounts,
                generationSkipSummary = generationSkipSummary
            )
            if (syntheticRows.length() == 0) return
            val savedRejected = SupabaseClient.saveGeneratedCandidates(syntheticRows)
            if (!savedRejected) {
                val sampleRow = syntheticRows.optJSONObject(0)?.toString() ?: "{}"
                Log.w(
                    TAG,
                    "ML_GENERATED_CANDIDATES_FAIL_DETAIL: rejectedOnly=true pollTs=$pollTs rows=${syntheticRows.length()} sample=${sampleRow.take(1200)}"
                )
            }
            LogBuffer.add(
                if (savedRejected) 'I' else 'W',
                TAG,
                "ML_GENERATED_CANDIDATES: saved=$savedRejected rows=${syntheticRows.length()} pollTs=$pollTs rejectedOnly=true"
            )
            return
        }

        val selected = boundedGeneratedCandidates(candidates, GENERATED_CANDIDATE_PERSIST_CAP)
        if (selected.length() == 0) return

        val pollTs = factPack.optString("poll_timestamp", snapObj.optString("poll_ts", "")).trim()
        val sessionDate = factPack.optString("session_date", snapObj.optString("session_date", "")).trim()
        val recommendationId = snapObj.opt("recommendation_id")
        val qualityTag = factPack.optString("quality_tag", "qualitative_prompt_v2")
        val decisionSource = factPack.optString("decision_source", "")
        val signalIndependence = factPack.optJSONObject("signal_independence") ?: JSONObject()

        fun JSONObject.putIfValue(key: String, value: Any?) {
            if (value == null || value == JSONObject.NULL) return
            when (value) {
                is String -> if (value.isNotBlank()) put(key, value)
                else -> put(key, value)
            }
        }

        val stableKeys = listOf(
            "snapshot_poll_ts",
            "session_date",
            "recommendation_id",
            "quality_tag",
            "decision_source",
            "candidate_id",
            "lane",
            "index_key",
            "strategy_type",
            "trade_mode",
            "rank",
            "watchlist_rank",
            "was_surfaced",
            "net_premium",
            "max_profit",
            "max_loss",
            "risk_reward",
            "premium_edge",
            "ev_per_1k",
            "est_cost",
            "capital_blocked",
            "width",
            "expiry",
            "sigma_otm",
            "iv_richness",
            "credit_width_ratio",
            "is_credit",
            "execution_ready",
            "execution_gate",
            "entry_action",
            "direction_safe",
            "brain_score",
            "p_ml",
            "ml_action",
            "ml_edge",
            "ml_regime",
            "ml_unsure",
            "ml_ood_flag",
            "signal_independence_score",
            "generated_count",
            "watchlist_count"
        )

        val rows = JSONArray()
        for (i in 0 until selected.length()) {
            val candidate = selected.optJSONObject(i) ?: continue
            val row = JSONObject().apply {
                stableKeys.forEach { put(it, JSONObject.NULL) }
            }
            row.putIfValue("snapshot_poll_ts", pollTs)
            row.putIfValue("session_date", sessionDate)
            row.putIfValue("recommendation_id", recommendationId)
            row.putIfValue("quality_tag", qualityTag)
            row.putIfValue("decision_source", decisionSource)
            row.putIfValue("candidate_id", candidate.opt("candidate_id"))
            row.putIfValue("lane", candidate.optString("lane"))
            row.putIfValue("index_key", candidate.optString("index"))
            row.putIfValue("strategy_type", candidate.optString("strategy_type"))
            row.putIfValue("trade_mode", candidate.optString("trade_mode"))
            row.putIfValue("rank", candidate.opt("rank"))
            if (!candidate.isNull("watchlist_rank")) {
                row.put("watchlist_rank", candidate.opt("watchlist_rank"))
                row.put("was_surfaced", true)
            } else {
                row.put("was_surfaced", false)
            }

            val economics = candidate.optJSONObject("economics") ?: JSONObject()
            row.putIfValue("net_premium", economics.opt("net_premium"))
            row.putIfValue("max_profit", economics.opt("max_profit"))
            row.putIfValue("max_loss", economics.opt("max_loss"))
            row.putIfValue("risk_reward", economics.opt("risk_reward"))
            row.putIfValue("premium_edge", economics.opt("premium_edge"))
            row.putIfValue("ev_per_1k", economics.opt("ev_per_1k"))
            row.putIfValue("est_cost", economics.opt("est_cost"))
            row.putIfValue("capital_blocked", economics.opt("capital_blocked"))

            val structure = candidate.optJSONObject("structure") ?: JSONObject()
            row.putIfValue("width", structure.opt("width"))
            row.putIfValue("expiry", structure.optString("expiry"))
            row.putIfValue("sigma_otm", structure.opt("sigma_otm"))
            row.putIfValue("iv_richness", structure.opt("iv_richness"))
            row.putIfValue("credit_width_ratio", structure.opt("credit_width_ratio"))
            row.putIfValue("is_credit", structure.opt("is_credit"))

            val execution = candidate.optJSONObject("execution") ?: JSONObject()
            row.putIfValue("execution_ready", execution.opt("execution_ready"))
            row.putIfValue("execution_gate", execution.optString("execution_gate"))
            row.putIfValue("entry_action", execution.optString("entry_action"))
            row.putIfValue("direction_safe", execution.opt("direction_safe"))

            val overlay = candidate.optJSONObject("ml_overlay") ?: JSONObject()
            row.putIfValue("brain_score", overlay.opt("brain_score"))
            row.putIfValue("p_ml", overlay.opt("p_ml"))
            row.putIfValue("ml_action", overlay.optString("ml_action"))
            row.putIfValue("ml_edge", overlay.opt("ml_edge"))
            row.putIfValue("ml_regime", overlay.optString("ml_regime"))
            row.putIfValue("ml_unsure", overlay.opt("ml_unsure"))
            row.putIfValue("ml_ood_flag", overlay.opt("ml_ood_flag"))

            row.putIfValue("signal_independence_score", signalIndependence.opt("score"))
            row.putIfValue("generated_count", candidateCounts.opt("generated"))
            row.putIfValue("watchlist_count", candidateCounts.opt("watchlist"))
            rows.put(row)
        }

        if (rows.length() == 0) return
        val saved = SupabaseClient.saveGeneratedCandidates(rows)
        if (!saved) {
            val sampleRow = rows.optJSONObject(0)?.toString() ?: "{}"
            Log.w(
                TAG,
                "ML_GENERATED_CANDIDATES_FAIL_DETAIL: pollTs=$pollTs rows=${rows.length()} sample=${sampleRow.take(1200)}"
            )
        }
        LogBuffer.add(
            if (saved) 'I' else 'W',
            TAG,
            "ML_GENERATED_CANDIDATES: saved=$saved rows=${rows.length()} pollTs=$pollTs"
        )
    }

    private fun buildRejectedCandidateRows(
        rejectedRows: JSONArray,
        factPack: JSONObject,
        snapObj: JSONObject,
        candidateCounts: JSONObject,
        generationSkipSummary: String
    ): JSONArray {
        val pollTs = factPack.optString("poll_timestamp", snapObj.optString("poll_ts", "")).trim()
        val sessionDate = factPack.optString("session_date", snapObj.optString("session_date", "")).trim()
        val recommendationId = snapObj.opt("recommendation_id")
        val qualityTag = factPack.optString("quality_tag", "qualitative_prompt_v2")
        val decisionSource = factPack.optString("decision_source", "")
        val stableKeys = listOf(
            "snapshot_poll_ts",
            "session_date",
            "recommendation_id",
            "quality_tag",
            "decision_source",
            "candidate_id",
            "lane",
            "index_key",
            "strategy_type",
            "trade_mode",
            "rank",
            "watchlist_rank",
            "was_surfaced",
            "net_premium",
            "max_profit",
            "max_loss",
            "risk_reward",
            "premium_edge",
            "ev_per_1k",
            "est_cost",
            "capital_blocked",
            "width",
            "expiry",
            "sigma_otm",
            "iv_richness",
            "credit_width_ratio",
            "is_credit",
            "execution_ready",
            "execution_gate",
            "entry_action",
            "direction_safe",
            "brain_score",
            "p_ml",
            "ml_action",
            "ml_edge",
            "ml_regime",
            "ml_unsure",
            "ml_ood_flag",
            "signal_independence_score",
            "generated_count",
            "watchlist_count"
        )

        fun JSONObject.putIfValue(key: String, value: Any?) {
            if (value == null || value == JSONObject.NULL) return
            when (value) {
                is String -> if (value.isNotBlank()) put(key, value)
                else -> put(key, value)
            }
        }

        fun syntheticRejectedId(candidate: JSONObject, ordinal: Int): String {
            val raw = listOf(
                pollTs,
                candidate.optString("index"),
                candidate.optString("lane"),
                candidate.optString("strategy_type"),
                candidate.optString("expiry"),
                candidate.opt("sellStrike"),
                candidate.optString("sellType"),
                candidate.opt("buyStrike"),
                candidate.optString("buyType"),
                candidate.optString("rejection_stage"),
                candidate.optString("rejection_reason"),
                ordinal.toString()
            ).joinToString("|")
            return "rej_${Integer.toUnsignedString(raw.hashCode(), 16)}"
        }

        val bounded = boundedGeneratedCandidates(rejectedRows, GENERATED_CANDIDATE_PERSIST_CAP)
        val rows = JSONArray()
        for (i in 0 until bounded.length()) {
            val candidate = bounded.optJSONObject(i) ?: continue
            val row = JSONObject().apply {
                stableKeys.forEach { put(it, JSONObject.NULL) }
            }
            val rejectionStage = candidate.optString("rejection_stage").trim()
            val rejectionReason = candidate.optString("rejection_reason").trim()
            row.putIfValue("snapshot_poll_ts", pollTs)
            row.putIfValue("session_date", sessionDate)
            row.putIfValue("recommendation_id", recommendationId)
            row.putIfValue("quality_tag", qualityTag)
            row.putIfValue("decision_source", if (decisionSource.isBlank()) "REJECTED_CANDIDATE" else decisionSource)
            row.putIfValue("candidate_id", syntheticRejectedId(candidate, i))
            row.putIfValue("lane", candidate.optString("lane"))
            row.putIfValue("index_key", candidate.optString("index"))
            row.putIfValue("strategy_type", candidate.optString("strategy_type"))
            row.putIfValue("trade_mode", candidate.optString("lane").substringAfter('_', ""))
            row.put("was_surfaced", false)
            row.putIfValue("net_premium", candidate.opt("netPremium"))
            row.putIfValue("max_profit", candidate.opt("maxProfit"))
            row.putIfValue("max_loss", candidate.opt("maxLoss"))
            row.putIfValue("width", candidate.opt("width"))
            row.putIfValue("expiry", candidate.optString("expiry"))
            row.putIfValue("sigma_otm", candidate.opt("sigmaOTM"))
            row.putIfValue("iv_richness", candidate.opt("ivRichness"))
            row.putIfValue("credit_width_ratio", candidate.opt("creditWidthRatio"))
            row.putIfValue("is_credit", candidate.opt("is_credit"))
            row.putIfValue("execution_ready", false)
            row.putIfValue("execution_gate", if (rejectionStage.isBlank()) "rejected" else "rejected:$rejectionStage")
            row.putIfValue(
                "entry_action",
                when {
                    rejectionReason.isNotBlank() -> rejectionReason
                    generationSkipSummary.isNotBlank() -> generationSkipSummary
                    else -> "REJECTED"
                }
            )
            row.putIfValue("ml_action", "REJECTED")
            row.putIfValue("generated_count", candidateCounts.opt("generated"))
            row.putIfValue("watchlist_count", candidateCounts.opt("watchlist"))
            rows.put(row)
        }
        return rows
    }

    private fun boundedGeneratedCandidates(candidates: JSONArray, cap: Int): JSONArray {
        if (cap <= 0 || candidates.length() == 0) return JSONArray()

        fun candidateId(candidate: JSONObject): String =
            candidate.optString("candidate_id", candidate.optString("id", "")).trim()

        val surfaced = mutableListOf<JSONObject>()
        val unsurfacedByLane = linkedMapOf<String, MutableList<JSONObject>>()
        for (i in 0 until candidates.length()) {
            val candidate = candidates.optJSONObject(i) ?: continue
            val lane = candidate.optString("lane", "").trim()
            if (!candidate.isNull("watchlist_rank")) {
                surfaced.add(JSONObject(candidate.toString()))
            } else {
                unsurfacedByLane.getOrPut(lane.ifBlank { "unknown" }) { mutableListOf() }
                    .add(JSONObject(candidate.toString()))
            }
        }

        surfaced.sortWith(
            compareBy<JSONObject> { candidate ->
                if (candidate.isNull("watchlist_rank")) Int.MAX_VALUE else candidate.optInt("watchlist_rank", Int.MAX_VALUE)
            }.thenBy { candidate ->
                if (candidate.isNull("rank")) Int.MAX_VALUE else candidate.optInt("rank", Int.MAX_VALUE)
            }
        )

        val selected = JSONArray()
        val seenIds = linkedSetOf<String>()

        fun addCandidate(candidate: JSONObject): Boolean {
            if (selected.length() >= cap) return false
            val id = candidateId(candidate)
            if (id.isNotBlank() && !seenIds.add(id)) return true
            selected.put(candidate)
            return selected.length() < cap
        }

        for (candidate in surfaced) {
            if (!addCandidate(candidate)) return selected
        }

        var madeProgress: Boolean
        do {
            madeProgress = false
            for ((_, laneCandidates) in unsurfacedByLane) {
                if (selected.length() >= cap) break
                if (laneCandidates.isEmpty()) continue
                val candidate = laneCandidates.removeAt(0)
                madeProgress = true
                if (!addCandidate(candidate)) return selected
            }
        } while (madeProgress && selected.length() < cap)

        return selected
    }

    private fun fetchSync(url: String, token: String): JSONObject? {
        val request = Request.Builder()
            .url(url)
            .addHeader("Authorization", "Bearer $token")
            .addHeader("Accept", "application/json")
            .build()

        return try {
            client.newCall(request).execute().use { response ->
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
                    JSONObject(response.body?.string() ?: "{}")
                }
            }
        } catch (e: Exception) {
            null
        }
    }

    private fun resolveNearestExpiryLive(instrumentKey: String, token: String, fallback: String): String {
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
            when {
                !nearest.isNullOrBlank() -> nearest
                fallback.isNotBlank() && fallback >= today -> fallback
                else -> getNextThursday()
            }
        } catch (e: Exception) {
            if (fallback.isNotBlank() && fallback >= today) fallback else getNextThursday()
        }
    }

    private fun refreshActiveExpiries(token: String): Pair<String, String> {
        val today = todayIstDate()
        val nextThu = getNextThursday()
        val storedBnf = (prefs.getString("expiry_bnf", nextThu) ?: nextThu).trim()
        val storedNf = (prefs.getString("expiry_nf", storedBnf.ifBlank { nextThu }) ?: storedBnf.ifBlank { nextThu }).trim()
        val lastRefreshDate = prefs.getString(EXPIRY_REFRESH_DATE_KEY, "") ?: ""

        if (
            lastRefreshDate == today &&
            storedBnf.isNotBlank() && storedBnf >= today &&
            storedNf.isNotBlank() && storedNf >= today
        ) {
            return Pair(storedBnf, storedNf)
        }

        val resolvedBnf = resolveNearestExpiryLive("NSE_INDEX|Nifty Bank", token, storedBnf.ifBlank { nextThu })
        val resolvedNf = resolveNearestExpiryLive("NSE_INDEX|Nifty 50", token, storedNf.ifBlank { resolvedBnf })

        prefs.edit()
            .putString("expiry_bnf", resolvedBnf)
            .putString("expiry_nf", resolvedNf)
            .putString(EXPIRY_REFRESH_DATE_KEY, today)
            .commit()

        Log.i(TAG, "EXPIRY_REFRESH: bnf=$resolvedBnf nf=$resolvedNf")
        LogBuffer.add('I', TAG, "EXPIRY_REFRESH: bnf=$resolvedBnf nf=$resolvedNf")
        return Pair(resolvedBnf, resolvedNf)
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
        return mins in 555..930 // 9:15 AM to 3:30 PM IST
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

    private fun captureChainSnapshots(ctx: JSONObject, py: Python) {
        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        val cal = Calendar.getInstance(ist)
        val mins = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE)
        
        // 2:00 PM Window (13:45 - 14:30)
        if (mins in 825..870 && !prefs.getBoolean("has2pmSnapshot", false)) {
            Log.d(TAG, "SNAPSHOT_TRIGGER: Capturing 2pm snapshot")
            prefs.edit().putBoolean("has2pmSnapshot", true).apply()
            serviceScope.launch(Dispatchers.IO) {
                try {
                    val brain = py.getModule("brain")
                    val snapJson = brain.callAttr("build_chain_snapshot_data", ctx.toString()).toString()
                    val data = JSONObject(snapJson)
                    
                    if (SupabaseClient.saveChainSnapshot("2pm", data)) {
                        prefs.edit().putString("snap_2pm_today", snapJson).apply()
                        Log.i(TAG, "SNAPSHOT_SAVED: 2pm snapshot synced to Supabase & Prefs")
                    } else {
                        prefs.edit().remove("has2pmSnapshot").apply()
                    }
                } catch (e: Exception) {
                    prefs.edit().remove("has2pmSnapshot").apply()
                    Log.e(TAG, "SNAPSHOT_ERROR 2pm: ${e.message}")
                }
            }
        }
        
        // 3:15 PM Window (15:00 - 15:30)
        if (mins in 900..930 && !prefs.getBoolean("has315pmSnapshot", false)) {
            Log.d(TAG, "SNAPSHOT_TRIGGER: Capturing 315pm snapshot")
            prefs.edit().putBoolean("has315pmSnapshot", true).apply()
            serviceScope.launch(Dispatchers.IO) {
                try {
                    val brain = py.getModule("brain")
                    val snapJson = brain.callAttr("build_chain_snapshot_data", ctx.toString()).toString()
                    val data = JSONObject(snapJson)
                    
                    if (SupabaseClient.saveChainSnapshot("315pm", data)) {
                        Log.i(TAG, "SNAPSHOT_SAVED: 315pm snapshot synced to Supabase")
                    } else {
                        prefs.edit().remove("has315pmSnapshot").apply()
                    }
                } catch (e: Exception) {
                    prefs.edit().remove("has315pmSnapshot").apply()
                    Log.e(TAG, "SNAPSHOT_ERROR 315pm: ${e.message}")
                }
            }
        }
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
            performPoll(token)
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
        if (prefs.getString(LAST_POLL_DISPATCH_SLOT_KEY, "") == slotKey) {
            LogBuffer.add('D', TAG, "POLL_DISPATCH_DEDUP[$trigger]: slot_already_dispatched=$slotKey")
            return
        }

        prefs.edit().putString(LAST_POLL_DISPATCH_SLOT_KEY, slotKey).commit()
        LogBuffer.add('I', TAG, "POLL_DISPATCH[$trigger]: slot=$slotKey")
        performPoll(token)
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
            val triggerAt = SystemClock.elapsedRealtime() + SERVICE_SELF_HEAL_DELAY_MS
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S || am.canScheduleExactAlarms()) {
                am.setExactAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, triggerAt, selfHealIntent())
            } else {
                am.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, triggerAt, selfHealIntent())
            }
            LogBuffer.add('W', TAG, "SERVICE_SELF_HEAL_SCHEDULED")
        } catch (e: Exception) {
            LogBuffer.add('W', TAG, "SERVICE_SELF_HEAL_FAIL: ${e.message}")
        }
    }

    override fun onDestroy() {
        // E5: Ensure wakelock is released when service is killed
        releaseWakeLock()
        releaseSessionWakeLock()
        scheduleSelfHealRestart()
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

    private fun nextPollNumberForToday(): Int {
        val lastPollDate = prefs.getString("last_poll_date", "") ?: ""
        return if (lastPollDate == todayIstDate()) prefs.getInt("poll_count", 0) + 1 else 1
    }
}
