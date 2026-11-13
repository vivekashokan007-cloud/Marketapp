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
        const val SESSION_VERSION = "premium_capture_session_v3_response_evidence_bound"
        const val EXPORT_VERSION = "premium_capture_export_v3_verified_and_resolved"

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
        /**
         * The write outcome of THIS attempt's own summary — the one carrying the batch, write and
         * refusal deltas. Previously each `bumpCounters` result here was discarded and the
         * coordinator's separate `coordinator_runs` increment was reported in its place, so a
         * transient failure of the summary that matters could be masked by a later success of an
         * unrelated counter. A failed disk cannot durably record its own failure; the returned
         * result must still identify that failure accurately.
         */
        val summaryWrite: PremiumCaptureStore.WriteResult? = null,
        /**
         * The control-reserve record of last resort, attempted ONLY when [summaryWrite] itself
         * failed. Null when the summary was persisted (nothing to escalate) — a non-persisted
         * value here means the refusal could not be recorded durably anywhere, and this returned
         * result is the only account of it.
         */
        val diagnosticWrite: PremiumCaptureStore.WriteResult? = null,
        /**
         * The STORE's own status for a refused write, so the cause can be named on the phone
         * without re-deriving it from the detail text. Null when no store write was refused.
         */
        val storeStatus: String? = null,
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
        // a content reference to the application's own contract authority, recorded once per
        // index/session by the caller and resolved from the export
        productionLotAuthorityRef: String? = null,
        /*
         * The scheduler's INTENDED slot, recorded as evidence beside the slot actually derived
         * from the acquisition clock.
         *
         * This is separate from `schedulerSlot` on purpose. Passing the scheduler's slot as
         * authoritative would relabel a response that arrived just after a five-minute boundary —
         * the poll grid starts at 09:15 and a chain fetch completing at 09:25:01 for a poll that
         * began at 09:24:58 is an ordinary case, not a late response. Codex requires that a
         * disagreement be REPORTED rather than used to relabel, so the real clock decides the slot
         * and the scheduler's intent is carried here with an agreement flag. A caller that does
         * supply a conflicting `schedulerSlot` is still refused, which is the accepted behaviour.
         */
        declaredSchedulerSlot: String? = null,
    ): SlotResult {
        fun refuse(status: String, detail: String?, counter: String,
                   storeStatus: String? = null): SlotResult {
            val at = System.currentTimeMillis()
            val slot = acquisitionHhmm ?: schedulerSlot
            /*
             * AN ATTEMPT THAT FAILED, OR NO ATTEMPT AT ALL — and the two must not share one
             * record.
             *
             * The counter says WHAT failed; the `refusal_*` fields say WHY, durably and at the
             * time, because an app-cap refusal, a low-device-space refusal and an I/O failure
             * are three different operator actions that a capacity reading taken hours later
             * cannot separate.
             *
             * But every session is followed by polls after 15:05, and each one is refused
             * OUTSIDE_EXPERIMENT_WINDOW through this same writer. Those refusals used to
             * overwrite `refusal_*` with an outside-window status and a NULL store status, so by
             * the end of the day the reason the session was empty had been replaced by the
             * reason the 15:30 poll was — exactly the displacement that had just been fixed one
             * layer up, in the About screen's `last_by_index`. The earlier fixture exported
             * immediately and so could not see it.
             *
             * An out-of-window poll is not an attempt to write this session's evidence. It keeps
             * its own count and its own `outside_window_*` fields, and never touches the
             * in-window reason.
             */
            val isAttempt = status != OUTSIDE_WINDOW
            val fields = if (isAttempt)
                mapOf("refusal_counter" to counter,
                      "refusal_status" to status,
                      "refusal_store_status" to storeStatus,
                      "refusal_detail" to detail,
                      "refusal_slot" to slot,
                      "refusal_at_ms" to at)
            else
                mapOf("outside_window_slot" to slot,
                      "outside_window_at_ms" to at)
            val sw = store.bumpCounters(index, sessionDate, mapOf("refused_$counter" to 1L),
                fields)
            // The diagnostic write is itself a write, and the device guard applies to it too. If
            // even this bounded record cannot be saved, fall back to the control reserve so the
            // export can say DIAGNOSTIC_WRITE_REFUSED instead of NOT_WRITTEN, which would read
            // as "nothing was attempted". Only for a real attempt: an out-of-window poll has no
            // reason worth a last-resort record, and writing one would displace the reason that
            // has.
            val dw = if (sw.persisted || !isAttempt) null else runCatching {
                store.recordDiagnosticFailure(index, sessionDate, JSONObject()
                    .put("at_ms", at)
                    .put("slot", slot)
                    .put("slot_status", status)
                    .put("refusal_counter", counter)
                    .put("refusal_store_status", storeStatus ?: JSONObject.NULL)
                    .put("summary_write_status", sw.status)
                    .put("summary_write_detail", sw.detail ?: JSONObject.NULL))
            }.getOrNull()
            return SlotResult(status, null, null, 0, 0, 0, 0, emptyMap(), detail = detail,
                summaryWrite = sw, diagnosticWrite = dw, storeStatus = storeStatus)
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
                return refuse(COHORT_PERSIST_FAILED, "${cr.status}: ${cr.detail}",
                    "cohort_persist", storeStatus = cr.status)
            cohort = sel.cohort
        }

        val clocks = PremiumObservationContract.clockEvidence(
            requestStartedMs, responseReceivedMs, vendorSourceTs)
            .put("slot_basis", slotId.basis)
            .put("actual_acquisition_clock", slotId.actualClock ?: JSONObject.NULL)
            .put("scheduler_slot_declared", declaredSchedulerSlot ?: JSONObject.NULL)
            .put("scheduler_slot_agrees_with_clock",
                if (declaredSchedulerSlot.isNullOrBlank()) JSONObject.NULL
                else declaredSchedulerSlot == slotId.slot)
        val lot = PremiumObservationContract.lotEvidence(
            claimedLot, claimedLotSource, lotTableRef, mapRef, productionLotAuthorityRef)

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

        // R2-1: the complete batch evidence is prepared BEFORE anything is persisted, and the
        // acquisition identity covers its digest. A quote-only change therefore cannot resume a
        // partially written batch under the earlier identity.
        val prepared = located.map { (p, loc) ->
            PremiumObservationContract.observationEvidence(p, loc.outcome, loc.row, loc.leg)
        }
        val batchRef = PremiumObservationContract.batchEvidenceRef(prepared)
        val requestOutcome = if (rawChain == null) PremiumObservationContract.REQUEST_FAILED
        else PremiumObservationContract.REQUEST_RESPONSE_PRESENT
        // Explicit attempt policy: with no clock and no vendor timestamp, two attempts carrying
        // identical evidence are INDISTINGUISHABLE and collapse to one idempotent acquisition.
        // That is recorded rather than hidden, because for a failed request the evidence is
        // identical by construction and the attempt count is genuinely not recoverable.
        val distinguishable = requestStartedMs != null || responseReceivedMs != null ||
            !vendorSourceTs.isNullOrBlank()
        val env = PremiumObservationContract.slotEnvelope(
            index.uppercase(), sessionDate, slotId.slot, cohort, clocks, underlying, lot,
            batchRef, requestOutcome, distinguishable)
        val acqId = env.optString("acquisition_id")
        // P2: the envelope key includes the acquisition id, so a same-slot retry with different
        // evidence becomes a DISTINCT acquisition instead of a conflict against the first one
        val envKey = "$sessionDate|${index.uppercase()}|${slotId.slot}|$acqId|ENVELOPE"
        val er = store.putEnvelope(index, sessionDate, envKey, env)
        if (!er.persisted) {
            // FAIL CLOSED: with no envelope there is nothing for an observation to resolve
            // against, so no observation is written. The old code wrote all six anyway.
            val sw = store.bumpCounters(index, sessionDate,
                mapOf("refused_envelope_not_persisted" to 1L, "batches_refused" to 1L))
            return SlotResult(ENVELOPE_NOT_PERSISTED, cohort.optString("cohort_id"), acqId,
                0, 1, 0, 0, emptyMap(), detail = "${er.status}: ${er.detail}",
                summaryWrite = sw)
        }

        var written = 0; var refused = 0; var dupes = 0; var conflicts = 0
        val outcomes = LinkedHashMap<String, String>()
        val deltas = HashMap<String, Long>()
        // every pinned contract gets an outcome, observed or not
        for ((i, pair) in located.withIndex()) {
            val (p, loc) = pair
            val rec = PremiumObservationContract.observation(
                prepared[i], cohort, sessionDate, index.uppercase(), slotId.slot, env)
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
        // the summary that carries the batch/write/refusal deltas: its own outcome is returned,
        // not replaced by a later unrelated counter increment
        val summaryWrite = store.bumpCounters(index, sessionDate, deltas)

        return SlotResult(OBSERVED, cohort.optString("cohort_id"), acqId,
            written, refused, dupes, conflicts, outcomes, batchComplete = complete,
            detail = "envelope=${er.status}", summaryWrite = summaryWrite)
    }

    /**
     * Where an export's evidence actually came from. **Required, with no default**, so no caller
     * can inherit a flattering claim by omission.
     *
     * R2-2/labelling (Codex): `export()` hardcoded `is_synthetic: false`, while the acceptance run
     * was a transformed fixture replay — historical vendor quotes with GENERATED receipt times,
     * MANUALLY ASSIGNED scheduler slots and a CONSTRUCTED lot table. Whatever the provenance of
     * the quote numbers, the acquisition metadata was invented, and the export said otherwise.
     * A future device export must obtain this from the real capture boundary.
     */
    data class EvidenceContext(
        val sourceKind: String,            // LIVE_DEVICE_CAPTURE | TRANSFORMED_FIXTURE_REPLAY
        val quoteProvenance: String,
        val clockProvenance: String,
        val slotProvenance: String,
        val lotTableProvenance: String,
        val deviceEvidence: Boolean,
        val syntheticFields: List<String>,
        val note: String? = null,
    ) {
        fun json(): JSONObject = JSONObject()
            .put("source_kind", sourceKind)
            .put("quote_provenance", quoteProvenance)
            .put("clock_provenance", clockProvenance)
            .put("slot_provenance", slotProvenance)
            .put("lot_table_provenance", lotTableProvenance)
            .put("device_evidence", deviceEvidence)
            .put("synthetic_fields", JSONArray().also { a -> syntheticFields.forEach { a.put(it) } })
            .put("note", note ?: JSONObject.NULL)

        companion object {
            /** The acceptance replay: real historical quotes, invented timing and metadata. */
            fun transformedFixtureReplay(fixture: String, note: String? = null) = EvidenceContext(
                sourceKind = "TRANSFORMED_FIXTURE_REPLAY",
                quoteProvenance = "HISTORICAL_VENDOR_FIXTURE:$fixture",
                clockProvenance = "SYNTHETIC_GENERATED_IN_TEST",
                slotProvenance = "MANUALLY_ASSIGNED_IN_TEST",
                lotTableProvenance = "CONSTRUCTED_TEST_TABLE",
                deviceEvidence = false,
                syntheticFields = listOf("clocks.acquisition_request_ms",
                    "clocks.acquisition_response_ms", "clocks.slot_basis", "slot",
                    "lot.contract_lot_table_ref", "lot.map_ref", "lot.lot_claimed_raw"),
                note = note,
            )

            /**
             * A session SYNTHESISED from a committed fixture: the quotes are derived from real
             * vendor rows by a deterministic generator, not reprinted from them, and every clock,
             * slot and lot reference is invented in the test.
             *
             * This exists because the full-session engineering test drives the production export
             * builder, which stamped `source_kind: LIVE_DEVICE_CAPTURE` and
             * `device_evidence: true` on files that never went near a phone. The export now has
             * to be told what it is, and the labelling is asserted rather than assumed.
             */
            fun syntheticPerturbedFixture(fixture: String, generator: String,
                                          note: String? = null) = EvidenceContext(
                sourceKind = "SYNTHETIC_PERTURBED_FIXTURE_REPLAY",
                quoteProvenance = "DERIVED_FROM_FIXTURE_BY_GENERATOR:$fixture:$generator",
                clockProvenance = "SYNTHETIC_GENERATED_IN_TEST",
                slotProvenance = "MANUALLY_ASSIGNED_IN_TEST",
                lotTableProvenance = "CONSTRUCTED_TEST_TABLE",
                deviceEvidence = false,
                syntheticFields = listOf("quotes.bid_price", "quotes.ask_price", "quotes.ltp",
                    "underlying_spot_price", "clocks.acquisition_request_ms",
                    "clocks.acquisition_response_ms", "clocks.slot_basis", "slot",
                    "lot.contract_lot_table_ref", "lot.map_ref", "lot.lot_claimed_raw"),
                note = note ?: "every price in this artifact is synthetic: no phone, no broker " +
                    "and no market data took part. It establishes collection and export " +
                    "coverage only, and counts toward no research session.",
            )
        }
    }

    /**
     * Export for offline audit.
     *
     * `lotTableObject` / `mapObject` are the ACTUAL dated metadata. When absent, or when the
     * resolver cannot validate them, the export says so per reference and per acquisition and
     * claims no offline resolution: a retained reference string is a name, not the data it names.
     */
    fun export(index: String, sessionDate: String, captureStartedSlot: String?,
               context: EvidenceContext,
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
        // `associateBy` would silently keep the last of a duplicated key, so duplicates are
        // detected and reported instead of being trusted away by a map overwrite (Codex, 4).
        val duplicateAcqIds = envRead.rows.map { it.optString("acquisition_id") }
            .groupingBy { it }.eachCount().filterValues { it > 1 }.keys
        val envByAcq = envRead.rows.associateBy { it.optString("acquisition_id") }
        // R2-3: PHYSICAL completeness and VERIFIED usability are separate verdicts. The previous
        // predicate counted six parsed records with matching envelope references and called that
        // COMPLETE — it checked no record hash, no envelope hash, not the expected pinned-key set
        // and no join dimension, so a parseable record edited on disk kept its envelope reference
        // and still counted as complete while its own hash failed.
        val expectedKeys = PremiumCaptureCohort.pinnedList(loaded.cohort)
            .map { it.instrumentKey }.toSet()
        val cohortRef = loaded.cohort?.let {
            PremiumObservationContract.ref(PremiumObservationContract.contentHash(it)) }
        val acqRows = JSONArray()
        val hashFailures = JSONArray()
        var structuralComplete = 0; var partial = 0; var verified = 0
        for (e in envRead.rows) {
            val id = e.optString("acquisition_id")
            val recs = byAcq[id] ?: emptyList()
            val expectedRef = PremiumObservationContract.ref(
                PremiumObservationContract.contentHash(e))
            val linked = recs.count { it.optString("envelope_ref") == expectedRef }
            val keys = recs.map { it.optString("instrument_key") }.toSet()
            val structural = when {
                recs.size == PremiumCaptureCohort.CONTRACTS_PER_COHORT &&
                    linked == recs.size -> "COMPLETE_STRUCTURAL"
                recs.isEmpty() -> "NO_OBSERVATIONS"
                else -> "PARTIAL"
            }
            if (structural == "COMPLETE_STRUCTURAL") structuralComplete++ else partial++

            // integrity, which is what makes a group USABLE as evidence
            val why = JSONArray()
            val envOk = PremiumObservationContract.verifyHash(e)
            if (!envOk) why.put("envelope_hash_invalid")

            // (1) the VERSIONS this verified contract covers. Older evidence is retained and
            // exported, never rewritten as current, and never silently verified.
            val envVer = e.optString("record_version")
            if (envVer !in PremiumObservationContract.SUPPORTED_ENVELOPE_VERSIONS)
                why.put("unsupported_envelope_version:$envVer")
            val recVersions = recs.map { it.optString("record_version") }.toSet()
            recVersions.filter { it !in PremiumObservationContract.SUPPORTED_RECORD_VERSIONS }
                .forEach { why.put("unsupported_record_version:$it") }

            // (2) RECOMPUTE the batch digest from the records, in pinned-cohort order, and
            // compare it with the envelope's. A partial group cannot satisfy a complete-batch
            // binding, so it is reported as unverifiable for that reason rather than by omission.
            val storedBatchRef = e.optString("batch_evidence_ref")
            if (storedBatchRef.isEmpty()) why.put("envelope_carries_no_batch_evidence_ref")
            else if (expectedKeys.isEmpty()) why.put("cohort_unavailable_for_batch_recomputation")
            else {
                val byKey = recs.associateBy { it.optString("instrument_key") }
                val ordered = PremiumCaptureCohort.pinnedList(loaded.cohort)
                    .map { byKey[it.instrumentKey] }
                if (ordered.any { it == null }) why.put("batch_incomplete_cannot_bind")
                else {
                    val recomputed = PremiumObservationContract.batchEvidenceRef(
                        ordered.map { PremiumObservationContract.evidenceFromRecord(it!!) })
                    if (recomputed != storedBatchRef) why.put("batch_evidence_ref_mismatch")
                }
            }

            // (3) RECOMPUTE the acquisition id from the envelope's declared basis. Non-circular:
            // the basis excludes the id and both hash fields.
            val recomputedAcq = PremiumObservationContract.acquisitionId(e)
            if (recomputedAcq != id) why.put("acquisition_id_not_derived_from_its_own_basis")

            // (4) the envelope itself must belong to the session and index being exported, and to
            // the validated cohort — not merely agree with the records that point at it
            if (e.optString("session_date") != sessionDate) why.put("envelope_session_foreign")
            if (e.optString("index") != idx) why.put("envelope_index_foreign")
            if (loaded.state != "VALID") why.put("cohort_not_valid_for_verification")
            if (duplicateAcqIds.contains(id)) why.put("duplicate_envelope_acquisition_id")
            val badHashes = recs.filter { !PremiumObservationContract.verifyHash(it) }
            badHashes.forEach { r ->
                why.put("record_hash_invalid")
                val ident = JSONObject()
                    .put("acquisition_id", id)
                    .put("slot", r.optString("slot"))
                    .put("instrument_key", r.optString("instrument_key"))
                    .put("claimed_payload_sha256",
                        r.optString(PremiumObservationContract.HASH_OMITTED_FIELD))
                hashFailures.put(ident)
            }
            if (structural != "COMPLETE_STRUCTURAL") why.put("batch_not_structurally_complete")
            if (expectedKeys.isEmpty()) why.put("cohort_unavailable_for_key_check")
            else if (keys != expectedKeys) why.put("pinned_key_set_mismatch")
            if (recs.size != keys.size) why.put("duplicate_instrument_keys")
            // every join dimension must agree with the envelope and the cohort
            if (recs.any { it.optString("session_date") != e.optString("session_date") })
                why.put("session_date_mismatch")
            if (recs.any { it.optString("index") != e.optString("index") })
                why.put("index_mismatch")
            if (recs.any { it.optString("slot") != e.optString("slot") })
                why.put("slot_mismatch")
            if (cohortRef == null) why.put("cohort_ref_unavailable")
            else {
                if (e.optString("cohort_ref") != cohortRef) why.put("envelope_cohort_ref_mismatch")
                if (recs.any { it.optString("cohort_ref") != cohortRef })
                    why.put("record_cohort_ref_mismatch")
            }
            if (recs.any { it.optString("acquisition_id") != id })
                why.put("acquisition_id_mismatch")
            val isVerified = why.length() == 0
            if (isVerified) verified++

            acqRows.put(JSONObject()
                .put("acquisition_id", id)
                .put("slot", e.optString("slot"))
                .put("envelope_ref", expectedRef)
                .put("batch_evidence_ref", e.opt("batch_evidence_ref") ?: JSONObject.NULL)
                .put("request_outcome", e.opt("request_outcome") ?: JSONObject.NULL)
                .put("attempt_distinguishable",
                    e.opt("attempt_distinguishable") ?: JSONObject.NULL)
                .put("envelope_hash_verified", envOk)
                .put("observations", recs.size)
                .put("observations_expected", PremiumCaptureCohort.CONTRACTS_PER_COHORT)
                .put("observations_linked_to_this_envelope", linked)
                .put("unique_instrument_keys", keys.size)
                .put("records_with_invalid_hash", badHashes.size)
                // the two verdicts, deliberately not collapsed into one word
                .put("structural_state", structural)
                .put("verified_state", if (isVerified) "VERIFIED" else "UNVERIFIED")
                .put("integrity_reasons", why))
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
        // R2-2: resolution is per acquisition and comes from an actual resolver run against the
        // supplied table — never from `obj != null`, and never from `any { claims authority }`.
        val underlying = loaded.cohort?.optString("underlying_key") ?: ""
        val lotResolutions = JSONArray()
        var lotResolved = 0; var lotConflicting = 0; var lotUnresolved = 0
        for (e in envRead.rows) {
            val envRef = e.optJSONObject("lot")?.optString("contract_lot_table_ref")
            val v = PremiumObservationContract.resolveLotFromTable(
                lotTableObject, envRef, underlying, sessionDate,
                e.optJSONObject("lot")?.optJSONObject("lot_claimed_raw")?.opt("raw"))
            when (v.optString("lot_resolution")) {
                PremiumObservationContract.LOT_RESOLVED -> lotResolved++
                PremiumObservationContract.LOT_CONFLICTS_WITH_CLAIM -> lotConflicting++
                else -> lotUnresolved++
            }
            lotResolutions.put(JSONObject(v.toString())
                .put("acquisition_id", e.optString("acquisition_id")))
        }
        val allLotResolved = envRead.rows.isNotEmpty() && lotResolved == envRead.rows.size

        /*
         * AUTHORITY IS THE PRODUCTION TABLE'S VERDICT ONLY, computed once here and used both for
         * the `claims` block and for `references.production_lot_authority`, so the two can never
         * disagree.
         *
         * Self-correction, found by reading the green acceptance artifact rather than the code:
         * `claims.authoritative_lot_resolved` was `allLotResolved`, which is true whenever a
         * SUPPLIED OFFLINE table resolves every acquisition -- including the constructed table
         * the tests supply. The artifact therefore claimed `authoritative_lot_resolved: true`
         * while `references.production_lot_authority` in the same document said
         * `authoritative: false, resolved: false, recorded: false`, and while the verified
         * contract listed `authoritative_lot_resolution` under `verified_does_not_mean`. That is
         * the overclaim R2-2 was about, surviving at the top level after being fixed at the
         * record level: a test-constructed table could name itself authoritative.
         *
         * Resolving a reference and holding authority are different things. `offline_resolution_
         * of_lot_table` keeps the former; authority now requires the application's own
         * ContractLotTable to have been recorded, resolved, marked authoritative, AND referenced
         * by content from every envelope that carries a reference.
         */
        val prodAuthRec = store.readLotAuthority(index, sessionDate)
        val prodAuthActual = prodAuthRec?.let {
            PremiumObservationContract.ref(PremiumObservationContract.contentHash(it))
        }
        /*
         * COVERAGE IS PER ACQUISITION AND MUST BE COMPLETE.
         *
         * The previous version did `mapNotNull { ... production_authority_ref }` and then asked
         * whether all REMAINING DISTINCT references matched. An envelope carrying no reference at
         * all was silently dropped from the set, so one referenced envelope plus one unreferenced
         * envelope satisfied a whole-session authority claim. Partial coverage is now visible and
         * disqualifying: every envelope is examined, and a missing reference is a reported gap
         * rather than an omission.
         */
        val prodAuthCoverage = JSONArray()
        var prodAuthMatched = 0
        var prodAuthMissingRef = 0
        var prodAuthMismatched = 0
        val prodAuthRefs = LinkedHashSet<String>()
        for (e in envRead.rows) {
            val ref = e.optJSONObject("lot")?.optString("production_authority_ref")
                ?.takeIf { r -> r.isNotEmpty() }
            val state = when {
                ref == null -> { prodAuthMissingRef++; "NO_AUTHORITY_REFERENCE_RECORDED" }
                prodAuthActual == null -> { prodAuthMissingRef++; "NO_AUTHORITY_RECORD_TO_MATCH" }
                ref == prodAuthActual -> { prodAuthMatched++; prodAuthRefs.add(ref); "MATCHED" }
                else -> { prodAuthMismatched++; prodAuthRefs.add(ref); "REFERENCE_DOES_NOT_MATCH" }
            }
            prodAuthCoverage.put(JSONObject()
                .put("acquisition_id", e.optString("acquisition_id"))
                .put("slot", e.optString("slot"))
                .put("production_authority_ref", ref ?: JSONObject.NULL)
                .put("state", state))
        }
        val prodAuthRefMatches = envRead.rows.isNotEmpty() &&
            prodAuthMatched == envRead.rows.size &&
            prodAuthMissingRef == 0 && prodAuthMismatched == 0

        /*
         * AND THE RECORD MUST BELONG TO THIS PINNED CONTEXT.
         *
         * The authority is resolved from the REQUEST's expiry, before a cohort is selected or
         * loaded, and it is first-write-wins. So a record written for one expiry must not grant
         * authority to a cohort that pinned a different one. The requested context is carried on
         * the record for exactly this check; if it is absent (an older record) the context cannot
         * be confirmed and authority does not hold.
         */
        val pinnedExpiry = loaded.cohort?.optString("expiry")?.takeIf { it.isNotEmpty() }
        val authCtxIndex = prodAuthRec?.optString("requested_index")?.takeIf { it.isNotEmpty() }
        val authCtxSession = prodAuthRec?.optString("requested_session_date")
            ?.takeIf { it.isNotEmpty() }
        val authCtxExpiry = prodAuthRec?.optString("requested_expiry")?.takeIf { it.isNotEmpty() }
        val authCtxReasons = JSONArray()
        if (prodAuthRec != null) {
            if (authCtxIndex == null || authCtxSession == null)
                authCtxReasons.put("authority_record_states_no_requested_context")
            else {
                if (authCtxIndex != index) authCtxReasons.put("authority_index_differs")
                if (authCtxSession != sessionDate) authCtxReasons.put("authority_session_differs")
            }
            if (pinnedExpiry != null && authCtxExpiry != null && authCtxExpiry != pinnedExpiry)
                authCtxReasons.put("authority_expiry_differs_from_pinned_cohort")
            if (pinnedExpiry != null && authCtxExpiry == null)
                authCtxReasons.put("authority_record_states_no_expiry_to_compare")
            if (prodAuthRec.optBoolean("lot_conflict", false))
                authCtxReasons.put("authority_record_reports_lot_conflict")
            val tv = prodAuthRec.optString("lot_table_version")
            if (tv.isEmpty()) authCtxReasons.put("authority_record_states_no_table_version")
            else if (tv != ContractLotTable.VERSION_ID)
                authCtxReasons.put("authority_table_version_differs_from_current")
        }
        val prodAuthContextAgrees = prodAuthRec != null && authCtxReasons.length() == 0

        val productionLotAuthoritative = prodAuthRec != null &&
            prodAuthRec.optBoolean("resolved", false) &&
            prodAuthRec.optBoolean("authoritative", false) &&
            !prodAuthRec.optBoolean("lot_conflict", false) &&
            prodAuthRefMatches && prodAuthContextAgrees

        /**
         * Metadata PRESENCE is not reference resolution. A supplied object is only ever reported
         * as present-but-unvalidated unless a resolver actually checked it.
         */
        fun refBlock(names: Set<String>, obj: JSONObject?, resolution: String): JSONObject =
            JSONObject()
                .put("referenced_names", JSONArray().also { a -> names.forEach { n -> a.put(n) } })
                .put("metadata_present", obj != null)
                .put("metadata_provenance",
                    obj?.optString("provenance")?.ifEmpty { "UNDECLARED" } ?: JSONObject.NULL)
                .put("resolution", resolution)
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
            // the caller must STATE the provenance; nothing about it is hardcoded here
            .put("evidence_context", context.json())
            .put("is_synthetic", context.syntheticFields.isNotEmpty())
            .put("device_evidence", context.deviceEvidence)
            .put("claims", JSONObject()
                .put("synchronised_exchange_quotes", false)
                .put("executable_fills", false)
                .put("forecast_of_future_inputs", false)
                // R2-2: resolution claimed only when the resolver resolved EVERY acquisition
                .put("offline_resolution_of_lot_table", allLotResolved)
                .put("offline_resolution_of_instrument_map", false)
                // resolving a reference is NOT holding authority: see the note above
                .put("authoritative_lot_resolved", productionLotAuthoritative)
                .put("lot_authority_basis",
                    "PRODUCTION_CONTRACT_LOT_TABLE_RECORDED_RESOLVED_AND_REFERENCED_BY_CONTENT")
                .put("a_supplied_offline_table_can_confer_authority", false)
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
                .put("contract_lot_table", refBlock(lotRefs, lotTableObject,
                    if (lotTableObject == null)
                        (if (lotRefs.isEmpty()) "NO_REFERENCE_RECORDED" else "UNRESOLVED_REFERENCE")
                    else if (allLotResolved) "RESOLVED_FOR_EVERY_ACQUISITION"
                    else "PARTIALLY_RESOLVED_SEE_COVERAGE")
                    .put("resolution_coverage", JSONObject()
                        .put("acquisitions", envRead.rows.size)
                        .put("resolved", lotResolved)
                        .put("conflicting_with_claim", lotConflicting)
                        .put("unresolved", lotUnresolved))
                    .put("per_acquisition", lotResolutions))
                // no schema is validated for the instrument map, so presence is all that is
                // claimed for it — a named reference is not the data it names
                // the application's own contract authority, resolved from the per-index/session
                // record the envelopes reference by content. Separate from the generic
                // supplied-table resolver above, which is a TEST path and must never stand in
                // for it.
                // the SAME verdict the `claims` block used, not a second computation that could
                // drift away from it
                .put("production_lot_authority", JSONObject().also { pa ->
                    pa.put("recorded", prodAuthRec != null)
                        .put("authority", prodAuthRec ?: JSONObject.NULL)
                        .put("referenced_by_envelopes", JSONArray().also { a ->
                            prodAuthRefs.forEach { r -> a.put(r) } })
                        .put("reference_matches_record", prodAuthRefMatches)
                        .put("resolved", prodAuthRec?.optBoolean("resolved", false) ?: false)
                        .put("authoritative",
                            prodAuthRec?.optBoolean("authoritative", false) ?: false)
                        .put("lot_conflict", prodAuthRec?.optBoolean("lot_conflict", false)
                            ?: false)
                        .put("authority_holds", productionLotAuthoritative)
                        // per-acquisition coverage, including the gaps. A whole-session claim
                        // requires EVERY envelope to be covered; a missing reference is reported
                        // rather than dropped from the comparison.
                        .put("coverage", JSONObject()
                            .put("envelopes", envRead.rows.size)
                            .put("matched", prodAuthMatched)
                            .put("missing_reference", prodAuthMissingRef)
                            .put("reference_mismatched", prodAuthMismatched)
                            .put("complete", prodAuthRefMatches)
                            .put("per_acquisition", prodAuthCoverage))
                        // the pinned context this authority was checked against
                        .put("pinned_context", JSONObject()
                            .put("pinned_expiry", pinnedExpiry ?: JSONObject.NULL)
                            .put("authority_requested_index", authCtxIndex ?: JSONObject.NULL)
                            .put("authority_requested_session_date",
                                authCtxSession ?: JSONObject.NULL)
                            .put("authority_requested_expiry", authCtxExpiry ?: JSONObject.NULL)
                            .put("current_table_version", ContractLotTable.VERSION_ID)
                            .put("agrees", prodAuthContextAgrees)
                            .put("reasons", authCtxReasons))
                        // A neutral block-kind label when there is no record: a constant `true`
                        // here asserted a property of something absent.
                        .put("block_kind", if (prodAuthRec != null)
                            "THE_APPLICATIONS_OWN_CONTRACT_LOT_TABLE"
                        else "NO_AUTHORITY_RECORD_PRESENT")
                        .put("is_the_applications_own_contract_table",
                            if (prodAuthRec != null) true else JSONObject.NULL)
                        .put("generic_supplied_table_substituted", false)
                })
                .put("instrument_map", refBlock(mapRefs, mapObject,
                    if (mapObject != null) "UNVALIDATED_METADATA_PRESENT"
                    else if (mapRefs.isEmpty()) "NO_REFERENCE_RECORDED" else "UNRESOLVED_REFERENCE"))
                .put("pinned_contracts", cohortObj?.optJSONArray("pinned_contracts") ?: JSONArray())
                .put("read_from", "SLOT_ENVELOPES"))
            .put("acquisitions", JSONObject()
                .put("rows", acqRows)
                .put("structurally_complete_batches", structuralComplete)
                .put("verified_batches", verified)
                .put("usable_evidence_acquisitions", verified)
                .put("partial_or_empty_batches", partial)
                .put("duplicate_acquisition_ids", JSONArray().also { a ->
                    duplicateAcqIds.forEach { a.put(it) } })
                .put("hash_failure_identifiers", hashFailures)
                .put("unresolved_record_groups", orphanRecords)
                // what VERIFIED does and does not mean, stated in the artifact itself
                .put("verified_contract", JSONObject()
                    .put("supported_record_versions", JSONArray().also { a ->
                        PremiumObservationContract.SUPPORTED_RECORD_VERSIONS.forEach { a.put(it) } })
                    .put("supported_envelope_versions", JSONArray().also { a ->
                        PremiumObservationContract.SUPPORTED_ENVELOPE_VERSIONS.forEach { a.put(it) } })
                    .put("verified_means",
                        "INTEGRITY_AND_LINKAGE_UNDER_THIS_CONTRACT_ONLY")
                    .put("verified_does_not_mean", JSONArray()
                        .put("valid_or_executable_quotes")
                        .put("a_usable_forecast_label")
                        .put("authoritative_lot_resolution")
                        .put("a_tradeable_contract"))
                    .put("checks", JSONArray()
                        .put("record_and_envelope_payload_hashes")
                        .put("supported_record_and_envelope_versions")
                        .put("batch_evidence_ref_recomputed_from_records_in_pinned_order")
                        .put("acquisition_id_recomputed_from_the_envelope_basis")
                        .put("expected_pinned_key_set_and_no_duplicate_keys")
                        .put("envelope_session_index_against_the_requested_export")
                        .put("record_to_envelope_and_cohort_joins")
                        .put("no_duplicate_envelope_acquisition_ids"))))
            .put("slot_manifest", JSONObject()
                .put("expected_slots", expected.size)
                .put("observed_slots", observedSlots.size)
                .put("capture_started_slot", captureStartedSlot ?: JSONObject.NULL)
                .put("rows", slotRows))
            .put("record_join", JSONObject()
                .put("keys", "session_date+index+slot+acquisition_id")
                .put("envelope_carries", JSONArray().put("clocks").put("underlying").put("lot")
                    .put("batch_evidence_ref").put("request_outcome")
                    .put("attempt_distinguishable"))
                .put("observation_carries", JSONArray().put("identity").put("iv").put("bbo")
                    .put("acquisition_id").put("envelope_ref").put("cohort_ref"))
                .put("acquisition_identity_covers", JSONArray().put("session_date").put("index")
                    .put("slot").put("cohort_ref").put("clocks").put("underlying").put("lot")
                    .put("batch_evidence_ref").put("request_outcome"))
                .put("rationale",
                    "a flat record exceeded the 2048 B per-record cap and 78 slots of them " +
                        "exceeded the session budget; the per-acquisition facts are identical " +
                        "for all six contracts in one acquisition, so they are written once")
                .put("measured_by",
                    "PremiumCaptureTest.measuredRecordSizesAgainstTheCommittedRawVendorFixture " +
                        "(the Kotlin producer; the Python mirror no longer measures sizes)"))
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
                .put("global_bytes", store.globalBytesFast())
                .put("orphan_temp_files", store.orphanTempFiles(index, sessionDate).size))
            .put("slot_envelopes", envArr)
            .put("records", recArr)
    }
}
