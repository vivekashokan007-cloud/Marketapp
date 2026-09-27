package com.marketradar.app

import android.content.Context
import android.util.Log
import androidx.work.Worker
import androidx.work.WorkerParameters

/**
 * B1.1 R2 (Codex §4): durable pending-tick drain job
 * ([POSITION_TICK_DRAIN_WORK_NAME], one-time, CONNECTED constraint).
 *
 * Runs exactly one bounded drain pass. Every follow-up (backoff retry,
 * budget continuation, diagnostic retry) is scheduled durably by the pass
 * itself, so the worker always returns success and never relies on
 * WorkManager's own retry. Upload only: no capture, no quote fetch, no
 * policy evaluation, no alert, no foreground service.
 */
class PositionTickDrainWorker(appContext: Context, params: WorkerParameters) : Worker(appContext, params) {
    override fun doWork(): Result {
        try {
            PositionTickUploadRunner.runWorkerPass(applicationContext)
        } catch (e: Exception) {
            // Class name only; the pass has already persisted its follow-up when it could.
            Log.e("PositionTickDrain", "POSITION_TICK_DRAIN_WORKER_ERROR: ex=${e.javaClass.simpleName}")
        }
        return Result.success()
    }
}
