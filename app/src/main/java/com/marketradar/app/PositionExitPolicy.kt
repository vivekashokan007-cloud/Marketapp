package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeParseException
import java.security.MessageDigest
import kotlin.math.abs
import kotlin.math.max

/**
 * G4 executable exit-policy and net-target contract (Kotlin monitor).
 *
 * Mirrors `position_exit_policy.py`. Net-basis alignment IS a new policy
 * version. Live SHADOW_* alerts stay on [PositionPolicyV1] gross constants;
 * this object is the shared net contract used for teacher/monitor conformance
 * and additive traces.
 *
 * Schedule (do not randomly change unrelated windows):
 *   policy exit intent        = 15:15 IST
 *   native poll/session close = 15:40 IST ([MarketWatchService], this service)
 *   Python readiness helper   = 15:30 IST (`check_execution_readiness`)
 */
object PositionExitPolicy {
    const val CONTRACT_VERSION = "position_exit_policy_v1_net_20260912"
    const val NET_TARGET_VERSION = "net_target_v1_gross_minus_costs_once_20260912"
    const val LEGACY_POSITION_POLICY_VERSION = PositionPolicyV1.VERSION
    const val TP_MULT = PositionPolicyV1.TP_MULT
    const val SL_MULT = PositionPolicyV1.SL_MULT
    const val POLICY_EXIT_INTENT_HH_MM = PositionPolicyV1.EOD_HH_MM
    const val POLICY_EXIT_INTENT_MINUTES = 15 * 60 + 15
    const val NATIVE_MARKET_CLOSE_HH_MM = "15:40"
    const val PYTHON_READINESS_CLOSE_HH_MM = "15:30"
    const val PNL_TOLERANCE_RUPEES = 1.0

    private val IST: ZoneId = ZoneId.of("Asia/Kolkata")

    data class Result(
        val entryValid: Boolean,
        val entryReasons: List<String>,
        val exitReason: String,
        val exitTs: String?,
        val netPnl: Double?,
        val learningResultNet: String,
        val learningWonNet: Boolean?,
        val learningFlatNet: Boolean?,
        val tpThreshold: Double?,
        val slThreshold: Double?,
        val precedenceApplied: String?,
        val tpHit: Boolean,
        val replayCertifiedTickEquivalent: Boolean,
        val replayResolution: String,
        val evidenceGrade: String,
        val role: String,
        val inputHash: String,
        val dataCutoffTs: String?,
    ) {
        fun toJson(): JSONObject = JSONObject().apply {
            put("entry_valid", entryValid)
            put("entry_reasons", JSONArray(entryReasons))
            put("exit_reason", exitReason)
            if (exitTs == null) put("exit_ts", JSONObject.NULL) else put("exit_ts", exitTs)
            if (netPnl == null) put("net_pnl", JSONObject.NULL) else put("net_pnl", netPnl)
            put("learning_result_net", learningResultNet)
            if (learningWonNet == null) put("learning_won_net", JSONObject.NULL) else put("learning_won_net", learningWonNet)
            if (learningFlatNet == null) put("learning_flat_net", JSONObject.NULL) else put("learning_flat_net", learningFlatNet)
            if (tpThreshold == null) put("tp_threshold", JSONObject.NULL) else put("tp_threshold", tpThreshold)
            if (slThreshold == null) put("sl_threshold", JSONObject.NULL) else put("sl_threshold", slThreshold)
            if (precedenceApplied == null) put("precedence_applied", JSONObject.NULL) else put("precedence_applied", precedenceApplied)
            put("tp_hit", tpHit)
            put("replay_certified_tick_equivalent", replayCertifiedTickEquivalent)
            put("replay_resolution", replayResolution)
            put("evidence_grade", evidenceGrade)
            put("role", role)
            put("position_exit_policy_version", CONTRACT_VERSION)
            put("net_target_version", NET_TARGET_VERSION)
            put("net_basis_alignment_is_new_policy_version", true)
            put("provenance", JSONObject().apply {
                if (dataCutoffTs == null) put("data_cutoff_ts", JSONObject.NULL) else put("data_cutoff_ts", dataCutoffTs)
                put("input_hash", inputHash)
                put("policy_version", CONTRACT_VERSION)
                put("net_target_version", NET_TARGET_VERSION)
                put("evaluator", "PositionExitPolicy.kt")
            })
        }
    }

