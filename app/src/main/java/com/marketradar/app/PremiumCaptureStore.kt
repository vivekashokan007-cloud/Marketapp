package com.marketradar.app

import org.json.JSONObject
import java.io.File

/**
 * Bounded, atomic store for premium observation records.
 *
 * Why this is not `CaptureStorage`: that class documents its collision archives as
 * "reported separately from the active cap (they are NOT bounded by it)". The requirement here is
 * the opposite — collision archives must not escape the limits — so its behaviour cannot be reused
 * unmodified, and its trading-path behaviour is deliberately left untouched.
 *
 * Every cap below is a **proposed initial implementation cap authorised for this experiment**, not
 * a measured typical payload. Nothing is deleted to make room: when a cap is reached the new write
 * is refused with an explicit reason and the existing evidence stays.
 *
 * ## P1 corrections (Codex review R1) — the admission gate
 *
 * Four separate defects were found and all four were real:
 *
 *  1. `archiveConflict` checked the ORIGINAL payload size against the budgets, then built a larger
 *     JSON wrapper and wrote that instead, so the wrapper's extra bytes passed no check at all and
 *     the 2 KiB per-record bound was never applied to an archive.
 *  2. `saveCohort` lived in the session and bypassed this class entirely, so cohort metadata could
 *     be written with the global store already at its cap, and its temp file escaped the orphan
 *     enumerator, which only visited records/conflicts/envelopes.
 *  3. Cap checks and writes were not serialized, so two callers sharing a root could both pass.
 *  4. `atomicWrite` reported an existing destination as `DUPLICATE_NO_OP` without comparing its
 *     content, so a genuine conflict in that race path was recorded as a duplicate.
 *
 * Now **every** write — record, envelope, cohort, conflict archive, summary — goes through one
 * root-scoped `admit()` that measures the ACTUAL FINAL BYTES of what will be written, under a lock
 * held for the whole check-and-write. `sessionBytes + finalBytes` is checked rather than the
 * settled size, which bounds the PEAK: during a write the temp file and any existing destination
 * coexist, and a replacement's settled size is never larger than that peak.
 *
 * Pure: org.json + java.io. No Android API. Capture failure is always a returned status, never an
 * exception that could reach the poll path.
 */
class PremiumCaptureStore(val root: File) {

