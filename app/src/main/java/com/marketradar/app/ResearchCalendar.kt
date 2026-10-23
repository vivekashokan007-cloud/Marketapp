package com.marketradar.app

import java.time.DayOfWeek
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId

/**
 * M0 full-day research collector: versioned, dated collection calendar (Batch 1: W2 REG-01 + W3 CAL-03/04).
 *
 * Collection-only. Nothing in trading, gates, evening evaluation or EV v1.2 reads this object; the existing
 * MarketWatchService poll schedule and holiday list are unchanged (parity tests pin the shared facts).
 *
 * Regimes (CL-A2 §2.1; dated, every plan carries its regime id):
 *   R0  .. 2026-07-31       index derivatives (expiring and non-expiring) to 15:30
 *   R1  2026-08-03..12-04   index derivatives incl. expiring to 15:40 (NSE/CMTR/75479); grid 09:15..15:40 (78)
 *   R2  2026-12-07..        non-expiring to 15:45, EXPIRING to 15:30 (SEBI 15 Oct 2026 ¶4.2.4/¶4.9.1/¶6.1);
 *                           grid 09:15..15:45 (79). Slots after a contract's stop are POST_STOP observations.
 *   R2b 2027-02-08..        ¶4.8 carry-forward (¶6.2); end times NOT_STATED in the authority we hold -> UNKNOWN.
 * Phase labels are PER CONTRACT CLASS (non-expiring vs expiring) and per slot; raw post-stop observations are never
 * tradable-contract evidence. Unknown authority is an explicit UNKNOWN, never a default schedule.
 *
 * Special sessions: Muhurat Sun 8 Nov 2026 is encoded field-by-field from NSE/FAOP/76940 (document date 19 Oct 2026,
 * day precision). Its capture grid derives ONLY from the 14:20-15:40 continuous market. It is not a regular session.
 * Any other special session is PENDING_CIRCULAR (no slots). The app poll driver does not run on Sundays, so its
 * slots reconcile to MISSING(special_session_not_polled) (documented dependency, denominator unchanged).
 */
object ResearchCalendar {
    const val VERSION = "research_m0_cal_v2:nse_fo_2026:regimes_R0_R1_R2_R2b:muhurat_20261108_faop76940:nf_tue_prev_session:bnf_live_chain_only"
    val IST: ZoneId = ZoneId.of("Asia/Kolkata")
    const val OPEN_MINUTE = 9 * 60 + 15
    const val SLOT_MINUTES = 5
    /** The live app poll driver (MarketWatchService) stops at 15:40 and is NOT changed by M0 (trading/eval
     *  scheduling unchanged). Slots after this minute are expected but cannot be collected by the live driver. */
    const val APP_POLL_DRIVER_LAST_MINUTE = 15 * 60 + 40
    const val NOT_STATED = "NOT_STATED"
    val INDICES = listOf("NF", "BNF")
    val COVERED_YEARS = setOf(2026)
    val HOLIDAYS_2026 = setOf(
        "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14", "2026-05-01",
        "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24",
        "2026-12-25"
    )

    // ------------------------------------------------------------------ regimes
    data class Regime(
        val id: String, val from: LocalDate?, val to: LocalDate?,
        /** null = NOT_STATED by the authority we hold. */
        val nonExpiringEndMinute: Int?, val expiringEndMinute: Int?,
        val source: String
    ) {
        val gridEndMinute: Int? get() = if (nonExpiringEndMinute == null || expiringEndMinute == null) null
            else maxOf(nonExpiringEndMinute, expiringEndMinute)
    }

