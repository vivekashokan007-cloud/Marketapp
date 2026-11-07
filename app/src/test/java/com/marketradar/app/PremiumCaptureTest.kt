package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * Bounded exact-contract IV/quote observation: the six evidence areas Codex required.
 *
 * Built against the COMMITTED RAW VENDOR FIXTURE
 * `app/src/test/resources/contracts/x1_upstox_raw_chains.json`, which carries real
 * `/v2/option/chain` response shape (`strike_price`, `expiry`, `underlying_key`,
 * `underlying_spot_price`, and per-leg `instrument_key` / `market_data` / `option_greeks`), so the
 * records are exercised against the actual response contract rather than a shape I invented.
 *
 * Everything here is desktop/JVM. **No device evidence exists and none is claimed.**
 */
class PremiumCaptureTest {

    private fun rawFixture(id: String): JSONObject {
        // same convention as B510X1RawFeedFormatterTest: resources come from the classloader
        val text = requireNotNull(
            javaClass.classLoader?.getResourceAsStream("contracts/x1_upstox_raw_chains.json"))
            .bufferedReader().readText()
        val doc = JSONObject(text)
        val snaps = doc.getJSONArray("snapshots")
        for (i in 0 until snaps.length()) {
            val s = snaps.getJSONObject(i)
            if (s.optString("id") == id) return s
        }
        throw AssertionError("fixture snapshot $id not found")
    }

    private fun tmpStore(): PremiumCaptureStore =
        PremiumCaptureStore(Files.createTempDirectory("premcap").toFile())

    private fun md(vararg kv: Pair<String, Any?>): JSONObject {
        val o = JSONObject()
        kv.forEach { (k, v) -> o.put(k, v ?: JSONObject.NULL) }
        return o
    }

    private fun leg(key: String, market: JSONObject?, greeks: JSONObject?): JSONObject {
        val o = JSONObject().put("instrument_key", key)
        if (market != null) o.put("market_data", market)
        if (greeks != null) o.put("option_greeks", greeks)
        return o
    }

    private fun chain(expiry: String, underlying: String, spot: Double,
                      strikes: List<Double>, ceKey: (Double) -> String? = { "CE|$it" },
                      peKey: (Double) -> String? = { "PE|$it" }): JSONObject {
        val data = JSONArray()
        for (k in strikes) {
            val row = JSONObject().put("strike_price", k).put("expiry", expiry)
                .put("underlying_key", underlying).put("underlying_spot_price", spot)
            ceKey(k)?.let { row.put("call_options", leg(it,
                md("bid_price" to 10.0, "ask_price" to 10.5, "bid_qty" to 50, "ask_qty" to 50,
                    "ltp" to 10.2), md("iv" to 14.2))) }
            peKey(k)?.let { row.put("put_options", leg(it,
                md("bid_price" to 9.0, "ask_price" to 9.5, "bid_qty" to 50, "ask_qty" to 50,
                    "ltp" to 9.2), md("iv" to 15.1))) }
            data.put(row)
        }
        return JSONObject().put("data", data)
    }

    // ───────────────── 1. causal deterministic selection, ties, restart ─────────────────

    @Test
    fun selectsThreeNearestPairedStrikesFromTheResponsesOwnSpot() {
        val ch = chain("2026-10-23", "NSE_INDEX|Nifty 50", 24510.0,
            listOf(24400.0, 24450.0, 24500.0, 24550.0, 24600.0, 24650.0))
        val sel = PremiumCaptureCohort.select(ch, "NF", "2026-10-19", "09:25",
            "NSE_INDEX|Nifty 50")
        assertEquals(PremiumCaptureCohort.SELECTED, sel.status)
        val strikes = sel.cohort!!.getJSONObject("causal_inputs")
            .getJSONArray("selected_strikes")
        assertEquals(3, strikes.length())
        // |24500-24510|=10, |24550-24510|=40, |24450-24510|=60, |24600-24510|=90
        assertEquals(listOf(24450.0, 24500.0, 24550.0),
            (0 until 3).map { strikes.getDouble(it) })
        assertEquals(6, sel.cohort!!.getJSONArray("pinned_contracts").length())
        assertEquals(24510.0, sel.cohort!!.getJSONObject("causal_inputs")
            .getDouble("underlying_spot_price"), 0.0)
        assertEquals("SAME_ACQUISITION_RESPONSE_ROW",
            sel.cohort!!.getJSONObject("causal_inputs").getString("spot_basis"))
    }

    @Test
    fun equalDistanceBreaksToTheLowerStrike() {
        // spot exactly between 24500 and 24600; the third pick must be the LOWER of the tie
        val ch = chain("2026-10-23", "NSE_INDEX|Nifty 50", 24550.0,
            listOf(24400.0, 24500.0, 24600.0, 24700.0))
        val sel = PremiumCaptureCohort.select(ch, "NF", "2026-10-19", "09:25", null)
        val got = sel.cohort!!.getJSONObject("causal_inputs").getJSONArray("selected_strikes")
        assertEquals(listOf(24400.0, 24500.0, 24600.0), (0 until 3).map { got.getDouble(it) })
    }

    @Test
    fun aStrikeMissingEitherLegIsNotPairedAndCannotBeSelected() {
        val ch = chain("2026-10-23", "NSE_INDEX|Nifty 50", 24500.0,
            listOf(24450.0, 24500.0, 24550.0), peKey = { if (it == 24500.0) null else "PE|$it" })
        val sel = PremiumCaptureCohort.select(ch, "NF", "2026-10-19", "09:25", null)
        // only two paired strikes remain, so selection is INCOMPLETE rather than invented
        assertEquals(PremiumCaptureCohort.INCOMPLETE_PAIRED_STRIKES, sel.status)
        assertNull(sel.cohort)
    }

