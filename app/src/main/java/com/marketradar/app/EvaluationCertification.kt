package com.marketradar.app

import org.json.JSONObject
import java.security.MessageDigest

/**
 * b513: pure (Android-free) certification + verification-first recovery decisions for the evening evaluation.
 *
 * - An upload acknowledgment is only a recovery HINT: it lets a retry read back first instead of re-uploading the
 *   retained (148 MB on 21 Oct) stream. It is never identity proof.
 * - Certification is exact composite-key coverage (snapshot_id|candidate_id|role) from server read-back, plus the
 *   required primary companions. Counts are never proof.
 * - Nothing here computes outcomes, changes the session date, or sets labels_saved/learning_complete.
 */
object EvaluationCertification {
    /** Ack schema version. v1 (b513 pre-R2, identity-keys only) is never accepted: it cannot prove content. */
    const val ACK_SCHEMA_VERSION = 2

    /**
     * What a successful save acknowledged, bound to (R2) the SHA-256 of the full validated output stream bytes and a
     * digest of the frozen run contract (session date, expected snapshot ids, expected primary keys, run input manifest hash,
     * evaluator version, policy label contract). A matching ack is only a recovery hint; server identity + companion verification still runs.
     */
    data class UploadAck(
        val sessionDate: String,
        val rows: Int,
        val bytes: Long,
        val keysDigest: String,
        val contentSha256: String,
        val contractDigest: String,
        val persistedCount: Int = 0,
        val primaryPersistedCount: Int = 0,
        val evaluationPersistedCount: Int = 0,
        val rejectedPersistedCount: Int = 0,
        val schemaVersion: Int = ACK_SCHEMA_VERSION
    ) {
        fun toJson(): JSONObject = JSONObject().put("v", schemaVersion).put("session_date", sessionDate).put("rows", rows)
            .put("bytes", bytes).put("keys_digest", keysDigest).put("content_sha256", contentSha256)
            .put("contract_digest", contractDigest).put("persisted", persistedCount)
            .put("primary_persisted", primaryPersistedCount).put("evaluation_persisted", evaluationPersistedCount)
            .put("rejected_persisted", rejectedPersistedCount)

        fun sameStream(other: UploadAck): Boolean = schemaVersion == ACK_SCHEMA_VERSION &&
            other.schemaVersion == ACK_SCHEMA_VERSION && contentSha256.length == 64 &&
            sessionDate == other.sessionDate && rows == other.rows && bytes == other.bytes &&
            keysDigest == other.keysDigest && contentSha256 == other.contentSha256 && contractDigest == other.contractDigest

        companion object {
            fun fromJson(raw: String?): UploadAck? = try {
                if (raw.isNullOrBlank()) null else JSONObject(raw).takeIf { it.optInt("v") == ACK_SCHEMA_VERSION }?.let {
                    UploadAck(it.getString("session_date"), it.getInt("rows"), it.getLong("bytes"), it.getString("keys_digest"),
                        it.getString("content_sha256"), it.getString("contract_digest"),
                        it.optInt("persisted"), it.optInt("primary_persisted"), it.optInt("evaluation_persisted"),
                        it.optInt("rejected_persisted"))
                }
            } catch (_: Exception) { null }

            /** Ack for the current validated stream (before any save; counts filled in after a successful write). */
            internal fun forStream(sessionDate: String, validated: EvaluationOutcomeFile.Validated, contractDigest: String): UploadAck =
                UploadAck(sessionDate, validated.count, validated.file.length(), keysDigest(validated.compositeKeys),
                    validated.contentSha256, contractDigest)
        }
    }

    /** Run ledger fields that freeze the evaluation input/policy; part of the ack contract. */
    val FROZEN_RUN_FIELDS = listOf("input_manifest_hash", "evaluator_version", "policy_label_contract", "scope")

    fun frozenContractOf(run: JSONObject?): Map<String, String> =
        FROZEN_RUN_FIELDS.associateWith { run?.opt(it)?.toString().orEmpty() }

    /** Frozen run contract digest: order-independent over ids/keys, prefixed with the ack schema version. */
    fun contractDigest(sessionDate: String, expectedSnapshotIds: Collection<String>,
                       expectedPrimaryCompositeKeys: Collection<String>, frozenContract: Map<String, String>): String {
        val md = MessageDigest.getInstance("SHA-256")
        fun put(s: String) { md.update(s.toByteArray(Charsets.UTF_8)); md.update(10) }
        put("ack_v$ACK_SCHEMA_VERSION"); put("date=$sessionDate"); frozenContract.toSortedMap().forEach { (k, v) -> put("$k=$v") }
        put("ids"); expectedSnapshotIds.distinct().sorted().forEach(::put)
        put("primary"); expectedPrimaryCompositeKeys.distinct().sorted().forEach(::put)
        return md.digest().joinToString("") { "%02x".format(it) }
    }

