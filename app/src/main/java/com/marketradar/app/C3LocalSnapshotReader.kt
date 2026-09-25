package com.marketradar.app

import org.json.JSONObject
import java.io.BufferedReader

/**
 * C3 R3: strict completeness read of the local brain-snapshot JSONL cache for
 * C3 finalization only. Pure JVM so it is unit-testable.
 *
 * Unlike the lenient [EvaluationLocalCache.forEachBrainSnapshot] (which skips
 * malformed lines and swallows read errors, returning a partial count — its
 * semantics are unchanged for the evaluation-input path), this reader reports:
 *  - COMPLETE: every non-blank line parsed as a JSON object and the stream was
 *    read to EOF without error.
 *  - EMPTY: no file, or a file with no rows.
 *  - FAILED(local_malformed_line | local_read_error, lineNumber, rowsBefore).
 *
 * Rows ARE streamed to [onRow] as they are read (so peak memory stays one full
 * snapshot); callers MUST buffer only compact frames and discard them unless
 * the result is COMPLETE. [C3FrameCollector] does exactly that.
 * Duplicate keys are skipped exactly as the lenient reader does (cache dedup),
 * and blank lines are ignored (they carry no data).
 */
sealed class C3LocalReadResult {
    data class Complete(val rows: Int, val duplicatesSkipped: Int, val blankLines: Int) : C3LocalReadResult()
    data class Empty(val reason: String) : C3LocalReadResult()
    data class Failed(
        val reason: String,
        val lineNumber: Int,
        val rowsBeforeFailure: Int,
        val detail: String = ""
    ) : C3LocalReadResult()
}

object C3LocalReadFailure {
    const val LOCAL_MALFORMED_LINE = "local_malformed_line"
    const val LOCAL_READ_ERROR = "local_read_error"
}

object C3LocalSnapshotReader {
    const val EMPTY_NO_FILE = "no_file"
    const val EMPTY_NO_ROWS = "no_rows"

    /** Same key rule as EvaluationLocalCache.snapshotKey (kept private there). */
    fun snapshotKey(row: JSONObject): String {
        return row.optString("id").ifBlank {
            "${row.optString("poll_ts")}|${row.optString("recommendation_id")}"
        }.ifBlank {
            row.toString()
        }
    }

    /**
     * @param open returns a reader for the cache file, or null when the file
     *   does not exist. Exceptions from [open] or while reading are FAILED.
     */
    fun readStrict(open: () -> BufferedReader?, onRow: (JSONObject) -> Unit): C3LocalReadResult {
        var rows = 0
        var dups = 0
        var blanks = 0
        var lineNumber = 0
        val seen = HashSet<String>()
        val reader = try {
            open() ?: return C3LocalReadResult.Empty(EMPTY_NO_FILE)
        } catch (t: Throwable) {
            return C3LocalReadResult.Failed(C3LocalReadFailure.LOCAL_READ_ERROR, 0, 0, t.javaClass.simpleName)
        }
        try {
            reader.use { r ->
                while (true) {
                    val line = r.readLine() ?: break
                    lineNumber += 1
                    if (line.isBlank()) {
                        blanks += 1
                        continue
                    }
                    val row = try {
                        JSONObject(line)
                    } catch (_: Exception) {
                        return C3LocalReadResult.Failed(
                            C3LocalReadFailure.LOCAL_MALFORMED_LINE, lineNumber, rows, "json_object_parse_failed"
                        )
                    }
                    val key = snapshotKey(row)
                    if (key.isNotBlank() && !seen.add(key)) {
                        dups += 1
                        continue
                    }
                    onRow(row)
                    rows += 1
                }
            }
        } catch (t: Throwable) {
            return C3LocalReadResult.Failed(C3LocalReadFailure.LOCAL_READ_ERROR, lineNumber, rows, t.javaClass.simpleName)
        }
        if (rows == 0) return C3LocalReadResult.Empty(EMPTY_NO_ROWS)
        return C3LocalReadResult.Complete(rows, dups, blanks)
    }

    fun describe(result: C3LocalReadResult): String = when (result) {
        is C3LocalReadResult.Complete -> "local=COMPLETE rows=${result.rows} dups=${result.duplicatesSkipped} blank=${result.blankLines}"
        is C3LocalReadResult.Empty -> "local=EMPTY reason=${result.reason}"
        is C3LocalReadResult.Failed ->
            "local=FAILED reason=${result.reason} line=${result.lineNumber} rowsBefore=${result.rowsBeforeFailure} detail=${result.detail}"
    }
}
