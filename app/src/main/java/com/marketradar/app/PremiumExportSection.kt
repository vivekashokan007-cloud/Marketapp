package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * Assembles the premium evidence section for the EXISTING session-evidence export, and merges it
 * into the existing bundle.
 *
 * Kept out of `NativeBridge` on purpose: the bridge needs an Android `Context`, which cannot be
 * constructed in a JVM unit test, so the assembly that actually matters would be untestable there.
 * This object takes a plain `filesDir`, so the tests exercise the real production assembly rather
 * than a second model of it. The bridge is a thin adapter over these functions.
 *
 * Boundaries this must hold:
 *  - the requested IST session date only; never today, never another retained date;
 *  - NF and BNF separately, under explicit identities, with no cross-index substitution;
 *  - a new top-level `premium` key — no existing part's meaning changes;
 *  - the combined final byte limit is enforced before saving, the old limit is never raised, and
 *    old parts are never truncated;
 *  - a concurrent capture write is reported, not silently served as a torn view.
 */
object PremiumExportSection {

    const val SECTION_VERSION = "premium_export_section_v1_requested_session_only"

    /** The existing combined export limit. Not raised here, only enforced. */
    const val COMBINED_CAP_BYTES = 4 * 1024 * 1024 + 65536

    /**
     * How a DEVICE export must describe itself. Device evidence means the observation came from a
     * real acquisition on this phone — it does **not** mean synchronised exchange ticks or
     * executable fills, and the acquisition clocks are device-local with the exchange event
     * semantics unverified. `deviceEvidence = true` is paired with an explicit statement of what
     * it does not imply, rather than left to be read optimistically.
     */
    fun deviceContext() = PremiumCaptureSession.EvidenceContext(
        sourceKind = "LIVE_DEVICE_CAPTURE",
        quoteProvenance = "VENDOR_OPTION_CHAIN_RESPONSE_AS_RECEIVED",
        clockProvenance = "DEVICE_LOCAL_ACQUISITION_RECEIPT_EXCHANGE_EVENT_SEMANTICS_UNVERIFIED",
        slotProvenance = "DERIVED_FROM_EACH_INDEX_OWN_ACQUISITION_CLOCK",
        lotTableProvenance = "CONTRACT_LOT_TABLE:${ContractLotTable.VERSION_ID}",
        deviceEvidence = true,
        syntheticFields = emptyList(),
        note = "device evidence does not mean synchronised exchange ticks or executable fills",
    )

