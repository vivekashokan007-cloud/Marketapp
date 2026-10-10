package com.marketradar.app

import org.json.JSONObject
import java.io.File

/**
 * R2: durable per-menu outbox for Paper decision-menu evidence (CAPTURED menus and MISSED_SESSION manifests).
 *
 * Layout (one directory, e.g. filesDir/paper_decision_menus):
 *   <menu_id>.json           frozen CAPTURED payload bytes (written once, never rewritten)
 *   <menu_id>.missed.json    frozen MISSED_SESSION manifest bytes (written once, never rewritten)
 *   <menu_id>.outbox.json    per-menu delivery state (the only mutable file)
 *
 * Rules
 *  - Bytes are frozen at enqueue; payload_sha256 is the SHA-256 of exactly those bytes and every retry sends them.
 *  - Each menu has its own attempts/state/ack; nothing is keyed to "today" or "previous session", so any number of
 *    pending sessions survive rollover and process restarts (state lives only in files).
 *  - Only an explicit server acknowledgement moves an entry to ACKED. Only ACKED entries older than the retention
 *    window are pruned. EXHAUSTED / CONFLICT / MISMATCH / REJECTED / LOCAL_CORRUPT entries are retained and
 *    reported by [summary]; EXHAUSTED entries get one new recovery round per new session date.
 *  - R2-review Blocker B: [reconcile] runs before rollover/MISSED generation (PaperMenuRecovery.preparePoll) and at
 *    the start of every [drain]. A validated frozen payload whose state file is missing (interrupted enqueue) gets its
 *    state rebuilt from the ORIGINAL bytes; a corrupt state file is moved aside (kept) and rebuilt from the payload
 *    or turned into a visible LOCAL_CORRUPT_RETAINED entry; nothing is silently skipped or deleted. A valid frozen
 *    CAPTURED payload is never replaced by, or queued as, a MISSED manifest for the same menu id.
 *  - Every state write is checked. A delivery whose state could not be saved is reported as a storage error and
 *    is not counted as acknowledged (see docs/contracts/paper_menu_recovery_contract.md).
 *  - Evidence only. Nothing here is read by ranking, gates, sizing, exits or primary selection.
 */
object PaperMenuOutbox {
    const val SCHEMA = "paper_menu_outbox_v1"
    const val MAX_ATTEMPTS_PER_ROUND = 12
    const val KIND_CAPTURED = "CAPTURED"
    const val KIND_MISSED = "MISSED"

    const val NOT_STARTED = "NOT_STARTED"
    const val RETRY_PENDING = "RETRY_PENDING"
    const val ACKED = "ACKED"
    const val EXHAUSTED = "EXHAUSTED"
    const val CONFLICT_RETAINED = "CONFLICT_RETAINED"
    const val MISMATCH_RETAINED = "MISMATCH_RETAINED"
    const val REJECTED_RETAINED = "REJECTED_RETAINED"
    const val LOCAL_CORRUPT_RETAINED = "LOCAL_CORRUPT_RETAINED"

    private val PENDING = setOf(NOT_STARTED, RETRY_PENDING)

    /** Server disposition from the uploader (SupabaseClient.PaperMenuUploadResult.disposition). */
    data class Delivery(val disposition: String, val outcome: String, val detail: String? = null)

    data class Entry(val menuId: String, val kind: String, val payloadSha256: String, val payloadFile: File, val state: JSONObject)

    data class DrainSummary(val attempted: Int, val acknowledged: Int, val retryPending: Int, val exhausted: Int,
                            val retained: Int, val requeued: Int, val storageErrors: List<String> = emptyList(),
                            val reconcile: ReconcileReport = ReconcileReport())

    const val AMBIGUOUS_RETAINED = "AMBIGUOUS_RETAINED"
    private val KNOWN_STATES = setOf(NOT_STARTED, RETRY_PENDING, ACKED, EXHAUSTED, CONFLICT_RETAINED, MISMATCH_RETAINED,
        REJECTED_RETAINED, LOCAL_CORRUPT_RETAINED, AMBIGUOUS_RETAINED)
    private val MENU_ID = Regex("pdm_[0-9a-f]{24}")
    private val SHA = Regex("[0-9a-f]{64}")

