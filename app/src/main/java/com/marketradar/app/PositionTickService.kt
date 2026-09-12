package com.marketradar.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import com.marketradar.app.util.LogBuffer
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.TimeUnit
import kotlin.math.max
import kotlin.math.min
import kotlin.random.Random

class PositionTickService : Service() {
    private lateinit var prefs: SharedPreferences
    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var loopJob: Job? = null
    private var foregroundReady = false
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        .build()

    override fun onCreate() {
        super.onCreate()
        prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        if (foregroundStartBackoffRemainingMs() > 0L) {
            val remainingMs = foregroundStartBackoffRemainingMs()
            Log.w(TAG, "POSITION_TICK_START_BACKOFF_ACTIVE: remainingMs=$remainingMs")
            LogBuffer.add('W', TAG, "POSITION_TICK_START_BACKOFF_ACTIVE: remainingMs=$remainingMs")
            stopSelf()
            return
        }
        try {
            startTickForeground(NOTIFICATION_ID, buildNotification())
            foregroundReady = true
            clearForegroundStartBackoff()
        } catch (t: Throwable) {
            Log.e(TAG, "POSITION_TICK_FOREGROUND_BLOCKED: ${t.javaClass.simpleName}: ${t.message}")
            LogBuffer.add('E', TAG, "POSITION_TICK_FOREGROUND_BLOCKED: ${t.javaClass.simpleName}: ${t.message}")
            recordForegroundStartBlocked(t)
            stopSelf()
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!foregroundReady) return START_NOT_STICKY
        if (loopJob?.isActive != true) {
            loopJob = serviceScope.launch { runLoop() }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        Thread { flushPending(force = true) }.start()
        serviceScope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun startTickForeground(id: Int, notification: android.app.Notification) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(id, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(id, notification)
        }
    }

    private fun foregroundStartBackoffRemainingMs(nowMs: Long = System.currentTimeMillis()): Long {
        val untilMs = prefs.getLong(PREF_FGS_BLOCKED_UNTIL_MS, 0L)
        return (untilMs - nowMs).coerceAtLeast(0L)
    }

    private fun clearForegroundStartBackoff() {
        if (!prefs.contains(PREF_FGS_BLOCKED_UNTIL_MS) && !prefs.contains(PREF_FGS_BLOCKED_COUNT)) return
        prefs.edit()
            .remove(PREF_FGS_BLOCKED_UNTIL_MS)
            .remove(PREF_FGS_BLOCKED_COUNT)
            .commit()
        LogBuffer.add('I', TAG, "POSITION_TICK_BACKOFF_CLEARED")
    }

    private fun recordForegroundStartBlocked(t: Throwable) {
        val now = System.currentTimeMillis()
        val previousCount = prefs.getInt(PREF_FGS_BLOCKED_COUNT, 0)
        val count = (previousCount + 1).coerceAtMost(6)
        val backoffMs = (FGS_BLOCKED_BASE_BACKOFF_MS * count).coerceAtMost(FGS_BLOCKED_MAX_BACKOFF_MS)
        prefs.edit()
            .putInt(PREF_FGS_BLOCKED_COUNT, count)
            .putLong(PREF_FGS_BLOCKED_UNTIL_MS, now + backoffMs)
            .commit()
        Log.w(TAG, "POSITION_TICK_FOREGROUND_BACKOFF: count=$count backoffMs=$backoffMs error=${t.javaClass.simpleName}")
        LogBuffer.add('W', TAG, "POSITION_TICK_FOREGROUND_BACKOFF: count=$count backoffMs=$backoffMs error=${t.javaClass.simpleName}")
    }

    private suspend fun runLoop() {
        while (serviceScope.coroutineContext.isActive) {
            val shouldContinue = captureOnce()
            if (!shouldContinue) {
                stopSelf()
                return
            }
            val jitterMs = Random.nextLong(-JITTER_MS, JITTER_MS + 1)
            delay(max(15_000L, TICK_MS + jitterMs))
        }
    }

    private fun captureOnce(): Boolean {
        if (!isMarketSessionActive()) {
            flushPending(force = true)
            return false
        }

        val openTrades = getOpenTradesFromPrefs()
        if (openTrades.length() == 0) {
            flushPending(force = true)
            return false
        }

        val tickTs = isoUtcNow()
        val sessionDate = istSessionDate()
        val trades = (0 until openTrades.length()).mapNotNull { openTrades.optJSONObject(it) }
        val allKeys = trades
            .flatMap { extractLegs(it).mapNotNull { leg -> leg.instrumentKey?.takeIf { key -> key.isNotBlank() } } }
            .distinct()

        val quoteFetch = fetchQuotesWithFallback(allKeys)
        val rows = JSONArray()
        trades.forEach { trade ->
            val row = buildTickRow(trade, sessionDate, tickTs, quoteFetch) ?: return@forEach
            maybeNotifyShadowExit(row, trade)
            rows.put(row)
        }
        enqueueRows(rows)
        flushPending(force = false)
        return true
    }

    private fun getOpenTradesFromPrefs(): JSONArray {
        val raw = prefs.getString(PREF_OPEN_TRADES, "[]") ?: "[]"
        return try {
            val rows = JSONArray(raw)
            val out = JSONArray()
            for (i in 0 until rows.length()) {
                val obj = rows.optJSONObject(i) ?: continue
                val status = obj.optStringAny("status").ifBlank { "OPEN" }
                if (status.isBlank() || status.equals("OPEN", ignoreCase = true)) out.put(obj)
            }
            out
        } catch (e: Exception) {
            Log.e(TAG, "Open trade parse failed: ${e.message}")
            JSONArray()
        }
    }

    private fun buildTickRow(
        trade: JSONObject,
        sessionDate: String,
        tickTs: String,
        quoteFetch: QuoteFetch
    ): JSONObject? {
        val tradeId = trade.optStringAny("id", "")
        val strategyType = trade.optStringAny("strategy_type", "strategyType")
        val isCredit = isCreditTrade(trade, strategyType)
        val entryPremium = trade.optDoubleAny("entry_premium", "entryPremium", "net_premium", "netPremium")
        val maxLoss = trade.optDoubleAny("max_loss", "maxLoss")
        val maxProfit = trade.optDoubleAny("max_profit", "maxProfit")
        val lotMeta = resolvePositionTickLotMeta(trade)
        if (lotMeta == null) {
            val indexKey = trade.optStringAny("index_key", "indexKey", "index")
            val reason = "trade_id=${tradeId.ifBlank { "__missing__" }} index_key=${indexKey.ifBlank { "__missing__" }} valuation_quality=LOT_SIZE_MISSING"
            Log.w(TAG, "POSITION_TICK_SKIPPED: $reason")
            LogBuffer.add('W', TAG, "POSITION_TICK_SKIPPED: $reason")
            return null
        }
        val lotSize = lotMeta.lotSize

        val legs = extractLegs(trade)

        // All quote resolution, structure validation, mark accumulation and P&L now
        // live in valuePositionTick() - a pure function with no Android dependency, so
        // the behaviour can be executed in a unit test rather than asserted about in
        // source text. This method is the adapter: fetch, delegate, log, serialize.
        val valuation = valuePositionTick(
            strategyType = strategyType,
            legs = legs,
            quotes = legs.mapNotNull { leg ->
                leg.instrumentKey?.let { k -> quoteFetch.quotes[k]?.let { k to LegQuote(it.bid, it.ask, it.ltp) } }
            }.toMap(),
            isCredit = isCredit,
            entryPremium = entryPremium,
            maxProfit = maxProfit,
            maxLoss = maxLoss,
            lotSize = lotSize
        )

        if (valuation.structure.status == STRUCTURE_LEGS_MISSING ||
            valuation.structure.status == STRUCTURE_ROLES_INVALID
        ) {
            val reason = "trade_id=${tradeId.ifBlank { "__missing__" }} strategy=$strategyType " +
                "status=${valuation.structure.status} expected=${valuation.structure.expected} " +
                "actual=${valuation.structure.actual} " +
                "problems=${valuation.structure.problems.joinToString(",")}"
            Log.w(TAG, "POSITION_TICK_STRUCTURE_REJECTED: $reason")
            LogBuffer.add('W', TAG, "POSITION_TICK_STRUCTURE_REJECTED: $reason")
        }
        if (valuation.nonPositiveQuoteLegs > 0 || valuation.crossedQuoteLegs > 0) {
            val reason = "trade_id=${tradeId.ifBlank { "__missing__" }} strategy=$strategyType " +
                "non_positive=${valuation.nonPositiveQuoteLegs} crossed=${valuation.crossedQuoteLegs} " +
                "valuation_quality=${valuation.valuationQuality}"
            Log.w(TAG, "POSITION_TICK_QUOTE_REJECTED: $reason")
            LogBuffer.add('W', TAG, "POSITION_TICK_QUOTE_REJECTED: $reason")
        }
        if (valuation.boundAnomaly) {
            val reason = "trade_id=${tradeId.ifBlank { "__missing__" }} strategy=$strategyType " +
                "current_pnl=${valuation.currentPnl} max_profit=$maxProfit max_loss=$maxLoss"
            Log.w(TAG, "POSITION_TICK_BOUND_ANOMALY: $reason")
            LogBuffer.add('W', TAG, "POSITION_TICK_BOUND_ANOMALY: $reason")
        }

        // Observed-extrema contract: every ACCEPTED valuation contributes, without
        // exception. A rejected valuation produces no P&L at all and so cannot reach
        // the extrema. Revision 2 additionally excluded bound anomalies here, which was
        // inconsistent - it trusted the bound check enough to censor research metrics
        // while deliberately not trusting it enough to veto P&L. One rule now.
        val running = updateRunningState(tradeId, valuation.currentPnl)
        val policy = evaluateShadowPolicy(
            tickTs, valuation.currentPnl, maxLoss, maxProfit, valuation.valuationQuality, lotMeta
        )
        // Diagnostics ride in policy_trace_json (jsonb, free-form) rather than new
        // top-level columns, so this ships without a position_ticks migration.
        policy.trace.apply {
            put("position_tick_guards_version", POSITION_TICK_GUARDS_VERSION)
            put("structure_contract", STRUCTURE_CONTRACT)
            put("quote_contract", QUOTE_CONTRACT)
            put("extrema_contract", EXTREMA_CONTRACT)
            put("structure_status", valuation.structure.status)
            put("expected_leg_count", valuation.structure.expected ?: JSONObject.NULL)
            put("actual_leg_count", valuation.structure.actual)
            put("structure_problems", JSONArray(valuation.structure.problems))
            put("valuation_accepted", valuation.valuationAccepted)
            put("bound_anomaly", valuation.boundAnomaly)
            put("non_positive_quote_legs", valuation.nonPositiveQuoteLegs)
            put("crossed_quote_legs", valuation.crossedQuoteLegs)
            put("raw_mark_complete", valuation.rawMarkComplete)
            put("running_state_updated", valuation.currentPnl != null)
            putOptNumber("raw_executable_mark", valuation.rawExecutableMark)
            putOptNumber("max_profit_ref", maxProfit)
            putOptNumber("max_loss_ref", maxLoss)
        }

        return JSONObject().apply {
            put("trade_id", tradeId)
            put("session_date", sessionDate)
            put("tick_ts", tickTs)
            put("source", SOURCE)
            put("auth_source", quoteFetch.authSource)
            put("index_key", trade.optStringAny("index_key", "indexKey"))
            put("strategy_type", strategyType)
            put("status", trade.optStringAny("status").ifBlank { "OPEN" })
            put("leg_count", legs.size)
            put("valuation_quality", valuation.valuationQuality)
            put("mark_basis", if (valuation.executableMark != null) "EXECUTABLE" else "NONE")
            putOptNumber("executable_mark", valuation.executableMark)
            putOptNumber("mid_mark", valuation.midMark)
            putOptNumber("ltp_mark", valuation.ltpMark)
            putOptNumber("current_pnl", valuation.currentPnl)
            putOptNumber("current_pnl_r", valuation.currentPnlR)
            putOptNumber("running_mae", running.mae)
            putOptNumber("running_mfe", running.mfe)
            put("policy_action", policy.action)
            put("policy_reason", policy.reason)
            put("policy_trace_json", policy.trace)
            put("legs_json", valuation.legsJson())
        }
    }

    private fun evaluateShadowPolicy(
        tickTs: String,
        currentPnl: Double?,
        maxLoss: Double?,
        maxProfit: Double?,
        valuationQuality: String,
        lotMeta: PositionTickLotMeta
    ): PolicyDecision {
        val slThreshold = maxLoss?.let { -PositionPolicyV1.SL_MULT * it }
        val tpThreshold = maxProfit?.let { PositionPolicyV1.TP_MULT * it }
        val eod = isAtOrAfterPolicyEod()
        val action = when {
            currentPnl != null && slThreshold != null && currentPnl <= slThreshold -> "SHADOW_SL"
            currentPnl != null && tpThreshold != null && currentPnl >= tpThreshold -> "SHADOW_TP"
            eod -> "SHADOW_EOD"
            valuationQuality != "OK" -> "SHADOW_DEGRADED"
            else -> "HOLD"
        }
        val reason = when (action) {
            "SHADOW_SL" -> "current_pnl <= -${PositionPolicyV1.SL_MULT} * max_loss"
            "SHADOW_TP" -> "current_pnl >= ${PositionPolicyV1.TP_MULT} * max_profit"
            "SHADOW_EOD" -> "tick_ts >= ${PositionPolicyV1.EOD_HH_MM} IST"
            "SHADOW_DEGRADED" -> "valuation_quality=$valuationQuality"
            else -> "no shadow exit rule matched"
        }
        return PolicyDecision(
            action = action,
            reason = reason,
            trace = JSONObject().apply {
                put("policy_version", PositionPolicyV1.VERSION)
                put("tick_ts", tickTs)
                putOptNumber("current_pnl", currentPnl)
                putOptNumber("sl_threshold", slThreshold)
                putOptNumber("tp_threshold", tpThreshold)
                put("eod_hh_mm", PositionPolicyV1.EOD_HH_MM)
                put("is_eod", eod)
                put("valuation_quality", valuationQuality)
                putOptNumber("lot_size_resolved", lotMeta.lotSize)
                put("lot_size_assumed", lotMeta.assumed)
                put("lot_size_source", lotMeta.source)
            }
        )
    }

    private fun updateRunningState(tradeId: String, currentPnl: Double?): RunningState {
        if (tradeId.isBlank()) return RunningState(currentPnl, currentPnl)
        val all = try {
            JSONObject(prefs.getString(PREF_RUNNING_STATE, "{}") ?: "{}")
        } catch (_: Exception) {
            JSONObject()
        }
        val prev = all.optJSONObject(tradeId) ?: JSONObject()
        val prevMae = prev.optNullableDouble("mae")
        val prevMfe = prev.optNullableDouble("mfe")
        val mae = when {
            currentPnl == null -> prevMae
            prevMae == null -> currentPnl
            else -> min(prevMae, currentPnl)
        }
        val mfe = when {
            currentPnl == null -> prevMfe
            prevMfe == null -> currentPnl
            else -> max(prevMfe, currentPnl)
        }
        all.put(tradeId, JSONObject().apply {
            putOptNumber("mae", mae)
            putOptNumber("mfe", mfe)
            put("updated_at", isoUtcNow())
        })
        prefs.edit().putString(PREF_RUNNING_STATE, all.toString()).apply()
        return RunningState(mae, mfe)
    }

    private fun extractLegs(trade: JSONObject): List<PositionLeg> {
        val legs = mutableListOf<PositionLeg>()
        addLeg(legs, trade, "sell", "SHORT", CloseSide.BUY_TO_CLOSE)
        addLeg(legs, trade, "buy", "LONG", CloseSide.SELL_TO_CLOSE)
        addLeg(legs, trade, "sell2", "SHORT", CloseSide.BUY_TO_CLOSE)
        addLeg(legs, trade, "buy2", "LONG", CloseSide.SELL_TO_CLOSE)
        return legs
    }

    private fun addLeg(
        legs: MutableList<PositionLeg>,
        trade: JSONObject,
        prefix: String,
        side: String,
        closeSide: CloseSide
    ) {
        // PWA trades record a second leg as sell_instrument_key2/sellStrike2,
        // whereas an earlier tracker draft expected sell2_instrument_key/sell2Strike.
        // Accept both forms so an IC/IB is always marked with all four legs.
        val keyNames = when (prefix) {
            "sell2" -> arrayOf("sell_instrument_key2", "sellInstrumentKey2", "sell2_instrument_key", "sell2InstrumentKey")
            "buy2" -> arrayOf("buy_instrument_key2", "buyInstrumentKey2", "buy2_instrument_key", "buy2InstrumentKey")
            else -> arrayOf("${prefix}_instrument_key", "${prefix}InstrumentKey")
        }
        val strikeNames = when (prefix) {
            "sell2" -> arrayOf("sell_strike2", "sellStrike2", "sell2_strike", "sell2Strike")
            "buy2" -> arrayOf("buy_strike2", "buyStrike2", "buy2_strike", "buy2Strike")
            else -> arrayOf("${prefix}_strike", "${prefix}Strike")
        }
        val typeNames = when (prefix) {
            "sell2" -> arrayOf("sell_type2", "sellType2", "sell2_type", "sell2Type", "sell_option_type2", "sellOptionType2", "sell2_option_type", "sell2OptionType")
            "buy2" -> arrayOf("buy_type2", "buyType2", "buy2_type", "buy2Type", "buy_option_type2", "buyOptionType2", "buy2_option_type", "buy2OptionType")
            else -> arrayOf("${prefix}_type", "${prefix}Type", "${prefix}_option_type", "${prefix}OptionType")
        }
        val key = trade.optStringAny(*keyNames)
        val strike = trade.optDoubleAny(*strikeNames)
        val type = trade.optStringAny(*typeNames)
        if (key.isBlank() && strike == null && type.isBlank()) return
        legs.add(PositionLeg(
            instrumentKey = key.ifBlank { null },
            side = side,
            closeSide = closeSide,
            optionType = type.ifBlank { null },
            strike = strike
        ))
    }

    private fun fetchQuotesWithFallback(keys: List<String>): QuoteFetch {
        if (keys.isEmpty()) return QuoteFetch("NONE", emptyMap())
        val analyticsEnabled = prefs.getBoolean(PREF_ANALYTICS_ENABLED, false)
        val analyticsToken = listOf(PREF_ANALYTICS_TOKEN, PREF_ANALYTICS_TOKEN_ALT)
            .firstNotNullOfOrNull { prefs.getString(it, null)?.takeIf { token -> token.isNotBlank() } }
        if (analyticsEnabled && analyticsToken != null) {
            val analyticsQuotes = fetchQuotes(keys, analyticsToken)
            if (analyticsQuotes != null) return QuoteFetch("ANALYTICS", analyticsQuotes)
            LogBuffer.add('W', TAG, "Analytics token quote fetch failed; falling back to daily token")
        }
        val dailyToken = prefs.getString(PREF_DAILY_TOKEN, null)?.takeIf { it.isNotBlank() }
            ?: return QuoteFetch("NONE", emptyMap())
        val dailyQuotes = fetchQuotes(keys, dailyToken)
        return QuoteFetch("DAILY", dailyQuotes ?: emptyMap())
    }

    private fun fetchQuotes(keys: List<String>, token: String): Map<String, Quote>? {
        val url = "https://api.upstox.com/v2/market-quote/quotes?instrument_key=${keys.joinToString(",")}"
        val request = Request.Builder()
            .url(url)
            .addHeader("Authorization", "Bearer $token")
            .addHeader("Accept", "application/json")
            .get()
            .build()
        return try {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    Log.e(TAG, "Quote fetch failed: ${response.code} ${response.message}")
                    return null
                }
                parseQuotes(response.body?.string() ?: "{}", keys)
            }
        } catch (e: Exception) {
            Log.e(TAG, "Quote fetch exception: ${e.message}")
            null
        }
    }

    private fun parseQuotes(raw: String, requestedKeys: List<String>): Map<String, Quote> {
        val out = mutableMapOf<String, Quote>()
        val data = try {
            JSONObject(raw).optJSONObject("data") ?: JSONObject(raw)
        } catch (_: Exception) {
            JSONObject()
        }
        data.keys().forEach { responseKey ->
            val quoteObj = data.optJSONObject(responseKey) ?: return@forEach
            val instrumentKey = quoteObj.optStringAny("instrument_key", "instrument_token").ifBlank { responseKey }
            val quote = Quote(
                bid = quoteObj.bestDepthPrice("buy") ?: quoteObj.optDoubleAny("best_bid_price", "bid_price", "bid"),
                ask = quoteObj.bestDepthPrice("sell") ?: quoteObj.optDoubleAny("best_ask_price", "ask_price", "ask"),
                ltp = quoteObj.optDoubleAny("last_price", "ltp", "last_traded_price")
            )
            out[instrumentKey] = quote
            out[responseKey] = quote
        }
        requestedKeys.forEach { key ->
            if (!out.containsKey(key)) {
                out.entries.firstOrNull { it.key.endsWith(key) || key.endsWith(it.key) }?.let { out[key] = it.value }
            }
        }
        return out
    }

    private fun enqueueRows(rows: JSONArray) {
        val queue = loadPendingQueue()
        for (i in 0 until rows.length()) queue.put(rows.optJSONObject(i))
        recordDroppedTicks(trimQueue(queue))
        prefs.edit().putString(PREF_PENDING_QUEUE, queue.toString()).apply()
    }

    private fun flushPending(force: Boolean) {
        val now = System.currentTimeMillis()
        val lastFlush = prefs.getLong(PREF_LAST_FLUSH_MS, 0L)
        if (!force && now - lastFlush < FLUSH_MIN_MS) return
        val queue = loadPendingQueue()
        if (queue.length() == 0) {
            prefs.edit().putLong(PREF_LAST_FLUSH_MS, now).apply()
            return
        }
        val dropped = trimQueue(queue)
        if (dropped > 0) {
            recordDroppedTicks(dropped)
            prefs.edit().putString(PREF_PENDING_QUEUE, queue.toString()).apply()
        }
        val ok = SupabaseClient.insertPositionTicks(queue)
        if (ok) {
            prefs.edit()
                .putString(PREF_PENDING_QUEUE, "[]")
                .putLong(PREF_LAST_FLUSH_MS, now)
                .putInt(PREF_FLUSH_FAILURE_COUNT, 0)
                .apply()
        } else {
            val failures = prefs.getInt(PREF_FLUSH_FAILURE_COUNT, 0) + 1
            Log.w(TAG, "Position tick flush failed; consecutive_failures=$failures pending_rows=${queue.length()}")
            LogBuffer.add('W', TAG, "POSITION_TICK_FLUSH_FAIL: consecutive=$failures pending=${queue.length()}")
            prefs.edit()
                .putLong(PREF_LAST_FLUSH_MS, now)
                .putInt(PREF_FLUSH_FAILURE_COUNT, failures)
                .apply()
        }
    }

    private fun trimQueue(queue: JSONArray): Int {
        var dropped = 0
        while (queue.length() > MAX_PENDING_TICKS) {
            queue.remove(0)
            dropped += 1
        }
        return dropped
    }

    private fun recordDroppedTicks(dropped: Int) {
        if (dropped <= 0) return
        val total = prefs.getLong(PREF_DROPPED_TICK_COUNT, 0L) + dropped
        prefs.edit().putLong(PREF_DROPPED_TICK_COUNT, total).apply()
        Log.w(TAG, "Dropped $dropped old position tick rows from bounded queue; total_dropped=$total")
        LogBuffer.add('W', TAG, "POSITION_TICK_QUEUE_DROP: dropped=$dropped total=$total")
    }

    private fun loadPendingQueue(): JSONArray {
        return try {
            JSONArray(prefs.getString(PREF_PENDING_QUEUE, "[]") ?: "[]")
        } catch (_: Exception) {
            JSONArray()
        }
    }

    private fun isMarketSessionActive(): Boolean {
        val cal = Calendar.getInstance(IST)
        val minutes = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE)
        return minutes in MARKET_OPEN_MINUTES..MARKET_CLOSE_MINUTES
    }

    private fun isAtOrAfterPolicyEod(): Boolean {
        val cal = Calendar.getInstance(IST)
        val minutes = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE)
        return minutes >= POLICY_EOD_MINUTES
    }

    private fun istSessionDate(): String = IST_DATE_FORMAT.get().format(Date())

    private fun isoUtcNow(): String = UTC_DATE_FORMAT.get().format(Date())

    /**
     * Live shadow-exit alerts. Dual path with brain.py position alerts for now —
     * notify only on (tradeId, shadowAction) transitions, with a short cooldown
     * against rapid flapping between recommendations.
     */
    private fun maybeNotifyShadowExit(row: JSONObject, trade: JSONObject) {
        val tradeId = row.optString("trade_id", "").ifBlank { trade.optStringAny("id") }
        if (tradeId.isBlank()) return
        val action = row.optString("policy_action", "HOLD")
        val lastActionKey = "shadow_last_action_$tradeId"
        val lastNotifyMsKey = "shadow_last_notify_ms_$tradeId"
        if (!action.startsWith("SHADOW_")) {
            if (prefs.contains(lastActionKey)) {
                prefs.edit().remove(lastActionKey).apply()
            }
            return
        }

        val now = System.currentTimeMillis()
        val lastAction = prefs.getString(lastActionKey, "") ?: ""
        val lastNotifyMs = prefs.getLong(lastNotifyMsKey, 0L)
        if (action == lastAction) return
        if (lastAction.isNotEmpty() && now - lastNotifyMs < SHADOW_NOTIFY_COOLDOWN_MS) {
            LogBuffer.add(
                'I',
                TAG,
                "SHADOW_EXIT_NOTIFY_THROTTLED: trade=$tradeId action=$action prev=$lastAction"
            )
            return
        }

        val indexKey = row.optString("index_key", trade.optStringAny("index_key", "indexKey", "index"))
        val strategy = row.optString("strategy_type", trade.optStringAny("strategy_type", "strategyType"))
        val pnl = if (row.isNull("current_pnl")) Double.NaN else row.optDouble("current_pnl", Double.NaN)
        val pnlText = if (pnl.isNaN()) "P&L n/a" else "P&L ₹${"%,.0f".format(kotlin.math.round(pnl))}"
        val label = listOf(indexKey, strategy).filter { it.isNotBlank() }.joinToString(" ").ifBlank { tradeId }

        val (title, body, notifType) = when (action) {
            "SHADOW_SL" -> Triple(
                "🛑 Stop Loss Near",
                "$label · $pnlText · Cut position.",
                "urgent"
            )
            "SHADOW_TP" -> Triple(
                "💰 Target Near",
                "$label · $pnlText · Book profit.",
                "urgent"
            )
            "SHADOW_EOD" -> Triple(
                "⏰ Exit — EOD",
                "$label · $pnlText · Square off before close.",
                "urgent"
            )
            "SHADOW_DEGRADED" -> Triple(
                "🧪 Position Data Incomplete",
                "$label · valuation degraded · review marks before trusting P&L.",
                "warning"
            )
            else -> return
        }

        val delivery = NotificationHelper.send(this, title, body, notifType, "positions")
        prefs.edit()
            .putString(lastActionKey, action)
            .putLong(lastNotifyMsKey, now)
            .apply()
        Log.i(TAG, "SHADOW_EXIT_NOTIFY: trade=$tradeId action=$action outcome=${delivery.outcome}")
        LogBuffer.add('I', TAG, "SHADOW_EXIT_NOTIFY: trade=$tradeId action=$action outcome=${delivery.outcome}")
    }

    private fun buildNotification() =
        NotificationCompat.Builder(this, NOTIFICATION_CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle("Market Radar position capture")
            .setContentText("Shadow position ticks active")
            .setOngoing(true)
            .setSilent(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .also {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    val manager = getSystemService(NotificationManager::class.java)
                    manager.createNotificationChannel(
                        NotificationChannel(
                            NOTIFICATION_CHANNEL_ID,
                            "Position capture",
                            NotificationManager.IMPORTANCE_LOW
                        )
                    )
                }
            }
            .build()

    private fun isCreditTrade(trade: JSONObject, strategyType: String): Boolean {
        if (trade.has("is_credit")) return trade.optBoolean("is_credit", false)
        if (trade.has("isCredit")) return trade.optBoolean("isCredit", false)
        val normalized = strategyType.uppercase(Locale.US)
        return normalized == "BULL_PUT" ||
            normalized == "BEAR_CALL" ||
            normalized == "IRON_CONDOR" ||
            normalized == "SELL_PREMIUM"
    }

    /* PositionLeg / CloseSide live at file level (bottom of this file) so the
       structure validator that consumes them can be unit-tested. */

    private data class Quote(val bid: Double?, val ask: Double?, val ltp: Double?)

    private data class QuoteFetch(val authSource: String, val quotes: Map<String, Quote>)

    private data class RunningState(val mae: Double?, val mfe: Double?)

    private data class PolicyDecision(val action: String, val reason: String, val trace: JSONObject)

    companion object {
        private const val TAG = "PositionTickService"
        private const val PREFS_NAME = "market_radar"
        private const val PREF_OPEN_TRADES = "open_trades"
        private const val PREF_DAILY_TOKEN = "auth_token"
        private const val PREF_ANALYTICS_ENABLED = "position_tick_analytics_enabled"
        private const val PREF_ANALYTICS_TOKEN = "upstox_analytics_token"
        private const val PREF_ANALYTICS_TOKEN_ALT = "analytics_token"
        private const val PREF_PENDING_QUEUE = "position_tick_pending_queue"
        private const val PREF_RUNNING_STATE = "position_tick_running_state"
        private const val PREF_LAST_FLUSH_MS = "position_tick_last_flush_ms"
        private const val PREF_DROPPED_TICK_COUNT = "position_tick_dropped_count"
        private const val PREF_FLUSH_FAILURE_COUNT = "position_tick_flush_failure_count"
        private const val PREF_FGS_BLOCKED_UNTIL_MS = "position_tick_fgs_blocked_until_ms"
        private const val PREF_FGS_BLOCKED_COUNT = "position_tick_fgs_blocked_count"
        private const val NOTIFICATION_CHANNEL_ID = "position_tick_capture"
        private const val NOTIFICATION_ID = 23018
        private const val SOURCE = "P1_REST_60S"
        /**
         * Bumped whenever the valuation guards change, so a production row proves which
         * logic wrote it. v2 validates leg roles/uniqueness (not just count), rejects a
         * zero executable price contradicted by a positive LTP, and records bound
         * breaches as anomalies rather than nulling P&L. v3 extracts valuation into the
         * pure, directly-testable valuePositionTick(); adds explicit crossed-quote
         * detection; makes raw_executable_mark all-or-nothing (never a partial sum);
         * stops excluding bound anomalies from running MAE/MFE (EXTREMA_CONTRACT: every
         * accepted valuation contributes); and reports an UNSUPPORTED strategy as
         * STRUCTURE_UNCHECKED rather than letting it reach valuation_quality=OK. v4
         * restores the complete bid/ask requirement for accepted marks and requires
         * every strategy leg before raw_mark_complete can be true.
         */
        internal const val POSITION_TICK_GUARDS_VERSION = "position_tick_guards_v4_complete_book_position"
        private const val TICK_MS = 60_000L
        /** Suppress repeat shadow alerts for the same (trade, action); transitions always notify. */
        private const val SHADOW_NOTIFY_COOLDOWN_MS = 10 * 60 * 1000L
        private const val JITTER_MS = 5_000L
        private const val FLUSH_MIN_MS = 60_000L
        private const val MAX_PENDING_TICKS = 1_500
        private const val FGS_BLOCKED_BASE_BACKOFF_MS = 10 * 60 * 1000L
        private const val FGS_BLOCKED_MAX_BACKOFF_MS = 30 * 60 * 1000L
        private const val MARKET_OPEN_MINUTES = 9 * 60 + 15
        private const val MARKET_CLOSE_MINUTES = 15 * 60 + 40
        private const val POLICY_EOD_MINUTES = 15 * 60 + 15
        private val IST = TimeZone.getTimeZone("Asia/Kolkata")
        private val UTC = TimeZone.getTimeZone("UTC")
        private val IST_DATE_FORMAT = ThreadLocal.withInitial {
            SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = IST }
        }
        private val UTC_DATE_FORMAT = ThreadLocal.withInitial {
            SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.US).apply { timeZone = UTC }
        }

        fun ensureRunning(context: Context) {
            val prefs = context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            val open = try {
                JSONArray(prefs.getString(PREF_OPEN_TRADES, "[]") ?: "[]")
            } catch (_: Exception) {
                JSONArray()
            }
            if (open.length() == 0 || !marketSessionActiveNow()) return
            val blockedUntilMs = prefs.getLong(PREF_FGS_BLOCKED_UNTIL_MS, 0L)
            val remainingMs = (blockedUntilMs - System.currentTimeMillis()).coerceAtLeast(0L)
            if (remainingMs > 0L) {
                LogBuffer.add('W', TAG, "POSITION_TICK_START_BACKOFF_ACTIVE: remainingMs=$remainingMs")
                return
            }
            val intent = Intent(context.applicationContext, PositionTickService::class.java)
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    context.applicationContext.startForegroundService(intent)
                } else {
                    context.applicationContext.startService(intent)
                }
            } catch (t: Throwable) {
                Log.e(TAG, "POSITION_TICK_START_BLOCKED: ${t.javaClass.simpleName}: ${t.message}")
                LogBuffer.add('E', TAG, "POSITION_TICK_START_BLOCKED: ${t.javaClass.simpleName}: ${t.message}")
            }
        }

        private fun marketSessionActiveNow(): Boolean {
            val cal = Calendar.getInstance(IST)
            val minutes = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE)
            return minutes in MARKET_OPEN_MINUTES..MARKET_CLOSE_MINUTES
        }
    }
}

