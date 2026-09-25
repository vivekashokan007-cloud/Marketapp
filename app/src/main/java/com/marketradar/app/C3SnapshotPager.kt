package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject

/**
 * C3 R2: fail-closed paging of remote ml_brain_snapshots for percentile
 * finalization. Pure JVM (no Android / network types) so paging semantics are
 * unit-testable with a fake page fetcher.
 *
 * Contract (see [C3PagingResult]):
 *  - COMPLETE only when the end of the result set is confirmed: a short page
 *    (rows < pageSize) or an explicit empty follow-up page / limit probe.
 *  - EMPTY only when the very first page is fetched, parsed and has zero rows.
 *  - FAILED on any page fetch error, any unparseable / malformed page, or when
 *    the page limit is exhausted without a confirmed end. Callers MUST discard
 *    every compact delivered before a FAILED result; FAILED frames never reach
 *    finalization.
 *
 * Memory design (R1) is unchanged: one page in memory at a time, only compact
 * c3_finalization_frame snapshots are delivered, full context_json is dropped
 * right after extraction. Each page is fully validated before any of its rows
 * is delivered, so a malformed page contributes nothing.
 */
sealed class C3PageFetch {
    data class Ok(val body: String) : C3PageFetch()
    data class Failed(val httpCode: Int? = null, val detail: String = "") : C3PageFetch()
}

sealed class C3PagingResult {
    abstract val mode: String
    abstract val pagesRead: Int
    abstract val pageBytesPeak: Int

    /** End confirmed; [delivered] compact snapshots were handed to the sink. */
    data class Complete(
        override val mode: String,
        val delivered: Int,
        val rowsSeen: Int,
        override val pagesRead: Int,
        override val pageBytesPeak: Int,
        val confirmedBy: String
    ) : C3PagingResult()

    /** First page fetched and parsed OK with zero rows. */
    data class Empty(
        override val mode: String,
        override val pagesRead: Int,
        override val pageBytesPeak: Int
    ) : C3PagingResult()

    /**
     * Remote read incomplete. [reason] is one of [C3PagingFailure]. [pageIndex]
     * is 0-based. [discardedDelivered] counts compacts the caller must drop.
     */
    data class Failed(
        override val mode: String,
        val reason: String,
        val pageIndex: Int,
        val discardedDelivered: Int,
        override val pagesRead: Int,
        override val pageBytesPeak: Int,
        val httpCode: Int? = null,
        val detail: String = ""
    ) : C3PagingResult()
}

object C3PagingFailure {
    const val PAGE_FETCH_FAILED = "page_fetch_failed"
    const val PAGE_PARSE_FAILED = "page_parse_failed"
    const val PAGE_LIMIT_UNCONFIRMED = "page_limit_unconfirmed"
}

object C3SnapshotPager {
    const val MODE_NARROW = "narrow_json_path"
    const val MODE_CONTEXT = "context_page_extract"
    const val NARROW_SELECT = "id,session_date,poll_ts,c3_finalization_frame:context_json->c3_finalization_frame"
    const val CONTEXT_SELECT = "id,session_date,poll_ts,context_json"
    const val NARROW_PAGE_SIZE = 25
    const val NARROW_MAX_PAGES = 40
    const val CONTEXT_PAGE_SIZE = 1
    const val CONTEXT_MAX_PAGES = 200
    const val FRAME_KEY = "c3_finalization_frame"

    private val SESSION_DATE = Regex("\\d{4}-\\d{2}-\\d{2}")

    fun pagePath(sessionDate: String, select: String, limit: Int, offset: Int): String =
        "ml_brain_snapshots?session_date=eq.$sessionDate" +
            "&select=$select&order=poll_ts.asc&limit=$limit&offset=$offset"

