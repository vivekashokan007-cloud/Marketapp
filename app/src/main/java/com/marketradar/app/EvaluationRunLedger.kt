package com.marketradar.app

import android.content.Context
import android.content.SharedPreferences
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest

/**
 * G5 durable evening evaluation-run identity + stage tracking.
 *
 * Prefs cache status for the UI, but the local mirror file (and Supabase
 * `ml_evaluation_runs` when available) is the completion record. Mirrors the
 * Python contract in evaluation_run_ledger.py.
 */
object EvaluationRunLedger {
    const val LEDGER_CONTRACT_VERSION = "evaluation_run_ledger_v1_20260912"
    const val EVALUATOR_VERSION = "evening_evaluator_g5_v1_20260912"
    const val DEFAULT_SCOPE = "owner_device_paper"
    const val DEFAULT_POLICY_LABEL_CONTRACT =
        "teacher_v1|tc_2026_07_A|" +
            "position_exit_policy_v1_net_20260912|" +
            "net_target_v1_gross_minus_costs_once_20260912"

    val STAGE_ORDER = listOf(
        "input_coverage",
        "outcome_computation",
        "outcome_persistence",
        "research_aggregation",
        "percentile_finalization",
        "performance_metrics"
    )

    const val REASON_CAPPED_POPULATION = "CAPPED_OR_INCOMPLETE_CANDIDATE_POPULATION"
    const val REASON_NO_FRAMES = "NO_C3_FRAMES"
    const val REASON_TRAINING_FROZEN = "TRAINING_FROZEN_NOT_ATTEMPTED"
    const val REASON_PROMOTION_DISABLED = "PROMOTION_DISABLED_NOT_ATTEMPTED"
    const val REASON_METRICS_DEFERRED_G6 = "PERFORMANCE_METRICS_DEFERRED_TO_G6" // legacy alias
    const val REASON_METRICS_READY_G6 = "PERFORMANCE_METRICS_G6_ENABLED"
    const val REASON_METRICS_WRITTEN = "METRICS_WRITTEN"
    const val REASON_DUPLICATE_LEASE = "ACTIVE_LEASE_HELD"

    private const val MIRROR_DIR = "evaluation_run_ledger"
    private const val PREF_RUN_JSON = "g5_evaluation_run_json"
    private const val PREF_RUN_ID = "g5_evaluation_run_id"
    private const val PREF_LABELS_SAVED = "g5_labels_saved"
    private const val PREF_LEARNING_COMPLETE = "g5_learning_complete"
    private const val LEASE_DEFAULT_MS = 45L * 60L * 1000L

    private val completionOk = setOf("verified", "ineligible", "disabled", "not_attempted")

