package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.format.DateTimeFormatter
import java.time.format.DateTimeFormatterBuilder
import java.time.temporal.ChronoField

/**
 * b503 C3 (round 4): the capture-time EXPECTED generated-candidate SAMPLED cohort with a FULL-CONTENT witness.
 * Frozen into the snapshot's poll_summary_json.generated_cohort_manifest BEFORE any upload. It describes the
 * bounded sample (GeneratedCandidateRows.PERSIST_CAP = 50 rows per poll) exactly as built; never the full
 * generator population.
 *
 * Contract `generated_row_witness_v2` (manifest schema generated_cohort_manifest_v2): every capture-written column of
 * public.ml_generated_candidates ([COLUMNS], PostgreSQL types verified read-only on 13 Oct 2026) is canonicalised by
 * its COLUMN type, not by its name, so the capture-side JSON value and the value read back from Postgres produce the
 * same text exactly when Postgres stores the same value:
 *   TIMESTAMPTZ  instant in epoch microseconds ("TS:<micros>"); any offset/format of the same instant is equal
 *   DATE         ISO yyyy-MM-dd ("D:")
 *   TEXT         exact string ("S:"); a JSON number written to a text column is stored as its JSON literal, so it is
 *                rendered with org.json's own number serialisation (the bytes the phone uploads)
 *   INTEGER      exact int32 ("I:"); integral JSON numbers (3, 3.0, 3e0) are the same value; non-integral/out of range
 *                is a type error
 *   BOOLEAN      "B:true"/"B:false"; the strings "true"/"false" are what Postgres accepts for boolean
 *   FLOAT8       exact IEEE-754 binary64 bits ("F:<16 hex>") of the parsed value: 1, 1.0, 1e0 and 1.000 are the SAME
 *                double and therefore equal; any different double (even 1 ulp) differs. No epsilon, no rounding.
 *                -0.0 and 0.0 are distinct bits and Postgres float8 preserves the sign. NaN/±Infinity are rejected
 *                ("!nonfinite"): the upload JSON cannot carry them.
 *   null         "N" for every type (null ≠ any value)
 *   type errors  "!type:<TYPE>" — never equal to a stored value, so the audit fails
 * Server-owned id and created_at are excluded. Per row: identity k, index i, and witness w = sha256 of the 43
 * canonical "column=value" lines. Per column across the sample: col_sha[c] = sha256 of that column's canonical values
 * in entry order (names the changed column on a mismatch). Size: ≤ 50 rows × (k, i, 64-hex w) + 43 column digests.
 */
object GeneratedCohort {
    const val SCHEMA = "generated_cohort_manifest_v2"
    const val CONTRACT = "generated_row_witness_v2"
    const val POPULATION = "bounded_sample_not_full_population"
    val INDEXES = listOf("NF", "BNF")
    /** The only legitimate original reasons for an empty expectation. */
    val EMPTY_REASONS = setOf("no_fact_pack", "zero_candidates")
    val STATUSES = setOf("EXPECTED", "NOT_EXPECTED", "BUILD_FAILED")

    enum class T { TIMESTAMPTZ, DATE, TEXT, INTEGER, BOOLEAN, FLOAT8 }

    /** Every capture-written column of public.ml_generated_candidates (excludes server-owned id, created_at). */
    val COLUMNS: List<Pair<String, T>> = listOf(
        "snapshot_poll_ts" to T.TIMESTAMPTZ, "session_date" to T.DATE, "recommendation_id" to T.TEXT,
        "quality_tag" to T.TEXT, "decision_source" to T.TEXT, "candidate_id" to T.TEXT, "lane" to T.TEXT,
        "index_key" to T.TEXT, "strategy_type" to T.TEXT, "trade_mode" to T.TEXT, "rank" to T.INTEGER,
        "watchlist_rank" to T.INTEGER, "was_surfaced" to T.BOOLEAN, "net_premium" to T.FLOAT8, "max_profit" to T.FLOAT8,
        "max_loss" to T.FLOAT8, "risk_reward" to T.TEXT, "premium_edge" to T.FLOAT8, "ev_per_1k" to T.FLOAT8,
        "est_cost" to T.FLOAT8, "capital_blocked" to T.BOOLEAN, "width" to T.INTEGER, "expiry" to T.TEXT,
        "sigma_otm" to T.FLOAT8, "iv_richness" to T.FLOAT8, "credit_width_ratio" to T.FLOAT8, "is_credit" to T.BOOLEAN,
        "execution_ready" to T.BOOLEAN, "execution_gate" to T.TEXT, "entry_action" to T.TEXT, "direction_safe" to T.BOOLEAN,
        "brain_score" to T.FLOAT8, "p_ml" to T.FLOAT8, "ml_action" to T.TEXT, "ml_edge" to T.FLOAT8, "ml_regime" to T.TEXT,
        "ml_unsure" to T.BOOLEAN, "ml_ood_flag" to T.BOOLEAN, "signal_independence_score" to T.FLOAT8,
        "generated_count" to T.INTEGER, "watchlist_count" to T.INTEGER, "app_version" to T.TEXT, "brain_version" to T.TEXT)
    val EXCLUDED = listOf("id", "created_at")

