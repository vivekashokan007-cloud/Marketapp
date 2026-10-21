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
    /** What a successful save acknowledged, bound to the exact validated local stream. */
    data class UploadAck(
        val sessionDate: String,
        val rows: Int,
        val bytes: Long,
        val keysDigest: String,
        val persistedCount: Int = 0,
        val primaryPersistedCount: Int = 0,
        val evaluationPersistedCount: Int = 0,
        val rejectedPersistedCount: Int = 0
    ) {
        fun toJson(): JSONObject = JSONObject().put("v", 1).put("session_date", sessionDate).put("rows", rows)
            .put("bytes", bytes).put("keys_digest", keysDigest).put("persisted", persistedCount)
            .put("primary_persisted", primaryPersistedCount).put("evaluation_persisted", evaluationPersistedCount)
            .put("rejected_persisted", rejectedPersistedCount)

        fun sameStream(other: UploadAck): Boolean = sessionDate == other.sessionDate && rows == other.rows &&
            bytes == other.bytes && keysDigest == other.keysDigest

        companion object {
            fun fromJson(raw: String?): UploadAck? = try {
                if (raw.isNullOrBlank()) null else JSONObject(raw).takeIf { it.optInt("v") == 1 }?.let {
                    UploadAck(it.getString("session_date"), it.getInt("rows"), it.getLong("bytes"), it.getString("keys_digest"),
                        it.optInt("persisted"), it.optInt("primary_persisted"), it.optInt("evaluation_persisted"),
                        it.optInt("rejected_persisted"))
                }
            } catch (_: Exception) { null }
        }
    }

    fun keysDigest(keys: Collection<String>): String {
        val md = MessageDigest.getInstance("SHA-256")
        keys.sorted().forEach { md.update(it.toByteArray(Charsets.UTF_8)); md.update(10) }
        return md.digest().joinToString("") { "%02x".format(it) }
    }

    /** Verify first only when the prior ack is for this exact same-date validated stream. */
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
