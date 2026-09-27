package com.marketradar.app

import android.content.Context
import android.content.SharedPreferences
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.util.Log
import com.marketradar.app.util.LogBuffer
import org.json.JSONArray
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * B1.1 Android wiring for [drainPositionTickQueue].
 *
 * Upload runs on one dedicated background thread, independent of
 * PositionTickService: it needs no open trade, no market session and no
 * foreground service, and it never fetches quotes or evaluates policy.
 *
 * Triggers: Application start (covers app upgrade, since the replaced package's
 * process starts fresh), MarketLifecycleReceiver (MY_PACKAGE_REPLACED / BOOT /
 * time changes), connectivity recovery (default-network callback), tick-service
 * start/stop, the capture loop's no-trade/closed-session exits, ensureRunning()
 * when capture is not eligible, and after every enqueue.
 */
internal object PositionTickUploadRunner {
    private const val TAG = "PositionTickDrain"
    private const val PREFS_NAME = "market_radar"

    private val executor = Executors.newSingleThreadExecutor { r ->
        Thread(r, "position-tick-drain").apply { isDaemon = true }
    }
    private val queued = AtomicBoolean(false)
    private val connectivityRegistered = AtomicBoolean(false)
    @Volatile private var pendingTrigger: String = PositionTickDrainTrigger.APP_START
    /** Per-process cache of the read-only schema probe (gate runtime half). */
    @Volatile private var clientEventIdColumnConfirmed: Boolean? = null

    /** App/process start: register connectivity recovery and drain once. */
    fun onProcessStart(context: Context, trigger: String = PositionTickDrainTrigger.APP_START) {
        registerConnectivityTrigger(context.applicationContext)
        request(context, trigger)
    }

    /** Coalescing trigger: at most one queued drain; running drains are serialized. */
    fun request(context: Context, trigger: String) {
        val app = context.applicationContext
        pendingTrigger = trigger
        if (!queued.compareAndSet(false, true)) return
        try {
            executor.execute {
                queued.set(false)
                val t = pendingTrigger
                try {
                    runOnce(app, t)
                } catch (e: Exception) {
                    // Never let a drain bug take down a caller; class name only.
                    Log.e(TAG, "POSITION_TICK_DRAIN_ERROR: trigger=$t ex=${e.javaClass.simpleName}")
                    LogBuffer.add('E', TAG, "POSITION_TICK_DRAIN_ERROR: trigger=$t ex=${e.javaClass.simpleName}")
                }
            }
        } catch (e: Exception) {
            queued.set(false)
            Log.e(TAG, "POSITION_TICK_DRAIN_SCHEDULE_FAIL: ex=${e.javaClass.simpleName}")
        }
    }

    private fun runOnce(context: Context, trigger: String) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val depth = synchronized(PositionTickQueueLock) { loadQueue(prefs).length() }
        if (depth == 0) return
        val network = isNetworkAvailable(context)
        if (!positionTickUploadEligible(depth, network)) {
            LogBuffer.add('D', TAG, "POSITION_TICK_DRAIN_SKIP: trigger=$trigger reason=no_network depth=$depth")
            return
        }
        val now = System.currentTimeMillis()
        val failures = prefs.getInt(PREF_POSITION_TICK_FLUSH_FAILURE_COUNT, 0)
        val lastClass = prefs.getString(PREF_POSITION_TICK_FLUSH_LAST_CLASS, POSITION_TICK_FLUSH_UNKNOWN)
        val decision = decidePositionTickDrainAttempt(
            trigger, now, prefs.getLong(PREF_POSITION_TICK_LAST_FLUSH_MS, 0L), failures, lastClass
        )
        if (!decision.allowed) {
            if (failures > 0) {
                LogBuffer.add(
                    'D', TAG,
                    "POSITION_TICK_FLUSH_BACKOFF: trigger=$trigger wait_ms=${decision.waitMs} " +
                        "reason=${decision.reason} consecutive=$failures class=$lastClass depth=$depth"
                )
            }
            return
        }
        prefs.edit().putLong(PREF_POSITION_TICK_LAST_FLUSH_MS, now).commit()

        val sendId = resolveClientEventIdGate()
        val started = System.currentTimeMillis()
        val report = drainPositionTickQueue(
            store = PrefsQueueStore(prefs),
            transport = PositionTickTransport { rows -> SupabaseClient.insertPositionTicksDetailed(rows) },
            config = PositionTickDrainConfig(sendClientEventId = sendId)
        )
        val elapsed = System.currentTimeMillis() - started

