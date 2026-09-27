package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * B1.1 acceptance / counterexample tests (Codex R3 reply §3).
 *
 * The store round-trips through a JSON string exactly like SharedPreferences, and
 * the fake server models the production table: no uniqueness on (trade_id, tick_ts)
 * and — only in "identity" mode — the planned unique index on client_event_id with
 * PostgreSQL's atomic multi-row INSERT semantics (verified against local
 * PostgreSQL 17 + PostgREST 12.2.3, see the handoff).
 */
class PositionTickDrainTest {

    // ---------------------------------------------------------------- fixtures

    private class StringStore(initial: String = "[]") : PositionTickQueueStore {
        var raw: String = initial
        var saves = 0
        var onSave: ((JSONArray) -> Unit)? = null
        override fun load(): JSONArray = JSONArray(raw)
        override fun save(queue: JSONArray) { raw = queue.toString(); saves += 1; onSave?.invoke(queue) }
        fun size() = JSONArray(raw).length()
        fun rows(): List<JSONObject> = JSONArray(raw).let { a -> (0 until a.length()).map { a.getJSONObject(it) } }
    }

    private enum class Mode { OK, NETWORK_BEFORE_SEND, COMMIT_THEN_LOSE_ACK, SERVER_5XX, SCHEMA_400, CONFLICT_OTHER }

    private class FakeServer(val identityIndex: Boolean) {
        val table = mutableListOf<JSONObject>()
        val script = ArrayDeque<Mode>()
        val requestSizes = mutableListOf<Int>()
        var beforeReturn: (() -> Unit)? = null

        fun insert(body: JSONArray): PositionTickInsertResult {
            requestSizes.add(body.length())
            val mode = script.removeFirstOrNull() ?: Mode.OK
            try {
                when (mode) {
                    Mode.NETWORK_BEFORE_SEND -> return transient(body.length())
                    Mode.SERVER_5XX -> return classifyPositionTickFlushFailure(503, "Unavailable", null, null, null, body.length())
                    Mode.SCHEMA_400 -> return classifyPositionTickFlushFailure(400, "Bad", null, null, "PGRST204", body.length())
                    Mode.CONFLICT_OTHER -> return classifyPositionTickFlushFailure(409, "Conflict", null, null, "23505", body.length())
                        .copy(allowlistedConstraint = extractAllowlistedConflictConstraint("""{"code":"23505","message":"duplicate key value violates unique constraint \"other_idx\""}"""))
                    else -> {}
                }
                val commit = commit(body)
                if (commit != null) return commit
                return if (mode == Mode.COMMIT_THEN_LOSE_ACK) transient(body.length())
                else classifyPositionTickFlushFailure(201, "Created", null, null, null, body.length())
            } finally {
                beforeReturn?.invoke()
            }
        }

        /** Atomic multi-row insert; returns a 409 result (nothing inserted) on identity clash. */
        private fun commit(body: JSONArray): PositionTickInsertResult? {
            val incoming = (0 until body.length()).map { JSONObject(body.getJSONObject(it).toString()) }
            if (identityIndex) {
                val existing = table.mapNotNull { it.optString(POSITION_TICK_CLIENT_EVENT_ID_KEY, "").ifBlank { null } }.toMutableSet()
                for (r in incoming) {
                    val id = r.optString(POSITION_TICK_CLIENT_EVENT_ID_KEY, "").ifBlank { null } ?: continue
                    if (!existing.add(id)) {
                        val pgBody = """{"code":"23505","details":null,"hint":null,"message":"duplicate key value violates unique constraint \"$POSITION_TICK_CLIENT_EVENT_ID_UNIQUE_INDEX\""}"""
                        return classifyPositionTickFlushFailure(
                            409, "Conflict", null, null, extractAllowlistedServerErrorCode(pgBody), body.length()
                        ).copy(allowlistedConstraint = extractAllowlistedConflictConstraint(pgBody))
                    }
                }
            }
            table.addAll(incoming)
            return null
        }

        private fun transient(n: Int) = classifyPositionTickFlushFailure(
            null, null, "java.net.SocketTimeoutException", "timeout", null, n
        )

        fun countAt(tradeId: String, tickTs: String) = table.count { it.optString("trade_id") == tradeId && it.optString("tick_ts") == tickTs }
    }