    val REGIMES = listOf(
        Regime("R0", null, LocalDate.of(2026, 7, 31), 15 * 60 + 30, 15 * 60 + 30, "CL-A2 2.1 [DB] last poll 15:30"),
        Regime("R1", LocalDate.of(2026, 8, 3), LocalDate.of(2026, 12, 4), 15 * 60 + 40, 15 * 60 + 40, "NSE/CMTR/75479"),
        Regime("R2", LocalDate.of(2026, 12, 7), LocalDate.of(2027, 2, 7), 15 * 60 + 45, 15 * 60 + 30,
            "SEBI 15 Oct 2026 para 4.2.4, 4.9.1, 6.1"),
        Regime("R2b", LocalDate.of(2027, 2, 8), null, null, null, "SEBI 15 Oct 2026 para 4.8, 6.2 (end times NOT_STATED)")
    )

    /** Regime for [d], or null when no dated regime covers it (UNKNOWN; e.g. a gap between regimes). */
    fun regimeOf(d: LocalDate): Regime? = REGIMES.firstOrNull { r ->
        (r.from == null || !d.isBefore(r.from)) && (r.to == null || !d.isAfter(r.to))
    }

    // ------------------------------------------------------------------ special sessions (W3)
    data class SourcedField(val value: String, val circular: String, val documentDate: String) {
        fun json() = org.json.JSONObject().put("value", value).put("circular", circular).put("document_date", documentDate)
    }
    data class Phase(val name: String, val startMinute: Int, val endMinute: Int, val detail: String)
    data class SpecialSession(
        val date: LocalDate, val type: String, val status: String,            // SOURCED | PENDING_CIRCULAR
        val circulars: Map<String, String>,                                   // id -> document date (day) or NOT_STATED
        val phases: List<Phase>, val continuousStartMinute: Int?, val continuousEndMinute: Int?,
        val fields: Map<String, SourcedField>,
        /** When this encoding became available in code (causal provenance), distinct from the document date. */
        val encodedAvailableFrom: String
    )

    private const val F76940 = "NSE/FAOP/76940"
    private const val D76940 = "2026-10-19"
    val MUHURAT_2026 = SpecialSession(
        date = LocalDate.of(2026, 11, 8), type = "MUHURAT", status = "SOURCED",
        circulars = linkedMapOf("NSE/FAOP/71777" to NOT_STATED, "NSE/CMTR/76941" to NOT_STATED, F76940 to D76940),
        phases = listOf(
            Phase("PRE_OPEN_ENTRY_1", 14 * 60 + 5, 14 * 60 + 10, "limit and market order entry/modification/cancellation"),
            Phase("PRE_OPEN_ENTRY_2", 14 * 60 + 10, 14 * 60 + 15,
                "limit-only; market order modification/cancellation prohibited; random closure in final two minutes"),
            Phase("PRE_OPEN_MATCHING", 14 * 60 + 15, 14 * 60 + 17, "order matching"),
            Phase("PRE_OPEN_BUFFER", 14 * 60 + 17, 14 * 60 + 20, "buffer"),
            Phase("CONTINUOUS_MARKET", 14 * 60 + 20, 15 * 60 + 40, "normal market; source of the capture grid"),
            Phase("TRADE_MODIFICATION", 14 * 60 + 20, 15 * 60 + 50, "trade modification window; NOT additional tradability")
        ),
        continuousStartMinute = 14 * 60 + 20, continuousEndMinute = 15 * 60 + 40,
        fields = linkedMapOf(
            "price_band_update" to SourcedField("15:15", F76940, D76940),
            "settlement_obligation" to SourcedField("ALL_TRADES_RESULT_IN_SETTLEMENT_OBLIGATIONS", F76940, D76940),
            "published_instant" to SourcedField(NOT_STATED, F76940, D76940),
            "closing_session" to SourcedField(NOT_STATED, F76940, D76940),
            "expiry_change" to SourcedField(NOT_STATED, F76940, D76940),
            "expiring_contract_end" to SourcedField(NOT_STATED, F76940, D76940)
        ),
        encodedAvailableFrom = "2026-10-23"
    )
    val SPECIAL_SESSIONS: List<SpecialSession> = listOf(MUHURAT_2026)
    /** Special sessions known to exist but whose timetable we do not hold: no slots, PENDING_CIRCULAR. */
    val PENDING_SPECIAL_SESSIONS: MutableSet<String> = linkedSetOf()

