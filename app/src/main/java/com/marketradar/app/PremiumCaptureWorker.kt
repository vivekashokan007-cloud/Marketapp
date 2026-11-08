package com.marketradar.app

import org.json.JSONObject

/**
 * Keeps storage persistence OFF the polling/brain thread, with bounded occupancy.
 *
 * ## Why the previous design was wrong
 *
 * The coordinator used to persist synchronously and check a wall-clock budget only *before*
 * starting each index. Everything after that check — deep copy, directory scans, acquiring the
 * store's root lock, cohort and authority writes, six record writes, counter writes — then ran
 * with no deadline at all, and the post-loop counter write ran regardless of budget exhaustion.
 * A slow or blocked filesystem could therefore delay a poll for far longer than the budget, and
 * `catch (Throwable)` does not help: it observes an eventual failure, it does not interrupt a
 * blocked call. The zero-budget test only ever proved that work can be skipped *before it starts*.
 *
 * So the budget is not the fix and has been removed as the primary mechanism. Isolation is.
 *
 * ## The contract
 *
 * - **The poll-facing call never waits.** [submit] takes a short lock to update bookkeeping and
 *   returns; it performs no IO and never waits on a store lock, so a blocked filesystem or a held
 *   capture lock cannot reach the poll thread.
 * - **One running job, at most one waiting job.** Not an unbounded executor, and not a thread per
 *   poll: a single daemon thread is created lazily and then reused for the life of the process.
 * - **Occupancy AND bytes are bounded.** A job carries immutable prepared evidence — the frozen
 *   response text, already measured — and is refused if it alone exceeds [MAX_PREPARED_BYTES_PER_JOB]
 *   or if admitting it would exceed [MAX_PENDING_BYTES] across running plus waiting.
 * - **Refusal is visible**, never silent: every rejection returns a reason the caller reports and
 *   the durable status records.
 * - **Durations use a monotonic clock** ([System.nanoTime]), never wall time. Evidence timestamps
 *   remain wall-clock, because those are real observations.
 *
 * ## What this deliberately does NOT claim
 *
 * A job already inside a blocking filesystem call cannot be interrupted, and this does not pretend
 * otherwise: there is no cancellation of in-flight IO. What is bounded is the poll thread's
 * exposure and the amount of pending work — not the duration of a write that the OS has already
 * accepted. A stuck job holds the single running slot, further submissions are refused with a
 * stated reason, and capture stops cleanly rather than queueing without limit.
 */
object PremiumCaptureWorker {

    const val WORKER_VERSION = "premium_capture_worker_v1_one_running_one_waiting_bounded"

    /** At most one job may wait behind the running one. */
    const val MAX_WAITING = 1

    /** A single prepared payload. Larger than any real NF/BNF chain, small enough to bound heap. */
    const val MAX_PREPARED_BYTES_PER_JOB = 1024 * 1024

    /** Running plus waiting, together. */
    const val MAX_PENDING_BYTES = 2 * MAX_PREPARED_BYTES_PER_JOB

    const val ACCEPTED_RUNNING = "ACCEPTED_STARTED"
    const val ACCEPTED_QUEUED = "ACCEPTED_QUEUED_BEHIND_ONE"
    const val REFUSED_BUSY = "REFUSED_WORKER_AT_OCCUPANCY_LIMIT"
    const val REFUSED_BYTES = "REFUSED_PENDING_BYTE_LIMIT"
    const val REFUSED_TOO_LARGE = "REFUSED_PREPARED_PAYLOAD_TOO_LARGE"

    /**
     * Immutable prepared evidence plus the persistence action.
     *
     * `chainText` is the frozen response as text. Freezing on the caller is what makes the payload
     * immutable: the live formatter cannot mutate a String, so the worker can never observe a
     * half-written object, and what it digests is exactly what was prepared.
     */
    class Job(
        val index: String,
        val sessionDate: String,
        val preparedBytes: Int,
        val persist: () -> JSONObject,
    )

    // java.lang.Object explicitly: Kotlin does not expose wait/notify on Any
    private val lock = java.lang.Object()
    private var running: Job? = null
    private var waiting: Job? = null
    private var pendingBytes: Int = 0
    private var thread: Thread? = null
    private var lastResult: JSONObject? = null
    /** The most recent completed result per index, so a caller can see BOTH indices. */
    private val lastByIndex = HashMap<String, JSONObject>()
    private var completed: Long = 0L
    private var failed: Long = 0L

