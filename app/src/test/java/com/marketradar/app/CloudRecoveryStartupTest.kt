package com.marketradar.app

import android.app.Application
import android.content.Context
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.io.File

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], application = Application::class)
class CloudRecoveryStartupTest {
    @Test fun verifiedExistingPhoneOpensDuringLongRecoveryButNeverStartsMonitoring() {
        val c = RuntimeEnvironment.getApplication()
        val readyField = CloudRecovery::class.java.getDeclaredField("ready").apply { isAccessible = true }
        val previousReady = readyField.getBoolean(CloudRecovery)
        val prefs = c.getSharedPreferences("cloud_vault_v1", Context.MODE_PRIVATE)
        val original = prefs.all.toMap()
        val marker = File(c.noBackupFilesDir, "cloud_restore_pending")
        val previousMarker = marker.takeIf { it.exists() }?.readBytes()
        try {
            readyField.setBoolean(CloudRecovery, false) // a new process after an in-place APK update
            marker.delete()
            prefs.edit().clear().commit()
            assertFalse(CloudRecovery.canOpen(c)) // new phone still needs verified restore
            prefs.edit().putBoolean("initialized", true).putString("owner", "owner-1")
                .putLong("revision", 17).putString("verified_at", "2026-11-27T06:57:37Z")
                .putString("checkpoint_sha256", "a".repeat(64)).commit()
            assertTrue(CloudRecovery.canOpen(c))
            assertFalse(CloudRecovery.canMonitor()) // the UI gate grants no monitoring lease
            marker.writeText("restore pending")
            assertFalse(CloudRecovery.canOpen(c)) // interrupted restore stays gated
            marker.delete()
            prefs.edit().remove("checkpoint_sha256").commit()
            assertFalse(CloudRecovery.canOpen(c)) // incomplete local checkpoint is insufficient
        } finally {
            marker.delete()
            if (previousMarker != null) marker.writeBytes(previousMarker)
            val editor = prefs.edit().clear()
            original.forEach { (key, value) -> when (value) {
                is String -> editor.putString(key, value)
                is Boolean -> editor.putBoolean(key, value)
                is Long -> editor.putLong(key, value)
                is Int -> editor.putInt(key, value)
                is Float -> editor.putFloat(key, value)
            } }
            editor.commit()
            readyField.setBoolean(CloudRecovery, previousReady)
        }
    }

    @Test fun repeatedActivityStartupDoesNotWaitForAnActiveBackupMonitor() {
        val field = CloudRecovery::class.java.getDeclaredField("started").apply { isAccessible = true }
        val gate = field.get(CloudRecovery) as AtomicBoolean
        val previous = gate.getAndSet(true)
        val held = CountDownLatch(1); val release = CountDownLatch(1)
        val threads = Executors.newFixedThreadPool(2)
        try {
            val backup = threads.submit { synchronized(CloudRecovery) {
                held.countDown(); release.await(5, TimeUnit.SECONDS)
            } }
            assertTrue(held.await(2, TimeUnit.SECONDS))
            val activityStart = threads.submit { CloudRecovery.start(RuntimeEnvironment.getApplication()) }
            // A reintroduced @Synchronized start would wait on the held network-pass lock.
            activityStart.get(1, TimeUnit.SECONDS)
            assertEquals(1L, release.count)
            release.countDown(); backup.get(2, TimeUnit.SECONDS)
        } finally { release.countDown(); gate.set(previous); threads.shutdownNow() }
    }
}