private fun JSONObject.optStringAny(vararg names: String, default: String = ""): String {
    names.forEach { name ->
        if (has(name) && !isNull(name)) {
            val value = optString(name, "")
            if (value.isNotBlank() && value != "null") return value
        }
    }
    return default
}

private fun JSONObject.optJSONObjectAny(vararg names: String): JSONObject? {
    names.forEach { name ->
        if (!has(name) || isNull(name)) return@forEach
        when (val raw = opt(name)) {
            is JSONObject -> return raw
            is String -> {
                val parsed = runCatching { JSONObject(raw) }.getOrNull()
                if (parsed != null) return parsed
            }
        }
    }
    return null
}

private fun JSONObject.optDoubleAny(vararg names: String): Double? {
    names.forEach { name ->
        if (has(name) && !isNull(name)) {
            val raw = opt(name)
            val value = when (raw) {
                is Number -> raw.toDouble()
                is String -> raw.trim().toDoubleOrNull()
                else -> null
            }
            if (value != null && value.isFinite()) return value
        }
    }
    return null
}

private fun JSONObject.optNullableDouble(name: String): Double? {
    if (!has(name) || isNull(name)) return null
    val raw = opt(name)
    return when (raw) {
        is Number -> raw.toDouble()
        is String -> raw.trim().toDoubleOrNull()
        else -> null
    }?.takeIf { it.isFinite() }
}

