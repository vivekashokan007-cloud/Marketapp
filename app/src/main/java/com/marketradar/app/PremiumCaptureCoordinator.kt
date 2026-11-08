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
 * ## Persistence is OFF the poll thread, because the budget was not enough
 *
 * This used to persist synchronously under a 2000 ms wall-clock budget checked only *before*
 * starting each index. Everything after that check ran with no deadline at all — deep copy,
 * directory scans, acquiring the store's root lock, cohort and authority writes, six record
 * writes, counter writes — and the post-loop counter write ran regardless of budget exhaustion.
 * A slow or blocked filesystem could therefore delay a poll far beyond the budget.
 * `catch (Throwable)` does not help: it observes an eventual failure, it does not interrupt a
 * blocked call. The zero-budget test only ever proved that work can be skipped *before it starts*.
 *
 * So the budget is no longer the safety mechanism. Isolation is:
 *
 *  - **Prepare on the caller**, bounded and measured with a monotonic clock: read the control,
 *    freeze each response to immutable text, and size it. No store lock is taken and no evidence
 *    file is touched here, so neither a blocked write nor a held capture lock can reach the poll.
 *  - **Persist in [PremiumCaptureWorker]**, one job at a time with at most one waiting, bounded by
 *    occupancy and by bytes, refusing visibly when full.
 *
 * The returned status therefore describes **submission, not persistence**. The persisted outcome
 * is recorded by the worker into the durable counters and the status file. Reporting it as
 * persisted here would claim an outcome that has not happened yet.
 *
 * ## Other deliberate choices
 *
 * **The chain is frozen anyway.** Each raw response is serialized to text before a single field is
 * read, so capture cannot observe a half-mutated object and the snapshot it digests is exactly
 * what it persisted. Freezing to text is also what makes the handed-off payload immutable.
 *
 * **Per-index isolation.** Each index gets its own acquisition clocks, its own cohort, its own
 * store paths and its own outcome. There is no cross-index fallback: a missing NF response never
 * borrows BNF evidence, and one index failing does not stop the other.
 *
 * **Real clocks only.** A non-positive timestamp is passed as `null`, never as `0` — and the
 * capture-owned completion clock is set on success AND failure, so a failed chain still has an
 * honest slot and its per-contract missing outcomes can be written.
 */
object PremiumCaptureCoordinator {

    const val COORDINATOR_VERSION =
        "premium_capture_coordinator_v2_prepare_on_caller_persist_off_poll"

    /**
     * Preparation only — control read, freezing and sizing, no evidence IO. Exceeding it stops
     * further preparation. It is NOT relied on to bound persistence; that is the worker's job.
     */
    const val PREPARE_BUDGET_MS_PER_POLL = 250L

    const val DISABLED = "DISABLED"
    const val CONTROL_UNREADABLE = "CONTROL_UNREADABLE_TREATED_AS_OFF"
    const val NO_SESSION = "NO_SESSION_DATE"
    const val PREPARE_BUDGET_EXCEEDED = "PREPARE_BUDGET_EXCEEDED"
    const val SUBMITTED = "SUBMITTED"

