package com.marketradar.app

import com.marketradar.app.util.LogBuffer
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * R8: fingerprint covers lot/policy; tracking_complete in mark broadcast. R7: unverified 409 retains queue; overflow never silently trims; privacy-safe
 * diagnostics; dedupe never collapses distinct payloads.
 */
class PositionTickFlushTest {

    @Before
    fun clearLogs() {
        LogBuffer.clear()
    }

    @Test
    fun http200_persistedOk() {
        val r = classifyPositionTickFlushFailure(200, "OK", null, null, null, 3)
        assertTrue(r.persisted)
        assertTrue(r.success)
        assertEquals(POSITION_TICK_FLUSH_OK, r.failureClass)
        assertTrue(shouldDrainPositionTickQueue(r))
        val (pending, drained) = applyPositionTickFlushDecision(3, r)
        assertEquals(0, pending)
        assertTrue(drained)
    }

    @Test
    fun http400_schemaPayload_retainsQueue() {
        val r = classifyPositionTickFlushFailure(
            400, "Bad Request", null, null,
            "22P02",
            4
        )
        assertFalse(r.persisted)
        assertEquals(POSITION_TICK_FLUSH_SCHEMA_PAYLOAD, r.failureClass)
        assertEquals(400, r.httpStatus)
        assertEquals("22P02", r.allowlistedServerCode)
        assertFalse(r.detail.contains("trade_id"))
        val (pending, drained) = applyPositionTickFlushDecision(4, r)
        assertEquals(4, pending)
        assertFalse(drained)
    }

    @Test
    fun http401_configAuth_retainsQueue() {
        val r = classifyPositionTickFlushFailure(401, "Unauthorized", null, null, null, 2)
        assertFalse(r.persisted)
        assertEquals(POSITION_TICK_FLUSH_CONFIG_AUTH, r.failureClass)
        assertFalse(shouldDrainPositionTickQueue(r))
    }

    @Test
    fun http422_schemaPayload() {
        val r = classifyPositionTickFlushFailure(422, "Unprocessable", null, null, "23514", 1)
        assertEquals(POSITION_TICK_FLUSH_SCHEMA_PAYLOAD, r.failureClass)
        assertFalse(r.persisted)
    }

    @Test
    fun http409_genericConflict_retainsQueue_notDrained() {
        // Must NOT pass merely because status is 409 or body contains "duplicate".
        val r = classifyPositionTickFlushFailure(
            409, "Conflict", null, null,
            null,
            5,
            verifiedExactDuplicates = false
        )
        assertFalse("generic 409 must not claim persisted", r.persisted)
        assertFalse(r.success)
        assertEquals(POSITION_TICK_FLUSH_CONFLICT_UNVERIFIED, r.failureClass)
        assertFalse(shouldDrainPositionTickQueue(r))
        val (pending, drained) = applyPositionTickFlushDecision(5, r)
        assertEquals(5, pending)
        assertFalse(drained)
    }

    @Test
    fun http409_bodySaysDuplicate_stillUnverified_retains() {
        // Classifier never sees the body; even a "duplicate" narrative must not drain
        // without verifiedExactDuplicates=true.
        val serverCode = extractAllowlistedServerErrorCode(
            """{"code":"23505","message":"duplicate key value violates unique constraint","details":"Key (trade_id, tick_ts)=(286, 2026-09-24T06:40:00Z) already exists. premium=12.5"}"""
        )
        assertEquals("23505", serverCode)
        val r = classifyPositionTickFlushFailure(
            409, "Conflict", null, null, serverCode, 3, verifiedExactDuplicates = false
        )
        assertEquals(POSITION_TICK_FLUSH_CONFLICT_UNVERIFIED, r.failureClass)
        assertFalse(r.persisted)
        assertFalse(shouldDrainPositionTickQueue(r))
    }

    @Test
    fun http409_verifiedExactDuplicates_drains() {
        val r = classifyPositionTickFlushFailure(
            409, "Conflict", null, null, "23505", 4, verifiedExactDuplicates = true
        )
        assertTrue(r.persisted)
        assertEquals(POSITION_TICK_FLUSH_IDEMPOTENT_CONFLICT, r.failureClass)
        assertTrue(shouldDrainPositionTickQueue(r))
        val (pending, drained) = applyPositionTickFlushDecision(4, r)
        assertEquals(0, pending)
        assertTrue(drained)
    }

