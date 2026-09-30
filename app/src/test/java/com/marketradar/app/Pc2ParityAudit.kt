package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * DB-1 parity audit (Codex round-2 finding B3).
 *
 * Certifies exact reconstruction from the STORED BYTES using the real compactor
 * and the pinned canonicalisation contract - the check SQL alone cannot make.
 * Read-only: it consumes three JSON-Lines exports produced by
 * `tools/pc2_parity_export.sh` and never touches a database.
 *
 * Batches are matched by the identity each snapshot references, never by poll
 * time, because reruns may legitimately store more than one batch per poll.
 *
 * For every snapshot in the explicit IST-day window it proves, in order:
 *
 *  1. the snapshot reference is self-consistent (its nine identity fields hash
 *     to its batch id);
 *  2. a source array exists (the snapshot's own array, or the legacy table's
 *     rows ordered by decision_index when the snapshot dropped it for budget)
 *     and, when both exist, that they agree;
 *  3. the source array digests to the referenced decision digest, and the real
 *     compactor rebuilds exactly the referenced batch id from it;
 *  4. the referenced batch is stored, its bytes hash to its grouped digest, its
 *     stored identity is self-consistent, it reconstructs structurally (unique,
 *     complete, in-range indexes), the reconstruction digests to BOTH the stored
 *     and the referenced decision digest, it equals the source array element by
 *     element, and its completeness metadata and counts match the reference.
 *
 * Findings are FAIL (blocks certification) or VISIBLE (reported, does not
 * block): extra batches such as reruns, duplicate references, capped sources,
 * polls without PC2 evidence. Capping is reported separately and is never
 * treated as full-poll evidence.
 *
 * Known conservative limit: the source array is re-read from Postgres jsonb and
 * canonicalised on the JVM. Android and the JVM parse numbers through different
 * types; the analysis in Pc2CompactBatch shows canonical output is idempotent
 * across both, but a divergence on some exotic number would appear here as
 * SOURCE_DIGEST_MISMATCH / EXPECTED_ID_MISMATCH - a false alarm, never a false
 * certification. Stored-byte checks do not depend on number parsing at all.
 */
object Pc2ParityAudit {

    enum class Severity { FAIL, VISIBLE }

    data class Finding(val category: String, val severity: Severity, val key: String, val detail: String)

    data class Report(
        val snapshots: Int,
        val referencedBatches: Int,
        val storedBatches: Int,
        val certifiedPolls: Int,
        val cappedPolls: Int,
        val sourceFromSnapshot: Int,
        val sourceFromLegacy: Int,
        val findings: List<Finding>
    ) {
        val certified: Boolean get() = findings.none { it.severity == Severity.FAIL }

        fun count(category: String): Int = findings.count { it.category == category }

        fun toJson(): JSONObject = JSONObject()
            .put("certified", certified)
            .put("snapshots", snapshots)
            .put("referenced_batches", referencedBatches)
            .put("stored_batches", storedBatches)
            .put("certified_polls", certifiedPolls)
            .put("capped_polls", cappedPolls)
            .put("source_from_snapshot", sourceFromSnapshot)
            .put("source_from_legacy", sourceFromLegacy)
            .put("categories", JSONObject().also { summary ->
                findings.groupBy { it.category }.toSortedMap().forEach { (category, list) ->
                    summary.put(category, JSONObject()
                        .put("severity", list.first().severity.name)
                        .put("count", list.size))
                }
            })
            .put("findings", JSONArray().also { array ->
                findings.forEach {
                    array.put(JSONObject().put("category", it.category).put("severity", it.severity.name)
                        .put("key", it.key).put("detail", it.detail))
                }
            })
    }

