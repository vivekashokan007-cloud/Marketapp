package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.util.Calendar
import java.util.TimeZone

/**
 * THREE FULL SYNTHETIC SESSIONS — both indices, all 69 five-minute slots 09:20–15:00 IST —
 * starting from the b529 retained footprint, through capture and exact-date export.
 *
 * Desktop/JVM only. **No device evidence is produced or claimed here, and every number is
 * synthetic**: no phone, no broker, no market data. What this establishes is COLLECTION AND
 * EXPORT COVERAGE across whole sessions. The two-slot fixture in
 * [PremiumCapacityRolloverTest] establishes date separation and capacity behaviour; it cannot
 * show that a session's 69 slots all land, that the pinned contracts hold for a whole day, or
 * that a 60-minute pair exists on the same contracts.
 *
 * The raw chains are perturbed per slot from the committed October fixture by a deterministic
 * generator seeded on (date, index, slot): quotes and the underlying spot move, while strikes,
 * expiries and instrument keys are held FIXED. Contract identity is therefore something this
 * test can check rather than something it is handed. The contract identities and the expiry are
 * October's, because that is what the committed fixture carries; a real 16 November session
 * would pin November contracts.
 *
 * Set `-Dpremium.export.dir=<dir>` to also write each date's export bundle to disk for a
 * replay or preflight run. Nothing is written by default.
 */
class PremiumFullSessionTest {

    private val b529RetainedBytes = 4_177_185L          // the measured 12 November two-index total

