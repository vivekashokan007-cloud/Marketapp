package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject

/**
 * Exact-contract IV/quote OBSERVATION record, built from an already-fetched `/v2/option/chain`
 * response. Evidence preparation for future IV forecasting — NOT a claim of synchronised exchange
 * quotes, executable fills, or any forecast of future inputs.
 *
 * Why this does not reuse the existing helpers, which is the whole point of a separate record:
 *
 *  - `OptionChainBrainFormatter.optionIv()` prefers `option_greeks.iv`, silently falls back to
 *    `market_data.iv`, and returns **0.0** when both are absent. Missing IV becomes a number, two
 *    different vendor fields collapse into one, and which was used is not recorded.
 *  - `OptionChainBrainFormatter.optionQuote()` returns `null` for any value `<= 0`, so a vendor
 *    zero is indistinguishable from an absent field.
 *
 * Both are correct enough for the trading display they serve and are deliberately left untouched;
 * neither is usable as an evidence record. Here every source is kept separate, a vendor zero is
 * retained as OBSERVED-but-invalid, and absence is ABSENT — never a fabricated zero.
 *
 * Pure: org.json + java.security only. No Android API, no network, no clock of its own.
 */
object PremiumObservationContract {

    /**
     * Why this record is split in two.
     *
     * A single flat record carrying identity + clocks + underlying + lot + iv + bbo did not fit the
     * authorised 2,048 B per-record cap, and six of them across 78 slots overran the 1,032,192 B
     * session budget — over on both. The redundancy was structural rather than incidental: clocks,
     * underlying, lot and the pinned identity are IDENTICAL for all six contracts in one
     * acquisition. So the per-acquisition facts are written once as a slot envelope and the
     * per-contract records carry only what varies, joined on `(session_date, index, slot)`. That
     * fits inside the same caps, which are NOT enlarged.
     *
     * Where the numbers come from, because they decayed once already. On the synthetic mirror
     * (`python3 -I tools/premium_capture_vectors.py --sizes`): flat 2,550 B, 78 slots 1,193,400 B;
     * after the split observation 1,441 B, envelope 1,274 B, per slot 9,920 B, 78 slots 773,760 B.
     * An earlier revision of this comment quoted 2,427 / 1,067 / 1,018 / 7,175 / 559,650 B from a
     * throwaway script that was not kept, so those figures are NOT reproducible and are retracted.
     *
     * The AUTHORITY is neither set: `PremiumCaptureTest.measuredRecordSizesAgainstTheCommitted` +
     * `RawVendorFixture` measures THIS code against the real committed vendor fixture, asserts the
     * caps and prints what it measured. All of it is desktop JVM/Python; none is a device
     * measurement.
     */
    const val RECORD_VERSION = "mr.premium_observation.v4"
    const val ENVELOPE_VERSION = "mr.premium_slot_envelope.v3"
    const val CANONICAL_SERIALIZATION = "json_sorted_keys_compact_utf8_v1"

    /**
     * Linkage reference length. A binding reference is the first 32 hex characters (128 bits) of a
     * SHA-256, which is ample to bind a record to one acquisition and keeps the record inside the
     * unchanged 2 KiB cap. `payload_sha256` is never truncated.
     */
    const val REF_HEX_LEN = 32
    fun ref(hex: String): String = hex.take(REF_HEX_LEN)
    /** `payload_sha256` is computed over the record WITHOUT this field, and excludes itself. */
    const val HASH_OMITTED_FIELD = "payload_sha256"

    // ── per-source value status ────────────────────────────────────────────────────────────────
    // ABSENT and OBSERVED_* are deliberately different things: one is "the vendor sent nothing",
    // the others are "the vendor sent this, and it is not a usable sigma". Codex: an actually
    // supplied zero, negative, malformed or non-finite value must not silently become "absent"
    // nor a valid sigma.
    const val ABSENT = "ABSENT"
    const val PRESENT_VALID = "PRESENT_VALID"
    const val OBSERVED_ZERO = "OBSERVED_ZERO"
    const val OBSERVED_NEGATIVE = "OBSERVED_NEGATIVE"
    const val OBSERVED_NON_FINITE = "OBSERVED_NON_FINITE"
    const val OBSERVED_MALFORMED = "OBSERVED_MALFORMED"

    // ── book status ────────────────────────────────────────────────────────────────────────────
    const val BOOK_TWO_SIDED = "TWO_SIDED"      // finite positive bid and ask, ask >= bid
    const val BOOK_CROSSED = "CROSSED"          // finite positive both, ask < bid
    const val BOOK_ONE_SIDED = "ONE_SIDED"      // exactly one usable side
    const val BOOK_INVALID = "INVALID"          // present but not usable (zero/negative/non-finite)
    const val BOOK_EMPTY = "EMPTY"              // market_data present, both sides absent
    const val BOOK_MISSING = "MISSING"          // no market_data at all

    const val UNIT_UNKNOWN = "UNKNOWN"          // never normalised without cited verification