    @Test
    fun http409_mixedPartialBatch_failClosed_retains() {
        // Partial/mixed batch conflict cannot be proven for every row → retain all.
        val r = classifyPositionTickFlushFailure(
            409, "Conflict", null, null, "23505", 10, verifiedExactDuplicates = false
        )
        assertFalse(r.persisted)
        assertEquals(POSITION_TICK_FLUSH_CONFLICT_UNVERIFIED, r.failureClass)
        val (pending, drained) = applyPositionTickFlushDecision(10, r)
        assertEquals(10, pending)
        assertFalse(drained)
    }

    @Test
    fun http503_server5xx_retains() {
        val r = classifyPositionTickFlushFailure(503, "Service Unavailable", null, null, null, 7)
        assertEquals(POSITION_TICK_FLUSH_SERVER_5XX, r.failureClass)
        assertFalse(r.persisted)
        val (pending, drained) = applyPositionTickFlushDecision(7, r)
        assertEquals(7, pending)
        assertFalse(drained)
    }

    @Test
    fun timeoutException_transient_notPersisted() {
        val r = classifyPositionTickFlushFailure(
            null, null,
            "java.net.SocketTimeoutException",
            "timeout",
            null,
            6
        )
        assertFalse(r.persisted)
        assertEquals(POSITION_TICK_FLUSH_TRANSIENT_NETWORK, r.failureClass)
        assertEquals("SocketTimeoutException", r.exceptionType)
        val (pending, drained) = applyPositionTickFlushDecision(6, r)
        assertEquals(6, pending)
        assertFalse(drained)
    }

    @Test
    fun sslHandshake_transport_notPersisted() {
        val r = classifyPositionTickFlushFailure(
            null, null,
            "javax.net.ssl.SSLHandshakeException",
            "Chain validation failed",
            null,
            3
        )
        assertFalse(r.persisted)
        assertEquals(POSITION_TICK_FLUSH_TRANSPORT, r.failureClass)
    }

    @Test
    fun sanitizeRedactsBearerAndJwt() {
        val raw = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc.def apikey=supersecret"
        val s = sanitizeFlushDiagText(raw)
        assertFalse(s.contains("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"))
        assertFalse(s.lowercase().contains("supersecret"))
        assertTrue(s.contains("***") || s.contains("***jwt***") || s.contains("Bearer ***"))
    }

    @Test
    fun privacySafeDiag_neverIncludesConstraintBodyValues_inLogBuffer() {
        val body = """
            {"code":"23505","message":"duplicate key value violates unique constraint position_ticks_trade_ts",
             "details":"Key (trade_id, tick_ts)=(286, 2026-09-24T06:44:00+05:30) already exists.",
             "hint":null,"premium":349.54,"authorization":"Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig",
             "apikey":"sb_secret_supersecretvalue123","current_pnl":-349.54}
        """.trimIndent()
        val serverCode = extractAllowlistedServerErrorCode(body)
        assertEquals("23505", serverCode)
        val diag = classifyPositionTickFlushFailure(
            409, "Conflict", null, null, serverCode, 2, verifiedExactDuplicates = false
        )
        val line = formatPositionTickInsertFailLog(diag)
        LogBuffer.add('E', "SupabaseClient", line, mirrorToLogcat = false)
        val snap = LogBuffer.snapshot("ALL").joinToString("\n") { it.message }
        // Allowlisted code may appear; raw body values / credentials must not.
        assertTrue(snap.contains("23505") || snap.contains("conflict_unverified"))
        assertFalse(snap.contains("286"))
        assertFalse(snap.contains("2026-09-24T06:44:00"))
        assertFalse(snap.contains("349.54"))
        assertFalse(snap.contains("-349.54"))
        assertFalse(snap.contains("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"))
        assertFalse(snap.contains("sb_secret_supersecretvalue123"))
        assertFalse(snap.contains("already exists"))
        assertFalse(snap.contains("position_ticks_trade_ts"))
        assertFalse(diag.detail.contains("286"))
        assertFalse(diag.detail.contains("premium"))
    }

    @Test
    fun extractAllowlistedServerErrorCode_rejectsNonCodes() {
        assertNull(extractAllowlistedServerErrorCode("""{"message":"nope"}"""))
        assertNull(normalizeAllowlistedServerCode("not-a-code"))
        assertNull(normalizeAllowlistedServerCode("DROP TABLE"))
        assertEquals("PGRST116", extractAllowlistedServerErrorCode("""{"code":"PGRST116"}"""))
    }