private fun JSONObject.putOptNumber(name: String, value: Double?) {
    if (value == null || !value.isFinite()) put(name, JSONObject.NULL) else put(name, value)
}

private fun JSONObject.bestDepthPrice(side: String): Double? {
    val depth = optJSONObject("depth") ?: return null
    val arr = depth.optJSONArray(side) ?: return null
    val first = arr.optJSONObject(0) ?: return null
    return first.optDoubleAny("price")
}

internal data class PositionTickLotMeta(
    val lotSize: Double,
    val assumed: Boolean,
    val source: String
)

internal fun resolvePositionTickLotMeta(trade: JSONObject): PositionTickLotMeta? {
    // Keep this contract pinned to brain.py:3283-3299; do not introduce a third lot-size rule.
    val indexKey = trade.optStringAny("index_key", "indexKey", "index").uppercase(Locale.US)
    val baseLot = when (indexKey) {
        "BNF" -> 30.0
        "NF" -> 65.0
        else -> 0.0
    }
    val entrySnapshot = trade.optJSONObjectAny("entry_snapshot", "entrySnapshot") ?: JSONObject()
    val tradeLot = trade.optDoubleAny("lot_size", "lotSize")
    val entrySnapshotLot = entrySnapshot.optDoubleAny("lot_size", "lotSize")
    val explicitLotSize = tradeLot ?: entrySnapshotLot ?: 0.0
    val lotsCount = max(trade.optDoubleAny("lots") ?: 1.0, 1.0)
    val lotSize = if (explicitLotSize > 0.0) explicitLotSize else baseLot * lotsCount
    if (lotSize <= 0.0) return null
    val source = when {
        (tradeLot ?: 0.0) > 0.0 -> "trade"
        (entrySnapshotLot ?: 0.0) > 0.0 -> "entry_snapshot"
        else -> "contract_default"
    }
    return PositionTickLotMeta(
        lotSize = lotSize,
        assumed = explicitLotSize <= 0.0,
        source = source
    )
}

