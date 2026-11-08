package com.marketradar.app

import org.json.JSONArray
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
import java.util.concurrent.CountDownLatch

/**
 * Bounded exact-contract IV/quote observation.
 *
 * Built against the COMMITTED RAW VENDOR FIXTURE
 * `app/src/test/resources/contracts/x1_upstox_raw_chains.json`, which carries real
 * `/v2/option/chain` response shape and holds matched NF and BNF snapshots for one session — which
 * is what the index-swap and index-isolation cases need.
 *
 * Everything here is desktop/JVM. **No device evidence exists and none is claimed.**
 *
 * The P1-P6 sections correspond to Codex review R1. Each carries at least one case that FAILS
 * against the previous implementation, named in a comment so the before/after is checkable.
 */
class PremiumCaptureTest {

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

    private fun tmpStore(): PremiumCaptureStore =
        PremiumCaptureStore(Files.createTempDirectory("premcap").toFile())

    /**
     * Strip block comments, line comments and string literals before a source scan, so an
     * assertion about what the code CALLS is not satisfied or broken by what it MENTIONS.
     */
    private fun stripCommentsAndStrings(src: String): String = src
        .replace(Regex("/\\*.*?\\*/", RegexOption.DOT_MATCHES_ALL), " ")
        .replace(Regex("//[^\n]*"), " ")
        .replace(Regex("\"(?:\\\\.|[^\"\\\\])*\""), "\"\"")

    private fun md(vararg kv: Pair<String, Any?>): JSONObject {
        val o = JSONObject()
        kv.forEach { (k, v) -> o.put(k, v ?: JSONObject.NULL) }
        return o
    }

    private fun leg(key: String, market: JSONObject?, greeks: JSONObject?): JSONObject {
        val o = JSONObject().put("instrument_key", key)
        if (market != null) o.put("market_data", market)
        if (greeks != null) o.put("option_greeks", greeks)
        return o
    }

    /** A minimal chain with paired CE/PE keys per strike, for the identity/validation cases. */
    private fun chain(expiry: String, underlying: String, spot: Double, strikes: List<Double>,
                      ceKey: (Double) -> String? = { "CE|$it" },
                      peKey: (Double) -> String? = { "PE|$it" }): JSONObject {
        val data = JSONArray()
        for (k in strikes) {
            val row = JSONObject().put("strike_price", k).put("expiry", expiry)
                .put("underlying_key", underlying).put("underlying_spot_price", spot)
            ceKey(k)?.let { row.put("call_options", leg(it,
                md("bid_price" to 10.0, "ask_price" to 10.5, "bid_qty" to 50, "ask_qty" to 50,
                    "ltp" to 10.2), md("iv" to 12.5))) }
            peKey(k)?.let { row.put("put_options", leg(it,
                md("bid_price" to 9.0, "ask_price" to 9.5, "bid_qty" to 50, "ask_qty" to 50,
                    "ltp" to 9.2), md("iv" to 13.0))) }
            data.put(row)
        }
        return JSONObject().put("data", data)
    }

    /** A record whose serialized payload is EXACTLY `target` bytes, for boundary cases. */
    private fun recordOfBytes(target: Int, tag: String): JSONObject {
        var pad = target
        var rec = JSONObject()
        while (pad > 0) {
            rec = JSONObject().put("tag", tag).put("pad", "x".repeat(pad))
            val len = rec.toString().toByteArray(Charsets.UTF_8).size
            if (len == target) return rec
            pad -= (len - target)
            if (pad < 0) break
        }
        throw AssertionError("could not build a $target B record")
    }

    private fun observeFixture(store: PremiumCaptureStore, snapId: String, index: String,
                               slot: String, receivedMs: Long = 1250L,
                               lotRef: String? = "contract_lot_table/NSE/2026-10-01",
                               mapRef: String? = "instrument_map/2026-10-01",
                               lot: Any? = 75, lotSource: String? = "CONTRACT_LOT_TABLE")
            : Pair<PremiumCaptureSession.SlotResult, JSONObject> {
        val snap = rawFixture(snapId)
        val s = PremiumCaptureSession(store)
        val r = s.observeSlot(index, snap.getString("session"), snap.getJSONObject("raw"),
            null, slot, snap.optString("requested_underlying_key").ifEmpty { null },
            receivedMs - 250L, receivedMs,
            claimedLot = lot, claimedLotSource = lotSource,
            lotTableRef = lotRef, mapRef = mapRef)
        return r to snap
    }

    // ═════════════════ P1 — the admission gate bounds the bytes actually written ═══════════════

    /**
     * FAILS BEFORE THE FIX. `archiveConflict` admitted on the ORIGINAL payload size and then wrote
     * a larger JSON wrapper, and never applied the per-record cap to the archive at all. A 1,900 B
     * payload wraps to more than 2,048 B once it is embedded as an escaped string plus two
     * digests, so the old code wrote an over-cap file after an in-cap check.
     */
    @Test
    fun aConflictArchiveIsAdmittedOnTheWrapperBytesNotThePayloadBytes() {
        val store = tmpStore()
        val first = recordOfBytes(1900, "first")
        assertEquals(PremiumCaptureStore.WRITTEN,
            store.put("NF", "S", "k", first).status)
        val second = recordOfBytes(1900, "second")
        val r = store.put("NF", "S", "k", second)
        val wrapperWouldBe = JSONObject()
            .put("conflict_of_key_sha256", PremiumObservationContract.sha256Hex("k"))
            .put("retained_evidence_policy", "FIRST_WRITE_WINS_CONFLICT_ARCHIVED")
            .put("payload_sha256", PremiumObservationContract.sha256Hex(second.toString()))
            .put("payload", second.toString()).toString()
            .toByteArray(Charsets.UTF_8).size
        assertTrue("the wrapper must actually exceed the cap for this case to mean anything: " +
            "$wrapperWouldBe B vs ${PremiumCaptureStore.MAX_BYTES_PER_RECORD} B",
            wrapperWouldBe > PremiumCaptureStore.MAX_BYTES_PER_RECORD)
        assertEquals(PremiumCaptureStore.REFUSED_RECORD_TOO_LARGE, r.status)
        assertEquals("nothing may be written when the wrapper is over cap",
            0, store.conflictCount("NF", "S"))
        // and the original evidence is untouched
        assertEquals(1, store.readAll("NF", "S").size)
        assertEquals("first", store.readAll("NF", "S").first().getString("tag"))
    }

    /** FAILS BEFORE THE FIX: conflicting content under an existing key was called a duplicate. */
    @Test
    fun conflictingContentIsNeverReportedAsADuplicate() {
        val store = tmpStore()
        store.put("NF", "S", "k", JSONObject().put("v", 1))
        val r = store.put("NF", "S", "k", JSONObject().put("v", 2))
        assertEquals(PremiumCaptureStore.CONFLICT_ARCHIVED, r.status)
        assertNotEquals(PremiumCaptureStore.DUPLICATE_NO_OP, r.status)
        assertEquals(1, store.conflictCount("NF", "S"))
        // first write still wins for the primary record
        assertEquals(1, store.readAll("NF", "S").first().getInt("v"))
        // and the archived payload is the one that lost, retained verbatim
        val archived = store.readConflicts("NF", "S").rows.first()
        assertEquals(JSONObject().put("v", 2).toString(), archived.getString("payload"))
        // an identical re-offer of the same content is a duplicate, not a second archive
        assertEquals(PremiumCaptureStore.DUPLICATE_NO_OP,
            store.put("NF", "S", "k", JSONObject().put("v", 1)).status)
    }

    /**
     * FAILS BEFORE THE FIX: `saveCohort` lived in the session and wrote `cohort.json` directly,
     * with no session/global/record budgeting at all.
     */
    @Test
    fun theCohortIsWrittenThroughTheStoreAndIsRefusedWhenTheGlobalCapIsReached() {
        val root = Files.createTempDirectory("premcap-full").toFile()
        val store = PremiumCaptureStore(root)
        // fill the store to its global cap with a non-json filler, which the byte walk counts
        val filler = File(root, "filler.bin")
        filler.parentFile?.mkdirs()
        filler.writeBytes(ByteArray(PremiumCaptureStore.MAX_BYTES_GLOBAL.toInt()) { 0x61 })
        assertTrue(store.globalBytes() >= PremiumCaptureStore.MAX_BYTES_GLOBAL)
        val cohort = PremiumObservationContract.stampHash(JSONObject().put("cohort_id", "x"))
        val r = store.putCohort("NF", "S", cohort)
        assertEquals(PremiumCaptureStore.REFUSED_GLOBAL_BYTES, r.status)
        assertNull("no cohort may be written at the cap", store.readCohortText("NF", "S"))
    }

    /** A different cohort must never replace a pin that records already resolve through. */
    @Test
    fun aDifferentCohortIsRefusedRatherThanReplacingThePin() {
        val store = tmpStore()
        val a = PremiumObservationContract.stampHash(JSONObject().put("cohort_id", "A"))
        val b = PremiumObservationContract.stampHash(JSONObject().put("cohort_id", "B"))
        assertEquals(PremiumCaptureStore.WRITTEN, store.putCohort("NF", "S", a).status)
        assertEquals(PremiumCaptureStore.DUPLICATE_NO_OP, store.putCohort("NF", "S", a).status)
        assertEquals(PremiumCaptureStore.REFUSED_COHORT_CAP, store.putCohort("NF", "S", b).status)
        assertEquals("A", JSONObject(store.readCohortText("NF", "S")!!).getString("cohort_id"))
    }

