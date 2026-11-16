package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * The follow-on research capture: a closing book and the next session's reopening book, for the
 * SAME contracts, through the normal acquisition path.
 *
 * THE READINESS CORRECTION THIS CLASS EXISTS FOR. The installed b530 captures
 * `[09:20, 15:05)` — inclusive of the open, EXCLUSIVE of the close — so its last slot is 15:00
 * and it can never produce the 15:10–15:30 close mark Stage G needs. No number of further
 * sessions on that window changes that. `theInstalledPilotWindowCannotProduceACloseMark` asserts
 * it from the production constants rather than from a claim.
 *
 * What the rest of these tests establish:
 *  * the pilot window is UNCHANGED and still refuses a 15:20 attempt;
 *  * a close capture writes a durable carry, and the next session's reopen pins **yesterday's
 *    strikes** rather than reselecting around the new spot — proved by asserting that a free
 *    selection on the same response would have chosen different strikes;
 *  * the carry survives a restart, because it is read from disk by fresh objects;
 *  * a rolled or missing contract is refused, never substituted with a neighbouring strike;
 *  * a carry into expiry day is refused, which is the exit-before-expiry rule;
 *  * a tampered carry is refused by its own digest;
 *  * the follow-on records, refusals and bytes live in their own tree, so the pilot's are
 *    untouched and the export carries the two separately;
 *  * NIFTY and BANKNIFTY are exercised separately.
 */
class PremiumFollowOnCaptureTest {

    private val friday = "2026-10-16"
    private val monday = "2026-10-19"
    private val reqMs = 1_792_000_000_000L
    private val respMs = reqMs + 120L

    private fun raw(id: String): JSONObject {
        val text = requireNotNull(
            javaClass.classLoader?.getResourceAsStream("contracts/x1_upstox_raw_chains.json"))
            .bufferedReader().readText()
        val snaps = JSONObject(text).getJSONArray("snapshots")
        for (i in 0 until snaps.length()) {
            val s = snaps.getJSONObject(i)
            if (s.optString("id") == id) return s.getJSONObject("raw")
        }
        throw AssertionError("fixture snapshot $id not found")
    }

    private fun tempDir(name: String): File =
        Files.createTempDirectory(name).toFile().apply { mkdirs() }

    /** A files dir with the pilot capture enabled AND this research path switched on. */
    private fun researchDir(name: String): File {
        val d = tempDir(name)
        PremiumCaptureControl.setEnabled(d, true)
        assertTrue(PremiumFollowOnCapture.setResearchEnabled(d, true))
        return d
    }

    private fun observe(filesDir: File, index: String, date: String, clock: String,
                        chainId: String, chain: JSONObject? = null):
        PremiumFollowOnCapture.Attempt = PremiumFollowOnCapture.observe(
            filesDir = filesDir, index = index, sessionDate = date,
            rawChain = chain ?: raw(chainId), acquisitionHhmm = clock, schedulerSlot = clock,
            requestedUnderlyingKey = PremiumCaptureCohort.INDEX_UNDERLYING[index],
            requestStartedMs = reqMs, responseReceivedMs = respMs,
            claimedLot = if (index == "NF") 75 else 35,
            claimedLotSource = "CONTRACT_LOT_TABLE",
            lotTableRef = "contract_lot_table/contract_lot_table_v3_20260914/TEST/$date",
            productionLotAuthorityRef = "a908c610e9a302ed31bc639283031f4d")

    // ───────────────────────── the readiness correction ─────────────────────────

    @Test
    fun theInstalledPilotWindowCannotProduceACloseMark() {
        assertEquals(9 * 60 + 20, PremiumCaptureCohort.WINDOW_OPEN_MIN)
        assertEquals(15 * 60 + 5, PremiumCaptureCohort.WINDOW_CLOSE_MIN)
        assertFalse("the close minute itself is excluded",
            PremiumCaptureCohort.PILOT_WINDOW.closeInclusive)
        // the last five-minute slot the pilot can capture is 15:00
        var last = -1
        var m = PremiumCaptureCohort.WINDOW_OPEN_MIN
        while (m < 24 * 60) {
            if (PremiumCaptureCohort.PILOT_WINDOW.contains(m)) last = m
            m += 5
        }
        assertEquals("15:00", "%02d:%02d".format(last / 60, last % 60))
        for (clock in listOf("15:05", "15:10", "15:20", "15:30")) {
            assertFalse("$clock must be outside the pilot window",
                PremiumCaptureCohort.inWindow(clock))
        }
        // and a real attempt at 15:20 through the pilot path is refused, not captured
        val filesDir = tempDir("followon-pilot-refuses")
        PremiumCaptureControl.setEnabled(filesDir, true)
        val store = PremiumCaptureStore(PremiumCaptureControl.root(filesDir))
        val r = PremiumCaptureSession(store).observeSlot(
            "NF", friday, raw("fri_1520_reference_NF"), "15:20", "15:20",
            PremiumCaptureCohort.INDEX_UNDERLYING["NF"], reqMs, respMs,
            claimedLot = 75, claimedLotSource = "CONTRACT_LOT_TABLE",
            lotTableRef = "contract_lot_table/x/$friday")
        assertEquals(PremiumCaptureSession.OUTSIDE_WINDOW, r.status)
        assertEquals(0, store.readAll("NF", friday).size)
    }

