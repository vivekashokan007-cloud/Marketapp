package com.marketradar.app

import org.json.JSONObject
import java.math.BigDecimal
import java.security.MessageDigest
import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.LocalTime
import java.time.ZoneOffset

/**
 * 2.6.76 / b507 - one acknowledged identity for the overnight comparison inputs (Codex overnight-integrity
 * conditions 1-4). Pure and JVM-testable; persistence is done by the caller with ONE commit() of
 * [PREF_KEY] so the morning snapshot and the evening reference are always published as a pair.
 *
 * Python `overnight_inputs.py` recomputes every revision and re-validates independently; the hash text formats
 * below are therefore a cross-language contract (pinned by the real-chain golden test).
 */
object SessionInputs {
    const val PREF_KEY = "session_inputs_v1"
    const val SCHEMA = "overnight_inputs_v1"
    const val PREF_LIVE_KEY = "global_direction_live_v1"
    private val IST: ZoneOffset = ZoneOffset.ofHoursMinutes(5, 30)
    private val CLOSE: LocalTime = LocalTime.of(15, 30)
    const val FUTURE_TOLERANCE_MS = 120_000L

    /** Context keys that are session inputs or derived from them; removed from inherited context on EVERY assembly. */
    val SCRUB_KEYS = listOf(
        "morning_input", "morningInput", "eveningClose", "evening_close", "globalDirection", "global_direction",
        "overnightInputs", "overnightDelta", "morningBias", "institutionalRegime", "effective_bias",
        "fiiCash", "diiCash", "fiiShort", "fiiShortPct", "overnightInputStatus",
    )

    fun sha16(text: String): String =
        MessageDigest.getInstance("SHA-256").digest(text.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }.substring(0, 16)

    /** Canonical decimal string for a strictly positive finite number, else null. */
    fun canonical(raw: Any?): String? {
        val s = when (raw) {
            null, JSONObject.NULL -> return null
            is Number -> raw.toString()
            else -> raw.toString().trim()
        }
        if (s.isEmpty()) return null
        return try {
            val d = s.toDouble()
            if (!d.isFinite() || d <= 0.0) return null
            BigDecimal(s).stripTrailingZeros().toPlainString()
        } catch (_: Exception) { null }
    }

    fun morningRevision(session: String, capturedMs: Long, seq: Long, dow: String, crude: String, gift: String) =
        sha16("morning|$session|$capturedMs|$seq|$dow|$crude|$gift")

    fun eveningRevision(intended: String, capturedMs: Long, seq: Long, dow: String, crude: String, gift: String) =
        sha16("evening|$intended|$capturedMs|$seq|$dow|$crude|$gift")

    fun pairRevision(session: String, mRev: String, eRev: String) = sha16("pair|$session|$mRev|$eRev")

    fun istDate(ms: Long): LocalDate = Instant.ofEpochMilli(ms).atOffset(IST).toLocalDate()
    fun closeMs(d: LocalDate): Long = LocalDateTime.of(d, CLOSE).toInstant(IST).toEpochMilli()

    private fun nextSession(d: LocalDate): LocalDate {
        var x = d.plusDays(1)
        while (!RuleBExitPolicy.isSession(x)) x = x.plusDays(1)
        return x
    }
    fun prevSession(d: LocalDate): LocalDate {
        var x = d.minusDays(1)
        while (!RuleBExitPolicy.isSession(x)) x = x.minusDays(1)
        return x
    }

    /** Documented convention (matches overnight_inputs.intended_session). Throws CalendarUnavailable. */
    fun intendedSession(capturedMs: Long): String {
        val d = istDate(capturedMs)
        return if (RuleBExitPolicy.isSession(d) && capturedMs < closeMs(d)) d.toString() else nextSession(d).toString()
    }

    fun read(raw: String?): JSONObject = try { JSONObject(raw ?: "{}") } catch (_: Exception) { JSONObject() }

    private fun nextSeq(state: JSONObject): Long = state.optLong("seq", 0L) + 1L

    class SaveResult(val ok: Boolean, val error: String?, val state: JSONObject?, val record: JSONObject?)

