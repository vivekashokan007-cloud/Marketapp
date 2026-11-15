package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * The canonical encoding is VERSIONED, and both versions stay verifiable.
 *
 * THE DEFECT. v1 (`json_sorted_keys_compact_utf8_v1`) delegated string escaping to
 * `JSONObject.quote`, which is not the same function on Android as on the JVM: Android escapes
 * every `/` as `\/` and leaves U+0080-U+009F and U+2000-U+20FF literal, while the reference
 * org.json escapes `/` only after `<` and escapes those ranges as `\uXXXX`. A premium envelope
 * carries slashes in `lot.contract_lot_table_ref`, so the SAME object hashed on a phone and in a
 * verifier produced different digests — the 12 November 2026 export carries 648 `\/` sequences,
 * and reading it off-device required Android's rule to be reimplemented from the outside.
 *
 * THE FIX. v2 (`json_sorted_keys_compact_utf8_v2_explicit_escaping`) states the rule and
 * implements it without delegating, in both the Kotlin and the committed Python mirror, so the
 * bytes cannot depend on which org.json is present. `Pc2CompactBatch` did this for PC2 telemetry
 * in round 3; this is the premium contract catching up.
 *
 * WHAT MUST NOT BREAK. Evidence already on a phone was hashed under v1, and an in-place upgrade
 * must not turn it into evidence that fails its own digest. Every verification path therefore
 * reads the rule each body DECLARES — `hash_basis.serialization`, which is itself covered by that
 * body's digest — instead of assuming today's. This class proves that for a stored envelope, for
 * its acquisition id and batch digest, and for a whole session holding both.
 */
class PremiumCanonicalVersioningTest {

    private val v1 = PremiumObservationContract.CANONICAL_SERIALIZATION_V1
    private val v2 = PremiumObservationContract.CANONICAL_SERIALIZATION

    private val date = "2026-10-19"
    private val requestMs = 1_794_466_205_573L     // 2026-11-12 12:20:05.573 IST
    private val responseMs = 1_794_466_205_692L
    private val hex32 = "a908c610e9a302ed31bc639283031f4d"
    private val lotRef =
        "contract_lot_table/contract_lot_table_v3_20260914/FAOP70616_NF_weekly_revised/2026-11-12"

    private fun source(path: String): String =
        listOf(File(path), File("app/$path"), File("../$path")).first { it.isFile }.readText()

    /**
     * Source with comments removed, for the guards below. The documentation of these functions
     * NAMES the delegation one of them deliberately keeps, and a guard that read prose as code
     * would fail on the explanation instead of on the code.
     */
    private fun codeOnly(src: String): String = src.lineSequence()
        .map { it.substringBefore("//") }
        .filterNot { it.trimStart().startsWith("*") || it.trimStart().startsWith("/*") }
        .joinToString("\n")

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

    /** One real acquisition through the real producer, into the store it is given. */
    private fun capture(store: PremiumCaptureStore, slot: String) {
        val sn = rawFixture("mon_0920_entry_NF")
        PremiumCaptureSession(store).observeSlot(
            "NF", date, sn.getJSONObject("raw"), slot, slot,
            sn.optString("requested_underlying_key").ifEmpty { null }, requestMs, responseMs,
            vendorSourceTs = null, claimedLot = 75, claimedLotSource = "CONTRACT_LOT_TABLE",
            lotTableRef = lotRef, mapRef = null, productionLotAuthorityRef = hex32)
    }

    /**
     * The bytes the PRE-UPGRADE build wrote for the same evidence: the identical body, declaring
     * v1 and digested under v1. The basis shape is taken from the live [stampHash] rather than
     * retyped here, so this cannot drift from what the producer actually writes.
     */
    private fun asV1(body: JSONObject): JSONObject {
        val b = PremiumObservationContract.stampHash(JSONObject(body.toString()))
        b.getJSONObject("hash_basis").put("serialization", v1)
        b.remove("payload_sha256")
        return b.put("payload_sha256", PremiumObservationContract.sha256Hex(
            PremiumObservationContract.canonicalJsonV1(b)))
    }

    // ───────────────────────── the rule itself ─────────────────────────

    /**
     * The v2 rule, asserted on exactly the characters the v1 delegation disagreed about. These
     * assertions hold on Android and on the JVM, which is the whole point: under v1 the same
     * three lines had two different right answers.
     */
    @Test
    fun theV2RuleIsPinnedOnTheCharactersV1DisagreedAbout() {
        val slash = JSONObject().put("ref", "contract_lot_table/v3/NF").put("tag", "</script>")
        assertEquals("{\"ref\":\"contract_lot_table/v3/NF\",\"tag\":\"</script>\"}",
            PremiumObservationContract.canonicalJson(slash))
        val ranges = JSONObject().put("c1", "a\u0085b").put("punct", "x\u2028y")
        assertEquals("{\"c1\":\"a\u0085b\",\"punct\":\"x\u2028y\"}",
            PremiumObservationContract.canonicalJson(ranges))
        // and the short forms and controls are unchanged from v1
        assertEquals("{\"s\":\"a\\\"b\\\\c\\nd\\te\\u0001\"}",
            PremiumObservationContract.canonicalJson(
                JSONObject().put("s", "a\"b\\c\nd\te\u0001")))
    }