    fun evaluateCase(case: JSONObject, role: String = "monitor"): Result {
        val identity = case.optJSONObject("identity") ?: JSONObject()
        val quantity = case.optJSONObject("quantity") ?: JSONObject()
        val entry = case.optJSONObject("entry") ?: JSONObject()
        val events = case.optJSONArray("events") ?: JSONArray()
        val published = case.optJSONObject("published")
        val overnight = case.optBoolean("overnight", false)
        val replay = case.optString("replay_resolution", "TICK")
        val cutoffRaw = if (case.has("data_cutoff_ts") && !case.isNull("data_cutoff_ts")) {
            case.optString("data_cutoff_ts")
        } else null
        return evaluate(
            identity = identity,
            quantity = quantity,
            entry = entry,
            events = events,
            published = published,
            overnight = overnight,
            role = role,
            replayResolution = replay,
            dataCutoffTs = cutoffRaw,
        )
    }

    fun evaluate(
        identity: JSONObject,
        quantity: JSONObject,
        entry: JSONObject,
        events: JSONArray,
        published: JSONObject? = null,
        overnight: Boolean = false,
        role: String = "monitor",
        replayResolution: String = "TICK",
        dataCutoffTs: String? = null,
    ): Result {
        val roleNorm = role.trim().lowercase()
        val evidence = if (roleNorm == "monitor") "OBSERVED_MONITOR" else "COUNTERFACTUAL_TEACHER"
        val cutoff = parseTs(dataCutoffTs)
        val entryCheck = validateEntry(identity, quantity, entry, overnight)
        val thresholds = entryThresholds(entry)
        val composed = composePublished(
            thresholds.tp,
            thresholds.sl,
            published,
        )
        val hash = inputHash(identity, entry, events, published)
        val replayCert = replayResolution.uppercase() == "TICK"

        if (!entryCheck.valid || !thresholds.ok) {
            val reasons = entryCheck.reasons.toMutableList()
            if (!thresholds.ok && "entry_bounds_or_cost_unavailable" !in reasons) {
                reasons.add(thresholds.reason)
            }
            return Result(
                entryValid = false,
                entryReasons = reasons,
                exitReason = "NO_ENTRY",
                exitTs = null,
                netPnl = null,
                learningResultNet = "UNAVAILABLE",
                learningWonNet = null,
                learningFlatNet = null,
                tpThreshold = composed.tp,
                slThreshold = composed.sl,
                precedenceApplied = null,
                tpHit = false,
                replayCertifiedTickEquivalent = replayCert,
                replayResolution = replayResolution,
                evidenceGrade = evidence,
                role = roleNorm,
                inputHash = hash,
                dataCutoffTs = dataCutoffTs,
            )
        }

        var peak: Double? = null
        var lastNet: Double? = null
        var lastTs: String? = null
        var lastGross: Double? = null
        var lastCost: Double? = null
        var exitReason: String? = null
        var exitTs: String? = null
        var exitNet: Double? = null
        var precedence: String? = null

        for (i in 0 until events.length()) {
            val event = events.optJSONObject(i) ?: continue
            if (!eventUsable(event, cutoff)) continue
            val gross = finiteOrNull(event.opt("gross_pnl")) ?: continue
            val cost = finiteOrNull(event.opt("cost")) ?: continue
            val n = round2(gross - cost)
            val ts = event.optString("ts", "")
            lastNet = n
            lastTs = ts
            lastGross = gross
            lastCost = cost
            if (peak == null || n > peak) peak = n

            val slHit = composed.sl != null && n <= composed.sl
            val tpHit = composed.tp != null && n >= composed.tp
            if (slHit && tpHit) {
                exitReason = "SL"; precedence = "SL_BEFORE_TP"
                exitTs = ts; exitNet = n; break
            }
            if (slHit) {
                exitReason = "SL"; precedence = "SL"
                exitTs = ts; exitNet = n; break
            }
            if (tpHit) {
                exitReason = "TP"; precedence = "TP"
                exitTs = ts; exitNet = n; break
            }
            if (!overnight && isAtOrAfterPolicyEod(ts)) {
                exitReason = "EOD"; precedence = "EOD"
                exitTs = ts; exitNet = n; break
            }
        }

        if (exitReason == null) {
            if (lastNet == null) {
                exitReason = "MISSING_QUOTES"
                exitTs = null
                exitNet = null
            } else if (overnight) {
                exitReason = "OVERNIGHT_HOLD"
                exitTs = lastTs
                exitNet = lastNet
            } else {
                exitReason = "EOD"
                exitTs = lastTs
                exitNet = lastNet
            }
        }

        val classified = classifyNet(exitNet)
        return Result(
            entryValid = true,
            entryReasons = emptyList(),
            exitReason = exitReason!!,
            exitTs = exitTs,
            netPnl = classified.net,
            learningResultNet = classified.result,
            learningWonNet = classified.won,
            learningFlatNet = classified.flat,
            tpThreshold = composed.tp,
            slThreshold = composed.sl,
            precedenceApplied = precedence,
            tpHit = exitReason == "TP",
            replayCertifiedTickEquivalent = replayCert,
            replayResolution = replayResolution,
            evidenceGrade = evidence,
            role = roleNorm,
            inputHash = hash,
            dataCutoffTs = dataCutoffTs,
        )
    }

