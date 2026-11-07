package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * Drives one index/session premium observation experiment from already-fetched chain responses,
 * and exports it so every reference resolves offline.
 *
 * Explicitly NOT done here, by design: no network request, no timer, no background wake, no
 * server/database write, no upload queue, no brain-context field, no authority, admission or exit
 * change, and no Paper lock. The caller hands in a response it already had; if capture fails the
 * caller is told and carries on.
 */
class PremiumCaptureSession(private val store: PremiumCaptureStore) {

    companion object {
        const val SESSION_VERSION = "premium_capture_session_v1_one_cohort_per_index_session"
        const val EXPORT_VERSION = "premium_capture_export_v1_offline_resolvable"

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

    /** The pinned cohort lives beside the records, in the store's own session directory. */
    private fun cohortFile(index: String, session: String) =
        File(File(store.root, "$session/${index.uppercase()}"), "cohort.json")

    /** The pinned cohort, restored from disk so a restart never reselects. */
    fun loadCohort(index: String, session: String): JSONObject? {
        val f = cohortFile(index, session)
        if (!f.isFile) return null
        return runCatching { JSONObject(f.readText()) }.getOrNull()
    }

    private fun saveCohort(index: String, session: String, cohort: JSONObject): Boolean =
        runCatching {
            val f = cohortFile(index, session)
            f.parentFile?.mkdirs()
            val tmp = File(f.parentFile, f.name + ".tmp")
            tmp.writeText(cohort.toString())
            if (f.exists()) { tmp.delete(); true } else tmp.renameTo(f)
        }.getOrDefault(false)

    data class SlotResult(
        val status: String,
        val cohortId: String?,
        val written: Int,
        val refused: Int,
        val duplicates: Int,
        val conflicts: Int,
        val outcomes: Map<String, String>,
        val detail: String? = null,
    )

    /**
     * Observe one already-fetched response for one index.
     *
     * `rawChain` null means the acquisition itself failed, which is recorded as
     * MISSING_REQUEST_FAILED for all six pinned contracts rather than as nothing at all.
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
        val slotId = PremiumCaptureCohort.slotIdentity(schedulerSlot, acquisitionHhmm)
            ?: return SlotResult("NO_SLOT_IDENTITY", null, 0, 0, 0, 0, emptyMap(),
                "no scheduler slot and no readable acquisition clock")

        var cohort = loadCohort(index, sessionDate)
        if (cohort == null) {
            val sel = PremiumCaptureCohort.select(rawChain, index, sessionDate, acquisitionHhmm,
                requestedUnderlyingKey, null)
            if (sel.status != PremiumCaptureCohort.SELECTED || sel.cohort == null)
                return SlotResult(sel.status, null, 0, 0, 0, 0, emptyMap(), sel.reason)
            cohort = sel.cohort
            if (!saveCohort(index, sessionDate, cohort))
                return SlotResult("COHORT_PERSIST_FAILED", cohort.optString("cohort_id"),
                    0, 0, 0, 0, emptyMap())
        }

        val clocks = PremiumObservationContract.clockEvidence(
            requestStartedMs, responseReceivedMs, vendorSourceTs)
            .put("slot_basis", slotId.basis)
            .put("actual_acquisition_clock", slotId.actualClock ?: JSONObject.NULL)
        val lot = PremiumObservationContract.lotEvidence(
            claimedLot, claimedLotSource, lotTableRef, mapRef)

        val pinned = PremiumCaptureCohort.pinnedList(cohort)
        // locate first, so the envelope's underlying comes from the SAME acquisition row that the
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

        var written = 0; var refused = 0; var dupes = 0; var conflicts = 0
        val env = PremiumObservationContract.slotEnvelope(
            index.uppercase(), sessionDate, slotId.slot, cohort, clocks, underlying, lot)
        val er = store.putEnvelope(index, sessionDate, slotId.slot, env)
        if (!er.accepted) refused++

        val outcomes = LinkedHashMap<String, String>()
        // every pinned contract gets an outcome, observed or not
        for ((p, loc) in located) {
            val rec = PremiumObservationContract.observation(
                p, cohort, sessionDate, slotId.slot, loc.outcome, loc.row, loc.leg)
            val key = "$sessionDate|${index.uppercase()}|${slotId.slot}|${p.instrumentKey}"
            val r = store.put(index, sessionDate, key, rec)
            outcomes[p.instrumentKey] = loc.outcome
            when (r.status) {
                PremiumCaptureStore.WRITTEN -> written++
                PremiumCaptureStore.DUPLICATE_NO_OP -> dupes++
                PremiumCaptureStore.CONFLICT_ARCHIVED -> conflicts++
                else -> refused++
            }
        }
        return SlotResult("OBSERVED", cohort.optString("cohort_id"),
            written, refused, dupes, conflicts, outcomes,
            detail = "envelope=${er.status}")
    }

    /**
     * Export for offline resolution: the records, the pinned cohort, the caps, the manifest of
     * EXPECTED versus OBSERVED slots, and the referenced objects needed to resolve identity and
     * lot without the device. No fabricated quote is produced for a slot that was never observed.
     */
    fun export(index: String, sessionDate: String, captureStartedSlot: String?,
               schedulerSkippedSlots: List<String> = emptyList()): JSONObject {
        val cohort = loadCohort(index, sessionDate)
        val records = store.readAll(index, sessionDate)
        val observedSlots = records.map { it.optString("slot") }.toSet()
        val expected = expectedSlots()
        val startMin = PremiumCaptureCohort.minuteOfDay(captureStartedSlot)
        val slotRows = JSONArray()
        for (s in expected) {
            val m = PremiumCaptureCohort.minuteOfDay(s) ?: continue
            val state = when {
                observedSlots.contains(s) -> "OBSERVED"
                startMin != null && m < startMin ->
                    PremiumObservationContract.OUTCOME_BEFORE_CAPTURE
                schedulerSkippedSlots.contains(s) -> PremiumObservationContract.OUTCOME_SLOT_SKIPPED
                else -> "NOT_OBSERVED_UNEXPLAINED"
            }
            slotRows.put(JSONObject().put("slot", s).put("state", state)
                .put("records", records.count { it.optString("slot") == s }))
        }
        val recArr = JSONArray()
        var hashOk = 0
        records.forEach {
            recArr.put(it)
            if (PremiumObservationContract.verifyHash(it)) hashOk++
        }
        // the per-acquisition facts, written once per slot; the records join on
        // (session_date, index, slot), so clocks/underlying/lot resolve offline from the export
        val envelopes = store.readEnvelopes(index, sessionDate)
        val envArr = JSONArray()
        var envHashOk = 0
        envelopes.forEach {
            envArr.put(it)
            if (PremiumObservationContract.verifyHash(it)) envHashOk++
        }
        return JSONObject()
            .put("export_version", EXPORT_VERSION)
            .put("session_version", SESSION_VERSION)
            .put("index", index.uppercase())
            .put("session_date", sessionDate)
            .put("is_synthetic", false)
            .put("device_evidence", false)
            .put("claims", JSONObject()
                .put("synchronised_exchange_quotes", false)
                .put("executable_fills", false)
                .put("forecast_of_future_inputs", false)
                .put("purpose", "EVIDENCE_PREPARATION_FOR_FUTURE_IV_FORECASTING"))
            .put("caps", store.capsJson())
            .put("cohort", cohort ?: JSONObject.NULL)
            // the referenced objects, carried so identity and lot resolve with no device present
            .put("references", JSONObject()
                .put("pinned_contracts", cohort?.optJSONArray("pinned_contracts") ?: JSONArray())
                .put("contract_lot_table_ref",
                    records.firstOrNull()?.optJSONObject("lot")?.opt("contract_lot_table_ref")
                        ?: JSONObject.NULL)
                .put("map_ref",
                    records.firstOrNull()?.optJSONObject("lot")?.opt("map_ref") ?: JSONObject.NULL))
            .put("slot_manifest", JSONObject()
                .put("expected_slots", expected.size)
                .put("observed_slots", observedSlots.size)
                .put("capture_started_slot", captureStartedSlot ?: JSONObject.NULL)
                .put("rows", slotRows))
            .put("slot_envelopes", envArr)
            .put("record_join", JSONObject()
                .put("keys", "session_date+index+slot")
                .put("envelope_carries", JSONArray().put("clocks").put("underlying").put("lot"))
                .put("observation_carries", JSONArray().put("identity").put("iv").put("bbo"))
                .put("rationale",
                    "a flat record exceeded the 2048 B per-record cap and 78 slots of them " +
                        "exceeded the session budget; the per-acquisition facts are identical " +
                        "for all six contracts in one acquisition, so they are written once")
                .put("measured_by",
                    "PremiumCaptureTest.measuredRecordSizesAgainstTheCommittedRawVendorFixture; " +
                        "mirror figures from tools/premium_capture_vectors.py --sizes"))
            .put("accounting", JSONObject()
                .put("records", records.size)
                .put("hash_verified", hashOk)
                .put("slot_envelopes", envelopes.size)
                .put("envelope_hash_verified", envHashOk)
                .put("conflicts_retained", store.conflictCount(index, sessionDate))
                .put("session_bytes", store.sessionBytes(index, sessionDate))
                .put("global_bytes", store.globalBytes())
                .put("orphan_temp_files", store.orphanTempFiles(index, sessionDate).size))
            .put("records", recArr)
    }
}
