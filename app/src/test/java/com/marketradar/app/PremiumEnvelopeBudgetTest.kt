package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files

/**
 * The slot envelope against its byte cap, measured the way the DEVICE measures it.
 *
 * TWO CORRECTIONS over the first version of this test, both of which made it understate the risk:
 *
 *  1. It passed `1000` and `1300` as acquisition clocks. Real clocks are 13-digit epoch
 *     milliseconds, so every envelope carries ~20 more bytes than that measured.
 *  2. It measured `JSONObject.toString()` on a JVM. The phone serializes with Android's
 *     org.json, which escapes every `/` as `\/` — so the stored bytes, which are what
 *     `putEnvelope` admits against the cap, are one byte larger per slash than the JVM produces.
 *     [androidStoredBytes] applies that exactly.
 *
 * MEASURED ON THE REAL 12 NOVEMBER EXPORT, from the stored bytes of all 107 envelopes:
 * 2,012 to **2,021 bytes** against the 2,048 B cap — so the production fleet runs on **27 to 36
 * bytes** of headroom, not the 37–75 the first version reported.
 *
 * AND A SUPPORTED PAYLOAD ALREADY OVERFLOWS IT. Taking that same largest envelope:
 *
 *     map_ref resolved to a 32-hex content reference   +30 B  ->  2,051 B   over by 3
 *     a vendor timestamp supplied                      +23 B  ->  2,044 B   4 B left
 *     both                                             +53 B  ->  2,074 B   over by 26
 *
 * Both are things the contract accepts and the field will eventually produce. An overflow is not
 * a degraded envelope: `putEnvelope` is refused and, by the fail-closed rule, **every observation
 * in that slot is dropped** — the 8 November incident (`a69bfd0`) exactly.
 *
 * MEASURED HERE, through the real producer with those clocks (`jvm` -> `android stored`):
 *
 *     NF weekly, map_ref null                1,991 -> 1,994   headroom 54
 *     BNF monthly, map_ref null              2,001 -> 2,004   headroom 44
 *     NF weekly, map_ref a dated path        2,014 -> 2,018   headroom 30
 *     BNF monthly, map_ref a content ref     2,031 -> 2,034   headroom 14
 *     the same plus a vendor timestamp              REFUSED BY THE STORE
 *
 * The last line is the overflow reached on the CURRENT build, not a projection. Note also that
 * these fixture envelopes are ~27 B smaller than the field's, so the floor below is calibrated
 * conservatively: the field runs tighter than this suite measures, and the refusal case above is
 * the guard that does not depend on that calibration.
 *
 * THE SMALLEST FIX, measured: `lot.lot_authority_requires` costs **106 bytes** per envelope and
 * has **one distinct value in the entire export** — a fixed policy string repeated 107 times,
 * already implied by `production_authority_ref`, which points at the per-session authority record
 * that the string describes. Moving it there takes the worst case to ~1,915 B and the
 * both-additions case to ~1,968 B, with no information lost. That is the same move that fixed
 * this before: take content out by reference rather than enlarge the cap.
 */
class PremiumEnvelopeBudgetTest {

    /** Measured stored sizes leave as little as 27 B, so the floor sits below that with margin. */
    private val requiredHeadroomBytes = 16

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

    /**
     * The size the PHONE stores, from an object serialized on a JVM.
     *
     * Android's org.json escapes every `/` as `\/` and the reference JVM one does not, so the
     * device's bytes are the JVM's plus one per slash. Nothing else in the two writers differs
     * for the characters a premium envelope contains, which is why this adjustment is exact
     * rather than an estimate — and it is applied because the cap is enforced on stored bytes.
     */
    private fun androidStoredBytes(o: JSONObject): Int {
        val jvm = o.toString()
        return jvm.toByteArray(Charsets.UTF_8).size + jvm.count { it == '/' }
    }

    /** Real IST epoch milliseconds, not a toy counter. */
    private val requestMs = 1_794_466_205_573L     // 2026-11-12 12:20:05.573 IST
    private val responseMs = 1_794_466_205_692L

