package com.marketradar.app

import org.json.JSONObject

/**
 * b513: privacy-safe, nonblank diagnostics for the evening identity read-back chain.
 *
 * The 21 Oct 2026 b511 log showed `status=incomplete_error error=` (blank) twice: OkHttp over HTTP/2 has an empty
 * reason phrase, and the structured PostgREST code was never copied into the error text. Everything here keeps only
 * classification fields (stage, table, page, offset, counts, elapsed, HTTP status, PostgREST/Postgres code, page status,
 * exception class). Never tokens, headers, URLs/query strings, response bodies, row values or account details.
 */
object EvaluationReadbackDiagnostics {
    const val STAGE_EVALUATION = "evaluation"
    const val STAGE_PRIMARY_COMPANION = "primary_companion"
    const val STAGE_REJECTED = "rejected"

    private val SAFE = Regex("[^A-Za-z0-9 _.:/()=-]")

    /** Short, sanitized fragment: never blank, never a URL query, at most [max] chars. */
    fun safeFragment(raw: String?, max: Int = 60): String =
        raw.orEmpty().substringBefore('?').replace(SAFE, "").trim().take(max)

    /** Pure: what fetchSyncTyped records for a non-2xx response. Never blank even with an empty HTTP/2 reason. */
    fun describeHttpFailure(httpCode: Int, reason: String?, body: String?): String {
        val code = postgrestCode(body)
        val r = safeFragment(reason, 40)
        return buildString {
            append("HTTP_").append(httpCode)
            if (code != null) append(" code=").append(code)
            if (r.isNotEmpty()) append(" reason=").append(r)
        }
    }

    /** Pure: exception class always retained; null/blank message becomes no_message. */
    fun describeException(e: Throwable): String {
        val msg = safeFragment(e.message, 60)
        return "${e.javaClass.simpleName}:${msg.ifEmpty { "no_message" }}"
    }

    /** Top-level PostgREST `code` only (same contract as SupabaseClient.extractPostgrestCode). */
    fun postgrestCode(body: String?): String? = try {
        if (body.isNullOrBlank()) null else JSONObject(body).optString("code", "").trim()
            .takeIf { it.matches(Regex("^[A-Za-z0-9]{1,12}$")) }
    } catch (_: Exception) { null }

    fun classify(pageStatus: String?, httpStatus: Int?, postgrestCode: String?, exceptionClass: String?): String {
        val code = postgrestCode?.uppercase()
        return when {
            pageStatus == "truncated" -> "budget_truncated"
            pageStatus == "cancelled" -> "transport_cancelled"
            pageStatus == "parse_error" -> "parse_error"
            code == "57014" -> "query_statement_timeout_57014"
            code == "42501" -> "auth_permission_42501"
            code == "PGRST301" || code == "PGRST302" -> "auth_jwt_$code"
            code == "PGRST205" || code == "42P01" -> "schema_missing_$code"
            code != null && code.startsWith("PGRST") -> "api_$code"
            code != null -> "query_$code"
            httpStatus == 401 || httpStatus == 403 -> "auth_http_$httpStatus"
            httpStatus == 429 -> "http_429_rate_limited"
            httpStatus != null && httpStatus >= 500 -> "http_${httpStatus}_server"
            httpStatus != null -> "http_$httpStatus"
            !exceptionClass.isNullOrBlank() -> "transport_$exceptionClass"
            else -> "unclassified_${pageStatus ?: "unknown"}"
        }
    }

    /** Bounded retry is allowed only for failures that a re-read of the SAME frozen boundary can plausibly clear. */
    fun isTransient(classification: String): Boolean =
        classification == "query_statement_timeout_57014" ||
            classification == "http_429_rate_limited" ||
            classification.startsWith("transport_") ||
            Regex("^http_5\\d\\d_server$").matches(classification)

    data class Failure(
        val stage: String,
        val table: String,
        val sessionDate: String,
        val pageStatus: String,
        val failedPage: Int?,
        val failedOffset: Int?,
        val rowsRead: Int,
        val pagesFetched: Int,
        val elapsedMs: Long,
        val httpStatus: Int?,
        val postgrestCode: String?,
        val exceptionClass: String?,
        val classification: String
    ) {
        val retryable: Boolean get() = isTransient(classification)

        /** One LogBuffer/job-status line. Nonblank by construction. */
        fun line(): String = "stage=$stage table=$table session=$sessionDate class=$classification " +
            "pageStatus=$pageStatus page=${failedPage ?: "-"} offset=${failedOffset ?: "-"} rowsRead=$rowsRead " +
            "pages=$pagesFetched elapsedMs=$elapsedMs http=${httpStatus ?: "-"} code=${postgrestCode ?: "-"} " +
            "exception=${exceptionClass ?: "-"}"

        fun toJson(): JSONObject = JSONObject()
            .put("stage", stage).put("table", table).put("session_date", sessionDate)
            .put("classification", classification).put("page_status", pageStatus)
            .put("failed_page", failedPage ?: JSONObject.NULL).put("failed_offset", failedOffset ?: JSONObject.NULL)
            .put("rows_read", rowsRead).put("pages_fetched", pagesFetched).put("elapsed_ms", elapsedMs)
            .put("http_status", httpStatus ?: JSONObject.NULL).put("postgrest_code", postgrestCode ?: JSONObject.NULL)
            .put("exception_class", exceptionClass ?: JSONObject.NULL).put("retryable", retryable)
    }

    /** Builds a Failure from a selectAllPages result object (incomplete_error / incomplete_truncated / other). */
    fun fromPaged(stage: String, table: String, sessionDate: String, paged: JSONObject): Failure {
        val status = paged.optString("status", "")
        val pageStatus = when (status) {
            "incomplete_truncated" -> "truncated"
            "incomplete_error" -> paged.optString("page_error_status", "").ifBlank { "http_error" }
            else -> status.ifBlank { "unknown" }
        }
        val http = if (paged.isNull("http_status") || !paged.has("http_status")) null else paged.optInt("http_status")
        val code = if (paged.isNull("postgrest_code") || !paged.has("postgrest_code")) null else paged.optString("postgrest_code").ifBlank { null }
        val exc = if (paged.isNull("exception_class") || !paged.has("exception_class")) null else paged.optString("exception_class").ifBlank { null }
        val failedPage = if (paged.isNull("failed_page") || !paged.has("failed_page")) null else paged.optInt("failed_page")
        val pageSize = paged.optInt("page_size", 500)
        return Failure(
            stage = stage, table = table, sessionDate = sessionDate, pageStatus = pageStatus,
            failedPage = failedPage ?: if (pageStatus == "truncated") paged.optInt("max_pages") else null,
            failedOffset = failedPage?.let { it * pageSize },
            rowsRead = paged.optInt("returned", 0), pagesFetched = paged.optInt("pages_fetched", 0),
            elapsedMs = paged.optLong("elapsed_ms", 0L), httpStatus = http, postgrestCode = code, exceptionClass = exc,
            classification = classify(pageStatus, http, code, exc)
        )
    }
}
