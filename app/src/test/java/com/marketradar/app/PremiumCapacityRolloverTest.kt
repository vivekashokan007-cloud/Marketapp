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
}