    /**
     * FAILS BEFORE THE FIX: the enumerator visited only records/conflicts/envelopes, so a
     * half-written cohort or summary in the session directory was invisible.
     */
    @Test
    fun aCohortOrSummaryTempFileIsVisibleToTheOrphanEnumerator() {
        val store = tmpStore()
        store.put("NF", "S", "k", JSONObject().put("v", 1))
        val sessionDir = File(store.root, "S/NF")
        File(sessionDir, "cohort.json.tmp").writeText("{\"partial\":")
        File(sessionDir, "summary.json.tmp").writeText("{\"partial\":")
        val names = store.orphanTempFiles("NF", "S").map { it.name }.toSet()
        assertTrue("cohort temp must be reported, got $names", names.contains("cohort.json.tmp"))
        assertTrue("summary temp must be reported, got $names", names.contains("summary.json.tmp"))
    }

    /**
     * FAILS BEFORE THE FIX: capacity checks and writes were not serialized, so two callers sharing
     * a root could both pass the same check. Two SEPARATE store instances are used deliberately —
     * that is the case an instance-level lock would miss.
     */
    @Test
    fun concurrentWritersOverOneRootCannotBothPassTheLastSlotInTheBudget() {
        val root = Files.createTempDirectory("premcap-race").toFile()
        val size = 1000
        val budget = PremiumCaptureStore.MAX_BYTES_PER_INDEX_SESSION -
            PremiumCaptureStore.SUMMARY_RESERVE_BYTES
        val sessionDir = File(root, "S/NF")
        sessionDir.mkdirs()
        // leave room for exactly one more record of `size` bytes
        File(sessionDir, "filler.bin").writeBytes(ByteArray((budget - size).toInt()) { 0x61 })

        val threads = 8
        val ready = CountDownLatch(threads)
        val go = CountDownLatch(1)
        val results = java.util.Collections.synchronizedList(ArrayList<String>())
        (0 until threads).map { i ->
            Thread {
                val own = PremiumCaptureStore(root)          // a separate instance per thread
                ready.countDown(); go.await()
                results.add(own.put("NF", "S", "k$i", recordOfBytes(size, "t$i")).status)
            }.also { it.start() }
        }.let { ts ->
            ready.await(); go.countDown(); ts.forEach { it.join(30_000) }
        }
        val written = results.count { it == PremiumCaptureStore.WRITTEN }
        assertEquals("exactly one writer may take the last slot, got $results", 1, written)
        assertEquals(threads, results.size)
        assertTrue("session bytes ${PremiumCaptureStore(root).sessionBytes("NF", "S")} " +
            "must stay within $budget",
            PremiumCaptureStore(root).sessionBytes("NF", "S") <= budget)
    }

    @Test
    fun everyWriteKindIsRefusedAboveThePerRecordCapAndTheCapsSaySo() {
        val store = tmpStore()
        val tooBig = recordOfBytes((PremiumCaptureStore.MAX_BYTES_PER_RECORD + 1).toInt(), "big")
        assertEquals(PremiumCaptureStore.REFUSED_RECORD_TOO_LARGE,
            store.put("NF", "S", "k", tooBig).status)
        assertEquals(PremiumCaptureStore.REFUSED_RECORD_TOO_LARGE,
            store.putEnvelope("NF", "S", "e", tooBig).status)
        assertEquals(PremiumCaptureStore.REFUSED_RECORD_TOO_LARGE,
            store.putCohort("NF", "S", tooBig).status)
        val caps = store.capsJson()
        assertEquals("ACTUAL_FINAL_BYTES_INCLUDING_CONFLICT_WRAPPERS",
            caps.getString("admission_measures"))
        assertTrue(caps.getBoolean("admission_serialized_per_root"))
        assertTrue(caps.getBoolean("conflict_archives_inside_caps"))
        assertEquals("NONE_IN_THIS_DELIVERY", caps.getString("retention_deletion"))
        assertEquals(468, caps.getInt("max_outcomes_per_index_session"))
    }

    // ═════════════════ P2 — a slot cannot join quotes to the wrong acquisition ═════════════════