    @Test
    fun selectionDoesNotRequireValidIvOrBookSoMissingQuoteEvidenceIsNotHidden() {
        val data = JSONArray()
        for (k in listOf(24450.0, 24500.0, 24550.0)) {
            data.put(JSONObject().put("strike_price", k).put("expiry", "2026-10-23")
                .put("underlying_key", "U").put("underlying_spot_price", 24500.0)
                // no market_data and no option_greeks at all
                .put("call_options", JSONObject().put("instrument_key", "CE|$k"))
                .put("put_options", JSONObject().put("instrument_key", "PE|$k")))
        }
        val sel = PremiumCaptureCohort.select(JSONObject().put("data", data), "NF",
            "2026-10-19", "09:25", null)
        assertEquals(PremiumCaptureCohort.SELECTED, sel.status)
    }

    @Test
    fun selectionIsUnavailableWithoutAValidSpotAndNeverCached() {
        val data = JSONArray().put(JSONObject().put("strike_price", 24500.0)
            .put("expiry", "2026-10-23").put("underlying_key", "U")
            .put("call_options", JSONObject().put("instrument_key", "CE"))
            .put("put_options", JSONObject().put("instrument_key", "PE")))
        val sel = PremiumCaptureCohort.select(JSONObject().put("data", data), "NF",
            "2026-10-19", "09:25", null)
        assertEquals(PremiumCaptureCohort.UNAVAILABLE_SPOT, sel.status)
    }

    @Test
    fun theExperimentWindowIsEnforcedAndALateStartIsRecorded() {
        val ch = chain("2026-10-23", "U", 24500.0, listOf(24450.0, 24500.0, 24550.0))
        assertEquals(PremiumCaptureCohort.OUTSIDE_WINDOW,
            PremiumCaptureCohort.select(ch, "NF", "2026-10-19", "09:15", null).status)
        assertEquals(PremiumCaptureCohort.OUTSIDE_WINDOW,
            PremiumCaptureCohort.select(ch, "NF", "2026-10-19", "15:05", null).status)
        val onTime = PremiumCaptureCohort.select(ch, "NF", "2026-10-19", "09:20", null)
        assertFalse(onTime.cohort!!.getBoolean("late_start"))
        val late = PremiumCaptureCohort.select(ch, "NF", "2026-10-19", "11:30", null)
        assertTrue(late.cohort!!.getBoolean("late_start"))
    }

    @Test
    fun theSameSixIdentitiesSurviveSpotMovementAndRestart() {
        val store = tmpStore()
        val s1 = PremiumCaptureSession(store)
        val first = chain("2026-10-23", "U", 24500.0,
            listOf(24400.0, 24450.0, 24500.0, 24550.0, 24600.0))
        val r1 = s1.observeSlot("NF", "2026-10-19", first, "09:25", "09:25", null, 1L, 2L)
        assertEquals("OBSERVED", r1.status)
        val pinned1 = PremiumCaptureCohort.pinnedList(s1.loadCohort("NF", "2026-10-19")!!)
            .map { it.instrumentKey }.sorted()

        // spot moves a long way; a NEW session object simulates a process restart
        val moved = chain("2026-10-23", "U", 24900.0,
            listOf(24400.0, 24450.0, 24500.0, 24550.0, 24600.0, 24850.0, 24900.0, 24950.0))
        val s2 = PremiumCaptureSession(store)
        val r2 = s2.observeSlot("NF", "2026-10-19", moved, "10:00", "10:00", null, 3L, 4L)
        assertEquals("OBSERVED", r2.status)
        assertEquals(r1.cohortId, r2.cohortId)
        assertEquals(pinned1, PremiumCaptureCohort.pinnedList(s2.loadCohort("NF", "2026-10-19")!!)
            .map { it.instrumentKey }.sorted())
        // the moved spot's near-ATM strikes were NOT substituted in
        assertFalse(pinned1.any { it.contains("24900") })
    }

    @Test
    fun aDerivedSlotIsLabelledAndTheActualClockIsRetained() {
        val fromScheduler = PremiumCaptureCohort.slotIdentity("09:25", "09:25:41")!!
        assertEquals("SCHEDULER_SLOT", fromScheduler.basis)
        val derived = PremiumCaptureCohort.slotIdentity(null, "09:27:41")!!
        assertEquals("09:25", derived.slot)
        assertEquals("DERIVED_FIVE_MINUTE_FROM_LOCAL_ACQUISITION_CLOCK", derived.basis)
        assertEquals("09:27:41", derived.actualClock)
        assertNull(PremiumCaptureCohort.slotIdentity(null, null))
    }

    // ───────────────── 2. raw IV, zero vs absent, units, books, lot ─────────────────

    @Test
    fun bothIvSourcesAreKeptSeparateAndNeitherIsNominatedAuthoritative() {
        val l = leg("K", md("iv" to 11.0), md("iv" to 14.2))
        val iv = PremiumObservationContract.ivEvidence(l)
        assertEquals(14.2, iv.getJSONObject("iv_greeks_raw").getDouble("raw"), 1e-9)
        assertEquals(11.0, iv.getJSONObject("iv_market_data_raw").getDouble("raw"), 1e-9)
        assertEquals("BOTH_PRESENT", iv.getString("iv_source"))
        assertEquals("UNKNOWN", iv.getString("iv_unit_status"))
        assertFalse(iv.getBoolean("iv_authoritative_resolved"))
        // a raw-field difference is reported, and explicitly not an economic claim
        val cmp = iv.getJSONObject("iv_raw_field_comparison")
        assertEquals("RAW_FIELD_DIFFERENCE_UNITS_UNVERIFIED", cmp.getString("basis"))
        assertFalse(cmp.getBoolean("economically_meaningful"))
    }

