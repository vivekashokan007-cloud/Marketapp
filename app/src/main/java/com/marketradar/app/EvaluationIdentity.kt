package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/** Exact identity only: never infer a database key from rank, nearest poll or row order. */
internal object EvaluationIdentity {
    fun positiveId(value: Any?): Long? = value?.toString()?.toLongOrNull()?.takeIf { it > 0 }

    private fun text(row: JSONObject, key: String): String =
        if (row.isNull(key)) "" else row.optString(key).trim()

    private fun instant(value: String) = try {
        OffsetDateTime.parse(value).toInstant()
    } catch (_: Exception) {
        try {
            OffsetDateTime.parse(value, DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ssZ")).toInstant()
        } catch (_: Exception) { null }
    }

    class SnapshotIndex(private val sessionDate: String, remote: JSONArray) {
        private val byTime = linkedMapOf<java.time.Instant, MutableList<JSONObject>>()

        init {
            for (i in 0 until remote.length()) {
                val row = remote.getJSONObject(i)
                val ts = instant(text(row, "poll_ts"))
                require(text(row, "session_date") == sessionDate && ts != null &&
                    ts.atZone(ZoneId.of("Asia/Kolkata")).toLocalDate().toString() == sessionDate &&
                    positiveId(row.opt("id")) != null) { "EVAL_IDENTITY_INVALID_REMOTE_ROW: row=$i" }
                byTime.getOrPut(ts) { mutableListOf() }.add(row)
            }
        }

        fun resolve(snapshot: JSONObject): Long {
            val ts = instant(text(snapshot, "poll_ts"))
            require(text(snapshot, "session_date") == sessionDate && ts != null) {
                "EVAL_IDENTITY_INVALID_SNAPSHOT: session or poll timestamp missing"
            }
            val recommendation = text(snapshot, "recommendation_id")
            val matches = byTime[ts].orEmpty().filter {
                recommendation.isEmpty() || text(it, "recommendation_id") == recommendation
            }
            val ids = matches.mapNotNull { positiveId(it.opt("id")) }.distinct()
            require(ids.size == 1) { "EVAL_IDENTITY_UNRESOLVED: poll=$ts matches=${ids.size}; local data retained" }
            val existing = positiveId(snapshot.opt("id"))
            require(existing == null || existing == ids.single()) {
                "EVAL_IDENTITY_CONFLICT: existing snapshot ID disagrees with remote poll; local data retained"
            }
            return ids.single()
        }
    }

    fun hasSnapshotIdentity(row: JSONObject, validIds: Set<Long>): Boolean =
        positiveId(row.opt("snapshot_id"))?.let { it in validIds } == true

    // Compare content independent of JSON object key order; conflicting duplicates
    // are errors, not a last-write-wins choice about teacher truth.
    private fun canonical(value: Any?): String = when (value) {
        null, JSONObject.NULL -> "null"
        is JSONObject -> value.keys().asSequence().toList().sorted().joinToString(",", "{", "}") {
            JSONObject.quote(it) + ":" + canonical(value.opt(it))
        }
        is JSONArray -> (0 until value.length()).joinToString(",", "[", "]") { canonical(value.opt(it)) }
        is String -> JSONObject.quote(value)
        else -> value.toString()
    }

    fun validatedDistinctOutcomes(sessionDate: String, body: JSONArray): JSONArray {
        val unique = linkedMapOf<List<String>, JSONObject>()
        for (i in 0 until body.length()) {
            val source = body.getJSONObject(i)
            val id = positiveId(source.opt("snapshot_id"))
            val candidate = text(source, "candidate_id")
            val role = text(source, "role").lowercase(java.util.Locale.US)
            require(id != null && candidate.isNotEmpty() &&
                text(source, "session_date") == sessionDate && role in setOf("primary", "secondary", "rejected")) {
                "EVAL_OUTCOME_IDENTITY_INVALID: row=$i; local results retained"
            }
            val key = listOf(id.toString(), candidate, role)
            val previous = unique[key]
            // Retain references, not another full copy/signature of thousands
            // of teacher rows. Only duplicates need a content comparison.
            require(previous == null || canonical(previous) == canonical(source)) {
                "EVAL_OUTCOME_IDENTITY_CONFLICT: snapshot=$id candidate=$candidate role=$role; local results retained"
            }
            if (previous == null) unique[key] = source
        }
        return JSONArray().also { out -> unique.values.forEach(out::put) }
    }
}
