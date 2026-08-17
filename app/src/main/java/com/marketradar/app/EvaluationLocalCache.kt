package com.marketradar.app

import android.content.Context
import com.marketradar.app.util.LogBuffer
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.RandomAccessFile
import java.time.LocalDate
import java.time.ZoneOffset
import java.time.format.DateTimeParseException
import java.time.temporal.ChronoUnit

object EvaluationLocalCache {
    private const val TAG = "EvaluationLocalCache"
    private const val DIR_NAME = "evaluation_local_cache"
    private const val RETENTION_DAYS = 45L
    private const val MAX_ROWS_PER_SESSION = 90
    private const val MAX_SUMMARY_ROWS_PER_SESSION = 120
    private const val MAX_SUMMARY_BYTES_PER_SESSION = 4L * 1024L * 1024L
    private const val MAX_COMPACT_SNAPSHOT_BYTES = 2L * 1024L * 1024L
    // Keep a full trading session of compact evaluator-grade evidence without
    // retaining the much larger raw Python payloads.
    private const val MAX_BYTES_PER_SESSION = 96L * 1024L * 1024L

    private data class SnapshotFileState(
        val rows: LinkedHashMap<String, String>,
        var totalBytes: Long,
        var needsRewrite: Boolean
    )

    private data class FullSnapshotIndex(
        val rows: LinkedHashMap<String, Long>,
        var totalBytes: Long,
        var needsRewrite: Boolean
    )

    private val snapshotStateByPath = mutableMapOf<String, SnapshotFileState>()
    private val fullSnapshotIndexByPath = mutableMapOf<String, FullSnapshotIndex>()

    private fun cacheDir(context: Context): File {
        return File(context.applicationContext.filesDir, DIR_NAME).apply { mkdirs() }
    }

    private fun safeDate(date: String): String {
        return date.filter { it.isDigit() || it == '-' }.ifBlank { "unknown" }
    }

    private fun brainSnapshotFile(context: Context, sessionDate: String): File {
        return File(cacheDir(context), "brain_snapshots_${safeDate(sessionDate)}.jsonl")
    }

    private fun brainSnapshotSummaryFile(context: Context, sessionDate: String): File {
        return File(cacheDir(context), "brain_snapshot_summaries_${safeDate(sessionDate)}.jsonl")
    }

    private fun build3AbFile(context: Context, sessionDate: String): File {
        return File(cacheDir(context), "build3_ab_${safeDate(sessionDate)}.jsonl")
    }

    private fun snapshotKey(row: JSONObject): String {
        return row.optString("id").ifBlank {
            "${row.optString("poll_ts")}|${row.optString("recommendation_id")}"
        }.ifBlank {
            row.toString()
        }
    }

    private fun pruneExpiredCacheFiles(context: Context) {
        val today = LocalDate.now(ZoneOffset.UTC)
        cacheDir(context).listFiles { file ->
            file.isFile &&
                (
                    file.name.startsWith("brain_snapshots_") ||
                        file.name.startsWith("brain_snapshot_summaries_") ||
                        file.name.startsWith("build3_ab_")
                    ) &&
                file.name.endsWith(".jsonl")
        }?.forEach { file ->
            val sessionDate = file.name
                .removePrefix("brain_snapshot_summaries_")
                .removePrefix("brain_snapshots_")
                .removePrefix("build3_ab_")
                .removeSuffix(".jsonl")
            val keep = try {
                val ageDays = ChronoUnit.DAYS.between(LocalDate.parse(sessionDate), today)
                ageDays in 0..RETENTION_DAYS
            } catch (_: DateTimeParseException) {
                val ageMs = today.plusDays(1).atStartOfDay().toInstant(ZoneOffset.UTC).toEpochMilli() - file.lastModified()
                ageMs <= RETENTION_DAYS * 24L * 60L * 60L * 1000L
            }
            if (!keep && file.delete()) {
                snapshotStateByPath.remove(file.absolutePath)
                fullSnapshotIndexByPath.remove(file.absolutePath)
                LogBuffer.add('I', TAG, "LOCAL_SNAPSHOT_PRUNE: removed=${file.name}")
            }
        }
    }

    private fun rowBytes(json: String): Long {
        return json.toByteArray(Charsets.UTF_8).size.toLong() + 1L
    }

    private fun trimToRecentLimit(rows: LinkedHashMap<String, String>, totalBytesRef: LongArray): Boolean {
        var trimmed = false
        while (rows.size > MAX_ROWS_PER_SESSION) {
            val oldest = rows.entries.firstOrNull() ?: break
            rows.remove(oldest.key)
            totalBytesRef[0] -= rowBytes(oldest.value)
            if (totalBytesRef[0] < 0L) totalBytesRef[0] = 0L
            trimmed = true
        }
        return trimmed
    }

    private fun trimToLimit(
        rows: LinkedHashMap<String, String>,
        totalBytesRef: LongArray,
        maxRows: Int,
        maxBytes: Long
    ): Boolean {
        var trimmed = false
        while (rows.size > maxRows) {
            val oldest = rows.entries.firstOrNull() ?: break
            rows.remove(oldest.key)
            totalBytesRef[0] -= rowBytes(oldest.value)
            if (totalBytesRef[0] < 0L) totalBytesRef[0] = 0L
            trimmed = true
        }
        while (rows.isNotEmpty() && totalBytesRef[0] > maxBytes) {
            val oldest = rows.entries.firstOrNull() ?: break
            rows.remove(oldest.key)
            totalBytesRef[0] -= rowBytes(oldest.value)
            if (totalBytesRef[0] < 0L) totalBytesRef[0] = 0L
            trimmed = true
        }
        return trimmed
    }

