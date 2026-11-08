package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * Connects the EXISTING acquisition to the capture library. One entry point, called from
 * `MarketWatchService.performPoll` with chain responses it has already fetched.
 *
 * ## What it must never do
 *
 * No additional quote, chain or expiry request. No broker retry. No timer, background wakeup or
 * scheduled work. No Supabase write and no upload queue. No trading-authority change: nothing here
 * is read by admission, ranking, sizing, exits, notifications or the Paper/Real lock, and capture
 * failure cannot fail or stall a poll — every path is wrapped and returns a status.
 *
 * ## Deliberate design choices, each with its reason
 *
 * **No queue.** Persistence runs synchronously on the caller's thread under a wall-clock budget.
 * A queue would need a bounded depth, an overflow policy and a frozen payload per entry; doing the
 * work inline means there is no pending work to bound, nothing to drop, and no chance of a worker
 * reading a `JSONObject` the live formatter has since mutated. The cost is poll-thread time, so
 * that time is measured and reported, and a budget stops capture rather than delaying a poll.
 *
 * **The chain is frozen anyway.** Each raw response is deep-copied through its own text before a
 * single field is read, so capture cannot observe a half-mutated object even inline, and the
 * snapshot it digests is exactly what it persisted.
 *
 * **Per-index isolation.** Each index gets its own acquisition clocks, its own cohort, its own
 * store paths and its own outcome. There is no cross-index fallback: a missing NF response never
 * borrows BNF evidence, and one index failing does not stop the other.
 *
 * **Real clocks only.** A non-positive timestamp is passed as `null`, never as `0` — the existing
 * `m0RecordCaptures` hook already uses `takeIf { it > 0 }` for the same reason. The combined
 * acquisition completion is never reused for both indices.
 */
object PremiumCaptureCoordinator {

    const val COORDINATOR_VERSION = "premium_capture_coordinator_v1_reuses_already_fetched_chain"

    /** Capture stops rather than delaying a poll. Measured, not assumed. */
    const val CAPTURE_BUDGET_MS_PER_POLL = 2_000L

    const val DISABLED = "DISABLED"
    const val CONTROL_UNREADABLE = "CONTROL_UNREADABLE_TREATED_AS_OFF"
    const val NO_SESSION = "NO_SESSION_DATE"
    const val BUDGET_EXCEEDED = "CAPTURE_BUDGET_EXCEEDED"
    const val RAN = "RAN"

    /**
     * One index's already-fetched acquisition.
     *
     * `rawChain == null` means that index's chain request failed. A failed chain is still recorded
     * once a cohort exists, so the gap is visible rather than absent.
     */
    data class ChainAcquisition(
        val index: String,
        val rawChain: JSONObject?,
        val requestStartedMs: Long?,
        val responseReceivedMs: Long?,
        val requestedUnderlyingKey: String?,
        val expiry: String?,
        /** IST HH:MM of THIS index's own response receipt. Never the combined completion. */
        val receiptHhmm: String?,
        val vendorSourceTs: String? = null,
    )

