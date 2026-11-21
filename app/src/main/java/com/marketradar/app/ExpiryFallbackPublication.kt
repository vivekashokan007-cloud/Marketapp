package com.marketradar.app

import org.json.JSONObject
import java.util.UUID

/**
 * Expiry-day Paper-BUY fallback (expiry_fallback_v1, REVIEW DRAFT r4): publication reconciliation for the Python
 * apply step (Codex R2 reply s3, R3 reply). Every apply is one ATTEMPT bound to an id and a generation (a monotonic
 * counter persisted through [GenerationStore], so it keeps increasing across a process restart). Native is the only
 * side that publishes, so native decides, once per step, under one lock:
 *   APPLY_ACCEPTED  (intermediate, R4) the Python result came back inside PyTimeout, the native apply deadline had
 *              not passed and the attempt was still the current one -> the patch is applied to the IN-MEMORY result
 *              only. Nothing is acknowledged to Python yet (its outcome stays UNCONFIRMED);
 *   PUBLISHED  (R4) only after the app's final gate, AnalysisAuthority.publish, returned PUBLISHED from a successful
 *              result-store commit ([publishThroughGate]); recorded with the time publish returned. It means the
 *              app committed the result, NOT that it was rendered, notified or seen;
 *   NOT_PUBLISHED (R4) the final gate refused it (SUPERSEDED_INPUT / STALE_MARKET / MARKET_IDENTITY_UNAVAILABLE /
 *              PERSIST_FAILED, the authority's own reason), or the analysis ended or aborted before the gate was
 *              entered ([fenceUnfinished]; the result was provably never committed by this analysis);
 *   GATE_UNRESOLVED (R4) the gate itself threw: the commit may or may not have happened, so nothing is sent and the
 *              retained outcome stays UNCONFIRMED (never inferred either way);
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

    enum class State { PENDING, APPLY_ACCEPTED, PUBLISHED, NOT_PUBLISHED, GATE_UNRESOLVED, TIMED_OUT, CANCELLED, UNKNOWN }

    data class Attempt(val id: String, val generation: Long) {
        fun toJson(): JSONObject = JSONObject().put("attempt_id", id).put("generation", generation)
    }

    /** One decision for an attempt: its final state and the attempts a new [begin] cancelled. */
    data class Decision(val attempt: Attempt, val state: State, val patch: JSONObject? = null,
                        val reason: String? = null, val decidedMs: Long? = null)

    private val states = LinkedHashMap<String, State>()
    private val reasons = HashMap<String, String>()
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
        while (states.size > maxTracked) states.keys.first().let { states.remove(it); reasons.remove(it) }
        return a to cancelled
    }

    /** APPLY_ACCEPTED (R4; R3 said PUBLISHED here) only when [a] is PENDING, still current and [nowMs] <= [deadlineMs]; otherwise the attempt is
     *  decided TIMED_OUT (deadline) or CANCELLED (superseded) if it was still pending, and that state is returned.
     *  An unknown attempt (e.g. from before a restart) returns UNKNOWN and is never published. */
    @Synchronized fun tryAccept(a: Attempt, nowMs: Long, deadlineMs: Long): State {
        val st = states[a.id] ?: return State.UNKNOWN
        if (st != State.PENDING) return st
        val next = when {
            current?.id != a.id -> State.CANCELLED
            nowMs > deadlineMs -> State.TIMED_OUT
            else -> State.APPLY_ACCEPTED
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

    /** R4: APPLY_ACCEPTED -> [to] (PUBLISHED / NOT_PUBLISHED / GATE_UNRESOLVED). True only if THIS call made the
     *  change; any other state (terminal, PENDING, unknown) is left untouched, so a finalizer can never overwrite a
     *  successful (or any other) terminal result. */
    @Synchronized fun finish(a: Attempt, to: State, reason: String?): Boolean {
        require(to == State.PUBLISHED || to == State.NOT_PUBLISHED || to == State.GATE_UNRESOLVED)
        if (states[a.id] != State.APPLY_ACCEPTED) return false
        states[a.id] = to
        if (reason != null) reasons[a.id] = reason
        return true
    }

    @Synchronized fun stateOf(a: Attempt): State = states[a.id] ?: State.UNKNOWN
    @Synchronized fun reasonOf(a: Attempt): String? = reasons[a.id]

    companion object {
        /**
         * One apply + publication decision. [apply] runs the Python apply (given the attempt JSON) under PyTimeout
         * [key]/[timeoutMs]; [reconcile] records every decided attempt in Python (asynchronously in the service).
         * The patch is applied to [result] ONLY when the attempt is APPLY_ACCEPTED (R4: not yet acknowledged; the
         * terminal decision comes from [publishThroughGate] / [fenceUnfinished]); on any other outcome every
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
            if (st == State.APPLY_ACCEPTED) {
                ExpiryFallbackAcquirer.applyPatch(result, patch)
                ExpiryFallbackAcquirer.markWait(result, "NOT_ACQUIRED")   // a skipped index never stays pending
            } else {
                ExpiryFallbackAcquirer.markWait(result, if (st == State.TIMED_OUT) "APPLY_TIMEOUT" else "APPLY_SUPERSEDED")
            }
            if (st != State.APPLY_ACCEPTED) reconcile(a, st)      // R4: no acknowledgement before the final gate
            return Decision(a, st, if (st == State.APPLY_ACCEPTED) patch else null)
        }

        /**
         * R4 (Codex R3 reply): the app's final publication gate for a result that may carry an APPLY_ACCEPTED
         * attempt. Calls the production [AnalysisAuthority.publish] with [commit] unchanged and returns its outcome
         * unchanged. Only afterwards is the attempt decided: PUBLISHED when the outcome is PUBLISHED (the commit
         * succeeded), else NOT_PUBLISHED with the authority's reason; [reconcile] then receives that one decision with
         * the time publish returned. If publish throws, the attempt becomes GATE_UNRESOLVED, nothing is reconciled
         * (Python keeps UNCONFIRMED) and the exception propagates. With no accepted attempt this is exactly
         * `authority.publish(identity, commit)`.
         */
        fun publishThroughGate(pub: ExpiryFallbackPublication, attempt: Attempt?, authority: AnalysisAuthority,
                               identity: AnalysisAuthority.Identity, nowMs: () -> Long, commit: () -> Boolean,
                               reconcile: (Decision) -> Unit): AnalysisAuthority.Outcome {
            if (attempt == null || pub.stateOf(attempt) != State.APPLY_ACCEPTED) return authority.publish(identity, commit)
            val outcome = try {
                authority.publish(identity, commit)
            } catch (e: Throwable) {
                pub.finish(attempt, State.GATE_UNRESOLVED, "GATE_EXCEPTION:" + e.javaClass.simpleName)
                throw e
            }
            val decidedMs = nowMs()
            val published = outcome == AnalysisAuthority.Outcome.PUBLISHED
            val reason = if (published) null else outcome.name
            if (pub.finish(attempt, if (published) State.PUBLISHED else State.NOT_PUBLISHED, reason)) {
                reconcile(Decision(attempt, pub.stateOf(attempt), null, reason, decidedMs))
            }
            return outcome
        }

        /**
         * R4 finalizer: an attempt still APPLY_ACCEPTED when the analysis ends never entered the final gate, so its
         * result was not committed by this analysis -> NOT_PUBLISHED with [reason]. Any other state (PUBLISHED,
         * NOT_PUBLISHED, GATE_UNRESOLVED, TIMED_OUT, CANCELLED) is never touched. Returns true if it fenced.
         */
        fun fenceUnfinished(pub: ExpiryFallbackPublication, attempt: Attempt?, reason: String, nowMs: () -> Long,
                            reconcile: (Decision) -> Unit): Boolean {
            if (attempt == null || !pub.finish(attempt, State.NOT_PUBLISHED, reason)) return false
            reconcile(Decision(attempt, State.NOT_PUBLISHED, null, reason, nowMs()))
            return true
        }
    }
}