    /**
     * @param snapshots rows: poll_ts_utc, session_date, compact_ref, decisions, policy, brain_version
     * @param batches   rows of public.ml_pc2_decision_batches
     * @param legacy    rows: poll_ts_utc, decision_index, decision_json
     */
    fun audit(snapshots: List<JSONObject>, batches: List<JSONObject>, legacy: List<JSONObject>): Report {
        val findings = mutableListOf<Finding>()
        fun fail(category: String, key: String, detail: String) =
            findings.add(Finding(category, Severity.FAIL, key, detail))
        fun visible(category: String, key: String, detail: String) =
            findings.add(Finding(category, Severity.VISIBLE, key, detail))

        // ---- stored batches, keyed by identity ----------------------------
        val stored = LinkedHashMap<String, JSONObject>()
        for (row in batches) {
            val id = row.optString("batch_id")
            if (stored.containsKey(id)) fail("DUPLICATE_BATCH_ROW", id, "batch id exported twice")
            stored[id] = row
        }

        // ---- legacy source arrays, validated as a source ------------------
        val legacyByPoll = legacy.groupBy { it.optString("poll_ts_utc") }
        val legacyArrays = HashMap<String, JSONArray>()
        for ((poll, rows) in legacyByPoll) {
            val sorted = rows.sortedBy { it.optInt("decision_index", -1) }
            val indexes = sorted.map { it.optInt("decision_index", -1) }
            if (indexes != (0 until sorted.size).toList()) {
                fail("LEGACY_INDEX_DEFECT", poll, "decision_index is not exactly 0..${sorted.size - 1}")
                continue
            }
            legacyArrays[poll] = JSONArray().also { a -> sorted.forEach { a.put(it.opt("decision_json")) } }
        }

        // ---- snapshots ------------------------------------------------------
        val snapshotPolls = HashMap<String, Int>()
        val referenced = HashMap<String, String>()          // batch_id -> poll
        var certifiedPolls = 0
        var capped = 0
        var fromSnapshot = 0
        var fromLegacy = 0

        for (snapshot in snapshots) {
            val poll = snapshot.optString("poll_ts_utc")
            snapshotPolls.merge(poll, 1, Int::plus)
            val ref = snapshot.optJSONObject("compact_ref")
            val snapshotArray = snapshot.optJSONArray("decisions")?.takeIf { it.length() > 0 }
            val legacyArray = legacyArrays[poll]

            if (ref == null) {
                if (snapshotArray != null || legacyArray != null) {
                    fail("NO_COMPACT_REF", poll, "PC2 evidence exists but the snapshot references no compact batch")
                } else {
                    visible("NO_PC2_EVIDENCE", poll, "no decisions and no compact reference")
                }
                continue
            }
            val refId = ref.optString("batch_id")
            val failuresBefore = findings.count { it.severity == Severity.FAIL }

            // 1. reference self-consistency
            val refIdentity = identityOf(ref)
            if (refIdentity == null || refIdentity != refId) {
                fail("REF_IDENTITY_INCONSISTENT", poll, "reference fields do not hash to $refId")
            }
            referenced[refId]?.let { other ->
                visible("DUPLICATE_REFERENCE", poll, "batch $refId is also referenced by $other")
            }
            referenced[refId] = poll
            if (ref.optBoolean("source_possibly_truncated", false)) {
                capped += 1
                visible("CAPPED_SOURCE", poll,
                    "source array is at the ${ref.optInt("source_tail_cap")}-decision tail cap; " +
                        "lossless for the array, not full-poll gate evidence")
            }

            // 2. source array
            val source = when {
                snapshotArray != null -> { fromSnapshot += 1; snapshotArray }
                legacyArray != null -> { fromLegacy += 1; legacyArray }
                else -> null
            }
            if (source == null) {
                fail("SOURCE_UNAVAILABLE", poll, "neither the snapshot nor the legacy table holds the array")
            }
            if (snapshotArray != null && legacyArray != null &&
                Pc2CompactBatch.digestOrderedDecisions(snapshotArray) !=
                Pc2CompactBatch.digestOrderedDecisions(legacyArray)
            ) {
                fail("LEGACY_SNAPSHOT_DISAGREE", poll, "the two source copies differ")
            }

            // 3. source -> reference
            if (source != null) {
                val sourceDigest = Pc2CompactBatch.digestOrderedDecisions(source)
                if (sourceDigest != ref.optString("decision_digest")) {
                    fail("SOURCE_DIGEST_MISMATCH", poll, "source $sourceDigest vs reference ${ref.optString("decision_digest")}")
                }
                if (source.length() != ref.optInt("decision_count", -1)) {
                    fail("COUNT_MISMATCH", poll, "source has ${source.length()} decisions, reference ${ref.optInt("decision_count")}")
                }
                val rebuilt = runCatching {
                    Pc2CompactBatch.build(
                        JSONObject()
                            .put("session_date", ref.optString("session_date_text"))
                            .put("poll_ts", ref.optString("poll_ts_text"))
                            .put("context_json", JSONObject()
                                .put("snapshot_pc2_authority_decisions", source)
                                .put("snapshot_pc2_authority_policy", snapshot.optJSONObject("policy") ?: JSONObject())
                                .put("snapshot_brain_version", snapshot.opt("brain_version") ?: JSONObject.NULL))
                    )
                }.getOrNull()
                val rebuiltId = rebuilt?.batchRow?.optString("batch_id")
                if (rebuiltId != refId) {
                    fail("EXPECTED_ID_MISMATCH", poll, "the compactor rebuilds $rebuiltId from the source, reference says $refId")
                }
            }

            // 4. stored batch
            val row = stored[refId]
            if (row == null) {
                fail("MISSING_BATCH", poll, "referenced batch $refId is not stored")
            } else {
                checkStored(poll, refId, ref, row, source, ::fail)
            }

            if (findings.count { it.severity == Severity.FAIL } == failuresBefore) certifiedPolls += 1
        }

        snapshotPolls.filterValues { it > 1 }.forEach { (poll, n) ->
            visible("DUPLICATE_SNAPSHOT_POLL", poll, "$n snapshot rows for one poll")
        }
        stored.forEach { (id, row) ->
            if (!referenced.containsKey(id)) {
                val poll = row.optString("poll_ts_text")
                visible("EXTRA_BATCH", id,
                    "stored but referenced by no snapshot in the window (poll $poll) - a rerun or an unexpected write")
                // Unreferenced rows are still evidence: a structurally broken one
                // means the table constraints were bypassed, which blocks
                // certification even though no snapshot points at it.
                val bytesOk = Pc2CompactBatch.sha256(row.optString("grouped_canonical", "")) ==
                    row.optString("grouped_digest")
                val identityOk = identityOf(row) == id
                val reconstructs = runCatching {
                    Pc2CompactBatch.digestOrderedDecisions(Pc2CompactBatch.reconstructOrderedDecisions(row)) ==
                        row.optString("decision_digest")
                }.getOrDefault(false)
                if (!bytesOk || !identityOk || !reconstructs) {
                    fail("STORED_ROW_NOT_RECONSTRUCTABLE", id,
                        "bytesOk=$bytesOk identityOk=$identityOk reconstructs=$reconstructs")
                }
            }
        }
        legacyByPoll.keys.filter { it !in snapshotPolls }.forEach {
            visible("LEGACY_WITHOUT_SNAPSHOT", it, "legacy rows for a poll with no snapshot")
        }

        return Report(
            snapshots = snapshots.size,
            referencedBatches = referenced.size,
            storedBatches = stored.size,
            certifiedPolls = certifiedPolls,
            cappedPolls = capped,
            sourceFromSnapshot = fromSnapshot,
            sourceFromLegacy = fromLegacy,
            findings = findings.also { list -> list.sortBy { "${it.severity}|${it.category}|${it.key}" } }
        )
    }