    /**
     * On THIS runtime (the JVM) the two encoders must actually disagree, or this test would be
     * asserting nothing: a passing comparison between two identical encoders proves no fix.
     */
    @Test
    fun theTwoEncodersGenuinelyDifferOnThisRuntime() {
        val ranges = JSONObject().put("c1", "a\u0085b")
        assertNotEquals("v1 escapes U+0085 on the JVM; v2 must not",
            PremiumObservationContract.canonicalJsonV1(ranges),
            PremiumObservationContract.canonicalJson(ranges))
        // the slash is the case that reaches real premium digests, and on the JVM's org.json v1
        // agrees with v2 there — which is exactly why the divergence went unnoticed off-device.
        val slash = JSONObject().put("ref", "a/b")
        assertEquals(PremiumObservationContract.canonicalJsonV1(slash),
            PremiumObservationContract.canonicalJson(slash))
        assertEquals("on a phone v1 would have escaped it, and that is what the verifier's " +
            "android-legacy profile reproduces", "{\"ref\":\"a/b\"}",
            PremiumObservationContract.canonicalJson(slash))
    }

    /** A lone surrogate is escaped, so the canonical bytes are always well-formed UTF-8. */
    @Test
    fun aLoneSurrogateIsEscapedAndAPairIsNot() {
        assertEquals("{\"s\":\"\\ud800x\"}", PremiumObservationContract.canonicalJson(
            JSONObject().put("s", "\uD800x")))
        assertEquals("{\"s\":\"\\udc00x\"}", PremiumObservationContract.canonicalJson(
            JSONObject().put("s", "\uDC00x")))
        val astral = JSONObject().put("s", "\uD83D\uDE00")       // U+1F600, a valid pair
        assertEquals("{\"s\":\"\uD83D\uDE00\"}",
            PremiumObservationContract.canonicalJson(astral))
        assertEquals("and it must survive as 4 UTF-8 bytes", 4,
            "\uD83D\uDE00".toByteArray(Charsets.UTF_8).size)
    }

    /**
     * The encoder must not delegate. A source guard, because this is the kind of regression a
     * later edit reintroduces silently: one `JSONObject.quote` inside [canonicalString] or
     * [canonicalJson] would make the digests platform-dependent again without any test failing.
     */
    @Test
    fun theV2EncoderContainsNoDelegationAndTheV1EncoderStillDoes() {
        val src = source("src/main/java/com/marketradar/app/PremiumObservationContract.kt")
        // the number path is shared by both serializations, so it must not delegate either
        val numStart = src.indexOf("private fun canonicalNumber(")
        val numBody = codeOnly(src.substring(numStart, src.indexOf("\n    }", numStart)))
        assertTrue("the number rule must not delegate either:\n$numBody",
            !numBody.contains("JSONObject.quote"))
        val start = src.indexOf("fun canonicalString(")
        val v1Start = src.indexOf("fun canonicalJsonV1(")
        assertTrue("the encoders must be found in source", start > 0 && v1Start > start)
        val v2Body = codeOnly(src.substring(start, v1Start))
        assertTrue("v2 must not delegate to org.json's quote:\n$v2Body",
            !v2Body.contains("JSONObject.quote"))
        val v1Body = src.substring(v1Start, src.indexOf("fun canonicalJsonFor("))
        assertTrue("v1 must keep delegating, deliberately: it has to reproduce the bytes the " +
            "runtime it is replaying actually wrote", v1Body.contains("JSONObject.quote"))
    }

    // ───────────────────────── stored evidence ─────────────────────────

    /**
     * A v1-stamped envelope — the shape already on every phone — still verifies, and would NOT
     * if verification used today's rule. The second half is what makes the first meaningful.
     */
    @Test
    fun aV1StampedEnvelopeStillVerifiesAndOnlyBecauseItsOwnRuleIsRead() {
        val filesDir = Files.createTempDirectory("premcap-v1verify").toFile()
        PremiumCaptureControl.setEnabled(filesDir, true)
        val store = PremiumCaptureStore(PremiumCaptureControl.root(filesDir))
        capture(store, "09:20")
        val v2Env = store.readEnvelopes("NF", date).first()
        assertEquals("the live producer stamps v2", v2, v2Env.getJSONObject("hash_basis")
            .getString("serialization"))
        assertTrue(PremiumObservationContract.verifyHash(v2Env))

        val v1Env = asV1(JSONObject(v2Env.toString()))
        assertEquals(v1, v1Env.getJSONObject("hash_basis").getString("serialization"))
        assertTrue("a v1 envelope must still verify after the upgrade",
            PremiumObservationContract.verifyHash(v1Env))
        assertTrue("and after the text round trip an export is",
            PremiumObservationContract.verifyHash(JSONObject(v1Env.toString())))

        // reading the declared rule is load-bearing rather than decorative: this envelope's lot
        // reference carries the slashes that made the v1 bytes platform-dependent
        assertTrue(v1Env.getJSONObject("lot").getString("contract_lot_table_ref").contains('/'))
    }