    @Test
    fun anObservedZeroIvIsNotAbsentAndNotAValidSigma() {
        val iv = PremiumObservationContract.ivEvidence(leg("K", null, md("iv" to 0.0)))
        val g = iv.getJSONObject("iv_greeks_raw")
        assertEquals(PremiumObservationContract.OBSERVED_ZERO, g.getString("status"))
        assertEquals(0.0, g.getDouble("raw"), 0.0)             // the vendor zero is RETAINED
        assertEquals("GREEKS_PRESENT_ONLY", iv.getString("iv_source"))
        // and it is not comparable, so no raw difference is claimed
        assertTrue(iv.isNull("iv_raw_field_comparison"))
    }

    @Test
    fun anAbsentIvIsNullAndNeverAFabricatedZero() {
        val iv = PremiumObservationContract.ivEvidence(leg("K", md("ltp" to 1.0), JSONObject()))
        val g = iv.getJSONObject("iv_greeks_raw")
        assertEquals(PremiumObservationContract.ABSENT, g.getString("status"))
        assertTrue(g.isNull("raw"))                            // NOT 0.0
        assertEquals("ABSENT", iv.getString("iv_source"))
    }

    @Test
    fun negativeMalformedAndNonFiniteIvAreDistinctAndJsonSafe() {
        val neg = PremiumObservationContract.ivEvidence(leg("K", null, md("iv" to -3.0)))
        assertEquals(PremiumObservationContract.OBSERVED_NEGATIVE,
            neg.getJSONObject("iv_greeks_raw").getString("status"))
        val bad = PremiumObservationContract.ivEvidence(leg("K", null, md("iv" to "n/a")))
        assertEquals(PremiumObservationContract.OBSERVED_MALFORMED,
            bad.getJSONObject("iv_greeks_raw").getString("status"))
        val nan = PremiumObservationContract.rawScalar(md("iv" to Double.NaN), "iv")
        assertEquals(PremiumObservationContract.OBSERVED_NON_FINITE, nan.status)
        assertEquals(PremiumObservationContract.NON_FINITE_NAN, nan.raw)
        // a JSON-safe sentinel, never a bare NaN number
        val serialized = JSONObject().put("x", nan.json()).toString()
        assertFalse(serialized.contains("NaN"))
        assertFalse(serialized.contains("Infinity"))
    }

    @Test
    fun theLiveIvHelperIsNotReusedAndIsNotChanged() {
        // the helper still collapses sources and fabricates 0.0 -- which is why this record does
        // not use it. Pinning its behaviour here proves this delivery did not alter trading code.
        assertEquals(0.0, OptionChainBrainFormatter.optionIv(JSONObject(), JSONObject()), 0.0)
        assertEquals(14.2, OptionChainBrainFormatter.optionIv(
            leg("K", null, md("iv" to 14.2)), null), 1e-9)
        val src = File("src/main/java/com/marketradar/app/PremiumObservationContract.kt").readText()
        assertFalse("the evidence record must not call optionIv", src.contains("optionIv("))
        assertFalse("the evidence record must not call optionQuote", src.contains("optionQuote("))
    }

    @Test
    fun bookStatesIncludeCrossedOneSidedEmptyInvalidAndMissing() {
        fun status(m: JSONObject?) = PremiumObservationContract
            .bboEvidence(leg("K", m, null)).getString("book_status")
        assertEquals(PremiumObservationContract.BOOK_TWO_SIDED,
            status(md("bid_price" to 10.0, "ask_price" to 10.5)))
        assertEquals(PremiumObservationContract.BOOK_TWO_SIDED,
            status(md("bid_price" to 10.0, "ask_price" to 10.0)))   // ask == bid is two-sided
        assertEquals(PremiumObservationContract.BOOK_CROSSED,
            status(md("bid_price" to 10.5, "ask_price" to 10.0)))
        assertEquals(PremiumObservationContract.BOOK_ONE_SIDED,
            status(md("bid_price" to 10.0)))
        assertEquals(PremiumObservationContract.BOOK_EMPTY, status(md("ltp" to 5.0)))
        assertEquals(PremiumObservationContract.BOOK_INVALID,
            status(md("bid_price" to 0.0, "ask_price" to 0.0)))
        assertEquals(PremiumObservationContract.BOOK_MISSING, status(null))
    }

    @Test
    fun noLtpFallbackAndNoInventedMidpointAndQuantityUnitsStayUnknown() {
        val b = PremiumObservationContract.bboEvidence(
            leg("K", md("ltp" to 10.2, "bid_qty" to 50), null))
        assertFalse(b.getBoolean("ltp_used_as_quote_fallback"))
        assertFalse(b.getBoolean("executable_mid_computed"))
        assertFalse(b.has("mid"))
        assertEquals("UNKNOWN", b.getString("quantity_unit_status"))
        // the ltp is retained as its own field, not promoted into a missing side
        assertEquals(10.2, b.getJSONObject("ltp_raw").getDouble("raw"), 1e-9)
        assertEquals(PremiumObservationContract.ABSENT,
            b.getJSONObject("ask_raw").getString("status"))
    }