    private fun row(tradeId: String, i: Int, pnl: Double = -1234.5 + i, legs: Int = 4): JSONObject {
        val ts = "2026-09-24T%02d:%02d:00.000Z".format(4 + i / 60, i % 60)
        return JSONObject().apply {
            put("trade_id", tradeId); put("session_date", "2026-09-24"); put("tick_ts", ts)
            put("source", "P1_REST_60S"); put("auth_source", "DAILY"); put("index_key", "NIFTY")
            put("strategy_type", "IRON_CONDOR"); put("status", "OPEN"); put("leg_count", legs)
            put("quantity_units", 75.0); put("contract_lot_size", 75); put("number_of_lots", 1.0)
            put("lot_authoritative", true); put("valuation_quality", "OK"); put("mark_basis", "EXECUTABLE")
            put("executable_mark", 12.35); put("mid_mark", 12.1); put("ltp_mark", JSONObject.NULL)
            put("current_pnl", pnl); put("current_pnl_r", -0.25); put("running_mae", -1500.0); put("running_mfe", 300.0)
            put("policy_action", "HOLD"); put("policy_reason", "WITHIN_THRESHOLDS")
            put("policy_trace_json", JSONObject().put("policy_version", "position_policy_v1").put("sl_threshold", -3000.0)
                .put("quote_validity", JSONObject().put("state", "VALID").put("reasons", JSONArray())))
            put("legs_json", JSONArray().apply {
                repeat(legs) { k -> put(JSONObject().put("instrument_key", "NSE_FO|4$k$i").put("bid", 10.05).put("ask", 10.25).put("side", if (k % 2 == 0) "SELL" else "BUY")) }
            })
        }
    }

    /** Queue exactly as an older app build persisted it: no client_event_id. */
    private fun legacyQueue(n: Int, tradeId: String = "T900"): String =
        JSONArray().apply { repeat(n) { put(row(tradeId, it)) } }.toString()

    private fun drain(store: StringStore, server: FakeServer, send: Boolean = false, maxChunks: Int = POSITION_TICK_UPLOAD_MAX_CHUNKS_PER_DRAIN) =
        drainPositionTickQueue(store, PositionTickTransport { server.insert(it) },
            PositionTickDrainConfig(sendClientEventId = send, maxChunksPerDrain = maxChunks), Any())

    private fun src(rel: String): String = listOf(File(rel), File("app/$rel")).first { it.isFile }.readText()

    // ------------------------------------------- §3 acceptance: eligibility split

    @Test fun noOpenTrades_captureIneligible_butQueueDrainsCompletely() {
        assertFalse(positionTickCaptureEligible(openTradeCount = 0, marketSessionActive = true))
        assertTrue(positionTickUploadEligible(pendingDepth = 120, networkAvailable = true))
        val store = StringStore(legacyQueue(120))
        val server = FakeServer(identityIndex = false)
        val r = drain(store, server)
        assertEquals(PositionTickDrainOutcome.DRAINED, r.outcome)
        assertEquals(0, store.size())
        assertEquals(120, server.table.size)
        assertEquals(listOf(50, 50, 20), server.requestSizes)
    }

    @Test fun outsideSession_captureIneligible_uploadStillEligible_andAttemptDecisionHasNoClockOfDay() {
        assertFalse(positionTickCaptureEligible(openTradeCount = 3, marketSessionActive = false))
        assertTrue(positionTickUploadEligible(pendingDepth = 1, networkAvailable = true))
        assertFalse(positionTickUploadEligible(pendingDepth = 0, networkAvailable = true))
        assertFalse(positionTickUploadEligible(pendingDepth = 5, networkAvailable = false))
        // Any trigger at e.g. 22:00 IST: the decision only looks at spacing/backoff.
        for (t in listOf(PositionTickDrainTrigger.APP_START, PositionTickDrainTrigger.CONNECTIVITY,
            PositionTickDrainTrigger.ENSURE_RUNNING_NO_CAPTURE, PositionTickDrainTrigger.SESSION_CLOSED)) {
            assertTrue(t, decidePositionTickDrainAttempt(t, 1_000_000L, 0L, 0, null).allowed)
        }
        val store = StringStore(legacyQueue(7))
        val server = FakeServer(false)
        drain(store, server)
        assertEquals(0, store.size())
        val decision = src("src/main/java/com/marketradar/app/PositionTickDrain.kt")
            .substringAfter("internal fun decidePositionTickDrainAttempt(").substringBefore("\n}\n")
        assertFalse(decision.contains("Calendar") || decision.contains("IST") || decision.contains("Session"))
    }

