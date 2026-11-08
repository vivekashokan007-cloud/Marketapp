package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject

/**
 * Drives one index/session premium observation experiment from already-fetched chain responses,
 * and exports it so the evidence can be audited offline.
 *
 * Explicitly NOT done here, by design: no network request, no timer, no background wake, no
 * server/database write, no upload queue, no brain-context field, no authority, admission or exit
 * change, and no Paper lock. The caller hands in a response it already had; if capture fails the
 * caller is told and carries on.
 *
 * ## P2/P3/P4 corrections (Codex review R1)
 *
 * **P2 — a slot could join quotes to the wrong acquisition.** The old code wrote the envelope and
 * then wrote all six observations *regardless of whether the envelope was accepted*, and the join
 * was `session_date + index + slot`. So: acquisition A persists its envelope but only some of its
 * observations; acquisition B retries the same slot with a different receipt time, spot and quotes;
 * B's envelope is archived as a conflict while A stays primary; B's observations fill the missing
 * keys. The export then joined B's quotes to A's clocks, spot and lot, with every hash valid.
 * Now each acquisition has an identity derived from its own evidence, every record carries that id
 * plus the envelope's content reference, keys include the id so two acquisitions never interleave,
 * and **an envelope that is not persisted writes no observations at all**.
 *
 * **P3 — refusal evidence did not survive.** `writeSummary` had no caller, so refusals, conflicts
 * and corruption lived only in a returned object until the process ended. Every attempt now bumps
 * durable counters, and the export reports partial batches, conflicts, corrupt files and orphan
 * temps rather than a bare count.
 *
 * **P4 — the export claimed offline resolution it did not have.** It read lot/map references from
 * `records.firstOrNull()`, but the envelope split had moved `lot` out of records, so those fields
 * were *always null*. References now come from envelopes, and the export says
 * `UNRESOLVED_REFERENCE` unless the actual dated metadata is supplied to it — retaining a string
 * named `map_ref` never made the metadata available.
 */
class PremiumCaptureSession(private val store: PremiumCaptureStore) {

    companion object {
        const val SESSION_VERSION = "premium_capture_session_v2_acquisition_bound"
        const val EXPORT_VERSION = "premium_capture_export_v2_linked_and_accounted"

        // slot-level refusals, all fail-closed: nothing is written
        const val NO_SLOT_IDENTITY = "NO_SLOT_IDENTITY"
        const val SLOT_IDENTITY_UNUSABLE = "SLOT_IDENTITY_UNUSABLE"
        const val OUTSIDE_WINDOW = "OUTSIDE_EXPERIMENT_WINDOW"
        const val COHORT_INVALID_REFUSED = "COHORT_INVALID_REFUSED"
        const val COHORT_PERSIST_FAILED = "COHORT_PERSIST_FAILED"
        const val ENVELOPE_NOT_PERSISTED = "ENVELOPE_NOT_PERSISTED"
        const val OBSERVED = "OBSERVED"

        /** Nominal five-minute experiment slots inside the window, for expected-vs-observed. */
        fun expectedSlots(): List<String> {
            val out = ArrayList<String>()
            var m = PremiumCaptureCohort.WINDOW_OPEN_MIN
            while (m < PremiumCaptureCohort.WINDOW_CLOSE_MIN) {
                out.add("%02d:%02d".format(m / 60, m % 60)); m += 5
            }
            return out
        }
    }

    /** The pinned cohort and whether it is usable, as three distinct states. */
    data class CohortState(val cohort: JSONObject?, val state: String, val reason: String? = null)

    fun loadCohort(index: String, session: String): CohortState {
        val text = store.readCohortText(index, session) ?: return CohortState(null, "ABSENT")
        val parsed = runCatching { JSONObject(text) }.getOrNull()
        val why = PremiumCaptureCohort.validateLoaded(parsed, index, session)
        // a corrupt cohort is NOT "absent": absent may select, corrupt must refuse (P3)
        return if (why == null) CohortState(parsed, "VALID") else CohortState(null, "INVALID", why)
    }

    data class SlotResult(
        val status: String,
        val cohortId: String?,
        val acquisitionId: String?,
        val written: Int,
        val refused: Int,
        val duplicates: Int,
        val conflicts: Int,
        val outcomes: Map<String, String>,
        val batchComplete: Boolean = false,
        val detail: String? = null,
    )