    /**
     * The premium section for ONE requested session.
     *
     * A consistency check brackets each index's assembly: its byte total and record count are read
     * before and after, and a concurrent capture write is REPORTED rather than silently producing
     * a torn view.
     */
    /**
     * @param context how the export must DESCRIBE ITSELF. Defaults to [deviceContext] — the
     *   phone's own path is unchanged and callers there pass nothing. A test that drives this
     *   builder has to pass its own provenance, because an artifact produced from a fixture must
     *   not be labelled as live device capture, whichever code assembled it.
     */
    fun build(filesDir: File, date: String,
              context: PremiumCaptureSession.EvidenceContext = deviceContext()): JSONObject {
        val section = JSONObject()
            .put("section_version", SECTION_VERSION)
            .put("requested_session_date", date)
            .put("indices", JSONObject())
        return try {
            if (!PremiumObservationContract.isCalendarDate(date))
                return section.put("state", "REQUESTED_DATE_INVALID").put("has_data", false)
                    .put("source_kind", context.sourceKind)
                    .put("device_evidence", context.deviceEvidence)
            val root = PremiumCaptureControl.root(filesDir)
            if (!root.exists()) return section.put("state", "NO_PREMIUM_DATA")
                .put("has_data", false)
                .put("source_kind", context.sourceKind)
                .put("device_evidence", context.deviceEvidence)
                .put("capture_enabled", PremiumCaptureControl.isEnabled(filesDir))
            val store = PremiumCaptureStore(root)
            val session = PremiumCaptureSession(store)
            var any = false
            var torn = false
            for (idx in listOf("NF", "BNF")) {
                if (store.readCohortText(idx, date) == null &&
                    store.outcomeCount(idx, date) == 0) {
                    /*
                     * An index that captured nothing still has to SAY WHY.
                     *
                     * This branch used to emit the state alone, so a completed export of an
                     * empty session carried no counter, no refusal reason and no remaining
                     * bytes — on 13 November 2026 the whole index block was one key and the
                     * cause had to be inferred from phone logs that do not record persistence
                     * outcomes at all.
                     *
                     * What is added is diagnostic only: the durable summary if one exists, and
                     * the store's capacity. Neither is evidence, and neither changes
                     * eligibility — `state` stays NO_PREMIUM_DATA_FOR_THIS_INDEX and the
                     * records, envelopes and pinned contracts a replay requires are still
                     * absent, so capture preflight still refuses this index.
                     */
                    val empty = JSONObject().put("state", "NO_PREMIUM_DATA_FOR_THIS_INDEX")
                        .put("source_kind", context.sourceKind)
                        .put("device_evidence", context.deviceEvidence)
                        .put("diagnostics_only",
                            "NOT_EVIDENCE_AND_NOT_ELIGIBILITY: why this index is empty")
                        .put("capacity", runCatching { store.capacitySnapshot() }
                            .getOrElse { JSONObject().put("unavailable", it.javaClass.simpleName) })
                    val summary = runCatching { store.readSummary(idx, date) }.getOrNull()
                    val diagFail = runCatching { store.readDiagnosticFailure(idx, date) }
                        .getOrNull()
                    if (summary != null) empty.put("durable_counters", summary)
                    else empty.put("durable_counters_state", when {
                        // the write of the diagnostic itself was refused -- NOT "nothing was
                        // attempted", which is what NOT_WRITTEN alone would say
                        diagFail != null -> "DIAGNOSTIC_WRITE_REFUSED"
                        store.sessionBytes(idx, date) > 0L -> "UNREADABLE"
                        else -> "NOT_WRITTEN"
                    })
                    if (diagFail != null) empty.put("diagnostic_write_failure", diagFail)
                    /*
                     * WHY, named from the reason recorded at the time of the refusal.
                     *
                     * An app-cap refusal, a low-device-space refusal and a file-system failure
                     * need three different responses, and the counter cannot tell them apart:
                     * `refused_cohort_persist` is the same integer in all three. Neither can the
                     * capacity reading above, taken whenever the export runs -- on 13 November
                     * 2026 the store was 735 B from its ceiling at the moment of the refusal, and
                     * a reading taken after a later ceiling change would have shown ample room.
                     */
                    empty.put("refusal", PremiumCaptureStore.classifyRefusal(
                        summary ?: diagFail?.let { f ->
                            JSONObject()
                                .put("last_refusal_store_status",
                                    f.opt("refusal_store_status") ?: JSONObject.NULL)
                                .put("last_refusal_status", f.opt("slot_status") ?: JSONObject.NULL)
                                .put("last_refusal_counter",
                                    f.opt("refusal_counter") ?: JSONObject.NULL)
                                .put("last_refusal_slot", f.opt("slot") ?: JSONObject.NULL)
                                .put("last_refusal_at_ms", f.opt("at_ms") ?: JSONObject.NULL)
                                .put("last_refusal_detail",
                                    f.opt("summary_write_detail") ?: JSONObject.NULL)
                        }))
                    section.getJSONObject("indices").put(idx, empty)
                    continue
                }
                /*
                 * A MONOTONIC GENERATION, not a size/count comparison.
                 *
                 * The old check compared session bytes and record counts before and after
                 * assembly, which cannot see an equal-size summary replacement or any content
                 * replacement that leaves both numbers unchanged — so `view_consistent=true` was
                 * a stronger claim than the evidence supported. The store now bumps a generation
                 * on EVERY mutation, so any interleaved write is visible whatever it did to the
                 * totals.
                 *
                 * Bounded retry, and no lock: the reader never holds the store's write lock
                 * across assembly, so a long export cannot block a capture write and therefore
                 * cannot reach the poll thread through it. If a consistent view cannot be
                 * obtained within the attempts allowed, the result SAYS so instead of being
                 * labelled consistent.
                 */
                var ex: JSONObject? = null
                var consistent = false
                var attempts = 0
                while (attempts < CONSISTENCY_ATTEMPTS) {
                    attempts++
                    val before = store.generation()
                    // EVEN means quiescent. A write in progress is odd, so a read that starts or
                    // ends inside one is refused outright: the previous version compared a
                    // post-write counter, which a writer descheduled between making its bytes
                    // visible and incrementing could leave unchanged across a read that had
                    // already observed the change.
                    if (before % 2L != 0L) continue
                    val built = session.export(idx, date, null, context)
                    val after = store.generation()
                    if (after == before && after % 2L == 0L) {
                        ex = built; consistent = true; break
                    }
                    ex = built
                }
                val out = ex ?: JSONObject().put("state", "PREMIUM_SECTION_ERROR")
                if (!consistent) torn = true
                out.put("view_consistent", consistent)
                    .put("view_consistency_basis",
                        "STORE_REVISION_EVEN_AND_UNCHANGED_ACROSS_READ_ODD_MEANS_WRITE_IN_PROGRESS")
                    .put("view_attempts", attempts)
                if (!consistent) out.put("view_state", "CONCURRENT_CAPTURE_WRITE_NOT_A_STABLE_VIEW")
                section.getJSONObject("indices").put(idx, out)
                any = true
            }
            section.put("state", if (any) "PRESENT" else "NO_PREMIUM_DATA")
                .put("has_data", any)
                // at the SECTION level too, so what the file is does not have to be inferred
                // from inside an index block
                .put("source_kind", context.sourceKind)
                .put("device_evidence", context.deviceEvidence)
                .put("capture_enabled", PremiumCaptureControl.isEnabled(filesDir))
                .put("concurrent_capture_write_detected", torn)
                .put("session_date_substitution", "NEVER_ONLY_THE_REQUESTED_DATE")
        } catch (t: Throwable) {
            section.put("state", "PREMIUM_SECTION_ERROR").put("has_data", false)
                .put("error", t.javaClass.simpleName)
        }
    }