        val overflowFlag = prefs.getBoolean(PREF_POSITION_TICK_OVERFLOW_ACTIVE, false)
        val rejectedTotal = prefs.getLong(PREF_POSITION_TICK_OVERFLOW_REJECTED_COUNT, 0L)
        val tracking = positionTickTrackingAfterDrain(
            report, overflowFlag, prefs.getBoolean(PREF_POSITION_TICK_TRACKING_COMPLETE, true), rejectedTotal
        )
        val editor = prefs.edit()
            .putBoolean(PREF_POSITION_TICK_OVERFLOW_ACTIVE, tracking.overflowActive)
            .putBoolean(PREF_POSITION_TICK_TRACKING_COMPLETE, tracking.trackingComplete)
        val failure = report.failure
        if (failure == null) {
            editor.putInt(PREF_POSITION_TICK_FLUSH_FAILURE_COUNT, 0)
                .putString(PREF_POSITION_TICK_FLUSH_LAST_CLASS, POSITION_TICK_FLUSH_OK)
        } else {
            editor.putInt(PREF_POSITION_TICK_FLUSH_FAILURE_COUNT, failures + 1)
                .putString(PREF_POSITION_TICK_FLUSH_LAST_CLASS, failure.failureClass)
        }
        editor.commit()

        val line = formatPositionTickDrainLog(
            trigger, report, tracking.overflowActive, rejectedTotal, tracking.trackingComplete, elapsed
        )
        if (failure == null) {
            Log.i(TAG, line); LogBuffer.add('I', TAG, line)
        } else {
            val failLine = formatPositionTickFlushFailLog(
                consecutive = failures + 1,
                pending = report.remaining,
                result = failure,
                backoffMs = computePositionTickFlushBackoffMs(failures + 1, failure.failureClass),
                overflowActive = tracking.overflowActive,
                trackingComplete = tracking.trackingComplete
            )
            Log.w(TAG, line); LogBuffer.add('W', TAG, line)
            Log.w(TAG, failLine); LogBuffer.add('W', TAG, failLine)
        }
        if (report.contentConflicts > 0) {
            LogBuffer.add(
                'W', TAG,
                "POSITION_TICK_QUEUE_CONTENT_CONFLICT: conflicts=${report.contentConflicts} " +
                    "pending=${report.remaining} retained_all=true"
            )
        }
        if (report.outcome == PositionTickDrainOutcome.BUDGET_EXHAUSTED && report.remaining > 0) {
            request(context, PositionTickDrainTrigger.CONTINUE)
        }
    }

    private fun resolveClientEventIdGate(): Boolean {
        if (!POSITION_TICK_CLIENT_EVENT_ID_SEND_COMPILED) return false
        var confirmed = clientEventIdColumnConfirmed
        if (confirmed != true) {
            confirmed = SupabaseClient.probePositionTickClientEventIdColumn()
            clientEventIdColumnConfirmed = confirmed
            LogBuffer.add('I', TAG, "POSITION_TICK_IDENTITY_PROBE: confirmed=${confirmed ?: "unknown"}")
        }
        return positionTickClientEventIdSendAllowed(POSITION_TICK_CLIENT_EVENT_ID_SEND_COMPILED, confirmed)
    }

    private fun registerConnectivityTrigger(app: Context) {
        if (!connectivityRegistered.compareAndSet(false, true)) return
        try {
            val cm = app.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager ?: return
            cm.registerDefaultNetworkCallback(object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: Network) {
                    request(app, PositionTickDrainTrigger.CONNECTIVITY)
                }
            })
        } catch (e: Exception) {
            connectivityRegistered.set(false)
            Log.w(TAG, "POSITION_TICK_CONNECTIVITY_REGISTER_FAIL: ex=${e.javaClass.simpleName}")
        }
    }

    private fun isNetworkAvailable(context: Context): Boolean = try {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
        val caps = cm?.activeNetwork?.let { cm.getNetworkCapabilities(it) }
        caps?.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) == true
    } catch (_: Exception) {
        // Unknown state: try; a failed POST is classified and backed off normally.
        true
    }

    private fun loadQueue(prefs: SharedPreferences): JSONArray = try {
        JSONArray(prefs.getString(PREF_POSITION_TICK_PENDING_QUEUE, "[]") ?: "[]")
    } catch (_: Exception) {
        JSONArray()
    }

    private class PrefsQueueStore(private val prefs: SharedPreferences) : PositionTickQueueStore {
        override fun load(): JSONArray = try {
            JSONArray(prefs.getString(PREF_POSITION_TICK_PENDING_QUEUE, "[]") ?: "[]")
        } catch (_: Exception) {
            JSONArray()
        }

        override fun save(queue: JSONArray) {
            prefs.edit().putString(PREF_POSITION_TICK_PENDING_QUEUE, queue.toString()).commit()
        }
    }
}
