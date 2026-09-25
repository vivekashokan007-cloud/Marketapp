package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder
import java.time.Instant
import java.time.OffsetDateTime

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
 * R3 read stability: pages are keyset-ordered by the unique composite key
 * (poll_ts, id) — `id` is the bigint identity PRIMARY KEY of
 * ml_brain_snapshots — and bounded above by `id <= maxId` captured once at the
 * start of the read, so rows inserted during the read (DB-generated identity
 * values are larger) are excluded. No offsets are used. Duplicate or
 * non-increasing keys, or ids above the boundary, are FAILED.
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
        val confirmedBy: String,
        val boundaryMaxId: Long? = null
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
    /** R3: duplicate or non-increasing (poll_ts, id) key within/across pages. */
    const val PAGE_KEY_ORDER_VIOLATION = "page_key_order_violation"
    /** R3: a row above the read boundary captured at the start of the read. */
    const val PAGE_BOUNDARY_VIOLATION = "page_boundary_violation"
    /** R3: the start-of-read boundary query failed / was malformed. */
    const val BOUNDARY_FETCH_FAILED = "boundary_fetch_failed"
    const val BOUNDARY_PARSE_FAILED = "boundary_parse_failed"
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

    const val MODE_BOUNDARY = "boundary"
    const val ORDER = "poll_ts.asc,id.asc"

    /** Keyset cursor: last (poll_ts, id) seen. [pollTsRaw] is echoed back verbatim. */
    data class Cursor(val pollTsRaw: String, val pollTs: Instant, val id: Long)

    fun boundaryPath(sessionDate: String): String =
        "ml_brain_snapshots?session_date=eq.$sessionDate&select=id&order=id.desc&limit=1"

    fun pagePath(sessionDate: String, select: String, limit: Int, maxId: Long, after: Cursor?): String {
        val keyset = if (after == null) "" else {
            val ts = after.pollTsRaw.replace("\"", "")
            val expr = "(poll_ts.gt.\"$ts\",and(poll_ts.eq.\"$ts\",id.gt.${after.id}))"
            "&or=" + URLEncoder.encode(expr, "UTF-8")
        }
        return "ml_brain_snapshots?session_date=eq.$sessionDate&id=lte.$maxId$keyset" +
            "&select=$select&order=$ORDER&limit=$limit"
    }

    private fun integralId(raw: Any?): Long? = when (raw) {
        is Int -> raw.toLong()
        is Long -> raw
        is Number -> raw.toDouble().let { d -> if (d == Math.floor(d) && !d.isInfinite()) d.toLong() else null }
        is String -> raw.trim().toLongOrNull()
        else -> null
    }

    private fun parsePollTs(raw: String): Instant? = try {
        OffsetDateTime.parse(raw).toInstant()
    } catch (_: Exception) {
        try { Instant.parse(raw) } catch (_: Exception) { null }
    }

    sealed class Boundary {
        data class Ready(val maxId: Long) : Boundary()
        object Empty : Boundary()
        data class Failed(val result: C3PagingResult.Failed) : Boundary()
    }

    /** Capture the fixed upper read boundary (max id for the session) once, before paging. */
    fun captureBoundary(sessionDate: String, fetch: (String) -> C3PageFetch): Boundary {
        fun failed(reason: String, httpCode: Int? = null, detail: String = "") = Boundary.Failed(
            C3PagingResult.Failed(MODE_BOUNDARY, reason, 0, 0, 0, 0, httpCode, detail)
        )
        val body = when (val f = fetch(boundaryPath(sessionDate))) {
            is C3PageFetch.Failed -> return failed(C3PagingFailure.BOUNDARY_FETCH_FAILED, f.httpCode, f.detail)
            is C3PageFetch.Ok -> f.body
        }
        val arr = try { JSONArray(body) } catch (_: Throwable) {
            return failed(C3PagingFailure.BOUNDARY_PARSE_FAILED, detail = "not_json_array")
        }
        if (arr.length() == 0) return Boundary.Empty
        val row = arr.opt(0) as? JSONObject ?: return failed(C3PagingFailure.BOUNDARY_PARSE_FAILED, detail = "row_not_object")
        val maxId = integralId(row.opt("id")) ?: return failed(C3PagingFailure.BOUNDARY_PARSE_FAILED, detail = "id_not_integral")
        return Boundary.Ready(maxId)
    }

    /**
     * Captures the read boundary, then narrow JSON-path keyset paging; the
     * full-context page-extract mode is used only when the narrow mode produced
     * nothing (EMPTY, or FAILED on page 0 before any delivery, e.g. JSON-path
     * select unsupported). A narrow failure after page 0 is final (modes are
     * never mixed). Both modes share the same boundary.
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
        val maxId = when (val b = captureBoundary(sessionDate, fetch)) {
            is Boundary.Failed -> return b.result
            is Boundary.Empty -> return C3PagingResult.Empty(MODE_BOUNDARY, 1, 0)
            is Boundary.Ready -> b.maxId
        }
        val narrow = pageMode(sessionDate, MODE_NARROW, narrowPageSize, narrowMaxPages, maxId, fetch, onCompact)
        val tryContext = when (narrow) {
            is C3PagingResult.Complete -> false
            is C3PagingResult.Empty -> true
            is C3PagingResult.Failed -> narrow.pageIndex == 0 && narrow.discardedDelivered == 0
        }
        if (!tryContext) return narrow
        return pageMode(sessionDate, MODE_CONTEXT, contextPageSize, contextMaxPages, maxId, fetch, onCompact)
    }

    fun pageMode(
        sessionDate: String,
        mode: String,
        pageSize: Int,
        maxPages: Int,
        maxId: Long,
        fetch: (String) -> C3PageFetch,
        onCompact: (JSONObject) -> Unit
    ): C3PagingResult {
        require(pageSize > 0 && maxPages > 0)
        val select = if (mode == MODE_NARROW) NARROW_SELECT else CONTEXT_SELECT
        var delivered = 0
        var rowsSeen = 0
        var pagesRead = 0
        var pageBytesPeak = 0
        var cursor: Cursor? = null
        val seenIds = HashSet<Long>()
        var pageIndex = 0
        while (true) {
            val isLimitProbe = pageIndex >= maxPages
            // After maxPages full pages, one limit=1 keyset probe confirms the end.
            val limit = if (isLimitProbe) 1 else pageSize
            fun failed(reason: String, httpCode: Int? = null, detail: String = "") = C3PagingResult.Failed(
                mode = mode, reason = reason, pageIndex = pageIndex, discardedDelivered = delivered,
                pagesRead = pagesRead, pageBytesPeak = pageBytesPeak, httpCode = httpCode, detail = detail
            )
            val body = when (val fetched = fetch(pagePath(sessionDate, select, limit, maxId, cursor))) {
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
                    confirmedBy = if (isLimitProbe) "empty_limit_probe" else "empty_followup_page",
                    boundaryMaxId = maxId
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
                // R3: every row must carry a valid unique key (poll_ts, id).
                val id = integralId(row.opt("id"))
                    ?: return failed(C3PagingFailure.PAGE_PARSE_FAILED, detail = "row_id_missing row=$i")
                val pollTs = if (row.isNull("poll_ts")) "" else row.optString("poll_ts", "").trim()
                val pollInstant = if (pollTs.isBlank()) null else parsePollTs(pollTs)
                if (pollInstant == null) {
                    return failed(C3PagingFailure.PAGE_PARSE_FAILED, detail = "row_poll_ts_invalid row=$i")
                }
                if (id > maxId) {
                    return failed(C3PagingFailure.PAGE_BOUNDARY_VIOLATION, detail = "id_above_boundary row=$i")
                }
                val prev = cursor
                if (!seenIds.add(id)) {
                    return failed(C3PagingFailure.PAGE_KEY_ORDER_VIOLATION, detail = "duplicate_id row=$i")
                }
                if (prev != null) {
                    val cmp = pollInstant.compareTo(prev.pollTs)
                    if (cmp < 0 || (cmp == 0 && id <= prev.id)) {
                        return failed(C3PagingFailure.PAGE_KEY_ORDER_VIOLATION, detail = "non_increasing_key row=$i")
                    }
                }
                cursor = Cursor(pollTs, pollInstant, id)
                val extracted = extractFrame(row, mode)
                if (extracted is FrameExtract.Malformed) {
                    return failed(C3PagingFailure.PAGE_PARSE_FAILED, detail = "${extracted.why} row=$i")
                }
                if (extracted !is FrameExtract.Present) continue
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
                return C3PagingResult.Complete(
                    mode, delivered, rowsSeen, pagesRead, pageBytesPeak,
                    confirmedBy = "short_page", boundaryMaxId = maxId
                )
            }
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
                "delivered=${result.delivered} confirmedBy=${result.confirmedBy} boundaryMaxId=${result.boundaryMaxId ?: "-"} " +
                "pageBytesPeak=${result.pageBytesPeak}"
        is C3PagingResult.Empty ->
            "status=EMPTY mode=${result.mode} pages=${result.pagesRead}"
        is C3PagingResult.Failed ->
            "status=FAILED mode=${result.mode} reason=${result.reason} pageIndex=${result.pageIndex} " +
                "http=${result.httpCode ?: "-"} discarded=${result.discardedDelivered} pages=${result.pagesRead} " +
                "detail=${result.detail.take(80)}"
    }
}
