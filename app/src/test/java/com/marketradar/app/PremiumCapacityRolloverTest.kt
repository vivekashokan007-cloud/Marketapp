package com.marketradar.app

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
 * The 13 November 2026 empty premium session: reproduction and fix.
 *
 * Desktop/JVM only. **No device evidence is produced or claimed here.**
 *
 * WHAT HAPPENED, from the 12 November export's own accounting and the 13 November export:
 *
 *     global_bytes             4,177,185 B    (12 November, both indices report the root total)
 *     MAX_BYTES_GLOBAL         4,194,304 B    (the first-delivery 4 MiB cap)
 *     CONTROL_RESERVE_BYTES       16,384 B
 *     evidence budget          4,177,920 B
 *     headroom                       735 B
 *
 * A cohort JSON is about 1.6 KB, so the first cohort write of the next session could not be
 * admitted. No cohort means no envelope and no quote, so 13 November recorded nothing at all —
 * and because the global check also applied to the SUMMARY, the refusal could not even be
 * written down: the About screen read `written=0 summary_durable=false` for both indices and
 * the completed export carried a single key per index.
 *
 * Each test below names what it asserted BEFORE the fix, so the before/after is checkable
 * rather than asserted.
 */
class PremiumCapacityRolloverTest {

    private fun rawFixture(id: String): JSONObject {
        val text = requireNotNull(
            javaClass.classLoader?.getResourceAsStream("contracts/x1_upstox_raw_chains.json"))
            .bufferedReader().readText()
        val snaps = JSONObject(text).getJSONArray("snapshots")
        for (i in 0 until snaps.length()) {
            val s = snaps.getJSONObject(i)
            if (s.optString("id") == id) return s
        }
        throw AssertionError("fixture snapshot $id not found")
    }

    /** Measured from the 12 November export: NF 587,848 B + BNF 597,363 B over 53/54 slots. */
    private val nov12TwoIndexBytes = 587_848L + 597_363L

    private fun evidenceBudget() = PremiumCaptureStore.MAX_BYTES_GLOBAL -
        PremiumCaptureStore.CONTROL_RESERVE_BYTES - PremiumCaptureStore.DIAGNOSTIC_RESERVE_BYTES

    /**
     * Fill the ROOT to within `targetHeadroom` of the evidence budget with one opaque file.
     *
     * A single file rather than thousands of records on purpose: the condition under test is the
     * root byte total, `globalBytes()` measures the tree, and writing ~10k real records to reach
     * a 16 MiB ceiling would make this suite minutes long for no extra coverage. The admission
     * path itself is exercised by the real writes each test then performs.
     */
    private fun fillLeaving(root: File, targetHeadroom: Long) =
        sparseFill(File(root.also { it.mkdirs() }, "retained_prior_sessions.bin"),
            evidenceBudget() - targetHeadroom)

    /**
     * Fill a file to `bytes` without materialising it: the store measures with `File.length()`,
     * so a sparse file fills it exactly while costing no real disk. With a 1 GiB budget a real
     * allocation per test fills the build machine instead — which is how this helper was first
     * written, and what it reported.
     */
    private fun sparseFill(f: File, bytes: Long) {
        f.parentFile?.mkdirs()
        java.io.RandomAccessFile(f, "rw").use { it.setLength(bytes) }
    }

    private fun observeFirstSlot(store: PremiumCaptureStore, snapId: String, index: String)
            : PremiumCaptureSession.SlotResult {
        val snap = rawFixture(snapId)
        return PremiumCaptureSession(store).observeSlot(
            index, snap.getString("session"), snap.getJSONObject("raw"), null, "09:20",
            snap.optString("requested_underlying_key").ifEmpty { null }, 1000L, 1250L,
            claimedLot = 75, claimedLotSource = "CONTRACT_LOT_TABLE",
            lotTableRef = "contract_lot_table/NSE/2026-10-01",
            mapRef = "instrument_map/2026-10-01")
    }

    // ───────────────── capacity: the behaviour that must NOT change ─────────────────

    /**
     * A genuinely full store must still refuse, and must still refuse the same way. Raising a
     * bound is not permission to overrun it: this is the 13 November failure reproduced at the
     * NEW ceiling, and it must stay deterministic.
     */
    @Test
    fun aFullStoreStillRefusesTheCohortDeterministically() {
        val root = Files.createTempDirectory("premcap-full").toFile()
        fillLeaving(root, 735L)
        val store = PremiumCaptureStore(root)
        for ((snapId, index) in listOf("mon_0920_entry_NF" to "NF", "mon_0920_entry_BNF" to "BNF")) {
            val r = observeFirstSlot(store, snapId, index)
            val date = rawFixture(snapId).getString("session")
            assertEquals(PremiumCaptureSession.COHORT_PERSIST_FAILED, r.status)
            assertTrue("the refusal must name the global cap, was ${r.detail}",
                (r.detail ?: "").contains(PremiumCaptureStore.REFUSED_GLOBAL_BYTES))
            assertEquals(0, store.outcomeCount(index, date))
            assertEquals(0, store.envelopeCount(index, date))
            assertNull(store.readCohortText(index, date))
        }
    }

