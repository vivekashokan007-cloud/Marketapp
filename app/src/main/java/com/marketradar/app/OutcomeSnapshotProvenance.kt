package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject

/**
 * G1 (Codex review R3, 4 Nov 2026): the explicit, durable outcome-to-snapshot JOIN CONTRACT.
 *
 * Final server outcome rows (ml_evaluation_outcomes / ml_recommendation_outcomes, built by
 * SupabaseClient.buildEvaluationRows / buildRecommendationRows) deliberately carry NO authority, construction or
 * selector lineage columns, and no server column is added for them. Instead each row carries the identity triple
 * that is already in the outcome allow-list:
 *
 *     (snapshot_id, session_date, candidate_id)
 *
 * snapshot_id is the ml_brain_snapshots primary key that the evaluator read back from the server
 * (SupabaseClient.fetchEvaluationSnapshots selects `id,...` and the teacher stamps `snapshot_id = snap.id`);
 * candidate_id is the candidate `id` inside that uploaded snapshot's context_json.snapshot_generated_candidates
 * (or primary_candidate_json / top_candidates_json). Joining a final row to that uploaded snapshot recovers the
 * exact policy / construction / decision metadata from the snapshot payload itself. This object is that join; it
 * is pure (no I/O) and is not on any runtime write path.
 *
 * See docs/design/G1_OUTCOME_SNAPSHOT_JOIN_CONTRACT.md for the fields that remain local / Python-only.
 */
internal object OutcomeSnapshotProvenance {
    const val CONTRACT = "mr.outcome_snapshot_join.v1"

    private fun obj(raw: Any?): JSONObject? = when (raw) {
        is JSONObject -> raw
        is String -> raw.trim().takeIf { it.startsWith("{") }?.let { runCatching { JSONObject(it) }.getOrNull() }
        else -> null
    }

    private fun arr(raw: Any?): JSONArray? = when (raw) {
        is JSONArray -> raw
        is String -> raw.trim().takeIf { it.startsWith("[") }?.let { runCatching { JSONArray(it) }.getOrNull() }
        else -> null
    }

    private fun str(o: JSONObject?, key: String): String =
        if (o == null || !o.has(key) || o.isNull(key)) "" else o.opt(key).toString().trim()

    private fun fail(status: String, detail: String): JSONObject =
        JSONObject().put("contract", CONTRACT).put("status", status).put("detail", detail)

    /** Candidates in an uploaded snapshot whose id equals [candidateId], from every place the snapshot stores them. */
    private fun matches(snapshot: JSONObject, ctx: JSONObject, candidateId: String): List<Pair<String, JSONObject>> {
        val out = mutableListOf<Pair<String, JSONObject>>()
        arr(ctx.opt("snapshot_generated_candidates"))?.let { a ->
            for (i in 0 until a.length()) a.optJSONObject(i)?.takeIf { str(it, "id") == candidateId }
                ?.let { out += "context_json.snapshot_generated_candidates" to it }
        }
        obj(snapshot.opt("primary_candidate_json"))?.takeIf { str(it, "id") == candidateId }
            ?.let { out += "primary_candidate_json" to it }
        arr(snapshot.opt("top_candidates_json"))?.let { a ->
            for (i in 0 until a.length()) a.optJSONObject(i)?.takeIf { str(it, "id") == candidateId }
                ?.let { out += "top_candidates_json" to it }
        }
        return out
    }

