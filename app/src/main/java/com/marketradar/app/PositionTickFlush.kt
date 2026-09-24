package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.util.LinkedHashMap

/** Confirmed empty/success insert. */
internal const val POSITION_TICK_FLUSH_OK = "ok"

/**
 * Server conflict verified as exact already-present duplicates for every
 * requested tick identity with matching immutable contents. Only this path may
 * drain on HTTP 409.
 */
internal const val POSITION_TICK_FLUSH_IDEMPOTENT_CONFLICT = "idempotent_conflict"

/**
 * HTTP 409 (or conflict) without proof that every pending tick is already
 * persisted with matching contents. Fail closed — retain the queue.
 */
internal const val POSITION_TICK_FLUSH_CONFLICT_UNVERIFIED = "conflict_unverified"

/** Missing/invalid config or auth (401/403). */
internal const val POSITION_TICK_FLUSH_CONFIG_AUTH = "config_auth"

/** Schema/payload rejection (400/404/415/422). */
internal const val POSITION_TICK_FLUSH_SCHEMA_PAYLOAD = "schema_payload"

/** Transient network/timeout style failures. */
internal const val POSITION_TICK_FLUSH_TRANSIENT_NETWORK = "transient_network"

/** Upstream 5xx. */
internal const val POSITION_TICK_FLUSH_SERVER_5XX = "server_5xx"

/** Transport/IO exception (sanitized type only). */
internal const val POSITION_TICK_FLUSH_TRANSPORT = "transport"

/** Unclassified. */
internal const val POSITION_TICK_FLUSH_UNKNOWN = "unknown"

/** Fixed categories used in privacy-safe detail strings (never raw bodies). */
internal const val POSITION_TICK_DETAIL_OK = "ok"
internal const val POSITION_TICK_DETAIL_EMPTY = "empty"
internal const val POSITION_TICK_DETAIL_CONFLICT_UNVERIFIED = "conflict_unverified"
internal const val POSITION_TICK_DETAIL_IDEMPOTENT_VERIFIED = "idempotent_verified"

/**
 * Privacy-safe insert/flush diagnostic. Never carries credentials, tokens, raw
 * response bodies, submitted tick rows, trade IDs, premiums, or position values —
 * only class, status, allowlisted server code, exception class, fixed detail, and
 * row count.
 */
data class PositionTickInsertResult(
    val success: Boolean,
    val persisted: Boolean,
    val failureClass: String,
    val httpStatus: Int?,
    val exceptionType: String?,
    val detail: String,
    val rowCount: Int,
    val allowlistedServerCode: String? = null
)

/** Result of admitting new ticks into a bounded pending queue without dropping old rows. */
data class PositionTickQueueAdmission(
    val queue: JSONArray,
    val admitted: Int,
    val rejected: Int,
    val overflowActive: Boolean,
    /** False whenever new ticks cannot be retained (overflow) — never claim full tracking. */
    val trackingComplete: Boolean
)

/** Result of client-side dedupe that never silently collapses conflicting payloads. */
data class PositionTickDedupeResult(
    val queue: JSONArray,
    val exactDupDropped: Int,
    val contentConflicts: Int
)

/**
 * Classify an insert outcome for observability. Failed flushes must set
 * [PositionTickInsertResult.persisted] = false so callers never drain the queue
 * or claim durable history.
 *
 * HTTP 409 drains ONLY when [verifiedExactDuplicates] is true (caller proved every
 * requested tick already persisted for exact identity with matching immutable
 * contents). Generic/unverified 409 → [POSITION_TICK_FLUSH_CONFLICT_UNVERIFIED].
 *
 * [allowlistedServerErrorCode] may be a short PostgREST/Postgres code (e.g. 23505);
 * never pass raw response bodies or row values here.
 */
