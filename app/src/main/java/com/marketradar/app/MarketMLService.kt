// MarketMLService.kt
// Market Radar v2.1 — Nightly ML Training Service
// Add to: E:\APP\Marketapp-main\app\src\main\java\com\marketradar\app\
//
// Wires together:
//   - AlarmManager: triggers at 11 PM nightly
//   - Chaquopy: runs ml_train.py on device
//   - Supabase: stores training results to ml_models + ml_performance
//   - NativeBridge: exposes ML model status to WebView
//
// Manifest entry (add to AndroidManifest.xml inside <application>):
//   <service android:name=".MarketMLService" android:exported="false"/>
//   <receiver android:name=".MLAlarmReceiver" android:exported="false"/>

package com.marketradar.app

import android.app.AlarmManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import com.chaquo.python.Python
import com.chaquo.python.PyObject
import kotlinx.coroutines.*
import java.io.File
import java.util.Calendar

// ─────────────────────────────────────────────────────────────────────────────
// ALARM RECEIVER — wakes up at 11 PM and starts training
// ─────────────────────────────────────────────────────────────────────────────

class MLAlarmReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        Log.i("MLAlarmReceiver", "11 PM alarm fired — starting ML training")
        val svc = Intent(context, MarketMLService::class.java)
        svc.action = "ACTION_TRAIN_NIGHTLY"
        context.startForegroundService(svc)
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// ML SERVICE
// ─────────────────────────────────────────────────────────────────────────────

class MarketMLService : Service() {