    /** Machine-checkable column contract embedded in every v2 manifest. */
    val COLUMN_CONTRACT: String = COLUMNS.joinToString(",") { "${it.first}:${it.second}" }

    private val TS_FORMAT: DateTimeFormatter = DateTimeFormatterBuilder().parseCaseInsensitive()
        .append(DateTimeFormatter.ISO_LOCAL_DATE).appendLiteral('T').appendPattern("HH:mm:ss")
        .optionalStart().appendFraction(ChronoField.NANO_OF_SECOND, 1, 9, true).optionalEnd()
        .optionalStart().appendOffset("+HH:MM", "Z").optionalEnd()
        .optionalStart().appendOffset("+HHMM", "Z").optionalEnd()
        .optionalStart().appendOffset("+HH", "Z").optionalEnd().toFormatter()

    fun instantOf(text: String): Instant? = runCatching { OffsetDateTime.parse(text.trim().replace(' ', 'T'), TS_FORMAT).toInstant() }.getOrNull()

    private fun finiteDouble(v: Any?): Double? = when (v) {
        is Number -> v.toDouble()
        is String -> v.trim().toDoubleOrNull()
        else -> null
    }

    /** Canonical text of one column value under the contract above. Pure; used for capture AND stored rows. */
    fun canonical(type: T, raw: Any?): String {
        if (raw == null || raw == JSONObject.NULL) return "N"
        return when (type) {
            T.TIMESTAMPTZ -> (raw as? String)?.let { instantOf(it) }
                ?.let { "TS:" + (Math.multiplyExact(it.epochSecond, 1_000_000L) + it.nano / 1000) } ?: "!type:$type"
            T.DATE -> (raw as? String)?.trim()?.takeIf { Regex("\\d{4}-\\d{2}-\\d{2}").matches(it) }
                ?.let { runCatching { "D:" + LocalDate.parse(it) }.getOrNull() } ?: "!type:$type"
            T.TEXT -> when (raw) {
                is String -> "S:$raw"
                is Number -> "S:" + JSONObject.numberToString(raw)
                is Boolean -> "S:$raw"
                else -> "!type:$type"
            }
            T.INTEGER -> {
                val d = finiteDouble(raw)
                val asLong = when {
                    raw is Int || raw is Long || raw is Short || raw is Byte -> (raw as Number).toLong()
                    d != null && d.isFinite() && d == Math.floor(d) && kotlin.math.abs(d) <= Int.MAX_VALUE.toDouble() -> d.toLong()
                    else -> null
                }
                if (asLong != null && asLong in Int.MIN_VALUE..Int.MAX_VALUE) "I:$asLong" else "!type:$type"
            }
            T.BOOLEAN -> when (raw) {
                is Boolean -> "B:$raw"
                "true" -> "B:true"; "false" -> "B:false"
                else -> "!type:$type"
            }
            T.FLOAT8 -> {
                val d = when (raw) { is Number -> raw.toDouble(); is String -> raw.trim().toDoubleOrNull(); else -> null }
                when {
                    d == null -> "!type:$type"
                    !d.isFinite() -> "!nonfinite"
                    else -> "F:" + java.lang.Long.toHexString(java.lang.Double.doubleToRawLongBits(d)).padStart(16, '0')
                }
            }
        }
    }