internal data class PositionLeg(
    val instrumentKey: String?,
    val side: String,
    val closeSide: CloseSide,
    val optionType: String?,
    val strike: Double?
)

internal enum class CloseSide { BUY_TO_CLOSE, SELL_TO_CLOSE }

internal fun computePositionTickCurrentPnl(
    entryPremium: Double,
    executableMarkValue: Double,
    isCredit: Boolean,
    lotSize: Double
): Double {
    return if (isCredit) {
        (entryPremium - executableMarkValue) * lotSize
    } else {
        (executableMarkValue - entryPremium) * lotSize
    }
}

// ---------------------------------------------------------------------------
// Published contracts. These strings are written into every tick's policy trace
// so a downstream reader knows exactly what was and was not asserted, without
// having to infer it from a status name.
// ---------------------------------------------------------------------------

/**
 * What structure validation does and does NOT cover.
 *
 * It asserts leg COUNT, leg ROLES (side + option type multiset) and instrument-key
 * UNIQUENESS. It does NOT validate strike geometry, expiry alignment, quantities, or
 * the relationship between the two short strikes - so it cannot distinguish an iron
 * butterfly from an iron condor, and `ROLES_OK` must never be read as "economically
 * valid structure".
 */
internal const val STRUCTURE_CONTRACT =
    "roles_count_keys_only_no_strike_or_expiry_geometry"

