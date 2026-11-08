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
        const val MAX_BYTES_PER_INDEX_SESSION = 1L * 1024 * 1024          // 1 MiB
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
        const val MAX_BYTES_GLOBAL = 4L * 1024 * 1024                     // 4 MiB
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

        private fun generationFor(root: File): java.util.concurrent.atomic.AtomicLong {
            val key = runCatching { root.canonicalPath }.getOrElse { root.absolutePath }
            return synchronized(GENERATIONS) {
                GENERATIONS.getOrPut(key) { java.util.concurrent.atomic.AtomicLong(0L) }
            }
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
    fun globalBytes() = bytesOf(root)

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
        // evidence must leave the control/status reserve intact, so the stop action and the
        // status that reports a full store remain writable at capacity
        val globalBudget = MAX_BYTES_GLOBAL - CONTROL_RESERVE_BYTES
        val global0 = globalBytes()
        if (global0 + finalBytes > globalBudget)
            return WriteResult(REFUSED_GLOBAL_BYTES, finalBytes,
                "$kind would take the store to ${global0 + finalBytes} B against " +
                    "$globalBudget B (cap $MAX_BYTES_GLOBAL B less $CONTROL_RESERVE_BYTES B " +
                    "control reserve); nothing is deleted to make room")
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
                return WriteResult(REPLACED, size)
            }
            if (!tmp.renameTo(dest)) {
                tmp.delete()
                return WriteResult(REFUSED_IO, size, "rename failed")
            }
            mutationPhaseHookForTest?.invoke("after_rename")
            WriteResult(WRITTEN, size)
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