    /** JSON-safe encoding of a non-finite double. NaN/Infinity are not valid JSON numbers. */
    const val NON_FINITE_NAN = "NON_FINITE_NAN"
    const val NON_FINITE_POS_INF = "NON_FINITE_POSITIVE_INFINITY"
    const val NON_FINITE_NEG_INF = "NON_FINITE_NEGATIVE_INFINITY"

    /** One raw scalar as observed, with its own validity status. `raw` is never invented. */
    data class RawScalar(val raw: Any?, val status: String) {
        fun json(): JSONObject = JSONObject()
            .put("raw", raw ?: JSONObject.NULL)
            .put("status", status)
    }

    /**
     * Read one vendor scalar WITHOUT collapsing anything.
     *
     * A missing or JSON-null field is ABSENT with `raw = null`. Anything present is retained: a
     * number as itself, a numeric string as itself (the original representation is preserved, not
     * reformatted), a non-finite as a documented JSON-safe sentinel string, and anything
     * unparseable as OBSERVED_MALFORMED with the original value kept when it is JSON-safe.
     */
    fun rawScalar(src: JSONObject?, key: String): RawScalar {
        if (src == null || !src.has(key) || src.isNull(key)) return RawScalar(null, ABSENT)
        val v = src.opt(key)
        val d: Double? = when (v) {
            is Number -> v.toDouble()
            is String -> v.trim().toDoubleOrNull()
            else -> null
        }
        if (d == null) {
            // present but not a number at all: keep a JSON-safe copy of what arrived
            val safe = if (v is String || v is Boolean) v else v?.toString()
            return RawScalar(safe, OBSERVED_MALFORMED)
        }
        if (d.isNaN()) return RawScalar(NON_FINITE_NAN, OBSERVED_NON_FINITE)
        if (d == Double.POSITIVE_INFINITY) return RawScalar(NON_FINITE_POS_INF, OBSERVED_NON_FINITE)
        if (d == Double.NEGATIVE_INFINITY) return RawScalar(NON_FINITE_NEG_INF, OBSERVED_NON_FINITE)
        // the ORIGINAL representation is kept (a string stays a string), so nothing is reformatted
        val keep: Any = if (v is Number) v else (v as? String) ?: d
        return when {
            d == 0.0 -> RawScalar(keep, OBSERVED_ZERO)
            d < 0.0 -> RawScalar(keep, OBSERVED_NEGATIVE)
            else -> RawScalar(keep, PRESENT_VALID)
        }
    }

    private fun usable(s: RawScalar): Double? {
        if (s.status != PRESENT_VALID) return null
        return when (val r = s.raw) {
            is Number -> r.toDouble().takeIf { it.isFinite() && it > 0.0 }
            is String -> r.trim().toDoubleOrNull()?.takeIf { it.isFinite() && it > 0.0 }
            else -> null
        }
    }

    /**
     * Both raw IV sources, kept separate, with a presence/provenance description.
     *
     * `iv_source` describes WHICH FIELDS WERE PRESENT. It does not nominate an authoritative
     * source, and nothing downstream may read it as one. `iv_unit_status` stays UNKNOWN: a raw
     * difference between two present values is a raw-field comparison only while unit and method
     * compatibility are unverified, so no economically meaningful disagreement is claimed and no
     * normalisation to FRACTION or PERCENT is performed.
     */
    fun ivEvidence(leg: JSONObject?): JSONObject {
        val greeks = leg?.optJSONObject("option_greeks")
        val md = leg?.optJSONObject("market_data")
        val g = rawScalar(greeks, "iv")
        val m = rawScalar(md, "iv")
        val gPresent = g.status != ABSENT
        val mPresent = m.status != ABSENT
        val source = when {
            gPresent && mPresent -> "BOTH_PRESENT"
            gPresent -> "GREEKS_PRESENT_ONLY"
            mPresent -> "MARKET_DATA_PRESENT_ONLY"
            else -> "ABSENT"
        }
        val out = JSONObject()
            .put("iv_greeks_raw", g.json())
            .put("iv_market_data_raw", m.json())
            .put("iv_source", source)
            .put("iv_unit_status", UNIT_UNKNOWN)
            .put("iv_authoritative_resolved", false)
            .put("iv_unit_verification", JSONObject.NULL)
        // a raw-field comparison, explicitly labelled as such and only when both are usable
        val gv = usable(g)
        val mv = usable(m)
        out.put("iv_raw_field_comparison", if (gv != null && mv != null)
            JSONObject().put("greeks_minus_market_data", gv - mv)
                .put("basis", "RAW_FIELD_DIFFERENCE_UNITS_UNVERIFIED")
                .put("economically_meaningful", false)
        else JSONObject.NULL)
        return out
    }

