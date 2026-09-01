package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Regression guard for the 2026-08-31 evaluation blackout.
 *
 * `SupabaseClient.normalizedChainRow` used to drop `bid`/`ask` while normalising
 * post-close evaluation chain rows. Since v2.6.3 the Python teacher runs with
 * `require_executable_quotes = true` and `allow_ltp_quote_fallback = false`, so a
 * chain row without executable quotes makes every close-side price unresolvable,
 * `_teacher_round_trip_cost` fails closed, and `_managed_teacher_outcome` returns
 * None with `entry_round_trip_cost_unavailable`. On 2026-08-31 that produced 0 rows
 * in `ml_evaluation_outcomes` from 77 complete snapshots and 38,688 complete chain
 * rows — the inputs were fine; normalisation threw the quotes away.
 *
 * If these assertions ever fail again, the whole session's grading goes silent.
 */
class EvaluationChainQuoteCarryTest {

    private fun row(json: String) = SupabaseClient.normalizedChainRow(JSONObject(json.trimIndent()))

    @Test
    fun carriesExecutableBidAskThrough() {
        val out = requireNotNull(
            row(
                """
                {
                  "index_key": "NF",
                  "strike": 23950,
                  "option_type": "PE",
                  "expiry": "2026-09-01",
                  "poll_ts": "2026-08-31T04:05:14Z",
                  "ltp": 29.95,
                  "bid": 29.8,
                  "ask": 29.9,
                  "session_date": "2026-08-31"
                }
                """
            )
        )

        assertEquals(29.8, out.getDouble("bid"), 0.0001)
        assertEquals(29.9, out.getDouble("ask"), 0.0001)
        assertEquals(29.95, out.getDouble("ltp"), 0.0001)
        assertEquals("NF", out.getString("index_key"))
        assertEquals("2026-09-01", out.getString("expiry"))
    }

    @Test
    fun missingQuotesStayNullAndAreNeverFilledFromLtp() {
        val out = requireNotNull(
            row(
                """
                {
                  "index_key": "BNF",
                  "strike": 63500,
                  "option_type": "CE",
                  "expiry": "2026-09-29",
                  "poll_ts": "2026-08-31T03:45:12Z",
                  "ltp": 1.35
                }
                """
            )
        )

        // Absence of a quote must remain absence. Substituting LTP here would let the
        // teacher price an unexecutable fill and silently fabricate friction.
        assertTrue(out.isNull("bid"))
        assertTrue(out.isNull("ask"))
        assertEquals(1.35, out.getDouble("ltp"), 0.0001)
    }

    @Test
    fun explicitJsonNullQuotesStayNull() {
        val out = requireNotNull(
            row(
                """
                {
                  "index_key": "NF",
                  "strike": 27200,
                  "option_type": "PE",
                  "expiry": "2026-09-01",
                  "poll_ts": "2026-08-31T10:10:23Z",
                  "ltp": 0.05,
                  "bid": null,
                  "ask": null
                }
                """
            )
        )

        assertTrue(out.isNull("bid"))
        assertTrue(out.isNull("ask"))
    }

    @Test
    fun oneSidedQuoteKeepsThePresentSide() {
        val out = requireNotNull(
            row(
                """
                {
                  "index_key": "BNF",
                  "strike": 63400,
                  "option_type": "CE",
                  "expiry": "2026-09-29",
                  "poll_ts": "2026-08-31T03:50:08Z",
                  "ltp": 2.10,
                  "bid": null,
                  "ask": 2.4
                }
                """
            )
        )

        assertTrue(out.isNull("bid"))
        assertEquals(2.4, out.getDouble("ask"), 0.0001)
    }

    @Test
    fun zeroQuoteIsTreatedAsAbsent() {
        val out = requireNotNull(
            row(
                """
                {
                  "index_key": "NF",
                  "strike": 24000,
                  "option_type": "PE",
                  "expiry": "2026-09-01",
                  "poll_ts": "2026-08-31T04:05:14Z",
                  "ltp": 12.5,
                  "bid": 0,
                  "ask": 12.7
                }
                """
            )
        )

        assertTrue(out.isNull("bid"))
        assertEquals(12.7, out.getDouble("ask"), 0.0001)
    }

    @Test
    fun acceptsSnakeCaseQuoteAliases() {
        val out = requireNotNull(
            row(
                """
                {
                  "index": "NF",
                  "strike_price": 24050,
                  "type": "PE",
                  "expiry_date": "2026-09-01",
                  "timestamp": "2026-08-31T04:05:14Z",
                  "last_price": 65.05,
                  "bid_price": 64.85,
                  "ask_price": 65.05
                }
                """
            )
        )

        assertEquals(64.85, out.getDouble("bid"), 0.0001)
        assertEquals(65.05, out.getDouble("ask"), 0.0001)
    }

    @Test
    fun rowWithoutLtpIsStillRejected() {
        assertNull(
            row(
                """
                {
                  "index_key": "NF",
                  "strike": 23950,
                  "option_type": "PE",
                  "expiry": "2026-09-01",
                  "poll_ts": "2026-08-31T04:05:14Z",
                  "bid": 29.8,
                  "ask": 29.9
                }
                """
            )
        )
    }
}
