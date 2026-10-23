package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.time.LocalDate
import java.time.LocalDateTime

/**
 * M0 Batch 1 - W3 (CAL-03/04 Muhurat special session) + W2 (REG-01 dated regimes, per-contract phases).
 * Each test drives the real ResearchCalendar and, where wiring matters, the real ResearchCollector
 * (ensureManifest / recordCapture / reconcile), not a helper copy.
 */
class M0Batch1CalendarTest {
    private lateinit var dir: File
    private fun d(s: String) = LocalDate.parse(s)
    private fun ms(day: String, h: Int, m: Int) = LocalDateTime.parse("${day}T%02d:%02d:00".format(h, m)).atZone(ResearchCalendar.IST).toInstant().toEpochMilli()

    @Before fun setUp() { dir = Files.createTempDirectory("m0b1cal").toFile() }
    @After fun tearDown() { dir.deleteRecursively(); ResearchCalendar.PENDING_SPECIAL_SESSIONS.clear() }

    private fun chain(index: String, expiry: String) = JSONObject().put("data", JSONArray().apply {
        for (k in listOf(100, 200)) put(JSONObject().put("strike_price", k).put("expiry", expiry)
            .put("call_options", JSONObject().put("instrument_key", "NSE_FO|$index${k}CE").put("market_data", JSONObject().put("bid_price", 1.0).put("ask_price", 1.1)))
            .put("put_options", JSONObject().put("instrument_key", "NSE_FO|$index${k}PE").put("market_data", JSONObject().put("bid_price", 1.0).put("ask_price", 1.1))))
    })

    // ------------------------------------------------------------------ W2 regimes
    @Test fun r2_december_schedule_contains_the_1545_observation() {
        val p = ResearchCalendar.plan("2026-12-08", "NF")
        assertTrue(p.available); assertEquals("R2", p.regime)
        assertEquals(79, p.slots.size)
        val last = p.slots.last()
        assertEquals("15:45", last.hhmm)
        assertEquals("CLOSE_OBSERVATION", last.phaseNonExpiring)
        assertEquals("POST_STOP_OBSERVATION", last.phaseExpiring)
        // the manifest (runtime wiring) carries the 15:45 slot and, since the live poll driver stops at 15:40,
        // reconcile shows it MISSING with the documented dependency - never dropped from the denominator.
        ResearchCollector.setEnabled(dir, true, ms("2026-12-08", 9, 0))
        ResearchCollector.reconcile(dir, ms("2026-12-08", 16, 30))
        val io = ResearchCollector.readManifest(dir, "2026-12-08")!!.getJSONObject("indices").getJSONObject("NF")
        assertEquals(79, io.getInt("expected")); assertEquals("R2", io.getString("regime"))
        val s79 = io.getJSONObject("slots").getJSONObject("79")
        assertEquals("15:45", s79.getString("nominal"))
        assertEquals("MISSING", s79.getString("state"))
        assertEquals("poll_driver_unavailable_after_1540", s79.getString("reason"))
        assertEquals("session_not_observed_by_collector", io.getJSONObject("slots").getJSONObject("78").getString("reason"))
    }

    @Test fun r1_schedule_and_boundary_4dec_close_7dec_open() {
        val r1 = ResearchCalendar.plan("2026-12-04", "NF")
        assertEquals("R1", r1.regime); assertEquals(78, r1.slots.size); assertEquals("15:40", r1.slots.last().hhmm)
        // R1: expiring index contracts also trade to 15:40
        val e = ResearchCalendar.plan("2026-10-27", "NF")
        assertTrue(e.ruleExpiryDay!!)
        val s1535 = e.slots.first { it.hhmm == "15:35" }
        assertEquals("CONTINUOUS_TRADING", s1535.phaseExpiring); assertEquals("CONTINUOUS_TRADING", s1535.phaseNonExpiring)
        assertEquals("CLOSE_OBSERVATION", e.slots.last().phaseExpiring)
        val r2 = ResearchCalendar.plan("2026-12-07", "NF")
        assertEquals("R2", r2.regime); assertEquals(79, r2.slots.size)
        assertEquals(listOf("SPAN_R1_R2"), ResearchCalendar.spanTags(d("2026-12-04"), d("2026-12-07")))
        // R0 history: 15:30 close for both classes
        val r0 = ResearchCalendar.plan("2026-07-15", "NF")
        assertEquals("R0", r0.regime); assertEquals("15:30", r0.slots.last().hhmm); assertEquals(76, r0.slots.size)
    }