    /**
     * Bid/ask/ltp and quantities, each retained separately with its own status, plus a book state.
     *
     * No LTP fallback and no invented executable midpoint: `ltp` is recorded as its own field and
     * is never substituted for a missing side. Quantity units are not documented by the vendor
     * response, so they stay unknown rather than being assumed to be lots or contracts.
     */
    fun bboEvidence(leg: JSONObject?): JSONObject {
        val md = leg?.optJSONObject("market_data")
        val bid = rawScalar(md, "bid_price")
        val ask = rawScalar(md, "ask_price")
        val bidQty = rawScalar(md, "bid_qty")
        val askQty = rawScalar(md, "ask_qty")
        val ltp = rawScalar(md, "ltp")
        val bv = usable(bid)
        val av = usable(ask)
        val status = when {
            md == null -> BOOK_MISSING
            bv != null && av != null -> if (av >= bv) BOOK_TWO_SIDED else BOOK_CROSSED
            bv != null || av != null -> BOOK_ONE_SIDED
            bid.status == ABSENT && ask.status == ABSENT -> BOOK_EMPTY
            else -> BOOK_INVALID       // present on at least one side but not usable
        }
        return JSONObject()
            .put("bid_raw", bid.json())
            .put("ask_raw", ask.json())
            .put("bid_qty_raw", bidQty.json())
            .put("ask_qty_raw", askQty.json())
            .put("ltp_raw", ltp.json())
            .put("book_status", status)
            .put("quantity_unit_status", UNIT_UNKNOWN)
            .put("executable_mid_computed", false)
            .put("ltp_used_as_quote_fallback", false)
    }

    /**
     * Pinned contract identity against what the response actually carried, with disagreement made
     * explicit. Nothing is ever paired across an index or an expiry.
     */
    fun identityEvidence(pinned: PremiumCaptureCohort.PinnedContract,
                         row: JSONObject?, leg: JSONObject?, lean: Boolean = false): JSONObject {
        val rowUnderlying = row?.optString("underlying_key", "")?.trim().orEmpty()
        val rowExpiry = row?.optString("expiry", "")?.trim().orEmpty()
        val rowStrike = rawScalar(row, "strike_price")
        val legKey = leg?.optString("instrument_key", "")?.trim().orEmpty()
        val disagreements = JSONArray()
        if (legKey.isNotEmpty() && legKey != pinned.instrumentKey) disagreements.put("instrument_key")
        if (rowUnderlying.isNotEmpty() && rowUnderlying != pinned.underlyingKey) disagreements.put("underlying_key")
        if (rowExpiry.isNotEmpty() && rowExpiry != pinned.expiry) disagreements.put("expiry")
        val rsv = usable(rowStrike)
        if (rsv != null && rsv != pinned.strike) disagreements.put("strike_price")

        // POSITIVE EVIDENCE, not absence of contradiction. Codex P5: `identity_verified` was true
        // whenever nothing contradicted the pinned tuple, so a response that carried no
        // underlying_key, no expiry and no strike_price "verified" on the leg key alone. Each
        // field must now be PRESENT AND EQUAL, and anything absent is named rather than excused.
        val missing = JSONArray()
        if (legKey.isEmpty()) missing.put("instrument_key")
        if (rowUnderlying.isEmpty()) missing.put("underlying_key")
        if (rowExpiry.isEmpty()) missing.put("expiry")
        if (rsv == null) missing.put("strike_price")
        val verified = disagreements.length() == 0 && missing.length() == 0

        // The lean form omits the pinned tuple, which is byte-identical to the cohort entry this
        // record already names. The record carries `cohort_ref` so the cohort it resolves through
        // is pinned by content, not merely named.
        if (lean) return JSONObject()
            .put("observed_strike_raw", rowStrike.json())
            .put("identity_disagreements", disagreements)
            .put("identity_evidence_missing", missing)
            .put("identity_verified", verified)
            .put("pinned_resolves_via", "cohort.pinned_contracts[instrument_key]")
        return JSONObject()
            .put("identity_evidence_missing", missing)
            .put("pinned_instrument_key", pinned.instrumentKey)
            .put("pinned_underlying_key", pinned.underlyingKey)
            .put("pinned_index", pinned.index)
            .put("pinned_expiry", pinned.expiry)
            .put("pinned_strike", pinned.strike)
            .put("pinned_option_type", pinned.optionType)
            // An explicit conditional, so an absent field is JSON null rather than "".
            // CORRECTION: an earlier version of this comment claimed `ifEmpty { JSONObject.NULL }`
            // would not typecheck here. That was wrong. `ifEmpty` is
            // `fun <C, R> C.ifEmpty(defaultValue: () -> R): R where C : CharSequence, C : R`, so R
            // infers to a supertype of String (Any) and the call compiles. The conditional is kept
            // only because it reads more plainly, not because the alternative was invalid.
            .put("observed_instrument_key", if (legKey.isEmpty()) JSONObject.NULL else legKey)
            .put("observed_underlying_key",
                if (rowUnderlying.isEmpty()) JSONObject.NULL else rowUnderlying)
            .put("observed_expiry", if (rowExpiry.isEmpty()) JSONObject.NULL else rowExpiry)
            .put("observed_strike_raw", rowStrike.json())
            .put("identity_disagreements", disagreements)
            .put("identity_verified", verified)
    }

