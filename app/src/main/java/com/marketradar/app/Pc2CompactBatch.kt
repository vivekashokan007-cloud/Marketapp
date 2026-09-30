package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.math.BigDecimal
import java.security.MessageDigest
import java.util.Locale
import java.util.TreeMap

/**
 * Lossless, deterministic PC2 telemetry compaction.
 *
 * The policy is content-addressed once. Identical decision bodies are stored
 * once per poll with their original indexes, so the exact ordered decision
 * array can be reconstructed for parity checks during the dual-write period.
 *
 * ## The canonicalisation contract (pinned, round 3)
 *
 * Every hash in this design is taken over bytes produced by [canonicalJson].
 * Until round 3 its string escaping was delegated to `JSONObject.quote`, which
 * is NOT the same function on Android and on the JVM:
 *
 *  - Android's org.json escapes every `/` as `\/` and leaves U+0080-U+009F
 *    and U+2000-U+20FF literal.
 *  - The reference org.json used by the JVM tests escapes `/` only after `<`,
 *    and escapes U+0080-U+009F and U+2000-U+20FF as `\uXXXX`.
 *
 * So the same decision produced different canonical bytes - and different
 * digests - on the phone and in any JVM verifier, including the parity audit.
 * No production decision contains those characters today (0 of 422 polls
 * checked on 30 Sep), which is why nothing has failed yet; one reason string
 * such as "n/a" would have broken every off-device verification silently.
 *
 * The contract is now written here and implemented without delegation:
 *
 *  - objects: keys sorted by UTF-16 code unit ([TreeMap] natural order), no
 *    whitespace;
 *  - strings: the RFC 8785 (JCS) string rule - escape `"` and `\`, the short
 *    forms `\b \f \n \r \t`, every other U+0000-U+001F as lowercase `\u00xx`;
 *    lone surrogates as lowercase `\uXXXX` so the bytes are always well-formed
 *    UTF-8; everything else literal, including `/` and all non-ASCII;
 *  - numbers: `BigDecimal(value.toString()).stripTrailingZeros().toPlainString()`,
 *    which is idempotent on its own output on both platforms;
 *  - `true`, `false`, `null` literal.
 */
object Pc2CompactBatch {
    const val GROUPING_SCHEMA_VERSION = "pc2_exact_dedup_v1"
    const val DIGEST_ALGORITHM = "sha256"
    const val DEFAULT_POLICY_VERSION = "pc2_authority_policy_v1"
    const val DEFAULT_DIAGNOSTICS_VERSION = "pc2_authority_diagnostics_v1"
    const val UNKNOWN_BRAIN_VERSION = "unknown"

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
     * v4: the canonicalisation contract above is pinned rather than delegated,
     * and identity strings follow [strictString]. Readback verifies this value.
     */
    const val COMPACT_CONTRACT_VERSION = "pc2_compact_contract_v4_pinned_canonical"

    val COMPLETENESS_FIELDS: List<String> = listOf(
        "envelope_completeness",
        "source_tail_cap",
        "source_possibly_truncated",
        "compact_contract_version"
    )

    /** ASCII whitespace stripped by [strictString]; the SQL twin uses the same six. */
    private const val IDENTITY_WHITESPACE = " \t\n\r\u000B\u000C"

    /**
     * Codex round-2 finding B1. One rule, implemented identically here and in
     * `public.pc2_derive_policy_version` in the migration:
     *
     *  - the value must be a JSON string, otherwise the fallback applies;
     *  - leading and trailing space, tab, LF, CR, VT and FF are stripped - and
     *    nothing else, so a Unicode space such as U+00A0 is kept on both sides;
     *  - an empty result takes the fallback.
     *
     * This replaces `optString(...).trim()`, which differed between platforms
     * (Android renders a JSON null as the string "null"; the reference library
     * returns the fallback) and trimmed a Unicode whitespace set that SQL's
     * `btrim` does not. Production values are always non-blank JSON strings, so
     * no real policy changes version under the new rule.
     */
    fun strictString(source: JSONObject, key: String, fallback: String): String {
        val raw = source.opt(key) as? String ?: return fallback
        val trimmed = raw.trim { it in IDENTITY_WHITESPACE }
        return if (trimmed.isEmpty()) fallback else trimmed
    }

    fun derivePolicyVersion(policy: JSONObject): String =
        strictString(policy, "version", DEFAULT_POLICY_VERSION)

    fun deriveDiagnosticsVersion(policy: JSONObject): String =
        strictString(policy, "authority_diagnostics_version", DEFAULT_DIAGNOSTICS_VERSION)

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

