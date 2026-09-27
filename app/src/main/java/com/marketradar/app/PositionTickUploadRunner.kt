package com.marketradar.app

import android.content.Context
import android.content.SharedPreferences
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.util.Log
import com.marketradar.app.util.LogBuffer
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
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
 *
 * B1.1 R2 (Codex §4): every pass that cannot finish the queue records a DURABLE
 * follow-up — the unique WorkManager one-time job [POSITION_TICK_DRAIN_WORK_NAME]
 * with a CONNECTED constraint (see PositionTickDrainCoordinator). Boot and
 * package-replaced receivers enqueue that job directly under goAsync().
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
                    // B1.1 R3: an unexpected pass exception arms a durable fallback job.
                    runPositionTickExecutorPass(
                        kv = SharedPrefsPositionTickKv(prefs(app)),
                        scheduler = WorkManagerPositionTickScheduler(app),
                        nowMs = { System.currentTimeMillis() },
                        trigger = t,
                        log = ::logLine
                    ) { runOnce(app, t) }
                } catch (e: Exception) {
                    // Never let a drain bug take down a caller; class name only.
                    logLine('E', "POSITION_TICK_DRAIN_ERROR: trigger=$t ex=${e.javaClass.simpleName} durable_fallback=failed")
                }
            }
        } catch (e: Exception) {
            queued.set(false)
            Log.e(TAG, "POSITION_TICK_DRAIN_SCHEDULE_FAIL: ex=${e.javaClass.simpleName}")
        }
    }

    /** Executor-thread pass (in-process triggers). */
    private fun runOnce(context: Context, trigger: String) {
        coordinator(context).runPass(trigger)
    }

    /**
     * Durable WorkManager job: one pass; follow-ups are scheduled durably by the
     * coordinator. An unexpected exception → RETRY (bounded explicit backoff).
     */
    fun runWorkerPass(context: Context, runAttemptCount: Int): PositionTickWorkerResult {
        val app = context.applicationContext
        return runPositionTickWorker(
            kv = SharedPrefsPositionTickKv(prefs(app)),
            nowMs = { System.currentTimeMillis() },
            runAttemptCount = runAttemptCount,
            log = ::logLine
        ) { coordinator(app).runWorkerPass() }
    }

    /**
     * Receiver path (BOOT / MY_PACKAGE_REPLACED / time changes): enqueue the durable
     * job directly; [finish] (PendingResult.finish) runs once the enqueue is durable.
     */
    fun enqueueDurableFromReceiver(context: Context, finish: () -> Unit) {
        val app = context.applicationContext
        enqueuePositionTickDrainFromReceiver(
            SharedPrefsPositionTickKv(prefs(app)), WorkManagerPositionTickScheduler(app), System.currentTimeMillis(),
            ::logLine, finish
        )
    }

    private fun logLine(level: Char, line: String) {
        when (level) {
            'W' -> Log.w(TAG, line)
            'E' -> Log.e(TAG, line)
            'I' -> Log.i(TAG, line)
            else -> Log.d(TAG, line)
        }
        LogBuffer.add(level, TAG, line)
    }

    private fun prefs(context: Context): SharedPreferences =
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    private fun coordinator(context: Context): PositionTickDrainCoordinator {
        val app = context.applicationContext
        return PositionTickDrainCoordinator(
            kv = SharedPrefsPositionTickKv(prefs(app)),
            transport = PositionTickTransport { rows -> SupabaseClient.insertPositionTicksDetailed(rows) },
            networkAvailable = { isNetworkAvailable(app) },
            clock = { System.currentTimeMillis() },
            scheduler = WorkManagerPositionTickScheduler(app),
            sendClientEventId = { resolveClientEventIdGate() },
            log = ::logLine
        )
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

    /** Unique one-time WorkManager job with a CONNECTED constraint (REPLACE = reschedule to the given delay). */
    private class WorkManagerPositionTickScheduler(private val app: Context) : PositionTickWorkScheduler {
        override fun enqueue(delayMs: Long, onResult: (Boolean) -> Unit) {
            val request = OneTimeWorkRequestBuilder<PositionTickDrainWorker>()
                .setInitialDelay(maxOf(0L, delayMs), TimeUnit.MILLISECONDS)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, POSITION_TICK_WORKER_RETRY_BACKOFF_MS, TimeUnit.MILLISECONDS)
                .build()
            // May throw synchronously (WorkManager unavailable): the caller clears its reservation.
            val operation = WorkManager.getInstance(app)
                .enqueueUniqueWork(POSITION_TICK_DRAIN_WORK_NAME, ExistingWorkPolicy.REPLACE, request)
            val future = operation.result
            // Success only if the operation's future completes normally (get() inside the listener).
            future.addListener({ completePositionTickEnqueueFuture(future, onResult) }, Runnable::run)
        }

        override fun cancel() {
            WorkManager.getInstance(app).cancelUniqueWork(POSITION_TICK_DRAIN_WORK_NAME)
        }
    }
}

/** SharedPreferences-backed [PositionTickKv] (drain, worker, receiver and the capture enqueue). */
internal class SharedPrefsPositionTickKv(private val prefs: SharedPreferences) : PositionTickKv {
    override fun getString(key: String, def: String?): String? = prefs.getString(key, def)
    override fun getInt(key: String, def: Int): Int = prefs.getInt(key, def)
    override fun getLong(key: String, def: Long): Long = prefs.getLong(key, def)
    override fun getBoolean(key: String, def: Boolean): Boolean = prefs.getBoolean(key, def)
    override fun edit(durable: Boolean, block: PositionTickKvEditor.() -> Unit) {
        val editor = prefs.edit()
        object : PositionTickKvEditor {
            override fun putString(key: String, value: String) { editor.putString(key, value) }
            override fun putInt(key: String, value: Int) { editor.putInt(key, value) }
            override fun putLong(key: String, value: Long) { editor.putLong(key, value) }
            override fun putBoolean(key: String, value: Boolean) { editor.putBoolean(key, value) }
        }.block()
        if (durable) editor.commit() else editor.apply()
    }
}
