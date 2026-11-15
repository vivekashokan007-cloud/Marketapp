package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files

/**
 * The slot envelope against its byte cap, and the null semantics of the hashed body.
 *
 * WHY A BUDGET TEST. On 8 November (`a69bfd0`) attaching the lot-table verdict inline pushed the
 * envelope past the 2 KiB per-record cap. `putEnvelope` was refused and, by the fail-closed rule,
 * **every observation in the coordinator path was dropped** — a whole slot lost, and nothing in
 * the delivery said so. The fix moved the authority out to a per-session record referenced by
 * content, and added a test that the cap holds. What it did not add is a measurement of how much
 * room is actually left, so the next field to be added would repeat the incident rather than fail
 * a test.
 *
 * Measured here under realistic payloads: **1,973 to 2,011 bytes, i.e. 37 to 75 bytes of
 * headroom.** The real 12 November phone envelope is 2,015 B — 33 B of headroom. One more
 * reference string, one more clock field, or a longer lot-table label spends it.
 *
 * So this asserts a FLOOR on the remaining room rather than only that the cap holds. If a change
 * takes the realistic worst case under that floor, this test fails while the capture still works,
 * which is the point: the warning has to arrive before the slot is lost, not after.
 */
class PremiumEnvelopeBudgetTest {

    /** Measured minimum headroom, less a small margin, so this reports a trend not noise. */
    private val requiredHeadroomBytes = 30

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

    /** One envelope produced through the real session path with the given lot references. */
    private fun envelope(lotRef: String, mapRef: String?, authRef: String?): JSONObject {
        val filesDir = Files.createTempDirectory("premcap-budget").toFile()
        PremiumCaptureControl.setEnabled(filesDir, true)
        val store = PremiumCaptureStore(PremiumCaptureControl.root(filesDir))
        val sn = rawFixture("mon_0920_entry_NF")
        PremiumCaptureSession(store).observeSlot(
            "NF", "2026-10-19", sn.getJSONObject("raw"), "09:20", "09:20",
            sn.optString("requested_underlying_key").ifEmpty { null }, 1_000L, 1_300L,
            claimedLot = 75, claimedLotSource = "CONTRACT_LOT_TABLE",
            lotTableRef = lotRef, mapRef = mapRef, productionLotAuthorityRef = authRef)
        val envs = store.readEnvelopes("NF", "2026-10-19")
        assertEquals("the envelope must persist for this configuration to be measurable",
            1, envs.size)
        return envs.first()
    }

    private val hex32 = "a908c610e9a302ed31bc639283031f4d"
    private val weeklyLotRef =
        "contract_lot_table/contract_lot_table_v3_20260914/FAOP70616_NF_weekly_revised/2026-11-12"
    private val monthlyLotRef =
        "contract_lot_table/contract_lot_table_v3_20260914/FAOP70616_BANKNIFTY_monthly_revised/2026-12-31"

    /** The four configurations the field actually produces, including the 12 November shape. */
    private fun realisticCases(): List<Pair<String, JSONObject>> = listOf(
        "12 November shape: real lot ref, map_ref null" to envelope(weeklyLotRef, null, hex32),
        "map_ref as a content reference" to envelope(weeklyLotRef, hex32, hex32),
        "map_ref as a dated path" to envelope(weeklyLotRef, "instrument_map/2026-11-12", hex32),
        "monthly lot ref and a content map ref" to envelope(monthlyLotRef, hex32, hex32))

    @Test
    fun theEnvelopeStaysInsideItsCapAndVerifiesUnderRealisticPayloads() {
        for ((name, e) in realisticCases()) {
            val n = e.toString().toByteArray(Charsets.UTF_8).size
            assertTrue("$name: $n B exceeds the ${PremiumCaptureStore.MAX_BYTES_PER_RECORD} B " +
                "cap, which would refuse the envelope and drop the whole slot",
                n <= PremiumCaptureStore.MAX_BYTES_PER_RECORD)
            assertTrue("$name: the envelope must verify its own digest",
                PremiumObservationContract.verifyHash(e))
        }
    }

    @Test
    fun theEnvelopeKeepsEnoughHeadroomForTheNextFieldToBeAddedSafely() {
        val worst = realisticCases().minByOrNull {
            PremiumCaptureStore.MAX_BYTES_PER_RECORD -
                it.second.toString().toByteArray(Charsets.UTF_8).size
        }!!
        val n = worst.second.toString().toByteArray(Charsets.UTF_8).size
        val headroom = PremiumCaptureStore.MAX_BYTES_PER_RECORD - n
        assertTrue("the realistic worst case (${worst.first}) leaves only $headroom B of the " +
            "${PremiumCaptureStore.MAX_BYTES_PER_RECORD} B cap. Below $requiredHeadroomBytes B " +
            "the next reference string added to the envelope overflows it, and an overflow is " +
            "not a degraded envelope: putEnvelope is refused and every observation in the slot " +
            "is dropped fail-closed (see a69bfd0). Move content out by reference as the lot " +
            "authority was, rather than enlarging the cap.",
            headroom >= requiredHeadroomBytes)
    }

    // ───────────────────────── JSON null in the hashed body ─────────────────────────

    /**
     * `org.json.JSONObject.put(key, null)` **removes the key**; only `put(key, JSONObject.NULL)`
     * stores a JSON null. In a hashed body those are not interchangeable: they are different
     * digests, and they mean different things — "the vendor supplied no map reference" versus
     * "this envelope shape never had the field". The producer must keep the key.
     */
    @Test
    fun anAbsentReferenceIsStoredAsJsonNullAndNotAsADroppedKey() {
        val lot = envelope(weeklyLotRef, null, hex32).getJSONObject("lot")
        assertTrue("map_ref must still be present when no map was supplied", lot.has("map_ref"))
        assertEquals("and present as a JSON null, not as a dropped key or an empty string",
            JSONObject.NULL, lot.opt("map_ref"))
    }

    @Test
    fun aDroppedKeyAndAJsonNullDoNotHashAlike() {
        val dropped = JSONObject().put("a", 1)
        val nulled = JSONObject().put("a", 1).put("map_ref", JSONObject.NULL)
        assertEquals("{\"a\":1}", PremiumObservationContract.canonicalJson(dropped))
        assertEquals("{\"a\":1,\"map_ref\":null}", PremiumObservationContract.canonicalJson(nulled))
        assertNotEquals("if these hashed alike, a silently dropped field would be undetectable",
            PremiumObservationContract.sha256Hex(
                PremiumObservationContract.canonicalJson(dropped)),
            PremiumObservationContract.sha256Hex(
                PremiumObservationContract.canonicalJson(nulled)))
        // and the Kotlin-null trap itself, pinned so it cannot be reintroduced by habit
        val trap = JSONObject().put("a", 1)
        trap.put("map_ref", null as Any?)
        assertTrue("org.json removes a key put with a Kotlin null; this is the trap the " +
            "producer must avoid in any hashed body", !trap.has("map_ref"))
    }

    /** Nulls must survive the export round trip, which is where the digest is re-checked. */
    @Test
    fun aNullBearingEnvelopeStillVerifiesAfterATextRoundTrip() {
        val e = envelope(weeklyLotRef, null, hex32)
        assertTrue("in memory", PremiumObservationContract.verifyHash(e))
        assertTrue("after a text round trip, which is what an export is",
            PremiumObservationContract.verifyHash(JSONObject(e.toString())))
    }
}