    fun specialSession(d: LocalDate): SpecialSession? = SPECIAL_SESSIONS.firstOrNull { it.date == d }

    // ------------------------------------------------------------------ sessions / expiries
    fun covered(d: LocalDate) = d.year in COVERED_YEARS

    /** REGULAR session? null when the year has no declared authority. Special sessions are NOT regular. */
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

    /** Regular sessions strictly after [a] up to and including [b]; null when any date in range is uncovered. */
    fun regularSessionsAfter(a: LocalDate, b: LocalDate): Int? {
        var n = 0; var x = a.plusDays(1)
        while (!x.isAfter(b)) { val s = isSession(x) ?: return null; if (s) n++; x = x.plusDays(1) }
        return n
    }

    /** Tags for a holding interval [entry, exit]: SPAN_SPECIAL_SESSION if a special session lies inside it, and
     *  SPAN_<a>_<b> when it crosses a regime boundary. */
    fun spanTags(entry: LocalDate, exit: LocalDate): List<String> {
        val out = ArrayList<String>()
        if (SPECIAL_SESSIONS.any { !it.date.isBefore(entry) && !it.date.isAfter(exit) } ||
            PENDING_SPECIAL_SESSIONS.any { val d = LocalDate.parse(it); !d.isBefore(entry) && !d.isAfter(exit) }) out.add("SPAN_SPECIAL_SESSION")
        val ra = regimeOf(entry)?.id ?: "UNKNOWN"; val rb = regimeOf(exit)?.id ?: "UNKNOWN"
        if (ra != rb) out.add("SPAN_${ra}_$rb")
        return out
    }

    // ------------------------------------------------------------------ slots
    data class Slot(
        val ordinal: Int, val minuteOfDay: Int, val nominalMs: Long,
        val phaseNonExpiring: String, val phaseExpiring: String, val kind: String   // REGULAR | SPECIAL
    ) {
        val hhmm: String get() = "%02d:%02d".format(minuteOfDay / 60, minuteOfDay % 60)
        /** Back-compat summary label: the non-expiring contract phase. */
        val phase: String get() = phaseNonExpiring
        fun phaseFor(expiring: Boolean?): String = when (expiring) { true -> phaseExpiring; false -> phaseNonExpiring; null -> "UNKNOWN_CONTRACT_CLASS" }
    }

    fun contractPhase(minute: Int, endMinute: Int?): String = when {
        endMinute == null -> "UNKNOWN"
        minute < endMinute -> "CONTINUOUS_TRADING"
        minute == endMinute -> "CLOSE_OBSERVATION"
        else -> "POST_STOP_OBSERVATION"
    }

    private fun ms(d: LocalDate, m: Int) = LocalDateTime.of(d.year, d.month, d.dayOfMonth, m / 60, m % 60).atZone(IST).toInstant().toEpochMilli()

    /** Regular-session grid for the regime of [sessionDate]; empty when the regime/end times are unknown. */
    fun slotGrid(sessionDate: LocalDate): List<Slot> {
        specialSession(sessionDate)?.let { return specialGrid(it) }
        val r = regimeOf(sessionDate) ?: return emptyList()
        val end = r.gridEndMinute ?: return emptyList()
        val out = ArrayList<Slot>()
        var m = OPEN_MINUTE; var ord = 1
        while (m <= end) {
            out.add(Slot(ord, m, ms(sessionDate, m), contractPhase(m, r.nonExpiringEndMinute), contractPhase(m, r.expiringEndMinute), "REGULAR"))
            m += SLOT_MINUTES; ord++
        }
        return out
    }