    companion object {
        const val STORE_VERSION = "premium_capture_store_v2_root_scoped_admission"

        /** 6 contracts x 78 slots: an upper RESOURCE bound, not an assertion of actual cadence. */
        const val MAX_OUTCOMES_PER_INDEX_SESSION = 468
        /**
         * One envelope per ACQUISITION, counted outside the contract-slot outcome cap. The nominal
         * window holds 69 slots, so this cap also absorbs a bounded number of same-slot retries
         * (P2 records a retry as a distinct acquisition rather than merging it); beyond that a
         * retry is refused rather than the cap being raised.
         */
        const val MAX_ENVELOPES_PER_INDEX_SESSION = 78
        const val MAX_CONFLICTS_PER_INDEX_SESSION = 32
        const val MAX_COHORTS_PER_INDEX_SESSION = 1
        /**
         * MEASURED. 12 November used 587,848 B (NF) and 597,363 B (BNF) over 53/54 captured
         * slots; scaled to the full 69-slot window that is about 764 KB, and the conflict
         * archive cap adds up to 64 KiB, leaving only ~20% head-room under the former 1 MiB
         * bound. Doubled so a long or retried day cannot hit the very failure mode this change
         * exists to remove. The per-session bound REMAINS: outcome, envelope and conflict
         * counts are unchanged, and they are what actually bounds a session.
         */
        const val MAX_BYTES_PER_INDEX_SESSION = 2L * 1024 * 1024          // 2 MiB
        /** Held back INSIDE the per-session cap so a final/refusal summary can always be written. */
        const val SUMMARY_RESERVE_BYTES = 16L * 1024                      // 16 KiB
        /**
         * Carved out INSIDE [MAX_BYTES_GLOBAL] for control/status files at the capture root,
         * which previously wrote around admission entirely. Evidence must leave it intact;
         * control files may use only it. This does not enlarge any evidence cap -- it reduces
         * the bytes evidence may claim, by the amount the status surface actually needs.
         */
        const val CONTROL_RESERVE_BYTES = 16L * 1024                     // 16 KiB
        const val MAX_BYTES_PER_CONTROL_FILE = 4L * 1024                 //  4 KiB
        /**
         * Carved out INSIDE [MAX_BYTES_GLOBAL], on top of [CONTROL_RESERVE_BYTES], so a session
         * summary stays writable when EVIDENCE is at the ceiling.
         *
         * Before this reserve the global check applied to every kind including [KIND_SUMMARY],
         * so a full store refused the very summary that would have recorded why it was full:
         * on 13 November 2026 both indices reported `written=0 summary_durable=false` and the
         * completed export carried no counter at all. A reserve only evidence is excluded from
         * is the smallest change that makes the first persistence failure durable.
         *
         * 128 KiB against a summary cap of [SUMMARY_RESERVE_BYTES] per index/session is room for
         * eight index/sessions at the maximum summary size, and summaries observed on 12
         * November are a few hundred bytes each.
         */
        const val DIAGNOSTIC_RESERVE_BYTES = 128L * 1024                 // 128 KiB
        /**
         * The store must never be the reason the DEVICE runs out of room. Evidence is refused
         * while fewer than this many bytes would remain on the filesystem, whatever the app
         * budget allows, and the refusal names the device rather than the cap. Paper and every
         * other surface are untouched by it: this gate only refuses a premium evidence write.
         */
        const val MIN_FREE_DEVICE_BYTES = 256L * 1024 * 1024              // 256 MiB
        /**
         * Within this distance of the evidence budget, admission stops trusting the incremental
         * byte total and re-measures the tree exactly. Far from the ceiling a cached total is
         * enough and a full walk is unaffordable; near it, correctness outranks the walk's cost.
         * Also bounds any drift the cache could accumulate from control-file writes, which do
         * not go through this class.
         */
        const val EXACT_RECHECK_MARGIN_BYTES = 4L * 1024 * 1024           // 4 MiB
        /**
         * MEASURED, not proposed. The 4 MiB first-delivery cap was explicitly
         * "PROPOSED_INITIAL_IMPLEMENTATION_CAPS_NOT_MEASURED_TYPICAL_PAYLOADS", and measurement
         * showed it holds only about three and a half two-index sessions. Nothing is ever
         * deleted, so on 13 November 2026 the store stood at 4,177,185 B against an evidence
         * budget of 4,177,920 B — 735 B left — and the first cohort write of the new session,
         * about 1.6 KB, was refused. No cohort means no envelope and no quote, so the session
         * recorded nothing.
         *
         * Sizing for three further complete sessions ABOVE the retained footprint:
         *
         *     retained on the device            4,177,185 B   (12 November export accounting)
         *     3 full two-index sessions         4,584,000 B   (1,185,211 B for 53/54 slots,
         *                                                      scaled to a 69-slot window)
         *     conflict archives, 32 x 2 KiB     393,216 B     (2 indices x 3 sessions)
         *     diagnostic reserve                131,072 B
         *     control reserve                    16,384 B
         *     ---------------------------------------------
         *     required                        9,301,857 B  ~= 8.87 MiB
         *
         * The pilot budget is 1 GiB: a ceiling that is PERMISSION TO GROW ON DEMAND, not an
         * allocation. Only actual writes consume it, nothing is pre-reserved, and the device's
         * own free space is a separate and stricter gate ([MIN_FREE_DEVICE_BYTES]), so a large
         * app budget can never be the reason the phone runs out of room.
         *
         * At this size the store can hold months of retained sessions, which is why admission
         * no longer walks the whole tree per write: see [treeBytes] and
         * [EXACT_RECHECK_MARGIN_BYTES]. Nothing is deleted or migrated for this pilot; every
         * retained file stays exactly where it is.
         */
        const val MAX_BYTES_GLOBAL = 1024L * 1024 * 1024                  // 1 GiB
        const val MAX_BYTES_PER_RECORD = 2L * 1024                        // 2 KiB

        // write outcomes
        const val WRITTEN = "WRITTEN"
        const val DUPLICATE_NO_OP = "DUPLICATE_NO_OP"
        const val REPLACED = "REPLACED"
        const val CONFLICT_ARCHIVED = "CONFLICT_ARCHIVED"
        const val REFUSED_RECORD_TOO_LARGE = "REFUSED_RECORD_TOO_LARGE"
        const val REFUSED_OUTCOME_CAP = "REFUSED_OUTCOME_CAP"
        const val REFUSED_ENVELOPE_CAP = "REFUSED_ENVELOPE_CAP"
        const val REFUSED_CONFLICT_CAP = "REFUSED_CONFLICT_CAP"
        const val REFUSED_COHORT_CAP = "REFUSED_COHORT_CAP"
        const val REFUSED_SESSION_BYTES = "REFUSED_SESSION_BYTE_CAP"
        const val REFUSED_GLOBAL_BYTES = "REFUSED_GLOBAL_BYTE_CAP"
        /** The DEVICE is out of room, which is not the same as the app budget being spent. */
        const val REFUSED_DEVICE_LOW_SPACE = "REFUSED_DEVICE_LOW_SPACE"
        const val REFUSED_IO = "REFUSED_IO_FAILURE"

        // kinds, for the one admission gate
        const val KIND_RECORD = "record"
        const val KIND_ENVELOPE = "envelope"
        const val KIND_CONFLICT = "conflict"
        const val KIND_COHORT = "cohort"
        const val KIND_SUMMARY = "summary"

        /**
         * One lock per store ROOT, so capacity checks and writes are serialized across every
         * caller sharing that root — including two `PremiumCaptureStore` instances constructed
         * separately over the same directory, which is exactly the case the old code let race.
         * Keyed by canonical path, so `./x` and an absolute path to the same directory share it.
         */
        private val LOCKS = HashMap<String, Any>()

        /**
         * A write REVISION per canonical root, shared by every store instance over that root
         * exactly as [LOCKS] is. **Even means quiescent; odd means a mutation is in progress.**
         *
         * Why a revision and not a size/count comparison: a reader comparing session bytes and
         * record counts before and after assembly cannot see an equal-size replacement, so it
         * could call a view consistent across a write that changed content without changing
         * either number.
         *
         * Why odd/even rather than a single post-write increment: incrementing only after a
         * successful `renameTo` is not a write-in-progress protocol. A writer descheduled between
         * making the new bytes visible and incrementing lets a reader sample the same value on
         * both sides of a read that already observed the change, and call a mixed view stable.
         * Temp creation, a short write and cleanup also change directory bytes and orphan-temp
         * accounting without ever reaching a completed rename. So the revision is marked odd
         * BEFORE the first observable filesystem mutation and published even in `finally`,
         * including on failure, and a reader accepts only equal EVEN revisions.
         */
        private val GENERATIONS = HashMap<String, java.util.concurrent.atomic.AtomicLong>()

        /** Re-entrancy depth per root, so a nested write does not publish an even revision early. */
        private val MUTATION_DEPTH = HashMap<String, Int>()

        /**
         * Test-only seam: invoked inside a mutation at named phases, so a test can pause a REAL
         * writer at a real interleaving instead of relying on timing. Phases:
         * `after_temp_write`, `after_rename`.
         */
        @Volatile
        internal var mutationPhaseHookForTest: ((String) -> Unit)? = null

        /**
         * Test-only seam: forces the next matching write to be REFUSED without touching the disk.
         *
         * Called with the write kind; returning a non-null string refuses the write with that
         * detail. It exists so a MIXED-OUTCOME sequence can be executed rather than described —
         * the capture batch summary failing while the coordinator's separate counter succeeds,
         * and the reverse — which is the case that proves one cannot mask the other. The previous
         * test only covered a healthy store and a wholly full one, neither of which forces that
         * interleaving.
         */
        @Volatile
        internal var writeRefusalInjectorForTest: ((String) -> String?)? = null

        private fun generationFor(root: File): java.util.concurrent.atomic.AtomicLong {
            val key = runCatching { root.canonicalPath }.getOrElse { root.absolutePath }
            return synchronized(GENERATIONS) {
                GENERATIONS.getOrPut(key) { java.util.concurrent.atomic.AtomicLong(0L) }
            }
        }

        /**
         * Incremental root byte total, per store root.
         *
         * WHY THIS EXISTS. Admission measured the root by WALKING EVERY RETAINED FILE, on every
         * write, and both status paths did the same on every refresh. Measured on this code:
         * 11.7 ms at 1,000 retained files, 227 ms at 60,000, 360 ms at 120,000 — and a capture
         * day is 828 writes, so a year of retention would have put minutes of tree-walking on
         * the poll thread per day and frozen the About screen for as long. At a 4 MiB ceiling
         * the tree never grew enough to matter; at 1 GiB it decides whether capture works at
         * all.
         *
         * The total is seeded once per root by a single exact walk and then adjusted by the
         * exact delta of each write, under the same lock that serialises the writes. It is a
         * CACHE, not the authority: [globalBytes] still measures, admission re-measures within
         * [EXACT_RECHECK_MARGIN_BYTES] of the ceiling, and control files — which this class
         * does not write — can only drift it by at most [CONTROL_RESERVE_BYTES], which is
         * already excluded from the evidence budget.
         */
        private val TREE_BYTES = HashMap<String, java.util.concurrent.atomic.AtomicLong>()

        private fun treeBytesFor(root: File, measure: () -> Long)
                : java.util.concurrent.atomic.AtomicLong {
            val key = runCatching { root.canonicalPath }.getOrElse { root.absolutePath }
            synchronized(TREE_BYTES) { TREE_BYTES[key] }?.let { return it }
            // measured OUTSIDE the map lock: the walk can be slow and must not block other roots
            val seeded = java.util.concurrent.atomic.AtomicLong(measure())
            return synchronized(TREE_BYTES) { TREE_BYTES.getOrPut(key) { seeded } }
        }

        /** Test and recovery hook: drop the cached totals so the next read re-measures. */
        fun forgetCachedBytesForTest() {
            synchronized(TREE_BYTES) { TREE_BYTES.clear() }
        }

        private fun rootKey(root: File) =
            runCatching { root.canonicalPath }.getOrElse { root.absolutePath }

        /** Mark the root as being mutated (odd revision) before any observable change. */
        private fun beginMutation(root: File) {
            val key = rootKey(root)
            synchronized(GENERATIONS) {
                val d = (MUTATION_DEPTH[key] ?: 0) + 1
                MUTATION_DEPTH[key] = d
                if (d == 1) generationFor(root).incrementAndGet()   // -> odd
            }
        }

        /** Publish a completed revision (even), whether the mutation succeeded or failed. */
        private fun endMutation(root: File) {
            val key = rootKey(root)
            synchronized(GENERATIONS) {
                val d = (MUTATION_DEPTH[key] ?: 1) - 1
                if (d <= 0) {
                    MUTATION_DEPTH.remove(key)
                    generationFor(root).incrementAndGet()            // -> even
                } else {
                    MUTATION_DEPTH[key] = d
                }
            }
        }

        private fun lockFor(root: File): Any {
            val key = runCatching { root.canonicalPath }.getOrElse { root.absolutePath }
            return synchronized(LOCKS) { LOCKS.getOrPut(key) { Any() } }
        }
    }

