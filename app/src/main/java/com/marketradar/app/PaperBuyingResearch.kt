package com.marketradar.app

import org.json.JSONObject
import java.io.File
import java.security.MessageDigest

/**
 * b510 Batch C: native ownership of the prospective X1 Paper buying research journal
 * (paper_buying_research.py). Research only: no order, candidate, notification or label.
 *
 * - Context keys are stripped before EVERY analyze call and re-added only for a genuine
 *   live (non-refresh) poll whose resolved execution mode is exactly "paper".
 * - Status and export are read-only and size-bounded; they never expose local paths.
 */
object PaperBuyingResearch {
    const val STATE_DIR = "paper_buying_x1_v2"
    const val CAPTURE_SOURCE = "LIVE_PHONE_POLL"
    const val MAX_STATUS_BYTES = 16_384L
    const val MAX_JOURNAL_BYTES = 1_000_000L
    const val EXPORT_SCHEMA = "x1_export_v1"
    val CONTEXT_KEYS = listOf("x1CaptureSource", "x1StateDir", "x1PollMs", "x1QuoteFetchedMs")

    fun stateDir(filesDir: File): File = File(filesDir, STATE_DIR)

    /** Returns true when X1 keys were attached. Always strips stale keys first. */
    fun applyContext(ctx: JSONObject, isRefresh: Boolean, filesDir: File, pollMs: Long,
                     nfQuoteMs: Long, bnfQuoteMs: Long): Boolean {
        for (key in CONTEXT_KEYS) ctx.remove(key)
        if (isRefresh || ctx.optString("executionMode", "") != "paper") return false
        val explicitAlt = ctx.opt("execution_mode")
        if (explicitAlt != null && explicitAlt != JSONObject.NULL && explicitAlt.toString().trim().isNotEmpty()
            && explicitAlt.toString().trim().lowercase() != "paper") return false
        ctx.put("x1CaptureSource", CAPTURE_SOURCE)
        ctx.put("x1StateDir", stateDir(filesDir).absolutePath)
        ctx.put("x1PollMs", pollMs)
        val quotes = JSONObject()
        if (nfQuoteMs > 0L) quotes.put("NF", nfQuoteMs)
        if (bnfQuoteMs > 0L) quotes.put("BNF", bnfQuoteMs)
        ctx.put("x1QuoteFetchedMs", quotes)
        return true
    }

    /** Bounded current status written by the Python collector after each live Paper poll. */
    fun status(filesDir: File): String {
        val file = File(stateDir(filesDir), "status.json")
        return try {
            if (!file.exists()) return JSONObject().put("status", "NOT_STARTED")
                .put("paper_research_only", true).put("recommendation_authority", false).toString()
            if (file.length() > MAX_STATUS_BYTES) return unavailable("STATUS_SIZE_LIMIT")
            val parsed = JSONObject(file.readText())
            parsed.put("paper_research_only", true).put("quote_simulation_only", true)
                .put("recommendation_authority", false).toString()
        } catch (e: Exception) {
            unavailable("STATUS_UNREADABLE")
        }
    }

    /**
     * Bounded export envelope of the raw journal bytes (also when the journal is corrupt, so it can
     * be reviewed). Null journal_text when absent or over the cap. Never includes a path.
     */
    fun exportEnvelope(filesDir: File, nowMs: Long): JSONObject {
        val dir = stateDir(filesDir)
        val journal = File(dir, "journal.json")
        val env = JSONObject().put("schema", EXPORT_SCHEMA).put("exported_at_ms", nowMs)
            .put("paper_research_only", true).put("recommendation_authority", false)
        if (!journal.exists()) return env.put("status", "NO_JOURNAL")
        val size = journal.length()
        env.put("journal_bytes", size)
        if (size > MAX_JOURNAL_BYTES) return env.put("status", "JOURNAL_SIZE_LIMIT")
        val bytes = journal.readBytes()
        env.put("journal_sha256", sha256(bytes))
        val text = String(bytes, Charsets.UTF_8)
        env.put("journal_parse_ok", runCatching { JSONObject(text) }.isSuccess)
        env.put("journal_text", text)
        val status = File(dir, "status.json")
        if (status.exists() && status.length() <= MAX_STATUS_BYTES) {
            runCatching { env.put("status_snapshot", JSONObject(status.readText())) }
        }
        return env.put("status", "OK")
    }

    fun sha256(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

    private fun unavailable(reason: String) = JSONObject().put("status", reason)
        .put("paper_research_only", true).put("recommendation_authority", false).toString()
}
