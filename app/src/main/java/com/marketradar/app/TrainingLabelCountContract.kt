package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale

/**
 * Single documented count contract for Paper training progress
 * (`training_label_progress`, version [VERSION]).
 *
 * A row counts as a verified chosen training label only when ALL hold:
 *  1. role == "primary" (the chosen recommendation; secondary/alternative and
 *     rejected rows are research populations and are counted separately),
 *  2. row session_date == the requested session (other sessions are excluded),
 *  3. lane is one of NF_intraday/NF_swing/BNF_intraday/BNF_swing and, if
 *     index_key is present, it agrees with the lane's index (no NF/BNF cross-count),
 *  4. unique identity snapshot_id|candidate_id|role is present and first-seen
 *     (missing parts => attribution_pending; repeats => duplicate_rows),
 *  5. outcome is known (canonical_won, else outcome_h2, else won in {0,1});
 *     unknown outcomes are primary_outcome_unknown, never losses,
 *  6. the session's authoritative evaluation ledger gate is open:
 *     labels_saved == true, phase != RESEARCH_ONLY, and the rows came from the
 *     session-attributed source (not the recent-outcomes fallback).
 *
 * When the gate is closed, verified counts are 0 and the observed primary
 * counts are reported as `primary_labeled_unverified`. win_rate_pct is null
 * whenever the eligible denominator is 0 (unknown, not 0%). The contract never
 * emits a retrain-readiness flag; readiness is a separate validation decision.
 * Old records are not rewritten; this is a read-side classification only.
 */
object TrainingLabelCountContract {
    const val VERSION = "chosen_primary_training_labels_v1"
    const val SCOPE_SESSION_ATTRIBUTED = "session_attributed"
    const val SCOPE_RECENT_FALLBACK = "recent_outcomes_fallback"
    val LANES = listOf("NF_intraday", "NF_swing", "BNF_intraday", "BNF_swing")

    data class LedgerGate(
        /** evaluation phase recorded for this exact session, or blank if unknown. */
        val phase: String?,
        /** ledger labels_saved for this exact session; null when no ledger run exists. */
        val labelsSaved: Boolean?
    )

    private fun normalizeWon(value: Any?): Int? = when (value) {
        is Boolean -> if (value) 1 else 0
        is Number -> value.toInt().takeIf { (it == 0 || it == 1) && value.toDouble() == it.toDouble() }
        is String -> when (value.trim().lowercase(Locale.US)) {
            "1", "true", "yes" -> 1
            "0", "false", "no" -> 0
            else -> null
        }
        else -> null
    }

    private fun field(row: JSONObject, key: String): Any? = if (row.isNull(key)) null else row.opt(key)

    fun rowWon(row: JSONObject): Int? =
        normalizeWon(field(row, "canonical_won") ?: field(row, "outcome_h2") ?: field(row, "won"))

    private fun laneIndex(lane: String): String = lane.substringBefore('_')

    private fun indexMatches(lane: String, indexKey: String): Boolean {
        val k = indexKey.trim().uppercase(Locale.US)
        if (k.isEmpty()) return true
        val want = laneIndex(lane)
        val norm = when (k) {
            "NF", "NIFTY", "NIFTY50", "NIFTY 50" -> "NF"
            "BNF", "BANKNIFTY", "NIFTYBANK", "NIFTY BANK", "BANK NIFTY" -> "BNF"
            else -> k
        }
        return norm == want
    }

    private class Lane {
        var primaryRows = 0; var primaryLabeled = 0; var primaryWins = 0; var primaryUnknown = 0
        var secondaryRows = 0; var secondaryLabeled = 0; var secondaryWins = 0
        var rejectedRows = 0
    }