    @Test fun ensureRunning_routesIneligibleCaptureToDrain_beforeAnyServiceStart() {
        val s = src("src/main/java/com/marketradar/app/PositionTickService.kt")
        val body = s.substringAfter("fun ensureRunning(context: Context) {").substringBefore("private fun marketSessionActiveNow()")
        val gate = body.indexOf("if (!positionTickCaptureEligible(open.length(), marketSessionActiveNow()))")
        val drain = body.indexOf("PositionTickDrainTrigger.ENSURE_RUNNING_NO_CAPTURE")
        val start = body.indexOf("startForegroundService(intent)")
        assertTrue(gate in 0 until drain)
        assertTrue(drain < start)
        assertTrue(body.substring(drain, start).contains("return"))
    }

    @Test fun triggers_wiredAtAppStartUpgradeConnectivityServiceStartAndAfterEnqueue() {
        val app = src("src/main/java/com/marketradar/app/MarketRadarApp.kt")
        assertTrue(app.contains("PositionTickUploadRunner.onProcessStart(this, PositionTickDrainTrigger.APP_START)"))
        val life = src("src/main/java/com/marketradar/app/MarketOpenScheduler.kt")
        // B1.1 R2: receivers enqueue the durable WorkManager job under goAsync().
        assertTrue(life.contains("val pending = goAsync()"))
        assertTrue(life.contains("PositionTickUploadRunner.enqueueDurableFromReceiver(context) { pending.finish() }"))
        val runner = src("src/main/java/com/marketradar/app/PositionTickUploadRunner.kt")
        assertTrue(runner.contains("registerDefaultNetworkCallback"))
        assertTrue(runner.contains("request(app, PositionTickDrainTrigger.CONNECTIVITY)"))
        val svc = src("src/main/java/com/marketradar/app/PositionTickService.kt")
        assertTrue(svc.contains("PositionTickUploadRunner.request(applicationContext, PositionTickDrainTrigger.SERVICE_START)"))
        val enq = svc.indexOf("enqueueRows(rows)\n        PositionTickUploadRunner.request(applicationContext, PositionTickDrainTrigger.AFTER_ENQUEUE)")
        assertTrue(enq > 0)
        assertFalse("old whole-queue flush must be gone", svc.contains("private fun flushPending("))
    }

    @Test fun noQuoteCaptureOutsideSession_drainPathHasNoQuoteOrPolicyAccess() {
        for (f in listOf("PositionTickDrain.kt", "PositionTickUploadRunner.kt", "PositionTickIdentity.kt",
            "PositionTickDrainCoordinator.kt", "PositionTickDrainWorker.kt")) {
            val s = src("src/main/java/com/marketradar/app/$f")
            for (banned in listOf("fetchQuotes", "api.upstox.com", "market-quote", "captureOnce", "buildTickRow",
                "PositionPolicyV1", "maybeNotifyShadowExit", "startForegroundService")) {
                assertFalse("$f must not reference $banned", s.contains(banned))
            }
        }
        val svc = src("src/main/java/com/marketradar/app/PositionTickService.kt")
        val cap = svc.substringAfter("private fun captureOnce(): Boolean {")
        val sessionGate = cap.indexOf("if (!isMarketSessionActive()) {")
        val sessionReturn = cap.indexOf("return false", sessionGate)
        val fetch = cap.indexOf("fetchQuotesWithFallback(allKeys)")
        assertTrue(sessionGate in 0 until sessionReturn)
        assertTrue("quotes are fetched only after the session gate returned", sessionReturn < fetch)
        val noTrades = cap.indexOf("if (openTrades.length() == 0) {")
        assertTrue(noTrades in 0 until fetch)
    }

    // --------------------------------------------------- §3: restart / network

    @Test fun restart_afterPartialDrain_resumesFifoFromDurableQueue_noRowLost() {
        val store = StringStore(legacyQueue(130))
        val originalIds = JSONArray(store.raw).let { a -> (0 until a.length()).map { computePositionTickClientEventId(a.getJSONObject(it)) } }
        val server = FakeServer(false)
        server.script.addAll(listOf(Mode.OK, Mode.SERVER_5XX))
        val first = drain(store, server)
        assertEquals(PositionTickDrainOutcome.FAILED, first.outcome)
        assertEquals(80, store.size())
        // "Process restart": brand-new drain with only the persisted string.
        val reborn = StringStore(store.raw)
        val second = drain(reborn, server)
        assertEquals(PositionTickDrainOutcome.DRAINED, second.outcome)
        assertEquals(0, reborn.size())
        assertEquals(130, server.table.size)
        val uploaded = server.table.map { computePositionTickClientEventId(it) }
        assertEquals("FIFO, each row exactly once", originalIds, uploaded)
    }