    /**
     * The derived identities too. `acquisition_id` is a digest of the envelope's declared basis —
     * which includes `lot`, and therefore the slashes — so recomputing it with today's rule would
     * report every stored envelope as not derived from its own basis. The export's verification
     * does exactly that recomputation, so this is the path that mattered.
     */
    @Test
    fun aStoredEnvelopesAcquisitionIdAndContentRefRecomputeUnderItsOwnRule() {
        val filesDir = Files.createTempDirectory("premcap-v1acq").toFile()
        PremiumCaptureControl.setEnabled(filesDir, true)
        val store = PremiumCaptureStore(PremiumCaptureControl.root(filesDir))
        capture(store, "09:20")
        val e = store.readEnvelopes("NF", date).first()
        assertEquals("a v2 envelope: today's rule and the declared rule agree",
            e.getString("acquisition_id"), PremiumObservationContract.acquisitionIdAsStored(e))

        val v1Env = asV1(JSONObject(e.toString()).put("acquisition_id",
            PremiumObservationContract.acquisitionId(JSONObject(e.toString()), v1)))
        assertEquals("the declared rule must reproduce the stored id",
            v1Env.getString("acquisition_id"),
            PremiumObservationContract.acquisitionIdAsStored(v1Env))

        // AND THE ID REALLY IS ENCODING-DEPENDENT. On THIS runtime the two rules agree on a
        // slash, so a premium body's id happens to coincide; on a phone, where v1 escaped every
        // slash, it does not. The dependence is shown here on a character the two JVM encoders
        // do disagree about, so the assertion bites on the runtime the test runs on.
        val encodingSensitive = JSONObject(v1Env.toString())
        encodingSensitive.getJSONObject("lot").put("contract_lot_table_ref", "a\u0085/b")
        assertNotEquals("the acquisition basis covers `lot`, so the id follows the encoding",
            PremiumObservationContract.acquisitionId(encodingSensitive, v1),
            PremiumObservationContract.acquisitionId(encodingSensitive, v2))
    }

    /**
     * [verifyHash] must read the rule the body declares. Two correctly stamped copies of the same
     * content, one v1 and one v2, carry DIFFERENT digests on this runtime — so a verifier pinned
     * to one encoder would reject the other. Both must verify.
     */
    @Test
    fun verifyHashFollowsEachBodysDeclaredRuleRatherThanTodays() {
        val content = JSONObject().put("note", "a\u0085b").put("ref", "x</y")
        val stampedV2 = PremiumObservationContract.stampHash(JSONObject(content.toString()))
        val stampedV1 = asV1(JSONObject(content.toString()))
        assertEquals(v2, stampedV2.getJSONObject("hash_basis").getString("serialization"))
        assertEquals(v1, stampedV1.getJSONObject("hash_basis").getString("serialization"))
        assertNotEquals("the two rules must produce different digests for this content, or " +
            "this test could pass with the selection broken",
            stampedV2.getString("payload_sha256"), stampedV1.getString("payload_sha256"))
        assertTrue("the v2 body must verify", PremiumObservationContract.verifyHash(stampedV2))
        assertTrue("and so must the v1 body", PremiumObservationContract.verifyHash(stampedV1))
        // the selector itself, stated: each tag maps to its own encoder
        assertEquals(PremiumObservationContract.canonicalJsonV1(content),
            PremiumObservationContract.canonicalJsonFor(v1)(content))
        assertEquals(PremiumObservationContract.canonicalJson(content),
            PremiumObservationContract.canonicalJsonFor(v2)(content))
        assertEquals("an absent tag predates the tag being used, so it means v1",
            PremiumObservationContract.canonicalJsonV1(content),
            PremiumObservationContract.canonicalJsonFor(null)(content))
    }

