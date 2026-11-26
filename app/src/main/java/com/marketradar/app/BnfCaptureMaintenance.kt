package com.marketradar.app

import android.content.Context
import androidx.work.*
import okhttp3.OkHttpClient
import okhttp3.Request
import java.time.LocalDate
import java.time.LocalTime
import java.time.ZoneId
import java.util.concurrent.TimeUnit

/** Android's network-constrained retry scheduler; reuses the same capture outbox. */
object BnfCaptureMaintenance {
    fun install(context: Context) {
        val work = PeriodicWorkRequestBuilder<BnfCaptureMaintenanceWorker>(1, TimeUnit.HOURS)
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()).build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork("bnf_capture_maintenance_v1", ExistingPeriodicWorkPolicy.KEEP, work)
    }
}
class BnfCaptureMaintenanceWorker(c: Context, p: WorkerParameters): Worker(c, p) {
    override fun doWork(): Result {
        val c = applicationContext
        val ist = ZoneId.of("Asia/Kolkata")
        val day = LocalDate.now(ist).toString()
        var failed = false
        runCatching { BnfReferenceCapture.daily(c.filesDir, day); BnfReferenceCapture.monthly(c.filesDir, day) }
            .onFailure { failed = true }
        // Capture late institutional publications with receipt clocks, even after watch mode ends.
        if (LocalTime.now(ist).hour >= 16) {
            val token = c.getSharedPreferences("market_radar", Context.MODE_PRIVATE).getString("auth_token", "").orEmpty()
            if (token.isNotBlank()) {
                val client = OkHttpClient.Builder().connectTimeout(15, TimeUnit.SECONDS).readTimeout(15, TimeUnit.SECONDS).build()
                for (suffix in listOf("fii?data_type=NSE_EQ%7CCASH,NSE_FO%7CINDEX_FUTURES,NSE_FO%7CINDEX_OPTIONS&interval=1D", "dii?data_type=NSE_EQ%7CCASH&interval=1D")) {
                    val url = "https://api.upstox.com/v2/market/$suffix"
                    val start = System.currentTimeMillis()
                    runCatching {
                        client.newCall(Request.Builder().url(url).header("Authorization", "Bearer $token")
                            .header("Accept", "application/json").build()).execute().use { r ->
                            require(BnfRawCapture.record(c.filesDir, url, start, System.currentTimeMillis(), r.code, r.body?.bytes()))
                            if (!r.isSuccessful) failed = true
                        }
                    }.onFailure {
                        failed = true
                        BnfRawCapture.record(c.filesDir, url, start, System.currentTimeMillis(), 0, null, it.javaClass.simpleName)
                    }
                }
            }
        }
        runCatching { BnfSourceUploader.drain(c) }.onFailure { failed = true }
        return if (failed) Result.retry() else Result.success()
    }
}
