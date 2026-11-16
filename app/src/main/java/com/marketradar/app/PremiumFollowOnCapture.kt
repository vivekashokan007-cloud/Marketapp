package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * The FOLLOW-ON research capture: one closing book and the next session's reopening book, for
 * the same exact contracts.
 *
 * ## Why it exists at all
 *
 * The installed b530 captures `[09:20, 15:05)` — `PremiumCaptureCohort.PILOT_WINDOW`, inclusive
 * of the open and **exclusive** of the close. Its last slot is therefore 15:00, and the Stage G
 * matcher needs a close mark in 15:10–15:30. **No number of further sessions on that window can
 * produce one.** This path adds the two windows that can, without moving the pilot's.
 *
 * ## What is separate, and how it stays separate
 *
 *  * **Its own windows.** `CLOSE_WINDOW` 15:10–15:30 inclusive and `REOPEN_WINDOW` 09:15–09:45
 *    inclusive. `PILOT_WINDOW` is untouched, and every existing caller still takes it by default,
 *    so the frozen 60-minute pilot and the b530 capture behave exactly as before.
 *  * **Its own store root.** `premium_capture_followon`, a sibling of the pilot's
 *    `premium_capture`. Records, refusal counters, summaries and the byte total are therefore
 *    separate, the pilot's cap is not shared with this path, and the export carries the two under
 *    separate keys. The follow-on tree is bounded by the same per-root cap
 *    (`PremiumCaptureStore.MAX_BYTES_GLOBAL`), and at two acquisitions per index per session it
 *    writes on the order of 30 KiB a session.
 *  * **Its own purpose tag** on every carry and every export section, so a reader never has to
 *    infer which capture produced a record.
 *
 * ## How the SAME contract is observed on both sides
 *
 * Through the normal acquisition path. This object is called from the same poll results the pilot
 * capture is called from, and it calls the same `PremiumCaptureSession.observeSlot`. The only
 * additions are the window and a list of required strikes:
 *
 *  1. in the close window, the cohort is selected normally and a **carry** is written: the index,
 *     underlying key, expiry and the six `(instrument_key, option_type, strike)` triples, with a
 *     digest over them;
 *  2. in the reopen window of the next session, the carry is read back **from disk** — so it
 *     survives a restart — validated, and its strikes are passed to `observeSlot` as
 *     `requireStrikes`. The next day's cohort is therefore pinned to yesterday's contracts
 *     instead of being reselected around the new spot, and a contract that no longer exists is
 *     refused as a roll rather than replaced by a neighbouring strike.
 *
 * ## What it refuses, and why
 *
 * The exit-before-expiry rule is applied at the carry: a carry whose expiry is on or before the
 * session now being captured is refused (`CARRY_EXPIRY_NOT_AFTER_SESSION`), because a mark into
 * expiry day is not a position this research can hold. Holiday and weekend gaps need no special
 * case — the carry is dated and is accepted for the next session that actually captures, and the
 * gap it crossed is visible in the two session dates.
 *
 * Nothing here changes the pilot, the brain, ranking, sizing, exits or notifications, and nothing
 * here is wired into the service by this commit: `observe` is the entry point a later candidate
 * calls from the existing poll path.
 */
object PremiumFollowOnCapture {

    const val PURPOSE = "premium_followon_overnight_mark_v1"
    const val CARRY_VERSION = "premium_followon_carry_v1"
    const val DIR_NAME = "premium_capture_followon"

    /** 15:10–15:30 IST inclusive: the window the Stage G close mark must come from. */
    val CLOSE_WINDOW = PremiumCaptureCohort.CaptureWindow(
        "followon_close_1510_1530", 15 * 60 + 10, 15 * 60 + 30, closeInclusive = true)

    /** 09:15–09:45 IST inclusive: the opening-mark tolerance the matcher declares. */
    val REOPEN_WINDOW = PremiumCaptureCohort.CaptureWindow(
        "followon_reopen_0915_0945", 9 * 60 + 15, 9 * 60 + 45, closeInclusive = true)