    @Test
    fun aConstantLotIsNeverAuthoritative() {
        val c = PremiumObservationContract.lotEvidence(75, "CONSTANT", null, null)
        assertFalse(c.getBoolean("lot_authoritative_resolved"))
        assertEquals("UNVERIFIED_PRODUCER_CLAIM", c.getString("lot_status"))
        assertEquals(75, c.getJSONObject("lot_claimed_raw").getInt("raw"))   // still recorded
        val dated = PremiumObservationContract.lotEvidence(75, "CONTRACT_LOT_TABLE",
            "lot_table@2026-10-01", "map@2026-10-01")
        assertTrue(dated.getBoolean("lot_authoritative_resolved"))
        assertEquals("lot_table@2026-10-01", dated.getString("contract_lot_table_ref"))
    }

    // ───────────────── 3. acquisition linkage, mismatch, missing bands ─────────────────

    @Test
    fun theSameAcquisitionUnderlyingIsPreferredAndCalledSoExplicitly() {
        val row = JSONObject().put("underlying_spot_price", 24500.0)
        val u = PremiumObservationContract.underlyingEvidence(row, 99.0, "nfSpot", "09:25", 5L)
        assertEquals("SAME_ACQUISITION", u.getString("basis"))
        assertEquals("SAME_RESPONSE_PAYLOAD", u.getString("synchronisation_with_quote"))
        assertEquals(24500.0, u.getJSONObject("spot_raw").getDouble("raw"), 1e-9)
    }

    @Test
    fun aSeparatelyObservedSpotIsNeverCalledSynchronised() {
        val u = PremiumObservationContract.underlyingEvidence(JSONObject(), 24500.0,
            "nfSpot", "09:25", 5L)
        assertEquals("SEPARATE_OBSERVATION", u.getString("basis"))
        assertEquals("UNKNOWN_NOT_SAME_ACQUISITION", u.getString("synchronisation_with_quote"))
        assertFalse(u.getBoolean("carried_by_chain_response"))
        val none = PremiumObservationContract.underlyingEvidence(JSONObject(), null, null, null, null)
        assertEquals("UNAVAILABLE", none.getString("basis"))
    }

    @Test
    fun clocksSeparatePresenceFromVerifiedEventSemantics() {
        val withTs = PremiumObservationContract.clockEvidence(100L, 350L, "2026-10-19T09:25:00Z")
        assertEquals("VENDOR_TS_PRESENT", withTs.getString("vendor_ts_status"))
        assertEquals("UNVERIFIED", withTs.getString("vendor_event_semantics"))
        assertFalse(withTs.getBoolean("quote_event_clock_verified"))
        assertEquals(250L, withTs.getLong("acquisition_elapsed_ms"))
        val without = PremiumObservationContract.clockEvidence(100L, 350L, null)
        assertEquals("VENDOR_TS_ABSENT", without.getString("vendor_ts_status"))
        assertTrue(without.isNull("vendor_source_ts"))          // stays unknown
    }

    @Test
    fun perIndexAcquisitionTimingIsRecordedNotASharedPollTime() {
        val store = tmpStore()
        val s = PremiumCaptureSession(store)
        val ch = chain("2026-10-23", "U", 24500.0, listOf(24450.0, 24500.0, 24550.0))
        s.observeSlot("NF", "2026-10-19", ch, "09:25", "09:25", null, 1000L, 1200L)
        s.observeSlot("BNF", "2026-10-19", ch, "09:25", "09:25", null, 5000L, 5900L)
        val nf = store.readEnvelopes("NF", "2026-10-19").first().getJSONObject("clocks")
        val bnf = store.readEnvelopes("BNF", "2026-10-19").first().getJSONObject("clocks")
        assertEquals(200L, nf.getLong("acquisition_elapsed_ms"))
        assertEquals(900L, bnf.getLong("acquisition_elapsed_ms"))
        assertEquals(1000L, nf.getLong("acquisition_request_ms"))
        assertEquals(5000L, bnf.getLong("acquisition_request_ms"))
    }

    @Test
    fun identityDisagreementIsExplicitAndNeverPairedAcrossExpiryOrIndex() {
        val pinned = PremiumCaptureCohort.PinnedContract("NF", "CE|24500", "U",
            "2026-10-23", 24500.0, "CE")
        val row = JSONObject().put("underlying_key", "OTHER").put("expiry", "2026-10-30")
            .put("strike_price", 24600.0)
        val id = PremiumObservationContract.identityEvidence(pinned, row, leg("CE|OTHER", null, null))
        assertFalse(id.getBoolean("identity_verified"))
        val d = (0 until id.getJSONArray("identity_disagreements").length())
            .map { id.getJSONArray("identity_disagreements").getString(it) }.sorted()
        assertEquals(listOf("expiry", "instrument_key", "strike_price", "underlying_key"), d)
    }