    @Test
    fun theFollowOnWindowsCoverTheCloseAndTheReopenAndNothingElse() {
        val close = PremiumFollowOnCapture.CLOSE_WINDOW
        val reopen = PremiumFollowOnCapture.REOPEN_WINDOW
        assertTrue(close.contains(15 * 60 + 10) && close.contains(15 * 60 + 30))
        assertFalse(close.contains(15 * 60 + 9) || close.contains(15 * 60 + 31))
        assertTrue(reopen.contains(9 * 60 + 15) && reopen.contains(9 * 60 + 45))
        assertFalse(reopen.contains(9 * 60 + 14) || reopen.contains(9 * 60 + 46))
        assertEquals(close.id, PremiumFollowOnCapture.windowFor("15:20")?.id)
        assertEquals(reopen.id, PremiumFollowOnCapture.windowFor("09:20")?.id)
        assertNull("midday belongs to the pilot, not to this capture",
            PremiumFollowOnCapture.windowFor("12:20"))
        assertNull(PremiumFollowOnCapture.windowFor(null))
    }

    @Test
    fun aPollOutsideBothWindowsDoesNothingAtAll() {
        val filesDir = researchDir("followon-noop")
        val a = observe(filesDir, "NF", friday, "12:20", "mon_0920_entry_NF")
        assertEquals(PremiumFollowOnCapture.NOT_IN_A_FOLLOW_ON_WINDOW, a.outcome)
        assertNull(a.slotResult)
        assertNull("nothing may be written outside the follow-on windows",
            PremiumFollowOnCapture.store(filesDir).readCohortText("NF", friday))
        assertEquals(0, PremiumFollowOnCapture.store(filesDir).readAll("NF", friday).size)
    }

    // ───────────────────────── the carry, and the same contracts ─────────────────────────

    @Test
    fun aCloseCaptureWritesACarryAndTheReopenPinsYesterdaysStrikes() {
        val filesDir = researchDir("followon-carry")

        val close = observe(filesDir, "NF", friday, "15:20", "fri_1520_reference_NF")
        assertEquals(PremiumCaptureSession.OBSERVED, close.slotResult?.status)
        assertEquals(PremiumCaptureCohort.CONTRACTS_PER_COHORT, close.slotResult?.written)
        val carry = requireNotNull(close.carryWritten)
        assertEquals(PremiumFollowOnCapture.PURPOSE, carry.getString("purpose"))
        assertEquals(friday, carry.getString("session_date"))
        assertEquals(6, carry.getJSONArray("contracts").length())
        val carriedStrikes = PremiumFollowOnCapture
            .readCarry(filesDir, "NF", monday).strikes
        assertEquals(3, carriedStrikes.size)

        // what a FREE selection would have pinned on Monday's response -- different strikes,
        // because the spot moved. If the carry did not work, the mark would be across contracts.
        val free = PremiumCaptureCohort.select(raw("mon_0920_entry_NF"), "NF", monday, "09:20",
            PremiumCaptureCohort.INDEX_UNDERLYING["NF"], null,
            PremiumFollowOnCapture.REOPEN_WINDOW, null)
        assertEquals(PremiumCaptureCohort.SELECTED, free.status)
        val freeStrikes = requireNotNull(free.cohort).getJSONArray("paired_strikes").let { a ->
            (0 until a.length()).map { a.getDouble(it) }
        }
        assertNotEquals("the fixture must actually move the spot, or this proves nothing",
            carriedStrikes, freeStrikes)

        val reopen = observe(filesDir, "NF", monday, "09:20", "mon_0920_entry_NF")
        assertEquals(PremiumCaptureSession.OBSERVED, reopen.slotResult?.status)
        val pinned = JSONObject(requireNotNull(
            PremiumFollowOnCapture.store(filesDir).readCohortText("NF", monday)))
        val pinnedStrikes = pinned.getJSONArray("paired_strikes").let { a ->
            (0 until a.length()).map { a.getDouble(it) }
        }
        assertEquals("the reopen must pin YESTERDAY's strikes", carriedStrikes, pinnedStrikes)
        assertEquals("THREE_STRIKES_REQUIRED_BY_CALLER_VALIDATED_IN_THIS_RESPONSE",
            pinned.getString("selection_basis"))
        assertEquals(PremiumFollowOnCapture.REOPEN_WINDOW.id, pinned.getString("capture_window"))

        // the same instrument keys on both sides, which is what Stage G requires
        val closeKeys = JSONObject(requireNotNull(
            PremiumFollowOnCapture.store(filesDir).readCohortText("NF", friday)))
            .getJSONArray("pinned_contracts").let { a ->
                (0 until a.length()).map { a.getJSONObject(it).getString("instrument_key") }
            }.toSet()
        val openKeys = pinned.getJSONArray("pinned_contracts").let { a ->
            (0 until a.length()).map { a.getJSONObject(it).getString("instrument_key") }
        }.toSet()
        assertEquals(closeKeys, openKeys)
    }