    private fun value(row: JSONObject, c: String): Any? = if (row.has(c)) row.opt(c) else null

    fun identity(row: JSONObject): String =
        listOf("candidate_id" to T.TEXT, "lane" to T.TEXT, "rank" to T.INTEGER).joinToString("|") { canonical(it.second, value(row, it.first)) }

    fun index(row: JSONObject): String = (value(row, "index_key") as? String) ?: "N"

    fun canonicalLines(row: JSONObject): List<String> = COLUMNS.map { (c, t) -> "$c=" + canonical(t, value(row, c)) }

    fun witness(row: JSONObject): String = FrozenCaptureRequest.sha256Hex(canonicalLines(row).joinToString("\n"))

    fun entries(rows: List<JSONObject>): List<Pair<JSONObject, JSONObject>> = rows.map {
        JSONObject().put("k", identity(it)).put("i", index(it)).put("w", witness(it)) to it
    }.sortedWith(compareBy({ it.first.getString("k") }, { it.first.getString("i") }, { it.first.getString("w") }))

    fun cohortSha(entries: List<JSONObject>): String =
        FrozenCaptureRequest.sha256Hex(CONTRACT + "\n" + entries.joinToString("\n") { "${it.optString("k")}\t${it.optString("i")}\t${it.optString("w")}" })

    /** Per column: digest of that column's canonical values over the rows in entry order. */
    fun columnShas(orderedRows: List<JSONObject>): JSONObject = JSONObject().also { o ->
        COLUMNS.forEach { (c, t) -> o.put(c, FrozenCaptureRequest.sha256Hex(orderedRows.joinToString("\n") { canonical(t, value(it, c)) }).take(32)) }
    }

    fun perIndex(entries: List<JSONObject>): JSONObject = JSONObject().also { o ->
        INDEXES.forEach { o.put(it, 0) }
        entries.forEach { e -> val i = e.optString("i"); o.put(i, o.optInt(i) + 1) }
    }

    private fun base(status: String, snapshotPollTs: String?, sessionDate: String, recommendationId: Any?) = JSONObject()
        .put("schema", SCHEMA).put("contract", CONTRACT).put("columns", COLUMN_CONTRACT).put("status", status)
        .put("sample_cap", GeneratedCandidateRows.PERSIST_CAP).put("population", POPULATION)
        .put("snapshot_poll_ts", snapshotPollTs ?: JSONObject.NULL)
        .put("session_date", sessionDate)
        .put("recommendation_id", recommendationId ?: JSONObject.NULL)

    private fun empty(m: JSONObject) = m.put("row_count", 0).put("per_index", perIndex(emptyList())).put("rows", JSONArray())
        .put("cohort_sha256", cohortSha(emptyList())).put("col_sha", columnShas(emptyList())).put("type_errors", JSONArray())

    fun expected(rows: JSONArray, sessionDate: String, recommendationId: Any?): JSONObject {
        val list = (0 until rows.length()).map { rows.getJSONObject(it) }
        val e = entries(list)
        return base("EXPECTED", list.first().optString("snapshot_poll_ts"), sessionDate, recommendationId)
            .put("row_count", e.size).put("per_index", perIndex(e.map { it.first }))
            .put("rows", JSONArray(e.map { it.first })).put("cohort_sha256", cohortSha(e.map { it.first }))
            .put("col_sha", columnShas(e.map { it.second }))
            .put("type_errors", JSONArray(e.flatMap { (en, r) -> canonicalLines(r).filter { it.substringAfter('=').startsWith("!") }
                .map { en.getString("k") + " " + it } }.take(20)))
    }

    fun notExpected(reason: String, snapshotPollTs: String?, sessionDate: String, recommendationId: Any?): JSONObject =
        empty(base("NOT_EXPECTED", snapshotPollTs, sessionDate, recommendationId).put("reason", reason))

    fun buildFailed(reason: String, snapshotPollTs: String?, sessionDate: String, recommendationId: Any?): JSONObject =
        empty(base("BUILD_FAILED", snapshotPollTs, sessionDate, recommendationId).put("reason", reason.take(80)))