/**
 * What counts as an executable price.
 *
 * Requires finite bid and ask, a non-crossed book, and a strictly positive finite
 * side that must be traded to close - ask for a short leg, bid for a long leg -
 * independent of LTP. This matches the Python teacher contract
 * (`net_economics_v2_executable_quote_contract`); the Kotlin tick path did not
 * previously enforce it.
 *
 * Zero is not accepted as "worthless". Across 47,338 persisted legs, every one of the
 * four zero executable prices carried a positive LTP (83.05, 404.15 x2, 1010.0) and
 * none carried a zero LTP, so no genuinely worthless leg has ever been observed and
 * the strict rule costs nothing measurable. If one appears it degrades that tick
 * rather than pricing the leg at nothing - the conservative direction.
 */
internal const val QUOTE_CONTRACT =
    "finite_two_sided_non_crossed_book_plus_strictly_positive_executable_side_independent_of_ltp"

/**
 * Which marks contribute to running MAE/MFE.
 *
 * Observed extrema: every ACCEPTED valuation contributes, with no further filtering.
 * A rejected valuation yields no P&L and therefore cannot reach the extrema. Bound
 * anomalies are recorded but do NOT censor the extrema - censoring there while
 * deliberately not vetoing P&L would be two different levels of trust in one check.
 */