internal fun classifyPositionTickFlushFailure(
    httpStatus: Int?,
    httpMessage: String?,
    exceptionType: String?,
    exceptionMessage: String?,
    allowlistedServerErrorCode: String?,
    rowCount: Int,
    verifiedExactDuplicates: Boolean = false
): PositionTickInsertResult {
    if (exceptionType != null) {
        val simple = sanitizeFlushExceptionType(exceptionType)
        val className = classifyTransportException(simple, exceptionMessage)
        return PositionTickInsertResult(
            success = false,
            persisted = false,
            failureClass = className,
            httpStatus = null,
            exceptionType = simple,
            detail = privacySafeFlushDetail(className, null, null, simple),
            rowCount = rowCount,
            allowlistedServerCode = null
        )
    }

    val code = httpStatus
    if (code != null && code in 200..299) {
        return PositionTickInsertResult(
            success = true,
            persisted = true,
            failureClass = POSITION_TICK_FLUSH_OK,
            httpStatus = code,
            exceptionType = null,
            detail = POSITION_TICK_DETAIL_OK,
            rowCount = rowCount,
            allowlistedServerCode = null
        )
    }

    if (code == 409) {
        if (verifiedExactDuplicates) {
            return PositionTickInsertResult(
                success = true,
                persisted = true,
                failureClass = POSITION_TICK_FLUSH_IDEMPOTENT_CONFLICT,
                httpStatus = code,
                exceptionType = null,
                detail = POSITION_TICK_DETAIL_IDEMPOTENT_VERIFIED,
                rowCount = rowCount,
                allowlistedServerCode = normalizeAllowlistedServerCode(allowlistedServerErrorCode)
            )
        }
        return PositionTickInsertResult(
            success = false,
            persisted = false,
            failureClass = POSITION_TICK_FLUSH_CONFLICT_UNVERIFIED,
            httpStatus = code,
            exceptionType = null,
            detail = POSITION_TICK_DETAIL_CONFLICT_UNVERIFIED,
            rowCount = rowCount,
            allowlistedServerCode = normalizeAllowlistedServerCode(allowlistedServerErrorCode)
        )
    }

    val failureClass = when (code) {
        401, 403 -> POSITION_TICK_FLUSH_CONFIG_AUTH
        400, 404, 415, 422 -> POSITION_TICK_FLUSH_SCHEMA_PAYLOAD
        in 500..599 -> POSITION_TICK_FLUSH_SERVER_5XX
        408, 429 -> POSITION_TICK_FLUSH_TRANSIENT_NETWORK
        null -> POSITION_TICK_FLUSH_UNKNOWN
        else -> POSITION_TICK_FLUSH_UNKNOWN
    }

    return PositionTickInsertResult(
        success = false,
        persisted = false,
        failureClass = failureClass,
        httpStatus = code,
        exceptionType = null,
        detail = privacySafeFlushDetail(
            failureClass,
            code,
            normalizeAllowlistedServerCode(allowlistedServerErrorCode),
            null
        ),
        rowCount = rowCount,
        allowlistedServerCode = normalizeAllowlistedServerCode(allowlistedServerErrorCode)
    )
}

/**
 * Build the LogBuffer / Log line for an insert failure. Contains only status,
 * allowlisted server code, row count, exception class, and fixed failure category.
 * Never includes raw/truncated bodies or tick/position values.
 */
internal fun formatPositionTickInsertFailLog(result: PositionTickInsertResult): String {
    val status = result.httpStatus ?: -1
    val code = result.allowlistedServerCode ?: "-"
    val ex = result.exceptionType ?: "-"
    return "POSITION_TICK_INSERT_FAIL: class=${result.failureClass} status=$status " +
        "server_code=$code rows=${result.rowCount} ex=$ex detail=${result.detail} persisted=false"
}

internal fun formatPositionTickFlushFailLog(
    consecutive: Int,
    pending: Int,
    result: PositionTickInsertResult,
    backoffMs: Long,
    overflowActive: Boolean,
    trackingComplete: Boolean
): String {
    val status = result.httpStatus ?: -1
    val code = result.allowlistedServerCode ?: "-"
    val ex = result.exceptionType ?: "-"
    return "POSITION_TICK_FLUSH_FAIL: consecutive=$consecutive pending=$pending " +
        "class=${result.failureClass} status=$status server_code=$code " +
        "ex=$ex backoff_ms=$backoffMs overflow=$overflowActive " +
        "tracking_complete=$trackingComplete persisted=false detail=${result.detail}"
}

internal fun privacySafeFlushDetail(
    failureClass: String,
    httpStatus: Int?,
    allowlistedServerCode: String?,
    exceptionType: String?
): String {
    val parts = mutableListOf<String>()
    parts.add("class=$failureClass")
    if (httpStatus != null) parts.add("http=$httpStatus")
    if (!allowlistedServerCode.isNullOrBlank()) parts.add("server_code=$allowlistedServerCode")
    if (!exceptionType.isNullOrBlank()) parts.add("ex=$exceptionType")
    return parts.joinToString(" ")
}