    @Test
    fun backoffBoundedAndObservable() {
        val b1 = computePositionTickFlushBackoffMs(1, POSITION_TICK_FLUSH_TRANSIENT_NETWORK)
        val b8 = computePositionTickFlushBackoffMs(8, POSITION_TICK_FLUSH_TRANSIENT_NETWORK)
        val b99 = computePositionTickFlushBackoffMs(99, POSITION_TICK_FLUSH_TRANSIENT_NETWORK)
        assertEquals(60_000L, b1)
        assertTrue(b8 <= 5 * 60_000L)
        assertEquals(5 * 60_000L, b99)
        val auth = computePositionTickFlushBackoffMs(10, POSITION_TICK_FLUSH_CONFIG_AUTH)
        assertTrue(auth <= 5 * 60_000L)
        val conflict = computePositionTickFlushBackoffMs(3, POSITION_TICK_FLUSH_CONFLICT_UNVERIFIED)
        assertTrue(conflict >= 60_000L)
    }

    @Test
    fun dedupeSameKeySamePayload_keepsOne() {
        val row = JSONObject()
            .put("trade_id", "286")
            .put("tick_ts", "t1")
            .put("current_pnl", 1.5)
            .put("executable_mark", 10.0)
        val q = JSONArray().put(JSONObject(row.toString())).put(JSONObject(row.toString()))
        val result = dedupePositionTicksByTradeTs(q)
        assertEquals(1, result.exactDupDropped)
        assertEquals(0, result.contentConflicts)
        assertEquals(1, result.queue.length())
        assertEquals(1.5, result.queue.getJSONObject(0).getDouble("current_pnl"), 0.0)
    }

    @Test
    fun dedupeSameKeyDifferentPayload_retainsAll_reportsConflict() {
        val q = JSONArray()
            .put(JSONObject().put("trade_id", "286").put("tick_ts", "t1").put("current_pnl", 1.0))
            .put(JSONObject().put("trade_id", "286").put("tick_ts", "t1").put("current_pnl", 2.0))
            .put(JSONObject().put("trade_id", "286").put("tick_ts", "t2").put("current_pnl", 3.0))
        val result = dedupePositionTicksByTradeTs(q)
        assertEquals(0, result.exactDupDropped)
        assertEquals(1, result.contentConflicts)
        assertEquals(3, result.queue.length())
        val pnls = (0 until result.queue.length()).map {
            result.queue.getJSONObject(it).getDouble("current_pnl")
        }.toSet()
        assertTrue(pnls.contains(1.0))
        assertTrue(pnls.contains(2.0))
        assertTrue(pnls.contains(3.0))
    }

    @Test
    fun overflowAdmit_preservesPriorRows_rejectsNew_marksIncomplete() {
        val max = 5
        var queue = JSONArray()
        // Fill to capacity across "failures" (no drain).
        for (i in 0 until max) {
            val incoming = JSONArray().put(
                JSONObject().put("trade_id", "T$i").put("tick_ts", "ts$i").put("current_pnl", i.toDouble())
            )
            val adm = admitPositionTicksToBoundedQueue(queue, incoming, max)
            assertEquals(1, adm.admitted)
            assertEquals(0, adm.rejected)
            assertTrue(adm.trackingComplete)
            queue = adm.queue
        }
        assertEquals(max, queue.length())
        val firstTrade = queue.getJSONObject(0).getString("trade_id")
        // Exceed capacity repeatedly — prior rows must remain; new ones rejected.
        for (round in 0 until 20) {
            val incoming = JSONArray().put(
                JSONObject().put("trade_id", "NEW$round").put("tick_ts", "n$round").put("current_pnl", 99.0)
            )
            val adm = admitPositionTicksToBoundedQueue(queue, incoming, max)
            assertEquals(0, adm.admitted)
            assertEquals(1, adm.rejected)
            assertTrue(adm.overflowActive)
            assertFalse(adm.trackingComplete)
            queue = adm.queue
            assertEquals(max, queue.length())
            assertEquals(firstTrade, queue.getJSONObject(0).getString("trade_id"))
            // Rejected trade must not appear.
            for (i in 0 until queue.length()) {
                assertFalse(queue.getJSONObject(i).getString("trade_id") == "NEW$round")
            }
        }
        // Rejected flush still retains pending (fail closed).
        val reject = classifyPositionTickFlushFailure(500, "err", null, null, null, queue.length())
        val (pending, drained) = applyPositionTickFlushDecision(queue.length(), reject)
        assertEquals(max, pending)
        assertFalse(drained)
        assertEquals(firstTrade, queue.getJSONObject(0).getString("trade_id"))
    }