internal const val EXTREMA_CONTRACT = "observed_every_accepted_valuation_no_exclusions"

internal const val STRUCTURE_ROLES_OK = "ROLES_OK"
internal const val STRUCTURE_LEGS_MISSING = "LEGS_MISSING"
internal const val STRUCTURE_ROLES_INVALID = "ROLES_INVALID"
internal const val STRUCTURE_UNSUPPORTED = "UNSUPPORTED"

/**
 * Outcome of validating a resolved leg set against its declared strategy.
 *
 * [status] is one of:
 *  - [STRUCTURE_ROLES_OK]       - count, roles and key uniqueness all matched.
 *                                 NOT a statement about strike or expiry geometry.
 *  - [STRUCTURE_LEGS_MISSING]   - supported strategy, fewer legs than required
 *  - [STRUCTURE_ROLES_INVALID]  - wrong roles, duplicate instruments, or extra legs
 *  - [STRUCTURE_UNSUPPORTED]    - strategy not in the supported set; nothing asserted
 *
 * The names deliberately avoid "COMPLETE"/"VALID" so that no reader can mistake a
 * role check for an economic one. See [STRUCTURE_CONTRACT].
 */
internal data class StructureCheck(
    val status: String,
    val expected: Int?,
    val actual: Int,
    val problems: List<String>
)