    // outcomes of a follow-on attempt, each reported rather than silently skipped
    const val NOT_IN_A_FOLLOW_ON_WINDOW = "NOT_IN_A_FOLLOW_ON_WINDOW"
    const val CARRY_ABSENT = "CARRY_ABSENT"
    const val CARRY_UNREADABLE = "CARRY_UNREADABLE"
    const val CARRY_DIGEST_MISMATCH = "CARRY_DIGEST_MISMATCH"
    const val CARRY_FOR_ANOTHER_INDEX = "CARRY_FOR_ANOTHER_INDEX"
    const val CARRY_FROM_THIS_SESSION = "CARRY_FROM_THIS_SESSION_NOT_A_PRIOR_ONE"
    const val CARRY_EXPIRY_NOT_AFTER_SESSION = "CARRY_EXPIRY_NOT_AFTER_SESSION"
    const val CARRY_INCOMPLETE = "CARRY_INCOMPLETE"

    /**
     * The carry is not from the session immediately before this one. A stale carry would pair a
     * close from days ago with this morning's open and call it an overnight mark.
     */
    const val CARRY_NOT_PREVIOUS_SESSION = "CARRY_NOT_FROM_THE_PREVIOUS_TRADING_SESSION"

    /** The trading calendar cannot speak about these dates, so the gap cannot be established. */
    const val CARRY_CALENDAR_NOT_COVERED = "CARRY_CALENDAR_NOT_COVERED"

    /**
     * A cohort is already pinned for this session and is NOT the carry's contracts. Refused
     * rather than observed: writing paired observations against a different cohort would make
     * the two halves of the mark different positions.
     */
    const val SAVED_COHORT_DOES_NOT_MATCH_CARRY = "SAVED_COHORT_DOES_NOT_MATCH_CARRY"

    /** The research control for this capture is off. Default off; never implied by the pilot's. */
    const val RESEARCH_CONTROL_OFF = "FOLLOW_ON_RESEARCH_CONTROL_OFF"

    /** The follow-on store root: a sibling of the pilot's, never inside it. */
    fun root(filesDir: File) = File(filesDir, DIR_NAME)

    fun store(filesDir: File) = PremiumCaptureStore(root(filesDir).apply { mkdirs() })

    /**
     * This capture's own research control, in its own tree. Default OFF, and never implied by
     * the pilot's capture flag: a research path must be switched on deliberately.
     */
    private fun controlFile(filesDir: File) = File(root(filesDir).apply { mkdirs() },
        "followon_research_enabled")

    fun isResearchEnabled(filesDir: File): Boolean = controlFile(filesDir).isFile

    fun setResearchEnabled(filesDir: File, on: Boolean): Boolean {
        val f = controlFile(filesDir)
        return if (on) {
            runCatching { f.writeText(PURPOSE) }.isSuccess && f.isFile
        } else {
            f.delete(); !f.isFile
        }
    }

    private fun carryFile(filesDir: File, index: String) =
        File(root(filesDir).apply { mkdirs() }, "carry_${index.trim().uppercase()}.json")

    /** Which follow-on window a clock falls in, or null. */
    fun windowFor(hhmm: String?): PremiumCaptureCohort.CaptureWindow? {
        val m = PremiumCaptureCohort.minuteOfDay(hhmm) ?: return null
        return when {
            CLOSE_WINDOW.contains(m) -> CLOSE_WINDOW
            REOPEN_WINDOW.contains(m) -> REOPEN_WINDOW
            else -> null
        }
    }

    // ── the carry ─────────────────────────────────────────────────────────────────────────────

    fun carryDigest(body: JSONObject): String = PremiumObservationContract.ref(
        PremiumObservationContract.sha256Hex(PremiumObservationContract.canonicalJson(body)))

