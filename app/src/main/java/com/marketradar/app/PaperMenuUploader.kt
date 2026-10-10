package com.marketradar.app

import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject

/**
 * Evidence batch 1 (R4 + R2-review Blocker A): Paper decision-menu upload through the authorized RPCs, then an
 * exact, bounded, keyset-paginated readback of every stored row before anything is acknowledged.
 *
 * Readback contract (docs/contracts/paper_menu_readback_contract.md):
 *  - Pages are ordered by the unique (menu_id, generated_rank) key and requested with
 *    `generated_rank=gt.<last rank seen>`; there is no OFFSET. The server may cap a page BELOW the requested
 *    limit (PostgREST db-max-rows), so a short page never ends the readback by itself.
 *  - End of data is proven only by (a) an empty page after the last rank seen, or (b) the page's exact
 *    remaining count (Content-Range total under `Prefer: count=exact`) equal to the rows on that page.
 *  - Every page must progress (strictly increasing ranks above the cursor); remaining counts must be
 *    consistent from page to page; the proven total must equal finalize's durable_candidate_rows.
 *    Any failure of these is READBACK_INCOMPLETE (retryable), never ACKNOWLEDGED and never MISMATCH.
 *  - MISMATCH is returned only for an established difference: a proven-complete, count-consistent readback
 *    whose rows differ from the frozen local rows (or a durable digest that differs).
 *  - The page budget is bounded: local rows + 2 requests (each non-empty page advances the cursor by >= 1).
 */
object PaperMenuUploader {
    /** Requested rows per readback page. A server cap below this is handled by the keyset loop. */
    const val READBACK_PAGE_ROWS = 500
    const val READBACK_SELECT = "candidate_id,generated_rank,content_sha256"

    const val ACKNOWLEDGED = "ACKNOWLEDGED"
    const val RETRY = "RETRY"
    const val READBACK_INCOMPLETE = "READBACK_INCOMPLETE"
    const val CONFLICT = "CONFLICT"
    const val MISMATCH = "MISMATCH"
    const val REJECTED_PERMANENT = "REJECTED_PERMANENT"

    data class HttpResult(val code: Int?, val body: String?, val contentRange: String? = null, val exception: String? = null) {
        val ok get() = code != null && code in 200..299
        val pgCode: String? get() = body?.let { Pc2TelemetryOutbox.errorCodeOf(it) }
    }

    /** The HTTP boundary. Production: [OkHttpTransport]; tests: the same OkHttp transport against a fake server. */
    interface Transport {
        fun rpc(fn: String, body: JSONObject): HttpResult
        fun get(pathAndQuery: String, headers: Map<String, String> = emptyMap()): HttpResult
    }

    class OkHttpTransport(
        private val client: OkHttpClient,
        private val restBase: String,
        private val baseHeaders: () -> Map<String, String>
    ) : Transport {
        private fun builder(path: String) = Request.Builder().url("$restBase/$path").also { b ->
            baseHeaders().forEach { (k, v) -> b.addHeader(k, v) }
        }

        override fun rpc(fn: String, body: JSONObject): HttpResult = try {
            val req = builder("rpc/$fn").post(body.toString().toRequestBody("application/json".toMediaTypeOrNull())).build()
            client.newCall(req).execute().use { r -> HttpResult(r.code, r.body?.string(), r.header("Content-Range")) }
        } catch (e: Exception) {
            HttpResult(null, null, null, "${e.javaClass.simpleName}:${e.message}")
        }

        override fun get(pathAndQuery: String, headers: Map<String, String>): HttpResult = try {
            val b = builder(pathAndQuery).get()
            headers.forEach { (k, v) -> b.addHeader(k, v) }
            client.newCall(b.build()).execute().use { r -> HttpResult(r.code, r.body?.string(), r.header("Content-Range")) }
        } catch (e: Exception) {
            HttpResult(null, null, null, "${e.javaClass.simpleName}:${e.message}")
        }
    }

    data class Result(val disposition: String, val outcome: String, val completeness: String? = null, val detail: String? = null)

