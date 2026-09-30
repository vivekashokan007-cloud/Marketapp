package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.time.Duration
import java.time.Instant
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.time.format.DateTimeFormatterBuilder
import java.time.temporal.ChronoField

/**
 * DB-1 parity audit: certifies ONE IST session for the three-session cutover
 * gate. Read-only; it consumes the files written by `tools/pc2_parity_export.sh`
 * and never touches a database.
 *
 * Round 4 (Codex round-3 findings A1 and A2) makes certification a statement
 * about a SESSION, not about whatever rows happened to be supplied:
 *
 *  A1  Inputs must be complete. `manifest.json` is written last by the
 *      exporter, inside the same read-only snapshot as the data, and records
 *      the IST day, each file's line count and SHA-256, and a table-wide count
 *      of malformed timestamps. A missing file, a count or hash mismatch, an
 *      incomplete manifest, an empty inventory, or a session without a single
 *      PC2 poll cannot certify.
 *
 *      Coverage is reconciled against an INDEPENDENT poll inventory: the polls
 *      in `ml_option_chain_snapshots`, which the phone persists before the
 *      brain runs (b493) and which therefore exist even when the brain or the
 *      snapshot write fails. Each inventory poll must map to exactly one
 *      snapshot instant between 0 and [INVENTORY_MATCH_WINDOW] later (on
 *      29 Sep 2026 all 78 matched, 2-18 s apart, with polls >= 289 s apart).
 *      An inventory poll without a snapshot, a snapshot without an inventory
 *      poll, and legacy PC2 rows without a snapshot all block certification,
 *      unless a reviewed exception names that exact poll.
 *
 *  A2  A reference must belong to the poll that holds it. The snapshot's own
 *      exported instant, the reference's `poll_ts_text` and the stored batch's
 *      `poll_ts_text` must be the same instant, and the snapshot session, the
 *      reference session and the requested IST day must agree. A reference
 *      copied onto a different poll fails even when the decision arrays are
 *      identical. Polls are counted as distinct instants, so a duplicated
 *      snapshot row cannot inflate the certified count.
 *
 * Byte-level checks (round 3, unchanged): reconstruction from stored bytes with
 * the real compactor, index coverage, digests against stored, referenced and
 * source evidence, completeness metadata, counts, and structural checks on
 * unreferenced rows.
 *
 * Known conservative limit: the source array is re-read from Postgres jsonb and
 * canonicalised on the JVM. Android and the JVM parse numbers through different
 * types; canonical output is idempotent on both, but a divergence on an exotic
 * number would appear as SOURCE_DIGEST_MISMATCH - a false alarm, never a false
 * certification.
 */
object Pc2ParityAudit {

    /** Chain evidence precedes its snapshot by 2-18 s in production; polls are >= 289 s apart. */
    val INVENTORY_MATCH_WINDOW: Duration = Duration.ofSeconds(60)

    /** Only session-continuity gaps may be excepted, never integrity failures. */
    val EXCEPTABLE = setOf("MISSING_SNAPSHOT_FOR_POLL", "SNAPSHOT_WITHOUT_INVENTORY", "LEGACY_WITHOUT_SNAPSHOT")

    private val IST = ZoneOffset.ofHoursMinutes(5, 30)

    enum class Severity { FAIL, VISIBLE }

    data class Finding(val category: String, val severity: Severity, val key: String, val detail: String)

    /** Session identity and independent evidence, from the export manifest. */
    data class SessionEvidence(
        val istDay: String,
        /** Poll instants from the independent chain-evidence inventory. */
        val inventory: List<String>,
        /** Rows anywhere in the batch table whose poll/session text fails validation. */
        val malformedBatchRows: Int = 0,
        /** Faults found while checking the export itself (missing file, count, hash). */
        val inputFaults: List<String> = emptyList()
    )