    @Test
    fun theCarrySurvivesARestartBecauseItIsReadFromDisk() {
        val filesDir = researchDir("followon-restart")
        observe(filesDir, "NF", friday, "15:20", "fri_1520_reference_NF")
        // a "restart": every object below is constructed fresh, and the only thing that crosses
        // the boundary is the file on disk -- no cohort, no pin and no contract list is held
        val carry = PremiumFollowOnCapture.readCarry(filesDir, "NF", monday)
        assertTrue(carry.status, carry.usable)
        val reopen = observe(filesDir, "NF", monday, "09:20", "mon_0920_entry_NF")
        assertEquals(PremiumCaptureSession.OBSERVED, reopen.slotResult?.status)
        assertEquals(carry.strikes, JSONObject(requireNotNull(
            PremiumFollowOnCapture.store(filesDir).readCohortText("NF", monday)))
            .getJSONArray("paired_strikes").let { a ->
                (0 until a.length()).map { a.getDouble(it) } })
    }

    @Test
    fun aRolledOrMissingContractIsRefusedRatherThanSubstituted() {
        val filesDir = researchDir("followon-roll")
        observe(filesDir, "NF", friday, "15:20", "fri_1520_reference_NF")
        val carried = PremiumFollowOnCapture.readCarry(filesDir, "NF", monday).strikes
        // Monday's response no longer carries one of the carried strikes -- a roll, or a strike
        // that simply stopped being listed
        val chain = JSONObject(raw("mon_0920_entry_NF").toString())
        val rows = chain.getJSONArray("data")
        val kept = org.json.JSONArray()
        for (i in 0 until rows.length()) {
            val row = rows.getJSONObject(i)
            if (row.optDouble("strike_price") != carried[1]) kept.put(row)
        }
        chain.put("data", kept)
        val reopen = observe(filesDir, "NF", monday, "09:20", "x", chain)
        assertEquals(PremiumCaptureCohort.REQUIRED_STRIKE_ABSENT, reopen.slotResult?.status)
        assertTrue(reopen.slotResult?.detail.orEmpty().contains("required strikes"))
        assertNull("nothing may be pinned when a carried contract is gone",
            PremiumFollowOnCapture.store(filesDir).readCohortText("NF", monday))
    }

    @Test
    fun aCarryIntoExpiryDayIsRefused() {
        val filesDir = researchDir("followon-expiry")
        // the fixture's expiry is 2026-10-27, so the close is captured on the session BEFORE it:
        // the previous-session rule is then satisfied and the expiry rule is what must fire
        val dayBefore = "2026-10-26"
        val expiryDay = "2026-10-27"
        observe(filesDir, "NF", dayBefore, "15:20", "fri_1520_reference_NF")
        val carry = PremiumFollowOnCapture.readCarry(filesDir, "NF", monday)
        assertEquals(expiryDay, requireNotNull(carry.body).getString("expiry"))
        assertEquals(dayBefore to null,
            PremiumFollowOnCapture.previousTradingSession(expiryDay))
        val onExpiry = PremiumFollowOnCapture.readCarry(filesDir, "NF", expiryDay)
        assertEquals(PremiumFollowOnCapture.CARRY_EXPIRY_NOT_AFTER_SESSION, onExpiry.status)
        val attempt = observe(filesDir, "NF", expiryDay, "09:20", "mon_0920_entry_NF")
        assertEquals(PremiumFollowOnCapture.CARRY_EXPIRY_NOT_AFTER_SESSION, attempt.outcome)
        assertNull(attempt.slotResult)
    }

