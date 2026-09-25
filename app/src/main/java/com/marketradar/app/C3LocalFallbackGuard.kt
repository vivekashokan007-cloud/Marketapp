package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * C3 R4: fail-closed guards for the C3 local-cache fallback. Pure JVM.
 *
 * A strictly COMPLETE local read only proves the *retained* file parses; the
 * cache is a bounded recent cache (EvaluationLocalCache trims to 36 rows once
 * it exceeds 60 rows / 64 MiB). The local fallback is therefore accepted only
 * when ALL hold:
 *  1. no durable trim marker exists for the session ([C3TrimEvidence]);
 *  2. an authoritative expected snapshot count is available
 *     ([C3ExpectedCountResolver]);
 *  3. the de-duplicated local row count AND the local frame count both equal
 *     that expected count exactly (no tolerance).
 */
data class C3TrimEvidence(
    /** "absent" | "trimmed" | "unreadable" */
    val status: String,
    val droppedRowsTotal: Int = 0,
    val maxRowsBeforeTrim: Int = 0,
    val trimEvents: Int = 0,
    val reasons: List<String> = emptyList()
) {
    val rejectsFallback: Boolean get() = status != STATUS_ABSENT

    companion object {
        const val STATUS_ABSENT = "absent"
        const val STATUS_TRIMMED = "trimmed"
        const val STATUS_UNREADABLE = "unreadable"
        val ABSENT = C3TrimEvidence(STATUS_ABSENT)
    }
}

/** Durable per-session trim sidecar written by EvaluationLocalCache at trim time. */
object C3LocalCacheTrimMarker {
    const val MARKER_VERSION = "c3_local_cache_trim_marker_v1_20260925"
    const val REASON_ROW_OR_BYTE_CAP = "row_or_byte_cap"
    const val REASON_MALFORMED_DROPPED = "malformed_line_dropped"

    fun merge(
        existing: JSONObject?,
        sessionDate: String,
        reason: String,
        droppedRows: Int,
        rowsBefore: Int,
        rowsRetained: Int,
        nowIso: String
    ): JSONObject {
        val prevReasons = existing?.optJSONArray("reasons")
        val reasons = linkedSetOf<String>()
        if (prevReasons != null) for (i in 0 until prevReasons.length()) reasons.add(prevReasons.optString(i))
        reasons.add(reason)
        return JSONObject()
            .put("marker_version", MARKER_VERSION)
            .put("session_date", sessionDate)
            .put("trimmed", true)
            .put("trim_events", (existing?.optInt("trim_events", 0) ?: 0) + 1)
            .put("dropped_rows_total", (existing?.optInt("dropped_rows_total", 0) ?: 0) + droppedRows.coerceAtLeast(0))
            .put("max_rows_before_trim", maxOf(existing?.optInt("max_rows_before_trim", 0) ?: 0, rowsBefore))
            .put("last_rows_retained", rowsRetained)
            .put("reasons", JSONArray(reasons.toList()))
            .put("first_trim_at", existing?.optString("first_trim_at", "")?.ifBlank { null } ?: nowIso)
            .put("last_trim_at", nowIso)
    }

    /** Atomic-ish write (tmp + rename, copy fallback). Throws on failure. */
    fun write(file: File, marker: JSONObject) {
        file.parentFile?.mkdirs()
        val tmp = File(file.parentFile, "${file.name}.tmp")
        tmp.writeText(marker.toString() + "\n")
        if (!tmp.renameTo(file)) {
            tmp.copyTo(file, overwrite = true)
            tmp.delete()
        }
    }

    fun readRaw(file: File): JSONObject? = try {
        if (file.exists()) JSONObject(file.readText().trim()) else null
    } catch (_: Exception) {
        null
    }

    /** Missing file => ABSENT. Present but unparseable/unknown => UNREADABLE (fail closed). */
    fun evidence(file: File): C3TrimEvidence {
        if (!file.exists()) return C3TrimEvidence.ABSENT
        val text = try { file.readText() } catch (_: Throwable) { return C3TrimEvidence(C3TrimEvidence.STATUS_UNREADABLE) }
        return parse(text)
    }