    internal data class EntryCheck(val valid: Boolean, val reasons: List<String>)
    internal data class Thresholds(
        val ok: Boolean,
        val reason: String,
        val tp: Double?,
        val sl: Double?,
    )
    internal data class Composed(val tp: Double?, val sl: Double?)
    internal data class Classified(val result: String, val won: Boolean?, val flat: Boolean?, val net: Double?)

    internal fun validateEntry(
        identity: JSONObject,
        quantity: JSONObject,
        entry: JSONObject,
        overnight: Boolean,
    ): EntryCheck {
        val reasons = mutableListOf<String>()
        val unit = quantity.optString("unit", "INR_TOTAL").trim().uppercase()
        if (unit !in setOf("INR_TOTAL", "TOTAL_CURRENCY")) reasons.add("quantity_unit_not_total_currency")
        val lotSize = finiteOrNull(quantity.opt("lot_size"))
        val lots = finiteOrNull(quantity.opt("lots"))
        if (lotSize == null || lotSize <= 0.0) reasons.add("lot_size_missing_or_non_positive")
        if (lots == null || lots <= 0.0) reasons.add("lots_missing_or_non_positive")
        if (!entry.optBoolean("legs_complete", false)) reasons.add("incomplete_legs")
        val q = entry.optString("quote_quality", "").trim().uppercase()
        if (q !in setOf("OK", "EXECUTABLE")) reasons.add("entry_quote_quality_not_executable")
        if (finiteOrNull(entry.opt("entry_cost_estimate")) == null) reasons.add("entry_cost_estimate_unavailable")
        if (finiteOrNull(entry.opt("gross_max_profit")) == null) reasons.add("gross_max_profit_unavailable")
        val mode = identity.optString(
            "execution_mode",
            identity.optString("trade_mode", "intraday")
        ).trim().lowercase()
        val decisionTs = identity.optString("decision_ts", entry.optString("ts", ""))
        if (mode in setOf("intraday", "paper_intraday") && !overnight && isAtOrAfterPolicyEod(decisionTs)) {
            reasons.add("no_entry_after_eod_intent")
        }
        val age = finiteOrNull(entry.opt("quote_age_ms"))
        if (age != null && age < 0) reasons.add("quote_age_negative")
        val tid = identity.optString("trade_id", identity.optString("candidate_id", "")).trim()
        if (tid.isEmpty()) reasons.add("missing_trade_or_candidate_identity")
        return EntryCheck(reasons.isEmpty(), reasons)
    }

    internal fun entryThresholds(entry: JSONObject): Thresholds {
        val maxProfit = finiteOrNull(entry.opt("gross_max_profit"))
        val maxLoss = finiteOrNull(entry.opt("gross_max_loss"))
        val entryCost = finiteOrNull(entry.opt("entry_cost_estimate"))
        if (maxProfit == null || maxProfit <= 0.0 || entryCost == null) {
            return Thresholds(false, "entry_bounds_or_cost_unavailable", null, null)
        }
        val netMaxProfit = round2(max(maxProfit - entryCost, 0.0))
        val netMaxLoss = if (maxLoss != null && maxLoss > 0.0) round2(maxLoss + entryCost) else null
        val tp = round2(netMaxProfit * TP_MULT)
        val sl = when {
            netMaxLoss != null && netMaxLoss > 0.0 -> round2(-(netMaxLoss * SL_MULT))
            maxLoss != null && maxLoss > 0.0 -> round2(-(maxLoss * SL_MULT))
            else -> null
        }
        return Thresholds(true, "ok", tp, sl)
    }