    private fun trimToByteLimit(rows: LinkedHashMap<String, String>, totalBytesRef: LongArray): Boolean {
        var trimmed = false
        while (rows.isNotEmpty() && totalBytesRef[0] > MAX_BYTES_PER_SESSION) {
            val oldest = rows.entries.firstOrNull() ?: break
            rows.remove(oldest.key)
            totalBytesRef[0] -= rowBytes(oldest.value)
            if (totalBytesRef[0] < 0L) totalBytesRef[0] = 0L
            trimmed = true
        }
        return trimmed
    }

    private fun rewriteCanonicalFile(file: File, rows: LinkedHashMap<String, String>) {
        if (rows.isEmpty()) {
            if (file.exists()) file.writeText("")
            return
        }
        val tmp = File(file.parentFile, "${file.name}.tmp")
        tmp.bufferedWriter(Charsets.UTF_8).use { writer ->
            rows.values.forEach { row ->
                writer.write(row)
                writer.newLine()
            }
        }
        if (!tmp.renameTo(file)) {
            tmp.copyTo(file, overwrite = true)
            tmp.delete()
        }
    }

    private fun loadSnapshotState(file: File): SnapshotFileState {
        snapshotStateByPath[file.absolutePath]?.let { return it }

        val rows = linkedMapOf<String, String>()
        val totalBytesRef = longArrayOf(0L)
        var needsRewrite = false
        if (file.exists()) {
            file.forEachLine { line ->
                val trimmed = line.trim()
                if (trimmed.isBlank()) {
                    needsRewrite = true
                    return@forEachLine
                }
                val row = try { JSONObject(trimmed) } catch (_: Exception) {
                    needsRewrite = true
                    return@forEachLine
                }
                val key = snapshotKey(row)
                if (rows.putIfAbsent(key, row.toString()) != null) {
                    needsRewrite = true
                } else {
                    totalBytesRef[0] += rowBytes(row.toString())
                }
            }
        }
        if (trimToRecentLimit(rows, totalBytesRef)) {
            needsRewrite = true
        }
        if (trimToByteLimit(rows, totalBytesRef)) {
            needsRewrite = true
        }
        val state = SnapshotFileState(rows, totalBytesRef[0], needsRewrite)
        snapshotStateByPath[file.absolutePath] = state
        return state
    }

    private fun loadFullSnapshotIndex(file: File): FullSnapshotIndex {
        fullSnapshotIndexByPath[file.absolutePath]?.let { return it }
        val rows = linkedMapOf<String, Long>()
        var totalBytes = 0L
        var needsRewrite = false
        if (file.exists()) {
            file.forEachLine { line ->
                val trimmed = line.trim()
                if (trimmed.isBlank()) {
                    needsRewrite = true
                    return@forEachLine
                }
                val row = try { JSONObject(trimmed) } catch (_: Exception) {
                    needsRewrite = true
                    return@forEachLine
                }
                val key = snapshotKey(row)
                val bytes = rowBytes(trimmed)
                if (rows.putIfAbsent(key, bytes) != null) {
                    needsRewrite = true
                } else {
                    totalBytes += bytes
                }
            }
        }
        val index = FullSnapshotIndex(rows, totalBytes, needsRewrite)
        fullSnapshotIndexByPath[file.absolutePath] = index
        return index
    }

    private fun trimFullSnapshotIndex(index: FullSnapshotIndex): Boolean {
        var trimmed = false
        while (index.rows.size > MAX_ROWS_PER_SESSION || index.totalBytes > MAX_BYTES_PER_SESSION) {
            val oldest = index.rows.entries.firstOrNull() ?: break
            index.rows.remove(oldest.key)
            index.totalBytes = (index.totalBytes - oldest.value).coerceAtLeast(0L)
            trimmed = true
        }
        return trimmed
    }

    private fun rewriteFullSnapshotFile(file: File, keepKeys: Set<String>) {
        val tmp = File(file.parentFile, "${file.name}.tmp")
        val written = mutableSetOf<String>()
        tmp.bufferedWriter(Charsets.UTF_8).use { writer ->
            if (file.exists()) {
                file.forEachLine { line ->
                    val trimmed = line.trim()
                    if (trimmed.isBlank()) return@forEachLine
                    val row = try { JSONObject(trimmed) } catch (_: Exception) { return@forEachLine }
                    val key = snapshotKey(row)
                    if (key in keepKeys && written.add(key)) {
                        writer.write(trimmed)
                        writer.newLine()
                    }
                }
            }
        }
        if (!tmp.renameTo(file)) {
            tmp.copyTo(file, overwrite = true)
            tmp.delete()
        }
    }

    private fun parseJsonObject(raw: Any?): JSONObject? {
        return when (raw) {
            is JSONObject -> raw
            is String -> {
                val trimmed = raw.trim()
                if (!trimmed.startsWith("{")) null else try { JSONObject(trimmed) } catch (_: Exception) { null }
            }
            else -> null
        }
    }

    private fun parseJsonArray(raw: Any?): JSONArray? {
        return when (raw) {
            is JSONArray -> raw
            is String -> {
                val trimmed = raw.trim()
                if (!trimmed.startsWith("[")) null else try { JSONArray(trimmed) } catch (_: Exception) { null }
            }
            else -> null
        }
    }

    private fun compactObject(raw: Any?, keys: Array<String>): JSONObject? {
        val src = parseJsonObject(raw) ?: return null
        val out = JSONObject()
        for (key in keys) {
            val value = src.opt(key)
            if (value != null && value != JSONObject.NULL) out.put(key, value)
        }
        return if (out.length() > 0) out else null
    }