    /**
     * The lot claim for one pinned contract, taken FAITHFULLY from the application's own
     * `ContractLotTable` — by index, expiry, cycle and observation date, which is why it is not
     * flattened to one number per underlying.
     *
     * `allowOperationalCurrent` is left false and both `asOf` and `expiry` are supplied, so the
     * table's operational-constant path is structurally unreachable here: an operational constant
     * must never stand in as historical authority. When the table does not resolve, the claim is
     * absent and production lot authority stays UNRESOLVED — which does not prevent retaining the
     * raw premium/IV observation, only a later lot-based P&L or authority claim.
     */
    fun lotClaimFromContractAuthority(index: String, sessionDate: String,
                                      expiry: String?): JSONObject {
        return runCatching {
            val asOf = runCatching { java.time.LocalDate.parse(sessionDate) }.getOrNull()
            val exp = expiry?.let { e -> runCatching { java.time.LocalDate.parse(e) }.getOrNull() }
            val r = ContractLotTable.resolve(
                indexRaw = index,
                asOf = asOf,
                numberOfLots = 1.0,
                expiry = exp,
                expiryCycle = null,
                capturedContractLot = null,
                allowOperationalCurrent = false,
            )
            // carried verbatim, so a reader can audit which rule matched and when
            val authority = JSONObject()
                .put("authority", "CONTRACT_LOT_TABLE")
                .put("lot_table_version", r.lotTableVersion)
                .put("matched_rule_id", r.matchedRuleId ?: JSONObject.NULL)
                .put("lot_source", r.lotSource)
                .put("lot_as_of", r.lotAsOf ?: JSONObject.NULL)
                .put("index_key", r.indexKey)
                .put("index_known", r.indexKnown)
                .put("expiry", r.expiry ?: JSONObject.NULL)
                .put("expiry_cycle", r.expiryCycle ?: JSONObject.NULL)
                .put("contract_lot_size", r.contractLotSize ?: JSONObject.NULL)
                .put("rule_contract_lot", r.ruleContractLot ?: JSONObject.NULL)
                .put("resolved", r.resolved)
                .put("authoritative", r.authoritative)
                .put("lot_conflict", r.lotConflict)
                .put("unavailable_reason", r.unavailableReason ?: JSONObject.NULL)
                .put("operational_current_allowed", false)
                .put("flattened_to_one_lot_per_underlying", false)
            val usable = r.resolved && r.authoritative && !r.lotConflict &&
                r.contractLotSize != null
            JSONObject()
                .put("claimed_lot", if (usable) r.contractLotSize else JSONObject.NULL)
                .put("claimed_source", if (usable) "CONTRACT_LOT_TABLE" else JSONObject.NULL)
                // a reference that identifies the exact matched rule AND carries a real date
                .put("table_ref", if (usable)
                    "contract_lot_table/${r.lotTableVersion}/${r.matchedRuleId ?: "unmatched"}/" +
                        (r.lotAsOf ?: sessionDate)
                else JSONObject.NULL)
                .put("production_authority", authority)
        }.getOrElse {
            JSONObject()
                .put("claimed_lot", JSONObject.NULL)
                .put("claimed_source", JSONObject.NULL)
                .put("table_ref", JSONObject.NULL)
                .put("production_authority", JSONObject()
                    .put("authority", "CONTRACT_LOT_TABLE")
                    .put("resolved", false)
                    .put("authoritative", false)
                    .put("unavailable_reason", "resolver_threw:${it.javaClass.simpleName}"))
        }
    }