    /**
     * Lot evidence. CONSTANT is NOT an accepted resolved source for this contract: a hardcoded
     * fallback is a producer claim, not an authoritative lot, so it is recorded UNVERIFIED and the
     * authoritative lot is left unresolved. Dated map/table references are retained so the actual
     * data resolves offline in the export.
     */
    /**
     * A REAL calendar date in `YYYY-MM-DD`, not merely a date-SHAPED substring.
     *
     * R2-2 (Codex): the previous check was `Regex(".*\\d{4}-\\d{2}-\\d{2}.*")`, which accepts
     * `9999-99-99`, `0000-00-00` and `junk-1234-56-78`. A matching substring was then treated as
     * dated evidence and promoted to authority.
     */
    fun isCalendarDate(s: String?): Boolean {
        val t = (s ?: "").trim()
        if (!Regex("^\\d{4}-\\d{2}-\\d{2}$").matches(t)) return false
        return runCatching { java.time.LocalDate.parse(t) }.isSuccess
    }

    /** The last `YYYY-MM-DD` inside a reference name, if it is a real date. */
    fun dateInReference(ref: String?): String? {
        val m = Regex("\\d{4}-\\d{2}-\\d{2}").findAll(ref ?: "").lastOrNull()?.value
        return m?.takeIf { isCalendarDate(it) }
    }

    // lot resolution verdicts. Presence, reference resolution and AUTHORITY are three things.
    const val LOT_UNRESOLVED_NO_METADATA = "UNRESOLVED_NO_METADATA_SUPPLIED"
    const val LOT_UNRESOLVED_REF_MISMATCH = "UNRESOLVED_TABLE_REFERENCE_MISMATCH"
    const val LOT_UNRESOLVED_NO_ROW = "UNRESOLVED_NO_ROW_FOR_UNDERLYING"
    const val LOT_UNRESOLVED_NOT_EFFECTIVE = "UNRESOLVED_NOT_EFFECTIVE_AT_SESSION"
    const val LOT_UNRESOLVED_INVALID_ROW = "UNRESOLVED_TABLE_ROW_LOT_INVALID"
    const val LOT_UNRESOLVED_AMBIGUOUS_ROWS = "UNRESOLVED_AMBIGUOUS_OVERLAPPING_ROWS"
    const val LOT_UNRESOLVED_BAD_REFERENCE = "UNRESOLVED_REFERENCE_NOT_DATED"
    const val LOT_CONFLICTS_WITH_CLAIM = "RESOLVED_BUT_CONFLICTS_WITH_PRODUCER_CLAIM"
    const val LOT_RESOLVED = "RESOLVED_FROM_DATED_TABLE_ROW"

    /**
     * The RAW lot claim and its references. **This never confers authority.**
     *
     * R2-2: the previous version set `lot_authoritative_resolved = true` from an allowed caller
     * source string, a positive integer and a date-shaped reference — none of which resolves a
     * table row or verifies an effective date. Authority now requires `resolveLotFromTable` to
     * validate the named reference, the underlying's applicability, an integral positive lot and
     * an effective/as-of date, and that happens where the actual metadata exists (at export).
     * Here the claim is recorded as a claim, with the shape checks published separately so a
     * reader can see exactly what was and was not established.
     */
    fun lotEvidence(claimedLot: Any?, claimedSource: String?, tableRef: String?,
                    mapRef: String?, productionAuthorityRef: String? = null): JSONObject {
        val src = (claimedSource ?: "").trim().uppercase()
        val lot = rawScalar(JSONObject().put("v", claimedLot ?: JSONObject.NULL), "v")
        val value = usable(lot)
        val integral = value != null && value == Math.floor(value) && value >= 1.0
        val ref = (tableRef ?: "").trim()
        val refDate = dateInReference(ref)
        return JSONObject()
            .put("lot_claimed_raw", lot.json())
            .put("lot_claimed_source", if (src.isEmpty()) JSONObject.NULL else src)
            // shape checks on the CLAIM, which are necessary but never sufficient
            .put("claimed_source_in_accepted_set",
                src == "CHAIN_METADATA" || src == "CONTRACT_LOT_TABLE")
            .put("claimed_lot_positive_integer", integral)
            .put("reference_carries_real_date", refDate != null)
            .put("reference_date", refDate ?: JSONObject.NULL)
            // authority is NOT decided here, and is never decided from a caller label
            .put("lot_status", "PRODUCER_CLAIM_UNRESOLVED")
            .put("lot_authoritative_resolved", false)
            .put("lot_resolution", LOT_UNRESOLVED_NO_METADATA)
            .put("lot_authority_requires",
                "RESOLVER_VALIDATED_TABLE_ROW_FOR_THIS_UNDERLYING_EFFECTIVE_AT_THE_SESSION_DATE")
            .put("contract_lot_table_ref", if (ref.isEmpty()) JSONObject.NULL else ref)
            .put("map_ref", mapRef ?: JSONObject.NULL)
            /*
             * A CONTENT REFERENCE to the application's own contract authority
             * (`ContractLotTable`), recorded once per index/session and resolved from the export.
             *
             * Codex: that table resolves by index, expiry, cycle and observation date because
             * different contracts coexist, so the generic underlying+session resolver below must
             * NOT be substituted for it, and its result must not be flattened into one number per
             * underlying. The authority itself keeps its version, source, matched rule and as-of
             * date, and is the only path to a PRODUCTION lot authority claim.
             *
             * It is a reference rather than the object because the object is a per-index/session
             * fact and inlining it per slot pushed the envelope past the 2 KiB per-record cap —
             * CI caught that as every observation being refused. The cap was not enlarged.
             */
            .put("production_authority_ref", productionAuthorityRef ?: JSONObject.NULL)
    }