    /** Build the next state with a new morning record (values from the lock). Caller commits [SaveResult.state]. */
    fun withMorning(state: JSONObject, nowMs: Long, input: JSONObject): SaveResult {
        val session = istDate(nowMs)
        try {
            if (!RuleBExitPolicy.isSession(session)) return SaveResult(false, "NON_TRADING_SESSION", null, null)
        } catch (_: RuleBExitPolicy.CalendarUnavailable) { return SaveResult(false, "CALENDAR_UNAVAILABLE", null, null) }
        if (nowMs >= closeMs(session)) return SaveResult(false, "SESSION_CLOSED", null, null)
        val dow = canonical(input.opt("dowClose")); val crude = canonical(input.opt("crudeSettle"))
        val gift = canonical(input.opt("giftSpot"))
        if (dow == null || crude == null || gift == null) return SaveResult(false, "INVALID_VALUES", null, null)
        val seq = nextSeq(state)
        val rec = JSONObject()
            .put("session", session.toString()).put("captured_at_ms", nowMs).put("seq", seq)
            .put("dowClose", dow).put("crudeSettle", crude).put("giftSpot", gift)
            .put("revision", morningRevision(session.toString(), nowMs, seq, dow, crude, gift))
        val next = JSONObject(state.toString()).put("schema", SCHEMA).put("seq", seq).put("morning", rec)
        return SaveResult(true, null, next, rec)
    }

    fun withEvening(state: JSONObject, nowMs: Long, input: JSONObject): SaveResult {
        val intended = try { intendedSession(nowMs) } catch (_: RuleBExitPolicy.CalendarUnavailable) {
            return SaveResult(false, "CALENDAR_UNAVAILABLE", null, null)
        }
        val dow = canonical(input.opt("dow")); val crude = canonical(input.opt("crude"))
        val gift = canonical(input.opt("gift"))
        if (dow == null || crude == null || gift == null) return SaveResult(false, "INVALID_VALUES", null, null)
        val seq = nextSeq(state)
        val rec = JSONObject()
            .put("intended_session", intended).put("captured_at_ms", nowMs).put("seq", seq)
            .put("dow", dow).put("crude", crude).put("gift", gift)
            .put("revision", eveningRevision(intended, nowMs, seq, dow, crude, gift))
        val next = JSONObject(state.toString()).put("schema", SCHEMA).put("seq", seq).put("evening", rec)
        return SaveResult(true, null, next, rec)
    }

    /** Status of the stored pair for [session] at [nowMs]. Same reasons/order as overnight_inputs.validate. */
    fun status(state: JSONObject, session: String, nowMs: Long): String {
        val m = state.optJSONObject("morning"); val e = state.optJSONObject("evening")
        val sday = try { LocalDate.parse(session) } catch (_: Exception) { return "WRONG_SESSION" }
        val prevClose = try {
            if (!RuleBExitPolicy.isSession(sday)) return "NON_TRADING_SESSION"
            closeMs(prevSession(sday))
        } catch (_: RuleBExitPolicy.CalendarUnavailable) { return "CALENDAR_UNAVAILABLE" }
        if (m == null) return "MORNING_NOT_LOCKED"
        if (e == null) return "NO_EVENING_REFERENCE"
        if (!m.has("captured_at_ms") || !e.has("captured_at_ms") || !m.has("revision") || !e.has("revision")) return "MISSING_IDENTITY"
        val mt = m.optLong("captured_at_ms"); val et = e.optLong("captured_at_ms")
        if (mt > nowMs + FUTURE_TOLERANCE_MS || et > nowMs + FUTURE_TOLERANCE_MS) return "FUTURE_CAPTURE"
        if (m.optString("session") != session || istDate(mt).toString() != session || mt >= closeMs(sday)) return "MORNING_WRONG_SESSION"
        if (e.optString("intended_session") != session) return "REFERENCE_WRONG_SESSION"
        if (!(et > prevClose && et <= closeMs(sday))) return "REFERENCE_OUTSIDE_WINDOW"
        return "OK"
    }

    /** The context block Python validates; always carries whatever records exist so Python can name the reason. */
    fun overnightInputs(state: JSONObject, session: String): JSONObject {
        val out = JSONObject().put("schema", SCHEMA).put("session", session)
        val m = state.optJSONObject("morning"); val e = state.optJSONObject("evening")
        if (m != null) out.put("morning", JSONObject(m.toString()))
        if (e != null) out.put("evening", JSONObject(e.toString()))
        if (m != null && e != null) out.put("pair_revision", pairRevision(session, m.optString("revision"), e.optString("revision")))
        return out
    }

