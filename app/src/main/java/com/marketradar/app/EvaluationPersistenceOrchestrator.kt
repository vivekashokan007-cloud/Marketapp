package com.marketradar.app

/**
 * b513 R1–R3: the evening evaluation save/verify decision, extracted from MarketMLService so it can be tested.
 *
 * Read phases each freeze their OWN created_at boundary, held fixed across all stages and bounded retries of that phase:
 *  - verify_first: boundary taken before any write in this attempt.
 *  - post_save / post_repair: NEW boundary taken AFTER the write returns, so freshly stamped / re-upserted created_at
 *    values are inside it (R1).
 * A matching current-schema ack (content + contract bound, R2) only selects verify-first; it never proves anything.
 * A failed/untrusted verify-first read never triggers a write (R3): it is a classified retryable verification failure.
 * A write after verify-first happens only when a SUCCESSFUL read proves a non-empty missing subset.
 */
object EvaluationPersistenceOrchestrator {
    enum class Outcome {
        VERIFIED_FROM_ACK,          // verify-first read proved exact set; no write
        VERIFY_FIRST_READ_FAILED,   // verify-first read failed/untrusted; no write; retryable
        REPAIRED_AND_VERIFIED,      // verify-first proved missing subset -> write -> post_repair read exact
        SAVED_AND_VERIFIED,         // no usable ack -> write -> post_save read exact
        WRITTEN_NOT_VERIFIED,       // write ok, post-write read failed or incomplete
        SAVE_FAILED
    }

    class Ports(
        /** Bounded read-back for one phase at one fixed created_at boundary. */
        val readback: (phase: String, cutoffIso: String) -> SupabaseClient.OutcomeIdentityReadback,
        /** Full idempotent upsert of the validated stream. */
        val save: () -> SupabaseClient.EvaluationSaveResult,
        val certify: (SupabaseClient.OutcomeIdentityReadback) -> EvaluationIdentityCoverage.Assessment,
        /** Wall clock ISO instant on the same clock that stamps created_at in the save builders. */
        val nowIso: () -> String = { SupabaseClient.exportFreezeCutoffIso() },
        val log: (String) -> Unit = {}
    )

    data class Result(
        val outcome: Outcome,
        val saveResult: SupabaseClient.EvaluationSaveResult,
        val readback: SupabaseClient.OutcomeIdentityReadback,
        val assessment: EvaluationIdentityCoverage.Assessment,
        val writes: Int,
        val ackToStore: EvaluationCertification.UploadAck?,
        val phaseCutoffs: Map<String, String>
    ) {
        val certified: Boolean get() = assessment.complete && readback.ok
    }

    fun run(prior: EvaluationCertification.UploadAck?, current: EvaluationCertification.UploadAck,
            producedCount: Int, ports: Ports): Result {
        val cutoffs = linkedMapOf<String, String>()
        fun read(phase: String): SupabaseClient.OutcomeIdentityReadback {
            val c = ports.nowIso(); cutoffs[phase] = c
            return ports.readback(phase, c)
        }
        fun writeThenVerify(phase: String, okOutcome: Outcome): Result {
            val save = ports.save()
            if (!save.success) {
                val rb = SupabaseClient.OutcomeIdentityReadback(ok = false, status = "not_read_save_failed",
                    error = "save_failed: ${save.message}")
                return Result(Outcome.SAVE_FAILED, save, rb, ports.certify(rb), 1, null, cutoffs)
            }
            val ack = current.copy(persistedCount = save.persistedCount, primaryPersistedCount = save.primaryPersistedCount,
                evaluationPersistedCount = save.evaluationPersistedCount, rejectedPersistedCount = save.rejectedPersistedCount)
            val rb = read(phase) // boundary taken after the write returned
            val a = ports.certify(rb)
            return Result(if (rb.ok && a.complete) okOutcome else Outcome.WRITTEN_NOT_VERIFIED, save, rb, a, 1, ack, cutoffs)
        }

        if (prior != null && EvaluationCertification.shouldVerifyFirst(prior, current)) {
            val rb = read("verify_first")
            val a = ports.certify(rb)
            val fromAck = EvaluationCertification.saveResultFromAck(prior, producedCount)
            return when {
                !rb.ok -> {
                    ports.log("verify_first read failed; no write; retryable " + (rb.failure?.line() ?: rb.status))
                    Result(Outcome.VERIFY_FIRST_READ_FAILED,
                        fromAck.copy(message = "verify_first_read_failed (no re-upload; prior ack retained)"),
                        rb, a, 0, null, cutoffs)
                }
                a.complete -> Result(Outcome.VERIFIED_FROM_ACK, fromAck, rb, a, 0, null, cutoffs)
                a.missingCount > 0 -> {
                    ports.log("verify_first proved missing=${a.missingCount}; repair write")
                    writeThenVerify("post_repair", Outcome.REPAIRED_AND_VERIFIED)
                }
                // Successful read but incomplete without a concrete missing subset: untrusted, do not write.
                else -> Result(Outcome.VERIFY_FIRST_READ_FAILED,
                    fromAck.copy(message = "verify_first_untrusted (no re-upload; prior ack retained)"), rb, a, 0, null, cutoffs)
            }
        }
        return writeThenVerify("post_save", Outcome.SAVED_AND_VERIFIED)
    }
}