    /**
     * Resolve a lot against an ACTUAL dated table, for one underlying as of one session.
     *
     * Expected bounded table shape, which the accepted fixture must label as test evidence:
     * ```
     * {"ref":"contract_lot_table/NSE/2026-10-01","provenance":"CONSTRUCTED_TEST_TABLE",
     *  "rows":[{"underlying_key":"NSE_INDEX|Nifty 50","lot":75,
     *           "effective_from":"2026-10-01","effective_to":null}]}
     * ```
     * A conflicting table value does NOT silently win and does not grant authority: both values
     * are published and the verdict says they disagree.
     */
    fun resolveLotFromTable(table: JSONObject?, expectedRef: String?, underlyingKey: String,
                            asOfSession: String, claimedLot: Any?): JSONObject {
        val out = JSONObject()
            .put("expected_table_ref", expectedRef ?: JSONObject.NULL)
            .put("underlying_key", underlyingKey)
            .put("as_of_session", asOfSession)
        fun verdict(v: String, extra: String? = null): JSONObject = out
            .put("lot_resolution", v)
            .put("lot_authoritative_resolved", v == LOT_RESOLVED)
            .put("resolved_lot", out.opt("resolved_lot") ?: JSONObject.NULL)
            .put("detail", extra ?: JSONObject.NULL)
        if (table == null) return verdict(LOT_UNRESOLVED_NO_METADATA)
        if (!isCalendarDate(asOfSession))
            return verdict(LOT_UNRESOLVED_NOT_EFFECTIVE, "as-of session '$asOfSession' is not a date")
        val want = (expectedRef ?: "").trim()
        if (want.isEmpty() || dateInReference(want) == null)
            return verdict(LOT_UNRESOLVED_BAD_REFERENCE,
                "the envelope's table reference carries no real date")
        val have = table.optString("ref", "").trim()
        out.put("supplied_table_ref", if (have.isEmpty()) JSONObject.NULL else have)
            .put("supplied_table_provenance",
                table.optString("provenance", "").ifEmpty { "UNDECLARED" })
        if (have != want) return verdict(LOT_UNRESOLVED_REF_MISMATCH,
            "supplied table is '$have', the records name '$want'")
        val rows = table.optJSONArray("rows") ?: JSONArray()
        val asOf = java.time.LocalDate.parse(asOfSession)
        // ALL effective rows are collected, not the first match: Codex requires ambiguous
        // overlapping rows to be rejected rather than silently resolved by document order.
        val effective = ArrayList<JSONObject>()
        for (i in 0 until rows.length()) {
            val r = rows.optJSONObject(i) ?: continue
            if (r.optString("underlying_key").trim() != underlyingKey) continue
            val from = r.optString("effective_from", "").trim()
            val to = r.optString("effective_to", "").trim()
            if (!isCalendarDate(from)) continue
            if (java.time.LocalDate.parse(from).isAfter(asOf)) continue
            if (to.isNotEmpty()) {
                if (!isCalendarDate(to)) continue
                if (java.time.LocalDate.parse(to).isBefore(asOf)) continue
            }
            effective.add(r)
        }
        if (effective.size > 1) {
            val distinct = effective.map { it.opt("lot")?.toString() }.toSet()
            out.put("overlapping_effective_rows", effective.size)
            if (distinct.size > 1) return verdict(LOT_UNRESOLVED_AMBIGUOUS_ROWS,
                "${effective.size} overlapping effective rows disagree: $distinct")
        }
        val row = effective.firstOrNull() ?: return verdict(LOT_UNRESOLVED_NO_ROW,
            "no row for '$underlyingKey' effective at $asOfSession among ${rows.length()} row(s)")
        val tableLot = rawScalar(row, "lot")
        val tv = usable(tableLot)
        if (tv == null || tv != Math.floor(tv) || tv < 1.0)
            return verdict(LOT_UNRESOLVED_INVALID_ROW, "table row lot is not a positive integer")
        out.put("resolved_lot", tableLot.raw)
            .put("effective_from", row.optString("effective_from"))
            .put("effective_to", row.opt("effective_to") ?: JSONObject.NULL)
        val claim = usable(rawScalar(JSONObject().put("v", claimedLot ?: JSONObject.NULL), "v"))
        out.put("producer_claimed_lot", claimedLot ?: JSONObject.NULL)
        if (claim != null && claim != tv)
            return verdict(LOT_CONFLICTS_WITH_CLAIM,
                "table says $tv, the producer claimed $claim")
        return verdict(LOT_RESOLVED)
    }