    private val lock = lockFor(root)

    data class WriteResult(val status: String, val bytes: Long = 0, val detail: String? = null) {
        val accepted: Boolean get() = status == WRITTEN || status == DUPLICATE_NO_OP ||
            status == REPLACED || status == CONFLICT_ARCHIVED
        /** The bytes are durably present under the primary key for THIS content. */
        val persisted: Boolean get() = status == WRITTEN || status == DUPLICATE_NO_OP ||
            status == REPLACED
    }

    /** A read that reports what it could NOT parse, instead of silently omitting it. */
    data class ReadResult(val rows: List<JSONObject>, val corrupt: List<String>)

    private fun sessionDir(index: String, session: String) =
        File(root, "$session/${index.uppercase()}")

    private fun recordsDir(index: String, session: String) = File(sessionDir(index, session), "records")
    private fun conflictsDir(index: String, session: String) = File(sessionDir(index, session), "conflicts")
    private fun envelopesDir(index: String, session: String) =
        File(sessionDir(index, session), "envelopes")
    private fun cohortFile(index: String, session: String) =
        File(sessionDir(index, session), "cohort.json")
    private fun summaryFile(index: String, session: String) =
        File(sessionDir(index, session), "summary.json")
    private fun lotAuthorityFile(index: String, session: String) =
        File(sessionDir(index, session), "lot_authority.json")

    private fun bytesOf(dir: File): Long {
        if (!dir.exists()) return 0
        var total = 0L
        dir.walkTopDown().forEach { if (it.isFile) total += it.length() }
        return total
    }

    private fun countJson(dir: File) =
        dir.listFiles()?.count { it.isFile && it.name.endsWith(".json") } ?: 0

    fun sessionBytes(index: String, session: String) = bytesOf(sessionDir(index, session))

    /** EXACT root total, by walking the tree. The authority, and the reconciliation source. */
    fun globalBytes() = bytesOf(root)