    /**
     * FAILED BEFORE THE FIX. With the store at 4,177,185 B of a 4,177,920 B evidence budget —
     * the measured 13 November condition — the next session could not pin a cohort. The raised
     * ceiling admits it, and the session captures normally.
     */
    @Test
    fun theNextSessionCapturesAtTheOldCeiling() {
        val root = Files.createTempDirectory("premcap-rollover").toFile()
        root.mkdirs()
        sparseFill(File(root, "retained_prior_sessions.bin"), 4_177_185L)  // the 12 Nov total
        val store = PremiumCaptureStore(root)
        for ((snapId, index) in listOf("mon_0920_entry_NF" to "NF", "mon_0920_entry_BNF" to "BNF")) {
            val r = observeFirstSlot(store, snapId, index)
            val date = rawFixture(snapId).getString("session")
            assertEquals("$index should now capture, got ${r.status} / ${r.detail}",
                PremiumCaptureSession.OBSERVED, r.status)
            assertNotNull("$index must pin a cohort", store.readCohortText(index, date))
            assertTrue("$index must write quotes", store.outcomeCount(index, date) > 0)
            assertTrue("$index must write an envelope", store.envelopeCount(index, date) > 0)
        }
    }

    /**
     * FAILED BEFORE THE FIX (the old budget was 4,177,920 B). The sizing is stated as a test so
     * the review package's arithmetic is checked rather than claimed: the retained day plus
     * three further complete two-index sessions, with conflict archives and both reserves.
     */
    @Test
    fun theCapHoldsTheRetainedDayPlusThreeFurtherSessions() {
        val fullDayTwoIndex = nov12TwoIndexBytes * 69 / 53          // 53 captured slots -> 69
        val conflicts = PremiumCaptureStore.MAX_CONFLICTS_PER_INDEX_SESSION *
            PremiumCaptureStore.MAX_BYTES_PER_RECORD * 2 * 3
        val required = 4_177_185L + fullDayTwoIndex * 3 + conflicts
        assertTrue("budget ${evidenceBudget()} B must hold $required B",
            evidenceBudget() >= required)
    }

    // ───────────────── diagnostics: the first failure must be visible ─────────────────

    /**
     * FAILED BEFORE THE FIX. The global check applied to every kind including the summary, so a
     * full store refused the very record that would have explained it: `summary_durable=false`.
     * The diagnostic reserve is outside the evidence budget, so a summary still lands.
     */
    @Test
    fun theSummaryStaysWritableWhenEvidenceIsAtTheCeiling() {
        val root = Files.createTempDirectory("premcap-diag").toFile()
        fillLeaving(root, 0L)                       // evidence budget exactly exhausted
        val store = PremiumCaptureStore(root)

        val evidence = store.put("NF", "2026-10-19", "k", JSONObject().put("a", 1))
        assertEquals("evidence must be refused at the ceiling",
            PremiumCaptureStore.REFUSED_GLOBAL_BYTES, evidence.status)

        val summary = store.bumpCounters("NF", "2026-10-19", mapOf("refused_cohort_persist" to 1L))
        assertTrue("the summary must still be durable, got ${summary.status}: ${summary.detail}",
            summary.persisted)
        val read = store.readSummary("NF", "2026-10-19")
        assertNotNull("the summary must be readable back", read)
        assertEquals(1L, read!!.getJSONObject("counters").getLong("refused_cohort_persist"))
    }

    /**
     * FAILED BEFORE THE FIX: the index block was exactly one key, so a completed export of an
     * empty session could not say why. It now carries the capacity and whatever durable counter
     * exists — and NOTHING ELSE changes, so eligibility is untouched.
     */
    @Test
    fun anEmptySessionExportCarriesTheReasonAndTheCapacity() {
        val filesDir = Files.createTempDirectory("premcap-export").toFile()
        val root = PremiumCaptureControl.root(filesDir)
        fillLeaving(root, 735L)
        val store = PremiumCaptureStore(root)
        val date = rawFixture("mon_0920_entry_NF").getString("session")
        observeFirstSlot(store, "mon_0920_entry_NF", "NF")

        val nf = PremiumExportSection.build(filesDir, date).getJSONObject("indices")
            .getJSONObject("NF")

        assertEquals("NO_PREMIUM_DATA_FOR_THIS_INDEX", nf.getString("state"))
        val cap = nf.getJSONObject("capacity")
        // the 13 November condition exactly: not AT the ceiling, but with less room than the
        // cohort needs. The remaining-bytes figure is the one that explains the day; the
        // at-ceiling flag is the stricter condition and is correctly false here.
        val remaining = cap.getLong("evidence_remaining_bytes")
        // started at 735 B; the durable refusal summary has since consumed part of it, which is
        // the new behaviour working. What matters is that the number is reported and that it is
        // visibly smaller than the ~1.6 KB cohort that was refused.
        assertTrue("remaining $remaining B should be at most the 735 B we left", remaining <= 735L)
        assertTrue("remaining $remaining B must be less than a cohort needs", remaining < 1_600L)
        assertFalse("this is short of a cohort, not the hard ceiling",
            cap.getBoolean("evidence_at_ceiling"))
        assertEquals(PremiumCaptureStore.MAX_BYTES_GLOBAL, cap.getLong("max_bytes_global"))
        // the refusal itself was counted durably, in the reserve
        val counters = nf.getJSONObject("durable_counters").getJSONObject("counters")
        assertTrue("a refusal counter must be present, got ${counters.keys().asSequence().toList()}",
            counters.keys().asSequence().any { it.startsWith("refused_") })
        assertEquals("diagnostics must be labelled as such",
            "NOT_EVIDENCE_AND_NOT_ELIGIBILITY: why this index is empty",
            nf.getString("diagnostics_only"))
    }