    private val hex32 = "a908c610e9a302ed31bc639283031f4d"
    private val weeklyLotRef =
        "contract_lot_table/contract_lot_table_v3_20260914/FAOP70616_NF_weekly_revised/2026-11-12"
    private val monthlyLotRef =
        "contract_lot_table/contract_lot_table_v3_20260914/FAOP70616_BANKNIFTY_monthly_revised/2026-12-31"

    private fun envelope(lotRef: String, mapRef: String?, authRef: String?,
                         vendorTs: String? = null, index: String = "NF"): JSONObject? {
        val filesDir = Files.createTempDirectory("premcap-budget").toFile()
        PremiumCaptureControl.setEnabled(filesDir, true)
        val store = PremiumCaptureStore(PremiumCaptureControl.root(filesDir))
        val id = if (index == "NF") "mon_0920_entry_NF" else "mon_0920_entry_BNF"
        val sn = rawFixture(id)
        PremiumCaptureSession(store).observeSlot(
            index, "2026-10-19", sn.getJSONObject("raw"), "09:20", "09:20",
            sn.optString("requested_underlying_key").ifEmpty { null }, requestMs, responseMs,
            vendorSourceTs = vendorTs,
            claimedLot = if (index == "NF") 75 else 35,
            claimedLotSource = "CONTRACT_LOT_TABLE",
            lotTableRef = lotRef, mapRef = mapRef, productionLotAuthorityRef = authRef)
        return store.readEnvelopes(index, "2026-10-19").firstOrNull()
    }

    /** The shapes the field produces today, with production-shaped clocks. */
    private fun producedCases(): List<Pair<String, JSONObject>> = listOf(
        "NF weekly, map_ref null (the 12 November shape)"
            to envelope(weeklyLotRef, null, hex32)!!,
        "BNF monthly, map_ref null"
            to envelope(monthlyLotRef, null, hex32, index = "BNF")!!,
        "NF weekly, map_ref a dated path"
            to envelope(weeklyLotRef, "instrument_map/2026-11-12", hex32)!!)

    @Test
    fun theProducedEnvelopeStaysInsideTheCapWhenStoredByAndroid() {
        for ((name, e) in producedCases()) {
            val n = androidStoredBytes(e)
            assertTrue("$name: $n B as Android would store it, against the " +
                "${PremiumCaptureStore.MAX_BYTES_PER_RECORD} B cap. An overflow is refused and " +
                "drops every observation in the slot.",
                n <= PremiumCaptureStore.MAX_BYTES_PER_RECORD)
            assertTrue("$name: the envelope must verify its own digest",
                PremiumObservationContract.verifyHash(e))
        }
    }

    @Test
    fun theHeadroomIsReportedAndHasNotFallenToNothing() {
        val worst = producedCases().minByOrNull {
            PremiumCaptureStore.MAX_BYTES_PER_RECORD - androidStoredBytes(it.second)
        }!!
        val headroom = PremiumCaptureStore.MAX_BYTES_PER_RECORD - androidStoredBytes(worst.second)
        assertTrue("the produced worst case (${worst.first}) leaves $headroom B of the " +
            "${PremiumCaptureStore.MAX_BYTES_PER_RECORD} B cap as Android stores it. The real " +
            "12 November fleet ran on 27 to 36 B. Below $requiredHeadroomBytes B the next " +
            "reference string overflows it, and an overflow drops the slot. The smallest fix " +
            "is to move lot.lot_authority_requires (106 B, one distinct value in the whole " +
            "export) onto the authority record that production_authority_ref already names.",
            headroom >= requiredHeadroomBytes)
    }