    /** File-system seam. Tests replace it to inject a crash or a failed write at an exact boundary. */
    interface FileOps {
        /** Writes [text] to a temp file, syncs it, then renames it over [target]. Returns false on failure. */
        fun atomicWrite(target: File, text: String): Boolean
        fun rename(from: File, to: File): Boolean
    }

    object RealFileOps : FileOps {
        override fun atomicWrite(target: File, text: String): Boolean = runCatching {
            target.parentFile?.mkdirs()
            val tmp = File(target.parentFile, target.name + ".tmp")
            java.io.FileOutputStream(tmp).use { out -> out.write(text.toByteArray(Charsets.UTF_8)); out.fd.sync() }
            if (!tmp.renameTo(target)) { tmp.delete(); false } else true
        }.getOrDefault(false)
        override fun rename(from: File, to: File): Boolean = runCatching { from.renameTo(to) }.getOrDefault(false)
    }

    @Volatile var fileOps: FileOps = RealFileOps

    private fun stateFile(dir: File, menuId: String) = File(dir, "$menuId.outbox.json")
    private fun payloadFile(dir: File, menuId: String, kind: String) =
        File(dir, if (kind == KIND_MISSED) "$menuId.missed.json" else "$menuId.json")

    private fun atomicWrite(target: File, text: String): Boolean = fileOps.atomicWrite(target, text)

    fun readState(dir: File, menuId: String): JSONObject? =
        stateFile(dir, menuId).takeIf { it.isFile }?.let { f -> parseState(f, menuId) }

    /** A state file is usable only when it parses and names this menu, a known kind/state and a sha of the right form. */
    private fun parseState(f: File, menuId: String): JSONObject? {
        val st = runCatching { JSONObject(f.readText(Charsets.UTF_8)) }.getOrNull() ?: return null
        if (st.optString("menu_id") != menuId) return null
        if (st.optString("kind") !in setOf(KIND_CAPTURED, KIND_MISSED)) return null
        if (st.optString("state") !in KNOWN_STATES) return null
        val sha = st.optString("payload_sha256")
        if (!SHA.matches(sha) && st.optString("state") != LOCAL_CORRUPT_RETAINED) return null
        if (st.optString("payload_file").isEmpty()) return null
        return st
    }

    private fun enqueue(dir: File, menuId: String, kind: String, bytes: String, nowMs: Long, sessionDate: String): JSONObject? = synchronized(this) {
        // Freeze once: an existing entry (either kind) is never replaced. A corrupt or missing state file is first
        // reconciled from the frozen payload on disk, so an interrupted earlier enqueue is found here too.
        reconcileOne(dir, menuId, nowMs, ReconcileReport())
        readState(dir, menuId)?.let { return it }
        if (kind == KIND_MISSED && payloadFile(dir, menuId, KIND_CAPTURED).exists()) {
            // A CAPTURED payload exists for this menu id but could not be validated (reconcileOne left no usable
            // state). Never substitute a MISSED manifest for it; the fault stays visible via [scanFaults].
            return null
        }
        val pf = payloadFile(dir, menuId, kind)
        if (pf.isFile) return null   // unreadable/invalid frozen payload of this kind: retained, reported, not overwritten
        if (!atomicWrite(pf, bytes)) return null
        return writeNewState(dir, menuId, kind, pf, PaperMenuCapture.sha256Hex(bytes), nowMs, sessionDate, null)
    }

    private fun writeNewState(dir: File, menuId: String, kind: String, pf: File, sha: String, nowMs: Long, sessionDate: String,
                              recovered: JSONObject?): JSONObject? {
        val st = JSONObject()
            .put("schema", SCHEMA).put("menu_id", menuId).put("kind", kind).put("session_date", sessionDate)
            .put("payload_file", pf.name).put("payload_sha256", sha)
            .put("state", NOT_STARTED).put("attempts", 0).put("total_attempts", 0).put("recovery_rounds", 0)
            .put("enqueued_at_ms", nowMs)
        if (recovered != null) st.put("recovered", recovered)
        return if (atomicWrite(stateFile(dir, menuId), st.toString())) st else null
    }

    // ---- R2-review Blocker B: directory reconciliation ----

