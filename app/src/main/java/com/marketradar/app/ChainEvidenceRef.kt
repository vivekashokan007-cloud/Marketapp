package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject

/**
 * Evidence batch 1, correction R1: the ONE explicit Kotlin -> Python contract for the pre-Brain chain
 * persistence reference (`chain_evidence_ref_v1`).
 *
 * Producer: MarketWatchService.persistFetchedChainEvidenceBeforeBrain builds it with [build] from the exact
 * rows it posted. Consumer: paper_menu_evidence.parse_chain_evidence_ref (Python). Both sides are tested
 * against the same golden fixture `app/src/test/resources/evidence_batch1/chain_evidence_ref_v1.json`.
 *
 * ctx["chain_evidence_ref"] (small; kept by the Brain snapshot compactor):
 *   schema           "chain_evidence_ref_v1"
 *   table            "ml_option_chain_snapshots" - the ONLY table quote_refs point to
 *   poll_ts          the exact string written into every row's poll_ts (join key with index_key, strike,
 *                    option_type; ml_ocs_unique). NOT named chain_poll_ts.
 *   rows             number of rows posted
 *   persist_status   SAVED_PRIMARY | SAVED_FALLBACK_TABLE | FAILED | NO_ROWS
 *   persist_table    table that acknowledged the write, or null
 *   saved            true ONLY for SAVED_PRIMARY (rows exist in `table`); a fallback-table save is false
 *   coverage_keys    number of distinct (index_key|expiry|option_type|strike) keys posted
 *   coverage_sha256  sha256 of the canonical coverage object below
 * ctx["chain_evidence_coverage"] (per-poll only; NOT kept by the snapshot compactor):
 *   { "<index_key>|<expiry>": { "CE": [ascending int strikes], "PE": [...] } }
 *
 * Evidence only: nothing in ranking, gates, sizing or exits reads either object.
 */
object ChainEvidenceRef {
    const val SCHEMA = "chain_evidence_ref_v1"
    const val TABLE = "ml_option_chain_snapshots"
    const val SAVED_PRIMARY = "SAVED_PRIMARY"
    const val SAVED_FALLBACK_TABLE = "SAVED_FALLBACK_TABLE"
    const val FAILED = "FAILED"
    const val NO_ROWS = "NO_ROWS"

    /** [persistTable] is the table that acknowledged the write (null when nothing did). */
    fun persistStatus(rowCount: Int, persistTable: String?): String = when {
        rowCount == 0 -> NO_ROWS
        persistTable == null -> FAILED
        persistTable.substringBefore('?') == TABLE -> SAVED_PRIMARY
        else -> SAVED_FALLBACK_TABLE
    }

    fun coverage(rows: List<JSONObject>): JSONObject {
        val keys = sortedMapOf<String, Pair<java.util.TreeSet<Int>, java.util.TreeSet<Int>>>()
        for (r in rows) {
            val strike = r.optInt("strike", 0)
            if (strike <= 0) continue
            val k = "${r.optString("index_key")}|${r.optString("expiry")}"
            val p = keys.getOrPut(k) { java.util.TreeSet<Int>() to java.util.TreeSet<Int>() }
            when (r.optString("option_type")) {
                "CE" -> p.first.add(strike)
                "PE" -> p.second.add(strike)
            }
        }
        val out = JSONObject()
        for ((k, p) in keys) {
            out.put(k, JSONObject().put("CE", JSONArray(p.first.toList())).put("PE", JSONArray(p.second.toList())))
        }
        return out
    }

    fun coverageKeyCount(coverage: JSONObject): Int =
        coverage.keys().asSequence().sumOf { k ->
            val o = coverage.getJSONObject(k)
            o.getJSONArray("CE").length() + o.getJSONArray("PE").length()
        }

    /** Returns (ref, coverage). [rows] are exactly the rows that were posted under [pollTs]. */
    fun build(pollTs: String, rows: List<JSONObject>, persistTable: String?): Pair<JSONObject, JSONObject> {
        val status = persistStatus(rows.size, persistTable)
        val cov = coverage(rows)
        val ref = JSONObject()
            .put("schema", SCHEMA)
            .put("table", TABLE)
            .put("poll_ts", pollTs)
            .put("rows", rows.size)
            .put("persist_status", status)
            .put("persist_table", persistTable?.substringBefore('?') ?: JSONObject.NULL)
            .put("saved", status == SAVED_PRIMARY)
            .put("coverage_keys", coverageKeyCount(cov))
            .put("coverage_sha256", Pc2CompactBatch.sha256(Pc2CompactBatch.canonicalJson(cov)))
        return ref to cov
    }
}
