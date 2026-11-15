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
 * AND A SUPPORTED PAYLOAD OVERFLOWED IT, which is now fixed. Before the change, taking that
 * largest envelope:
 *
 *     map_ref resolved to a 32-hex content reference   +30 B  ->  2,051 B   over by 3
 *     a vendor timestamp supplied                      +23 B  ->  2,044 B   4 B left
 *     both                                             +53 B  ->  2,074 B   over by 26
 *
 * Both are things the contract accepts, and the store refused the pair outright. An overflow is
 * not a degraded envelope: `putEnvelope` is refused and, by the fail-closed rule, **every
 * observation in that slot is dropped** — the 8 November incident (`a69bfd0`) exactly.
 *
 * THE FIX, APPLIED: `lot.lot_authority_requires` cost **106 bytes** per envelope and had **one
 * distinct value in the entire export** — a fixed policy string repeated 107 times, already
 * implied by `production_authority_ref`, which names the per-session authority record the string
 * describes. It now lives on that record as `authority_requires`, and the envelope is v4.
 * Measured through the real producer (Android stored bytes), before -> after the constant moved
 * -> and after the v2 serialization tag, which is 17 bytes longer than v1's:
 *
 *     NF weekly, map_ref null                1,994 -> 1,888 -> 1,906   headroom  54 -> 142
 *     BNF monthly, map_ref null              2,004 -> 1,898 -> 1,916   headroom  44 -> 132
 *     NF weekly, map_ref a dated path        2,018 -> 1,912 -> 1,930   headroom  30 -> 118
 *     BNF monthly, map_ref a content ref     2,034 -> 1,928 -> 1,946   headroom  14 -> 102
 *     the same plus a vendor timestamp     REFUSED -> 1,952 -> 1,974   headroom  --  ->  74
 *
 * 106 bytes recovered per envelope, exactly the size of the constant that moved; 17 given back to
 * the longer tag, which is what makes a stored body say which rule hashed it. The payload that
 * could not be stored at all now stores with 74 bytes to spare. v3 envelopes and v1 digests both
 * stay supported, so an in-place upgrade does not orphan evidence already on the phone.
 *
 * The CAP is enforced on `JSONObject.toString()` bytes, which Android escapes its own way — the
 * canonical encoding governs digests, not stored size, so moving to v2 changes nothing here
 * beyond the length of the tag itself.
 */
class PremiumEnvelopeBudgetTest {

    /**
     * The produced worst case now leaves 136 B (it left 30 before the constant moved), so the
     * floor is raised to 64: comfortably under today's margin, comfortably over the 27 B the
     * fleet was running on, and tight enough that the next field added to the envelope has to
     * be justified rather than just fitted.
     */
    private val requiredHeadroomBytes = 64

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

    // ───────────── the overflow, resolved on the real store path ─────────────

    /**
     * THE FIX, PROVEN. The payload that was refused before — BANKNIFTY monthly with `map_ref` as
     * a content reference AND a vendor timestamp — now writes through the production path, with
     * headroom, because `lot_authority_requires` is no longer carried per slot.
     *
     * Measured after the change (`jvm -> android stored`):
     *
     *     NF weekly, map_ref null                1,885 -> 1,888   headroom 160
     *     BNF monthly, map_ref null              1,895 -> 1,898   headroom 150
     *     NF weekly, map_ref a dated path        1,908 -> 1,912   headroom 136
     *     BNF monthly, map_ref a content ref     1,925 -> 1,928   headroom 120
     *     the same plus a vendor timestamp       1,949 -> 1,952   headroom  96  (was REFUSED)
     *
     * 106 bytes recovered per envelope, exactly the size of the constant that moved.
     */
    @Test
    fun theLargestSupportedPayloadNowWritesThroughTheProductionPathWithHeadroom() {
        for (index in listOf("NF", "BNF")) {
            val lotRef = if (index == "NF") weeklyLotRef else monthlyLotRef
            val e = envelope(lotRef + "/revision_2", hex32, hex32,
                vendorTs = "2026-11-12T13:45:00+05:30", index = index)
            assertTrue("$index: the largest supported payload must persist, not be refused",
                e != null)
            val n = androidStoredBytes(e!!)
            val headroom = PremiumCaptureStore.MAX_BYTES_PER_RECORD - n
            assertTrue("$index: $n B as Android stores it, only $headroom B left",
                headroom >= 48)
            assertTrue("$index: and it must verify its own digest",
                PremiumObservationContract.verifyHash(e))
            assertTrue("$index: the constant must be gone from the per-slot lot block",
                !e.getJSONObject("lot").has("lot_authority_requires"))
        }
    }

