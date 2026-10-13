package com.marketradar.app

import org.json.JSONObject
import java.io.File
import java.time.Instant
import java.time.ZoneOffset

/** b503: response contract of public.capture_event_record (migration 20261013120000). */
object CaptureEventProtocol {
    data class Response(val code: Int?, val body: String?)

    /** [quota] = PT429 (daily device quota): retryable, but drains should cool down until the next IST day. */
    data class Decision(val outcome: FrozenEvidenceOutbox.Outcome, val quota: Boolean = false, val reason: String)

    /**
     * R5: an acknowledgement is bound to the queued event. INSERTED / DUPLICATE are ACKs ONLY when the response
     * names exactly [expectedEventId]; REJECTED_CONFLICT is quarantined only for that id. A missing, malformed or
     * mismatched acknowledgement is unproven: it stays queued (RETRY), it never deletes the event.
     */
    fun decide(code: Int?, body: String?, expectedEventId: String?): Decision {
        if (code != null && code in 200..299) {
            val j = runCatching { JSONObject(body ?: "") }.getOrNull()
                ?: return Decision(FrozenEvidenceOutbox.Outcome.RETRY, reason = "ack_body_unreadable")
            val status = j.optString("status")
            val id = if (j.isNull("event_id")) "" else j.optString("event_id")
            if (status !in setOf("INSERTED", "DUPLICATE", "REJECTED_CONFLICT"))
                return Decision(FrozenEvidenceOutbox.Outcome.RETRY, reason = "ack_status_unknown")
            if (expectedEventId == null || id != expectedEventId)
                return Decision(FrozenEvidenceOutbox.Outcome.RETRY, reason = "ack_event_id_mismatch")
            return if (status == "REJECTED_CONFLICT") Decision(FrozenEvidenceOutbox.Outcome.QUARANTINE, reason = "conflict")
            else Decision(FrozenEvidenceOutbox.Outcome.ACKNOWLEDGED, reason = status.lowercase())
        }
        val pg = body?.let { Pc2TelemetryOutbox.errorCodeOf(it) }
        return when {
            code == null -> Decision(FrozenEvidenceOutbox.Outcome.RETRY, reason = "transport")
            pg == "PT429" -> Decision(FrozenEvidenceOutbox.Outcome.RETRY, quota = true, reason = "daily_quota")
            pg == "PT403" -> Decision(FrozenEvidenceOutbox.Outcome.RETRY, reason = "not_authorized_yet")
            pg == "PGRST202" || code == 404 -> Decision(FrozenEvidenceOutbox.Outcome.RETRY, reason = "capability_absent")
            pg == "23514" || pg == "22023" || pg == "22P02" -> Decision(FrozenEvidenceOutbox.Outcome.QUARANTINE, reason = "rejected_$pg")
            code in 400..499 && code !in listOf(401, 403, 408, 429) -> Decision(FrozenEvidenceOutbox.Outcome.QUARANTINE, reason = "http_$code")
            else -> Decision(FrozenEvidenceOutbox.Outcome.RETRY, reason = "http_$code")
        }
    }

    fun classify(code: Int?, body: String?, expectedEventId: String?): FrozenEvidenceOutbox.Outcome =
        decide(code, body, expectedEventId).outcome

    /** Status-only form: cannot bind an acknowledgement to an event, so it never acknowledges. */
    fun classify(code: Int?, body: String?): FrozenEvidenceOutbox.Outcome = classify(code, body, null)
}

/**
 * b503 R4: drains the capture-event outbox with a bounded quota cooldown. A PT429 (server daily cap) stops the
 * drain and persists a cooldown until the next IST day (+5 min); events stay queued, nothing is evicted. The
 * local outbox itself is bounded (2000 entries / 8 MiB); when it is full, terminal evidence stays in the capture
 * journal markers instead (see CaptureEngine.terminalize), so "nothing lost" holds only within those bounds.
 */
class CaptureEventDrainer(
    private val outbox: FrozenEvidenceOutbox,
    private val cooldownFile: File,
    private val nowMs: () -> Long = { System.currentTimeMillis() },
    private val fs: CaptureFs = CaptureFs.REAL
) {
    data class Result(val status: FrozenEvidenceOutbox.Status, val cooldownUntilMs: Long, val skippedForCooldown: Boolean)

    fun cooldownUntilMs(): Long = runCatching { fs.read(cooldownFile).trim().toLong() }.getOrDefault(0L)

    fun drain(maxAttempts: Int = 20, send: (eventId: String, payload: String) -> CaptureEventProtocol.Response): Result {
        val until = cooldownUntilMs()
        val now = nowMs()
        // A cooldown never exceeds 25 h; a corrupt/far-future value is ignored rather than blocking forever.
        if (until > now && until - now <= 25L * 3600_000L) return Result(outbox.status(stop = "quota_cooldown"), until, true)
        var quota = false
        val s = outbox.drain(maxAttempts) { id, payload ->
            if (quota) return@drain FrozenEvidenceOutbox.Outcome.RETRY // stop spending requests after the cap
            val r = send(id, payload)
            val d = CaptureEventProtocol.decide(r.code, r.body, id)
            if (d.quota) quota = true
            d.outcome
        }
        var next = 0L
        if (quota) {
            next = nextIstDayStartMs(now) + 5 * 60_000L
            runCatching { fs.atomicWrite(cooldownFile, next.toString()) }
        } else if (until != 0L) runCatching { fs.delete(cooldownFile) }
        return Result(s, next, false)
    }

    companion object {
        private val IST = ZoneOffset.ofHoursMinutes(5, 30)
        fun nextIstDayStartMs(nowMs: Long): Long =
            Instant.ofEpochMilli(nowMs).atOffset(IST).toLocalDate().plusDays(1).atStartOfDay().toInstant(IST).toEpochMilli()
    }
}