    const val CONSISTENCY_ATTEMPTS = 3

    /**
     * Reduce the section in DECLARED stages, dropping whole fields, so nothing is truncated
     * mid-value and nothing is dropped silently.
     *
     * NO LONGER USED BY [mergeWithinCap], which now preserves the section whole or refuses. Kept
     * only as the mechanism a future, explicitly-typed summary-only output would build on; it is
     * not a path any current export takes, and nothing calls it to make an over-cap export look
     * like a normal one.
     */
    fun reduce(section: JSONObject, stage: Int): JSONObject {
        val reduced = JSONObject(section.toString())
        val idx = reduced.optJSONObject("indices") ?: return reduced
        val dropped = JSONArray()
        for (k in idx.keys().asSequence().toList()) {
            val one = idx.optJSONObject(k) ?: continue
            if (stage >= 1 && one.has("records")) { one.remove("records"); dropped.put("records") }
            if (stage >= 2 && one.has("slot_envelopes")) {
                one.remove("slot_envelopes"); dropped.put("slot_envelopes")
            }
            if (stage >= 3) {
                one.remove("cohort"); one.remove("conflicts"); one.remove("references")
                dropped.put("cohort_conflicts_references")
            }
        }
        return reduced.put("reduction_stage", stage)
            .put("reduced_fields", dropped)
            .put("reduction_note",
                "dropped whole fields to fit the combined export limit; the retained evidence is " +
                    "unchanged and nothing was truncated mid-value")
    }

    /**
     * Splice a new top-level `premium` key into the bundle WITHOUT reparsing or re-serializing it.
     * Prepending one key cannot alter any existing part, and avoids holding a second parsed copy
     * of a multi-megabyte bundle — this project has an OOM history on exactly that pattern.
     */
    fun splice(bundle: String, premium: JSONObject): String? {
        val t = bundle.trimStart()
        if (!t.startsWith("{")) return null
        val rest = t.substring(1).trimStart()
        val head = "{\"premium\":" + premium.toString()
        return if (rest.startsWith("}")) "$head}" else "$head,$rest"
    }

    data class Merged(val bytes: ByteArray, val stage: Int, val merged: Boolean,
                      val overCap: Boolean)

    /**
     * Merge and fit. Returns the final bytes to save, the declared reduction stage reached, and
     * whether it is still over the cap — in which case the caller must refuse rather than save.
     */
    /**
     * Splice within the already-authorised combined limit, **preserving the premium section whole
     * or refusing**.
     *
     * This used to reduce in stages — dropping records, then envelopes, then
     * cohort/references/conflicts — while leaving the verified/usable counts and the
     * `PRESENT`/`has_data` claims untouched. The reduction was declared inside the JSON, but the
     * bridge still reported ordinary success, so a caller could receive what looked like a full
     * export whose removed groups could no longer be verified offline while the document still
     * counted them as usable. Dropping evidence to fit and then describing the result as a normal
     * export is the kind of quiet downgrade this whole contract exists to prevent.
     *
     * So there is no reduction here. Either the complete section fits under the existing limit, or
     * `overCap` is true and the caller refuses. The limit is NOT raised and old parts are never
     * truncated. `stage` remains 0 and is kept only so the result shape is stable for callers.
     */
    fun mergeWithinCap(bundle: String, bundleBytes: ByteArray, section: JSONObject): Merged {
        val merged = splice(bundle, section)
        val bytes = merged?.toByteArray(Charsets.UTF_8) ?: bundleBytes
        return Merged(bytes, 0, merged != null, bytes.size > COMBINED_CAP_BYTES)
    }

    /** The premium-only payload, for a date the brain bundle has nothing for. */
    fun premiumOnlyBytes(date: String, section: JSONObject): ByteArray =
        JSONObject().put("premium", section).put("session_date", date)
            .put("parts_present", JSONArray().put("premium"))
            .put("note", "the brain bundle had nothing for this session; the premium evidence " +
                "below is for exactly the requested date and substitutes no other day")
            .toString().toByteArray(Charsets.UTF_8)
}