    sealed class Readback {
        data class Verified(val rows: Int, val pages: Int) : Readback()
        /** Proven complete and count-consistent, but different from the frozen local rows. */
        data class Mismatch(val reason: String, val pages: Int) : Readback()
        /** Not proven complete (transport, cap/progress/count anomaly). Retryable; never acknowledged. */
        data class Incomplete(val reason: String, val pages: Int) : Readback()
    }

    private fun failure(stage: String, r: HttpResult): Result {
        val code = r.pgCode
        // Payload-shape faults cannot be fixed by retrying the same frozen bytes; the item is retained.
        val permanent = code in setOf("23514", "22P02", "PT413", "23502", "22003", "22007", "22008", "23505")
        return Result(if (permanent) REJECTED_PERMANENT else RETRY, "${stage}_FAILED:${r.code ?: r.exception}:${code ?: "-"}",
            detail = r.body?.take(300))
    }

    fun upload(t: Transport, deviceKey: String, manifestRow: JSONObject, canonicalRowsInRankOrder: List<String>,
               payloadSha256: String): Result {
        if (!Pc2IngestCredential.isValidKey(deviceKey)) return Result(RETRY, "NO_DEVICE_CREDENTIAL")
        val menuId = manifestRow.getString("menu_id")
        val m = t.rpc("paper_decision_menu_ingest_manifest", JSONObject().put("p_device_key", deviceKey).put("p_manifest", manifestRow))
        if (!m.ok) return failure("MANIFEST", m)
        val ms = runCatching { JSONObject(m.body ?: "") }.getOrNull()
        when (ms?.optString("status")) {
            "ACCEPTED", "REPLAY" -> Unit
            "REJECTED_CONFLICT" -> return Result(CONFLICT, "MANIFEST_CONFLICT:${ms.optString("conflict")}",
                detail = "accepted_payload_sha256=${ms.optString("accepted_payload_sha256")}")
            else -> return Result(RETRY, "MANIFEST_UNEXPECTED_RESPONSE", detail = m.body?.take(300))
        }
        for ((i, chunk) in canonicalRowsInRankOrder.chunked(PaperMenuCapture.CANDIDATE_CHUNK).withIndex()) {
            val r = t.rpc("paper_decision_menu_ingest_candidates", JSONObject()
                .put("p_device_key", deviceKey).put("p_menu_id", menuId).put("p_payload_sha256", payloadSha256)
                .put("p_rows", JSONArray(chunk)))
            if (!r.ok) return failure("ROWS_CHUNK_${i + 1}", r)
            val rs = runCatching { JSONObject(r.body ?: "") }.getOrNull()
            when (rs?.optString("status")) {
                "OK", "REJECTED_TERMINAL" -> Unit
                "REJECTED_CONFLICT" -> return Result(CONFLICT, "ROWS_CONFLICT:${rs.optString("conflict")}",
                    detail = "candidate_id=${rs.optString("candidate_id")}")
                else -> return Result(RETRY, "ROWS_UNEXPECTED_RESPONSE", detail = r.body?.take(300))
            }
        }
        val f = t.rpc("paper_decision_menu_finalize", JSONObject()
            .put("p_device_key", deviceKey).put("p_menu_id", menuId).put("p_payload_sha256", payloadSha256))
        if (!f.ok) return failure("FINALIZE", f)
        val fs = runCatching { JSONObject(f.body ?: "") }.getOrNull() ?: return Result(RETRY, "FINALIZE_UNPARSEABLE")
        if (fs.optString("status") == "REJECTED_CONFLICT") return Result(CONFLICT, "FINALIZE_CONFLICT:${fs.optString("conflict")}")
        val completeness = fs.optString("completeness_state").ifBlank { null }
        return when (completeness) {
            "COMPLETE", "NO_ROWS_EXPECTED" -> {
                if (fs.optString("durable_content_sha256") != PaperMenuCapture.contentDigest(canonicalRowsInRankOrder))
                    return Result(MISMATCH, "DURABLE_DIGEST_DIFFERS", completeness)
                val declared = if (fs.has("durable_candidate_rows") && !fs.isNull("durable_candidate_rows"))
                    fs.optInt("durable_candidate_rows", -1) else null
                when (val rb = readback(t, menuId, canonicalRowsInRankOrder, declared)) {
                    is Readback.Verified -> Result(ACKNOWLEDGED, "OK", completeness, "readback_rows=${rb.rows} pages=${rb.pages}")
                    is Readback.Mismatch -> Result(MISMATCH, "READBACK_DIFFERS:${rb.reason}", completeness, "pages=${rb.pages}")
                    is Readback.Incomplete -> Result(READBACK_INCOMPLETE, "READBACK_INCOMPLETE:${rb.reason}", completeness, "pages=${rb.pages}")
                }
            }
            "MISMATCH" -> Result(MISMATCH, "SERVER_MISMATCH", completeness)
            else -> Result(RETRY, "NOT_YET_COMPLETE", completeness)
        }
    }