    data class ReconcileReport(
        val recovered: MutableList<String> = mutableListOf(),          // "<menu>:<kind>:<reason>"
        val faults: MutableList<String> = mutableListOf(),             // "<menu or file>:<fault>"
        val storageErrors: MutableList<String> = mutableListOf()
    ) {
        fun toJson(): JSONObject = JSONObject().put("recovered", org.json.JSONArray(recovered))
            .put("faults", org.json.JSONArray(faults)).put("storage_errors", org.json.JSONArray(storageErrors))
    }

    private fun menuIdOf(name: String): String? = when {
        name.endsWith(".outbox.json") -> name.removeSuffix(".outbox.json")
        name.endsWith(".missed.json") -> name.removeSuffix(".missed.json")
        name.endsWith(".json") -> name.removeSuffix(".json")
        else -> null
    }?.takeIf { MENU_ID.matches(it) }

    /**
     * Validates a frozen payload against its file name. Returns (session_date, null) when valid, or (null, reason).
     * CAPTURED: manifest.menu_id == file id == menuIdFor(manifest.session_date, policy). MISSED: menu_id == file id,
     * capture_status MISSED_SESSION, menu_id == menuIdFor(session_date, capture_policy_version).
     */
    fun validatePayload(menuId: String, kind: String, text: String?): Pair<String?, String?> {
        if (text == null) return null to "payload_unreadable"
        val j = runCatching { JSONObject(text) }.getOrNull() ?: return null to "payload_not_json"
        return if (kind == KIND_CAPTURED) {
            val m = j.optJSONObject("manifest") ?: return null to "manifest_missing"
            val date = m.optString("session_date")
            val policy = m.optString("capture_policy_version").ifBlank { PaperMenuCapture.POLICY_VERSION }
            when {
                m.optString("menu_id") != menuId -> null to "manifest_menu_id_differs_from_file"
                date.isEmpty() -> null to "session_date_missing"
                PaperMenuCapture.menuIdFor(date, policy) != menuId -> null to "menu_id_not_bound_to_session"
                else -> date to null
            }
        } else {
            val date = j.optString("session_date")
            when {
                j.optString("menu_id") != menuId -> null to "missed_menu_id_differs_from_file"
                j.optString("capture_status") != "MISSED_SESSION" -> null to "missed_status_invalid"
                date.isEmpty() -> null to "session_date_missing"
                PaperMenuCapture.menuIdFor(date, j.optString("capture_policy_version").ifBlank { PaperMenuCapture.POLICY_VERSION }) != menuId ->
                    null to "menu_id_not_bound_to_session"
                else -> date to null
            }
        }
    }

    private fun readText(f: File): String? = if (f.isFile) runCatching { f.readText(Charsets.UTF_8) }.getOrNull() else null