    /**
     * Bytes of the files sitting DIRECTLY in the root. This class never writes there —
     * [sessionDir] puts everything under `root/<index>/<session>/` — but
     * [PremiumCaptureControl] keeps its meta and last-run files there, so they are measured
     * fresh on every call rather than cached. It is a handful of small files, so the listing is
     * cheap and the total stays exact no matter who wrote them.
     */
    private fun rootImmediateBytes(): Long =
        root.listFiles()?.asSequence()?.filter { it.isFile }?.sumOf { it.length() } ?: 0L

    /** The store-owned subtree: everything under `root/<index>/...`, seeded by one walk. */
    private fun subtreeBytesSeed(): Long =
        root.listFiles()?.asSequence()?.filter { it.isDirectory }?.sumOf { bytesOf(it) } ?: 0L

    /**
     * The root total without walking the tree: the cached store-owned subtree plus a fresh
     * reading of the root's own files.
     *
     * Splitting it this way is what makes the cache SOUND rather than merely fast. Everything
     * the cache tracks is written by this class under its own lock, so the deltas are exact;
     * everything written by anyone else lands in the root's immediate files, which are measured
     * every time. A control write — or a test's filler — therefore cannot leave the total
     * stale, which an earlier whole-root cache did: the module's own full-store tests kept
     * admitting writes because the cache had been seeded before their filler appeared.
     */
    fun globalBytesFast(): Long =
        treeBytesFor(root) { subtreeBytesSeed() }.get() + rootImmediateBytes()

    /**
     * The same total, but ONLY if the subtree cache is already seeded; null otherwise.
     *
     * The seeding walk is one-off per process but is O(retained files) — measured on this code
     * at 12 ms for 1,000 files, 396 ms for 120,000 and 869 ms for 250,000. The capture path can
     * afford it on its background thread, and must, because admission needs a number. A status
     * refresh must not: the About screen would block for exactly that long on the first open
     * after launch. Status callers therefore use this and render "unknown" rather than waiting,
     * and the value appears as soon as the first capture write has seeded it.
     */
    fun globalBytesFastIfSeeded(): Long? {
        val key = runCatching { root.canonicalPath }.getOrElse { root.absolutePath }
        val cell = synchronized(TREE_BYTES) { TREE_BYTES[key] } ?: return null
        return cell.get() + rootImmediateBytes()
    }

    /** Apply the exact delta of a completed write to the cached subtree total. */
    private fun addTreeBytes(delta: Long) {
        if (delta != 0L) treeBytesFor(root) { subtreeBytesSeed() }.addAndGet(delta)
    }

    /**
     * Re-measure the tree and adopt the exact value, returning the drift that was corrected.
     * Called by admission whenever the cached total is within [EXACT_RECHECK_MARGIN_BYTES] of
     * the ceiling, so a drifted cache can never admit a write past the cap.
     */
    fun reconcileTreeBytes(): Long {
        val exact = subtreeBytesSeed()
        val cell = treeBytesFor(root) { exact }
        val drift = exact - cell.getAndSet(exact)
        if (drift != 0L) driftCorrectedBytes += kotlin.math.abs(drift)
        return drift
    }

    /** Bytes of drift corrected by reconciliation in this process, for the diagnostics. */
    @Volatile
    var driftCorrectedBytes: Long = 0L
        private set

    /**
     * Bounded capacity view, for the status surface and for an export of a session that wrote
     * nothing. Reads only directory sizes and constants, so it still answers when the store is
     * at its ceiling — which is exactly when it is needed.
     */
    fun capacitySnapshot(): JSONObject {
        val g = globalBytesFast()
        val evidenceBudget = MAX_BYTES_GLOBAL - CONTROL_RESERVE_BYTES - DIAGNOSTIC_RESERVE_BYTES
        val free = runCatching { root.usableSpace }.getOrDefault(-1L)
        return JSONObject()
            .put("global_bytes", g)
            .put("global_bytes_source", "INCREMENTAL_CACHE_RECONCILED_NEAR_THE_CEILING")
            .put("max_bytes_global", MAX_BYTES_GLOBAL)
            .put("control_reserve_bytes", CONTROL_RESERVE_BYTES)
            .put("diagnostic_reserve_bytes", DIAGNOSTIC_RESERVE_BYTES)
            .put("evidence_budget_bytes", evidenceBudget)
            .put("evidence_remaining_bytes", (evidenceBudget - g).coerceAtLeast(0L))
            .put("evidence_at_ceiling", g >= evidenceBudget)
            .put("budget_is", "PERMISSION_TO_GROW_ON_DEMAND_NOT_AN_ALLOCATION")
            // the device, reported beside the app budget so a refusal can name the right one
            .put("device_free_bytes", free)
            .put("min_free_device_bytes", MIN_FREE_DEVICE_BYTES)
            .put("device_low_space", free in 0 until MIN_FREE_DEVICE_BYTES)
            .put("drift_corrected_bytes", driftCorrectedBytes)
            .put("retention_deletion", "NONE_IN_THIS_DELIVERY")
    }

    /**
     * The durable session summary as written, or null when none exists or it is unreadable.
     *
     * DELIBERATELY LOCK-FREE, like [readCohortText]. The export calls this during assembly, and
     * the export is documented never to hold the store's write lock across assembly, so that a
     * long export cannot block a capture write. An earlier draft took `lock` here and the
     * module's own `aWriterPausedAfterRenameCannotProduceAStableView` hung on it: a writer
     * paused between rename and generation bump holds the lock, and the reader waited forever.
     * Summaries are written through [atomicWriteLocked], so a reader sees either the previous
     * file or the new one, never a torn one; an unreadable file reads as null, which the caller
     * reports rather than treating as absent.
     */
    fun readSummary(index: String, session: String): JSONObject? {
        val f = summaryFile(index, session)
        if (!f.isFile) return null
        return runCatching { JSONObject(f.readText()) }.getOrNull()
    }

    /** The current write generation for this root. Changes on every mutation, never decreases. */
    fun generation(): Long = generationFor(root).get()

