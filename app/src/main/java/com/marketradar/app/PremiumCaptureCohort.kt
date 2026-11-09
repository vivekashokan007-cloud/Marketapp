package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject

/**
 * Pins the exact option contracts observed for one index/session, and validates every identity
 * claim before anything is written.
 *
 * ## The rule
 *
 * **Three paired strikes**: the three strikes nearest the acquisition's own spot that carry BOTH a
 * call and a put instrument key, giving six contracts — 3 strikes x {CE, PE}. Equal distance breaks
 * to the lower strike.
 *
 * ## P5/P6 corrections (Codex review R1)
 *
 * The previous version chose "three strikes per side" by distance and derived the option type from
 * whether the strike sat above or below the spot. That could not produce a pair at all: the
 * supplied synthetic export contained `24550 PE, 24600 PE, 24650 CE, 24650 CE, 24700 CE,
 * 24750 CE` — five unique keys, a duplicated contract, and **zero** paired strikes, while every
 * hash verified. Hash validity says nothing about whether a cohort obeys its own rule, so the rule
 * is now enforced in code and asserted in tests.
 *
 * Also corrected, each a real hole:
 *  - the index label was trusted independently of the underlying key, so a Bank Nifty response
 *    could be stored under `NF` if the caller passed matching BNF keys with the wrong label;
 *  - duplicate or contradictory rows for one strike were resolved by first occurrence;
 *  - CE and PE keys were not required to be distinct, nor the six keys unique;
 *  - the experiment window was checked only while SELECTING, so once a cohort was pinned an
 *    acquisition at or after 15:05 still produced observations;
 *  - `schedulerSlot` was any non-empty string, unvalidated and never compared with the
 *    acquisition clock;
 *  - the cohort carried no integrity stamp, although observation identity resolves through it.
 *
 * Invalid or missing QUOTES never reject a correctly identified cohort — recording those is the
 * point of the capture. Only IDENTITY problems refuse.
 */
object PremiumCaptureCohort {

    const val COHORT_RULE_VERSION = "premium_capture_cohort_v2_three_paired_strikes_validated"
    const val PAIRED_STRIKES = 3
    const val CONTRACTS_PER_COHORT = PAIRED_STRIKES * 2

    /** Nominal experiment window, inclusive of the open and EXCLUSIVE of the close. */
    const val WINDOW_OPEN_MIN = 9 * 60 + 20     // 09:20
    const val WINDOW_CLOSE_MIN = 15 * 60 + 5    // 15:05

    /**
     * The only index labels this capture accepts, each bound to its underlying key. An index label
     * is never trusted on its own: the response row's `underlying_key` must match, and so must the
     * caller's requested key when one is supplied.
     */
    val INDEX_UNDERLYING = mapOf(
        "NF" to "NSE_INDEX|Nifty 50",
        "BNF" to "NSE_INDEX|Nifty Bank",
    )

    const val SELECTED = "SELECTED"
    const val UNAVAILABLE_SPOT = "UNAVAILABLE_NO_VALID_SPOT"
    const val UNAVAILABLE_IDENTITY = "UNAVAILABLE_INSUFFICIENT_IDENTITY"
    const val INCOMPLETE_PAIRED_STRIKES = "INCOMPLETE_PAIRED_STRIKES"
    const val OUTSIDE_WINDOW = "OUTSIDE_EXPERIMENT_WINDOW"
    const val ALREADY_PINNED = "ALREADY_PINNED"
    const val UNKNOWN_INDEX = "UNKNOWN_INDEX_LABEL"
    const val INDEX_UNDERLYING_MISMATCH = "INDEX_UNDERLYING_MISMATCH"
    const val MIXED_EXPIRY = "MIXED_EXPIRY_IN_RESPONSE"
    const val CONTRADICTORY_ROWS = "CONTRADICTORY_ROWS_FOR_STRIKE"
    const val DUPLICATE_INSTRUMENT_KEYS = "DUPLICATE_INSTRUMENT_KEYS"

    data class PinnedContract(
        val index: String,
        val instrumentKey: String,
        val underlyingKey: String,
        val expiry: String,
        val strike: Double,
        val optionType: String,       // "CE" | "PE"
    ) {
        fun json(): JSONObject = JSONObject()
            .put("index", index)
            .put("instrument_key", instrumentKey)
            .put("underlying_key", underlyingKey)
            .put("expiry", expiry)
            .put("strike", strike)
            .put("option_type", optionType)

        companion object {
            fun from(o: JSONObject) = PinnedContract(
                index = o.optString("index"),
                instrumentKey = o.optString("instrument_key"),
                underlyingKey = o.optString("underlying_key"),
                expiry = o.optString("expiry"),
                strike = o.optDouble("strike", Double.NaN),
                optionType = o.optString("option_type"),
            )
        }
    }

