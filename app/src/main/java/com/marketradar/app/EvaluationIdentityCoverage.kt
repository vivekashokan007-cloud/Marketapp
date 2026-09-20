package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject

/**
 * Batch C corrective gate: exact evaluation completeness.
 *
 * Expected snapshot coverage comes from the frozen labelable manifest.
 * Produced composites come from the local upsert attempt.
 * Persisted proof comes ONLY from Supabase identity readback
 * (snapshot_id + candidate_id + role). Aggregate counts are never proof.
 *
 * Called by MarketMLService on the real post-save path — not a copied predicate.
 */
object EvaluationIdentityCoverage {
    const val PHASE_INCOMPLETE_IDENTITY = "INCOMPLETE_IDENTITY"
    const val PHASE_FAILED_IDENTITY_COVERAGE = "FAILED_IDENTITY_COVERAGE"
    const val REASON_PARTIAL_IDENTITY = "PARTIAL_IDENTITY_COVERAGE"
    const val REASON_READBACK_FAILED = "IDENTITY_READBACK_FAILED"
    const val REASON_EMPTY_EXPECTED_NONEMPTY_SNAPSHOTS = "IDENTITY_MANIFEST_EMPTY_WHILE_SNAPSHOTS_PRESENT"
    const val REASON_MANIFEST_PARSE_FAILED = "IDENTITY_MANIFEST_PARSE_FAILED"

    data class CompositeKey(
        val snapshotId: String,
        val candidateId: String,
        val role: String
    ) {
        fun encoded(): String = "$snapshotId|$candidateId|$role"

        companion object {
            fun parse(raw: String): CompositeKey? {
                val parts = raw.split("|")
                if (parts.size != 3) return null
                val sid = parts[0].trim()
                val cid = parts[1].trim()
                val role = parts[2].trim().lowercase(java.util.Locale.US)
                if (sid.isEmpty() || cid.isEmpty() || role.isEmpty()) return null
                return CompositeKey(sid, cid, role)
            }

            fun fromRow(row: JSONObject): CompositeKey? {
                val sid = row.opt("snapshot_id")?.toString()?.trim().orEmpty()
                val cid = row.optString("candidate_id", "").trim()
                val role = row.optString("role", "").trim().lowercase(java.util.Locale.US)
                if (sid.isEmpty() || sid == "null" || cid.isEmpty() || role.isEmpty()) return null
                return CompositeKey(sid, cid, role)
            }
        }
    }

    data class Transition(
        val phase: String,
        val labelsSaved: Boolean,
        val writeEvaluationDoneDate: Boolean,
        val publishLabelsSaved: Boolean,
        val startC3: Boolean,
        val cancelReminder: Boolean,
        val logEvaluationComplete: Boolean,
        val clearRecoveryState: Boolean,
        val scheduleContinuation: Boolean
    )

    /** Production orchestration decision consumed by MarketMLService. */
    fun transitionFor(assessment: Assessment): Transition = if (assessment.complete) {
        Transition(
            phase = "LABELS_SAVED",
            labelsSaved = true,
            writeEvaluationDoneDate = true,
            publishLabelsSaved = true,
            startC3 = true,
            cancelReminder = true,
            logEvaluationComplete = true,
            clearRecoveryState = true,
            scheduleContinuation = false
        )
    } else {
        Transition(
            phase = assessment.phase.ifBlank { PHASE_INCOMPLETE_IDENTITY },
            labelsSaved = false,
            writeEvaluationDoneDate = false,
            publishLabelsSaved = false,
            startC3 = false,
            cancelReminder = false,
            logEvaluationComplete = false,
            clearRecoveryState = false,
            scheduleContinuation = true
        )
    }