    fun build(rows: JSONArray, sessionDate: String, gate: LedgerGate, sourceScope: String): JSONObject {
        val lanes = LANES.associateWith { Lane() }
        val seen = HashSet<String>()
        var otherSession = 0
        var duplicates = 0
        var attributionPending = 0
        var crossIndexRejected = 0
        for (i in 0 until rows.length()) {
            val row = rows.optJSONObject(i) ?: continue
            val rowSession = row.optString("session_date", "").trim().take(10)
            if (rowSession != sessionDate) { otherSession += 1; continue }
            val role = row.optString("role", "").trim().lowercase(Locale.US)
            val laneKey = row.optString("lane", "").trim()
            val lane = lanes[laneKey]
            val snap = row.optString("snapshot_id", "").trim().takeUnless { it.isEmpty() || it == "null" }
            val cand = row.optString("candidate_id", "").trim().takeUnless { it.isEmpty() || it == "null" }
            if (lane == null || snap == null || cand == null || role !in setOf("primary", "secondary", "rejected")) {
                attributionPending += 1; continue
            }
            if (!indexMatches(laneKey, row.optString("index_key", ""))) { crossIndexRejected += 1; continue }
            if (!seen.add("$snap|$cand|$role")) { duplicates += 1; continue }
            val won = rowWon(row)
            when (role) {
                "primary" -> {
                    lane.primaryRows += 1
                    if (won == null) lane.primaryUnknown += 1 else {
                        lane.primaryLabeled += 1; if (won == 1) lane.primaryWins += 1
                    }
                }
                "secondary" -> {
                    lane.secondaryRows += 1
                    if (won != null) { lane.secondaryLabeled += 1; if (won == 1) lane.secondaryWins += 1 }
                }
                else -> lane.rejectedRows += 1
            }
        }

        val phase = gate.phase?.trim()?.uppercase(Locale.US).orEmpty()
        val ineligibleReason = when {
            sourceScope != SCOPE_SESSION_ATTRIBUTED -> "SOURCE_NOT_SESSION_ATTRIBUTED"
            phase == EvaluationIdentityCoverage.PHASE_RESEARCH_ONLY -> "RESEARCH_ONLY"
            gate.labelsSaved == null -> "LEDGER_UNAVAILABLE"
            gate.labelsSaved == false -> "LABELS_NOT_SAVED"
            else -> null
        }
        val eligible = ineligibleReason == null

        fun rate(w: Int, n: Int): Any = if (n > 0) String.format(Locale.US, "%.2f", w * 100.0 / n).toDouble() else JSONObject.NULL

        val lanesJson = JSONObject()
        val indexTotals = linkedMapOf("NF" to IntArray(4), "BNF" to IntArray(4)) // verified, wins, secondaryRows, pendingPrimaryUnknown
        var tVerified = 0; var tWins = 0; var tPrimary = 0; var tPrimaryLabeled = 0; var tUnknown = 0
        var tSecondary = 0; var tSecondaryLabeled = 0; var tRejected = 0
        for ((key, l) in lanes) {
            val verified = if (eligible) l.primaryLabeled else 0
            val wins = if (eligible) l.primaryWins else 0
            lanesJson.put(key, JSONObject()
                .put("index", laneIndex(key))
                .put("mode", key.substringAfter('_'))
                .put("verified_training_labels", verified)
                .put("verified_training_wins", wins)
                .put("win_rate_pct", rate(wins, verified))
                .put("primary_rows", l.primaryRows)
                .put("primary_labeled_unverified", if (eligible) 0 else l.primaryLabeled)
                .put("primary_outcome_unknown", l.primaryUnknown)
                .put("secondary_research_rows", l.secondaryRows)
                .put("secondary_research_labeled", l.secondaryLabeled)
                .put("secondary_research_wins", l.secondaryWins)
                .put("rejected_research_rows", l.rejectedRows))
            indexTotals.getValue(laneIndex(key)).let { it[0] += verified; it[1] += wins; it[2] += l.secondaryRows; it[3] += l.primaryUnknown }
            tVerified += verified; tWins += wins; tPrimary += l.primaryRows; tPrimaryLabeled += l.primaryLabeled
            tUnknown += l.primaryUnknown; tSecondary += l.secondaryRows; tSecondaryLabeled += l.secondaryLabeled; tRejected += l.rejectedRows
        }
        val indexJson = JSONObject()
        for ((k, v) in indexTotals) {
            indexJson.put(k, JSONObject()
                .put("verified_training_labels", v[0])
                .put("verified_training_wins", v[1])
                .put("win_rate_pct", rate(v[1], v[0]))
                .put("secondary_research_rows", v[2])
                .put("primary_outcome_unknown", v[3]))
        }
        return JSONObject()
            .put("contract_version", VERSION)
            .put("session_date", sessionDate)
            .put("source_scope", sourceScope)
            .put("ledger_phase", phase)
            .put("ledger_labels_saved", gate.labelsSaved ?: JSONObject.NULL)
            .put("eligible", eligible)
            .put("ineligible_reason", ineligibleReason ?: JSONObject.NULL)
            .put("verified_training_labels", tVerified)
            .put("verified_training_wins", tWins)
            .put("win_rate_pct", rate(tWins, tVerified))
            .put("primary_rows", tPrimary)
            .put("primary_labeled_unverified", if (eligible) 0 else tPrimaryLabeled)
            .put("primary_outcome_unknown", tUnknown)
            .put("secondary_research_rows", tSecondary)
            .put("secondary_research_labeled", tSecondaryLabeled)
            .put("rejected_research_rows", tRejected)
            .put("attribution_pending_rows", attributionPending)
            .put("cross_index_mismatch_rows", crossIndexRejected)
            .put("duplicate_rows", duplicates)
            .put("other_session_rows", otherSession)
            .put("index_totals", indexJson)
            .put("lanes", lanesJson)
    }
}
