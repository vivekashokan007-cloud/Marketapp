package com.marketradar.app

import android.content.Context
import androidx.work.Worker
import androidx.work.WorkerParameters

/**
 * B1.1 R2/R3 (Codex §4, R3 Blocker B): durable pending-tick drain job
 * ([POSITION_TICK_DRAIN_WORK_NAME], one-time, CONNECTED constraint, explicit
 * EXPONENTIAL backoff from [POSITION_TICK_WORKER_RETRY_BACKOFF_MS]).
 *
 * Runs exactly one bounded drain pass. Classified outcomes (backoff retry,
 * budget continuation, diagnostic retry, empty queue) are followed up durably
 * by the pass itself → success. An UNEXPECTED exception → Result.retry(), so
 * WorkManager re-runs this job after the bounded backoff; the queue is kept.
 * Upload only: no capture, no quote fetch, no policy evaluation, no alert,
 * no foreground service.
 */
class PositionTickDrainWorker(appContext: Context, params: WorkerParameters) : Worker(appContext, params) {
    override fun doWork(): Result =
        when (PositionTickUploadRunner.runWorkerPass(applicationContext, runAttemptCount)) {
            PositionTickWorkerResult.SUCCESS -> Result.success()
            PositionTickWorkerResult.RETRY -> Result.retry()
        }
}