    /**
     * THE CENTRAL P2 CASE, and it FAILS BEFORE THE FIX.
     *
     * Acquisition A observes a slot. Acquisition B retries the SAME slot with a different receipt
     * clock and a different response. Before the fix the join was `session+index+slot`, B's
     * envelope was archived as a conflict while A's stayed primary, and B's observations filled
     * whichever record keys A had not written — so the export joined B's quotes to A's clocks.
     *
     * Now each acquisition has an identity derived from its own evidence, record keys include it,
     * and every record carries its own envelope's content reference.
     */
    @Test
    fun aSameSlotRetryIsADistinctAcquisitionAndNeverMergesWithTheFirst() {
        val store = tmpStore()
        val (a, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20", receivedMs = 1_000)
        assertEquals(PremiumCaptureSession.OBSERVED, a.status)
        assertEquals(6, a.written)
        val session = snap.getString("session")

        // same slot, different acquisition evidence (clock) and a different response
        val s = PremiumCaptureSession(store)
        val other = rawFixture("mon_0925_control_NF")
        val b = s.observeSlot("NF", session, other.getJSONObject("raw"), null, "09:20",
            null, 90_000L, 99_999L, claimedLot = 75, claimedLotSource = "CONTRACT_LOT_TABLE",
            lotTableRef = "contract_lot_table/NSE/2026-10-01", mapRef = "instrument_map/2026-10-01")
        assertEquals(PremiumCaptureSession.OBSERVED, b.status)
        assertNotNull(a.acquisitionId); assertNotNull(b.acquisitionId)
        assertNotEquals("different evidence must mean a different acquisition",
            a.acquisitionId, b.acquisitionId)

        // two envelopes, twelve records, and no conflict: these are distinct acquisitions, not a
        // collision between one slot's two attempts
        assertEquals(2, store.envelopeCount("NF", session))
        assertEquals(12, store.outcomeCount("NF", session))
        assertEquals(0, store.conflictCount("NF", session))

        val ex = PremiumCaptureSession(store).export("NF", session, "09:20")
        val rows = ex.getJSONObject("acquisitions").getJSONArray("rows")
        assertEquals(2, rows.length())
        for (i in 0 until rows.length()) {
            val r = rows.getJSONObject(i)
            assertEquals("each acquisition must own exactly its six observations",
                6, r.getInt("observations"))
            assertEquals("and every one must link to THIS envelope",
                6, r.getInt("observations_linked_to_this_envelope"))
            assertEquals(6, r.getInt("unique_instrument_keys"))
            assertEquals("COMPLETE", r.getString("batch_state"))
        }
        // no record may carry an envelope reference belonging to the other acquisition
        val envByAcq = HashMap<String, String>()
        store.readEnvelopes("NF", session).forEach {
            envByAcq[it.getString("acquisition_id")] =
                PremiumObservationContract.ref(PremiumObservationContract.contentHash(it))
        }
        store.readAll("NF", session).forEach {
            assertEquals("record bound to the wrong envelope",
                envByAcq[it.getString("acquisition_id")], it.getString("envelope_ref"))
        }
    }

    /**
     * FAILS BEFORE THE FIX: the old `observeSlot` wrote all six observations even when the
     * envelope write was refused, leaving records with nothing to resolve against.
     */
    @Test
    fun anEnvelopeThatIsNotPersistedWritesNoObservations() {
        val store = tmpStore()
        val snap = rawFixture("mon_0920_entry_NF")
        val session = snap.getString("session")
        // exhaust the envelope bound directly, so the real envelope write must be refused
        for (i in 0 until PremiumCaptureStore.MAX_ENVELOPES_PER_INDEX_SESSION) {
            assertEquals(PremiumCaptureStore.WRITTEN,
                store.putEnvelope("NF", session, "filler$i",
                    PremiumObservationContract.stampHash(JSONObject().put("n", i))).status)
        }
        val (r, _) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        assertEquals(PremiumCaptureSession.ENVELOPE_NOT_PERSISTED, r.status)
        assertEquals(0, r.written)
        assertEquals("no observation may exist without its acquisition envelope",
            0, store.outcomeCount("NF", session))
        // and the refusal is durable, not only in the returned result
        assertTrue(store.readCounters("NF", session)
            .optLong("refused_envelope_not_persisted", 0L) >= 1L)
    }

    /** A crash after some observations leaves a batch that the export reports as PARTIAL. */
    @Test
    fun aPartialBatchIsVisibleInTheExportRatherThanLookingComplete() {
        val store = tmpStore()
        val (r, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        assertEquals(6, r.written)
        val session = snap.getString("session")
        // simulate a crash after four of the six writes
        val recs = File(store.root, "$session/NF/records").listFiles()!!
            .filter { it.name.endsWith(".json") }.sortedBy { it.name }
        recs.take(2).forEach { assertTrue(it.delete()) }
        val ex = PremiumCaptureSession(store).export("NF", session, "09:20")
        val row = ex.getJSONObject("acquisitions").getJSONArray("rows").getJSONObject(0)
        assertEquals("PARTIAL", row.getString("batch_state"))
        assertEquals(4, row.getInt("observations"))
        assertEquals(6, row.getInt("observations_expected"))
        assertEquals(0, ex.getJSONObject("acquisitions").getInt("complete_batches"))
        assertEquals(1, ex.getJSONObject("acquisitions").getInt("partial_or_empty_batches"))
        // the slot still says OBSERVED, but completeness is reported separately and explicitly
        assertEquals("OBSERVED", ex.getJSONObject("slot_manifest").getJSONArray("rows")
            .getJSONObject(0).getString("state"))
    }

    @Test
    fun recordsWithNoEnvelopeAreReportedUnresolvedNotCounted() {
        val store = tmpStore()
        val (_, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        val session = snap.getString("session")
        File(store.root, "$session/NF/envelopes").listFiles()!!.forEach { it.delete() }
        val ex = PremiumCaptureSession(store).export("NF", session, "09:20")
        val unresolved = ex.getJSONObject("acquisitions").getJSONArray("unresolved_record_groups")
        assertEquals(1, unresolved.length())
        assertEquals("UNRESOLVED_NO_ENVELOPE", unresolved.getJSONObject(0).getString("state"))
        assertEquals(6, unresolved.getJSONObject(0).getInt("records"))
    }

    // ═════════════════ P3 — refusal, conflict and corruption evidence is durable ═══════════════

    /**
     * FAILS BEFORE THE FIX: `writeSummary` had no caller, so every refusal reason lived only in a
     * returned object. A NEW store instance is used to read, so this proves durability on disk.
     */
    @Test
    fun refusalsSurviveInDurableCountersNotOnlyInAReturnedResult() {
        val store = tmpStore()
        val snap = rawFixture("mon_0920_entry_NF")
        val session = snap.getString("session")
        val s = PremiumCaptureSession(store)
        // outside the window, and an unusable slot identity: both refuse and both must persist
        s.observeSlot("NF", session, snap.getJSONObject("raw"), "15:30", "15:30", null, 1L, 2L)
        s.observeSlot("NF", session, snap.getJSONObject("raw"), "09:25", "09:20", null, 1L, 2L)
        val counters = PremiumCaptureStore(store.root).readCounters("NF", session)
        assertTrue("outside-window refusal must be durable: $counters",
            counters.optLong("refused_outside_window", 0L) >= 1L)
        assertTrue("slot-identity refusal must be durable: $counters",
            counters.optLong("refused_slot_identity_unusable", 0L) >= 1L)
    }

    /**
     * FAILS BEFORE THE FIX: the summary went through a first-write-only path, so a second update
     * was reported as a duplicate and the counters could never advance.
     */
    @Test
    fun theSummaryIsReplaceableSoCountersActuallyAccumulate() {
        val store = tmpStore()
        assertEquals(PremiumCaptureStore.WRITTEN,
            store.bumpCounters("NF", "S", mapOf("x" to 1L)).status)
        assertEquals(PremiumCaptureStore.REPLACED,
            store.bumpCounters("NF", "S", mapOf("x" to 2L)).status)
        assertEquals(PremiumCaptureStore.REPLACED,
            store.bumpCounters("NF", "S", mapOf("y" to 5L)).status)
        val c = store.readCounters("NF", "S")
        assertEquals(3L, c.getLong("x"))
        assertEquals(5L, c.getLong("y"))
        assertTrue(store.summaryIsReadable("NF", "S"))
    }

    @Test
    fun anUnreadableSummaryIsRecordedRatherThanSilentlyResetToZero() {
        val store = tmpStore()
        store.bumpCounters("NF", "S", mapOf("x" to 7L))
        File(store.root, "S/NF/summary.json").writeText("{not json")
        assertFalse(store.summaryIsReadable("NF", "S"))
        assertEquals(PremiumCaptureStore.REPLACED,
            store.bumpCounters("NF", "S", mapOf("x" to 1L)).status)
        val c = store.readCounters("NF", "S")
        assertEquals("the lost prior count is not invented back", 1L, c.getLong("x"))
        assertEquals("but losing it is recorded", 1L, c.getLong("summary_unreadable_resets"))
    }

    /**
     * FAILS BEFORE THE FIX: `loadCohort` turned a parse failure into null, which was
     * indistinguishable from "no cohort", so a corrupt cohort caused silent RESELECTION — repinning
     * different contracts mid-session while earlier records still claimed the original ones.
     */
    @Test
    fun aCorruptCohortIsRefusedExplicitlyAndNeverSilentlyReselected() {
        val store = tmpStore()
        val (_, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        val session = snap.getString("session")
        val cohortFile = File(store.root, "$session/NF/cohort.json")
        val before = cohortFile.readText()
        cohortFile.writeText("{\"cohort_id\": truncated")

        val st = PremiumCaptureSession(store).loadCohort("NF", session)
        assertEquals("INVALID", st.state)
        assertEquals("cohort_absent_or_unparseable", st.reason)

        val s = PremiumCaptureSession(store)
        val r = s.observeSlot("NF", session, snap.getJSONObject("raw"), null, "09:25",
            null, 1L, 2L)
        assertEquals(PremiumCaptureSession.COHORT_INVALID_REFUSED, r.status)
        assertEquals(0, r.written)
        assertEquals("the corrupt cohort must not be overwritten by a reselection",
            "{\"cohort_id\": truncated", cohortFile.readText())
        assertNotEquals(before, cohortFile.readText())
        val ex = PremiumCaptureSession(store).export("NF", session, "09:20")
        assertEquals("INVALID", ex.getJSONObject("cohort_state").getString("state"))
        assertFalse(ex.getJSONObject("cohort_state").getBoolean("reselection_on_invalid"))
    }

    /** A cohort whose stamped content was altered must not validate. */
    @Test
    fun aTamperedCohortDoesNotValidate() {
        val store = tmpStore()
        val (_, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        val session = snap.getString("session")
        val cohort = JSONObject(store.readCohortText("NF", session)!!)
        assertNull(PremiumCaptureCohort.validateLoaded(cohort, "NF", session))
        val altered = JSONObject(cohort.toString())
        altered.getJSONArray("pinned_contracts").getJSONObject(0).put("strike", 99999.0)
        assertEquals("cohort_hash_does_not_verify",
            PremiumCaptureCohort.validateLoaded(altered, "NF", session))
        // a cohort for another index or session is refused even if it verifies
        assertEquals("cohort_index_mismatch",
            PremiumCaptureCohort.validateLoaded(cohort, "BNF", session))
        assertEquals("cohort_session_mismatch",
            PremiumCaptureCohort.validateLoaded(cohort, "NF", "1999-01-01"))
    }

    /** FAILS BEFORE THE FIX: unparseable files were silently omitted from every read. */
    @Test
    fun corruptRecordAndEnvelopeFilesAreAccountedNotOmitted() {
        val store = tmpStore()
        val (_, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        val session = snap.getString("session")
        File(File(store.root, "$session/NF/records"), "zz_broken.json").writeText("{oops")
        File(File(store.root, "$session/NF/envelopes"), "zz_broken.json").writeText("{oops")
        val rr = store.readRecords("NF", session)
        assertEquals(6, rr.rows.size)
        assertEquals(listOf("zz_broken.json"), rr.corrupt)
        val ex = PremiumCaptureSession(store).export("NF", session, "09:20")
        val integ = ex.getJSONObject("integrity")
        assertEquals(1, integ.getJSONArray("corrupt_record_files").length())
        assertEquals(1, integ.getJSONArray("corrupt_envelope_files").length())
        assertEquals(2, ex.getJSONObject("accounting").getInt("corrupt_files"))
    }

    /** FAILS BEFORE THE FIX: conflicts were counted but their payloads never left the device. */
    @Test
    fun conflictEvidenceAndItsLinkageAreExportedNotJustCounted() {
        val store = tmpStore()
        val (_, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        val session = snap.getString("session")
        val keyHash = PremiumObservationContract.sha256Hex("deliberate-conflict-key")
        store.put("NF", session, "deliberate-conflict-key", JSONObject().put("v", 1))
        store.put("NF", session, "deliberate-conflict-key", JSONObject().put("v", 2))
        val ex = PremiumCaptureSession(store).export("NF", session, "09:20")
        val conflicts = ex.getJSONObject("conflicts")
        assertEquals(1, conflicts.getInt("retained"))
        val ev = conflicts.getJSONArray("evidence").getJSONObject(0)
        assertEquals(keyHash, ev.getString("conflict_of_key_sha256"))
        assertEquals("FIRST_WRITE_WINS_CONFLICT_ARCHIVED",
            ev.getString("retained_evidence_policy"))
        assertEquals(JSONObject().put("v", 2).toString(), ev.getString("payload"))
    }

    // ═════════════════ P4 — references, lot authority and cohort identity ═════════════════════

    /**
     * FAILS BEFORE THE FIX, and this one was silent: `export` read lot/map references from
     * `records.firstOrNull()`, but the envelope split had moved `lot` out of the records, so those
     * exported fields were ALWAYS null even though the envelopes carried them.
     */
    @Test
    fun exportReadsLotAndMapReferencesFromEnvelopesAfterTheSplit() {
        val store = tmpStore()
        val (_, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        val session = snap.getString("session")
        val refs = PremiumCaptureSession(store).export("NF", session, "09:20")
            .getJSONObject("references")
        assertEquals("SLOT_ENVELOPES", refs.getString("read_from"))
        val lotNames = refs.getJSONObject("contract_lot_table").getJSONArray("referenced_names")
        assertEquals(1, lotNames.length())
        assertEquals("contract_lot_table/NSE/2026-10-01", lotNames.getString(0))
        val mapNames = refs.getJSONObject("instrument_map").getJSONArray("referenced_names")
        assertEquals(1, mapNames.length())
        assertEquals("instrument_map/2026-10-01", mapNames.getString(0))
        // the records themselves must NOT carry lot any more
        assertFalse(store.readAll("NF", session).first().has("lot"))
    }

    /** A reference NAME is not the data it names, and the export must say so. */
    @Test
    fun anUnsuppliedReferenceIsUnresolvedAndClaimsNoOfflineResolution() {
        val store = tmpStore()
        val (_, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        val session = snap.getString("session")
        val ex = PremiumCaptureSession(store).export("NF", session, "09:20")
        val lot = ex.getJSONObject("references").getJSONObject("contract_lot_table")
        assertEquals("UNRESOLVED_REFERENCE", lot.getString("resolution"))
        assertTrue(lot.isNull("metadata"))
        assertFalse(ex.getJSONObject("claims").getBoolean("offline_resolution_of_lot_table"))
        assertFalse(ex.getJSONObject("claims").getBoolean("offline_resolution_of_instrument_map"))

        // supplied metadata resolves, and is pinned by content
        val table = JSONObject().put("NSE", JSONObject().put("NIFTY", 75))
        val ex2 = PremiumCaptureSession(store).export("NF", session, "09:20",
            lotTableObject = table)
        val lot2 = ex2.getJSONObject("references").getJSONObject("contract_lot_table")
        assertEquals("RESOLVED_FROM_SUPPLIED_METADATA", lot2.getString("resolution"))
        assertEquals(PremiumObservationContract.ref(
            PremiumObservationContract.contentHash(table)), lot2.getString("metadata_ref"))
        assertTrue(ex2.getJSONObject("claims").getBoolean("offline_resolution_of_lot_table"))
    }

    /**
     * FAILS BEFORE THE FIX: authority came from the caller's string alone, so
     * `claimedLotSource = "CONTRACT_LOT_TABLE"` with no dated evidence and even a FRACTIONAL lot
     * was reported as `RESOLVED_FROM_DATED_SOURCE`.
     */
    @Test
    fun lotAuthorityRequiresAnAcceptedSourceADatedReferenceAndAnIntegerLot() {
        fun lot(v: Any?, src: String?, ref: String?) =
            PremiumObservationContract.lotEvidence(v, src, ref, null)

        val ok = lot(75, "CONTRACT_LOT_TABLE", "contract_lot_table/NSE/2026-11-07")
        assertTrue(ok.getBoolean("lot_authoritative_resolved"))
        assertEquals("RESOLVED_FROM_DATED_SOURCE", ok.getString("lot_status"))
        assertEquals(0, ok.getJSONArray("lot_unverified_reasons").length())

        val noRef = lot(75, "CONTRACT_LOT_TABLE", null)
        assertFalse(noRef.getBoolean("lot_authoritative_resolved"))
        assertTrue(noRef.getJSONArray("lot_unverified_reasons").toString()
            .contains("no_dated_reference_supplied"))

        val undated = lot(75, "CONTRACT_LOT_TABLE", "contract_lot_table/NSE/current")
        assertFalse(undated.getBoolean("lot_authoritative_resolved"))

        val fractional = lot(75.5, "CONTRACT_LOT_TABLE", "contract_lot_table/NSE/2026-11-07")
        assertFalse("a fractional lot is not a contract size",
            fractional.getBoolean("lot_authoritative_resolved"))
        assertTrue(fractional.getJSONArray("lot_unverified_reasons").toString()
            .contains("lot_value_not_a_positive_integer"))

        val constant = lot(75, "CONSTANT", "contract_lot_table/NSE/2026-11-07")
        assertFalse("a hardcoded constant is never authoritative",
            constant.getBoolean("lot_authoritative_resolved"))
        assertTrue(constant.getJSONArray("lot_unverified_reasons").toString()
            .contains("lot_source_not_accepted"))

        val zero = lot(0, "CHAIN_METADATA", "chain/2026-11-07")
        assertFalse(zero.getBoolean("lot_authoritative_resolved"))
        assertEquals(PremiumObservationContract.OBSERVED_ZERO,
            zero.getJSONObject("lot_claimed_raw").getString("status"))
    }

    /**
     * FAILS BEFORE THE FIX: observation identity resolved through a cohort that carried no
     * integrity stamp and was not covered by the record's hash, so altering the cohort could
     * reinterpret which contract a record described while its hash still verified.
     */
    @Test
    fun alteringTheCohortIsDetectableFromTheRecordsOwnCohortReference() {
        val store = tmpStore()
        val (_, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        val session = snap.getString("session")
        val cohort = JSONObject(store.readCohortText("NF", session)!!)
        val rec = store.readAll("NF", session).first()
        assertEquals(PremiumObservationContract.ref(
            PremiumObservationContract.contentHash(cohort)), rec.getString("cohort_ref"))
        // the record still verifies on its own, and that is exactly why the binding is needed
        assertTrue(PremiumObservationContract.verifyHash(rec))
        val altered = JSONObject(cohort.toString())
        altered.getJSONArray("pinned_contracts").getJSONObject(0).put("strike", 1.0)
        assertNotEquals("an altered cohort must no longer match the record's reference",
            rec.getString("cohort_ref"),
            PremiumObservationContract.ref(PremiumObservationContract.contentHash(altered)))
    }

    // ═════════════════ P5 — the validation boundary ════════════════════════════════════════════

    /**
     * FAILS BEFORE THE FIX: the window was checked only while SELECTING, so once a cohort was
     * pinned an acquisition at or after 15:05 still produced observations.
     */
    @Test
    fun anAcquisitionOutsideTheWindowIsRefusedEvenWithACohortAlreadyPinned() {
        val store = tmpStore()
        val (first, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        assertEquals(PremiumCaptureSession.OBSERVED, first.status)
        val session = snap.getString("session")
        val s = PremiumCaptureSession(store)
        for (late in listOf("15:05", "15:30", "16:00")) {
            val r = s.observeSlot("NF", session, snap.getJSONObject("raw"), late, late,
                null, 1L, 2L)
            assertEquals("$late must be refused", PremiumCaptureSession.OUTSIDE_WINDOW, r.status)
            assertEquals(0, r.written)
        }
        assertEquals("only the in-window acquisition may have written",
            6, store.outcomeCount("NF", session))
        // the open edge is inclusive and the close edge is exclusive
        assertTrue(PremiumCaptureCohort.inWindow("09:20"))
        assertFalse(PremiumCaptureCohort.inWindow("09:15"))
        assertTrue(PremiumCaptureCohort.inWindow("15:00"))
        assertFalse(PremiumCaptureCohort.inWindow("15:05"))
    }

    /**
     * FAILS BEFORE THE FIX: `schedulerSlot` was accepted as any non-empty string and was never
     * compared with the acquisition clock, so a mislabelled slot relabelled real quotes.
     */
    @Test
    fun aSchedulerSlotMustBeValidOnGridAndAgreeWithTheAcquisitionClock() {
        assertEquals("SCHEDULER_SLOT",
            PremiumCaptureCohort.slotIdentity("09:20", "09:23")?.basis)
        assertEquals("DERIVED_FROM_ACQUISITION_CLOCK",
            PremiumCaptureCohort.slotIdentity(null, "09:23")?.basis)
        assertEquals("SCHEDULER_SLOT_INVALID",
            PremiumCaptureCohort.slotIdentity("not-a-slot", "09:23")?.basis)
        assertEquals("SCHEDULER_SLOT_INVALID",
            PremiumCaptureCohort.slotIdentity("09:23", "09:23")?.basis)   // off the 5-minute grid
        assertEquals("SCHEDULER_SLOT_DISAGREES_WITH_CLOCK",
            PremiumCaptureCohort.slotIdentity("09:20", "09:31")?.basis)
        assertNull(PremiumCaptureCohort.slotIdentity(null, null))
        assertNotNull(PremiumCaptureCohort.slotIdentity("09:20", "09:31")?.disagreement)

        // and the session refuses rather than picking one of the two
        val store = tmpStore()
        val snap = rawFixture("mon_0920_entry_NF")
        val r = PremiumCaptureSession(store).observeSlot("NF", snap.getString("session"),
            snap.getJSONObject("raw"), "09:31", "09:20", null, 1L, 2L)
        assertEquals(PremiumCaptureSession.SLOT_IDENTITY_UNUSABLE, r.status)
        assertEquals(0, r.written)
    }

    /**
     * FAILS BEFORE THE FIX: the index label was trusted independently of the underlying key, so a
     * Bank Nifty response could be pinned and stored under `NF`.
     */
    @Test
    fun anIndexLabelCannotContradictTheUnderlyingKeyInEitherDirection() {
        val bnf = rawFixture("mon_0920_entry_BNF").getJSONObject("raw")
        val nf = rawFixture("mon_0920_entry_NF").getJSONObject("raw")
        assertEquals(PremiumCaptureCohort.INDEX_UNDERLYING_MISMATCH,
            PremiumCaptureCohort.select(bnf, "NF", "2026-10-19", "09:20", null).status)
        assertEquals(PremiumCaptureCohort.INDEX_UNDERLYING_MISMATCH,
            PremiumCaptureCohort.select(nf, "BNF", "2026-10-19", "09:20", null).status)
        // a caller key that contradicts the label is refused before the response is even read
        assertEquals(PremiumCaptureCohort.INDEX_UNDERLYING_MISMATCH,
            PremiumCaptureCohort.select(nf, "NF", "2026-10-19", "09:20",
                "NSE_INDEX|Nifty Bank").status)
        assertEquals(PremiumCaptureCohort.UNKNOWN_INDEX,
            PremiumCaptureCohort.select(nf, "SENSEX", "2026-10-19", "09:20", null).status)
        // the matched pairs still select
        assertEquals(PremiumCaptureCohort.SELECTED,
            PremiumCaptureCohort.select(nf, "NF", "2026-10-19", "09:20",
                "NSE_INDEX|Nifty 50").status)
        assertEquals(PremiumCaptureCohort.SELECTED,
            PremiumCaptureCohort.select(bnf, "BNF", "2026-10-19", "09:20",
                "NSE_INDEX|Nifty Bank").status)
    }

    /** FAILS BEFORE THE FIX: contradictory rows for one strike were resolved by first occurrence. */
    @Test
    fun mixedExpiryAndContradictoryRowsAreRefusedRatherThanResolvedByOrder() {
        val u = "NSE_INDEX|Nifty 50"
        val mixed = chain("2026-10-27", u, 24600.0, listOf(24550.0, 24600.0, 24650.0))
        mixed.getJSONArray("data").getJSONObject(1).put("expiry", "2026-11-24")
        assertEquals(PremiumCaptureCohort.MIXED_EXPIRY,
            PremiumCaptureCohort.select(mixed, "NF", "2026-10-19", "09:20", null).status)

        val dup = chain("2026-10-27", u, 24600.0, listOf(24550.0, 24600.0, 24650.0))
        val clash = JSONObject(dup.getJSONArray("data").getJSONObject(1).toString())
        clash.put("call_options", leg("CE|DIFFERENT", md("bid_price" to 1.0), null))
        dup.getJSONArray("data").put(clash)
        assertEquals(PremiumCaptureCohort.CONTRADICTORY_ROWS,
            PremiumCaptureCohort.select(dup, "NF", "2026-10-19", "09:20", null).status)

        // an identical repeated row is not a contradiction
        val same = chain("2026-10-27", u, 24600.0, listOf(24550.0, 24600.0, 24650.0))
        same.getJSONArray("data")
            .put(JSONObject(same.getJSONArray("data").getJSONObject(1).toString()))
        assertEquals(PremiumCaptureCohort.SELECTED,
            PremiumCaptureCohort.select(same, "NF", "2026-10-19", "09:20", null).status)
    }

    /**
     * THE P6 RULE, IN CODE. The previous cohort chose "three strikes per side" and derived the
     * option type from the strike's position against the spot, so it could not form a pair at all.
     */
    @Test
    fun aCohortIsAlwaysThreePairedStrikesAndSixUniqueContracts() {
        for (id in listOf("mon_0920_entry_NF", "mon_0920_entry_BNF")) {
            val snap = rawFixture(id)
            val index = if (id.endsWith("BNF")) "BNF" else "NF"
            val sel = PremiumCaptureCohort.select(snap.getJSONObject("raw"), index,
                snap.getString("session"), "09:20", null)
            assertEquals(id, PremiumCaptureCohort.SELECTED, sel.status)
            val pinned = PremiumCaptureCohort.pinnedList(sel.cohort)
            assertEquals(6, pinned.size)
            assertEquals("six UNIQUE instrument keys", 6,
                pinned.map { it.instrumentKey }.toSet().size)
            val strikes = pinned.map { it.strike }.toSet()
            assertEquals("three distinct strikes", 3, strikes.size)
            for (k in strikes) assertEquals("strike $k must carry BOTH legs",
                setOf("CE", "PE"), pinned.filter { it.strike == k }.map { it.optionType }.toSet())
            assertNull(PremiumCaptureCohort.validateLoaded(sel.cohort, index,
                snap.getString("session")))
        }
        // a chain with fewer than three fully paired strikes is refused, not partly filled
        val oneSided = chain("2026-10-27", "NSE_INDEX|Nifty 50", 24600.0,
            listOf(24550.0, 24600.0, 24650.0), peKey = { null })
        assertEquals(PremiumCaptureCohort.INCOMPLETE_PAIRED_STRIKES,
            PremiumCaptureCohort.select(oneSided, "NF", "2026-10-19", "09:20", null).status)
        // a strike whose CE and PE keys are identical is not a pair
        val sameKey = chain("2026-10-27", "NSE_INDEX|Nifty 50", 24600.0,
            listOf(24550.0, 24600.0, 24650.0), ceKey = { "X|$it" }, peKey = { "X|$it" })
        assertEquals(PremiumCaptureCohort.INCOMPLETE_PAIRED_STRIKES,
            PremiumCaptureCohort.select(sameKey, "NF", "2026-10-19", "09:20", null).status)
    }

    /**
     * FAILS BEFORE THE FIX: `identity_verified` was true whenever nothing CONTRADICTED the pinned
     * tuple, so a response with no underlying key, no expiry and no strike "verified".
     */
    @Test
    fun identityIsVerifiedOnlyByPositiveEvidenceNotByAbsenceOfContradiction() {
        val pinned = PremiumCaptureCohort.PinnedContract("NF", "K", "NSE_INDEX|Nifty 50",
            "2026-10-27", 24600.0, "CE")
        val full = JSONObject().put("underlying_key", "NSE_INDEX|Nifty 50")
            .put("expiry", "2026-10-27").put("strike_price", 24600.0)
        val good = PremiumObservationContract.identityEvidence(pinned, full, leg("K", null, null))
        assertTrue(good.getBoolean("identity_verified"))
        assertEquals(0, good.getJSONArray("identity_evidence_missing").length())

        // nothing contradicts, but nothing is evidenced either
        val bare = PremiumObservationContract.identityEvidence(pinned, JSONObject(),
            leg("K", null, null))
        assertFalse("absence of contradiction is not verification",
            bare.getBoolean("identity_verified"))
        val missing = bare.getJSONArray("identity_evidence_missing").toString()
        assertTrue(missing.contains("underlying_key"))
        assertTrue(missing.contains("expiry"))
        assertTrue(missing.contains("strike_price"))
        assertEquals(0, bare.getJSONArray("identity_disagreements").length())

        // a present contradiction is still reported
        val wrong = JSONObject(full.toString()).put("expiry", "2026-11-24")
        val bad = PremiumObservationContract.identityEvidence(pinned, wrong, leg("K", null, null))
        assertFalse(bad.getBoolean("identity_verified"))
        assertTrue(bad.getJSONArray("identity_disagreements").toString().contains("expiry"))
    }

    /** Missing or invalid QUOTES must never reject a correctly identified cohort. */
    @Test
    fun invalidQuotesDoNotRejectACorrectlyIdentifiedCohort() {
        val u = "NSE_INDEX|Nifty 50"
        val c = chain("2026-10-27", u, 24600.0, listOf(24550.0, 24600.0, 24650.0))
        for (i in 0 until c.getJSONArray("data").length()) {
            val row = c.getJSONArray("data").getJSONObject(i)
            row.getJSONObject("call_options").put("market_data",
                md("bid_price" to 0.0, "ask_price" to null, "ltp" to -1.0))
            row.getJSONObject("call_options").remove("option_greeks")
        }
        val sel = PremiumCaptureCohort.select(c, "NF", "2026-10-19", "09:20", null)
        assertEquals(PremiumCaptureCohort.SELECTED, sel.status)
        assertEquals(6, PremiumCaptureCohort.pinnedList(sel.cohort).size)
    }

    // ═════════════════ P6 — the acceptance export, produced by THESE classes ══════════════════

    /**
     * The acceptance artifact. Produced by the real `PremiumCaptureSession`/`PremiumCaptureStore`
     * from the committed raw vendor fixture — NOT by the Python mirror, whose cohort violated the
     * capture's own rule while every hash still verified.
     *
     * It is printed so the exact bytes CI produced can be lifted from the log and handed over, and
     * every property Codex asked to be asserted is asserted here rather than described.
     */
    @Test
    fun theAcceptanceExportIsProducedByTheseClassesAndObeysEveryRule() {
        val store = tmpStore()
        val snap = rawFixture("mon_0920_entry_NF")
        val session = snap.getString("session")
        val s = PremiumCaptureSession(store)
        val slots = listOf("mon_0920_entry_NF" to "09:20", "mon_0925_control_NF" to "09:25",
            "mon_0951_primary_exit_NF" to "09:50")
        var t = 1_000L
        for ((id, slot) in slots) {
            val r = s.observeSlot("NF", session, rawFixture(id).getJSONObject("raw"), null, slot,
                "NSE_INDEX|Nifty 50", t, t + 300L,
                claimedLot = 75, claimedLotSource = "CONTRACT_LOT_TABLE",
                lotTableRef = "contract_lot_table/NSE/2026-10-01",
                mapRef = "instrument_map/2026-10-01")
            assertEquals("$id/$slot", PremiumCaptureSession.OBSERVED, r.status)
            assertEquals(6, r.written)
            assertTrue(r.batchComplete)
            t += 300_000L
        }
        val table = JSONObject().put("ref", "contract_lot_table/NSE/2026-10-01")
            .put("NIFTY", 75)
        val ex = s.export("NF", session, "09:20", lotTableObject = table)

        // three paired strikes, six unique contracts
        val pinned = PremiumCaptureCohort.pinnedList(ex.getJSONObject("cohort"))
        assertEquals(6, pinned.size)
        assertEquals(6, pinned.map { it.instrumentKey }.toSet().size)
        assertEquals(3, pinned.map { it.strike }.toSet().size)
        for (k in pinned.map { it.strike }.toSet())
            assertEquals(setOf("CE", "PE"),
                pinned.filter { it.strike == k }.map { it.optionType }.toSet())

        // one identity-consistent outcome per key per slot, and envelope linkage throughout
        val acc = ex.getJSONObject("accounting")
        assertEquals(18, acc.getInt("records"))
        assertEquals(18, acc.getInt("hash_verified"))
        assertEquals(3, acc.getInt("slot_envelopes"))
        assertEquals(3, acc.getInt("envelope_hash_verified"))
        assertEquals(0, acc.getInt("conflicts_retained"))
        assertEquals(0, acc.getInt("corrupt_files"))
        assertEquals(0, acc.getInt("orphan_temp_files"))
        val rows = ex.getJSONObject("acquisitions").getJSONArray("rows")
        assertEquals(3, rows.length())
        assertEquals(3, ex.getJSONObject("acquisitions").getInt("complete_batches"))
        assertEquals(0, ex.getJSONObject("acquisitions")
            .getJSONArray("unresolved_record_groups").length())
        for (i in 0 until rows.length()) {
            val r = rows.getJSONObject(i)
            assertEquals("COMPLETE", r.getString("batch_state"))
            assertEquals(6, r.getInt("observations_linked_to_this_envelope"))
            assertEquals(6, r.getInt("unique_instrument_keys"))
            assertTrue(r.getBoolean("envelope_hash_verified"))
        }
        // per slot, exactly one acquisition and six records
        val slotRows = ex.getJSONObject("slot_manifest").getJSONArray("rows")
        var observed = 0
        for (i in 0 until slotRows.length()) {
            val r = slotRows.getJSONObject(i)
            if (r.getString("state") == "OBSERVED") {
                observed++
                assertEquals(1, r.getInt("acquisitions"))
                assertEquals(6, r.getInt("records"))
            }
        }
        assertEquals(3, observed)

        // offline reference resolution, claimed only where it is true
        assertEquals("RESOLVED_FROM_SUPPLIED_METADATA", ex.getJSONObject("references")
            .getJSONObject("contract_lot_table").getString("resolution"))
        assertEquals("UNRESOLVED_REFERENCE", ex.getJSONObject("references")
            .getJSONObject("instrument_map").getString("resolution"))
        assertTrue(ex.getJSONObject("claims").getBoolean("authoritative_lot_resolved"))
        assertFalse(ex.getJSONObject("claims").getBoolean("synchronised_exchange_quotes"))
        assertFalse(ex.getBoolean("device_evidence"))
        assertEquals("VALID", ex.getJSONObject("cohort_state").getString("state"))
        assertTrue(ex.getJSONObject("cohort_state").getBoolean("hash_verified"))

        // durable counters came from disk, not from this object
        val counters = ex.getJSONObject("durable_counters")
        assertEquals(3L, counters.getLong("batches_attempted"))
        assertEquals(3L, counters.getLong("batches_complete"))

        println("PREMIUM_ACCEPTANCE_EXPORT_BEGIN")
        println(ex.toString())
        println("PREMIUM_ACCEPTANCE_EXPORT_END")
        println("PREMIUM_ACCEPTANCE_EXPORT_BYTES=" +
            ex.toString().toByteArray(Charsets.UTF_8).size)
    }

    /** NF and BNF share a store and must not contaminate each other. */
    @Test
    fun niftyAndBankNiftyAreIsolatedInOneStore() {
        val store = tmpStore()
        val session = rawFixture("mon_0920_entry_NF").getString("session")
        val s = PremiumCaptureSession(store)
        for ((id, idx) in listOf("mon_0920_entry_NF" to "NF", "mon_0920_entry_BNF" to "BNF")) {
            val r = s.observeSlot(idx, session, rawFixture(id).getJSONObject("raw"), null, "09:20",
                null, 1_000L, 1_300L, claimedLot = 75, claimedLotSource = "CONTRACT_LOT_TABLE",
                lotTableRef = "contract_lot_table/NSE/2026-10-01")
            assertEquals(id, PremiumCaptureSession.OBSERVED, r.status)
            assertEquals(6, r.written)
        }
        assertEquals(6, store.outcomeCount("NF", session))
        assertEquals(6, store.outcomeCount("BNF", session))
        val nfKeys = store.readAll("NF", session).map { it.getString("instrument_key") }.toSet()
        val bnfKeys = store.readAll("BNF", session).map { it.getString("instrument_key") }.toSet()
        assertEquals("no contract may appear under both indices",
            emptySet<String>(), nfKeys.intersect(bnfKeys))
        store.readAll("NF", session).forEach { assertEquals("NF", it.getString("index")) }
        store.readAll("BNF", session).forEach { assertEquals("BNF", it.getString("index")) }
        assertEquals("NSE_INDEX|Nifty 50",
            JSONObject(store.readCohortText("NF", session)!!).getString("underlying_key"))
        assertEquals("NSE_INDEX|Nifty Bank",
            JSONObject(store.readCohortText("BNF", session)!!).getString("underlying_key"))
        // and each index's acquisitions resolve only against its own envelopes
        for (idx in listOf("NF", "BNF")) {
            val ex = PremiumCaptureSession(store).export(idx, session, "09:20")
            assertEquals(1, ex.getJSONObject("acquisitions").getInt("complete_batches"))
            assertEquals(0, ex.getJSONObject("acquisitions")
                .getJSONArray("unresolved_record_groups").length())
        }
    }

    // ═════════════════ the record itself: missingness is preserved ════════════════════════════

    @Test
    fun bothIvSourcesAreKeptSeparateAndNeitherIsNominatedAuthoritative() {
        val l = leg("K", md("iv" to 11.0), md("iv" to 14.2))
        val iv = PremiumObservationContract.ivEvidence(l)
        assertEquals(14.2, iv.getJSONObject("iv_greeks_raw").getDouble("raw"), 1e-9)
        assertEquals(11.0, iv.getJSONObject("iv_market_data_raw").getDouble("raw"), 1e-9)
        assertEquals("BOTH_PRESENT", iv.getString("iv_source"))
        assertEquals("UNKNOWN", iv.getString("iv_unit_status"))
        assertFalse(iv.getBoolean("iv_authoritative_resolved"))
        val cmp = iv.getJSONObject("iv_raw_field_comparison")
        assertEquals("RAW_FIELD_DIFFERENCE_UNITS_UNVERIFIED", cmp.getString("basis"))
        assertFalse(cmp.getBoolean("economically_meaningful"))
    }

    @Test
    fun anObservedZeroIvIsNotAbsentAndNotAValidSigma() {
        val iv = PremiumObservationContract.ivEvidence(leg("K", null, md("iv" to 0.0)))
        val g = iv.getJSONObject("iv_greeks_raw")
        assertEquals(PremiumObservationContract.OBSERVED_ZERO, g.getString("status"))
        assertEquals(0.0, g.getDouble("raw"), 0.0)
        assertEquals("GREEKS_PRESENT_ONLY", iv.getString("iv_source"))
        assertTrue(iv.isNull("iv_raw_field_comparison"))
    }

    @Test
    fun anAbsentIvIsNullAndNeverAFabricatedZero() {
        val iv = PremiumObservationContract.ivEvidence(leg("K", md("ltp" to 1.0), JSONObject()))
        val g = iv.getJSONObject("iv_greeks_raw")
        assertEquals(PremiumObservationContract.ABSENT, g.getString("status"))
        assertTrue(g.isNull("raw"))
        assertEquals("ABSENT", iv.getString("iv_source"))
    }

    @Test
    fun negativeMalformedAndNonFiniteIvAreDistinctAndJsonSafe() {
        val neg = PremiumObservationContract.ivEvidence(leg("K", null, md("iv" to -3.0)))
        assertEquals(PremiumObservationContract.OBSERVED_NEGATIVE,
            neg.getJSONObject("iv_greeks_raw").getString("status"))
        val bad = PremiumObservationContract.ivEvidence(leg("K", null, md("iv" to "n/a")))
        assertEquals(PremiumObservationContract.OBSERVED_MALFORMED,
            bad.getJSONObject("iv_greeks_raw").getString("status"))

        // A BARE NON-FINITE JSON NUMBER CANNOT REACH THIS RECORD, which CI established rather than
        // my reading: org.json's put() runs testValidity and refuses a non-finite double, and JSON
        // text cannot express one either. The reachable route is a numeric STRING.
        for (text in listOf("NaN", "Infinity", "-Infinity")) {
            val s = PremiumObservationContract.rawScalar(md("iv" to text), "iv")
            assertEquals("$text must be non-finite, not malformed and not valid",
                PremiumObservationContract.OBSERVED_NON_FINITE, s.status)
            assertTrue(s.raw == PremiumObservationContract.NON_FINITE_NAN ||
                s.raw == PremiumObservationContract.NON_FINITE_POS_INF ||
                s.raw == PremiumObservationContract.NON_FINITE_NEG_INF)
            val serialized = JSONObject().put("x", s.json()).toString()
            assertFalse("a bare NaN must never be serialized", serialized.contains("\"raw\":NaN"))
            assertFalse("a bare Infinity must never be serialized",
                serialized.contains("\"raw\":Infinity"))
            assertEquals(s.raw, JSONObject(serialized).getJSONObject("x").getString("raw"))
        }
        var refused = false
        try { JSONObject().put("iv", Double.NaN) } catch (e: Exception) { refused = true }
        assertTrue("org.json must refuse a non-finite double", refused)
    }

    @Test
    fun bookStatesIncludeCrossedOneSidedEmptyInvalidAndMissing() {
        fun status(m: JSONObject?) = PremiumObservationContract
            .bboEvidence(leg("K", m, null)).getString("book_status")
        assertEquals(PremiumObservationContract.BOOK_TWO_SIDED,
            status(md("bid_price" to 10.0, "ask_price" to 10.5)))
        assertEquals(PremiumObservationContract.BOOK_TWO_SIDED,
            status(md("bid_price" to 10.0, "ask_price" to 10.0)))
        assertEquals(PremiumObservationContract.BOOK_CROSSED,
            status(md("bid_price" to 10.5, "ask_price" to 10.0)))
        assertEquals(PremiumObservationContract.BOOK_ONE_SIDED, status(md("bid_price" to 10.0)))
        assertEquals(PremiumObservationContract.BOOK_EMPTY, status(md("ltp" to 5.0)))
        assertEquals(PremiumObservationContract.BOOK_INVALID,
            status(md("bid_price" to 0.0, "ask_price" to -1.0)))
        assertEquals(PremiumObservationContract.BOOK_MISSING, status(null))
        // no invented mid, and LTP is never substituted for a missing side
        val b = PremiumObservationContract.bboEvidence(leg("K", md("ltp" to 5.0), null))
        assertFalse(b.getBoolean("executable_mid_computed"))
        assertFalse(b.getBoolean("ltp_used_as_quote_fallback"))
        assertEquals("UNKNOWN", b.getString("quantity_unit_status"))
    }

    @Test
    fun aMissingContractStillProducesACompleteRecordWithAReason() {
        val store = tmpStore()
        val snap = rawFixture("mon_0920_entry_NF")
        val session = snap.getString("session")
        // pin from a good response, then hand in a failed acquisition
        observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        val r = PremiumCaptureSession(store).observeSlot("NF", session, null, null, "09:25",
            null, 2_000L, 2_300L, claimedLot = 75, claimedLotSource = "CONTRACT_LOT_TABLE",
            lotTableRef = "contract_lot_table/NSE/2026-10-01")
        assertEquals(PremiumCaptureSession.OBSERVED, r.status)
        assertEquals("absence is recorded for every pinned contract", 6, r.written)
        assertTrue(r.outcomes.values.all {
            it == PremiumObservationContract.OUTCOME_REQUEST_FAILED })
        val failed = store.readAll("NF", session).filter { it.optString("slot") == "09:25" }
        assertEquals(6, failed.size)
        failed.forEach {
            assertEquals(PremiumObservationContract.OUTCOME_REQUEST_FAILED,
                it.getString("outcome"))
            assertTrue("a non-observation carries no fabricated quote", it.isNull("iv"))
            assertTrue(it.isNull("bbo"))
            assertTrue(PremiumObservationContract.verifyHash(it))
        }
    }

    @Test
    fun theUnderlyingIsSynchronisedOnlyWhenTheResponseItselfCarriedIt() {
        val row = JSONObject().put("underlying_spot_price", 24560.0)
        val same = PremiumObservationContract.underlyingEvidence(row, null, null, null, null)
        assertEquals("SAME_ACQUISITION", same.getString("basis"))
        assertEquals("SAME_RESPONSE_PAYLOAD", same.getString("synchronisation_with_quote"))
        val sep = PremiumObservationContract.underlyingEvidence(JSONObject(), 24560.0,
            "ltp", "2026-10-19T09:20:00+05:30", 1234L)
        assertEquals("SEPARATE_OBSERVATION", sep.getString("basis"))
        assertEquals("UNKNOWN_NOT_SAME_ACQUISITION", sep.getString("synchronisation_with_quote"))
        assertFalse(sep.getBoolean("carried_by_chain_response"))
        val none = PremiumObservationContract.underlyingEvidence(JSONObject(), null, null, null, null)
        assertEquals("UNAVAILABLE", none.getString("basis"))
    }

    @Test
    fun theVendorClockIsPresenceNotVerifiedEventSemantics() {
        val c = PremiumObservationContract.clockEvidence(1000L, 1400L, "2026-10-19T09:20:00Z")
        assertEquals("VENDOR_TS_PRESENT", c.getString("vendor_ts_status"))
        assertEquals("UNVERIFIED", c.getString("vendor_event_semantics"))
        assertFalse(c.getBoolean("quote_event_clock_verified"))
        assertEquals(400L, c.getLong("acquisition_elapsed_ms"))
        val absent = PremiumObservationContract.clockEvidence(null, null, null)
        assertEquals("VENDOR_TS_ABSENT", absent.getString("vendor_ts_status"))
        assertTrue(absent.isNull("acquisition_elapsed_ms"))
    }

    // ═════════════════ serialization, integrity, and the measured sizes ═══════════════════════

    @Test
    fun canonicalSerializationMatchesTheCommittedPythonMirrorVectors() {
        val text = requireNotNull(
            javaClass.classLoader?.getResourceAsStream("premium_capture/canonical_vectors.json"))
            .bufferedReader().readText()
        val doc = JSONObject(text)
        assertEquals(PremiumObservationContract.CANONICAL_SERIALIZATION,
            doc.getString("serialization"))
        // drift guard: the mirror restates these, so a rename on either side must fail here
        val mc = doc.getJSONObject("mirrored_constants")
        assertEquals(PremiumObservationContract.RECORD_VERSION, mc.getString("record_version"))
        assertEquals(PremiumObservationContract.ENVELOPE_VERSION, mc.getString("envelope_version"))
        assertEquals(PremiumCaptureCohort.COHORT_RULE_VERSION, mc.getString("cohort_rule_version"))
        assertEquals(PremiumObservationContract.HASH_OMITTED_FIELD,
            mc.getString("hash_omitted_field"))
        assertEquals(PremiumObservationContract.NON_FINITE_NAN, mc.getString("non_finite_nan"))
        assertEquals(PremiumObservationContract.REF_HEX_LEN, mc.getInt("ref_hex_len"))

        val vectors = doc.getJSONArray("vectors")
        assertTrue("expected a non-trivial vector set", vectors.length() >= 19)
        var exact = 0; var roundTrip = 0
        for (i in 0 until vectors.length()) {
            val v = vectors.getJSONObject(i)
            val name = v.getString("name")
            val input = JSONObject(v.getJSONObject("input").toString())
            val canonical = PremiumObservationContract.canonicalJson(input)
            when (v.getString("mode")) {
                "exact" -> {
                    exact++
                    assertEquals("canonical form diverged for $name",
                        v.getString("canonical"), canonical)
                    assertEquals("sha256 diverged for $name",
                        v.getString("sha256"), PremiumObservationContract.sha256Hex(canonical))
                }
                // a DERIVED non-exact double is not asserted across runtimes: Double.toString only
                // guarantees shortest-round-trip from JDK 19 and CI runs 17. What matters is
                // asserted instead — stability under a text round trip, and value preservation.
                "round_trip" -> {
                    roundTrip++
                    assertEquals("canonical form is not stable for $name", canonical,
                        PremiumObservationContract.canonicalJson(JSONObject(input.toString())))
                    val reparsed = JSONObject(input.toString())
                    for (k in input.keys()) assertEquals("value changed for $name.$k",
                        input.getDouble(k), reparsed.getDouble(k), 0.0)
                }
                else -> throw AssertionError("unknown vector mode in $name")
            }
        }
        assertTrue(exact >= 16)
        assertTrue(roundTrip >= 3)
    }

    @Test
    fun aTamperedRecordFailsVerificationIncludingItsHashBasis() {
        val store = tmpStore()
        val (_, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        val rec = store.readAll("NF", snap.getString("session"))
            .first { it.getString("outcome") == PremiumObservationContract.OUTCOME_OBSERVED }
        assertTrue(PremiumObservationContract.verifyHash(rec))

        val valueTampered = JSONObject(rec.toString())
        valueTampered.getJSONObject("iv").getJSONObject("iv_greeks_raw").put("raw", 9.99)
        assertFalse("a changed observed value must not verify",
            PremiumObservationContract.verifyHash(valueTampered))

        val basisTampered = JSONObject(rec.toString())
        basisTampered.getJSONObject("hash_basis").put("authenticates_vendor_truth", true)
        assertFalse("hash_basis is inside the hashed content, so rewriting it must not verify",
            PremiumObservationContract.verifyHash(basisTampered))

        val linkTampered = JSONObject(rec.toString())
        linkTampered.put("acquisition_id", "0".repeat(PremiumObservationContract.REF_HEX_LEN))
        assertFalse("the acquisition binding is covered by the hash",
            PremiumObservationContract.verifyHash(linkTampered))

        val digestStripped = JSONObject(rec.toString())
        digestStripped.remove(PremiumObservationContract.HASH_OMITTED_FIELD)
        assertFalse("a record with no digest is unverified, never verified-by-default",
            PremiumObservationContract.verifyHash(digestStripped))
    }

    /**
     * THE INVARIANT WHOSE VIOLATION WAS A REAL BUG, so it cannot come back. `stampHash` hashes a
     * live object graph; `verifyHash` hashes a text round trip. `org.json:json:20240303` parses a
     * decimal as BigDecimal, so `61.0` rendered "61.0" live while `toString()` trimmed it to `61`,
     * which reparsed as Integer and rendered "61" — a stamped record failed its own verification.
     */
    @Test
    fun theCanonicalFormSurvivesAJsonTextRoundTrip() {
        val store = tmpStore()
        val (_, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        val session = snap.getString("session")
        val all = store.readAll("NF", session) + store.readEnvelopes("NF", session) +
            listOf(JSONObject(store.readCohortText("NF", session)!!))
        assertEquals(8, all.size)
        for (rec in all) {
            val live = PremiumObservationContract.canonicalJson(rec)
            val roundTripped = PremiumObservationContract.canonicalJson(JSONObject(rec.toString()))
            assertEquals("canonical form changed across a text round trip", live, roundTripped)
            assertTrue("a stored object must verify", PremiumObservationContract.verifyHash(rec))
            assertTrue("and must still verify after a round trip",
                PremiumObservationContract.verifyHash(JSONObject(rec.toString())))
        }
        val numbers = JSONObject("{\"a\":61.0,\"b\":10.50,\"c\":600,\"d\":0.0,\"e\":-0.0," +
            "\"f\":0.1412,\"g\":1e-5,\"h\":24650}")
        assertEquals("{\"a\":61,\"b\":10.5,\"c\":600,\"d\":0,\"e\":0,\"f\":0.1412," +
            "\"g\":0.00001,\"h\":24650}", PremiumObservationContract.canonicalJson(numbers))
    }

    /**
     * Sizes measured by THIS code against the real vendor fixture — the only authority for them.
     * The flat shape is built here too, so the reason for the envelope split is reproducible from
     * the producer rather than quoted from a script.
     */
    @Test
    fun measuredRecordSizesAgainstTheCommittedRawVendorFixture() {
        val store = tmpStore()
        val (r, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        val session = snap.getString("session")
        assertEquals(6, r.written)
        val recs = store.readAll("NF", session)
        val envs = store.readEnvelopes("NF", session)
        assertEquals(6, recs.size); assertEquals(1, envs.size)

        val recSizes = recs.map { it.toString().toByteArray(Charsets.UTF_8).size }
        val envSize = envs.first().toString().toByteArray(Charsets.UTF_8).size
        (recSizes + envSize).forEach {
            assertTrue("$it B exceeds the unchanged ${PremiumCaptureStore.MAX_BYTES_PER_RECORD} B cap",
                it <= PremiumCaptureStore.MAX_BYTES_PER_RECORD)
        }
        val perSlot = recSizes.sum() + envSize
        val budget = PremiumCaptureStore.MAX_BYTES_PER_INDEX_SESSION -
            PremiumCaptureStore.SUMMARY_RESERVE_BYTES
        assertTrue("projected 78 slots = ${perSlot * 78L} B exceeds $budget B",
            perSlot * 78L < budget)
        assertEquals(PremiumCaptureStore.MAX_OUTCOMES_PER_INDEX_SESSION,
            PremiumCaptureCohort.CONTRACTS_PER_COHORT * 78)

        // the flat shape the split replaced: the per-acquisition facts inlined into every record
        val env = envs.first()
        val flat = recs.map { rec ->
            JSONObject(rec.toString())
                .put("clocks", env.getJSONObject("clocks"))
                .put("underlying", env.getJSONObject("underlying"))
                .put("lot", env.getJSONObject("lot"))
                .put("cohort_id", env.getString("cohort_id"))
                .toString().toByteArray(Charsets.UTF_8).size
        }
        val flatPerSlot = flat.sum()
        println("MEASURED(desktop JVM, real vendor fixture): records=$recSizes envelope=$envSize " +
            "perSlot=$perSlot projected78=${perSlot * 78L} budget=$budget " +
            "flatRecords=$flat flatPerSlot=$flatPerSlot projected78Flat=${flatPerSlot * 78L} " +
            "perRecordCap=${PremiumCaptureStore.MAX_BYTES_PER_RECORD}")
        assertTrue("the split must actually save bytes", flatPerSlot > perSlot)
    }

    // ═════════════════ nothing is wired, nothing reaches the trading path ═════════════════════

    @Test
    fun theCaptureSourcesTouchNoNetworkSchedulerOrTradingSurface() {
        val files = listOf("PremiumObservationContract.kt", "PremiumCaptureCohort.kt",
            "PremiumCaptureStore.kt", "PremiumCaptureSession.kt")
        val banned = listOf("OkHttp", "HttpURLConnection", "Request.Builder", "AlarmManager",
            "setExact", "WorkManager", "JobScheduler", "Timer(",
            "SupabaseClient", "NotificationHelper", "OrderExecutionService",
            "SharedPreferences", "getSharedPreferences", "startService", "sendBroadcast")
        for (n in files) {
            // comments and string literals stripped: the assertion is about references, not prose
            val src = stripCommentsAndStrings(
                File("src/main/java/com/marketradar/app/$n").readText())
            for (b in banned) assertFalse("$n must not reference $b", src.contains(b))
        }
    }

    @Test
    fun captureIsNotWiredIntoTheBrainSnapshotOrServerIngestion() {
        val dir = File("src/main/java/com/marketradar/app")
        val referrers = (dir.listFiles() ?: emptyArray()).filter { f ->
            f.isFile && f.name.endsWith(".kt") && !f.name.startsWith("Premium") &&
                stripCommentsAndStrings(f.readText()).contains("PremiumCapture")
        }.map { it.name }
        assertEquals("no shipped source may reference the capture yet",
            emptyList<String>(), referrers)
    }

    @Test
    fun theLiveIvHelperIsNotReusedAndIsNotChanged() {
        // the helper still collapses sources and fabricates 0.0, which is why this record does not
        // use it. Pinning its behaviour here proves this delivery did not alter trading code.
        assertEquals(0.0, OptionChainBrainFormatter.optionIv(JSONObject(), JSONObject()), 0.0)
        assertEquals(14.2, OptionChainBrainFormatter.optionIv(
            leg("K", null, md("iv" to 14.2)), null), 1e-9)
        assertNull(OptionChainBrainFormatter.optionQuote(md("bid_price" to 0.0), "bid_price"))
        val src = stripCommentsAndStrings(
            File("src/main/java/com/marketradar/app/PremiumObservationContract.kt").readText())
        assertFalse("the evidence record must not CALL optionIv", src.contains("optionIv("))
        assertFalse("the evidence record must not CALL optionQuote", src.contains("optionQuote("))
    }

    @Test
    fun aCaptureFailureCannotPropagateOutOfTheStore() {
        val store = PremiumCaptureStore(File("/proc/nonexistent-premium-capture"))
        val r = store.put("NF", "S", "k",
            PremiumObservationContract.stampHash(JSONObject().put("v", 1)))
        assertFalse(r.accepted)
        assertEquals(PremiumCaptureStore.REFUSED_IO, r.status)
        assertEquals(0, store.readAll("NF", "S").size)
        assertEquals(0, store.conflictCount("NF", "S"))
        assertEquals(0, store.readRecords("NF", "S").corrupt.size)
        // and the session degrades to a status rather than throwing
        val sr = PremiumCaptureSession(store).observeSlot("NF", "2026-10-19", null, null, "09:20",
            null, 1L, 2L)
        assertFalse(sr.status == PremiumCaptureSession.OBSERVED)
    }
}