    data class Report(
        val istDay: String?,
        val inventoryPolls: Int,
        val snapshotPolls: Int,
        val expectedPc2Polls: Int,
        val certifiedPolls: Int,
        val cappedPolls: Int,
        val sourceFromSnapshot: Int,
        val sourceFromLegacy: Int,
        val exceptionsApplied: Int,
        val findings: List<Finding>
    ) {
        /** True only for a complete, reconciled session with at least one verified PC2 poll. */
        val certified: Boolean
            get() = findings.none { it.severity == Severity.FAIL } &&
                expectedPc2Polls > 0 && certifiedPolls == expectedPc2Polls

        fun count(category: String): Int = findings.count { it.category == category }

        fun toJson(): JSONObject = JSONObject()
            .put("certified", certified)
            .put("eligible_for_parity_session", certified)
            .put("ist_day", istDay ?: JSONObject.NULL)
            .put("inventory_polls", inventoryPolls)
            .put("snapshot_polls", snapshotPolls)
            .put("expected_pc2_polls", expectedPc2Polls)
            .put("certified_polls", certifiedPolls)
            .put("capped_polls", cappedPolls)
            .put("source_from_snapshot", sourceFromSnapshot)
            .put("source_from_legacy", sourceFromLegacy)
            .put("exceptions_applied", exceptionsApplied)
            .put("categories", JSONObject().also { summary ->
                findings.groupBy { it.category }.toSortedMap().forEach { (category, list) ->
                    summary.put(category, JSONObject()
                        .put("fail", list.count { it.severity == Severity.FAIL })
                        .put("visible", list.count { it.severity == Severity.VISIBLE }))
                }
            })
            .put("findings", JSONArray().also { array ->
                findings.forEach {
                    array.put(JSONObject().put("category", it.category).put("severity", it.severity.name)
                        .put("key", it.key).put("detail", it.detail))
                }
            })
    }

    // ---- timestamps -----------------------------------------------------------

    private val INSTANT_FORMAT: DateTimeFormatter = DateTimeFormatterBuilder()
        .parseCaseInsensitive()
        .append(DateTimeFormatter.ISO_LOCAL_DATE)
        .appendLiteral('T')
        .appendPattern("HH:mm:ss")
        .optionalStart().appendFraction(ChronoField.NANO_OF_SECOND, 1, 9, true).optionalEnd()
        .optionalStart().appendOffset("+HH:MM", "Z").optionalEnd()
        .optionalStart().appendOffset("+HHMM", "Z").optionalEnd()
        .toFormatter()
        .withResolverStyle(java.time.format.ResolverStyle.STRICT)
        .withChronology(java.time.chrono.IsoChronology.INSTANCE)

    /**
     * A poll instant, or null. An explicit offset is REQUIRED (`Z`, `+05:30` or
     * `+0530`), dates and times must be real, and nothing else is accepted.
     */
    fun parseInstant(text: String?): Instant? {
        if (text.isNullOrBlank()) return null
        return runCatching { OffsetDateTime.parse(text, INSTANT_FORMAT).toInstant() }.getOrNull()
    }

    private fun parseDay(text: String?): LocalDate? =
        if (text == null || !Regex("\\d{4}-\\d{2}-\\d{2}").matches(text)) null
        else runCatching { LocalDate.parse(text) }.getOrNull()

    fun istDayOf(instant: Instant): LocalDate = instant.atOffset(IST).toLocalDate()

    // ---- the audit --------------------------------------------------------------