    /**
     * Write the carry from a pinned cohort. Durable, because the reopen happens in another
     * process lifetime: the file is the only thing that survives an overnight restart.
     */
    fun writeCarry(filesDir: File, cohort: JSONObject, writtenAtMs: Long): JSONObject {
        val contracts = JSONArray()
        val pins = cohort.optJSONArray("pinned_contracts") ?: JSONArray()
        for (i in 0 until pins.length()) {
            val c = pins.optJSONObject(i) ?: continue
            contracts.put(JSONObject()
                .put("instrument_key", c.optString("instrument_key"))
                .put("option_type", c.optString("option_type"))
                .put("strike", c.optDouble("strike")))
        }
        val body = JSONObject()
            .put("carry_version", CARRY_VERSION)
            .put("purpose", PURPOSE)
            .put("session_date", cohort.optString("session_date"))
            .put("index", cohort.optString("index"))
            .put("underlying_key", cohort.optString("underlying_key"))
            .put("expiry", cohort.optString("expiry"))
            .put("cohort_id", cohort.optString("cohort_id"))
            .put("contracts", contracts)
            .put("written_at_ms", writtenAtMs)
        val stamped = JSONObject(body.toString()).put("carry_digest", carryDigest(body))
        val f = carryFile(filesDir, cohort.optString("index"))
        val tmp = File(f.parentFile, f.name + ".tmp")
        tmp.writeText(stamped.toString())
        if (!tmp.renameTo(f)) {
            f.writeText(stamped.toString())
            tmp.delete()
        }
        return stamped
    }

    data class Carry(val status: String, val body: JSONObject?, val detail: String? = null) {
        val usable: Boolean get() = status == "USABLE"

        /** The EXACT contracts carried: strike, side and instrument key together. */
        val contracts: List<PremiumCaptureCohort.RequiredContract>
            get() {
                val out = mutableListOf<PremiumCaptureCohort.RequiredContract>()
                val a = body?.optJSONArray("contracts") ?: return out
                for (i in 0 until a.length()) {
                    val c = a.optJSONObject(i) ?: continue
                    val k = c.optDouble("strike")
                    if (!k.isFinite()) continue
                    out.add(PremiumCaptureCohort.RequiredContract(
                        k, c.optString("option_type"), c.optString("instrument_key")))
                }
                return out.sortedWith(compareBy({ it.strike }, { it.optionType }))
            }

        val strikes: List<Double> get() = contracts.map { it.strike }.distinct().sorted()
        val expiry: String? get() = body?.optString("expiry")?.takeIf { it.isNotEmpty() }
    }

    /**
     * The trading session immediately before `sessionDate`, or null with a reason.
     *
     * Uses the app's own `RuleBExitPolicy.isSession` — the same NSE holiday list the brain and
     * the EV calendar carry — and fails closed outside the years it covers rather than guessing
     * a weekend.
     */
    fun previousTradingSession(sessionDate: String, limit: Int = 10): Pair<String?, String?> {
        val start = runCatching { java.time.LocalDate.parse(sessionDate) }.getOrNull()
            ?: return null to "session date '$sessionDate' is not a date"
        var d = start
        for (i in 0 until limit) {
            d = d.minusDays(1)
            val ok = try {
                RuleBExitPolicy.isSession(d)
            } catch (e: Exception) {
                return null to CARRY_CALENDAR_NOT_COVERED
            }
            if (ok) return d.toString() to null
        }
        return null to "no trading session within $limit days before $sessionDate"
    }