    @Test
    fun missingBandsAndRequestFailuresEachGetTheirOwnOutcome() {
        val store = tmpStore()
        val s = PremiumCaptureSession(store)
        val full = chain("2026-10-23", "U", 24500.0, listOf(24450.0, 24500.0, 24550.0))
        s.observeSlot("NF", "2026-10-19", full, "09:25", "09:25", null, 1L, 2L)

        // a later response that no longer carries one pinned strike
        val partial = chain("2026-10-23", "U", 24500.0, listOf(24450.0, 24500.0))
        val r = s.observeSlot("NF", "2026-10-19", partial, "09:30", "09:30", null, 3L, 4L)
        assertEquals(2, r.outcomes.values.count {
            it == PremiumObservationContract.OUTCOME_NOT_IN_RESPONSE })

        // a failed acquisition: all six are MISSING_REQUEST_FAILED, not absent records
        val f = s.observeSlot("NF", "2026-10-19", null, "09:35", "09:35", null, 5L, null)
        assertEquals(6, f.outcomes.size)
        assertTrue(f.outcomes.values.all {
            it == PremiumObservationContract.OUTCOME_REQUEST_FAILED })

        // the pinned expiry disappearing is a rollover status, never a silent roll
        val rolled = chain("2026-10-30", "U", 24500.0, listOf(24450.0, 24500.0, 24550.0))
        val rr = s.observeSlot("NF", "2026-10-19", rolled, "09:40", "09:40", null, 7L, 8L)
        assertTrue(rr.outcomes.values.all {
            it == PremiumObservationContract.OUTCOME_EXPIRY_ROLLED })
        assertEquals("2026-10-23", s.loadCohort("NF", "2026-10-19")!!.getString("expiry"))
    }

    @Test
    fun everyPinnedContractGetsAnOutcomeInEveryAttemptedSlot() {
        val store = tmpStore()
        val s = PremiumCaptureSession(store)
        val ch = chain("2026-10-23", "U", 24500.0, listOf(24450.0, 24500.0, 24550.0))
        val r = s.observeSlot("NF", "2026-10-19", ch, "09:25", "09:25", null, 1L, 2L)
        assertEquals(PremiumCaptureCohort.CONTRACTS_PER_COHORT, r.outcomes.size)
        assertEquals(6, r.written)
    }

    // ───────────────── 4. export round trip resolves offline ─────────────────

    @Test
    fun exportRoundTripResolvesRecordsReferencesAndRawValuesOffline() {
        val store = tmpStore()
        val s = PremiumCaptureSession(store)
        val ch = chain("2026-10-23", "NSE_INDEX|Nifty 50", 24500.0,
            listOf(24450.0, 24500.0, 24550.0))
        s.observeSlot("NF", "2026-10-19", ch, "09:25", "09:25", "NSE_INDEX|Nifty 50", 1L, 2L,
            claimedLot = 75, claimedLotSource = "CONTRACT_LOT_TABLE",
            lotTableRef = "lot_table@2026-10-01", mapRef = "map@2026-10-01")
        val ex = s.export("NF", "2026-10-19", "09:25")

        // survives a serialise/parse round trip with no device present
        val reparsed = JSONObject(ex.toString())
        assertEquals(6, reparsed.getJSONArray("records").length())
        assertEquals(6, reparsed.getJSONObject("accounting").getInt("hash_verified"))
        assertEquals(6, reparsed.getJSONObject("references")
            .getJSONArray("pinned_contracts").length())
        // lot now resolves from the slot envelope, joined on session_date+index+slot
        assertEquals(1, reparsed.getJSONArray("slot_envelopes").length())
        val envl = reparsed.getJSONArray("slot_envelopes").getJSONObject(0)
        assertEquals("lot_table@2026-10-01",
            envl.getJSONObject("lot").getString("contract_lot_table_ref"))
        assertEquals("SAME_ACQUISITION", envl.getJSONObject("underlying").getString("basis"))
        assertEquals(1, reparsed.getJSONObject("accounting").getInt("envelope_hash_verified"))
        // every record names a slot that an envelope exists for, so the join closes
        val envSlots = (0 until reparsed.getJSONArray("slot_envelopes").length())
            .map { reparsed.getJSONArray("slot_envelopes").getJSONObject(it).getString("slot") }
            .toSet()
        for (i in 0 until reparsed.getJSONArray("records").length()) {
            assertTrue(envSlots.contains(
                reparsed.getJSONArray("records").getJSONObject(i).getString("slot")))
        }
        // the original raw values are present in the export itself
        val rec = reparsed.getJSONArray("records").getJSONObject(0)
        assertNotNull(rec.getJSONObject("iv").getJSONObject("iv_greeks_raw").get("raw"))
        assertTrue(rec.getJSONObject("bbo").has("bid_raw"))
        assertEquals("session_date+index+slot", rec.getString("envelope_join"))
        assertFalse("per-acquisition facts must not be repeated per contract", rec.has("clocks"))
        assertFalse(rec.has("lot"))
        assertFalse(rec.has("underlying"))
        assertFalse(reparsed.getBoolean("device_evidence"))
        assertFalse(reparsed.getJSONObject("claims")
            .getBoolean("synchronised_exchange_quotes"))
    }

    @Test
    fun theManifestRevealsExpectedVersusObservedSlotsWithNoFabricatedQuotes() {
        val store = tmpStore()
        val s = PremiumCaptureSession(store)
        val ch = chain("2026-10-23", "U", 24500.0, listOf(24450.0, 24500.0, 24550.0))
        s.observeSlot("NF", "2026-10-19", ch, "10:00", "10:00", null, 1L, 2L)
        val ex = s.export("NF", "2026-10-19", "10:00", schedulerSkippedSlots = listOf("10:05"))
        val man = ex.getJSONObject("slot_manifest")
        assertEquals(PremiumCaptureSession.expectedSlots().size, man.getInt("expected_slots"))
        assertEquals(1, man.getInt("observed_slots"))
        val rows = man.getJSONArray("rows")
        val byState = HashMap<String, Int>()
        for (i in 0 until rows.length()) {
            val st = rows.getJSONObject(i).getString("state")
            byState[st] = (byState[st] ?: 0) + 1
        }
        // slots before capture started are distinguished from a skipped slot and from a failure
        assertTrue((byState[PremiumObservationContract.OUTCOME_BEFORE_CAPTURE] ?: 0) > 0)
        assertEquals(1, byState[PremiumObservationContract.OUTCOME_SLOT_SKIPPED])
        assertEquals(1, byState["OBSERVED"])
        // no record exists for an unobserved slot
        for (i in 0 until rows.length()) {
            val r = rows.getJSONObject(i)
            if (r.getString("state") != "OBSERVED") assertEquals(0, r.getInt("records"))
        }
    }

