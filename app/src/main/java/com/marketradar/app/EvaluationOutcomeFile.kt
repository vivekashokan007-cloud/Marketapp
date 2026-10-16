package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** Replayable disk-backed outcomes. Only one row or one upload batch owns payloads in memory. */
internal object EvaluationOutcomeFile {
    data class Validated(val file: File, val count: Int, val duplicateCount: Int,
                         val compositeKeys: Set<String>, val rejectedCount: Int)

    /** Strict top-level object array reader, also usable by JVM recovery tests. */
    fun forEach(file: File, consume: (JSONObject) -> Unit) {
        file.bufferedReader().use { reader ->
            fun next(): Int { var c: Int; do { c = reader.read() } while (c >= 0 && c.toChar().isWhitespace()); return c }
            check(next() == '['.code) { "Evaluation output ${file.name} is not a JSON array" }
            var token = next()
            if (token != ']'.code) {
                while (true) {
                    check(token == '{'.code) { "Evaluation output ${file.name} has a non-object row" }
                    val text = StringBuilder("{")
                    var depth = 1
                    var quoted = false
                    var escaped = false
                    while (depth > 0) {
                        val c = reader.read()
                        check(c >= 0) { "Evaluation output ${file.name} is truncated" }
                        val ch = c.toChar()
                        text.append(ch)
                        if (quoted) {
                            if (escaped) escaped = false
                            else if (ch == '\\') escaped = true
                            else if (ch == '"') quoted = false
                        } else when (ch) {
                            '"' -> quoted = true
                            '{', '[' -> depth++
                            '}', ']' -> depth--
                        }
                    }
                    consume(JSONObject(text.toString()))
                    token = next()
                    if (token == ']'.code) break
                    check(token == ','.code) { "Evaluation output ${file.name} is malformed" }
                    token = next()
                }
            }
            check(next() == -1) { "Evaluation output ${file.name} has trailing data" }
        }
    }

    /** Validate the ENTIRE file before HTTP writes. Keep only identities and content hashes. */
    fun validate(sessionDate: String, source: File, target: File): Validated {
        require(source.canonicalFile != target.canonicalFile)
        target.parentFile?.mkdirs()
        val temp = File(target.parentFile, target.name + ".tmp")
        val hashes = linkedMapOf<String, String>()
        var duplicates = 0
        var rejected = 0
        try {
            temp.bufferedWriter().use { writer ->
                writer.write("[")
                forEach(source) { row ->
                    val key = EvaluationIdentity.validatedOutcomeKey(sessionDate, row)
                    val digest = EvaluationIdentity.contentDigest(row)
                    val previous = hashes[key]
                    check(previous == null || previous == digest) {
                        "EVAL_OUTCOME_IDENTITY_CONFLICT: $key; local results retained"
                    }
                    if (previous == null) {
                        if (hashes.isNotEmpty()) writer.write(",")
                        writer.write(row.toString())
                        hashes[key] = digest
                        if (key.endsWith("|rejected")) rejected++
                    } else duplicates++
                }
                writer.write("]")
            }
            check(temp.renameTo(target)) { "Could not commit validated evaluation output" }
            return Validated(target, hashes.size, duplicates, hashes.keys.toSet(), rejected)
        } finally { temp.delete() }
    }

    fun batches(file: File, maxRows: Int = 32, maxBytes: Int = 512 * 1024, consume: (JSONArray) -> Unit) {
        require(maxRows > 0 && maxBytes > 0)
        var rows = JSONArray()
        var bytes = 2
        fun flush() { if (rows.length() > 0) { consume(rows); rows = JSONArray(); bytes = 2 } }
        forEach(file) { row ->
            val size = row.toString().toByteArray(Charsets.UTF_8).size + 1
            if (rows.length() > 0 && (rows.length() >= maxRows || bytes.toLong() + size > maxBytes)) flush()
            rows.put(row)
            bytes += size
        }
        flush()
    }
}
