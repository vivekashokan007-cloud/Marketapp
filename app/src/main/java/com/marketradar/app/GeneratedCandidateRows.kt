package com.marketradar.app

import com.marketradar.app.util.LogBuffer
import org.json.JSONArray
import org.json.JSONObject

/**
 * b503: the generated-candidate cohort builder, moved verbatim out of MarketWatchService so the
 * capture fan-out can build the cohort from the FROZEN fact pack on any owner thread and be
 * tested directly. Semantics (50-row bounded sample, zero-candidate rejected rows, lineage via
 * recommendation_id) are unchanged.
 */
object GeneratedCandidateRows {
    private const val TAG = "MarketWatchService"
    const val PERSIST_CAP = 50

    /** Test seam: android.util.Log is unavailable in JVM tests. */
    @Volatile
    internal var logger: (Char, String) -> Unit = { level, msg -> LogBuffer.add(level, TAG, msg) }

    fun build(factPack: JSONObject, snapObj: JSONObject): JSONArray {
        val candidates = factPack.optJSONArray("candidates") ?: return JSONArray()
        val candidateCounts = factPack.optJSONObject("candidate_counts") ?: JSONObject()
        val persistedCandidateCount = if (!candidateCounts.isNull("ranked_evidence")) {
            candidateCounts.opt("ranked_evidence")
        } else {
            candidateCounts.opt("generated")
        }
        // Compatibility field: generated_count is the ranked-evidence count here,
        // not the uncapped in-memory generator population. The Python shadow payload
        // carries the explicit generated_count_semantics/ranked_evidence_count labels.
        val generationSkip = factPack.optJSONObject("generation_skip_reason")
        val generationSkipSummary = generationSkip?.optString("detail")
            ?: factPack.optString("generation_skip_reason", "")
        if (candidates.length() == 0) {
            val snapshotContext = snapObj.optJSONObject("context_json")
            val rejectedRows = factPack.optJSONArray("rejected_candidates")
                ?: snapshotContext?.optJSONArray("snapshot_rejected_candidates")
            val rejectedStats = snapshotContext?.optJSONObject("snapshot_rejected_candidate_stats")
                ?: factPack.optJSONObject("rejected_candidate_stats")
            val trace = snapshotContext?.optJSONObject("candidate_generation_trace")
            val rejectedCount = rejectedStats?.optInt("total", 0) ?: 0
            val traceAccepted = trace?.optInt("accepted_count", 0) ?: 0
            val traceRejected = trace?.optInt("rejected_count", 0) ?: 0
            val byStage = rejectedStats?.optJSONObject("by_stage")
            val topStage = byStage?.keys()?.asSequence()?.map { key ->
                "$key:${byStage.optInt(key, 0)}"
            }?.take(3)?.joinToString(",") ?: ""
            val pollTs = factPack.optString("poll_timestamp", snapObj.optString("poll_ts", "")).trim()
            val diag = buildString {
                append("ML_GENERATED_CANDIDATES_SKIP: generated=0")
                append(" rejected=")
                append(rejectedCount)
                append(" traceAccepted=")
                append(traceAccepted)
                append(" traceRejected=")
                append(traceRejected)
                if (topStage.isNotBlank()) {
                    append(" byStage=")
                    append(topStage)
                }
                if (!generationSkipSummary.isNullOrBlank()) {
                    append(" skip=")
                    append(generationSkipSummary)
                }
                if (pollTs.isNotBlank()) {
                    append(" pollTs=")
                    append(pollTs)
                }
            }
            runCatching { logger('W', diag) }
            if (rejectedRows == null || rejectedRows.length() == 0) return JSONArray()

            val syntheticRows = buildRejectedCandidateRows(
                rejectedRows = rejectedRows,
                factPack = factPack,
                snapObj = snapObj,
                candidateCounts = candidateCounts,
                persistedCandidateCount = persistedCandidateCount,
                generationSkipSummary = generationSkipSummary
            )
            if (syntheticRows.length() == 0) return JSONArray()
            return syntheticRows
        }

        val selected = boundedGeneratedCandidates(candidates, PERSIST_CAP)
        if (selected.length() == 0) return JSONArray()

        val pollTs = factPack.optString("poll_timestamp", snapObj.optString("poll_ts", "")).trim()
        val sessionDate = factPack.optString("session_date", snapObj.optString("session_date", "")).trim()
        val recommendationId = snapObj.opt("recommendation_id")
        val qualityTag = factPack.optString("quality_tag", "qualitative_prompt_v2")
        val decisionSource = factPack.optString("decision_source", "")
        val appVersion = factPack.optString("app_version", BuildConfig.VERSION_NAME)
        val brainVersion = factPack.optString("brain_version", "")
        val signalIndependence = factPack.optJSONObject("signal_independence") ?: JSONObject()

        fun JSONObject.putIfValue(key: String, value: Any?) {
            if (value == null || value == JSONObject.NULL) return
            when (value) {
                is String -> if (value.isNotBlank()) put(key, value)
                else -> put(key, value)
            }
        }

        val stableKeys = listOf(
            "snapshot_poll_ts",
            "session_date",
            "recommendation_id",
            "quality_tag",
            "decision_source",
            "app_version",
            "brain_version",
            "candidate_id",
            "lane",
            "index_key",
            "strategy_type",
            "trade_mode",
            "rank",
            "watchlist_rank",
            "was_surfaced",
            "net_premium",
            "max_profit",
            "max_loss",
            "risk_reward",
            "premium_edge",
            "ev_per_1k",
            "est_cost",
            "capital_blocked",
            "width",
            "expiry",
            "sigma_otm",
            "iv_richness",
            "credit_width_ratio",
            "is_credit",
            "execution_ready",
            "execution_gate",
            "entry_action",
            "direction_safe",
            "brain_score",
            "p_ml",
            "ml_action",
            "ml_edge",
            "ml_regime",
            "ml_unsure",
            "ml_ood_flag",
            "signal_independence_score",
            "generated_count",
            "watchlist_count"
        )

        val rows = JSONArray()
        for (i in 0 until selected.length()) {
            val candidate = selected.optJSONObject(i) ?: continue
            val row = JSONObject().apply {
                stableKeys.forEach { put(it, JSONObject.NULL) }
            }
            row.putIfValue("snapshot_poll_ts", pollTs)
            row.putIfValue("session_date", sessionDate)
            row.putIfValue("recommendation_id", recommendationId)
            row.putIfValue("quality_tag", qualityTag)
            row.putIfValue("decision_source", decisionSource)
            row.putIfValue("app_version", candidate.optString("app_version", appVersion))
            row.putIfValue("brain_version", candidate.optString("brain_version", brainVersion))
            row.putIfValue("candidate_id", candidate.opt("candidate_id"))
            row.putIfValue("lane", candidate.optString("lane"))
            row.putIfValue("index_key", candidate.optString("index"))
            row.putIfValue("strategy_type", candidate.optString("strategy_type"))
            row.putIfValue("trade_mode", candidate.optString("trade_mode"))
            row.putIfValue("rank", candidate.opt("rank"))
            if (!candidate.isNull("watchlist_rank")) {
                row.put("watchlist_rank", candidate.opt("watchlist_rank"))
                row.put("was_surfaced", true)
            } else {
                row.put("was_surfaced", false)
            }

            val economics = candidate.optJSONObject("economics") ?: JSONObject()
            row.putIfValue("net_premium", economics.opt("net_premium"))
            row.putIfValue("max_profit", economics.opt("max_profit"))
            row.putIfValue("max_loss", economics.opt("max_loss"))
            row.putIfValue("risk_reward", economics.opt("risk_reward"))
            row.putIfValue("premium_edge", economics.opt("premium_edge"))
            row.putIfValue("ev_per_1k", economics.opt("ev_per_1k"))
            row.putIfValue("est_cost", economics.opt("est_cost"))
            row.putIfValue("capital_blocked", economics.opt("capital_blocked"))

            val structure = candidate.optJSONObject("structure") ?: JSONObject()
            row.putIfValue("width", structure.opt("width"))
            row.putIfValue("expiry", structure.optString("expiry"))
            row.putIfValue("sigma_otm", structure.opt("sigma_otm"))
            row.putIfValue("iv_richness", structure.opt("iv_richness"))
            row.putIfValue("credit_width_ratio", structure.opt("credit_width_ratio"))
            row.putIfValue("is_credit", structure.opt("is_credit"))

            val execution = candidate.optJSONObject("execution") ?: JSONObject()
            row.putIfValue("execution_ready", execution.opt("execution_ready"))
            row.putIfValue("execution_gate", execution.optString("execution_gate"))
            row.putIfValue("entry_action", execution.optString("entry_action"))
            row.putIfValue("direction_safe", execution.opt("direction_safe"))

            val overlay = candidate.optJSONObject("ml_overlay") ?: JSONObject()
            row.putIfValue("brain_score", overlay.opt("brain_score"))
            row.putIfValue("p_ml", overlay.opt("p_ml"))
            row.putIfValue("ml_action", overlay.optString("ml_action"))
            row.putIfValue("ml_edge", overlay.opt("ml_edge"))
            row.putIfValue("ml_regime", overlay.optString("ml_regime"))
            row.putIfValue("ml_unsure", overlay.opt("ml_unsure"))
            row.putIfValue("ml_ood_flag", overlay.opt("ml_ood_flag"))

            row.putIfValue("signal_independence_score", signalIndependence.opt("score"))
            row.putIfValue("generated_count", persistedCandidateCount)
            row.putIfValue("watchlist_count", candidateCounts.opt("watchlist"))
            rows.put(row)
        }

        if (rows.length() == 0) return JSONArray()
        return rows
    }

