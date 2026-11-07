package com.marketradar.app

import org.json.JSONObject
import java.io.File

/**
 * Bounded, atomic, append-only store for premium observation records.
 *
 * Why this is not `CaptureStorage`: that class documents its collision archives as
 * "reported separately from the active cap (they are NOT bounded by it)". Codex's requirement is
 * the opposite — collision archives must not escape the limits — so its behaviour cannot be reused
 * unmodified here, and its trading-path behaviour is deliberately left untouched.
 *
 * Every cap below is a **proposed initial implementation cap authorised for this experiment**, not
 * a measured typical payload. Nothing is deleted to make room: when a cap is reached the new write
 * is refused with an explicit reason and the existing evidence stays.
 *
 * Pure: org.json + java.io. No Android API. Capture failure is always a returned status, never an
 * exception that could reach the poll path.
 */
class PremiumCaptureStore(val root: File) {

    companion object {
        const val STORE_VERSION = "premium_capture_store_v1_bounded_no_eviction"

        /** 6 contracts x 78 slots: an upper RESOURCE bound, not an assertion of actual cadence. */
        const val MAX_OUTCOMES_PER_INDEX_SESSION = 468
        /** One slot envelope per slot, counted outside the contract-slot outcome cap above. */
        const val MAX_ENVELOPES_PER_INDEX_SESSION = 78
        const val MAX_CONFLICTS_PER_INDEX_SESSION = 32
        const val MAX_BYTES_PER_INDEX_SESSION = 1L * 1024 * 1024          // 1 MiB
        /** Held back INSIDE the per-session cap so a final/refusal summary can always be written. */
        const val SUMMARY_RESERVE_BYTES = 16L * 1024                      // 16 KiB
        const val MAX_BYTES_GLOBAL = 4L * 1024 * 1024                     // 4 MiB
        const val MAX_BYTES_PER_RECORD = 2L * 1024                        // 2 KiB

        // write outcomes
        const val WRITTEN = "WRITTEN"
        const val DUPLICATE_NO_OP = "DUPLICATE_NO_OP"
        const val CONFLICT_ARCHIVED = "CONFLICT_ARCHIVED"
        const val REFUSED_RECORD_TOO_LARGE = "REFUSED_RECORD_TOO_LARGE"
        const val REFUSED_OUTCOME_CAP = "REFUSED_OUTCOME_CAP"
        const val REFUSED_CONFLICT_CAP = "REFUSED_CONFLICT_CAP"
        const val REFUSED_SESSION_BYTES = "REFUSED_SESSION_BYTE_CAP"
        const val REFUSED_GLOBAL_BYTES = "REFUSED_GLOBAL_BYTE_CAP"
        const val REFUSED_IO = "REFUSED_IO_FAILURE"
    }

    data class WriteResult(val status: String, val bytes: Long = 0, val detail: String? = null) {
        val accepted: Boolean get() = status == WRITTEN || status == DUPLICATE_NO_OP ||
            status == CONFLICT_ARCHIVED
    }

    private fun sessionDir(index: String, session: String) =
        File(root, "$session/${index.uppercase()}")

    private fun recordsDir(index: String, session: String) = File(sessionDir(index, session), "records")
    private fun conflictsDir(index: String, session: String) = File(sessionDir(index, session), "conflicts")
    /**
     * Slot envelopes are stored separately and counted separately, because Codex's cap is 468
     * **primary contract-slot outcomes** (six contracts x 78 slots). Folding one envelope per slot
     * into that number would quietly consume the authorised outcome budget; they are still fully
     * inside the byte caps, and bounded in their own right at one per slot.
     */
    private fun envelopesDir(index: String, session: String) =
        File(sessionDir(index, session), "envelopes")

    private fun bytesOf(dir: File): Long {
        if (!dir.exists()) return 0
        var total = 0L
        dir.walkTopDown().forEach { if (it.isFile) total += it.length() }
        return total
    }

    fun sessionBytes(index: String, session: String) = bytesOf(sessionDir(index, session))
    fun globalBytes() = bytesOf(root)
    fun outcomeCount(index: String, session: String) =
        recordsDir(index, session).listFiles()?.count { it.isFile && it.name.endsWith(".json") } ?: 0
    fun conflictCount(index: String, session: String) =
        conflictsDir(index, session).listFiles()?.count { it.isFile && it.name.endsWith(".json") } ?: 0
    fun envelopeCount(index: String, session: String) =
        envelopesDir(index, session).listFiles()?.count { it.isFile && it.name.endsWith(".json") } ?: 0

