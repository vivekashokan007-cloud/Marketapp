package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder

/**
 * b512: idempotent replay of one frozen generated-candidate envelope, without needing UPDATE.
 *
 * Why: until 2.6.80 the upload used `resolution=merge-duplicates` (INSERT .. ON CONFLICT DO UPDATE). Live RLS on
 * ml_generated_candidates has INSERT and SELECT policies for anon and no UPDATE policy, so once a cohort (or any row
 * of it) was committed, every retry of the same frozen bytes failed with HTTP 401 / 42501
 * "new row violates row-level security policy (USING expression)" for ever (reproduced in
 * tools/candidate_replay_db_suite; responses in app/src/test/resources/candidate_replay/postgrest_golden.json).
 *
 * Now: plain INSERT (no on_conflict, return=minimal). A 409/23505 means some keys already exist, so the stored rows of
 * that poll are read back through the SELECT policy and compared per row with the generated_row_witness_v2 canonical
 * form (GeneratedCohort), float8 read as exact text:
 *  - all present and identical  -> ACKNOWLEDGED / ALREADY_COMMITTED_IDENTICAL (a lost success)
 *  - some absent, rest identical-> insert only the absent rows, read back again, ACK only if now all identical
 *  - any differing / extra row  -> CONFLICT (bytes kept in quarantine as content_conflict; nothing overwritten)
 *  - auth/RLS/network/read-back failure -> RETRY (bytes and evidence retained)
 * Pure: the network is behind [Transport]. Diagnostics carry only identity, counts, HTTP and SQLSTATE codes.
 */
object GeneratedCandidateReplay {
    const val TABLE = "ml_generated_candidates"
    const val INSERT_PREFER = "return=minimal"

    data class Http(val code: Int?, val body: String?, val exception: String? = null) {
        val ok: Boolean get() = code != null && code in 200..299
        /** SQLSTATE from a PostgREST error body, e.g. 42501 / 23505; never the message text. */
        val pgCode: String? get() = runCatching { JSONObject(body ?: "").optString("code").takeIf { Regex("[0-9A-Z]{5}|PGRST[0-9]{3}").matches(it) } }.getOrNull()
    }

    interface Transport {
        fun insert(rows: JSONArray): Http
        fun readBack(path: String): Http
    }

    enum class State {
        INSERTED, ALREADY_COMMITTED_IDENTICAL, PARTIAL_COMPLETED, CONFLICT,
        RLS_OR_AUTH_REJECTED, TRANSIENT, READBACK_FAILED, READBACK_INVISIBLE, PARTIAL_RACE, PERMANENT_REJECTION
    }

    data class Attempt(
        val outcome: GeneratedCandidateOutbox.Outcome,
        val state: State,
        val http: Int? = null,
        val pg: String? = null,
        val rows: Int = 0,
        val present: Int = -1,
        val absent: Int = -1,
        val conflicting: Int = -1,
        val extra: Int = -1,
        /** Column names (never values) that differ on conflicting rows; bounded. */
        val conflictColumns: List<String> = emptyList()
    ) {
        fun describe(): String = "state=$state http=${http ?: "none"} pg=${pg ?: "none"} rows=$rows present=$present " +
            "absent=$absent conflicting=$conflicting extra=$extra" +
            (if (conflictColumns.isEmpty()) "" else " conflictColumns=${conflictColumns.joinToString(",")}")
    }

    private val FLOAT8 = GeneratedCohort.COLUMNS.filter { it.second == GeneratedCohort.T.FLOAT8 }.map { it.first }.toSet()

    /** Same string as tools/candidate_replay_db_suite/suite.py readback_path (golden-asserted). */
    fun readBackPath(pollTs: String): String {
        val select = GeneratedCohort.COLUMNS.joinToString(",") { (c, _) -> if (c in FLOAT8) "$c::text" else c }
        val ts = URLEncoder.encode(pollTs, "UTF-8").replace("+", "%20")
        return "$TABLE?select=$select&snapshot_poll_ts=eq.$ts&order=candidate_id.asc&limit=1000"
    }

    private fun failure(h: Http, rows: Int, readBack: Boolean = false): Attempt {
        val code = h.code
        return when {
            code == null -> Attempt(GeneratedCandidateOutbox.Outcome.RETRY, if (readBack) State.READBACK_FAILED else State.TRANSIENT, null, null, rows)
            code == 401 || code == 403 -> Attempt(GeneratedCandidateOutbox.Outcome.RETRY, State.RLS_OR_AUTH_REJECTED, code, h.pgCode, rows)
            readBack -> Attempt(GeneratedCandidateOutbox.Outcome.RETRY, State.READBACK_FAILED, code, h.pgCode, rows)
            GeneratedCandidateOutbox.classifyHttpFailure(code) == GeneratedCandidateOutbox.Outcome.QUARANTINE ->
                Attempt(GeneratedCandidateOutbox.Outcome.QUARANTINE, State.PERMANENT_REJECTION, code, h.pgCode, rows)
            else -> Attempt(GeneratedCandidateOutbox.Outcome.RETRY, State.TRANSIENT, code, h.pgCode, rows)
        }
    }

