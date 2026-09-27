package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * B1.1: measured request bytes for one 50-row chunk.
 *
 * Worst-case row = the largest production row shape (4 legs; top-level and
 * policy_trace_json key set read-only from production on 27 Sep 2026, largest row
 * 4,342 B as jsonb text) with every number widened to 20 characters and every
 * string padded 50% beyond its observed maximum, PLUS every field the current
 * producer adds that no production row has yet (uploads stopped on 24 Sep):
 * B3 quote_validity (12 reasons, 4 legs), mark_trust + book_width, mark_store_trust,
 * B3.1 mid-fallback fields, item-3 quote_refresh trace (4 keys, 12 reasons),
 * a persistent mark-failure episode, per-leg source annotations, and the 64-hex
 * client_event_id. The real structures are produced by the real toJson() code.
 */
class PositionTickChunkBytesTest {

    private val num = -12345.678901234567  // 18-19 chars as JSON
    private fun pad(n: Int) = "X".repeat(n)
    private fun reason(i: Int) = "leg:NSE_FO|4815162$i:source_age_exceeded_limit_ms"

    private fun worstRow(withB3: Boolean = true): JSONObject {
        val keys = (0 until 4).map { "NSE_FO|4815162$it" }
        val reasons12 = (0 until 12).map { reason(it) }
        val validity = PositionQuoteValidity(
            state = QV_INVALID, reasons = reasons12,
            legs = keys.map { LegQuoteValidity(it, false, reasons12.take(3), "2026-09-24T06:31:55.581Z", 1_758_695_515_581L, 123_456L, "2026-09-24T06:31:50.000Z", false) },
            receiptTs = "2026-09-24T06:31:55.581Z", receiptMs = 1_758_695_515_581L,
            earliestSourceMs = 1_758_695_515_581L, latestSourceMs = 1_758_695_515_999L,
            earliestSourceTs = "2026-09-24T06:31:55.581Z", maxSourceAgeMs = 123_456L,
            interLegSkewMs = 99_999L, bookFingerprint = "f".repeat(64), fetchStatus = "HTTP_503"
        )
        val trust = MarkTrust(
            state = TRUST_UNTRUSTED, cause = "WIDE_LIQUIDATION_BOOK", boundAnomalyStored = true,
            boundAnomalyStructural = true, boundReferenceStatus = "MISMATCH",
            expected = StructuralBounds(num, num, num), midPnl = num,
            detail = (0 until 6).map { "detail_${it}_${pad(40)}" },
            bookWidth = BookWidthCheck(BOOK_WIDTH_WIDE, num, num, "STORED_MAX_LOSS", num)
        )
        val trace = JSONObject()
        // Production trace key set (27 Sep read-only shape query) at widened sizes.
        val prodStr = mapOf(
            "batch_b_parity_action" to 6, "batch_b_parity_contract_version" to 49, "batch_b_parity_event_ts" to 26,
            "batch_b_parity_join_authority" to 72, "batch_b_parity_quote_ts_unavailable_reason" to 41,
            "batch_b_parity_reason" to 29, "batch_b_parity_session_id" to 12, "batch_b_parity_source" to 29,
            "batch_b_parity_trade_id" to 5, "batch_b_shadow_action" to 6, "batch_b_shadow_reason" to 29,
            "eod_hh_mm" to 7, "extrema_contract" to 49, "lot_as_of" to 12, "lot_size_source" to 24,
            "lot_table_version" to 32, "net_target_version" to 47, "policy_version" to 20,
            "position_exit_policy_version" to 38, "position_tick_guards_version" to 48,
            "published_schema_version" to 2, "quote_contract" to 93, "stop_threshold_basis" to 15,
            "structure_contract" to 52, "structure_status" to 10, "target_threshold_basis" to 15,
            "tick_ts" to 26, "valuation_quality" to 4
        )
        prodStr.forEach { (k, n) -> trace.put(k, pad(n + n / 2)) }
        listOf("actual_leg_count", "constant_sl_threshold", "constant_tp_threshold", "contract_lot_size",
            "crossed_quote_legs", "current_pnl", "expected_leg_count", "lot_size_resolved", "max_loss_ref",
            "max_profit_ref", "non_positive_quote_legs", "number_of_lots", "published_age_ms",
            "raw_executable_mark", "sl_threshold", "tp_threshold", "published_sl_threshold",
            "published_tp_threshold", "batch_b_parity_quote_ts").forEach { trace.put(it, num) }
        listOf("batch_b_notification_authority_selected", "batch_b_observation_only", "batch_b_parity_observation",
            "bound_anomaly", "is_eod", "lot_identity_authoritative", "lot_size_assumed",
            "net_basis_alignment_is_new_policy_version", "number_of_lots_assumed", "raw_mark_complete",
            "running_state_updated", "valuation_accepted").forEach { trace.put(it, false) }
        trace.put("structure_problems", JSONArray((0 until 4).map { "problem_${it}_${pad(30)}" }))
        trace.put("batch_b_parity_leg_quote_timings", JSONArray(keys.map {
            JSONObject().put("instrument_key", it).put("source_ts", "2026-09-24T06:31:55.581Z").put("age_ms", 123_456)
        }))
        if (withB3) {
            trace.put("quote_validity", validity.toJson())
            trace.put("mark_trust", trust.toJson().put("paper", true).put("price_gate_applied", true).put("real_observation_only", false))
            trace.put("a1_raw_policy_action", "SHADOW_SL"); trace.put("a1_raw_policy_reason", pad(45))
            trace.put("mark_store_trust", JSONObject().put("paper", true).put("trust_state", TRUST_UNTRUSTED)
                .put("trust_cause", "WIDE_LIQUIDATION_BOOK").put("quote_validity_state", QV_INVALID)
                .put("earliest_source_ms", 1_758_695_515_581L).put("earliest_source_ts", "2026-09-24T06:31:55.581Z")
                .put("latest_source_ms", 1_758_695_515_999L).put("book_fingerprint", "f".repeat(64)))
            trace.put("trusted_extrema_contract", pad(60))
            trace.put("price_policy_trusted", false); trace.put("price_policy_untrusted_cause", "WIDE_LIQUIDATION_BOOK")
            trace.put("stop_target_evaluation", "MID_FALLBACK_STOP_ONLY"); trace.put("mid_fallback_contract", MID_FALLBACK_CONTRACT)
            trace.put("mid_fallback_eligible", true); trace.put("mid_fallback_stop", true)
            trace.put("stop_basis", "MID_FALLBACK_WIDE_BOOK"); trace.put("expected_fill_basis", "EXECUTABLE_BID_ASK")
            trace.put("mid_fallback_pnl_role", "INDICATIVE_NOT_A_CLOSING_PRICE")
            trace.put("quote_refresh", JSONObject().put("contract", QUOTE_REFRESH_CONTRACT).put("paper_only", true)
                .put("attempted", true).put("requests_this_tick", 1).put("requested_keys", JSONArray(keys))
                .put("replaced_keys", JSONArray(keys)).put("refresh_status", "PARTIAL_STILL_INVALID")
                .put("plan_truncated", false).put("max_keys", 8)
                .put("first_attempt_receipt_ts", "2026-09-24T06:31:55.581Z").put("refreshed_receipt_ts", "2026-09-24T06:31:56.981Z")
                .put("first_attempt_reasons", JSONArray(reasons12)))
            trace.put("mark_failure_episode", JSONObject().apply {
                listOf("episode_id", "trade_id", "session_date", "first_failure_ts", "last_failure_ts", "last_notified_ts",
                    "state", "last_cause", "last_reasons_digest", "contract").forEach { put(it, pad(40)) }
                put("consecutive_failures", 999); put("notified_count", 9); put("updated_at", "2026-09-24T06:31:56.981Z")
            })
        }
        val legs = JSONArray(keys.map { k ->
            JSONObject().put("ask", num).put("bid", num).put("executable_price", num).put("instrument_key", k)
                .put("ltp", num).put("mid", num).put("option_type", "PE").put("price_basis", "EXECUTABLE_ASK")
                .put("quote_status", "NON_POSITIVE_QUOTE").put("side", "SELL").put("strike", 25_550).apply {
                    if (withB3) put("source_ts", "2026-09-24T06:31:55.581Z").put("source_age_ms", 123_456)
                        .put("last_trade_ts", "2026-09-24T06:31:50.000Z").put("key_match", "INEXACT_OR_MISSING")
                        .put("source_validity", reasons12.take(3).joinToString(","))
                }
        })
        return JSONObject().apply {
            put("trade_id", pad(36)); put("session_date", "2026-09-24"); put("tick_ts", "2026-09-24T06:31:55.581Z")
            put("source", "P1_REST_60S"); put("auth_source", "ANALYTICS"); put("index_key", "BANKNIFTY")
            put("strategy_type", "IRON_BUTTERFLY_WIDE"); put("status", "OPEN"); put("leg_count", 4)
            put("quantity_units", num); put("contract_lot_size", num); put("number_of_lots", num)
            put("lot_authoritative", true); put("valuation_quality", "BOUND_ANOMALY"); put("mark_basis", "EXECUTABLE")
            put("executable_mark", num); put("mid_mark", num); put("ltp_mark", num); put("current_pnl", num)
            put("current_pnl_r", num); put("running_mae", num); put("running_mfe", num)
            put("policy_action", "SHADOW_SL"); put("policy_reason", pad(45))
            put("policy_trace_json", trace); put("legs_json", legs)
        }
    }