    private fun compactLegs(raw: Any?): JSONArray {
        val source = parseJsonArray(raw) ?: return JSONArray()
        val keys = arrayOf(
            "action", "option_type", "strike", "ltp", "bid", "ask",
            "expiry", "instrument_key", "leg_schema_version"
        )
        val out = JSONArray()
        for (i in 0 until source.length()) {
            compactObject(source.opt(i), keys)?.let(out::put)
        }
        return out
    }

    private fun compactPc2GateBasis(raw: Any?): JSONArray {
        val source = parseJsonArray(raw) ?: return JSONArray()
        val keys = arrayOf(
            "gate_name", "gate_field", "gate_basis", "passed",
            "live_percentile_authority", "pct_target", "slice_key",
            "basis_support_count", "basis_stability_ratio", "basis_stability_bar",
            "basis_stability_pass", "observed_value", "threshold_value", "margin", "margin_pct"
        )
        val out = JSONArray()
        for (i in 0 until minOf(source.length(), 12)) {
            compactObject(source.opt(i), keys)?.let(out::put)
        }
        return out
    }

    private fun compactCandidate(raw: Any?): JSONObject? {
        val src = parseJsonObject(raw) ?: return null
        val out = JSONObject()
        val keys = arrayOf(
            "candidate_id",
            "id",
            "type",
            "strategy",
            "strategy_type",
            "index",
            "index_key",
            "mode",
            "trade_mode",
            "lane",
            "role",
            "expiry",
            "tDTE",
            "dte",
            "legCount",
            "leg_schema_version",
            "candidate_schema_version",
            "poll_ts",
            "score",
            "probability",
            "probProfit",
            "prob_source",
            "probSource",
            "prob_status",
            "trueProb",
            "expected_r",
            "ev",
            "premiumEdge",
            "riskReward",
            "build3ExpectedWin",
            "build3ExpectedLoss",
            "build3EvFloor",
            "build3EvFloorMult",
            "build3EvPass",
            "marketConfidence",
            "entryConfidence",
            "entryEligible",
            "entryGate",
            "executionReady",
            "executionGate",
            "entryAction",
            "directionSafe",
            "brainScore",
            "contextPercentileScore",
            "contextPercentileRawScore",
            "contextPercentileSchemaVersion",
            "contextPercentileRecordingVersion",
            "contextPercentileLiveRanking",
            "p_ml",
            "mlAction",
            "mlEdge",
            "mlOod",
            "mlOodFlag",
            "mlOodConf",
            "mlOodBlocked",
            "mlRegime",
            "mlUnsure",
            "pc2PaperRank",
            "pc2PaperResearchRank",
            "pc2PaperPrimaryEligible",
            "pc2PaperSelectorVersion",
            "pc2PaperMode",
            "pc2PaperSortComponents",
            "pc2SupplyWidthSource",
            "pc2SupplyWidthExpanded",
            "pc2SupplyLadderVersion",
            "pc2BatchFCandleScore",
            "pc2BatchFCandleComponents",
            "pc2BatchFCandleExcludedPatterns",
            "pc2BatchFCandleScoringMethod",
            "teacher_shadow_rank",
            "stage2a_live_rank",
            "teacher_bucket_key",
            "teacher_bucket_n",
            "teacher_r_score",
            "teacher_success_rate_pct",
            "teacher_coverage",
            "teacher_recommendable",
            "netPremium",
            "entry_credit",
            "estCost",
            "max_profit",
            "maxProfit",
            "max_loss",
            "maxLoss",
            "width",
            "sell_strike",
            "sellStrike",
            "buy_strike",
            "buyStrike",
            "sell_strike2",
            "sellStrike2",
            "buy_strike2",
            "buyStrike2",
            "sell_type",
            "sellType",
            "buy_type",
            "buyType",
            "sell_type2",
            "sellType2",
            "buy_type2",
            "buyType2",
            "sellLTP",
            "buyLTP",
            "sellLTP2",
            "buyLTP2",
            "isCredit",
            "is_credit",
            "lotSize",
            "sigmaOTM",
            "ivRichness",
            "creditWidthRatio",
            "rank",
            "deterministic_rank",
            "varsityTier",
            "targetProfit",
            "stopLoss",
            "marginRequired",
            "marginForSizing",
            "marginSource",
            "marginFallbackUsed",
            "marginFallbackValue",
            "marginFallbackReason",
            "marginModelVersion",
            "brainMaxLoss",
            "marginSizingBehavior",
            "marginQuoteStatus",
            "marginQuoteSource",
            "marginQuotedAt",
            "realMargin",
            "upstoxRequiredMargin",
            "upstoxFinalMargin",
            "upstoxSpanMargin",
            "upstoxExposureMargin",
            "upstoxNetBuyPremium",
            "rejection_stage",
            "rejection_reason",
            "reason_code",
            "reject_reason",
            "gate_name",
            "gate_field",
            "gate_basis",
            "gate_basis_summary",
            "pct_target",
            "slice_key",
            "basis_support_count",
            "basis_stability_ratio",
            "basis_stability_bar",
            "basis_stability_pass",
            "counterfactual_basis",
            "observed_value",
            "threshold_value",
            "margin",
            "margin_pct",
            "marginRequestUrl",
            "marginQuoteError",
            "expected_win",
            "expected_loss",
            "ev_floor",
            "ev_ratio"
        )
        for (key in keys) {
            val value = src.opt(key)
            if (value != null && value != JSONObject.NULL) out.put(key, value)
        }
        val compactLegs = compactLegs(src.opt("legs"))
        if (compactLegs.length() > 0) out.put("legs", compactLegs)
        val compactGateBasis = compactPc2GateBasis(src.opt("pc2_gate_basis"))
        if (compactGateBasis.length() > 0) out.put("pc2_gate_basis", compactGateBasis)
        compactObject(
            src.opt("generationQualityShadow"),
            arrayOf("quality_flags", "would_suppress", "credit_to_risk", "premium_edge")
        )?.let { out.put("generationQualityShadow", it) }
        compactObject(
            src.opt("pc2CompositeShadow"),
            arrayOf("score", "raw_score", "context_score", "economics_percentile", "teacher_modifier", "version")
        )?.let { out.put("pc2CompositeShadow", it) }
        return if (out.length() > 0) out else null
    }

