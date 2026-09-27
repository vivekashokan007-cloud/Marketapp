package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.math.BigDecimal
import java.security.MessageDigest

/**
 * B1.1 — stable client event identity for position ticks.
 *
 * Contract `position_tick_client_event_id_v1`:
 *
 *   client_event_id = lowercase hex SHA-256 of the UTF-8 bytes of
 *       "position_tick_client_event_id_v1\n" + canonicalJson(payload)
 *
 * where payload = the tick row exactly as queued, minus the excluded keys
 * [POSITION_TICK_IDENTITY_EXCLUDED_KEYS] (server-generated `id`, `created_at`
 * and the identity field itself). Every other key present in the row is hashed,
 * including keys whose value is JSON null; an absent key and a null key are
 * different payloads.
 *
 * canonicalJson:
 * - objects: keys sorted by Kotlin String natural order (UTF-16 code units; all
 *   producer keys are ASCII), no whitespace, `{"k":v,...}`;
 * - arrays: element order preserved;
 * - strings: `"` and `\` escaped, U+0000..U+001F as `\u00xx` (lowercase hex),
 *   everything else emitted raw (UTF-8);
 * - numbers: `BigDecimal(n.toString()).stripTrailingZeros().toPlainString()`, so
 *   30, 30.0 and 3.0E1 hash identically (org.json on Android and on the JVM test
 *   classpath round-trip numbers differently through SharedPreferences);
 *   -0 → `0`; non-finite doubles → the strings `"NaN"`, `"Infinity"`, `"-Infinity"`;
 * - booleans `true`/`false`; JSON null / JSONObject.NULL → `null`.
 *
 * Exact duplicates (identical payload) therefore get the SAME id and different
 * payloads at the same (trade_id, tick_ts) get DIFFERENT ids — a retry resolves
 * as already persisted while conflicting evidence is kept.
 */
internal const val POSITION_TICK_CLIENT_EVENT_ID_CONTRACT = "position_tick_client_event_id_v1"
internal const val POSITION_TICK_CLIENT_EVENT_ID_KEY = "client_event_id"
internal val POSITION_TICK_IDENTITY_EXCLUDED_KEYS: Set<String> =
    setOf("id", "created_at", POSITION_TICK_CLIENT_EVENT_ID_KEY)

/** Name of the unique index created by 20260927090000_position_ticks_client_event_id.sql. */
internal const val POSITION_TICK_CLIENT_EVENT_ID_UNIQUE_INDEX = "position_ticks_client_event_id_uidx"

/**
 * Exact PostgREST insert shape for the production position_ticks table.
 *
 * Bulk JSON inserts require a uniform key set. A durable queue may contain rows
 * captured by different APK versions, so the upload boundary must not forward
 * each row's historical key set verbatim. This list deliberately reuses the
 * audited producer/fingerprint contract: missing nullable fields are sent as
 * JSON null and unknown/local-only fields are ignored.
 */
internal val POSITION_TICK_UPLOAD_COLUMNS: List<String> =
    POSITION_TICK_IMMUTABLE_FINGERPRINT_KEYS

internal fun positionTickUploadColumnNames(sendClientEventId: Boolean): List<String> =
    if (sendClientEventId) POSITION_TICK_UPLOAD_COLUMNS + POSITION_TICK_CLIENT_EVENT_ID_KEY
    else POSITION_TICK_UPLOAD_COLUMNS

/** Pin the accepted columns so PostgREST never infers them from a mixed-shape batch. */
internal fun positionTickInsertPath(sendClientEventId: Boolean): String =
    "position_ticks?columns=${positionTickUploadColumnNames(sendClientEventId).joinToString(",")}"

internal data class PositionTickUploadBatch(
    val rows: JSONArray,
    val sendClientEventId: Boolean,
    val errorDetail: String? = null
)