    /**
     * The underlying observation paired with this contract.
     *
     * SAME_ACQUISITION is preferred and is the only pairing that may be called synchronised with
     * the chain response, because `underlying_spot_price` is carried on the response row itself. A
     * separately observed spot is retained with its own clocks and an explicit unknown-skew
     * statement; a cached spot is never attached and called synchronized.
     */
    fun underlyingEvidence(row: JSONObject?, separateSpot: Double?, separateSourceField: String?,
                           separateSourceTs: String?, separateReceiptMs: Long?): JSONObject {
        val sameAcq = rawScalar(row, "underlying_spot_price")
        val sameUsable = usable(sameAcq)
        if (sameUsable != null) {
            return JSONObject()
                .put("basis", "SAME_ACQUISITION")
                .put("spot_raw", sameAcq.json())
                .put("source_field", "underlying_spot_price")
                .put("carried_by_chain_response", true)
                .put("synchronisation_with_quote", "SAME_RESPONSE_PAYLOAD")
                .put("skew_ms", JSONObject.NULL)
        }
        if (separateSpot == null || !separateSpot.isFinite() || separateSpot <= 0.0) {
            return JSONObject()
                .put("basis", "UNAVAILABLE")
                .put("spot_raw", sameAcq.json())
                .put("carried_by_chain_response", false)
                .put("synchronisation_with_quote", "UNKNOWN")
                .put("skew_ms", JSONObject.NULL)
        }
        return JSONObject()
            .put("basis", "SEPARATE_OBSERVATION")
            .put("spot_raw", JSONObject().put("raw", separateSpot).put("status", PRESENT_VALID))
            .put("source_field", separateSourceField ?: JSONObject.NULL)
            .put("source_ts", separateSourceTs ?: JSONObject.NULL)
            .put("receipt_ms", separateReceiptMs ?: JSONObject.NULL)
            .put("carried_by_chain_response", false)
            .put("synchronisation_with_quote", "UNKNOWN_NOT_SAME_ACQUISITION")
            .put("skew_ms", JSONObject.NULL)
    }

    /**
     * Clocks for ONE index acquisition. Local receipt, vendor-timestamp presence and vendor
     * event semantics are three separate statements: VENDOR_TS_PRESENT alone does not imply a
     * verified quote-event clock, and a missing source time stays unknown rather than being
     * filled from the local clock.
     */
    fun clockEvidence(requestStartedMs: Long?, responseReceivedMs: Long?,
                      vendorSourceTs: String?): JSONObject {
        val present = !vendorSourceTs.isNullOrBlank()
        return JSONObject()
            .put("acquisition_request_ms", requestStartedMs ?: JSONObject.NULL)
            .put("acquisition_response_ms", responseReceivedMs ?: JSONObject.NULL)
            .put("acquisition_elapsed_ms",
                if (requestStartedMs != null && responseReceivedMs != null)
                    responseReceivedMs - requestStartedMs else JSONObject.NULL)
            .put("vendor_source_ts", vendorSourceTs ?: JSONObject.NULL)
            .put("vendor_ts_status", if (present) "VENDOR_TS_PRESENT" else "VENDOR_TS_ABSENT")
            // separate from presence, and deliberately never "verified" in this delivery
            .put("vendor_event_semantics", "UNVERIFIED")
            .put("quote_event_clock_verified", false)
            .put("clock_basis", "LOCAL_ACQUISITION_RECEIPT")
    }

    /**
     * VALUE-canonical rendering of one number, so numerically equal values hash identically
     * whatever Java type they were parsed into.
     *
     * This exists because of a real defect found by CI, not as a precaution. `org.json:json:20240303`
     * parses a JSON decimal into a **BigDecimal**, so a fixture `61.0` arrived as
     * `BigDecimal("61.0")` and the old generic `is Number -> o.toString()` branch rendered it
     * `"61.0"` — while `Double` 61.0 rendered `"61"` and `Integer` 61 rendered `"61"`. Worse,
     * `JSONObject.toString()` runs org.json's `numberToString`, which TRIMS trailing zeros, so a
     * record written and reparsed came back as `Integer` 61. `stampHash` therefore hashed `61.0`
     * and `verifyHash` hashed `61`, and a freshly stamped record failed its own verification.
     *
     * Routing every `Number` through one BigDecimal normalisation fixes it at the root: integral
     * values render with no decimal point, fractional values render in plain (never exponent) form,
     * and the rendering is stable across a `toString()` / reparse round trip — which
     * `theCanonicalFormSurvivesAJsonTextRoundTrip` now asserts.
     */
    private fun canonicalNumber(n: Number): String {
        val d = n.toDouble()
        // defensive: a bare NaN/Infinity is not valid JSON and org.json refuses to hold one
        if (d.isNaN() || d.isInfinite()) return JSONObject.quote(NON_FINITE_NAN)
        val stripped = try {
            java.math.BigDecimal(n.toString()).stripTrailingZeros()
        } catch (e: NumberFormatException) {
            return JSONObject.quote(NON_FINITE_NAN)
        }
        return if (stripped.scale() <= 0) stripped.toBigInteger().toString()
        else stripped.toPlainString()
    }

    /** Canonical JSON with sorted keys, compact separators, UTF-8 — the hashed representation. */
    fun canonicalJson(o: Any?): String = when (o) {
        null, JSONObject.NULL -> "null"
        is JSONObject -> o.keys().asSequence().sorted().joinToString(",", "{", "}") { k ->
            JSONObject.quote(k) + ":" + canonicalJson(o.opt(k))
        }
        is JSONArray -> (0 until o.length()).joinToString(",", "[", "]") { canonicalJson(o.opt(it)) }
        is String -> JSONObject.quote(o)
        is Boolean -> o.toString()
        is Number -> canonicalNumber(o)
        else -> JSONObject.quote(o.toString())
    }