    /** Special-session grid: derived ONLY from the sourced continuous market window. */
    fun specialGrid(s: SpecialSession): List<Slot> {
        val a = s.continuousStartMinute ?: return emptyList(); val b = s.continuousEndMinute ?: return emptyList()
        if (s.status != "SOURCED") return emptyList()
        val expEnd = s.fields["expiring_contract_end"]?.value?.takeIf { it != NOT_STATED }
        val out = ArrayList<Slot>(); var m = a; var ord = 1
        while (m <= b) {
            out.add(Slot(ord, m, ms(s.date, m), contractPhase(m, b), if (expEnd == null) "UNKNOWN" else contractPhase(m, b), "SPECIAL"))
            m += SLOT_MINUTES; ord++
        }
        return out
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
        val regime: String = "UNKNOWN",
        val sessionKind: String = "NONE",            // REGULAR | SPECIAL | NONE
        val specialType: String? = null,
        val version: String = VERSION
    )

    fun plan(sessionDate: String, index: String): SessionPlan {
        val d = try { LocalDate.parse(sessionDate) } catch (e: Exception) {
            return SessionPlan(sessionDate, index, false, "BAD_DATE", false, "NONE", null, null, emptyList())
        }
        if (index !in INDICES) return SessionPlan(sessionDate, index, false, "UNKNOWN_INDEX", false, "NONE", null, null, emptyList())
        val s = isSession(d) ?: return SessionPlan(sessionDate, index, false, "CALENDAR_YEAR_NOT_COVERED", false, "NONE", null, null, emptyList())
        val regime = regimeOf(d)
        val rid = regime?.id ?: "UNKNOWN"
        if (sessionDate in PENDING_SPECIAL_SESSIONS) {
            return SessionPlan(sessionDate, index, false, "PENDING_CIRCULAR", false, "NONE", null, null, emptyList(), rid, "SPECIAL")
        }
        specialSession(d)?.let { sp ->
            val grid = specialGrid(sp)
            if (grid.isEmpty()) return SessionPlan(sessionDate, index, false, "PENDING_CIRCULAR", false, "NONE", null, null, emptyList(), rid, "SPECIAL", sp.type)
            val (auth, e) = if (index == "NF") "NF_WEEKLY_TUE_PREV_SESSION_RULE" to nfRuleExpiry(d) else "LIVE_CHAIN_ONLY" to null
            return SessionPlan(sessionDate, index, true, null, true, auth, e?.toString(), if (e == null) null else false, grid, rid, "SPECIAL", sp.type)
        }
        if (!s) return SessionPlan(sessionDate, index, true, "NOT_A_SESSION", false, "NONE", null, null, emptyList(), rid)
        if (regime == null) return SessionPlan(sessionDate, index, false, "REGIME_UNKNOWN", true, "NONE", null, null, emptyList(), rid, "REGULAR")
        if (regime.gridEndMinute == null) return SessionPlan(sessionDate, index, false, "REGIME_PARAMETERS_NOT_STATED", true, "NONE", null, null, emptyList(), rid, "REGULAR")
        return if (index == "NF") {
            val e = nfRuleExpiry(d)
            SessionPlan(sessionDate, index, true, null, true, "NF_WEEKLY_TUE_PREV_SESSION_RULE", e?.toString(), e == d, slotGrid(d), rid, "REGULAR")
        } else {
            SessionPlan(sessionDate, index, true, null, true, "LIVE_CHAIN_ONLY", null, null, slotGrid(d), rid, "REGULAR")
        }
    }

    /** Collection dates in [from, to]: regular sessions and special sessions (null element = uncovered year). */
    fun sessionsBetween(from: LocalDate, to: LocalDate): List<Pair<LocalDate, Boolean?>> {
        val out = ArrayList<Pair<LocalDate, Boolean?>>()
        var x = from
        var guard = 0
        while (!x.isAfter(to) && guard < 800) {
            val s = isSession(x)
            if (s == null) out.add(x to null) else if (s || specialSession(x) != null) out.add(x to true)
            x = x.plusDays(1); guard++
        }
        return out
    }