    /**
     * One index's already-fetched acquisition.
     *
     * `rawChain == null` means that index's chain request failed. A failed chain is still recorded
     * once a cohort exists, so the gap is visible rather than absent — which requires
     * `receiptHhmm` to be present on the failure path too, from a capture-owned completion clock.
     */
    data class ChainAcquisition(
        val index: String,
        val rawChain: JSONObject?,
        val requestStartedMs: Long?,
        val responseReceivedMs: Long?,
        val requestedUnderlyingKey: String?,
        val expiry: String?,
        /** IST HH:MM of THIS index's own request completion. Never the combined completion. */
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
            // Carried verbatim, so a reader can audit which rule matched and when. The REQUESTED
            // context is carried too: this verdict is computed from the request's expiry before a
            // cohort is selected, so the export must be able to check it against the cohort it was
            // actually used with rather than assuming the two agree.
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
                .put("requested_index", index)
                .put("requested_session_date", sessionDate)
                .put("requested_expiry", expiry ?: JSONObject.NULL)
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
                    .put("requested_index", index)
                    .put("requested_session_date", sessionDate)
                    .put("requested_expiry", expiry ?: JSONObject.NULL)
                    .put("unavailable_reason", "resolver_threw:${it.javaClass.simpleName}"))
        }
    }

    /**
     * Everything that touches the store for one index. Runs on the WORKER thread, never on the
     * poll thread. Returns the row describing what actually happened.
     *
     * Exposed internally so a test can drive exactly what the worker runs.
     */
    /**
     * The immutable, metadata-only description a queued job carries.
     *
     * A waiting job used to capture the whole `ChainAcquisition`, whose `rawChain` still points at
     * the original mutable `JSONObject`. Persistence reads only the frozen text, so that was not a
     * mixed-response correctness bug — but it kept the entire original response graph reachable
     * from the queue, outside the prepared-payload bytes that are actually measured and bounded.
     * This value carries scalars only, so what a waiting job retains is the frozen text plus these
     * fields and nothing else.
     */
    data class PreparedMeta(
        val index: String,
        val requestStartedMs: Long?,
        val responseReceivedMs: Long?,
        val requestedUnderlyingKey: String?,
        val expiry: String?,
        val receiptHhmm: String?,
        val vendorSourceTs: String?,
    ) {
        companion object {
            fun from(a: ChainAcquisition) = PreparedMeta(
                a.index, a.requestStartedMs, a.responseReceivedMs, a.requestedUnderlyingKey,
                a.expiry, a.receiptHhmm, a.vendorSourceTs)
        }
    }

    internal fun persistOne(filesDir: File, index: String, sessionDate: String,
                            chainText: String?, acq: PreparedMeta,
                            declaredSchedulerSlot: String?): JSONObject {
        val row = JSONObject().put("index", index)
        val startedNs = System.nanoTime()
        try {
            val store = PremiumCaptureStore(PremiumCaptureControl.root(filesDir))
            val session = PremiumCaptureSession(store)
            // parsed here, on the worker, from the immutable text the caller prepared
            val chain = chainText?.let { JSONObject(it) }
            val lot = lotClaimFromContractAuthority(index, sessionDate, acq.expiry)
            // the authority is a per-index/session fact: recorded ONCE and referenced by content
            // from each slot, so the per-slot envelope stays inside the 2 KiB cap
            val authority = lot.getJSONObject("production_authority")
            val authorityWrite = store.putLotAuthority(index, sessionDate, authority)
            val authorityRef = if (authorityWrite.persisted)
                PremiumObservationContract.ref(
                    PremiumObservationContract.contentHash(authority)) else null
            row.put("lot_authority_write", authorityWrite.status)
            val r = session.observeSlot(
                index = index,
                sessionDate = sessionDate,
                rawChain = chain,
                // each index's OWN completion clock decides its slot; no shared completion
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
                productionLotAuthorityRef = authorityRef,
            )
            // This coordinator's OWN extra counter. It is named separately and must never be
            // reported as the capture summary's outcome: that one carries the batch, write and
            // refusal deltas, and a transient failure of it followed by a success here would
            // otherwise read as `durably_recorded: true` while the counters that matter were lost.
            val counterWrite = store.bumpCounters(index, sessionDate,
                mapOf("coordinator_runs" to 1L))
            row.put("status", r.status)
                .put("acquisition_id", r.acquisitionId ?: JSONObject.NULL)
                .put("written", r.written).put("duplicates", r.duplicates)
                .put("refused", r.refused).put("conflicts", r.conflicts)
                .put("batch_complete", r.batchComplete)
                .put("chain_present", chain != null)
                .put("detail", r.detail ?: JSONObject.NULL)
                // THE CAPTURE SUMMARY: this attempt's own batch/write/refusal counters
                .put("summary_write", JSONObject()
                    .put("kind", "CAPTURE_BATCH_WRITE_AND_REFUSAL_COUNTERS")
                    .put("status", r.summaryWrite?.status ?: "NOT_ATTEMPTED")
                    .put("durably_recorded", r.summaryWrite?.persisted ?: false)
                    .put("detail", r.summaryWrite?.detail ?: JSONObject.NULL))
                // the coordinator's separate run counter, reported under its own name
                .put("coordinator_counter_write", JSONObject()
                    .put("kind", "COORDINATOR_RUN_COUNT_ONLY_NOT_THE_CAPTURE_SUMMARY")
                    .put("status", counterWrite.status)
                    .put("durably_recorded", counterWrite.persisted)
                    .put("detail", counterWrite.detail ?: JSONObject.NULL))
        } catch (t: Throwable) {
            // capture failure is a status, never an exception reaching anything else
            row.put("status", "CAPTURE_ERROR").put("error", t.javaClass.simpleName)
        }
        return row.put("persist_elapsed_ms", (System.nanoTime() - startedNs) / 1_000_000L)
    }

    /**
     * Capture one poll's already-fetched chains. Returns a SUBMISSION status; it never throws and
     * never waits for IO.
     *
     * When the control is OFF it returns immediately without creating the capture directory or
     * preparing any evidence.
     */
    fun onChainsAcquired(filesDir: File, sessionDate: String?, declaredSchedulerSlot: String?,
                         acquisitions: List<ChainAcquisition>,
                         prepareBudgetMs: Long = PREPARE_BUDGET_MS_PER_POLL): JSONObject {
        val startedNs = System.nanoTime()
        fun elapsedMs() = (System.nanoTime() - startedNs) / 1_000_000L
        val out = JSONObject()
            .put("coordinator_version", COORDINATOR_VERSION)
            .put("declared_scheduler_slot", declaredSchedulerSlot ?: JSONObject.NULL)
            .put("slot_authority", "DERIVED_FROM_EACH_INDEX_OWN_ACQUISITION_CLOCK")
            .put("session_date", sessionDate ?: JSONObject.NULL)
            .put("persistence", "OFF_POLL_BOUNDED_WORKER")
            .put("status_describes", "SUBMISSION_NOT_PERSISTENCE")
        return try {
            if (PremiumCaptureControl.controlUnreadable(filesDir))
                return out.put("status", CONTROL_UNREADABLE).put("indices", JSONArray())
            if (!PremiumCaptureControl.isEnabled(filesDir))
                return out.put("status", DISABLED).put("indices", JSONArray())
            if (sessionDate.isNullOrBlank() ||
                !PremiumObservationContract.isCalendarDate(sessionDate))
                return out.put("status", NO_SESSION).put("indices", JSONArray())

            val rows = JSONArray()
            var budgetHit = false
            // a fixed order, so submission is deterministic
            for (acq in acquisitions.sortedBy { it.index }) {
                if (elapsedMs() >= prepareBudgetMs) {
                    budgetHit = true
                    rows.put(JSONObject().put("index", acq.index)
                        .put("status", PREPARE_BUDGET_EXCEEDED))
                    continue
                }
                val row = JSONObject().put("index", acq.index)
                try {
                    // FREEZE to immutable text before a single field is read, and SIZE it, so the
                    // handed-off payload cannot be mutated and its bytes are known in advance
                    val chainText = acq.rawChain?.toString()
                    val bytes = chainText?.toByteArray(Charsets.UTF_8)?.size ?: 0
                    row.put("prepared_bytes", bytes)
                    // scalars only: the closure must not keep the original mutable response graph
                    // reachable from the queue
                    val meta = PreparedMeta.from(acq)
                    val submission = PremiumCaptureWorker.submit(PremiumCaptureWorker.Job(
                        index = meta.index, sessionDate = sessionDate, preparedBytes = bytes,
                        persist = {
                            val r = persistOne(filesDir, meta.index, sessionDate, chainText, meta,
                                declaredSchedulerSlot)
                            // the status file is written HERE, on the worker, not on the poll
                            // thread, and through the accounted control path
                            PremiumCaptureControl.recordRun(filesDir, r)
                            r
                        }))
                    row.put("status", submission)
                } catch (t: Throwable) {
                    row.put("status", "PREPARE_ERROR").put("error", t.javaClass.simpleName)
                }
                rows.put(row)
            }
            out.put("status", if (budgetHit) PREPARE_BUDGET_EXCEEDED else SUBMITTED)
                .put("indices", rows)
                .put("prepare_elapsed_ms", elapsedMs())
                .put("prepare_budget_ms", prepareBudgetMs)
                .put("worker", PremiumCaptureWorker.stats())
                .put("queued_work", "BOUNDED_ONE_RUNNING_AT_MOST_ONE_WAITING")
        } catch (t: Throwable) {
            out.put("status", "CAPTURE_ERROR").put("error", t.javaClass.simpleName)
                .put("indices", JSONArray())
        }
    }
}