    private fun compactCandidates(raw: Any?, limit: Int = Int.MAX_VALUE): JSONArray {
        val source = parseJsonArray(raw) ?: return JSONArray()
        val out = JSONArray()
        for (i in 0 until minOf(source.length(), limit)) {
            compactCandidate(source.opt(i))?.let(out::put)
        }
        return out
    }

    private fun compactSummaryCandidate(raw: Any?): JSONObject? {
        val src = parseJsonObject(raw) ?: return null
        val out = JSONObject()
        val keys = arrayOf(
            "candidate_id",
            "id",
            "type",
            "strategy",
            "strategy_type",
            "index",
            "index_key",
            "mode",
            "trade_mode",
            "lane",
            "expiry",
            "tDTE",
            "legCount",
            "legs",
            "leg_schema_version",
            "candidate_schema_version",
            "rank",
            "pc2PaperRank",
            "pc2PaperResearchRank",
            "entryEligible",
            "entryConfidence",
            "entryGate",
            "premiumEdge",
            "expected_r",
            "ev",
            "probProfit",
            "netPremium",
            "maxProfit",
            "maxLoss",
            "width",
            "sellStrike",
            "buyStrike",
            "sellStrike2",
            "buyStrike2",
            "sellType",
            "buyType",
            "sellType2",
            "buyType2",
            "sellLTP",
            "buyLTP",
            "sellLTP2",
            "buyLTP2",
            "isCredit",
            "is_credit",
            "lotSize"
        )
        for (key in keys) {
            val value = src.opt(key)
            if (value != null && value != JSONObject.NULL) out.put(key, value)
        }
        return if (out.length() > 0) out else null
    }

    private fun compactSummaryCandidates(raw: Any?, limit: Int): JSONArray {
        val source = parseJsonArray(raw) ?: return JSONArray()
        val out = JSONArray()
        for (i in 0 until minOf(source.length(), limit)) {
            compactSummaryCandidate(source.opt(i))?.let(out::put)
        }
        return out
    }