    @Test fun networkLoss_retainsWholeQueueUnchanged_andConnectivityRecoveryMayRetryEarly() {
        val store = StringStore(legacyQueue(60))
        val before = JSONArray(store.raw).let { a -> (0 until a.length()).map { positionTickCanonicalPayload(a.getJSONObject(it)) } }
        val server = FakeServer(false)
        server.script.add(Mode.NETWORK_BEFORE_SEND)
        val r = drain(store, server)
        assertEquals(PositionTickDrainOutcome.FAILED, r.outcome)
        assertEquals(POSITION_TICK_FLUSH_TRANSIENT_NETWORK, r.failureClass)
        assertEquals(0, r.rowsAcked)
        val after = store.rows().map { positionTickCanonicalPayload(it) }
        assertEquals("content unchanged; only client_event_id was added", before, after)
        assertTrue(store.rows().all { isValidPositionTickClientEventId(it.optString(POSITION_TICK_CLIENT_EVENT_ID_KEY)) })
        assertTrue(decidePositionTickDrainAttempt(PositionTickDrainTrigger.CONNECTIVITY, 100_000L, 90_000L, 1, POSITION_TICK_FLUSH_TRANSIENT_NETWORK).allowed)
        assertFalse(decidePositionTickDrainAttempt(PositionTickDrainTrigger.AFTER_ENQUEUE, 100_000L, 90_000L, 1, POSITION_TICK_FLUSH_TRANSIENT_NETWORK).allowed)
        // A rejected payload keeps its backoff even when the network comes back.
        assertFalse(decidePositionTickDrainAttempt(PositionTickDrainTrigger.CONNECTIVITY, 100_000L, 90_000L, 1, POSITION_TICK_FLUSH_SCHEMA_PAYLOAD).allowed)
        assertFalse(decidePositionTickDrainAttempt(PositionTickDrainTrigger.APP_START, 100_000L, 97_000L, 0, null).allowed)
        drain(store, server)
        assertEquals(0, store.size())
        assertEquals(60, server.table.size)
    }

    // ------------------------------------------------ §3: lost ack / exact retry

    @Test fun serverSuccessThenLostAck_withIdentity_resolvesAsAlreadyPersisted_noDuplicate() {
        val store = StringStore(legacyQueue(70))
        val server = FakeServer(identityIndex = true)
        server.script.add(Mode.COMMIT_THEN_LOSE_ACK)
        val first = drain(store, server, send = true)
        assertEquals(PositionTickDrainOutcome.FAILED, first.outcome)
        assertEquals("chunk kept although the server committed it", 70, store.size())
        assertEquals(50, server.table.size)
        val second = drain(store, server, send = true)
        assertEquals(PositionTickDrainOutcome.DRAINED, second.outcome)
        assertEquals(50, second.rowsIdempotent)
        assertEquals(70, second.rowsAcked)
        assertEquals(0, store.size())
        assertEquals(70, server.table.size)
        assertEquals(70, server.table.map { it.getString(POSITION_TICK_CLIENT_EVENT_ID_KEY) }.toSet().size)
    }

    @Test fun serverSuccessThenLostAck_withoutIdentityGate_neverLosesRows_butCanDuplicate_documentedGap() {
        // Current production (column not applied, gate off): the retry re-inserts the
        // committed chunk. This is the known gap behind the 16 duplicate groups; the
        // test pins it so nobody claims idempotency before the migration + gate.
        val store = StringStore(legacyQueue(70))
        val server = FakeServer(identityIndex = false)
        server.script.add(Mode.COMMIT_THEN_LOSE_ACK)
        drain(store, server)
        assertEquals(70, store.size())
        drain(store, server)
        assertEquals(0, store.size())
        assertEquals(120, server.table.size)
        assertEquals(2, server.countAt("T900", row("T900", 0).getString("tick_ts")))
        assertTrue(server.table.none { it.has(POSITION_TICK_CLIENT_EVENT_ID_KEY) })
    }