    // ───────────────────────── the identity, the control and the calendar ─────────────────────

    @Test
    fun theResearchControlIsOffByDefaultAndIsNotImpliedByThePilots() {
        val filesDir = tempDir("followon-control")
        PremiumCaptureControl.setEnabled(filesDir, true)        // the PILOT's flag only
        assertFalse(PremiumFollowOnCapture.isResearchEnabled(filesDir))
        val off = observe(filesDir, "NF", friday, "15:20", "fri_1520_reference_NF")
        assertEquals(PremiumFollowOnCapture.RESEARCH_CONTROL_OFF, off.outcome)
        assertNull(off.slotResult)
        assertNull(PremiumFollowOnCapture.store(filesDir).readCohortText("NF", friday))
        assertTrue(PremiumFollowOnCapture.setResearchEnabled(filesDir, true))
        assertEquals(PremiumCaptureSession.OBSERVED,
            observe(filesDir, "NF", friday, "15:20", "fri_1520_reference_NF")
                .slotResult?.status)
        assertTrue(PremiumFollowOnCapture.setResearchEnabled(filesDir, false))
        assertFalse(PremiumFollowOnCapture.isResearchEnabled(filesDir))
    }

    @Test
    fun regressionAStrikeThatNowResolvesToAnotherInstrumentKeyIsRefused() {
        val filesDir = researchDir("followon-keyswap")
        observe(filesDir, "NF", friday, "15:20", "fri_1520_reference_NF")
        val carried = PremiumFollowOnCapture.readCarry(filesDir, "NF", monday).contracts
        // the same strikes, but one leg now carries a DIFFERENT instrument key
        val chain = JSONObject(raw("mon_0920_entry_NF").toString())
        val rows = chain.getJSONArray("data")
        var swapped = false
        for (i in 0 until rows.length()) {
            val row = rows.getJSONObject(i)
            if (row.optDouble("strike_price") == carried[0].strike) {
                row.getJSONObject("call_options").put("instrument_key", "NSE_FO|999999")
                swapped = true
            }
        }
        assertTrue("the fixture must carry the strike this test swaps", swapped)
        val reopen = observe(filesDir, "NF", monday, "09:20", "x", chain)
        assertEquals(PremiumCaptureCohort.REQUIRED_KEY_MISMATCH, reopen.slotResult?.status)
        assertTrue(reopen.slotResult?.detail.orEmpty().contains("NSE_FO|999999"))
        assertNull(PremiumFollowOnCapture.store(filesDir).readCohortText("NF", monday))
    }

    @Test
    fun regressionAnExpiryRollIsRefusedAtTheReopen() {
        val filesDir = researchDir("followon-rollexpiry")
        observe(filesDir, "NF", friday, "15:20", "fri_1520_reference_NF")
        // Monday's response is for the NEXT expiry: the carried contracts' expiry is gone
        val chain = JSONObject(raw("mon_0920_entry_NF").toString())
        val rows = chain.getJSONArray("data")
        for (i in 0 until rows.length()) rows.getJSONObject(i).put("expiry", "2026-11-03")
        val reopen = observe(filesDir, "NF", monday, "09:20", "x", chain)
        assertEquals(PremiumCaptureCohort.REQUIRED_EXPIRY_MISMATCH, reopen.slotResult?.status)
        assertNull(PremiumFollowOnCapture.store(filesDir).readCohortText("NF", monday))
    }

    @Test
    fun regressionAStaleCarryFromAnEarlierSessionIsRefused() {
        val filesDir = researchDir("followon-stale")
        observe(filesDir, "NF", friday, "15:20", "fri_1520_reference_NF")
        // 2026-10-20 is an NSE holiday, so the session after Monday 10-19 is Wednesday 10-21.
        // A carry from Friday is not the previous session for that Wednesday.
        assertEquals("2026-10-19" to null,
            PremiumFollowOnCapture.previousTradingSession("2026-10-21"))
        val stale = PremiumFollowOnCapture.readCarry(filesDir, "NF", "2026-10-21")
        assertEquals(PremiumFollowOnCapture.CARRY_NOT_PREVIOUS_SESSION, stale.status)
        assertTrue(stale.detail.orEmpty().contains("2026-10-19"))
        assertEquals(PremiumFollowOnCapture.CARRY_NOT_PREVIOUS_SESSION,
            observe(filesDir, "NF", "2026-10-21", "09:20", "mon_0920_entry_NF").outcome)
    }