    private fun buildRejectedCandidateRows(
        rejectedRows: JSONArray,
        factPack: JSONObject,
        snapObj: JSONObject,
        candidateCounts: JSONObject,
        persistedCandidateCount: Any?,
        generationSkipSummary: String
    ): JSONArray {
        val pollTs = factPack.optString("poll_timestamp", snapObj.optString("poll_ts", "")).trim()
        val sessionDate = factPack.optString("session_date", snapObj.optString("session_date", "")).trim()
        val recommendationId = snapObj.opt("recommendation_id")
        val qualityTag = factPack.optString("quality_tag", "qualitative_prompt_v2")
        val decisionSource = factPack.optString("decision_source", "")
        val appVersion = factPack.optString("app_version", BuildConfig.VERSION_NAME)
        val brainVersion = factPack.optString("brain_version", "")
        val stableKeys = listOf(
            "snapshot_poll_ts",
            "session_date",
            "recommendation_id",
            "quality_tag",
            "decision_source",
            "app_version",
            "brain_version",
            "candidate_id",
            "lane",
            "index_key",
            "strategy_type",
            "trade_mode",
            "rank",
            "watchlist_rank",
            "was_surfaced",
            "net_premium",
            "max_profit",
            "max_loss",
            "risk_reward",
            "premium_edge",
            "ev_per_1k",
            "est_cost",
            "capital_blocked",
            "width",
            "expiry",
            "sigma_otm",
            "iv_richness",
            "credit_width_ratio",
            "is_credit",
            "execution_ready",
            "execution_gate",
            "entry_action",
            "direction_safe",
            "brain_score",
            "p_ml",
            "ml_action",
            "ml_edge",
            "ml_regime",
            "ml_unsure",
            "ml_ood_flag",
            "signal_independence_score",
            "generated_count",
            "watchlist_count"
        )

        fun JSONObject.putIfValue(key: String, value: Any?) {
            if (value == null || value == JSONObject.NULL) return
            when (value) {
                is String -> if (value.isNotBlank()) put(key, value)
                else -> put(key, value)
            }
        }

        fun syntheticRejectedId(candidate: JSONObject, ordinal: Int): String {
            val raw = listOf(
                pollTs,
                candidate.optString("index"),
                candidate.optString("lane"),
                candidate.optString("strategy_type"),
                candidate.optString("expiry"),
                candidate.opt("sellStrike"),
                candidate.optString("sellType"),
                candidate.opt("buyStrike"),
                candidate.optString("buyType"),
                candidate.optString("rejection_stage"),
                candidate.optString("rejection_reason"),
                ordinal.toString()
            ).joinToString("|")
            return "rej_${Integer.toUnsignedString(raw.hashCode(), 16)}"
        }

        val bounded = boundedGeneratedCandidates(rejectedRows, PERSIST_CAP)
        val rows = JSONArray()
        for (i in 0 until bounded.length()) {
            val candidate = bounded.optJSONObject(i) ?: continue
            val row = JSONObject().apply {
                stableKeys.forEach { put(it, JSONObject.NULL) }
            }
            val rejectionStage = candidate.optString("rejection_stage").trim()
            val rejectionReason = candidate.optString("rejection_reason").trim()
            row.putIfValue("snapshot_poll_ts", pollTs)
            row.putIfValue("session_date", sessionDate)
            row.putIfValue("recommendation_id", recommendationId)
            row.putIfValue("quality_tag", qualityTag)
            row.putIfValue("decision_source", if (decisionSource.isBlank()) "REJECTED_CANDIDATE" else decisionSource)
            row.putIfValue("app_version", appVersion)
            row.putIfValue("brain_version", brainVersion)
            row.putIfValue("candidate_id", syntheticRejectedId(candidate, i))
            row.putIfValue("lane", candidate.optString("lane"))
            row.putIfValue("index_key", candidate.optString("index"))
            row.putIfValue("strategy_type", candidate.optString("strategy_type"))
            row.putIfValue("trade_mode", candidate.optString("lane").substringAfter('_', ""))
            row.put("was_surfaced", false)
            row.putIfValue("net_premium", candidate.opt("netPremium"))
            row.putIfValue("max_profit", candidate.opt("maxProfit"))
            row.putIfValue("max_loss", candidate.opt("maxLoss"))
            row.putIfValue("width", candidate.opt("width"))
            row.putIfValue("expiry", candidate.optString("expiry"))
            row.putIfValue("sigma_otm", candidate.opt("sigmaOTM"))
            row.putIfValue("iv_richness", candidate.opt("ivRichness"))
            row.putIfValue("credit_width_ratio", candidate.opt("creditWidthRatio"))
            row.putIfValue("is_credit", candidate.opt("is_credit"))
            row.putIfValue("execution_ready", false)
            row.putIfValue("execution_gate", if (rejectionStage.isBlank()) "rejected" else "rejected:$rejectionStage")
            row.putIfValue(
                "entry_action",
                when {
                    rejectionReason.isNotBlank() -> rejectionReason
                    generationSkipSummary.isNotBlank() -> generationSkipSummary
                    else -> "REJECTED"
                }
            )
            row.putIfValue("ml_action", "REJECTED")
            row.putIfValue("generated_count", persistedCandidateCount)
            row.putIfValue("watchlist_count", candidateCounts.opt("watchlist"))
            rows.put(row)
        }
        return rows
    }