    private fun checkStored(
        poll: String,
        refId: String,
        ref: JSONObject,
        row: JSONObject,
        source: JSONArray?,
        fail: (String, String, String) -> Boolean
    ) {
        val bytes = row.optString("grouped_canonical", "")
        val bytesDigest = Pc2CompactBatch.sha256(bytes)
        if (bytes.isEmpty() || bytesDigest != row.optString("grouped_digest") ||
            bytesDigest != ref.optString("grouped_digest")
        ) {
            fail("GROUPED_BYTES_MISMATCH", poll, "stored bytes do not hash to the stored and referenced grouped digest")
        }
        if (identityOf(row) != refId) {
            fail("STORED_IDENTITY_INCONSISTENT", poll, "stored identity fields do not hash to $refId")
        }
        val reconstructed = try {
            Pc2CompactBatch.reconstructOrderedDecisions(row)
        } catch (e: Exception) {
            fail("NOT_RECONSTRUCTABLE", poll, e.message ?: e.javaClass.simpleName)
            return
        }
        val digest = Pc2CompactBatch.digestOrderedDecisions(reconstructed)
        if (digest != row.optString("decision_digest") || digest != ref.optString("decision_digest")) {
            fail("STORED_DIGEST_MISMATCH", poll,
                "reconstruction $digest, stored ${row.optString("decision_digest")}, reference ${ref.optString("decision_digest")}")
        }
        if (source != null) {
            val firstDifference = (0 until maxOf(source.length(), reconstructed.length())).firstOrNull { i ->
                Pc2CompactBatch.canonicalJson(source.opt(i)) != Pc2CompactBatch.canonicalJson(reconstructed.opt(i))
            }
            if (firstDifference != null) {
                fail("SOURCE_CONTENT_MISMATCH", poll, "first differing decision index $firstDifference")
            }
        }
        val grouped = Pc2CompactBatch.groupedOf(row)
        val expected = JSONObject()
        Pc2CompactBatch.COMPLETENESS_FIELDS.forEach { if (ref.has(it)) expected.put(it, ref.get(it)) }
        if (!Pc2CompactBatch.completenessMetadataMatches(expected, grouped)) {
            fail("COMPLETENESS_MISMATCH", poll, "stored completeness metadata differs from the reference")
        }
        if (row.optInt("decision_count", -1) != ref.optInt("decision_count", -2) ||
            row.optInt("distinct_decision_count", -1) != ref.optInt("distinct_decision_count", -2) ||
            row.optInt("decision_count", -1) != reconstructed.length()
        ) {
            fail("COUNT_MISMATCH", poll, "stored, referenced and reconstructed counts disagree")
        }
    }

