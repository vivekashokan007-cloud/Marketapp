package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.math.BigDecimal
import java.security.MessageDigest
import java.util.TreeMap

/**
 * Lossless, deterministic PC2 telemetry compaction.
 *
 * The policy is content-addressed once. Identical decision bodies are stored
 * once per poll with their original indexes, so the exact ordered decision
 * array can be reconstructed for parity checks during the dual-write period.
 */
object Pc2CompactBatch {
    const val GROUPING_SCHEMA_VERSION = "pc2_exact_dedup_v1"
    const val DIGEST_ALGORITHM = "sha256"

    /**
     * Review correction C6: brain.py publishes only the LAST 128 authority
     * decisions of a poll (`result['pc2_authority_decisions'] = ...[-128:]`).
     * This compactor is lossless with respect to the array it is given, but that
     * array is itself a tail. `complete = true` therefore means "this envelope
     * reproduces the stored array exactly", NOT "these are all the decisions the
     * brain made". Gate (`parameter_threshold`) decisions are evaluated before
     * the ranking-context calls and are displaced by the cap on full-menu
     * sessions, so a compact batch must never be read as complete gate evidence.
     */
    const val SOURCE_TAIL_CAP = 128
    const val ENVELOPE_COMPLETENESS = "LOSSLESS_OF_SNAPSHOT_ARRAY"

    /**
     * Codex review correction R4: the batch identity and the decision digest
     * cover the DECISIONS, not the completeness metadata beside them. A row
     * could therefore carry the right decisions under wrong or absent
     * completeness metadata and still be acknowledged. The contract is pinned
     * here and verified on readback by [completenessMetadataMatches].
     */
    const val COMPACT_CONTRACT_VERSION = "pc2_compact_contract_v2_completeness_verified"

    val COMPLETENESS_FIELDS: List<String> = listOf(
        "envelope_completeness",
        "source_tail_cap",
        "source_possibly_truncated",
        "compact_contract_version"
    )

    /**
     * R4 verification. A locally built envelope always declares the contract, so
     * every field must be present and equal in the stored row.
     *
     * Explicit legacy behaviour: an envelope written before this correction
     * declares none of the fields. It verifies only against a stored row that
     * also carries none of them - a row that has acquired metadata the local
     * envelope never sent is a mismatch, not an upgrade.
     */
    fun completenessMetadataMatches(expected: JSONObject?, stored: JSONObject?): Boolean {
        if (expected == null || stored == null) return false
        val declares = COMPLETENESS_FIELDS.any { expected.has(it) }
        if (!declares) return COMPLETENESS_FIELDS.none { stored.has(it) }
        return COMPLETENESS_FIELDS.all { key ->
            expected.has(key) && stored.has(key) &&
                canonicalJson(expected.opt(key)) == canonicalJson(stored.opt(key))
        }
    }

    data class Built(
        val policyRow: JSONObject,
        val batchRow: JSONObject,
        val snapshotRef: JSONObject
    ) {
        fun envelope(): JSONObject = JSONObject()
            .put("schema_version", GROUPING_SCHEMA_VERSION)
            .put("policy_row", JSONObject(policyRow.toString()))
            .put("batch_row", JSONObject(batchRow.toString()))
    }

