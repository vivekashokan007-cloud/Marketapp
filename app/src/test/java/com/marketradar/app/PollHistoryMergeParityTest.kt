package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Executable parity matrix: [PollHistoryMerge]'s identity must agree with the real consumer,
 * `index_authority_v1.parse_ts` / `poll_time` / `valid_spot_series`.
 *
 * Every expectation below is the **measured** output of that consumer for the same input on
 * session 2026-11-04, not a reading of the code. The companion Python test
 * `tests/test_poll_identity_parity.py` asserts the same table against the live consumer, so if
 * `parse_ts` ever changes, that side fails and this table is known to be stale. Neither side is
 * allowed to be the sole definition of the contract.
 *
 * The v1 implementation failed the starred rows: it stripped offsets instead of applying them,
 * compared the ISO string's date prefix instead of the IST date, truncated fractional seconds,
 * rejected epochs and `hh:mm AM`, and accepted naive stamps the consumer rejects.
 *
 * Synthetic MOCK DATA.
 */
class PollHistoryMergeParityTest {

    private val session = "2026-11-04"

    private fun keyOf(field: String, value: Any?): String? =
        PollHistoryMerge.canonicalKey(JSONObject().put(field, value), session)

    // ---- absolute stamps -------------------------------------------------------------------

    @Test
    fun utcAndIstSpellingsOfOneInstantShareOneIdentity() {
        // * v1 returned "03:50:00" for the UTC form, so one observation was kept twice.
        // Oracle: both -> 2026-11-04T09:20:00+05:30
        assertEquals("09:20:00", keyOf("poll_ts", "2026-11-04T03:50:00Z"))
        assertEquals("09:20:00", keyOf("poll_ts", "2026-11-04T09:20:00+05:30"))
        assertEquals(
            keyOf("poll_ts", "2026-11-04T03:50:00Z"),
            keyOf("poll_ts", "2026-11-04T09:20:00+05:30")
        )
    }

    @Test
    fun offsetSpellingsAreAllApplied() {
        // Oracle: +05:30 -> 09:20, +0530 -> 09:20, +05 -> 09:50, +05:30:00 -> 09:20
        assertEquals("09:20:00", keyOf("poll_ts", "2026-11-04T09:20:00+0530"))
        assertEquals("09:50:00", keyOf("poll_ts", "2026-11-04T09:20:00+05"))
        assertEquals("09:20:00", keyOf("poll_ts", "2026-11-04T09:20:00+05:30:00"))
        assertEquals("09:20:00", keyOf("poll_ts", "2026-11-04T09:20+05:30"))
        assertEquals("a space separator is an ISO stamp in CPython 3.11",
            "09:20:00", keyOf("poll_ts", "2026-11-04 09:20:00+05:30"))
    }

    @Test
    fun naiveAbsoluteStampsAreRejectedExactlyAsTheConsumerRejectsThem() {
        // * v1 ACCEPTED this and could let it win a conflict over a row the consumer can use.
        // Oracle: '2026-11-04T09:20:00' -> None ("naive timestamps are never assumed to be IST")
        assertNull(keyOf("poll_ts", "2026-11-04T09:20:00"))
        // Oracle: lowercase 'z' -> None (the consumer replaces only uppercase 'Z')
        assertNull(keyOf("poll_ts", "2026-11-04T09:20:00z"))
        // Oracle: a bare date -> None
        assertNull(keyOf("poll_ts", "2026-11-04"))
        assertNull(keyOf("poll_ts", "2026-11-04T0920:00+05:30"))
    }

    @Test
    fun epochNumbersAreAccepted() {
        // * v1 rejected both unless another field happened to carry a clock time.
        // Oracle: 1793764200000 (ms) and 1793764200 (s) -> 2026-11-04T09:20:00+05:30
        assertEquals("09:20:00", keyOf("poll_ts", 1793764200000L))
        assertEquals("09:20:00", keyOf("poll_ts", 1793764200L))
        assertEquals("09:20:00", keyOf("poll_ts", 1793764200.0))
    }