    /**
     * And the authority it points at is still resolvable. The requirement moved onto that record,
     * so it is still in the artifact — once per index/session instead of once per slot — and the
     * envelope's content reference must still match the record the export resolves.
     */
    @Test
    fun theAuthorityRecordStaysResolvableAndCarriesTheRequirement() {
        val filesDir = Files.createTempDirectory("premcap-authority").toFile()
        PremiumCaptureControl.setEnabled(filesDir, true)
        PremiumCaptureWorker.resetForTest()
        try {
            val acqs = listOf("NF", "BNF").map { ix ->
                val sn = rawFixture(if (ix == "NF") "mon_0920_entry_NF" else "mon_0920_entry_BNF")
                PremiumCaptureCoordinator.ChainAcquisition(
                    index = ix, rawChain = sn.getJSONObject("raw"),
                    requestStartedMs = requestMs, responseReceivedMs = responseMs,
                    requestedUnderlyingKey = PremiumCaptureCohort.INDEX_UNDERLYING[ix],
                    expiry = "2026-10-27", receiptHhmm = "09:20")
            }
            PremiumCaptureCoordinator.onChainsAcquired(filesDir, "2026-10-19", "09:20", acqs,
                PremiumCaptureCoordinator.PREPARE_BUDGET_MS_PER_POLL)
            assertTrue(PremiumCaptureWorker.awaitIdle(30_000L))

            val store = PremiumCaptureStore(PremiumCaptureControl.root(filesDir))
            for (ix in listOf("NF", "BNF")) {
                val auth = store.readLotAuthority(ix, "2026-10-19")
                assertTrue("$ix: the authority record must exist", auth != null)
                assertEquals("$ix: and must now carry the requirement that left the envelope",
                    PremiumObservationContract.LOT_AUTHORITY_REQUIRES,
                    auth!!.getString("authority_requires"))
                val env = store.readEnvelopes(ix, "2026-10-19").first()
                assertEquals("$ix: the envelope's reference must still resolve to that record",
                    PremiumObservationContract.ref(PremiumObservationContract.contentHash(auth)),
                    env.getJSONObject("lot").getString("production_authority_ref"))
            }
            // and the export resolves it, matching reference to record
            val sec = PremiumExportSection.build(filesDir, "2026-10-19")
            for (ix in listOf("NF", "BNF")) {
                val pa = sec.getJSONObject("indices").getJSONObject(ix)
                    .getJSONObject("references").getJSONObject("production_lot_authority")
                assertTrue("$ix: the export must record the authority", pa.getBoolean("recorded"))
                assertTrue("$ix: and the envelopes' reference must match it",
                    pa.getBoolean("reference_matches_record"))
                assertEquals("$ix: the requirement must be resolvable from the export",
                    PremiumObservationContract.LOT_AUTHORITY_REQUIRES,
                    pa.getJSONObject("authority").getString("authority_requires"))
            }
        } finally {
            PremiumCaptureWorker.resetForTest()
        }
    }