    private fun sha256Hex(text: String): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(text.toByteArray(Charsets.UTF_8))
        return digest.joinToString("") { "%02x".format(it) }
    }

    fun hashInputManifest(manifest: JSONObject): String =
        sha256Hex(canonicalJson(manifest))

    fun buildRunId(
        sessionDate: String,
        scope: String = DEFAULT_SCOPE,
        policyLabelContract: String = DEFAULT_POLICY_LABEL_CONTRACT,
        inputManifestHash: String,
        evaluatorVersion: String = EVALUATOR_VERSION
    ): String {
        val material = listOf(
            sessionDate.trim(),
            scope.trim(),
            policyLabelContract.trim(),
            inputManifestHash.trim(),
            evaluatorVersion.trim(),
            LEDGER_CONTRACT_VERSION
        ).joinToString("|")
        return "erun_" + sha256Hex(material).take(32)
    }

    private fun canonicalJson(value: Any?): String = when (value) {
        null, JSONObject.NULL -> "null"
        is JSONObject -> value.keys().asSequence().toList().sorted().joinToString(",", "{", "}") {
            JSONObject.quote(it) + ":" + canonicalJson(value.opt(it))
        }
        is JSONArray -> (0 until value.length()).joinToString(",", "[", "]") { canonicalJson(value.opt(it)) }
        is String -> JSONObject.quote(value)
        is Number, is Boolean -> value.toString()
        else -> JSONObject.quote(value.toString())
    }

    private fun emptyStage(name: String, initial: String = "pending"): JSONObject =
        JSONObject()
            .put("name", name)
            .put("state", initial)
            .put("reason_code", "")
            .put("expected_count", 0)
            .put("written_count", 0)
            .put("verified_count", 0)
            .put("nonlabelable_count", 0)
            .put("started_at", JSONObject.NULL)
            .put("ended_at", JSONObject.NULL)
            .put("last_error", "")
            .put("detail", JSONObject())

    fun newRun(
        sessionDate: String,
        inputManifest: JSONObject = JSONObject(),
        scope: String = DEFAULT_SCOPE,
        policyLabelContract: String = DEFAULT_POLICY_LABEL_CONTRACT,
        evaluatorVersion: String = EVALUATOR_VERSION,
        revision: Int = 1
    ): JSONObject {
        val manifestHash = hashInputManifest(inputManifest)
        val runId = buildRunId(sessionDate, scope, policyLabelContract, manifestHash, evaluatorVersion)
        val stages = JSONObject()
        for (name in STAGE_ORDER) stages.put(name, emptyStage(name))
        stages.put(
            "training",
            emptyStage("training", "disabled").put("reason_code", REASON_TRAINING_FROZEN)
        )
        stages.put(
            "promotion",
            emptyStage("promotion", "disabled").put("reason_code", REASON_PROMOTION_DISABLED)
        )
        stages.put(
            "performance_metrics",
            emptyStage("performance_metrics", "pending").put("reason_code", REASON_METRICS_READY_G6)
        )
        val now = java.time.Instant.now().toString()
        return JSONObject()
            .put("run_id", runId)
            .put("revision", revision)
            .put("session_date", sessionDate)
            .put("scope", scope)
            .put("policy_label_contract", policyLabelContract)
            .put("input_manifest_hash", manifestHash)
            .put("input_manifest", inputManifest)
            .put("evaluator_version", evaluatorVersion)
            .put("ledger_contract_version", LEDGER_CONTRACT_VERSION)
            .put("lease_holder", JSONObject.NULL)
            .put("lease_expires_at_ms", 0L)
            .put("created_at", now)
            .put("updated_at", now)
            .put("stages", stages)
            .put("labels_saved", false)
            .put("learning_complete", false)
            .put("active", true)
    }

    fun refreshCompletionFlags(run: JSONObject): JSONObject {
        val stages = run.optJSONObject("stages") ?: JSONObject()
        val persistence = stages.optJSONObject("outcome_persistence") ?: JSONObject()
        val labelsSaved = persistence.optString("state") == "verified"
        run.put("labels_saved", labelsSaved)

        var learningOk = labelsSaved
        for (name in STAGE_ORDER) {
            val st = stages.optJSONObject(name)?.optString("state") ?: "pending"
            if (st !in completionOk) {
                learningOk = false
                break
            }
        }
        for (name in listOf("training", "promotion")) {
            val st = stages.optJSONObject(name)?.optString("state") ?: "pending"
            if (st !in setOf("disabled", "not_attempted")) learningOk = false
        }
        if ((stages.optJSONObject("percentile_finalization")?.optString("state") ?: "") == "failed") {
            learningOk = false
        }
        run.put("learning_complete", learningOk && labelsSaved)
        return run
    }

    fun setStage(
        run: JSONObject,
        name: String,
        state: String,
        reasonCode: String = "",
        expectedCount: Int? = null,
        writtenCount: Int? = null,
        verifiedCount: Int? = null,
        nonlabelableCount: Int? = null,
        lastError: String = "",
        detail: JSONObject? = null
    ): JSONObject {
        val stages = run.optJSONObject("stages") ?: JSONObject().also { run.put("stages", it) }
        val stage = stages.optJSONObject(name) ?: emptyStage(name)
        val now = java.time.Instant.now().toString()
        val prev = stage.optString("state")
        stage.put("state", state)
        if (reasonCode.isNotBlank()) stage.put("reason_code", reasonCode)
        if (expectedCount != null) stage.put("expected_count", expectedCount)
        if (writtenCount != null) stage.put("written_count", writtenCount)
        if (verifiedCount != null) stage.put("verified_count", verifiedCount)
        if (nonlabelableCount != null) stage.put("nonlabelable_count", nonlabelableCount)
        if (lastError.isNotBlank() || state == "failed" || state == "ineligible") {
            stage.put("last_error", lastError)
        }
        if (detail != null) {
            val merged = stage.optJSONObject("detail") ?: JSONObject()
            detail.keys().forEach { key -> merged.put(key, detail.opt(key)) }
            stage.put("detail", merged)
        }
        if (state == "running" && prev != "running") {
            stage.put("started_at", now)
            stage.put("ended_at", JSONObject.NULL)
        }
        if (state in setOf("verified", "failed", "ineligible", "disabled", "not_attempted")) {
            if (stage.isNull("started_at") || stage.optString("started_at").isBlank()) {
                stage.put("started_at", now)
            }
            stage.put("ended_at", now)
        }
        stages.put(name, stage)
        run.put("updated_at", now)
        return refreshCompletionFlags(run)
    }

    fun nextResumableStage(run: JSONObject): String? {
        val stages = run.optJSONObject("stages") ?: return STAGE_ORDER.first()
        for (name in STAGE_ORDER) {
            val st = stages.optJSONObject(name)?.optString("state") ?: "pending"
            if (st in setOf("pending", "running", "failed")) return name
        }
        return null
    }

    fun assessC3Frames(frames: JSONArray): JSONObject {
        if (frames.length() == 0) {
            return JSONObject()
                .put("eligible", false)
                .put("reason_code", REASON_NO_FRAMES)
                .put("frame_count", 0)
                .put("verified_population_frames", 0)
                .put("capped_or_incomplete_frames", 0)
                .put("would_write_rows", false)
                .put("message", "No C3 recording frames in original evidence.")
        }
        var verified = 0
        var capped = 0
        for (i in 0 until frames.length()) {
            val frame = frames.optJSONObject(i)
            if (frame == null) {
                capped += 1
                continue
            }
            val popOk = frame.optBoolean("candidate_population_verified", false)
            val genOk = frame.optBoolean("generated_capture_complete", false)
            var trunc = 0
            for (key in listOf(
                "truncated_at_ranked_evidence",
                "truncated_at_persistence",
                "truncated_at_candidates"
            )) {
                trunc += frame.optInt(key, 0)
                val detail = frame.optJSONObject("detail")
                if (detail != null) trunc += detail.optInt(key, 0)
            }
            if (popOk && genOk && trunc == 0) verified += 1 else capped += 1
        }
        if (verified == 0 || capped > 0) {
            return JSONObject()
                .put("eligible", false)
                .put("reason_code", REASON_CAPPED_POPULATION)
                .put("frame_count", frames.length())
                .put("verified_population_frames", verified)
                .put("capped_or_incomplete_frames", capped)
                .put("would_write_rows", false)
                .put(
                    "message",
                    "Original C3 frames exist but candidate population provenance is " +
                        "capped/incomplete; refusing to fabricate verified percentile rows."
                )
        }
        return JSONObject()
            .put("eligible", true)
            .put("reason_code", "")
            .put("frame_count", frames.length())
            .put("verified_population_frames", verified)
            .put("capped_or_incomplete_frames", 0)
            .put("would_write_rows", true)
            .put("message", "$verified frames have verified candidate-population provenance.")
    }

    
    fun applyPerformanceMetricsResult(run: JSONObject, result: JSONObject): JSONObject {
        val state = result.optString("state", "verified").ifBlank { "verified" }
        val reason = result.optString("reason_code", REASON_METRICS_WRITTEN)
        val detail = JSONObject()
            .put("active_recommendation_unchanged", result.optBoolean("active_recommendation_unchanged", true))
            .put("g6", true)
        result.optJSONObject("detail")?.let { d ->
            detail.put("variants", d.opt("variants"))
            detail.put("shadow_b_differ_count", d.opt("shadow_b_differ_count"))
        }
        return setStage(
            run,
            "performance_metrics",
            state,
            reasonCode = reason,
            expectedCount = result.optInt("expected_count", 0),
            writtenCount = result.optInt("written_count", 0),
            verifiedCount = result.optInt("verified_count", 0),
            detail = detail
        )
    }

    fun applyC3Assessment(run: JSONObject, assessment: JSONObject): JSONObject {
        return if (assessment.optBoolean("eligible", false)) {
            setStage(
                run,
                "percentile_finalization",
                "pending",
                expectedCount = assessment.optInt("frame_count", 0),
                detail = assessment
            )
        } else {
            setStage(
                run,
                "percentile_finalization",
                "ineligible",
                reasonCode = assessment.optString("reason_code", REASON_NO_FRAMES),
                expectedCount = assessment.optInt("frame_count", 0),
                writtenCount = 0,
                verifiedCount = 0,
                lastError = assessment.optString("message", ""),
                detail = assessment
            )
        }
    }

    fun acquireLease(
        run: JSONObject,
        holder: String,
        nowMs: Long = System.currentTimeMillis(),
        leaseMs: Long = LEASE_DEFAULT_MS,
        force: Boolean = false
    ): Triple<JSONObject, Boolean, String> {
        val expires = run.optLong("lease_expires_at_ms", 0L)
        val current = if (run.isNull("lease_holder")) "" else run.optString("lease_holder", "")
        if (current.isNotBlank() && current != holder && expires > nowMs && !force) {
            return Triple(run, false, REASON_DUPLICATE_LEASE)
        }
        run.put("lease_holder", holder)
        run.put("lease_expires_at_ms", nowMs + leaseMs)
        run.put("updated_at", java.time.Instant.now().toString())
        run.put("active", true)
        return Triple(run, true, "")
    }

    private fun mirrorFile(context: Context, sessionDate: String): File {
        val dir = File(context.applicationContext.filesDir, MIRROR_DIR).apply { mkdirs() }
        val safe = sessionDate.filter { it.isDigit() || it == '-' }.ifBlank { "unknown" }
        return File(dir, "run_$safe.json")
    }

    fun persistLocal(context: Context, prefs: SharedPreferences, run: JSONObject) {
        refreshCompletionFlags(run)
        val sessionDate = run.optString("session_date")
        mirrorFile(context, sessionDate).writeText(run.toString())
        prefs.edit()
            .putString(PREF_RUN_JSON, run.toString())
            .putString(PREF_RUN_ID, run.optString("run_id"))
            .putBoolean(PREF_LABELS_SAVED, run.optBoolean("labels_saved", false))
            .putBoolean(PREF_LEARNING_COMPLETE, run.optBoolean("learning_complete", false))
            .putString("g5_evaluation_run_session", sessionDate)
            .commit()
    }

    fun loadLocal(context: Context, prefs: SharedPreferences, sessionDate: String): JSONObject? {
        val file = mirrorFile(context, sessionDate)
        if (file.exists()) {
            return try {
                JSONObject(file.readText())
            } catch (_: Exception) {
                null
            }
        }
        val cachedSession = prefs.getString("g5_evaluation_run_session", "") ?: ""
        if (cachedSession == sessionDate) {
            val raw = prefs.getString(PREF_RUN_JSON, null) ?: return null
            return try {
                JSONObject(raw)
            } catch (_: Exception) {
                null
            }
        }
        return null
    }

    fun beginOrResume(
        context: Context,
        prefs: SharedPreferences,
        sessionDate: String,
        holder: String,
        inputManifest: JSONObject
    ): Triple<JSONObject, Boolean, String> {
        val existing = loadLocal(context, prefs, sessionDate)
        val manifestHash = hashInputManifest(inputManifest)
        val desiredId = buildRunId(sessionDate, inputManifestHash = manifestHash)
        val run = when {
            existing != null && existing.optString("run_id") == desiredId -> existing
            else -> newRun(sessionDate, inputManifest)
        }
        val (leased, ok, reason) = acquireLease(run, holder)
        if (ok) persistLocal(context, prefs, leased)
        return Triple(leased, ok, reason)
    }

    fun stagesSummaryJson(run: JSONObject): JSONObject {
        val stages = run.optJSONObject("stages") ?: JSONObject()
        val out = JSONObject()
        val names = STAGE_ORDER + listOf("training", "promotion")
        for (name in names) {
            val stage = stages.optJSONObject(name) ?: continue
            out.put(
                name,
                JSONObject()
                    .put("state", stage.optString("state"))
                    .put("reason_code", stage.optString("reason_code"))
                    .put("expected_count", stage.optInt("expected_count", 0))
                    .put("written_count", stage.optInt("written_count", 0))
                    .put("verified_count", stage.optInt("verified_count", 0))
                    .put("nonlabelable_count", stage.optInt("nonlabelable_count", 0))
                    .put("last_error", stage.optString("last_error"))
            )
        }
        return out
    }
}