/**
 * Extract only an allowlisted PostgREST/Postgres error code from a response body.
 * Returns null unless the code alone is recognized — never returns message/details/
 * hint/row payloads. Safe to call with arbitrary bodies containing secrets or values.
 */
internal fun extractAllowlistedServerErrorCode(rawBody: String?): String? {
    if (rawBody.isNullOrBlank()) return null
    val code = try {
        val obj = JSONObject(rawBody)
        obj.optString("code", "").ifBlank {
            obj.optJSONObject("error")?.optString("code", "") ?: ""
        }
    } catch (_: Exception) {
        // Fallback: look for a bare "code":"XXXX" without parsing values.
        val m = Regex(""""code"\s*:\s*"([A-Za-z0-9_]+)"""").find(rawBody)
        m?.groupValues?.getOrNull(1).orEmpty()
    }
    return normalizeAllowlistedServerCode(code)
}

internal fun normalizeAllowlistedServerCode(raw: String?): String? {
    if (raw.isNullOrBlank()) return null
    val c = raw.trim()
    // Postgres SQLSTATE (5 chars) or PostgREST PGRST* codes only.
    val ok = Regex("""^(?:[0-9A-Z]{5}|PGRST[0-9A-Z]+)$""", RegexOption.IGNORE_CASE).matches(c)
    return if (ok) c.take(32) else null
}

internal fun sanitizeFlushExceptionType(raw: String): String {
    val trimmed = raw.trim()
    val simple = trimmed.substringAfterLast('.').ifBlank { trimmed }
    return simple.take(80).replace(Regex("[^A-Za-z0-9_$.]"), "")
}

internal fun classifyTransportException(simpleType: String, message: String?): String {
    val blob = ((simpleType) + " " + (message ?: "")).lowercase()
    return when {
        "timeout" in blob || "timedout" in blob || "sockettimeout" in blob ->
            POSITION_TICK_FLUSH_TRANSIENT_NETWORK
        "unknownhost" in blob || "connectexception" in blob || "connection reset" in blob ||
            "network" in blob || "unreachable" in blob ->
            POSITION_TICK_FLUSH_TRANSIENT_NETWORK
        "ssl" in blob || "certificate" in blob || "handshake" in blob ->
            POSITION_TICK_FLUSH_TRANSPORT
        else -> POSITION_TICK_FLUSH_TRANSPORT
    }
}

/**
 * Legacy sanitizer retained for non-body exception type/message scrubbing only.
 * Must NOT be used to put response bodies into diagnostics — prefer
 * [extractAllowlistedServerErrorCode] + [privacySafeFlushDetail].
 */
internal fun sanitizeFlushDiagText(raw: String?, maxLen: Int = 180): String {
    if (raw.isNullOrBlank()) return ""
    var s = raw.replace('\n', ' ').replace('\r', ' ')
    s = Regex("""(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*""").replace(s, "$1***")
    s = Regex("""(?i)(apikey["'\s:=]+)[A-Za-z0-9\-._~+/]+=*""").replace(s, "$1***")
    s = Regex("""(?i)(authorization["'\s:=]+)[^\s,;]+""").replace(s, "$1***")
    s = Regex("""(?i)(eyJ[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-._]+)""").replace(s, "***jwt***")
    s = Regex("""(?i)(sb_publishable_|sb_secret_)[A-Za-z0-9_]+""").replace(s, "***key***")
    if (s.length > maxLen) s = s.take(maxLen) + "…"
    return s
}

/**
 * Bounded retry backoff from consecutive failures + class. Observable via logs.
 * Config/schema/unverified-conflict failures back off faster to the cap so we do
 * not hammer a bad payload.
 */
internal fun computePositionTickFlushBackoffMs(consecutiveFailures: Int, failureClass: String): Long {
    val base = 60_000L
    val cappedFailures = consecutiveFailures.coerceIn(0, 10)
    val mult = when (failureClass) {
        POSITION_TICK_FLUSH_CONFIG_AUTH,
        POSITION_TICK_FLUSH_SCHEMA_PAYLOAD,
        POSITION_TICK_FLUSH_CONFLICT_UNVERIFIED ->
            (cappedFailures).coerceAtLeast(1).coerceAtMost(5)
        POSITION_TICK_FLUSH_SERVER_5XX, POSITION_TICK_FLUSH_TRANSIENT_NETWORK, POSITION_TICK_FLUSH_TRANSPORT ->
            (cappedFailures).coerceAtLeast(1).coerceAtMost(8)
        else -> (cappedFailures).coerceAtLeast(1).coerceAtMost(6)
    }
    return (base * mult).coerceAtMost(5 * 60_000L)
}

/**
 * Admit [incoming] into [existing] without ever deleting already-queued rows.
 * When capacity is exhausted, new rows are rejected and overflow is marked so
 * tracking degradation is visible (trackingComplete=false). Does not disguise
 * drops as success.
 */
internal fun admitPositionTicksToBoundedQueue(
    existing: JSONArray,
    incoming: JSONArray,
    maxPending: Int
): PositionTickQueueAdmission {
    val max = maxPending.coerceAtLeast(0)
    val out = JSONArray()
    for (i in 0 until existing.length()) {
        existing.optJSONObject(i)?.let { out.put(it) }
    }
    // If somehow over capacity already, preserve all existing and reject all new.
    var admitted = 0
    var rejected = 0
    for (i in 0 until incoming.length()) {
        val row = incoming.optJSONObject(i) ?: continue
        if (out.length() < max) {
            out.put(row)
            admitted += 1
        } else {
            rejected += 1
        }
    }
    val overflow = rejected > 0 || out.length() > max
    return PositionTickQueueAdmission(
        queue = out,
        admitted = admitted,
        rejected = rejected,
        overflowActive = overflow,
        trackingComplete = !overflow
    )
}

/**
 * De-dupe pending queue by trade_id|tick_ts.
 *
 * Generation path uses valuation_ts as tick_ts (one tick per trade per cycle), so
 * the composite key normally uniquely identifies an immutable tick. Same key with
 * identical payload → keep one. Same key with different payloads → retain ALL
 * conflicting rows and report contentConflicts (never silently choose one).
 */
internal fun dedupePositionTicksByTradeTs(queue: JSONArray): PositionTickDedupeResult {
    if (queue.length() <= 1) {
        return PositionTickDedupeResult(queue = queue, exactDupDropped = 0, contentConflicts = 0)
    }
    val groups = LinkedHashMap<String, MutableList<JSONObject>>()
    for (i in 0 until queue.length()) {
        val row = queue.optJSONObject(i) ?: continue
        val tradeId = row.optString("trade_id", "").ifBlank { "__missing__" }
        val tickTs = row.optString("tick_ts", "").ifBlank { "__missing__" }
        val key = "$tradeId|$tickTs"
        groups.getOrPut(key) { mutableListOf() }.add(row)
    }
    val out = JSONArray()
    var exactDropped = 0
    var contentConflicts = 0
    for ((_, rows) in groups) {
        if (rows.size == 1) {
            out.put(rows[0])
            continue
        }
        val fingerprints = rows.map { positionTickImmutableFingerprint(it) }.toSet()
        if (fingerprints.size == 1) {
            out.put(rows.last())
            exactDropped += rows.size - 1
        } else {
            // Distinct payloads sharing a key: retain all; do not silently pick one.
            contentConflicts += 1
            rows.forEach { out.put(it) }
        }
    }
    return PositionTickDedupeResult(
        queue = out,
        exactDupDropped = exactDropped,
        contentConflicts = contentConflicts
    )
}

/**
 * Fingerprint of immutable tick contents used to detect same-key conflicts.
 *
 * Must cover every field [PositionTickService.buildTickRow] writes into the
 * persisted payload that affects valuation or provenance. R8 expanded the set
 * after Codex showed same trade_id|tick_ts + equal marks/P&L but different
 * lot authority / policy_trace_json were treated as exact duplicates and one
 * was silently dropped.
 *
 * Intentional exclusions vs builder:
 * - None. Builder keys are: trade_id, session_date, tick_ts, source, auth_source,
 *   index_key, strategy_type, status, leg_count, quantity_units, contract_lot_size,
 *   number_of_lots, lot_authoritative, valuation_quality, mark_basis,
 *   executable_mark, mid_mark, ltp_mark, current_pnl, current_pnl_r, running_mae,
 *   running_mfe, policy_action, policy_reason, policy_trace_json, legs_json.
 * - Keep this list in lockstep with buildTickRow; missing valuation/provenance
 *   fields are a data-loss risk under same-key dedupe.
 */
internal val POSITION_TICK_IMMUTABLE_FINGERPRINT_KEYS: List<String> = listOf(
    "trade_id", "tick_ts", "session_date", "source", "auth_source",
    "index_key", "strategy_type", "status", "leg_count",
    "quantity_units", "contract_lot_size", "number_of_lots", "lot_authoritative",
    "valuation_quality", "mark_basis",
    "executable_mark", "mid_mark", "ltp_mark",
    "current_pnl", "current_pnl_r", "running_mae", "running_mfe",
    "policy_action", "policy_reason", "policy_trace_json", "legs_json"
)

internal fun positionTickImmutableFingerprint(row: JSONObject): String {
    val sb = StringBuilder()
    for (k in POSITION_TICK_IMMUTABLE_FINGERPRINT_KEYS) {
        sb.append(k).append('=')
        if (row.has(k) && !row.isNull(k)) {
            val v = row.opt(k)
            sb.append(
                when (v) {
                    is JSONObject -> v.toString()
                    is JSONArray -> v.toString()
                    else -> v.toString()
                }
            )
        } else {
            sb.append("<missing>")
        }
        sb.append('|')
    }
    return sb.toString()
}

/** Pref keys for overflow / history-capture completeness (shared with mark UI). */
internal const val PREF_POSITION_TICK_OVERFLOW_ACTIVE = "position_tick_overflow_active"
internal const val PREF_POSITION_TICK_OVERFLOW_REJECTED_COUNT = "position_tick_overflow_rejected_count"
internal const val PREF_POSITION_TICK_TRACKING_COMPLETE = "position_tick_tracking_complete"

/** Intent extras on ACTION_POSITION_MARK_TICK (broadcast payload). */
internal const val EXTRA_POSITION_TICK_TRACKING_COMPLETE = "tracking_complete"
internal const val EXTRA_POSITION_TICK_OVERFLOW_ACTIVE = "overflow_active"
internal const val EXTRA_POSITION_TICK_OVERFLOW_REJECTED_COUNT = "overflow_rejected_count"

internal data class PositionTickTrackingStatus(
    val trackingComplete: Boolean,
    val overflowActive: Boolean,
    val overflowRejectedCount: Long
)

/**
 * Read overflow / tracking_complete prefs. tracking_complete is false after any
 * overflow reject until restored; overflow_active also forces incomplete.
 */
internal fun readPositionTickTrackingStatus(
    prefs: android.content.SharedPreferences
): PositionTickTrackingStatus {
    val rejected = prefs.getLong(PREF_POSITION_TICK_OVERFLOW_REJECTED_COUNT, 0L)
    val overflowActive =
        prefs.getBoolean(PREF_POSITION_TICK_OVERFLOW_ACTIVE, false) || rejected > 0L
    val trackingComplete =
        prefs.getBoolean(PREF_POSITION_TICK_TRACKING_COMPLETE, true) && !overflowActive
    return PositionTickTrackingStatus(
        trackingComplete = trackingComplete,
        overflowActive = overflowActive,
        overflowRejectedCount = rejected
    )
}

/** Compact JSON for broadcast Intent / MainActivity → PWA wake payload. */
internal fun positionTickTrackingBroadcastPayload(
    status: PositionTickTrackingStatus
): JSONObject = JSONObject().apply {
    put("position_mark_tick", true)
    put("tracking_complete", status.trackingComplete)
    put("overflow_active", status.overflowActive)
    put("overflow_rejected_count", status.overflowRejectedCount)
}

/** Queue drains only after confirmed persistence (success or verified idempotent conflict). */
internal fun shouldDrainPositionTickQueue(result: PositionTickInsertResult): Boolean = result.persisted

/**
 * Simulate flush decision for tests: retain queue on rejection; drain only when
 * [shouldDrainPositionTickQueue] is true. Does not claim persistence on failure.
 */
internal fun applyPositionTickFlushDecision(
    pendingBefore: Int,
    result: PositionTickInsertResult
): Pair<Int, Boolean> {
    return if (shouldDrainPositionTickQueue(result)) {
        0 to true
    } else {
        pendingBefore to false
    }
}