    /**
     * Write one slot envelope. Same duplicate/conflict/byte discipline as an observation, but
     * counted against its own bound so the authorised 468 contract-slot outcomes stay intact.
     */
    fun putEnvelope(index: String, session: String, slot: String, envelope: JSONObject): WriteResult {
        val payload = envelope.toString()
        val size = payload.toByteArray(Charsets.UTF_8).size.toLong()
        if (size > MAX_BYTES_PER_RECORD)
            return WriteResult(REFUSED_RECORD_TOO_LARGE, size,
                "envelope $size B exceeds $MAX_BYTES_PER_RECORD B; not truncated")
        val dir = envelopesDir(index, session)
        val key = "$session|${index.uppercase()}|$slot|ENVELOPE"
        val dest = fileFor(dir, key)
        if (dest.isFile) {
            val existing = runCatching { dest.readText() }.getOrNull()
            if (existing == payload) return WriteResult(DUPLICATE_NO_OP, size)
            return archiveConflict(index, session, key, payload, size)
        }
        if (envelopeCount(index, session) >= MAX_ENVELOPES_PER_INDEX_SESSION)
            return WriteResult(REFUSED_OUTCOME_CAP, size,
                "envelopes at cap $MAX_ENVELOPES_PER_INDEX_SESSION")
        val budget = MAX_BYTES_PER_INDEX_SESSION - SUMMARY_RESERVE_BYTES
        if (sessionBytes(index, session) + size > budget)
            return WriteResult(REFUSED_SESSION_BYTES, size, "envelope would exceed $budget B")
        if (globalBytes() + size > MAX_BYTES_GLOBAL)
            return WriteResult(REFUSED_GLOBAL_BYTES, size, "envelope would exceed the global cap")
        return atomicWrite(dir, dest, payload, size)
    }

    fun readEnvelopes(index: String, session: String): List<JSONObject> =
        (envelopesDir(index, session).listFiles() ?: emptyArray())
            .filter { it.isFile && it.name.endsWith(".json") }
            .sortedBy { it.name }
            .mapNotNull { f -> runCatching { JSONObject(f.readText()) }.getOrNull() }

    /** Deterministic, collision-resistant file name from the record's own key. */
    private fun fileFor(dir: File, key: String) =
        File(dir, PremiumObservationContract.sha256Hex(key).take(40) + ".json")

    /**
     * Atomic bounded write.
     *
     * Identical key + identical bytes is a no-op. Conflicting bytes under the same key are NEVER
     * overwritten and never silently dropped: the incoming payload is archived under a digest of
     * its own bytes, inside the same caps, and the retained-evidence policy is documented —
     * **first write wins for the primary record, the conflicting payload is retained as an
     * archive**, so the originally observed evidence is never replaced by a later one.
     *
     * The temp file is written in the destination directory and moved into place, so a crash leaves
     * either the old file or the new one, and at most one temp file per key.
     */
    fun put(index: String, session: String, key: String, record: JSONObject): WriteResult {
        val payload = record.toString()
        val size = payload.toByteArray(Charsets.UTF_8).size.toLong()
        if (size > MAX_BYTES_PER_RECORD)
            return WriteResult(REFUSED_RECORD_TOO_LARGE, size,
                "record $size B exceeds $MAX_BYTES_PER_RECORD B; not truncated")

        val dir = recordsDir(index, session)
        val dest = fileFor(dir, key)
        // identical bytes under the same key: no-op before any cap is consumed
        if (dest.isFile) {
            val existing = runCatching { dest.readText() }.getOrNull()
            if (existing == payload) return WriteResult(DUPLICATE_NO_OP, size)
            return archiveConflict(index, session, key, payload, size)
        }
        if (outcomeCount(index, session) >= MAX_OUTCOMES_PER_INDEX_SESSION)
            return WriteResult(REFUSED_OUTCOME_CAP, size,
                "outcomes at cap $MAX_OUTCOMES_PER_INDEX_SESSION")
        val budget = MAX_BYTES_PER_INDEX_SESSION - SUMMARY_RESERVE_BYTES
        if (sessionBytes(index, session) + size > budget)
            return WriteResult(REFUSED_SESSION_BYTES, size,
                "session would exceed $budget B (cap $MAX_BYTES_PER_INDEX_SESSION B less " +
                    "$SUMMARY_RESERVE_BYTES B summary reserve)")
        if (globalBytes() + size > MAX_BYTES_GLOBAL)
            return WriteResult(REFUSED_GLOBAL_BYTES, size,
                "global would exceed $MAX_BYTES_GLOBAL B; nothing is deleted to make room")
        return atomicWrite(dir, dest, payload, size)
    }

    private fun archiveConflict(index: String, session: String, key: String,
                                payload: String, size: Long): WriteResult {
        if (conflictCount(index, session) >= MAX_CONFLICTS_PER_INDEX_SESSION)
            return WriteResult(REFUSED_CONFLICT_CAP, size,
                "conflicts at cap $MAX_CONFLICTS_PER_INDEX_SESSION; counted in the bounded summary")
        val dir = conflictsDir(index, session)
        val digest = PremiumObservationContract.sha256Hex(payload).take(40)
        val dest = File(dir, digest + ".json")
        if (dest.isFile) return WriteResult(DUPLICATE_NO_OP, size)
        // conflicts are inside the SAME caps: they cannot escape them
        val budget = MAX_BYTES_PER_INDEX_SESSION - SUMMARY_RESERVE_BYTES
        if (sessionBytes(index, session) + size > budget)
            return WriteResult(REFUSED_SESSION_BYTES, size, "conflict archive would exceed $budget B")
        if (globalBytes() + size > MAX_BYTES_GLOBAL)
            return WriteResult(REFUSED_GLOBAL_BYTES, size, "conflict archive would exceed the global cap")
        val env = JSONObject()
            .put("conflict_of_key_sha256", PremiumObservationContract.sha256Hex(key))
            .put("retained_evidence_policy", "FIRST_WRITE_WINS_CONFLICT_ARCHIVED")
            .put("payload_sha256", PremiumObservationContract.sha256Hex(payload))
            .put("payload", payload).toString()
        val r = atomicWrite(dir, dest, env, env.toByteArray(Charsets.UTF_8).size.toLong())
        return if (r.status == WRITTEN) WriteResult(CONFLICT_ARCHIVED, r.bytes) else r
    }