    private fun compactBrainSnapshot(snapshot: JSONObject): JSONObject {
        val compact = JSONObject()
        val scalarKeys = arrayOf(
            "recommendation_id",
            "session_date",
            "poll_ts",
            "action",
            "strategy",
            "confidence",
            "is_labelable",
            "b1a_rv_status",
            "b1a_bnf_rv_to_iv_daily_ratio",
            "b1a_nf_rv_to_iv_daily_ratio"
        )
        for (key in scalarKeys) {
            val value = snapshot.opt(key)
            if (value != null && value != JSONObject.NULL) compact.put(key, value)
        }

        compactCandidate(snapshot.opt("primary_candidate_json"))?.let {
            compact.put("primary_candidate_json", it)
        }
        arrayOf(
            "verdict_json",
            "market_forces_json",
            "poll_summary_json",
            "b1a_intraday_rv_json"
        ).forEach { key ->
            parseJsonObject(snapshot.opt(key))?.let { compact.put(key, it) }
        }

        val context = parseJsonObject(snapshot.opt("context_json")) ?: JSONObject()
        val generated = parseJsonArray(context.opt("snapshot_generated_candidates"))
            ?: parseJsonArray(snapshot.opt("top_candidates_json"))
            ?: JSONArray()
        val rankedFull = parseJsonArray(context.opt("snapshot_ranked_candidates_full"))
        val rejectedFull = parseJsonArray(context.opt("snapshot_rejected_candidates_full"))
        val rejected = rejectedFull
            ?: parseJsonArray(context.opt("snapshot_rejected_candidates"))

        val compactContext = JSONObject()
        val contextKeys = arrayOf(
            "vix",
            "bnfSpot",
            "nfSpot",
            "significant_move",
            "bias_net",
            "morningBias",
            "snapshot_generation_skip_reason",
            "snapshot_supply_state"
        )
        for (key in contextKeys) {
            val value = context.opt(key)
            if (value != null && value != JSONObject.NULL) compactContext.put(key, value)
        }

        parseJsonArray(context.opt("snapshot_generation_skip_reasons"))?.let {
            if (it.length() > 0) compactContext.put("snapshot_generation_skip_reasons", it)
        }
        parseJsonObject(context.opt("snapshot_rejected_candidate_stats"))?.let {
            compactContext.put("snapshot_rejected_candidate_stats", it)
        }
        parseJsonObject(context.opt("snapshot_rejected_candidate_selection"))?.let {
            compactContext.put("snapshot_rejected_candidate_selection", it)
        }
        arrayOf(
            "effective_bias",
            "snapshot_phase3_expected_r_shadow",
            "snapshot_phase4_ev_ladder_shadow",
            "snapshot_phase5_gate_registry",
            "snapshot_c3_const_inventory",
            "snapshot_pc2_parameter_authority",
            "snapshot_pc2_batch_a_width_wall",
            "snapshot_pc2_batch_b_regime_sigma",
            "snapshot_pc2_vix_regime_context",
            "snapshot_pc2_batch_c_cross_market",
            "snapshot_pc2_batch_d_exit_policy",
            "snapshot_pc2_batch_e_alert_timing",
            "snapshot_pc2_batch_f_supply_pattern",
            "snapshot_shadow_selector_suite",
            "snapshot_menu_abstention_shadow",
            "snapshot_brain_notification",
            "snapshot_latest_poll",
            "snapshot_android_compaction",
            "signal_independence",
            "candidate_generation_trace",
            "context_percentiles"
        ).forEach { key ->
            parseJsonObject(context.opt(key))?.let { compactContext.put(key, it) }
        }
        parseJsonObject(context.opt("snapshot_build3_gate"))?.let {
            compactContext.put("snapshot_build3_gate", it)
        }
        parseJsonObject(context.opt("snapshot_build3_lane_gate"))?.let {
            compactContext.put("snapshot_build3_lane_gate", it)
        }
        parseJsonObject(context.opt("snapshot_build3_flow"))?.let {
            compactContext.put("snapshot_build3_flow", it)
        }
        parseJsonObject(context.opt("snapshot_pc2_paper_primary"))?.let {
            compactContext.put("snapshot_pc2_paper_primary", it)
        }
        parseJsonObject(context.opt("snapshot_pc2_composite_shadow"))?.let {
            compactContext.put("snapshot_pc2_composite_shadow", it)
        }
        parseJsonObject(context.opt("snapshot_pc2_supply_quality_shadow"))?.let {
            compactContext.put("snapshot_pc2_supply_quality_shadow", it)
        }
        parseJsonObject(context.opt("snapshot_pc2_batch_f_paper_context"))?.let {
            compactContext.put("snapshot_pc2_batch_f_paper_context", it)
        }
        parseJsonObject(context.opt("snapshot_pc2_authority_policy"))?.let {
            compactContext.put("snapshot_pc2_authority_policy", it)
        }
        parseJsonArray(context.opt("snapshot_pc2_authority_decisions"))?.let {
            if (it.length() > 0) compactContext.put("snapshot_pc2_authority_decisions", it)
        }
        arrayOf(
            "snapshot_supply_states",
            "snapshot_evaluation_legs"
        ).forEach { key ->
            parseJsonArray(context.opt(key))?.let {
                if (it.length() > 0) compactContext.put(key, it)
            }
        }
        val snapshotBrainVersion = context.opt("snapshot_brain_version")
        if (snapshotBrainVersion != null && snapshotBrainVersion != JSONObject.NULL) {
            compactContext.put("snapshot_brain_version", snapshotBrainVersion)
        }
        // This small, immutable frame is the post-close C3 source of truth.
        // Keep it even when the full context is compacted for local fallback.
        parseJsonObject(context.opt("c3_finalization_frame"))?.let {
            compactContext.put("c3_finalization_frame", it)
        }

        val gap = parseJsonObject(context.opt("gap"))
        if (gap != null) {
            val gapType = gap.opt("type")
            if (gapType != null && gapType != JSONObject.NULL) {
                compactContext.put("gap", JSONObject().put("type", gapType))
            }
        }

        val compactGenerated = compactCandidates(generated)
        val compactRankedFull = compactCandidates(rankedFull)
        val compactRejected = compactCandidates(rejected)

        compactContext.put("snapshot_generated_candidates", compactGenerated)
        if (compactRankedFull.length() > 0) {
            compactContext.put("snapshot_ranked_candidates_full", compactRankedFull)
        }
        if (compactRejected.length() > 0) {
            compactContext.put(
                if (rejectedFull != null) "snapshot_rejected_candidates_full"
                else "snapshot_rejected_candidates",
                compactRejected
            )
        }

        compact.put("context_json", compactContext)
        compact.put("top_candidates_json", compactGenerated)
        return enforceSnapshotBudget(compact)
    }

    private fun enforceSnapshotBudget(snapshot: JSONObject): JSONObject {
        var bytes = snapshot.toString().toByteArray(Charsets.UTF_8).size.toLong()
        if (bytes <= MAX_COMPACT_SNAPSHOT_BYTES) return snapshot
        val context = snapshot.optJSONObject("context_json") ?: return snapshot
        val removable = arrayOf(
            "snapshot_phase3_expected_r_shadow",
            "snapshot_phase4_ev_ladder_shadow",
            "snapshot_phase5_gate_registry",
            "snapshot_shadow_selector_suite",
            "snapshot_menu_abstention_shadow",
            "snapshot_pc2_supply_quality_shadow",
            "snapshot_pc2_composite_shadow",
            "snapshot_evaluation_legs",
            "snapshot_pc2_authority_decisions"
        )
        val removed = mutableListOf<String>()
        for (key in removable) {
            if (!context.has(key)) continue
            context.remove(key)
            removed.add(key)
            bytes = snapshot.toString().toByteArray(Charsets.UTF_8).size.toLong()
            if (bytes <= MAX_COMPACT_SNAPSHOT_BYTES) break
        }
        LogBuffer.add(
            if (bytes <= MAX_COMPACT_SNAPSHOT_BYTES) 'W' else 'E',
            TAG,
            "LOCAL_SNAPSHOT_BUDGET: bytes=$bytes cap=$MAX_COMPACT_SNAPSHOT_BYTES removed=${removed.joinToString(",")}"
        )
        return snapshot
    }

    fun compactBrainSnapshotForPersistence(snapshot: JSONObject): JSONObject {
        return compactBrainSnapshot(snapshot)
    }

    @Synchronized
    fun releaseMemory() {
        snapshotStateByPath.clear()
        fullSnapshotIndexByPath.clear()
        LogBuffer.add('I', TAG, "LOCAL_SNAPSHOT_MEMORY_RELEASED")
    }

