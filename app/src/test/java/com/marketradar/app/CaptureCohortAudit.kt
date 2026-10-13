package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.time.Instant

/**
 * b503 C3: GENERATED-CANDIDATE SAMPLED-COHORT audit, separate from (and consumed together with) the narrow
 * Pc2ParityAudit certificate in one readiness report.
 *
 * What a PASS certifies (and nothing more): for every snapshot of the IST day whose ORIGIN session is that day,
 * the stored ml_generated_candidates rows for its lineage (snapshot_poll_ts = the frozen fact-pack poll time,
 * recommendation_id, session_date) are exactly the capture-time expected bounded sample recorded in
 * poll_summary_json.generated_cohort_manifest (same row identities, per-index counts, index, content digest,
 * versions), every empty expectation carries an explicit original reason, and no generated row is orphaned.
 * It does NOT certify the full generator population (sample cap 50), C3 completeness or any source-cap gate:
 * those stay CLOSED in the readiness report regardless of this result.
 */
object CaptureCohortAudit {
    enum class Severity { FAIL, VISIBLE }
    data class Finding(val category: String, val severity: Severity, val key: String, val detail: String)

    class Report(val istDay: String, val findings: List<Finding>, val snapshots: Int, val verifiedCohorts: Int,
                 val emptyCohorts: Int, val generatedRows: Int) {
        val verified: Boolean get() = findings.none { it.severity == Severity.FAIL } && snapshots > 0
        fun count(category: String) = findings.count { it.category == category }
        fun toJson(): JSONObject = JSONObject()
            .put("schema", "capture_cohort_report_v1").put("ist_day", istDay)
            .put("certifies", "generated sampled cohort (<= ${GeneratedCandidateRows.PERSIST_CAP}/poll) matches the capture-time manifest; NOT full population, NOT C3")
            .put("snapshots", snapshots).put("verified_cohorts", verifiedCohorts).put("empty_cohorts", emptyCohorts)
            .put("generated_rows", generatedRows).put("sampled_cohort_verified", verified)
            .put("full_population_verified", false)
            .put("categories", JSONObject().also { o -> findings.groupBy { it.category }.toSortedMap().forEach { (k, l) ->
                o.put(k, JSONObject().put("fail", l.count { it.severity == Severity.FAIL }).put("visible", l.count { it.severity == Severity.VISIBLE })) } })
            .put("findings", JSONArray().also { a -> findings.forEach { a.put(JSONObject().put("category", it.category)
                .put("severity", it.severity.name).put("key", it.key).put("detail", it.detail)) } })
    }

    private fun ts(text: String?): Instant? = Pc2ParityAudit.parseInstant(text)

