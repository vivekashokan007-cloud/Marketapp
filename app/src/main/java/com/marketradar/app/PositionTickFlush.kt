package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.util.LinkedHashMap

/** Confirmed empty/success insert. */
internal const val POSITION_TICK_FLUSH_OK = "ok"

/** Server reported conflict; treated as already-present for queue drain. */
internal const val POSITION_TICK_FLUSH_IDEMPOTENT_CONFLICT = "idempotent_conflict"

/** Missing/invalid config or auth (401/403). */
internal const val POSITION_TICK_FLUSH_CONFIG_AUTH = "config_auth"

/** Schema/payload rejection (400/409/422) — 409 separately mapped to idempotent when conflict. */
internal const val POSITION_TICK_FLUSH_SCHEMA_PAYLOAD = "schema_payload"

/** Transient network/timeout style failures. */
internal const val POSITION_TICK_FLUSH_TRANSIENT_NETWORK = "transient_network"

/** Upstream 5xx. */
internal const val POSITION_TICK_FLUSH_SERVER_5XX = "server_5xx"

/** Transport/IO exception (sanitized type only). */
internal const val POSITION_TICK_FLUSH_TRANSPORT = "transport"

/** Unclassified. */
internal const val POSITION_TICK_FLUSH_UNKNOWN = "unknown"

/**
 * Privacy-safe insert/flush diagnostic. Never carries credentials, tokens, or full
 * trade payloads — only class, status, sanitized short detail, and row count.
 */
data class PositionTickInsertResult(
    val success: Boolean,
    val persisted: Boolean,
    val failureClass: String,
    val httpStatus: Int?,
    val exceptionType: String?,
    val detail: String,
    val rowCount: Int
)

/**
 * Classify an insert outcome for observability. Failed flushes must set
 * [PositionTickInsertResult.persisted] = false so callers never drain the queue
 * or claim durable history.
 */
internal fun classifyPositionTickFlushFailure(
    httpStatus: Int?,
    httpMessage: String?,
    exceptionType: String?,
    exceptionMessage: String?,
    responseBodySnippet: String?,
    rowCount: Int
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
            detail = sanitizeFlushDiagText(exceptionMessage ?: simple),
            rowCount = rowCount
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
            detail = sanitizeFlushDiagText(httpMessage ?: "ok"),
            rowCount = rowCount
        )
    }

    // 409 Conflict: treat as already-present so retries do not stall forever if a
    // unique constraint exists or PostgREST reports conflict. Without a unique key
    // this path is rare; drain only when conflict is explicit.
    if (code == 409) {
        return PositionTickInsertResult(
            success = true,
            persisted = true,
            failureClass = POSITION_TICK_FLUSH_IDEMPOTENT_CONFLICT,
            httpStatus = code,
            exceptionType = null,
            detail = sanitizeFlushDiagText(responseBodySnippet ?: httpMessage ?: "conflict"),
            rowCount = rowCount
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

    val detailParts = listOfNotNull(
        httpMessage?.takeIf { it.isNotBlank() },
        responseBodySnippet?.takeIf { it.isNotBlank() }
    ).joinToString(" | ")

    return PositionTickInsertResult(
        success = false,
        persisted = false,
        failureClass = failureClass,
        httpStatus = code,
        exceptionType = null,
        detail = sanitizeFlushDiagText(detailParts.ifBlank { "http_$code" }),
        rowCount = rowCount
    )
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
 * Strip credentials/tokens and truncate. Never log raw JWTs or apikeys.
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
 * Config/schema failures back off faster to the cap so we do not hammer a bad payload.
 */
internal fun computePositionTickFlushBackoffMs(consecutiveFailures: Int, failureClass: String): Long {
    val base = 60_000L
    val cappedFailures = consecutiveFailures.coerceIn(0, 10)
    val mult = when (failureClass) {
        POSITION_TICK_FLUSH_CONFIG_AUTH, POSITION_TICK_FLUSH_SCHEMA_PAYLOAD ->
            (cappedFailures).coerceAtLeast(1).coerceAtMost(5)
        POSITION_TICK_FLUSH_SERVER_5XX, POSITION_TICK_FLUSH_TRANSIENT_NETWORK, POSITION_TICK_FLUSH_TRANSPORT ->
            (cappedFailures).coerceAtLeast(1).coerceAtMost(8)
        else -> (cappedFailures).coerceAtLeast(1).coerceAtMost(6)
    }
    return (base * mult).coerceAtMost(5 * 60_000L)
}

/**
 * De-dupe pending queue by trade_id|tick_ts keeping the latest row. Retries of the
 * same tick must not inflate history when a prior attempt may have partially landed
 * client-side. Does not invent rows or bypass validation.
 */
internal fun dedupePositionTicksByTradeTs(queue: JSONArray): Pair<JSONArray, Int> {
    if (queue.length() <= 1) return queue to 0
    val lastIndexByKey = LinkedHashMap<String, Int>()
    for (i in 0 until queue.length()) {
        val row = queue.optJSONObject(i) ?: continue
        val tradeId = row.optString("trade_id", "").ifBlank { "__missing__" }
        val tickTs = row.optString("tick_ts", "").ifBlank { "__missing__" }
        lastIndexByKey["$tradeId|$tickTs"] = i
    }
    if (lastIndexByKey.size == queue.length()) return queue to 0
    val out = JSONArray()
    val keep = lastIndexByKey.values.toSortedSet()
    for (i in keep) {
        queue.optJSONObject(i)?.let { out.put(it) }
    }
    return out to (queue.length() - out.length())
}

/** Queue drains only after confirmed persistence (success or idempotent conflict). */
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
