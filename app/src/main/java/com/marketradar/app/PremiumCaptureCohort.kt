package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject

/**
 * Causal, deterministic selection and pinning of the six contracts observed for one index in one
 * session. Reads an ALREADY-FETCHED `/v2/option/chain` response; it never requests anything, never
 * schedules anything, and never substitutes another expiry or a historical backfill.
 *
 * The 09:20–15:05 IST window defines the observation EXPERIMENT. It is not a position exit rule and
 * confers no trading permission.
 */
object PremiumCaptureCohort {

    const val COHORT_RULE_VERSION = "premium_capture_cohort_v1_three_nearest_paired_strikes"
    const val STRIKES_PER_SIDE = 3
    const val CONTRACTS_PER_COHORT = STRIKES_PER_SIDE * 2

    /** Inclusive start, exclusive end, IST minutes-of-day. */
    const val WINDOW_OPEN_MIN = 9 * 60 + 20     // 09:20
    const val WINDOW_CLOSE_MIN = 15 * 60 + 5    // 15:05

    const val SELECTED = "SELECTED"
    const val UNAVAILABLE_SPOT = "UNAVAILABLE_NO_VALID_SPOT"
    const val UNAVAILABLE_IDENTITY = "UNAVAILABLE_INSUFFICIENT_IDENTITY"
    const val INCOMPLETE_PAIRED_STRIKES = "INCOMPLETE_PAIRED_STRIKES"
    const val OUTSIDE_WINDOW = "OUTSIDE_EXPERIMENT_WINDOW"
    const val ALREADY_PINNED = "ALREADY_PINNED"

    data class PinnedContract(
        val index: String,
        val instrumentKey: String,
        val underlyingKey: String,
        val expiry: String,
        val strike: Double,
        val optionType: String,       // "CE" | "PE"
    ) {
        fun json(): JSONObject = JSONObject()
            .put("index", index).put("instrument_key", instrumentKey)
            .put("underlying_key", underlyingKey).put("expiry", expiry)
            .put("strike", strike).put("option_type", optionType)

        companion object {
            fun from(o: JSONObject) = PinnedContract(
                o.optString("index"), o.optString("instrument_key"),
                o.optString("underlying_key"), o.optString("expiry"),
                o.optDouble("strike", 0.0), o.optString("option_type"))
        }
    }

    data class Selection(val status: String, val cohort: JSONObject?, val reason: String? = null)

    private fun finitePositive(o: JSONObject?, key: String): Double? {
        if (o == null || !o.has(key) || o.isNull(key)) return null
        val d = when (val v = o.opt(key)) {
            is Number -> v.toDouble()
            is String -> v.trim().toDoubleOrNull()
            else -> null
        } ?: return null
        return d.takeIf { it.isFinite() && it > 0.0 }
    }

    /** IST minutes-of-day from an `HH:MM[:SS]` acquisition time. Null when unreadable. */
    fun minuteOfDay(hhmm: String?): Int? {
        val p = (hhmm ?: "").trim().split(":")
        if (p.size < 2) return null
        val h = p[0].toIntOrNull() ?: return null
        val m = p[1].toIntOrNull() ?: return null
        if (h !in 0..23 || m !in 0..59) return null
        return h * 60 + m
    }

    fun inWindow(hhmm: String?): Boolean {
        val mod = minuteOfDay(hhmm) ?: return false
        return mod >= WINDOW_OPEN_MIN && mod < WINDOW_CLOSE_MIN
    }

    /**
     * Canonical five-minute experiment slot, used ONLY when the scheduler supplies no stable slot
     * identity. It is labelled as derived and the actual acquisition clock is retained separately,
     * so a derived label can never be mistaken for the scheduler's own identity.
     */
    fun derivedSlot(hhmm: String?): String? {
        val mod = minuteOfDay(hhmm) ?: return null
        val floored = mod - (mod % 5)
        return "%02d:%02d".format(floored / 60, floored % 60)
    }

    data class SlotIdentity(val slot: String, val basis: String, val actualClock: String?)

    fun slotIdentity(schedulerSlot: String?, acquisitionHhmm: String?): SlotIdentity? {
        val s = (schedulerSlot ?: "").trim()
        if (s.isNotEmpty()) return SlotIdentity(s, "SCHEDULER_SLOT", acquisitionHhmm)
        val d = derivedSlot(acquisitionHhmm) ?: return null
        return SlotIdentity(d, "DERIVED_FIVE_MINUTE_FROM_LOCAL_ACQUISITION_CLOCK", acquisitionHhmm)
    }

