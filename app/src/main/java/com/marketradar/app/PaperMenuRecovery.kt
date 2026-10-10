package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * R2-review Blocker B: the two storage boundaries of Paper-menu capture, extracted from MarketWatchService so the
 * exact sequence the service runs is JVM-testable with injected crashes / failed writes.
 *
 *  preparePoll   : outbox directory reconcile -> state reconcile with frozen captures -> rollover / MISSED
 *                  generation -> MISSED frozen into the outbox -> preference commit (checked).
 *  commitCapture : freeze CAPTURED bytes into the outbox -> preference commit (checked). An already-frozen capture
 *                  for the same session is kept and the preference state is reconciled to it (no replacement).
 *
 * Evidence only; nothing here is read by ranking, gates, sizing, exits or primary selection.
 */
object PaperMenuRecovery {
    data class PollPrep(
        val prepared: PaperMenuCapture.Prepared,
        val reconcile: PaperMenuOutbox.ReconcileReport?,
        /** null = nothing to save (non-Paper); false = preference commit failed or was skipped after an enqueue failure. */
        val stateSaved: Boolean?,
        /** (level, message) log events in order. */
        val events: List<Pair<Char, String>>
    )

    fun frozenLookup(dir: File) = PaperMenuCapture.FrozenCaptures { id ->
        PaperMenuOutbox.validCaptured(dir, id)?.let { it.payloadSha256 to it.payloadFile.absolutePath }
    }

    fun preparePoll(dir: File, populationMode: String?, rawState: String?, sessionDate: String, slotKey: String?,
                    slotOrdinal: Int?, nowIso: String, nowMs: Long, saveState: (String) -> Boolean): PollPrep {
        val events = mutableListOf<Pair<Char, String>>()
        if (!ExecutionModeResolver.isPaper(populationMode))
            return PollPrep(PaperMenuCapture.prepare(populationMode, rawState, sessionDate, slotKey, slotOrdinal, nowIso), null, null, events)
        val rec = PaperMenuOutbox.reconcile(dir, nowMs)
        rec.recovered.forEach { events.add('W' to "PAPER_MENU_OUTBOX_RECOVERED: $it (original frozen bytes kept)") }
        rec.faults.forEach { events.add('E' to "PAPER_MENU_OUTBOX_FAULT_RETAINED: $it") }
        rec.storageErrors.forEach { events.add('E' to "PAPER_MENU_STORAGE_ERROR: $it") }
        val p = PaperMenuCapture.prepare(populationMode, rawState, sessionDate, slotKey, slotOrdinal, nowIso, frozenLookup(dir))
        val suppressed = mutableSetOf<String>()
        var frozenOk = true
        for (m in p.missedToEnqueue) {
            val id = m.optString("menu_id")
            val e = runCatching { PaperMenuOutbox.enqueueMissed(dir, m, nowMs) }.getOrNull()
            when {
                e == null -> {
                    frozenOk = false
                    events.add('E' to "PAPER_MENU_OUTBOX_ENQUEUE_FAIL: menu=$id kind=MISSED (state not advanced; retried next poll)")
                }
                e.optString("kind") != PaperMenuOutbox.KIND_MISSED -> {
                    suppressed.add(id)
                    events.add('W' to "PAPER_MENU_MISSED_SUPPRESSED: menu=$id existing_entry=${e.optString("kind")}:${e.optString("state")} (a frozen capture is never replaced by MISSED)")
                }
            }
        }
        p.events.forEach { ev ->
            val isMissed = ev.startsWith("PAPER_MENU_MISSED_SESSION")
            if (isMissed && suppressed.any { ev.contains("menu=$it") }) return@forEach
            events.add((if (isMissed) 'W' else 'I') to ev)
        }
        val saved = if (frozenOk && p.stateToSave != null) {
            runCatching { saveState(p.stateToSave) }.getOrDefault(false).also {
                if (!it) events.add('E' to "PAPER_MENU_STORAGE_ERROR: capture_state_commit_failed (outbox entries retained; state re-derived and reconciled next poll)")
            }
        } else false
        return PollPrep(p, rec, saved, events)
    }

