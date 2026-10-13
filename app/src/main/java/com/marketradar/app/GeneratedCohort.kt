package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject

/**
 * b503 C3: the capture-time EXPECTED generated-candidate cohort, frozen into the snapshot's
 * poll_summary_json.generated_cohort_manifest BEFORE any upload. It describes the bounded sample
 * (GeneratedCandidateRows.PERSIST_CAP = 50 per poll) exactly as built — never the full generator population.
 *
 * Row canonical form (identical for capture JSON and the exported database row):
 *   identity  = candidate_id | lane | rank            (stable id of one sampled row)
 *   index     = index_key                              (checked separately: WRONG_INDEX)
 *   content   = sha256(canonical CONTENT_FIELDS)[:16]  (text/int/bool columns only; floats excluded on purpose:
 *                                                       their textual form is not stable across the JSON/DB round trip)
 */
object GeneratedCohort {
    const val SCHEMA = "generated_cohort_manifest_v1"
    val INDEXES = listOf("NF", "BNF")
    val CONTENT_FIELDS = listOf("session_date", "recommendation_id", "quality_tag", "decision_source", "strategy_type",
        "trade_mode", "watchlist_rank", "was_surfaced", "width", "expiry", "execution_gate", "entry_action",
        "app_version", "brain_version")
    /** The only legitimate original reasons for an empty expectation. */
    val EMPTY_REASONS = setOf("no_fact_pack", "zero_candidates")

    fun canon(v: Any?): String = when (v) {
        null, JSONObject.NULL -> "\u2205"
        is Boolean -> v.toString()
        is Number -> { val d = v.toDouble(); if (d == Math.floor(d) && !d.isInfinite() && kotlin.math.abs(d) < 1e15) d.toLong().toString() else v.toString() }
        is String -> if (v.isBlank()) "\u2205" else v
        else -> v.toString()
    }

    fun identity(row: JSONObject): String =
        listOf("candidate_id", "lane", "rank").joinToString("|") { canon(if (row.has(it)) row.opt(it) else null) }

    fun content(row: JSONObject): String = FrozenCaptureRequest.sha256Hex(
        CONTENT_FIELDS.joinToString("\n") { "$it=" + canon(if (row.has(it)) row.opt(it) else null) }).take(16)

    fun entries(rows: List<JSONObject>): List<JSONObject> = rows.map {
        JSONObject().put("k", identity(it)).put("i", canon(it.opt("index_key"))).put("c", content(it))
    }.sortedWith(compareBy({ it.getString("k") }, { it.getString("i") }, { it.getString("c") }))

    fun cohortSha(entries: List<JSONObject>): String =
        FrozenCaptureRequest.sha256Hex(entries.joinToString("\n") { "${it.getString("k")}\t${it.getString("i")}\t${it.getString("c")}" })

    fun perIndex(entries: List<JSONObject>): JSONObject = JSONObject().also { o ->
        INDEXES.forEach { o.put(it, 0) }
        entries.forEach { e -> val i = e.getString("i"); o.put(i, o.optInt(i) + 1) }
    }

    private fun base(status: String, snapshotPollTs: String?, sessionDate: String, recommendationId: Any?) = JSONObject()
        .put("schema", SCHEMA).put("status", status)
        .put("sample_cap", GeneratedCandidateRows.PERSIST_CAP)
        .put("population", "bounded_sample_not_full_population")
        .put("snapshot_poll_ts", snapshotPollTs ?: JSONObject.NULL)
        .put("session_date", sessionDate)
        .put("recommendation_id", recommendationId ?: JSONObject.NULL)

    fun expected(rows: JSONArray, sessionDate: String, recommendationId: Any?): JSONObject {
        val list = (0 until rows.length()).map { rows.getJSONObject(it) }
        val e = entries(list)
        return base("EXPECTED", list.first().optString("snapshot_poll_ts"), sessionDate, recommendationId)
            .put("row_count", e.size).put("per_index", perIndex(e)).put("rows", JSONArray(e)).put("cohort_sha256", cohortSha(e))
    }

    fun notExpected(reason: String, snapshotPollTs: String?, sessionDate: String, recommendationId: Any?): JSONObject =
        base("NOT_EXPECTED", snapshotPollTs, sessionDate, recommendationId).put("reason", reason)
            .put("row_count", 0).put("per_index", perIndex(emptyList())).put("rows", JSONArray()).put("cohort_sha256", cohortSha(emptyList()))

    fun buildFailed(reason: String, snapshotPollTs: String?, sessionDate: String, recommendationId: Any?): JSONObject =
        base("BUILD_FAILED", snapshotPollTs, sessionDate, recommendationId).put("reason", reason.take(80))
}
