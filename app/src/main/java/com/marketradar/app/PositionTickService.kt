package com.marketradar.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
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
        try {
            startForeground(NOTIFICATION_ID, buildNotification())
            foregroundReady = true
        } catch (t: Throwable) {
            Log.e(TAG, "POSITION_TICK_FOREGROUND_BLOCKED: ${t.javaClass.simpleName}: ${t.message}")
            LogBuffer.add('E', TAG, "POSITION_TICK_FOREGROUND_BLOCKED: ${t.javaClass.simpleName}: ${t.message}")
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
            rows.put(buildTickRow(trade, sessionDate, tickTs, quoteFetch))
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
    ): JSONObject {
        val tradeId = trade.optStringAny("id", "")
        val strategyType = trade.optStringAny("strategy_type", "strategyType")
        val isCredit = isCreditTrade(trade, strategyType)
        val entryPremium = trade.optDoubleAny("entry_premium", "entryPremium", "net_premium", "netPremium")
        val maxLoss = trade.optDoubleAny("max_loss", "maxLoss")
        val maxProfit = trade.optDoubleAny("max_profit", "maxProfit")
        val lotSize = trade.optDoubleAny("lot_size", "lotSize").takeIf { it != null && it > 0.0 } ?: 1.0

        val legs = extractLegs(trade)
        val legsJson = JSONArray()
        var anyQuote = false
        var hasMissingKey = false
        var hasMissingExecutableSide = false
        var executableMark = 0.0
        var midMark = 0.0
        var ltpMark = 0.0
        var midComplete = legs.isNotEmpty()
        var ltpComplete = legs.isNotEmpty()

        legs.forEach { leg ->
            val quote = leg.instrumentKey?.let { quoteFetch.quotes[it] }
            val bid = quote?.bid
            val ask = quote?.ask
            val ltp = quote?.ltp
            val mid = if (bid != null && ask != null) (bid + ask) / 2.0 else null
            val executablePrice = if (leg.closeSide == CloseSide.BUY_TO_CLOSE) ask else bid
            val status = when {
                leg.instrumentKey.isNullOrBlank() -> "KEY_MISSING"
                quote == null || (bid == null && ask == null && ltp == null) -> "NO_QUOTE"
                bid == null || ask == null -> "NO_DEPTH"
                else -> "OK"
            }
            val priceBasis = when {
                executablePrice != null -> "EXECUTABLE"
                ltp != null -> "LTP"
                else -> "NONE"
            }
            if (status == "KEY_MISSING") hasMissingKey = true
            if (bid != null || ask != null || ltp != null) anyQuote = true
            if (bid == null || ask == null || executablePrice == null) hasMissingExecutableSide = true

            val sign = markSign(leg, isCredit)
            if (executablePrice != null) executableMark += sign * executablePrice
            if (mid != null) midMark += sign * mid else midComplete = false
            if (ltp != null) ltpMark += sign * ltp else ltpComplete = false

            legsJson.put(JSONObject().apply {
                put("instrument_key", leg.instrumentKey ?: JSONObject.NULL)
                put("side", leg.side)
                put("option_type", leg.optionType ?: JSONObject.NULL)
                put("strike", leg.strike ?: JSONObject.NULL)
                putOptNumber("bid", bid)
                putOptNumber("ask", ask)
                putOptNumber("ltp", ltp)
                putOptNumber("mid", mid)
                putOptNumber("executable_price", executablePrice)
                put("price_basis", priceBasis)
                put("quote_status", status)
            })
        }

        val valuationQuality = when {
            !anyQuote -> "UNAVAILABLE"
            hasMissingKey || hasMissingExecutableSide -> "DEGRADED"
            else -> "OK"
        }
        val executableMarkValue = if (valuationQuality == "OK" && legs.isNotEmpty()) executableMark else null
        val midMarkValue = if (midComplete && legs.isNotEmpty()) midMark else null
        val ltpMarkValue = if (ltpComplete && legs.isNotEmpty()) ltpMark else null
        val currentPnl = if (entryPremium != null && executableMarkValue != null) {
            if (isCredit) (entryPremium - executableMarkValue) * lotSize else (executableMarkValue - entryPremium) * lotSize
        } else {
            null
        }
        val currentPnlR = if (currentPnl != null && maxLoss != null && maxLoss > 0.0) currentPnl / maxLoss else null
        val running = updateRunningState(tradeId, currentPnl)
        val policy = evaluateShadowPolicy(tickTs, currentPnl, maxLoss, maxProfit, valuationQuality)

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
            put("valuation_quality", valuationQuality)
            put("mark_basis", if (executableMarkValue != null) "EXECUTABLE" else "NONE")
            putOptNumber("executable_mark", executableMarkValue)
            putOptNumber("mid_mark", midMarkValue)
            putOptNumber("ltp_mark", ltpMarkValue)
            putOptNumber("current_pnl", currentPnl)
            putOptNumber("current_pnl_r", currentPnlR)
            putOptNumber("running_mae", running.mae)
            putOptNumber("running_mfe", running.mfe)
            put("policy_action", policy.action)
            put("policy_reason", policy.reason)
            put("policy_trace_json", policy.trace)
            put("legs_json", legsJson)
        }
    }

    private fun evaluateShadowPolicy(
        tickTs: String,
        currentPnl: Double?,
        maxLoss: Double?,
        maxProfit: Double?,
        valuationQuality: String
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
        val key = trade.optStringAny("${prefix}_instrument_key", "${prefix}InstrumentKey")
        val strike = trade.optDoubleAny("${prefix}_strike", "${prefix}Strike")
        val type = trade.optStringAny("${prefix}_type", "${prefix}Type", "${prefix}_option_type", "${prefix}OptionType")
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

    private fun markSign(leg: PositionLeg, isCredit: Boolean): Double {
        return if (isCredit) {
            if (leg.closeSide == CloseSide.BUY_TO_CLOSE) 1.0 else -1.0
        } else {
            if (leg.closeSide == CloseSide.SELL_TO_CLOSE) 1.0 else -1.0
        }
    }

    private fun isCreditTrade(trade: JSONObject, strategyType: String): Boolean {
        if (trade.has("is_credit")) return trade.optBoolean("is_credit", false)
        if (trade.has("isCredit")) return trade.optBoolean("isCredit", false)
        val normalized = strategyType.uppercase(Locale.US)
        return normalized == "BULL_PUT" ||
            normalized == "BEAR_CALL" ||
            normalized == "IRON_CONDOR" ||
            normalized == "SELL_PREMIUM"
    }

    private data class PositionLeg(
        val instrumentKey: String?,
        val side: String,
        val closeSide: CloseSide,
        val optionType: String?,
        val strike: Double?
    )

    private enum class CloseSide { BUY_TO_CLOSE, SELL_TO_CLOSE }

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
        private const val NOTIFICATION_CHANNEL_ID = "position_tick_capture"
        private const val NOTIFICATION_ID = 23018
        private const val SOURCE = "P1_REST_60S"
        private const val TICK_MS = 60_000L
        private const val JITTER_MS = 5_000L
        private const val FLUSH_MIN_MS = 60_000L
        private const val MAX_PENDING_TICKS = 1_500
        private const val MARKET_OPEN_MINUTES = 9 * 60 + 15
        private const val MARKET_CLOSE_MINUTES = 15 * 60 + 30
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