    @Test
    fun numericEpochsRoundRatherThanTruncate() {
        // `datetime.fromtimestamp` ROUNDS the fraction (`us = round(frac * 1e6)`). Truncating
        // split one observation's identity: oracle gives .123000 for the ms epoch below, while
        // truncation gave .122999 — so a millisecond epoch and its own ISO spelling would never
        // deduplicate against each other.
        assertEquals("09:20:00.123000000", keyOf("poll_ts", 1793764200123L))
        assertEquals("09:20:00.123457000", keyOf("poll_ts", 1793764200.1234567))
        assertEquals("09:20:00.999000000", keyOf("poll_ts", 1793764200999L))
        // The exactly-representable case that hid the problem.
        assertEquals("09:20:00.125000000", keyOf("poll_ts", 1793764200125L))
        assertEquals(
            "a millisecond epoch and its ISO spelling are one identity",
            keyOf("poll_ts", "2026-11-04T09:20:00.123+05:30"),
            keyOf("poll_ts", 1793764200123L)
        )
    }

    @Test
    fun offsetSecondsAreAppliedNotDropped() {
        // Oracle: '+05:30:30' is 09:19:30 IST, not 09:20:00. Truncating the offset to +HH:MM
        // silently shifted the instant, and the old test used only '+05:30:00' so it could not see it.
        assertEquals("09:19:30", keyOf("poll_ts", "2026-11-04T09:20:00+05:30:30"))
        assertEquals("20:20:30", keyOf("poll_ts", "2026-11-04T09:20:00-05:30:30"))
        assertEquals("09:20:00", keyOf("poll_ts", "2026-11-04T09:20:00+05:30:00"))
    }

    @Test
    fun anySingleDateTimeSeparatorIsAccepted() {
        // CPython 3.11 fromisoformat takes whatever sits at index 10 as the separator.
        for (sep in listOf("T", " ", "X", "x", "_")) {
            assertEquals(sep, "09:20:00", keyOf("poll_ts", "2026-11-04${sep}09:20:00+05:30"))
        }
        // ...but the time still needs two-digit fields, and a missing separator fails.
        assertNull(keyOf("poll_ts", "2026-11-0409:20:00+05:30"))
        assertNull(keyOf("poll_ts", "2026-11-04T9:20:00+05:30"))
    }

    @Test
    fun strptimeAcceptsOneOrTwoDigitsPerComponent() {
        // Requiring two minute digits rejected rows the consumer keeps.
        assertEquals("09:05:00", keyOf("t", "9:5"))
        assertEquals("01:05:00", keyOf("t", "1:5"))
        assertEquals("00:00:00", keyOf("t", "0:0"))
        assertEquals("09:05:03", keyOf("t", "9:5:3"))
        assertEquals("09:05:03", keyOf("t", "09:5:3"))
        assertEquals("21:05:00", keyOf("t", "9:5 PM"))
    }

    @Test
    fun theAmPmSeparatorIsOneOrMoreSpacesNotZero() {
        // The literal space in `%I:%M %p` compiles to `\s+`, so `\s*` was too permissive.
        assertNull(keyOf("t", "09:20AM"))
        assertEquals("09:20:00", keyOf("t", "09:20  AM"))
        assertEquals("09:20:00", keyOf("t", "09:20\tAM"))
    }

    @Test
    fun nonPositiveAndNonNumericValuesAreRejected() {
        // Oracle: True, False, 0, -5 -> None
        assertNull(keyOf("poll_ts", true))
        assertNull(keyOf("poll_ts", false))
        assertNull(keyOf("poll_ts", 0))
        assertNull(keyOf("poll_ts", -5))
    }

    @Test
    fun fractionalSecondsDistinguishDifferentInstants() {
        // * v1 truncated to whole seconds, so two different instants deduplicated to one.
        // Oracle: '.125' preserved; '.123456789' truncated to '.123456' (datetime resolution)
        assertEquals("09:20:00.125000000", keyOf("poll_ts", "2026-11-04T09:20:00.125+05:30"))
        assertEquals("09:20:00.123456000", keyOf("poll_ts", "2026-11-04T09:20:00.123456789+05:30"))
        assertEquals("09:20:00", keyOf("poll_ts", "2026-11-04T09:20:00.000+05:30"))
    }

    // ---- the IST date boundary --------------------------------------------------------------

    @Test
    fun sessionMembershipFollowsTheIstDateNotTheStringPrefix() {
        // * This is the pair v1 got backwards in BOTH directions.
        // Oracle: '2026-11-03T20:00:00Z' -> 2026-11-04T01:30 IST, IN the 11-04 session.
        assertEquals("01:30:00", keyOf("poll_ts", "2026-11-03T20:00:00Z"))
        // Oracle: '2026-11-04T19:00:00Z' -> 2026-11-05T00:30 IST, NOT in the 11-04 session.
        assertNull(keyOf("poll_ts", "2026-11-04T19:00:00Z"))
        // Exact edges of the IST day.
        assertEquals("23:59:59", keyOf("poll_ts", "2026-11-04T18:29:59Z"))
        assertNull(keyOf("poll_ts", "2026-11-04T18:30:00Z"))
    }