    /** The batch identity, computed from its nine identity fields. */
    fun batchIdOf(
        sessionDate: String,
        pollTs: String,
        brainVersion: String,
        policyHash: String,
        policyVersion: String,
        diagnosticsVersion: String,
        decisionDigest: String,
        groupedDigest: String
    ): String = sha256(
        listOf(
            GROUPING_SCHEMA_VERSION,
            sessionDate,
            pollTs,
            brainVersion,
            policyHash,
            policyVersion,
            diagnosticsVersion,
            decisionDigest,
            groupedDigest
        ).joinToString("|")
    )

    fun build(snapshot: JSONObject): Built? {
        val context = parseObject(snapshot.opt("context_json")) ?: return null
        val decisions = parseArray(context.opt("snapshot_pc2_authority_decisions")) ?: return null
        if (decisions.length() == 0) return null
        val policy = parseObject(context.opt("snapshot_pc2_authority_policy")) ?: JSONObject()
        val sessionDate = strictString(snapshot, "session_date", "")
        val pollTs = strictString(snapshot, "poll_ts", "")
        if (sessionDate.isEmpty() || pollTs.isEmpty()) return null

        val canonicalPolicy = canonicalJson(policy)
        val policyHash = sha256(canonicalPolicy)
        val policyVersion = derivePolicyVersion(policy)
        val policyDiagnosticsVersion = deriveDiagnosticsVersion(policy)

        val canonicalDecisions = ArrayList<String>(decisions.length())
        for (index in 0 until decisions.length()) {
            val decision = decisions.optJSONObject(index) ?: return null
            canonicalDecisions += canonicalJson(decision)
        }

        val decisionDigest = sha256("[${canonicalDecisions.joinToString(",")}]")
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

        // Codex review correction R5. The grouped payload is stored as the exact
        // canonical bytes it is hashed over, and every identity-bearing column
        // is folded into the batch id, so the database can re-derive both hashes
        // from the stored row.
        val groupedCanonical = canonicalJson(grouped)
        val groupedDigest = sha256(groupedCanonical)

        val brainVersion = strictString(context, "snapshot_brain_version", UNKNOWN_BRAIN_VERSION)
        val batchId = batchIdOf(
            sessionDate, pollTs, brainVersion, policyHash, policyVersion,
            policyDiagnosticsVersion, decisionDigest, groupedDigest
        )
        val policyRow = JSONObject()
            .put("policy_hash", policyHash)
            .put("policy_version", policyVersion)
            .put("schema_version", GROUPING_SCHEMA_VERSION)
            .put("digest_algorithm", DIGEST_ALGORITHM)
            .put("canonical_bytes", canonicalPolicy.toByteArray(Charsets.UTF_8).size)
            .put("canonical_policy", canonicalPolicy)
        val batchRow = JSONObject()
            .put("batch_id", batchId)
            .put("session_date_text", sessionDate)
            .put("poll_ts_text", pollTs)
            .put("brain_version", brainVersion)
            .put("policy_hash", policyHash)
            .put("policy_version", policyVersion)
            .put("authority_diagnostics_version", policyDiagnosticsVersion)
            .put("grouping_schema_version", GROUPING_SCHEMA_VERSION)
            .put("digest_algorithm", DIGEST_ALGORITHM)
            .put("decision_count", decisions.length())
            .put("distinct_decision_count", prototypes.length())
            .put("decision_digest", decisionDigest)
            .put("grouped_digest", groupedDigest)
            .put("grouped_canonical", groupedCanonical)
            .put("complete", true)
        // Codex round-2 finding B3: the snapshot reference carries every identity
        // field, so an off-device audit can recompute the expected batch id from
        // snapshot-side evidence alone and match batches by identity rather than
        // by poll timestamp.
        val snapshotRef = JSONObject()
            .put("batch_id", batchId)
            .put("session_date_text", sessionDate)
            .put("poll_ts_text", pollTs)
            .put("brain_version", brainVersion)
            .put("policy_hash", policyHash)
            .put("policy_version", policyVersion)
            .put("authority_diagnostics_version", policyDiagnosticsVersion)
            .put("decision_count", decisions.length())
            .put("distinct_decision_count", prototypes.length())
            .put("decision_digest", decisionDigest)
            .put("grouped_digest", groupedDigest)
            .put("grouping_schema_version", GROUPING_SCHEMA_VERSION)
            .put("envelope_completeness", ENVELOPE_COMPLETENESS)
            .put("source_tail_cap", SOURCE_TAIL_CAP)
            .put("source_possibly_truncated", sourceTruncated)
            .put("compact_contract_version", COMPACT_CONTRACT_VERSION)
            .put("complete", true)
        return Built(policyRow, batchRow, snapshotRef)
    }