    fun audit(istDay: String, snapshots: List<JSONObject>, generated: List<JSONObject>, inputFaults: List<String> = emptyList()): Report {
        val f = mutableListOf<Finding>()
        fun fail(c: String, k: String, d: String) = f.add(Finding(c, Severity.FAIL, k, d))
        fun visible(c: String, k: String, d: String) = f.add(Finding(c, Severity.VISIBLE, k, d))
        inputFaults.forEach { fail("INPUT_INCOMPLETE", it.substringBefore(':'), it) }

        // Generated rows grouped by their lineage instant (the frozen fact-pack poll time, NOT completion).
        val byLineage = HashMap<Instant, MutableList<JSONObject>>()
        for (r in generated) {
            val t = ts(r.optString("snapshot_poll_ts_utc"))
            if (t == null) { fail("MALFORMED_TIMESTAMP", r.optString("snapshot_poll_ts_utc").ifEmpty { "-" }, "generated row lineage time invalid"); continue }
            byLineage.getOrPut(t) { mutableListOf() } += r
        }
        val claimed = HashSet<Instant>()
        var verified = 0; var empty = 0; var considered = 0

        for (s in snapshots) {
            val key = s.optString("poll_ts_utc").ifEmpty { "-" }
            val provRaw = if (s.has("capture_provenance")) s.opt("capture_provenance") else null
            val prov = CaptureSlotReconciler.validate(provRaw, s.optString("session_date").ifEmpty { null }, ts(key)?.epochSecond)
            val p = (prov as? CaptureSlotReconciler.Validation.Valid)?.provenance
            if (p != null && p.sessionDate != istDay) continue // audited with its origin session
            considered++
            val m = s.optJSONObject("generated_cohort_manifest")
            if (m == null) {
                if (prov is CaptureSlotReconciler.Validation.Absent) fail("COHORT_UNVERIFIABLE_PRE_B503", key, "no capture-time cohort manifest (pre-b503 snapshot); generated cohort cannot be verified")
                else fail("MISSING_COHORT_MANIFEST", key, "b503 snapshot without generated_cohort_manifest")
                continue
            }
            if (prov is CaptureSlotReconciler.Validation.Invalid) { fail("INVALID_CAPTURE_PROVENANCE", key, prov.reason); continue }
            if (m.optString("schema") != GeneratedCohort.SCHEMA) { fail("COHORT_WRONG_VERSION", key, "manifest schema '${m.optString("schema")}'"); continue }
            val status = m.optString("status")
            val sess = m.optString("session_date")
            if (sess != istDay || (s.optString("session_date").isNotEmpty() && s.optString("session_date") != sess)) {
                fail("COHORT_LINEAGE_MISMATCH", key, "manifest session '$sess', snapshot session '${s.optString("session_date")}', day '$istDay'"); continue
            }
            val lineage = if (m.isNull("snapshot_poll_ts")) null else ts(m.optString("snapshot_poll_ts"))
            val slot0 = p?.slotStartEpochSec
            if (slot0 != null && lineage != null &&
                (lineage.epochSecond < slot0 || lineage.epochSecond >= slot0 + FrozenCaptureRequest.POLL_SLOT_MINUTES * 60)) {
                fail("COHORT_LINEAGE_MISMATCH", key, "manifest lineage $lineage is outside origin slot ${p.captureKey}"); continue
            }
            val snapRec = if (s.isNull("recommendation_id")) null else s.optString("recommendation_id").ifEmpty { null }
            val manRec = if (m.isNull("recommendation_id")) null else m.optString("recommendation_id").ifEmpty { null }
            if (s.has("recommendation_id") && snapRec != manRec) { fail("COHORT_LINEAGE_MISMATCH", key, "snapshot recommendation '$snapRec' vs manifest '$manRec'"); continue }
            if (lineage != null && !claimed.add(lineage)) { fail("DUPLICATE_COHORT_LINEAGE", lineage.toString(), "two snapshots claim one generated lineage"); continue }
            val rows = lineage?.let { byLineage[it] }.orEmpty()
            when (status) {
                "BUILD_FAILED" -> { fail("GENERATED_BUILD_FAILED", key, "generation failed at capture: ${m.optString("reason")}"); continue }
                "NOT_EXPECTED" -> {
                    val reason = m.optString("reason")
                    if (reason !in GeneratedCohort.EMPTY_REASONS) { fail("INVALID_COHORT_MANIFEST", key, "empty expectation without a legitimate original reason ('$reason')"); continue }
                    if (rows.isNotEmpty()) { fail("COHORT_EXTRA_ROWS", key, "${rows.size} rows stored for an empty expectation ($reason)"); continue }
                    empty++; verified++; continue
                }
                "EXPECTED" -> {}
                else -> { fail("INVALID_COHORT_MANIFEST", key, "unknown status '$status'"); continue }
            }
            if (lineage == null) { fail("INVALID_COHORT_MANIFEST", key, "EXPECTED cohort without snapshot_poll_ts"); continue }
            val exp = m.optJSONArray("rows")
            val expEntries = (0 until (exp?.length() ?: 0)).mapNotNull { exp!!.optJSONObject(it) }
            val per = m.optJSONObject("per_index")
            val consistent = exp != null && expEntries.size == m.optInt("row_count", -1) && expEntries.isNotEmpty() &&
                GeneratedCohort.cohortSha(expEntries) == m.optString("cohort_sha256") &&
                per != null && GeneratedCohort.perIndex(expEntries).let { calc ->
                    (calc.keys().asSequence() + per.keys().asSequence()).toSet().all { k -> calc.optInt(k) == per.optInt(k, -1) || (calc.optInt(k) == 0 && !per.has(k)) } } &&
                expEntries.size <= m.optInt("sample_cap", GeneratedCandidateRows.PERSIST_CAP)
            if (!consistent) { fail("INVALID_COHORT_MANIFEST", key, "manifest rows/count/per_index/sha are inconsistent"); continue }
            if (rows.isEmpty()) { fail("GENERATED_COHORT_ABSENT", key, "expected ${expEntries.size} rows, none stored for lineage $lineage"); continue }
            val before = f.count { it.severity == Severity.FAIL }
            val got = GeneratedCohort.entries(rows)
            val dupIds = got.groupBy { it.getString("k") }.filter { it.value.size > 1 }.keys
            if (dupIds.isNotEmpty()) fail("COHORT_DUPLICATE_ROWS", key, "duplicate stored identities: ${dupIds.take(3)}")
            val expByK = expEntries.associateBy { it.getString("k") }
            val gotByK = got.associateBy { it.getString("k") }
            val missing = expByK.keys - gotByK.keys
            val extra = gotByK.keys - expByK.keys
            if (missing.isNotEmpty()) fail("COHORT_ROWS_MISSING", key, "${missing.size} of ${expByK.size} expected rows absent: ${missing.take(3)}")
            if (extra.isNotEmpty()) fail("COHORT_EXTRA_ROWS", key, "${extra.size} unexpected rows: ${extra.take(3)}")
            for (k in expByK.keys intersect gotByK.keys) {
                val e = expByK.getValue(k); val g = gotByK.getValue(k)
                if (e.getString("i") != g.getString("i")) fail("COHORT_WRONG_INDEX", key, "$k expected ${e.getString("i")} stored ${g.getString("i")}")
                else if (e.getString("c") != g.getString("c")) fail("COHORT_CONTENT_CONFLICT", key, "$k content digest differs")
            }
            rows.forEach { r ->
                val rr = if (r.isNull("recommendation_id")) null else r.optString("recommendation_id")
                if (rr != manRec || r.optString("session_date") != sess) {
                    fail("COHORT_LINEAGE_MISMATCH", key, "row ${GeneratedCohort.identity(r)} belongs to recommendation '$rr' / ${r.optString("session_date")}"); return@forEach
                }
            }
            if (f.count { it.severity == Severity.FAIL } == before) verified++
        }
        byLineage.keys.filter { it !in claimed }.sorted().forEach {
            fail("ORPHAN_GENERATED_ROWS", it.toString(), "${byLineage.getValue(it).size} generated rows match no snapshot cohort manifest")
        }
        if (considered == 0) fail("NO_SNAPSHOTS", istDay, "no snapshot of this origin session: nothing to verify")
        visible("SAMPLE_NOT_POPULATION", istDay, "cohorts are the bounded ${GeneratedCandidateRows.PERSIST_CAP}-row sample; full population / C3 not verified")
        return Report(istDay, f, considered, verified, empty, generated.size)
    }