    @Test fun expiring_and_non_expiring_differ_on_the_same_r2_date_through_recordCapture() {
        val day = "2026-12-08"   // NF weekly expiry (Tuesday)
        ResearchCollector.setEnabled(dir, true, ms(day, 9, 0))
        val ord = ResearchCalendar.plan(day, "NF").slots.first { it.hhmm == "15:35" }.ordinal
        fun rec(expiry: String): JSONObject {
            val b = ResearchCollector.beginAttempt(dir, "NF", "$day|$ord", ms(day, 15, 35)) as ResearchCollector.Begin.Started
            val c = ResearchCollector.captureFromChain(dir, "NF", chain("NF", expiry), ms(day, 15, 35), ms(day, 15, 35),
                "ml_option_chain_snapshots", ms(day, 15, 35), "2026-12-08T15:35:00+05:30", ms(day, 15, 35), b.attemptId)
            ResearchCollector.recordCapture(dir, b.slotId, b.attemptId, c, ms(day, 15, 36))
            val atts = ResearchCollector.readManifest(dir, day)!!.getJSONObject("indices").getJSONObject("NF").getJSONObject("slots")
                .getJSONObject(ord.toString()).getJSONArray("attempts")
            return atts.getJSONObject(atts.length() - 1).getJSONObject("capture")
        }
        val expiring = rec(day)
        assertEquals("EXPIRING", expiring.getString("contract_class"))
        assertEquals("POST_STOP_OBSERVATION", expiring.getString("contract_phase"))
        assertFalse(expiring.getBoolean("tradable_contract_evidence"))
        val next = rec("2026-12-15")
        assertEquals("NON_EXPIRING", next.getString("contract_class"))
        assertEquals("CONTINUOUS_TRADING", next.getString("contract_phase"))
        assertTrue(next.getBoolean("tradable_contract_evidence"))
    }

    @Test fun holiday_adjusted_expiry_and_unknown_authority_is_explicit() {
        assertEquals(d("2026-11-09"), ResearchCalendar.nfRuleExpiry(d("2026-11-04")))   // Tue 10 Nov holiday
        assertEquals(d("2026-11-23"), ResearchCalendar.nfRuleExpiry(d("2026-11-18")))   // Tue 24 Nov holiday
        assertTrue(ResearchCalendar.plan("2026-11-09", "NF").ruleExpiryDay!!)
        assertEquals("LIVE_CHAIN_ONLY", ResearchCalendar.plan("2026-11-09", "BNF").expiryAuthority)
        // R2b: end times NOT_STATED -> UNKNOWN phase, no grid, never a default schedule
        val r2b = ResearchCalendar.regimeOf(d("2027-02-08"))!!
        assertEquals("R2b", r2b.id); assertNull(r2b.gridEndMinute)
        assertEquals("UNKNOWN", ResearchCalendar.contractPhase(15 * 60, r2b.expiringEndMinute))
        assertTrue(ResearchCalendar.slotGrid(d("2027-02-09")).isEmpty())
        val p = ResearchCalendar.plan("2027-02-09", "NF")
        assertFalse(p.available); assertEquals("CALENDAR_YEAR_NOT_COVERED", p.reason); assertTrue(p.slots.isEmpty())
        // a capture whose contract class cannot be determined is labelled, not defaulted
        assertEquals("UNKNOWN_CONTRACT_CLASS", ResearchCalendar.plan("2026-12-08", "NF").slots.last().phaseFor(null))
    }

    @Test fun denominators_never_shrink_across_reconcile_and_late_capture() {
        val day = "2026-10-21"
        ResearchCollector.setEnabled(dir, true, ms(day, 9, 0))
        ResearchCollector.reconcile(dir, ms(day, 23, 0))
        val io = ResearchCollector.readManifest(dir, day)!!.getJSONObject("indices").getJSONObject("NF")
        assertEquals(78, io.getInt("expected")); assertEquals(78, io.getJSONObject("slots").length())
        ResearchCollector.reconcile(dir, ms(day, 23, 30))
        assertEquals(78, ResearchCollector.readManifest(dir, day)!!.getJSONObject("indices").getJSONObject("NF").getJSONObject("slots").length())
    }

    // ------------------------------------------------------------------ W3 Muhurat
    @Test fun muhurat_is_not_a_regular_session_and_spanning_cohorts_are_tagged() {
        val m = d("2026-11-08")
        assertEquals(false, ResearchCalendar.isSession(m))
        assertEquals(1, ResearchCalendar.regularSessionsAfter(d("2026-11-06"), d("2026-11-09")))
        assertTrue("SPAN_SPECIAL_SESSION" in ResearchCalendar.spanTags(d("2026-11-06"), d("2026-11-09")))
        assertFalse("SPAN_SPECIAL_SESSION" in ResearchCalendar.spanTags(d("2026-11-09"), d("2026-11-11")))
    }