    data class Assessment(
        val complete: Boolean,
        val expectedSnapshotIds: List<String>,
        val expectedPrimaryCompositeKeys: List<String>,
        val producedCompositeKeys: List<String>,
        val serverCompositeKeys: List<String>,
        val verifiedCompositeKeys: List<String>,
        val verifiedSnapshotIds: List<String>,
        val missingSnapshotIds: List<String>,
        val missingCompositeKeys: List<String>,
        val unexpectedServerCompositeKeys: List<String>,
        val reasonCode: String,
        val phase: String,
        val missingCount: Int,
        val missingPreview: List<String>
    )

    fun compositesFromOutcomes(outcomes: JSONArray): LinkedHashSet<String> {
        val out = linkedSetOf<String>()
        for (i in 0 until outcomes.length()) {
            val row = outcomes.optJSONObject(i) ?: continue
            fromRowOrNull(row)?.let { out.add(it.encoded()) }
        }
        return out
    }

    fun fromRowOrNull(row: JSONObject): CompositeKey? = CompositeKey.fromRow(row)

    /**
     * Exact completeness: every frozen labelable snapshot must have its exact
     * chosen-primary composite produced and server-verified, and every produced
     * composite must be present in server readback. A secondary or a different
     * primary candidate cannot substitute for the frozen chosen identity.
     */
    fun assess(
        expectedSnapshotIds: Collection<String>,
        producedOutcomes: JSONArray,
        serverCompositeKeys: Collection<String>,
        readbackOk: Boolean,
        readbackError: String? = null,
        expectedPrimaryCompositeKeys: Collection<String> = emptyList()
    ): Assessment {
        val expected = expectedSnapshotIds.map { it.trim() }.filter { it.isNotEmpty() && it != "null" }.distinct().sorted()
        val expectedPrimary = expectedPrimaryCompositeKeys
            .map { it.trim() }
            .filter { CompositeKey.parse(it)?.role == "primary" }
            .distinct()
            .sorted()
        val produced = compositesFromOutcomes(producedOutcomes)
        val server = serverCompositeKeys.map { it.trim() }.filter { it.isNotEmpty() }.toCollection(linkedSetOf())

        if (!readbackOk) {
            val missing = expected
            return Assessment(
                complete = false,
                expectedSnapshotIds = expected,
                expectedPrimaryCompositeKeys = expectedPrimary,
                producedCompositeKeys = produced.toList(),
                serverCompositeKeys = emptyList(),
                verifiedCompositeKeys = emptyList(),
                verifiedSnapshotIds = emptyList(),
                missingSnapshotIds = missing,
                missingCompositeKeys = produced.toList(),
                unexpectedServerCompositeKeys = emptyList(),
                reasonCode = REASON_READBACK_FAILED,
                phase = PHASE_FAILED_IDENTITY_COVERAGE,
                missingCount = missing.size.coerceAtLeast(produced.size),
                missingPreview = (missing + produced).distinct().take(20)
            )
        }

        val verified = produced.filter { it in server }.toCollection(linkedSetOf())
        val missingComposites = produced.filterNot { it in server }.sorted()
        val producedPrimary = produced.filter { CompositeKey.parse(it)?.role == "primary" }.toCollection(linkedSetOf())
        val verifiedPrimary = verified.filter { CompositeKey.parse(it)?.role == "primary" }.toCollection(linkedSetOf())
        val requiredPrimary = if (expectedPrimary.isNotEmpty()) {
            expectedPrimary.toCollection(linkedSetOf())
        } else {
            // Legacy callers still fail closed: one verified primary is required
            // for every labelable snapshot. A secondary can never substitute.
            producedPrimary.filter { CompositeKey.parse(it)?.snapshotId in expected }.toCollection(linkedSetOf())
        }
        val verifiedRequiredPrimary = if (expectedPrimary.isNotEmpty()) {
            verifiedPrimary.filter { it in requiredPrimary }.toCollection(linkedSetOf())
        } else {
            verifiedPrimary
        }
        val verifiedSnapshots = verifiedRequiredPrimary.mapNotNull { CompositeKey.parse(it)?.snapshotId }.toCollection(linkedSetOf())
        val producedPrimarySnapshots = producedPrimary.mapNotNull { CompositeKey.parse(it)?.snapshotId }.toSet()
        val missingExpectedPrimary = if (expectedPrimary.isNotEmpty()) {
            requiredPrimary.filterNot { it in produced }.sorted()
        } else {
            emptyList()
        }
        val unverifiedExpectedPrimary = requiredPrimary.filterNot { it in server }.sorted()
        val missingPrimarySnapshots = expected.filterNot { it in verifiedSnapshots }.sorted()
        // A labelable snapshot with no produced primary is incomplete even when a
        // secondary outcome for the same snapshot was produced and persisted.
        val unproducedPrimarySnapshots = expected.filterNot { it in producedPrimarySnapshots }.sorted()
        val allMissingSnapshots = (missingPrimarySnapshots + unproducedPrimarySnapshots).distinct().sorted()
        val missingRequiredPrimary = (missingExpectedPrimary + unverifiedExpectedPrimary).distinct().sorted()
        val unexpected = server.filterNot { it in produced }.sorted()

        val complete = allMissingSnapshots.isEmpty() && missingComposites.isEmpty() &&
            missingRequiredPrimary.isEmpty() && expected.isNotEmpty()
        // Empty expected with nonempty intent is handled by the caller (manifest fail-closed).
        val emptyComplete = expected.isEmpty() && produced.isEmpty()
        val ok = complete || emptyComplete

        val preview = (
            allMissingSnapshots.map { "snap:$it" } +
                missingRequiredPrimary.map { "primary:$it" } +
                missingComposites
            ).distinct().take(20)
        // Do not double-count a missing snapshot and its missing composites.
        val extraMissingComposites = missingComposites.count { key ->
            CompositeKey.parse(key)?.snapshotId !in allMissingSnapshots.toSet()
        }
        val missingPrimaryOutsideMissingSnapshots = missingRequiredPrimary.count { key ->
            CompositeKey.parse(key)?.snapshotId !in allMissingSnapshots.toSet()
        }
        val missingCount = allMissingSnapshots.size + extraMissingComposites + missingPrimaryOutsideMissingSnapshots
        return Assessment(
            complete = ok,
            expectedSnapshotIds = expected,
            expectedPrimaryCompositeKeys = expectedPrimary,
            producedCompositeKeys = produced.toList(),
            serverCompositeKeys = server.toList(),
            verifiedCompositeKeys = verified.toList(),
            verifiedSnapshotIds = verifiedSnapshots.sorted(),
            missingSnapshotIds = allMissingSnapshots,
            missingCompositeKeys = missingComposites,
            unexpectedServerCompositeKeys = unexpected,
            reasonCode = if (ok) "" else REASON_PARTIAL_IDENTITY,
            phase = if (ok) "" else PHASE_INCOMPLETE_IDENTITY,
            missingCount = if (ok) 0 else missingCount.coerceAtLeast(allMissingSnapshots.size),
            missingPreview = preview
        )
    }

    fun detailJson(assessment: Assessment, readbackError: String? = null): JSONObject {
        return JSONObject()
            .put("missing_identity_count", assessment.missingCount)
            .put("missing_identity_preview", assessment.missingPreview.joinToString(","))
            .put("missing_snapshot_count", assessment.missingSnapshotIds.size)
            .put("missing_composite_count", assessment.missingCompositeKeys.size)
            .put("expected_primary_composite_count", assessment.expectedPrimaryCompositeKeys.size)
            .put("verified_composite_count", assessment.verifiedCompositeKeys.size)
            .put("produced_composite_count", assessment.producedCompositeKeys.size)
            .put("server_composite_count", assessment.serverCompositeKeys.size)
            .put("unexpected_server_composite_count", assessment.unexpectedServerCompositeKeys.size)
            .put("unexpected_server_preview", assessment.unexpectedServerCompositeKeys.take(8).joinToString(","))
            .put("readback_error", readbackError ?: JSONObject.NULL)
            .put("identity_key", "snapshot_id|candidate_id|role")
    }
}
