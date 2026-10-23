package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * M0 Batch 1 W8 (COH-01 hooks; interface + tests only, the cohort builder is Stage 3).
 *
 * Frozen-cohort source preservation BY COPY under research_m0/cohorts/<id>/ (separate from raw manifests, envelopes
 * and maps, which are the only things the guarded pruners touch). A frozen cohort holds verified copies of:
 * selected-contract entry and exit marks (same strike, expiry and type), underlying references, the chain at O(T),
 * the entry quote, the forecast envelope and the calendar/lot/fee/rate versions.
 *
 * Replayability is decided ONLY from the cohort's own verified copies; a passed slot count never certifies it.
 * Bounded: [MAX_COHORTS] and [MAX_TOTAL_BYTES]; at the cap a new cohort is REJECTED_CAPACITY (no eviction).
 */
object ResearchCohorts {
    const val SUBDIR = "cohorts"
    @Volatile var MAX_COHORTS = 2000
    @Volatile var MAX_TOTAL_BYTES = 64L * 1024 * 1024
    val REQUIRED = listOf("ENTRY_MARK", "EXIT_MARK", "UNDERLYING_REF", "CHAIN_AT_OT", "ENTRY_QUOTE", "FORECAST_ENVELOPE", "VERSIONS")
    val VERSION_KEYS = listOf("calendar_version", "lot_version", "fee_version", "rate_version")
    private val lock = Any()

    data class Contract(val index: String, val expiry: String, val strike: Long, val type: String) {
        fun json() = JSONObject().put("index", index).put("expiry", expiry).put("strike", strike).put("type", type)
    }
    data class Item(val kind: String, val contract: Contract?, val content: String, val sourceRef: String)

    private fun root(dir: File) = File(dir, SUBDIR)
    private fun safe(s: String) = s.replace(Regex("[^A-Za-z0-9_.-]"), "_").take(80)
    private fun cdir(dir: File, id: String) = File(root(dir), safe(id))

    private fun totalBytes(dir: File): Long = root(dir).walkTopDown().filter { it.isFile }.sumOf { it.length() }

    /** Copy and seal every item; returns FROZEN, INCOMPLETE (stored, not replayable), EXISTS, or REJECTED_*. */
    fun freeze(dir: File, cohortId: String, items: List<Item>, nowMs: Long): String = synchronized(lock) {
        val cd = cdir(dir, cohortId)
        if (File(cd, "index.json").exists()) return "EXISTS"
        val n = root(dir).listFiles { f -> f.isDirectory }?.size ?: 0
        if (n >= MAX_COHORTS) return "REJECTED_CAPACITY"
        val entry = items.firstOrNull { it.kind == "ENTRY_MARK" }?.contract
        // B1-R5: serialize everything first and account the EXACT sealed UTF-8 bytes of every file to be written,
        // plus one transient temp copy of the largest file (atomic replace), before anything is committed.
        val idx = JSONArray()
        val bodies = items.mapIndexed { i, it ->
            val name = "item_$i.json"
            idx.put(JSONObject().put("file", name).put("kind", it.kind).put("sha256", M0Durable.sha256(it.content))
                .put("contract", it.contract?.json() ?: JSONObject.NULL))
            name to JSONObject().put("kind", it.kind).put("source_ref", it.sourceRef)
                .put("contract", it.contract?.json() ?: JSONObject.NULL).put("content", it.content).toString()
        }
        val check = evaluate(idx, entry)
        val indexBody = JSONObject().put("cohort_id", cohortId).put("frozen_ms", nowMs)
            .put("items", idx).put("entry_contract", entry?.json() ?: JSONObject.NULL).put("frozen_check", check).toString()
        // B1-R5b: an interrupted earlier freeze may have left files here. Only bytes that this freeze DEMONSTRABLY
        // replaces (a same-named target file that the atomic rename overwrites) are subtracted; every other leftover
        // (extra item files from a longer attempt, *.tmp / *.tmp.rejected temps, unknown files) keeps counting and is
        // never deleted. The cap is checked at the PEAK of the write sequence (old target + new temp coexist).
        val writes = bodies + ("index.json" to indexBody)
        var cur = totalBytes(dir)
        var peak = cur
        for ((name, body) in writes) {
            val newSize = M0Durable.sealedSize(name, body)
            val old = File(cd, name).let { if (it.isFile) it.length() else 0L }
            // a stale "<name>.tmp" is replaced by this write's temp; it is counted in cur already
            val staleTmp = File(cd, "$name.tmp").let { if (it.isFile) it.length() else 0L }
            peak = maxOf(peak, cur - staleTmp + newSize)          // temp fully written, old target still present
            cur = cur - staleTmp - old + newSize                   // after rename: old target replaced
        }
        if (peak > MAX_TOTAL_BYTES || cur > MAX_TOTAL_BYTES) return "REJECTED_CAPACITY"
        for ((name, body) in bodies) M0Durable.write(File(cd, name), body)
        M0Durable.write(File(cd, "index.json"), indexBody)
        if (check.optBoolean("complete")) "FROZEN" else "INCOMPLETE"
    }

