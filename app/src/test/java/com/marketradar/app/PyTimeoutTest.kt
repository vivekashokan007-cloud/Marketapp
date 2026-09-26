package com.marketradar.app

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.util.Collections
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/** H1 (2.6.59): effective timeout helper for blocking Chaquopy calls. */
class PyTimeoutTest {

    private val logs = Collections.synchronizedList(mutableListOf<String>())
    private val releases = mutableListOf<CountDownLatch>()

    @Before
    fun setUp() {
        PyTimeout.logger = { logs.add(it) }
    }

    @After
    fun tearDown() {
        releases.forEach { it.countDown() }
    }

    private fun key() = "test.${UUID.randomUUID()}"

    private fun latch(): CountDownLatch = CountDownLatch(1).also { releases.add(it) }

    @Test
    fun fastPath_returnsValueUnchanged() {
        val value = "{\"ok\":true,\"score\":0.61}"
        val k = key()
        val out = PyTimeout.callWithTimeout(k, 2_000L) { value }
        assertSame(value, out)
        // PyTimeout.logger is process-global: a late completion from an earlier
        // test's released background call may log here. Only this key matters.
        assertTrue(logs.none { it.contains(k) })
    }

    @Test
    fun fastPath_runsOnDaemonThread() {
        var daemon = false
        PyTimeout.callWithTimeout(key(), 2_000L) { daemon = Thread.currentThread().isDaemon; 1 }
        assertTrue(daemon)
    }

    @Test
    fun timeout_returnsNullAfterAboutTheLimitNotTheBlockDuration() {
        val k = key()
        val release = latch()
        val t0 = System.nanoTime()
        val out = PyTimeout.callWithTimeout(k, 200L) { release.await(10, TimeUnit.SECONDS); "late" }
        val elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - t0)
        assertNull(out)
        assertTrue("elapsed=$elapsedMs", elapsedMs in 180L..2_000L)
        assertTrue(logs.any { it.startsWith("PY_TIMEOUT: key=$k exceeded 200ms") })
    }

    @Test
    fun timedOutCallInFlight_blocksSecondCallOnSameKey() {
        val k = key()
        val release = latch()
        val starts = AtomicInteger(0)
        assertNull(PyTimeout.callWithTimeout(k, 100L) {
            starts.incrementAndGet(); release.await(10, TimeUnit.SECONDS); "late"
        })
        assertTrue(PyTimeout.isTimedOutCallInFlight(k))

        val t0 = System.nanoTime()
        val second = PyTimeout.callWithTimeout(k, 5_000L) { starts.incrementAndGet(); "second" }
        val elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - t0)
        assertNull(second)
        assertEquals(1, starts.get())
        assertTrue("skip must be immediate, elapsed=$elapsedMs", elapsedMs < 1_000L)
        assertTrue(logs.any { it.startsWith("PY_TIMEOUT_INFLIGHT_SKIP: key=$k") })

        // Once the stuck call finishes, the key is usable again.
        release.countDown()
        val deadline = System.currentTimeMillis() + 5_000L
        while (PyTimeout.isTimedOutCallInFlight(k) && System.currentTimeMillis() < deadline) Thread.sleep(10)
        assertFalse(PyTimeout.isTimedOutCallInFlight(k))
        assertEquals("third", PyTimeout.callWithTimeout(k, 2_000L) { starts.incrementAndGet(); "third" })
        assertEquals(2, starts.get())
    }

    @Test
    fun exceptions_propagateUnwrapped() {
        val boom = IllegalStateException("py boom")
        try {
            PyTimeout.callWithTimeout(key(), 2_000L) { throw boom }
            fail("expected exception")
        } catch (e: IllegalStateException) {
            assertSame(boom, e)
        }
        class FakePyException(msg: String) : RuntimeException(msg)
        try {
            PyTimeout.callWithTimeout<String>(key(), 2_000L) { throw FakePyException("Traceback: KeyError") }
            fail("expected exception")
        } catch (e: FakePyException) {
            assertEquals("Traceback: KeyError", e.message)
        }
    }

    @Test
    fun exceptionDoesNotMarkKeyInFlight() {
        val k = key()
        try { PyTimeout.callWithTimeout<String>(k, 2_000L) { throw RuntimeException("x") } } catch (_: RuntimeException) {}
        assertFalse(PyTimeout.isTimedOutCallInFlight(k))
        assertEquals("ok", PyTimeout.callWithTimeout(k, 2_000L) { "ok" })
    }

    @Test
    fun differentKeys_doNotBlockEachOther() {
        val stuckKey = key()
        val release = latch()
        assertNull(PyTimeout.callWithTimeout(stuckKey, 100L) { release.await(10, TimeUnit.SECONDS); "late" })
        assertTrue(PyTimeout.isTimedOutCallInFlight(stuckKey))
        val t0 = System.nanoTime()
        val other = PyTimeout.callWithTimeout(key(), 2_000L) { "other" }
        val elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - t0)
        assertEquals("other", other)
        assertTrue("elapsed=$elapsedMs", elapsedMs < 1_000L)
    }

    @Test
    fun callerInterrupt_doesNotAbortWait_andIsRestored() {
        val caller = Thread.currentThread()
        val interrupter = Thread { Thread.sleep(50); caller.interrupt() }
        interrupter.start()
        val out = PyTimeout.callWithTimeout(key(), 2_000L) { Thread.sleep(200); "done" }
        assertEquals("done", out)
        assertTrue(Thread.interrupted()) // flag restored (and cleared here)
        interrupter.join()
    }

    @Test
    fun fastPath_sameKeyCallsAreNotSerialized() {
        // setContext scores candidates via one key; the guard must not serialize healthy calls.
        val k = key()
        val barrier = java.util.concurrent.CyclicBarrier(2)
        val results = Collections.synchronizedList(mutableListOf<String?>())
        val threads = (1..2).map { i ->
            Thread {
                results.add(PyTimeout.callWithTimeout(k, 3_000L) { barrier.await(2, TimeUnit.SECONDS); "r$i" })
            }
        }
        threads.forEach { it.start() }
        threads.forEach { it.join() }
        // Both blocks had to be running at once to pass the barrier; serialization would time out.
        assertEquals(setOf("r1", "r2"), results.toSet())
        assertFalse(PyTimeout.isTimedOutCallInFlight(k))
    }

    @Test
    fun loggerFailure_doesNotChangeOutcome() {
        PyTimeout.logger = { throw RuntimeException("Method w in android.util.Log not mocked") }
        val release = latch()
        assertNull(PyTimeout.callWithTimeout(key(), 50L) { release.await(10, TimeUnit.SECONDS); "late" })
    }
}