    @Test
    fun theHashExcludesItselfAndDetectsAnyEdit() {
        val rec = PremiumObservationContract.stampHash(JSONObject().put("a", 1).put("b", "x"))
        assertTrue(PremiumObservationContract.verifyHash(rec))
        assertEquals(PremiumObservationContract.HASH_OMITTED_FIELD,
            rec.getJSONObject("hash_basis").getString("omitted_field"))
        assertFalse(rec.getJSONObject("hash_basis").getBoolean("authenticates_vendor_truth"))
        val tampered = JSONObject(rec.toString()).put("a", 2)
        assertFalse(PremiumObservationContract.verifyHash(tampered))
        // editing the hash_basis description is also detected, because it is hashed too
        val tampered2 = JSONObject(rec.toString())
        tampered2.getJSONObject("hash_basis").put("authenticates_vendor_truth", true)
        assertFalse(PremiumObservationContract.verifyHash(tampered2))
    }

    // ───────────────── 5. duplicates, conflicts, caps, restart, interrupted writes ─────────────

    @Test
    fun identicalBytesAreANoOpAndConflictsAreArchivedWithinTheCaps() {
        val store = tmpStore()
        val rec = PremiumObservationContract.stampHash(JSONObject().put("v", 1))
        assertEquals(PremiumCaptureStore.WRITTEN, store.put("NF", "S", "k", rec).status)
        assertEquals(PremiumCaptureStore.DUPLICATE_NO_OP, store.put("NF", "S", "k", rec).status)
        assertEquals(1, store.outcomeCount("NF", "S"))

        val other = PremiumObservationContract.stampHash(JSONObject().put("v", 2))
        assertEquals(PremiumCaptureStore.CONFLICT_ARCHIVED, store.put("NF", "S", "k", other).status)
        // the FIRST write still stands: the originally observed evidence is not replaced
        assertEquals(1, store.readAll("NF", "S").size)
        assertEquals(1, store.readAll("NF", "S").first().getInt("v"))
        assertEquals(1, store.conflictCount("NF", "S"))
    }

    @Test
    fun conflictArchivesCannotEscapeTheCaps() {
        val store = tmpStore()
        store.put("NF", "S", "k", PremiumObservationContract.stampHash(JSONObject().put("v", 0)))
        for (i in 1..PremiumCaptureStore.MAX_CONFLICTS_PER_INDEX_SESSION) {
            assertEquals("conflict $i", PremiumCaptureStore.CONFLICT_ARCHIVED,
                store.put("NF", "S", "k",
                    PremiumObservationContract.stampHash(JSONObject().put("v", i))).status)
        }
        assertEquals(PremiumCaptureStore.MAX_CONFLICTS_PER_INDEX_SESSION,
            store.conflictCount("NF", "S"))
        // past the cap the conflict is REFUSED, not archived outside the bound
        val r = store.put("NF", "S", "k", PremiumObservationContract.stampHash(
            JSONObject().put("v", 9999)))
        assertEquals(PremiumCaptureStore.REFUSED_CONFLICT_CAP, r.status)
        assertEquals(PremiumCaptureStore.MAX_CONFLICTS_PER_INDEX_SESSION,
            store.conflictCount("NF", "S"))
    }

    @Test
    fun anOverlargeRecordIsRefusedNotSilentlyTruncated() {
        val store = tmpStore()
        val big = JSONObject().put("pad", "x".repeat(4000))
        val r = store.put("NF", "S", "k", big)
        assertEquals(PremiumCaptureStore.REFUSED_RECORD_TOO_LARGE, r.status)
        assertTrue(r.detail!!.contains("not truncated"))
        assertEquals(0, store.outcomeCount("NF", "S"))
    }

    @Test
    fun theOutcomeCapRefusesRatherThanEvicting() {
        val store = tmpStore()
        for (i in 1..PremiumCaptureStore.MAX_OUTCOMES_PER_INDEX_SESSION) {
            val r = store.put("NF", "S", "k$i",
                PremiumObservationContract.stampHash(JSONObject().put("i", i)))
            if (r.status != PremiumCaptureStore.WRITTEN) {
                // a byte cap may bind first; that is also a refusal, never an eviction
                assertTrue(r.status.startsWith("REFUSED_"))
                assertTrue(store.outcomeCount("NF", "S") > 0)
                return
            }
        }
        val over = store.put("NF", "S", "overflow",
            PremiumObservationContract.stampHash(JSONObject().put("i", -1)))
        assertEquals(PremiumCaptureStore.REFUSED_OUTCOME_CAP, over.status)
        assertEquals(PremiumCaptureStore.MAX_OUTCOMES_PER_INDEX_SESSION,
            store.outcomeCount("NF", "S"))
    }