    private fun isDuplicate(h: Http) = h.code == 409 && h.pgCode == "23505"

    private data class Compare(val present: Int, val absent: List<JSONObject>, val conflicting: Int, val extra: Int, val cols: List<String>)

    private fun compare(frozen: List<JSONObject>, stored: JSONArray): Compare {
        val byId = LinkedHashMap<String, JSONObject>()
        var extra = 0
        for (i in 0 until stored.length()) {
            val r = stored.optJSONObject(i) ?: run { extra++; null } ?: continue
            if (byId.put(r.optString("candidate_id"), r) != null) extra++ // duplicate key cannot be ours
        }
        val absent = mutableListOf<JSONObject>()
        var present = 0
        var conflicting = 0
        val cols = sortedSetOf<String>()
        for (row in frozen) {
            val s = byId.remove(row.optString("candidate_id"))
            if (s == null) { absent += row; continue }
            present++
            val a = GeneratedCohort.canonicalLines(row)
            val b = GeneratedCohort.canonicalLines(s)
            if (a != b) {
                conflicting++
                a.indices.filter { a[it] != b[it] }.forEach { cols += a[it].substringBefore('=') }
            }
        }
        extra += byId.size // stored rows of this poll that the frozen cohort does not contain
        return Compare(present, absent, conflicting, extra, cols.take(12))
    }

    fun replay(payload: String, transport: Transport): Attempt {
        val env = JSONObject(payload)
        val rows = env.getJSONArray("rows")
        val n = rows.length()
        if (n == 0) return Attempt(GeneratedCandidateOutbox.Outcome.ACKNOWLEDGED, State.INSERTED, rows = 0)
        val frozen = (0 until n).map { rows.getJSONObject(it) }
        val pollTs = env.getString("poll_ts")

        val first = transport.insert(rows)
        if (first.ok) return Attempt(GeneratedCandidateOutbox.Outcome.ACKNOWLEDGED, State.INSERTED, first.code, null, n, n, 0, 0, 0)
        if (!isDuplicate(first)) return failure(first, n)

        val rb = transport.readBack(readBackPath(pollTs))
        val stored = if (rb.ok) runCatching { JSONArray(rb.body ?: "") }.getOrNull() else null
        if (stored == null) return failure(if (rb.ok) rb.copy(code = null) else rb, n, readBack = true).copy(http = rb.code)
        val c = compare(frozen, stored)
        fun result(o: GeneratedCandidateOutbox.Outcome, s: State, cc: Compare, http: Int?, pg: String?) =
            Attempt(o, s, http, pg, n, cc.present, cc.absent.size, cc.conflicting, cc.extra, cc.cols)
        if (c.conflicting > 0 || c.extra > 0) return result(GeneratedCandidateOutbox.Outcome.CONFLICT, State.CONFLICT, c, first.code, first.pgCode)
        // A duplicate was reported but nothing is visible: SELECT is not visible to this role -> never acknowledge.
        if (c.present == 0) return result(GeneratedCandidateOutbox.Outcome.RETRY, State.READBACK_INVISIBLE, c, rb.code, null)
        if (c.absent.isEmpty()) return result(GeneratedCandidateOutbox.Outcome.ACKNOWLEDGED, State.ALREADY_COMMITTED_IDENTICAL, c, first.code, first.pgCode)

        val missing = JSONArray().also { a -> c.absent.forEach { a.put(it) } }
        val fill = transport.insert(missing)
        if (!fill.ok) {
            return if (isDuplicate(fill)) result(GeneratedCandidateOutbox.Outcome.RETRY, State.PARTIAL_RACE, c, fill.code, fill.pgCode)
            else failure(fill, n).let { it.copy(present = c.present, absent = c.absent.size, conflicting = 0, extra = 0,
                outcome = if (it.outcome == GeneratedCandidateOutbox.Outcome.QUARANTINE) GeneratedCandidateOutbox.Outcome.RETRY else it.outcome) }
        }
        val verify = transport.readBack(readBackPath(pollTs))
        val stored2 = if (verify.ok) runCatching { JSONArray(verify.body ?: "") }.getOrNull() else null
        if (stored2 == null) return result(GeneratedCandidateOutbox.Outcome.RETRY, State.READBACK_FAILED, c, verify.code, verify.pgCode)
        val c2 = compare(frozen, stored2)
        return when {
            c2.conflicting > 0 || c2.extra > 0 -> result(GeneratedCandidateOutbox.Outcome.CONFLICT, State.CONFLICT, c2, verify.code, null)
            c2.absent.isNotEmpty() -> result(GeneratedCandidateOutbox.Outcome.RETRY, State.READBACK_INVISIBLE, c2, verify.code, null)
            else -> result(GeneratedCandidateOutbox.Outcome.ACKNOWLEDGED, State.PARTIAL_COMPLETED, c, fill.code, null)
        }
    }
}