/** Defensive normalization for every caller, including callers outside the drain. */
internal fun normalizePositionTickUploadBatch(rows: JSONArray): PositionTickUploadBatch {
    val first = rows.optJSONObject(0)
        ?: return PositionTickUploadBatch(JSONArray(), false, "local_non_object_row")
    val sendClientEventId = first.has(POSITION_TICK_CLIENT_EVENT_ID_KEY)
    val normalized = JSONArray()
    for (i in 0 until rows.length()) {
        val row = rows.optJSONObject(i)
            ?: return PositionTickUploadBatch(JSONArray(), sendClientEventId, "local_non_object_row")
        if (row.has(POSITION_TICK_CLIENT_EVENT_ID_KEY) != sendClientEventId) {
            return PositionTickUploadBatch(JSONArray(), sendClientEventId, "local_mixed_identity_shape")
        }
        normalized.put(positionTickUploadRow(row, sendClientEventId))
    }
    return PositionTickUploadBatch(normalized, sendClientEventId)
}

/**
 * PRODUCER-KEY GATE (compile time). While false, `client_event_id` is computed and
 * kept in the LOCAL queue for chunk acknowledgement only and is stripped from every
 * upload body, so this build sends exactly the 28-column production payload.
 *
 * Flip to true ONLY in a reviewed commit after the migration has been applied with
 * Vivek's approval and the column + unique index were read back from production.
 * Even then the runtime probe ([positionTickClientEventIdSendAllowed]) must confirm
 * the column before the key is sent. Pinned false by PositionTickDrainTest.
 */
internal const val POSITION_TICK_CLIENT_EVENT_ID_SEND_COMPILED = false

/** Both keys of the gate must be on; either one off keeps the key out of the request. */
internal fun positionTickClientEventIdSendAllowed(
    compiledEnabled: Boolean,
    productionColumnConfirmed: Boolean?
): Boolean = compiledEnabled && productionColumnConfirmed == true

private val HEX = "0123456789abcdef".toCharArray()
private val CLIENT_EVENT_ID_RE = Regex("^[0-9a-f]{64}$")

internal fun isValidPositionTickClientEventId(value: String?): Boolean =
    value != null && CLIENT_EVENT_ID_RE.matches(value)

/** Compute the v1 identity of [row] (never reads or trusts an existing id field). */
internal fun computePositionTickClientEventId(row: JSONObject): String {
    val sb = StringBuilder(4096)
    sb.append(POSITION_TICK_CLIENT_EVENT_ID_CONTRACT).append('\n')
    appendCanonicalObject(sb, row, POSITION_TICK_IDENTITY_EXCLUDED_KEYS)
    val digest = MessageDigest.getInstance("SHA-256").digest(sb.toString().toByteArray(Charsets.UTF_8))
    val out = CharArray(digest.size * 2)
    digest.forEachIndexed { i, b ->
        val v = b.toInt() and 0xff
        out[i * 2] = HEX[v ushr 4]
        out[i * 2 + 1] = HEX[v and 0x0f]
    }
    return String(out)
}

/** Canonical JSON of [row] minus the identity-excluded keys (exposed for tests/audit). */
internal fun positionTickCanonicalPayload(row: JSONObject): String {
    val sb = StringBuilder(4096)
    appendCanonicalObject(sb, row, POSITION_TICK_IDENTITY_EXCLUDED_KEYS)
    return sb.toString()
}

/**
 * Ensure every row in [queue] carries a valid v1 identity. Rows with a missing or
 * malformed id get one computed from their own content. A row whose stored id does
 * not match its content is RE-derived (the stored value is never trusted blindly),
 * so an edited/corrupted id cannot make distinct evidence collide.
 * Mutates the row objects in place; returns how many rows were (re)assigned.
 * Never removes, reorders or otherwise changes rows.
 */