    @Test fun exactRetry_sameChunkTwice_withIdentity_storesEachEventOnce() {
        val rows = JSONArray().apply { repeat(10) { put(row("T1", it)) } }
        ensurePositionTickIdentities(rows)
        val server = FakeServer(true)
        val body = JSONArray().apply { for (i in 0 until rows.length()) put(positionTickUploadRow(rows.getJSONObject(i), true)) }
        assertTrue(server.insert(body).persisted)
        val retry = server.insert(body)
        assertFalse(retry.persisted)
        assertTrue(isPositionTickClientEventIdDuplicate(retry))
        assertEquals(10, server.table.size)
        // Through the drain: queue holding the same 10 rows resolves idempotently.
        val store = StringStore(rows.toString())
        val r = drain(store, server, send = true)
        assertEquals(10, r.rowsIdempotent)
        assertEquals(0, store.size())
        assertEquals(10, server.table.size)
    }

    @Test fun conflictingPayloadSameTradeAndTime_getsDifferentIdentity_bothKeptAndUploaded() {
        val a = row("T7", 5, pnl = -100.0)
        val b = row("T7", 5, pnl = -250.0)
        assertEquals(a.getString("tick_ts"), b.getString("tick_ts"))
        assertNotEquals(computePositionTickClientEventId(a), computePositionTickClientEventId(b))
        for (identity in listOf(false, true)) {
            val store = StringStore(JSONArray().put(JSONObject(a.toString())).put(JSONObject(b.toString())).toString())
            val server = FakeServer(identity)
            val r = drain(store, server, send = identity)
            assertEquals(1, r.contentConflicts)
            assertEquals(0, store.size())
            assertEquals("different evidence stays different ($identity)", 2, server.countAt("T7", a.getString("tick_ts")))
        }
    }

    @Test fun exactDuplicateRowsInQueue_shareIdentity_uploadOnce() {
        val a = row("T8", 1)
        val store = StringStore(JSONArray().put(a).put(JSONObject(a.toString())).toString())
        val server = FakeServer(false)
        val r = drain(store, server)
        assertEquals(1, r.exactDupDropped)
        assertEquals(1, server.table.size)
    }

    // ----------------------------------------------------- §3: chunk failures

    @Test fun secondChunkFailure_keepsSecondAndLaterChunks_inOrder_firstAckedOnly() {
        val store = StringStore(legacyQueue(140))
        val all = store.load().let { q -> (0 until q.length()).map { computePositionTickClientEventId(q.getJSONObject(it)) } }
        val server = FakeServer(false)
        server.script.addAll(listOf(Mode.OK, Mode.SCHEMA_400))
        val r = drain(store, server)
        assertEquals(PositionTickDrainOutcome.FAILED, r.outcome)
        assertEquals(POSITION_TICK_FLUSH_SCHEMA_PAYLOAD, r.failureClass)
        assertEquals(1, r.chunksAcked)
        assertEquals(50, r.rowsAcked)
        assertEquals(90, r.remaining)
        assertEquals(all.drop(50), store.rows().map { it.getString(POSITION_TICK_CLIENT_EVENT_ID_KEY) })
        assertEquals(listOf(50, 50), server.requestSizes)
    }

    @Test fun perRowResolution_stopsAtFirstRealFailure_keepsThatRowAndLater() {
        val store = StringStore(legacyQueue(5))
        val server = FakeServer(true)
        server.script.add(Mode.COMMIT_THEN_LOSE_ACK)   // chunk of 5 committed, ack lost
        drain(store, server, send = true)
        store.raw = JSONArray(store.raw).apply { put(row("T901", 99)) }.toString() // one new row
        // Next pass: chunk 409 -> rows 1..2 resolve, row 3 hits a 5xx.
        server.script.addAll(listOf(Mode.OK, Mode.OK, Mode.OK, Mode.SERVER_5XX))
        val r = drain(store, server, send = true)
        assertEquals(PositionTickDrainOutcome.FAILED, r.outcome)
        assertEquals(2, r.rowsIdempotent)
        assertEquals(4, store.size())
        assertEquals(5, server.table.size)
    }