    /**
     * The grouped payload as an object, from either the content-bound
     * `grouped_canonical` text (current) or the earlier `grouped_decisions_json`
     * object. Returns null when neither is present or parseable.
     */
    fun groupedOf(row: JSONObject?): JSONObject? {
        if (row == null) return null
        row.opt("grouped_canonical")?.let { raw ->
            if (raw is String) return runCatching { JSONObject(raw) }.getOrNull()
        }
        return row.optJSONObject("grouped_decisions_json")
    }

    /**
     * Rebuilds the ordered decision array. Throws on any structural defect:
     * a missing or non-integer count, an index out of range, a duplicate index
     * or a gap. Round 3 also rejects non-integer indexes and a non-positive
     * count explicitly rather than relying on coercion.
     */
    fun reconstructOrderedDecisions(batchRow: JSONObject): JSONArray {
        val grouped = requireNotNull(groupedOf(batchRow)) { "PC2 grouped payload missing" }
        val countValue = grouped.opt("ordered_count")
        require(countValue is Number && isIntegral(countValue)) { "PC2 ordered_count missing or not an integer" }
        val count = countValue.toInt()
        require(count > 0) { "PC2 ordered_count must be positive" }
        val ordered = arrayOfNulls<JSONObject>(count)
        val prototypes = grouped.optJSONArray("prototypes")
            ?: throw IllegalArgumentException("PC2 prototypes missing")
        for (prototypeIndex in 0 until prototypes.length()) {
            val prototype = prototypes.getJSONObject(prototypeIndex)
            val decision = prototype.getJSONObject("decision")
            val indexes = prototype.getJSONArray("indexes")
            require(indexes.length() > 0) { "PC2 prototype without indexes" }
            for (i in 0 until indexes.length()) {
                val raw = indexes.get(i)
                require(raw is Number && isIntegral(raw)) { "PC2 non-integer index: $raw" }
                val index = raw.toInt()
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
                canonicalString(it.key) + ":" + canonicalJson(it.value)
            }
        }
        is JSONArray -> (0 until value.length()).joinToString(prefix = "[", postfix = "]", separator = ",") {
            canonicalJson(value.opt(it))
        }
        is String -> canonicalString(value)
        is Boolean -> value.toString()
        is Number -> canonicalNumber(value)
        else -> canonicalString(value.toString())
    }

    /** The pinned string rule: see the contract in the class documentation. */
    fun canonicalString(value: String): String {
        val out = StringBuilder(value.length + 2)
        out.append('"')
        var i = 0
        while (i < value.length) {
            val c = value[i]
            when {
                c == '"' -> out.append("\\\"")
                c == '\\' -> out.append("\\\\")
                c == '\b' -> out.append("\\b")
                c == '\u000C' -> out.append("\\f")
                c == '\n' -> out.append("\\n")
                c == '\r' -> out.append("\\r")
                c == '\t' -> out.append("\\t")
                c < ' ' -> out.append(unicodeEscape(c))
                Character.isHighSurrogate(c) &&
                    i + 1 < value.length && Character.isLowSurrogate(value[i + 1]) -> {
                    out.append(c).append(value[i + 1])
                    i += 1
                }
                Character.isSurrogate(c) -> out.append(unicodeEscape(c))
                else -> out.append(c)
            }
            i += 1
        }
        out.append('"')
        return out.toString()
    }

    private fun unicodeEscape(c: Char): String =
        "\\u" + String.format(Locale.ROOT, "%04x", c.code)

    fun sha256(value: String): String = MessageDigest.getInstance("SHA-256")
        .digest(value.toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it.toInt() and 0xff) }

    private fun canonicalNumber(value: Number): String {
        val text = value.toString()
        val decimal = runCatching { BigDecimal(text) }.getOrNull()
            ?: throw IllegalArgumentException("Non-finite JSON number: $text")
        return decimal.stripTrailingZeros().toPlainString()
    }

    private fun isIntegral(value: Number): Boolean {
        val decimal = runCatching { BigDecimal(value.toString()) }.getOrNull() ?: return false
        return decimal.stripTrailingZeros().scale() <= 0 &&
            decimal >= BigDecimal.ZERO.minus(BigDecimal(Int.MAX_VALUE)) &&
            decimal <= BigDecimal(Int.MAX_VALUE)
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