    /**
     * Narrow JSON-path paging first; the full-context page-extract mode is used
     * only when the narrow mode produced nothing (EMPTY, or FAILED on page 0
     * before any delivery, e.g. JSON-path select unsupported). A narrow failure
     * after page 0 is final (modes are never mixed).
     */
    fun fetchAll(
        sessionDate: String,
        fetch: (String) -> C3PageFetch,
        onCompact: (JSONObject) -> Unit,
        narrowPageSize: Int = NARROW_PAGE_SIZE,
        narrowMaxPages: Int = NARROW_MAX_PAGES,
        contextPageSize: Int = CONTEXT_PAGE_SIZE,
        contextMaxPages: Int = CONTEXT_MAX_PAGES
    ): C3PagingResult {
        require(SESSION_DATE.matches(sessionDate)) { "C3 finalization sessionDate must be yyyy-MM-dd" }
        val narrow = pageMode(sessionDate, MODE_NARROW, narrowPageSize, narrowMaxPages, fetch, onCompact)
        val tryContext = when (narrow) {
            is C3PagingResult.Complete -> false
            is C3PagingResult.Empty -> true
            is C3PagingResult.Failed -> narrow.pageIndex == 0 && narrow.discardedDelivered == 0
        }
        if (!tryContext) return narrow
        return pageMode(sessionDate, MODE_CONTEXT, contextPageSize, contextMaxPages, fetch, onCompact)
    }

    fun pageMode(
        sessionDate: String,
        mode: String,
        pageSize: Int,
        maxPages: Int,
        fetch: (String) -> C3PageFetch,
        onCompact: (JSONObject) -> Unit
    ): C3PagingResult {
        require(pageSize > 0 && maxPages > 0)
        val select = if (mode == MODE_NARROW) NARROW_SELECT else CONTEXT_SELECT
        var delivered = 0
        var rowsSeen = 0
        var pagesRead = 0
        var pageBytesPeak = 0
        var offset = 0
        var pageIndex = 0
        while (true) {
            val isLimitProbe = pageIndex >= maxPages
            // After maxPages full pages, one limit=1 probe confirms the end.
            val limit = if (isLimitProbe) 1 else pageSize
            fun failed(reason: String, httpCode: Int? = null, detail: String = "") = C3PagingResult.Failed(
                mode = mode, reason = reason, pageIndex = pageIndex, discardedDelivered = delivered,
                pagesRead = pagesRead, pageBytesPeak = pageBytesPeak, httpCode = httpCode, detail = detail
            )
            val body = when (val fetched = fetch(pagePath(sessionDate, select, limit, offset))) {
                is C3PageFetch.Failed -> return failed(C3PagingFailure.PAGE_FETCH_FAILED, fetched.httpCode, fetched.detail)
                is C3PageFetch.Ok -> fetched.body
            }
            pageBytesPeak = maxOf(pageBytesPeak, body.length)
            val page = try {
                JSONArray(body)
            } catch (oom: OutOfMemoryError) {
                return failed(C3PagingFailure.PAGE_PARSE_FAILED, detail = "oom bytes=${body.length}")
            } catch (_: Exception) {
                return failed(C3PagingFailure.PAGE_PARSE_FAILED, detail = "not_json_array bytes=${body.length}")
            }
            pagesRead += 1
            if (page.length() == 0) {
                if (pageIndex == 0) return C3PagingResult.Empty(mode, pagesRead, pageBytesPeak)
                return C3PagingResult.Complete(
                    mode, delivered, rowsSeen, pagesRead, pageBytesPeak,
                    confirmedBy = if (isLimitProbe) "empty_limit_probe" else "empty_followup_page"
                )
            }
            if (isLimitProbe) {
                return failed(C3PagingFailure.PAGE_LIMIT_UNCONFIRMED, detail = "rows_beyond_limit maxPages=$maxPages pageSize=$pageSize")
            }
            if (page.length() > pageSize) {
                return failed(C3PagingFailure.PAGE_PARSE_FAILED, detail = "page_oversize rows=${page.length()} pageSize=$pageSize")
            }
            // Validate the whole page before delivering any row from it.
            val compacts = ArrayList<JSONObject>(page.length())
            for (i in 0 until page.length()) {
                val row = page.opt(i) as? JSONObject
                    ?: return failed(C3PagingFailure.PAGE_PARSE_FAILED, detail = "row_not_object row=$i")
                val extracted = extractFrame(row, mode)
                if (extracted is FrameExtract.Malformed) {
                    return failed(C3PagingFailure.PAGE_PARSE_FAILED, detail = "${extracted.why} row=$i")
                }
                if (extracted !is FrameExtract.Present) continue
                val pollTs = row.optString("poll_ts", "").trim()
                if (pollTs.isBlank() || row.isNull("poll_ts")) {
                    return failed(C3PagingFailure.PAGE_PARSE_FAILED, detail = "frame_without_poll_ts row=$i")
                }
                val session = row.optString("session_date", sessionDate).ifBlank { sessionDate }
                compacts.add(compactSnapshot(row.opt("id"), session, pollTs, extracted.frame))
            }
            // Release the page (and any full context_json) before delivery.
            val pageRows = page.length()
            rowsSeen += pageRows
            for (compact in compacts) {
                onCompact(compact)
                delivered += 1
            }
            compacts.clear()
            if (pageRows < pageSize) {
                return C3PagingResult.Complete(mode, delivered, rowsSeen, pagesRead, pageBytesPeak, confirmedBy = "short_page")
            }
            offset += pageRows
            pageIndex += 1
        }
    }