    /**
     * A WHOLE SESSION after an in-place upgrade: the 09:20 acquisition written by the old build,
     * the 09:25 acquisition written by this one, one cohort pinned under v1 and reused. The
     * export must report both as verified evidence — not the new one verified and the morning's
     * quietly failing its own digest.
     *
     * HONEST LIMIT. On this JVM the two rules encode a premium body to the same bytes except for
     * the declared tag itself: the characters they disagree about (a slash on Android, `</` and
     * the C1/U+2000 ranges here) are not in this evidence. So this is an integration proof — both
     * encodings present, both verified, the cohort reused, nothing re-stamped — while the proof
     * that the selection changes the bytes is in the character-level tests above, and the proof
     * against real Android bytes is the verifier's `android-legacy` profile on the 12 November
     * export.
     */
    @Test
    fun aSessionHoldingPreUpgradeAndPostUpgradeAcquisitionsExportsBothAsVerified() {
        // (1) one real acquisition, to take real bodies from
        val dirA = Files.createTempDirectory("premcap-mixed-a").toFile()
        PremiumCaptureControl.setEnabled(dirA, true)
        val storeA = PremiumCaptureStore(PremiumCaptureControl.root(dirA))
        capture(storeA, "09:20")
        val envA = storeA.readEnvelopes("NF", date).first()
        val recsA = storeA.readRecords("NF", date).rows
        val cohortA = JSONObject(requireNotNull(storeA.readCohortText("NF", date)))
        assertEquals(PremiumCaptureCohort.CONTRACTS_PER_COHORT, recsA.size)

        // (2) the same evidence as the OLD build would have persisted it, written through the
        // store's own public write path into a fresh session: v1 cohort, v1 envelope, v1 records.
        val dirB = Files.createTempDirectory("premcap-mixed-b").toFile()
        PremiumCaptureControl.setEnabled(dirB, true)
        val storeB = PremiumCaptureStore(PremiumCaptureControl.root(dirB))
        val v1Cohort = asV1(cohortA)
        assertTrue(storeB.putCohort("NF", date, v1Cohort).persisted)

        // the batch digest, rebuilt in pinned-cohort order — the same reconstruction the export's
        // verification performs — under v1
        val byKey = recsA.associateBy { it.optString("instrument_key") }
        val ordered = PremiumCaptureCohort.pinnedList(v1Cohort).map {
            requireNotNull(byKey[it.instrumentKey]) }
        val body = JSONObject(envA.toString())
            .put("batch_evidence_ref", PremiumObservationContract.batchEvidenceRef(
                ordered.map { PremiumObservationContract.evidenceFromRecord(it) }, v1))
        val v1Id = PremiumObservationContract.acquisitionId(body, v1)
        val v1Env = asV1(body.put("acquisition_id", v1Id))
        // the key format is the session's: date | index | slot | acquisition | role
        assertTrue(storeB.putEnvelope("NF", date, "$date|NF|09:20|$v1Id|ENVELOPE", v1Env)
            .persisted)
        val v1EnvRef = PremiumObservationContract.ref(
            PremiumObservationContract.contentHash(v1Env))
        for (r in ordered) {
            val rec = asV1(JSONObject(r.toString())
                .put("acquisition_id", v1Id).put("envelope_ref", v1EnvRef))
            assertTrue(storeB.put("NF", date,
                "$date|NF|09:20|$v1Id|${rec.getString("instrument_key")}", rec).persisted)
        }

        // (3) the upgrade: this build captures the next slot into the same session
        capture(storeB, "09:25")

        val sec = PremiumExportSection.build(dirB, date)
        val nf = sec.getJSONObject("indices").getJSONObject("NF")
        val acct = nf.getJSONObject("accounting")
        assertEquals("both acquisitions' records must be present", 12, acct.getInt("records"))
        assertEquals("and every one of them must verify its own digest",
            12, acct.getInt("hash_verified"))
        assertEquals(2, acct.getInt("slot_envelopes"))
        assertEquals("including the envelope stamped before the upgrade",
            2, acct.getInt("envelope_hash_verified"))

        val acqs = nf.getJSONObject("acquisitions")
        assertEquals("both batches must be usable evidence, not one of them",
            2, acqs.getInt("verified_batches"))
        val rows = acqs.getJSONArray("rows")
        assertEquals(2, rows.length())
        for (i in 0 until rows.length()) {
            val row = rows.getJSONObject(i)
            assertEquals("${row.optString("slot")}: ${row.optJSONArray("integrity_reasons")}",
                "VERIFIED", row.getString("verified_state"))
            assertEquals(0, row.getJSONArray("integrity_reasons").length())
        }

        // and the export really does carry both encodings, or the assertions above would be
        // describing a single-version session
        val declared = (0 until nf.getJSONArray("slot_envelopes").length()).map {
            nf.getJSONArray("slot_envelopes").getJSONObject(it)
                .getJSONObject("hash_basis").getString("serialization")
        }.toSet()
        assertEquals(setOf(v1, v2), declared)
        assertEquals("the pre-upgrade cohort must be reused, not re-pinned under the new rule",
            v1, JSONObject(requireNotNull(storeB.readCohortText("NF", date)))
                .getJSONObject("hash_basis").getString("serialization"))
    }
}
