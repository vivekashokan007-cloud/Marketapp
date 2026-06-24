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
import com.marketradar.app.util.LogBuffer
import kotlinx.coroutines.*
import java.io.File
import java.util.Calendar
import java.util.Date
import java.util.Locale
import java.util.TimeZone

// ─────────────────────────────────────────────────────────────────────────────
// ALARM RECEIVER — wakes up at 11 PM and starts training
// ─────────────────────────────────────────────────────────────────────────────

class MLAlarmReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        Log.i("MLAlarmReceiver", "Ignoring stale 11 PM ML alarm — nightly training is disabled")
        MarketMLService.cancelNightlyTraining(context)
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// EVALUATION ALARM RECEIVER — fires at 4:30 PM, shows notification with button
// ─────────────────────────────────────────────────────────────────────────────

class EvaluationAlarmReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val istToday = MarketMLService.todayIstDate()
        val prefs = context.getSharedPreferences("market_radar", Context.MODE_PRIVATE)
        if (istToday == prefs.getString("evaluation_done_date", null)) {
            Log.i("EvaluationAlarmReceiver", "Skipping — evaluation already done today")
            return
        }
        val nowIst = Calendar.getInstance(MarketMLService.IST)
        val marketStatus = MarketOpenScheduler.currentStatus(nowIst)
        if (!MarketMLService.isEvaluationReminderWindow(nowIst, marketStatus)) {
            Log.i("EvaluationAlarmReceiver", "Skipping — outside evaluation reminder window")
            MarketMLService.scheduleDayEvaluationReminder(context)
            return
        }
        if (istToday == prefs.getString("evaluation_running_date", null)) {
            Log.i("EvaluationAlarmReceiver", "Skipping — evaluation already running today")
            return
        }

        Log.i("EvaluationAlarmReceiver", "4:30 PM+ alarm fired — showing evaluation reminder")
        val runIntent = Intent(context, MarketMLService::class.java).apply {
            action = "ACTION_DAY_EVALUATION"
        }
        val pendingIntent = PendingIntent.getForegroundService(
            context, 1002, runIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val channel = android.app.NotificationChannel(
            "ml_evaluation", "Day Evaluation",
            android.app.NotificationManager.IMPORTANCE_DEFAULT
        )
        val nm = context.getSystemService(android.app.NotificationManager::class.java)
        nm?.createNotificationChannel(channel)

        val notification = android.app.Notification.Builder(context, "ml_evaluation")
            .setContentTitle("Day Evaluation Ready")
            .setContentText("Tap to evaluate today's brain recommendations")
            .setSmallIcon(android.R.drawable.ic_menu_manage)
            .setContentIntent(pendingIntent)
            .setAutoCancel(true)
            .build()

        nm?.notify(2003, notification)

        // Schedule next reminder in 30 min
        MarketMLService.scheduleNextEvaluationReminder(context)
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// ML SERVICE
// ─────────────────────────────────────────────────────────────────────────────

class MarketMLService : Service() {

    companion object {
        private const val TAG = "MarketMLService"
        private const val EVENING_EVAL_TIMEOUT_MS = 45 * 60 * 1000L
        private const val EVAL_BATCH_SIZE = 12
        private const val EVAL_BATCH_TIMEOUT_MS = 120_000L
        private const val MONTHLY_RETRAIN_GATE_ROWS = 500
        private const val RETRAIN_DISABLED_REASON = "Retrain paused until canonical won-label unification is completed."
        internal val IST: TimeZone = TimeZone.getTimeZone("Asia/Kolkata")
        internal const val EVAL_STALE_AFTER_MS = 15 * 60 * 1000L
        private const val EVAL_REMINDER_START_MIN = 16 * 60 + 30
        private const val EVAL_REMINDER_END_MIN = 18 * 60 + 30

        // File paths inside app's internal storage
        fun backtestPath(ctx: Context): String =
            File(ctx.filesDir, "backtest_trades.csv").absolutePath

        fun appTradesPath(ctx: Context): String =
            File(ctx.filesDir, "app_trades.json").absolutePath

        fun evalOutcomesPath(ctx: Context): String =
            File(ctx.filesDir, "evaluation_outcomes.json").absolutePath

        fun brainSnapshotsPath(ctx: Context): String =
            File(ctx.filesDir, "brain_snapshots.json").absolutePath

        fun modelPath(ctx: Context): String =
            File(ctx.filesDir, "ml_model.json").absolutePath

        fun temporalModelPath(ctx: Context): String =
            File(ctx.filesDir, "temporal_model.json").absolutePath

        private fun evaluationDir(ctx: Context): File =
            File(ctx.filesDir, "day_evaluation").apply { mkdirs() }

        fun evaluationSnapshotsPath(ctx: Context, sessionDate: String): String =
            File(evaluationDir(ctx), "snapshots_$sessionDate.json").absolutePath

        fun evaluationChainPath(ctx: Context, sessionDate: String): String =
            File(evaluationDir(ctx), "chain_filtered_v3_$sessionDate.json").absolutePath

        fun evaluationPrepareCompletePath(ctx: Context, sessionDate: String): String =
            File(evaluationDir(ctx), "chain_filtered_v3_$sessionDate.complete").absolutePath

        fun evaluationOutcomesPath(ctx: Context, sessionDate: String): String =
            File(evaluationDir(ctx), "outcomes_$sessionDate.json").absolutePath

        fun evaluationResearchReportPath(ctx: Context, sessionDate: String): String =
            File(evaluationDir(ctx), "teacher_research_$sessionDate.json").absolutePath

        // ── Schedule nightly 11 PM alarm ─────────────────────────────────
        fun scheduleNightlyTraining(context: Context) {
            cancelNightlyTraining(context)
            Log.i(TAG, "Nightly ML training schedule skipped — manual/monthly gated retraining only")
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

        // ── Schedule first evaluation reminder at 4:30 PM IST (one-shot) ──
        fun scheduleDayEvaluationReminder(context: Context) {
            val istToday = todayIstDate()
            val prefs = context.getSharedPreferences("market_radar", Context.MODE_PRIVATE)
            if (istToday == prefs.getString("evaluation_done_date", null)) {
                Log.i(TAG, "Skipping schedule — evaluation already done today")
                cancelDayEvaluationReminder(context)
                return
            }

            val am = context.getSystemService(ALARM_SERVICE) as AlarmManager
            val intent = PendingIntent.getBroadcast(
                context, 1002,
                Intent(context, EvaluationAlarmReceiver::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )

            val cal = nextEvaluationReminderAt()

            am.set(AlarmManager.RTC_WAKEUP, cal.timeInMillis, intent)
            Log.i(TAG, "First evaluation reminder scheduled at ${cal.time}")
        }

        // ── Schedule next evaluation reminder 30 min from now (one-shot) ──
        fun scheduleNextEvaluationReminder(context: Context) {
            val prefs = context.getSharedPreferences("market_radar", Context.MODE_PRIVATE)
            val today = todayIstDate()
            if (prefs.getString("evaluation_done_date", null) == today) {
                cancelDayEvaluationReminder(context)
                return
            }
            val nowIst = Calendar.getInstance(IST)
            val marketStatus = MarketOpenScheduler.currentStatus(nowIst)
            if (!isEvaluationReminderWindow(nowIst, marketStatus)) {
                scheduleDayEvaluationReminder(context)
                return
            }
            val am = context.getSystemService(ALARM_SERVICE) as AlarmManager
            val intent = PendingIntent.getBroadcast(
                context, 1002,
                Intent(context, EvaluationAlarmReceiver::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )

            val cal = Calendar.getInstance(IST).apply {
                timeInMillis = System.currentTimeMillis() + 30 * 60 * 1000L
                set(Calendar.SECOND, 0)
                set(Calendar.MILLISECOND, 0)
            }

            am.set(AlarmManager.RTC_WAKEUP, cal.timeInMillis, intent)
            Log.i(TAG, "Next evaluation reminder scheduled in 30 min at ${cal.time}")
        }

        fun cancelDayEvaluationReminder(context: Context) {
            val am = context.getSystemService(ALARM_SERVICE) as AlarmManager
            val intent = PendingIntent.getBroadcast(
                context, 1002,
                Intent(context, EvaluationAlarmReceiver::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            am.cancel(intent)
        }

        internal fun todayIstDate(): String {
            return java.text.SimpleDateFormat("yyyy-MM-dd", Locale.US).apply {
                timeZone = IST
            }.format(Date())
        }

        internal fun isEvaluationReminderWindow(
            now: Calendar = Calendar.getInstance(IST),
            status: MarketOpenScheduler.MarketClockStatus = MarketOpenScheduler.currentStatus(now)
        ): Boolean {
            if (!status.marketDay) return false
            val minutes = now.get(Calendar.HOUR_OF_DAY) * 60 + now.get(Calendar.MINUTE)
            return minutes in EVAL_REMINDER_START_MIN..EVAL_REMINDER_END_MIN
        }

        private fun nextEvaluationReminderAt(from: Calendar = Calendar.getInstance(IST)): Calendar {
            val next = Calendar.getInstance(IST).apply {
                timeInMillis = from.timeInMillis
                set(Calendar.SECOND, 0)
                set(Calendar.MILLISECOND, 0)
            }
            while (true) {
                val status = MarketOpenScheduler.currentStatus(next)
                val minutes = next.get(Calendar.HOUR_OF_DAY) * 60 + next.get(Calendar.MINUTE)
                if (status.marketDay && minutes < EVAL_REMINDER_START_MIN) {
                    next.set(Calendar.HOUR_OF_DAY, 16)
                    next.set(Calendar.MINUTE, 30)
                    return next
                }
                next.add(Calendar.DATE, 1)
                next.set(Calendar.HOUR_OF_DAY, 16)
                next.set(Calendar.MINUTE, 30)
                if (MarketOpenScheduler.currentStatus(next).marketDay) return next
            }
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
    private val istTz: TimeZone = TimeZone.getTimeZone("Asia/Kolkata")

    override fun onCreate() {
        super.onCreate()
        Log.i(TAG, "MarketMLService created: pid=${android.os.Process.myPid()}")
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.action
        // B3: Must promote to foreground within 5s on Android 8+
        val channel = android.app.NotificationChannel(
            "ml_training", "ML Engine Updates",
            android.app.NotificationManager.IMPORTANCE_LOW
        )
        val nm = getSystemService(android.app.NotificationManager::class.java)
        nm?.createNotificationChannel(channel)
        
        val notification = NotificationCompat.Builder(this, "ml_training")
            .setContentTitle("ML Engine")
            .setContentText(when (action) {
                "ACTION_CHECK_RETRAIN" -> "Checking retrain readiness"
                "ACTION_CONFIRM_TRAIN", "ACTION_TRAIN_NIGHTLY" -> "Training ML model"
                "ACTION_ONLINE_UPDATE" -> "Updating from closed trade"
                "ACTION_TRAIN_TEMPORAL" -> "Training temporal model"
                "ACTION_DAY_EVALUATION" -> "Running day evaluation"
                else -> "Working"
            })
            .setSmallIcon(android.R.drawable.ic_menu_manage)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setOngoing(true)
            .build()
        
        startForeground(2002, notification)
        Log.i(TAG, "onStartCommand: action=$action sessionDate=${intent?.getStringExtra("session_date")}")
        
        when (action) {
            "ACTION_CHECK_RETRAIN" -> {
                scope.launch {
                    try {
                        checkRetrainReadiness()
                    } catch (t: Throwable) {
                        Log.e(TAG, "RETRAIN_ACTION_FAIL: ${t.message}", t)
                    } finally {
                        stopForeground(STOP_FOREGROUND_REMOVE)
                        stopSelf(startId)
                    }
                }
            }
            "ACTION_CONFIRM_TRAIN", "ACTION_TRAIN_NIGHTLY" -> {
                scope.launch {
                    try {
                        runNightlyTraining()
                    } catch (t: Throwable) {
                        Log.e(TAG, "TRAIN_ACTION_FAIL: ${t.message}", t)
                    } finally {
                        stopForeground(STOP_FOREGROUND_REMOVE)
                        stopSelf(startId)
                    }
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
                    try {
                        runOnlineUpdate(tradeJson)
                    } catch (t: Throwable) {
                        Log.e(TAG, "ONLINE_UPDATE_ACTION_FAIL: ${t.message}", t)
                    } finally {
                        stopForeground(STOP_FOREGROUND_REMOVE)
                        stopSelf(startId)
                    }
                }
            }
            "ACTION_TRAIN_TEMPORAL" -> {
                scope.launch {
                    try {
                        runTemporalTraining()
                    } catch (t: Throwable) {
                        Log.e(TAG, "TEMPORAL_TRAIN_ACTION_FAIL: ${t.message}", t)
                    } finally {
                        stopForeground(STOP_FOREGROUND_REMOVE)
                        stopSelf(startId)
                    }
                }
            }
            "ACTION_DAY_EVALUATION" -> {
                val sessionDate = intent.getStringExtra("session_date")?.ifBlank { null } ?: todayIstDate()
                scope.launch {
                    try {
                        Log.i(TAG, "DAY_EVAL_LAUNCHED: sessionDate=$sessionDate")
                        runDayEvaluation(sessionDate)
                    } catch (t: Throwable) {
                        Log.e(TAG, "DAY_EVAL_ACTION_FAIL: ${t.message}", t)
                        LogBuffer.recordCrash(
                            TAG,
                            "DAY_EVAL_ACTION_FAIL: ${t.message}",
                            t
                        )
                    } finally {
                        clearPostCloseHandoffState(reason = "day_evaluation_action")
                        stopForeground(STOP_FOREGROUND_REMOVE)
                        stopSelf(startId)
                    }
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

    override fun onDestroy() {
        clearPostCloseHandoffState(reason = "service_destroy")
        scope.cancel()
        super.onDestroy()
    }

    private fun updateEvaluationJobState(
        sessionDate: String,
        phase: String,
        message: String,
        totalSnapshots: Int? = null,
        completedSnapshots: Int? = null,
        producedCount: Int? = null,
        persistedCount: Int? = null,
        running: Boolean,
        lastError: String? = null
    ) {
        val now = System.currentTimeMillis()
        val editor = prefs.edit()
            .putString("evaluation_job_date", sessionDate)
            .putString("evaluation_phase", phase)
            .putString("last_evaluation_message", message)
            .putLong("evaluation_job_updated_at_ms", now)
            .putString("evaluation_running_date", if (running) sessionDate else "")
            .putLong("evaluation_started_at_ms", if (running) {
                val existing = prefs.getLong("evaluation_started_at_ms", 0L)
                if (existing > 0L) existing else now
            } else 0L)

        if (totalSnapshots != null) editor.putInt("evaluation_total_snapshots", totalSnapshots)
        if (completedSnapshots != null) editor.putInt("evaluation_completed_snapshots", completedSnapshots)
        if (producedCount != null) editor.putInt("last_evaluation_produced_count", producedCount)
        if (persistedCount != null) editor.putInt("last_evaluation_outcome_count", persistedCount)
        if (lastError.isNullOrBlank()) editor.remove("evaluation_last_error") else editor.putString("evaluation_last_error", lastError)
        editor.commit()
    }

    private fun readJsonArrayFile(file: File): org.json.JSONArray {
        if (!file.exists()) return org.json.JSONArray()
        val raw = file.readText().trim()
        if (raw.isBlank()) return org.json.JSONArray()
        return try {
            org.json.JSONArray(raw)
        } catch (_: Exception) {
            org.json.JSONArray()
        }
    }

    private fun writeJsonArrayFile(file: File, body: org.json.JSONArray) {
        file.parentFile?.mkdirs()
        file.writeText(body.toString())
    }

    private fun writeJsonArrayFileStreamed(file: File, body: org.json.JSONArray) {
        file.parentFile?.mkdirs()
        file.bufferedWriter().use { writer ->
            writer.write("[")
            var first = true
            for (i in 0 until body.length()) {
                val row = body.optJSONObject(i) ?: continue
                if (!first) writer.write(",")
                writer.write(row.toString())
                first = false
            }
            writer.write("]")
        }
    }

    private fun appendJsonArrayFile(file: File, rows: org.json.JSONArray): Int {
        val merged = readJsonArrayFile(file)
        for (i in 0 until rows.length()) {
            rows.optJSONObject(i)?.let(merged::put)
        }
        writeJsonArrayFile(file, merged)
        return merged.length()
    }

    private fun parseJsonObject(value: Any?): org.json.JSONObject? {
        return when (value) {
            is org.json.JSONObject -> value
            is String -> {
                val trimmed = value.trim()
                if (trimmed.startsWith("{")) {
                    try {
                        org.json.JSONObject(trimmed)
                    } catch (_: Exception) {
                        null
                    }
                } else {
                    null
                }
            }
            else -> null
        }
    }

    private fun parseJsonArray(value: Any?): org.json.JSONArray? {
        return when (value) {
            is org.json.JSONArray -> value
            is String -> {
                val trimmed = value.trim()
                if (trimmed.startsWith("[")) {
                    try {
                        org.json.JSONArray(trimmed)
                    } catch (_: Exception) {
                        null
                    }
                } else {
                    null
                }
            }
            else -> null
        }
    }

    private fun optStringAny(obj: org.json.JSONObject, vararg names: String): String {
        for (name in names) {
            val value = obj.opt(name) ?: continue
            val text = value.toString().trim()
            if (text.isNotBlank() && text != "null") return text
        }
        return ""
    }

    private fun optDoubleAny(obj: org.json.JSONObject, vararg names: String): Double {
        for (name in names) {
            val value = obj.opt(name) ?: continue
            val parsed = when (value) {
                is Number -> value.toDouble()
                is String -> value.trim().toDoubleOrNull()
                else -> null
            }
            if (parsed != null && !parsed.isNaN()) return parsed
        }
        return Double.NaN
    }

    private fun extractEvaluationLegKeys(snapshots: org.json.JSONArray): List<SupabaseClient.EvaluationLegKey> {
        val keys = linkedSetOf<SupabaseClient.EvaluationLegKey>()

        fun addLeg(
            cand: org.json.JSONObject,
            strikeNames: Array<String>,
            typeNames: Array<String>,
            indexKey: String,
            expiry: String
        ) {
            val strike = optDoubleAny(cand, *strikeNames)
            val optionType = optStringAny(cand, *typeNames).uppercase(Locale.US)
            if (strike.isNaN() || optionType.isBlank()) return
            keys += SupabaseClient.EvaluationLegKey(indexKey, expiry, strike, optionType)
        }

        fun addCandidate(raw: Any?) {
            val cand = parseJsonObject(raw) ?: return
            val indexKey = optStringAny(cand, "index", "index_key", "underlying").ifBlank { "BNF" }
            val expiry = optStringAny(cand, "expiry", "expiry_date")
            addLeg(cand, arrayOf("sellStrike", "sell_strike"), arrayOf("sellType", "sell_type"), indexKey, expiry)
            addLeg(cand, arrayOf("buyStrike", "buy_strike"), arrayOf("buyType", "buy_type"), indexKey, expiry)
            addLeg(cand, arrayOf("sellStrike2", "sell_strike2"), arrayOf("sellType2", "sell_type2"), indexKey, expiry)
            addLeg(cand, arrayOf("buyStrike2", "buy_strike2"), arrayOf("buyType2", "buy_type2"), indexKey, expiry)
        }

        for (i in 0 until snapshots.length()) {
            val snap = snapshots.optJSONObject(i) ?: continue
            addCandidate(snap.opt("primary_candidate_json"))

            val context = parseJsonObject(snap.opt("context_json"))
            val generated = parseJsonArray(context?.opt("snapshot_generated_candidates"))
                ?: parseJsonArray(snap.opt("top_candidates_json"))

            if (generated != null) {
                for (j in 0 until generated.length()) {
                    addCandidate(generated.opt(j))
                }
            }
        }

        return keys.toList()
    }

    private suspend fun ensureEvaluationInputFiles(sessionDate: String): Pair<File, File> = withContext(Dispatchers.IO) {
        val snapshotsFile = File(evaluationSnapshotsPath(this@MarketMLService, sessionDate))
        val chainFile = File(evaluationChainPath(this@MarketMLService, sessionDate))
        val completeFile = File(evaluationPrepareCompletePath(this@MarketMLService, sessionDate))
        if (
            snapshotsFile.exists() &&
            chainFile.exists() &&
            completeFile.exists() &&
            snapshotsFile.length() > 0L &&
            chainFile.length() > 0L
        ) {
            val preparedCount = try {
                org.json.JSONObject(completeFile.readText()).optInt("snapshot_count", -1)
            } catch (_: Exception) {
                -1
            }
            if (preparedCount > 0) {
                return@withContext Pair(snapshotsFile, chainFile)
            }
            Log.w(TAG, "EVAL_INPUT_CACHE_INVALID: prepared snapshot_count=$preparedCount date=$sessionDate")
            snapshotsFile.delete()
            completeFile.delete()
        }

        completeFile.delete()
        chainFile.delete()
        File("${chainFile.absolutePath}.tmp").delete()
        var snapshotsJsonArray = SupabaseClient.fetchEvaluationSnapshots(sessionDate)
        if (snapshotsJsonArray.length() == 0) {
            val localSnapshots = EvaluationLocalCache.readBrainSnapshots(this@MarketMLService, sessionDate)
            if (localSnapshots.length() > 0) {
                Log.w(TAG, "EVAL_SNAPSHOT_LOCAL_FALLBACK: date=$sessionDate rows=${localSnapshots.length()}")
                LogBuffer.add('W', TAG, "EVAL_SNAPSHOT_LOCAL_FALLBACK: date=$sessionDate rows=${localSnapshots.length()}")
                snapshotsJsonArray = localSnapshots
            }
        }
        val legKeys = extractEvaluationLegKeys(snapshotsJsonArray)
        if (snapshotsJsonArray.length() > 0 && legKeys.isEmpty()) {
            throw IllegalStateException("EVAL_NO_LEGKEYS: no candidate option legs found across ${snapshotsJsonArray.length()} snapshots.")
        }
        updateEvaluationJobState(
            sessionDate = sessionDate,
            phase = "PREPARING",
            message = "Preparing teacher evaluation for ${snapshotsJsonArray.length()} snapshots and ${legKeys.size} candidate legs...",
            totalSnapshots = snapshotsJsonArray.length(),
            completedSnapshots = 0,
            running = true
        )
        Log.i(TAG, "EVAL_SNAPSHOTS_WRITE_START: snapshots=${snapshotsJsonArray.length()} date=$sessionDate")
        updateEvaluationJobState(
            sessionDate = sessionDate,
            phase = "PREPARING",
            message = "Writing ${snapshotsJsonArray.length()} snapshot rows to disk before teacher evaluation...",
            totalSnapshots = snapshotsJsonArray.length(),
            completedSnapshots = 0,
            running = true
        )
        writeJsonArrayFileStreamed(snapshotsFile, snapshotsJsonArray)
        Log.i(
            TAG,
            "EVAL_SNAPSHOTS_WRITE_DONE: snapshots=${snapshotsJsonArray.length()} bytes=${snapshotsFile.length()} date=$sessionDate"
        )
        val chainFeed = SupabaseClient.writeEvaluationChainCandlesForLegs(sessionDate, legKeys, chainFile) { source, pages, rows ->
            updateEvaluationJobState(
                sessionDate = sessionDate,
                phase = "PREPARING",
                message = "Fetching relevant chain paths from $source: page $pages, rows $rows...",
                totalSnapshots = snapshotsJsonArray.length(),
                completedSnapshots = 0,
                running = true
            )
        }
        if (chainFeed.capped) {
            chainFile.delete()
            throw IllegalStateException("EVAL_CHAIN_TRUNCATED: ${chainFeed.source} hit page cap after ${chainFeed.pageCount} pages and ${chainFeed.rowCount} filtered rows.")
        }
        if (snapshotsJsonArray.length() > 0 && chainFeed.rowCount == 0) {
            chainFile.delete()
            throw IllegalStateException("EVAL_NO_CHAINROWS: no matching chain rows found for ${legKeys.size} candidate legs from ${chainFeed.source}.")
        }
        completeFile.writeText(
            org.json.JSONObject().apply {
                put("session_date", sessionDate)
                put("snapshot_count", snapshotsJsonArray.length())
                put("leg_key_count", legKeys.size)
                put("chain_row_count", chainFeed.rowCount)
                put("source", chainFeed.source)
                put("page_count", chainFeed.pageCount)
                put("completed_at", java.time.Instant.now().toString())
            }.toString()
        )
        Log.i(
            TAG,
            "EVAL_INPUTS_FILTERED_STREAM: snapshots=${snapshotsJsonArray.length()} legKeys=${legKeys.size} chainSlices=${chainFeed.rowCount} pages=${chainFeed.pageCount} source=${chainFeed.source} date=$sessionDate snapshotBytes=${snapshotsFile.length()} chainBytes=${chainFile.length()}"
        )
        if (chainFeed.rowCount == 0) {
            Log.w(TAG, "EVAL_CHAIN_FALLBACK: no chain rows found for $sessionDate source=${chainFeed.source}")
        }
        return@withContext Pair(snapshotsFile, chainFile)
    }

    // ─────────────────────────────────────────────────────────────────────────
    // CHECK RETRAIN READINESS — counts closed trades, shows notification
    // ─────────────────────────────────────────────────────────────────────────

    private suspend fun checkRetrainReadiness() = withContext(Dispatchers.IO) {
        try {
            // Build a PendingIntent that starts training when tapped
            val trainIntent = Intent(this@MarketMLService, MarketMLService::class.java).apply {
                action = "ACTION_CONFIRM_TRAIN"
            }
            val pendingIntent = android.app.PendingIntent.getForegroundService(
                this@MarketMLService, 0, trainIntent,
                android.app.PendingIntent.FLAG_UPDATE_CURRENT or android.app.PendingIntent.FLAG_IMMUTABLE
            )

            val title = "⏸ ML Retrain Paused"
            val body = RETRAIN_DISABLED_REASON

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
            Log.i(TAG, "Retrain check: paused pending canonical label unification")

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
            Log.i(TAG, "ML training blocked: $RETRAIN_DISABLED_REASON")
            NotificationHelper.send(
                this@MarketMLService,
                "ML Training Paused",
                RETRAIN_DISABLED_REASON,
                "info"
            )
            if (!prefs.getBoolean("ml_retrain_force_enable", false)) {
                return@withContext
            }

            val py   = Python.getInstance()
            val mod  = py.getModule("ml_train")

            // 1. Export closed trades and evaluator-backed labels to disk
            exportAppTrades()
            exportCanonicalEvaluationInputs()

            // 2. Run training (MLS5: Timeout increased to 300s for large NN/GBT datasets)
            val result = withTimeoutOrNull(300_000L) {
                mod.callAttr(
                    "run",
                    backtestPath(this@MarketMLService),
                    appTradesPath(this@MarketMLService),
                    modelPath(this@MarketMLService),
                    py.builtins.callAttr("print"),         // log_fn = print → logcat
                    evalOutcomesPath(this@MarketMLService),
                    brainSnapshotsPath(this@MarketMLService)
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
            val success = json.optBoolean("success", false)
            val reason = json.optString("reason", "")
            val pBefore = json.optDouble("p_before", 0.5)
            val pAfter  = json.optDouble("p_after", 0.5)
            val correct = json.optBoolean("direction_correct", false)

            if (success) {
                Log.i(TAG, "Online update: p $pBefore → $pAfter  correct=$correct")
            } else {
                Log.i(TAG, "Online update skipped: ${if (reason.isNotBlank()) reason else "no reason"}")
            }

            // Store prediction record in ml_features table
            val tradeId = tradeDict.optInt("id", -1)
            val won = when {
                tradeDict.has("canonical_won") -> tradeDict.optBoolean("canonical_won", false)
                tradeDict.has("outcome_h2") -> tradeDict.optInt("outcome_h2", 0) == 1
                else -> tradeDict.optBoolean("won", false)
            }
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
                put("canonical_won", won)
                put("won", won)
                put("outcome_h2", if (won) 1 else 0)
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

    private suspend fun exportCanonicalEvaluationInputs() {
        try {
            val outcomes = SupabaseClient.fetchRecentEvaluationOutcomes(1000)
            val snapshots = SupabaseClient.fetchRecentBrainSnapshots(1000)
            File(evalOutcomesPath(this)).writeText(outcomes.toString())
            File(brainSnapshotsPath(this)).writeText(snapshots.toString())
            Log.i(
                TAG,
                "Canonical evaluator inputs exported: outcomes=${outcomes.length()} snapshots=${snapshots.length()}"
            )
        } catch (e: Exception) {
            Log.w(TAG, "Could not export canonical evaluator inputs: ${e.message}")
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

    // ── ML Arch V2: Run Day Evaluation (evening evaluator) ────────────────────
    private suspend fun runDayEvaluation(sessionDateOverride: String? = null) = withContext(Dispatchers.IO) {
        val sessionDate = sessionDateOverride?.takeIf { it.isNotBlank() } ?: todayIstDate()
        val outputsFile = File(evaluationOutcomesPath(this@MarketMLService, sessionDate))
        var evalPhase = "PREPARING"
        var runId = "eval-$sessionDate-${System.currentTimeMillis()}"
        var totalSnapshots = 0
        var completedSnapshots = 0
        var producedCount = 0
        var brain: PyObject? = null
        Log.i(TAG, "EVAL_START: sessionDate=$sessionDate runId=$runId")
        try {
            if (prefs.getString("evaluation_done_date", null) == sessionDate) {
                updateEvaluationJobState(
                    sessionDate = sessionDate,
                    phase = "DONE",
                    message = if (sessionDate == todayIstDate()) "Today's evaluation already done." else "Evaluation already done for $sessionDate.",
                    producedCount = prefs.getInt("last_evaluation_produced_count", 0),
                    persistedCount = prefs.getInt("last_evaluation_outcome_count", 0),
                    running = false
                )
                Log.i(TAG, "EVAL_SKIP: already done for $sessionDate")
                return@withContext
            }

            updateEvaluationJobState(
                sessionDate = sessionDate,
                phase = evalPhase,
                message = "Preparing full-session teacher evaluation for $sessionDate...",
                totalSnapshots = 0,
                completedSnapshots = 0,
                producedCount = 0,
                persistedCount = 0,
                running = true
            )

            val py = Python.getInstance()
            brain = py.getModule("brain")
            val (snapshotsFile, chainFile) = ensureEvaluationInputFiles(sessionDate)
            val prepareStr = withTimeoutOrNull(EVAL_BATCH_TIMEOUT_MS) {
                brain.callAttr(
                    "evaluation_job_prepare",
                    runId,
                    snapshotsFile.absolutePath,
                    chainFile.absolutePath,
                    TeacherTruthConfig.toJson().toString()
                ).toString()
            } ?: throw IllegalStateException("Evaluation preparation timed out.")

            val prepareJson = org.json.JSONObject(prepareStr)
            if (!prepareJson.optBoolean("ok", false)) {
                throw IllegalStateException(prepareJson.optString("error", "Evaluation preparation failed."))
            }
            totalSnapshots = prepareJson.optInt("snapshot_count", 0)
            producedCount = 0

            if (totalSnapshots == 0) {
                val pollCount = prefs.getInt("poll_count", 0)
                val lastPollDate = prefs.getString("last_poll_date", "") ?: ""
                if (lastPollDate == sessionDate && pollCount > 0) {
                    throw IllegalStateException(
                        "EVAL_NO_SNAPSHOTS_AFTER_POLLING: $sessionDate has $pollCount polls but no evaluation snapshots were prepared."
                    )
                }
                writeJsonArrayFile(outputsFile, org.json.JSONArray())
                prefs.edit().putString("evaluation_done_date", sessionDate).commit()
                updateEvaluationJobState(
                    sessionDate = sessionDate,
                    phase = "DONE",
                    message = "Evaluation done for $sessionDate: no brain snapshots found.",
                    totalSnapshots = 0,
                    completedSnapshots = 0,
                    producedCount = 0,
                    persistedCount = 0,
                    running = false
                )
                cancelDayEvaluationReminder(this@MarketMLService)
                Log.i(TAG, "EVAL_SKIP: no brain snapshots for $sessionDate")
                return@withContext
            }

            val canResume = (prefs.getString("evaluation_job_date", "") == sessionDate) &&
                (prefs.getInt("evaluation_total_snapshots", 0) == totalSnapshots) &&
                outputsFile.exists()
            if (!canResume) {
                writeJsonArrayFile(outputsFile, org.json.JSONArray())
                completedSnapshots = 0
                producedCount = 0
            } else {
                completedSnapshots = prefs.getInt("evaluation_completed_snapshots", 0).coerceIn(0, totalSnapshots)
                producedCount = readJsonArrayFile(outputsFile).length()
                if (completedSnapshots > 0 && producedCount == 0) {
                    completedSnapshots = 0
                }
            }

            updateEvaluationJobState(
                sessionDate = sessionDate,
                phase = evalPhase,
                message = if (completedSnapshots > 0) {
                    "Resuming teacher evaluation for $sessionDate from snapshot $completedSnapshots of $totalSnapshots."
                } else {
                    "Prepared full-session inputs for $sessionDate with $totalSnapshots snapshots."
                },
                totalSnapshots = totalSnapshots,
                completedSnapshots = completedSnapshots,
                producedCount = producedCount,
                persistedCount = 0,
                running = true
            )

            while (completedSnapshots < totalSnapshots) {
                evalPhase = "RUNNING"
                updateEvaluationJobState(
                    sessionDate = sessionDate,
                    phase = evalPhase,
                    message = "Evaluating $sessionDate snapshots ${completedSnapshots + 1}-${minOf(completedSnapshots + EVAL_BATCH_SIZE, totalSnapshots)} of $totalSnapshots...",
                    totalSnapshots = totalSnapshots,
                    completedSnapshots = completedSnapshots,
                    producedCount = producedCount,
                    persistedCount = 0,
                    running = true
                )

                val batchStr = withTimeoutOrNull(EVAL_BATCH_TIMEOUT_MS) {
                    brain.callAttr(
                        "evaluation_job_run_batch",
                        runId,
                        completedSnapshots,
                        EVAL_BATCH_SIZE
                    ).toString()
                } ?: throw IllegalStateException("Evaluation batch timed out at snapshot $completedSnapshots of $totalSnapshots.")

                val batchJson = org.json.JSONObject(batchStr)
                if (!batchJson.optBoolean("ok", false)) {
                    throw IllegalStateException(batchJson.optString("error", "Evaluation batch failed."))
                }

                val batchOutcomes = batchJson.optJSONArray("outcomes") ?: org.json.JSONArray()
                producedCount = appendJsonArrayFile(outputsFile, batchOutcomes)
                completedSnapshots = batchJson.optInt("end", completedSnapshots).coerceIn(completedSnapshots, totalSnapshots)

                val batchErrorCount = batchJson.optInt("error_count", 0)
                val batchErrors = batchJson.optJSONArray("errors") ?: org.json.JSONArray()
                val batchErrorHint = if (batchErrorCount > 0 && batchErrors.length() > 0) {
                    batchErrors.optJSONObject(0)?.optString("error", "") ?: ""
                } else {
                    ""
                }
                updateEvaluationJobState(
                    sessionDate = sessionDate,
                    phase = evalPhase,
                    message = buildString {
                        append("Evaluated $sessionDate snapshots $completedSnapshots/$totalSnapshots. Produced $producedCount outcomes.")
                        if (batchErrorCount > 0) {
                            append(" Skipped $batchErrorCount malformed rows in the last batch")
                            if (batchErrorHint.isNotBlank()) append(" ($batchErrorHint)")
                            append(".")
                        }
                    },
                    totalSnapshots = totalSnapshots,
                    completedSnapshots = completedSnapshots,
                    producedCount = producedCount,
                    persistedCount = 0,
                    running = true,
                    lastError = if (batchErrorCount > 0) batchErrorHint else null
                )
            }

            val evaluatedOutcomes = readJsonArrayFile(outputsFile)
            if (evaluatedOutcomes.length() > 0) {
                val exitCounts = linkedMapOf<String, Int>()
                var pathMin = Int.MAX_VALUE
                var pathMax = 0
                var pathSum = 0
                var pathSeen = 0
                val samples = mutableListOf<String>()
                for (i in 0 until evaluatedOutcomes.length()) {
                    val row = evaluatedOutcomes.optJSONObject(i) ?: continue
                    val exitReason = row.optString("exit_reason", "UNKNOWN")
                    exitCounts[exitReason] = (exitCounts[exitReason] ?: 0) + 1
                    val pathPoints = row.optInt("path_points_count", 0)
                    if (pathPoints > 0) {
                        pathSeen += 1
                        pathSum += pathPoints
                        pathMin = minOf(pathMin, pathPoints)
                        pathMax = maxOf(pathMax, pathPoints)
                    }
                    if (samples.size < 6) {
                        samples += "${row.optString("candidate_id", "unknown")}:$pathPoints:$exitReason"
                    }
                }
                val avgPath = if (pathSeen > 0) pathSum.toDouble() / pathSeen.toDouble() else 0.0
                val minPath = if (pathSeen > 0) pathMin else 0
                Log.i(
                    TAG,
                    "EVAL_PATH_STATS: rows=${evaluatedOutcomes.length()} pathSeen=$pathSeen min=$minPath max=$pathMax avg=${"%.2f".format(avgPath)} exitReasons=$exitCounts samples=$samples"
                )
            }

            evalPhase = "SAVING"
            updateEvaluationJobState(
                sessionDate = sessionDate,
                phase = evalPhase,
                message = "Saving $producedCount evaluated outcomes for $sessionDate to Supabase...",
                totalSnapshots = totalSnapshots,
                completedSnapshots = completedSnapshots,
                producedCount = producedCount,
                persistedCount = 0,
                running = true
            )

            val saveResult = if (evaluatedOutcomes.length() > 0) {
                SupabaseClient.saveEvaluationOutcomes(sessionDate, evaluatedOutcomes)
            } else {
                SupabaseClient.EvaluationSaveResult(
                    success = true,
                    producedCount = 0,
                    persistedCount = 0,
                    primaryPersistedCount = 0,
                    evaluationPersistedCount = 0,
                    message = "No evaluable shadow teacher labels were produced from today's saved recommendations."
                )
            }
            if (!saveResult.success) {
                updateEvaluationJobState(
                    sessionDate = sessionDate,
                    phase = "FAILED_SAVE",
                    message = "Evaluation for $sessionDate finished locally with ${saveResult.producedCount} outcomes, but Supabase persistence failed. Retry will reuse the saved local output.",
                    totalSnapshots = totalSnapshots,
                    completedSnapshots = completedSnapshots,
                    producedCount = saveResult.producedCount,
                    persistedCount = saveResult.persistedCount,
                    running = false,
                    lastError = saveResult.message
                )
                Log.w(TAG, "EVAL_SAVE_FAIL: ${saveResult.message}")
                return@withContext
            }

            if (evaluatedOutcomes.length() > 0) {
                evalPhase = "AGGREGATING"
                updateEvaluationJobState(
                    sessionDate = sessionDate,
                    phase = evalPhase,
                    message = "Aggregating lane summaries and teacher comparison for $sessionDate...",
                    totalSnapshots = totalSnapshots,
                    completedSnapshots = completedSnapshots,
                    producedCount = saveResult.producedCount,
                    persistedCount = saveResult.persistedCount,
                    running = true
                )
                val snapshotsJsonArray = readJsonArrayFile(snapshotsFile)
                runAggregationPipeline(sessionDate, snapshotsJsonArray, evaluatedOutcomes)
                buildTeacherResearchReport(brain, sessionDate, snapshotsJsonArray, evaluatedOutcomes)
            }

            val evaluationMessage = if (evaluatedOutcomes.length() > 0) {
                "Evaluation done for $sessionDate: ${saveResult.producedCount} outcomes produced, ${saveResult.persistedCount} persisted to Supabase."
            } else {
                "Evaluation done for $sessionDate: 0 evaluable shadow teacher outcomes saved from the day's recommendations."
            }
            prefs.edit().putString("evaluation_done_date", sessionDate).commit()
            updateEvaluationJobState(
                sessionDate = sessionDate,
                phase = "DONE",
                message = evaluationMessage,
                totalSnapshots = totalSnapshots,
                completedSnapshots = completedSnapshots,
                producedCount = saveResult.producedCount,
                persistedCount = saveResult.persistedCount,
                running = false
            )
            cancelDayEvaluationReminder(this@MarketMLService)
            Log.i(
                TAG,
                "EVAL_COMPLETE: produced=${saveResult.producedCount} persisted=${saveResult.persistedCount} primaryPersisted=${saveResult.primaryPersistedCount} evalPersisted=${saveResult.evaluationPersistedCount} for $sessionDate — reminder cancelled"
            )
        } catch (e: Exception) {
            val crashExtra = org.json.JSONObject()
                .put("session_date", sessionDate)
                .put("eval_phase", evalPhase)
                .put("completed_snapshots", completedSnapshots)
                .put("total_snapshots", totalSnapshots)
                .put("produced_count", producedCount)
            updateEvaluationJobState(
                sessionDate = sessionDate,
                phase = if (evalPhase == "SAVING") "FAILED_SAVE" else if (evalPhase == "RUNNING") "FAILED" else "FAILED",
                message = "Evaluation failed during ${evalPhase.lowercase(java.util.Locale.US)} at $completedSnapshots/$totalSnapshots snapshots. Retry will resume from the last completed batch.",
                totalSnapshots = totalSnapshots,
                completedSnapshots = completedSnapshots,
                producedCount = producedCount,
                persistedCount = prefs.getInt("last_evaluation_outcome_count", 0),
                running = false,
                lastError = e.message ?: "unknown"
            )
            Log.e(TAG, "EVAL_FAIL: ${e.message}", e)
            LogBuffer.recordCrash(TAG, "EVAL_FAIL[$evalPhase]: ${e.message}", e, crashExtra)
        } finally {
            try {
                brain?.callAttr("evaluation_job_finalize", runId)
            } catch (_: Exception) {
            }
            clearPostCloseHandoffState(reason = "day_evaluation_complete")
        }
    }

    private fun clearPostCloseHandoffState(reason: String) {
        val hadLatch = prefs.getBoolean("hasDayEvalRun", false)
        val hadTs = prefs.contains("day_eval_handoff_ts")
        val hadRunning = prefs.getString("evaluation_running_date", "") == todayIstDate()
        if (!hadLatch && !hadTs && !hadRunning) return
        val result = prefs.edit()
            .putBoolean("hasDayEvalRun", false)
            .remove("day_eval_handoff_ts")
            .remove("day_eval_handoff_date")
            .remove("evaluation_running_date")
            .remove("evaluation_job_updated_at_ms")
            .putLong("evaluation_job_updated_at_ms", 0L)
            .commit()
        Log.i(
            TAG,
            "DAY_EVAL_HANDOFF_STATE_CLEARED: reason=$reason hadLatch=$hadLatch hadTs=$hadTs hadRunning=$hadRunning commit=$result"
        )
    }

    // ── Batch 6: daily/weekly/monthly aggregation loop ───────────────────────
    private suspend fun runAggregationPipeline(
        sessionDate: String,
        snapshotsJsonArray: org.json.JSONArray,
        evaluatedOutcomes: org.json.JSONArray
    ) = withContext(Dispatchers.IO) {
        try {
            val snapshotIdToLabelable = mutableMapOf<Int, Boolean>()
            for (i in 0 until snapshotsJsonArray.length()) {
                val s = snapshotsJsonArray.optJSONObject(i) ?: continue
                val sid = s.optInt("id", -1)
                if (sid <= 0) continue
                snapshotIdToLabelable[sid] = s.optBoolean("is_labelable", false)
            }

            var labeledRows = 0
            var wins = 0
            for (i in 0 until evaluatedOutcomes.length()) {
                val row = evaluatedOutcomes.optJSONObject(i) ?: continue
                if (!row.optString("role", "secondary").equals("primary", ignoreCase = true)) continue
                val sid = row.optInt("snapshot_id", -1)
                val labelable = snapshotIdToLabelable[sid] == true
                if (!labelable) continue
                val won = when {
                    row.has("canonical_won") -> row.optInt("canonical_won", -1)
                    row.has("outcome_h2") -> row.optInt("outcome_h2", -1)
                    row.has("won") -> if (row.optBoolean("won", false)) 1 else 0
                    else -> -1
                }
                if (won !in listOf(0, 1)) continue
                labeledRows += 1
                if (won == 1) wins += 1
            }

            val accuracyPct = if (labeledRows > 0) (wins * 100.0) / labeledRows else 0.0
            val dayBody = org.json.JSONObject()
                .put("session_date", sessionDate)
                .put("labeled_rows", labeledRows)
                .put("wins", wins)
                .put("accuracy_pct", String.format(java.util.Locale.US, "%.2f", accuracyPct).toDouble())
                .put("method", "h2_primary_labelable")
                .put("updated_at", nowIsoUtc())
            postAggregateRow(
                tableNames = listOf("ml_daily_accuracy", "ml_accuracy_daily"),
                body = dayBody,
                onConflict = "session_date"
            )

            NotificationHelper.send(
                this@MarketMLService,
                "Brain accuracy today",
                "${String.format(java.util.Locale.US, "%.1f", accuracyPct)}% on $labeledRows recommendations",
                "info"
            )

            maybeAggregateWeek(sessionDate)
            maybeAggregateMonth(sessionDate)
        } catch (e: Exception) {
            Log.w(TAG, "AGGREGATION_FAIL: ${e.message}")
        }
    }

    private fun buildTeacherResearchReport(
        brain: PyObject?,
        sessionDate: String,
        snapshotsJsonArray: org.json.JSONArray,
        evaluatedOutcomes: org.json.JSONArray
    ) {
        if (brain == null) return
        try {
            val reportRaw = brain.callAttr(
                "session_teacher_research_report",
                sessionDate,
                snapshotsJsonArray.toString(),
                evaluatedOutcomes.toString()
            ).toString()
            val report = org.json.JSONObject(reportRaw)
            if (!report.optBoolean("ok", false)) {
                Log.w(TAG, "TEACHER_RESEARCH_REPORT_FAIL: ${report.optString("error", "unknown")}")
                return
            }
            val outFile = File(evaluationResearchReportPath(this@MarketMLService, sessionDate))
            outFile.parentFile?.mkdirs()
            outFile.writeText(report.toString())
            prefs.edit()
                .putString("teacher_research_report_date", sessionDate)
                .putString("teacher_research_report", report.toString())
                .commit()
            val pvb = report.optJSONObject("primary_vs_best") ?: org.json.JSONObject()
            Log.i(
                TAG,
                "TEACHER_RESEARCH_REPORT: session=$sessionDate compared=${pvb.optInt("snapshots_compared", 0)} better=${pvb.optInt("better_candidate_available", 0)} upliftR=${pvb.optDouble("avg_best_minus_primary_r", 0.0)}"
            )
        } catch (e: Exception) {
            Log.w(TAG, "TEACHER_RESEARCH_REPORT_FAIL: ${e.message}")
        }
    }

    private fun maybeAggregateWeek(sessionDate: String) {
        try {
            val dayCal = istCalendarFromDate(sessionDate)
            if (dayCal.get(Calendar.DAY_OF_WEEK) != Calendar.SATURDAY) return

            val weekEnd = dateFromIstCal(dayCal)
            dayCal.add(Calendar.DAY_OF_YEAR, -6)
            val weekStart = dateFromIstCal(dayCal)

            val rows = SupabaseClient.select(
                table = "ml_daily_accuracy",
                filter = "session_date=gte.$weekStart&session_date=lte.$weekEnd",
                order = "session_date.asc",
                limit = 10
            )
            if (rows.length() == 0) return

            var labeled = 0
            var wins = 0
            for (i in 0 until rows.length()) {
                val r = rows.optJSONObject(i) ?: continue
                labeled += r.optInt("labeled_rows", 0)
                wins += r.optInt("wins", 0)
            }
            if (labeled <= 0) return
            val acc = (wins * 100.0) / labeled
            val body = org.json.JSONObject()
                .put("week_start", weekStart)
                .put("week_end", weekEnd)
                .put("labeled_rows", labeled)
                .put("wins", wins)
                .put("accuracy_pct", String.format(java.util.Locale.US, "%.2f", acc).toDouble())
                .put("updated_at", nowIsoUtc())
            postAggregateRow(
                tableNames = listOf("ml_weekly_accuracy", "ml_accuracy_weekly"),
                body = body,
                onConflict = "week_start"
            )
        } catch (e: Exception) {
            Log.w(TAG, "WEEKLY_AGG_FAIL: ${e.message}")
        }
    }

    private fun maybeAggregateMonth(sessionDate: String) {
        try {
            val dayCal = istCalendarFromDate(sessionDate)
            val isLastFriday = dayCal.get(Calendar.DAY_OF_WEEK) == Calendar.FRIDAY &&
                dayCal.clone().let {
                    val c = it as Calendar
                    c.add(Calendar.DAY_OF_YEAR, 7)
                    c.get(Calendar.MONTH) != dayCal.get(Calendar.MONTH)
                }
            if (!isLastFriday) return

            val monthKey = String.format(
                java.util.Locale.US,
                "%04d-%02d",
                dayCal.get(Calendar.YEAR),
                dayCal.get(Calendar.MONTH) + 1
            )
            val startCal = dayCal.clone() as Calendar
            startCal.set(Calendar.DAY_OF_MONTH, 1)
            val monthStart = dateFromIstCal(startCal)
            val monthEnd = sessionDate

            val rows = SupabaseClient.select(
                table = "ml_daily_accuracy",
                filter = "session_date=gte.$monthStart&session_date=lte.$monthEnd",
                order = "session_date.asc",
                limit = 40
            )
            if (rows.length() == 0) return

            var labeled = 0
            var wins = 0
            for (i in 0 until rows.length()) {
                val r = rows.optJSONObject(i) ?: continue
                labeled += r.optInt("labeled_rows", 0)
                wins += r.optInt("wins", 0)
            }
            if (labeled <= 0) return
            val acc = (wins * 100.0) / labeled
            val body = org.json.JSONObject()
                .put("month_key", monthKey)
                .put("month_start", monthStart)
                .put("month_end", monthEnd)
                .put("labeled_rows", labeled)
                .put("wins", wins)
                .put("accuracy_pct", String.format(java.util.Locale.US, "%.2f", acc).toDouble())
                .put("hard_gate_triggered", labeled >= MONTHLY_RETRAIN_GATE_ROWS)
                .put("updated_at", nowIsoUtc())
            postAggregateRow(
                tableNames = listOf("ml_monthly_summary", "ml_accuracy_monthly"),
                body = body,
                onConflict = "month_key"
            )

            if (labeled >= MONTHLY_RETRAIN_GATE_ROWS) {
                NotificationHelper.send(
                    this@MarketMLService,
                    "ML Retrain Gate Triggered",
                    "Month $monthKey reached $labeled labeled rows. Review calibration and run retrain.",
                    "important"
                )
                scope.launch { checkRetrainReadiness() }
            }
        } catch (e: Exception) {
            Log.w(TAG, "MONTHLY_AGG_FAIL: ${e.message}")
        }
    }

    private fun postAggregateRow(tableNames: List<String>, body: org.json.JSONObject, onConflict: String): Boolean {
        for (table in tableNames) {
            if (SupabaseClient.upsert(table, body, onConflict = onConflict)) return true
        }
        return false
    }

    private fun istCalendarFromDate(date: String): Calendar {
        val cal = Calendar.getInstance(istTz)
        val parsed = java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.US).apply {
            timeZone = istTz
        }.parse(date)
        if (parsed != null) cal.time = parsed
        return cal
    }

    private fun dateFromIstCal(cal: Calendar): String {
        return java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.US).apply {
            timeZone = istTz
        }.format(cal.time)
    }

    private fun nowIsoUtc(): String {
        return java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", java.util.Locale.US).apply {
            timeZone = TimeZone.getTimeZone("UTC")
        }.format(java.util.Date())
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