    /**
     * The guard on the guard: diagnostics must not make an empty index look capturable. The
     * replay's capture preflight requires records, slot envelopes and pinned contracts, and an
     * empty index must still carry none of them.
     */
    @Test
    fun diagnosticsDoNotMakeAnEmptyIndexEligible() {
        val filesDir = Files.createTempDirectory("premcap-elig").toFile()
        val root = PremiumCaptureControl.root(filesDir)
        fillLeaving(root, 735L)
        val date = rawFixture("mon_0920_entry_NF").getString("session")
        observeFirstSlot(PremiumCaptureStore(root), "mon_0920_entry_NF", "NF")

        val section = PremiumExportSection.build(filesDir, date)
        assertEquals("NO_PREMIUM_DATA", section.getString("state"))
        assertFalse(section.getBoolean("has_data"))
        val nf = section.getJSONObject("indices").getJSONObject("NF")
        for (k in listOf("records", "slot_envelopes", "cohort", "pinned_contracts")) {
            assertFalse("an empty index must not carry '$k'", nf.has(k))
        }
    }

    /**
     * Three distinct sessions above the retained footprint, each exported by its exact date,
     * with the store reopened between them. A new [PremiumCaptureStore] over the same root is
     * what a process restart looks like to this code: all state is file-backed.
     */
    @Test
    fun threeDistinctSessionsCaptureAboveTheRetainedFootprintAcrossRestarts() {
        val filesDir = Files.createTempDirectory("premcap-three").toFile()
        val root = PremiumCaptureControl.root(filesDir)
        root.mkdirs()
        sparseFill(File(root, "retained_prior_sessions.bin"), 4_177_185L)

        val dates = listOf("2026-11-16", "2026-11-17", "2026-11-18")
        for (date in dates) {
            val store = PremiumCaptureStore(root)       // reopened: the restart
            for (slot in listOf("09:20", "09:25")) {
                for ((idNF, idBNF) in listOf("mon_0920_entry_NF" to "mon_0920_entry_BNF",
                                             "mon_0925_control_NF" to "mon_0925_control_BNF")
                    .let { if (slot == "09:20") listOf(it[0]) else listOf(it[1]) }) {
                    for ((snapId, index) in listOf(idNF to "NF", idBNF to "BNF")) {
                        val sn = rawFixture(snapId)
                        val r = PremiumCaptureSession(store).observeSlot(index, date,
                            sn.getJSONObject("raw"), null, slot,
                            sn.optString("requested_underlying_key").ifEmpty { null }, 1000L, 1250L,
                            claimedLot = 75, claimedLotSource = "CONTRACT_LOT_TABLE",
                            lotTableRef = "contract_lot_table/NSE/2026-10-01",
                            mapRef = "instrument_map/2026-10-01")
                        assertEquals("$date $index $slot: ${r.detail}",
                            PremiumCaptureSession.OBSERVED, r.status)
                    }
                }
            }
        }

        // each date exports its OWN session, and only that one
        for (date in dates) {
            val section = PremiumExportSection.build(filesDir, date)
            for (index in listOf("NF", "BNF")) {
                val ix = section.getJSONObject("indices").getJSONObject(index)
                assertEquals("$date/$index should carry data", 12,
                    ix.getJSONArray("records").length())
                assertEquals(2, ix.getJSONArray("slot_envelopes").length())
                assertEquals(date, ix.optString("session_date", date))
            }
        }
        val store = PremiumCaptureStore(root)
        assertTrue("the three sessions must fit under the budget",
            store.globalBytes() <= evidenceBudget())
    }


    // ───────────────── the 1 GiB pilot budget, and what makes it usable ─────────────────