    private fun compactSnapshotSummary(snapshot: JSONObject): JSONObject {
        val compact = JSONObject()
        val scalarKeys = arrayOf(
            "id",
            "session_date",
            "poll_ts",
            "action",
            "strategy",
            "confidence",
            "is_labelable",
            "brain_version",
            "app_version",
            "b1a_rv_status",
            "b1a_bnf_rv_to_iv_daily_ratio",
            "b1a_nf_rv_to_iv_daily_ratio"
        )
        for (key in scalarKeys) {
            val value = snapshot.opt(key)
            if (value != null && value != JSONObject.NULL) compact.put(key, value)
        }
        compactCandidate(snapshot.opt("primary_candidate_json"))?.let {
            compact.put("primary_candidate_json", it)
        }
        arrayOf(
            "verdict_json",
            "market_forces_json",
            "poll_summary_json",
            "b1a_intraday_rv_json"
        ).forEach { key ->
            parseJsonObject(snapshot.opt(key))?.let { compact.put(key, it) }
        }

        val context = parseJsonObject(snapshot.opt("context_json")) ?: JSONObject()
        val generated = parseJsonArray(context.opt("snapshot_generated_candidates"))
            ?: parseJsonArray(snapshot.opt("top_candidates_json"))
            ?: JSONArray()
        val rankedFull = parseJsonArray(context.opt("snapshot_ranked_candidates_full"))
        val compactGenerated = compactSummaryCandidates(generated, 5)
        val compactRankedFull = compactSummaryCandidates(rankedFull, 12)
        val compactContext = JSONObject()
        val contextKeys = arrayOf(
            "vix",
            "bnfSpot",
            "nfSpot",
            "significant_move",
            "snapshot_generation_skip_reason"
        )
        for (key in contextKeys) {
            val value = context.opt(key)
            if (value != null && value != JSONObject.NULL) compactContext.put(key, value)
        }
        parseJsonArray(context.opt("snapshot_generation_skip_reasons"))?.let {
            if (it.length() > 0) compactContext.put("snapshot_generation_skip_reasons", it)
        }
        parseJsonObject(context.opt("snapshot_rejected_candidate_stats"))?.let {
            compactContext.put("snapshot_rejected_candidate_stats", it)
        }
        parseJsonObject(context.opt("snapshot_build3_gate"))?.let {
            compactContext.put("snapshot_build3_gate", it)
        }
        parseJsonObject(context.opt("snapshot_build3_lane_gate"))?.let {
            compactContext.put("snapshot_build3_lane_gate", it)
        }
        parseJsonObject(context.opt("snapshot_build3_flow"))?.let {
            compactContext.put("snapshot_build3_flow", it)
        }
        parseJsonObject(context.opt("snapshot_pc2_paper_primary"))?.let {
            compactContext.put("snapshot_pc2_paper_primary", it)
        }
        parseJsonObject(context.opt("snapshot_pc2_composite_shadow"))?.let {
            compactContext.put("snapshot_pc2_composite_shadow", it)
        }
        parseJsonObject(context.opt("snapshot_pc2_supply_quality_shadow"))?.let {
            compactContext.put("snapshot_pc2_supply_quality_shadow", it)
        }
        parseJsonObject(context.opt("c3_finalization_frame"))?.let {
            compactContext.put("c3_finalization_frame", it)
        }
        compactContext.put("snapshot_generated_candidates", compactGenerated)
        if (compactRankedFull.length() > 0) {
            compactContext.put("snapshot_ranked_candidates_full", compactRankedFull)
        }
        compact.put("context_json", compactContext)
        compact.put("top_candidates_json", compactGenerated)
        return compact
    }

    private fun appendSnapshotSummary(context: Context, sessionDate: String, snapshot: JSONObject): Boolean {
        val file = brainSnapshotSummaryFile(context, sessionDate)
        val state = loadSnapshotState(file)
        val summary = compactSnapshotSummary(snapshot)
        val key = snapshotKey(summary)
        val json = summary.toString()
        val previous = state.rows.put(key, json)
        if (previous != null) {
            state.totalBytes -= rowBytes(previous)
            if (state.totalBytes < 0L) state.totalBytes = 0L
        }
        state.totalBytes += rowBytes(json)
        val byteCounterRef = longArrayOf(state.totalBytes)
        val trimmed = trimToLimit(
            state.rows,
            byteCounterRef,
            MAX_SUMMARY_ROWS_PER_SESSION,
            MAX_SUMMARY_BYTES_PER_SESSION
        )
        state.totalBytes = byteCounterRef[0]
        rewriteCanonicalFile(file, state.rows)
        state.needsRewrite = false
        if (trimmed) {
            LogBuffer.add(
                'I',
                TAG,
                "LOCAL_SNAPSHOT_SUMMARY_TRIM: date=$sessionDate rows=${state.rows.size} bytes=${state.totalBytes} rowCap=$MAX_SUMMARY_ROWS_PER_SESSION byteCap=$MAX_SUMMARY_BYTES_PER_SESSION"
            )
        }
        LogBuffer.add('D', TAG, "LOCAL_SNAPSHOT_SUMMARY_APPEND: date=$sessionDate bytes=${file.length()}")
        return true
    }