    /**
     * THE RISK, REPRODUCED rather than asserted away. `map_ref` resolved to a content reference
     * and a supplied vendor timestamp are both accepted by the contract, and together they push
     * the envelope past the cap. The store then refuses it — which is correct and fail-closed,
     * and is also why the slot's observations are lost.
     *
     * This test documents the overflow as reachable. When the envelope is slimmed it will still
     * pass, because it asserts on what the store DID, not on a size.
     */
    @Test
    fun aSupportedPayloadCanOverflowTheCapAndTheStoreRefusesRatherThanTruncates() {
        val filesDir = Files.createTempDirectory("premcap-overflow").toFile()
        PremiumCaptureControl.setEnabled(filesDir, true)
        val store = PremiumCaptureStore(PremiumCaptureControl.root(filesDir))
        val sn = rawFixture("mon_0920_entry_BNF")
        // the fattest shape the contract accepts: monthly lot ref, a content map reference and a
        // vendor timestamp, with real clocks
        val r = PremiumCaptureSession(store).observeSlot(
            "BNF", "2026-10-19", sn.getJSONObject("raw"), "09:20", "09:20",
            sn.optString("requested_underlying_key").ifEmpty { null }, requestMs, responseMs,
            vendorSourceTs = "2026-11-12T13:45:00+05:30",
            claimedLot = 35, claimedLotSource = "CONTRACT_LOT_TABLE",
            lotTableRef = monthlyLotRef + "/revision_2", mapRef = hex32,
            productionLotAuthorityRef = hex32)
        val envs = store.readEnvelopes("BNF", "2026-10-19")
        if (envs.isEmpty()) {
            // refused: the fail-closed path. Every observation in the slot is dropped, which is
            // the consequence worth seeing stated.
            assertEquals("the slot must record that nothing was persisted",
                0, store.outcomeCount("BNF", "2026-10-19"))
            assertTrue("the refusal must be reported, not silent: ${r.status} / ${r.detail}",
                (r.detail ?: "").isNotEmpty() || r.status != PremiumCaptureSession.OBSERVED)
        } else {
            // it fit on this fixture; then it must still be inside the cap AS ANDROID STORES IT,
            // and the margin is what this suite exists to watch
            val n = androidStoredBytes(envs.first())
            assertTrue("a supported payload reached $n B, over the " +
                "${PremiumCaptureStore.MAX_BYTES_PER_RECORD} B cap as Android would store it, " +
                "while the JVM measured ${envs.first().toString().toByteArray().size} B and so " +
                "would not have noticed",
                n <= PremiumCaptureStore.MAX_BYTES_PER_RECORD)
        }
    }

    // ───────────────────────── JSON null in the hashed body ─────────────────────────

    /**
     * `org.json.JSONObject.put(key, null)` **removes the key**; only `put(key, JSONObject.NULL)`
     * stores a JSON null. In a hashed body those are not interchangeable: different digests, and
     * different claims — "no map reference was supplied" versus "this shape never had the field".
     */
    @Test
    fun anAbsentReferenceIsStoredAsJsonNullAndNotAsADroppedKey() {
        val lot = envelope(weeklyLotRef, null, hex32)!!.getJSONObject("lot")
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
        val trap = JSONObject().put("a", 1)
        trap.put("map_ref", null as Any?)
        assertTrue("org.json removes a key put with a Kotlin null; this is the trap the " +
            "producer must avoid in any hashed body", !trap.has("map_ref"))
    }

    @Test
    fun aNullBearingEnvelopeStillVerifiesAfterATextRoundTrip() {
        val e = envelope(weeklyLotRef, null, hex32)!!
        assertTrue("in memory", PremiumObservationContract.verifyHash(e))
        assertTrue("after a text round trip, which is what an export is",
            PremiumObservationContract.verifyHash(JSONObject(e.toString())))
    }

    /**
     * The canonical encoding delegates to `JSONObject.quote`, which differs between Android and
     * the JVM — so this JVM assertion is explicitly about the JVM rule, and says so rather than
     * claiming to describe the phone. The device rule is implemented and tested on the verifier
     * side (`tools/premium_export_verify.py`, profile `android-legacy`), against real phone bytes.
     */
    @Test
    fun theJvmCanonicalFormLeavesSlashesBareWhichIsNotWhatThePhoneDoes() {
        val withSlash = JSONObject().put("ref", "contract_lot_table/v3/NF")
        assertEquals("{\"ref\":\"contract_lot_table/v3/NF\"}",
            PremiumObservationContract.canonicalJson(withSlash))
        assertTrue("a premium envelope really does carry slashes in its references, so this " +
            "difference reaches real digests",
            envelope(weeklyLotRef, null, hex32)!!
                .getJSONObject("lot").getString("contract_lot_table_ref").contains('/'))
    }
}