    @Test fun conflictWithoutIdentityProof_failsClosed() {
        val store = StringStore(legacyQueue(3))
        val server = FakeServer(true)
        server.script.add(Mode.CONFLICT_OTHER)
        val r = drain(store, server, send = true)
        assertEquals(POSITION_TICK_FLUSH_CONFLICT_UNVERIFIED, r.failureClass)
        assertEquals(3, store.size())
        assertEquals(0, r.rowsIdempotent)
        // Gate off: even a matching constraint name is never used as proof.
        val s2 = StringStore(legacyQueue(3))
        val srv2 = FakeServer(true)
        srv2.script.add(Mode.COMMIT_THEN_LOSE_ACK)
        drain(s2, srv2, send = true)
        val off = drain(s2, srv2, send = false)
        assertEquals(PositionTickDrainOutcome.DRAINED, off.outcome) // no id sent -> server has no clash
        assertEquals(0, off.rowsIdempotent)
    }

    @Test fun backlog480_isNeverPostedInOneRequest_chunkConstantIs50() {
        assertEquals(50, POSITION_TICK_UPLOAD_CHUNK_ROWS)
        val store = StringStore(legacyQueue(480))
        val server = FakeServer(false)
        drain(store, server)
        assertEquals(List(9) { 50 } + 30, server.requestSizes)
        assertTrue(server.requestSizes.all { it <= POSITION_TICK_UPLOAD_CHUNK_ROWS })
    }

    @Test fun byteGuard_shrinksChunk_neverBelowOneRow() {
        val store = StringStore(legacyQueue(6))
        val server = FakeServer(false)
        val one = positionTickUploadRow(row("T900", 0), false).toString().toByteArray().size
        val r = drainPositionTickQueue(store, PositionTickTransport { server.insert(it) },
            PositionTickDrainConfig(maxChunkBytes = one * 2 + 3), Any())
        assertEquals(0, store.size())
        assertTrue(server.requestSizes.all { it in 1..2 })
        val tiny = StringStore(legacyQueue(2))
        val s2 = FakeServer(false)
        drainPositionTickQueue(tiny, PositionTickTransport { s2.insert(it) }, PositionTickDrainConfig(maxChunkBytes = 10), Any())
        assertEquals(listOf(1, 1), s2.requestSizes)
        assertTrue(r.maxChunkBytesSent <= one * 2 + 3)
    }

    @Test fun concurrentEnqueueDuringUpload_isNotLostByAck() {
        val store = StringStore(legacyQueue(50, "T1"))
        val server = FakeServer(false)
        var injected = false
        server.beforeReturn = {
            if (!injected) {
                injected = true
                val q = store.load(); val extra = row("T2", 0); ensurePositionTickIdentities(JSONArray().put(extra))
                q.put(extra); store.raw = q.toString()
            }
        }
        val r = drain(store, server, maxChunks = 1)
        assertEquals(50, r.rowsAcked)
        assertEquals(1, store.size())
        assertEquals("T2", store.rows()[0].getString("trade_id"))
    }

    // --------------------------------------------- §3: legacy rows / overflow

    @Test fun legacyRowsWithoutIdentity_getContentDerivedIdBeforeUpload_contentOtherwiseUnchanged() {
        val raw = legacyQueue(3)
        val legacy = JSONArray(raw)
        assertTrue((0 until 3).none { legacy.getJSONObject(it).has(POSITION_TICK_CLIENT_EVENT_ID_KEY) })
        val expected = (0 until 3).map { computePositionTickClientEventId(legacy.getJSONObject(it)) }
        val store = StringStore(raw)
        val server = FakeServer(false)
        server.script.add(Mode.SERVER_5XX)
        val r = drain(store, server)
        assertEquals(3, r.identitiesAssigned)
        assertEquals(expected, store.rows().map { it.getString(POSITION_TICK_CLIENT_EVENT_ID_KEY) })
        assertEquals((0 until 3).map { positionTickCanonicalPayload(legacy.getJSONObject(it)) },
            store.rows().map { positionTickCanonicalPayload(it) })
        // Gate off -> the POST body is exactly the legacy key set.
        drain(store, server)
        assertEquals(legacy.getJSONObject(0).keySet(), server.table[0].keySet())
        // Identity is stable across a SharedPreferences-style round trip.
        assertEquals(expected[0], computePositionTickClientEventId(JSONObject(JSONObject(legacy.getJSONObject(0).toString()).toString())))
    }

