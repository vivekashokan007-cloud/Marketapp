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
        assertTrue(ex.getJSONObject("claims").getBoolean("authoritative_lot_resolved"))

        // a MIXED session must not be a blanket success: one acquisition resolves, one conflicts
        val conflicting = JSONObject(table.toString())
        conflicting.getJSONArray("rows").getJSONObject(0).put("lot", 50)
        val mixed = PremiumCaptureSession(store).export("NF", session, "09:20", replayContext(),
            lotTableObject = conflicting)
        val lotM = mixed.getJSONObject("references").getJSONObject("contract_lot_table")
        assertEquals("PARTIALLY_RESOLVED_SEE_COVERAGE", lotM.getString("resolution"))
        assertEquals(1, lotM.getJSONObject("resolution_coverage")
            .getInt("conflicting_with_claim"))
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
        assertTrue(ex.getJSONObject("claims").getBoolean("authoritative_lot_resolved"))
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
        val r = PremiumCaptureCoordinator.onChainsAcquired(fd, "2026-10-19", "09:20",
            listOf(acq("NF", "mon_0920_entry_NF"), acq("BNF", "mon_0920_entry_BNF")))
        assertEquals(PremiumCaptureCoordinator.RAN, r.getString("status"))
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
        PremiumCaptureCoordinator.onChainsAcquired(fd, "2026-10-19", "09:25",
            listOf(acq("NF", "mon_0920_entry_NF", receipt = "09:25",
                start = 300_000L, end = 300_300L)))
        val row = PremiumCaptureCoordinator.onChainsAcquired(fd, "2026-10-19", "09:30",
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
        PremiumCaptureCoordinator.onChainsAcquired(fd, "2026-10-19", "09:20",
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

        // a malformed control file is OFF, and the fact is reported rather than hidden
        File(PremiumCaptureControl.root(fd), "control.json").writeText("{not json")
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
        val r = PremiumCaptureCoordinator.onChainsAcquired(fd, "2026-10-19", "09:20",
            listOf(acq("NF", "mon_0920_entry_NF"), acq("BNF", "mon_0920_entry_BNF")))
        assertEquals(PremiumCaptureCoordinator.DISABLED, r.getString("status"))
        assertEquals(0, r.getJSONArray("indices").length())
        assertFalse("OFF must not create the capture directory",
            PremiumCaptureControl.root(fd).exists())
        // an unreadable control is also not ON
        PremiumCaptureControl.setEnabled(fd, true)
        File(PremiumCaptureControl.root(fd), "control.json").writeText("{broken")
        assertEquals(PremiumCaptureCoordinator.CONTROL_UNREADABLE,
            PremiumCaptureCoordinator.onChainsAcquired(fd, "2026-10-19", "09:20",
                listOf(acq("NF", "mon_0920_entry_NF"))).getString("status"))
    }

    /** CONTROL ON: the already-fetched chain reaches persisted evidence, per index. */
    @Test
    fun whenOnTheAlreadyFetchedChainReachesPersistedEvidencePerIndex() {
        val fd = tmpFilesDir()
        assertTrue(PremiumCaptureControl.setEnabled(fd, true).ok)
        val r = PremiumCaptureCoordinator.onChainsAcquired(fd, "2026-10-19", "09:20",
            listOf(acq("NF", "mon_0920_entry_NF"), acq("BNF", "mon_0920_entry_BNF")))
        assertEquals(PremiumCaptureCoordinator.RAN, r.getString("status"))
        assertEquals("NONE_SYNCHRONOUS_NO_PENDING_QUEUE", r.getString("queued_work"))
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
            assertTrue(row.has("elapsed_ms"))
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
        val r = PremiumCaptureCoordinator.onChainsAcquired(fd, "2026-10-19", "09:20",
            listOf(acq("NF", "mon_0920_entry_NF", receipt = "09:26")))
        assertEquals(PremiumCaptureCoordinator.RAN, r.getString("status"))
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
        PremiumCaptureCoordinator.onChainsAcquired(fd, "2026-10-19", "09:20",
            listOf(acq("NF", "mon_0920_entry_NF")))
        val r = PremiumCaptureCoordinator.onChainsAcquired(fd, "2026-10-19", "09:25",
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
        PremiumCaptureCoordinator.onChainsAcquired(fd, "2026-10-19", "09:20",
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
        File(root, "filler.bin").writeBytes(
            ByteArray(PremiumCaptureStore.MAX_BYTES_GLOBAL.toInt()))
        val r = PremiumCaptureCoordinator.onChainsAcquired(fd, "2026-10-19", "09:20",
            listOf(acq("NF", "mon_0920_entry_NF")))
        val row = r.getJSONArray("indices").getJSONObject(0)
        assertNotEquals("a full store must refuse, not throw",
            "CAPTURE_ERROR", row.getString("status"))
        assertEquals(PremiumCaptureSession.COHORT_PERSIST_FAILED, row.getString("status"))
        assertEquals(0, row.getInt("written"))
        // and the refusal to persist the summary is reported as such, not as durable accounting
        val sw = r.getJSONObject("summary_write")
        assertFalse("a failed disk cannot record its own failure",
            sw.getBoolean("durably_recorded"))
        assertEquals(PremiumCaptureStore.REFUSED_GLOBAL_BYTES, sw.getString("status"))
    }

    /** Capture failure is always a status; it can never propagate to the caller. */
    /**
     * The poll budget stops capture rather than delaying a poll. Exercised, not merely asserted
     * to exist: with a zero budget the very first index is skipped and reported, and nothing is
     * written. There is no queue, so there is no pending work to drop and nothing to overflow.
     */
    @Test
    fun theCaptureBudgetStopsWorkRatherThanDelayingThePoll() {
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        val r = PremiumCaptureCoordinator.onChainsAcquired(fd, "2026-10-19", "09:20",
            listOf(acq("NF", "mon_0920_entry_NF"), acq("BNF", "mon_0920_entry_BNF")),
            budgetMs = 0L)
        assertEquals(PremiumCaptureCoordinator.BUDGET_EXCEEDED, r.getString("status"))
        assertEquals(0L, r.getLong("budget_ms"))
        val rows = r.getJSONArray("indices")
        assertEquals("both indices must be reported, not silently skipped", 2, rows.length())
        for (i in 0 until rows.length()) {
            assertEquals(PremiumCaptureCoordinator.BUDGET_EXCEEDED,
                rows.getJSONObject(i).getString("status"))
        }
        // nothing was written, and the refusal is visible rather than implicit
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        assertEquals(0, store.outcomeCount("NF", "2026-10-19"))
        assertEquals(0, store.envelopeCount("NF", "2026-10-19"))
        assertEquals("NONE_SYNCHRONOUS_NO_PENDING_QUEUE", r.getString("queued_work"))
        // and the production default is the constant, not the injected value
        assertEquals(2_000L, PremiumCaptureCoordinator.CAPTURE_BUDGET_MS_PER_POLL)
    }

    @Test
    fun coordinatorFailureIsAlwaysAStatusNeverAnException() {
        // an unwritable files dir
        val r = PremiumCaptureCoordinator.onChainsAcquired(
            File("/proc/nonexistent-premium-files"), "2026-10-19", "09:20",
            listOf(acq("NF", "mon_0920_entry_NF")))
        assertEquals("no control file is readable there, so it must be OFF",
            PremiumCaptureCoordinator.DISABLED, r.getString("status"))
        // a bad session date is refused explicitly
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        for (bad in listOf(null, "", "not-a-date", "2026-02-30")) {
            assertEquals(PremiumCaptureCoordinator.NO_SESSION,
                PremiumCaptureCoordinator.onChainsAcquired(fd, bad, "09:20",
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
        for ((i, slot) in slots.withIndex()) {
            val t0 = System.nanoTime()
            val r = PremiumCaptureCoordinator.onChainsAcquired(fd, "2026-10-19", slot,
                listOf(acq("NF", "mon_0920_entry_NF", receipt = slot,
                    start = 1_000L + i * 300_000L, end = 1_300L + i * 300_000L),
                    acq("BNF", "mon_0920_entry_BNF", receipt = slot,
                        start = 1_000L + i * 300_000L, end = 1_320L + i * 300_000L)))
            perRun.add((System.nanoTime() - t0) / 1_000_000)
            assertEquals(PremiumCaptureCoordinator.RAN, r.getString("status"))
            val rows = r.getJSONArray("indices")
            for (j in 0 until rows.length()) writes += rows.getJSONObject(j).getInt("written")
        }
        val store = PremiumCaptureStore(PremiumCaptureControl.root(fd))
        val nfBytes = store.sessionBytes("NF", "2026-10-19")
        val bnfBytes = store.sessionBytes("BNF", "2026-10-19")
        val global = store.globalBytes()
        assertEquals("every slot must persist both indices' six contracts",
            slots.size * 12, writes)
        // the budget must not have been hit on a fixture of this size
        assertTrue("per-run cost ${perRun} exceeded the poll budget",
            perRun.all { it < PremiumCaptureCoordinator.CAPTURE_BUDGET_MS_PER_POLL })

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
                "per_run_ms_both_indices=$perRun\n" +
                "per_run_ms_max=${perRun.max()} budget_ms=" +
                "${PremiumCaptureCoordinator.CAPTURE_BUDGET_MS_PER_POLL}\n" +
                "records_written=$writes\n" +
                "session_bytes_NF=$nfBytes\nsession_bytes_BNF=$bnfBytes\n" +
                "global_bytes=$global global_cap=${PremiumCaptureStore.MAX_BYTES_GLOBAL}\n" +
                "bytes_per_slot_per_index=$bytesPerSlot\n" +
                "session_cap=${PremiumCaptureStore.MAX_BYTES_PER_INDEX_SESSION}\n" +
                "pending_queue=NONE_SYNCHRONOUS\n" +
                "retention_index_sessions_until_global_cap=" +
                "${if (perIndexSession > 0) PremiumCaptureStore.MAX_BYTES_GLOBAL /
                    (perIndexSession / slotsPerIndexSession * 69).coerceAtLeast(1) else -1}\n" +
                "retention_note=the global cap bounds total collection; capture stops cleanly, " +
                "nothing is deleted\n")
        assertTrue(out.isFile && out.length() > 0L)
    }

    // ═════════════════ Commit 3 — the premium section on the existing export ═════════════════

    /** Capture `slots` slots for both indices into a fresh filesDir, and return it. */
    private fun capturedFilesDir(slots: List<String>, date: String = "2026-10-19"): File {
        val fd = tmpFilesDir()
        PremiumCaptureControl.setEnabled(fd, true)
        for ((i, slot) in slots.withIndex()) {
            PremiumCaptureCoordinator.onChainsAcquired(fd, date, slot,
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
        PremiumCaptureCoordinator.onChainsAcquired(fd, "2026-10-19", "09:20",
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
     * The combined limit is enforced on the FINAL bytes. The old limit is not raised and old parts
     * are never truncated: the premium section is reduced in declared stages, and if it still will
     * not fit the export is refused.
     */
    @Test
    fun theCombinedCapIsEnforcedByDeclaredStagedReductionThenRefusal() {
        val fd = capturedFilesDir(listOf("09:20", "09:25", "09:30", "09:35"))
        val sec = PremiumExportSection.build(fd, "2026-10-19")
        val secBytes = sec.toString().toByteArray(Charsets.UTF_8).size
        assertTrue("the section must be substantial for this case to mean anything", secBytes > 50_000)

        // (a) a bundle that leaves room only after records are dropped
        val roomy = "{\"filler\":\"" + "x".repeat(
            PremiumExportSection.COMBINED_CAP_BYTES - secBytes / 2) + "\"}"
        val fitA = PremiumExportSection.mergeWithinCap(
            roomy, roomy.toByteArray(Charsets.UTF_8), sec)
        assertTrue("it must have reduced rather than refused", fitA.stage >= 1)
        assertFalse(fitA.overCap)
        assertTrue(fitA.bytes.size <= PremiumExportSection.COMBINED_CAP_BYTES)
        // the reduction is declared, and nothing is cut mid-value
        val mergedA = JSONObject(String(fitA.bytes, Charsets.UTF_8)).getJSONObject("premium")
        assertEquals(fitA.stage, mergedA.getInt("reduction_stage"))
        assertTrue(mergedA.getJSONArray("reduced_fields").toString().contains("records"))
        assertFalse("records were dropped wholesale, not truncated",
            mergedA.getJSONObject("indices").getJSONObject("NF").has("records"))
        // The parts that remain are intact, and the ACCOUNTING is deliberately NOT reduced with
        // the records: it still reports the true count, so a reader can tell that evidence was
        // dropped to fit rather than never collected.
        //
        // Derived from the slot list and the cohort size rather than written as a literal. The
        // literal that was here was the 2-slot figure from the test above, copied into this
        // 4-slot case; CI caught it as expected:<12> but was:<24> and the production count was
        // the correct one. `reduce` deep-copies its input, so `sec` is still unreduced here.
        val trueRecords = 4 * PremiumCaptureCohort.CONTRACTS_PER_COHORT
        assertEquals(trueRecords, sec.getJSONObject("indices").getJSONObject("NF")
            .getJSONObject("accounting").getInt("records"))
        assertEquals("the accounting must survive the reduction unchanged",
            trueRecords, mergedA.getJSONObject("indices").getJSONObject("NF")
                .getJSONObject("accounting").getInt("records"))

        // (b) a bundle already at the limit: no reduction can help, so it must REFUSE
        val full = "{\"filler\":\"" + "x".repeat(
            PremiumExportSection.COMBINED_CAP_BYTES - 200) + "\"}"
        val fitB = PremiumExportSection.mergeWithinCap(
            full, full.toByteArray(Charsets.UTF_8), sec)
        assertTrue("it must refuse rather than raise the limit or truncate", fitB.overCap)
        assertEquals(3, fitB.stage)
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
            // the one coordinator
            "PremiumCaptureCoordinator.kt", "PremiumExportSection.kt",
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
        // the premium block itself must contain no request and no scheduling
        val block = src.substringAfter("premiumCaptureSafely {")
            .substringBefore("POLL_FAIL: Chain or stocks fetch returned null")
        for (banned in listOf("fetchSync(", "async(", "SupabaseClient", "AlarmManager",
            "WorkManager", "startService", "Thread(")) {
            assertFalse("the premium capture block must not contain $banned", block.contains(banned))
        }
        // it must be placed BEFORE the stocks abort, so a good chain is not lost to an
        // unrelated failure -- the position the existing m0 hook gets wrong
        val hookAt = src.indexOf("PremiumCaptureCoordinator.onChainsAcquired(")
        val abortAt = src.indexOf("POLL_FAIL: Chain or stocks fetch returned null")
        val m0At = src.indexOf("m0RecordCaptures(mapOf(")
        assertTrue("premium capture must run before the stocks abort", hookAt in 1 until abortAt)
        assertTrue("and the existing m0 hook is after it, which is the difference",
            abortAt < m0At)
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