    /**
     * Run `block` while holding this root's write lock.
     *
     * Exists so a test can hold the real lock from another thread and prove the poll-facing
     * submission does not wait on it. This is the same lock every write takes — not a stand-in
     * for the production lock, but the production lock itself.
     */
    internal fun <T> withRootLockForTest(block: () -> T): T = synchronized(lock) { block() }

    fun outcomeCount(index: String, session: String) = countJson(recordsDir(index, session))
    fun conflictCount(index: String, session: String) = countJson(conflictsDir(index, session))
    fun envelopeCount(index: String, session: String) = countJson(envelopesDir(index, session))

    /**
     * THE single admission gate. Called under `lock`, with the actual final bytes of the payload
     * that will be written — never with the size of some smaller intermediate.
     *
     * `replacingBytes` is the size of the destination being replaced, if any; it is already
     * included in `sessionBytes`, so checking `sessionBytes + finalBytes` bounds the peak (temp +
     * old destination) and therefore also the settled size.
     */
    private fun admit(kind: String, index: String, session: String, finalBytes: Long): WriteResult? {
        val perRecord = if (kind == KIND_SUMMARY) SUMMARY_RESERVE_BYTES else MAX_BYTES_PER_RECORD
        if (finalBytes > perRecord)
            return WriteResult(REFUSED_RECORD_TOO_LARGE, finalBytes,
                "$kind $finalBytes B exceeds $perRecord B; not truncated and not split")
        when (kind) {
            KIND_RECORD -> if (outcomeCount(index, session) >= MAX_OUTCOMES_PER_INDEX_SESSION)
                return WriteResult(REFUSED_OUTCOME_CAP, finalBytes,
                    "outcomes at cap $MAX_OUTCOMES_PER_INDEX_SESSION")
            KIND_ENVELOPE -> if (envelopeCount(index, session) >= MAX_ENVELOPES_PER_INDEX_SESSION)
                return WriteResult(REFUSED_ENVELOPE_CAP, finalBytes,
                    "envelopes at cap $MAX_ENVELOPES_PER_INDEX_SESSION")
            KIND_CONFLICT -> if (conflictCount(index, session) >= MAX_CONFLICTS_PER_INDEX_SESSION)
                return WriteResult(REFUSED_CONFLICT_CAP, finalBytes,
                    "conflicts at cap $MAX_CONFLICTS_PER_INDEX_SESSION")
            // KIND_COHORT: at most one per index/session, enforced by putCohort refusing a
            // different pin rather than by a count. KIND_SUMMARY: one replaceable file.
        }
        // the summary may use the reserve; everything else must leave it intact
        val budget = if (kind == KIND_SUMMARY) MAX_BYTES_PER_INDEX_SESSION
        else MAX_BYTES_PER_INDEX_SESSION - SUMMARY_RESERVE_BYTES
        val session0 = sessionBytes(index, session)
        if (session0 + finalBytes > budget)
            return WriteResult(REFUSED_SESSION_BYTES, finalBytes,
                "$kind would take the session to ${session0 + finalBytes} B against $budget B " +
                    "(cap $MAX_BYTES_PER_INDEX_SESSION B less $SUMMARY_RESERVE_BYTES B reserve)")
        // Evidence must leave the control/status reserve intact, so the stop action and the
        // status that reports a full store remain writable at capacity. It must also leave the
        // DIAGNOSTIC reserve intact, so the summary that records WHY the store is full can still
        // be written after evidence has been refused. A summary may use that second reserve;
        // nothing else may.
        val globalBudget = if (kind == KIND_SUMMARY) MAX_BYTES_GLOBAL - CONTROL_RESERVE_BYTES
        else MAX_BYTES_GLOBAL - CONTROL_RESERVE_BYTES - DIAGNOSTIC_RESERVE_BYTES
        // The DEVICE is the stricter gate, and it is checked first: a 1 GiB app budget is
        // permission to grow, never a claim on space the phone does not have. `usableSpace` is
        // a single stat call, so this costs nothing per write.
        // `File.getUsableSpace()` returns 0 for a path that does not exist, which is UNKNOWN and
        // not "full": the module's own aCaptureFailureCannotPropagateOutOfTheStore uses a root
        // under /proc, and an early draft of this gate turned its genuine IO failure into a
        // low-space refusal. A non-positive reading therefore leaves this gate out of the way
        // and lets the real failure surface; the app budget below still applies.
        val free = runCatching { root.usableSpace }.getOrDefault(0L)
        if (free > 0L && free - finalBytes < MIN_FREE_DEVICE_BYTES)
            return WriteResult(REFUSED_DEVICE_LOW_SPACE, finalBytes,
                "$kind needs $finalBytes B but the device has $free B free and " +
                    "$MIN_FREE_DEVICE_BYTES B must remain; the app budget is not the limit here")
        // Cached total far from the ceiling, exact re-measure near it, so a drifted cache can
        // never admit past the cap. See TREE_BYTES for why the walk cannot run on every write.
        var global0 = globalBytesFast()
        if (global0 + finalBytes > globalBudget - EXACT_RECHECK_MARGIN_BYTES) {
            reconcileTreeBytes()
            global0 = globalBytesFast()
        }
        if (global0 + finalBytes > globalBudget)
            return WriteResult(REFUSED_GLOBAL_BYTES, finalBytes,
                "$kind would take the store to ${global0 + finalBytes} B against " +
                    "$globalBudget B (cap $MAX_BYTES_GLOBAL B less $CONTROL_RESERVE_BYTES B " +
                    "control reserve" +
                    (if (kind == KIND_SUMMARY) "" else
                        " less $DIAGNOSTIC_RESERVE_BYTES B diagnostic reserve") +
                    "); nothing is deleted to make room")
        return null
    }

    /** Deterministic, collision-resistant file name from the record's own key. */
    private fun fileFor(dir: File, key: String) =
        File(dir, PremiumObservationContract.sha256Hex(key).take(40) + ".json")

    private fun bytesOfText(s: String) = s.toByteArray(Charsets.UTF_8).size.toLong()