    @Test
    fun theSummaryReserveIsAlwaysWritableEvenAfterDataWritesAreRefused() {
        val store = tmpStore()
        var refusals = 0
        for (i in 1..2000) {
            val r = store.put("NF", "S", "k$i", PremiumObservationContract.stampHash(
                JSONObject().put("pad", "y".repeat(1200)).put("i", i)))
            if (r.status.startsWith("REFUSED_")) { refusals++; if (refusals > 2) break }
        }
        assertTrue("expected a byte or outcome refusal", refusals > 0)
        val s = store.writeSummary("NF", "S", JSONObject()
            .put("refused", refusals).put("note", "bounded summary from the reserve"))
        assertEquals(PremiumCaptureStore.WRITTEN, s.status)
    }

    @Test
    fun nothingIsDeletedToMakeRoomAndTheGlobalCapRefusesExplicitly() {
        val store = tmpStore()
        var sessions = 0
        var hitGlobal = false
        outer@ for (d in 1..40) {
            for (i in 1..500) {
                val r = store.put("NF", "2026-10-%02d".format(d),
                    "k$i", PremiumObservationContract.stampHash(
                        JSONObject().put("pad", "z".repeat(1200)).put("i", i)))
                if (r.status == PremiumCaptureStore.REFUSED_GLOBAL_BYTES) { hitGlobal = true; break@outer }
            }
            sessions = d
        }
        assertTrue("expected the global cap to bind", hitGlobal)
        assertTrue(store.globalBytes() <= PremiumCaptureStore.MAX_BYTES_GLOBAL)
        // earlier sessions still exist: no retention deletion in this delivery
        assertTrue(sessions > 0)
        assertTrue(store.outcomeCount("NF", "2026-10-01") > 0)
    }

    @Test
    fun repeatedRestartsDoNotAccumulateTempFiles() {
        val store = tmpStore()
        val ch = chain("2026-10-23", "U", 24500.0, listOf(24450.0, 24500.0, 24550.0))
        repeat(8) { n ->
            val s = PremiumCaptureSession(store)         // a fresh object each time = a restart
            s.observeSlot("NF", "2026-10-19", ch, "09:%02d".format(25 + n * 5),
                "09:%02d".format(25 + n * 5), null, 1L, 2L)
        }
        assertEquals(0, store.orphanTempFiles("NF", "2026-10-19").size)
    }

    @Test
    fun anInterruptedWriteLeavesTheOldFileIntact() {
        val store = tmpStore()
        val first = PremiumObservationContract.stampHash(JSONObject().put("v", 1))
        store.put("NF", "S", "k", first)
        // simulate a crashed write: a stray temp file beside the destination
        val dir = File(store.root, "S/NF/records")
        File(dir, "crashed.json.tmp").writeText("{\"partial\":")
        assertEquals(1, store.orphanTempFiles("NF", "S").size)
        // the readable record set is unaffected and still parses
        assertEquals(1, store.readAll("NF", "S").size)
        assertEquals(1, store.readAll("NF", "S").first().getInt("v"))
        // and the orphan is reported in the export accounting rather than hidden
        val ex = PremiumCaptureSession(store).export("NF", "S", null)
        assertEquals(1, ex.getJSONObject("accounting").getInt("orphan_temp_files"))
    }

    @Test
    fun aPermissionFailureIsAnExplicitStatusNotAnException() {
        val roRoot = Files.createTempDirectory("premcap-ro").toFile()
        val store = PremiumCaptureStore(File(roRoot, "nested"))
        roRoot.setWritable(false)
        try {
            val r = store.put("NF", "S", "k",
                PremiumObservationContract.stampHash(JSONObject().put("v", 1)))
            assertTrue("expected an explicit refusal, got ${r.status}",
                r.status == PremiumCaptureStore.REFUSED_IO || r.accepted)
        } finally {
            roRoot.setWritable(true)
        }
    }

    // ───────────────── 6. no new API call, wake, brain growth or permission change ─────────────

    @Test
    fun theCaptureSourcesTouchNoNetworkSchedulerOrTradingSurface() {
        val files = listOf("PremiumObservationContract.kt", "PremiumCaptureCohort.kt",
            "PremiumCaptureStore.kt", "PremiumCaptureSession.kt")
        val banned = listOf("OkHttp", "HttpURLConnection", "Request.Builder", "AlarmManager",
            "setExact", "WorkManager", "JobScheduler", "Thread(", "Timer(",
            "SupabaseClient", "NotificationHelper", "OrderExecutionService",
            "SharedPreferences", "getSharedPreferences", "startService", "sendBroadcast")
        for (n in files) {
            val src = File("src/main/java/com/marketradar/app/$n").readText()
            for (b in banned) assertFalse("$n must not reference $b", src.contains(b))
        }
    }

    @Test
    fun captureIsNotWiredIntoTheBrainSnapshotOrServerIngestion() {
        // nothing in the shipped poll path references these types yet: this delivery is the
        // store/record/export only, so it cannot enlarge the brain payload or reach ingestion.
        val dir = File("src/main/java/com/marketradar/app")
        val referrers = (dir.listFiles() ?: emptyArray()).filter { f ->
            f.isFile && f.name.endsWith(".kt") && !f.name.startsWith("Premium") &&
                f.readText().contains("PremiumCapture")
        }.map { it.name }
        assertEquals("no shipped source may reference the capture yet", emptyList<String>(), referrers)
    }

    @Test
    fun aCaptureFailureCannotPropagateOutOfTheStore() {
        // every failure path returns a status; none throws
        val store = PremiumCaptureStore(File("/proc/nonexistent-premium-capture"))
        val r = store.put("NF", "S", "k",
            PremiumObservationContract.stampHash(JSONObject().put("v", 1)))
        assertFalse(r.accepted)
        assertEquals(PremiumCaptureStore.REFUSED_IO, r.status)
        // and the readers degrade to empty rather than raising
        assertEquals(0, store.readAll("NF", "S").size)
        assertEquals(0, store.conflictCount("NF", "S"))
    }