    fun build(snapshot: JSONObject): Built? {
        val context = parseObject(snapshot.opt("context_json")) ?: return null
        val decisions = parseArray(context.opt("snapshot_pc2_authority_decisions")) ?: return null
        if (decisions.length() == 0) return null
        val policy = parseObject(context.opt("snapshot_pc2_authority_policy")) ?: JSONObject()
        val sessionDate = snapshot.optString("session_date", "").trim()
        val pollTs = snapshot.optString("poll_ts", "").trim()
        if (sessionDate.isEmpty() || pollTs.isEmpty()) return null

        val canonicalPolicy = canonicalJson(policy)
        val policyHash = sha256(canonicalPolicy)
        val policyVersion = policy.optString("version", "pc2_authority_policy_v1")
            .trim().ifEmpty { "pc2_authority_policy_v1" }
        val policyDiagnosticsVersion = policy.optString(
            "authority_diagnostics_version",
            "pc2_authority_diagnostics_v1"
        ).trim().ifEmpty { "pc2_authority_diagnostics_v1" }

        val canonicalDecisions = ArrayList<String>(decisions.length())
        val originalDecisions = ArrayList<JSONObject>(decisions.length())
        var complete = true
        for (index in 0 until decisions.length()) {
            val decision = decisions.optJSONObject(index)
            if (decision == null) {
                complete = false
                continue
            }
            originalDecisions += JSONObject(decision.toString())
            canonicalDecisions += canonicalJson(decision)
        }
        if (!complete || originalDecisions.size != decisions.length()) return null

        val orderedCanonicalArray = "[${canonicalDecisions.joinToString(",")}]"
        val decisionDigest = sha256(orderedCanonicalArray)
        val groupedByCanonical = linkedMapOf<String, MutableList<Int>>()
        canonicalDecisions.forEachIndexed { index, canonical ->
            groupedByCanonical.getOrPut(canonical) { mutableListOf() }.add(index)
        }
        val prototypes = JSONArray()
        groupedByCanonical.forEach { (canonical, indexes) ->
            prototypes.put(
                JSONObject()
                    .put("decision", JSONObject(canonical))
                    .put("indexes", JSONArray(indexes))
            )
        }
        val sourceTruncated = decisions.length() >= SOURCE_TAIL_CAP
        val grouped = JSONObject()
            .put("schema_version", GROUPING_SCHEMA_VERSION)
            .put("ordered_count", decisions.length())
            .put("envelope_completeness", ENVELOPE_COMPLETENESS)
            .put("source_tail_cap", SOURCE_TAIL_CAP)
            .put("source_possibly_truncated", sourceTruncated)
            .put("compact_contract_version", COMPACT_CONTRACT_VERSION)
            .put("prototypes", prototypes)

        val brainVersion = context.optString("snapshot_brain_version", "unknown")
            .trim().ifEmpty { "unknown" }
        val batchId = sha256(
            listOf(
                GROUPING_SCHEMA_VERSION,
                sessionDate,
                pollTs,
                policyHash,
                policyDiagnosticsVersion,
                decisionDigest
            ).joinToString("|")
        )
        val policyRow = JSONObject()
            .put("policy_hash", policyHash)
            .put("policy_version", policyVersion)
            .put("schema_version", GROUPING_SCHEMA_VERSION)
            .put("digest_algorithm", DIGEST_ALGORITHM)
            .put("canonical_bytes", canonicalPolicy.toByteArray(Charsets.UTF_8).size)
            .put("policy_json", JSONObject(policy.toString()))
        val batchRow = JSONObject()
            .put("batch_id", batchId)
            .put("session_date", sessionDate)
            .put("poll_ts", pollTs)
            .put("brain_version", brainVersion)
            .put("policy_hash", policyHash)
            .put("policy_version", policyVersion)
            .put("authority_diagnostics_version", policyDiagnosticsVersion)
            .put("grouping_schema_version", GROUPING_SCHEMA_VERSION)
            .put("digest_algorithm", DIGEST_ALGORITHM)
            .put("decision_count", decisions.length())
            .put("distinct_decision_count", prototypes.length())
            .put("decision_digest", decisionDigest)
            .put("grouped_decisions_json", grouped)
            .put("complete", true)
        val snapshotRef = JSONObject()
            .put("batch_id", batchId)
            .put("policy_hash", policyHash)
            .put("decision_count", decisions.length())
            .put("distinct_decision_count", prototypes.length())
            .put("decision_digest", decisionDigest)
            .put("grouping_schema_version", GROUPING_SCHEMA_VERSION)
            .put("envelope_completeness", ENVELOPE_COMPLETENESS)
            .put("source_tail_cap", SOURCE_TAIL_CAP)
            .put("source_possibly_truncated", sourceTruncated)
            .put("compact_contract_version", COMPACT_CONTRACT_VERSION)
            .put("complete", true)
        return Built(policyRow, batchRow, snapshotRef)
    }

    fun reconstructOrderedDecisions(batchRow: JSONObject): JSONArray {
        val grouped = batchRow.getJSONObject("grouped_decisions_json")
        val count = grouped.getInt("ordered_count")
        val ordered = arrayOfNulls<JSONObject>(count)
        val prototypes = grouped.getJSONArray("prototypes")
        for (prototypeIndex in 0 until prototypes.length()) {
            val prototype = prototypes.getJSONObject(prototypeIndex)
            val decision = prototype.getJSONObject("decision")
            val indexes = prototype.getJSONArray("indexes")
            for (i in 0 until indexes.length()) {
                val index = indexes.getInt(i)
                require(index in 0 until count) { "PC2 index out of bounds: $index/$count" }
                require(ordered[index] == null) { "PC2 duplicate reconstructed index: $index" }
                ordered[index] = JSONObject(decision.toString())
            }
        }
        require(ordered.all { it != null }) { "PC2 reconstruction is incomplete" }
        return JSONArray().also { result -> ordered.forEach { result.put(it) } }
    }

    fun digestOrderedDecisions(decisions: JSONArray): String {
        val canonical = buildString {
            append('[')
            for (index in 0 until decisions.length()) {
                if (index > 0) append(',')
                append(canonicalJson(decisions.get(index)))
            }
            append(']')
        }
        return sha256(canonical)
    }

    fun canonicalJson(value: Any?): String = when (value) {
        null, JSONObject.NULL -> "null"
        is JSONObject -> {
            val sorted = TreeMap<String, Any?>()
            value.keys().forEach { key -> sorted[key] = value.opt(key) }
            sorted.entries.joinToString(prefix = "{", postfix = "}", separator = ",") {
                JSONObject.quote(it.key) + ":" + canonicalJson(it.value)
            }
        }
        is JSONArray -> (0 until value.length()).joinToString(prefix = "[", postfix = "]", separator = ",") {
            canonicalJson(value.opt(it))
        }
        is String -> JSONObject.quote(value)
        is Boolean -> value.toString()
        is Number -> canonicalNumber(value)
        else -> JSONObject.quote(value.toString())
    }

    fun sha256(value: String): String = MessageDigest.getInstance("SHA-256")
        .digest(value.toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it.toInt() and 0xff) }

    private fun canonicalNumber(value: Number): String {
        val text = value.toString()
        val decimal = runCatching { BigDecimal(text) }.getOrNull()
            ?: throw IllegalArgumentException("Non-finite JSON number: $text")
        return decimal.stripTrailingZeros().toPlainString()
    }

    private fun parseObject(value: Any?): JSONObject? = when (value) {
        is JSONObject -> value
        is String -> runCatching { JSONObject(value) }.getOrNull()
        else -> null
    }

    private fun parseArray(value: Any?): JSONArray? = when (value) {
        is JSONArray -> value
        is String -> runCatching { JSONArray(value) }.getOrNull()
        else -> null
    }
}