/** A raw quote for one leg, as returned by the market-data fetch. */
internal data class LegQuote(val bid: Double?, val ask: Double?, val ltp: Double?)

/** Per-leg valuation outcome, serialized into `legs_json`. */
internal data class LegValuation(
    val leg: PositionLeg,
    val bid: Double?,
    val ask: Double?,
    val ltp: Double?,
    val mid: Double?,
    val executablePrice: Double?,
    val priceBasis: String,
    val quoteStatus: String
)

/**
 * Complete outcome of valuing one position tick.
 *
 * Accepted vs raw is explicit throughout: [executableMark], [currentPnl] and
 * [currentPnlR] are null unless [valuationAccepted]; [rawExecutableMark] is a
 * diagnostic that is null unless [rawMarkComplete].
 */
internal data class TickValuation(
    val legValuations: List<LegValuation>,
    val structure: StructureCheck,
    val valuationQuality: String,
    val valuationAccepted: Boolean,
    val executableMark: Double?,
    val midMark: Double?,
    val ltpMark: Double?,
    val rawExecutableMark: Double?,
    val rawMarkComplete: Boolean,
    val currentPnl: Double?,
    val currentPnlR: Double?,
    val boundAnomaly: Boolean,
    val nonPositiveQuoteLegs: Int,
    val crossedQuoteLegs: Int
) {
    fun legsJson(): JSONArray {
        val out = JSONArray()
        legValuations.forEach { lv ->
            out.put(JSONObject().apply {
                put("instrument_key", lv.leg.instrumentKey ?: JSONObject.NULL)
                put("side", lv.leg.side)
                put("option_type", lv.leg.optionType ?: JSONObject.NULL)
                put("strike", lv.leg.strike ?: JSONObject.NULL)
                putOptNumber("bid", lv.bid)
                putOptNumber("ask", lv.ask)
                putOptNumber("ltp", lv.ltp)
                putOptNumber("mid", lv.mid)
                putOptNumber("executable_price", lv.executablePrice)
                put("price_basis", lv.priceBasis)
                put("quote_status", lv.quoteStatus)
            })
        }
        return out
    }
}

private fun Double?.usable(): Boolean = this != null && this.isFinite() && this > 0.0

/** Sign a leg contributes to the close-out mark (BUY_TO_CLOSE vs SELL_TO_CLOSE, credit vs debit). */
private fun legMarkSign(leg: PositionLeg, isCredit: Boolean): Double {
    val closing = leg.closeSide == CloseSide.BUY_TO_CLOSE
    return if (isCredit) {
        if (closing) 1.0 else -1.0
    } else {
        if (closing) -1.0 else 1.0
    }
}

/**
 * Values one position tick. Pure: no Android, no prefs, no clock, no I/O.
 *
 * This is the function that decides `valuation_quality`, which marks are published,
 * and whether a P&L exists. It exists as a separate pure function specifically so
 * that tests can execute the real decision path instead of asserting that source
 * strings are present - a distinction that matters, because a test suite asserting on
 * source text passes unchanged when the behaviour it describes is deleted.
 */
internal fun valuePositionTick(
    strategyType: String,
    legs: List<PositionLeg>,
    quotes: Map<String, LegQuote>,
    isCredit: Boolean,
    entryPremium: Double?,
    maxProfit: Double?,
    maxLoss: Double?,
    lotSize: Double,
    boundTolerance: Double = STRUCTURAL_BOUND_TOLERANCE_VALUE
): TickValuation {
    val structure = validateStructure(strategyType, legs)

    var anyQuote = false
    var hasMissingKey = false
    var hasMissingExecutableSide = false
    var nonPositive = 0
    var crossed = 0
    var executableMark = 0.0
    var rawExecutableMark = 0.0
    var midMark = 0.0
    var ltpMark = 0.0
    var midComplete = legs.isNotEmpty()
    var ltpComplete = legs.isNotEmpty()
    var rawMarkComplete = legs.isNotEmpty()

    val legValuations = legs.map { leg ->
        val q = leg.instrumentKey?.let { quotes[it] }
        val bid = q?.bid?.takeIf { it.isFinite() }
        val ask = q?.ask?.takeIf { it.isFinite() }
        val ltp = q?.ltp?.takeIf { it.isFinite() }
        val isCrossed = bid != null && ask != null && bid > ask
        val mid = if (bid != null && ask != null && !isCrossed) (bid + ask) / 2.0 else null
        val executableRaw = if (leg.closeSide == CloseSide.BUY_TO_CLOSE) ask else bid
        // QUOTE_CONTRACT: finite two-sided non-crossed book plus a strictly
        // positive executable close side, independent of LTP.
        val executablePrice = if (executableRaw.usable() && !isCrossed) executableRaw else null

        val status = when {
            leg.instrumentKey.isNullOrBlank() -> "KEY_MISSING"
            q == null || (bid == null && ask == null && ltp == null) -> "NO_QUOTE"
            isCrossed -> "CROSSED_QUOTE"
            bid == null || ask == null -> "NO_DEPTH"
            !executableRaw.usable() -> "NON_POSITIVE_QUOTE"
            else -> "OK"
        }
        val priceBasis = when {
            executablePrice != null -> "EXECUTABLE"
            ltp != null -> "LTP"
            else -> "NONE"
        }

        if (status == "KEY_MISSING") hasMissingKey = true
        if (status == "NON_POSITIVE_QUOTE") nonPositive += 1
        if (isCrossed) crossed += 1
        if (bid != null || ask != null || ltp != null) anyQuote = true
        // A complete two-sided book is required for an accepted tick. The closing
        // side alone is insufficient: without both sides a reader cannot validate
        // the book or detect a crossed quote. Keep any raw side as diagnostics only.
        if (bid == null || ask == null || executablePrice == null) hasMissingExecutableSide = true

        val sign = legMarkSign(leg, isCredit)
        if (executablePrice != null) executableMark += sign * executablePrice
        // rawExecutableMark is a DIAGNOSTIC of what the venue actually quoted, so it
        // uses the unfiltered side - but it is only published when every leg supplied
        // one, never as a partial sum (see rawMarkComplete).
        if (executableRaw != null) rawExecutableMark += sign * executableRaw else rawMarkComplete = false
        if (mid != null) midMark += sign * mid else midComplete = false
        if (ltp != null) ltpMark += sign * ltp else ltpComplete = false

        LegValuation(leg, bid, ask, ltp, mid, executablePrice, priceBasis, status)
    }

    val valuationQuality = when {
        structure.status == STRUCTURE_LEGS_MISSING -> "STRUCTURE_INCOMPLETE"
        structure.status == STRUCTURE_ROLES_INVALID -> "STRUCTURE_MALFORMED"
        // An UNSUPPORTED strategy was never role-checked at all - not "checked and
        // fine". Reported distinctly so "OK" is never reached without validateStructure()
        // actually having asserted the leg roles. Found while extracting this function;
        // not something revision 2 or Codex's review raised, and it costs nothing
        // measurable - every strategy_type ever persisted (IRON_BUTTERFLY, BEAR_CALL,
        // BEAR_PUT, IRON_CONDOR, BULL_PUT) is in STRUCTURE_SPEC.
        structure.status == STRUCTURE_UNSUPPORTED -> "STRUCTURE_UNCHECKED"
        !anyQuote -> "UNAVAILABLE"
        hasMissingKey || hasMissingExecutableSide -> "DEGRADED"
        else -> "OK"
    }
    val valuationAccepted = valuationQuality == "OK" && legs.isNotEmpty()
    val structureUsable =
        structure.status == STRUCTURE_ROLES_OK || structure.status == STRUCTURE_UNSUPPORTED

    val executableMarkValue = if (valuationAccepted) executableMark else null
    // Diagnostic marks. Published whenever they are internally complete and the
    // structure is not rejected; they are NOT executable valuations and must never be
    // substituted for executable_mark by a consumer.
    val midMarkValue = if (structureUsable && midComplete && legs.isNotEmpty()) midMark else null
    val ltpMarkValue = if (structureUsable && ltpComplete && legs.isNotEmpty()) ltpMark else null
    // A raw mark only represents a complete position when every required leg was
    // present and supplied the relevant side. It must not look complete for a
    // truncated four-leg structure.
    val rawMarkIsCompletePosition = rawMarkComplete && structure.status == STRUCTURE_ROLES_OK && legs.isNotEmpty()
    val rawMarkValue = if (rawMarkIsCompletePosition) rawExecutableMark else null

    val currentPnl = if (entryPremium != null && executableMarkValue != null) {
        computePositionTickCurrentPnl(entryPremium, executableMarkValue, isCredit, lotSize)
    } else {
        null
    }
    val currentPnlR =
        if (currentPnl != null && maxLoss != null && maxLoss > 0.0) currentPnl / maxLoss else null
    val boundAnomaly = violatesStructuralBounds(currentPnl, maxProfit, maxLoss, boundTolerance)

    return TickValuation(
        legValuations = legValuations,
        structure = structure,
        valuationQuality = valuationQuality,
        valuationAccepted = valuationAccepted,
        executableMark = executableMarkValue,
        midMark = midMarkValue,
        ltpMark = ltpMarkValue,
        rawExecutableMark = rawMarkValue,
        rawMarkComplete = rawMarkIsCompletePosition,
        currentPnl = currentPnl,
        currentPnlR = currentPnlR,
        boundAnomaly = boundAnomaly,
        nonPositiveQuoteLegs = nonPositive,
        crossedQuoteLegs = crossed
    )
}

