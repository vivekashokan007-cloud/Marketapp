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
import java.util.TimeZone
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

    /**
     * Fill a file to `bytes` WITHOUT materialising it in memory.
     *
     * These fixtures used to allocate a ByteArray of the whole global cap, which was fine at the
     * first-delivery 4 MiB but allocates a gigabyte once the pilot budget is 1 GiB. The store
     * measures with `File.length()`, so a sparse file of the right length fills the store
     * exactly as before and every assertion is unchanged.
     */
    private fun fillFile(f: File, bytes: Long) {
        f.parentFile?.mkdirs()
        java.io.RandomAccessFile(f, "rw").use { it.setLength(bytes) }
    }

    private fun tmpStore(): PremiumCaptureStore =
        PremiumCaptureStore(Files.createTempDirectory("premcap").toFile())

    /**
     * Strip COMMENTS ONLY before a source scan, so an assertion about what the code references is
     * not tripped by what the prose mentions.
     *
     * Hand-rolled and linear, for two reasons CI established. The previous regex version — a
     * quoted-string pattern with an escape alternation, plus a lazy block-comment pattern —
     * overflowed the stack inside `java.util.regex` on the larger files in this package
     * (`StackOverflowError at Pattern.java`), because that engine recurses on an alternation
     * inside a repetition. It also stripped string literals, which is a FALSE-NEGATIVE risk for a
     * "must not reference" assertion: a reference inside a string template would have been erased
     * rather than caught. String literals are therefore copied through verbatim, and only the
     * quote character is tracked, so that a line-comment marker inside a string is not mistaken
     * for a comment.
     *
     * The regex patterns are described rather than quoted here on purpose: writing a block-comment
     * terminator inside a KDoc block ENDS THE COMMENT at that point, which is what the earlier
     * draft of this very comment did.
     */
    private fun stripComments(src: String): String {
        val out = StringBuilder(src.length)
        var i = 0
        val n = src.length
        while (i < n) {
            val c = src[i]
            if (c == '/' && i + 1 < n && src[i + 1] == '/') {
                while (i < n && src[i] != '\n') i++
            } else if (c == '/' && i + 1 < n && src[i + 1] == '*') {
                i += 2
                while (i + 1 < n && !(src[i] == '*' && src[i + 1] == '/')) i++
                i = minOf(i + 2, n)
            } else if (c == '"' && i + 2 < n && src[i + 1] == '"' && src[i + 2] == '"') {
                val start = i
                i += 3
                while (i + 2 < n && !(src[i] == '"' && src[i + 1] == '"' && src[i + 2] == '"')) i++
                i = minOf(i + 3, n)
                out.append(src, start, i)          // raw string kept, not erased
            } else if (c == '"') {
                val start = i
                i++
                while (i < n && src[i] != '"') { if (src[i] == '\\') i++; i++ }
                i = minOf(i + 1, n)
                out.append(src, start, i)          // string literal kept, not erased
            } else {
                out.append(c); i++
            }
        }
        return out.toString()
    }

    /** The provenance every test export must state: real quotes, invented timing/metadata. */
    private fun replayContext() = PremiumCaptureSession.EvidenceContext.transformedFixtureReplay(
        "contracts/x1_upstox_raw_chains.json",
        "generated receipt clocks, manually assigned scheduler slots, constructed lot table")

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
        fillFile(filler, PremiumCaptureStore.MAX_BYTES_GLOBAL)
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

        val ex = PremiumCaptureSession(store).export("NF", session, "09:20", replayContext())
        val rows = ex.getJSONObject("acquisitions").getJSONArray("rows")
        assertEquals(2, rows.length())
        for (i in 0 until rows.length()) {
            val r = rows.getJSONObject(i)
            assertEquals("each acquisition must own exactly its six observations",
                6, r.getInt("observations"))
            assertEquals("and every one must link to THIS envelope",
                6, r.getInt("observations_linked_to_this_envelope"))
            assertEquals(6, r.getInt("unique_instrument_keys"))
            assertEquals("COMPLETE_STRUCTURAL", r.getString("structural_state"))
            assertEquals("VERIFIED", r.getString("verified_state"))
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
     * THE R2-1 CASE, and it FAILS BEFORE THAT FIX — this is the hole my R1 test missed because it
     * varied the receipt clocks.
     *
     * Same slot, same spot, same lot, **null clocks**, CHANGED QUOTES. The old acquisition id
     * hashed only session/index/slot/cohort/clocks/underlying/lot, so both responses produced the
     * same id and a byte-identical envelope: the envelope write was a duplicate no-op, B wrote
     * under A's identity, A's surviving records became conflicts and A's missing keys took B's
     * quotes — a primary group combining two responses, reportable as complete.
     *
     * Interruption after each observation write is exercised, so recovery is covered too.
     */
    @Test
    fun aQuoteOnlyChangeWithNullClocksCannotResumeTheEarlierAcquisition() {
        val snap = rawFixture("mon_0920_entry_NF")
        val session = snap.getString("session")
        val rawA = snap.getJSONObject("raw")
        // same identity and same spot, DIFFERENT quotes: edit only market_data on the legs
        val rawB = JSONObject(rawA.toString())
        for (i in 0 until rawB.getJSONArray("data").length()) {
            val row = rawB.getJSONArray("data").getJSONObject(i)
            for (side in listOf("call_options", "put_options")) {
                val mdB = row.optJSONObject(side)?.optJSONObject("market_data") ?: continue
                mdB.put("bid_price", mdB.optDouble("bid_price", 1.0) + 7.25)
                mdB.put("ask_price", mdB.optDouble("ask_price", 1.0) + 7.25)
            }
        }
        assertEquals("the spot must be unchanged for this case to mean anything",
            rawA.getJSONArray("data").getJSONObject(0).getDouble("underlying_spot_price"),
            rawB.getJSONArray("data").getJSONObject(0).getDouble("underlying_spot_price"), 0.0)

        // interrupt A after each possible number of writes, then let B retry the same slot
        for (survivors in 0..5) {
            val store = tmpStore()
            val s = PremiumCaptureSession(store)
            val a = s.observeSlot("NF", session, rawA, null, "09:20", null, null, null,
                claimedLot = 75, claimedLotSource = "CONTRACT_LOT_TABLE",
                lotTableRef = "contract_lot_table/NSE/2026-10-01")
            assertEquals(PremiumCaptureSession.OBSERVED, a.status)
            assertEquals(6, a.written)
            // simulate the interruption: only `survivors` of A's six records persisted
            val dir = File(store.root, "$session/NF/records")
            dir.listFiles()!!.filter { it.name.endsWith(".json") }.sortedBy { it.name }
                .drop(survivors).forEach { assertTrue(it.delete()) }
            assertEquals(survivors, store.outcomeCount("NF", session))

            val b = s.observeSlot("NF", session, rawB, null, "09:20", null, null, null,
                claimedLot = 75, claimedLotSource = "CONTRACT_LOT_TABLE",
                lotTableRef = "contract_lot_table/NSE/2026-10-01")
            assertEquals(PremiumCaptureSession.OBSERVED, b.status)
            assertNotEquals("survivors=$survivors: a quote-only change must be a NEW acquisition",
                a.acquisitionId, b.acquisitionId)
            // B's six records are its own; A's survivors keep A's identity
            assertEquals(survivors + 6, store.outcomeCount("NF", session))
            assertEquals("no record may be a conflict: the identities are distinct",
                0, store.conflictCount("NF", session))
            val byAcq = store.readAll("NF", session).groupBy { it.getString("acquisition_id") }
            assertEquals(6, byAcq[b.acquisitionId]!!.size)
            if (survivors > 0) assertEquals(survivors, byAcq[a.acquisitionId]!!.size)
            // every record resolves to the envelope of its OWN acquisition
            val envRef = store.readEnvelopes("NF", session).associate {
                it.getString("acquisition_id") to PremiumObservationContract.ref(
                    PremiumObservationContract.contentHash(it)) }
            store.readAll("NF", session).forEach {
                assertEquals("survivors=$survivors: record bound to the wrong envelope",
                    envRef[it.getString("acquisition_id")], it.getString("envelope_ref")) }
            // and A's partial group is never reported as usable evidence
            val ex = s.export("NF", session, "09:20", replayContext())
            val rows = ex.getJSONObject("acquisitions").getJSONArray("rows")
            for (i in 0 until rows.length()) {
                val r = rows.getJSONObject(i)
                if (r.getString("acquisition_id") == a.acquisitionId && survivors < 6) {
                    assertNotEquals("COMPLETE_STRUCTURAL", r.getString("structural_state"))
                    assertEquals("UNVERIFIED", r.getString("verified_state"))
                }
            }
            assertEquals(1, ex.getJSONObject("acquisitions").getInt("verified_batches"))
        }
    }

    /** Byte-identical evidence re-presented is still idempotent: a retry is not a new batch. */
    @Test
    fun anIdenticalResponseRepresentedIsIdempotent() {
        val store = tmpStore()
        val snap = rawFixture("mon_0920_entry_NF")
        val session = snap.getString("session")
        val s = PremiumCaptureSession(store)
        fun once() = s.observeSlot("NF", session, snap.getJSONObject("raw"), null, "09:20",
            null, null, null, claimedLot = 75, claimedLotSource = "CONTRACT_LOT_TABLE",
            lotTableRef = "contract_lot_table/NSE/2026-10-01")
        val a = once(); val b = once()
        assertEquals(a.acquisitionId, b.acquisitionId)
        assertEquals(6, a.written)
        assertEquals("the second pass writes nothing new", 0, b.written)
        assertEquals(6, b.duplicates)
        assertEquals(1, store.envelopeCount("NF", session))
        assertEquals(6, store.outcomeCount("NF", session))
        assertEquals(0, store.conflictCount("NF", session))
    }

    /** A failed request carries an explicit attempt policy rather than an invented distinction. */
    @Test
    fun aFailedRequestStatesWhetherItsAttemptsAreDistinguishable() {
        val store = tmpStore()
        val snap = rawFixture("mon_0920_entry_NF")
        val session = snap.getString("session")
        val s = PremiumCaptureSession(store)
        observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        // no clocks at all: two failed attempts in one slot are genuinely indistinguishable
        val f1 = s.observeSlot("NF", session, null, null, "09:25", null, null, null)
        val f2 = s.observeSlot("NF", session, null, null, "09:25", null, null, null)
        assertEquals(f1.acquisitionId, f2.acquisitionId)
        assertEquals(6, f1.written); assertEquals(0, f2.written)
        val env = store.readEnvelopes("NF", session).first { it.getString("slot") == "09:25" }
        assertEquals(PremiumObservationContract.REQUEST_FAILED, env.getString("request_outcome"))
        assertFalse("with no clock, attempts are not distinguishable",
            env.getBoolean("attempt_distinguishable"))
        // with a clock, two failed attempts ARE distinct and both are recorded
        val g1 = s.observeSlot("NF", session, null, null, "09:30", null, 1L, 2L)
        val g2 = s.observeSlot("NF", session, null, null, "09:30", null, 50L, 60L)
        assertNotEquals(g1.acquisitionId, g2.acquisitionId)
        val envG = store.readEnvelopes("NF", session).first { it.getString("slot") == "09:30" }
        assertTrue(envG.getBoolean("attempt_distinguishable"))
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
        val ex = PremiumCaptureSession(store).export("NF", session, "09:20", replayContext())
        val row = ex.getJSONObject("acquisitions").getJSONArray("rows").getJSONObject(0)
        assertEquals("PARTIAL", row.getString("structural_state"))
        assertEquals("UNVERIFIED", row.getString("verified_state"))
        assertEquals(4, row.getInt("observations"))
        assertEquals(6, row.getInt("observations_expected"))
        assertEquals(0, ex.getJSONObject("acquisitions").getInt("structurally_complete_batches"))
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
        val ex = PremiumCaptureSession(store).export("NF", session, "09:20", replayContext())
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
        val ex = PremiumCaptureSession(store).export("NF", session, "09:20", replayContext())
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
        val ex = PremiumCaptureSession(store).export("NF", session, "09:20", replayContext())
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
        val ex = PremiumCaptureSession(store).export("NF", session, "09:20", replayContext())
        val conflicts = ex.getJSONObject("conflicts")
        assertEquals(1, conflicts.getInt("retained"))
        val ev = conflicts.getJSONArray("evidence").getJSONObject(0)
        assertEquals(keyHash, ev.getString("conflict_of_key_sha256"))
        assertEquals("FIRST_WRITE_WINS_CONFLICT_ARCHIVED",
            ev.getString("retained_evidence_policy"))
        assertEquals(JSONObject().put("v", 2).toString(), ev.getString("payload"))
    }

    /**
     * THE R2-3 CASE, and it FAILS BEFORE THAT FIX. The old COMPLETE predicate counted six parsed
     * records with matching envelope references; it checked no record hash, no envelope hash, not
     * the expected pinned-key set and no join dimension. So a parseable record edited on disk kept
     * its envelope reference and still counted as complete while its own hash failed.
     */
    @Test
    fun aTamperedButParseableRecordIsStructurallyCompleteYetNeverVerified() {
        val store = tmpStore()
        val (_, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        val session = snap.getString("session")
        val dir = File(store.root, "$session/NF/records")
        val victim = dir.listFiles()!!.first { it.name.endsWith(".json") }
        val rec = JSONObject(victim.readText())
        // a value change that leaves the file parseable and the envelope reference intact
        rec.getJSONObject("identity").getJSONObject("observed_strike_raw").put("raw", 1.0)
        victim.writeText(rec.toString())
        assertFalse("the premise: the record no longer verifies",
            PremiumObservationContract.verifyHash(JSONObject(victim.readText())))

        val ex = PremiumCaptureSession(store).export("NF", session, "09:20", replayContext())
        val row = ex.getJSONObject("acquisitions").getJSONArray("rows").getJSONObject(0)
        assertEquals("physical counts are retained",
            "COMPLETE_STRUCTURAL", row.getString("structural_state"))
        assertEquals("but it is not usable evidence", "UNVERIFIED", row.getString("verified_state"))
        assertEquals(1, row.getInt("records_with_invalid_hash"))
        assertTrue(row.getJSONArray("integrity_reasons").toString()
            .contains("record_hash_invalid"))
        assertEquals(1, ex.getJSONObject("acquisitions").getInt("structurally_complete_batches"))
        assertEquals(0, ex.getJSONObject("acquisitions").getInt("verified_batches"))
        assertEquals(0, ex.getJSONObject("acquisitions").getInt("usable_evidence_acquisitions"))
        // the failing record is IDENTIFIED, not silently dropped
        val ids = ex.getJSONObject("acquisitions").getJSONArray("hash_failure_identifiers")
        assertEquals(1, ids.length())
        assertEquals(rec.getString("instrument_key"),
            ids.getJSONObject(0).getString("instrument_key"))
        assertEquals(6, ex.getJSONObject("accounting").getInt("records"))
        assertEquals("the record is still exported", 6, ex.getJSONArray("records").length())
    }

    /** Envelope tampering, duplicate/missing keys and a foreign cohort each block verification. */
    @Test
    fun envelopeTamperingKeySetFaultsAndForeignBindingsAllBlockVerification() {
        val session = rawFixture("mon_0920_entry_NF").getString("session")

        // (a) envelope tampered
        run {
            val store = tmpStore()
            observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
            val f = File(store.root, "$session/NF/envelopes").listFiles()!!
                .first { it.name.endsWith(".json") }
            val env = JSONObject(f.readText())
            env.getJSONObject("underlying").getJSONObject("spot_raw").put("raw", 1.0)
            f.writeText(env.toString())
            val row = PremiumCaptureSession(store)
                .export("NF", session, "09:20", replayContext())
                .getJSONObject("acquisitions").getJSONArray("rows").getJSONObject(0)
            assertFalse(row.getBoolean("envelope_hash_verified"))
            assertEquals("UNVERIFIED", row.getString("verified_state"))
            assertTrue(row.getJSONArray("integrity_reasons").toString()
                .contains("envelope_hash_invalid"))
        }
        // (b) a missing pinned key: six records are expected, five present
        run {
            val store = tmpStore()
            observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
            val dir = File(store.root, "$session/NF/records")
            assertTrue(dir.listFiles()!!.first { it.name.endsWith(".json") }.delete())
            val row = PremiumCaptureSession(store)
                .export("NF", session, "09:20", replayContext())
                .getJSONObject("acquisitions").getJSONArray("rows").getJSONObject(0)
            assertEquals("PARTIAL", row.getString("structural_state"))
            assertEquals("UNVERIFIED", row.getString("verified_state"))
            assertTrue(row.getJSONArray("integrity_reasons").toString()
                .contains("pinned_key_set_mismatch"))
        }
        // (c) a foreign cohort binding: the records name a cohort that is no longer pinned
        run {
            val store = tmpStore()
            observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
            val cf = File(store.root, "$session/NF/cohort.json")
            val foreign = PremiumCaptureCohort.select(
                rawFixture("mon_0925_control_NF").getJSONObject("raw"), "NF", session, "09:25",
                null).cohort!!
            cf.writeText(foreign.toString())
            val ex = PremiumCaptureSession(store).export("NF", session, "09:20", replayContext())
            val row = ex.getJSONObject("acquisitions").getJSONArray("rows").getJSONObject(0)
            assertEquals("UNVERIFIED", row.getString("verified_state"))
            val why = row.getJSONArray("integrity_reasons").toString()
            assertTrue("expected a cohort-ref mismatch, got $why",
                why.contains("cohort_ref_mismatch"))
        }
    }

    // ───── Commit 1: the verifier recomputes the bindings instead of trusting them ─────

    /** Rewrite a stored file and RE-STAMP it, so its own payload hash is valid again. */
    private fun rewriteStamped(f: File, mutate: (JSONObject) -> Unit) {
        val o = JSONObject(f.readText())
        o.remove(PremiumObservationContract.HASH_OMITTED_FIELD)
        o.remove("hash_basis")
        mutate(o)
        f.writeText(PremiumObservationContract.stampHash(o).toString())
        assertTrue("the premise: the rewritten object must itself verify",
            PremiumObservationContract.verifyHash(JSONObject(f.readText())))
    }

    private fun recordFiles(store: PremiumCaptureStore, session: String, index: String = "NF") =
        File(store.root, "$session/$index/records").listFiles()!!
            .filter { it.name.endsWith(".json") }.sortedBy { it.name }

    private fun envelopeFile(store: PremiumCaptureStore, session: String, index: String = "NF") =
        File(store.root, "$session/$index/envelopes").listFiles()!!
            .first { it.name.endsWith(".json") }

    private fun firstAcqRow(store: PremiumCaptureStore, session: String, index: String = "NF") =
        PremiumCaptureSession(store).export(index, session, "09:20", replayContext())
            .getJSONObject("acquisitions").getJSONArray("rows").getJSONObject(0)

    /**
     * FAILS BEFORE THIS FIX, and Codex demonstrated it by replaying the predicate against my own
     * previous acceptance artifact: all three of its groups satisfied the old verification
     * predicate although they carry observation v3 / envelope v2 and **no `batch_evidence_ref`
     * at all**. The version bump labelled the rows; the verifier never read it. So my claim that
     * the bump let the verifier distinguish the identity contracts was not supported by the
     * exporter. Every object here is individually hash-valid.
     */
    @Test
    fun legacyVersionedEvidenceIsRetainedAndIdentifiedButNeverVerified() {
        val store = tmpStore()
        val (_, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        val session = snap.getString("session")
        // make the group look exactly like the pre-fix format
        rewriteStamped(envelopeFile(store, session)) {
            it.put("record_version", "mr.premium_slot_envelope.v2")
            it.remove("batch_evidence_ref")
            it.remove("request_outcome")
            it.remove("attempt_distinguishable")
        }
        recordFiles(store, session).forEach { f ->
            rewriteStamped(f) { it.put("record_version", "mr.premium_observation.v3") }
        }
        val ex = PremiumCaptureSession(store).export("NF", session, "09:20", replayContext())
        val row = ex.getJSONObject("acquisitions").getJSONArray("rows").getJSONObject(0)
        val why = row.getJSONArray("integrity_reasons").toString()
        assertEquals("UNVERIFIED", row.getString("verified_state"))
        assertTrue("expected the envelope version to be rejected, got $why",
            why.contains("unsupported_envelope_version:mr.premium_slot_envelope.v2"))
        assertTrue("expected the record version to be rejected, got $why",
            why.contains("unsupported_record_version:mr.premium_observation.v3"))
        assertTrue("a v2 envelope carries no batch binding, got $why",
            why.contains("envelope_carries_no_batch_evidence_ref"))
        assertEquals(0, ex.getJSONObject("acquisitions").getInt("verified_batches"))
        // retained, not rewritten and not discarded
        assertEquals(6, ex.getJSONArray("records").length())
        assertEquals("mr.premium_observation.v3",
            ex.getJSONArray("records").getJSONObject(0).getString("record_version"))
        assertEquals(6, ex.getJSONObject("accounting").getInt("records"))
        assertEquals("the files still verify individually, which is the point",
            6, ex.getJSONObject("accounting").getInt("hash_verified"))
        // and the contract the export is verifying against is published
        val vc = ex.getJSONObject("acquisitions").getJSONObject("verified_contract")
        assertEquals("INTEGRITY_AND_LINKAGE_UNDER_THIS_CONTRACT_ONLY",
            vc.getString("verified_means"))
        assertTrue(vc.getJSONArray("supported_record_versions").toString()
            .contains(PremiumObservationContract.RECORD_VERSION))
    }

    /**
     * A record whose evidence was edited and RE-STAMPED is individually hash-valid and keeps every
     * join, so only the recomputed batch digest can catch it. Before this fix nothing did.
     */
    @Test
    fun aReStampedRecordWhoseEvidenceChangedBreaksTheRecomputedBatchDigest() {
        val store = tmpStore()
        val (_, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        val session = snap.getString("session")
        val victim = recordFiles(store, session).first {
            JSONObject(it.readText()).optString("outcome") ==
                PremiumObservationContract.OUTCOME_OBSERVED
        }
        rewriteStamped(victim) {
            it.getJSONObject("iv").getJSONObject("iv_greeks_raw").put("raw", 99.0)
        }
        val row = firstAcqRow(store, session)
        val why = row.getJSONArray("integrity_reasons").toString()
        assertEquals("UNVERIFIED", row.getString("verified_state"))
        assertTrue("expected the recomputed batch digest to disagree, got $why",
            why.contains("batch_evidence_ref_mismatch"))
        // the stale-hash check cannot be what caught it
        assertEquals(0, row.getInt("records_with_invalid_hash"))
        assertEquals("COMPLETE_STRUCTURAL", row.getString("structural_state"))
    }

    /** A partial group cannot satisfy a COMPLETE-batch binding, and says so explicitly. */
    @Test
    fun aPartialGroupCannotBindTheCompleteBatchDigest() {
        val store = tmpStore()
        val (_, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        val session = snap.getString("session")
        assertTrue(recordFiles(store, session).first().delete())
        val why = firstAcqRow(store, session).getJSONArray("integrity_reasons").toString()
        assertTrue("expected an explicit cannot-bind reason, got $why",
            why.contains("batch_incomplete_cannot_bind"))
    }

    /**
     * An acquisition id that is not derived from its own declared basis. The envelope and all six
     * records are re-stamped and fully consistent with each other, so only recomputation catches
     * it.
     */
    @Test
    fun anAcquisitionIdNotDerivedFromItsOwnBasisIsRejected() {
        val store = tmpStore()
        val (_, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        val session = snap.getString("session")
        val forged = "f".repeat(PremiumObservationContract.REF_HEX_LEN)
        val envF = envelopeFile(store, session)
        rewriteStamped(envF) { it.put("acquisition_id", forged) }
        val newEnvRef = PremiumObservationContract.ref(
            PremiumObservationContract.contentHash(JSONObject(envF.readText())))
        recordFiles(store, session).forEach { f ->
            rewriteStamped(f) {
                it.put("acquisition_id", forged); it.put("envelope_ref", newEnvRef)
            }
        }
        val row = firstAcqRow(store, session)
        val why = row.getJSONArray("integrity_reasons").toString()
        assertEquals(forged, row.getString("acquisition_id"))
        assertEquals("UNVERIFIED", row.getString("verified_state"))
        assertTrue("expected the recomputed acquisition id to disagree, got $why",
            why.contains("acquisition_id_not_derived_from_its_own_basis"))
        // every join and every individual hash is intact: only recomputation finds this
        assertEquals("COMPLETE_STRUCTURAL", row.getString("structural_state"))
        assertEquals(0, row.getInt("records_with_invalid_hash"))
        assertTrue(row.getBoolean("envelope_hash_verified"))
        assertEquals(6, row.getInt("observations_linked_to_this_envelope"))
    }

    /** An envelope must belong to the session and index being exported, not merely to its records. */
    @Test
    fun anEnvelopeForeignToTheRequestedSessionOrIndexIsRejected() {
        for (field in listOf("session_date", "index")) {
            val store = tmpStore()
            val (_, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
            val session = snap.getString("session")
            val foreign = if (field == "session_date") "1999-01-01" else "BNF"
            rewriteStamped(envelopeFile(store, session)) { it.put(field, foreign) }
            // the records are rewritten to AGREE with the envelope, so the record-to-envelope
            // joins all pass and only the envelope-to-request check can catch it
            val envRef = PremiumObservationContract.ref(PremiumObservationContract.contentHash(
                JSONObject(envelopeFile(store, session).readText())))
            recordFiles(store, session).forEach { f ->
                rewriteStamped(f) { it.put(field, foreign); it.put("envelope_ref", envRef) }
            }
            val row = firstAcqRow(store, session)
            val why = row.getJSONArray("integrity_reasons").toString()
            assertEquals("UNVERIFIED", row.getString("verified_state"))
            val expected = if (field == "session_date") "envelope_session_foreign"
            else "envelope_index_foreign"
            assertTrue("expected $expected, got $why", why.contains(expected))
            assertTrue(row.getBoolean("envelope_hash_verified"))
        }
    }

    /** Two envelopes claiming one acquisition id are reported, not resolved by map overwrite. */
    @Test
    fun duplicateEnvelopeAcquisitionIdsAreReportedRatherThanOverwritten() {
        val store = tmpStore()
        val (a, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        val session = snap.getString("session")
        // a second envelope file under a different key, claiming the SAME acquisition id
        val clone = JSONObject(envelopeFile(store, session).readText())
        clone.remove(PremiumObservationContract.HASH_OMITTED_FIELD)
        clone.remove("hash_basis")
        clone.put("slot", "09:25")
        assertEquals(PremiumCaptureStore.WRITTEN, store.putEnvelope("NF", session,
            "forged-duplicate", PremiumObservationContract.stampHash(clone)).status)
        val ex = PremiumCaptureSession(store).export("NF", session, "09:20", replayContext())
        val dupes = ex.getJSONObject("acquisitions").getJSONArray("duplicate_acquisition_ids")
        assertEquals(1, dupes.length())
        assertEquals(a.acquisitionId, dupes.getString(0))
        val rows = ex.getJSONObject("acquisitions").getJSONArray("rows")
        assertEquals("both envelopes must appear", 2, rows.length())
        for (i in 0 until rows.length()) {
            assertEquals("UNVERIFIED", rows.getJSONObject(i).getString("verified_state"))
            assertTrue(rows.getJSONObject(i).getJSONArray("integrity_reasons").toString()
                .contains("duplicate_envelope_acquisition_id"))
        }
        assertEquals(0, ex.getJSONObject("acquisitions").getInt("verified_batches"))
    }

    /** A missing quote is a valid explicit outcome, never an integrity failure. */
    @Test
    fun missingQuotesStayDistinctFromIntegrityFailure() {
        val store = tmpStore()
        val snap = rawFixture("mon_0920_entry_NF")
        val session = snap.getString("session")
        observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        // a failed acquisition: six complete records, all MISSING_REQUEST_FAILED
        PremiumCaptureSession(store).observeSlot("NF", session, null, null, "09:25", null, 9L, 10L)
        val ex = PremiumCaptureSession(store).export("NF", session, "09:20", replayContext())
        val rows = ex.getJSONObject("acquisitions").getJSONArray("rows")
        assertEquals(2, rows.length())
        for (i in 0 until rows.length()) {
            val r = rows.getJSONObject(i)
            assertEquals("a missing quote is not an integrity fault: ${r.getJSONArray(
                "integrity_reasons")}", "VERIFIED", r.getString("verified_state"))
            assertEquals("COMPLETE_STRUCTURAL", r.getString("structural_state"))
        }
        assertEquals(2, ex.getJSONObject("acquisitions").getInt("verified_batches"))
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
        val refs = PremiumCaptureSession(store).export("NF", session, "09:20", replayContext())
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
        val ex = PremiumCaptureSession(store).export("NF", session, "09:20", replayContext())
        val lot = ex.getJSONObject("references").getJSONObject("contract_lot_table")
        assertEquals("UNRESOLVED_REFERENCE", lot.getString("resolution"))
        assertTrue(lot.isNull("metadata"))
        assertFalse(ex.getJSONObject("claims").getBoolean("offline_resolution_of_lot_table"))
        assertFalse(ex.getJSONObject("claims").getBoolean("offline_resolution_of_instrument_map"))

        // R2-2: a supplied object is NOT resolution. An unrelated table is present-but-unresolved.
        val unrelated = JSONObject().put("NSE", JSONObject().put("NIFTY", 75))
        val ex2 = PremiumCaptureSession(store).export("NF", session, "09:20", replayContext(),
            lotTableObject = unrelated)
        val lot2 = ex2.getJSONObject("references").getJSONObject("contract_lot_table")
        assertTrue("presence is reported", lot2.getBoolean("metadata_present"))
        assertNotEquals("presence must not be called resolution",
            "RESOLVED_FOR_EVERY_ACQUISITION", lot2.getString("resolution"))
        assertEquals(PremiumObservationContract.ref(
            PremiumObservationContract.contentHash(unrelated)), lot2.getString("metadata_ref"))
        assertFalse(ex2.getJSONObject("claims").getBoolean("offline_resolution_of_lot_table"))
        assertFalse(ex2.getJSONObject("claims").getBoolean("authoritative_lot_resolved"))
        assertEquals(0, lot2.getJSONObject("resolution_coverage").getInt("resolved"))
    }

    /**
     * FAILS BEFORE THE FIX, twice over. `lotEvidence` set
     * `lot_authoritative_resolved = true` from an allowed caller source string, a positive integer
     * and a DATE-SHAPED reference — and the shape test was `Regex(".*\\d{4}-\\d{2}-\\d{2}.*")`,
     * which accepts `9999-99-99`, `0000-00-00` and `junk-1234-56-78`. None of that resolves a
     * table row or verifies an effective date.
     */
    @Test
    fun aLotClaimNeverConfersAuthorityAndTheDateMustBeARealDate() {
        val claim = PremiumObservationContract.lotEvidence(
            75, "CONTRACT_LOT_TABLE", "contract_lot_table/NSE/2026-11-07", null)
        assertFalse("a claim is never authority",
            claim.getBoolean("lot_authoritative_resolved"))
        assertEquals("PRODUCER_CLAIM_UNRESOLVED", claim.getString("lot_status"))
        assertEquals(PremiumObservationContract.LOT_UNRESOLVED_NO_METADATA,
            claim.getString("lot_resolution"))
        // the shape checks are published, as necessary-but-not-sufficient
        assertTrue(claim.getBoolean("claimed_source_in_accepted_set"))
        assertTrue(claim.getBoolean("claimed_lot_positive_integer"))
        assertTrue(claim.getBoolean("reference_carries_real_date"))
        assertEquals("2026-11-07", claim.getString("reference_date"))

        // impossible and date-SHAPED-but-not-real references are rejected
        for (bad in listOf("9999-99-99", "0000-00-00", "junk-1234-56-78",
            "contract_lot_table/NSE/2026-02-30", "contract_lot_table/NSE/current")) {
            val e = PremiumObservationContract.lotEvidence(75, "CONTRACT_LOT_TABLE", bad, null)
            assertFalse("'$bad' must not count as a real date",
                e.getBoolean("reference_carries_real_date"))
            assertFalse(e.getBoolean("lot_authoritative_resolved"))
        }
        assertFalse(PremiumObservationContract.isCalendarDate("2026-02-30"))
        assertFalse(PremiumObservationContract.isCalendarDate("9999-99-99"))
        assertTrue(PremiumObservationContract.isCalendarDate("2026-02-28"))
        assertTrue(PremiumObservationContract.isCalendarDate("2024-02-29"))   // a real leap day
        assertFalse(PremiumObservationContract.isCalendarDate("2026-2-8"))
        assertFalse(PremiumObservationContract.isCalendarDate("x2026-02-08"))

        val fractional = PremiumObservationContract.lotEvidence(
            75.5, "CONTRACT_LOT_TABLE", "contract_lot_table/NSE/2026-11-07", null)
        assertFalse(fractional.getBoolean("claimed_lot_positive_integer"))
        val constant = PremiumObservationContract.lotEvidence(
            75, "CONSTANT", "contract_lot_table/NSE/2026-11-07", null)
        assertFalse(constant.getBoolean("claimed_source_in_accepted_set"))
    }

    /**
     * R2-2: authority comes from a resolver that validates the named reference, the underlying's
     * applicability, an integral positive lot and an effective date — and a conflicting table does
     * NOT silently win.
     */
    @Test
    fun theLotResolverValidatesReferenceUnderlyingEffectiveDateAndValue() {
        val ref = "contract_lot_table/NSE/2026-10-01"
        val nf = "NSE_INDEX|Nifty 50"
        fun table(rows: JSONArray, r: String = ref) = JSONObject()
            .put("ref", r).put("provenance", "CONSTRUCTED_TEST_TABLE").put("rows", rows)
        fun row(u: String, lot: Any?, from: String, to: Any? = JSONObject.NULL) = JSONObject()
            .put("underlying_key", u).put("lot", lot)
            .put("effective_from", from).put("effective_to", to)
        fun resolve(t: JSONObject?, r: String? = ref, u: String = nf,
                    asOf: String = "2026-10-19", claimed: Any? = 75) =
            PremiumObservationContract.resolveLotFromTable(t, r, u, asOf, claimed)

        // the one genuinely matching dated test table
        val good = resolve(table(JSONArray().put(row(nf, 75, "2026-10-01"))))
        assertEquals(PremiumObservationContract.LOT_RESOLVED, good.getString("lot_resolution"))
        assertTrue(good.getBoolean("lot_authoritative_resolved"))
        assertEquals(75, good.getInt("resolved_lot"))
        assertEquals("CONSTRUCTED_TEST_TABLE", good.getString("supplied_table_provenance"))

        assertEquals(PremiumObservationContract.LOT_UNRESOLVED_NO_METADATA,
            resolve(null).getString("lot_resolution"))
        assertEquals("an empty object resolves nothing",
            PremiumObservationContract.LOT_UNRESOLVED_REF_MISMATCH,
            resolve(JSONObject()).getString("lot_resolution"))
        assertEquals(PremiumObservationContract.LOT_UNRESOLVED_REF_MISMATCH,
            resolve(table(JSONArray().put(row(nf, 75, "2026-10-01")),
                "contract_lot_table/NSE/2026-09-01")).getString("lot_resolution"))
        // a BNF-only table supplied for NF
        assertEquals(PremiumObservationContract.LOT_UNRESOLVED_NO_ROW,
            resolve(table(JSONArray().put(row("NSE_INDEX|Nifty Bank", 15, "2026-10-01"))))
                .getString("lot_resolution"))
        // not yet effective, and already expired
        assertEquals(PremiumObservationContract.LOT_UNRESOLVED_NO_ROW,
            resolve(table(JSONArray().put(row(nf, 75, "2026-11-01")))).getString("lot_resolution"))
        assertEquals(PremiumObservationContract.LOT_UNRESOLVED_NO_ROW,
            resolve(table(JSONArray().put(row(nf, 75, "2026-09-01", "2026-10-10"))))
                .getString("lot_resolution"))
        // an impossible effective date is not effective
        assertEquals(PremiumObservationContract.LOT_UNRESOLVED_NO_ROW,
            resolve(table(JSONArray().put(row(nf, 75, "2026-02-30")))).getString("lot_resolution"))
        // an invalid row value
        for (bad in listOf<Any?>(0, -5, 75.5, "x", JSONObject.NULL)) {
            assertEquals("row lot $bad must not resolve",
                PremiumObservationContract.LOT_UNRESOLVED_INVALID_ROW,
                resolve(table(JSONArray().put(row(nf, bad, "2026-10-01"))))
                    .getString("lot_resolution"))
        }
        // a conflicting value is reported as a conflict and grants NO authority
        val conflict = resolve(table(JSONArray().put(row(nf, 50, "2026-10-01"))), claimed = 75)
        assertEquals(PremiumObservationContract.LOT_CONFLICTS_WITH_CLAIM,
            conflict.getString("lot_resolution"))
        assertFalse(conflict.getBoolean("lot_authoritative_resolved"))
        assertEquals(50, conflict.getInt("resolved_lot"))
        assertEquals(75, conflict.getInt("producer_claimed_lot"))
        // a reference carrying no real date cannot be resolved against anything
        assertEquals(PremiumObservationContract.LOT_UNRESOLVED_BAD_REFERENCE,
            resolve(table(JSONArray().put(row(nf, 75, "2026-10-01"))), r = "lot_table/current")
                .getString("lot_resolution"))
    }

    /** With a genuinely matching dated test table, the export resolves every acquisition. */
    @Test
    fun aMatchingDatedTestTableResolvesEveryAcquisitionAndSaysSo() {
        val store = tmpStore()
        val (_, snap) = observeFixture(store, "mon_0920_entry_NF", "NF", "09:20")
        val session = snap.getString("session")
        val table = JSONObject()
            .put("ref", "contract_lot_table/NSE/2026-10-01")
            .put("provenance", "CONSTRUCTED_TEST_TABLE")
            .put("rows", JSONArray().put(JSONObject()
                .put("underlying_key", "NSE_INDEX|Nifty 50").put("lot", 75)
                .put("effective_from", "2026-10-01").put("effective_to", JSONObject.NULL)))
        val ex = PremiumCaptureSession(store).export("NF", session, "09:20", replayContext(),
            lotTableObject = table)
        val lot = ex.getJSONObject("references").getJSONObject("contract_lot_table")
        assertEquals("RESOLVED_FOR_EVERY_ACQUISITION", lot.getString("resolution"))
        val cov = lot.getJSONObject("resolution_coverage")
        assertEquals(1, cov.getInt("acquisitions"))
        assertEquals(1, cov.getInt("resolved"))
        assertEquals(0, cov.getInt("conflicting_with_claim"))
        assertEquals(0, cov.getInt("unresolved"))
        assertEquals(1, lot.getJSONArray("per_acquisition").length())
        // The supplied table RESOLVED every acquisition -- that is what this test is about.
        assertTrue(ex.getJSONObject("claims").getBoolean("offline_resolution_of_lot_table"))
        // ...and resolving a reference is still NOT holding authority. This assertion used to
        // read the authority claim as a proxy for resolution, which let a table constructed
        // inside this very test mark itself authoritative.
        assertFalse("a supplied offline table must never confer authority",
            ex.getJSONObject("claims").getBoolean("authoritative_lot_resolved"))
        assertFalse(ex.getJSONObject("claims")
            .getBoolean("a_supplied_offline_table_can_confer_authority"))
        assertFalse(ex.getJSONObject("references").getJSONObject("production_lot_authority")
            .getBoolean("authority_holds"))

        // a MIXED session must not be a blanket success: one acquisition resolves, one conflicts
        val conflicting = JSONObject(table.toString())
        conflicting.getJSONArray("rows").getJSONObject(0).put("lot", 50)
        val mixed = PremiumCaptureSession(store).export("NF", session, "09:20", replayContext(),
            lotTableObject = conflicting)
        val lotM = mixed.getJSONObject("references").getJSONObject("contract_lot_table")
        assertEquals("PARTIALLY_RESOLVED_SEE_COVERAGE", lotM.getString("resolution"))
        assertEquals(1, lotM.getJSONObject("resolution_coverage")
            .getInt("conflicting_with_claim"))
        // The point of the mixed case: a partial resolution must not be reported as resolution.
        // Asserting the AUTHORITY claim here would now pass vacuously, since authority is always
        // false without the production table, so it asserts the resolution claim instead.
        assertFalse("a partially resolved session must not claim offline resolution",
            mixed.getJSONObject("claims").getBoolean("offline_resolution_of_lot_table"))
        assertFalse(mixed.getJSONObject("claims").getBoolean("authoritative_lot_resolved"))
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
        // a genuinely matching dated TEST table, labelled as test evidence
        val table = JSONObject()
            .put("ref", "contract_lot_table/NSE/2026-10-01")
            .put("provenance", "CONSTRUCTED_TEST_TABLE")
            .put("rows", JSONArray().put(JSONObject()
                .put("underlying_key", "NSE_INDEX|Nifty 50").put("lot", 75)
                .put("effective_from", "2026-10-01").put("effective_to", JSONObject.NULL)))
        val ex = s.export("NF", session, "09:20", replayContext(), lotTableObject = table)

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
        assertEquals(3, ex.getJSONObject("acquisitions").getInt("structurally_complete_batches"))
        assertEquals(0, ex.getJSONObject("acquisitions")
            .getJSONArray("unresolved_record_groups").length())
        for (i in 0 until rows.length()) {
            val r = rows.getJSONObject(i)
            assertEquals("COMPLETE_STRUCTURAL", r.getString("structural_state"))
            assertEquals("VERIFIED", r.getString("verified_state"))
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

        // offline reference resolution, claimed only where it is true. The instrument map has no
        // validated schema here, so no resolution is claimed for it even though it is named.
        assertEquals("UNRESOLVED_REFERENCE", ex.getJSONObject("references")
            .getJSONObject("instrument_map").getString("resolution"))
        assertFalse(ex.getJSONObject("claims").getBoolean("offline_resolution_of_instrument_map"))
        assertTrue(ex.getJSONObject("claims").getBoolean("offline_resolution_of_lot_table"))
        // The acceptance export is built with a CONSTRUCTED test table, so it must not claim
        // authoritative lot resolution -- which is also what its own verified contract says
        // under `verified_does_not_mean`. Those two disagreed before this fix.
        assertFalse("the acceptance artifact must not claim authority from a test table",
            ex.getJSONObject("claims").getBoolean("authoritative_lot_resolved"))
        assertFalse(ex.getJSONObject("claims").getBoolean("synchronised_exchange_quotes"))
        assertFalse(ex.getBoolean("device_evidence"))
        assertEquals("VALID", ex.getJSONObject("cohort_state").getString("state"))
        assertTrue(ex.getJSONObject("cohort_state").getBoolean("hash_verified"))

        // durable counters came from disk, not from this object
        val counters = ex.getJSONObject("durable_counters")
        assertEquals(3L, counters.getLong("batches_attempted"))
        assertEquals(3L, counters.getLong("batches_complete"))

        // LABELLING: this is a transformed fixture replay with synthetic acquisition metadata,
        // and the export must say so rather than hardcoding is_synthetic=false (Codex R2).
        val ctx = ex.getJSONObject("evidence_context")
        assertEquals("TRANSFORMED_FIXTURE_REPLAY", ctx.getString("source_kind"))
        assertEquals("HISTORICAL_VENDOR_FIXTURE:contracts/x1_upstox_raw_chains.json",
            ctx.getString("quote_provenance"))
        assertEquals("SYNTHETIC_GENERATED_IN_TEST", ctx.getString("clock_provenance"))
        assertEquals("MANUALLY_ASSIGNED_IN_TEST", ctx.getString("slot_provenance"))
        assertEquals("CONSTRUCTED_TEST_TABLE", ctx.getString("lot_table_provenance"))
        assertFalse(ctx.getBoolean("device_evidence"))
        assertTrue(ctx.getJSONArray("synthetic_fields").length() > 0)
        assertTrue("the replay carries synthetic fields, so is_synthetic must be true",
            ex.getBoolean("is_synthetic"))
        assertFalse(ex.getBoolean("device_evidence"))

        // every acquisition is VERIFIED, not merely structurally complete
        assertEquals(3, ex.getJSONObject("acquisitions").getInt("verified_batches"))
        assertEquals(3, ex.getJSONObject("acquisitions").getInt("usable_evidence_acquisitions"))
        assertEquals(0, ex.getJSONObject("acquisitions")
            .getJSONArray("hash_failure_identifiers").length())
        // the lot resolver ran against the supplied dated table, per acquisition
        val lotRef = ex.getJSONObject("references").getJSONObject("contract_lot_table")
        assertEquals("RESOLVED_FOR_EVERY_ACQUISITION", lotRef.getString("resolution"))
        assertEquals("CONSTRUCTED_TEST_TABLE", lotRef.getString("metadata_provenance"))
        assertEquals(3, lotRef.getJSONObject("resolution_coverage").getInt("resolved"))
        assertEquals(3, lotRef.getJSONArray("per_acquisition").length())
        // the acquisition identity covers this batch's own quote evidence
        for (e in store.readEnvelopes("NF", session)) {
            assertEquals(PremiumObservationContract.REQUEST_RESPONSE_PRESENT,
                e.getString("request_outcome"))
            assertTrue(e.getBoolean("attempt_distinguishable"))
            assertEquals(PremiumObservationContract.REF_HEX_LEN,
                e.getString("batch_evidence_ref").length)
        }

        // Written to a FILE rather than printed. CI established that Gradle captures test stdout
        // and does not surface it, so the println markers this previously used never reached the
        // log at all and the artifact could not be collected. The workflow cats this file.
        val artifact = File("build/premium_capture_acceptance_export.json")
        artifact.parentFile?.mkdirs()
        artifact.writeText(ex.toString(2))
        assertTrue("the acceptance artifact must be written for the review to collect it",
            artifact.isFile && artifact.length() > 0L)
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
            val ex = PremiumCaptureSession(store).export(idx, session, "09:20", replayContext())
            assertEquals(1, ex.getJSONObject("acquisitions").getInt("structurally_complete_batches"))
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
        assertTrue("the split must actually save bytes", flatPerSlot > perSlot)
        // to a FILE, for the same reason as the acceptance export: Gradle captures test stdout, so
        // a println of these numbers is invisible and the measurement cannot be quoted as evidence
        val out = File("build/premium_capture_measurements.txt")
        out.parentFile?.mkdirs()
        out.writeText(
            "source=THIS Kotlin against app/src/test/resources/contracts/x1_upstox_raw_chains.json\n" +
                "desktop JVM, not a device measurement\n" +
                "observation_bytes=$recSizes\n" +
                "envelope_bytes=$envSize\n" +
                "per_slot_bytes=$perSlot\n" +
                "projected_78_slots=${perSlot * 78L}\n" +
                "session_budget=$budget\n" +
                "headroom=${budget - perSlot * 78L}\n" +
                "flat_record_bytes=$flat\n" +
                "flat_per_slot_bytes=$flatPerSlot\n" +
                "projected_78_slots_flat=${flatPerSlot * 78L}\n" +
                "per_record_cap=${PremiumCaptureStore.MAX_BYTES_PER_RECORD}\n" +
                "bytes_saved_by_split_over_78_slots=${(flatPerSlot - perSlot) * 78L}\n")
        assertTrue(out.isFile && out.length() > 0L)
    }

    // ═════════════════ Commit 2 — the runtime coordinator ════════════════════════════════════

    private fun tmpFilesDir(): File = Files.createTempDirectory("premfiles").toFile()

    private fun acq(index: String, snapId: String?, receipt: String? = "09:20",
                    start: Long = 1_000L, end: Long = 1_300L) =
        PremiumCaptureCoordinator.ChainAcquisition(
            index = index,
            rawChain = snapId?.let { rawFixture(it).getJSONObject("raw") },
            requestStartedMs = start, responseReceivedMs = end,
            requestedUnderlyingKey = PremiumCaptureCohort.INDEX_UNDERLYING[index],
            expiry = "2026-10-27", receiptHhmm = receipt)

    /**
     * THE CASE THAT WAS MISSING, and CI found its absence the expensive way.
     *
     * My size test measured records built by `observeFixture`, which passes no lot authority — so
     * it never covered the path that actually grew the envelope. Attaching the ContractLotTable
     * verdict inline pushed the envelope past the 2 KiB per-record cap, `putEnvelope` was refused,
     * and every observation in the coordinator path was silently dropped with
     * ENVELOPE_NOT_PERSISTED. The cap was not enlarged: the authority is now recorded once per
     * index/session and referenced by content. This asserts the cap on the REAL coordinator path.
     */
    @Test
    fun theCoordinatorPathKeepsEveryRecordInsideThePerRecordCap() {
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        val r = coordinate(fd, "2026-10-19", "09:20",
            listOf(acq("NF", "mon_0920_entry_NF"), acq("BNF", "mon_0920_entry_BNF")))
        assertEquals(PremiumCaptureCoordinator.SUBMITTED, r.getString("status"))
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        var maxSize = 0
        for (idx in listOf("NF", "BNF")) {
            val envs = store.readEnvelopes(idx, "2026-10-19")
            val recs = store.readAll(idx, "2026-10-19")
            assertEquals("$idx must have persisted its envelope", 1, envs.size)
            assertEquals("$idx must have persisted all six observations", 6, recs.size)
            for (o in envs + recs) {
                val n = o.toString().toByteArray(Charsets.UTF_8).size
                maxSize = maxOf(maxSize, n)
                assertTrue("$idx: a ${n} B record exceeds the unchanged " +
                    "${PremiumCaptureStore.MAX_BYTES_PER_RECORD} B cap",
                    n <= PremiumCaptureStore.MAX_BYTES_PER_RECORD)
            }
            // the authority is recorded ONCE per index/session, not per slot
            val auth = store.readLotAuthority(idx, "2026-10-19")
            assertNotNull("$idx: the lot authority must be recorded", auth)
            assertEquals("CONTRACT_LOT_TABLE", auth!!.getString("authority"))
            // and each envelope references it by content rather than carrying it
            val env = envs.first()
            assertFalse("the authority must not be inlined per slot",
                env.getJSONObject("lot").has("production_authority"))
            val ref = env.getJSONObject("lot").getString("production_authority_ref")
            assertEquals(PremiumObservationContract.ref(
                PremiumObservationContract.contentHash(auth)), ref)
        }
        // a second slot must not rewrite the authority
        coordinate(fd, "2026-10-19", "09:25",
            listOf(acq("NF", "mon_0920_entry_NF", receipt = "09:25",
                start = 300_000L, end = 300_300L)))
        val row = coordinate(fd, "2026-10-19", "09:30",
            listOf(acq("NF", "mon_0920_entry_NF", receipt = "09:30",
                start = 600_000L, end = 600_300L)))
            .getJSONArray("indices").getJSONObject(0)
        assertEquals(PremiumCaptureStore.DUPLICATE_NO_OP, row.getString("lot_authority_write"))
        println("COORDINATOR_MAX_RECORD_BYTES=$maxSize cap=" +
            "${PremiumCaptureStore.MAX_BYTES_PER_RECORD}")
    }

    /** The authority is exported from the per-index/session record the envelopes reference. */
    @Test
    fun theExportResolvesTheProductionLotAuthorityFromItsOwnRecord() {
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        coordinate(fd, "2026-10-19", "09:20",
            listOf(acq("NF", "mon_0920_entry_NF")))
        val pa = PremiumExportSection.build(fd, "2026-10-19")
            .getJSONObject("indices").getJSONObject("NF")
            .getJSONObject("references").getJSONObject("production_lot_authority")
        assertTrue(pa.getBoolean("recorded"))
        assertTrue("the envelopes' reference must match the recorded authority",
            pa.getBoolean("reference_matches_record"))
        assertTrue(pa.getBoolean("is_the_applications_own_contract_table"))
        assertFalse("the generic test resolver must never stand in for it",
            pa.getBoolean("generic_supplied_table_substituted"))
        val auth = pa.getJSONObject("authority")
        assertEquals(ContractLotTable.VERSION_ID, auth.getString("lot_table_version"))
        assertFalse(auth.getBoolean("operational_current_allowed"))
        assertFalse(auth.getBoolean("flattened_to_one_lot_per_underlying"))
        // the claim is only authoritative if the table itself said so
        assertEquals(auth.optBoolean("authoritative", false), pa.getBoolean("authoritative"))

        // THE INVARIANT, pinned here because the two disagreed in a shipped green artifact:
        // the top-level authority CLAIM must equal the production authority verdict in the same
        // document. Asserted as agreement rather than as a fixed outcome, so this holds whichever
        // way ContractLotTable rules on this expiry and cannot rot if the table changes.
        val nf = PremiumExportSection.build(fd, "2026-10-19")
            .getJSONObject("indices").getJSONObject("NF")
        val claims = nf.getJSONObject("claims")
        val expectedAuthority = pa.getBoolean("recorded") && pa.getBoolean("resolved") &&
            pa.getBoolean("authoritative") && pa.getBoolean("reference_matches_record")
        assertEquals("the authority claim must agree with the production authority block",
            expectedAuthority, claims.getBoolean("authoritative_lot_resolved"))
        assertEquals(expectedAuthority, nf.getJSONObject("references")
            .getJSONObject("production_lot_authority").getBoolean("authority_holds"))
        assertFalse(claims.getBoolean("a_supplied_offline_table_can_confer_authority"))
        assertEquals("PRODUCTION_CONTRACT_LOT_TABLE_RECORDED_RESOLVED_AND_REFERENCED_BY_CONTENT",
            claims.getString("lot_authority_basis"))

        // And supplying an offline table cannot raise the verdict: the same session exported with
        // a supplied table object reports the same authority as without one. (The table here is
        // deliberately empty — the claim must not move merely because a table was passed.)
        val table = JSONObject().put("version", "test_lot_table_v1")
            .put("provenance", "CONSTRUCTED_IN_TEST")
            .put("rows", JSONArray())
        val withTable = PremiumCaptureSession(PremiumCaptureStore(PremiumCaptureControl.root(fd)))
            .export("NF", "2026-10-19", "09:20", replayContext(), lotTableObject = table)
        assertEquals("a supplied table must not change the authority verdict",
            expectedAuthority,
            withTable.getJSONObject("claims").getBoolean("authoritative_lot_resolved"))
    }

    @Test
    fun theControlIsOffByDefaultAndAnUnreadableControlIsOffNotOn() {
        val fd = tmpFilesDir()
        assertFalse("capture must be OFF with no control file at all",
            PremiumCaptureControl.isEnabled(fd))
        assertFalse(PremiumCaptureControl.status(fd).getBoolean("enabled"))
        assertFalse("a status read must not create the capture directory",
            PremiumCaptureControl.root(fd).exists())

        assertTrue(PremiumCaptureControl.setEnabled(fd, true).ok)
        assertTrue(PremiumCaptureControl.isEnabled(fd))
        assertTrue(PremiumCaptureControl.setEnabled(fd, false).ok)
        assertFalse(PremiumCaptureControl.isEnabled(fd))

        // A malformed control file is OFF, and the fact is reported rather than hidden.
        //
        // The explicit refresh is required, and that IS the documented policy: a hand edit of the
        // file mid-process is deliberately not observed, because the control is read once and
        // then answered from memory to keep filesystem IO off the poll and UI paths. Without the
        // refresh this test would assert a behaviour the design states it does not have.
        File(PremiumCaptureControl.root(fd), "control.json").writeText("{not json")
        PremiumCaptureControl.refresh(fd)
        assertFalse("a capture that cannot read its own switch must not run",
            PremiumCaptureControl.isEnabled(fd))
        assertTrue(PremiumCaptureControl.controlUnreadable(fd))
        assertTrue(PremiumCaptureControl.status(fd).getBoolean("control_unreadable"))
        // and the control is explicit about what it is not
        val st = PremiumCaptureControl.status(fd)
        assertFalse(st.getBoolean("is_trading_mode"))
        assertFalse(st.getBoolean("changes_paper_authority"))
        assertEquals("NONE_CAPTURE_STOPS_CLEANLY_AT_THE_CAP",
            st.getJSONObject("retention").getString("deletion_policy"))
    }

    /** CONTROL OFF: no evidence prepared, no file written, nothing created. */
    @Test
    fun whenOffTheCoordinatorTouchesNothingAtAll() {
        val fd = tmpFilesDir()
        // the persisted control is loaded ASYNCHRONOUSLY and admission is conservative
        // until it settles, so DISABLED is only the honest answer once it has
        assertTrue(PremiumCaptureControl.awaitInitialized(fd, 10_000L))
        val r = coordinate(fd, "2026-10-19", "09:20",
            listOf(acq("NF", "mon_0920_entry_NF"), acq("BNF", "mon_0920_entry_BNF")))
        assertEquals(PremiumCaptureCoordinator.DISABLED, r.getString("status"))
        assertEquals(0, r.getJSONArray("indices").length())
        assertFalse("OFF must not create the capture directory",
            PremiumCaptureControl.root(fd).exists())
        // AN UNREADABLE CONTROL IS NOT ON -- EVEN AFTER AN EXPLICIT IN-PROCESS ON.
        //
        // Two steps are required and both are deliberate. The explicit refresh is the documented
        // policy: an external edit mid-process is not observed, because the control is answered
        // from memory to keep IO off the poll path. CI caught this test asserting the old
        // per-poll-re-read behaviour, which the design no longer has.
        //
        // The refusal then overrides the in-process ON, which is the arguable part: a damaged
        // control file means the subsystem cannot confirm or persist its own switch, so it stops,
        // consistent with this project's rule that corruption is never treated as safety.
        PremiumCaptureControl.setEnabled(fd, true)
        assertTrue("the premise: it really is ON before the file is damaged",
            PremiumCaptureControl.isEnabled(fd))
        File(PremiumCaptureControl.root(fd), "control.json").writeText("{broken")
        PremiumCaptureControl.refresh(fd)
        val adm = PremiumCaptureControl.admission(fd)
        assertFalse(adm.getBoolean("admit"))
        assertTrue("the override of a live in-process ON must be reported, not silent",
            adm.getBoolean("overrides_in_process_intent"))
        assertEquals(PremiumCaptureCoordinator.CONTROL_UNREADABLE,
            coordinate(fd, "2026-10-19", "09:20",
                listOf(acq("NF", "mon_0920_entry_NF"))).getString("status"))

        // ...and it is not a trap: writing a valid control again recovers, so a corrupt file
        // cannot permanently disable capture
        assertTrue(PremiumCaptureControl.setEnabled(fd, true).ok)
        assertEquals(PremiumCaptureControl.INIT_READY,
            PremiumCaptureControl.statusFast(fd).getString("control_init"))
        assertTrue("pressing the control again must recover",
            PremiumCaptureControl.isEnabled(fd))
    }

    /** CONTROL ON: the already-fetched chain reaches persisted evidence, per index. */
    @Test
    fun whenOnTheAlreadyFetchedChainReachesPersistedEvidencePerIndex() {
        val fd = tmpFilesDir()
        assertTrue(PremiumCaptureControl.setEnabled(fd, true).ok)
        val r = coordinate(fd, "2026-10-19", "09:20",
            listOf(acq("NF", "mon_0920_entry_NF"), acq("BNF", "mon_0920_entry_BNF")))
        assertEquals(PremiumCaptureCoordinator.SUBMITTED, r.getString("status"))
        assertEquals("BOUNDED_ONE_RUNNING_AT_MOST_ONE_WAITING", r.getString("queued_work"))
        val rows = r.getJSONArray("indices")
        assertEquals(2, rows.length())
        val byIndex = (0 until rows.length()).associate {
            rows.getJSONObject(it).getString("index") to rows.getJSONObject(it) }
        for (idx in listOf("NF", "BNF")) {
            val row = byIndex[idx]!!
            assertEquals("$idx: ${row.optString("detail")}",
                PremiumCaptureSession.OBSERVED, row.getString("status"))
            assertEquals(6, row.getInt("written"))
            assertTrue(row.getBoolean("batch_complete"))
            // renamed with the move off the poll thread: this is the WORKER's persist time, not
            // time the poll paid, and the field name now says so
            assertTrue(row.has("persist_elapsed_ms"))
        }
        // NF and BNF are isolated: separate cohorts, separate contracts, no cross-index fallback
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        assertEquals(6, store.outcomeCount("NF", "2026-10-19"))
        assertEquals(6, store.outcomeCount("BNF", "2026-10-19"))
        val nfKeys = store.readAll("NF", "2026-10-19").map { it.getString("instrument_key") }.toSet()
        val bnfKeys = store.readAll("BNF", "2026-10-19").map { it.getString("instrument_key") }.toSet()
        assertEquals(emptySet<String>(), nfKeys.intersect(bnfKeys))
        assertNotEquals(byIndex["NF"]!!.getString("acquisition_id"),
            byIndex["BNF"]!!.getString("acquisition_id"))
        assertEquals("NSE_INDEX|Nifty 50",
            JSONObject(store.readCohortText("NF", "2026-10-19")!!).getString("underlying_key"))
        assertEquals("NSE_INDEX|Nifty Bank",
            JSONObject(store.readCohortText("BNF", "2026-10-19")!!).getString("underlying_key"))
        // the scheduler's intent is recorded beside the clock-derived slot, not used to relabel
        val env = store.readEnvelopes("NF", "2026-10-19").first()
        assertEquals("09:20", env.getString("slot"))
        assertEquals("09:20", env.getJSONObject("clocks").getString("scheduler_slot_declared"))
        assertTrue(env.getJSONObject("clocks")
            .getBoolean("scheduler_slot_agrees_with_clock"))
    }

    /** A late response is not relabelled: the clock decides the slot, the disagreement is recorded. */
    @Test
    fun aLateResponseKeepsItsRealSlotAndRecordsTheDisagreement() {
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        // the scheduler intended 09:20; this index's response actually arrived at 09:26
        val r = coordinate(fd, "2026-10-19", "09:20",
            listOf(acq("NF", "mon_0920_entry_NF", receipt = "09:26")))
        assertEquals(PremiumCaptureCoordinator.SUBMITTED, r.getString("status"))
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        val env = store.readEnvelopes("NF", "2026-10-19").first()
        assertEquals("the real clock decides the slot", "09:25", env.getString("slot"))
        assertEquals("09:20", env.getJSONObject("clocks").getString("scheduler_slot_declared"))
        assertFalse("the disagreement is reported, not resolved by relabelling",
            env.getJSONObject("clocks").getBoolean("scheduler_slot_agrees_with_clock"))
    }

    /** A failed chain is recorded once a cohort exists, not lost. */
    @Test
    fun aFailedChainIsRecordedOnceACohortExists() {
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        coordinate(fd, "2026-10-19", "09:20",
            listOf(acq("NF", "mon_0920_entry_NF")))
        val r = coordinate(fd, "2026-10-19", "09:25",
            listOf(acq("NF", null, receipt = "09:25", start = 60_000L, end = 61_000L)))
        val row = r.getJSONArray("indices").getJSONObject(0)
        assertEquals(PremiumCaptureSession.OBSERVED, row.getString("status"))
        assertFalse(row.getBoolean("chain_present"))
        assertEquals(6, row.getInt("written"))
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        val failed = store.readAll("NF", "2026-10-19").filter { it.getString("slot") == "09:25" }
        assertEquals(6, failed.size)
        failed.forEach {
            assertEquals(PremiumObservationContract.OUTCOME_REQUEST_FAILED,
                it.getString("outcome"))
        }
        val env = store.readEnvelopes("NF", "2026-10-19").first { it.getString("slot") == "09:25" }
        assertEquals(PremiumObservationContract.REQUEST_FAILED, env.getString("request_outcome"))
    }

    /** `0` is never a real timestamp, and the vendor clock stays unknown. */
    @Test
    fun aNonPositiveTimestampIsUnknownAndNeverZero() {
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        coordinate(fd, "2026-10-19", "09:20",
            listOf(acq("NF", "mon_0920_entry_NF", start = 0L, end = 0L)))
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        val clocks = store.readEnvelopes("NF", "2026-10-19").first().getJSONObject("clocks")
        assertTrue("0 must not be stored as a real request clock",
            clocks.isNull("acquisition_request_ms"))
        assertTrue(clocks.isNull("acquisition_response_ms"))
        assertTrue(clocks.isNull("acquisition_elapsed_ms"))
        assertEquals("VENDOR_TS_ABSENT", clocks.getString("vendor_ts_status"))
        assertFalse(clocks.getBoolean("quote_event_clock_verified"))
    }

    /**
     * The lot claim comes from the application's OWN contract authority, by index/expiry/date, and
     * stays unresolved rather than being invented. Codex: do not flatten that table to one number
     * per underlying, and never use an operational constant as historical authority.
     */
    @Test
    fun theLotClaimUsesTheContractAuthorityFaithfullyOrStaysUnresolved() {
        val nf = PremiumCaptureCoordinator.lotClaimFromContractAuthority(
            "NF", "2026-10-19", "2026-10-27")
        val auth = nf.getJSONObject("production_authority")
        assertEquals("CONTRACT_LOT_TABLE", auth.getString("authority"))
        assertEquals(ContractLotTable.VERSION_ID, auth.getString("lot_table_version"))
        assertFalse("an operational constant is never historical authority",
            auth.getBoolean("operational_current_allowed"))
        assertFalse(auth.getBoolean("flattened_to_one_lot_per_underlying"))
        // whatever the table says, its own verdict is carried rather than overridden
        assertTrue(auth.has("resolved")); assertTrue(auth.has("authoritative"))
        assertTrue(auth.has("matched_rule_id")); assertTrue(auth.has("lot_as_of"))
        assertTrue(auth.has("expiry_cycle")); assertTrue(auth.has("unavailable_reason"))
        if (!auth.getBoolean("resolved") || !auth.getBoolean("authoritative")) {
            assertTrue("an unresolved table must not produce a claim", nf.isNull("claimed_lot"))
            assertTrue(nf.isNull("table_ref"))
        } else {
            assertEquals("CONTRACT_LOT_TABLE", nf.getString("claimed_source"))
            assertNotNull(PremiumObservationContract.dateInReference(nf.getString("table_ref")))
        }
        // an unknown index resolves nothing and invents nothing
        val junk = PremiumCaptureCoordinator.lotClaimFromContractAuthority(
            "SENSEX", "2026-10-19", "2026-10-27")
        assertTrue(junk.isNull("claimed_lot"))
        assertFalse(junk.getJSONObject("production_authority").getBoolean("authoritative"))
    }

    /** A full store stops capture cleanly and visibly, and never throws. */
    @Test
    fun aFullStoreStopsCaptureCleanlyAndVisibly() {
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        val root = PremiumCaptureControl.root(fd)
        root.mkdirs()
        fillFile(File(root, "filler.bin"), PremiumCaptureStore.MAX_BYTES_GLOBAL)
        // measured BEFORE the run: this root is deliberately filled past the cap by the test
        // itself, so the property to assert is that capture adds NOTHING, not that the total
        // happens to sit under the limit. CI rejected the absolute form and was right to:
        // 4194304 B of filler plus the control file written above is already over.
        val rootBytesBefore = PremiumCaptureStore(PremiumCaptureControl.root(fd)).globalBytes()
        val r = coordinate(fd, "2026-10-19", "09:20",
            listOf(acq("NF", "mon_0920_entry_NF")))
        val row = r.getJSONArray("indices").getJSONObject(0)
        assertNotEquals("a full store must refuse, not throw",
            "CAPTURE_ERROR", row.getString("status"))
        assertEquals(PremiumCaptureSession.COHORT_PERSIST_FAILED, row.getString("status"))
        assertEquals(0, row.getInt("written"))
        // The refused summary is reported from THIS index's own attempt. It used to be a single
        // coordinator-level verdict for `acquisitions.first()`, which could not speak for both
        // indices; it now lives inside each index's row.
        val sw = row.getJSONObject("summary_write")
        assertFalse("a failed disk cannot record its own failure",
            sw.getBoolean("durably_recorded"))
        assertEquals(PremiumCaptureStore.REFUSED_GLOBAL_BYTES, sw.getString("status"))

        // AND THE ROOT MUST NOT HAVE GROWN AT ALL. control.json/last_run.json and their temps
        // used to be written straight to this root, outside admission and outside the store's
        // lock, so the status file could add bytes to a root that had just refused the records it
        // describes. Asserted on final occupancy, not only on record counts.
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        assertEquals("a full root must not grow by writing about its own refusal",
            rootBytesBefore, store.globalBytes())

        // ...and turning capture OFF must still be possible at capacity. Refusing the stop action
        // because the store is full would be the worst available failure mode.
        assertTrue("OFF must remain possible when the store is full",
            PremiumCaptureControl.setEnabled(fd, false).ok)
        assertFalse(PremiumCaptureControl.isEnabled(fd))
        // and the operator status must SAY what is happening, not just that the switch is set
        assertEquals("OFF", PremiumCaptureControl.status(fd).getString("collection_state"))
    }

    /**
     * The status file is a bounded STRUCTURED object, not truncated text.
     *
     * `run.toString().take(8192)` bounded characters rather than UTF-8 bytes and could cut a JSON
     * document mid-value, so the one file whose job is to report state could be left unparseable.
     */
    @Test
    fun theStatusFileIsBoundedStructuredJsonAndNeverTruncatedText() {
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        // a hostile run object: very long non-ASCII error text and a huge nested payload
        val huge = JSONObject()
            .put("index", "NF")
            .put("status", "CAPTURE_ERROR")
            .put("error", "सत्यापन" .repeat(4000))
            .put("written", 0)
            .put("detail", JSONObject().put("blob", "🚀".repeat(20_000)))
        PremiumCaptureControl.recordRun(fd, huge)
        val f = File(PremiumCaptureControl.root(fd), "last_run.json")
        assertTrue("the status file must exist", f.isFile)
        val text = f.readText()
        // it must PARSE — the whole point. Truncated text would throw here.
        val parsed = JSONObject(text)
        assertEquals("NF", parsed.getString("index"))
        assertEquals("CAPTURE_ERROR", parsed.getString("status"))
        assertEquals("BOUNDED_STRUCTURED_FIELDS_NEVER_TRUNCATED_TEXT",
            parsed.getString("projection"))
        assertTrue("non-ASCII error text must be length-capped, not cut mid-document",
            parsed.getString("error").length <= 64)
        assertFalse("the unbounded nested payload must not be carried at all", parsed.has("detail"))
        assertTrue("the file must fit the per-control-file bound: ${f.length()} B",
            f.length() <= PremiumCaptureStore.MAX_BYTES_PER_CONTROL_FILE)
        // and it is accounted: the root's control bytes stay inside the declared reserve
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        assertTrue(store.controlBytes() <= PremiumCaptureStore.CONTROL_RESERVE_BYTES)
    }

    /**
     * Evidence must leave the control reserve intact, so the stop action and the status that
     * reports a full store stay writable. The evidence caps are not enlarged by this: the
     * reserve is carved out INSIDE the existing global bound.
     */
    @Test
    fun evidenceCannotConsumeTheControlReserve() {
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        val root = PremiumCaptureControl.root(fd)
        root.mkdirs()
        // fill to just inside the global cap but past the evidence budget
        val fill = PremiumCaptureStore.MAX_BYTES_GLOBAL -
            PremiumCaptureStore.CONTROL_RESERVE_BYTES / 2
        fillFile(File(root, "filler.bin"), fill)
        val store = PremiumCaptureStore(root)
        val w = store.putLotAuthority("NF", "2026-10-19", JSONObject().put("a", 1))
        assertEquals("evidence must be refused once only the reserve is left",
            PremiumCaptureStore.REFUSED_GLOBAL_BYTES, w.status)
        assertTrue("the refusal must name the reserve",
            (w.detail ?: "").contains("control reserve"))
        // the OFF action still works against the reserve
        assertTrue(PremiumCaptureControl.setEnabled(fd, false).ok)
    }

    /** Capture failure is always a status; it can never propagate to the caller. */
    /**
     * The PREPARE budget stops preparation rather than delaying a poll.
     *
     * Narrower than the claim this test used to make. The old budget was checked only before each
     * index started and then bounded nothing: deep copy, directory scans, lock acquisition and
     * every write ran with no deadline, so slow IO could still delay a poll for far longer than
     * the budget, and this test — passing with a zero budget — only ever proved that work can be
     * skipped BEFORE IT STARTS. It never showed that an operation becoming slow after admission
     * is survivable, so it must not be read as evidence of that.
     *
     * What bounds persistence now is isolation, not this budget. See
     * [slowPersistenceCannotReachThePollThreadAndPendingWorkStaysBounded].
     */
    @Test
    fun theCaptureBudgetStopsWorkRatherThanDelayingThePoll() {
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        val r = coordinate(fd, "2026-10-19", "09:20",
            listOf(acq("NF", "mon_0920_entry_NF"), acq("BNF", "mon_0920_entry_BNF")),
            prepareBudgetMs = 0L)
        assertEquals(PremiumCaptureCoordinator.PREPARE_BUDGET_EXCEEDED, r.getString("status"))
        assertEquals(0L, r.getLong("prepare_budget_ms"))
        val rows = r.getJSONArray("indices")
        assertEquals("both indices must be reported, not silently skipped", 2, rows.length())
        for (i in 0 until rows.length()) {
            assertEquals(PremiumCaptureCoordinator.PREPARE_BUDGET_EXCEEDED,
                rows.getJSONObject(i).getString("status"))
        }
        // nothing was written, and the refusal is visible rather than implicit
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        assertEquals(0, store.outcomeCount("NF", "2026-10-19"))
        assertEquals(0, store.envelopeCount("NF", "2026-10-19"))
        assertEquals("BOUNDED_ONE_RUNNING_AT_MOST_ONE_WAITING", r.getString("queued_work"))
        // and the production default is the constant, not the injected value
        assertEquals(250L, PremiumCaptureCoordinator.PREPARE_BUDGET_MS_PER_POLL)
        // the status says what it describes, so a reader cannot mistake acceptance for a write
        assertEquals("SUBMISSION_NOT_PERSISTENCE", r.getString("status_describes"))
    }

    /**
     * SLOW PERSISTENCE CANNOT REACH THE POLL THREAD, and pending work stays bounded.
     *
     * This is the test the old budget test could not be: IO that blocks AFTER the work has begun,
     * rather than work skipped before it starts. The persistence action blocks indefinitely on the
     * worker; the poll-facing submission must still return promptly, a second job may wait, and a
     * third must be refused with a stated reason rather than queued without limit.
     *
     * It also states what is NOT bounded: a job already inside a blocking call cannot be
     * interrupted. The running slot stays occupied until it returns, and capture refuses further
     * work instead of pretending it was cancelled.
     */
    @Test
    fun slowPersistenceCannotReachThePollThreadAndPendingWorkStaysBounded() {
        PremiumCaptureWorker.resetForTest()
        val release = java.util.concurrent.CountDownLatch(1)
        val entered = java.util.concurrent.CountDownLatch(1)
        fun blockingJob(index: String) = PremiumCaptureWorker.Job(
            index = index, sessionDate = "2026-10-19", preparedBytes = 16,
            persist = {
                entered.countDown()
                // blocks after work has begun; this is exactly what a stuck filesystem looks like
                release.await()
                JSONObject().put("status", "OK").put("index", index)
            })
        try {
            assertEquals(PremiumCaptureWorker.ACCEPTED_RUNNING,
                PremiumCaptureWorker.submit(blockingJob("NF")))
            assertTrue("the blocking job must actually be running",
                entered.await(10, java.util.concurrent.TimeUnit.SECONDS))

            // the poll-facing submission must RETURN, promptly, while IO is blocked
            val t0 = System.nanoTime()
            val queued = PremiumCaptureWorker.submit(blockingJob("BNF"))
            val submitMs = (System.nanoTime() - t0) / 1_000_000L
            assertEquals(PremiumCaptureWorker.ACCEPTED_QUEUED, queued)
            assertTrue("submission must not wait on blocked IO: took $submitMs ms",
                submitMs < 1_000L)

            // and a third is refused, not queued without limit
            val t1 = System.nanoTime()
            val refused = PremiumCaptureWorker.submit(blockingJob("NF"))
            assertEquals(PremiumCaptureWorker.REFUSED_BUSY, refused)
            assertTrue("refusal must also be prompt",
                (System.nanoTime() - t1) / 1_000_000L < 1_000L)

            val st = PremiumCaptureWorker.stats()
            assertTrue(st.getBoolean("running"))
            assertTrue(st.getBoolean("waiting"))
            assertTrue("occupancy must be visible, not inferred",
                st.getInt("pending_bytes") > 0)
            // the honest limitation, asserted rather than left to prose
            assertTrue(st.getBoolean("in_flight_io_is_not_cancellable"))
        } finally {
            release.countDown()
            PremiumCaptureWorker.awaitIdle(30_000L)
        }
    }

    /** A prepared payload larger than the per-job bound is refused, not trimmed. */
    @Test
    fun anOversizedPreparedPayloadIsRefusedRatherThanTrimmed() {
        PremiumCaptureWorker.resetForTest()
        val s = PremiumCaptureWorker.submit(PremiumCaptureWorker.Job(
            index = "NF", sessionDate = "2026-10-19",
            preparedBytes = PremiumCaptureWorker.MAX_PREPARED_BYTES_PER_JOB + 1,
            persist = { JSONObject().put("status", "SHOULD_NOT_RUN") }))
        assertEquals(PremiumCaptureWorker.REFUSED_TOO_LARGE, s)
        assertFalse(PremiumCaptureWorker.stats().getBoolean("running"))
    }

    /**
     * A held capture lock cannot reach the poll thread either.
     *
     * The export and the worker both take the store's root lock. Submission must not, so holding
     * that lock from another thread must not delay the poll-facing call.
     */
    @Test
    fun aHeldCaptureLockDoesNotDelayThePollFacingSubmission() {
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        PremiumCaptureWorker.resetForTest()
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        val holding = java.util.concurrent.CountDownLatch(1)
        val release = java.util.concurrent.CountDownLatch(1)
        val holder = Thread {
            store.withRootLockForTest {
                holding.countDown()
                release.await()
            }
        }
        holder.isDaemon = true
        holder.start()
        try {
            assertTrue(holding.await(10, java.util.concurrent.TimeUnit.SECONDS))
            val t0 = System.nanoTime()
            val r = PremiumCaptureCoordinator.onChainsAcquired(fd, "2026-10-19", "09:20",
                listOf(acq("NF", "mon_0920_entry_NF")))
            val ms = (System.nanoTime() - t0) / 1_000_000L
            assertEquals(PremiumCaptureCoordinator.SUBMITTED, r.getString("status"))
            assertTrue("submission must not block on a held capture lock: took $ms ms",
                ms < 2_000L)
        } finally {
            release.countDown()
            holder.join(10_000L)
            PremiumCaptureWorker.awaitIdle(30_000L)
        }
    }

    @Test
    fun coordinatorFailureIsAlwaysAStatusNeverAnException() {
        // an unwritable files dir
        val missing = File("/proc/nonexistent-premium-files")
        assertTrue(PremiumCaptureControl.awaitInitialized(missing, 10_000L))
        val r = coordinate(
            missing, "2026-10-19", "09:20",
            listOf(acq("NF", "mon_0920_entry_NF")))
        assertEquals("no control file is readable there, so it must be OFF",
            PremiumCaptureCoordinator.DISABLED, r.getString("status"))
        // a bad session date is refused explicitly
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        for (bad in listOf(null, "", "not-a-date", "2026-02-30")) {
            assertEquals(PremiumCaptureCoordinator.NO_SESSION,
                coordinate(fd, bad, "09:20",
                    listOf(acq("NF", "mon_0920_entry_NF"))).getString("status"))
        }
    }

    /**
     * Measured resource cost of the coordinator on the real fixture, written to a file the CI
     * step prints. Desktop JVM, **not** a device measurement: it bounds the work the poll thread
     * takes on this hardware, and is explicitly not a claim about phone CPU or IO.
     */
    @Test
    fun measuredCoordinatorResourceCostOnTheFixture() {
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        val slots = listOf("09:20", "09:25", "09:30", "09:35", "09:40", "09:45", "09:50", "09:55")
        val perRun = ArrayList<Long>()
        var writes = 0
        // Measured SEPARATELY, because they are different costs and only one of them is paid by
        // the poll thread: `perRun` is the poll-facing submission alone, `perDrain` is the
        // off-poll persistence. The old single figure covered synchronous persistence and would
        // now silently measure submission plus the worker drain, which no poll ever waits for.
        val perDrain = ArrayList<Long>()
        for ((i, slot) in slots.withIndex()) {
            PremiumCaptureWorker.resetForTest()
            val t0 = System.nanoTime()
            val r = PremiumCaptureCoordinator.onChainsAcquired(fd, "2026-10-19", slot,
                listOf(acq("NF", "mon_0920_entry_NF", receipt = slot,
                    start = 1_000L + i * 300_000L, end = 1_300L + i * 300_000L),
                    acq("BNF", "mon_0920_entry_BNF", receipt = slot,
                        start = 1_000L + i * 300_000L, end = 1_320L + i * 300_000L)))
            perRun.add((System.nanoTime() - t0) / 1_000_000)
            assertEquals(PremiumCaptureCoordinator.SUBMITTED, r.getString("status"))
            val t1 = System.nanoTime()
            assertTrue(PremiumCaptureWorker.awaitIdle(30_000L))
            perDrain.add((System.nanoTime() - t1) / 1_000_000)
            for (idx in listOf("NF", "BNF")) {
                val persisted = PremiumCaptureWorker.resultFor(idx)
                assertNotNull("both indices must have persisted a result", persisted)
                writes += persisted!!.getInt("written")
            }
        }
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        val nfBytes = store.sessionBytes("NF", "2026-10-19")
        val bnfBytes = store.sessionBytes("BNF", "2026-10-19")
        val global = store.globalBytes()
        assertEquals("every slot must persist both indices' six contracts",
            slots.size * 12, writes)
        // The POLL-FACING cost is what must stay inside the prepare budget. The drain cost is
        // reported but deliberately NOT asserted against it: no poll waits for it, and asserting
        // a bound on it here would imply a guarantee about IO duration that nothing provides.
        assertTrue("poll-facing submission cost $perRun exceeded the prepare budget",
            perRun.all { it < PremiumCaptureCoordinator.PREPARE_BUDGET_MS_PER_POLL })

        // retention: the global cap bounds collection, so state how far it goes
        val perIndexSession = maxOf(nfBytes, bnfBytes)
        val slotsPerIndexSession = slots.size
        val bytesPerSlot = perIndexSession / slotsPerIndexSession
        val out = File("build/premium_capture_resource_cost.txt")
        out.parentFile?.mkdirs()
        out.writeText(
            "source=THIS Kotlin coordinator over the committed raw vendor fixture\n" +
                "desktop JVM, NOT a device measurement; no phone CPU/IO claim is made\n" +
                "slots_run=$slotsPerIndexSession indices_per_slot=2\n" +
                "poll_facing_submit_ms_both_indices=$perRun\n" +
                "poll_facing_submit_ms_max=${perRun.max()} prepare_budget_ms=" +
                "${PremiumCaptureCoordinator.PREPARE_BUDGET_MS_PER_POLL}\n" +
                "off_poll_persist_drain_ms_both_indices=$perDrain\n" +
                "off_poll_persist_drain_ms_max=${perDrain.max()}\n" +
                "note=the poll thread pays only the submit figure; the drain is off-poll and no " +
                "poll waits for it. Neither figure bounds IO duration on a phone.\n" +
                "records_written=$writes\n" +
                "session_bytes_NF=$nfBytes\nsession_bytes_BNF=$bnfBytes\n" +
                "global_bytes=$global global_cap=${PremiumCaptureStore.MAX_BYTES_GLOBAL}\n" +
                "bytes_per_slot_per_index=$bytesPerSlot\n" +
                "session_cap=${PremiumCaptureStore.MAX_BYTES_PER_INDEX_SESSION}\n" +
                "pending_queue=BOUNDED_ONE_RUNNING_AT_MOST_ONE_WAITING\n" +
                "worker_max_pending_bytes=${PremiumCaptureWorker.MAX_PENDING_BYTES}\n" +
                "control_reserve_bytes=${PremiumCaptureStore.CONTROL_RESERVE_BYTES}\n" +
                // against the EVIDENCE budget, which is the global cap less the control reserve
                "retention_index_sessions_until_evidence_budget=" +
                "${if (perIndexSession > 0)
                    (PremiumCaptureStore.MAX_BYTES_GLOBAL -
                        PremiumCaptureStore.CONTROL_RESERVE_BYTES) /
                        (perIndexSession / slotsPerIndexSession * 69).coerceAtLeast(1)
                else -1}\n" +
                "retention_note=the global cap bounds total collection; capture stops cleanly, " +
                "nothing is deleted\n")
        assertTrue(out.isFile && out.length() > 0L)
    }

    /**
     * AN EQUAL-SIZE WRITE MUST STILL BREAK THE CONSISTENT-VIEW CLAIM.
     *
     * The old check compared session bytes and record counts before and after assembly, so a
     * replacement that changed content without changing either number was invisible and the view
     * was still labelled consistent. The store now bumps a monotonic generation on every
     * mutation, which is what this asserts: same byte count, same record count, different content
     * — and the view must refuse to call itself consistent.
     */
    @Test
    fun anEqualSizeConcurrentWriteStillBreaksTheConsistentViewClaim() {
        val fd = capturedTwoSlots()
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        val countBefore = store.outcomeCount("NF", "2026-10-19")
        val envBefore = store.envelopeCount("NF", "2026-10-19")
        val genBefore = store.generation()

        // A summary replacement: it changes CONTENT while leaving the record and envelope counts
        // untouched, which is the shape of write the old before/after comparison could not see.
        store.bumpCounters("NF", "2026-10-19", mapOf("coordinator_runs" to 1L))

        assertEquals("the premise: the record count must be unchanged by this write",
            countBefore, store.outcomeCount("NF", "2026-10-19"))
        assertEquals("and so must the envelope count",
            envBefore, store.envelopeCount("NF", "2026-10-19"))
        assertTrue("the generation must advance even when the counts do not: " +
            "$genBefore -> ${store.generation()}", store.generation() > genBefore)

        // and a build that races a write reports an honest, non-consistent view
        val section = PremiumExportSection.build(fd, "2026-10-19")
        val nf = section.getJSONObject("indices").getJSONObject("NF")
        assertEquals("the basis must name the odd/even protocol, not the old counter compare",
            "STORE_REVISION_EVEN_AND_UNCHANGED_ACROSS_READ_ODD_MEANS_WRITE_IN_PROGRESS",
            nf.getString("view_consistency_basis"))
        // with no concurrent writer, a quiet read IS consistent
        assertTrue("a quiet read must still be consistent", nf.getBoolean("view_consistent"))
        assertTrue(nf.getInt("view_attempts") >= 1)
    }

    /**
     * A WRITER PAUSED AFTER RENAME, BEFORE THE REVISION IS PUBLISHED, MUST NOT YIELD A STABLE VIEW.
     *
     * This is the interleaving a post-write counter could not detect, and it is forced
     * deterministically rather than raced: the real store's real write is paused at a real phase
     * hook immediately after `renameTo` makes the new bytes visible, while the export runs.
     *
     *   1. writer replaces a file; the rename is visible
     *   2. writer is paused before publishing its completed revision
     *   3. export reads parts that already include the change
     *   4. under the old protocol it would sample the same counter twice and declare the mixed
     *      view consistent
     *
     * With odd/even it cannot: the revision is odd throughout the paused window, so the reader
     * refuses rather than reporting a stable view.
     */
    @Test
    fun aWriterPausedAfterRenameCannotProduceAStableView() {
        val fd = capturedTwoSlots()
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        assertTrue("the store must be quiescent before the test begins", store.quiescent())
        assertEquals("a quiescent revision must be even", 0L, store.generation() % 2L)

        val paused = java.util.concurrent.CountDownLatch(1)
        val release = java.util.concurrent.CountDownLatch(1)
        PremiumCaptureStore.mutationPhaseHookForTest = { phase ->
            if (phase == "after_rename") {
                paused.countDown()
                release.await()
            }
        }
        val writer = Thread {
            // a real write through the real store: the summary replacement
            store.bumpCounters("NF", "2026-10-19", mapOf("coordinator_runs" to 1L))
        }
        writer.isDaemon = true
        try {
            writer.start()
            assertTrue("the writer must reach the post-rename pause",
                paused.await(15, java.util.concurrent.TimeUnit.SECONDS))

            // the bytes are visible but the revision is NOT published
            assertFalse("a paused mutation must leave the revision odd", store.quiescent())
            assertEquals(1L, store.generation() % 2L)

            val nf = PremiumExportSection.build(fd, "2026-10-19")
                .getJSONObject("indices").getJSONObject("NF")
            assertFalse("a read spanning an unpublished write must NOT be called consistent",
                nf.getBoolean("view_consistent"))
            assertEquals("CONCURRENT_CAPTURE_WRITE_NOT_A_STABLE_VIEW", nf.getString("view_state"))
            assertEquals(PremiumExportSection.CONSISTENCY_ATTEMPTS, nf.getInt("view_attempts"))
        } finally {
            release.countDown()
            writer.join(15_000L)
            PremiumCaptureStore.mutationPhaseHookForTest = null
        }

        // once published, the same read is consistent again — so the refusal above was the
        // protocol working, not the export being permanently unable to produce a stable view
        assertTrue("the revision must be published after the writer completes", store.quiescent())
        val after = PremiumExportSection.build(fd, "2026-10-19")
            .getJSONObject("indices").getJSONObject("NF")
        assertTrue("a quiescent store must read consistently", after.getBoolean("view_consistent"))
    }

    /**
     * TEMP-FILE ACCOUNTING CHANGES BEFORE ANY RENAME, and must also be covered.
     *
     * A write creates a temp file in the destination directory before renaming it, which already
     * changes directory bytes and the orphan-temp enumeration — and on a short write or a refused
     * replace no rename ever completes. So the window after the temp exists must also be odd, not
     * just the window after a successful rename. Paused there, the revision must be odd; after the
     * write finishes it must be even again, whatever the outcome.
     */
    @Test
    fun theRevisionIsAlreadyOddWhileOnlyATempFileExists() {
        val fd = capturedTwoSlots()
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        assertTrue(store.quiescent())
        val paused = java.util.concurrent.CountDownLatch(1)
        val release = java.util.concurrent.CountDownLatch(1)
        PremiumCaptureStore.mutationPhaseHookForTest = { phase ->
            if (phase == "after_temp_write") { paused.countDown(); release.await() }
        }
        val writer = Thread {
            store.bumpCounters("NF", "2026-10-19", mapOf("coordinator_runs" to 1L))
        }
        writer.isDaemon = true
        try {
            writer.start()
            assertTrue("the writer must reach the post-temp-write pause",
                paused.await(15, java.util.concurrent.TimeUnit.SECONDS))
            assertFalse("a temp file already changes accounting, so the revision must be odd",
                store.quiescent())
            // and a reader must refuse this window too, not only the post-rename one
            val nf = PremiumExportSection.build(fd, "2026-10-19")
                .getJSONObject("indices").getJSONObject("NF")
            assertFalse("a read spanning a temp-only mutation must not be called consistent",
                nf.getBoolean("view_consistent"))
        } finally {
            release.countDown()
            writer.join(15_000L)
            PremiumCaptureStore.mutationPhaseHookForTest = null
        }
        assertTrue("the revision must be published once the write finishes", store.quiescent())
    }

    /**
     * A write refused BEFORE it mutates anything must not move the revision at all — otherwise
     * every rejected oversize payload would look like a change to a reader.
     */
    @Test
    fun aWriteRefusedBeforeMutatingAnythingDoesNotMoveTheRevision() {
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        val before = store.generation()
        assertTrue(store.quiescent())
        // over the per-control-file bound, so it is refused before any temp is created
        val r = store.putControlFile("probe.json",
            JSONObject().put("blob", "x".repeat(8192)))
        assertFalse(r.persisted)
        assertEquals(PremiumCaptureStore.REFUSED_RECORD_TOO_LARGE, r.status)
        assertEquals("a refusal that touched nothing must not look like a write",
            before, store.generation())
        assertTrue(store.quiescent())
    }

    /**
     * A writer that keeps mutating during assembly must yield an explicitly INCONSISTENT view
     * rather than a consistent-looking one, and must not be labelled consistent after the
     * bounded attempts are used up.
     */
    @Test
    fun aContinuouslyWritingStoreYieldsAnHonestInconsistentView() {
        val fd = capturedTwoSlots()
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        val stop = java.util.concurrent.atomic.AtomicBoolean(false)
        val writer = Thread {
            var i = 0L
            while (!stop.get()) {
                store.bumpCounters("NF", "2026-10-19", mapOf("churn" to (++i)))
            }
        }
        writer.isDaemon = true
        writer.start()
        try {
            var sawInconsistent = false
            repeat(20) {
                val nf = PremiumExportSection.build(fd, "2026-10-19")
                    .getJSONObject("indices").getJSONObject("NF")
                if (!nf.getBoolean("view_consistent")) {
                    sawInconsistent = true
                    assertEquals("CONCURRENT_CAPTURE_WRITE_NOT_A_STABLE_VIEW",
                        nf.getString("view_state"))
                    assertEquals(PremiumExportSection.CONSISTENCY_ATTEMPTS,
                        nf.getInt("view_attempts"))
                }
            }
            assertTrue("a continuously-written store must be reported as not a stable view",
                sawInconsistent)
        } finally {
            stop.set(true)
            writer.join(10_000L)
        }
    }

    /**
     * CAPTURE ON LEAVES THE POLL'S BRAIN INPUTS BYTE-IDENTICAL TO CAPTURE OFF.
     *
     * The previously-admitted gap: "Control OFF -> unchanged trading result" was asserted
     * structurally and never measured. This measures the part that is measurable here, and the
     * handoff states what it does and does not establish.
     *
     * What is compared is the thing the brain is actually handed: the chain response objects. The
     * same objects are run through capture OFF and then ON, and their serialized bytes must be
     * identical afterwards — so whatever capture did, the inputs to the trading path did not
     * move. Capture freezes to text before reading a field precisely so this holds.
     *
     * What this does NOT establish: that `brain.analyze` produces an identical verdict, which
     * needs the Python engine. That half is covered separately by a Python guard asserting no
     * decision path can even reach capture output. Neither is a device observation.
     */
    @Test
    fun captureOnLeavesThePollsBrainInputsByteIdenticalToCaptureOff() {
        val nfChain = rawFixture("mon_0920_entry_NF").getJSONObject("raw")
        val bnfChain = rawFixture("mon_0920_entry_BNF").getJSONObject("raw")
        // exact bytes of what the poll will hand onward, captured before anything runs
        val nfBefore = nfChain.toString()
        val bnfBefore = bnfChain.toString()

        // (1) capture OFF: the control is absent, so nothing may happen at all
        val off = tmpFilesDir()
        assertTrue(PremiumCaptureControl.awaitInitialized(off, 10_000L))
        val offResult = PremiumCaptureCoordinator.onChainsAcquired(off, "2026-10-19", "09:20",
            PremiumCapturePollAdapter.acquisitions(listOf(
                PremiumCapturePollAdapter.IndexResult("NF", nfChain, 1_000L,
                    istMillis("2026-10-19", 9, 20), "NSE_INDEX|Nifty 50", "2026-10-27"),
                PremiumCapturePollAdapter.IndexResult("BNF", bnfChain, 1_000L,
                    istMillis("2026-10-19", 9, 20), "NSE_INDEX|Nifty Bank", "2026-10-27"))))
        assertEquals(PremiumCaptureCoordinator.DISABLED, offResult.getString("status"))
        assertFalse("OFF must not create the capture directory",
            PremiumCaptureControl.root(off).exists())
        assertEquals("the inputs must be untouched with capture OFF", nfBefore, nfChain.toString())
        assertEquals(bnfBefore, bnfChain.toString())

        // (2) capture ON, through the real worker, over the SAME objects
        val on = tmpFilesDir()
        PremiumCaptureControl.setEnabled(on, true)
        val onResult = coordinate(on, "2026-10-19", "09:20",
            PremiumCapturePollAdapter.acquisitions(listOf(
                PremiumCapturePollAdapter.IndexResult("NF", nfChain, 1_000L,
                    istMillis("2026-10-19", 9, 20), "NSE_INDEX|Nifty 50", "2026-10-27"),
                PremiumCapturePollAdapter.IndexResult("BNF", bnfChain, 1_000L,
                    istMillis("2026-10-19", 9, 20), "NSE_INDEX|Nifty Bank", "2026-10-27"))))
        assertEquals(PremiumCaptureCoordinator.SUBMITTED, onResult.getString("status"))
        // it really did work, or this comparison would be vacuous
        val store = PremiumCaptureStore(PremiumCaptureControl.root(on))
        assertEquals(6, store.outcomeCount("NF", "2026-10-19"))
        assertEquals(6, store.outcomeCount("BNF", "2026-10-19"))

        // THE COMPARISON: the brain's inputs are byte-identical after capture ran
        assertEquals("capture must not mutate the chain the trading path reads",
            nfBefore, nfChain.toString())
        assertEquals(bnfBefore, bnfChain.toString())

        // and nothing capture returns is of a type the trading path consumes: the status is a
        // standalone report, not a value threaded into any decision
        assertTrue(onResult.has("status_describes"))
        assertEquals("SUBMISSION_NOT_PERSISTENCE", onResult.getString("status_describes"))

        // the hook must also not assign to any poll variable the brain path later reads
        val src = stripComments(
            File("src/main/java/com/marketradar/app/MarketWatchService.kt").readText())
        // sliced to the quotes abort CONDITION, not to its log message: ending at the message
        // left `if (quotesJson == null)` inside the slice
        val block = src.substringAfter("premiumCaptureSafely {")
            .substringBefore("if (quotesJson == null)")
        for (owned in listOf("quotesJson", "bnfChainJson", "nfChainJson",
            "bnfStocksJson", "nf50Breadth", "spotProvenance", "x1NfChainFetchedAtMs",
            "x1BnfChainFetchedAtMs", "evNfChainFetchedAtMs")) {
            // ASSIGNMENT only: `=` not followed by `=`. The first version banned the bare string
            // "quotesJson =", which also matches the comparison `quotesJson == null` — so the
            // test failed on code that reads a variable without writing it. Reading an input is
            // exactly what the hook is supposed to do; writing one is what it must never do.
            val assigns = Regex("""\b${Regex.escape(owned)}\s*=(?!=)""").containsMatchIn(block)
            assertFalse("the capture hook must not assign $owned", assigns)
        }
        // the premise: the slice must actually contain the hook, or this proves nothing
        assertTrue("the slice must contain the capture call",
            block.contains("PremiumCaptureCoordinator.onChainsAcquired("))
    }

    // ═════════ R2: the operator control must not block, and must tell the truth ═════════

    /**
     * THE CONTROL MUST RETURN WHILE THE CAPTURE LOCK IS HELD.
     *
     * `setEnabled` writes through the store and therefore waits on the same root lock a blocked
     * persistence write holds. On the Android UI thread that can freeze the app and make Paper
     * unusable — and worker submission being non-blocking says nothing about this path. The
     * production handler the dialog calls is `requestEnabled`, and it must come back promptly with
     * the effective state already changed while the lock is held by someone else.
     */
    @Test
    fun theOperatorControlReturnsPromptlyWhileTheCaptureLockIsHeld() {
        val fd = tmpFilesDir()
        assertTrue(PremiumCaptureControl.setEnabled(fd, true).ok)
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        val holding = java.util.concurrent.CountDownLatch(1)
        val release = java.util.concurrent.CountDownLatch(1)
        val holder = Thread { store.withRootLockForTest { holding.countDown(); release.await() } }
        holder.isDaemon = true
        holder.start()
        try {
            assertTrue(holding.await(10, java.util.concurrent.TimeUnit.SECONDS))
            val t0 = System.nanoTime()
            val r = PremiumCaptureControl.requestEnabled(fd, false)
            val ms = (System.nanoTime() - t0) / 1_000_000L
            assertTrue("the UI-facing control must not wait on the capture lock: took $ms ms",
                ms < 1_000L)
            assertTrue(r.getBoolean("accepted"))
            assertFalse(r.getBoolean("effective_enabled"))
            assertTrue(r.getBoolean("effective_immediately"))
            // OFF gates admission AT ONCE, without waiting for the file write
            assertFalse("OFF must stop admitting capture immediately",
                PremiumCaptureControl.isEnabled(fd))
            // and persistence is reported as pending, not acknowledged as durable
            assertEquals("PENDING", r.getString("persistence"))
            assertEquals("PENDING", PremiumCaptureControl.statusFast(fd)
                .getJSONObject("persistence").getString("state"))
            // the honest statement about what OFF cannot do
            assertTrue(r.getString("note").contains("cannot be cancelled"))
        } finally {
            release.countDown()
            holder.join(10_000L)
        }
        // once the lock is free the write settles, and the state is distinguishable from pending
        val deadline = System.nanoTime() + 10_000_000_000L
        var state = ""
        while (System.nanoTime() < deadline) {
            state = PremiumCaptureControl.statusFast(fd)
                .getJSONObject("persistence").optString("state")
            if (state != "PENDING") break
            Thread.sleep(25)
        }
        assertEquals("the durable outcome must become visible, not stay PENDING forever",
            "PERSISTED", state)
    }

    /**
     * The collection state must reflect what capture is actually doing, including the case a
     * successful `last_run` would otherwise mask: a refused job never runs its closure, so it
     * records nothing, and the operator would keep seeing the old success.
     */
    @Test
    fun theCollectionStateDistinguishesOffFullRefusingAndCollecting() {
        val cap = PremiumCaptureStore.MAX_BYTES_GLOBAL
        val collecting = JSONObject().put("status", "OBSERVED").put("written", 6)
            .put("recorded_at_ms", 1_000L)
        assertEquals("OFF", PremiumCaptureControl.collectionState(false, 0L, collecting))
        assertEquals("ON_COLLECTING", PremiumCaptureControl.collectionState(true, 0L, collecting))
        assertEquals("ON_BUT_STOPPED_STORE_FULL",
            PremiumCaptureControl.collectionState(true, cap, collecting))
        assertEquals("ON_NO_RUN_RECORDED_YET",
            PremiumCaptureControl.collectionState(true, 0L, null))
        assertEquals("ON_LAST_RUN_ERRORED", PremiumCaptureControl.collectionState(true, 0L,
            JSONObject().put("status", "CAPTURE_ERROR")))
        // an unknown byte total must not be read as an empty store
        assertEquals("ON_COLLECTING", PremiumCaptureControl.collectionState(true, -1L, collecting))

        // A REFUSAL NEWER THAN THE LAST RUN WINS over a stale success.
        val refusing = JSONObject()
            .put("last_refusal_at_ms", 2_000L)
            .put("last_refusal_reason", PremiumCaptureWorker.REFUSED_BUSY)
        assertEquals("ON_BUT_REFUSING_" + PremiumCaptureWorker.REFUSED_BUSY,
            PremiumCaptureControl.collectionState(true, 0L, collecting, refusing))
        // an OLDER refusal does not override a newer successful run
        val staleRefusal = JSONObject().put("last_refusal_at_ms", 500L)
            .put("last_refusal_reason", PremiumCaptureWorker.REFUSED_BUSY)
        assertEquals("ON_COLLECTING",
            PremiumCaptureControl.collectionState(true, 0L, collecting, staleRefusal))
        // saturation is visible even with no refusal yet
        val saturated = JSONObject().put("running", true).put("waiting", true)
        assertEquals("ON_WORKER_SATURATED",
            PremiumCaptureControl.collectionState(true, 0L, collecting, saturated))
    }

    /** Worker refusals are counted and the latest reason is reported, with its durability stated. */
    @Test
    fun workerRefusalsAreCountedAndReportedWithoutATouchOfDisk() {
        PremiumCaptureWorker.resetForTest()
        assertEquals(0L, PremiumCaptureWorker.stats().getLong("refused_total"))
        val s = PremiumCaptureWorker.submit(PremiumCaptureWorker.Job(
            index = "NF", sessionDate = "2026-10-19",
            preparedBytes = PremiumCaptureWorker.MAX_PREPARED_BYTES_PER_JOB + 1,
            persist = { JSONObject().put("status", "SHOULD_NOT_RUN") }))
        assertEquals(PremiumCaptureWorker.REFUSED_TOO_LARGE, s)
        val st = PremiumCaptureWorker.stats()
        assertEquals("a refused job records nothing on disk, so this counter is its only trace",
            1L, st.getLong("refused_total"))
        assertEquals(PremiumCaptureWorker.REFUSED_TOO_LARGE, st.getString("last_refusal_reason"))
        assertEquals(1L, st.getJSONObject("refused_by_reason")
            .getLong(PremiumCaptureWorker.REFUSED_TOO_LARGE))
        assertTrue(st.getLong("last_refusal_at_ms") > 0L)
        assertEquals("IN_MEMORY_ONLY_NOT_DURABLE", st.getString("refusals_are_recorded"))
    }

    /**
     * AN OLDER ON COMPLETING MUST NOT REACTIVATE CAPTURE AFTER A NEWER OFF.
     *
     * The defect: a successful disk write re-assigned effective intent, so
     *   1. ON is requested and its save blocks on the store lock
     *   2. OFF is requested; effective intent becomes false
     *   3. the ON save completes and set intent back to TRUE
     *   4. capture could be admitted again despite the newer explicit OFF
     *
     * Forced deterministically by holding the real store root lock so the ON save cannot finish
     * until OFF has been requested.
     */
    @Test
    fun anOlderOnCompletingCannotReactivateCaptureAfterANewerOff() {
        val fd = tmpFilesDir()
        assertTrue(PremiumCaptureControl.awaitInitialized(fd, 10_000L))
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        val holding = java.util.concurrent.CountDownLatch(1)
        val release = java.util.concurrent.CountDownLatch(1)
        val holder = Thread { store.withRootLockForTest { holding.countDown(); release.await() } }
        holder.isDaemon = true
        holder.start()
        try {
            assertTrue(holding.await(10, java.util.concurrent.TimeUnit.SECONDS))

            // (1) ON requested; its save is now stuck behind the held lock
            val on = PremiumCaptureControl.requestEnabled(fd, true)
            assertTrue(PremiumCaptureControl.isEnabled(fd))

            // (2) OFF requested while the ON save is still blocked
            val off = PremiumCaptureControl.requestEnabled(fd, false)
            assertTrue("the newer request must carry a higher sequence",
                off.getLong("seq") > on.getLong("seq"))
            assertFalse("OFF must take effect immediately",
                PremiumCaptureControl.isEnabled(fd))
        } finally {
            release.countDown()
            holder.join(10_000L)
        }

        // (3) the ON save is now free to complete. Effective admission must STAY OFF.
        val deadline = System.nanoTime() + 10_000_000_000L
        while (System.nanoTime() < deadline) {
            assertFalse("no completed save may re-enable capture after a newer OFF",
                PremiumCaptureControl.isEnabled(fd))
            val st = PremiumCaptureControl.statusFast(fd)
            val latest = st.optJSONObject("latest_request")
            if (latest != null && latest.optString("state") != "PENDING" &&
                latest.optBoolean("requested") == false) break
            Thread.sleep(20)
        }
        assertFalse("still OFF after everything settled", PremiumCaptureControl.isEnabled(fd))

        val st = PremiumCaptureControl.statusFast(fd)
        // the displayed latest-request state belongs to the OFF request, not the older ON
        val latest = st.getJSONObject("latest_request")
        assertFalse("the latest request shown must be the OFF one", latest.getBoolean("requested"))
        assertEquals(false, st.get("effective_intent"))
        // the durable value is reported separately rather than conflated with intent
        assertTrue(st.has("durable_value"))
        assertTrue(st.has("last_durable_write"))
    }

    /** ON/OFF/ON ordering: the last request wins and is the one displayed. */
    @Test
    fun theLastControlRequestWinsAcrossRepeatedToggles() {
        val fd = tmpFilesDir()
        assertTrue(PremiumCaptureControl.awaitInitialized(fd, 10_000L))
        PremiumCaptureControl.requestEnabled(fd, true)
        PremiumCaptureControl.requestEnabled(fd, false)
        val last = PremiumCaptureControl.requestEnabled(fd, true)
        assertTrue(PremiumCaptureControl.isEnabled(fd))
        val deadline = System.nanoTime() + 10_000_000_000L
        while (System.nanoTime() < deadline) {
            val l = PremiumCaptureControl.statusFast(fd).optJSONObject("latest_request")
            if (l != null && l.optString("state") == "PERSISTED") break
            Thread.sleep(20)
        }
        val st = PremiumCaptureControl.statusFast(fd)
        val latest = st.getJSONObject("latest_request")
        assertEquals("the final request's sequence must be the one displayed",
            last.getLong("seq"), latest.getLong("seq"))
        assertTrue(latest.getBoolean("requested"))
        assertTrue(PremiumCaptureControl.isEnabled(fd))
        assertEquals(true, st.get("durable_value"))
    }

    /**
     * THE CONTROL PERSISTENCE QUEUE IS BOUNDED: one running save plus one coalesced latest
     * desired state, however many toggles arrive.
     *
     * `newSingleThreadExecutor` has an unbounded queue, so every toggle behind a blocked write
     * used to retain another obsolete Runnable. Many alternating requests are issued while the
     * real root lock is held, finishing on OFF.
     */
    @Test
    fun repeatedTogglesUnderAHeldLockStayBoundedAndSettleOnTheLastOne() {
        val fd = tmpFilesDir()
        assertTrue(PremiumCaptureControl.awaitInitialized(fd, 10_000L))
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        val holding = java.util.concurrent.CountDownLatch(1)
        val release = java.util.concurrent.CountDownLatch(1)
        val holder = Thread { store.withRootLockForTest { holding.countDown(); release.await() } }
        holder.isDaemon = true
        holder.start()
        try {
            assertTrue(holding.await(10, java.util.concurrent.TimeUnit.SECONDS))
            var slowest = 0L
            for (i in 0 until 40) {
                val t0 = System.nanoTime()
                PremiumCaptureControl.requestEnabled(fd, i % 2 == 0)
                slowest = maxOf(slowest, (System.nanoTime() - t0) / 1_000_000L)
            }
            // finish on OFF
            PremiumCaptureControl.requestEnabled(fd, false)
            assertTrue("every UI-facing call must stay prompt: slowest $slowest ms",
                slowest < 1_000L)
            assertFalse("OFF must hold in memory even with persistence busy",
                PremiumCaptureControl.isEnabled(fd))
            val q = PremiumCaptureControl.statusFast(fd).getJSONObject("control_queue")
            assertEquals("at most one coalesced pending state may be retained",
                1, q.getInt("max_coalesced_pending"))
            assertEquals("ONE_RUNNING_SAVE_PLUS_ONE_COALESCED_LATEST_DESIRED_STATE",
                q.getString("policy"))
            assertTrue(q.getLong("latest_seq") >= 41L)
        } finally {
            release.countDown()
            holder.join(10_000L)
        }
        // the LATEST state eventually persists, and nothing re-enabled capture on the way
        val deadline = System.nanoTime() + 15_000_000_000L
        while (System.nanoTime() < deadline) {
            assertFalse("no stale completion may override the final OFF",
                PremiumCaptureControl.isEnabled(fd))
            if (PremiumCaptureControl.statusFast(fd).getJSONObject("control_queue")
                    .getBoolean("saving").not()) break
            Thread.sleep(20)
        }
        assertFalse(PremiumCaptureControl.isEnabled(fd))
        assertEquals(false, PremiumCaptureControl.statusFast(fd).get("durable_value"))
    }

    /**
     * COLD START IS HONEST: admission is conservative until the persisted control has loaded, an
     * existing ON is picked up, a malformed file is an ERROR that reads as OFF, and a request made
     * during initialization is not overwritten by the load completing.
     */
    @Test
    fun coldStartLoadsThePersistedControlWithoutLyingAboutIt() {
        // (a) an existing ON control at cold start
        val on = tmpFilesDir()
        PremiumCaptureControl.root(on).mkdirs()
        File(PremiumCaptureControl.root(on), "control.json")
            .writeText(JSONObject().put("enabled", true).toString())
        // before the load settles, admission must NOT claim a confirmed OFF
        val early = PremiumCaptureControl.statusFast(on)
        if (!early.getBoolean("enabled_known")) {
            assertEquals(PremiumCaptureControl.ADMIT_INITIALIZING,
                early.getJSONObject("admission").getString("reason"))
            assertFalse("conservative until loaded", early.getBoolean("enabled"))
        }
        assertTrue(PremiumCaptureControl.awaitInitialized(on, 10_000L))
        assertEquals(PremiumCaptureControl.INIT_READY,
            PremiumCaptureControl.statusFast(on).getString("control_init"))
        assertTrue("an existing ON control must be picked up",
            PremiumCaptureControl.isEnabled(on))

        // (b) a malformed control is ERROR and reads as OFF
        val bad = tmpFilesDir()
        PremiumCaptureControl.root(bad).mkdirs()
        File(PremiumCaptureControl.root(bad), "control.json").writeText("{not json")
        assertTrue(PremiumCaptureControl.awaitInitialized(bad, 10_000L))
        assertEquals(PremiumCaptureControl.INIT_ERROR,
            PremiumCaptureControl.statusFast(bad).getString("control_init"))
        assertFalse("a capture that cannot read its own switch must not run",
            PremiumCaptureControl.isEnabled(bad))
        assertTrue(PremiumCaptureControl.controlUnreadable(bad))

        // (c) a request made during initialization must win over the load
        val racing = tmpFilesDir()
        PremiumCaptureControl.root(racing).mkdirs()
        File(PremiumCaptureControl.root(racing), "control.json")
            .writeText(JSONObject().put("enabled", true).toString())
        PremiumCaptureControl.requestEnabled(racing, false)   // explicit OFF before/while loading
        assertTrue(PremiumCaptureControl.awaitInitialized(racing, 10_000L))
        assertFalse("initialization must not overwrite a newer explicit request",
            PremiumCaptureControl.isEnabled(racing))
        assertEquals(false, PremiumCaptureControl.statusFast(racing).get("effective_intent"))
    }

    /**
     * THE IN-MEMORY CONTROL STATE IS PER CAPTURE ROOT, not per process.
     *
     * The first version of the non-blocking control held `intent`/`cachedFile` in process-global
     * fields. A real app has exactly one `filesDir`, so production would have behaved correctly —
     * but the abstraction was wrong: `isEnabled(someOtherDir)` answered from whichever directory
     * had last been touched. CI caught it across five tests at once; this pins it so the same
     * shortcut cannot return unnoticed.
     */
    @Test
    fun theInMemoryControlStateIsKeyedByCaptureRootNotProcessGlobal() {
        val a = tmpFilesDir()
        val b = tmpFilesDir()
        assertFalse("both must start OFF", PremiumCaptureControl.isEnabled(a))
        assertFalse(PremiumCaptureControl.isEnabled(b))

        // turn A on through the non-blocking path, which sets the in-memory admission gate
        assertTrue(PremiumCaptureControl.requestEnabled(a, true).getBoolean("accepted"))
        assertTrue(PremiumCaptureControl.isEnabled(a))
        assertFalse("enabling one directory must not enable another",
            PremiumCaptureControl.isEnabled(b))

        // the status surfaces must not bleed either
        assertTrue(PremiumCaptureControl.statusFast(a).getBoolean("enabled"))
        assertFalse(PremiumCaptureControl.statusFast(b).getBoolean("enabled"))
        assertEquals("OFF", PremiumCaptureControl.statusFast(b).getString("collection_state"))

        // and the coordinator must honour the per-root answer
        assertTrue(PremiumCaptureControl.awaitInitialized(b, 10_000L))
        val r = PremiumCaptureCoordinator.onChainsAcquired(b, "2026-10-19", "09:20",
            listOf(acq("NF", "mon_0920_entry_NF")))
        assertEquals(PremiumCaptureCoordinator.DISABLED, r.getString("status"))
        assertFalse("a disabled directory must stay untouched",
            PremiumCaptureControl.root(b).exists())
    }

    /** The fast status touches no files, so the dialog cannot block behind a stuck write. */
    @Test
    fun theFastStatusTouchesNoFilesAndNamesWhatItDoesNotKnow() {
        PremiumCaptureWorker.resetForTest()
        val fd = tmpFilesDir()
        val before = PremiumCaptureControl.root(fd).exists()
        val s = PremiumCaptureControl.statusFast(fd)
        assertEquals("IN_MEMORY_NO_FILESYSTEM_IO", s.getString("source"))
        assertEquals("a status read must not create the capture directory",
            before, PremiumCaptureControl.root(fd).exists())
        assertFalse(s.getBoolean("is_trading_mode"))
        assertFalse(s.getBoolean("changes_paper_authority"))
        assertTrue(s.getBoolean("in_flight_cannot_be_cancelled"))
        assertTrue(s.has("worker"))
    }

    /** The native dialog must RENDER the state, not merely compute it. */
    @Test
    fun theNativeDialogRendersTheCaptureStateAndUsesTheNonBlockingControl() {
        val src = stripComments(
            File("src/main/java/com/marketradar/app/MainActivity.kt").readText())
        assertTrue("the dialog must render the capture status",
            src.contains("premiumCaptureStatusText()"))
        assertTrue("and that text must come from the non-blocking status",
            src.contains("PremiumCaptureControl.statusFast("))
        val dialog = src.substringAfter("private fun showVersionDialog()")
            .substringBefore("private fun premiumCaptureStatusText()")
        assertTrue("the toggle must use the non-blocking request",
            dialog.contains("PremiumCaptureControl.requestEnabled("))
        assertFalse("the UI thread must not call the store-writing setEnabled",
            dialog.contains("PremiumCaptureControl.setEnabled("))
        // the rendered text must carry the operational facts, not just ON/OFF
        val render = src.substringAfter("private fun premiumCaptureStatusText()")
            .substringBefore("private fun premiumCaptureButtonLabel()")
        for (field in listOf("collection_state", "evidence_bytes_remaining", "refused_total",
            "last_refusal_reason", "persistence", "last_run")) {
            assertTrue("the dialog must show $field", render.contains(field))
        }
    }

    // ═════════ R3: the capture summary's own write outcome ═════════

    /**
     * The CAPTURE summary and the coordinator's own counter are reported separately, and the
     * coordinator's success cannot stand in for the capture summary's failure.
     */
    @Test
    fun theCaptureSummaryWriteIsReportedSeparatelyFromTheCoordinatorCounter() {
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        val r = coordinate(fd, "2026-10-19", "09:20", listOf(acq("NF", "mon_0920_entry_NF")))
        val row = r.getJSONArray("indices").getJSONObject(0)

        val summary = row.getJSONObject("summary_write")
        assertEquals("CAPTURE_BATCH_WRITE_AND_REFUSAL_COUNTERS", summary.getString("kind"))
        assertTrue("the capture summary must be durably recorded on a healthy store",
            summary.getBoolean("durably_recorded"))

        val counter = row.getJSONObject("coordinator_counter_write")
        assertEquals("COORDINATOR_RUN_COUNT_ONLY_NOT_THE_CAPTURE_SUMMARY",
            counter.getString("kind"))
        assertNotEquals("the two must be distinguishable, not one field reused",
            summary.toString(), counter.toString())
    }

    /**
     * THE MIXED OUTCOME, EXECUTED: capture summary FAILS, coordinator counter SUCCEEDS — and the
     * reverse.
     *
     * This is the case that proves one cannot mask the other, and my earlier version of this test
     * did not run it: it checked a healthy session and then a wholly full root, neither of which
     * forces the two writes to disagree. Forced here by refusing exactly the FIRST summary write
     * of the pair through the store's injector, then exactly the SECOND.
     */
    @Test
    fun aFailingCaptureSummaryAndASucceedingCoordinatorCounterStayDistinguishable() {
        for (failFirst in listOf(true, false)) {
            val fd = tmpFilesDir()
            PremiumCaptureControl.setEnabled(fd, true)
            val seen = java.util.concurrent.atomic.AtomicInteger(0)
            PremiumCaptureStore.writeRefusalInjectorForTest = { kind ->
                if (kind != PremiumCaptureStore.KIND_SUMMARY) null
                else {
                    val n = seen.incrementAndGet()
                    if ((failFirst && n == 1) || (!failFirst && n == 2))
                        "forced summary failure #$n" else null
                }
            }
            val row = try {
                coordinate(fd, "2026-10-19", "09:20",
                    listOf(acq("NF", "mon_0920_entry_NF")))
                    .getJSONArray("indices").getJSONObject(0)
            } finally {
                PremiumCaptureStore.writeRefusalInjectorForTest = null
            }
            val capture = row.getJSONObject("summary_write")
            val counter = row.getJSONObject("coordinator_counter_write")
            assertEquals("CAPTURE_BATCH_WRITE_AND_REFUSAL_COUNTERS", capture.getString("kind"))
            assertEquals("COORDINATOR_RUN_COUNT_ONLY_NOT_THE_CAPTURE_SUMMARY",
                counter.getString("kind"))
            assertEquals("exactly two summary writes must have been attempted", 2, seen.get())
            if (failFirst) {
                assertFalse("the CAPTURE summary failed and must be reported as failed",
                    capture.getBoolean("durably_recorded"))
                assertTrue("the coordinator counter succeeded and says so, separately",
                    counter.getBoolean("durably_recorded"))
            } else {
                assertTrue("the capture summary succeeded",
                    capture.getBoolean("durably_recorded"))
                assertFalse("the coordinator counter failed, and that is not the capture summary",
                    counter.getBoolean("durably_recorded"))
            }
        }
    }

    /**
     * A healthy session returns a persisted capture summary; a full root returns a refused one.
     *
     * Kept as the coarse case. The mixed-outcome case above is what establishes that the two
     * cannot mask each other, which this one does not force.
     */
    @Test
    fun aFailedCaptureSummaryIsNeverMaskedByTheCoordinatorCounter() {
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        // the observeSlot path must still report ITS OWN summary outcome
        val session = PremiumCaptureSession(store)
        // observe once so a cohort exists and the batch path is reached
        val first = session.observeSlot(
            index = "NF", sessionDate = "2026-10-19",
            rawChain = rawFixture("mon_0920_entry_NF").getJSONObject("raw"),
            acquisitionHhmm = "09:20", schedulerSlot = null, declaredSchedulerSlot = null,
            requestedUnderlyingKey = PremiumCaptureCohort.INDEX_UNDERLYING["NF"],
            requestStartedMs = 1_000L, responseReceivedMs = 1_300L, vendorSourceTs = null,
            claimedLot = null, claimedLotSource = null, lotTableRef = null, mapRef = null)
        assertEquals(PremiumCaptureSession.OBSERVED, first.status)
        assertNotNull("the batch path must return its OWN summary write outcome",
            first.summaryWrite)
        assertTrue(first.summaryWrite!!.persisted)

        // now fill the root so the NEXT attempt's summary is refused, and check the refusal is
        // reported rather than replaced by an unrelated success
        fillFile(File(PremiumCaptureControl.root(fd), "filler.bin"),
            PremiumCaptureStore.MAX_BYTES_GLOBAL)
        val refused = session.observeSlot(
            index = "NF", sessionDate = "2026-10-19",
            rawChain = rawFixture("mon_0920_entry_NF").getJSONObject("raw"),
            acquisitionHhmm = "09:25", schedulerSlot = null, declaredSchedulerSlot = null,
            requestedUnderlyingKey = PremiumCaptureCohort.INDEX_UNDERLYING["NF"],
            requestStartedMs = 2_000L, responseReceivedMs = 2_300L, vendorSourceTs = null,
            claimedLot = null, claimedLotSource = null, lotTableRef = null, mapRef = null)
        assertNotNull("even a refused attempt must report its own summary outcome",
            refused.summaryWrite)
        assertFalse("a full store cannot durably record the counters it just refused",
            refused.summaryWrite!!.persisted)
    }

    // ═════════ Production lot authority: coverage must be COMPLETE and in context ═════════

    private fun authorityBlock(fd: File, idx: String = "NF", date: String = "2026-10-19") =
        PremiumExportSection.build(fd, date).getJSONObject("indices").getJSONObject(idx)
            .getJSONObject("references").getJSONObject("production_lot_authority")

    private fun capturedTwoSlots(): File {
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        for (slot in listOf("09:20", "09:25"))
            coordinate(fd, "2026-10-19", slot, listOf(acq("NF", "mon_0920_entry_NF",
                receipt = slot)))
        return fd
    }

    /**
     * ONE UNREFERENCED ENVELOPE DISQUALIFIES THE WHOLE-SESSION CLAIM.
     *
     * The old predicate did `mapNotNull { production_authority_ref }` and then asked whether all
     * REMAINING distinct references matched. An envelope carrying no reference at all was dropped
     * from the set, so one referenced envelope plus one unreferenced envelope satisfied a
     * document-level authority claim. Partial coverage is now reported and disqualifying.
     */
    @Test
    fun oneEnvelopeWithoutAnAuthorityReferenceBreaksTheWholeSessionClaim() {
        val fd = capturedTwoSlots()
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        val before = authorityBlock(fd)
        assertEquals(2, before.getJSONObject("coverage").getInt("envelopes"))

        // strip the authority reference from exactly ONE envelope, re-stamping it so it stays
        // individually hash-valid: only the coverage rule can catch this
        val envs = File(store.root, "2026-10-19/NF/envelopes").listFiles()!!
            .filter { it.name.endsWith(".json") }.sortedBy { it.name }
        assertEquals(2, envs.size)
        rewriteStamped(envs[0]) { it.getJSONObject("lot").remove("production_authority_ref") }

        val pa = authorityBlock(fd)
        val cov = pa.getJSONObject("coverage")
        assertEquals(2, cov.getInt("envelopes"))
        assertEquals(1, cov.getInt("matched"))
        assertEquals("the uncovered envelope must be COUNTED, not dropped",
            1, cov.getInt("missing_reference"))
        assertFalse(cov.getBoolean("complete"))
        assertFalse("partial coverage must not satisfy the authority predicate",
            pa.getBoolean("authority_holds"))
        // and it is named per acquisition rather than only summarised
        val per = cov.getJSONArray("per_acquisition")
        assertEquals(2, per.length())
        val states = (0 until per.length()).map { per.getJSONObject(it).getString("state") }
        assertTrue(states.contains("NO_AUTHORITY_REFERENCE_RECORDED"))
        // top-level and detailed claims agree
        val claims = PremiumExportSection.build(fd, "2026-10-19")
            .getJSONObject("indices").getJSONObject("NF").getJSONObject("claims")
        assertEquals(pa.getBoolean("authority_holds"),
            claims.getBoolean("authoritative_lot_resolved"))
    }

    /**
     * The authority record is resolved from the REQUEST's expiry before any cohort is selected,
     * and it is first-write-wins — so a record written for one expiry must not grant authority to
     * a cohort that pinned a different one.
     */
    @Test
    fun anAuthorityRecordedForADifferentExpiryDoesNotGrantAuthority() {
        val fd = capturedTwoSlots()
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        val rec = store.readLotAuthority("NF", "2026-10-19")!!
        // the pinned cohort's expiry is the fixture's; claim the authority was for another
        File(store.root, "2026-10-19/NF/lot_authority.json")
            .writeText(JSONObject(rec.toString()).put("requested_expiry", "2026-12-31").toString())
        val pa = authorityBlock(fd)
        assertTrue("the record is still present", pa.getBoolean("recorded"))
        assertFalse(pa.getBoolean("authority_holds"))
        assertFalse(pa.getJSONObject("pinned_context").getBoolean("agrees"))
        assertTrue(pa.getJSONObject("pinned_context").getJSONArray("reasons").toString()
            .contains("authority_expiry_differs_from_pinned_cohort"))
    }

    /** A record written for another index or session date cannot grant authority here. */
    @Test
    fun anAuthorityRecordedForAnotherIndexOrSessionDoesNotGrantAuthority() {
        for ((field, value, reason) in listOf(
            Triple("requested_index", "BNF", "authority_index_differs"),
            Triple("requested_session_date", "2026-10-20", "authority_session_differs"))) {
            val fd = capturedTwoSlots()
            val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
            val rec = store.readLotAuthority("NF", "2026-10-19")!!
            File(store.root, "2026-10-19/NF/lot_authority.json")
                .writeText(JSONObject(rec.toString()).put(field, value).toString())
            val pa = authorityBlock(fd)
            assertFalse("$field=$value must not hold authority", pa.getBoolean("authority_holds"))
            assertTrue(pa.getJSONObject("pinned_context").getJSONArray("reasons").toString()
                .contains(reason))
        }
    }

    /** A declared lot conflict, or a stale table version, is not authority either. */
    @Test
    fun aConflictingOrStaleAuthorityRecordIsNotAuthority() {
        val fdA = capturedTwoSlots()
        val storeA = PremiumCaptureStore(PremiumCaptureControl.root(fdA))
        val recA = storeA.readLotAuthority("NF", "2026-10-19")!!
        File(storeA.root, "2026-10-19/NF/lot_authority.json")
            .writeText(JSONObject(recA.toString()).put("lot_conflict", true).toString())
        val paA = authorityBlock(fdA)
        assertTrue(paA.getBoolean("lot_conflict"))
        assertFalse(paA.getBoolean("authority_holds"))

        val fdB = capturedTwoSlots()
        val storeB = PremiumCaptureStore(PremiumCaptureControl.root(fdB))
        val recB = storeB.readLotAuthority("NF", "2026-10-19")!!
        File(storeB.root, "2026-10-19/NF/lot_authority.json")
            .writeText(JSONObject(recB.toString())
                .put("lot_table_version", "some_other_version").toString())
        val paB = authorityBlock(fdB)
        assertFalse(paB.getBoolean("authority_holds"))
        assertTrue(paB.getJSONObject("pinned_context").getJSONArray("reasons").toString()
            .contains("authority_table_version_differs_from_current"))
    }

    /** A corrupt or absent authority record is reported, and claims nothing. */
    @Test
    fun aCorruptOrAbsentAuthorityRecordClaimsNothingAndSaysSo() {
        val fd = capturedTwoSlots()
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        File(store.root, "2026-10-19/NF/lot_authority.json").writeText("{not json")
        val pa = authorityBlock(fd)
        assertFalse("an unparseable record is not a record", pa.getBoolean("recorded"))
        assertFalse(pa.getBoolean("authority_holds"))
        // the neutral block label, instead of a constant `true` asserting a property of nothing
        assertEquals("NO_AUTHORITY_RECORD_PRESENT", pa.getString("block_kind"))
        assertTrue("the old constant must no longer assert a property of an absent record",
            pa.isNull("is_the_applications_own_contract_table"))
        val cov = pa.getJSONObject("coverage")
        assertEquals(2, cov.getInt("missing_reference"))
        assertFalse(cov.getBoolean("complete"))
    }

    /**
     * The fully matching case, so these tests are not all negative: every envelope covered, the
     * context agreeing, and the top-level claim equal to the detailed verdict either way.
     */
    @Test
    fun theFullyMatchingAuthorityCaseAgreesAtEveryLevel() {
        val fd = capturedTwoSlots()
        val pa = authorityBlock(fd)
        val cov = pa.getJSONObject("coverage")
        assertTrue(pa.getBoolean("recorded"))
        assertEquals("THE_APPLICATIONS_OWN_CONTRACT_LOT_TABLE", pa.getString("block_kind"))
        assertEquals(cov.getInt("envelopes"), cov.getInt("matched"))
        assertEquals(0, cov.getInt("missing_reference"))
        assertEquals(0, cov.getInt("reference_mismatched"))
        assertTrue("coverage must be complete in the matching case", cov.getBoolean("complete"))
        assertTrue(pa.getJSONObject("pinned_context").getBoolean("agrees"))
        assertEquals(0, pa.getJSONObject("pinned_context").getJSONArray("reasons").length())
        // Asserted as AGREEMENT, not as a fixed outcome: whether ContractLotTable grants
        // authority for this expiry is its decision, and this must hold either way.
        val expected = pa.getBoolean("recorded") && pa.getBoolean("resolved") &&
            pa.getBoolean("authoritative") && !pa.getBoolean("lot_conflict") &&
            cov.getBoolean("complete") && pa.getJSONObject("pinned_context").getBoolean("agrees")
        assertEquals(expected, pa.getBoolean("authority_holds"))
        val claims = PremiumExportSection.build(fd, "2026-10-19")
            .getJSONObject("indices").getJSONObject("NF").getJSONObject("claims")
        assertEquals(expected, claims.getBoolean("authoritative_lot_resolved"))
    }

    // ═════════ The real service adapter: what the poll ACTUALLY passes to capture ═════════

    /** A real IST wall-clock millisecond value for a session date and HH:MM. */
    private fun istMillis(date: String, hour: Int, minute: Int): Long {
        val cal = java.util.Calendar.getInstance(TimeZone.getTimeZone("Asia/Kolkata"))
        val parts = date.split("-").map { it.toInt() }
        cal.clear()
        cal.set(parts[0], parts[1] - 1, parts[2], hour, minute, 0)
        return cal.timeInMillis
    }

    private fun pollResult(index: String, fixture: String?, completedAtMs: Long,
                           startedMs: Long = 1_000L, expiry: String? = "2026-10-27") =
        PremiumCapturePollAdapter.IndexResult(
            index = index,
            chain = fixture?.let { rawFixture(it).getJSONObject("raw") },
            requestStartedMs = startedMs, completedAtMs = completedAtMs,
            underlyingKey = PremiumCaptureCohort.INDEX_UNDERLYING[index],
            expiry = expiry)

    /**
     * A FAILED chain must still carry its real completion clock, so its slot is honest and the six
     * per-contract missing outcomes can be written once a cohort exists.
     *
     * This is the integration fault a passing JVM test hid: the old failed-chain test supplied a
     * synthetic completion time and receipt HH:MM that the service never supplies on that path,
     * because `x1*ChainFetchedAtMs` is assigned only when the response is non-null. With no clock
     * and no scheduler slot, `observeSlot` could only answer NO_SLOT_IDENTITY. Asserted here on
     * the values the adapter really produces.
     */
    @Test
    fun theAdapterGivesAFailedChainItsRealCompletionClock() {
        val completed = istMillis("2026-10-19", 9, 25)
        val acqs = PremiumCapturePollAdapter.acquisitions(listOf(
            pollResult("NF", null, completed),
            pollResult("BNF", "mon_0920_entry_BNF", completed)))
        val nf = acqs.first { it.index == "NF" }
        assertNull("a failed chain stays absent; nothing is invented for it", nf.rawChain)
        assertEquals("09:25", nf.receiptHhmm)
        assertEquals(completed, nf.responseReceivedMs)
        // and a clock that never ran is unknown, never 0
        val never = PremiumCapturePollAdapter.acquisitions(listOf(
            pollResult("NF", null, 0L, startedMs = 0L))).first()
        assertNull(never.receiptHhmm)
        assertNull(never.responseReceivedMs)
        assertNull(never.requestStartedMs)
    }

    /**
     * The failed chain reaches PERSISTED missing-contract outcomes once a cohort exists, through
     * the real adapter and the real coordinator — not through hand-written arguments.
     */
    @Test
    fun aFailedChainFromTheRealAdapterStillRecordsSixMissingOutcomes() {
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        // slot 1 succeeds for both indices, pinning each cohort
        coordinate(fd, "2026-10-19", "09:20", PremiumCapturePollAdapter.acquisitions(listOf(
            pollResult("NF", "mon_0920_entry_NF", istMillis("2026-10-19", 9, 20)),
            pollResult("BNF", "mon_0920_entry_BNF", istMillis("2026-10-19", 9, 20)))))
        // slot 2: NF's chain FAILS, BNF succeeds
        val r = coordinate(fd, "2026-10-19", "09:25", PremiumCapturePollAdapter.acquisitions(listOf(
            pollResult("NF", null, istMillis("2026-10-19", 9, 25)),
            pollResult("BNF", "mon_0920_entry_BNF", istMillis("2026-10-19", 9, 25)))))
        val nf = (0 until r.getJSONArray("indices").length())
            .map { r.getJSONArray("indices").getJSONObject(it) }
            .first { it.getString("index") == "NF" }
        assertFalse("the chain really was absent", nf.getBoolean("chain_present"))
        assertEquals("the gap must be recorded, not skipped for want of a clock",
            6, nf.getInt("written"))
        // and BNF is unaffected: no cross-index fallback either way
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        assertEquals(12, store.outcomeCount("NF", "2026-10-19"))
        assertEquals(12, store.outcomeCount("BNF", "2026-10-19"))
    }

    /** Both chains failing is still recorded per index, with no cross-index borrowing. */
    @Test
    fun bothChainsFailingIsRecordedForEachIndexSeparately() {
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        coordinate(fd, "2026-10-19", "09:20", PremiumCapturePollAdapter.acquisitions(listOf(
            pollResult("NF", "mon_0920_entry_NF", istMillis("2026-10-19", 9, 20)),
            pollResult("BNF", "mon_0920_entry_BNF", istMillis("2026-10-19", 9, 20)))))
        val r = coordinate(fd, "2026-10-19", "09:25", PremiumCapturePollAdapter.acquisitions(listOf(
            pollResult("NF", null, istMillis("2026-10-19", 9, 25)),
            pollResult("BNF", null, istMillis("2026-10-19", 9, 25)))))
        val rows = r.getJSONArray("indices")
        assertEquals(2, rows.length())
        for (i in 0 until rows.length()) {
            val row = rows.getJSONObject(i)
            assertFalse(row.getBoolean("chain_present"))
            assertEquals("each index records its own gap", 6, row.getInt("written"))
        }
    }

    /**
     * The scheduler's intended slot is derived from the service's own `slotKey` format, and is
     * kept beside the acquisition clock rather than relabelling it.
     */
    @Test
    fun theDeclaredSlotComesFromTheServicesOwnSlotKeyFormat() {
        assertEquals("09:15", PremiumCapturePollAdapter.declaredSlotFromSlotKey("2026-10-19|1"))
        assertEquals("09:20", PremiumCapturePollAdapter.declaredSlotFromSlotKey("2026-10-19|2"))
        assertEquals("15:10", PremiumCapturePollAdapter.declaredSlotFromSlotKey("2026-10-19|72"))
        for (bad in listOf(null, "", "closed", "2026-10-19|0", "2026-10-19|x", "2026-10-19|9999"))
            assertNull("a slot key of '$bad' must not produce a slot",
                PremiumCapturePollAdapter.declaredSlotFromSlotKey(bad))
        // the adapter's grid constants must match the service's, or every slot would be offset
        val svc = stripComments(
            File("src/main/java/com/marketradar/app/MarketWatchService.kt").readText())
        assertTrue(svc.contains("MARKET_OPEN_MINUTE = 9 * 60 + 15"))
        assertTrue(svc.contains("POLL_SLOT_MINUTES = 5"))
        assertEquals(9 * 60 + 15, PremiumCapturePollAdapter.MARKET_OPEN_MINUTE)
        assertEquals(5, PremiumCapturePollAdapter.POLL_SLOT_MINUTES)
    }

    // ═════════════════ Commit 3 — the premium section on the existing export ═════════════════

    /**
     * Submit through the REAL coordinator, then wait for the off-poll worker to drain.
     *
     * Persistence is no longer synchronous: the coordinator prepares on the caller and hands the
     * work to [PremiumCaptureWorker], so a test asserting on files immediately after submission
     * would be racing the worker. The wait is bounded and its result is asserted, so a stuck
     * worker fails the test rather than producing a flake. Tests about SUBMISSION rather than
     * persistence call the coordinator directly instead of using this.
     */
    private fun coordinate(fd: File, date: String?, slot: String?,
                           acqs: List<PremiumCaptureCoordinator.ChainAcquisition>,
                           prepareBudgetMs: Long =
                               PremiumCaptureCoordinator.PREPARE_BUDGET_MS_PER_POLL): JSONObject {
        PremiumCaptureWorker.resetForTest()
        val r = PremiumCaptureCoordinator.onChainsAcquired(fd, date, slot, acqs, prepareBudgetMs)
        assertTrue("the capture worker must drain inside the test bound",
            PremiumCaptureWorker.awaitIdle(30_000L))
        // Replace each submission row with the PERSISTED row for that index, keeping the
        // submission status beside it. The coordinator can only report submission now, so a test
        // that wants persistence has to read what the worker actually did — this reassembles it
        // rather than letting the assertions quietly weaken to "it was accepted".
        val rows = r.optJSONArray("indices") ?: return r
        val merged = JSONArray()
        for (i in 0 until rows.length()) {
            val sub = rows.getJSONObject(i)
            val idx = sub.optString("index")
            val persisted = PremiumCaptureWorker.resultFor(idx)
            merged.put(if (persisted != null)
                JSONObject(persisted.toString()).put("submission", sub.optString("status"))
            else sub)
        }
        return r.put("indices", merged)
    }

    /** Capture `slots` slots for both indices into a fresh filesDir, and return it. */
    private fun capturedFilesDir(slots: List<String>, date: String = "2026-10-19"): File {
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        for ((i, slot) in slots.withIndex()) {
            coordinate(fd, date, slot,
                listOf(acq("NF", "mon_0920_entry_NF", receipt = slot,
                    start = 1_000L + i * 300_000L, end = 1_300L + i * 300_000L),
                    acq("BNF", "mon_0920_entry_BNF", receipt = slot,
                        start = 1_000L + i * 300_000L, end = 1_320L + i * 300_000L)))
        }
        return fd
    }

    @Test
    fun theSectionCarriesNfAndBnfSeparatelyForTheRequestedDateOnly() {
        val fd = capturedFilesDir(listOf("09:20", "09:25"))
        val sec = PremiumExportSection.build(fd, "2026-10-19")
        assertEquals("PRESENT", sec.getString("state"))
        assertTrue(sec.getBoolean("has_data"))
        assertEquals("2026-10-19", sec.getString("requested_session_date"))
        assertEquals("NEVER_ONLY_THE_REQUESTED_DATE", sec.getString("session_date_substitution"))
        val idx = sec.getJSONObject("indices")
        for (i in listOf("NF", "BNF")) {
            val one = idx.getJSONObject(i)
            assertEquals(i, one.getString("index"))
            assertEquals("2026-10-19", one.getString("session_date"))
            assertEquals(12, one.getJSONObject("accounting").getInt("records"))
            assertEquals(2, one.getJSONObject("acquisitions").getInt("verified_batches"))
            assertTrue(one.getBoolean("view_consistent"))
        }
        // a different retained date is NEVER substituted for the requested one
        val other = PremiumExportSection.build(fd, "2026-10-16")
        assertEquals("NO_PREMIUM_DATA", other.getString("state"))
        assertFalse(other.getBoolean("has_data"))
        assertEquals("2026-10-16", other.getString("requested_session_date"))
        // and an invalid requested date is its own state, not today's data
        assertEquals("REQUESTED_DATE_INVALID",
            PremiumExportSection.build(fd, "not-a-date").getString("state"))
    }

    @Test
    fun noPremiumDataIsADeliberateStateNotAnError() {
        // never captured at all: the directory does not even exist
        val fresh = tmpFilesDir()
        val sec = PremiumExportSection.build(fresh, "2026-10-19")
        assertEquals("NO_PREMIUM_DATA", sec.getString("state"))
        assertFalse(sec.getBoolean("has_data"))
        assertFalse(sec.getBoolean("capture_enabled"))
        // one index captured, the other not: each state is explicit
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        coordinate(fd, "2026-10-19", "09:20",
            listOf(acq("NF", "mon_0920_entry_NF")))
        val one = PremiumExportSection.build(fd, "2026-10-19")
        assertEquals("PRESENT", one.getString("state"))
        assertEquals("NO_PREMIUM_DATA_FOR_THIS_INDEX",
            one.getJSONObject("indices").getJSONObject("BNF").getString("state"))
        assertEquals(6, one.getJSONObject("indices").getJSONObject("NF")
            .getJSONObject("accounting").getInt("records"))
    }

    @Test
    fun partialCaptureIsCarriedWithItsVerifiedUsability() {
        val fd = capturedFilesDir(listOf("09:20"))
        // lose two records, as an interrupted write would
        val recs = File(PremiumCaptureControl.root(fd), "2026-10-19/NF/records")
            .listFiles()!!.filter { it.name.endsWith(".json") }.sortedBy { it.name }
        recs.take(2).forEach { assertTrue(it.delete()) }
        val nf = PremiumExportSection.build(fd, "2026-10-19")
            .getJSONObject("indices").getJSONObject("NF")
        val row = nf.getJSONObject("acquisitions").getJSONArray("rows").getJSONObject(0)
        assertEquals("PARTIAL", row.getString("structural_state"))
        assertEquals("UNVERIFIED", row.getString("verified_state"))
        assertEquals(0, nf.getJSONObject("acquisitions").getInt("usable_evidence_acquisitions"))
        // the evidence is still carried, with the missing outcomes visible
        assertEquals(4, nf.getJSONObject("accounting").getInt("records"))
    }

    /** A new top-level key only: every existing part of the bundle survives byte-for-byte. */
    @Test
    fun theSpliceAddsOnlyANewTopLevelKeyAndChangesNoExistingPart() {
        val bundle = JSONObject().put("manifest", JSONObject().put("status", "OK")
            .put("session_date_used", "2026-10-19"))
            .put("trades", JSONArray().put(JSONObject().put("id", 1).put("pnl", 12.5)))
            .put("labels", JSONObject().put("n", 3)).toString()
        val sec = JSONObject().put("state", "PRESENT").put("has_data", true)
        val merged = PremiumExportSection.splice(bundle, sec)
        assertNotNull(merged)
        val before = JSONObject(bundle)
        val after = JSONObject(merged!!)
        assertEquals("PRESENT", after.getJSONObject("premium").getString("state"))
        for (k in before.keys()) {
            assertEquals("existing part '$k' must be unchanged",
                before.get(k).toString(), after.get(k).toString())
        }
        // an empty bundle object must still yield valid JSON
        val emptyMerged = PremiumExportSection.splice("{}", sec)
        assertNotNull(emptyMerged)
        assertTrue(JSONObject(emptyMerged!!).has("premium"))
        assertEquals(1, JSONObject(emptyMerged).length())
        // a non-object bundle is refused rather than corrupted
        assertNull(PremiumExportSection.splice("[1,2,3]", sec))
        assertNull(PremiumExportSection.splice("not json", sec))
    }

    /**
     * The combined limit is enforced on the FINAL bytes, and the premium section is kept WHOLE or
     * the export is refused.
     *
     * This test used to assert the opposite — that the section is reduced in declared stages until
     * it fits. That behaviour was wrong, not just under-tested: the reduction dropped records,
     * then envelopes, then cohort/references/conflicts, while leaving the verified and usable
     * counts and the PRESENT/has_data claims untouched, so a caller received what looked like an
     * ordinary successful export whose removed groups could no longer be verified offline but were
     * still counted as usable. Evidence is no longer silently downgraded to fit; it either fits or
     * the export says it does not. The limit itself is unchanged and old parts are never touched.
     */
    @Test
    fun theCombinedCapKeepsThePremiumSectionWholeOrRefuses() {
        val fd = capturedFilesDir(listOf("09:20", "09:25", "09:30", "09:35"))
        val sec = PremiumExportSection.build(fd, "2026-10-19")
        val secBytes = sec.toString().toByteArray(Charsets.UTF_8).size
        assertTrue("the section must be substantial for this case to mean anything",
            secBytes > 50_000)
        val trueRecords = 4 * PremiumCaptureCohort.CONTRACTS_PER_COHORT
        assertEquals(trueRecords, sec.getJSONObject("indices").getJSONObject("NF")
            .getJSONObject("accounting").getInt("records"))

        // (a) room for the WHOLE section: it is spliced complete, with nothing dropped
        val roomy = "{\"filler\":\"x\"}"
        val fitA = PremiumExportSection.mergeWithinCap(
            roomy, roomy.toByteArray(Charsets.UTF_8), sec)
        assertFalse(fitA.overCap)
        assertTrue(fitA.merged)
        assertEquals("no reduction may happen on any path", 0, fitA.stage)
        assertTrue(fitA.bytes.size <= PremiumExportSection.COMBINED_CAP_BYTES)
        val mergedA = JSONObject(String(fitA.bytes, Charsets.UTF_8)).getJSONObject("premium")
        assertTrue("the records must still be present, not dropped to fit",
            mergedA.getJSONObject("indices").getJSONObject("NF").has("records"))
        assertEquals(trueRecords, mergedA.getJSONObject("indices").getJSONObject("NF")
            .getJSONObject("accounting").getInt("records"))
        assertEquals("the retained count and the included evidence must agree",
            trueRecords, mergedA.getJSONObject("indices").getJSONObject("NF")
                .getJSONArray("records").length())
        assertFalse("nothing may be declared reduced when nothing was reduced",
            mergedA.has("reduced_fields"))

        // (b) a bundle already at the limit: it must REFUSE, and must NOT shrink the evidence
        val full = "{\"filler\":\"" + "x".repeat(
            PremiumExportSection.COMBINED_CAP_BYTES - 200) + "\"}"
        val fitB = PremiumExportSection.mergeWithinCap(
            full, full.toByteArray(Charsets.UTF_8), sec)
        assertTrue("it must refuse rather than raise the limit or drop evidence", fitB.overCap)
        assertEquals("refusal must not be reached by reducing first", 0, fitB.stage)
        // the section handed in is untouched by the attempt
        assertEquals(trueRecords, sec.getJSONObject("indices").getJSONObject("NF")
            .getJSONObject("accounting").getInt("records"))
    }

    /** A device export states what device evidence does NOT mean. */
    @Test
    fun aDeviceExportDoesNotClaimSynchronisedTicksOrExecutableFills() {
        val ctx = PremiumExportSection.deviceContext().json()
        assertEquals("LIVE_DEVICE_CAPTURE", ctx.getString("source_kind"))
        assertTrue(ctx.getBoolean("device_evidence"))
        assertEquals(0, ctx.getJSONArray("synthetic_fields").length())
        assertTrue("the clock provenance must say the exchange semantics are unverified",
            ctx.getString("clock_provenance").contains("EXCHANGE_EVENT_SEMANTICS_UNVERIFIED"))
        assertTrue(ctx.getString("note").contains("does not mean synchronised exchange ticks"))
        assertTrue(ctx.getString("lot_table_provenance").contains(ContractLotTable.VERSION_ID))

        val fd = capturedFilesDir(listOf("09:20"))
        val nf = PremiumExportSection.build(fd, "2026-10-19")
            .getJSONObject("indices").getJSONObject("NF")
        assertTrue(nf.getBoolean("device_evidence"))
        assertFalse("a device export is still not a synchronised tick",
            nf.getJSONObject("claims").getBoolean("synchronised_exchange_quotes"))
        assertFalse(nf.getJSONObject("claims").getBoolean("executable_fills"))
        assertFalse(nf.getJSONObject("claims").getBoolean("forecast_of_future_inputs"))
        assertFalse("a real device capture is not synthetic", nf.getBoolean("is_synthetic"))
    }

    /** The premium-only payload names the requested date and substitutes no other. */
    @Test
    fun aPremiumOnlyPayloadIsForExactlyTheRequestedDate() {
        val fd = capturedFilesDir(listOf("09:20"))
        val sec = PremiumExportSection.build(fd, "2026-10-19")
        val bytes = PremiumExportSection.premiumOnlyBytes("2026-10-19", sec)
        val o = JSONObject(String(bytes, Charsets.UTF_8))
        assertEquals("2026-10-19", o.getString("session_date"))
        assertEquals(1, o.getJSONArray("parts_present").length())
        assertEquals("premium", o.getJSONArray("parts_present").getString(0))
        assertTrue(o.getString("note").contains("substitutes no other day"))
        assertTrue(o.getJSONObject("premium").getBoolean("has_data"))
    }

    /** The bridge is a thin adapter: the assembly lives where it can be tested. */
    @Test
    fun theBridgeDelegatesTheExportAssemblyAndGuardsItWithThrowable() {
        val src = stripComments(
            File("src/main/java/com/marketradar/app/NativeBridge.kt").readText())
        assertTrue("the bridge must delegate the section build",
            src.contains("PremiumExportSection.build(context.filesDir, date)"))
        assertTrue("and the cap enforcement", src.contains("PremiumExportSection.mergeWithinCap("))
        assertTrue("and must not re-implement the section itself",
            !src.contains("fun premiumSectionFor("))
        // the two new control methods exist and are JS-exposed
        assertTrue(src.contains("fun getPremiumCaptureStatus()"))
        assertTrue(src.contains("fun setPremiumCapture(enabled: Boolean)"))
        // each new bridge method catches Throwable: an OOM here aborted the whole calling JS
        // function once already (CLAUDE.md, b321)
        val status = src.substringAfter("fun getPremiumCaptureStatus()").substringBefore("fun ")
        val control = src.substringAfter("fun setPremiumCapture(enabled: Boolean)")
            .substringBefore("private fun")
        assertTrue("getPremiumCaptureStatus must catch Throwable",
            status.contains("catch (t: Throwable)"))
        assertTrue("setPremiumCapture must catch Throwable", control.contains("catch (t: Throwable)"))

        // AND THE EXPORT ENTRY POINT ITSELF, which this test previously did not check at all
        // despite its name. exportSessionEvidence allocates multi-megabyte arrays in bundleBytes,
        // in the merge and in the premium-only encoding; those allocations are OUTSIDE the
        // smaller helpers' guards, and it ended in `catch (e: Exception)`, so an OutOfMemoryError
        // escaped the bridge and aborted the whole calling JS function.
        val export = src.substringAfter("fun exportSessionEvidence(")
            .substringBefore("fun setIndexSelector(")
        assertTrue("exportSessionEvidence must catch Throwable, not Exception",
            export.contains("catch (t: Throwable)"))
        // The OUTERMOST guard is what matters, and it is the LAST catch in the method. The first
        // version of this assertion banned `catch (e: Exception)` anywhere in the body, which CI
        // rejected — correctly: the method has legitimate inner catches around sub-steps, and
        // forbidding them would force every nested failure to be handled by the outer guard
        // instead of locally. What must not happen is the method ENDING on an Exception catch.
        val catches = Regex("""catch \((\w+): (\w+)\)""").findAll(export)
            .map { it.groupValues[2] }.toList()
        assertTrue("the method must have a catch at all", catches.isNotEmpty())
        assertEquals("the outermost guard must be Throwable; catches found in order: $catches",
            "Throwable", catches.last())
        // the allocation-light fallback is itself Throwable-guarded, so the last TWO are both
        assertTrue("the error response must itself be guarded",
            catches.size >= 2 && catches.takeLast(2).all { it == "Throwable" })
        // the error response must be allocation-light: no echo of the payload or of the sizes
        // that just failed to fit, and a constant-string fallback if even that allocation fails
        assertTrue("there must be a constant-string fallback response",
            export.contains("\"{\\\"ok\\\":false,\\\"status\\\":\\\"EXPORT_ERROR\\\"}\""))
    }

    /**
     * An allocation failure inside the export assembly is reported, not propagated.
     *
     * Injected through the real assembly: a section whose serialization throws an
     * OutOfMemoryError. The production bridge method needs an Android context, so the guarded
     * decision path is exercised through the same production helpers the bridge calls, and the
     * bridge's own guard is pinned by source above. This is a seam, and it is labelled as one
     * rather than described as the bridge having been executed.
     */
    @Test
    fun anAllocationFailureInTheAssemblyIsReportedRatherThanPropagated() {
        val hostile = object : JSONObject() {
            override fun toString(): String = throw OutOfMemoryError("injected")
        }
        // splice must not propagate it as a crash of the caller's own flow
        val threw = runCatching {
            PremiumExportSection.splice("{\"a\":1}", hostile)
        }.exceptionOrNull()
        assertTrue("the injected Error must be an Error, or this proves nothing",
            threw is OutOfMemoryError)
        // ...which is exactly why the bridge entry point has to catch Throwable: a helper does
        // not stop this, and `catch (Exception)` would let it reach WebView. Asserted as the
        // real behaviour of the real helper, not assumed.
        val guardedResult = try {
            PremiumExportSection.splice("{\"a\":1}", hostile)
            "NO_THROW"
        } catch (t: Throwable) {
            "EXPORT_ERROR:${t.javaClass.simpleName}"
        }
        assertEquals("EXPORT_ERROR:OutOfMemoryError", guardedResult)
    }

    // ═════════════════ nothing is wired, nothing reaches the trading path ═════════════════════

    @Test
    fun theCaptureSourcesTouchNoNetworkSchedulerOrTradingSurface() {
        val files = listOf("PremiumObservationContract.kt", "PremiumCaptureCohort.kt",
            "PremiumCaptureStore.kt", "PremiumCaptureSession.kt", "PremiumCaptureControl.kt",
            "PremiumCaptureCoordinator.kt", "PremiumExportSection.kt")
        val banned = listOf("OkHttp", "HttpURLConnection", "Request.Builder", "AlarmManager",
            "setExact", "WorkManager", "JobScheduler", "Timer(",
            "SupabaseClient", "NotificationHelper", "OrderExecutionService",
            "SharedPreferences", "getSharedPreferences", "startService", "sendBroadcast")
        for (n in files) {
            // comments and string literals stripped: the assertion is about references, not prose
            val src = stripComments(
                File("src/main/java/com/marketradar/app/$n").readText())
            for (b in banned) assertFalse("$n must not reference $b", src.contains(b))
        }
    }

    /**
     * REPLACES the old "the library has no caller" test, which is now false by design and which
     * Codex rightly said must not be preserved by hiding references. The boundary is an explicit
     * ALLOWLIST: only the approved entry points may reach the capture library.
     */
    @Test
    fun onlyApprovedEntryPointsMayCallTheCaptureLibrary() {
        val approved = setOf(
            // the library itself
            "PremiumObservationContract.kt", "PremiumCaptureCohort.kt", "PremiumCaptureStore.kt",
            "PremiumCaptureSession.kt", "PremiumCaptureControl.kt",
            // the bounded off-poll persistence worker
            "PremiumCaptureWorker.kt",
            // the one coordinator, and the pure poll->capture mapping it is driven with
            "PremiumCaptureCoordinator.kt", "PremiumCapturePollAdapter.kt",
            "PremiumExportSection.kt",
            // the existing acquisition path
            "MarketWatchService.kt",
            // the existing export/control bridge
            "NativeBridge.kt",
            // the native operator control in the existing settings dialog
            "MainActivity.kt",
        )
        val dir = File("src/main/java/com/marketradar/app")
        val referrers = (dir.listFiles() ?: emptyArray()).filter { f ->
            f.isFile && f.name.endsWith(".kt") &&
                stripComments(f.readText()).contains("PremiumCapture")
        }.map { it.name }.toSet()
        val unapproved = referrers - approved
        assertEquals("only approved entry points may call the capture library",
            emptySet<String>(), unapproved)
        // and the boundary is real: the coordinator IS wired, so this is not the old test renamed
        assertTrue("the coordinator must actually be called from the acquisition path",
            referrers.contains("MarketWatchService.kt"))
    }

    /**
     * The service hook must make no request, schedule nothing, and be unable to fail the poll.
     * Asserted on the real `MarketWatchService` source, scoped to the premium block.
     */
    @Test
    fun theServiceHookMakesNoRequestAndCannotFailThePoll() {
        val src = stripComments(
            File("src/main/java/com/marketradar/app/MarketWatchService.kt").readText())
        // the hook exists, and it is inside the Throwable-guarded wrapper
        assertTrue("the coordinator must be invoked from the poll",
            src.contains("PremiumCaptureCoordinator.onChainsAcquired("))
        assertTrue("the invocation must be wrapped so capture cannot fail the poll",
            src.contains("premiumCaptureSafely {"))
        // the guard itself must catch Throwable: an OutOfMemoryError from a large chain snapshot
        // is an Error and would otherwise escape. Checked on the declaration, not on the file.
        // sliced to the next declaration, NOT to the first "}": the body contains
        // `try { block() }`, so substringBefore("}") cut the slice before the catch and the
        // assertion could never see it. That was a bug in this test, found by CI.
        val guard = src.substringAfter("private inline fun premiumCaptureSafely(")
            .substringBefore("private fun istHhmmOrNull")
        assertTrue("the guard must catch Throwable, not Exception, got: $guard",
            guard.contains("catch (t: Throwable)"))
        // the premium block itself must contain no request and no scheduling. Sliced to the
        // quotes abort CONDITION rather than its log message, so the abort's own code is not
        // inside the slice being judged.
        val block = src.substringAfter("premiumCaptureSafely {")
            .substringBefore("if (quotesJson == null)")
        for (banned in listOf("fetchSync(", "async(", "SupabaseClient", "AlarmManager",
            "WorkManager", "startService", "Thread(")) {
            assertFalse("the premium capture block must not contain $banned", block.contains(banned))
        }
        // It must be placed before EVERY early return that can discard a good chain. The hook
        // used to sit below the quotes abort, so a failure of the separate index-quotes request
        // threw away both successful chains -- the same mistake as the stocks abort, one abort
        // earlier, and moving it above only the stocks abort did not fix it.
        val hookAt = src.indexOf("PremiumCaptureCoordinator.onChainsAcquired(")
        val quotesAbortAt = src.indexOf("POLL_FAIL: Quotes fetch returned null")
        val stocksAbortAt = src.indexOf("POLL_FAIL: Chain or stocks fetch returned null")
        val m0At = src.indexOf("m0RecordCaptures(mapOf(")
        assertTrue("the quotes abort must be found for this assertion to mean anything",
            quotesAbortAt > 0 && stocksAbortAt > 0 && m0At > 0)
        assertTrue("premium capture must run before the QUOTES abort, not just the stocks one",
            hookAt in 1 until quotesAbortAt)
        assertTrue("and before the stocks abort", hookAt < stocksAbortAt)
        assertTrue("while the existing m0 hook is after both, which is the difference",
            stocksAbortAt < m0At)

        // The capture-owned completion clocks must be assigned UNCONDITIONALLY, or a failed chain
        // reaches capture with no clock and no slot and its six missing-contract outcomes can
        // never be written. The success-only x1* assignment must remain conditional, because
        // other code depends on those semantics.
        assertTrue("capture must have its own completion clock for BNF",
            src.contains("capBnfCompletedAtMs = System.currentTimeMillis()"))
        assertTrue("capture must have its own completion clock for NF",
            src.contains("capNfCompletedAtMs = evNfChainFetchedAtMs"))
        assertTrue("the success-only x1 semantics must be preserved",
            src.contains("if (it != null) x1BnfChainFetchedAtMs = capBnfCompletedAtMs"))
        assertFalse("the failed-chain path must not feed capture the success-only clock",
            block.contains("x1BnfChainFetchedAtMs") || block.contains("x1NfChainFetchedAtMs"))
    }

    @Test
    fun theLiveIvHelperIsNotReusedAndIsNotChanged() {
        // the helper still collapses sources and fabricates 0.0, which is why this record does not
        // use it. Pinning its behaviour here proves this delivery did not alter trading code.
        assertEquals(0.0, OptionChainBrainFormatter.optionIv(JSONObject(), JSONObject()), 0.0)
        assertEquals(14.2, OptionChainBrainFormatter.optionIv(
            leg("K", null, md("iv" to 14.2)), null), 1e-9)
        assertNull(OptionChainBrainFormatter.optionQuote(md("bid_price" to 0.0), "bid_price"))
        val src = stripComments(
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