    /**
     * Select the three strikes nearest this response's own `underlying_spot_price` that have BOTH
     * CE and PE instrument identities present for the response's trading expiry.
     *
     * Deliberately does NOT require a valid IV or a usable book: requiring them would hide exactly
     * the missing and invalid quote evidence this capture exists to record.
     *
     * Equal distance breaks to the LOWER strike.
     */
    fun select(rawChain: JSONObject?, index: String, sessionDate: String,
               acquisitionHhmm: String?, requestedUnderlyingKey: String?,
               existingCohort: JSONObject? = null): Selection {
        if (existingCohort != null) return Selection(ALREADY_PINNED, existingCohort)
        if (!inWindow(acquisitionHhmm)) return Selection(OUTSIDE_WINDOW, null, "acquisition=${acquisitionHhmm}")
        val data = rawChain?.optJSONArray("data")
        if (data == null || data.length() == 0) return Selection(UNAVAILABLE_IDENTITY, null, "no_data_rows")

        // the spot must come from THIS response, not from a cache
        var spot: Double? = null
        var expiry = ""
        var underlying = ""
        for (i in 0 until data.length()) {
            val row = data.optJSONObject(i) ?: continue
            if (spot == null) spot = finitePositive(row, "underlying_spot_price")
            if (expiry.isEmpty()) expiry = row.optString("expiry", "").trim()
            if (underlying.isEmpty()) underlying = row.optString("underlying_key", "").trim()
        }
        if (spot == null) return Selection(UNAVAILABLE_SPOT, null, "no_valid_underlying_spot_price")
        if (expiry.isEmpty() || underlying.isEmpty())
            return Selection(UNAVAILABLE_IDENTITY, null, "row_identity_incomplete")
        if (!requestedUnderlyingKey.isNullOrBlank() && requestedUnderlyingKey.trim() != underlying)
            return Selection(UNAVAILABLE_IDENTITY, null, "underlying_key_disagreement")

        // candidate strikes: both legs identified, on the response's own expiry, same underlying
        data class Cand(val strike: Double, val ce: String, val pe: String)
        val cands = ArrayList<Cand>()
        for (i in 0 until data.length()) {
            val row = data.optJSONObject(i) ?: continue
            if (row.optString("expiry", "").trim() != expiry) continue
            if (row.optString("underlying_key", "").trim() != underlying) continue
            val k = finitePositive(row, "strike_price") ?: continue
            val ce = row.optJSONObject("call_options")?.optString("instrument_key", "")?.trim().orEmpty()
            val pe = row.optJSONObject("put_options")?.optString("instrument_key", "")?.trim().orEmpty()
            if (ce.isEmpty() || pe.isEmpty()) continue
            if (cands.none { it.strike == k }) cands.add(Cand(k, ce, pe))
        }
        if (cands.size < STRIKES_PER_SIDE)
            return Selection(INCOMPLETE_PAIRED_STRIKES, null, "paired_strikes=${cands.size}")

        val s = spot
        // nearest by |strike - spot|; equal distance breaks to the LOWER strike
        val chosen = cands
            .sortedWith(compareBy<Cand> { Math.abs(it.strike - s) }.thenBy { it.strike })
            .take(STRIKES_PER_SIDE).sortedBy { it.strike }

        val pinned = JSONArray()
        for (c in chosen) {
            pinned.put(PinnedContract(index, c.ce, underlying, expiry, c.strike, "CE").json())
            pinned.put(PinnedContract(index, c.pe, underlying, expiry, c.strike, "PE").json())
        }
        val mod = minuteOfDay(acquisitionHhmm) ?: WINDOW_OPEN_MIN
        val cohort = JSONObject()
            .put("cohort_rule_version", COHORT_RULE_VERSION)
            .put("cohort_id", PremiumObservationContract.sha256Hex(
                "$index|$sessionDate|$expiry|$underlying|" +
                    chosen.joinToString(",") { it.strike.toString() }).take(32))
            .put("index", index)
            .put("session_date", sessionDate)
            .put("underlying_key", underlying)
            .put("expiry", expiry)
            .put("selection_acquisition_time", acquisitionHhmm ?: JSONObject.NULL)
            .put("selection_minute_of_day", mod)
            .put("late_start", mod > WINDOW_OPEN_MIN)
            .put("window_open_min", WINDOW_OPEN_MIN)
            .put("window_close_min", WINDOW_CLOSE_MIN)
            // the causal inputs the selection actually used, frozen with it
            .put("causal_inputs", JSONObject()
                .put("underlying_spot_price", s)
                .put("spot_basis", "SAME_ACQUISITION_RESPONSE_ROW")
                .put("paired_strike_candidates", cands.size)
                .put("selected_strikes", JSONArray().also { a -> chosen.forEach { a.put(it.strike) } }))
            .put("pinned_contracts", pinned)
            .put("contracts_expected", CONTRACTS_PER_COHORT)
        return Selection(SELECTED, cohort)
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
            if (row.optString("expiry", "").trim() == pinned.expiry) expirySeen = true
            if (row.optString("expiry", "").trim() != pinned.expiry) continue
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