    fun audit(
        evidence: SessionEvidence?,
        snapshots: List<JSONObject>,
        batches: List<JSONObject>,
        legacy: List<JSONObject>,
        exceptions: List<JSONObject> = emptyList()
    ): Report {
        val findings = mutableListOf<Finding>()
        fun fail(category: String, key: String, detail: String) =
            findings.add(Finding(category, Severity.FAIL, key, detail))
        fun visible(category: String, key: String, detail: String) =
            findings.add(Finding(category, Severity.VISIBLE, key, detail))

        // ---- A1: session identity and input completeness --------------------
        val day = parseDay(evidence?.istDay)
        if (evidence == null) fail("MISSING_SESSION_EVIDENCE", "-", "no manifest / inventory: cannot identify the session")
        else if (day == null) fail("INVALID_SESSION_DAY", evidence.istDay, "IST day must be a real YYYY-MM-DD date")
        evidence?.inputFaults?.forEach { fail("INPUT_INCOMPLETE", it.substringBefore(':'), it) }
        if ((evidence?.malformedBatchRows ?: 0) > 0) {
            fail("MALFORMED_TIMESTAMP_ROWS", "table",
                "${evidence!!.malformedBatchRows} stored batch rows have an invalid poll or session timestamp")
        }
        val inventory = mutableListOf<Instant>()
        evidence?.inventory?.forEach { text ->
            val instant = parseInstant(text)
            if (instant == null) fail("MALFORMED_TIMESTAMP", text, "inventory poll time is not a valid instant")
            else inventory += instant
        }
        if (evidence != null && evidence.inventory.isEmpty()) {
            fail("EMPTY_INVENTORY", evidence.istDay, "no independent poll evidence for the session")
        }
        fun inSession(instant: Instant) = day != null && istDayOf(instant) == day

        inventory.filterNot { inSession(it) }.forEach {
            fail("SESSION_MISMATCH", it.toString(), "inventory poll is outside IST day $day")
        }

        // ---- stored batches ----------------------------------------------------
        val stored = LinkedHashMap<String, JSONObject>()
        for (row in batches) {
            val id = row.optString("batch_id")
            if (stored.containsKey(id)) fail("DUPLICATE_BATCH_ROW", id, "batch id exported twice")
            stored[id] = row
        }

        // ---- legacy source arrays, keyed by instant ------------------------------
        val legacyArrays = HashMap<Instant, JSONArray>()
        val legacyPolls = HashSet<Instant>()
        legacy.groupBy { it.optString("poll_ts_utc") }.forEach { (text, rows) ->
            val instant = parseInstant(text)
            if (instant == null) {
                fail("MALFORMED_TIMESTAMP", text, "legacy poll time is not a valid instant"); return@forEach
            }
            legacyPolls += instant
            val sorted = rows.sortedBy { it.optInt("decision_index", -1) }
            if (sorted.map { it.optInt("decision_index", -1) } != (0 until sorted.size).toList()) {
                fail("LEGACY_INDEX_DEFECT", instant.toString(), "decision_index is not exactly 0..${sorted.size - 1}")
                return@forEach
            }
            legacyArrays[instant] = JSONArray().also { a -> sorted.forEach { a.put(it.opt("decision_json")) } }
        }

        // ---- snapshots, grouped by their actual instant ------------------------
        val byInstant = LinkedHashMap<Instant, MutableList<JSONObject>>()
        for (snapshot in snapshots) {
            val text = snapshot.optString("poll_ts_utc")
            val instant = parseInstant(text)
            if (instant == null) {
                fail("MALFORMED_TIMESTAMP", text.ifEmpty { "-" }, "snapshot poll time is not a valid instant"); continue
            }
            byInstant.getOrPut(instant) { mutableListOf() } += snapshot
        }
        val snapshotInstants = byInstant.keys.sorted()

        // ---- A1: reconcile against the independent inventory --------------------
        val matchedSnapshots = HashSet<Instant>()
        for (poll in inventory.sorted()) {
            val candidates = snapshotInstants.filter {
                !it.isBefore(poll) && Duration.between(poll, it) <= INVENTORY_MATCH_WINDOW
            }
            when {
                candidates.isEmpty() -> fail("MISSING_SNAPSHOT_FOR_POLL", poll.toString(),
                    "the phone polled (chain evidence) but no snapshot exists within $INVENTORY_MATCH_WINDOW")
                candidates.size > 1 -> fail("AMBIGUOUS_INVENTORY_MATCH", poll.toString(),
                    "${candidates.size} snapshot instants follow one poll")
                else -> {
                    if (!matchedSnapshots.add(candidates.single())) {
                        fail("AMBIGUOUS_INVENTORY_MATCH", candidates.single().toString(),
                            "one snapshot follows two inventory polls")
                    }
                }
            }
        }
        snapshotInstants.filter { it !in matchedSnapshots }.forEach {
            fail("SNAPSHOT_WITHOUT_INVENTORY", it.toString(), "no chain evidence precedes this snapshot")
        }
        legacyPolls.filter { it !in byInstant }.forEach {
            fail("LEGACY_WITHOUT_SNAPSHOT", it.toString(), "legacy PC2 rows exist for a poll with no snapshot")
        }

        // ---- per-poll verification -------------------------------------------------
        val referencedBy = HashMap<String, Instant>()
        var expectedPc2 = 0
        var certifiedPolls = 0
        var capped = 0
        var fromSnapshot = 0
        var fromLegacy = 0

        for (instant in snapshotInstants) {
            val rows = byInstant.getValue(instant)
            val key = instant.toString()
            if (!inSession(instant)) fail("SESSION_MISMATCH", key, "snapshot is outside IST day $day")
            if (rows.size > 1) {
                visible("DUPLICATE_SNAPSHOT_POLL", key, "${rows.size} snapshot rows for one poll; counted once")
                val ids = rows.map { it.optJSONObject("compact_ref")?.optString("batch_id") }.toSet()
                if (ids.size > 1) fail("CONFLICTING_DUPLICATE_SNAPSHOT", key, "duplicate rows reference different batches")
            }
            val hasPc2 = rows.any { it.optJSONObject("compact_ref") != null ||
                (it.optJSONArray("decisions")?.length() ?: 0) > 0 } || legacyArrays.containsKey(instant) ||
                instant in legacyPolls
            if (!hasPc2) {
                visible("NO_PC2_EVIDENCE", key, "no decisions and no compact reference at this poll")
                continue
            }
            expectedPc2 += 1
            val before = findings.count { it.severity == Severity.FAIL }
            val row = rows.first()
            val counted = checkSnapshot(day, instant, row, legacyArrays[instant], stored, referencedBy, ::fail, ::visible)
            if (counted.capped) capped += 1
            if (counted.source == "snapshot") fromSnapshot += 1
            if (counted.source == "legacy") fromLegacy += 1
            if (findings.count { it.severity == Severity.FAIL } == before) certifiedPolls += 1
        }

        if (evidence != null && day != null && expectedPc2 == 0) {
            fail("NO_PC2_EVIDENCE_IN_SESSION", day.toString(), "a session with no PC2 poll cannot count toward parity")
        }

        // ---- unreferenced stored rows --------------------------------------------
        stored.forEach { (id, row) ->
            if (referencedBy.containsKey(id)) return@forEach
            visible("EXTRA_BATCH", id, "stored but referenced by no snapshot in the session (poll ${row.optString("poll_ts_text")})")
            val bytesOk = Pc2CompactBatch.sha256(row.optString("grouped_canonical", "")) == row.optString("grouped_digest")
            val identityOk = identityOf(row) == id
            val reconstructs = runCatching {
                Pc2CompactBatch.digestOrderedDecisions(Pc2CompactBatch.reconstructOrderedDecisions(row)) ==
                    row.optString("decision_digest")
            }.getOrDefault(false)
            val timeOk = parseInstant(row.optString("poll_ts_text")) != null
            if (!bytesOk || !identityOk || !reconstructs || !timeOk) {
                fail("STORED_ROW_NOT_RECONSTRUCTABLE", id,
                    "bytesOk=$bytesOk identityOk=$identityOk reconstructs=$reconstructs timeOk=$timeOk")
            }
        }

        // ---- reviewed exceptions (session continuity only) -------------------------
        var applied = 0
        for (exception in exceptions) {
            val category = exception.optString("category")
            val instant = parseInstant(exception.optString("poll_ts_utc"))
            val reason = exception.optString("reason").trim()
            val reviewer = exception.optString("reviewed_by").trim()
            if (category !in EXCEPTABLE || instant == null || reason.isEmpty() || reviewer.isEmpty()) {
                fail("INVALID_EXCEPTION", exception.toString(),
                    "exceptions need an exceptable category, a valid poll_ts_utc, a reason and a reviewer")
                continue
            }
            val index = findings.indexOfFirst {
                it.severity == Severity.FAIL && it.category == category && it.key == instant.toString()
            }
            if (index < 0) {
                visible("UNUSED_EXCEPTION", instant.toString(), "$category excepted but not observed")
                continue
            }
            val original = findings[index]
            findings[index] = original.copy(severity = Severity.VISIBLE,
                detail = "EXCEPTED ($reviewer): $reason | ${original.detail}")
            applied += 1
        }

        return Report(
            istDay = day?.toString(),
            inventoryPolls = inventory.size,
            snapshotPolls = snapshotInstants.size,
            expectedPc2Polls = expectedPc2,
            certifiedPolls = certifiedPolls,
            cappedPolls = capped,
            sourceFromSnapshot = fromSnapshot,
            sourceFromLegacy = fromLegacy,
            exceptionsApplied = applied,
            findings = findings.sortedBy { "${it.severity}|${it.category}|${it.key}" }
        )
    }