    /**
     * Reconciles one menu id. Precedence: an existing usable state is kept as is; otherwise a valid CAPTURED payload
     * wins over a MISSED one; an invalid payload becomes a visible LOCAL_CORRUPT_RETAINED entry (bytes untouched).
     */
    private fun reconcileOne(dir: File, menuId: String, nowMs: Long, report: ReconcileReport) {
        val sf = stateFile(dir, menuId)
        val capF = payloadFile(dir, menuId, KIND_CAPTURED)
        val misF = payloadFile(dir, menuId, KIND_MISSED)
        val existing = if (sf.isFile) parseState(sf, menuId) else null
        if (existing != null) {
            // A MISSED entry that was never acknowledged while a valid CAPTURED payload exists for the same menu id
            // (possible only on directories written by the R1 code) is ambiguous: retained and visible, not sent.
            if (existing.optString("kind") == KIND_MISSED && existing.optString("state") != ACKED &&
                existing.optString("state") != AMBIGUOUS_RETAINED && capF.isFile &&
                validatePayload(menuId, KIND_CAPTURED, readText(capF)).second == null) {
                existing.put("state", AMBIGUOUS_RETAINED).put("last_outcome", "VALID_CAPTURED_PAYLOAD_ALSO_PRESENT").put("last_at_ms", nowMs)
                if (!atomicWrite(sf, existing.toString())) report.storageErrors.add("$menuId:state_write_failed")
                report.faults.add("$menuId:ambiguous_missed_and_captured")
            }
            return
        }
        var corruptNote: String? = null
        if (sf.exists()) {
            // Corrupt / unusable state file: keep it for diagnosis under a name entries() does not read.
            val aside = File(dir, "$menuId.outbox.corrupt.$nowMs")
            if (!fileOps.rename(sf, aside)) {
                report.faults.add("$menuId:corrupt_state_file_could_not_be_moved_aside")
                report.storageErrors.add("$menuId:corrupt_state_rename_failed")
                return
            }
            corruptNote = aside.name
            report.faults.add("$menuId:corrupt_state_file_retained_as:${aside.name}")
        }
        val capText = readText(capF)
        val (capDate, capFault) = if (capF.exists()) validatePayload(menuId, KIND_CAPTURED, capText) else (null to null)
        val misText = readText(misF)
        val (misDate, misFault) = if (misF.exists()) validatePayload(menuId, KIND_MISSED, misText) else (null to null)
        val why = if (corruptNote != null) "corrupt_state" else "missing_state"
        fun recoveredNote(kind: String, f: File) = JSONObject().put("reason", why).put("at_ms", nowMs)
            .put("payload_mtime_ms", f.lastModified()).put("corrupt_state_file", corruptNote ?: JSONObject.NULL).put("kind", kind)
        when {
            capDate != null -> {
                if (misF.exists()) report.faults.add("$menuId:missed_payload_superseded_by_valid_capture_retained")
                val ok = writeNewState(dir, menuId, KIND_CAPTURED, capF, PaperMenuCapture.sha256Hex(capText!!), capF.lastModified(),
                    capDate, recoveredNote(KIND_CAPTURED, capF))
                if (ok != null) report.recovered.add("$menuId:$KIND_CAPTURED:$why") else report.storageErrors.add("$menuId:state_write_failed")
            }
            capF.exists() -> {
                // Invalid CAPTURED bytes: never replaced by a MISSED manifest. Retained and made visible.
                writeFault(dir, menuId, KIND_CAPTURED, capF, capText, "CAPTURED_$capFault", nowMs, corruptNote, report)
            }
            misDate != null -> {
                val ok = writeNewState(dir, menuId, KIND_MISSED, misF, PaperMenuCapture.sha256Hex(misText!!), misF.lastModified(),
                    misDate, recoveredNote(KIND_MISSED, misF))
                if (ok != null) report.recovered.add("$menuId:$KIND_MISSED:$why") else report.storageErrors.add("$menuId:state_write_failed")
            }
            misF.exists() -> writeFault(dir, menuId, KIND_MISSED, misF, misText, "MISSED_$misFault", nowMs, corruptNote, report)
            corruptNote != null -> report.faults.add("$menuId:corrupt_state_without_any_payload")
        }
    }

    private fun writeFault(dir: File, menuId: String, kind: String, pf: File, text: String?, fault: String, nowMs: Long,
                           corruptNote: String?, report: ReconcileReport) {
        val st = JSONObject().put("schema", SCHEMA).put("menu_id", menuId).put("kind", kind).put("session_date", "")
            .put("payload_file", pf.name).put("payload_sha256", text?.let { PaperMenuCapture.sha256Hex(it) } ?: "")
            .put("state", LOCAL_CORRUPT_RETAINED).put("attempts", 0).put("total_attempts", 0).put("recovery_rounds", 0)
            .put("enqueued_at_ms", pf.lastModified()).put("last_outcome", fault).put("last_at_ms", nowMs)
            .put("recovered", JSONObject().put("reason", "invalid_payload").put("corrupt_state_file", corruptNote ?: JSONObject.NULL))
        report.faults.add("$menuId:$fault")
        if (!atomicWrite(stateFile(dir, menuId), st.toString())) report.storageErrors.add("$menuId:fault_state_write_failed")
    }

    /** Reconciles the whole directory. Idempotent; never deletes or rewrites a frozen payload. */
    fun reconcile(dir: File, nowMs: Long): ReconcileReport = synchronized(this) {
        val report = ReconcileReport()
        val names = dir.list() ?: return report
        val ids = names.mapNotNull { menuIdOf(it) }.toSortedSet()
        for (id in ids) reconcileOne(dir, id, nowMs, report)
        report.faults.addAll(scanFaults(dir).filter { f -> report.faults.none { it == f } })
        report
    }