    fun parse(text: String): C3TrimEvidence {
        val obj = try { JSONObject(text.trim()) } catch (_: Exception) {
            return C3TrimEvidence(C3TrimEvidence.STATUS_UNREADABLE)
        }
        val reasons = obj.optJSONArray("reasons")?.let { a -> (0 until a.length()).map { a.optString(it) } } ?: emptyList()
        // Any marker file is trim evidence; only a well-formed trimmed=true is "trimmed".
        val status = if (obj.optBoolean("trimmed", false)) C3TrimEvidence.STATUS_TRIMMED else C3TrimEvidence.STATUS_UNREADABLE
        return C3TrimEvidence(
            status = status,
            droppedRowsTotal = obj.optInt("dropped_rows_total", 0),
            maxRowsBeforeTrim = obj.optInt("max_rows_before_trim", 0),
            trimEvents = obj.optInt("trim_events", 0),
            reasons = reasons
        )
    }
}

sealed class C3ExpectedCount {
    data class Known(val count: Int, val source: String) : C3ExpectedCount()
    data class Unavailable(val why: String) : C3ExpectedCount()
}

/**
 * Expected session snapshot count for the C3 local fallback.
 *
 * Source: the local evaluation-run ledger `input_manifest.snapshot_count`
 * (the evaluated snapshot population the labels were computed over, e.g.
 * 77/77), cross-checked against `input_manifest.snapshot_ids` (remote
 * identities reconciled for every evaluated snapshot) and the evaluation
 * prepare-complete metadata, which must say the evaluation snapshots came from
 * a COMPLETE remote read (`snapshot_source=remote_complete`). If the evaluation
 * itself used the (possibly trimmed) local cache, the count is not independent
 * of the cache and is rejected as unavailable.
 *
 * Poll counts are deliberately NOT used: polls and saved snapshots can differ
 * (incident: 78 polls, 77 snapshots) and no exact relationship is guaranteed.
 */
object C3ExpectedCountResolver {
    const val SOURCE = "evaluation_ledger_input_manifest+prepare_meta_remote_complete"
    const val SNAPSHOT_SOURCE_REMOTE_COMPLETE = "remote_complete"
    const val SNAPSHOT_SOURCE_LOCAL_FALLBACK = "local_fallback"
    const val SNAPSHOT_SOURCE_NONE = "none"

    fun resolve(sessionDate: String, ledgerRun: JSONObject?, prepareMeta: JSONObject?): C3ExpectedCount {
        if (ledgerRun == null) return C3ExpectedCount.Unavailable("no_evaluation_ledger")
        if (ledgerRun.optString("session_date") != sessionDate) return C3ExpectedCount.Unavailable("ledger_session_mismatch")
        val manifest = ledgerRun.optJSONObject("input_manifest")
            ?: return C3ExpectedCount.Unavailable("ledger_input_manifest_missing")
        val count = if (manifest.has("snapshot_count")) manifest.optInt("snapshot_count", -1) else -1
        if (count <= 0) return C3ExpectedCount.Unavailable("ledger_snapshot_count_missing")
        val ids = manifest.optJSONArray("snapshot_ids")
            ?: return C3ExpectedCount.Unavailable("ledger_snapshot_ids_missing")
        val distinctIds = (0 until ids.length()).mapNotNull { ids.opt(it)?.toString()?.takeIf { s -> s.isNotBlank() && s != "null" } }.toSet()
        if (distinctIds.size != count) return C3ExpectedCount.Unavailable("ledger_snapshot_ids_count_mismatch")
        if (prepareMeta == null) return C3ExpectedCount.Unavailable("prepare_meta_missing")
        if (prepareMeta.optString("session_date") != sessionDate) return C3ExpectedCount.Unavailable("prepare_meta_session_mismatch")
        if (prepareMeta.optInt("snapshot_count", -1) != count) return C3ExpectedCount.Unavailable("prepare_meta_count_mismatch")
        val source = prepareMeta.optString("snapshot_source", "").ifBlank { "unknown" }
        if (source != SNAPSHOT_SOURCE_REMOTE_COMPLETE) {
            return C3ExpectedCount.Unavailable("evaluation_snapshot_source_$source")
        }
        return C3ExpectedCount.Known(count, SOURCE)
    }
}