    private sealed class FrameExtract {
        object Absent : FrameExtract()
        data class Present(val frame: JSONObject) : FrameExtract()
        data class Malformed(val why: String) : FrameExtract()
    }

    private fun parseFrameValue(raw: Any?): FrameExtract = when (raw) {
        null, JSONObject.NULL -> FrameExtract.Absent
        is JSONObject -> FrameExtract.Present(raw)
        is String -> {
            val trimmed = raw.trim()
            val parsed = if (trimmed.startsWith("{")) {
                try { JSONObject(trimmed) } catch (_: Exception) { null }
            } else null
            if (parsed != null) FrameExtract.Present(parsed) else FrameExtract.Malformed("frame_unparseable")
        }
        else -> FrameExtract.Malformed("frame_wrong_type")
    }

    private fun extractFrame(row: JSONObject, mode: String): FrameExtract {
        if (mode == MODE_NARROW) {
            // PostgREST always returns the aliased key (null when the snapshot
            // has no frame). An absent key means the response shape is wrong.
            if (!row.has(FRAME_KEY)) return FrameExtract.Malformed("frame_field_missing")
            return parseFrameValue(row.opt(FRAME_KEY))
        }
        if (!row.has("context_json")) return FrameExtract.Malformed("context_field_missing")
        val context = when (val rawCtx = row.opt("context_json")) {
            null, JSONObject.NULL -> return FrameExtract.Absent
            is JSONObject -> rawCtx
            is String -> try { JSONObject(rawCtx) } catch (_: Exception) {
                return FrameExtract.Malformed("context_unparseable")
            }
            else -> return FrameExtract.Malformed("context_wrong_type")
        }
        val extracted = parseFrameValue(context.opt(FRAME_KEY))
        // Drop the full context reference as soon as the frame is captured.
        row.remove("context_json")
        return extracted
    }

    /** Compact snapshot: id/session_date/poll_ts + context_json{c3_finalization_frame} only. */
    fun compactSnapshot(id: Any?, sessionDate: String, pollTs: String, frame: JSONObject): JSONObject =
        JSONObject().apply {
            if (id != null && id != JSONObject.NULL) put("id", id)
            put("session_date", sessionDate)
            put("poll_ts", pollTs)
            put("context_json", JSONObject().put(FRAME_KEY, frame))
        }

    /** Privacy-safe one-line summary (counters/reasons only). */
    fun describe(result: C3PagingResult): String = when (result) {
        is C3PagingResult.Complete ->
            "status=COMPLETE mode=${result.mode} pages=${result.pagesRead} rows=${result.rowsSeen} " +
                "delivered=${result.delivered} confirmedBy=${result.confirmedBy} pageBytesPeak=${result.pageBytesPeak}"
        is C3PagingResult.Empty ->
            "status=EMPTY mode=${result.mode} pages=${result.pagesRead}"
        is C3PagingResult.Failed ->
            "status=FAILED mode=${result.mode} reason=${result.reason} pageIndex=${result.pageIndex} " +
                "http=${result.httpCode ?: "-"} discarded=${result.discardedDelivered} pages=${result.pagesRead} " +
                "detail=${result.detail.take(80)}"
    }
}
