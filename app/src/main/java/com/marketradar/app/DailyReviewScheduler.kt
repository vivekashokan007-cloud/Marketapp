package com.marketradar.app

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import org.json.JSONObject
import com.marketradar.app.util.LogBuffer
import java.util.concurrent.TimeUnit

/**
 * Delivery R7 (Codex R6 review s6): polling-independent Daily review trigger.
 *
 * A persistent WorkManager job (unique periodic work, plus a unique one-time request from the existing evening
 * evaluation alarm, boot / package-replaced, and app process start) calls brain.daily_review_catchup, which
 * reviews every retained, due, not-yet-completed session and records PENDING / ATTEMPTED / COMPLETED in a local
 * ledger. Failed or incomplete sessions are retried with backoff and on the next trigger (wake / restart).
 * The teacher is never awaited. No exact Android firing time is claimed (WorkManager / Doze decide).
 */
object DailyReviewScheduler {
    const val ONE_TIME = "daily_review_catchup_v1"
    const val PERIODIC = "daily_review_catchup_periodic_v1"
    const val PERIOD_HOURS = 3L
    const val KEY_TRIGGER = "trigger"
    const val MAX_RETRIES = 5

    fun request(context: Context, trigger: String) {
        try {
            val wm = WorkManager.getInstance(context.applicationContext)
            val req = OneTimeWorkRequestBuilder<DailyReviewWorker>()
                .setInputData(workDataOf(KEY_TRIGGER to trigger))
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.MINUTES)
                .build()
            wm.enqueueUniqueWork(ONE_TIME, ExistingWorkPolicy.KEEP, req)
            ensurePeriodic(context)
        } catch (e: Exception) {
            LogBuffer.add('W', "DailyReviewScheduler", "DAILY_REVIEW_ENQUEUE_FAIL: ${e.javaClass.simpleName}")
        }
    }

    fun ensurePeriodic(context: Context) {
        val req = PeriodicWorkRequestBuilder<DailyReviewWorker>(PERIOD_HOURS, TimeUnit.HOURS)
            .setInputData(workDataOf(KEY_TRIGGER to "periodic"))
            .build()
        WorkManager.getInstance(context.applicationContext)
            .enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.KEEP, req)
    }

    /** Pure: map the Python catch-up result to the worker outcome. */
    fun outcome(resultJson: String?, runAttemptCount: Int): String {
        val o = try { JSONObject(resultJson ?: "") } catch (e: Exception) { return if (runAttemptCount < MAX_RETRIES) "RETRY" else "FAILURE" }
        val st = o.optString("status")
        if (st == "OK" && !o.optBoolean("retry", false)) return "SUCCESS"
        if (st == "OK" || st == "LEDGER_BUSY" || st == "ERROR") return if (runAttemptCount < MAX_RETRIES) "RETRY" else "SUCCESS"
        return "SUCCESS"   // LEDGER_CORRUPT / CLOCK_UNAVAILABLE: fail closed, no hot retry loop
    }
}

class DailyReviewWorker(context: Context, params: WorkerParameters) : Worker(context, params) {
    override fun doWork(): Result {
        return try {
            if (!com.chaquo.python.Python.isStarted()) {
                com.chaquo.python.Python.start(com.chaquo.python.android.AndroidPlatform(applicationContext))
            }
            val prefs = applicationContext.getSharedPreferences("market_radar", Context.MODE_PRIVATE)
            val brain = com.chaquo.python.Python.getInstance().getModule("brain")
            val out = brain.callAttr("daily_review_catchup",
                IndexAuthorityContext.stateDir(applicationContext.filesDir).absolutePath,
                IndexAuthorityContext.reportDir(applicationContext.filesDir).absolutePath,
                0, IndexAuthorityContext.DAILY_REVIEW_BUDGET_S, prefs.getString("closed_trades", null),
                inputData.getString(DailyReviewScheduler.KEY_TRIGGER) ?: "unknown").toString()
            LogBuffer.add('I', "DailyReviewWorker", "DAILY_REVIEW_CATCHUP: ${out.take(200)}")
            when (DailyReviewScheduler.outcome(out, runAttemptCount)) {
                "RETRY" -> Result.retry()
                "FAILURE" -> Result.failure()
                else -> Result.success()
            }
        } catch (e: Exception) {
            LogBuffer.add('W', "DailyReviewWorker", "DAILY_REVIEW_CATCHUP_FAIL: ${e.javaClass.simpleName}")
            if (runAttemptCount < DailyReviewScheduler.MAX_RETRIES) Result.retry() else Result.failure()
        }
    }
}
