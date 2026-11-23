package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The validated-extrema read path: one series for the summary, daily review and export.
 *
 * Context, and the field contract that had to be established BEFORE calling anything erroneous:
 *
 *   EXTREMA_CONTRACT          = "observed_every_accepted_valuation_no_exclusions"
 *   TRUSTED_EXTREMA_CONTRACT  = "a1_trusted_extrema_v1_trusted_marks_only_raw_extrema_unchanged"
 *
 * So `running_mae`/`running_mfe` diverging from `trusted_mae`/`trusted_mfe` is BY DESIGN -- the raw
 * pair deliberately admits every accepted valuation (guards v3 "stops excluding bound anomalies
 * from running MAE/MFE"), the trusted pair admits only TRUSTED marks, and the raw pair is
 * explicitly left unchanged. Neither is wrong. An earlier note of mine claiming "three extrema are
 * stored and only one is right" was wrong about the raw pair and is corrected.
 *
 * What was genuinely missing: the trusted pair was readable only inside PositionTickService and
 * inside `position_ticks.policy_trace_json`. `trades_v2.peak_pnl` / `trough_pnl` are written from
 * the PWA close payload (ClosedTradeLedger: "Kotlin does not itself write trades_v2 close
 * patches"), so the PWA could not consume it. This read path is the fix, and it is a read path
 * only -- no stored value changes and no historical row is touched.
 *
 * Observed production figures used below (23 November 2026, gross, from `policy_trace_json`):
 *
 *   trade  running_mfe  trusted_mfe  running_mae  trusted_mae  trades_v2.peak_pnl
 *   318       4476.00      4476.00      -402.00      -402.00       3776
 *   319       8586.50      4036.50      -230.75      -230.75        267   <- one rejected tick
 *   320       1673.75      1673.75    -1755.00     -1755.00        1645
 *   321       2775.00      2775.00    -3595.50     -3595.50        2457
 */
class ValidatedExtremaReadPathTest {

    private fun blob(vararg rows: Pair<String, JSONObject>): String {
        val o = JSONObject()
        rows.forEach { (id, row) -> o.put(id, row) }
        return o.toString()
    }

    private fun row(mae: Double?, mfe: Double?, updatedAt: String = "2026-11-23T09:00:00Z") =
        JSONObject().apply {
            if (mae != null) put("trusted_mae", mae)
            if (mfe != null) put("trusted_mfe", mfe)
            put("updated_at", updatedAt)
        }

    // ------------------------------------------------------------------ contract is published

    @Test
    fun `every reply states contract basis and close handling`() {
        val r = validatedExtremaFromBlob(blob("321" to row(-3595.50, 2775.00)), "321")
        assertEquals(TRUSTED_EXTREMA_CONTRACT, r.getString("contract"))
        assertEquals(VALIDATED_EXTREMA_BASIS, r.getString("basis"))
        assertEquals("GROSS_BEFORE_CHARGES", r.getString("basis"))
        assertFalse(
            "a peak sampled before exit may sit below a booked close; the reply must say so",
            r.getBoolean("includes_accepted_close")
        )
    }

    @Test
    fun `unknown replies also carry the contract so a caller cannot misread them`() {
        val r = validatedExtremaFromBlob("{}", "999")
        assertEquals(VALIDATED_EXTREMA_STATUS_UNKNOWN, r.getString("status"))
        assertEquals(TRUSTED_EXTREMA_CONTRACT, r.getString("contract"))
        assertEquals(VALIDATED_EXTREMA_BASIS, r.getString("basis"))
    }

    // ------------------------------------------------------------------ trade 319, the live case

    @Test
    fun `trade 319 returns the trusted pair, not the unfiltered running pair`() {
        val r = validatedExtremaFromBlob(blob("319" to row(-230.75, 4036.50)), "319")
        assertEquals(VALIDATED_EXTREMA_STATUS_OK, r.getString("status"))
        assertEquals(4036.50, r.getDouble("trusted_mfe"), 0.001)
        assertEquals(-230.75, r.getDouble("trusted_mae"), 0.001)
    }

    @Test
    fun `trade 319 never returns the rejected-tick peak or the trade-row value`() {
        val r = validatedExtremaFromBlob(blob("319" to row(-230.75, 4036.50)), "319")
        val mfe = r.getDouble("trusted_mfe")
        assertFalse("8586.50 came from the tick the structural guard rejected", mfe == 8586.50)
        assertFalse("267 is the trades_v2 value, below 319's own booked 3094", mfe == 267.0)
    }

    @Test
    fun `the three unaffected trades agree with their running pair, which is expected`() {
        // Where no tick was rejected, trusted == running. That agreement is the normal case and
        // is not evidence that the two fields are the same thing.
        val b = blob(
            "318" to row(-402.00, 4476.00),
            "320" to row(-1755.00, 1673.75),
            "321" to row(-3595.50, 2775.00)
        )
        assertEquals(4476.00, validatedExtremaFromBlob(b, "318").getDouble("trusted_mfe"), 0.001)
        assertEquals(1673.75, validatedExtremaFromBlob(b, "320").getDouble("trusted_mfe"), 0.001)
        assertEquals(-3595.50, validatedExtremaFromBlob(b, "321").getDouble("trusted_mae"), 0.001)
    }

    // ------------------------------------------------------------------ restart / resume

    @Test
    fun `restart that preserved the blob keeps the validated pair`() {
        // The blob lives in SharedPreferences, so a process restart or service resume re-reads the
        // same bytes. Nothing is recomputed and nothing is lost.
        val persisted = blob("321" to row(-3595.50, 2775.00, "2026-11-23T08:48:00Z"))
        val afterRestart = validatedExtremaFromBlob(persisted, "321")
        assertEquals(VALIDATED_EXTREMA_STATUS_OK, afterRestart.getString("status"))
        assertEquals(2775.00, afterRestart.getDouble("trusted_mfe"), 0.001)
        assertEquals("2026-11-23T08:48:00Z", afterRestart.getString("updated_at"))
    }

    @Test
    fun `restart that cleared app data reports unknown, never zero`() {
        val r = validatedExtremaFromBlob(null, "321")
        assertEquals(VALIDATED_EXTREMA_STATUS_UNKNOWN, r.getString("status"))
        assertFalse("absence must not be published as a value", r.has("trusted_mfe"))
        assertFalse(r.has("trusted_mae"))
    }

    // ------------------------------------------------------------------ legacy and malformed

    @Test
    fun `legacy position with no validated evidence is unknown`() {
        // Closed before this contract shipped: the id is simply absent from the blob.
        val r = validatedExtremaFromBlob(blob("321" to row(-3595.50, 2775.00)), "318")
        assertEquals(VALIDATED_EXTREMA_STATUS_UNKNOWN, r.getString("status"))
    }

    @Test
    fun `row present but both values null is unknown, not a zero excursion`() {
        val r = validatedExtremaFromBlob(blob("321" to row(null, null)), "321")
        assertEquals(VALIDATED_EXTREMA_STATUS_UNKNOWN, r.getString("status"))
        assertFalse(r.has("trusted_mfe"))
    }

    @Test
    fun `a corrupt blob is unknown rather than an exception or a guess`() {
        listOf("not json at all", "[1,2,3]", "", "{\"321\":", "null").forEach { bad ->
            val r = validatedExtremaFromBlob(bad, "321")
            assertEquals(
                "corrupt blob must degrade to unknown: $bad",
                VALIDATED_EXTREMA_STATUS_UNKNOWN, r.getString("status")
            )
        }
    }

    @Test
    fun `blank trade id is unknown`() {
        assertEquals(
            VALIDATED_EXTREMA_STATUS_UNKNOWN,
            validatedExtremaFromBlob(blob("321" to row(-1.0, 1.0)), "").getString("status")
        )
    }

    // ------------------------------------------------------------------ one-sided evidence

    @Test
    fun `only a favourable mark observed still reports what is known`() {
        val r = validatedExtremaFromBlob(blob("322" to row(null, 500.00)), "322")
        assertEquals(VALIDATED_EXTREMA_STATUS_OK, r.getString("status"))
        assertEquals(500.00, r.getDouble("trusted_mfe"), 0.001)
        assertFalse("an unobserved adverse side stays absent, not 0.0", r.has("trusted_mae"))
    }

    @Test
    fun `only an adverse mark observed still reports what is known`() {
        val r = validatedExtremaFromBlob(blob("323" to row(-750.00, null)), "323")
        assertEquals(VALIDATED_EXTREMA_STATUS_OK, r.getString("status"))
        assertEquals(-750.00, r.getDouble("trusted_mae"), 0.001)
        assertFalse(r.has("trusted_mfe"))
    }

    // ------------------------------------------------------------------ the writer still agrees

    @Test
    fun `updateTrustedExtrema remains the only writer and is unchanged by this read path`() {
        var (mae, mfe) = updateTrustedExtrema(null, null, -100.0)
        assertEquals(-100.0, mae!!, 0.001)
        assertEquals(-100.0, mfe!!, 0.001)
        val next = updateTrustedExtrema(mae, mfe, 250.0)
        assertEquals(-100.0, next.first!!, 0.001)
        assertEquals(250.0, next.second!!, 0.001)
        // A null (untrusted) mark leaves both ends alone -- the property that kept 319 correct.
        val held = updateTrustedExtrema(next.first, next.second, null)
        assertEquals(-100.0, held.first!!, 0.001)
        assertEquals(250.0, held.second!!, 0.001)
    }

    @Test
    fun `the blob key is shared between reader and writer`() {
        assertEquals("position_tick_trusted_extrema_v1", PREF_TRUSTED_EXTREMA_BLOB)
        assertTrue(PREF_TRUSTED_EXTREMA_BLOB.isNotBlank())
    }
}