    data class Selection(val status: String, val cohort: JSONObject?, val reason: String? = null)

    private fun finitePositive(o: JSONObject?, key: String): Double? {
        val d = when (val v = o?.opt(key)) {
            is Number -> v.toDouble()
            is String -> v.trim().toDoubleOrNull()
            else -> null
        }
        return d?.takeIf { it.isFinite() && it > 0.0 }
    }

    fun minuteOfDay(hhmm: String?): Int? {
        val p = (hhmm ?: "").trim().split(":")
        if (p.size != 2 || p[0].length !in 1..2 || p[1].length != 2) return null
        val h = p[0].toIntOrNull() ?: return null
        val m = p[1].toIntOrNull() ?: return null
        if (h !in 0..23 || m !in 0..59) return null
        return h * 60 + m
    }

    /**
     * True only inside `[WINDOW_OPEN_MIN, WINDOW_CLOSE_MIN)`. Enforced on EVERY attempt, not only
     * at selection — the old code let a pinned cohort keep producing observations after 15:05.
     */
    fun inWindow(hhmm: String?): Boolean {
        val mod = minuteOfDay(hhmm) ?: return false
        return mod >= WINDOW_OPEN_MIN && mod < WINDOW_CLOSE_MIN
    }

    /** The nominal five-minute slot an acquisition clock falls in. */
    fun derivedSlot(hhmm: String?): String? {
        val mod = minuteOfDay(hhmm) ?: return null
        val floored = mod - (mod % 5)
        return "%02d:%02d".format(floored / 60, floored % 60)
    }

    data class SlotIdentity(val slot: String, val basis: String, val actualClock: String?,
                            val disagreement: String? = null)

    /**
     * Resolve the slot an acquisition belongs to.
     *
     * P5: `schedulerSlot` used to be accepted as any non-empty string and was never compared with
     * the acquisition clock, so a mislabelled slot silently relabelled real quotes. It must now be
     * a valid HH:MM on the five-minute grid, and when an acquisition clock is also present the two
     * must agree — a disagreement is REPORTED rather than resolved by preferring one of them.
     */
    fun slotIdentity(schedulerSlot: String?, acquisitionHhmm: String?): SlotIdentity? {
        val s = (schedulerSlot ?: "").trim()
        val derived = derivedSlot(acquisitionHhmm)
        if (s.isEmpty()) {
            val d = derived ?: return null
            return SlotIdentity(d, "DERIVED_FROM_ACQUISITION_CLOCK", acquisitionHhmm?.trim())
        }
        val sMin = minuteOfDay(s)
            ?: return SlotIdentity(s, "SCHEDULER_SLOT_INVALID", acquisitionHhmm?.trim(),
                "scheduler slot '$s' is not a valid HH:MM")
        if (sMin % 5 != 0)
            return SlotIdentity(s, "SCHEDULER_SLOT_INVALID", acquisitionHhmm?.trim(),
                "scheduler slot '$s' is not on the five-minute grid")
        if (derived != null && derived != s)
            return SlotIdentity(s, "SCHEDULER_SLOT_DISAGREES_WITH_CLOCK", acquisitionHhmm?.trim(),
                "scheduler slot '$s' but the acquisition clock falls in '$derived'")
        return SlotIdentity(s, "SCHEDULER_SLOT", acquisitionHhmm?.trim())
    }