    /**
     * Write one observation record.
     *
     * Identical key + identical bytes is a no-op. Conflicting bytes under the same key are NEVER
     * overwritten and never silently dropped: the incoming payload is archived under a digest of
     * its own bytes, inside the same caps — **first write wins for the primary record, the
     * conflicting payload is retained as an archive.**
     */
    fun put(index: String, session: String, key: String, record: JSONObject): WriteResult {
        // a BLOCK body, not an expression body: Kotlin forbids a non-local `return` from inside
        // an inline lambda when the enclosing function has an expression body, and every one of
        // these admission paths returns early.
        return synchronized(lock) {
            val payload = record.toString()
            val size = bytesOfText(payload)
            val dest = fileFor(recordsDir(index, session), key)
            if (dest.isFile) {
                val existing = runCatching { dest.readText() }.getOrNull()
                if (existing == payload) return WriteResult(DUPLICATE_NO_OP, size)
                return archiveConflictLocked(index, session, key, payload)
            }
            admit(KIND_RECORD, index, session, size)?.let { return it }
            atomicWriteLocked(recordsDir(index, session), dest, payload, size, replace = false)
        }
    }

    /** Write one acquisition envelope. Counted against its own bound, inside the byte caps. */
    fun putEnvelope(index: String, session: String, key: String,
                    envelope: JSONObject): WriteResult {
        return synchronized(lock) {
            val payload = envelope.toString()
            val size = bytesOfText(payload)
            val dest = fileFor(envelopesDir(index, session), key)
            if (dest.isFile) {
                val existing = runCatching { dest.readText() }.getOrNull()
                if (existing == payload) return WriteResult(DUPLICATE_NO_OP, size)
                return archiveConflictLocked(index, session, key, payload)
            }
            admit(KIND_ENVELOPE, index, session, size)?.let { return it }
            atomicWriteLocked(envelopesDir(index, session), dest, payload, size, replace = false)
        }
    }

    /**
     * Write the pinned cohort THROUGH THE STORE, under the same budget as everything else.
     *
     * P1(2): this used to live in the session and write `cohort.json` directly, so it could be
     * written with the store already at its global cap, and its temp file was invisible to the
     * orphan enumerator. A cohort is pinned once per index/session: an identical re-save is a
     * no-op, and DIFFERENT content is refused rather than replacing the pin a record may already
     * resolve through.
     */
    fun putCohort(index: String, session: String, cohort: JSONObject): WriteResult {
        return synchronized(lock) {
            val payload = cohort.toString()
            val size = bytesOfText(payload)
            val dest = cohortFile(index, session)
            if (dest.isFile) {
                val existing = runCatching { dest.readText() }.getOrNull()
                if (existing == payload) return WriteResult(DUPLICATE_NO_OP, size)
                return WriteResult(REFUSED_COHORT_CAP, size,
                    "a different cohort is already pinned for $index/$session; not replaced")
            }
            admit(KIND_COHORT, index, session, size)?.let { return it }
            atomicWriteLocked(sessionDir(index, session), dest, payload, size, replace = false)
        }
    }

    /**
     * The production lot authority for one index/session, written ONCE.
     *
     * It lives here rather than inside each slot envelope because it is a per-index/session fact —
     * the cohort pins the expiry, so the ContractLotTable verdict is constant across that
     * session's slots — and because repeating it per slot pushed the envelope past the 2 KiB
     * per-record cap. CI caught exactly that: every observation was refused with
     * ENVELOPE_NOT_PERSISTED. The cap is not enlarged; the redundancy is removed.
     */
    fun putLotAuthority(index: String, session: String, authority: JSONObject): WriteResult {
        return synchronized(lock) {
            val payload = authority.toString()
            val size = bytesOfText(payload)
            val dest = lotAuthorityFile(index, session)
            if (dest.isFile) {
                val existing = runCatching { dest.readText() }.getOrNull()
                if (existing == payload) return WriteResult(DUPLICATE_NO_OP, size)
                return WriteResult(REFUSED_COHORT_CAP, size,
                    "a different lot authority is already recorded for $index/$session")
            }
            admit(KIND_COHORT, index, session, size)?.let { return it }
            atomicWriteLocked(sessionDir(index, session), dest, payload, size, replace = false)
        }
    }

    /**
     * Write one bounded control/status file at the capture ROOT, inside the declared storage
     * policy instead of around it.
     *
     * Previously `PremiumCaptureControl` wrote `control.json`, `last_run.json` and their temps
     * straight to this root — outside [admit] and outside this lock — so those bytes could take
     * the store past the global limit that every evidence write respects, and the status file was
     * written even after a full root had refused the records it described. The caps are NOT
     * enlarged to fix that: [CONTROL_RESERVE_BYTES] is carved out *inside* the existing global
     * bound, evidence must leave it intact, and control files may use only it.
     *
     * `allowAtCapacity` exists for exactly one case: turning capture **OFF** must stay possible
     * when the store is full, because refusing the stop action at capacity would be the worst
     * possible failure mode. It replaces one small existing file and never grows the store beyond
     * the reserve.
     */
    fun putControlFile(name: String, obj: JSONObject, allowAtCapacity: Boolean = false):
        WriteResult = synchronized(lock) {
        val payload = obj.toString()
        val size = bytesOfText(payload)
        if (size > MAX_BYTES_PER_CONTROL_FILE)
            return WriteResult(REFUSED_RECORD_TOO_LARGE, size,
                "control $name $size B exceeds $MAX_BYTES_PER_CONTROL_FILE B; " +
                    "status must be a bounded structured object, never truncated text")
        val dest = File(root, name)
        val existing = if (dest.isFile) runCatching { dest.readText() }.getOrNull() else null
        if (existing == payload) return WriteResult(DUPLICATE_NO_OP, size)
        val replacing = if (dest.isFile) bytesOfText(existing ?: "") else 0L
        if (!allowAtCapacity) {
            val control0 = controlBytes()
            if (control0 - replacing + size > CONTROL_RESERVE_BYTES)
                return WriteResult(REFUSED_GLOBAL_BYTES, size,
                    "control files would take ${control0 - replacing + size} B against the " +
                        "$CONTROL_RESERVE_BYTES B reserve; nothing is deleted to make room")
            val global0 = globalBytes()
            if (global0 - replacing + size > MAX_BYTES_GLOBAL)
                return WriteResult(REFUSED_GLOBAL_BYTES, size,
                    "control $name would take the store to ${global0 - replacing + size} B " +
                        "against $MAX_BYTES_GLOBAL B")
        }
        atomicWriteLocked(root, dest, payload, size, replace = true)
    }