    private fun chunkBytes(row: JSONObject, n: Int, sendId: Boolean): Int {
        val q = JSONArray(); repeat(n) { q.put(JSONObject(row.toString())) }
        ensurePositionTickIdentities(q)
        val body = JSONArray()
        for (i in 0 until n) body.put(positionTickUploadRow(q.getJSONObject(i), sendId))
        return body.toString().toByteArray(Charsets.UTF_8).size
    }

    @Test fun worstCase50RowChunk_measured_andUnderByteGuard() {
        val row = worstRow()
        assertEquals(26, row.length())
        val oneRow = positionTickUploadRow(row, false).toString().toByteArray().size
        val gateOff = chunkBytes(row, POSITION_TICK_UPLOAD_CHUNK_ROWS, sendId = false)
        val gateOn = chunkBytes(row, POSITION_TICK_UPLOAD_CHUNK_ROWS, sendId = true)
        val preB3 = chunkBytes(worstRow(withB3 = false), POSITION_TICK_UPLOAD_CHUNK_ROWS, sendId = false)
        val report = JSONObject()
            .put("worst_row_bytes", oneRow)
            .put("chunk50_gate_off_bytes", gateOff)
            .put("chunk50_gate_on_bytes", gateOn)
            .put("chunk50_pre_b3_shape_bytes", preB3)
            .put("byte_guard", POSITION_TICK_UPLOAD_MAX_CHUNK_BYTES)
        println("B1_1_CHUNK_BYTES $report")
        runCatching { File("build/b1_1_chunk_bytes.json").apply { parentFile?.mkdirs() }.writeText(report.toString(1)) }
        assertEquals("id adds 64 hex + key/quotes/comma per row", gateOff + 50 * (",\"client_event_id\":\"\"".length + 64), gateOn)
        assertTrue("50 worst-case rows must fit the guard so the chunk is not split", gateOn <= POSITION_TICK_UPLOAD_MAX_CHUNK_BYTES)
    }
}
