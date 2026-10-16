package com.marketradar.app

/** Cooperative work budget. A returned batch is durable before a continuation is requested. */
internal class EvaluationBudget(timeoutMs: Long, private val clock: () -> Long = { System.nanoTime() / 1_000_000L }) {
    private val deadline = clock() + timeoutMs
    fun check(stage: String) {
        check(clock() < deadline) {
            "EVALUATION_TIME_BUDGET_EXCEEDED: cooperative budget reached during $stage; retry resumes the saved checkpoint."
        }
    }
    fun commitBatch(checkpoint: () -> Unit) {
        checkpoint()
        check("batch_checkpoint_saved")
    }
}
