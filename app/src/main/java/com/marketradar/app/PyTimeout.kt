package com.marketradar.app

import com.marketradar.app.util.LogBuffer
import java.util.concurrent.Callable
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ExecutionException
import java.util.concurrent.Future
import java.util.concurrent.FutureTask
import java.util.concurrent.SynchronousQueue
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException
import java.util.concurrent.atomic.AtomicInteger

/**
 * H1 fix (2.6.59): an effective timeout for blocking Chaquopy `callAttr` calls.
 *
 * `withTimeoutOrNull { py.callAttr(...) }` never fires, because the block never
 * suspends. This helper runs the block on a dedicated daemon thread and waits
 * with `Future.get(timeout)`.
 *
 * - Fast path: the block's value is returned as is. An exception thrown by the
 *   block is rethrown unwrapped, not as an ExecutionException.
 * - Timeout: returns null so the caller's EXISTING timeout/fallback branch runs.
 *   The Python call is NOT interrupted (Chaquopy/CPython frames can't be
 *   interrupted safely), so it keeps running in the background.
 * - No pile-up: while a timed-out call for [key] is still running, later calls
 *   for the same key don't start a second Python call. They return null at once
 *   (the same timeout path) and log PY_TIMEOUT_INFLIGHT_SKIP.
 * - Different keys never block each other: the executor is unbounded, and the
 *   pile-up guard allows at most one stuck call per key.
 * - The caller's wait ignores interrupts, as the old undispatched
 *   withTimeoutOrNull did. Any interrupt is restored before returning.
 */
object PyTimeout {
    private const val TAG = "PyTimeout"

    private class InFlight(val future: Future<*>, val startedMs: Long, val timeoutMs: Long)

    private val timedOutInFlight = ConcurrentHashMap<String, InFlight>()
    private val threadSeq = AtomicInteger(0)

    private val executor = ThreadPoolExecutor(
        0, Int.MAX_VALUE, 60L, TimeUnit.SECONDS, SynchronousQueue()
    ) { r ->
        Thread(r, "py-timeout-${threadSeq.incrementAndGet()}").apply { isDaemon = true }
    }

    /** Test seam: replaced in unit tests, where android.util.Log is unavailable. */
    @Volatile
    internal var logger: (String) -> Unit = { msg -> LogBuffer.add('W', TAG, msg) }

    /** True while a timed-out call for [key] is still running in the background. */
    fun isTimedOutCallInFlight(key: String): Boolean {
        val prior = timedOutInFlight[key] ?: return false
        return !prior.future.isDone
    }

    /**
     * [T] is non-null on purpose: a null return means "timed out / skipped" and
     * nothing else. A block that can legitimately yield null (for example a raw
     * PyObject? from a Python None) must not use this API unwrapped. Every current
     * call site returns `callAttr(...).toString()`, which is never null.
     * No lock is taken on the fast path, so same-key calls that haven't timed
     * out run concurrently, exactly as they did on their caller threads before.
     */
    fun <T : Any> callWithTimeout(key: String, timeoutMs: Long, block: () -> T): T? {
        val prior = timedOutInFlight[key]
        if (prior != null) {
            if (!prior.future.isDone) {
                log(
                    "PY_TIMEOUT_INFLIGHT_SKIP: key=$key a previous call that timed out " +
                        "(limit ${prior.timeoutMs}ms) is still running " +
                        "(age=${System.currentTimeMillis() - prior.startedMs}ms); not starting a second " +
                        "Python call, taking the timeout path"
                )
                return null
            }
            timedOutInFlight.remove(key, prior)
        }

        val startedMs = System.currentTimeMillis()
        val task = object : FutureTask<T>(Callable { block() }) {
            override fun done() {
                val stuck = timedOutInFlight[key]
                if (stuck != null && stuck.future === this && timedOutInFlight.remove(key, stuck)) {
                    log(
                        "PY_TIMEOUT_LATE_COMPLETE: key=$key timed-out call finished after " +
                            "${System.currentTimeMillis() - stuck.startedMs}ms; result discarded"
                    )
                }
            }
        }
        executor.execute(task)
        return try {
            getUninterruptibly(task, timeoutMs)
        } catch (e: TimeoutException) {
            val entry = InFlight(task, startedMs, timeoutMs)
            timedOutInFlight[key] = entry
            // Finished in the gap after the timeout: done() missed the entry, so drop it here.
            if (task.isDone) timedOutInFlight.remove(key, entry)
            log(
                "PY_TIMEOUT: key=$key exceeded ${timeoutMs}ms; the Python call keeps running " +
                    "in the background (not interrupted); taking the timeout path"
            )
            null
        } catch (e: ExecutionException) {
            throw e.cause ?: e
        }
    }

    private fun <T> getUninterruptibly(future: Future<T>, timeoutMs: Long): T {
        var interrupted = false
        try {
            val deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(timeoutMs)
            while (true) {
                try {
                    return future.get(deadline - System.nanoTime(), TimeUnit.NANOSECONDS)
                } catch (_: InterruptedException) {
                    interrupted = true
                }
            }
        } finally {
            if (interrupted) Thread.currentThread().interrupt()
        }
    }

    private fun log(msg: String) {
        try {
            logger(msg)
        } catch (_: Throwable) {
            // Logging must never change the call's outcome.
        }
    }
}