    private fun fixture(id: String): JSONObject {
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

    /** 09:20 .. 15:00 inclusive at five minutes: the 69 slots of the experiment window. */
    private fun slots(): List<String> {
        val out = ArrayList<String>()
        var m = 9 * 60 + 20
        while (m <= 15 * 60) { out.add("%02d:%02d".format(m / 60, m % 60)); m += 5 }
        return out
    }

    private fun minuteOf(hhmm: String) =
        hhmm.substring(0, 2).toInt() * 60 + hhmm.substring(3, 5).toInt()

    /** Epoch ms for an IST date and HH:MM, so the recorded clocks are real wall clocks. */
    private fun istMs(date: String, hhmm: String, second: Int): Long {
        val c = Calendar.getInstance(TimeZone.getTimeZone("Asia/Kolkata"))
        c.clear()
        c.set(date.substring(0, 4).toInt(), date.substring(5, 7).toInt() - 1,
            date.substring(8, 10).toInt(), hhmm.substring(0, 2).toInt(),
            hhmm.substring(3, 5).toInt(), second)
        return c.timeInMillis
    }

    /** A deterministic mixer, so a run is reproducible from (date, index, slot) alone. */
    private fun mix(vararg parts: String): Long {
        var h = -0x61c8864680b583ebL
        for (p in parts) for (ch in p) { h = h xor ch.code.toLong(); h *= 0x100000001b3L }
        return h
    }

    /** Move one market_data block by a bounded, deterministic amount. Keys are never touched. */
    private fun jiggle(md: JSONObject?, seed: Long, drift: Double) {
        if (md == null) return
        val r = ((seed ushr 11) % 2001L) / 1000.0 - 1.0              // −1.0 .. +1.0
        val f = 1.0 + drift + r * 0.015                              // ±1.5 % around the drift
        for (k in listOf("bid_price", "ask_price", "ltp")) {
            if (!md.has(k)) continue
            val v = md.optDouble(k, Double.NaN)
            if (v.isNaN() || v <= 0.0) continue
            md.put(k, Math.round(v * f * 20.0) / 20.0)               // 0.05 tick
        }
        val b = md.optDouble("bid_price", Double.NaN)
        val a = md.optDouble("ask_price", Double.NaN)
        if (!b.isNaN() && !a.isNaN() && a <= b)
            md.put("ask_price", Math.round((b + 0.05) * 20.0) / 20.0)
    }

    private fun perturb(template: JSONObject, date: String, index: String, slot: String)
            : JSONObject {
        val raw = JSONObject(template.toString())          // deep copy; the fixture is immutable
        val sign = if (mix(date, index) and 1L == 0L) 1.0 else -1.0
        val drift = sign * (minuteOf(slot) - (9 * 60 + 20)) / 340.0 * 0.06   // ±6 % over the day
        val rows = raw.optJSONArray("data") ?: return raw
        for (i in 0 until rows.length()) {
            val row = rows.getJSONObject(i)
            val seed = mix(date, index, slot, i.toString())
            jiggle(row.optJSONObject("call_options")?.optJSONObject("market_data"), seed, drift)
            jiggle(row.optJSONObject("put_options")?.optJSONObject("market_data"), seed * 31,
                -drift)
            val spot = row.optDouble("underlying_spot_price", Double.NaN)
            if (!spot.isNaN()) row.put("underlying_spot_price",
                Math.round(spot * (1.0 + drift * 0.07) * 100.0) / 100.0)
        }
        return raw
    }

    private fun bundle(date: String, section: JSONObject) = JSONObject()
        .put("manifest", JSONObject()
            .put("session_date_used", date)
            .put("created_at", "${date}T15:45:00+05:30")
            .put("limits", JSONObject().put("max_bundle_bytes", 4_194_304)))
        .put("premium", section)
        .put("parts", JSONObject().put("authority_doc", JSONObject().put("history", JSONObject())))

    private fun maybeWrite(name: String, text: String) {
        val dir = System.getProperty("premium.export.dir") ?: return
        File(dir).mkdirs()
        File(dir, name).writeText(text)
    }

    @Test
    fun threeFullSessionsCaptureAllSixtyNineSlotsAndExportPerDate() {
        val filesDir = Files.createTempDirectory("premcap-fullsession").toFile()
        val root = PremiumCaptureControl.root(filesDir).apply { mkdirs() }
        // start from the b529 retained footprint, not from an empty store
        java.io.RandomAccessFile(File(root, "retained_b529_footprint.bin"), "rw")
            .use { it.setLength(b529RetainedBytes) }
        PremiumCaptureControl.setEnabled(filesDir, true)
        val store = PremiumCaptureStore(root)

        // one genuinely captured OLD date, so "old evidence still exports" is checked against
        // real captured evidence rather than against the opaque filler
        val oldDate = fixture("mon_0920_entry_NF").getString("session")
        for ((id, idx) in listOf("mon_0920_entry_NF" to "NF", "mon_0920_entry_BNF" to "BNF")) {
            val sn = fixture(id)
            PremiumCaptureSession(store).observeSlot(idx, oldDate, sn.getJSONObject("raw"),
                "09:20", "09:20", sn.optString("requested_underlying_key").ifEmpty { null },
                istMs(oldDate, "09:20", 2), istMs(oldDate, "09:20", 3),
                claimedLot = 75, claimedLotSource = "CONTRACT_LOT_TABLE",
                lotTableRef = "contract_lot_table/NSE/2026-10-01",
                mapRef = "instrument_map/2026-10-01")
        }
        val oldRecordsAtCapture = PremiumCaptureStore(root).outcomeCount("NF", oldDate)
        assertEquals(PremiumCaptureCohort.CONTRACTS_PER_COHORT, oldRecordsAtCapture)

        val template = mapOf("NF" to fixture("mon_0920_entry_NF"),
                             "BNF" to fixture("mon_0920_entry_BNF"))
        val all = slots()
        assertEquals("the experiment window is 69 five-minute slots", 69, all.size)
        assertEquals("09:20", all.first())
        assertEquals("15:00", all.last())

        val sizes = LinkedHashMap<String, Int>()
        for (date in listOf("2026-11-16", "2026-11-17", "2026-11-18")) {
            for (index in listOf("NF", "BNF")) {
                val tmpl = template[index]!!
                var observed = 0
                for (slot in all) {
                    val r = PremiumCaptureSession(store).observeSlot(
                        index, date, perturb(tmpl.getJSONObject("raw"), date, index, slot),
                        slot, slot, tmpl.optString("requested_underlying_key").ifEmpty { null },
                        istMs(date, slot, 2), istMs(date, slot, 3),
                        claimedLot = if (index == "NF") 75 else 35,
                        claimedLotSource = "CONTRACT_LOT_TABLE",
                        lotTableRef = "contract_lot_table/NSE/2026-11-01",
                        mapRef = "instrument_map/2026-11-01")
                    assertEquals("$date/$index/$slot: ${r.detail}",
                        PremiumCaptureSession.OBSERVED, r.status)
                    observed++
                }
                assertEquals("$date/$index must observe every slot", all.size, observed)
            }

            val section = PremiumExportSection.build(filesDir, date)
            assertEquals("$date must export as present", "PRESENT", section.optString("state"))
            val text = bundle(date, section).toString()
            sizes[date] = text.toByteArray(Charsets.UTF_8).size
            maybeWrite("export_full_session_$date.json", bundle(date, section).toString(1))

            for (index in listOf("NF", "BNF")) {
                val d = section.getJSONObject("indices").getJSONObject(index)
                val recs = d.getJSONArray("records")
                val pins = d.getJSONObject("cohort").getJSONArray("pinned_contracts")
                val pinKeys = (0 until pins.length())
                    .map { pins.getJSONObject(it).optString("instrument_key") }.toSortedSet()
                assertEquals("$date/$index must pin six contracts",
                    PremiumCaptureCohort.CONTRACTS_PER_COHORT, pinKeys.size)
                assertEquals("$date/$index must carry six records per slot",
                    all.size * PremiumCaptureCohort.CONTRACTS_PER_COHORT, recs.length())
                assertEquals("$date/$index must carry one envelope per slot",
                    all.size, d.getJSONArray("slot_envelopes").length())

                // PINNED CONTRACT IDENTITY: every slot observed the SAME six contracts, so a
                // 60-minute comparison is of one contract's own premium and not of two
                // different strikes that happened to be nearest at different times of day.
                val bySlot = HashMap<String, MutableSet<String>>()
                for (i in 0 until recs.length()) {
                    val r = recs.getJSONObject(i)
                    bySlot.getOrPut(r.optString("slot")) { HashSet() }
                        .add(r.optString("instrument_key"))
                }
                assertEquals("$date/$index records must cover every slot", all.size, bySlot.size)
                val drifted = bySlot.filterValues { it.toSortedSet() != pinKeys }.keys.sorted()
                assertTrue("$date/$index slots observed contracts other than the pinned six: " +
                    "${drifted.take(4)}", drifted.isEmpty())

                // SAME-CONTRACT t+60 COVERAGE: every slot with a +60 partner inside the window
                // has one, on the same pinned contracts.
                val expectedPairs = all.count {
                    "%02d:%02d".format((minuteOf(it) + 60) / 60, (minuteOf(it) + 60) % 60) in all
                }
                var pairs = 0
                for (slot in all) {
                    val m = minuteOf(slot) + 60
                    val later = "%02d:%02d".format(m / 60, m % 60)
                    if (later !in all) continue
                    val a = bySlot[slot]?.toSortedSet()
                    val b = bySlot[later]?.toSortedSet()
                    if (a == pinKeys && b == pinKeys) pairs++
                }
                assertEquals("$date/$index 60-minute pairs on the pinned contracts",
                    expectedPairs, pairs)
                assertEquals("09:20 .. 14:00 is 57 such pairs", 57, expectedPairs)
            }
        }

        // ── the bundle must still fit the combined export limit ──────────────────────────────
        // The real 12 November phone export carried 341,540 B of brain parts beside the premium
        // section, and the combined limit is enforced on the FINAL bytes: an over-cap export is
        // refused outright, so a full session's premium has to leave room for the rest.
        for ((date, n) in sizes) {
            assertTrue("$date bundle $n B must fit ${PremiumExportSection.COMBINED_CAP_BYTES} B",
                n < PremiumExportSection.COMBINED_CAP_BYTES)
            assertTrue("$date bundle $n B must leave room for a 430 KB brain bundle",
                n + 430_211 < PremiumExportSection.COMBINED_CAP_BYTES)
        }

        // ── and the retained prior date must still export, unchanged ─────────────────────────
        val oldSection = PremiumExportSection.build(filesDir, oldDate)
        assertEquals("the prior date must still export", "PRESENT", oldSection.optString("state"))
        assertEquals("and its records must be untouched", oldRecordsAtCapture,
            oldSection.getJSONObject("indices").getJSONObject("NF")
                .getJSONArray("records").length())
        maybeWrite("export_retained_prior_$oldDate.json",
            bundle(oldDate, oldSection).toString(1))

        // three full sessions are a small fraction of the pilot budget
        val used = store.globalBytesFast()
        assertTrue("three sessions plus the retained footprint should be well inside 1 GiB, " +
            "was $used B", used < 20L * 1024 * 1024)
        assertEquals("and the capture path must not walk the tree", 0L, store.exactWalks)
    }

    /**
     * The deliberate refusal case, re-exported: the 13 November condition exactly, 735 B short
     * of what a cohort needs. It must still produce a no-data verdict that the replay excludes —
     * now with the cause named — while a captured session on the same build exports normally.
     */
    @Test
    fun theDeliberateRefusalCaseStillExportsAsNoData() {
        val filesDir = Files.createTempDirectory("premcap-refusal").toFile()
        val root = PremiumCaptureControl.root(filesDir).apply { mkdirs() }
        java.io.RandomAccessFile(File(root, "retained.bin"), "rw")
            .use { it.setLength(PremiumCaptureStore.evidenceBudgetBytes() - 735L) }
        PremiumCaptureControl.setEnabled(filesDir, true)
        val store = PremiumCaptureStore(root)
        val date = "2026-11-19"
        for (index in listOf("NF", "BNF")) {
            val tmpl = fixture(if (index == "NF") "mon_0920_entry_NF" else "mon_0920_entry_BNF")
            val r = PremiumCaptureSession(store).observeSlot(index, date,
                perturb(tmpl.getJSONObject("raw"), date, index, "09:20"), "09:20", "09:20",
                tmpl.optString("requested_underlying_key").ifEmpty { null },
                istMs(date, "09:20", 2), istMs(date, "09:20", 3),
                claimedLot = 75, claimedLotSource = "CONTRACT_LOT_TABLE",
                lotTableRef = "contract_lot_table/NSE/2026-11-01",
                mapRef = "instrument_map/2026-11-01")
            assertEquals(PremiumCaptureSession.COHORT_PERSIST_FAILED, r.status)
        }
        val section = PremiumExportSection.build(filesDir, date)
        assertEquals("NO_PREMIUM_DATA", section.optString("state"))
        for (index in listOf("NF", "BNF")) {
            val d = section.getJSONObject("indices").getJSONObject(index)
            assertEquals("NO_PREMIUM_DATA_FOR_THIS_INDEX", d.getString("state"))
            // the replay's preflight requires all three of these, and must find none
            assertTrue(!d.has("records") && !d.has("slot_envelopes") && !d.has("cohort"))
            assertEquals(PremiumCaptureStore.CAUSE_APP_CAP,
                d.getJSONObject("refusal").getString("cause"))
            assertEquals(1L, d.getJSONObject("durable_counters").getJSONObject("counters")
                .getLong("refused_cohort_persist"))
        }
        maybeWrite("export_refused_at_cap_$date.json", bundle(date, section).toString(1))
    }
}