    @Test
    fun theCalendarFailsClosedOutsideTheYearsItCovers() {
        val (prev, why) = PremiumFollowOnCapture.previousTradingSession("2027-01-04")
        assertNull(prev)
        assertEquals(PremiumFollowOnCapture.CARRY_CALENDAR_NOT_COVERED, why)
        val filesDir = researchDir("followon-calendar")
        observe(filesDir, "NF", friday, "15:20", "fri_1520_reference_NF")
        assertEquals(PremiumFollowOnCapture.CARRY_CALENDAR_NOT_COVERED,
            PremiumFollowOnCapture.readCarry(filesDir, "NF", "2027-01-04").status)
    }

    @Test
    fun aCohortAlreadyPinnedForTheSessionMustBeTheCarriedContracts() {
        val filesDir = researchDir("followon-savedcohort")
        observe(filesDir, "NF", friday, "15:20", "fri_1520_reference_NF")
        val carry = PremiumFollowOnCapture.readCarry(filesDir, "NF", monday)
        assertTrue(carry.usable)
        // a cohort for Monday pinned on DIFFERENT contracts, as a free selection would produce
        val free = PremiumCaptureCohort.select(raw("mon_0920_entry_NF"), "NF", monday, "09:20",
            PremiumCaptureCohort.INDEX_UNDERLYING["NF"], null,
            PremiumFollowOnCapture.REOPEN_WINDOW, null)
        assertEquals(PremiumCaptureCohort.SELECTED, free.status)
        assertTrue(PremiumFollowOnCapture.store(filesDir)
            .putCohort("NF", monday, requireNotNull(free.cohort)).persisted)
        assertNotNull("the pinned cohort must differ from the carry, or this proves nothing",
            PremiumFollowOnCapture.cohortMatchesCarry(free.cohort, carry))
        val reopen = observe(filesDir, "NF", monday, "09:20", "mon_0920_entry_NF")
        assertEquals(PremiumFollowOnCapture.SAVED_COHORT_DOES_NOT_MATCH_CARRY, reopen.outcome)
        assertNull(reopen.slotResult)
        assertTrue(reopen.detail.orEmpty().contains("not the carried"))
        // and a cohort that IS the carry's contracts is accepted
        val dir2 = researchDir("followon-savedcohort-ok")
        observe(dir2, "NF", friday, "15:20", "fri_1520_reference_NF")
        val c2 = PremiumFollowOnCapture.readCarry(dir2, "NF", monday)
        val bound = PremiumCaptureCohort.select(raw("mon_0920_entry_NF"), "NF", monday, "09:20",
            PremiumCaptureCohort.INDEX_UNDERLYING["NF"], null,
            PremiumFollowOnCapture.REOPEN_WINDOW, c2.contracts, c2.expiry)
        assertEquals(PremiumCaptureCohort.SELECTED, bound.status)
        assertNull(PremiumFollowOnCapture.cohortMatchesCarry(bound.cohort, c2))
        assertTrue(PremiumFollowOnCapture.store(dir2)
            .putCohort("NF", monday, requireNotNull(bound.cohort)).persisted)
        assertEquals(PremiumCaptureSession.OBSERVED,
            observe(dir2, "NF", monday, "09:20", "mon_0920_entry_NF").slotResult?.status)
    }

    @Test
    fun aTamperedCarryIsRefusedByItsOwnDigest() {
        val filesDir = researchDir("followon-tamper")
        observe(filesDir, "NF", friday, "15:20", "fri_1520_reference_NF")
        val f = File(PremiumFollowOnCapture.root(filesDir), "carry_NF.json")
        val body = JSONObject(f.readText())
        body.getJSONArray("contracts").getJSONObject(0).put("strike", 99999.0)
        f.writeText(body.toString())
        assertEquals(PremiumFollowOnCapture.CARRY_DIGEST_MISMATCH,
            PremiumFollowOnCapture.readCarry(filesDir, "NF", monday).status)
        assertEquals(PremiumFollowOnCapture.CARRY_DIGEST_MISMATCH,
            observe(filesDir, "NF", monday, "09:20", "mon_0920_entry_NF").outcome)
    }