    @Synchronized
    fun appendBrainSnapshot(context: Context, sessionDate: String, snapshot: JSONObject): Boolean {
        return try {
            pruneExpiredCacheFiles(context)
            val file = brainSnapshotFile(context, sessionDate)
            val index = loadFullSnapshotIndex(file)
            val compactSnapshot = compactBrainSnapshot(snapshot)
            val key = snapshotKey(compactSnapshot)
            if (index.rows.containsKey(key)) {
                LogBuffer.add('I', TAG, "LOCAL_SNAPSHOT_SKIP_DUP: date=$sessionDate key=$key")
                return true
            }
            val json = compactSnapshot.toString()
            val compactBytes = rowBytes(json)
            file.appendText(json + "\n")
            index.rows[key] = compactBytes
            index.totalBytes += compactBytes
            val trimmed = trimFullSnapshotIndex(index)
            if (index.needsRewrite || trimmed) {
                rewriteFullSnapshotFile(file, index.rows.keys)
                index.needsRewrite = false
                if (trimmed) {
                    LogBuffer.add(
                        'I',
                        TAG,
                        "LOCAL_SNAPSHOT_TRIM: date=$sessionDate rows=${index.rows.size} bytes=${index.totalBytes} rowCap=$MAX_ROWS_PER_SESSION byteCap=$MAX_BYTES_PER_SESSION"
                    )
                } else {
                    LogBuffer.add('I', TAG, "LOCAL_SNAPSHOT_COMPACT_ONCE: file=${file.name} rows=${index.rows.size}")
                }
            }
            LogBuffer.add(
                'I',
                TAG,
                "LOCAL_SNAPSHOT_COMPACTED: date=$sessionDate compactBytes=$compactBytes"
            )
            LogBuffer.add('D', TAG, "LOCAL_SNAPSHOT_APPEND: date=$sessionDate bytes=${file.length()}")
            try {
                appendSnapshotSummary(context, sessionDate, compactSnapshot)
            } catch (summaryError: Throwable) {
                LogBuffer.add('W', TAG, "LOCAL_SNAPSHOT_SUMMARY_APPEND_FAIL: date=$sessionDate error=${summaryError.message}")
            }
            true
        } catch (e: Throwable) {
            LogBuffer.add('W', TAG, "LOCAL_SNAPSHOT_APPEND_FAIL: date=$sessionDate error=${e.message}")
            false
        }
    }

    @Synchronized
    fun readBrainSnapshots(context: Context, sessionDate: String): JSONArray {
        val out = JSONArray()
        val seen = linkedSetOf<String>()

        try {
            pruneExpiredCacheFiles(context)
            val file = brainSnapshotFile(context, sessionDate)
            if (!file.exists()) return out
            file.forEachLine { line ->
                val row = try { JSONObject(line) } catch (_: Exception) { return@forEachLine }
                val key = snapshotKey(row)
                if (key.isBlank() || seen.add(key)) out.put(row)
            }
            LogBuffer.add('I', TAG, "LOCAL_SNAPSHOT_READ: date=$sessionDate rows=${out.length()} bytes=${file.length()}")
        } catch (e: Throwable) {
            LogBuffer.add('W', TAG, "LOCAL_SNAPSHOT_READ_FAIL: date=$sessionDate error=${e.message}")
        }
        return out
    }

    @Synchronized
    fun forEachBrainSnapshot(
        context: Context,
        sessionDate: String,
        onRow: (JSONObject) -> Unit
    ): Int {
        val seen = linkedSetOf<String>()
        var count = 0
        try {
            pruneExpiredCacheFiles(context)
            val file = brainSnapshotFile(context, sessionDate)
            if (!file.exists()) return 0
            file.forEachLine { line ->
                val row = try { JSONObject(line) } catch (_: Exception) { return@forEachLine }
                val key = snapshotKey(row)
                if (key.isNotBlank() && !seen.add(key)) return@forEachLine
                onRow(row)
                count += 1
            }
            LogBuffer.add('I', TAG, "LOCAL_SNAPSHOT_STREAM: date=$sessionDate rows=$count bytes=${file.length()}")
        } catch (e: Throwable) {
            LogBuffer.add('W', TAG, "LOCAL_SNAPSHOT_STREAM_FAIL: date=$sessionDate rows=$count error=${e.message}")
        }
        return count
    }

    @Synchronized
    fun streamBrainSnapshotsToJsonArrayFile(
        context: Context,
        sessionDate: String,
        target: File,
        onRow: (JSONObject) -> Unit
    ): Int {
        target.parentFile?.mkdirs()
        val temp = File("${target.absolutePath}.local.tmp")
        temp.delete()
        var count = 0
        try {
            temp.bufferedWriter().use { writer ->
                writer.write("[")
                count = forEachBrainSnapshot(context, sessionDate) { row ->
                    onRow(row)
                    if (count > 0) writer.write(",")
                    writer.write(row.toString())
                    count += 1
                }
                writer.write("]")
            }
            if (count == 0) {
                temp.delete()
                target.delete()
                return 0
            }
            if (target.exists() && !target.delete()) {
                throw IllegalStateException("Unable to replace ${target.name}")
            }
            if (!temp.renameTo(target)) {
                temp.copyTo(target, overwrite = true)
                temp.delete()
            }
            LogBuffer.add('I', TAG, "LOCAL_SNAPSHOT_STREAM_WRITE: date=$sessionDate rows=$count bytes=${target.length()}")
        } catch (e: Throwable) {
            temp.delete()
            target.delete()
            LogBuffer.add('W', TAG, "LOCAL_SNAPSHOT_STREAM_WRITE_FAIL: date=$sessionDate rows=$count error=${e.message}")
            return 0
        }
        return count
    }