    /**
     * Observe one already-fetched response for one index.
     *
     * `rawChain` null means the acquisition itself failed, which is recorded as
     * MISSING_REQUEST_FAILED for all six pinned contracts rather than as nothing at all — but only
     * once a cohort is already pinned, since a failed response carries no identity to pin from.
     */
    fun observeSlot(
        index: String,
        sessionDate: String,
        rawChain: JSONObject?,
        acquisitionHhmm: String?,
        schedulerSlot: String?,
        requestedUnderlyingKey: String?,
        requestStartedMs: Long?,
        responseReceivedMs: Long?,
        vendorSourceTs: String? = null,
        separateSpot: Double? = null,
        separateSpotSourceField: String? = null,
        separateSpotSourceTs: String? = null,
        separateSpotReceiptMs: Long? = null,
        claimedLot: Any? = null,
        claimedLotSource: String? = null,
        lotTableRef: String? = null,
        mapRef: String? = null,
    ): SlotResult {
        fun refuse(status: String, detail: String?, counter: String): SlotResult {
            store.bumpCounters(index, sessionDate, mapOf("refused_$counter" to 1L))
            return SlotResult(status, null, null, 0, 0, 0, 0, emptyMap(), detail = detail)
        }

        val slotId = PremiumCaptureCohort.slotIdentity(schedulerSlot, acquisitionHhmm)
            ?: return refuse(NO_SLOT_IDENTITY,
                "no scheduler slot and no readable acquisition clock", "no_slot_identity")
        if (slotId.disagreement != null)
            return refuse(SLOT_IDENTITY_UNUSABLE, slotId.disagreement, "slot_identity_unusable")

        // P5: the window is enforced on EVERY attempt, not only while selecting. The acquisition
        // clock is preferred; with none, the slot itself must be inside the window.
        val windowClock = slotId.actualClock?.takeIf { it.isNotEmpty() } ?: slotId.slot
        if (!PremiumCaptureCohort.inWindow(windowClock))
            return refuse(OUTSIDE_WINDOW, "clock=$windowClock is outside the experiment window",
                "outside_window")

        val loaded = loadCohort(index, sessionDate)
        if (loaded.state == "INVALID")
            return refuse(COHORT_INVALID_REFUSED,
                "a pinned cohort exists but is unusable (${loaded.reason}); NOT reselected",
                "cohort_invalid")
        var cohort = loaded.cohort
        if (cohort == null) {
            // the RESOLVED clock, not the raw one. CI caught this: `select` enforces the window on
            // the clock it is handed, so passing `acquisitionHhmm` straight through meant an
            // acquisition identified only by a scheduler slot — a legitimate case, with no
            // acquisition clock at all — was refused OUTSIDE_WINDOW and could never pin a cohort.
            val sel = PremiumCaptureCohort.select(rawChain, index, sessionDate, windowClock,
                requestedUnderlyingKey, null)
            if (sel.status != PremiumCaptureCohort.SELECTED || sel.cohort == null)
                return refuse(sel.status, sel.reason, "cohort_not_selected")
            val cr = store.putCohort(index, sessionDate, sel.cohort)
            if (!cr.persisted)
                return refuse(COHORT_PERSIST_FAILED, "${cr.status}: ${cr.detail}", "cohort_persist")
            cohort = sel.cohort
        }

        val clocks = PremiumObservationContract.clockEvidence(
            requestStartedMs, responseReceivedMs, vendorSourceTs)
            .put("slot_basis", slotId.basis)
            .put("actual_acquisition_clock", slotId.actualClock ?: JSONObject.NULL)
        val lot = PremiumObservationContract.lotEvidence(
            claimedLot, claimedLotSource, lotTableRef, mapRef)

        val pinned = PremiumCaptureCohort.pinnedList(cohort)
        // locate first, so the envelope's underlying comes from the SAME acquisition row the
        // observations were read from rather than from a different row or a cache
        val located = pinned.map { p ->
            p to (if (rawChain == null)
                PremiumCaptureCohort.Located(
                    PremiumObservationContract.OUTCOME_REQUEST_FAILED, null, null)
            else PremiumCaptureCohort.locate(rawChain, p))
        }
        val anyRow = located.firstOrNull { it.second.row != null }?.second?.row
        val underlying = PremiumObservationContract.underlyingEvidence(
            anyRow, separateSpot, separateSpotSourceField, separateSpotSourceTs,
            separateSpotReceiptMs)

        val env = PremiumObservationContract.slotEnvelope(
            index.uppercase(), sessionDate, slotId.slot, cohort, clocks, underlying, lot)
        val acqId = env.optString("acquisition_id")
        // P2: the envelope key includes the acquisition id, so a same-slot retry with different
        // evidence becomes a DISTINCT acquisition instead of a conflict against the first one
        val envKey = "$sessionDate|${index.uppercase()}|${slotId.slot}|$acqId|ENVELOPE"
        val er = store.putEnvelope(index, sessionDate, envKey, env)
        if (!er.persisted) {
            // FAIL CLOSED: with no envelope there is nothing for an observation to resolve
            // against, so no observation is written. The old code wrote all six anyway.
            store.bumpCounters(index, sessionDate,
                mapOf("refused_envelope_not_persisted" to 1L, "batches_refused" to 1L))
            return SlotResult(ENVELOPE_NOT_PERSISTED, cohort.optString("cohort_id"), acqId,
                0, 1, 0, 0, emptyMap(), detail = "${er.status}: ${er.detail}")
        }

        var written = 0; var refused = 0; var dupes = 0; var conflicts = 0
        val outcomes = LinkedHashMap<String, String>()
        val deltas = HashMap<String, Long>()
        // every pinned contract gets an outcome, observed or not
        for ((p, loc) in located) {
            val rec = PremiumObservationContract.observation(
                p, cohort, sessionDate, slotId.slot, loc.outcome, loc.row, loc.leg, env)
            val key = "$sessionDate|${index.uppercase()}|${slotId.slot}|$acqId|${p.instrumentKey}"
            val r = store.put(index, sessionDate, key, rec)
            outcomes[p.instrumentKey] = loc.outcome
            when (r.status) {
                PremiumCaptureStore.WRITTEN -> written++
                PremiumCaptureStore.DUPLICATE_NO_OP -> dupes++
                PremiumCaptureStore.CONFLICT_ARCHIVED -> conflicts++
                else -> refused++
            }
            deltas["write_${r.status.lowercase()}"] = (deltas["write_${r.status.lowercase()}"] ?: 0L) + 1L
        }
        val complete = (written + dupes) == PremiumCaptureCohort.CONTRACTS_PER_COHORT
        deltas["batches_attempted"] = 1L
        deltas[if (complete) "batches_complete" else "batches_partial"] = 1L
        deltas["envelope_${er.status.lowercase()}"] = 1L
        store.bumpCounters(index, sessionDate, deltas)

        return SlotResult(OBSERVED, cohort.optString("cohort_id"), acqId,
            written, refused, dupes, conflicts, outcomes, batchComplete = complete,
            detail = "envelope=${er.status}")
    }