/** Required (side, optionType) multiset per supported strategy. */
private val STRUCTURE_SPEC: Map<String, List<Pair<String, String>>> = mapOf(
    "BEAR_CALL" to listOf("SHORT" to "CE", "LONG" to "CE"),
    "BULL_CALL" to listOf("LONG" to "CE", "SHORT" to "CE"),
    "BULL_PUT" to listOf("SHORT" to "PE", "LONG" to "PE"),
    "BEAR_PUT" to listOf("LONG" to "PE", "SHORT" to "PE"),
    "IRON_CONDOR" to listOf("SHORT" to "CE", "LONG" to "CE", "SHORT" to "PE", "LONG" to "PE"),
    "IRON_BUTTERFLY" to listOf("SHORT" to "CE", "LONG" to "CE", "SHORT" to "PE", "LONG" to "PE")
)

/** Legs a supported structure must have; null when the strategy is not recognised. */
internal fun expectedLegCount(strategyType: String): Int? =
    STRUCTURE_SPEC[strategyType.uppercase()]?.size

/**
 * Validates that a resolved leg set actually is the structure it claims to be.
 *
 * Counting alone is insufficient - duplicate instruments, inverted roles, or an extra
 * leg all satisfy `legs.size >= expected`. This asserts the exact multiset of
 * (side, option type) plus instrument-key uniqueness.
 */
internal fun validateStructure(strategyType: String, legs: List<PositionLeg>): StructureCheck {
    val spec = STRUCTURE_SPEC[strategyType.uppercase()]
        ?: return StructureCheck(STRUCTURE_UNSUPPORTED, null, legs.size, listOf("unsupported_strategy"))

    val problems = mutableListOf<String>()
    if (legs.size < spec.size) {
        problems.add("missing_legs:${spec.size - legs.size}")
        return StructureCheck(STRUCTURE_LEGS_MISSING, spec.size, legs.size, problems)
    }
    if (legs.size > spec.size) problems.add("extra_legs:${legs.size - spec.size}")

    val keys = legs.mapNotNull { it.instrumentKey?.takeIf { k -> k.isNotBlank() } }
    if (keys.size != keys.distinct().size) problems.add("duplicate_instrument_keys")
    if (legs.any { it.instrumentKey.isNullOrBlank() }) problems.add("blank_instrument_key")

    val want = spec.map { "${it.first}:${it.second}" }.sorted()
    val got = legs.map { "${it.side}:${(it.optionType ?: "?").uppercase()}" }.sorted()
    if (want != got) problems.add("role_mismatch:expected=${want.joinToString("|")}:got=${got.joinToString("|")}")

    val status = if (problems.isEmpty()) STRUCTURE_ROLES_OK else STRUCTURE_ROLES_INVALID
    return StructureCheck(status, spec.size, legs.size, problems)
}

/**
 * Slack allowed on the defined-risk P&L bounds before a mark is flagged as anomalous.
 *
 * max_profit / max_loss are recorded gross of friction while the mark is executable
 * (bid/ask), so a few percent of legitimate drift is expected.
 */
internal const val STRUCTURAL_BOUND_TOLERANCE_VALUE: Double = 1.05

/**
 * True when a mark lies outside what a defined-risk structure can realise.
 *
 * This is an ANOMALY SIGNAL, not a veto. Callers record it and keep the P&L: a wide
 * quoted market can briefly price outside the expiry payoff envelope without the
 * position being mismarked, and suppressing P&L on those ticks would blind the shadow
 * stop-loss path at exactly the moment it matters most.
 */
internal fun violatesStructuralBounds(
    pnl: Double?,
    maxProfit: Double?,
    maxLoss: Double?,
    tolerance: Double = STRUCTURAL_BOUND_TOLERANCE_VALUE
): Boolean {
    if (pnl == null || !pnl.isFinite()) return false
    if (maxProfit != null && maxProfit > 0.0 && pnl > maxProfit * tolerance) return true
    if (maxLoss != null && maxLoss > 0.0 && pnl < -maxLoss * tolerance) return true
    return false
}