    @Synchronized
    fun readRecentBrainSnapshotSummaries(
        context: Context,
        sessionDate: String,
        limit: Int,
        maxBytes: Long
    ): JSONArray {
        val out = JSONArray()
        val safeLimit = limit.coerceAtLeast(1)
        val safeMaxBytes = maxBytes.coerceAtLeast(1024L)
        val cappedRows = linkedMapOf<String, String>()
        var cappedBytes = 0L

        try {
            pruneExpiredCacheFiles(context)
            val file = brainSnapshotSummaryFile(context, sessionDate)
            if (!file.exists()) return out

            readRecentLinesFromEnd(file, safeLimit, safeMaxBytes).forEach { line ->
                val trimmed = line.trim()
                if (trimmed.isBlank()) return@forEach
                val row = try { JSONObject(trimmed) } catch (_: Exception) { return@forEach }
                val key = snapshotKey(row)
                val json = row.toString()
                val previous = cappedRows.remove(key)
                if (previous != null) {
                    cappedBytes -= rowBytes(previous)
                    if (cappedBytes < 0L) cappedBytes = 0L
                }
                cappedRows[key] = json
                cappedBytes += rowBytes(json)
                while (cappedRows.size > safeLimit || (cappedRows.size > 1 && cappedBytes > safeMaxBytes)) {
                    val oldest = cappedRows.entries.firstOrNull() ?: break
                    cappedRows.remove(oldest.key)
                    cappedBytes -= rowBytes(oldest.value)
                    if (cappedBytes < 0L) cappedBytes = 0L
                }
            }

            cappedRows.values.toList().asReversed().forEach { json ->
                val row = try { JSONObject(json) } catch (_: Exception) { return@forEach }
                out.put(row)
            }
            LogBuffer.add(
                'I',
                TAG,
                "LOCAL_SNAPSHOT_SUMMARY_READ_RECENT: date=$sessionDate rows=${out.length()} bytes=$cappedBytes fileBytes=${file.length()} limit=$safeLimit byteCap=$safeMaxBytes"
            )
        } catch (e: Exception) {
            LogBuffer.add('W', TAG, "LOCAL_SNAPSHOT_SUMMARY_READ_RECENT_FAIL: date=$sessionDate error=${e.message}")
        }
        return out
    }

    @Synchronized
    fun readRecentBrainSnapshots(
        context: Context,
        sessionDate: String,
        limit: Int,
        maxBytes: Long
    ): JSONArray {
        val out = JSONArray()
        val safeLimit = limit.coerceAtLeast(1)
        val safeMaxBytes = maxBytes.coerceAtLeast(1024L)
        val cappedRows = linkedMapOf<String, String>()
        var cappedBytes = 0L

        try {
            pruneExpiredCacheFiles(context)
            val file = brainSnapshotFile(context, sessionDate)
            if (!file.exists()) return out

            readRecentLinesFromEnd(file, safeLimit, safeMaxBytes).forEach { line ->
                val trimmed = line.trim()
                if (trimmed.isBlank()) return@forEach
                val row = try { JSONObject(trimmed) } catch (_: Exception) { return@forEach }
                val key = snapshotKey(row)
                val json = row.toString()
                val previous = cappedRows.remove(key)
                if (previous != null) {
                    cappedBytes -= rowBytes(previous)
                    if (cappedBytes < 0L) cappedBytes = 0L
                }
                cappedRows[key] = json
                cappedBytes += rowBytes(json)

                while (cappedRows.size > safeLimit || (cappedRows.size > 1 && cappedBytes > safeMaxBytes)) {
                    val oldest = cappedRows.entries.firstOrNull() ?: break
                    cappedRows.remove(oldest.key)
                    cappedBytes -= rowBytes(oldest.value)
                    if (cappedBytes < 0L) cappedBytes = 0L
                }
            }

            cappedRows.values.toList().asReversed().forEach { json ->
                val row = try { JSONObject(json) } catch (_: Exception) { return@forEach }
                out.put(row)
            }
            LogBuffer.add(
                'I',
                TAG,
                "LOCAL_SNAPSHOT_READ_RECENT: date=$sessionDate rows=${out.length()} bytes=$cappedBytes fileBytes=${file.length()} limit=$safeLimit byteCap=$safeMaxBytes"
            )
        } catch (e: Exception) {
            LogBuffer.add('W', TAG, "LOCAL_SNAPSHOT_READ_RECENT_FAIL: date=$sessionDate error=${e.message}")
        }
        return out
    }

    private fun readRecentLinesFromEnd(file: File, limit: Int, maxBytes: Long): List<String> {
        val lines = ArrayList<String>()
        if (!file.exists() || file.length() <= 0L) return lines

        RandomAccessFile(file, "r").use { raf ->
            var pointer = raf.length() - 1
            val line = StringBuilder()
            var collectedBytes = 0L

            while (pointer >= 0 && lines.size < limit && (lines.isEmpty() || collectedBytes < maxBytes)) {
                raf.seek(pointer)
                val ch = raf.read().toChar()
                if (ch == '\n') {
                    if (line.isNotEmpty()) {
                        val value = line.reverse().toString()
                        lines.add(value)
                        collectedBytes += rowBytes(value)
                        line.clear()
                    }
                } else {
                    line.append(ch)
                }
                pointer--
            }

            if (line.isNotEmpty() && lines.size < limit && (lines.isEmpty() || collectedBytes < maxBytes)) {
                lines.add(line.reverse().toString())
            }
        }

        return lines
    }

    @Synchronized
    fun appendBuild3AbDecision(context: Context, sessionDate: String, row: JSONObject): Boolean {
        return try {
            pruneExpiredCacheFiles(context)
            val file = build3AbFile(context, sessionDate)
            file.appendText(row.toString() + "\n")
            LogBuffer.add('D', TAG, "LOCAL_BUILD3_AB_APPEND: date=$sessionDate bytes=${file.length()}")
            true
        } catch (e: Exception) {
            LogBuffer.add('W', TAG, "LOCAL_BUILD3_AB_APPEND_FAIL: date=$sessionDate error=${e.message}")
            false
        }
    }
}