    private fun atomicWrite(dir: File, dest: File, payload: String, size: Long): WriteResult {
        return try {
            if (!dir.exists() && !dir.mkdirs() && !dir.exists())
                return WriteResult(REFUSED_IO, size, "cannot create ${dir.name}")
            // one temp per destination, in the same directory, so a crash cannot accumulate them
            val tmp = File(dir, dest.name + ".tmp")
            tmp.writeText(payload)
            if (tmp.length() != payload.toByteArray(Charsets.UTF_8).size.toLong()) {
                tmp.delete()
                return WriteResult(REFUSED_IO, size, "short write (disk full?)")
            }
            if (dest.exists()) {            // lost a race; the first write stands
                tmp.delete()
                return WriteResult(DUPLICATE_NO_OP, size)
            }
            if (!tmp.renameTo(dest)) {
                tmp.delete()
                return WriteResult(REFUSED_IO, size, "rename failed")
            }
            WriteResult(WRITTEN, size)
        } catch (e: SecurityException) {
            WriteResult(REFUSED_IO, size, "permission: ${e.javaClass.simpleName}")
        } catch (e: Exception) {
            WriteResult(REFUSED_IO, size, "${e.javaClass.simpleName}")
        }
    }

    /** Orphaned temp files, which a crash can leave behind. Reported, and bounded by the caps. */
    fun orphanTempFiles(index: String, session: String): List<File> =
        listOf(recordsDir(index, session), conflictsDir(index, session),
            envelopesDir(index, session))
            .flatMap { d -> d.listFiles()?.filter { it.isFile && it.name.endsWith(".tmp") }
                ?: emptyList() }

    fun readAll(index: String, session: String): List<JSONObject> =
        (recordsDir(index, session).listFiles() ?: emptyArray())
            .filter { it.isFile && it.name.endsWith(".json") }
            .sortedBy { it.name }
            .mapNotNull { f -> runCatching { JSONObject(f.readText()) }.getOrNull() }

    /**
     * Bounded summary, written from the reserve held back inside the per-session cap so it can
     * always be produced — including when data writes were refused.
     */
    fun writeSummary(index: String, session: String, summary: JSONObject): WriteResult {
        val payload = summary.toString()
        val size = payload.toByteArray(Charsets.UTF_8).size.toLong()
        if (size > SUMMARY_RESERVE_BYTES)
            return WriteResult(REFUSED_RECORD_TOO_LARGE, size,
                "summary $size B exceeds the $SUMMARY_RESERVE_BYTES B reserve")
        if (sessionBytes(index, session) + size > MAX_BYTES_PER_INDEX_SESSION)
            return WriteResult(REFUSED_SESSION_BYTES, size, "summary would exceed the session cap")
        if (globalBytes() + size > MAX_BYTES_GLOBAL)
            return WriteResult(REFUSED_GLOBAL_BYTES, size, "summary would exceed the global cap")
        val dir = sessionDir(index, session)
        return atomicWrite(dir, File(dir, "summary.json"), payload, size)
    }

    fun capsJson(): JSONObject = JSONObject()
        .put("store_version", STORE_VERSION)
        .put("caps_are", "PROPOSED_INITIAL_IMPLEMENTATION_CAPS_NOT_MEASURED_TYPICAL_PAYLOADS")
        .put("max_outcomes_per_index_session", MAX_OUTCOMES_PER_INDEX_SESSION)
        .put("outcome_cap_counts", "PRIMARY_CONTRACT_SLOT_OUTCOMES_ONLY")
        .put("max_envelopes_per_index_session", MAX_ENVELOPES_PER_INDEX_SESSION)
        .put("max_conflicts_per_index_session", MAX_CONFLICTS_PER_INDEX_SESSION)
        .put("max_bytes_per_index_session", MAX_BYTES_PER_INDEX_SESSION)
        .put("summary_reserve_bytes", SUMMARY_RESERVE_BYTES)
        .put("max_bytes_global", MAX_BYTES_GLOBAL)
        .put("max_bytes_per_record", MAX_BYTES_PER_RECORD)
        .put("conflict_archives_inside_caps", true)
        .put("retention_deletion", "NONE_IN_THIS_DELIVERY")
        .put("retained_evidence_policy", "FIRST_WRITE_WINS_CONFLICT_ARCHIVED")
}