    @Test
    fun overflowBatchPartialAdmit_visibleDegradation() {
        val existing = JSONArray()
            .put(JSONObject().put("trade_id", "A").put("tick_ts", "1"))
            .put(JSONObject().put("trade_id", "B").put("tick_ts", "2"))
        val incoming = JSONArray()
            .put(JSONObject().put("trade_id", "C").put("tick_ts", "3"))
            .put(JSONObject().put("trade_id", "D").put("tick_ts", "4"))
            .put(JSONObject().put("trade_id", "E").put("tick_ts", "5"))
        val adm = admitPositionTicksToBoundedQueue(existing, incoming, maxPending = 4)
        assertEquals(2, adm.admitted)
        assertEquals(1, adm.rejected)
        assertEquals(4, adm.queue.length())
        assertTrue(adm.overflowActive)
        assertFalse(adm.trackingComplete)
        assertEquals("A", adm.queue.getJSONObject(0).getString("trade_id"))
        assertEquals("B", adm.queue.getJSONObject(1).getString("trade_id"))
    }

    @Test
    fun retryAfterRejectionThenSuccess_drainsOnlyOnSuccess() {
        val reject = classifyPositionTickFlushFailure(500, "err", null, null, null, 4)
        var pending = 4
        var (p1, d1) = applyPositionTickFlushDecision(pending, reject)
        assertFalse(d1)
        assertEquals(4, p1)
        pending = p1
        val ok = classifyPositionTickFlushFailure(201, "Created", null, null, null, pending)
        val (p2, d2) = applyPositionTickFlushDecision(pending, ok)
        assertTrue(d2)
        assertEquals(0, p2)
        assertTrue(ok.persisted)
        assertFalse(reject.persisted)
    }

    @Test
    fun fingerprintStableForIdenticalRows() {
        val a = JSONObject().put("trade_id", "1").put("tick_ts", "t").put("current_pnl", 1.0)
        val b = JSONObject().put("trade_id", "1").put("tick_ts", "t").put("current_pnl", 1.0)
        assertEquals(positionTickImmutableFingerprint(a), positionTickImmutableFingerprint(b))
        val c = JSONObject().put("trade_id", "1").put("tick_ts", "t").put("current_pnl", 2.0)
        assertTrue(positionTickImmutableFingerprint(a) != positionTickImmutableFingerprint(c))
    }

    /**
     * Codex R8 reproduction: same trade_id + tick_ts, equal marks/P&L, but different
     * lot authority and policy_trace_json must NOT be treated as exact duplicates.
     */
    @Test
    fun fingerprintCodexCounterexample_lotAuthorityAndPolicyTrace_notDuplicates() {
        val base = JSONObject()
            .put("trade_id", "286")
            .put("tick_ts", "2026-09-24T05:00:00.000Z")
            .put("session_date", "2026-09-24")
            .put("source", "P1_REST_60S")
            .put("executable_mark", 42.5)
            .put("mid_mark", 41.0)
            .put("ltp_mark", 40.0)
            .put("current_pnl", 1677.0)
            .put("current_pnl_r", 0.5)
            .put("valuation_quality", "OK")
            .put("mark_basis", "EXECUTABLE")
            .put("quantity_units", 30)
            .put("contract_lot_size", 15)
            .put("number_of_lots", 2)
        val a = JSONObject(base.toString())
            .put("lot_authoritative", true)
            .put("policy_trace_json", JSONObject().put("path", "A").put("guards", "v3"))
        val b = JSONObject(base.toString())
            .put("lot_authoritative", false)
            .put("policy_trace_json", JSONObject().put("path", "B").put("guards", "v3"))
        assertTrue(
            positionTickImmutableFingerprint(a) != positionTickImmutableFingerprint(b)
        )
        val q = JSONArray().put(a).put(b)
        val result = dedupePositionTicksByTradeTs(q)
        assertEquals(0, result.exactDupDropped)
        assertEquals(1, result.contentConflicts)
        assertEquals(2, result.queue.length())
    }

    @Test
    fun fingerprintCoversBuilderLotAndPolicyFields() {
        val keys = POSITION_TICK_IMMUTABLE_FINGERPRINT_KEYS
        assertTrue(keys.contains("quantity_units"))
        assertTrue(keys.contains("contract_lot_size"))
        assertTrue(keys.contains("number_of_lots"))
        assertTrue(keys.contains("lot_authoritative"))
        assertTrue(keys.contains("policy_trace_json"))
        assertTrue(keys.contains("auth_source"))
        assertTrue(keys.contains("legs_json"))
    }

    @Test
    fun trackingBroadcastPayload_includesTrackingComplete() {
        val status = PositionTickTrackingStatus(
            trackingComplete = false,
            overflowActive = true,
            overflowRejectedCount = 7L
        )
        val payload = positionTickTrackingBroadcastPayload(status)
        assertEquals(true, payload.getBoolean("position_mark_tick"))
        assertEquals(false, payload.getBoolean("tracking_complete"))
        assertEquals(true, payload.getBoolean("overflow_active"))
        assertEquals(7L, payload.getLong("overflow_rejected_count"))
    }
}