    @Test fun tamperedStoredIdentity_isRederivedFromContent() {
        val a = row("T3", 1)
        val q = JSONArray().put(JSONObject(a.toString()).put(POSITION_TICK_CLIENT_EVENT_ID_KEY, "f".repeat(64)))
        assertEquals(1, ensurePositionTickIdentities(q))
        assertEquals(computePositionTickClientEventId(a), q.getJSONObject(0).getString(POSITION_TICK_CLIENT_EVENT_ID_KEY))
        assertEquals(0, ensurePositionTickIdentities(q))
    }

    @Test fun overflowHistory_survivesDrain_trackingStaysIncomplete_countersUntouched() {
        // Admission when full: old rows preserved, new refused (pre-B1.1 invariant).
        val full = JSONArray(legacyQueue(POSITION_TICK_MAX_PENDING))
        val adm = admitPositionTicksToBoundedQueue(full, JSONArray().put(row("NEW", 1)), POSITION_TICK_MAX_PENDING)
        assertEquals(1, adm.rejected)
        assertEquals(POSITION_TICK_MAX_PENDING, adm.queue.length())
        assertFalse(adm.trackingComplete)

        val store = StringStore(adm.queue.toString())
        val server = FakeServer(false)
        val r = drain(store, server)
        assertEquals(PositionTickDrainOutcome.DRAINED, r.outcome)
        assertEquals(30, r.chunksAttempted)
        assertEquals(0, store.size())
        val after = positionTickTrackingAfterDrain(r, overflowActiveFlag = true, trackingCompletePref = false, rejectedTotal = 1L)
        assertFalse("capacity freed", after.overflowActive)
        assertFalse("historical gap stays visible", after.trackingComplete)
        val status = derivePositionTickTrackingStatus(after.overflowActive, after.trackingComplete, 1L)
        assertFalse(status.trackingComplete)
        assertEquals(1L, status.overflowRejectedCount)
        // No history -> tracking restored after progress.
        assertTrue(positionTickTrackingAfterDrain(r, true, false, 0L).trackingComplete)
        // No progress (failure) -> flags unchanged.
        val failed = r.copy(rowsAcked = 0, remaining = POSITION_TICK_MAX_PENDING)
        assertTrue(positionTickTrackingAfterDrain(failed, true, false, 0L).overflowActive)
    }

    @Test fun drainNeverClearsOrTruncatesQueue_sourceContract() {
        val d = src("src/main/java/com/marketradar/app/PositionTickDrain.kt")
        val runner = src("src/main/java/com/marketradar/app/PositionTickUploadRunner.kt")
        for (s in listOf(d, runner)) {
            assertFalse(s.contains("putString(PREF_POSITION_TICK_PENDING_QUEUE, \"[]\")"))
            assertFalse(s.contains(".remove(PREF_POSITION_TICK_PENDING_QUEUE"))
        }
        assertTrue(d.contains("shouldDrainPositionTickQueue(result)"))
        assertTrue(d.contains("positionTickRowIdentity(row) in ids"))
    }

    // ------------------------------------------------------------- gate/identity

    @Test fun producerKeyGate_isCompiledOff_andBothHalvesRequired() {
        assertFalse("B1.1 must not ship the client_event_id key", POSITION_TICK_CLIENT_EVENT_ID_SEND_COMPILED)
        assertFalse(positionTickClientEventIdSendAllowed(false, true))
        assertFalse(positionTickClientEventIdSendAllowed(true, null))
        assertFalse(positionTickClientEventIdSendAllowed(true, false))
        assertTrue(positionTickClientEventIdSendAllowed(true, true))
        val q = JSONArray().put(row("T1", 1)); ensurePositionTickIdentities(q)
        val off = positionTickUploadRow(q.getJSONObject(0), false)
        assertFalse(off.has(POSITION_TICK_CLIENT_EVENT_ID_KEY))
        assertEquals(26, off.length())
        val on = positionTickUploadRow(q.getJSONObject(0), true)
        assertEquals(q.getJSONObject(0).getString(POSITION_TICK_CLIENT_EVENT_ID_KEY), on.getString(POSITION_TICK_CLIENT_EVENT_ID_KEY))
        // Runner consults the compiled flag before any probe/network call.
        val runner = src("src/main/java/com/marketradar/app/PositionTickUploadRunner.kt")
        val gate = runner.substringAfter("private fun resolveClientEventIdGate(): Boolean {")
        assertTrue(gate.indexOf("if (!POSITION_TICK_CLIENT_EVENT_ID_SEND_COMPILED) return false") <
            gate.indexOf("probePositionTickClientEventIdColumn"))
    }