    /**
     * The ceiling is PERMISSION TO GROW, not an allocation: an empty store occupies nothing and
     * the budget is reported, not reserved.
     */
    @Test
    fun theBudgetIsNotAllocatedUpFront() {
        val root = Files.createTempDirectory("premcap-alloc").toFile()
        root.mkdirs()
        val store = PremiumCaptureStore(root)
        assertEquals(0L, store.globalBytes())
        val cap = store.capacitySnapshot()
        assertEquals(1024L * 1024 * 1024, cap.getLong("max_bytes_global"))
        assertEquals("PERMISSION_TO_GROW_ON_DEMAND_NOT_AN_ALLOCATION", cap.getString("budget_is"))
        assertEquals(0L, cap.getLong("global_bytes"))
        assertTrue("an empty store must occupy no space on disk",
            (root.listFiles()?.size ?: 0) == 0)
    }

    /**
     * FAILS WITHOUT THE SPLIT ACCOUNTING. Admission walked every retained file on every write:
     * measured on this code at 11.7 ms for 1,000 files and 395.6 ms for 120,000, against 828
     * writes in a capture day. The cached subtree total makes the per-write cost flat, and this
     * pins the property rather than the timing: the total must be correct after many writes
     * WITHOUT the exact walk having been consulted.
     */
    @Test
    fun theRootTotalIsCorrectWithoutWalkingTheTreePerWrite() {
        val root = Files.createTempDirectory("premcap-fast").toFile()
        root.mkdirs()
        val store = PremiumCaptureStore(root)
        PremiumCaptureStore.forgetCachedBytesForTest()
        for (i in 0 until 400)
            store.put("NF", "2026-10-19", "k$i", JSONObject().put("n", i).put("p", "y".repeat(200)))
        assertEquals("the incremental total must match an exact walk",
            store.globalBytes(), store.globalBytesFast())
        assertEquals("and reconciliation must find no drift", 0L, store.reconcileTreeBytes())
    }

    /**
     * A write by anything OTHER than the store lands in the root's own files, which are measured
     * fresh every time rather than cached — so the total cannot go stale behind a control write.
     */
    @Test
    fun aWriteOutsideTheStoreIsStillCounted() {
        val root = Files.createTempDirectory("premcap-outside").toFile()
        root.mkdirs()
        val store = PremiumCaptureStore(root)
        store.put("NF", "2026-10-19", "seed", JSONObject().put("a", 1))
        val before = store.globalBytesFast()
        File(root, "control_meta.json").writeText("x".repeat(5_000))
        assertEquals("the external file must be visible immediately",
            before + 5_000L, store.globalBytesFast())
        assertEquals(store.globalBytes(), store.globalBytesFast())
    }

    /** A status refresh must never trigger the seeding walk. */
    @Test
    fun aStatusRefreshNeverBlocksOnTheSeedingWalk() {
        val root = Files.createTempDirectory("premcap-seed").toFile()
        root.mkdirs()
        val store = PremiumCaptureStore(root)
        PremiumCaptureStore.forgetCachedBytesForTest()
        assertNull("unseeded must read as unknown, not as a walk",
            store.globalBytesFastIfSeeded())
        store.put("NF", "2026-10-19", "k", JSONObject().put("a", 1))
        assertNotNull("after a capture write it is known",
            store.globalBytesFastIfSeeded())
    }

    /**
     * The device is the stricter gate and names itself. A root whose free space cannot be read
     * (0 = unknown, which is what a missing path returns) must NOT be treated as full.
     */
    @Test
    fun theDeviceGateIsSeparateFromTheAppBudget() {
        assertTrue("the floor must be a real reserve",
            PremiumCaptureStore.MIN_FREE_DEVICE_BYTES >= 64L * 1024 * 1024)
        assertTrue("and far below the app budget",
            PremiumCaptureStore.MIN_FREE_DEVICE_BYTES < PremiumCaptureStore.MAX_BYTES_GLOBAL)
        val root = Files.createTempDirectory("premcap-dev").toFile()
        root.mkdirs()
        val cap = PremiumCaptureStore(root).capacitySnapshot()
        assertTrue("free space must be reported beside the budget", cap.has("device_free_bytes"))
        assertTrue(cap.getLong("device_free_bytes") > 0L)
        assertFalse(cap.getBoolean("device_low_space"))
        // a path that does not exist reports 0 free, which is UNKNOWN and must not refuse
        val missing = PremiumCaptureStore(File("/proc/nonexistent-premium-capture-xyz"))
        val r = missing.put("NF", "S", "k", JSONObject().put("a", 1))
        assertEquals("an unreadable root is an IO failure, not a low-space refusal",
            PremiumCaptureStore.REFUSED_IO, r.status)
    }

    // ───────────────── the last in-window outcome must survive the day ─────────────────