    /** Identity of everything that should invalidate an analysis: both records (or their absence) for the session. */
    fun inputRevision(state: JSONObject, session: String): String {
        val m = state.optJSONObject("morning")?.optString("revision") ?: "-"
        val e = state.optJSONObject("evening")?.optString("revision") ?: "-"
        return sha16("input|$session|$m|$e")
    }

    /**
     * Condition 1: scrub every inherited session input from a persisted context, then inject only the current
     * session's values. [morningInput] is the stored lock object (or null) and is accepted only when its
     * `_session` equals [session].
     */
    fun assemble(ctx: JSONObject, state: JSONObject, session: String, nowMs: Long, morningInput: JSONObject?,
                 live: JSONObject? = null): JSONObject {
        for (k in SCRUB_KEYS) ctx.remove(k)
        val mi = morningInput?.takeIf { it.optString("_session") == session }
        if (mi != null) {
            ctx.put("morning_input", JSONObject(mi.toString()))
            // morning-derived institutional fields re-injected only from the current lock
            for (k in listOf("fiiCash", "diiCash", "fiiShortPct")) if (mi.has(k)) ctx.put(k, mi.get(k))
            if (mi.has("fiiShortPct")) ctx.put("fiiShort", mi.get("fiiShortPct"))
        }
        globalDirection(state, session, live)?.let { ctx.put("globalDirection", it) }
        ctx.put("overnightInputs", overnightInputs(state, session))
        ctx.put("today_ist", session)
        ctx.put("now_ms", nowMs)
        return ctx
    }

    /** Intraday quotes the user typed, stamped with the session and receipt time (each quote keeps its own time). */
    fun liveQuotes(incoming: JSONObject, session: String, nowMs: Long): JSONObject {
        val out = JSONObject().put("session", session).put("captured_at_ms", nowMs)
        for (k in listOf("dowNow", "crudeNow", "giftNow")) canonical(incoming.opt(k))?.let { out.put(k, it.toDouble()) }
        return out
    }

    /** Explicitly dated global direction: today's morning record, plus today's live quotes when present. */
    fun globalDirection(state: JSONObject, session: String, live: JSONObject?): JSONObject? {
        val m = state.optJSONObject("morning")?.takeIf { it.optString("session") == session } ?: return null
        val gd = JSONObject()
            .put("dowClose", m.optString("dowClose").toDouble())
            .put("crudeSettle", m.optString("crudeSettle").toDouble())
            .put("session", session).put("_date", session).put("source", "morning_lock")
            .put("morning_revision", m.optString("revision"))
            .put("morning_captured_at_ms", m.optLong("captured_at_ms"))
        if (live != null && live.optString("session") == session) {
            for (k in listOf("dowNow", "crudeNow", "giftNow")) if (live.has(k)) gd.put(k, live.getDouble(k))
            gd.put("live_quotes_captured_at_ms", live.optLong("captured_at_ms"))
        }
        return gd
    }

    /**
     * Condition 4: authoritative acknowledgement from a durable write. [storedRaw] is the pref value read back
     * AFTER commit(); success only when commit() returned true and the stored record carries this revision.
     */
    fun acknowledge(committed: Boolean, kind: String, rec: JSONObject, storedRaw: String?, session: String, nowMs: Long): JSONObject {
        val stored = read(storedRaw)
        val back = stored.optJSONObject(kind)
        if (!committed || back == null || back.optString("revision") != rec.optString("revision")) {
            return JSONObject().put("ok", false).put("error", "SAVE_NOT_PERSISTED: $kind was not saved; please retry")
                .put("kind", kind)
        }
        return JSONObject().put("ok", true).put("kind", kind).put("record", back)
            .put("revision", back.optString("revision")).put("state", publicState(stored, session, nowMs))
    }

    /** Public, display-safe view for the PWA (getOvernightInputState). */
    fun publicState(state: JSONObject, session: String, nowMs: Long): JSONObject {
        val m = state.optJSONObject("morning"); val e = state.optJSONObject("evening")
        return JSONObject()
            .put("schema", SCHEMA).put("session", session)
            .put("status", status(state, session, nowMs))
            .put("input_revision", inputRevision(state, session))
            .put("pair_revision", if (m != null && e != null) pairRevision(session, m.optString("revision"), e.optString("revision")) else JSONObject.NULL)
            .put("morning", m?.let { JSONObject(it.toString()) } ?: JSONObject.NULL)
            .put("evening", e?.let { JSONObject(it.toString()) } ?: JSONObject.NULL)
    }
}
