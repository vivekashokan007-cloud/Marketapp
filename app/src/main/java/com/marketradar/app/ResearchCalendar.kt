package com.marketradar.app

import java.time.DayOfWeek
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId

/**
 * M0 full-day research collector: versioned, dated collection calendar (Codex M0 brief B2/B6).
 *
 * Collection-only. Nothing in trading, gates, evening evaluation or EV v1.2 reads this object; the existing
 * MarketWatchService poll schedule and holiday list are unchanged (a parity test pins this list to both
 * MarketWatchService and Python ev_v12_calendar so they cannot drift silently).
 *
 * - Sessions: weekdays not in the 2026 NSE F&O holiday list. Any year without declared authority (2027+)
 *   is CALENDAR_YEAR_NOT_COVERED: the session plan is unavailable and coverage is reported UNKNOWN, never
 *   a zero denominator and never an assumed schedule (fail closed, existing F1 policy).
 * - Special sessions (e.g. Muhurat): none are declared for 2026 in this version; a date listed in
 *   [UNDECLARED_SPECIAL_SESSION_DATES] is reported CALENDAR_SPECIAL_SESSION_UNDECLARED rather than guessed.
 * - Slots: the app's existing 5-minute poll grid (09:15 .. 15:40 IST). The denominator is the number of slots
 *   in the plan for that session/index, derived from the schedule parameters, not a hardcoded constant.
 * - Phases: TRADING (< 15:30) and POST_CLOSE_MARKS (>= 15:30: quotes may be observed, nothing is tradable).
 * - Expiry authority: NF weekly = Tuesday, previous session when Tuesday is a holiday (same rule as
 *   ev_v12_calendar). BNF = LIVE_CHAIN_ONLY: the collector records the live chain expiry and never derives a
 *   BNF date from the NF rule (no cross-mapping). Unknown index -> UNKNOWN_INDEX (unavailable evidence).
 */
object ResearchCalendar {
    const val VERSION = "research_m0_cal_v1:nse_fo_2026:grid_0915_1540_5m:nf_tue_prev_session:bnf_live_chain_only"
    val IST: ZoneId = ZoneId.of("Asia/Kolkata")
    const val OPEN_MINUTE = 9 * 60 + 15
    const val LAST_SLOT_MINUTE = 15 * 60 + 40
    const val SLOT_MINUTES = 5
    const val TRADING_END_MINUTE = 15 * 60 + 30
    val INDICES = listOf("NF", "BNF")
    val COVERED_YEARS = setOf(2026)
    val HOLIDAYS_2026 = setOf(
        "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14", "2026-05-01",
        "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24",
        "2026-12-25"
    )
    val UNDECLARED_SPECIAL_SESSION_DATES: Set<String> = emptySet()

    data class Slot(val ordinal: Int, val minuteOfDay: Int, val phase: String, val nominalMs: Long) {
        val hhmm: String get() = "%02d:%02d".format(minuteOfDay / 60, minuteOfDay % 60)
    }

    data class SessionPlan(
        val sessionDate: String,
        val index: String,
        val available: Boolean,
        val reason: String?,
        val isSession: Boolean,
        val expiryAuthority: String,
        val ruleExpiry: String?,
        val ruleExpiryDay: Boolean?,
        val slots: List<Slot>,
        val version: String = VERSION
    )

    fun covered(d: LocalDate) = d.year in COVERED_YEARS

    /** null when the year has no declared authority. */
    fun isSession(d: LocalDate): Boolean? {
        if (!covered(d)) return null
        return d.dayOfWeek != DayOfWeek.SATURDAY && d.dayOfWeek != DayOfWeek.SUNDAY && d.toString() !in HOLIDAYS_2026
    }

    fun nfRuleExpiry(d: LocalDate): LocalDate? {
        var tue = d.plusDays(((DayOfWeek.TUESDAY.value - d.dayOfWeek.value + 7) % 7).toLong())
        var exp = prevOrSameSession(tue) ?: return null
        if (exp.isBefore(d)) {
            tue = tue.plusDays(7)
            exp = prevOrSameSession(tue) ?: return null
        }
        return exp
    }

    private fun prevOrSameSession(d: LocalDate): LocalDate? {
        var x = d
        repeat(10) {
            val s = isSession(x) ?: return null
            if (s) return x
            x = x.minusDays(1)
        }
        return null
    }

    fun slotGrid(sessionDate: LocalDate): List<Slot> {
        val out = ArrayList<Slot>()
        var m = OPEN_MINUTE
        var ord = 1
        while (m <= LAST_SLOT_MINUTE) {
            val ms = LocalDateTime.of(sessionDate.year, sessionDate.month, sessionDate.dayOfMonth, m / 60, m % 60)
                .atZone(IST).toInstant().toEpochMilli()
            out.add(Slot(ord, m, if (m < TRADING_END_MINUTE) "TRADING" else "POST_CLOSE_MARKS", ms))
            m += SLOT_MINUTES; ord++
        }
        return out
    }

    fun plan(sessionDate: String, index: String): SessionPlan {
        val d = try { LocalDate.parse(sessionDate) } catch (e: Exception) {
            return SessionPlan(sessionDate, index, false, "BAD_DATE", false, "NONE", null, null, emptyList())
        }
        if (index !in INDICES) return SessionPlan(sessionDate, index, false, "UNKNOWN_INDEX", false, "NONE", null, null, emptyList())
        val s = isSession(d) ?: return SessionPlan(sessionDate, index, false, "CALENDAR_YEAR_NOT_COVERED", false, "NONE", null, null, emptyList())
        if (sessionDate in UNDECLARED_SPECIAL_SESSION_DATES) {
            return SessionPlan(sessionDate, index, false, "CALENDAR_SPECIAL_SESSION_UNDECLARED", false, "NONE", null, null, emptyList())
        }
        if (!s) return SessionPlan(sessionDate, index, true, "NOT_A_SESSION", false, "NONE", null, null, emptyList())
        return if (index == "NF") {
            val e = nfRuleExpiry(d)
            SessionPlan(sessionDate, index, true, null, true, "NF_WEEKLY_TUE_PREV_SESSION_RULE", e?.toString(), e == d, slotGrid(d))
        } else {
            SessionPlan(sessionDate, index, true, null, true, "LIVE_CHAIN_ONLY", null, null, slotGrid(d))
        }
    }

    /** Sessions in [from, to] inclusive; a null element marks a date whose year is not covered (unknown). */
    fun sessionsBetween(from: LocalDate, to: LocalDate): List<Pair<LocalDate, Boolean?>> {
        val out = ArrayList<Pair<LocalDate, Boolean?>>()
        var x = from
        var guard = 0
        while (!x.isAfter(to) && guard < 800) {
            val s = isSession(x)
            if (s != false) out.add(x to s)
            x = x.plusDays(1); guard++
        }
        return out
    }
}