    /** Bytes currently held by control/status files at the root (not inside any session). */
    fun controlBytes(): Long =
        (root.listFiles()?.filter { it.isFile }?.sumOf { it.length() } ?: 0L)

    fun readLotAuthority(index: String, session: String): JSONObject? {
        val f = lotAuthorityFile(index, session)
        if (!f.isFile) return null
        return runCatching { JSONObject(f.readText()) }.getOrNull()
    }

    /** The pinned cohort as stored bytes, or null. Parse failure is the caller's to classify. */
    fun readCohortText(index: String, session: String): String? {
        val f = cohortFile(index, session)
        if (!f.isFile) return null
        return runCatching { f.readText() }.getOrNull()
    }

    private fun archiveConflictLocked(index: String, session: String, key: String,
                                      payload: String): WriteResult {
        // BUILD THE WRAPPER FIRST, then admit on ITS bytes. The old code admitted on `payload`
        // and wrote this larger object, so the difference passed no check (P1.1).
        val wrapper = JSONObject()
            .put("conflict_of_key_sha256", PremiumObservationContract.sha256Hex(key))
            .put("retained_evidence_policy", "FIRST_WRITE_WINS_CONFLICT_ARCHIVED")
            .put("payload_sha256", PremiumObservationContract.sha256Hex(payload))
            .put("payload", payload)
            .toString()
        val size = bytesOfText(wrapper)
        val dir = conflictsDir(index, session)
        val dest = File(dir, PremiumObservationContract.sha256Hex(wrapper).take(40) + ".json")
        if (dest.isFile) {
            val existing = runCatching { dest.readText() }.getOrNull()
            if (existing == wrapper) return WriteResult(DUPLICATE_NO_OP, size)
        }
        admit(KIND_CONFLICT, index, session, size)?.let { return it }
        val r = atomicWriteLocked(dir, dest, wrapper, size, replace = false)
        return if (r.status == WRITTEN) WriteResult(CONFLICT_ARCHIVED, r.bytes) else r
    }

    /**
     * Atomic bounded write. The temp file is written in the destination directory and moved into
     * place, so a crash leaves either the old file or the new one, and at most one temp per key.
     *
     * P1(4): when the destination appears after the temp is written, the CONTENT IS COMPARED.
     * Identical content is a duplicate; different content is reported as such and never called a
     * duplicate, so the caller can archive it.
     */
    private fun atomicWriteLocked(dir: File, dest: File, payload: String, size: Long,
                                  replace: Boolean): WriteResult {
        // odd BEFORE the first observable change, even in `finally` even on failure
        beginMutation(root)
        // measured before the write so a replacement's delta is exact
        val before = if (dest.isFile) dest.length() else 0L
        var settled: WriteResult? = null
        return try {
            if (!dir.exists() && !dir.mkdirs() && !dir.exists())
                return WriteResult(REFUSED_IO, size, "cannot create ${dir.name}")
            val tmp = File(dir, dest.name + ".tmp")
            tmp.writeText(payload)
            mutationPhaseHookForTest?.invoke("after_temp_write")
            if (tmp.length() != size) {
                tmp.delete()
                return WriteResult(REFUSED_IO, size, "short write (disk full?)")
            }
            if (dest.exists()) {
                val existing = runCatching { dest.readText() }.getOrNull()
                if (existing == payload) { tmp.delete(); return WriteResult(DUPLICATE_NO_OP, size) }
                if (!replace) {
                    tmp.delete()
                    // NOT a duplicate: the stored bytes differ from what was offered
                    return WriteResult(REFUSED_IO, size,
                        "destination holds different content and replace is not permitted")
                }
                if (!tmp.renameTo(dest)) { tmp.delete(); return WriteResult(REFUSED_IO, size, "replace failed") }
                mutationPhaseHookForTest?.invoke("after_rename")
                return WriteResult(REPLACED, size).also { settled = it }
            }
            if (!tmp.renameTo(dest)) {
                tmp.delete()
                return WriteResult(REFUSED_IO, size, "rename failed")
            }
            mutationPhaseHookForTest?.invoke("after_rename")
            WriteResult(WRITTEN, size).also { settled = it }
        } catch (e: SecurityException) {
            WriteResult(REFUSED_IO, size, "permission: ${e.javaClass.simpleName}")
        } catch (e: Exception) {
            WriteResult(REFUSED_IO, size, "${e.javaClass.simpleName}")
        } finally {
            // Publishes the completed (even) revision on EVERY exit — the early returns above, a
            // short write, a refused replace, and a thrown failure alike. A failed write still
            // created and removed a temp file and so still changed directory and orphan-temp
            // accounting; it must register as a completed mutation rather than leave the root
            // permanently marked in progress.
            // The cached root total follows the bytes that actually settled: a new file adds
            // its size, a replacement adds only the difference, and a duplicate, a refusal or a
            // thrown failure leaves it alone because the temp file was removed.
            settled?.let { r ->
                if (r.status == WRITTEN) addTreeBytes(size)
                else if (r.status == REPLACED) addTreeBytes(size - before)
            }
            endMutation(root)
        }
    }