    private class Counted(val capped: Boolean, val source: String?)

    private fun checkSnapshot(
        day: LocalDate?,
        instant: Instant,
        snapshot: JSONObject,
        legacyArray: JSONArray?,
        stored: Map<String, JSONObject>,
        referencedBy: MutableMap<String, Instant>,
        fail: (String, String, String) -> Boolean,
        visible: (String, String, String) -> Boolean
    ): Counted {
        val key = instant.toString()
        val ref = snapshot.optJSONObject("compact_ref")
        val snapshotArray = snapshot.optJSONArray("decisions")?.takeIf { it.length() > 0 }
        if (ref == null) {
            fail("NO_COMPACT_REF", key, "PC2 evidence exists but the snapshot references no compact batch")
            return Counted(false, null)
        }
        val refId = ref.optString("batch_id")

        // A2: the reference belongs to THIS poll and THIS session.
        val refInstant = parseInstant(ref.optString("poll_ts_text"))
        if (refInstant == null) {
            fail("MALFORMED_TIMESTAMP", key, "reference poll_ts_text '${ref.optString("poll_ts_text")}' is not a valid instant")
        } else if (refInstant != instant) {
            fail("REFERENCE_POLL_MISMATCH", key, "snapshot is at $instant but its reference is for $refInstant")
        }
        val snapshotSession = snapshot.optString("session_date")
        val refSession = ref.optString("session_date_text")
        if (refSession != snapshotSession || (day != null && refSession != day.toString()) ||
            (refInstant != null && istDayOf(refInstant).toString() != refSession)
        ) {
            fail("SESSION_MISMATCH", key,
                "snapshot session '$snapshotSession', reference session '$refSession', requested '$day'")
        }
        val refIdentity = identityOf(ref)
        if (refIdentity == null || refIdentity != refId) {
            fail("REF_IDENTITY_INCONSISTENT", key, "reference fields do not hash to $refId")
        }
        referencedBy[refId]?.let { other ->
            fail("DUPLICATE_REFERENCE", key, "batch $refId is also referenced by the distinct poll $other")
        }
        referencedBy[refId] = instant
        val isCapped = ref.optBoolean("source_possibly_truncated", false)
        if (isCapped) {
            visible("CAPPED_SOURCE", key,
                "source array is at the ${ref.optInt("source_tail_cap")}-decision tail cap; " +
                    "lossless for the array, not full-poll gate evidence")
        }

        val source = snapshotArray ?: legacyArray
        val sourceKind = when {
            snapshotArray != null -> "snapshot"
            legacyArray != null -> "legacy"
            else -> null
        }
        if (source == null) fail("SOURCE_UNAVAILABLE", key, "neither the snapshot nor the legacy table holds the array")
        if (snapshotArray != null && legacyArray != null &&
            Pc2CompactBatch.digestOrderedDecisions(snapshotArray) != Pc2CompactBatch.digestOrderedDecisions(legacyArray)
        ) {
            fail("LEGACY_SNAPSHOT_DISAGREE", key, "the two source copies differ")
        }
        if (source != null) {
            val sourceDigest = Pc2CompactBatch.digestOrderedDecisions(source)
            if (sourceDigest != ref.optString("decision_digest")) {
                fail("SOURCE_DIGEST_MISMATCH", key, "source $sourceDigest vs reference ${ref.optString("decision_digest")}")
            }
            if (source.length() != ref.optInt("decision_count", -1)) {
                fail("COUNT_MISMATCH", key, "source has ${source.length()} decisions, reference ${ref.optInt("decision_count")}")
            }
            // Expected identity rebuilt from snapshot-side evidence. The poll string
            // is the reference's, which A2 has just proven to be this poll's instant.
            val rebuiltId = runCatching {
                Pc2CompactBatch.build(
                    JSONObject()
                        .put("session_date", snapshotSession)
                        .put("poll_ts", ref.optString("poll_ts_text"))
                        .put("context_json", JSONObject()
                            .put("snapshot_pc2_authority_decisions", source)
                            .put("snapshot_pc2_authority_policy", snapshot.optJSONObject("policy") ?: JSONObject())
                            .put("snapshot_brain_version", snapshot.opt("brain_version") ?: JSONObject.NULL))
                )?.batchRow?.optString("batch_id")
            }.getOrNull()
            if (rebuiltId != refId) {
                fail("EXPECTED_ID_MISMATCH", key, "the compactor rebuilds $rebuiltId from the snapshot evidence, reference says $refId")
            }
        }

        val row = stored[refId]
        if (row == null) {
            fail("MISSING_BATCH", key, "referenced batch $refId is not stored")
        } else {
            if (parseInstant(row.optString("poll_ts_text")) != instant) {
                fail("REFERENCE_POLL_MISMATCH", key, "stored batch poll ${row.optString("poll_ts_text")} is not this poll")
            }
            checkStored(key, refId, ref, row, source, fail)
        }
        return Counted(isCapped, sourceKind)
    }