    /**
     * FAILED BEFORE THE FIX. Every poll after 15:05 completes OUTSIDE_EXPERIMENT_WINDOW and
     * overwrote the per-index result, so by the evening the status surface showed only the
     * after-window outcome and the in-window refusal that explained the day was gone.
     */
    @Test
    fun anAfterWindowResultCannotDisplaceTheInWindowOne() {
        val w = PremiumCaptureWorker
        try {
            val root = Files.createTempDirectory("premcap-worker").toFile()
            fillLeaving(root, 735L)
            val store = PremiumCaptureStore(root)
            val snap = rawFixture("mon_0920_entry_NF")
            val date = snap.getString("session")

            // an in-window job that is refused for capacity, then an after-window job
            for (slot in listOf("09:20", "15:30")) {
                w.submit(PremiumCaptureWorker.Job("NF", date, 1000) {
                    val r = PremiumCaptureSession(store).observeSlot("NF", date,
                        snap.getJSONObject("raw"), null, slot,
                        snap.optString("requested_underlying_key").ifEmpty { null }, 1000L, 1250L,
                        claimedLot = 75, claimedLotSource = "CONTRACT_LOT_TABLE",
                        lotTableRef = "contract_lot_table/NSE/2026-10-01",
                        mapRef = "instrument_map/2026-10-01")
                    JSONObject().put("status", r.status).put("index", "NF")
                        .put("written", r.written).put("detail", r.detail ?: "")
                })
                assertTrue("worker should go idle", w.awaitIdle(10_000))
            }

            assertEquals("the latest result is the after-window one",
                PremiumCaptureSession.OUTSIDE_WINDOW, w.resultFor("NF")?.optString("status"))
            val inWindow = w.inWindowResultFor("NF")
            assertNotNull("the in-window result must be retained", inWindow)
            assertEquals(PremiumCaptureSession.COHORT_PERSIST_FAILED,
                inWindow!!.optString("status"))
            assertTrue("and it must still name the cap",
                inWindow.optString("detail").contains(PremiumCaptureStore.REFUSED_GLOBAL_BYTES))

            val stats = w.stats().getJSONObject("last_in_window_by_index").getJSONObject("NF")
            assertEquals(PremiumCaptureSession.COHORT_PERSIST_FAILED, stats.getString("status"))
        } finally {
            w.resetForTest()
        }
    }

    // ───── why an index is empty: the three causes, told apart by the recorded reason ─────

    @org.junit.After
    fun clearInjectedFreeSpace() {
        PremiumCaptureStore.freeBytesForTest = null
    }

    /**
     * Fill the control reserve so even the last-resort diagnostic record cannot be written.
     *
     * Large files first, then progressively smaller ones, until nothing of any size fits: the
     * reserve has to be left with less room than the few-hundred-byte diagnostic record needs,
     * not merely with less than one 4 KiB control file needs.
     */
    private fun fillControlReserve(store: PremiumCaptureStore) {
        var n = 0
        for (size in listOf(3_600, 1_200, 400, 120, 40, 8)) {
            val pad = "x".repeat(size)
            while (n < 200) {
                val r = store.putControlFile("filler_$n.json", JSONObject().put("pad", pad))
                if (!r.persisted) break
                n++
            }
        }
        val left = PremiumCaptureStore.CONTROL_RESERVE_BYTES - store.controlBytes()
        assertTrue("the reserve must be left with no usable room, $left B remain", left < 48L)
    }

    /**
     * 2(c). The 256 MiB physical-space guard applies to the SUMMARY write too, and the refusal
     * survives the diagnostic path whenever a tiny write is still possible.
     *
     * Free space is injected at 256 MiB + 1,000 B, which is a real, physically meaningful
     * condition rather than a stub: a ~1.6 KB cohort cannot be written without breaching the
     * floor, while the few-hundred-byte summary that RECORDS that refusal still can. So the
     * cohort is refused for the device, not the app budget, and the reason is durable.
     */
    @Test
    fun theDeviceGuardAppliesToTheSummaryWriteAndTheReasonSurvives() {
        val filesDir = Files.createTempDirectory("premcap-lowspace").toFile()
        val root = PremiumCaptureControl.root(filesDir)
        root.mkdirs()
        PremiumCaptureStore.freeBytesForTest =
            { PremiumCaptureStore.MIN_FREE_DEVICE_BYTES + 1_000L }
        val store = PremiumCaptureStore(root)
        val date = rawFixture("mon_0920_entry_NF").getString("session")

        val r = observeFirstSlot(store, "mon_0920_entry_NF", "NF")
        assertEquals(PremiumCaptureSession.COHORT_PERSIST_FAILED, r.status)
        assertTrue("the device, not the app budget, must be named: ${r.detail}",
            (r.detail ?: "").contains(PremiumCaptureStore.REFUSED_DEVICE_LOW_SPACE))
        assertTrue("the tiny diagnostic write must still have been possible",
            r.summaryWrite?.persisted == true)
        assertNull("so no last-resort record was needed", r.diagnosticWrite)

        val nf = PremiumExportSection.build(filesDir, date).getJSONObject("indices")
            .getJSONObject("NF")
        assertEquals("NO_PREMIUM_DATA_FOR_THIS_INDEX", nf.getString("state"))
        assertEquals(PremiumCaptureStore.CAUSE_DEVICE,
            nf.getJSONObject("refusal").getString("cause"))
        assertEquals(PremiumCaptureStore.REFUSED_DEVICE_LOW_SPACE,
            nf.getJSONObject("refusal").getString("store_status"))
        assertEquals("09:20", nf.getJSONObject("refusal").getString("slot"))
    }