    @Test
    fun withNoCloseCaptureThereIsNoCarryAndTheReopenIsRefused() {
        val filesDir = researchDir("followon-nocarry")
        val a = observe(filesDir, "NF", monday, "09:20", "mon_0920_entry_NF")
        assertEquals(PremiumFollowOnCapture.CARRY_ABSENT, a.outcome)
        assertNull(a.slotResult)
        assertNull(PremiumFollowOnCapture.store(filesDir).readCohortText("NF", monday))
    }

    @Test
    fun aCarryFromTheSameSessionIsNotAPriorSession() {
        val filesDir = researchDir("followon-sameday")
        observe(filesDir, "NF", friday, "15:20", "fri_1520_reference_NF")
        assertEquals(PremiumFollowOnCapture.CARRY_FROM_THIS_SESSION,
            PremiumFollowOnCapture.readCarry(filesDir, "NF", friday).status)
    }

    // ───────────────────────── separation from the pilot ─────────────────────────

    @Test
    fun theFollowOnTreeIsSeparateAndTheExportCarriesBothApart() {
        val filesDir = researchDir("followon-separate")
        // the pilot captures its own midday slot, through its own path
        val pilotStore = PremiumCaptureStore(PremiumCaptureControl.root(filesDir))
        val pilot = PremiumCaptureSession(pilotStore).observeSlot(
            "NF", friday, raw("mon_0920_entry_NF"), "09:20", "09:20",
            PremiumCaptureCohort.INDEX_UNDERLYING["NF"], reqMs, respMs,
            claimedLot = 75, claimedLotSource = "CONTRACT_LOT_TABLE",
            lotTableRef = "contract_lot_table/x/$friday")
        assertEquals(PremiumCaptureSession.OBSERVED, pilot.status)
        // and the follow-on captures the close, into its own tree
        observe(filesDir, "NF", friday, "15:20", "fri_1520_reference_NF")

        val pilotRoot = PremiumCaptureControl.root(filesDir)
        val followOnRoot = PremiumFollowOnCapture.root(filesDir)
        assertNotEquals(pilotRoot.canonicalPath, followOnRoot.canonicalPath)
        assertFalse("the follow-on tree must not be inside the pilot's",
            followOnRoot.canonicalPath.startsWith(pilotRoot.canonicalPath + File.separator))
        assertTrue(pilotStore.globalBytes() > 0)
        assertTrue(PremiumCaptureStore(followOnRoot).globalBytes() > 0)

        // the pilot's own export sees ONLY its 09:20 slot
        val pilotSection = PremiumExportSection.build(filesDir, friday)
        val pilotSlots = pilotSection.getJSONObject("indices").getJSONObject("NF")
            .getJSONArray("slot_envelopes").let { a ->
                (0 until a.length()).map { a.getJSONObject(it).getString("slot") }
            }
        assertEquals(listOf("09:20"), pilotSlots)

        // and the follow-on section sees only the 15:20 slot, under its own key and purpose
        val follow = PremiumFollowOnCapture.exportSection(filesDir, friday)
        assertEquals(PremiumFollowOnCapture.PURPOSE, follow.getString("purpose"))
        assertEquals(PremiumFollowOnCapture.DIR_NAME, follow.getString("store_root"))
        assertTrue(follow.getJSONObject("pilot_window")
            .getBoolean("independent_of_this_capture"))
        val followSlots = follow.getJSONObject("section").getJSONObject("indices")
            .getJSONObject("NF").getJSONArray("slot_envelopes").let { a ->
                (0 until a.length()).map { a.getJSONObject(it).getString("slot") }
            }
        assertEquals(listOf("15:20"), followSlots)
        assertTrue(follow.getLong("bytes") > 0)
        assertEquals(friday, follow.getJSONObject("carries").getJSONObject("NF")
            .getString("session_date"))
        assertEquals(PremiumFollowOnCapture.CARRY_ABSENT,
            follow.getJSONObject("carries").getJSONObject("BNF").getString("state"))
    }