    @Test fun identity_goldenVector_matchesPythonReference() {
        // Same fixture + digest as tests/test_b1_1_tick_identity_20260927.py.
        val r = JSONObject("""{"trade_id":"T42","tick_ts":"2026-09-24T06:31:55.581Z","leg_count":4,
            "quantity_units":30.0,"lot_authoritative":true,"ltp_mark":null,"current_pnl":-1234.50,
            "policy_trace_json":{"b":[1,2.50,"x\"y"],"a":{"z":null,"k":"\u00e9\n"}},
            "id":99,"created_at":"2026-09-24T06:31:56Z"}""")
        assertEquals(
            """{"current_pnl":-1234.5,"leg_count":4,"lot_authoritative":true,"ltp_mark":null,"policy_trace_json":{"a":{"k":"é\u000a","z":null},"b":[1,2.5,"x\"y"]},"quantity_units":30,"tick_ts":"2026-09-24T06:31:55.581Z","trade_id":"T42"}""",
            positionTickCanonicalPayload(r)
        )
        assertEquals(GOLDEN_ID, computePositionTickClientEventId(r))
    }

    @Test fun identity_isKeyOrderAndNumberFormIndependent_butNullVsAbsentAndValuesMatter() {
        val a = JSONObject("""{"trade_id":"T1","current_pnl":30,"x":1.0E-7}""")
        val b = JSONObject("""{"x":0.0000001,"current_pnl":30.000,"trade_id":"T1"}""")
        assertEquals(computePositionTickClientEventId(a), computePositionTickClientEventId(b))
        val c = JSONObject("""{"trade_id":"T1","current_pnl":30,"x":1.0E-7,"ltp_mark":null}""")
        assertNotEquals(computePositionTickClientEventId(a), computePositionTickClientEventId(c))
        val d = JSONObject("""{"trade_id":"T1","current_pnl":30.01,"x":1.0E-7}""")
        assertNotEquals(computePositionTickClientEventId(a), computePositionTickClientEventId(d))
        val e = JSONObject(a.toString()).put("id", 5).put("created_at", "z").put(POSITION_TICK_CLIENT_EVENT_ID_KEY, "q")
        assertEquals("server fields and the id itself are excluded", computePositionTickClientEventId(a), computePositionTickClientEventId(e))
        assertTrue(isValidPositionTickClientEventId(computePositionTickClientEventId(a)))
    }

    @Test fun conflictConstraintExtraction_isAllowlistedOnly() {
        val pg = """{"code":"23505","details":null,"hint":null,"message":"duplicate key value violates unique constraint \"position_ticks_client_event_id_uidx\""}"""
        assertEquals(POSITION_TICK_CLIENT_EVENT_ID_UNIQUE_INDEX, extractAllowlistedConflictConstraint(pg))
        assertNull(extractAllowlistedConflictConstraint("""{"message":"duplicate key value violates unique constraint \"position_ticks_pkey\""}"""))
        assertNull(extractAllowlistedConflictConstraint("""{"message":"position_ticks_client_event_id_uidx_other"}"""))
        assertNull(extractAllowlistedConflictConstraint(null))
    }

    @Test fun drainLog_isPrivacySafe_andCarriesRequiredCounters() {
        val store = StringStore(legacyQueue(60, "SECRET_TRADE_77"))
        val server = FakeServer(false)
        server.script.addAll(listOf(Mode.OK, Mode.SERVER_5XX))
        val r = drain(store, server)
        val line = formatPositionTickDrainLog("app_start", r, false, 12L, false, 42L)
        for (k in listOf("depth_before=60", "chunk_size=50", "acked=50", "remaining=10", "class=server_5xx",
            "overflow_active=false", "overflow_rejected_total=12", "tracking_complete=false", "status=503")) {
            assertTrue("$k in $line", line.contains(k))
        }
        for (bad in listOf("SECRET_TRADE_77", "NSE_FO", "1234", "12.35", "IRON_CONDOR", "eyJ", "apikey", "Bearer")) {
            assertFalse("$bad leaked", line.contains(bad))
        }
    }

    companion object {
        const val GOLDEN_ID = "f1638593ccf912b3fbbe1d9ebbfd0d2f72a7209c7f1d62363016ca911065df15"
    }
}