    data class CommitResult(
        /** CAPTURED | ALREADY_FROZEN_RECONCILED | NOT_OPEN | FAILED */
        val status: String,
        val reason: String?,
        val payloadSha256: String?,
        val payloadBytes: Int,
        val stateSaved: Boolean,
        val events: List<Pair<Char, String>>
    )

    fun commitCapture(dir: File, live: JSONObject, slotKey: String, frozen: JSONObject, nowIso: String, nowMs: Long,
                      saveState: (JSONObject) -> Boolean): CommitResult {
        val events = mutableListOf<Pair<Char, String>>()
        if (live.optString("status") != "OPEN") return CommitResult("NOT_OPEN", live.optString("status"), null, 0, false, events)
        val menuId = live.getString("menu_id")
        // The frozen payload carries every attempt including this successful one.
        val attempts = JSONArray(live.getJSONArray("attempts").toString()).put(
            JSONObject().put("slot_key", slotKey).put("outcome", "CAPTURED").put("detail", JSONObject.NULL).put("at", nowIso))
        frozen.put("attempts", attempts)
        val payload = frozen.toString()
        val sha = PaperMenuCapture.sha256Hex(payload)
        fun save(): Boolean = runCatching { saveState(live) }.getOrDefault(false).also {
            if (!it) events.add('E' to "PAPER_MENU_STORAGE_ERROR: capture_state_commit_failed menu=$menuId (frozen outbox evidence retained; reconciled next poll)")
        }
        val entry = runCatching { PaperMenuOutbox.enqueueCaptured(dir, menuId, live.optString("session_date"), payload, nowMs) }.getOrNull()
        if (entry == null) {
            PaperMenuCapture.recordAttempt(live, slotKey, "FAILED", "frozen_file_write_failed", nowIso)
            val saved = save()
            events.add('E' to "PAPER_MENU_CAPTURE_FAILED: slot=$slotKey reason=frozen_file_write_failed")
            return CommitResult("FAILED", "frozen_file_write_failed", null, 0, saved, events)
        }
        if (entry.optString("payload_sha256") != sha) {
            val existing = PaperMenuOutbox.validCaptured(dir, menuId)
            if (existing != null) {
                // Already frozen for this session (earlier enqueue whose state commit was lost): keep the first bytes.
                PaperMenuCapture.recordAttempt(live, slotKey, "DISCARDED", "existing_frozen_capture_kept", nowIso)
                PaperMenuCapture.markCaptured(live, existing.payloadSha256, existing.payloadFile.absolutePath, nowIso)
                val saved = save()
                events.add('W' to "PAPER_MENU_STATE_RECONCILED: menu=$menuId slot=$slotKey existing frozen capture kept sha=${existing.payloadSha256.take(12)}")
                return CommitResult("ALREADY_FROZEN_RECONCILED", "existing_frozen_capture_kept", existing.payloadSha256, 0, saved, events)
            }
            PaperMenuCapture.recordAttempt(live, slotKey, "FAILED", "outbox_entry_exists_with_other_bytes", nowIso)
            val saved = save()
            events.add('E' to "PAPER_MENU_CAPTURE_FAILED: slot=$slotKey reason=outbox_entry_exists_with_other_bytes:${entry.optString("kind")}:${entry.optString("state")}")
            return CommitResult("FAILED", "outbox_entry_exists_with_other_bytes", null, 0, saved, events)
        }
        val target = File(dir, entry.optString("payload_file"))
        PaperMenuCapture.recordAttempt(live, slotKey, "CAPTURED", null, nowIso)
        PaperMenuCapture.markCaptured(live, sha, target.absolutePath, nowIso)
        val saved = save()
        return CommitResult("CAPTURED", null, sha, payload.toByteArray(Charsets.UTF_8).size, saved, events)
    }
}
