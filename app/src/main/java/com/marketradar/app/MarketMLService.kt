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
                val result = module.callAttr("validate_model", modelPath(context)).toString()
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
                val mle = py.getModule("ml_engine")
                val engine = mle.callAttr("_LOADED_ENGINE") ?: return "{}"
                val cand = py.builtins.callAttr("eval", candidateJson)
                val p_win = engine.callAttr("predict", cand)
                // Returns tuple (p_win, regime, detail_dict)
                p_win.toString()
            } catch (e: Exception) {
                Log.w(TAG, "Predict failed: ${e.message}")
                "{}"
            }
        }
    }

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            "ACTION_TRAIN_NIGHTLY" -> {
                scope.launch {
                    runNightlyTraining()
                    stopSelf(startId)
                }
            }
            "ACTION_ONLINE_UPDATE" -> {
                val tradeJson = intent.getStringExtra("trade_json") ?: return START_NOT_STICKY
                scope.launch {
                    runOnlineUpdate(tradeJson)
                    stopSelf(startId)
                }
            }
            "ACTION_TRAIN_TEMPORAL" -> {
                scope.launch {
                    runTemporalTraining()
                    stopSelf(startId)
                }
            }
            "ACTION_EXPORT_BACKTEST" -> {
                // Called from SupabaseClient after fetching all closed trades to CSV
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
    // NIGHTLY FULL TRAINING
    // ─────────────────────────────────────────────────────────────────────────

    private suspend fun runNightlyTraining() = withContext(Dispatchers.IO) {
        Log.i(TAG, "=== Nightly ML training starting ===")
        val startMs = System.currentTimeMillis()

        try {
            val py   = Python.getInstance()
            val mod  = py.getModule("ml_train")

            // 1. Export closed trades to JSON for app_trades.json
            exportAppTrades()

            // 2. Run training
            val result = mod.callAttr(
                "run",
                backtestPath(this@MarketMLService),
                appTradesPath(this@MarketMLService),
                modelPath(this@MarketMLService),
                py.builtins.callAttr("print")          // log_fn = print → logcat
            ).toString()

            val json    = org.json.JSONObject(result)
            val success = json.optBoolean("success", false)
            val deployed = json.optBoolean("deployed", false)
            val accNew  = json.optDouble("accuracy_new", 0.0)
            val accOld  = json.optDouble("accuracy_old", 0.0)
            val nTrain  = json.optInt("n_train", 0)
            val elapsed = json.optDouble("duration_sec", 0.0)
            val reason  = json.optString("reason", "")

            Log.i(TAG, "Training result: success=$success deployed=$deployed " +
                       "acc=$accOld→$accNew n=$nTrain ${elapsed}s")

            // 3. Store result in Supabase ml_models table
            if (success) {
                saveModelMetaToSupabase(
                    version   = "2.1.1",
                    nTrain    = nTrain,
                    accGbt    = accNew,
                    accEns    = accNew,
                    deployed  = deployed,
                    reason    = reason,
                    topFeatures = json.optJSONArray("top_features")?.toString() ?: "[]"
                )

                // 4. Update ml_performance table
                savePerformanceToSupabase(accNew)

                // 5. Also train temporal model while we're awake
                runTemporalTraining()

                // 6. Hot-reload ML engine in Chaquopy (reload module)
                reloadMLEngine(py)
            }

            val totalMs = System.currentTimeMillis() - startMs
            Log.i(TAG, "=== Nightly training complete in ${totalMs/1000}s ===")

        } catch (e: Exception) {
            Log.e(TAG, "Nightly training ERROR: ${e.message}", e)
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

            // Convert JSONObject to Python dict
            val pyDict = py.builtins.callAttr("dict")
            tradeDict.keys().forEach { key ->
                pyDict.callAttr("__setitem__", key, tradeDict.get(key).toString())
            }

            val result = mod.callAttr(
                "online_update",
                modelPath(this@MarketMLService),
                pyDict,
                null  // no log_fn for online update
            ).toString()

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
            val te = if (nReal >= 20) {
                // Real training
                mod.callAttr("train_temporal",
                    null,                                    // csv_path
                    buildPyListFromRows(py, sequences),      // rows
                    8,                                       // epochs
                    py.builtins.callAttr("print")
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
            SupabaseClient.upsert("ml_models", body)
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
            org.json.JSONArray(resp)
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
            val builtins    = py.builtins
            val importlib   = py.getModule("importlib")
            val mlModule    = py.getModule("ml_engine")
            importlib.callAttr("reload", mlModule)
            // Re-load model into module-level _ML_ENGINE
            mlModule.callAttr("_ml_load_if_needed")
            Log.i(TAG, "ML engine hot-reloaded")
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
}

// ─────────────────────────────────────────────────────────────────────────────
// DATA CLASS
// ─────────────────────────────────────────────────────────────────────────────

data class MLModelStatus(
    val ok:       Boolean = false,
    val version:  String  = "",
    val nTrain:   Int     = 0,
    val thrTake:  Double  = 0.70,
    val thrWatch: Double  = 0.58,
    val baseWr:   Double  = 0.588,
    val sampleP:  Double  = 0.5,
    val error:    String  = ""
)

// ─────────────────────────────────────────────────────────────────────────────
// NATIVEBRIDGE ADDITIONS — add these methods to your existing NativeBridge.kt
// ─────────────────────────────────────────────────────────────────────────────

/*
// Add to NativeBridge.kt:

@JavascriptInterface
fun getMLModelStatus(): String {
    return try {
        val status = MarketMLService.validateModel(context)
        org.json.JSONObject().apply {
            put("ok",        status.ok)
            put("version",   status.version)
            put("nTrain",    status.nTrain)
            put("thrTake",   status.thrTake)
            put("thrWatch",  status.thrWatch)
            put("baseWr",    status.baseWr)
            put("sampleP",   status.sampleP)
        }.toString()
    } catch (e: Exception) { "{\"ok\":false}" }
}

@JavascriptInterface
fun triggerMLOnlineUpdate(tradeJson: String) {
    val intent = Intent(context, MarketMLService::class.java).apply {
        action = "ACTION_ONLINE_UPDATE"
        putExtra("trade_json", tradeJson)
    }
    context.startForegroundService(intent)
}

@JavascriptInterface
fun isMLModelReady(): Boolean {
    return File(MarketMLService.modelPath(context)).exists()
}

// Add to NativeBridge 14-method list as methods 15 + 16 + 17:
// 15: getMLModelStatus() → String (JSON)
// 16: triggerMLOnlineUpdate(tradeJson: String) → void
// 17: isMLModelReady() → Boolean
*/

// ─────────────────────────────────────────────────────────────────────────────
// NIGHTLY TRAINING SETUP — call from MainActivity.onCreate()
// ─────────────────────────────────────────────────────────────────────────────

/*
// Add to MainActivity.kt onCreate():

// Schedule ML training at 11 PM nightly
MarketMLService.scheduleNightlyTraining(this)

// Validate model on first launch
CoroutineScope(Dispatchers.IO).launch {
    val status = MarketMLService.validateModel(this@MainActivity)
    if (status.ok) {
        Log.i("MainActivity", "ML model ready: v${status.version}  n=${status.nTrain}  thr=${status.thrTake}")
        // Push status to WebView
        runOnUiThread {
            webView.evaluateJavascript("window.mlStatus = ${status.toJson()};", null)
        }
    } else {
        Log.w("MainActivity", "ML model not ready: ${status.error}")
        // Trigger immediate training if no model exists
        if (!File(MarketMLService.modelPath(this@MainActivity)).exists()) {
            Log.i("MainActivity", "No model found — triggering initial training")
            val intent = Intent(this@MainActivity, MarketMLService::class.java)
            intent.action = "ACTION_TRAIN_NIGHTLY"
            startForegroundService(intent)
        }
    }
}
*/