    /** Exact remaining-row count from a PostgREST Content-Range header ("0-199/740", "*\/0"); null if absent or "*". */
    fun remainingFromContentRange(h: String?): Int? = h?.substringAfter('/', "")?.trim()?.toIntOrNull()

    fun readback(t: Transport, menuId: String, local: List<String>, declaredRows: Int?): Readback {
        if (declaredRows == null || declaredRows < 0) return Readback.Incomplete("finalize_declared_rows_missing", 0)
        val collected = JSONArray()
        var cursor = 0                      // generated_rank > 0 is a table CHECK, so 0 precedes every row
        var expectedRemaining: Int? = null  // from the previous page's exact count
        val budget = local.size + 2
        var pages = 0
        while (true) {
            if (pages >= budget) return Readback.Incomplete("page_budget_exhausted:$budget", pages)
            pages++
            val r = t.get("ml_paper_decision_menu_candidates?menu_id=eq.$menuId&generated_rank=gt.$cursor" +
                "&select=$READBACK_SELECT&order=generated_rank.asc&limit=$READBACK_PAGE_ROWS", mapOf("Prefer" to "count=exact"))
            if (!r.ok) return Readback.Incomplete("page_${pages}_http:${r.code ?: r.exception}", pages)
            val arr = runCatching { JSONArray(r.body ?: "") }.getOrNull()
                ?: return Readback.Incomplete("page_${pages}_unparseable", pages)
            val remaining = remainingFromContentRange(r.contentRange)
            if (remaining != null && expectedRemaining != null && remaining != expectedRemaining)
                return Readback.Incomplete("page_${pages}_count_not_consistent:$remaining!=$expectedRemaining", pages)
            if (arr.length() == 0) {
                if (remaining != null && remaining != 0) return Readback.Incomplete("page_${pages}_empty_but_count:$remaining", pages)
                break                       // proven: nothing after the last rank seen
            }
            if (remaining != null && remaining < arr.length())
                return Readback.Incomplete("page_${pages}_count_below_rows:$remaining<${arr.length()}", pages)
            for (i in 0 until arr.length()) {
                val row = arr.optJSONObject(i) ?: return Readback.Incomplete("page_${pages}_row_${i}_not_object", pages)
                val rank = if (row.has("generated_rank") && !row.isNull("generated_rank")) row.optInt("generated_rank", -1) else -1
                if (rank <= cursor) return Readback.Incomplete("page_${pages}_non_progressing_or_duplicate:rank=$rank<=cursor=$cursor", pages)
                if (row.optString("candidate_id").isEmpty() || row.optString("content_sha256").isEmpty())
                    return Readback.Incomplete("page_${pages}_row_${i}_fields_missing", pages)
                cursor = rank
                collected.put(row)
                if (collected.length() > maxOf(local.size, declaredRows))
                    return Readback.Incomplete("more_rows_than_declared:>${maxOf(local.size, declaredRows)}", pages)
            }
            if (remaining != null && remaining == arr.length()) break   // proven: this page held all remaining rows
            expectedRemaining = remaining?.minus(arr.length())
        }
        // Complete readback proven. It must agree with the server's own declared durable count first.
        if (collected.length() != declaredRows)
            return Readback.Incomplete("readback_rows_${collected.length()}_disagree_with_declared_$declaredRows", pages)
        val diff = PaperMenuCapture.readbackDifference(local, collected)
        return if (diff == null) Readback.Verified(collected.length(), pages) else Readback.Mismatch(diff, pages)
    }
}
