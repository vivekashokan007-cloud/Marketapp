package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter

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
 * ## Identity is defined by the real consumer, not by string shape
 *
 * v1 claimed to "mirror the Python consumer's precedence" but only pattern-matched strings:
 * it stripped a timezone suffix instead of converting it, compared the ISO *string's* date
 * prefix instead of the IST date, truncated fractional seconds, rejected epoch numbers and
 * `hh:mm AM` forms the consumer accepts, and accepted naive ISO stamps the consumer *rejects*.
 * Each of those can keep two representations of one observation, drop a supported observation,
 * deduplicate two different instants, or hand a conflict to the wrong row.
 *
 * [parseTs] and [pollInstant] are therefore a deliberate port of
 * `index_authority_v1.parse_ts` / `poll_time`, and the session check is applied to the
 * **IST-converted instant** exactly as `valid_spot_series` does (`dt.date() != d`).
 * `PollHistoryMergeParityTest` pins each case against the real consumer's result.
 *
 * ## What the merge guarantees
 *
 *  - rows are taken whole; fields are never combined, re-timed or interpolated across rows;
 *  - a row enters only with a trustworthy same-session identity AND verified session ownership
 *    (see [merge]'s `localSessionDate`);
 *  - on a same-identity conflict the local row wins, except where it is unusable to the real
 *    spot consumer and the remote row is not (see [usableIndexCount]);
 *  - ordering is by parsed instant, never by key string;
 *  - genuine gaps stay genuine — nothing is manufactured.
 *
 * Pure JVM (org.json + java.time, minSdk 26) so the contract is unit-testable off-device.
 */
object PollHistoryMerge {

    const val VERSION = "poll_history_merge_v2_consumer_identity_parity"

    /** Existing retained-history limit; preserved, not raised. */
    const val MAX_ROWS = 100

    /** `index_authority_v1.IST` = UTC+05:30. */
    val IST: ZoneOffset = ZoneOffset.ofHoursMinutes(5, 30)

    /** Mirrors `poll_time`'s field precedence: first key that PARSES wins, not first present. */
    private val ABSOLUTE_KEYS = arrayOf("poll_ts", "pollTs", "ts")
    private val CLOCK_KEYS = arrayOf("t", "time")

    /** How a row's claim to [merge]'s session was established. */
    enum class Ownership {
        /** An absolute instant, or a row whose explicit `date` names the session. */
        EXPLICIT,

        /** Time-only row whose owning container is verified to be this session. */
        VERIFIED_CONTAINER,

        /** Time-only row whose owning session cannot be established. Never admitted. */
        UNKNOWN
    }

    data class Result(
        val history: JSONArray,
        val kept: Int,
        val fromLocal: Int,
        val fromRemote: Int,
        val rejected: Int,
        val conflicts: Int,
        val trimmed: Int,
        /** Local rows excluded because their owning session could not be established. */
        val unownedLocal: Int = 0,
        /** Conflicts where the remote row won because the local row was unusable. */
        val remoteWonUnusable: Int = 0,
        /** True when no usable remote observation existed, so local was returned as-is. */
        val remoteUnusable: Boolean = false
    )

    // ---- identity: a port of index_authority_v1.parse_ts -------------------------------------

    /**
     * Port of `parse_ts(value, session_date)`. Returns the instant, or null when the consumer
     * would also return null.
     *
     * Mirrored exactly:
     *  - numbers: finite and > 0; a value > 1e11 is milliseconds (`v /= 1000.0`);
     *  - ISO branch taken when the string contains `T`, or is longer than 10 chars with `-` at
     *    index 4 (so a space-separated stamp is an ISO stamp, as in Python 3.11);
     *  - `Z` means `+00:00`;
     *  - **a naive ISO stamp returns null** — "naive timestamps are never assumed to be IST";
     *  - otherwise `%H:%M:%S`, then `%H:%M`, then `%I:%M %p` (upper-cased) on [sessionDate].
     *
     * Sub-second values are truncated to microseconds, matching CPython's `datetime`
     * resolution, so this never manufactures identity the consumer cannot distinguish.
     */
    fun parseTs(value: Any?, sessionDate: String?): Instant? {
        if (value == null || value === JSONObject.NULL) return null
        if (value is Boolean) return null                      // Python excludes bool explicitly
        if (value is Number) {
            var v = value.toDouble()
            if (!v.isFinite() || v <= 0.0) return null
            if (v > 1e11) v /= 1000.0
            val secs = Math.floor(v)
            // Truncate to microseconds: CPython's datetime carries microsecond resolution, so
            // keeping nanoseconds here would invent identity the consumer cannot distinguish.
            val micros = ((v - secs) * 1_000_000.0).toLong()
            return try {
                Instant.ofEpochSecond(secs.toLong(), micros * 1_000L)
            } catch (_: Exception) { null }
        }
        val s = value.toString().trim()
        if (s.isEmpty()) return null

        if (s.contains('T') || (s.length > 10 && s[4] == '-')) return parseIsoAware(s)

        val d = parseDate(sessionDate) ?: return null
        val hms = parseClock(s) ?: return null
        return OffsetDateTime.of(d.year, d.monthValue, d.dayOfMonth, hms[0], hms[1], hms[2], hms[3], IST)
            .toInstant()
    }

    /** ISO stamp -> instant. An offset is REQUIRED; naive input is rejected like the consumer. */
    private fun parseIsoAware(raw: String): Instant? {
        // `s.replace('Z', '+00:00')` in the consumer — UPPERCASE only. A lowercase `z` is left
        // in place there and fails to parse, so accepting it here would admit an instant the
        // consumer rejects. Verified against the oracle: '...09:20:00z' -> None.
        var s = raw.replace("Z", "+00:00").trim()
        // Python 3.11 accepts any single separator; normalise a space to 'T'.
        if (!s.contains('T') && s.length > 10 && s[10] == ' ') s = s.substring(0, 10) + "T" + s.substring(11)
        if (!s.contains('T')) return null

        // Offsets may be written +HH:MM, +HHMM or +HH. Normalise to +HH:MM for java.time.
        val timeStart = s.indexOf('T') + 1
        var signIdx = -1
        for (i in s.length - 1 downTo timeStart) {
            val c = s[i]
            if (c == '+' || c == '-') { signIdx = i; break }
        }
        if (signIdx < 0) return null                           // naive -> consumer returns None
        val off = s.substring(signIdx + 1)
        val normOff = when {
            Regex("^\\d{2}:\\d{2}$").matches(off) -> off
            Regex("^\\d{4}$").matches(off) -> off.substring(0, 2) + ":" + off.substring(2)
            Regex("^\\d{2}$").matches(off) -> "$off:00"
            Regex("^\\d{2}:\\d{2}:\\d{2}$").matches(off) -> off.substring(0, 5)
            else -> return null
        }
        s = s.substring(0, signIdx + 1) + normOff
        return try {
            val inst = OffsetDateTime.parse(s, DateTimeFormatter.ISO_OFFSET_DATE_TIME).toInstant()
            // Match CPython datetime resolution (oracle: '.123456789' -> '.123456').
            Instant.ofEpochSecond(inst.epochSecond, (inst.nano / 1000L) * 1000L)
        } catch (_: Exception) { null }
    }

    /** `%H:%M:%S` | `%H:%M` | `%I:%M %p` -> [h, m, s, nano], mirroring strptime's order. */
    private fun parseClock(value: String): IntArray? {
        val s = value.trim().uppercase()
        Regex("^(\\d{1,2}):(\\d{2}):(\\d{2})$").find(s)?.let { m ->
            val h = m.groupValues[1].toInt(); val mi = m.groupValues[2].toInt(); val se = m.groupValues[3].toInt()
            return if (h in 0..23 && mi in 0..59 && se in 0..59) intArrayOf(h, mi, se, 0) else null
        }
        Regex("^(\\d{1,2}):(\\d{2})$").find(s)?.let { m ->
            val h = m.groupValues[1].toInt(); val mi = m.groupValues[2].toInt()
            return if (h in 0..23 && mi in 0..59) intArrayOf(h, mi, 0, 0) else null
        }
        Regex("^(\\d{1,2}):(\\d{2})\\s*([AP]M)$").find(s)?.let { m ->
            val h12 = m.groupValues[1].toInt(); val mi = m.groupValues[2].toInt()
            if (h12 !in 1..12 || mi !in 0..59) return null
            val h = when { m.groupValues[3] == "AM" && h12 == 12 -> 0
                           m.groupValues[3] == "AM" -> h12
                           h12 == 12 -> 12
                           else -> h12 + 12 }
            return intArrayOf(h, mi, 0, 0)
        }
        return null
    }

    private fun parseDate(value: String?): LocalDate? {
        val s = (value ?: "").trim()
        if (s.length < 10) return null
        return try { LocalDate.parse(s.substring(0, 10)) } catch (_: Exception) { null }
    }

    /**
     * Port of `poll_time`: the first of `poll_ts`/`pollTs`/`ts` that PARSES, else `t`/`time`
     * (`t or time`, so a blank `t` falls through). Null when the consumer gets null, or when
     * the resulting instant's **IST date** is not [sessionDate] — the same test
     * `valid_spot_series` applies, so an offset crossing the IST date boundary resolves the way
     * the consumer resolves it rather than by the string's date prefix.
     *
     * A row carrying an explicit `date` that disagrees with [sessionDate] is rejected up front.
     * The consumer has no such field, so this is a deliberately stricter guard: a time-only row
     * dated yesterday would otherwise be re-stamped onto today by `parse_ts`.
     */
    fun pollInstant(row: JSONObject?, sessionDate: String): Instant? {
        if (row == null || sessionDate.isBlank()) return null
        val rowDate = row.optString("date", "").trim()
        if (rowDate.isNotEmpty() && rowDate != sessionDate) return null

        var dt: Instant? = null
        for (key in ABSOLUTE_KEYS) {
            dt = parseTs(row.opt(key), sessionDate)
            if (dt != null) break
        }
        if (dt == null) {
            val t = row.opt(CLOCK_KEYS[0])
            val chosen = if (t != null && t !== JSONObject.NULL && t.toString().isNotBlank()) t
                         else row.opt(CLOCK_KEYS[1])
            dt = parseTs(chosen, sessionDate)
        }
        if (dt == null) return null

        val sess = parseDate(sessionDate) ?: return null
        val istDate = OffsetDateTime.ofInstant(dt, IST).toLocalDate()
        return if (istDate == sess) dt else null
    }

    /**
     * Canonical same-session identity, as IST `HH:mm:ss` (plus `.nnnnnnnnn` when the instant
     * carries a fraction), or null when the row cannot be trusted to belong to [sessionDate].
     *
     * Derived from [pollInstant], so `2026-11-04T03:50:00Z` and `2026-11-04T09:20:00+05:30`
     * yield the same key — they are the same instant.
     */
    fun canonicalKey(row: JSONObject?, sessionDate: String): String? =
        pollInstant(row, sessionDate)?.let { keyOf(it) }

    private fun keyOf(instant: Instant): String {
        val t = OffsetDateTime.ofInstant(instant, IST)
        val base = String.format("%02d:%02d:%02d", t.hour, t.minute, t.second)
        return if (t.nano == 0) base else base + String.format(".%09d", t.nano)
    }

    // ---- usability: a port of index_authority_v1.poll_spot -----------------------------------

    private fun finitePos(v: Any?): Boolean {
        if (v == null || v === JSONObject.NULL || v is Boolean) return false
        val d = (v as? Number)?.toDouble() ?: v.toString().trim().toDoubleOrNull() ?: return false
        return d.isFinite() && d > 0.0
    }

    /**
     * How many indices this row can actually serve, per `poll_spot`: `nfSpot` else `nf` for NF,
     * `bnfSpot` else `bnf` for BNF, each finite and > 0.
     *
     * Used only to stop an unusable row from suppressing a usable one. A zero-count row is still
     * retained when nothing else holds its identity — older/partial schemas are preserved, never
     * deleted, and fields are never spliced between rows.
     */
    fun usableIndexCount(row: JSONObject?): Int {
        if (row == null) return 0
        var n = 0
        if (finitePos(row.opt("nfSpot")) || finitePos(row.opt("nf"))) n++
        if (finitePos(row.opt("bnfSpot")) || finitePos(row.opt("bnf"))) n++
        return n
    }

    /** The row's assigned poll identity, when it carries a valid one. Never inferred. */
    fun assignedPollNumber(row: JSONObject?): Int? {
        val raw = row?.opt("poll_number") ?: return null
        if (raw === JSONObject.NULL || raw is Boolean) return null
        val n = (raw as? Number)?.toInt() ?: raw.toString().trim().toIntOrNull() ?: return null
        return if (n > 0) n else null
    }

    // ---- merge --------------------------------------------------------------------------------

    /**
     * Merge [remote] into [local] for [sessionDate].
     *
     * [localSessionDate] is the session header that owns [local] (`last_poll_date`), read from
     * the *current* state under the same lock as the write. A local time-only row is admitted
     * only when that header names [sessionDate]; otherwise its owning session is unknown and it
     * is excluded rather than re-stamped onto today. Remote time-only rows take their container
     * context from the verified `poll_history_<date>` server key, which is [sessionDate] itself.
     *
     * When no usable remote observation exists, [local]'s validated rows are returned with
     * [Result.remoteUnusable] set and nothing is dropped, so a failed or empty restore can
     * neither shrink the history nor replace good state with an empty normalisation result.
     */
    fun merge(
        local: JSONArray?,
        remote: JSONArray?,
        sessionDate: String,
        localSessionDate: String? = sessionDate,
        maxRows: Int = MAX_ROWS
    ): Result {
        val localOwnsSession = (localSessionDate ?: "").trim() == sessionDate

        data class Entry(val instant: Instant, val row: JSONObject)
        val byKey = LinkedHashMap<String, Entry>()
        val localKeys = HashSet<String>()
        var rejected = 0
        var fromLocal = 0
        var fromRemote = 0
        var conflicts = 0
        var unownedLocal = 0
        var remoteWonUnusable = 0

        for (i in 0 until (local?.length() ?: 0)) {
            val row = local!!.optJSONObject(i)
            val instant = pollInstant(row, sessionDate)
            if (row == null || instant == null) { rejected++; continue }
            if (ownershipOf(row, localOwnsSession) == Ownership.UNKNOWN) { unownedLocal++; continue }
            val key = keyOf(instant)
            localKeys.add(key)
            if (byKey.put(key, Entry(instant, row)) == null) fromLocal++
        }

        var usableRemote = 0
        for (i in 0 until (remote?.length() ?: 0)) {
            val row = remote!!.optJSONObject(i)
            val instant = pollInstant(row, sessionDate)
            if (row == null || instant == null) { rejected++; continue }
            usableRemote++
            val key = keyOf(instant)
            val held = byKey[key]
            if (held == null) {
                byKey[key] = Entry(instant, row)
                fromRemote++
                continue
            }
            conflicts++
            // The local observation wins, EXCEPT where it cannot serve an index the remote row
            // can: a time-valid row with no usable spot is dropped by the real consumer, so
            // letting it hold the identity would suppress usable evidence. Rows are swapped
            // whole; no field is ever spliced between two observations.
            if (usableIndexCount(row) > usableIndexCount(held.row)) {
                byKey[key] = Entry(instant, row)
                // Only a LOCAL row being displaced reduces the local contribution. Two remote
                // rows sharing an identity must not decrement it.
                if (localKeys.remove(key)) fromLocal-- else fromRemote--
                fromRemote++
                remoteWonUnusable++
            }
        }

        if (usableRemote == 0) {
            // Accurate no-op: hand back the local history unchanged rather than a re-normalised,
            // re-sorted, re-trimmed copy of it. Nothing was learned, so nothing is rewritten.
            val unchanged = local ?: JSONArray()
            return Result(
                history = unchanged, kept = unchanged.length(), fromLocal = unchanged.length(),
                fromRemote = 0, rejected = 0, conflicts = 0, trimmed = 0,
                unownedLocal = 0, remoteWonUnusable = 0, remoteUnusable = true
            )
        }

        val ordered = byKey.values.sortedBy { it.instant }        // by INSTANT, never by key text
        val cap = if (maxRows > 0) maxRows else MAX_ROWS
        val trimmed = if (ordered.size > cap) ordered.size - cap else 0
        val keptRows = if (trimmed > 0) ordered.subList(trimmed, ordered.size) else ordered

        val out = JSONArray()
        keptRows.forEach { out.put(it.row) }
        return Result(
            history = out, kept = keptRows.size, fromLocal = fromLocal, fromRemote = fromRemote,
            rejected = rejected, conflicts = conflicts, trimmed = trimmed,
            unownedLocal = unownedLocal, remoteWonUnusable = remoteWonUnusable, remoteUnusable = false
        )
    }

    /** EXPLICIT when the row dates itself or carries an absolute instant; else container-bound. */
    fun ownershipOf(row: JSONObject?, containerOwnsSession: Boolean): Ownership {
        if (row == null) return Ownership.UNKNOWN
        if (row.optString("date", "").trim().isNotEmpty()) return Ownership.EXPLICIT
        for (key in ABSOLUTE_KEYS) {
            val raw = row.opt(key)
            if (raw == null || raw === JSONObject.NULL) continue
            // Only an absolute form is self-dating; `poll_ts: "09:20"` is still time-only.
            if (raw is Number || raw.toString().let { it.contains('T') || (it.length > 10 && it[4] == '-') }) {
                return Ownership.EXPLICIT
            }
        }
        return if (containerOwnsSession) Ownership.VERIFIED_CONTAINER else Ownership.UNKNOWN
    }

    /** The most recent row of an already-merged history, or null. */
    fun latest(history: JSONArray?): JSONObject? {
        if (history == null || history.length() == 0) return null
        return history.optJSONObject(history.length() - 1)
    }

    /**
     * The clock time to publish as `last_poll_time` for [row], derived from the parsed identity.
     *
     * `row.optString("t")` blanks the timestamp for an ISO-only row, so the stored pointer must
     * come from the identity rather than from one optional field.
     */
    fun latestClock(row: JSONObject?, sessionDate: String): String? {
        val instant = pollInstant(row, sessionDate) ?: return null
        val t = row?.optString("t", "")?.trim()
        // Keep the row's own spelling when it genuinely denotes the identity instant, so an
        // existing "09:20" is not rewritten to "09:20:00". A `t` that does not parse, or that
        // disagrees with the identity, is NOT published: a row can carry a valid `poll_ts`
        // alongside a junk `t`, and publishing that junk as last_poll_time would be worse than
        // the ISO-only blanking this helper exists to fix.
        if (!t.isNullOrEmpty() && parseTs(t, sessionDate) == instant) return t
        val d = OffsetDateTime.ofInstant(instant, IST)
        return String.format("%02d:%02d:%02d", d.hour, d.minute, d.second)
    }
}