    /** Recomputes a batch id from the nine identity fields of a ref or row. */
    fun identityOf(fields: JSONObject): String? {
        val names = listOf(
            "session_date_text", "poll_ts_text", "brain_version", "policy_hash", "policy_version",
            "authority_diagnostics_version", "decision_digest", "grouped_digest"
        )
        if (names.any { fields.optString(it, "").isEmpty() }) return null
        return Pc2CompactBatch.batchIdOf(
            fields.getString("session_date_text"),
            fields.getString("poll_ts_text"),
            fields.getString("brain_version"),
            fields.getString("policy_hash"),
            fields.getString("policy_version"),
            fields.getString("authority_diagnostics_version"),
            fields.getString("decision_digest"),
            fields.getString("grouped_digest")
        )
    }

    fun readJsonLines(file: File): List<JSONObject> =
        if (!file.isFile) emptyList()
        else file.readLines(Charsets.UTF_8).filter { it.isNotBlank() }.map { JSONObject(it) }

    /** Runs the audit over an export directory and writes pc2_parity_report.json beside it. */
    fun run(directory: File): Report {
        val report = audit(
            readJsonLines(File(directory, "snapshots.jsonl")),
            readJsonLines(File(directory, "batches.jsonl")),
            readJsonLines(File(directory, "legacy.jsonl"))
        )
        File(directory, "pc2_parity_report.json").writeText(report.toJson().toString(2), Charsets.UTF_8)
        return report
    }

    @JvmStatic
    fun main(args: Array<String>) {
        val directory = File(args.firstOrNull() ?: error("usage: Pc2ParityAudit <export-dir>"))
        val report = run(directory)
        println(report.toJson().toString(2))
        if (!report.certified) kotlin.system.exitProcess(1)
    }
}