    @Test fun muhurat_slots_derive_from_the_1420_1540_normal_market_only() {
        val p = ResearchCalendar.plan("2026-11-08", "NF")
        assertTrue(p.available); assertEquals("SPECIAL", p.sessionKind); assertEquals("MUHURAT", p.specialType)
        assertEquals(17, p.slots.size)
        assertEquals("14:20", p.slots.first().hhmm); assertEquals("15:40", p.slots.last().hhmm)
        assertTrue(p.slots.all { it.kind == "SPECIAL" && it.minuteOfDay in (14 * 60 + 20)..(15 * 60 + 40) })
        assertTrue(p.slots.none { it.hhmm == "09:15" })
        assertEquals("CLOSE_OBSERVATION", p.slots.last().phaseNonExpiring)
        // expiring-contract end is NOT_STATED for the special session -> UNKNOWN, not inferred
        assertTrue(p.slots.all { it.phaseExpiring == "UNKNOWN" })
        // trade-modification window (to 15:50) adds NO tradable slot
        assertTrue(p.slots.none { it.minuteOfDay > 15 * 60 + 40 })
    }

    @Test fun muhurat_record_is_source_faithful_with_split_pre_open() {
        val s = ResearchCalendar.MUHURAT_2026
        assertEquals("2026-10-19", s.circulars["NSE/FAOP/76940"])
        assertEquals(setOf("NSE/FAOP/71777", "NSE/CMTR/76941", "NSE/FAOP/76940"), s.circulars.keys)
        val ph = s.phases.associateBy { it.name }
        assertEquals(14 * 60 + 5 to 14 * 60 + 10, ph["PRE_OPEN_ENTRY_1"]!!.let { it.startMinute to it.endMinute })
        assertEquals(14 * 60 + 10 to 14 * 60 + 15, ph["PRE_OPEN_ENTRY_2"]!!.let { it.startMinute to it.endMinute })
        assertTrue(ph["PRE_OPEN_ENTRY_2"]!!.detail.contains("random closure"))
        assertTrue(ph["PRE_OPEN_ENTRY_2"]!!.detail.contains("prohibited"))
        assertEquals(14 * 60 + 15 to 14 * 60 + 17, ph["PRE_OPEN_MATCHING"]!!.let { it.startMinute to it.endMinute })
        assertEquals(14 * 60 + 17 to 14 * 60 + 20, ph["PRE_OPEN_BUFFER"]!!.let { it.startMinute to it.endMinute })
        assertEquals(14 * 60 + 20 to 15 * 60 + 40, ph["CONTINUOUS_MARKET"]!!.let { it.startMinute to it.endMinute })
        assertEquals(14 * 60 + 20 to 15 * 60 + 50, ph["TRADE_MODIFICATION"]!!.let { it.startMinute to it.endMinute })
        assertTrue(ph["TRADE_MODIFICATION"]!!.detail.contains("NOT additional tradability"))
        assertEquals("15:15", s.fields["price_band_update"]!!.value)
        assertEquals("NSE/FAOP/76940", s.fields["price_band_update"]!!.circular)
        assertEquals("2026-10-19", s.fields["price_band_update"]!!.documentDate)   // day precision, no invented instant
        assertEquals(ResearchCalendar.NOT_STATED, s.fields["published_instant"]!!.value)
        assertTrue(s.fields.values.all { it.documentDate.matches(Regex("\\d{4}-\\d{2}-\\d{2}")) })
        assertNotEquals(s.encodedAvailableFrom, s.fields["price_band_update"]!!.documentDate)   // retrieval/app availability kept separate
    }

    @Test fun pending_or_unknown_special_session_yields_no_slots() {
        ResearchCalendar.PENDING_SPECIAL_SESSIONS.add("2026-12-20")
        val p = ResearchCalendar.plan("2026-12-20", "NF")
        assertFalse(p.available); assertEquals("PENDING_CIRCULAR", p.reason); assertTrue(p.slots.isEmpty())
        assertTrue("SPAN_SPECIAL_SESSION" in ResearchCalendar.spanTags(d("2026-12-18"), d("2026-12-21")))
        val pendingCopy = ResearchCalendar.MUHURAT_2026.copy(status = "PENDING_CIRCULAR")
        assertTrue(ResearchCalendar.specialGrid(pendingCopy).isEmpty())
    }