    /**
     * Read and validate the carry for one index against the session about to be captured.
     *
     * Every check is a refusal with a name, because a carry that is quietly ignored would turn
     * into a silent reselection — the exact failure the carry exists to prevent.
     */
    fun readCarry(filesDir: File, index: String, sessionDate: String): Carry {
        val f = carryFile(filesDir, index)
        if (!f.isFile) return Carry(CARRY_ABSENT, null, f.name)
        val body = runCatching { JSONObject(f.readText()) }.getOrNull()
            ?: return Carry(CARRY_UNREADABLE, null, f.name)
        val claimed = body.optString("carry_digest", "")
        val without = JSONObject(body.toString()).also { it.remove("carry_digest") }
        if (claimed.isEmpty() || claimed != carryDigest(without))
            return Carry(CARRY_DIGEST_MISMATCH, body, "digest does not cover these bytes")
        if (body.optString("index") != index.trim().uppercase())
            return Carry(CARRY_FOR_ANOTHER_INDEX, body, body.optString("index"))
        val carried = body.optString("session_date")
        if (carried.isEmpty() || carried >= sessionDate)
            return Carry(CARRY_FROM_THIS_SESSION, body, "carried '$carried' vs now '$sessionDate'")
        val (previous, calendarWhy) = previousTradingSession(sessionDate)
        if (previous == null)
            return Carry(if (calendarWhy == CARRY_CALENDAR_NOT_COVERED)
                CARRY_CALENDAR_NOT_COVERED else CARRY_NOT_PREVIOUS_SESSION, body, calendarWhy)
        if (carried != previous)
            return Carry(CARRY_NOT_PREVIOUS_SESSION, body,
                "carried '$carried' but the session before '$sessionDate' is '$previous'")
        val expiry = body.optString("expiry")
        if (expiry.isEmpty() || expiry <= sessionDate)
            return Carry(CARRY_EXPIRY_NOT_AFTER_SESSION, body,
                "expiry '$expiry' is not after '$sessionDate'")
        val contracts = body.optJSONArray("contracts") ?: JSONArray()
        if (contracts.length() != PremiumCaptureCohort.CONTRACTS_PER_COHORT)
            return Carry(CARRY_INCOMPLETE, body,
                "${contracts.length()} contracts, need ${PremiumCaptureCohort.CONTRACTS_PER_COHORT}")
        for (i in 0 until contracts.length()) {
            val c = contracts.optJSONObject(i)
            val k = c?.optDouble("strike")
            if (c == null || c.optString("instrument_key").isEmpty() ||
                c.optString("option_type").isEmpty() || k == null || !k.isFinite() || k <= 0)
                return Carry(CARRY_INCOMPLETE, body, "contract $i is incomplete")
        }
        return Carry("USABLE", body)
    }

    // ── the capture itself, through the normal acquisition path ───────────────────────────────

    data class Attempt(
        val window: String?,
        val outcome: String,
        val slotResult: PremiumCaptureSession.SlotResult?,
        val carry: Carry?,
        val carryWritten: JSONObject? = null,
        val lotAuthorityWrite: String? = null,
        val lotAuthorityRef: String? = null,
        val detail: String? = null,
    )

    /**
     * Whether a pinned cohort is exactly the carry's contracts, or why it is not.
     *
     * Compared on the full identity — index, expiry and each (strike, side, instrument key) —
     * because a changed key under an unchanged strike is a different contract.
     */
    fun cohortMatchesCarry(cohort: JSONObject?, carry: Carry): String? {
        if (cohort == null) return "the pinned cohort could not be parsed"
        val body = carry.body ?: return "the carry has no body"
        if (cohort.optString("index") != body.optString("index"))
            return "cohort index '${cohort.optString("index")}' vs carry " +
                "'${body.optString("index")}'"
        if (cohort.optString("expiry") != body.optString("expiry"))
            return "cohort expiry '${cohort.optString("expiry")}' vs carry " +
                "'${body.optString("expiry")}'"
        val pins = cohort.optJSONArray("pinned_contracts") ?: return "the cohort pins nothing"
        val have = mutableListOf<Triple<Double, String, String>>()
        for (i in 0 until pins.length()) {
            val c = pins.optJSONObject(i) ?: continue
            have.add(Triple(c.optDouble("strike"), c.optString("option_type"),
                c.optString("instrument_key")))
        }
        val want = carry.contracts.map { Triple(it.strike, it.optionType, it.instrumentKey) }
        val h = have.sortedWith(compareBy({ it.first }, { it.second }))
        val w = want.sortedWith(compareBy({ it.first }, { it.second }))
        if (h != w) return "pinned contracts $h are not the carried $w"
        return null
    }

