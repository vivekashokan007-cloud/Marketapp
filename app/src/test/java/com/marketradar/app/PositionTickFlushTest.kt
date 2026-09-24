package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Deterministic flush/retry diagnostics: queue retention after HTTP rejection,
 * timeout/network failure, eventual success; failed flush must not report persisted.
 */
class PositionTickFlushTest {

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
            """{"message":"invalid input","hint":"column"}""",
            4
        )
        assertFalse(r.persisted)
        assertEquals(POSITION_TICK_FLUSH_SCHEMA_PAYLOAD, r.failureClass)
        assertEquals(400, r.httpStatus)
        val (pending, drained) = applyPositionTickFlushDecision(4, r)
        assertEquals(4, pending)
        assertFalse(drained)
    }

    @Test
    fun http401_configAuth_retainsQueue() {
        val r = classifyPositionTickFlushFailure(401, "Unauthorized", null, null, "jwt expired", 2)
        assertFalse(r.persisted)
        assertEquals(POSITION_TICK_FLUSH_CONFIG_AUTH, r.failureClass)
        assertFalse(shouldDrainPositionTickQueue(r))
    }

    @Test
    fun http422_schemaPayload() {
        val r = classifyPositionTickFlushFailure(422, "Unprocessable", null, null, "check constraint", 1)
        assertEquals(POSITION_TICK_FLUSH_SCHEMA_PAYLOAD, r.failureClass)
        assertFalse(r.persisted)
    }

    @Test
    fun http409_idempotentConflict_drains() {
        val r = classifyPositionTickFlushFailure(409, "Conflict", null, null, "duplicate", 5)
        assertTrue(r.persisted)
        assertEquals(POSITION_TICK_FLUSH_IDEMPOTENT_CONFLICT, r.failureClass)
        assertTrue(shouldDrainPositionTickQueue(r))
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
    fun backoffBoundedAndObservable() {
        val b1 = computePositionTickFlushBackoffMs(1, POSITION_TICK_FLUSH_TRANSIENT_NETWORK)
        val b8 = computePositionTickFlushBackoffMs(8, POSITION_TICK_FLUSH_TRANSIENT_NETWORK)
        val b99 = computePositionTickFlushBackoffMs(99, POSITION_TICK_FLUSH_TRANSIENT_NETWORK)
        assertEquals(60_000L, b1)
        assertTrue(b8 <= 5 * 60_000L)
        assertEquals(5 * 60_000L, b99)
        val auth = computePositionTickFlushBackoffMs(10, POSITION_TICK_FLUSH_CONFIG_AUTH)
        assertTrue(auth <= 5 * 60_000L)
    }

    @Test
    fun dedupeKeepsLatestByTradeTs() {
        val q = JSONArray()
            .put(JSONObject().put("trade_id", "286").put("tick_ts", "t1").put("current_pnl", 1))
            .put(JSONObject().put("trade_id", "286").put("tick_ts", "t1").put("current_pnl", 2))
            .put(JSONObject().put("trade_id", "286").put("tick_ts", "t2").put("current_pnl", 3))
        val (out, dropped) = dedupePositionTicksByTradeTs(q)
        assertEquals(1, dropped)
        assertEquals(2, out.length())
        assertEquals(2.0, out.getJSONObject(0).getDouble("current_pnl"), 0.0)
        assertEquals("t2", out.getJSONObject(1).getString("tick_ts"))
    }

    @Test
    fun retryAfterRejectionThenSuccess_drainsOnlyOnSuccess() {
        val reject = classifyPositionTickFlushFailure(500, "err", null, null, "boom", 4)
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
}