    private fun checkStored(
        key: String,
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
            fail("GROUPED_BYTES_MISMATCH", key, "stored bytes do not hash to the stored and referenced grouped digest")
        }
        if (identityOf(row) != refId) {
            fail("STORED_IDENTITY_INCONSISTENT", key, "stored identity fields do not hash to $refId")
        }
        val reconstructed = try {
            Pc2CompactBatch.reconstructOrderedDecisions(row)
        } catch (e: Exception) {
            fail("NOT_RECONSTRUCTABLE", key, e.message ?: e.javaClass.simpleName)
            return
        }
        val digest = Pc2CompactBatch.digestOrderedDecisions(reconstructed)
        if (digest != row.optString("decision_digest") || digest != ref.optString("decision_digest")) {
            fail("STORED_DIGEST_MISMATCH", key,
                "reconstruction $digest, stored ${row.optString("decision_digest")}, reference ${ref.optString("decision_digest")}")
        }
        if (source != null) {
            val firstDifference = (0 until maxOf(source.length(), reconstructed.length())).firstOrNull { i ->
                Pc2CompactBatch.canonicalJson(source.opt(i)) != Pc2CompactBatch.canonicalJson(reconstructed.opt(i))
            }
            if (firstDifference != null) fail("SOURCE_CONTENT_MISMATCH", key, "first differing decision index $firstDifference")
        }
        val expected = JSONObject()
        Pc2CompactBatch.COMPLETENESS_FIELDS.forEach { if (ref.has(it)) expected.put(it, ref.get(it)) }
        if (!Pc2CompactBatch.completenessMetadataMatches(expected, Pc2CompactBatch.groupedOf(row))) {
            fail("COMPLETENESS_MISMATCH", key, "stored completeness metadata differs from the reference")
        }
        if (row.optInt("decision_count", -1) != ref.optInt("decision_count", -2) ||
            row.optInt("distinct_decision_count", -1) != ref.optInt("distinct_decision_count", -2) ||
            row.optInt("decision_count", -1) != reconstructed.length()
        ) {
            fail("COUNT_MISMATCH", key, "stored, referenced and reconstructed counts disagree")
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

    // ---- export directory ---------------------------------------------------------

    const val MANIFEST = "manifest.json"
    val DATA_FILES = listOf("snapshots.jsonl", "batches.jsonl", "legacy.jsonl", "inventory.jsonl")

    private fun sha256Hex(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it.toInt() and 0xff) }