    internal fun composePublished(constantTp: Double?, constantSl: Double?, published: JSONObject?): Composed {
        val pubTp = if (published == null) null else finiteOrNull(published.opt("target_pnl_at"))
        val pubSl = if (published == null) null else finiteOrNull(published.opt("stop_pnl_at"))
        val tp = listOfNotNull(constantTp, pubTp).minOrNull()
        val sl = listOfNotNull(constantSl, pubSl).maxOrNull()
        return Composed(tp, sl)
    }

    internal fun classifyNet(value: Double?): Classified {
        if (value == null) return Classified("UNAVAILABLE", null, null, null)
        val n = round2(value)
        return when {
            n > 0.0 -> Classified("WIN", true, false, n)
            n == 0.0 -> Classified("FLAT", false, true, n)
            else -> Classified("LOSS", false, false, n)
        }
    }

    internal fun isAtOrAfterPolicyEod(ts: String?): Boolean {
        val mins = istMinutes(ts) ?: return false
        return mins >= POLICY_EXIT_INTENT_MINUTES
    }

    private fun eventUsable(event: JSONObject, cutoff: ZonedDateTime?): Boolean {
        val quality = event.optString("quote_quality", "").trim().uppercase()
        if (quality in setOf("MISSING", "LATE", "NONE", "STALE")) return false
        if (quality.isNotEmpty() && quality !in setOf("OK", "EXECUTABLE", "GAP")) return false
        val ts = parseTs(event.optString("ts", null)) ?: return false
        if (cutoff != null && ts.isAfter(cutoff)) return false
        if (finiteOrNull(event.opt("gross_pnl")) == null) return false
        if (finiteOrNull(event.opt("cost")) == null) return false
        return true
    }

    internal fun finiteOrNull(value: Any?): Double? {
        if (value == null || value == JSONObject.NULL) return null
        val n = when (value) {
            is Number -> value.toDouble()
            is String -> value.toDoubleOrNull()
            else -> return null
        } ?: return null
        return if (n.isFinite()) n else null
    }

    internal fun parseTs(raw: String?): ZonedDateTime? {
        if (raw.isNullOrBlank()) return null
        var text = raw.trim()
        if (text.endsWith("Z")) text = text.dropLast(1) + "+00:00"
        return try {
            OffsetDateTime.parse(text).atZoneSameInstant(IST)
        } catch (_: DateTimeParseException) {
            try {
                ZonedDateTime.parse(text)
            } catch (_: DateTimeParseException) {
                null
            }
        }
    }

    internal fun istMinutes(ts: String?): Int? {
        val z = parseTs(ts) ?: return null
        val local = z.withZoneSameInstant(IST)
        return local.hour * 60 + local.minute
    }

    internal fun round2(value: Double): Double = Math.round(value * 100.0) / 100.0

    private fun inputHash(
        identity: JSONObject,
        entry: JSONObject,
        events: JSONArray,
        published: JSONObject?,
    ): String {
        val payload = JSONObject()
            .put("identity", identity)
            .put("entry", entry)
            .put("events", events)
            .put("published", published ?: JSONObject.NULL)
        val digest = MessageDigest.getInstance("SHA-256").digest(payload.toString().toByteArray())
        return digest.joinToString("") { "%02x".format(it) }
    }

    fun agree(teacher: Result, monitor: Result, tolerance: Double = PNL_TOLERANCE_RUPEES): Boolean {
        if (teacher.entryValid != monitor.entryValid) return false
        if (teacher.exitReason != monitor.exitReason) return false
        if (teacher.exitTs != monitor.exitTs) return false
        if (teacher.learningResultNet != monitor.learningResultNet) return false
        if (teacher.precedenceApplied != monitor.precedenceApplied) return false
        val tn = teacher.netPnl
        val mn = monitor.netPnl
        if (tn == null || mn == null) return tn == mn
        return abs(tn - mn) <= tolerance
    }
}