    private fun evaluate(idx: JSONArray, entry: Contract?): JSONObject {
        val kinds = (0 until idx.length()).map { idx.getJSONObject(it).optString("kind") }.toSet()
        val missing = REQUIRED.filter { it !in kinds }
        val problems = JSONArray()
        if (entry == null) problems.put("ENTRY_CONTRACT_UNKNOWN")
        for (i in 0 until idx.length()) {
            val x = idx.getJSONObject(i)
            if (x.optString("kind") in setOf("ENTRY_MARK", "EXIT_MARK", "ENTRY_QUOTE")) {
                val c = x.optJSONObject("contract")
                if (entry == null || c == null || c.toString() != entry.json().toString()) problems.put("CONTRACT_MISMATCH:" + x.optString("kind"))
            }
        }
        return JSONObject().put("missing", JSONArray(missing)).put("problems", problems).put("complete", missing.isEmpty() && problems.length() == 0)
    }

    /** REPLAYABLE only when every required copy is present, verifies against its digest and names the same contract. */
    fun replayable(dir: File, cohortId: String): JSONObject = synchronized(lock) {
        val cd = cdir(dir, cohortId)
        val ix = M0Durable.read(File(cd, "index.json"))
        if (ix !is M0Durable.Read.Ok) return JSONObject().put("status", "NOT_REPLAYABLE").put("reason", if (ix is M0Durable.Read.Absent) "NO_COHORT_COPY" else "COHORT_INDEX_CORRUPT")
        val items = ix.json.getJSONArray("items")
        val bad = JSONArray()
        for (i in 0 until items.length()) {
            val x = items.getJSONObject(i)
            val r = M0Durable.read(File(cd, x.getString("file")))
            if (r !is M0Durable.Read.Ok || M0Durable.sha256(r.json.optString("content")) != x.optString("sha256")) bad.put(x.getString("file"))
            if (x.optString("kind") == "VERSIONS" && r is M0Durable.Read.Ok) {
                val v = try { JSONObject(r.json.optString("content")) } catch (_: Exception) { JSONObject() }
                VERSION_KEYS.filter { v.optString(it, "").isBlank() }.forEach { bad.put("VERSIONS_MISSING:$it") }
            }
        }
        val entry = ix.json.optJSONObject("entry_contract")?.let { Contract(it.getString("index"), it.getString("expiry"), it.getLong("strike"), it.getString("type")) }
        val check = evaluate(items, entry)
        val ok = check.optBoolean("complete") && bad.length() == 0
        return JSONObject().put("status", if (ok) "REPLAYABLE" else "NOT_REPLAYABLE").put("check", check).put("unverified", bad)
    }
}
