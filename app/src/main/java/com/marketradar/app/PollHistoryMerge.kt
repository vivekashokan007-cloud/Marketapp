package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject

/**
 * Non-destructive merge of a remote poll-history copy into the retained local history.
 *
 * Why this exists: the remote copy in `app_config.poll_history_<date>` is written only on a
 * sampled cadence (every third assigned poll, best effort), so it can legitimately lag the
 * device. Bootstrap previously *replaced* the local history with that remote copy whenever the
 * remote copy was non-empty, which can discard same-session observations the device already
 * holds. A missing stretch of observations inside the session is not recoverable later: the
 * efficiency-ratio series is rebuilt from the open reference on every poll, so one gap wide
 * enough to exceed `ER_MAX_GAP_MIN` suppresses ER for the rest of the session.
 *
 * This merge keeps every validated same-session observation from either side. It never
 * fabricates, re-times or splices observations:
 *  - rows are taken whole; fields are never combined across two observations;
 *  - a row is accepted only with a trustworthy same-session identity (see [canonicalKey]);
 *  - on a same-identity conflict the validated LOCAL row wins, and the remote row may only
 *    fill an identity the local history does not have;
 *  - genuine gaps stay genuine — nothing is interpolated and no slot is manufactured.
 *
 * Pure JVM (org.json only) so the contract is unit-testable without an Android device.
 */
object PollHistoryMerge {

    const val VERSION = "poll_history_merge_v1"

    /** Existing retained-history limit; preserved, not raised. */
    const val MAX_ROWS = 100

    data class Result(
        val history: JSONArray,
        val kept: Int,
        val fromLocal: Int,
        val fromRemote: Int,
        val rejected: Int,
        val conflicts: Int,
        val trimmed: Int
    )

    /**
     * Canonical same-session identity for one poll row, as `HH:mm:ss`, or null when the row
     * cannot be trusted to belong to [sessionDate].
     *
     * Mirrors the Python consumer's precedence (`index_authority_v1.poll_time`): an explicit
     * `poll_ts` / `pollTs` / `ts` is preferred, then `t` / `time`. A row carrying an explicit
     * `date` that disagrees with [sessionDate] is rejected outright, so a previous session's
     * rows can never be merged into today's series.
     *
     * Optional payload fields are NOT required: an older row that lacks `vix`, `dailySigma`
     * or similar is still a valid observation and is kept.
     */
    fun canonicalKey(row: JSONObject?, sessionDate: String): String? {
        if (row == null || sessionDate.isBlank()) return null
        val rowDate = row.optString("date", "").trim()
        if (rowDate.isNotEmpty() && rowDate != sessionDate) return null

        for (key in arrayOf("poll_ts", "pollTs", "ts")) {
            val raw = row.opt(key)
            if (raw is String && raw.contains('T')) {
                val datePart = raw.substringBefore('T').trim()
                if (datePart != sessionDate) return null          // explicit other-day stamp
                return normalizeClock(raw.substringAfter('T'))
            }
        }
        for (key in arrayOf("t", "time")) {
            val raw = row.opt(key)
            if (raw is String) {
                val norm = normalizeClock(raw)
                if (norm != null) return norm
            }
        }
        return null
    }

    /** "H:m", "HH:mm" or "HH:mm:ss" (optionally zone-suffixed) -> zero-padded "HH:mm:ss". */
    private fun normalizeClock(value: String?): String? {
        if (value == null) return null
        var s = value.trim()
        if (s.isEmpty()) return null
        for (cut in arrayOf("+", "-", "Z", "z")) {
            val i = s.indexOf(cut)
            if (i > 0) s = s.substring(0, i)
        }
        s = s.trim().substringBefore('.')
        val parts = s.split(":")
        if (parts.size < 2 || parts.size > 3) return null
        val h = parts[0].trim().toIntOrNull() ?: return null
        val m = parts[1].trim().toIntOrNull() ?: return null
        val sec = if (parts.size == 3) (parts[2].trim().toIntOrNull() ?: return null) else 0
        if (h !in 0..23 || m !in 0..59 || sec !in 0..59) return null
        return String.format("%02d:%02d:%02d", h, m, sec)
    }

    /**
     * Merge [remote] into [local] for [sessionDate].
     *
     * Local observations are authoritative on conflict. Unparseable or other-session rows on
     * either side are rejected rather than guessed at. If [remote] is unusable the local
     * history is returned unchanged, so a malformed or failed restore can never shrink it.
     * The result is sorted chronologically and trimmed to the most recent [maxRows].
     */
    fun merge(
        local: JSONArray?,
        remote: JSONArray?,
        sessionDate: String,
        maxRows: Int = MAX_ROWS
    ): Result {
        val byKey = LinkedHashMap<String, JSONObject>()
        var rejected = 0
        var fromLocal = 0
        var fromRemote = 0
        var conflicts = 0

        for (i in 0 until (local?.length() ?: 0)) {
            val row = local!!.optJSONObject(i)
            val key = canonicalKey(row, sessionDate)
            if (row == null || key == null) { rejected++; continue }
            if (byKey.put(key, row) == null) fromLocal++
        }

        for (i in 0 until (remote?.length() ?: 0)) {
            val row = remote!!.optJSONObject(i)
            val key = canonicalKey(row, sessionDate)
            if (row == null || key == null) { rejected++; continue }
            if (byKey.containsKey(key)) {
                conflicts++                                   // local observation is kept
            } else {
                byKey[key] = row
                fromRemote++
            }
        }

        val ordered = byKey.entries.sortedBy { it.key }.map { it.value }
        val cap = if (maxRows > 0) maxRows else MAX_ROWS
        val trimmed = if (ordered.size > cap) ordered.size - cap else 0
        val keptRows = if (trimmed > 0) ordered.subList(trimmed, ordered.size) else ordered

        val out = JSONArray()
        keptRows.forEach { out.put(it) }
        return Result(out, keptRows.size, fromLocal, fromRemote, rejected, conflicts, trimmed)
    }

    /** The most recent valid row of an already-merged history, or null. */
    fun latest(history: JSONArray?): JSONObject? {
        if (history == null || history.length() == 0) return null
        return history.optJSONObject(history.length() - 1)
    }
}