    /**
     * B: the common manifest contract, checked for EVERY status before anything is certified. Returns problems
     * (empty = structurally valid). BUILD_FAILED may be structurally valid but is never certifiable.
     */
    fun validate(m: JSONObject): List<String> {
        val p = mutableListOf<String>()
        fun str(k: String) = m.opt(k) as? String
        if (str("schema") != SCHEMA) p += "schema"
        if (str("contract") != CONTRACT) p += "contract"
        if (str("columns") != COLUMN_CONTRACT) p += "column_contract"
        val status = str("status"); if (status !in STATUSES) p += "status"
        if (m.opt("sample_cap") !is Int || m.optInt("sample_cap") != GeneratedCandidateRows.PERSIST_CAP) p += "sample_cap"
        if (str("population") != POPULATION) p += "population"
        val sess = str("session_date"); if (sess == null || canonical(T.DATE, sess).startsWith("!")) p += "session_date"
        val lin = m.opt("snapshot_poll_ts")
        if (lin != null && lin != JSONObject.NULL && (lin !is String || instantOf(lin) == null)) p += "snapshot_poll_ts"
        val rec = m.opt("recommendation_id"); if (rec != null && rec != JSONObject.NULL && rec !is String) p += "recommendation_id"
        val count = m.opt("row_count") as? Int; if (count == null || count < 0) p += "row_count"
        val rows = m.opt("rows") as? JSONArray; if (rows == null) p += "rows"
        val per = m.opt("per_index") as? JSONObject
        if (per == null || per.keys().asSequence().toSet() != INDEXES.toSet() || INDEXES.any { per.opt(it) !is Int || per.optInt(it) < 0 }) p += "per_index"
        val colSha = m.opt("col_sha") as? JSONObject
        if (colSha == null || colSha.keys().asSequence().toSet() != COLUMNS.map { it.first }.toSet()) p += "col_sha"
        val te = m.opt("type_errors") as? JSONArray
        if (te == null) p += "type_errors_field" else if (te.length() > 0) p += "capture_value_type_errors"
        if (rows == null) return p
        val ent = (0 until rows.length()).map { rows.opt(it) as? JSONObject }
        if (ent.any { e -> e == null || e.opt("k") !is String || e.opt("i") !is String || (e.opt("w") as? String)?.matches(Regex("[0-9a-f]{64}")) != true }) { p += "row_entry_types"; return p }
        val es = ent.map { it!! }
        if (count != null && count != es.size) p += "row_count_vs_rows"
        if (es.size > GeneratedCandidateRows.PERSIST_CAP) p += "over_sample_cap"
        if (es.map { it.getString("k") }.toSet().size != es.size) p += "duplicate_identity"
        if (es.any { it.getString("i") !in INDEXES }) p += "index_key_not_allowed"
        if (per != null && INDEXES.any { perIndex(es).optInt(it) != per.optInt(it, -1) }) p += "per_index_vs_rows"
        if (str("cohort_sha256") != cohortSha(es)) p += "cohort_sha256"
        if (es != es.sortedWith(compareBy({ it.getString("k") }, { it.getString("i") }, { it.getString("w") }))) p += "entry_order"
        when (status) {
            "NOT_EXPECTED", "BUILD_FAILED" -> {
                if (es.isNotEmpty() || count != 0) p += "empty_status_with_rows"
                if (per != null && INDEXES.any { per.optInt(it) != 0 }) p += "empty_status_with_per_index"
                if (str("cohort_sha256") != cohortSha(emptyList())) p += "empty_status_digest"
                if (colSha != null && columnShas(emptyList()).let { e -> COLUMNS.any { (c, _) -> colSha.opt(c) != e.getString(c) } }) p += "empty_status_col_sha"
                if (status == "NOT_EXPECTED") {
                    val reason = str("reason")
                    if (reason !in EMPTY_REASONS) p += "empty_reason"
                    if (reason == "zero_candidates" && (lin == null || lin == JSONObject.NULL)) p += "zero_candidates_without_lineage"
                }
            }
            "EXPECTED" -> {
                if (es.isEmpty()) p += "expected_without_rows"
                if (lin == null || lin == JSONObject.NULL) p += "expected_without_lineage"
                if (m.has("reason")) p += "expected_with_reason"
            }
        }
        return p
    }
}