internal fun ensurePositionTickIdentities(queue: JSONArray): Int {
    var assigned = 0
    for (i in 0 until queue.length()) {
        val row = queue.optJSONObject(i) ?: continue
        val expected = computePositionTickClientEventId(row)
        val existing = if (row.has(POSITION_TICK_CLIENT_EVENT_ID_KEY) && !row.isNull(POSITION_TICK_CLIENT_EVENT_ID_KEY)) {
            row.optString(POSITION_TICK_CLIENT_EVENT_ID_KEY, "")
        } else {
            null
        }
        if (existing != expected) {
            row.put(POSITION_TICK_CLIENT_EVENT_ID_KEY, expected)
            assigned += 1
        }
    }
    return assigned
}

/** Row identity for ack bookkeeping (computes when absent; never null for an object row). */
internal fun positionTickRowIdentity(row: JSONObject): String {
    val stored = row.optString(POSITION_TICK_CLIENT_EVENT_ID_KEY, "")
    return if (isValidPositionTickClientEventId(stored)) stored else computePositionTickClientEventId(row)
}

/**
 * Build the exact, uniform upload row. Missing historical columns become JSON
 * null, extra/local-only columns are excluded, and the queued row is not modified.
 * With the gate off the identity key is omitted so the request matches the current
 * production schema.
 */
internal fun positionTickUploadRow(row: JSONObject, sendClientEventId: Boolean): JSONObject {
    val out = JSONObject()
    for (key in POSITION_TICK_UPLOAD_COLUMNS) {
        out.put(key, if (row.has(key)) row.opt(key) ?: JSONObject.NULL else JSONObject.NULL)
    }
    if (sendClientEventId) out.put(POSITION_TICK_CLIENT_EVENT_ID_KEY, positionTickRowIdentity(row))
    return out
}

private fun appendCanonicalObject(sb: StringBuilder, obj: JSONObject, excluded: Set<String>) {
    val keys = ArrayList<String>()
    val it = obj.keys()
    while (it.hasNext()) {
        val k = it.next()
        if (k !in excluded) keys.add(k)
    }
    keys.sort()
    sb.append('{')
    keys.forEachIndexed { idx, k ->
        if (idx > 0) sb.append(',')
        appendCanonicalString(sb, k)
        sb.append(':')
        appendCanonicalValue(sb, obj.opt(k))
    }
    sb.append('}')
}

private fun appendCanonicalValue(sb: StringBuilder, v: Any?) {
    when {
        v == null || v == JSONObject.NULL -> sb.append("null")
        v is JSONObject -> appendCanonicalObject(sb, v, emptySet())
        v is JSONArray -> {
            sb.append('[')
            for (i in 0 until v.length()) {
                if (i > 0) sb.append(',')
                appendCanonicalValue(sb, v.opt(i))
            }
            sb.append(']')
        }
        v is Boolean -> sb.append(if (v) "true" else "false")
        v is Number -> appendCanonicalNumber(sb, v)
        v is Map<*, *> -> appendCanonicalObject(sb, JSONObject(v), emptySet())
        v is Collection<*> -> appendCanonicalValue(sb, JSONArray(v))
        else -> appendCanonicalString(sb, v.toString())
    }
}

private fun appendCanonicalNumber(sb: StringBuilder, n: Number) {
    if (n is Double && !n.isFinite()) { appendCanonicalString(sb, n.toString()); return }
    if (n is Float && !n.isFinite()) { appendCanonicalString(sb, n.toDouble().toString()); return }
    val bd = try {
        if (n is BigDecimal) n else BigDecimal(n.toString())
    } catch (_: NumberFormatException) {
        appendCanonicalString(sb, n.toString()); return
    }
    val s = if (bd.signum() == 0) "0" else bd.stripTrailingZeros().toPlainString()
    sb.append(s)
}

private fun appendCanonicalString(sb: StringBuilder, s: String) {
    sb.append('"')
    for (ch in s) {
        when {
            ch == '"' -> sb.append("\\\"")
            ch == '\\' -> sb.append("\\\\")
            ch.code < 0x20 -> {
                sb.append("\\u00")
                sb.append(HEX[(ch.code ushr 4) and 0xf])
                sb.append(HEX[ch.code and 0xf])
            }
            else -> sb.append(ch)
        }
    }
    sb.append('"')
}