    private fun lines(file: File): List<String> =
        file.readText(Charsets.UTF_8).split('\n').filter { it.isNotBlank() }

    /**
     * Reads and checks an export directory. Every fault in the export itself
     * becomes an INPUT_INCOMPLETE finding; nothing is silently treated as empty.
     */
    fun run(directory: File): Report {
        val faults = mutableListOf<String>()
        val manifestFile = File(directory, MANIFEST)
        val manifest = if (manifestFile.isFile) runCatching { JSONObject(manifestFile.readText()) }.getOrNull() else null
        if (manifest == null) faults += "$MANIFEST: missing or unreadable - the export did not complete"
        if (manifest != null && !manifest.optBoolean("complete", false)) faults += "$MANIFEST: complete is not true"

        val contents = HashMap<String, List<JSONObject>>()
        for (name in DATA_FILES) {
            val file = File(directory, name)
            if (!file.isFile) {
                faults += "$name: missing"
                contents[name] = emptyList()
                continue
            }
            val raw = file.readBytes()
            val rows = runCatching { lines(file).map { JSONObject(it) } }
            if (rows.isFailure) faults += "$name: a line is not a JSON object"
            contents[name] = rows.getOrDefault(emptyList())
            val declared = manifest?.optJSONObject("files")?.optJSONObject(name)
            if (manifest != null) {
                if (declared == null) {
                    faults += "$name: not declared in the manifest"
                } else {
                    if (declared.optInt("rows", -1) != contents.getValue(name).size) {
                        faults += "$name: ${contents.getValue(name).size} rows, manifest says ${declared.optInt("rows", -1)}"
                    }
                    if (declared.optString("sha256") != sha256Hex(raw)) faults += "$name: SHA-256 differs from the manifest"
                }
            }
        }
        val exceptionsFile = File(directory, "exceptions.json")
        val exceptions = if (!exceptionsFile.isFile) emptyList() else runCatching {
            val array = JSONArray(exceptionsFile.readText())
            (0 until array.length()).map { array.getJSONObject(it) }
        }.getOrElse { faults += "exceptions.json: unreadable"; emptyList() }

        if (manifest != null && !manifest.has("malformed_batch_rows")) {
            faults += "$MANIFEST: malformed_batch_rows not recorded"
        }
        val evidence = SessionEvidence(
            istDay = manifest?.optString("ist_day").orEmpty(),
            inventory = contents.getValue("inventory.jsonl").map { row -> row.optString("poll_ts_utc") },
            malformedBatchRows = manifest?.optInt("malformed_batch_rows", 0) ?: 0,
            inputFaults = faults
        )

        val report = audit(
            evidence,
            contents.getValue("snapshots.jsonl"),
            contents.getValue("batches.jsonl"),
            contents.getValue("legacy.jsonl"),
            exceptions
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