    @Test
    fun bothIndicesCarryAndReopenIndependently() {
        val filesDir = researchDir("followon-both")
        for ((ix, closeId, openId) in listOf(
            Triple("NF", "fri_1520_reference_NF", "mon_0920_entry_NF"),
            Triple("BNF", "fri_1520_reference_BNF", "mon_0920_entry_BNF"))) {
            val close = observe(filesDir, ix, friday, "15:20", closeId)
            assertEquals("$ix close", PremiumCaptureSession.OBSERVED, close.slotResult?.status)
            val carry = PremiumFollowOnCapture.readCarry(filesDir, ix, monday)
            assertTrue("$ix carry", carry.usable)
            val reopen = observe(filesDir, ix, monday, "09:20", openId)
            assertEquals("$ix reopen", PremiumCaptureSession.OBSERVED, reopen.slotResult?.status)
            assertEquals("$ix pinned", carry.strikes, JSONObject(requireNotNull(
                PremiumFollowOnCapture.store(filesDir).readCohortText(ix, monday)))
                .getJSONArray("paired_strikes").let { a ->
                    (0 until a.length()).map { a.getDouble(it) } })
        }
        // one index's carry is never read for the other
        val nf = requireNotNull(PremiumFollowOnCapture.readCarry(filesDir, "NF", monday).body)
        val bnf = requireNotNull(PremiumFollowOnCapture.readCarry(filesDir, "BNF", monday).body)
        assertNotEquals(nf.getString("underlying_key"), bnf.getString("underlying_key"))
        assertNotEquals(nf.getJSONArray("contracts").getJSONObject(0).getString("instrument_key"),
            bnf.getJSONArray("contracts").getJSONObject(0).getString("instrument_key"))
    }

    // ───────────────────────── the poll and export integration ─────────────────────────

    private fun source(path: String): String =
        listOf(File(path), File("app/$path"), File("../$path")).first { it.isFile }.readText()

    private fun stripKotlinComments(src: String): String = src.lineSequence()
        .map { it.substringBefore("//") }
        .filterNot { it.trimStart().startsWith("*") || it.trimStart().startsWith("/*") }
        .joinToString("\n")

    /**
     * The follow-on hook must sit in the real poll, inside the Throwable-guarded wrapper, behind
     * its own control, and make no request of its own — the same contract the pilot hook already
     * holds, asserted the same way, on the real `MarketWatchService` source.
     */
    @Test
    fun theFollowOnHookIsWiredIntoThePollAndCannotFailIt() {
        val src = stripKotlinComments(
            source("src/main/java/com/marketradar/app/MarketWatchService.kt"))
        assertTrue("the follow-on capture must be invoked from the poll",
            src.contains("PremiumFollowOnCapture.observe("))
        assertTrue("and gated by its own research control",
            src.contains("PremiumFollowOnCapture.isResearchEnabled(filesDir)"))
        // the block is inside a premiumCaptureSafely wrapper, after the pilot hook
        val pilotAt = src.indexOf("PremiumCaptureCoordinator.onChainsAcquired(")
        val followAt = src.indexOf("PremiumFollowOnCapture.observe(")
        val guardBefore = src.lastIndexOf("premiumCaptureSafely {", followAt)
        assertTrue("the pilot hook must be found", pilotAt > 0)
        assertTrue("the follow-on hook must come after the pilot's", followAt > pilotAt)
        assertTrue("the follow-on hook must be inside a premiumCaptureSafely wrapper",
            guardBefore > pilotAt)
        // and it must reuse the chains already in hand rather than fetch anything
        val block = src.substring(guardBefore, src.indexOf("if (quotesJson == null)", followAt))
        for (banned in listOf("fetchSync(", "async(", "SupabaseClient", "AlarmManager",
            "WorkManager", "startService", "Thread(", "okhttp")) {
            assertFalse("the follow-on block must not contain $banned", block.contains(banned))
        }
        assertTrue("it must reuse the existing chain responses",
            block.contains("chain = bnfChainJson") && block.contains("chain = nfChainJson"))
        assertTrue("and run before the quotes abort that would discard good chains",
            followAt < src.indexOf("POLL_FAIL: Quotes fetch returned null"))
    }