    /**
     * Observe one poll for the follow-on capture.
     *
     * The arguments are the ones the poll already has — the same values the pilot capture hook is
     * given. Outside both follow-on windows this does nothing and says so, so wiring it into the
     * poll path costs one call and cannot affect any other slot.
     */
    fun observe(
        filesDir: File,
        index: String,
        sessionDate: String,
        rawChain: JSONObject?,
        acquisitionHhmm: String?,
        schedulerSlot: String?,
        requestedUnderlyingKey: String?,
        requestStartedMs: Long?,
        responseReceivedMs: Long?,
        vendorSourceTs: String? = null,
        claimedLot: Any? = null,
        claimedLotSource: String? = null,
        lotTableRef: String? = null,
        mapRef: String? = null,
        productionLotAuthorityRef: String? = null,
        declaredSchedulerSlot: String? = null,
        /**
         * The expiry this acquisition is for. Supplied, the follow-on path resolves and writes
         * the production lot authority for this tree exactly as the pilot's coordinator does;
         * omitted, it falls back to the caller's own claim and the tree carries no authority.
         */
        expiryForAuthority: String? = null,
    ): Attempt {
        val clock = acquisitionHhmm?.takeIf { it.isNotEmpty() } ?: schedulerSlot
        val window = windowFor(clock)
            ?: return Attempt(null, NOT_IN_A_FOLLOW_ON_WINDOW, null, null)
        if (!isResearchEnabled(filesDir))
            return Attempt(window.id, RESEARCH_CONTROL_OFF, null, null)
        val st = store(filesDir)
        val session = PremiumCaptureSession(st)

        /*
         * THE PRODUCTION LOT AUTHORITY, in this tree.
         *
         * The authority record is resolved from the application's own `ContractLotTable` and
         * written once per index/session; the pilot does it in `PremiumCaptureCoordinator`. The
         * first version of this path called `observeSlot` directly and therefore produced a tree
         * whose envelopes carried a `production_authority_ref` with NO record to match — the
         * export said `NO_AUTHORITY_RECORD_PRESENT` and every Stage D row over it would have been
         * refused `lot_authority_unresolved`. The same resolver is reused here, rather than a
         * second copy of it, and the caller's own claim is used only when it supplies no expiry.
         */
        val resolvedClaim = expiryForAuthority?.let {
            runCatching {
                PremiumCaptureCoordinator.lotClaimFromContractAuthority(index, sessionDate, it)
            }.getOrNull()
        }
        var authorityRef = productionLotAuthorityRef
        var authorityWrite: String? = null
        var lotForSlot: Any? = claimedLot
        var lotSourceForSlot: String? = claimedLotSource
        var tableRefForSlot: String? = lotTableRef
        if (resolvedClaim != null) {
            val authority = resolvedClaim.optJSONObject("production_authority")
            if (authority != null) {
                val write = st.putLotAuthority(index, sessionDate, authority)
                authorityWrite = write.status
                if (write.persisted)
                    authorityRef = PremiumObservationContract.ref(
                        PremiumObservationContract.contentHash(authority))
            }
            if (!resolvedClaim.isNull("claimed_lot")) {
                lotForSlot = resolvedClaim.opt("claimed_lot")
                lotSourceForSlot = resolvedClaim.optString("claimed_source", null)
                tableRefForSlot = resolvedClaim.optString("table_ref", null)
            }
        }

        var carry: Carry? = null
        var require: List<PremiumCaptureCohort.RequiredContract>? = null
        var requireExpiry: String? = null
        if (window === REOPEN_WINDOW) {
            carry = readCarry(filesDir, index, sessionDate)
            if (!carry.usable)
                return Attempt(window.id, carry.status, null, carry, null, authorityWrite,
                    authorityRef)
            require = carry.contracts
            requireExpiry = carry.expiry
            // a cohort already pinned for this session must BE the carry's contracts; otherwise
            // observations would be written against a different position
            val existing = st.readCohortText(index, sessionDate)
            if (existing != null) {
                val why = cohortMatchesCarry(
                    runCatching { JSONObject(existing) }.getOrNull(), carry)
                if (why != null)
                    return Attempt(window.id, SAVED_COHORT_DOES_NOT_MATCH_CARRY, null, carry,
                        null, authorityWrite, authorityRef, why)
            }
        }

        val result = session.observeSlot(
            index = index, sessionDate = sessionDate, rawChain = rawChain,
            acquisitionHhmm = acquisitionHhmm, schedulerSlot = schedulerSlot,
            requestedUnderlyingKey = requestedUnderlyingKey,
            requestStartedMs = requestStartedMs, responseReceivedMs = responseReceivedMs,
            vendorSourceTs = vendorSourceTs,
            claimedLot = lotForSlot, claimedLotSource = lotSourceForSlot,
            lotTableRef = tableRefForSlot, mapRef = mapRef,
            productionLotAuthorityRef = authorityRef,
            declaredSchedulerSlot = declaredSchedulerSlot,
            window = window, requireContracts = require, requireExpiry = requireExpiry)

        // the carry is written from the CLOSE side only, and only once the cohort is pinned
        var written: JSONObject? = null
        if (window === CLOSE_WINDOW) {
            val cohortText = st.readCohortText(index, sessionDate)
            if (cohortText != null) {
                val cohort = runCatching { JSONObject(cohortText) }.getOrNull()
                if (cohort != null && cohort.optJSONArray("pinned_contracts") != null)
                    written = writeCarry(filesDir, cohort,
                        responseReceivedMs ?: System.currentTimeMillis())
            }
        }
        recordOutcome(st, index, sessionDate, window, result.status)
        return Attempt(window.id, result.status, result, carry, written, authorityWrite,
            authorityRef)
    }