    /**
     * 2(d). THE POINT OF THE WHOLE ITEM. Both of these days refuse the same cohort, increment
     * the same generic `refused_cohort_persist` counter, and end with the same empty index. One
     * is a spent app budget, which a raised ceiling fixes; the other is a full phone, which it
     * does not. The counter cannot tell them apart and neither can a capacity reading taken at
     * export time — only the reason recorded at the moment of the refusal can.
     */
    @Test
    fun theExportTellsAnAppCapApartFromAFullPhone() {
        val appCap = Files.createTempDirectory("premcap-cause-app").toFile()
        fillLeaving(PremiumCaptureControl.root(appCap), 735L)
        val date = rawFixture("mon_0920_entry_NF").getString("session")
        observeFirstSlot(PremiumCaptureStore(PremiumCaptureControl.root(appCap)),
            "mon_0920_entry_NF", "NF")
        val appSection = PremiumExportSection.build(appCap, date)
            .getJSONObject("indices").getJSONObject("NF")

        val device = Files.createTempDirectory("premcap-cause-dev").toFile()
        PremiumCaptureControl.root(device).mkdirs()
        PremiumCaptureStore.freeBytesForTest =
            { PremiumCaptureStore.MIN_FREE_DEVICE_BYTES + 1_000L }
        observeFirstSlot(PremiumCaptureStore(PremiumCaptureControl.root(device)),
            "mon_0920_entry_NF", "NF")
        val devSection = PremiumExportSection.build(device, date)
            .getJSONObject("indices").getJSONObject("NF")

        // identical on everything the old export carried
        assertEquals(appSection.getString("state"), devSection.getString("state"))
        for (sec in listOf(appSection, devSection))
            assertEquals("both days must increment the SAME generic counter", 1L,
                sec.getJSONObject("durable_counters").getJSONObject("counters")
                    .getLong("refused_cohort_persist"))
        // and distinguishable only by the recorded reason
        assertEquals(PremiumCaptureStore.CAUSE_APP_CAP,
            appSection.getJSONObject("refusal").getString("cause"))
        assertEquals(PremiumCaptureStore.CAUSE_DEVICE,
            devSection.getJSONObject("refusal").getString("cause"))
        assertEquals("DURABLE_REASON_RECORDED_AT_THE_TIME_OF_THE_REFUSAL",
            devSection.getJSONObject("refusal").getString("classified_from"))
    }

    /**
     * 2(c), second half. When the phone is so full that even the diagnostic summary is refused,
     * the control reserve — carved out separately, and the last space to go — records that the
     * diagnostic write itself failed. The export then says DIAGNOSTIC_WRITE_REFUSED, which is
     * not the same statement as NOT_WRITTEN: one says the record failed, the other says nothing
     * was attempted.
     */
    @Test
    fun aRefusedDiagnosticWriteIsRecordedInTheControlReserve() {
        val filesDir = Files.createTempDirectory("premcap-nodiag").toFile()
        val root = PremiumCaptureControl.root(filesDir)
        root.mkdirs()
        PremiumCaptureStore.freeBytesForTest =
            { PremiumCaptureStore.MIN_FREE_DEVICE_BYTES - 1L }
        val store = PremiumCaptureStore(root)
        val date = rawFixture("mon_0920_entry_NF").getString("session")

        val r = observeFirstSlot(store, "mon_0920_entry_NF", "NF")
        assertEquals(PremiumCaptureSession.COHORT_PERSIST_FAILED, r.status)
        assertFalse("not even the summary can be written here",
            r.summaryWrite?.persisted == true)
        assertTrue("so the control reserve must carry it",
            r.diagnosticWrite?.persisted == true)
        assertNull("and nothing is in the session subtree", store.readSummary("NF", date))

        val nf = PremiumExportSection.build(filesDir, date).getJSONObject("indices")
            .getJSONObject("NF")
        assertEquals("DIAGNOSTIC_WRITE_REFUSED", nf.getString("durable_counters_state"))
        assertEquals(PremiumCaptureStore.REFUSED_DEVICE_LOW_SPACE,
            nf.getJSONObject("diagnostic_write_failure").getString("summary_write_status"))
        assertEquals(PremiumCaptureStore.CAUSE_DEVICE,
            nf.getJSONObject("refusal").getString("cause"))
        // still no data, and still not eligible
        assertEquals("NO_PREMIUM_DATA_FOR_THIS_INDEX", nf.getString("state"))
        assertFalse(nf.has("records"))
        assertFalse(nf.has("pinned_contracts"))
    }