    /** Files that are not part of a usable entry: never silently ignored (reported by [summary]). */
    fun scanFaults(dir: File): List<String> {
        val names = dir.list()?.sorted() ?: return emptyList()
        val out = mutableListOf<String>()
        for (n in names) {
            when {
                n.contains(".outbox.corrupt.") -> out.add("$n:corrupt_state_file_retained")
                n.endsWith(".tmp") -> out.add("$n:uncommitted_temp_file_retained")
                n.endsWith(".outbox.json") -> {
                    val id = menuIdOf(n)
                    if (id == null || parseState(File(dir, n), id) == null) out.add("$n:unusable_state_file")
                }
                n.endsWith(".json") -> {
                    val id = menuIdOf(n)
                    if (id == null) out.add("$n:unrecognized_file")
                    else if (!stateFile(dir, id).isFile) out.add("$n:payload_without_state")
                }
                else -> out.add("$n:unrecognized_file")
            }
        }
        return out
    }

    /** Valid frozen CAPTURED entry for [menuId] (state usable, payload bytes match the frozen sha), else null. */
    fun validCaptured(dir: File, menuId: String): Entry? {
        val st = readState(dir, menuId) ?: return null
        if (st.optString("kind") != KIND_CAPTURED) return null
        if (st.optString("state") in setOf(LOCAL_CORRUPT_RETAINED, AMBIGUOUS_RETAINED)) return null
        val f = File(dir, st.optString("payload_file"))
        val text = readText(f) ?: return null
        if (PaperMenuCapture.sha256Hex(text) != st.optString("payload_sha256")) return null
        return Entry(menuId, KIND_CAPTURED, st.optString("payload_sha256"), f, st)
    }

    /** Freezes a CAPTURED payload. Returns the entry state (existing one if already enqueued), or null on I/O failure. */
    fun enqueueCaptured(dir: File, menuId: String, sessionDate: String, payload: String, nowMs: Long): JSONObject? =
        enqueue(dir, menuId, KIND_CAPTURED, payload, nowMs, sessionDate)

    /** Freezes a MISSED_SESSION manifest. Returns the entry state, or null on I/O failure. */
    fun enqueueMissed(dir: File, missedManifest: JSONObject, nowMs: Long): JSONObject? =
        enqueue(dir, missedManifest.getString("menu_id"), KIND_MISSED, missedManifest.toString(), nowMs,
            missedManifest.optString("session_date"))

    fun entries(dir: File): List<Entry> {
        // Unusable state files are not entries; they are reported by [scanFaults] and repaired by [reconcile].
        val files = dir.listFiles { f -> f.name.endsWith(".outbox.json") } ?: return emptyList()
        return files.mapNotNull { f ->
            val id = menuIdOf(f.name) ?: return@mapNotNull null
            val st = parseState(f, id) ?: return@mapNotNull null
            Entry(st.optString("menu_id"), st.optString("kind"), st.optString("payload_sha256"),
                File(dir, st.optString("payload_file")), st)
        }.sortedWith(compareBy({ it.state.optString("session_date") }, { it.state.optLong("enqueued_at_ms") }, { it.menuId }))
    }