    /**
     * Durable accounting for what this window did, as a counter named for the outcome.
     *
     * WHY THIS EXISTS. A cohort-level refusal — a switched instrument key, a rolled expiry, a
     * missing strike — happens BEFORE any record is written, so nothing reached the store and
     * the export published only `NO_PREMIUM_DATA_FOR_THIS_INDEX`. The analysis side then saw a
     * bare absence: the matcher correctly refused with `no_observations_for_index_in_next_session`,
     * but the export carried no reason for it, and the reason existed only in a returned object
     * until the process ended. The outcome is now counted in the store's own bounded summary and
     * published per index, so a refusal is a stated reason in the evidence.
     *
     * A counter, not a log: `bumpCounters` is replace-in-place inside the declared reserve, so
     * this cannot grow the store or push evidence out of it.
     */
    private fun recordOutcome(store: PremiumCaptureStore, index: String, session: String,
                              window: PremiumCaptureCohort.CaptureWindow, status: String) {
        val key = if (window === REOPEN_WINDOW) "followon_reopen" else "followon_close"
        runCatching {
            store.bumpCounters(index, session, mapOf(
                "${key}_attempts" to 1L,
                "${key}_${if (status == PremiumCaptureSession.OBSERVED) "observed"
                    else "refused_$status"}" to 1L))
        }
    }

    /** The outcome counters this capture recorded for a session, per index. */
    fun outcomeCounters(filesDir: File, date: String): JSONObject {
        val st = PremiumCaptureStore(root(filesDir))
        return JSONObject().also { out ->
            for (ix in listOf("NF", "BNF")) {
                val c = runCatching { st.readCounters(ix, date) }.getOrNull() ?: JSONObject()
                val kept = JSONObject()
                for (k in c.keys()) if (k.startsWith("followon_")) kept.put(k, c.opt(k))
                out.put(ix, kept)
            }
        }
    }

    /**
     * The follow-on export section, assembled by the SAME code as the pilot's over the follow-on
     * root, under its own key so the two are never read as one capture.
     */
    fun exportSection(filesDir: File, date: String): JSONObject {
        val section = PremiumExportSection.build(filesDir, date, root(filesDir))
        return JSONObject()
            .put("purpose", PURPOSE)
            .put("capture_windows", JSONObject()
                .put("close", CLOSE_WINDOW.id)
                .put("close_label", CLOSE_WINDOW.label)
                .put("reopen", REOPEN_WINDOW.id)
                .put("reopen_label", REOPEN_WINDOW.label))
            .put("pilot_window", JSONObject()
                .put("id", PremiumCaptureCohort.PILOT_WINDOW.id)
                .put("label", PremiumCaptureCohort.PILOT_WINDOW.label)
                .put("independent_of_this_capture", true))
            .put("store_root", DIR_NAME)
            // what each window did, including the refusals that wrote no record at all
            .put("window_outcomes", outcomeCounters(filesDir, date))
            .put("bytes", PremiumCaptureStore(root(filesDir)).globalBytes())
            .put("carries", JSONObject().also { c ->
                for (ix in listOf("NF", "BNF")) {
                    val f = carryFile(filesDir, ix)
                    c.put(ix, if (f.isFile) (runCatching { JSONObject(f.readText()) }
                        .getOrNull() ?: JSONObject().put("state", CARRY_UNREADABLE))
                    else JSONObject().put("state", CARRY_ABSENT))
                }
            })
            .put("section", section)
    }
}