    private fun sha256Hex(b: ByteArray) = MessageDigest.getInstance("SHA-256").digest(b).joinToString("") { "%02x".format(it) }
    private fun readJsonl(file: File) = file.readText(Charsets.UTF_8).split('\n').filter { it.isNotBlank() }.map { JSONObject(it) }

    /** Reads an export directory (tools/pc2_parity_export.sh), writes capture_cohort_report.json. */
    fun run(directory: File): Report {
        val faults = mutableListOf<String>()
        val manifest = runCatching { JSONObject(File(directory, Pc2ParityAudit.MANIFEST).readText()) }.getOrNull()
        if (manifest == null || !manifest.optBoolean("complete")) faults += "manifest.json: missing or incomplete"
        val data = HashMap<String, List<JSONObject>>()
        for (name in listOf("snapshots.jsonl", "generated.jsonl")) {
            val file = File(directory, name)
            if (!file.isFile) { faults += "$name: missing (generated channel unexamined)"; data[name] = emptyList(); continue }
            val rows = runCatching { readJsonl(file) }
            if (rows.isFailure) faults += "$name: a line is not a JSON object"
            data[name] = rows.getOrDefault(emptyList())
            val declared = manifest?.optJSONObject("files")?.optJSONObject(name)
            if (declared == null) faults += "$name: not declared in the manifest"
            else {
                if (declared.optInt("rows", -1) != data.getValue(name).size) faults += "$name: row count differs from the manifest"
                if (declared.optString("sha256") != sha256Hex(file.readBytes())) faults += "$name: SHA-256 differs from the manifest"
            }
        }
        val report = audit(manifest?.optString("ist_day").orEmpty(), data.getValue("snapshots.jsonl"), data.getValue("generated.jsonl"), faults)
        File(directory, "capture_cohort_report.json").writeText(report.toJson().toString(2))
        return report
    }
}

/**
 * b503 C3: one readiness report consuming both audits. Each line states exactly what it certifies; full
 * population (C3) and every source-cap gate stay CLOSED here by construction.
 */
object CaptureReadiness {
    fun report(pc2: Pc2ParityAudit.Report, cohort: CaptureCohortAudit.Report): JSONObject = JSONObject()
        .put("schema", "capture_readiness_v1").put("ist_day", cohort.istDay)
        .put("pc2_parity_certified", pc2.certified)
        .put("pc2_parity_certifies", "snapshot/compact/retained-legacy PC2 decisions vs independent chain inventory")
        .put("generated_sampled_cohort_verified", cohort.verified)
        .put("generated_sampled_cohort_certifies", "stored generated rows == capture-time expected bounded sample (<= ${GeneratedCandidateRows.PERSIST_CAP}/poll)")
        .put("capture_cohort_complete", pc2.certified && cohort.verified)
        .put("full_population_verified", false)
        .put("c3_gate", "CLOSED").put("source_cap_gate", "CLOSED")
        .put("gate_reason", "a 50-row sampled cohort is not the full generator population; C3/source-cap need separate evidence")

    fun run(directory: File): JSONObject {
        val pc2 = Pc2ParityAudit.run(directory)
        val cohort = CaptureCohortAudit.run(directory)
        return report(pc2, cohort).also { File(directory, "capture_readiness_report.json").writeText(it.toString(2)) }
    }
}