    fun sha256Hex(s: String): String =
        java.security.MessageDigest.getInstance("SHA-256").digest(s.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }

    /**
     * Stamp `payload_sha256` over the canonical record WITHOUT that field, and record exactly how
     * it was computed. The hash identifies the recorded bytes and identity; it does not
     * authenticate vendor truth, and says so in the record.
     */
    fun stampHash(record: JSONObject): JSONObject {
        record.remove(HASH_OMITTED_FIELD)
        // hash_basis is stamped BEFORE the digest is taken, so the description of how the record
        // was hashed is itself covered by the hash and cannot be altered undetected.
        record.put("hash_basis", JSONObject()
            .put("serialization", CANONICAL_SERIALIZATION)
            .put("omitted_field", HASH_OMITTED_FIELD)
            .put("authenticates", "RECORDED_BYTES_AND_IDENTITY_ONLY")
            .put("authenticates_vendor_truth", false))
        return record.put(HASH_OMITTED_FIELD, sha256Hex(canonicalJson(record)))
    }

    /**
     * Recompute and compare, for the export round trip. `hash_basis` IS part of the hashed content
     * (it is stamped before the digest is taken), so only the digest field itself is removed.
     */
    fun verifyHash(record: JSONObject): Boolean {
        val claimed = record.optString(HASH_OMITTED_FIELD, "")
        if (claimed.isEmpty()) return false
        val rebuilt = JSONObject(record.toString())
        rebuilt.remove(HASH_OMITTED_FIELD)
        return claimed == sha256Hex(canonicalJson(rebuilt))
    }

    // ── outcome statuses for one pinned contract in one slot ───────────────────────────────────
    const val OUTCOME_OBSERVED = "OBSERVED"
    const val OUTCOME_NOT_IN_RESPONSE = "MISSING_NOT_IN_RESPONSE"
    const val OUTCOME_REQUEST_FAILED = "MISSING_REQUEST_FAILED"
    const val OUTCOME_EXPIRY_ROLLED = "MISSING_PINNED_EXPIRY_CHANGED"
    const val OUTCOME_BEFORE_CAPTURE = "NOT_ATTEMPTED_BEFORE_CAPTURE_START"
    const val OUTCOME_SLOT_SKIPPED = "NOT_ATTEMPTED_SCHEDULER_SLOT_SKIPPED"

    /**
     * The facts of ONE index acquisition, written once per slot: the clocks, the underlying
     * observation and the lot evidence are identical for all six contracts in that acquisition, so
     * repeating them six times is redundancy, not evidence. The six observation records join to
     * this on `(session_date, index, slot)`.
     */
    fun slotEnvelope(index: String, sessionDate: String, slot: String, cohort: JSONObject,
                     clocks: JSONObject, underlying: JSONObject, lot: JSONObject,
                     batchEvidenceRef: String, requestOutcome: String,
                     attemptDistinguishable: Boolean): JSONObject {
        val body = JSONObject()
            .put("record_version", ENVELOPE_VERSION)
            .put("session_date", sessionDate)
            .put("index", index)
            .put("slot", slot)
            .put("cohort_id", cohort.optString("cohort_id"))
            .put("cohort_ref", ref(contentHash(cohort)))
            .put("clocks", clocks)
            .put("underlying", underlying)
            .put("lot", lot)
            // the QUOTE-BEARING evidence of this acquisition, by content
            .put("batch_evidence_ref", batchEvidenceRef)
            .put("request_outcome", requestOutcome)
            .put("attempt_distinguishable", attemptDistinguishable)
        return stampHash(body.put("acquisition_id", acquisitionId(body)))
    }

    const val REQUEST_RESPONSE_PRESENT = "RESPONSE_PRESENT"
    const val REQUEST_FAILED = "REQUEST_FAILED"

    /**
     * Identity of ONE acquisition, derived from its own evidence rather than assigned.
     *
     * R2-1 (Codex): the first version hashed session, index, slot, cohort ref, clocks, underlying
     * and lot — **but not the response, and not the quotes**. Since `observeSlot` accepts null
     * request/response timestamps and a scheduler-only slot, two DIFFERENT responses for one slot
     * with an unchanged spot, an unchanged lot and absent clocks produced the SAME acquisition id
     * and a byte-identical envelope. The envelope write was then a duplicate no-op, response B
     * wrote under A's identity, A's differing records became conflicts, and A's missing keys
     * received B's quotes — so the primary group combined two responses and could still be
     * reported complete. My R1 test changed the receipt clocks, so it never exercised this.
     *
     * `batch_evidence_ref` closes it: the identity now covers a digest of the complete prepared
     * six-contract evidence (each contract's identity, IV and book), computed BEFORE anything is
     * persisted. A quote-only change therefore yields a different acquisition, never a resumption
     * of the old one. The chain is non-circular — evidence digest, then acquisition id, then
     * envelope, then the records that reference it — so no hash depends on its own output.
     *
     * Idempotence is preserved: byte-identical evidence re-presented gives the same id and a
     * duplicate no-op, which is the correct outcome for a genuine retry of the same response.
     */
    fun acquisitionId(envelopeBody: JSONObject): String {
        val basis = JSONObject()
            .put("session_date", envelopeBody.opt("session_date"))
            .put("index", envelopeBody.opt("index"))
            .put("slot", envelopeBody.opt("slot"))
            .put("cohort_ref", envelopeBody.opt("cohort_ref"))
            .put("clocks", envelopeBody.opt("clocks"))
            .put("underlying", envelopeBody.opt("underlying"))
            .put("lot", envelopeBody.opt("lot"))
            .put("batch_evidence_ref", envelopeBody.opt("batch_evidence_ref"))
            .put("request_outcome", envelopeBody.opt("request_outcome"))
        return ref(sha256Hex(canonicalJson(basis)))
    }