    /**
     * The normal export assembly must carry the two streams under separate top-level keys, and
     * the research stream must never truncate or refuse the pilot's.
     */
    @Test
    fun theNormalExportAssemblyCarriesBothStreamsApart() {
        val bridge = stripKotlinComments(
            source("src/main/java/com/marketradar/app/NativeBridge.kt"))
        assertTrue("the follow-on section must be spliced under its own key",
            bridge.contains("\"premium_followon\")"))
        assertTrue("behind its own control",
            bridge.contains("PremiumFollowOnCapture.isResearchEnabled(context.filesDir)"))
        assertTrue("and reported in the result",
            bridge.contains(".put(\"premium_followon\", JSONObject()"))

        // and the real assembly, exercised: a bundle spliced with both sections
        val filesDir = researchDir("followon-assembly")
        val pilotStore = PremiumCaptureStore(PremiumCaptureControl.root(filesDir))
        assertEquals(PremiumCaptureSession.OBSERVED, PremiumCaptureSession(pilotStore).observeSlot(
            "NF", friday, raw("mon_0920_entry_NF"), "09:20", "09:20",
            PremiumCaptureCohort.INDEX_UNDERLYING["NF"], reqMs, respMs,
            claimedLot = 75, claimedLotSource = "CONTRACT_LOT_TABLE",
            lotTableRef = "contract_lot_table/x/$friday").status)
        observe(filesDir, "NF", friday, "15:20", "fri_1520_reference_NF")

        val bundle = JSONObject().put("manifest", JSONObject().put("status", "OK")
            .put("session_date_used", friday)).toString()
        val pilot = PremiumExportSection.build(filesDir, friday)
        val follow = PremiumFollowOnCapture.exportSection(filesDir, friday)
        val first = PremiumExportSection.mergeWithinCap(
            bundle, bundle.toByteArray(Charsets.UTF_8), pilot)
        assertTrue(first.merged && !first.overCap)
        val both = PremiumExportSection.mergeWithinCap(
            String(first.bytes, Charsets.UTF_8), first.bytes, follow, "premium_followon")
        assertTrue(both.merged && !both.overCap)
        val doc = JSONObject(String(both.bytes, Charsets.UTF_8))
        assertTrue("the pilot stream keeps its own key", doc.has("premium"))
        assertTrue("and the follow-on gets its own", doc.has("premium_followon"))
        assertEquals("the existing manifest must be untouched", "OK",
            doc.getJSONObject("manifest").getString("status"))
        // the two streams carry DIFFERENT slots, which is how a reader tells them apart
        val pilotSlots = doc.getJSONObject("premium").getJSONObject("indices")
            .getJSONObject("NF").getJSONArray("slot_envelopes").let { a ->
                (0 until a.length()).map { a.getJSONObject(it).getString("slot") } }
        val followSlots = doc.getJSONObject("premium_followon").getJSONObject("section")
            .getJSONObject("indices").getJSONObject("NF").getJSONArray("slot_envelopes")
            .let { a -> (0 until a.length()).map { a.getJSONObject(it).getString("slot") } }
        assertEquals(listOf("09:20"), pilotSlots)
        assertEquals(listOf("15:20"), followSlots)
        assertEquals(PremiumFollowOnCapture.PURPOSE,
            doc.getJSONObject("premium_followon").getString("purpose"))
    }

    /**
     * The export must carry what the Stage G matcher reads: a receipt clock per acquisition, the
     * two exchange-clock fields, the pinned identity and the underlying with its provenance.
     * Asserted here on the real producer's output, so the Python matcher's fixture is this
     * shape rather than a hand-written approximation.
     */
    @Test
    fun theFollowOnExportCarriesTheClockAndContractProvenanceStageGReads() {
        val filesDir = researchDir("followon-provenance")
        observe(filesDir, "NF", friday, "15:20", "fri_1520_reference_NF")
        val block = PremiumFollowOnCapture.exportSection(filesDir, friday)
            .getJSONObject("section").getJSONObject("indices").getJSONObject("NF")
        val env = block.getJSONArray("slot_envelopes").getJSONObject(0)
        val clocks = env.getJSONObject("clocks")
        assertEquals(respMs, clocks.getLong("acquisition_response_ms"))
        assertTrue(clocks.has("vendor_source_ts"))
        assertTrue(clocks.has("quote_event_clock_verified"))
        assertEquals(friday, env.getString("session_date"))
        assertEquals("15:20", env.getString("slot"))
        assertTrue(env.getJSONObject("underlying").getJSONObject("spot_raw").has("raw"))
        val rec = block.getJSONArray("records").getJSONObject(0)
        assertTrue(rec.getJSONObject("bbo").has("bid_raw"))
        assertTrue(rec.getJSONObject("bbo").has("ask_raw"))
        assertTrue(rec.getJSONObject("identity").getBoolean("identity_verified"))
        val pin = block.getJSONObject("references").getJSONArray("pinned_contracts")
            .getJSONObject(0)
        for (k in listOf("instrument_key", "option_type", "strike", "expiry")) {
            assertTrue("pinned contracts must carry $k", pin.has(k))
        }
    }
}