    @Test fun muhurat_not_polled_reconciles_to_missing_special_session_not_polled() {
        ResearchCollector.setEnabled(dir, true, ms("2026-11-06", 9, 0))
        ResearchCollector.reconcile(dir, ms("2026-11-09", 9, 0))
        val io = ResearchCollector.readManifest(dir, "2026-11-08")!!.getJSONObject("indices").getJSONObject("BNF")
        assertEquals("SPECIAL", io.getString("session_kind")); assertEquals(17, io.getInt("expected"))
        val slots = io.getJSONObject("slots")
        for (k in slots.keys()) {
            assertEquals("MISSING", slots.getJSONObject(k).getString("state"))
            assertEquals("special_session_not_polled", slots.getJSONObject(k).getString("reason"))
        }
        assertNull(ResearchCollector.readManifest(dir, "2026-11-07"))   // Saturday: no manifest
    }

    @Test fun freeze_deadline_is_derived_from_enabled_horizons_not_the_week_before() {
        val sp = ResearchCalendar.MUHURAT_2026

        assertEquals(d("2026-11-06"), ResearchCalendar.freezeDeadline(sp, listOf(ResearchCalendar.Horizon.Sessions("NF", 1))).earliestAffectedEntry)
        assertEquals(d("2026-11-05"), ResearchCalendar.freezeDeadline(sp, listOf(ResearchCalendar.Horizon.Sessions("NF", 2))).earliestAffectedEntry)
        assertEquals(d("2026-11-02"), ResearchCalendar.freezeDeadline(sp, listOf(ResearchCalendar.Horizon.Sessions("BNF", 5))).earliestAffectedEntry)
        assertEquals(d("2026-11-04"), ResearchCalendar.freezeDeadline(sp, listOf(ResearchCalendar.Horizon.ToExpiry("NF", null))).earliestAffectedEntry)
        // BNF monthly to-expiry with dated expiries (Oct 27, Nov 23 [24 Nov holiday]) -> earliest entry 28 Oct,
        // i.e. BEFORE the 30 Oct "week before" target.
        val bnf = listOf(d("2026-10-27"), d("2026-11-23"), d("2026-12-29"))
        val all = ResearchCalendar.freezeDeadline(sp, listOf(ResearchCalendar.Horizon.Sessions("NF", 1), ResearchCalendar.Horizon.Sessions("NF", 2), ResearchCalendar.Horizon.Sessions("BNF", 5),
            ResearchCalendar.Horizon.ToExpiry("NF", null), ResearchCalendar.Horizon.ToExpiry("BNF", bnf)))
        assertEquals("DERIVED", all.status); assertEquals(d("2026-10-28"), all.earliestAffectedEntry)
        // unknown BNF expiry authority -> UNKNOWN (never a guessed deadline)
        assertEquals("UNKNOWN", ResearchCalendar.freezeDeadline(sp, listOf(ResearchCalendar.Horizon.ToExpiry("BNF", null))).status)
        // a rule frozen on 30 Oct is post hoc for a 28 Oct entry; prospective only for later entries
        assertEquals("RULE_POST_HOC", ResearchCalendar.cohortRuleStatus(d("2026-10-28"), d("2026-10-30")))
        assertEquals("RULE_POST_HOC", ResearchCalendar.cohortRuleStatus(d("2026-10-30"), d("2026-10-30")))
        assertEquals("PROSPECTIVE", ResearchCalendar.cohortRuleStatus(d("2026-11-02"), d("2026-10-30")))
    }

    // ------------------------------------------------------------------ CAL-05 shared-fact parity
    @Test fun shared_fact_parity_with_python_calendar() {
        val code = """
import sys, json; sys.path.insert(0, 'src/main/python')
from datetime import date, timedelta
import ev_v12_calendar as c
cal = c.Calendar()
out = {}
x = date(2026,1,1)
while x <= date(2026,12,22):
    out[x.isoformat()] = [cal.is_session(x), cal.nf_front_expiry(x).isoformat()]
    x += timedelta(days=1)
print(json.dumps({'days': out, 'between': cal.sessions_between('2026-11-06','2026-11-09'), 'holidays': sorted(c.NSE_HOLIDAYS_2026)}))
"""
        val p = ProcessBuilder("python3", "-c", code).redirectErrorStream(true).start()
        val txt = p.inputStream.bufferedReader().readText(); assertEquals(txt, 0, p.waitFor())
        val o = JSONObject(txt.trim().lines().last())
        val days = o.getJSONObject("days")
        for (k in days.keys()) {
            val a = days.getJSONArray(k)
            assertEquals(k, a.getBoolean(0), ResearchCalendar.isSession(d(k)))
            assertEquals(k, a.getString(1), ResearchCalendar.nfRuleExpiry(d(k)).toString())
        }
        assertEquals(o.getInt("between"), ResearchCalendar.regularSessionsAfter(d("2026-11-06"), d("2026-11-09")))
        val h = o.getJSONArray("holidays"); assertEquals(ResearchCalendar.HOLIDAYS_2026, (0 until h.length()).map { h.getString(it) }.toSet())
    }
}