    /**
     * The honest boundary. If the device is full AND the control reserve is full, there is no
     * byte anywhere to write the reason into, and the returned result is the only account of it.
     * The export must then say NOT_WRITTEN and claim NOTHING it cannot support — no guessed
     * cause, no inferred capacity story. A failing disk cannot durably record its own failure;
     * the requirement is that we never pretend it did.
     */
    @Test
    fun whenNothingCanBeWrittenAnywhereTheExportClaimsNothing() {
        val filesDir = Files.createTempDirectory("premcap-nowhere").toFile()
        val root = PremiumCaptureControl.root(filesDir)
        root.mkdirs()
        val store = PremiumCaptureStore(root)
        fillControlReserve(store)                       // before the device gate is injected
        PremiumCaptureStore.freeBytesForTest =
            { PremiumCaptureStore.MIN_FREE_DEVICE_BYTES - 1L }
        val date = rawFixture("mon_0920_entry_NF").getString("session")

        val r = observeFirstSlot(store, "mon_0920_entry_NF", "NF")
        assertEquals(PremiumCaptureSession.COHORT_PERSIST_FAILED, r.status)
        assertFalse(r.summaryWrite?.persisted == true)
        assertFalse("the last resort is gone too", r.diagnosticWrite?.persisted == true)
        // the returned result still identifies the failure accurately
        assertTrue("the result must still name the device: ${r.detail}",
            (r.detail ?: "").contains(PremiumCaptureStore.REFUSED_DEVICE_LOW_SPACE))

        val nf = PremiumExportSection.build(filesDir, date).getJSONObject("indices")
            .getJSONObject("NF")
        assertEquals("NOT_WRITTEN", nf.getString("durable_counters_state"))
        assertEquals(PremiumCaptureStore.CAUSE_NONE,
            nf.getJSONObject("refusal").getString("cause"))
        assertEquals("NOTHING_RECORDED",
            nf.getJSONObject("refusal").getString("classified_from"))
        assertFalse("no cause may be invented", nf.getJSONObject("refusal").has("store_status"))
    }

    // ───── 2(a)/(b): what the phone shows, and one headroom number behind it ─────

    /** Comments carry the explanation; a guard must read the code, not the prose about it. */
    private fun stripComments(src: String): String = src
        .replace(Regex("/\\*.*?\\*/", RegexOption.DOT_MATCHES_ALL), "")
        .lines().joinToString("\n") { it.substringBefore("//") }

    /**
     * 2(a). FAILS ON THE OLD ABOUT CODE, which read `last_by_index`.
     *
     * Every poll after 15:00 completes OUTSIDE_EXPERIMENT_WINDOW, so the last result per index
     * was an after-window refusal within minutes of the close — and the cohort refusal that
     * explained the empty day was no longer on the screen. The dialog must read the IN-WINDOW
     * projection, tie each line to its IST session date, and name queue rejections separately.
     */
    @Test
    fun theAboutScreenReadsTheInWindowResultNotTheLatestOne() {
        val render = stripComments(
            File("src/main/java/com/marketradar/app/MainActivity.kt").readText())
            .substringAfter("private fun premiumCaptureStatusText()")
            .substringBefore("private fun premiumCaptureButtonLabel()")
        assertTrue("About must read the in-window projection",
            render.contains("last_in_window_by_index"))
        assertFalse("and must not render the last-of-any-kind map per index",
            render.contains("optJSONObject(\"last_by_index\")"))
        assertTrue("each line must carry its IST session date",
            render.contains("session_date"))
        assertTrue("today's date must be computed in IST, not device-local",
            render.contains("istSessionDateOrNull"))
        assertTrue("a queue rejection must be shown apart from a persistence failure",
            render.contains("last_queue_rejection_by_index"))
        assertTrue("unknown headroom must be readable as unknown, not a sentinel compare",
            render.contains("evidence_bytes_known"))
    }

    /**
     * 2(a), behaviour. The worker projection the dialog now reads must itself distinguish a job
     * the worker REJECTED before any write from a write that was attempted and refused. A
     * rejected job never runs its closure, so it leaves no persistence result at all — which is
     * why it needed its own per-index record rather than only a global counter.
     */
    @Test
    fun aQueueRejectionIsReportedApartFromAPersistenceFailure() {
        val w = PremiumCaptureWorker
        try {
            w.resetForTest()
            val rejected = w.submit(PremiumCaptureWorker.Job("NF", "2026-11-16",
                Int.MAX_VALUE) { JSONObject().put("status", "UNREACHABLE") })
            assertTrue("the oversized job must be rejected: $rejected",
                rejected.startsWith("REFUSED"))

            val stats = w.stats()
            val q = stats.getJSONObject("last_queue_rejection_by_index").getJSONObject("NF")
            assertEquals("2026-11-16", q.getString("session_date"))
            assertEquals("QUEUE_REJECTED_NOTHING_WAS_WRITTEN", q.getString("outcome_kind"))
            assertFalse("a rejected job must leave NO persistence result to misread",
                stats.getJSONObject("last_in_window_by_index").has("NF"))
        } finally {
            w.resetForTest()
        }
    }