    companion object {
        private const val TAG = "MarketMLService"

        // File paths inside app's internal storage
        fun backtestPath(ctx: Context): String =
            File(ctx.filesDir, "backtest_trades.csv").absolutePath

        fun appTradesPath(ctx: Context): String =
            File(ctx.filesDir, "app_trades.json").absolutePath

        fun modelPath(ctx: Context): String =
            File(ctx.filesDir, "ml_model.json").absolutePath

        fun temporalModelPath(ctx: Context): String =
            File(ctx.filesDir, "temporal_model.json").absolutePath

        // ── Schedule nightly 11 PM alarm ─────────────────────────────────
        fun scheduleNightlyTraining(context: Context) {
            val am = context.getSystemService(ALARM_SERVICE) as AlarmManager
            val intent = PendingIntent.getBroadcast(
                context, 0,
                Intent(context, MLAlarmReceiver::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )

            // Next 11:00 PM
            val cal = Calendar.getInstance().apply {
                set(Calendar.HOUR_OF_DAY, 23)
                set(Calendar.MINUTE, 0)
                set(Calendar.SECOND, 0)
                if (timeInMillis <= System.currentTimeMillis()) {
                    add(Calendar.DAY_OF_YEAR, 1)
                }
            }

            am.setInexactRepeating(
                AlarmManager.RTC_WAKEUP,
                cal.timeInMillis,
                AlarmManager.INTERVAL_DAY,
                intent
            )
            Log.i(TAG, "Nightly ML training scheduled at 11 PM")
        }

        // ── Cancel alarm ─────────────────────────────────────────────────
        fun cancelNightlyTraining(context: Context) {
            val am = context.getSystemService(ALARM_SERVICE) as AlarmManager
            val intent = PendingIntent.getBroadcast(
                context, 0,
                Intent(context, MLAlarmReceiver::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            am.cancel(intent)
        }

        // ── Validate model is loaded and usable ───────────────────────────
        fun validateModel(context: Context): MLModelStatus {
            return try {
                val py = Python.getInstance()
                val module = py.getModule("ml_train")
                val result = runBlocking {
                    withTimeoutOrNull(10_000L) {
                        module.callAttr("validate_model", modelPath(context)).toString()
                    }
                } ?: return MLModelStatus(ok = false, error = "Timeout")
                val json = org.json.JSONObject(result)
                MLModelStatus(
                    ok          = json.optBoolean("ok", false),
                    version     = json.optString("version", "unknown"),
                    nTrain      = json.optInt("n_train", 0),
                    thrTake     = json.optDouble("thr_take", 0.70),
                    thrWatch    = json.optDouble("thr_watch", 0.58),
                    baseWr      = json.optDouble("base_wr", 0.588),
                    sampleP     = json.optDouble("sample_p", 0.5),
                    error       = json.optString("error", "")
                )
            } catch (e: Exception) {
                Log.e(TAG, "Model validation failed: ${e.message}")
                MLModelStatus(ok = false, error = e.message ?: "unknown")
            }
        }

        // ── Predict on a candidate dict (call from NativeBridge) ──────────
        fun predictCandidate(candidateJson: String): String {
            return try {
                val py = Python.getInstance()
                val json = py.getModule("json")
                val mle = py.getModule("ml_engine")
                val engine = mle.get("_ML_ENGINE") ?: return "{}" // MLS2: use _ML_ENGINE
                
                // MLS1: Use json.loads instead of eval() to avoid RCE vulnerabilities
                val cand = json.callAttr("loads", candidateJson)
                
                val result = runBlocking {
                    withTimeoutOrNull(5_000L) {
                        engine.callAttr("predict", cand)
                    }
                } ?: return "{}"
                result.toString()
            } catch (e: Exception) {
                Log.w(TAG, "Predict failed: ${e.message}")
                "{}"
            }
        }
    }

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val prefs by lazy { getSharedPreferences("market_radar", Context.MODE_PRIVATE) }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // B3: Must promote to foreground within 5s on Android 8+
        val channel = android.app.NotificationChannel(
            "ml_training", "ML Engine Updates",
            android.app.NotificationManager.IMPORTANCE_LOW
        )
        val nm = getSystemService(android.app.NotificationManager::class.java)
        nm?.createNotificationChannel(channel)
        
        val notification = NotificationCompat.Builder(this, "ml_training")
            .setContentTitle("ML Engine")
            .setContentText(when (intent?.action) {
                "ACTION_CHECK_RETRAIN" -> "Checking retrain readiness"
                "ACTION_CONFIRM_TRAIN", "ACTION_TRAIN_NIGHTLY" -> "Training ML model"
                "ACTION_ONLINE_UPDATE" -> "Updating from closed trade"
                "ACTION_TRAIN_TEMPORAL" -> "Training temporal model"
                else -> "Working"
            })
            .setSmallIcon(android.R.drawable.ic_menu_manage)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setOngoing(true)
            .build()
        
        startForeground(2002, notification)
        
        when (intent?.action) {
            "ACTION_CHECK_RETRAIN" -> {
                scope.launch {
                    checkRetrainReadiness()
                    stopForeground(STOP_FOREGROUND_REMOVE)
                    stopSelf(startId)
                }
            }
            "ACTION_CONFIRM_TRAIN", "ACTION_TRAIN_NIGHTLY" -> {
                scope.launch {
                    runNightlyTraining()
                    stopForeground(STOP_FOREGROUND_REMOVE)
                    stopSelf(startId)
                }
            }
            "ACTION_ONLINE_UPDATE" -> {
                val tradeJson = intent.getStringExtra("trade_json")
                if (tradeJson == null) {
                    stopForeground(STOP_FOREGROUND_REMOVE)
                    stopSelf(startId)
                    return START_NOT_STICKY
                }
                scope.launch {
                    runOnlineUpdate(tradeJson)
                    stopForeground(STOP_FOREGROUND_REMOVE)
                    stopSelf(startId)
                }
            }
            "ACTION_TRAIN_TEMPORAL" -> {
                scope.launch {
                    runTemporalTraining()
                    stopForeground(STOP_FOREGROUND_REMOVE)
                    stopSelf(startId)
                }
            }
            "ACTION_EXPORT_BACKTEST" -> {
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf(startId)
            }
            else -> {
                // Unknown action — shouldn't happen but don't leak foreground
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf(startId)
            }
        }
        return START_NOT_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    // ─────────────────────────────────────────────────────────────────────────
    // CHECK RETRAIN READINESS — counts closed trades, shows notification
    // ─────────────────────────────────────────────────────────────────────────

    private suspend fun checkRetrainReadiness() = withContext(Dispatchers.IO) {
        try {
            val closedTrades = SupabaseClient.select(
                "ml_decisions",
                filter = "outcome=not.is.null",
                limit = 500
            )
            val count = closedTrades.length()

            // Build a PendingIntent that starts training when tapped
            val trainIntent = Intent(this@MarketMLService, MarketMLService::class.java).apply {
                action = "ACTION_CONFIRM_TRAIN"
            }
            val pendingIntent = android.app.PendingIntent.getService(
                this@MarketMLService, 0, trainIntent,
                android.app.PendingIntent.FLAG_UPDATE_CURRENT or android.app.PendingIntent.FLAG_IMMUTABLE
            )

            val threshold = prefs.getInt("retrain_threshold", 20) // MLS12: Configurable threshold
            val title: String
            val body: String
            if (count < threshold) {
                title = "⚠️ ML Retrain — Low Data"
                body = "Only $count trades recorded — retrain needs $threshold+ for meaningful improvement. Tap to train anyway."
            } else {
                title = "🧠 ML Retrain Ready"
                body = "$count trades ready — tap to retrain ML model."
            }

            // Show actionable notification
            val channel = android.app.NotificationChannel(
                "ml_training", "ML Engine Updates",
                android.app.NotificationManager.IMPORTANCE_HIGH
            )
            val nm = getSystemService(android.app.NotificationManager::class.java)
            nm.createNotificationChannel(channel)

            val notification = android.app.Notification.Builder(this@MarketMLService, "ml_training")
                .setContentTitle(title)
                .setContentText(body)
                .setSmallIcon(android.R.drawable.ic_menu_manage)
                .setContentIntent(pendingIntent)
                .setAutoCancel(true)
                .build()

            nm.notify(2001, notification)
            Log.i(TAG, "Retrain check: $count trades → notification shown")

        } catch (e: Exception) {
            Log.w(TAG, "Retrain check failed: ${e.message}")
        }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // NIGHTLY FULL TRAINING
    // ─────────────────────────────────────────────────────────────────────────

    private suspend fun runNightlyTraining() = withContext(Dispatchers.IO) {
        Log.i(TAG, "=== ML training starting ===")
        val startMs = System.currentTimeMillis()

        try {
            val py   = Python.getInstance()
            val mod  = py.getModule("ml_train")

            // 1. Export closed trades to JSON for app_trades.json
            exportAppTrades()

            // 2. Run training (MLS5: Timeout increased to 300s for large NN/GBT datasets)
            val result = withTimeoutOrNull(300_000L) {
                mod.callAttr(
                    "run",
                    backtestPath(this@MarketMLService),
                    appTradesPath(this@MarketMLService),
                    modelPath(this@MarketMLService),
                    py.builtins.callAttr("print")          // log_fn = print → logcat
                ).toString()
            }
            
            if (result == null) {
                Log.w(TAG, "TRAINING_TIMEOUT: ml_train.run timed out after 60s")
                NotificationHelper.send(this@MarketMLService, "❌ Training Timeout", "Python trainer took too long", "urgent")
                return@withContext
            }

            val json    = org.json.JSONObject(result)
            val success = json.optBoolean("success", false)
            val deployed = json.optBoolean("deployed", false)
            val accGbt  = json.optDouble("acc_gbt", 0.0) // MLS7: Distinct accuracy fields
            val accEns  = json.optDouble("acc_ens", 0.0)
            val nTrain  = json.optInt("n_train", 0)
            val elapsed = json.optDouble("duration_sec", 0.0)
            val reason  = json.optString("reason", "")

            Log.i(TAG, "Training result: success=$success deployed=$deployed " +
                       "accEns=$accEns n=$nTrain ${elapsed}s")

            // 3. Store result in Supabase ml_models table
            if (success) {
                val pyEngine = py.getModule("ml_engine")
                val currentVersion = pyEngine.get("ML_VERSION")?.toString() ?: "2.2.0" // MLS6: Read from Python
                
                saveModelMetaToSupabase(
                    version   = currentVersion,
                    nTrain    = nTrain,
                    accGbt    = accGbt,
                    accEns    = accEns,
                    deployed  = deployed,
                    reason    = reason,
                    topFeatures = json.optJSONArray("top_features")?.toString() ?: "[]"
                )
                
                // MLS8: Cleanup old model files after successful training
                cleanupOldModels()

                // 4. Update ml_performance table
                savePerformanceToSupabase(accEns)

                // 5. Also train temporal model while we're awake
                runTemporalTraining()

                // 6. Hot-reload ML engine in Chaquopy (reload module)
                reloadMLEngine(py)

                // Notify user of success
                NotificationHelper.send(this@MarketMLService,
                    "✅ ML Model Updated",
                    "Accuracy: ${String.format("%.1f", accEns * 100)}% on $nTrain trades (${String.format("%.0f", elapsed)}s)",
                    "info")
            }

            val totalMs = System.currentTimeMillis() - startMs
            Log.i(TAG, "=== ML training complete in ${totalMs/1000}s ===")

        } catch (e: Exception) {
            Log.e(TAG, "ML training ERROR: ${e.message}", e)
            NotificationHelper.send(this@MarketMLService,
                "❌ ML Training Failed",
                e.message ?: "Unknown error",
                "urgent")
        }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // ONLINE UPDATE — called when a trade closes
    // ─────────────────────────────────────────────────────────────────────────

    private suspend fun runOnlineUpdate(tradeJson: String) = withContext(Dispatchers.IO) {
        try {
            val py  = Python.getInstance()
            val mod = py.getModule("ml_train")

            // Parse trade dict
            val tradeDict = org.json.JSONObject(tradeJson)

            // MLS13: Pass numeric fields as numbers, not strings, to avoid 'None' rejection in Python
            val pyDict = py.builtins.callAttr("dict")
            tradeDict.keys().forEach { key ->
                val rawVal = tradeDict.get(key)
                if (listOf("pnl", "id", "days_held").contains(key)) {
                    val numVal = tradeDict.optDouble(key, 0.0)
                    pyDict.callAttr("__setitem__", key, numVal)
                } else {
                    pyDict.callAttr("__setitem__", key, rawVal.toString())
                }
            }

            val result = withTimeoutOrNull(30_000L) {
                mod.callAttr(
                    "online_update",
                    modelPath(this@MarketMLService),
                    pyDict,
                    null  // no log_fn for online update
                ).toString()
            }
            
            if (result == null) {
                Log.w(TAG, "ONLINE_UPDATE_TIMEOUT: Python online_update timed out after 30s")
                return@withContext
            }
            
            val json = org.json.JSONObject(result)
            val pBefore = json.optDouble("p_before", 0.5)
            val pAfter  = json.optDouble("p_after", 0.5)
            val correct = json.optBoolean("direction_correct", false)

            Log.i(TAG, "Online update: p $pBefore → $pAfter  correct=$correct")

            // Store prediction record in ml_features table
            val tradeId = tradeDict.optInt("id", -1)
            val won     = tradeDict.optBoolean("won", false)
            if (tradeId > 0) {
                updateMLFeatureOutcome(tradeId, won, tradeDict.optDouble("pnl", 0.0))
            }
            Unit

        } catch (e: Exception) {
            Log.w(TAG, "Online update failed: ${e.message}")
        }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // TEMPORAL MODEL TRAINING
    // ─────────────────────────────────────────────────────────────────────────

    private suspend fun runTemporalTraining() = withContext(Dispatchers.IO) {
        try {
            val py  = Python.getInstance()
            val mod = py.getModule("ml_temporal")

            // Fetch poll sequences from Supabase for real training
            val sequences   = fetchPollSequencesForTraining()
            val nReal       = sequences.length()

            Log.i(TAG, "Temporal training: $nReal real sequences available")

            // Train (synthetic if <20 real sequences, mixed if more)
            val te = withTimeoutOrNull(45_000L) {
                if (nReal >= 20) {
                    // MLS9: Use fit_real route for actual poll sequences
                    mod.callAttr("train_temporal",
                        null,                                    // csv_path
                        buildPyListFromRows(py, sequences),      // rows
                        8,                                       // epochs
                        py.builtins.callAttr("print"),
                        true                                     // is_real=True
                    )
                } else {
                    // Synthetic pre-training from backtest CSV
                    mod.callAttr("train_temporal",
                        backtestPath(this@MarketMLService),      // csv_path
                        null,                                    // rows
                        8,                                       // epochs
                        py.builtins.callAttr("print")
                    )
                }
            } ?: return@withContext

            mod.callAttr("save_temporal", te, temporalModelPath(this@MarketMLService))
            Log.i(TAG, "Temporal model saved")

        } catch (e: Exception) {
            Log.w(TAG, "Temporal training failed (non-critical): ${e.message}")
        }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // SUPABASE HELPERS
    // ─────────────────────────────────────────────────────────────────────────

    private suspend fun saveModelMetaToSupabase(
        version: String, nTrain: Int, accGbt: Double, accEns: Double,
        deployed: Boolean, reason: String, topFeatures: String
    ) {
        try {
            val body = org.json.JSONObject().apply {
                put("version",       version)
                put("n_train",       nTrain)
                put("gbt_val_acc",   accGbt)
                put("ensemble_acc",  accEns)
                put("deployed",      deployed)
                put("deploy_reason", reason)
                put("top_features",  org.json.JSONArray(topFeatures))
            }
            // MLS11: Use onConflict to prevent duplication in ml_models
            SupabaseClient.upsert("ml_models", body, onConflict = "version")
            Log.i(TAG, "ML model meta saved to Supabase")
        } catch (e: Exception) {
            Log.w(TAG, "Failed to save model meta: ${e.message}")
        }
    }

    private suspend fun savePerformanceToSupabase(accuracy: Double) {
        try {
            val today = java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.US)
                .format(java.util.Date())
            val body = org.json.JSONObject().apply {
                put("date",         today)
                put("accuracy_all", accuracy)
            }
            SupabaseClient.upsert("ml_performance", body, onConflict = "date")
        } catch (e: Exception) {
            Log.w(TAG, "Failed to save performance: ${e.message}")
        }
    }

    private suspend fun updateMLFeatureOutcome(tradeId: Int, won: Boolean, pnl: Double) {
        try {
            val body = org.json.JSONObject().apply {
                put("won",        won)
                put("actual_pnl", pnl)
            }
            SupabaseClient.update("ml_features", body, "trade_id=eq.$tradeId")
        } catch (e: Exception) {
            Log.w(TAG, "Failed to update ml_feature outcome: ${e.message}")
        }
    }

    private suspend fun fetchPollSequencesForTraining(): org.json.JSONArray {
        return try {
            val resp = SupabaseClient.select(
                "ml_poll_sequences",
                filter = "won=not.is.null",
                order  = "date.desc",
                limit  = 500
            )
            resp  // MLS12: already JSONArray, don't double-wrap
        } catch (e: Exception) {
            Log.w(TAG, "Could not fetch poll sequences: ${e.message}")
            org.json.JSONArray()
        }
    }

    // ── Export app trades to JSON file for ml_train.run() ─────────────────────
    private suspend fun exportAppTrades() {
        try {
            val resp = SupabaseClient.select(
                "trades_v2",
                filter = "paper=eq.REAL",
                order  = "date.asc",
                limit  = 500
            )
            File(appTradesPath(this)).writeText(resp.toString())
            Log.i(TAG, "App trades exported to ${appTradesPath(this)}")
        } catch (e: Exception) {
            Log.w(TAG, "Could not export app trades: ${e.message}")
        }
    }

    // ── Hot-reload ML engine module after training ─────────────────────────────
    private fun reloadMLEngine(py: Python) {
        try {
            val importlib = py.getModule("importlib")
            val mlEngineModule = py.getModule("ml_engine")
            val brainModule = py.getModule("brain")
            
            // 1. Reload ml_engine module (fresh class definitions)
            importlib.callAttr("reload", mlEngineModule)
            
            // 2. Invalidate brain's cached engine reference
            // brainModule.put("_ML_ENGINE", null) // Replaced by _ml_invalidate in v2.2.7
            
            // 3. Trigger re-load by calling brain's loader
            brainModule.callAttr("_ml_invalidate")
            brainModule.callAttr("_ml_load_if_needed")
            
            Log.i(TAG, "ML engine hot-reloaded (brain cache invalidated)")
        } catch (e: Exception) {
            Log.w(TAG, "ML engine reload failed (non-critical): ${e.message}")
        }
    }

    // ── Build Python list from JSONArray (for passing to Chaquopy) ────────────
    private fun buildPyListFromRows(py: Python, arr: org.json.JSONArray): PyObject {
        val pyList = py.builtins.callAttr("list")
        for (i in 0 until arr.length()) {
            val obj = arr.getJSONObject(i)
            val d   = py.builtins.callAttr("dict")
            obj.keys().forEach { k -> d.callAttr("__setitem__", k, obj.get(k).toString()) }
            pyList.callAttr("append", d)
        }
        return pyList
    }

    // ── MLS8: Model File Cleanup ──────────────────────────────────────────────
    private fun cleanupOldModels() {
        try {
            val dir = applicationContext.filesDir
            val models = dir.listFiles { _, name -> name.startsWith("ml_model.json.v") } ?: return
            if (models.size <= 5) return

            // Sort by version number (descending)
            val sorted = models.sortedByDescending { it.name.substringAfterLast(".v").toIntOrNull() ?: 0 }
            
            // Delete anything beyond the first 5
            for (i in 5 until sorted.size) {
                if (sorted[i].delete()) {
                    Log.d(TAG, "MLS8: Deleted old model: ${sorted[i].name}")
                }
            }
            Log.i(TAG, "MLS8: Model cleanup complete. Retained ${minOf(sorted.size, 5)} versions.")
        } catch (e: Exception) {
            Log.w(TAG, "MLS8: Cleanup failed: ${e.message}")
        }
    }
}

data class MLModelStatus(
    val ok: Boolean = false,
    val version: String = "unknown",
    val nTrain: Int = 0,
    val thrTake: Double = 0.0,
    val thrWatch: Double = 0.0,
    val baseWr: Double = 0.0,
    val sampleP: Double = 0.0,
    val error: String = ""
)