    /**
     * The versions the current VERIFIED contract covers. Anything else is retained and exported,
     * but reported as legacy/unverified rather than silently accepted.
     *
     * Codex replayed the previous verification predicate against the earlier acceptance artifact
     * and all three of its groups satisfied it, although they carry observation v3 / envelope v2
     * and **no `batch_evidence_ref` at all**. So bumping the version labelled the rows but the
     * verifier never looked at it — my claim that the bump let the verifier distinguish the
     * identity contracts was not supported by the exporter. These sets are what make it true.
     */
    val SUPPORTED_RECORD_VERSIONS = setOf(RECORD_VERSION)
    val SUPPORTED_ENVELOPE_VERSIONS = setOf(ENVELOPE_VERSION)

    /** The five evidence fields, lifted back out of a stored record for re-digesting. */
    val EVIDENCE_FIELDS = listOf("instrument_key", "outcome", "identity", "iv", "bbo")

    /**
     * Reconstruct one contract's evidence body from a stored record, so the batch digest can be
     * RECOMPUTED at verification time instead of trusted. Linkage, version and hash fields are
     * excluded, exactly as when the record was built.
     */
    fun evidenceFromRecord(record: JSONObject): JSONObject {
        val e = JSONObject()
        for (k in EVIDENCE_FIELDS) e.put(k, record.opt(k) ?: JSONObject.NULL)
        return e
    }

    /**
     * One contract's quote-bearing evidence, with NO linkage fields — the unit the acquisition
     * digest is taken over, and the body an observation record is built from.
     */
    fun observationEvidence(pinned: PremiumCaptureCohort.PinnedContract, outcome: String,
                            row: JSONObject?, leg: JSONObject?): JSONObject {
        val e = JSONObject()
            .put("instrument_key", pinned.instrumentKey)
            .put("outcome", outcome)
            .put("identity", identityEvidence(pinned, row, leg, lean = true))
        return if (outcome == OUTCOME_OBSERVED)
            e.put("iv", ivEvidence(leg)).put("bbo", bboEvidence(leg))
        else e.put("iv", JSONObject.NULL).put("bbo", JSONObject.NULL)
    }

    /**
     * Digest of the COMPLETE prepared batch, in pinned order. Taken before persistence, so a
     * partially written batch cannot be completed from a different response under the same id.
     */
    fun batchEvidenceRef(evidence: List<JSONObject>): String {
        val arr = JSONArray()
        evidence.forEach { arr.put(it) }
        return ref(sha256Hex(canonicalJson(arr)))
    }

    /**
     * Content hash of an object, ignoring any digest/basis fields it already carries, so a stamped
     * and an unstamped copy of the same content hash identically.
     */
    fun contentHash(o: JSONObject): String {
        val body = JSONObject(o.toString())
        body.remove(HASH_OMITTED_FIELD)
        body.remove("hash_basis")
        return sha256Hex(canonicalJson(body))
    }

    /**
     * One observation record for one pinned contract at one slot, carrying only what varies
     * between the six. A complete record is produced for every pinned contract, observed or not:
     * absence is evidence, and a shorter file is not.
     */
    fun observation(evidence: JSONObject, cohort: JSONObject, sessionDate: String, index: String,
                    slot: String, envelope: JSONObject): JSONObject {
        val rec = JSONObject()
            .put("record_version", RECORD_VERSION)
            .put("session_date", sessionDate)
            .put("index", index)
            .put("slot", slot)
            .put("instrument_key", evidence.optString("instrument_key"))
            .put("outcome", evidence.optString("outcome"))
            .put("identity", evidence.opt("identity"))
            .put("iv", evidence.opt("iv"))
            .put("bbo", evidence.opt("bbo"))
            // P2/R2-1: bound to ONE acquisition, whose identity covers this batch's own quote
            // evidence, and to the envelope's content — not to whatever envelope shares the slot.
            // P4: and to the cohort BY CONTENT, so altering the cohort cannot silently reinterpret
            // which contract this record is about.
            .put("acquisition_id", envelope.optString("acquisition_id"))
            .put("envelope_ref", ref(contentHash(envelope)))
            .put("cohort_ref", ref(contentHash(cohort)))
            .put("envelope_join", "session_date+index+slot+acquisition_id")
        return stampHash(rec)
    }
}