    private fun boundedGeneratedCandidates(candidates: JSONArray, cap: Int): JSONArray {
        if (cap <= 0 || candidates.length() == 0) return JSONArray()

        fun candidateId(candidate: JSONObject): String =
            candidate.optString("candidate_id", candidate.optString("id", "")).trim()

        val surfaced = mutableListOf<JSONObject>()
        val unsurfacedByLane = linkedMapOf<String, MutableList<JSONObject>>()
        for (i in 0 until candidates.length()) {
            val candidate = candidates.optJSONObject(i) ?: continue
            val lane = candidate.optString("lane", "").trim()
            if (!candidate.isNull("watchlist_rank")) {
                surfaced.add(JSONObject(candidate.toString()))
            } else {
                unsurfacedByLane.getOrPut(lane.ifBlank { "unknown" }) { mutableListOf() }
                    .add(JSONObject(candidate.toString()))
            }
        }

        surfaced.sortWith(
            compareBy<JSONObject> { candidate ->
                if (candidate.isNull("watchlist_rank")) Int.MAX_VALUE else candidate.optInt("watchlist_rank", Int.MAX_VALUE)
            }.thenBy { candidate ->
                if (candidate.isNull("rank")) Int.MAX_VALUE else candidate.optInt("rank", Int.MAX_VALUE)
            }
        )

        val selected = JSONArray()
        val seenIds = linkedSetOf<String>()

        fun addCandidate(candidate: JSONObject): Boolean {
            if (selected.length() >= cap) return false
            val id = candidateId(candidate)
            if (id.isNotBlank() && !seenIds.add(id)) return true
            selected.put(candidate)
            return selected.length() < cap
        }

        for (candidate in surfaced) {
            if (!addCandidate(candidate)) return selected
        }

        var madeProgress: Boolean
        do {
            madeProgress = false
            for ((_, laneCandidates) in unsurfacedByLane) {
                if (selected.length() >= cap) break
                if (laneCandidates.isEmpty()) continue
                val candidate = laneCandidates.removeAt(0)
                madeProgress = true
                if (!addCandidate(candidate)) return selected
            }
        } while (madeProgress && selected.length() < cap)

        return selected
    }
}