    /**
     * Capture one poll's already-fetched chains.
     *
     * Returns a status object; it never throws. When the control is OFF it returns immediately
     * **without touching the filesystem** — no directory, no evidence preparation, no work.
     */
    fun onChainsAcquired(filesDir: File, sessionDate: String?, declaredSchedulerSlot: String?,
                         acquisitions: List<ChainAcquisition>): JSONObject {
        val started = System.currentTimeMillis()
        val out = JSONObject()
            .put("coordinator_version", COORDINATOR_VERSION)
            .put("declared_scheduler_slot", declaredSchedulerSlot ?: JSONObject.NULL)
            .put("slot_authority", "DERIVED_FROM_EACH_INDEX_OWN_ACQUISITION_CLOCK")
            .put("session_date", sessionDate ?: JSONObject.NULL)
        return try {
            if (PremiumCaptureControl.controlUnreadable(filesDir))
                return out.put("status", CONTROL_UNREADABLE).put("indices", JSONArray())
            if (!PremiumCaptureControl.isEnabled(filesDir))
                return out.put("status", DISABLED).put("indices", JSONArray())
            if (sessionDate.isNullOrBlank() ||
                !PremiumObservationContract.isCalendarDate(sessionDate))
                return out.put("status", NO_SESSION).put("indices", JSONArray())

            val store = PremiumCaptureStore(PremiumCaptureControl.root(filesDir))
            val session = PremiumCaptureSession(store)
            val rows = JSONArray()
            var budgetHit = false
            // a fixed order, so persistence is serialized and reproducible
            for (acq in acquisitions.sortedBy { it.index }) {
                if (System.currentTimeMillis() - started > CAPTURE_BUDGET_MS_PER_POLL) {
                    budgetHit = true
                    rows.put(JSONObject().put("index", acq.index).put("status", BUDGET_EXCEEDED))
                    continue
                }
                val t0 = System.currentTimeMillis()
                val row = JSONObject().put("index", acq.index)
                try {
                    // FREEZE first: an immutable snapshot taken before any field is read, so the
                    // live formatter cannot mutate what capture digests and persists
                    val frozen = acq.rawChain?.let { JSONObject(it.toString()) }
                    val lot = lotClaimFromContractAuthority(acq.index, sessionDate, acq.expiry)
                    val r = session.observeSlot(
                        index = acq.index,
                        sessionDate = sessionDate,
                        rawChain = frozen,
                        // each index's OWN receipt clock decides its slot; no shared completion
                        acquisitionHhmm = acq.receiptHhmm,
                        schedulerSlot = null,
                        declaredSchedulerSlot = declaredSchedulerSlot,
                        requestedUnderlyingKey = acq.requestedUnderlyingKey,
                        // real clocks only: a non-positive stamp is unknown, never 0
                        requestStartedMs = acq.requestStartedMs?.takeIf { it > 0L },
                        responseReceivedMs = acq.responseReceivedMs?.takeIf { it > 0L },
                        vendorSourceTs = acq.vendorSourceTs,
                        claimedLot = lot.opt("claimed_lot")?.takeIf { it != JSONObject.NULL },
                        claimedLotSource = lot.optString("claimed_source").ifEmpty { null },
                        lotTableRef = lot.optString("table_ref").ifEmpty { null },
                        mapRef = null,
                        productionLotAuthority = lot.optJSONObject("production_authority"),
                    )
                    row.put("status", r.status)
                        .put("acquisition_id", r.acquisitionId ?: JSONObject.NULL)
                        .put("written", r.written).put("duplicates", r.duplicates)
                        .put("refused", r.refused).put("conflicts", r.conflicts)
                        .put("batch_complete", r.batchComplete)
                        .put("chain_present", frozen != null)
                        .put("detail", r.detail ?: JSONObject.NULL)
                } catch (t: Throwable) {
                    // capture failure is a status, never an exception reaching the poll
                    row.put("status", "CAPTURE_ERROR").put("error", t.javaClass.simpleName)
                }
                rows.put(row.put("elapsed_ms", System.currentTimeMillis() - t0))
            }
            // summary persistence can itself be refused; that is reported, not described as
            // durably recorded. A failed disk cannot record its own failure.
            val counterWrite = store.bumpCounters(
                acquisitions.firstOrNull()?.index ?: "NF", sessionDate,
                mapOf("coordinator_runs" to 1L))
            out.put("status", if (budgetHit) BUDGET_EXCEEDED else RAN)
                .put("indices", rows)
                .put("total_elapsed_ms", System.currentTimeMillis() - started)
                .put("budget_ms", CAPTURE_BUDGET_MS_PER_POLL)
                .put("queued_work", "NONE_SYNCHRONOUS_NO_PENDING_QUEUE")
                .put("summary_write", JSONObject()
                    .put("status", counterWrite.status)
                    .put("durably_recorded", counterWrite.persisted)
                    .put("detail", counterWrite.detail ?: JSONObject.NULL))
                .also { PremiumCaptureControl.recordRun(filesDir, it) }
        } catch (t: Throwable) {
            out.put("status", "CAPTURE_ERROR").put("error", t.javaClass.simpleName)
                .put("indices", JSONArray())
        }
    }
}