    // ---- clock-only forms --------------------------------------------------------------------

    @Test
    fun twelveHourClockFormsAreAccepted() {
        // * v1 rejected every one of these; the consumer's third strptime format is '%I:%M %p'.
        // Oracle: 09:20 AM -> 09:20, 01:20 PM -> 13:20, 12:00 AM -> 00:00, 12:00 PM -> 12:00
        assertEquals("09:20:00", keyOf("t", "09:20 AM"))
        assertEquals("13:20:00", keyOf("t", "01:20 PM"))
        assertEquals("00:00:00", keyOf("t", "12:00 AM"))
        assertEquals("12:00:00", keyOf("t", "12:00 PM"))
        assertEquals("lower case is upper-cased by the consumer", "09:20:00", keyOf("t", "09:20 am"))
    }

    @Test
    fun twentyFourHourClockFormsMatchStrptimeOrder() {
        assertEquals("09:20:00", keyOf("t", "09:20"))
        assertEquals("09:20:00", keyOf("t", "9:20"))
        assertEquals("09:20:30", keyOf("t", "09:20:30"))
        assertEquals("surrounding whitespace is stripped", "09:20:00", keyOf("t", "  09:20  "))
        assertNull(keyOf("t", "25:00"))
        assertNull(keyOf("t", "09:60"))
        assertNull(keyOf("t", "not-a-time"))
    }

    // ---- field precedence --------------------------------------------------------------------

    @Test
    fun precedenceTakesTheFirstFieldThatParsesNotTheFirstPresent() {
        // Oracle: {'poll_ts':'09:20','t':'09:25'} -> 09:20  (* v1 returned 09:25)
        assertEquals("09:20:00", PollHistoryMerge.canonicalKey(
            JSONObject().put("poll_ts", "09:20").put("t", "09:25"), session))
        // Oracle: {'poll_ts':'junk','t':'09:25'} -> 09:25  (unparseable field falls through)
        assertEquals("09:25:00", PollHistoryMerge.canonicalKey(
            JSONObject().put("poll_ts", "junk").put("t", "09:25"), session))
        // Oracle: {'t':'','time':'09:30'} -> 09:30  (`poll.get('t') or poll.get('time')`)
        assertEquals("09:30:00", PollHistoryMerge.canonicalKey(
            JSONObject().put("t", "").put("time", "09:30"), session))
        // Oracle: {'ts':'09:40'} -> 09:40
        assertEquals("09:40:00", PollHistoryMerge.canonicalKey(
            JSONObject().put("ts", "09:40"), session))
        assertEquals("09:35:00", PollHistoryMerge.canonicalKey(
            JSONObject().put("pollTs", "09:35"), session))
    }

    @Test
    fun orderingIsByInstantNotByKeyText() {
        // 01:30 IST written as yesterday's UTC string must sort BEFORE 09:20, even though its
        // raw string compares later. v1 sorted on the key text it derived from the raw string.
        val early = JSONObject().put("poll_ts", "2026-11-03T20:00:00Z").put("nf", 1.0)
        val late = JSONObject().put("poll_ts", "2026-11-04T03:50:00Z").put("nf", 2.0)
        val merged = PollHistoryMerge.merge(
            org.json.JSONArray().put(late), org.json.JSONArray().put(early), session
        )
        assertEquals(2, merged.kept)
        assertEquals(1.0, merged.history.getJSONObject(0).optDouble("nf"), 1e-9)
        assertEquals(2.0, merged.history.getJSONObject(1).optDouble("nf"), 1e-9)
    }

    @Test
    fun anExplicitDateFieldDisagreeingWithTheSessionIsRejected() {
        // Stricter than the consumer on purpose: parse_ts would re-stamp a time-only row onto
        // the session date, so a row that dates itself to another day must never be admitted.
        assertNull(PollHistoryMerge.canonicalKey(
            JSONObject().put("date", "2026-11-03").put("t", "09:20"), session))
        assertEquals("09:20:00", PollHistoryMerge.canonicalKey(
            JSONObject().put("date", session).put("t", "09:20"), session))
    }
}