    @Test
    fun capsAreDeclaredProposedAndNotMeasuredTypicalPayloads() {
        val caps = PremiumCaptureStore(Files.createTempDirectory("c").toFile()).capsJson()
        assertEquals("PROPOSED_INITIAL_IMPLEMENTATION_CAPS_NOT_MEASURED_TYPICAL_PAYLOADS",
            caps.getString("caps_are"))
        assertEquals(468, caps.getInt("max_outcomes_per_index_session"))
        assertEquals(32, caps.getInt("max_conflicts_per_index_session"))
        assertEquals("NONE_IN_THIS_DELIVERY", caps.getString("retention_deletion"))
        assertTrue(caps.getBoolean("conflict_archives_inside_caps"))
    }

    // ───────────────── measured fixture sizes, against the real vendor shape ─────────────

    @Test
    fun measuredRecordSizesAgainstTheCommittedRawVendorFixture() {
        val snap = rawFixture("mon_0920_entry_NF")
        val raw = snap.getJSONObject("raw")
        val store = tmpStore()
        val s = PremiumCaptureSession(store)
        val session = snap.getString("session")
        val r = s.observeSlot("NF", session, raw, "09:20", "09:20",
            snap.optString("requested_underlying_key").ifEmpty { null }, 1000L, 1250L,
            claimedLot = 75, claimedLotSource = "CONTRACT_LOT_TABLE",
            lotTableRef = "lot_table@2026-10-01", mapRef = "map@2026-10-01")
        assertEquals("OBSERVED", r.status)
        assertEquals(6, r.written)
        assertEquals(1, store.envelopeCount("NF", session))

        val recs = store.readAll("NF", session)
        val envs = store.readEnvelopes("NF", session)
        assertEquals(6, recs.size)
        val recSizes = recs.map { it.toString().toByteArray(Charsets.UTF_8).size }
        val envSize = envs.first().toString().toByteArray(Charsets.UTF_8).size
        // every record and envelope fits the per-record cap, which was NOT enlarged
        (recSizes + envSize).forEach {
            assertTrue("$it B exceeds ${PremiumCaptureStore.MAX_BYTES_PER_RECORD}",
                it <= PremiumCaptureStore.MAX_BYTES_PER_RECORD)
        }
        // and the full 78-slot projection fits the session budget, reserve excluded
        val perSlot = recSizes.sum() + envSize
        val budget = PremiumCaptureStore.MAX_BYTES_PER_INDEX_SESSION -
            PremiumCaptureStore.SUMMARY_RESERVE_BYTES
        assertTrue("projected 78 slots = ${perSlot * 78L} B exceeds $budget B",
            perSlot * 78L < budget)
        // the contract-slot outcome cap still admits six per slot for all 78 slots
        assertEquals(PremiumCaptureStore.MAX_OUTCOMES_PER_INDEX_SESSION,
            PremiumCaptureCohort.CONTRACTS_PER_COHORT * 78)
        println("MEASURED(desktop JVM, canonical encoding): records=$recSizes envelope=$envSize " +
            "perSlot=$perSlot projected78Slots=${perSlot * 78L} budget=$budget " +
            "sessionBytesAfter1Slot=${store.sessionBytes("NF", session)}")
    }

    @Test
    fun theFlatRecordThatDidNotFitIsRecordedAsTheReasonForTheSplit() {
        // the measured 2427 B flat record is why the per-acquisition facts moved to the envelope.
        // This pins that the split is real: a record must not carry them back inline.
        val snap = rawFixture("mon_0920_entry_NF")
        val store = tmpStore()
        val s = PremiumCaptureSession(store)
        s.observeSlot("NF", snap.getString("session"), snap.getJSONObject("raw"), "09:20",
            "09:20", null, 1L, 2L)
        val rec = store.readAll("NF", snap.getString("session")).first()
        listOf("clocks", "underlying", "lot", "cohort_rule_version").forEach {
            assertFalse("$it belongs to the slot envelope, not every record", rec.has(it))
        }
        val env = store.readEnvelopes("NF", snap.getString("session")).first()
        listOf("clocks", "underlying", "lot").forEach { assertTrue(env.has(it)) }
        assertEquals(PremiumObservationContract.ENVELOPE_VERSION,
            env.getString("record_version"))
    }

    @Test
    fun theRealFixtureCarriesTheIvAndBookEvidenceWeClaim() {
        val snap = rawFixture("mon_0920_entry_NF")
        val row = snap.getJSONObject("raw").getJSONArray("data").getJSONObject(0)
        val ce = row.getJSONObject("call_options")
        val iv = PremiumObservationContract.ivEvidence(ce)
        // the vendor fixture carries option_greeks.iv but no market_data.iv: recorded accurately
        assertEquals("GREEKS_PRESENT_ONLY", iv.getString("iv_source"))
        assertEquals(PremiumObservationContract.PRESENT_VALID,
            iv.getJSONObject("iv_greeks_raw").getString("status"))
        assertEquals(PremiumObservationContract.ABSENT,
            iv.getJSONObject("iv_market_data_raw").getString("status"))
        assertTrue(iv.getJSONObject("iv_market_data_raw").isNull("raw"))
        val bbo = PremiumObservationContract.bboEvidence(ce)
        assertEquals(PremiumObservationContract.BOOK_TWO_SIDED, bbo.getString("book_status"))
    }
}
