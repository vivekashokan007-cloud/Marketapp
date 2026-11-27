package com.marketradar.app

import android.app.Application
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

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], application = Application::class)
class CloudRecoveryStartupTest {
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