    /**
     * Resolve one FINAL server outcome row against the uploaded snapshot it names.
     * status: RESOLVED | SNAPSHOT_MISMATCH | SESSION_MISMATCH | CANDIDATE_NOT_FOUND | AMBIGUOUS_CANDIDATE |
     * ROW_SNAPSHOT_DISAGREE (index/type/role in the row contradict the snapshot).
     */
    fun resolve(row: JSONObject, snapshot: JSONObject): JSONObject {
        val sid = str(row, "snapshot_id")
        if (sid.isEmpty() || sid != str(snapshot, "id")) return fail("SNAPSHOT_MISMATCH", "row snapshot_id=$sid snapshot id=${str(snapshot, "id")}")
        val rowDate = str(row, "session_date").take(10)
        val snapDate = str(snapshot, "session_date").take(10)
        if (rowDate.isEmpty() || (snapDate.isNotEmpty() && rowDate != snapDate)) return fail("SESSION_MISMATCH", "row=$rowDate snapshot=$snapDate")
        val cid = str(row, "candidate_id")
        if (cid.isEmpty()) return fail("CANDIDATE_NOT_FOUND", "row has no candidate_id")
        val ctx = obj(snapshot.opt("context_json")) ?: JSONObject()
        val found = matches(snapshot, ctx, cid)
        if (found.isEmpty()) return fail("CANDIDATE_NOT_FOUND", "candidate $cid not in snapshot $sid")
        // Same id in several places must describe the same construction (lineage + structure).
        val lineageKeys = EvaluationLocalCache.AUTHORITY_LINEAGE_KEYS.toList() + listOf("index", "type", "isCredit", "netPremium")
        val signature = { c: JSONObject -> lineageKeys.filter { c.has(it) && !c.isNull(it) }.associateWith { c.opt(it).toString() } }
        val base = signature(found.first().second)
        for ((_, c) in found.drop(1)) {
            val sig = signature(c)
            val conflict = sig.keys.intersect(base.keys).any { sig[it] != base[it] }
            if (conflict) return fail("AMBIGUOUS_CANDIDATE", "candidate $cid has conflicting copies in snapshot $sid")
        }
        val cand = found.firstOrNull { it.first == "context_json.snapshot_generated_candidates" }?.second ?: found.first().second
        val index = str(cand, "index").ifEmpty { str(cand, "index_key") }.uppercase()
        val type = str(cand, "type").ifEmpty { str(cand, "strategy_type") }
        val rowIndex = str(row, "index_key").uppercase()
        val rowType = str(row, "strategy_type")
        if ((rowIndex.isNotEmpty() && index.isNotEmpty() && rowIndex != index) || (rowType.isNotEmpty() && type.isNotEmpty() && rowType != type)) {
            return fail("ROW_SNAPSHOT_DISAGREE", "row $rowIndex/$rowType vs snapshot $index/$type")
        }
        val primaryId = str(obj(snapshot.opt("primary_candidate_json")), "id")
        if (str(row, "role").equals("primary", ignoreCase = true) && primaryId != cid) {
            return fail("ROW_SNAPSHOT_DISAGREE", "row role=primary but snapshot primary is '$primaryId'")
        }

        val lineage = JSONObject()
        for (k in lineageKeys) if (cand.has(k) && !cand.isNull(k)) lineage.put(k, cand.opt(k))
        val out = JSONObject()
            .put("contract", CONTRACT)
            .put("status", "RESOLVED")
            .put("join", JSONObject().put("snapshot_id", sid).put("session_date", rowDate).put("candidate_id", cid)
                .put("poll_ts", snapshot.opt("poll_ts") ?: JSONObject.NULL))
            .put("candidate_source", JSONArray(found.map { it.first }.distinct()))
            .put("candidate_lineage", lineage)
            .put("is_snapshot_primary", primaryId == cid)

        obj(ctx.opt("snapshot_index_authority_primary"))?.let { rec ->
            val byIx = obj(rec.opt("by_index"))?.let { obj(it.opt(index)) }
            val r = JSONObject()
            for (k in listOf("record_schema", "primary_policy", "primary_reason")) r.put(k, rec.opt(k) ?: JSONObject.NULL)
            if (byIx != null) {
                for (k in listOf("decision_id", "action")) r.put(k, byIx.opt(k) ?: JSONObject.NULL)
                val ids = arr(byIx.opt("candidate_ids"))
                r.put("candidate_in_authority_menu", ids != null && (0 until ids.length()).any { ids.optString(it) == cid })
            }
            // indexDecisionId on the candidate must equal the per-index decision in the same snapshot.
            val did = str(cand, "indexDecisionId")
            if (did.isNotEmpty() && byIx != null) r.put("decision_id_matches_candidate", did == str(byIx, "decision_id"))
            out.put("index_authority", r)
        }
        obj(ctx.opt("snapshot_managed_policy"))?.let { mp ->
            val byIx = obj(mp.opt("by_index"))?.let { obj(it.opt(index)) }
            val m = JSONObject().put("schema", mp.opt("schema") ?: JSONObject.NULL)
            if (byIx != null) {
                for (k in listOf("policy_id", "reason", "admissible_count", "selected_candidate_id")) m.put(k, byIx.opt(k) ?: JSONObject.NULL)
                arr(byIx.opt("gates"))?.let { g ->
                    (0 until g.length()).mapNotNull { g.optJSONObject(it) }.firstOrNull { str(it, "candidate_id") == cid }
                        ?.let { m.put("candidate_gate", it) }
                }
            }
            out.put("managed_policy", m)
        }
        return out
    }
}