    fun keysDigest(keys: Collection<String>): String {
        val md = MessageDigest.getInstance("SHA-256")
        keys.sorted().forEach { md.update(it.toByteArray(Charsets.UTF_8)); md.update(10) }
        return md.digest().joinToString("") { "%02x".format(it) }
    }

    /** Verify first only when the prior ack is a current-schema ack for this exact content + contract. */
    fun shouldVerifyFirst(prior: UploadAck?, current: UploadAck): Boolean = prior != null && prior.sameStream(current)

    /**
     * Exact certification: identity coverage (EvaluationIdentityCoverage.assessComposites) AND every required primary
     * companion present. Same rule MarketMLService applied inline before b513, now shared by verify-first and post-save.
     */
    fun certify(
        expectedSnapshotIds: Collection<String>,
        producedCompositeKeys: Collection<String>,
        expectedPrimaryCompositeKeys: Collection<String>,
        readback: SupabaseClient.OutcomeIdentityReadback
    ): EvaluationIdentityCoverage.Assessment {
        val assessment = EvaluationIdentityCoverage.assessComposites(
            expectedSnapshotIds = expectedSnapshotIds,
            producedCompositeKeys = producedCompositeKeys,
            serverCompositeKeys = if (readback.ok) readback.compositeKeys else emptySet(),
            readbackOk = readback.ok,
            readbackError = readback.error,
            expectedPrimaryCompositeKeys = expectedPrimaryCompositeKeys
        )
        if (!assessment.complete) return assessment
        val producedPrimary = assessment.producedCompositeKeys.filter { it.endsWith("|primary") }
        val required = (expectedPrimaryCompositeKeys + producedPrimary).distinct()
        val missingReco = required.filter { it !in readback.recommendationCompositeKeys }
        if (missingReco.isEmpty()) return assessment
        return assessment.copy(
            complete = false,
            missingCompositeKeys = (assessment.missingCompositeKeys + missingReco).distinct().sorted(),
            missingCount = assessment.missingCount + missingReco.size,
            missingPreview = (assessment.missingPreview + missingReco.map { "reco:$it" }).distinct().take(20),
            reasonCode = EvaluationIdentityCoverage.REASON_PARTIAL_IDENTITY,
            phase = EvaluationIdentityCoverage.PHASE_INCOMPLETE_IDENTITY
        )
    }

    /** Synthetic save result for a verify-first success: counts come from the prior ack, flagged as not re-uploaded. */
    fun saveResultFromAck(ack: UploadAck, producedCount: Int): SupabaseClient.EvaluationSaveResult =
        SupabaseClient.EvaluationSaveResult(
            success = true, producedCount = producedCount, persistedCount = ack.persistedCount,
            primaryPersistedCount = ack.primaryPersistedCount, evaluationPersistedCount = ack.evaluationPersistedCount,
            message = "verified_from_prior_upload_ack (no re-upload)", rejectedPersistedCount = ack.rejectedPersistedCount,
            rejectedSaveMode = "prior_ack", evaluationSaved = true, recommendationSaved = true,
            rejectedSaved = ack.rejectedPersistedCount > 0
        )

    /** One-line, privacy-safe status for job state / LogBuffer. Distinguishes "rows written" from "verified". */
    fun honestStatusLine(sessionDate: String, rowsWritten: Int?, readback: SupabaseClient.OutcomeIdentityReadback,
                         assessment: EvaluationIdentityCoverage.Assessment): String = when {
        !readback.ok -> "date=$sessionDate rowsWrittenAck=${rowsWritten ?: "-"} verification=FAILED attempts=${readback.attempts} " +
            (readback.failure?.line() ?: "class=unclassified_${readback.status.ifBlank { "unknown" }}") +
            " verifiedIdentities=unknown labelsSaved=false"
        !assessment.complete -> "date=$sessionDate rowsWrittenAck=${rowsWritten ?: "-"} verification=INCOMPLETE " +
            "missing=${assessment.missingCount} labelsSaved=false"
        else -> "date=$sessionDate rowsWrittenAck=${rowsWritten ?: "-"} verification=EXACT verified=${assessment.verifiedCompositeKeys.size}"
    }
}