    /** Pin the cohort from ONE response, validating every identity claim it carries. */
    fun select(rawChain: JSONObject?, index: String, sessionDate: String,
               acquisitionHhmm: String?, requestedUnderlyingKey: String?,
               existingCohort: JSONObject? = null): Selection {
        if (existingCohort != null) return Selection(ALREADY_PINNED, existingCohort)
        val idx = index.trim().uppercase()
        val expectedUnderlying = INDEX_UNDERLYING[idx]
            ?: return Selection(UNKNOWN_INDEX, null,
                "index '$index' is not one of ${INDEX_UNDERLYING.keys}")
        if (!inWindow(acquisitionHhmm))
            return Selection(OUTSIDE_WINDOW, null, "acquisition=$acquisitionHhmm")
        val requested = (requestedUnderlyingKey ?: "").trim()
        if (requested.isNotEmpty() && requested != expectedUnderlying)
            return Selection(INDEX_UNDERLYING_MISMATCH, null,
                "caller asked for '$requested' under index '$idx', which is '$expectedUnderlying'")

        val data = rawChain?.optJSONArray("data")
        if (data == null || data.length() == 0)
            return Selection(UNAVAILABLE_IDENTITY, null, "no_data_rows")

        // identity must come from THIS response, never from a cache or from the caller alone
        var spot: Double? = null
        var expiry = ""
        for (i in 0 until data.length()) {
            val row = data.optJSONObject(i) ?: continue
            val rowUnderlying = row.optString("underlying_key", "").trim()
            if (rowUnderlying.isNotEmpty() && rowUnderlying != expectedUnderlying)
                return Selection(INDEX_UNDERLYING_MISMATCH, null,
                    "row carries '$rowUnderlying' but index '$idx' is '$expectedUnderlying'")
            val rowExpiry = row.optString("expiry", "").trim()
            if (rowExpiry.isNotEmpty()) {
                if (expiry.isEmpty()) expiry = rowExpiry
                else if (expiry != rowExpiry)
                    return Selection(MIXED_EXPIRY, null, "'$expiry' and '$rowExpiry'")
            }
            if (spot == null) spot = finitePositive(row, "underlying_spot_price")
        }
        val s = spot ?: return Selection(UNAVAILABLE_SPOT, null,
            "no usable underlying_spot_price on the response")
        if (expiry.isEmpty())
            return Selection(UNAVAILABLE_IDENTITY, null, "no expiry on the response")

        // one candidate per strike; a CONTRADICTION refuses rather than taking the first row
        data class Cand(val strike: Double, val ce: String, val pe: String)
        val byStrike = LinkedHashMap<Double, Cand>()
        for (i in 0 until data.length()) {
            val row = data.optJSONObject(i) ?: continue
            val k = finitePositive(row, "strike_price") ?: continue
            val ce = row.optJSONObject("call_options")?.optString("instrument_key", "")?.trim().orEmpty()
            val pe = row.optJSONObject("put_options")?.optString("instrument_key", "")?.trim().orEmpty()
            val cand = Cand(k, ce, pe)
            val prior = byStrike[k]
            if (prior != null) {
                if (prior.ce != cand.ce || prior.pe != cand.pe)
                    return Selection(CONTRADICTORY_ROWS, null,
                        "strike $k appears more than once with different instrument keys")
                continue
            }
            byStrike[k] = cand
        }
        // only a strike carrying BOTH legs can form a pair, and a leg cannot be its own counterpart
        val paired = byStrike.values.filter {
            it.ce.isNotEmpty() && it.pe.isNotEmpty() && it.ce != it.pe
        }
        if (paired.size < PAIRED_STRIKES)
            return Selection(INCOMPLETE_PAIRED_STRIKES, null,
                "${paired.size} fully paired strikes available, need $PAIRED_STRIKES")

        val chosen = paired.sortedWith(compareBy({ Math.abs(it.strike - s) }, { it.strike }))
            .take(PAIRED_STRIKES)
            .sortedBy { it.strike }

        val pinned = JSONArray()
        val keys = HashSet<String>()
        for (c in chosen) {
            for ((type, key) in listOf("CE" to c.ce, "PE" to c.pe)) {
                if (!keys.add(key)) return Selection(DUPLICATE_INSTRUMENT_KEYS, null,
                    "instrument key '$key' appears more than once")
                pinned.put(PinnedContract(idx, key, expectedUnderlying, expiry, c.strike, type).json())
            }
        }
        if (pinned.length() != CONTRACTS_PER_COHORT)
            return Selection(INCOMPLETE_PAIRED_STRIKES, null,
                "built ${pinned.length()} contracts, need $CONTRACTS_PER_COHORT")

        val cohort = JSONObject()
            .put("cohort_rule_version", COHORT_RULE_VERSION)
            .put("cohort_id", "$sessionDate|$idx|" +
                PremiumObservationContract.ref(
                    PremiumObservationContract.sha256Hex(pinned.toString())))
            .put("session_date", sessionDate)
            .put("index", idx)
            .put("underlying_key", expectedUnderlying)
            .put("expiry", expiry)
            .put("paired_strikes", JSONArray().also { a -> chosen.forEach { a.put(it.strike) } })
            .put("selection_spot", s)
            .put("selection_slot", derivedSlot(acquisitionHhmm) ?: JSONObject.NULL)
            .put("selection_basis", "THREE_STRIKES_NEAREST_THIS_RESPONSE_SPOT_WITH_BOTH_LEGS")
            .put("contracts_expected", CONTRACTS_PER_COHORT)
            .put("pinned_contracts", pinned)
        // P4: pinned identity is pinned BY CONTENT, so altering it cannot silently reinterpret a
        // record that resolves through it — observations carry this content reference.
        return Selection(SELECTED, PremiumObservationContract.stampHash(cohort))
    }