    /**
     * Export for offline audit.
     *
     * `lotTableObject` / `mapObject` are the ACTUAL dated metadata. When they are absent the export
     * says `UNRESOLVED_REFERENCE` and claims no offline resolution for them (P4): a retained
     * reference string is a name, not the data it names.
     */
    fun export(index: String, sessionDate: String, captureStartedSlot: String?,
               schedulerSkippedSlots: List<String> = emptyList(),
               lotTableObject: JSONObject? = null,
               mapObject: JSONObject? = null): JSONObject {
        val idx = index.uppercase()
        val loaded = loadCohort(index, sessionDate)
        val recRead = store.readRecords(index, sessionDate)
        val envRead = store.readEnvelopeRecords(index, sessionDate)
        val conRead = store.readConflicts(index, sessionDate)

        // ── acquisition linkage, which is the point of the P2 fix ──────────────────────────────
        val byAcq = recRead.rows.groupBy { it.optString("acquisition_id") }
        val envByAcq = envRead.rows.associateBy { it.optString("acquisition_id") }
        val acqRows = JSONArray()
        var complete = 0; var partial = 0
        for (e in envRead.rows) {
            val id = e.optString("acquisition_id")
            val recs = byAcq[id] ?: emptyList()
            val expectedRef = PremiumObservationContract.ref(
                PremiumObservationContract.contentHash(e))
            val linked = recs.count { it.optString("envelope_ref") == expectedRef }
            val state = when {
                recs.size == PremiumCaptureCohort.CONTRACTS_PER_COHORT &&
                    linked == recs.size -> "COMPLETE"
                recs.isEmpty() -> "NO_OBSERVATIONS"
                else -> "PARTIAL"
            }
            if (state == "COMPLETE") complete++ else partial++
            acqRows.put(JSONObject()
                .put("acquisition_id", id)
                .put("slot", e.optString("slot"))
                .put("envelope_ref", expectedRef)
                .put("envelope_hash_verified", PremiumObservationContract.verifyHash(e))
                .put("observations", recs.size)
                .put("observations_expected", PremiumCaptureCohort.CONTRACTS_PER_COHORT)
                .put("observations_linked_to_this_envelope", linked)
                .put("unique_instrument_keys", recs.map { it.optString("instrument_key") }
                    .toSet().size)
                .put("batch_state", state))
        }
        // a record whose acquisition has no envelope is UNRESOLVED and must be visible as such
        val orphanRecords = JSONArray()
        for ((id, recs) in byAcq) if (!envByAcq.containsKey(id)) orphanRecords.put(JSONObject()
            .put("acquisition_id", id).put("records", recs.size)
            .put("state", "UNRESOLVED_NO_ENVELOPE"))

        // ── slot manifest ─────────────────────────────────────────────────────────────────────
        val observedSlots = envRead.rows.map { it.optString("slot") }.toSet()
        val expected = expectedSlots()
        val startMin = PremiumCaptureCohort.minuteOfDay(captureStartedSlot)
        val slotRows = JSONArray()
        for (s in expected) {
            val m = PremiumCaptureCohort.minuteOfDay(s) ?: continue
            val acqsHere = envRead.rows.filter { it.optString("slot") == s }
            val state = when {
                acqsHere.size > 1 -> "OBSERVED_MULTIPLE_ACQUISITIONS"
                acqsHere.size == 1 -> "OBSERVED"
                startMin != null && m < startMin ->
                    PremiumObservationContract.OUTCOME_BEFORE_CAPTURE
                schedulerSkippedSlots.contains(s) -> PremiumObservationContract.OUTCOME_SLOT_SKIPPED
                else -> "NOT_OBSERVED_UNEXPLAINED"
            }
            slotRows.put(JSONObject().put("slot", s).put("state", state)
                .put("acquisitions", acqsHere.size)
                .put("records", recRead.rows.count { it.optString("slot") == s }))
        }

        val recArr = JSONArray()
        var hashOk = 0
        recRead.rows.forEach {
            recArr.put(it)
            if (PremiumObservationContract.verifyHash(it)) hashOk++
        }
        val envArr = JSONArray()
        var envHashOk = 0
        envRead.rows.forEach {
            envArr.put(it)
            if (PremiumObservationContract.verifyHash(it)) envHashOk++
        }

        // ── references: resolved only when the actual metadata is here ────────────────────────
        val lotRefs = envRead.rows.mapNotNull {
            it.optJSONObject("lot")?.optString("contract_lot_table_ref")?.takeIf { s -> s.isNotEmpty() }
        }.toSet()
        val mapRefs = envRead.rows.mapNotNull {
            it.optJSONObject("lot")?.optString("map_ref")?.takeIf { s -> s.isNotEmpty() }
        }.toSet()
        val lotAuthoritative = envRead.rows.any {
            it.optJSONObject("lot")?.optBoolean("lot_authoritative_resolved", false) == true
        }
        fun refBlock(names: Set<String>, obj: JSONObject?): JSONObject = JSONObject()
            .put("referenced_names", JSONArray().also { a -> names.forEach { n -> a.put(n) } })
            .put("resolution", if (obj != null) "RESOLVED_FROM_SUPPLIED_METADATA"
                else if (names.isEmpty()) "NO_REFERENCE_RECORDED" else "UNRESOLVED_REFERENCE")
            .put("metadata", obj ?: JSONObject.NULL)
            .put("metadata_ref", if (obj != null)
                PremiumObservationContract.ref(PremiumObservationContract.contentHash(obj))
            else JSONObject.NULL)

        val cohortObj = loaded.cohort
        return JSONObject()
            .put("export_version", EXPORT_VERSION)
            .put("session_version", SESSION_VERSION)
            .put("index", idx)
            .put("session_date", sessionDate)
            .put("is_synthetic", false)
            .put("device_evidence", false)
            .put("claims", JSONObject()
                .put("synchronised_exchange_quotes", false)
                .put("executable_fills", false)
                .put("forecast_of_future_inputs", false)
                // P4: stated per reference rather than as a blanket claim
                .put("offline_resolution_of_lot_table", lotTableObject != null)
                .put("offline_resolution_of_instrument_map", mapObject != null)
                .put("authoritative_lot_resolved", lotAuthoritative)
                .put("purpose", "EVIDENCE_PREPARATION_FOR_FUTURE_IV_FORECASTING"))
            .put("caps", store.capsJson())
            .put("cohort", cohortObj ?: JSONObject.NULL)
            .put("cohort_state", JSONObject()
                .put("state", loaded.state)
                .put("reason", loaded.reason ?: JSONObject.NULL)
                .put("cohort_ref", if (cohortObj != null)
                    PremiumObservationContract.ref(
                        PremiumObservationContract.contentHash(cohortObj))
                else JSONObject.NULL)
                .put("hash_verified", cohortObj != null &&
                    PremiumObservationContract.verifyHash(cohortObj))
                .put("reselection_on_invalid", false))
            .put("references", JSONObject()
                .put("contract_lot_table", refBlock(lotRefs, lotTableObject))
                .put("instrument_map", refBlock(mapRefs, mapObject))
                .put("pinned_contracts", cohortObj?.optJSONArray("pinned_contracts") ?: JSONArray())
                .put("read_from", "SLOT_ENVELOPES"))
            .put("acquisitions", JSONObject()
                .put("rows", acqRows)
                .put("complete_batches", complete)
                .put("partial_or_empty_batches", partial)
                .put("unresolved_record_groups", orphanRecords))
            .put("slot_manifest", JSONObject()
                .put("expected_slots", expected.size)
                .put("observed_slots", observedSlots.size)
                .put("capture_started_slot", captureStartedSlot ?: JSONObject.NULL)
                .put("rows", slotRows))
            .put("record_join", JSONObject()
                .put("keys", "session_date+index+slot+acquisition_id")
                .put("envelope_carries", JSONArray().put("clocks").put("underlying").put("lot"))
                .put("observation_carries", JSONArray().put("identity").put("iv").put("bbo")
                    .put("acquisition_id").put("envelope_ref").put("cohort_ref"))
                .put("rationale",
                    "a flat record exceeded the 2048 B per-record cap and 78 slots of them " +
                        "exceeded the session budget; the per-acquisition facts are identical " +
                        "for all six contracts in one acquisition, so they are written once")
                .put("measured_by",
                    "PremiumCaptureTest.measuredRecordSizesAgainstTheCommittedRawVendorFixture; " +
                        "mirror figures from tools/premium_capture_vectors.py --sizes"))
            .put("integrity", JSONObject()
                .put("corrupt_record_files", JSONArray().also { a ->
                    recRead.corrupt.forEach { a.put(it) } })
                .put("corrupt_envelope_files", JSONArray().also { a ->
                    envRead.corrupt.forEach { a.put(it) } })
                .put("corrupt_conflict_files", JSONArray().also { a ->
                    conRead.corrupt.forEach { a.put(it) } })
                .put("summary_readable", store.summaryIsReadable(index, sessionDate))
                .put("orphan_temp_files", JSONArray().also { a ->
                    store.orphanTempFiles(index, sessionDate).forEach { f -> a.put(f.name) } }))
            .put("durable_counters", store.readCounters(index, sessionDate))
            .put("conflicts", JSONObject()
                .put("retained", conRead.rows.size)
                .put("evidence", JSONArray().also { a -> conRead.rows.forEach { c -> a.put(c) } }))
            .put("accounting", JSONObject()
                .put("records", recRead.rows.size)
                .put("hash_verified", hashOk)
                .put("slot_envelopes", envRead.rows.size)
                .put("envelope_hash_verified", envHashOk)
                .put("conflicts_retained", conRead.rows.size)
                .put("corrupt_files",
                    recRead.corrupt.size + envRead.corrupt.size + conRead.corrupt.size)
                .put("session_bytes", store.sessionBytes(index, sessionDate))
                .put("global_bytes", store.globalBytes())
                .put("orphan_temp_files", store.orphanTempFiles(index, sessionDate).size))
            .put("slot_envelopes", envArr)
            .put("records", recArr)
    }
}