    /** True when no mutation is in progress for this root: odd revisions are in-progress. */
    fun quiescent(): Boolean = generation() % 2L == 0L

    /**
     * Orphaned temp files a crash can leave behind — now including the SESSION directory itself,
     * where the cohort and summary temps live. The old enumerator visited only
     * records/conflicts/envelopes, so a half-written cohort was invisible (P1.2).
     */
    fun orphanTempFiles(index: String, session: String): List<File> =
        listOf(sessionDir(index, session), recordsDir(index, session),
            conflictsDir(index, session), envelopesDir(index, session))
            .flatMap { d -> d.listFiles()?.filter { it.isFile && it.name.endsWith(".tmp") }
                ?: emptyList() }

    private fun readDir(dir: File): ReadResult {
        val rows = ArrayList<JSONObject>()
        val corrupt = ArrayList<String>()
        (dir.listFiles() ?: emptyArray())
            .filter { it.isFile && it.name.endsWith(".json") }
            .sortedBy { it.name }
            .forEach { f ->
                val o = runCatching { JSONObject(f.readText()) }.getOrNull()
                if (o == null) corrupt.add(f.name) else rows.add(o)
            }
        return ReadResult(rows, corrupt)
    }

    /** Records, WITH the names of any that could not be parsed — not silently omitted (P3). */
    fun readRecords(index: String, session: String) = readDir(recordsDir(index, session))
    fun readEnvelopeRecords(index: String, session: String) = readDir(envelopesDir(index, session))
    fun readConflicts(index: String, session: String) = readDir(conflictsDir(index, session))

    fun readAll(index: String, session: String): List<JSONObject> =
        readRecords(index, session).rows
    fun readEnvelopes(index: String, session: String): List<JSONObject> =
        readEnvelopeRecords(index, session).rows

    /**
     * Durable counters, merged and REPLACED atomically inside the reserve.
     *
     * P3: `writeSummary` had no caller and went through a first-write-only path, so a later update
     * was reported as a duplicate and the counters could never advance. Refusal, overflow,
     * conflict and corruption evidence therefore survived only in a returned `SlotResult` — that
     * is, in memory, until the process ended.
     */
    fun bumpCounters(index: String, session: String, deltas: Map<String, Long>): WriteResult {
        return synchronized(lock) {
            if (deltas.isEmpty()) return WriteResult(DUPLICATE_NO_OP, 0)
            val existing = runCatching {
                summaryFile(index, session).takeIf { it.isFile }?.readText()?.let { JSONObject(it) }
            }.getOrNull()
            val counters = existing?.optJSONObject("counters") ?: JSONObject()
            // a corrupt or unreadable summary is recorded, never silently reset to zero
            val lostPrior = summaryFile(index, session).isFile && existing == null
            for ((k, v) in deltas) counters.put(k, counters.optLong(k, 0L) + v)
            if (lostPrior) counters.put("summary_unreadable_resets",
                counters.optLong("summary_unreadable_resets", 0L) + 1L)
            val summary = JSONObject()
                .put("store_version", STORE_VERSION)
                .put("index", index.uppercase())
                .put("session_date", session)
                .put("counters", counters)
                .put("counters_are", "DURABLE_ADMISSION_AND_INTEGRITY_ACCOUNTING")
                .put("updates", (existing?.optLong("updates", 0L) ?: 0L) + 1L)
            val payload = summary.toString()
            val size = bytesOfText(payload)
            writeRefusalInjectorForTest?.invoke(KIND_SUMMARY)?.let {
                return WriteResult(REFUSED_IO, size, "injected: $it")
            }
            admit(KIND_SUMMARY, index, session, size)?.let { return it }
            atomicWriteLocked(sessionDir(index, session), summaryFile(index, session),
                payload, size, replace = true)
        }
    }

    fun readCounters(index: String, session: String): JSONObject =
        runCatching {
            summaryFile(index, session).takeIf { it.isFile }?.readText()
                ?.let { JSONObject(it).optJSONObject("counters") }
        }.getOrNull() ?: JSONObject()

    fun summaryIsReadable(index: String, session: String): Boolean {
        val f = summaryFile(index, session)
        if (!f.isFile) return true     // absent is not corrupt
        return runCatching { JSONObject(f.readText()) }.isSuccess
    }

    fun capsJson(): JSONObject = JSONObject()
        .put("store_version", STORE_VERSION)
        .put("caps_are", "PROPOSED_INITIAL_IMPLEMENTATION_CAPS_NOT_MEASURED_TYPICAL_PAYLOADS")
        .put("max_outcomes_per_index_session", MAX_OUTCOMES_PER_INDEX_SESSION)
        .put("outcome_cap_counts", "PRIMARY_CONTRACT_SLOT_OUTCOMES_ONLY")
        .put("max_envelopes_per_index_session", MAX_ENVELOPES_PER_INDEX_SESSION)
        .put("max_conflicts_per_index_session", MAX_CONFLICTS_PER_INDEX_SESSION)
        .put("max_cohorts_per_index_session", MAX_COHORTS_PER_INDEX_SESSION)
        .put("max_bytes_per_index_session", MAX_BYTES_PER_INDEX_SESSION)
        .put("summary_reserve_bytes", SUMMARY_RESERVE_BYTES)
        .put("max_bytes_global", MAX_BYTES_GLOBAL)
        .put("max_bytes_per_record", MAX_BYTES_PER_RECORD)
        .put("conflict_archives_inside_caps", true)
        .put("admission_measures", "ACTUAL_FINAL_BYTES_INCLUDING_CONFLICT_WRAPPERS")
        .put("admission_serialized_per_root", true)
        .put("writes_covered_by_admission",
            "RECORDS_ENVELOPES_COHORT_CONFLICT_ARCHIVES_AND_SUMMARY")
        .put("retention_deletion", "NONE_IN_THIS_DELIVERY")
        .put("retained_evidence_policy", "FIRST_WRITE_WINS_CONFLICT_ARCHIVED")
}