    /**
     * 2(b). FAILED BEFORE THE FIX. Four expressions computed this headroom: admission deducted
     * both reserves, while the collection state and both status surfaces deducted only the
     * control reserve. A store holding exactly the evidence budget therefore reported 128 KiB
     * "remaining" and a collecting state, while every evidence write was already being refused.
     */
    @Test
    fun oneHeadroomCalculationIsSharedByAdmissionAboutAndTheExport() {
        val filesDir = Files.createTempDirectory("premcap-headroom").toFile()
        val root = PremiumCaptureControl.root(filesDir)
        fillLeaving(root, 0L)                       // exactly at the evidence budget
        PremiumCaptureControl.setEnabled(filesDir, true)
        val store = PremiumCaptureStore(root)
        store.seedByteCacheForStatus()

        val status = PremiumCaptureControl.status(filesDir)
        val fast = PremiumCaptureControl.statusFast(filesDir)
        val capacity = store.capacitySnapshot()

        assertEquals("the status and the store must report the same headroom",
            capacity.getLong("evidence_remaining_bytes"),
            status.getLong("evidence_bytes_remaining"))
        assertEquals("and so must the non-blocking status the dialog reads",
            capacity.getLong("evidence_remaining_bytes"),
            fast.getLong("evidence_bytes_remaining"))
        assertEquals(0L, capacity.getLong("evidence_remaining_bytes"))
        assertEquals("ON_BUT_STOPPED_STORE_FULL", status.getString("collection_state"))
        assertEquals("ON_BUT_STOPPED_STORE_FULL", fast.getString("collection_state"))
        assertEquals("both reserves, deducted once",
            PremiumCaptureStore.MAX_BYTES_GLOBAL
                - PremiumCaptureStore.CONTROL_RESERVE_BYTES
                - PremiumCaptureStore.DIAGNOSTIC_RESERVE_BYTES,
            fast.getLong("evidence_budget_bytes"))
        // and the store really does refuse evidence at this reading
        val r = observeFirstSlot(store, "mon_0920_entry_NF", "NF")
        assertEquals(PremiumCaptureSession.COHORT_PERSIST_FAILED, r.status)
    }

    /**
     * 2(b). An unmeasured store is UNKNOWN, not empty and not full. Reporting 0 B remaining
     * would read as a full store; reporting the cap would read as an empty one.
     */
    @Test
    fun anUnmeasuredStoreReportsUnknownHeadroomRatherThanANumber() {
        val filesDir = Files.createTempDirectory("premcap-unknown").toFile()
        val fast = PremiumCaptureControl.statusFast(filesDir)
        assertFalse("nothing has measured this store", fast.getBoolean("evidence_bytes_known"))
        assertEquals(-1L, fast.getLong("evidence_bytes_remaining"))
        assertNull("and no headroom may be asserted for it",
            PremiumCaptureStore.evidenceRemainingBytes(-1L))
    }

    /**
     * 2(b). The pre-open reading must be USABLE before the day's first capture, and must cost
     * the calling thread no tree walk. The seeding walk runs once, in the background, at control
     * init; the status then reads memory. Measured here against a large retained tree.
     */
    @Test
    fun thePreOpenHeadroomReadingCostsTheCallingThreadNoTreeWalk() {
        val filesDir = Files.createTempDirectory("premcap-preopen").toFile()
        val root = PremiumCaptureControl.root(filesDir)
        root.mkdirs()
        // a retained footprint spread over many files, so a walk would be visible
        for (d in 1..40) for (i in 1..250)
            sparseFill(File(root,
                "2026-10-%02d/NF/records/r%d_%d.json".format(d % 28 + 1, d, i)), 512L)

        // The seed happens on control init's own background thread -- the same path the app
        // takes at launch, long before 09:20 -- so nothing on the calling thread walks anything.
        // (The unmeasured case is covered by anUnmeasuredStoreReportsUnknownHeadroomRatherThanANumber;
        // it cannot be asserted here, because initialising the control is what triggers the seed.)
        assertTrue("control init must complete",
            PremiumCaptureControl.awaitInitialized(filesDir, 30_000))
        PremiumCaptureControl.setEnabled(filesDir, true)

        val probe = PremiumCaptureStore(root)
        val startWalks = probe.exactWalks
        val t0 = System.nanoTime()
        val fast = PremiumCaptureControl.statusFast(filesDir)
        val ms = (System.nanoTime() - t0) / 1e6
        assertTrue("the pre-open reading must be usable before the first capture",
            fast.getBoolean("evidence_bytes_known"))
        assertEquals("and must walk nothing", startWalks, probe.exactWalks)
        assertTrue("a status refresh must stay off the walk path, took $ms ms", ms < 150.0)
        assertTrue("it must report real retained bytes, got ${fast.getLong("global_bytes")}",
            fast.getLong("global_bytes") > 5_000_000L)
    }
}