    /**
     * Revalidate a cohort loaded from disk, on every restart.
     *
     * P3/P5: a malformed or foreign cohort must be refused explicitly and must never lead to a
     * silent reselection, which would repin different contracts mid-session while earlier records
     * still claimed the original ones. Returns null when valid, else the reason it is not.
     */
    fun validateLoaded(cohort: JSONObject?, index: String, sessionDate: String): String? {
        if (cohort == null) return "cohort_absent_or_unparseable"
        if (!PremiumObservationContract.verifyHash(cohort)) return "cohort_hash_does_not_verify"
        if (cohort.optString("cohort_rule_version") != COHORT_RULE_VERSION)
            return "cohort_rule_version_mismatch"
        val idx = index.trim().uppercase()
        if (cohort.optString("index") != idx) return "cohort_index_mismatch"
        if (cohort.optString("session_date") != sessionDate) return "cohort_session_mismatch"
        val expected = INDEX_UNDERLYING[idx] ?: return "unknown_index_label"
        if (cohort.optString("underlying_key") != expected) return "cohort_underlying_mismatch"
        val expiry = cohort.optString("expiry")
        if (expiry.isEmpty()) return "cohort_expiry_absent"
        val pinned = pinnedList(cohort)
        if (pinned.size != CONTRACTS_PER_COHORT) return "cohort_contract_count_mismatch"
        if (pinned.map { it.instrumentKey }.toSet().size != CONTRACTS_PER_COHORT)
            return "cohort_duplicate_instrument_keys"
        if (pinned.any { it.instrumentKey.isEmpty() || !it.strike.isFinite() })
            return "cohort_incomplete_pinned_identity"
        if (pinned.any { it.underlyingKey != expected }) return "cohort_pinned_underlying_mismatch"
        if (pinned.any { it.expiry != expiry }) return "cohort_pinned_expiry_mismatch"
        if (pinned.any { it.index != idx }) return "cohort_pinned_index_mismatch"
        val strikes = pinned.map { it.strike }.toSet()
        if (strikes.size != PAIRED_STRIKES) return "cohort_strike_count_mismatch"
        for (k in strikes) {
            val types = pinned.filter { it.strike == k }.map { it.optionType }.toSet()
            if (types != setOf("CE", "PE")) return "cohort_strike_not_paired"
        }
        return null
    }

    fun pinnedList(cohort: JSONObject?): List<PinnedContract> {
        val arr = cohort?.optJSONArray("pinned_contracts") ?: return emptyList()
        return (0 until arr.length()).mapNotNull { arr.optJSONObject(it)?.let(PinnedContract::from) }
    }

    /**
     * Locate a pinned contract in a response, and say WHY when it is not there.
     *
     * A pinned expiry that has vanished or changed is reported as such; the cohort is never
     * silently rolled onto a new expiry and a missing contract is never replaced with the current
     * ATM.
     */
    data class Located(val outcome: String, val row: JSONObject?, val leg: JSONObject?)

    fun locate(rawChain: JSONObject?, pinned: PinnedContract): Located {
        val data = rawChain?.optJSONArray("data")
            ?: return Located(PremiumObservationContract.OUTCOME_REQUEST_FAILED, null, null)
        var expirySeen = false
        for (i in 0 until data.length()) {
            val row = data.optJSONObject(i) ?: continue
            if (row.optString("expiry", "").trim() != pinned.expiry) continue
            expirySeen = true
            val legKey = if (pinned.optionType == "CE") "call_options" else "put_options"
            val leg = row.optJSONObject(legKey) ?: continue
            if (leg.optString("instrument_key", "").trim() == pinned.instrumentKey)
                return Located(PremiumObservationContract.OUTCOME_OBSERVED, row, leg)
        }
        return if (!expirySeen)
            Located(PremiumObservationContract.OUTCOME_EXPIRY_ROLLED, null, null)
        else Located(PremiumObservationContract.OUTCOME_NOT_IN_RESPONSE, null, null)
    }
}