    /**
     * Reconciles the directory, then sends every pending entry oldest-first. [upload] receives the frozen bytes
     * (verified against the frozen sha). [sessionDate] (IST) grants EXHAUSTED entries one new recovery round per new
     * session. A delivery counts as acknowledged only when its ACKED state was durably written; a failed state write
     * is reported in [DrainSummary.storageErrors] (the entry is then re-sent later, which the server treats as replay).
     */
    fun drain(dir: File, sessionDate: String, nowMs: Long, upload: (Entry, String) -> Delivery): DrainSummary {
        val rec = reconcile(dir, nowMs)
        val storage = rec.storageErrors.toMutableList()
        var attempted = 0; var acked = 0; var requeued = 0
        fun save(e: Entry, st: JSONObject, what: String): Boolean = synchronized(this) {
            atomicWrite(stateFile(dir, e.menuId), st.toString()).also { if (!it) storage.add("${e.menuId}:state_write_failed:$what") }
        }
        for (e in entries(dir)) {
            val st = e.state
            if (st.optString("state") == EXHAUSTED && st.optString("last_recovery_session", st.optString("exhausted_session")) < sessionDate) {
                st.put("state", RETRY_PENDING).put("attempts", 0).put("recovery_rounds", st.optInt("recovery_rounds") + 1)
                    .put("last_recovery_session", sessionDate)
                if (save(e, st, "requeue")) requeued++
            }
            if (st.optString("state") !in PENDING) continue
            val text = if (e.payloadFile.isFile) runCatching { e.payloadFile.readText(Charsets.UTF_8) }.getOrNull() else null
            if (text == null || PaperMenuCapture.sha256Hex(text) != e.payloadSha256) {
                st.put("state", LOCAL_CORRUPT_RETAINED).put("last_outcome", if (text == null) "FROZEN_FILE_MISSING" else "FROZEN_FILE_SHA_MISMATCH")
                    .put("last_at_ms", nowMs)
                save(e, st, "local_corrupt")
                continue
            }
            attempted++
            val d = runCatching { upload(e, text) }.getOrElse { Delivery("RETRY", "EXCEPTION:${it.javaClass.simpleName}") }
            st.put("attempts", st.optInt("attempts") + 1).put("total_attempts", st.optInt("total_attempts") + 1)
                .put("last_outcome", d.outcome).put("last_detail", d.detail ?: JSONObject.NULL).put("last_at_ms", nowMs)
            when (d.disposition) {
                "READBACK_INCOMPLETE" -> st.put("readback_state", "INCOMPLETE")
                "MISMATCH" -> st.put("readback_state", "MISMATCH")
                "ACKNOWLEDGED" -> st.put("readback_state", "VERIFIED")
            }
            val newState = when (d.disposition) {
                "ACKNOWLEDGED" -> { st.put("acked_at_ms", nowMs); ACKED }
                "CONFLICT" -> CONFLICT_RETAINED
                "MISMATCH" -> MISMATCH_RETAINED
                "REJECTED_PERMANENT" -> REJECTED_RETAINED
                // RETRY and READBACK_INCOMPLETE (unproven readback) stay retryable.
                else -> if (st.optInt("attempts") >= MAX_ATTEMPTS_PER_ROUND) {
                    st.put("exhausted_session", sessionDate).put("last_recovery_session", sessionDate); EXHAUSTED
                } else RETRY_PENDING
            }
            st.put("state", newState)
            if (save(e, st, "after_delivery:${d.disposition}") && newState == ACKED) acked++
        }
        val all = entries(dir).map { it.state.optString("state") }
        return DrainSummary(attempted, acked, all.count { it in PENDING }, all.count { it == EXHAUSTED },
            all.count { it !in PENDING && it != ACKED && it != EXHAUSTED }, requeued, storage, rec)
    }

    /** Deletes only ACKED entries (state + payload) acknowledged before nowMs - retentionMs. Returns removed menu ids. */
    fun prune(dir: File, nowMs: Long, retentionMs: Long): List<String> {
        val removed = mutableListOf<String>()
        for (e in entries(dir)) {
            if (e.state.optString("state") != ACKED) continue
            if (e.state.optLong("acked_at_ms", Long.MAX_VALUE) >= nowMs - retentionMs) continue
            // payload first: a crash between the two deletes leaves an ACKED state without payload, never the reverse.
            if (e.payloadFile.exists() && !e.payloadFile.delete()) continue
            if (stateFile(dir, e.menuId).delete()) removed.add(e.menuId)
        }
        return removed
    }

    /** Observable recovery summary (logged and stored for the PWA/diagnostics). */
    fun summary(dir: File): JSONObject {
        val es = entries(dir)
        val byState = JSONObject()
        es.groupBy { it.state.optString("state") }.forEach { (k, v) -> byState.put(k, v.size) }
        val unacked = es.filter { it.state.optString("state") != ACKED }
        return JSONObject().put("schema", SCHEMA).put("total", es.size).put("by_state", byState)
            .put("unacked_menu_ids", org.json.JSONArray(unacked.map { "${it.menuId}:${it.state.optString("state")}" }))
            .put("oldest_unacked_session", unacked.map { it.state.optString("session_date") }.filter { it.isNotBlank() }.minOrNull() ?: JSONObject.NULL)
            .put("faults", org.json.JSONArray(scanFaults(dir) + es.filter { it.state.optString("state") in
                setOf(LOCAL_CORRUPT_RETAINED, AMBIGUOUS_RETAINED) }.map { "${it.menuId}:${it.state.optString("state")}:${it.state.optString("last_outcome")}" }))
    }
}
