package com.marketradar.app

import org.json.JSONObject
import java.util.UUID

/**
 * Expiry-day Paper-BUY fallback (expiry_fallback_v1, REVIEW DRAFT r3): publication reconciliation for the Python
 * apply step (Codex R2 reply s3). Every apply is one ATTEMPT bound to an id and a generation (a monotonic counter
 * persisted through [GenerationStore], so it keeps increasing across a process restart). Native is the only side
 * that publishes, so native decides, once, under one lock:
 *   PUBLISHED  the Python result came back inside PyTimeout, the native apply deadline had not passed, and the
 *              attempt was still the current one -> the patch is applied and the plan published;
 *   TIMED_OUT  PyTimeout fired (the Python call keeps running and may finish later), or the result came back after
 *              the native apply deadline (returned but not yet accepted);
 *   CANCELLED  a newer attempt began first (superseded), or the apply returned an error.
 * A decided attempt never changes: a late completion of a TIMED_OUT / CANCELLED attempt is refused and never
 * applied, so it can never overwrite a newer decision or lead to an admission. The decision is sent to Python
 * (single_leg_expiry_fallback_reconcile) so the retained outcome says whether it was actually published; Python
 * refuses any change to a decided attempt as well (a late write inherits TIMED_OUT, never PUBLISHED).
 */
class ExpiryFallbackPublication(private val store: GenerationStore, private val maxTracked: Int = 32) {
    interface GenerationStore { fun load(): Long; fun save(value: Long) }

    /** In-memory store for tests / no prefs (a restart is modelled by a new instance over the same store). */
    class MemoryStore(private var v: Long = 0L) : GenerationStore {
        override fun load(): Long = v
        override fun save(value: Long) { v = value }
    }

    enum class State { PENDING, PUBLISHED, TIMED_OUT, CANCELLED, UNKNOWN }

    data class Attempt(val id: String, val generation: Long) {
        fun toJson(): JSONObject = JSONObject().put("attempt_id", id).put("generation", generation)
    }

    /** One decision for an attempt: its final state and the attempts a new [begin] cancelled. */
    data class Decision(val attempt: Attempt, val state: State, val patch: JSONObject? = null)

    private val states = LinkedHashMap<String, State>()
    private var current: Attempt? = null

    /** Starts a new attempt (generation = persisted counter + 1); a still-PENDING older attempt is CANCELLED. */
    @Synchronized fun begin(): Pair<Attempt, List<Decision>> {
        val g = store.load() + 1
        store.save(g)
        val a = Attempt("efb-$g-" + UUID.randomUUID().toString().substring(0, 8), g)
        val cancelled = mutableListOf<Decision>()
        current?.let { prev -> if (states[prev.id] == State.PENDING) { states[prev.id] = State.CANCELLED; cancelled.add(Decision(prev, State.CANCELLED)) } }
        current = a
        states[a.id] = State.PENDING
        while (states.size > maxTracked) states.remove(states.keys.first())
        return a to cancelled
    }

    /** PUBLISHED only when [a] is PENDING, still current and [nowMs] <= [deadlineMs]; otherwise the attempt is
     *  decided TIMED_OUT (deadline) or CANCELLED (superseded) if it was still pending, and that state is returned.
     *  An unknown attempt (e.g. from before a restart) returns UNKNOWN and is never published. */
    @Synchronized fun tryAccept(a: Attempt, nowMs: Long, deadlineMs: Long): State {
        val st = states[a.id] ?: return State.UNKNOWN
        if (st != State.PENDING) return st
        val next = when {
            current?.id != a.id -> State.CANCELLED
            nowMs > deadlineMs -> State.TIMED_OUT
            else -> State.PUBLISHED
        }
        states[a.id] = next
        return next
    }

    /** PENDING -> [to] (TIMED_OUT or CANCELLED); returns the attempt's final state (unchanged if already decided). */
    @Synchronized fun decide(a: Attempt, to: State): State {
        require(to == State.TIMED_OUT || to == State.CANCELLED)
        val st = states[a.id] ?: return State.UNKNOWN
        if (st != State.PENDING) return st
        states[a.id] = to
        return to
    }

    @Synchronized fun stateOf(a: Attempt): State = states[a.id] ?: State.UNKNOWN

    companion object {
        /**
         * One apply + publication decision. [apply] runs the Python apply (given the attempt JSON) under PyTimeout
         * [key]/[timeoutMs]; [reconcile] records every decided attempt in Python (asynchronously in the service).
         * The patch is applied to [result] ONLY when the attempt is accepted as PUBLISHED; on any other outcome every
         * plan still EVIDENCE_REQUIRED becomes an explicit WAIT (APPLY_TIMEOUT / APPLY_SUPERSEDED /
         * APPLY_DEADLINE_EXCEEDED / APPLY_ERROR). Returns the decision for this attempt.
         */
        fun applyAndPublish(result: JSONObject, pub: ExpiryFallbackPublication, key: String, timeoutMs: Long,
                            deadlineMs: Long, nowMs: () -> Long, apply: (JSONObject) -> String,
                            reconcile: (Attempt, State) -> Unit): Decision {
            val (a, cancelled) = pub.begin()
            cancelled.forEach { reconcile(it.attempt, it.state) }
            val r = try {
                PyTimeout.callWithOutcome(key, timeoutMs) { apply(a.toJson()) }
            } catch (e: Exception) {
                val st = pub.decide(a, State.CANCELLED)
                ExpiryFallbackAcquirer.markWait(result, "APPLY_ERROR")
                reconcile(a, st)
                return Decision(a, st)
            }
            val raw = r.value
            if (raw == null) {                               // PyTimeout fired or a timed-out call is still in flight
                val st = pub.decide(a, State.TIMED_OUT)
                ExpiryFallbackAcquirer.markWait(result, "APPLY_TIMEOUT")
                reconcile(a, st)
                return Decision(a, st)
            }
            val patch = try { JSONObject(raw) } catch (e: Exception) { JSONObject().put("error", "APPLY_ERROR") }
            if (patch.has("error")) {
                val dl = patch.optString("error") == "APPLY_DEADLINE_EXCEEDED"
                val st = pub.decide(a, if (dl) State.TIMED_OUT else State.CANCELLED)
                ExpiryFallbackAcquirer.markWait(result, if (dl) "APPLY_DEADLINE_EXCEEDED" else "APPLY_ERROR")
                reconcile(a, st)
                return Decision(a, st)
            }
            val st = pub.tryAccept(a, nowMs(), deadlineMs)
            if (st == State.PUBLISHED) {
                ExpiryFallbackAcquirer.applyPatch(result, patch)
                ExpiryFallbackAcquirer.markWait(result, "NOT_ACQUIRED")   // a skipped index never stays pending
            } else {
                ExpiryFallbackAcquirer.markWait(result, if (st == State.TIMED_OUT) "APPLY_TIMEOUT" else "APPLY_SUPERSEDED")
            }
            reconcile(a, st)
            return Decision(a, st, if (st == State.PUBLISHED) patch else null)
        }
    }
}