    /**
     * Hand a prepared job to the worker. Returns a status immediately and NEVER waits for IO.
     */
    fun submit(job: Job): String {
        if (job.preparedBytes > MAX_PREPARED_BYTES_PER_JOB) return REFUSED_TOO_LARGE
        synchronized(lock) {
            if (running != null && waiting != null) return REFUSED_BUSY
            if (pendingBytes + job.preparedBytes > MAX_PENDING_BYTES) return REFUSED_BYTES
            val status: String
            if (running == null) {
                running = job
                status = ACCEPTED_RUNNING
            } else {
                waiting = job
                status = ACCEPTED_QUEUED
            }
            pendingBytes += job.preparedBytes
            ensureThreadLocked()
            lock.notifyAll()
            return status
        }
    }

    /** One daemon thread for the process, created on first use and then reused. */
    private fun ensureThreadLocked() {
        if (thread?.isAlive == true) return
        val t = Thread({ loop() }, "premium-capture-worker")
        t.isDaemon = true
        thread = t
        t.start()
    }

    private fun loop() {
        while (true) {
            val job: Job
            synchronized(lock) {
                while (running == null) {
                    try {
                        lock.wait()
                    } catch (ie: InterruptedException) {
                        Thread.currentThread().interrupt()
                        return
                    }
                }
                job = running!!
            }
            // OUTSIDE the lock, so a slow write cannot block a submission's bookkeeping
            var result: JSONObject? = null
            try {
                result = job.persist()
            } catch (t: Throwable) {
                result = JSONObject().put("status", "CAPTURE_ERROR")
                    .put("error", t.javaClass.simpleName).put("index", job.index)
            }
            synchronized(lock) {
                pendingBytes -= job.preparedBytes
                if (pendingBytes < 0) pendingBytes = 0
                lastResult = result
                result?.let { lastByIndex[job.index] = it }
                if (result?.optString("status") == "CAPTURE_ERROR") failed++ else completed++
                running = waiting
                waiting = null
                // wake awaitIdle and any submitter watching occupancy; without this the
                // idle wait could only ever end by timing out
                lock.notifyAll()
            }
        }
    }

    /**
     * Test and status helper: wait until nothing is running or waiting. Returns false on timeout
     * rather than throwing, and uses a monotonic deadline.
     */
    fun awaitIdle(timeoutMs: Long): Boolean {
        val deadline = System.nanoTime() + timeoutMs * 1_000_000L
        synchronized(lock) {
            while (running != null || waiting != null) {
                val remainingNs = deadline - System.nanoTime()
                if (remainingNs <= 0L) return false
                try {
                    lock.wait(remainingNs / 1_000_000L + 1L)
                } catch (ie: InterruptedException) {
                    Thread.currentThread().interrupt()
                    return false
                }
            }
            return true
        }
    }

    /** The last completed persistence result for one index, or null. */
    fun resultFor(index: String): JSONObject? = synchronized(lock) { lastByIndex[index] }

    /** Observable occupancy, for the status surface and for tests. */
    fun stats(): JSONObject = synchronized(lock) {
        JSONObject()
            .put("worker_version", WORKER_VERSION)
            .put("running", running != null)
            .put("waiting", waiting != null)
            .put("pending_bytes", pendingBytes)
            .put("max_pending_bytes", MAX_PENDING_BYTES)
            .put("max_waiting", MAX_WAITING)
            .put("completed", completed)
            .put("failed", failed)
            .put("last_result", lastResult ?: JSONObject.NULL)
            .put("in_flight_io_is_not_cancellable", true)
    }

    /**
     * Drop any WAITING job and forget counters. Test-only: it cannot cancel a running job,
     * because in-flight IO is not interruptible, and it says so rather than implying otherwise.
     */
    fun resetForTest() {
        synchronized(lock) {
            waiting?.let { pendingBytes -= it.preparedBytes }
            waiting = null
            lastResult = null
            lastByIndex.clear()
            completed = 0L
            failed = 0L
            if (pendingBytes < 0) pendingBytes = 0
        }
    }
}