    /**
     * An in-place upgrade must not orphan evidence already on the phone. A v3 envelope — the
     * shape that carried the constant inline — still verifies its own digest and is still a
     * supported version, so it continues to export.
     */
    @Test
    fun anEnvelopeStoredBeforeTheUpgradeStillVerifiesAndIsStillSupported() {
        assertTrue("v4 is what new envelopes declare",
            PremiumObservationContract.ENVELOPE_VERSION.endsWith(".v4"))
        assertTrue("and v3 must stay supported",
            PremiumObservationContract.SUPPORTED_ENVELOPE_VERSIONS.contains(
                PremiumObservationContract.ENVELOPE_VERSION_V3_WITH_INLINE_AUTHORITY_REQUIREMENT))
        // a v3-shaped envelope: the v4 body, plus the inline constant, restamped as v3
        val v4 = envelope(weeklyLotRef, null, hex32)!!
        val v3 = JSONObject(v4.toString())
            .put("record_version",
                PremiumObservationContract.ENVELOPE_VERSION_V3_WITH_INLINE_AUTHORITY_REQUIREMENT)
        v3.getJSONObject("lot").put("lot_authority_requires",
            PremiumObservationContract.LOT_AUTHORITY_REQUIRES)
        PremiumObservationContract.stampHash(v3)
        assertTrue("a v3 envelope must still verify its own digest",
            PremiumObservationContract.verifyHash(v3))
        assertTrue("and still carry the constant it was stored with",
            v3.getJSONObject("lot").has("lot_authority_requires"))
    }

    /**
     * The cap still refuses oversized input with an exact reason. Slimming the envelope must not
     * have softened the admission gate: a record genuinely over the per-record cap is refused,
     * and the refusal names the kind, the size and the cap rather than failing vaguely.
     */
    @Test
    fun oversizedInputIsStillRefusedWithAnExactDiagnostic() {
        val filesDir = Files.createTempDirectory("premcap-oversize").toFile()
        val store = PremiumCaptureStore(PremiumCaptureControl.root(filesDir).apply { mkdirs() })
        val fat = JSONObject().put("pad", "x".repeat(4096))
        val r = store.put("NF", "2026-10-19", "k1", fat)
        assertEquals(PremiumCaptureStore.REFUSED_RECORD_TOO_LARGE, r.status)
        val d = r.detail ?: ""
        assertTrue("the refusal must name the size: $d",
            d.contains("${PremiumCaptureStore.MAX_BYTES_PER_RECORD}"))
        assertTrue("and must say it was neither truncated nor split: $d",
            d.contains("not truncated") && d.contains("not split"))
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
     * The probe must describe the runtime it ran on, not a guess. On this JVM the reference
     * org.json leaves `/` bare; on a phone Android's escapes it. Asserting the probe against the
     * runtime's actual behaviour is what makes the recorded value trustworthy on either.
     */
    @Test
    fun theQuoterProbeAgreesWithTheRuntimeItRanOn() {
        val slashIsEscaped = JSONObject.quote("/") == "\"\\/\""
        val expected = if (slashIsEscaped) PremiumObservationContract.QUOTE_ANDROID
        else PremiumObservationContract.QUOTE_REFERENCE
        assertEquals("the probe must report what this runtime actually does",
            expected, PremiumObservationContract.quoteImplementation())
        // this harness is the JVM, so it must be the reference rule; if this ever flips here,
        // the vectors and every JVM-side canonical assertion are describing something else
        assertEquals(PremiumObservationContract.QUOTE_REFERENCE,
            PremiumObservationContract.quoteImplementation())
    }

    /**
     * Recording which quoter ran must not be able to change a digest, so it lives outside every
     * hashed body. If it leaked into the envelope or a record, every existing digest would break.
     */
    @Test
    fun theRecordedQuoterIsOutsideEveryHashedBody() {
        val e = envelope(weeklyLotRef, null, hex32)!!
        assertTrue("the envelope must not carry it", !e.has("canonical_quote_impl"))
        assertTrue("nor may its hashed sub-objects",
            !e.getJSONObject("lot").has("canonical_quote_impl"))
        assertTrue("and the envelope must still verify", PremiumObservationContract.verifyHash(e))
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