    /** Why the live poll driver cannot collect a planned slot, or null when it can. Documented dependency. */
    fun liveDriverGap(sessionDate: LocalDate, slot: Slot): String? = when {
        slot.kind == "SPECIAL" -> "special_session_not_polled"
        sessionDate.dayOfWeek == DayOfWeek.SATURDAY || sessionDate.dayOfWeek == DayOfWeek.SUNDAY -> "special_session_not_polled"
        slot.minuteOfDay > APP_POLL_DRIVER_LAST_MINUTE -> "poll_driver_unavailable_after_1540"
        else -> null
    }

    // ------------------------------------------------------------------ freeze deadline (Codex §3)
    sealed class Horizon {
        /** Exit k regular sessions after entry. */
        data class Sessions(val index: String, val k: Int) : Horizon()
        /** Hold the front contract to its expiry; [expiries] are the dated expiries (NF from rule if null). */
        data class ToExpiry(val index: String, val expiries: List<LocalDate>?) : Horizon()
    }

    data class FreezeDeadline(val earliestAffectedEntry: LocalDate?, val status: String, val basis: List<String>)

    /**
     * Earliest prospective entry session whose holding interval can contain the special session [sp], over the
     * ENABLED horizons. The special-session treatment must be frozen before that entry. UNKNOWN when any horizon
     * lacks dated expiry authority (e.g. BNF without a supplied expiry list) - never "the week before".
     */
    fun freezeDeadline(sp: SpecialSession, horizons: List<Horizon>): FreezeDeadline {
        var best: LocalDate? = null; val basis = ArrayList<String>(); var unknown = false
        for (h in horizons) {
            val e: LocalDate? = when (h) {
                is Horizon.Sessions -> {
                    // entry e, exit = k-th regular session after e; affected iff e < sp.date <= exit
                    var x = sp.date.minusDays(1); var cand: LocalDate? = null
                    var guard = 0
                    while (guard++ < 60) {
                        val s = isSession(x) ?: break
                        if (s) { val n = regularSessionsAfter(x, sp.date) ?: break; if (n < h.k) cand = x else break }
                        x = x.minusDays(1)
                    }
                    cand
                }
                is Horizon.ToExpiry -> {
                    val exps = h.expiries ?: if (h.index == "NF") nfExpiriesAround(sp.date) else null
                    if (exps == null) { unknown = true; basis.add("${h.index}:to_expiry:EXPIRY_AUTHORITY_UNKNOWN"); null }
                    else {
                        val held = exps.filter { it.isAfter(sp.date) }.minOrNull()
                        val prev = exps.filter { !it.isAfter(sp.date) }.maxOrNull()
                        if (held == null || prev == null) { unknown = true; basis.add("${h.index}:to_expiry:EXPIRY_LIST_INCOMPLETE"); null }
                        else { var x = prev.plusDays(1); while (isSession(x) == false) x = x.plusDays(1); x }
                    }
                }
            }
            if (e != null) { basis.add("$h -> $e"); if (best == null || e.isBefore(best)) best = e }
        }
        return FreezeDeadline(best, if (unknown) "UNKNOWN" else "DERIVED", basis)
    }

    private fun nfExpiriesAround(d: LocalDate): List<LocalDate>? {
        val out = ArrayList<LocalDate>(); var x = d.minusDays(21)
        while (!x.isAfter(d.plusDays(21))) { val e = nfRuleExpiry(x) ?: return null; if (e !in out) out.add(e); x = x.plusDays(1) }
        return out
    }

    /** PROSPECTIVE only if the rule was frozen strictly before the cohort's entry; otherwise RULE_POST_HOC. */
    fun cohortRuleStatus(entry: LocalDate, ruleFrozenOn: LocalDate?): String =
        if (ruleFrozenOn != null && ruleFrozenOn.isBefore(entry)) "PROSPECTIVE" else "RULE_POST_HOC"
}
