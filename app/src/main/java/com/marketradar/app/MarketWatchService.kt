package com.marketradar.app

import android.app.*
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.os.*
import android.util.Log
import androidx.core.app.NotificationCompat
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlinx.coroutines.*
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import okhttp3.logging.HttpLoggingInterceptor
import com.marketradar.app.util.LogBuffer
import java.text.SimpleDateFormat
import java.util.*
import java.util.concurrent.TimeUnit
import java.io.File

class MarketWatchService : Service() {

    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var wakeLock: PowerManager.WakeLock? = null
    private lateinit var prefs: SharedPreferences
    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .addInterceptor(HttpLoggingInterceptor { msg ->
            LogBuffer.add('I', "OkHttp", msg)
        }.apply { level = HttpLoggingInterceptor.Level.BASIC })
        .build()
    
    private var token401Counter = 0
    private var lastAlertKeys = mutableSetOf<String>()
    private var pollingJob: Job? = null

    companion object {
        const val CHANNEL_ID = "market_radar_service"
        const val NOTIFICATION_ID = 1001
        const val TAG = "MarketWatchService"
        
        private val BNF_WEIGHTS = mapOf(
            "NSE_EQ|HDFCBANK" to 0.285,
            "NSE_EQ|ICICIBANK" to 0.235,
            "NSE_EQ|AXISBANK" to 0.095,
            "NSE_EQ|SBIN" to 0.092,
            "NSE_EQ|KOTAKBANK" to 0.085
        )
    }

    override fun onCreate() {
        super.onCreate()
        LogBuffer.add('I', "MarketWatchService", "Service started, pid=${android.os.Process.myPid()}")
        prefs = getSharedPreferences("market_radar", Context.MODE_PRIVATE)
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == "STOP") {
            stopPolling()
            return START_NOT_STICKY
        }

        startForeground(NOTIFICATION_ID, createNotification("Service Starting", "Initializing poll loop..."))
        prefs.edit().putBoolean("service_running", true).commit() // NB5: use commit() for cross-process visibility
        
        serviceScope.launch {
            bootstrapFromSupabase()
            // We need the token for OHLC/Futures Key resolution which are Startup tasks
            val token = prefs.getString("auth_token", "") ?: ""
            if (token.isNotEmpty()) {
                bootstrapFromUpstox(token)
            }
            startPolling()
        }
        return START_STICKY
    }

    private suspend fun bootstrapFromSupabase() {
        val lastSync = prefs.getLong("last_bootstrap_time", 0L)
        val now = System.currentTimeMillis()
        
        // Only fetch if data is missing or older than 30 minutes
        val isStale = (now - lastSync) > 30 * 60 * 1000L
        val hasBaseline = prefs.contains("morning_baseline")
        
        if (!isStale && hasBaseline) {
            Log.d(TAG, "Bootstrap skipped: data is fresh")
            val premHistory = JSONArray(prefs.getString("premium_history", "[]"))
            val ySig = prefs.getString("yesterday_signal", "null")
            val historyLoadedLog = "HISTORY_LOADED_RESTART: vixCount=${premHistory.length()}, fiiCount=${extractFiiHistory().length()}, ySignal=$ySig"
            Log.d(TAG, historyLoadedLog)
            return
        }

        updateForegroundNotification("Bootstrapping", "Fetching data from Supabase...")
        Log.d(TAG, "Starting bootstrap from Supabase...")

        withContext(Dispatchers.IO) {
            try {
                // 1. Baseline
                SupabaseClient.getBaseline()?.let {
                    prefs.edit().putString("morning_baseline", it.toString()).apply()
                }

                // 2. Open Trades
                val open = SupabaseClient.getOpenTrades()
                prefs.edit().putString("open_trades", open.toString()).apply()

                // 3. Closed Trades
                val closed = SupabaseClient.getClosedTrades()
                prefs.edit().putString("closed_trades", closed.toString()).apply()
                val today = SimpleDateFormat("yyyy-MM-dd", Locale.getDefault()).format(Date())
                val history = SupabaseClient.getPollHistory(today)
                if (history.length() > 0) {
                    prefs.edit().putString("poll_history", history.toString()).apply()
                }

                // 5. Historial Premium Data & Signal
                val premHistory = SupabaseClient.getPremiumHistory()
                if (premHistory.length() > 0) {
                    prefs.edit().putString("premium_history", premHistory.toString()).apply()
                }
                
                val yesterday = getYesterdayDate()
                SupabaseClient.getYesterdaySignal(yesterday)?.let {
                    prefs.edit().putString("yesterday_signal", it.toString()).apply()
                    Log.d(TAG, "SIGNAL_PRIOR_LOADED: $it")
                }

                val historyLoadedLog = "HISTORY_LOADED: vixCount=${premHistory.length()}, ivPercentile=${calculateIvPercentile(18.0)}, fiiCount=${extractFiiHistory().length()}"
                Log.d(TAG, historyLoadedLog)

                prefs.edit().putLong("last_bootstrap_time", now).apply()
                Log.d(TAG, "Bootstrap complete")
            } catch (e: Exception) {
                Log.e(TAG, "Bootstrap failed: ${e.message}")
            }
        }
    }

    private suspend fun bootstrapFromUpstox(token: String) {
        try {
            fetchYesterdayOHLC(token)
            // WS1: Ensure we have the near-month keys for both indices for breadth/fp
            val bnfFutKey = getFuturesKey("BNF")
            val nfFutKey = getFuturesKey("NF")
            Log.d(TAG, "BOOTSTRAP_FUTURES: BNF=$bnfFutKey, NF=$nfFutKey")
            
            // Fetch initial futures quotes to prime the system
            val url = "https://api.upstox.com/v2/market-quote/quotes?instrument_key=$bnfFutKey,$nfFutKey"
            fetchSync(url, token)
        } catch (e: Exception) {
            Log.e(TAG, "Bootstrap from Upstox failed: ${e.message}")
        }
    }

    private suspend fun fetchYesterdayOHLC(token: String) {
        val today = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date())
        if (prefs.getString("ohlc_date", "") == today) return

        Log.d(TAG, "Fetching yesterday's OHLC...")
        val url = "https://api.upstox.com/v2/market-quote/ohlc?instrument_key=NSE_INDEX|Nifty Bank,NSE_INDEX|Nifty 50&interval=1d"
        val json = fetchSync(url, token)
        if (json != null) {
            prefs.edit().putString("yesterday_ohlc", json.toString())
                .putString("ohlc_date", today)
                .apply()
            Log.d(TAG, "OHLC_FETCHED: Success")
        }
    }

    private fun getYesterdayDate(): String {
        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        val cal = Calendar.getInstance(ist)
        
        // WS2: NSE Holiday support (2026 partial list - example)
        val holidays = setOf("2026-01-26", "2026-03-06", "2026-03-24", "2026-04-02", "2026-04-14", "2026-05-01")
        
        do {
            cal.add(Calendar.DATE, -1)
            val ds = SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = ist }.format(cal.time)
            val isWeekend = cal.get(Calendar.DAY_OF_WEEK) == Calendar.SUNDAY || cal.get(Calendar.DAY_OF_WEEK) == Calendar.SATURDAY
            val isHoliday = holidays.contains(ds)
        } while (isWeekend || isHoliday)
        
        return SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = ist }.format(cal.time)
    }

    private fun getFuturesKey(index: String): String {
        // WS6: Implement near-month futures rollover logic
        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        val cal = Calendar.getInstance(ist)
        val day = cal.get(Calendar.DAY_OF_MONTH)
        val month = cal.get(Calendar.MONTH) // 0-based
        val year = cal.get(Calendar.YEAR)
        
        // Find last Thursday of current month
        val temp = Calendar.getInstance(ist).apply {
            set(Calendar.YEAR, year)
            set(Calendar.MONTH, month)
            set(Calendar.DAY_OF_MONTH, getActualMaximum(Calendar.DAY_OF_MONTH))
        }
        while (temp.get(Calendar.DAY_OF_WEEK) != Calendar.THURSDAY) {
            temp.add(Calendar.DAY_OF_MONTH, -1)
        }
        val lastThursday = temp.get(Calendar.DAY_OF_MONTH)
        
        // If today is past last Thursday, roll to next month
        val targetMonthCal = Calendar.getInstance(ist)
        if (day > lastThursday) {
            targetMonthCal.add(Calendar.MONTH, 1)
        }
        
        val monthStr = SimpleDateFormat("MMM", Locale.US).format(targetMonthCal.time).uppercase()
        val yearShort = SimpleDateFormat("yy", Locale.US).format(targetMonthCal.time)
        
        val symbol = if (index == "NF") "NIFTY" else "BANKNIFTY"
        return "NSE_FO|$symbol$yearShort$monthStr" + "FUT"
    }

    private fun startPolling() {
        pollingJob?.cancel() // Guard: Stop any existing poll coroutine before starting a new one
        pollingJob = serviceScope.launch {
            while (isActive) {
                if (isMarketOpen()) {
                    // Re-read token dynamically on each poll cycle (Bug 1 Fix)
                    val appCtx = applicationContext
                    val currentPrefs = appCtx.getSharedPreferences("market_radar", Context.MODE_PRIVATE)
                    val currentToken = currentPrefs.getString("auth_token", "") ?: ""
                    
                    if (currentToken.isNotEmpty()) {
                        acquirePartialWakeLock()
                        try {
                            performPoll(currentToken)
                        } catch (e: Exception) {
                            Log.e(TAG, "Poll failed: ${e.message}")
                            LogBuffer.add('E', "MarketWatchService", "Poll #${prefs.getInt("poll_count", 0) + 1} FAILED: ${e.message}")
                        } finally {
                            releaseWakeLock()
                        }
                    } else {
                        Log.w(TAG, "No auth_token in prefs, skipping poll.")
                        updateForegroundNotification("Waiting for Token", "Open app to sync Upstox token")
                    }
                } else {
                    Log.d(TAG, "Market closed. Skipping poll.")
                    updateForegroundNotification("Market Closed", "Waiting for next session...")
                }
                
                // WS5: Read poll delay dynamically (default 5m)
                val pollIntervalMins = prefs.getInt("poll_frequency_mins", 5)
                delay(pollIntervalMins * 60 * 1000L)
            }
        }
    }

    private fun stopPolling() {
        prefs.edit().putBoolean("service_running", false).commit() 
        // WS27: Cancel job before stopping foreground to ensure no new notifications are triggered
        pollingJob?.cancel()
        serviceScope.cancel()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private suspend fun performPoll(token: String) {
        val pollCount = prefs.getInt("poll_count", 0) + 1
        Log.d(TAG, "POLL_START: performPoll() entered")
        LogBuffer.add('I', "MarketWatchService", "Poll #$pollCount starting")
        
        // C4: Dynamically compute next Thursday if expiry is missing
        val nextThu = getNextThursday()
        val bnfExpiry = prefs.getString("expiry_bnf", nextThu) ?: nextThu
        
        // Expiry Rollover check
        val sdfUTC = SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = TimeZone.getTimeZone("Asia/Kolkata") }
        val today = sdfUTC.format(Date())
        
        if (bnfExpiry < today) {
            Log.w(TAG, "POLL_SKIP: Expiry passed: $bnfExpiry < $today")
            NotificationHelper.send(this, "⚠️ Expiry passed", "Poll skipped. Open app to refresh expiry dates.", "info")
            return
        }

        updateForegroundNotification("Polling Market", "Fetching Quotes...")

        // Step 1: Fetch Spot Prices
        Log.d(TAG, "POLL_STEP1: Fetching spot prices")
        val quotesUrl = "https://api.upstox.com/v2/market-quote/quotes?instrument_key=NSE_INDEX|Nifty Bank,NSE_INDEX|Nifty 50,NSE_INDEX|India VIX"
        val quotesJson = fetchSync(quotesUrl, token)
        if (quotesJson == null) {
            Log.e(TAG, "POLL_FAIL: Quotes fetch returned null — network or auth error")
            return
        }
        
        // Step 2: Fetch BNF chain
        val bnfStocks = BNF_WEIGHTS.keys.joinToString(",")
        Log.d(TAG, "POLL_STEP2: Fetching BNF chain + Breadth stocks")
        val nfExpiry = prefs.getString("expiry_nf", bnfExpiry) ?: bnfExpiry
        val bnfUrl = "https://api.upstox.com/v2/option/chain?instrument_key=NSE_INDEX|Nifty Bank&expiry_date=$bnfExpiry"
        val bnfStocksUrl = "https://api.upstox.com/v2/market-quote/quotes?instrument_key=$bnfStocks,${getFuturesKey("BANKNIFTY")},${getFuturesKey("NIFTY")}"
        
        val bnfChainJson = fetchSync(bnfUrl, token)
        val bnfStocksJson = fetchSync(bnfStocksUrl, token)
        
        if (bnfChainJson == null || bnfStocksJson == null) {
            Log.e(TAG, "POLL_FAIL: BNF chain or stocks/futures fetch returned null")
            return
        }
        
        // Step 3: Fetch NF chain
        Log.d(TAG, "POLL_STEP3: Fetching NF option chain (expiry=$nfExpiry)")
        val nfUrl = "https://api.upstox.com/v2/option/chain?instrument_key=NSE_INDEX|Nifty 50&expiry_date=$nfExpiry"
        val nfChainJson = fetchSync(nfUrl, token)
        if (nfChainJson == null) {
            Log.e(TAG, "POLL_FAIL: NF chain fetch returned null")
            return
        }

        val data = quotesJson.getJSONObject("data")
        val bnfSpot = data.getJSONObject("NSE_INDEX:Nifty Bank").getDouble("last_price")
        val nfSpot = data.getJSONObject("NSE_INDEX:Nifty 50").getDouble("last_price")
        val vix = data.getJSONObject("NSE_INDEX:India VIX").getDouble("last_price")
        Log.d(TAG, "POLL_DATA_RECEIVED: BNF=$bnfSpot NF=$nfSpot VIX=$vix")

        // Step 5: Build poll object and save
        Log.d(TAG, "POLL_STEP5: Saving poll object")
        val pollObj = parsePollData(quotesJson, bnfChainJson, bnfStocksJson, bnfSpot)
        savePoll(pollObj)
        LogBuffer.add('I', "MarketWatchService", "Poll #$pollCount complete, candidates=${pollObj.optJSONArray("candidates")?.length() ?: 0}")

        // Step 6: Run Python Brain
        Log.d(TAG, "POLL_STEP6: Launching brain analysis")
        runBrainAnalysis(pollObj, bnfChainJson, nfChainJson, bnfSpot, nfSpot, vix, bnfStocksJson)
    }


    private fun minutesSince(oldT: String, newT: String): Int {
        try {
            val sdf = SimpleDateFormat("HH:mm", Locale.getDefault())
            val d1 = sdf.parse(oldT)
            val d2 = sdf.parse(newT)
            return ((d2.time - d1.time) / (1000 * 60)).toInt()
        } catch (e: Exception) { return 99 }
    }

    private fun extractLtpMap(chainJson: JSONObject): JSONObject {
        val map = JSONObject()
        val data = chainJson.optJSONArray("data") ?: return map
        for (i in 0 until data.length()) {
            val item = data.getJSONObject(i)
            val strike = item.optDouble("strike_price").toString()
            val pair = JSONObject()
            pair.put("CE", item.optJSONObject("call_options")?.optJSONObject("market_data")?.optDouble("last_price", 0.0) ?: 0.0)
            pair.put("PE", item.optJSONObject("put_options")?.optJSONObject("market_data")?.optDouble("last_price", 0.0) ?: 0.0)
            map.put(strike, pair)
        }
        return map
    }



    private fun parsePollData(quotes: JSONObject, chain: JSONObject, stocks: JSONObject, spot: Double): JSONObject {
        val data = quotes.getJSONObject("data")
        val sData = stocks.getJSONObject("data")
        val bnf = data.getJSONObject("NSE_INDEX:Nifty Bank").getDouble("last_price")
        val nf = data.getJSONObject("NSE_INDEX:Nifty 50").getDouble("last_price")
        val vix = data.getJSONObject("NSE_INDEX:India VIX").getDouble("last_price")
        
        val time = SimpleDateFormat("HH:mm", Locale.getDefault()).apply { timeZone = TimeZone.getTimeZone("Asia/Kolkata") }.format(Date())
        
        var maxCallOi = 0.0
        var maxPutOi = 0.0
        var cw = 0.0
        var pw = 0.0
        
        // Near-ATM PCR (±10 strikes)
        var nearAtmCOI = 0.0
        var nearAtmPOI = 0.0
        
        // Straddle Logic (ATM premiums)
        var atmCE = 0.0
        var atmPE = 0.0
        var atmDist = Double.MAX_VALUE
        var atmStrike = 0.0
        
        val chainData = chain.getJSONArray("data")
        for (i in 0 until chainData.length()) {
            val item = chainData.getJSONObject(i)
            val strikePrice = item.getDouble("strike_price")
            
            val callMd = item.optJSONObject("call_options")?.optJSONObject("market_data")
            val putMd = item.optJSONObject("put_options")?.optJSONObject("market_data")
            
            val coi = callMd?.optDouble("oi", 0.0) ?: 0.0
            val poi = putMd?.optDouble("oi", 0.0) ?: 0.0
            
            // Wall logic
            if (coi > maxCallOi) { maxCallOi = coi; cw = strikePrice }
            if (poi > maxPutOi) { maxPutOi = poi; pw = strikePrice }
            
            // Near-ATM Logic
            val gapS = bnf * (vix / 100.0) / Math.sqrt(252.0)
            if (gapS > 0 && Math.abs(strikePrice - bnf) <= gapS * 1.5) { // C6 partial: use sigma-based near-atm
                nearAtmCOI += coi
                nearAtmPOI += poi
            }
            
            // Straddle Logic
            val dist = Math.abs(strikePrice - bnf)
            if (dist < atmDist) {
                atmDist = dist
                atmStrike = strikePrice
                
                // Fallback to bid/ask if LTP is 0
                atmCE = callMd?.optDouble("last_price", 0.0) ?: 0.0
                if (atmCE == 0.0) {
                    val bid = callMd?.optDouble("bid_price", 0.0) ?: 0.0
                    val ask = callMd?.optDouble("ask_price", 0.0) ?: 0.0
                    atmCE = if (bid > 0 && ask > 0) (bid + ask) / 2.0 else Math.max(bid, ask)
                }
                
                atmPE = putMd?.optDouble("last_price", 0.0) ?: 0.0
                if (atmPE == 0.0) {
                    val bid = putMd?.optDouble("bid_price", 0.0) ?: 0.0
                    val ask = putMd?.optDouble("ask_price", 0.0) ?: 0.0
                    atmPE = if (bid > 0 && ask > 0) (bid + ask) / 2.0 else Math.max(bid, ask)
                }
            }
        }

        val poll = JSONObject()
        poll.put("t", time)
        poll.put("bnf", bnf)
        poll.put("nf", nf)
        poll.put("vix", vix)
        // Sigma Logic (Fix C2: correct daily sigma sqrt(252))
        val dailySigma = bnf * (vix / 100.0) / Math.sqrt(252.0)
        poll.put("gap_sigma", dailySigma)
        
        // Phase E.1 — spotSigma / vixSigma wiring (port-first from app.js L5540-5552)
        // Required by runBrainAnalysis significantMove gate. Without these two fields
        // populated here, ctx.significant_move is always false and evaluate_alerts
        // skips WATCHLIST/POSITION/MARKET branches.
        val baselineStr = prefs.getString("morning_baseline", null)
        var spotSigma = 0.0
        var vixSigmaValue = 0.0
        if (baselineStr != null) {
            try {
                val baseline = JSONObject(baselineStr)
                val baselineSpot = baseline.optDouble("bnfSpot", 0.0)
                val baselineVix = baseline.optDouble("vix", 0.0)
                if (baselineSpot > 0 && baselineVix > 0) {
                    val spotDailySigma = baselineSpot * (baselineVix / 100.0) / Math.sqrt(252.0)
                    if (spotDailySigma > 0) {
                        spotSigma = (bnf - baselineSpot) / spotDailySigma
                    }
                    val vixDailySigma = baselineVix * 0.10
                    if (vixDailySigma > 0) {
                        vixSigmaValue = (vix - baselineVix) / vixDailySigma
                    }
                }
            } catch (e: Exception) {
                Log.w(TAG, "spotSigma/vixSigma compute failed: ${e.message}")
            }
        }
        poll.put("spotSigma", spotSigma)
        poll.put("vixSigma", vixSigmaValue)
        
        poll.put("cw", cw)
        poll.put("pw", pw)
        poll.put("cwOI", maxCallOi)
        poll.put("pwOI", maxPutOi)
        poll.put("straddle", atmCE + atmPE)
        poll.put("pcr", if (nearAtmCOI > 0) nearAtmPOI / nearAtmCOI else 1.0) // brain expects PE/CE
        
        // Futures Premium
        val bnfFutKey = getFuturesKey("BANKNIFTY")
        Log.d(TAG, "FP_DEBUG: bnfFutKey=$bnfFutKey")
        var bnfFutLtp = sData.optJSONObject(bnfFutKey)?.optDouble("last_price", 0.0) ?: 0.0
        
        if (bnfFutLtp > 0) {
            Log.d(TAG, "FP_SOURCE: actual ($bnfFutLtp)")
        } else {
            // Synthetic Fallback: ATM_CE - ATM_PE + Spot
            bnfFutLtp = atmCE - atmPE + bnf
            Log.d(TAG, "FP_SOURCE: synthetic ($bnfFutLtp)")
        }
        
        poll.put("fp", (bnfFutLtp - bnf) / bnf * 100.0)
        
        Log.d("AUDIT_POLL", poll.toString())
        return poll
    }

    private fun savePoll(poll: JSONObject) {
        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        val today = SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = ist }.format(Date())
        val lastPollDate = prefs.getString("last_poll_date", "") ?: ""
        
        var history = JSONArray(prefs.getString("poll_history", "[]"))
        var pollCount = prefs.getInt("poll_count", 0)

        // A1+A5: Daily Reset
        if (lastPollDate != today) {
            Log.i(TAG, "DAILY_RESET: New trading day detected ($today). Resetting history.")
            history = JSONArray()
            pollCount = 0
            prefs.edit().putString("last_poll_date", today).apply()
        }
        
        // Append new poll
        history.put(poll)
        pollCount++ // A5: Monotonic increment
        
        // Keep last 100 for memory
        if (history.length() > 100) {
            val trimmed = JSONArray()
            for (i in (history.length() - 100) until history.length()) {
                trimmed.put(history.get(i))
            }
            history = trimmed
        }

        prefs.edit().apply {
            putString("latest_poll", poll.toString())
            putString("poll_history", history.toString())
            putString("last_poll_time", poll.getString("t"))
            putInt("poll_count", pollCount)
        }.apply()
        
        // GAP 12: Institutional Positioning
        checkInstitutionalPositioning(poll)

        // GAP 6: Upsert to Supabase every 3rd poll
        if (pollCount > 0 && pollCount % 3 == 0) {
            serviceScope.launch(Dispatchers.IO) {
                val today = SimpleDateFormat("yyyy-MM-dd", Locale.getDefault()).format(Date())
                SupabaseClient.upsertPollHistory(today, history)
            }
        }

        updateForegroundNotification("Watching Market", "BNF: ${poll.getDouble("bnf")} | VIX: ${poll.getDouble("vix")}")
    }

    private fun checkInstitutionalPositioning(poll: JSONObject) {
        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        val cal = Calendar.getInstance(ist)
        val mins = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE)
        val today = SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = ist }.format(Date())
        val lastSavedDate = prefs.getString("positioning_date", "")

        // Reset if new day
        if (lastSavedDate != today) {
            prefs.edit().apply {
                remove("afternoon_baseline")
                remove("tomorrow_signal")
                remove("has2pmSnapshot")
                remove("has315pmSnapshot")
                putString("positioning_date", today)
            }.apply()
        }

        // At 2:00 PM (840 mins) to 2:10 PM
        if (mins in 840..850) {
            if (!prefs.contains("afternoon_baseline")) {
                prefs.edit().putString("afternoon_baseline", poll.toString()).apply()
                Log.d(TAG, "Captured 2PM institutional baseline")
            }
        }

        // At 3:15 PM (915 mins) to 3:30 PM
    }


    private fun calculateBreadth(stocks: JSONObject): JSONObject {
        val result = JSONObject()
        try {
            val data = stocks.getJSONObject("data")
            var weightedBnfPct = 0.0
            var advancing = 0
            var declining = 0
            
            for ((key, weight) in BNF_WEIGHTS) {
                val stockKey = key.replace("|", ":")
                val stockData = data.optJSONObject(stockKey)
                val ltp = stockData?.optDouble("last_price", 0.0) ?: 0.0
                val close = stockData?.optJSONObject("ohlc")?.optDouble("close", 0.0) ?: 0.0
                
                if (ltp > close && close > 0) {
                    advancing++
                    weightedBnfPct += weight * 100.0
                } else if (ltp < close && close > 0) {
                    declining++
                    weightedBnfPct += weight * 0.0
                } else {
                    weightedBnfPct += weight * 50.0 // neutral
                }
            }
            
            result.put("pct", weightedBnfPct)
            result.put("advancing", advancing)
            result.put("declining", declining)
        } catch (e: Exception) {
            Log.e(TAG, "Breadth calc failed: ${e.message}")
            result.put("pct", 50.0).put("advancing", 0).put("declining", 0)
        }
        return result
    }

    private fun calculateIvPercentile(vix: Double): Int {
        try {
            val histStr = prefs.getString("premium_history", "[]") ?: "[]"
            val hist = JSONArray(histStr)
            if (hist.length() < 10) return 50
            var lower = 0
            for (i in 0 until hist.length()) {
                if (vix > hist.getJSONObject(i).optDouble("vix", 0.0)) lower++
            }
            return (lower * 100 / hist.length())
        } catch (e: Exception) { return 50 }
    }

    private fun extractFiiHistory(): JSONArray {
        val uniqueDays = LinkedHashMap<String, JSONObject>()
        try {
            val hist = JSONArray(prefs.getString("premium_history", "[]") ?: "[]")
            for (i in 0 until hist.length()) {
                val row = hist.getJSONObject(i)
                val date = row.optString("date", "")
                if (date.isEmpty()) continue
                
                if (!uniqueDays.containsKey(date)) {
                    val entry = JSONObject()
                    entry.put("fiiCash", row.optDouble("fii_cash", 0.0))
                    entry.put("fiiShort", row.optDouble("fii_short_pct", 0.0))
                    entry.put("vix", row.optDouble("vix", 0.0))
                    entry.put("date", date)
                    uniqueDays[date] = entry
                }
                if (uniqueDays.size >= 5) break
            }
        } catch (e: Exception) {
            Log.e(TAG, "extractFiiHistory error: ${e.message}")
        }
        
        val result = JSONArray()
        uniqueDays.values.forEach { result.put(it) }
        return result
    }

    private fun computeGapObject(ohlc: JSONObject, bnfSpot: Double): JSONObject {
        val gap = JSONObject()
        try {
            val bnfOhlc = ohlc.getJSONObject("data").getJSONObject("NSE_INDEX:Nifty Bank").getJSONObject("ohlc")
            val prevClose = bnfOhlc.getDouble("close")
            val todayOpen = bnfOhlc.getDouble("open")
            
            val pct = (todayOpen - prevClose) / prevClose * 100.0
            val sigma = pct / 0.5 // Simplified: 1 sigma = 0.5%
            
            gap.put("type", if (pct > 0.3) "GAP_UP" else if (pct < -0.3) "GAP_DOWN" else "FLAT")
            gap.put("pct", pct)
            gap.put("sigma", sigma)
        } catch (e: Exception) {
            gap.put("type", "FLAT").put("pct", 0.0).put("sigma", 0.0)
        }
        return gap
    }

    private fun formatChainForBrain(chain: JSONObject, spot: Double): JSONObject {
        val result = JSONObject()
        try {
            val data = chain.optJSONArray("data") ?: return result
            val strikesObj = JSONObject()
            val allStrikesArr = JSONArray()
            
            var atmStrike = 0.0
            var atmMinDist = Double.MAX_VALUE
            var atmCEIv = 0.0
            var atmPEIv = 0.0

            for (i in 0 until data.length()) {
                val item = data.getJSONObject(i)
                val strike = item.getDouble("strike_price")
                allStrikesArr.put(strike)
                
                val call = item.optJSONObject("call_options")
                val put = item.optJSONObject("put_options")
                
                val dist = Math.abs(strike - spot)
                if (dist < atmMinDist) {
                    atmMinDist = dist
                    atmStrike = strike
                    val cmd = call?.optJSONObject("market_data")
                    val pmd = put?.optJSONObject("market_data")
                    atmCEIv = cmd?.optDouble("iv", 0.0) ?: 0.0
                    atmPEIv = pmd?.optDouble("iv", 0.0) ?: 0.0
                }
                
                val strikeObj = JSONObject()
                strikeObj.put("CE", JSONObject().apply {
                    val md = call?.optJSONObject("market_data")
                    put("ltp", md?.optDouble("last_price", 0.0) ?: 0.0)
                    val bid = md?.optDouble("bid_price", 0.0) ?: 0.0
                    val ask = md?.optDouble("ask_price", 0.0) ?: 0.0
                    put("bid", bid)
                    put("ask", ask)
                    put("mid", if (bid > 0 && ask > 0) (bid + ask) / 2.0 else md?.optDouble("last_price", 0.0) ?: 0.0)
                    put("oi", md?.optDouble("oi", 0.0) ?: 0.0)
                    put("volume", md?.optDouble("volume", 0.0) ?: 0.0)
                    put("iv", md?.optDouble("iv", 0.0) ?: 0.0)
                    put("prev_oi", md?.optDouble("prev_oi", 0.0) ?: 0.0)  // PHASE C STEP 7.0
                    
                    val gr = call?.optJSONObject("option_greeks")
                    put("delta", gr?.optDouble("delta", 0.0) ?: 0.0)
                    put("gamma", gr?.optDouble("gamma", 0.0) ?: 0.0)
                    put("theta", gr?.optDouble("theta", 0.0) ?: 0.0)
                    put("vega", gr?.optDouble("vega", 0.0) ?: 0.0)        // PHASE C STEP 7.0
                    put("pop", gr?.optDouble("pop", 0.0) ?: 0.0)          // PHASE C STEP 7.0
                })
                strikeObj.put("PE", JSONObject().apply {
                    val md = put?.optJSONObject("market_data")
                    put("ltp", md?.optDouble("last_price", 0.0) ?: 0.0)
                    val bid = md?.optDouble("bid_price", 0.0) ?: 0.0
                    val ask = md?.optDouble("ask_price", 0.0) ?: 0.0
                    put("bid", bid)
                    put("ask", ask)
                    put("mid", if (bid > 0 && ask > 0) (bid + ask) / 2.0 else md?.optDouble("last_price", 0.0) ?: 0.0)
                    put("oi", md?.optDouble("oi", 0.0) ?: 0.0)
                    put("volume", md?.optDouble("volume", 0.0) ?: 0.0)
                    put("iv", md?.optDouble("iv", 0.0) ?: 0.0)
                    put("prev_oi", md?.optDouble("prev_oi", 0.0) ?: 0.0)  // PHASE C STEP 7.0
                    
                    val gr = put?.optJSONObject("option_greeks")
                    put("delta", gr?.optDouble("delta", 0.0) ?: 0.0)
                    put("gamma", gr?.optDouble("gamma", 0.0) ?: 0.0)
                    put("theta", gr?.optDouble("theta", 0.0) ?: 0.0)
                    put("vega", gr?.optDouble("vega", 0.0) ?: 0.0)        // PHASE C STEP 7.0
                    put("pop", gr?.optDouble("pop", 0.0) ?: 0.0)          // PHASE C STEP 7.0
                })
                strikesObj.put(strike.toString(), strikeObj)
            }
            
            result.put("strikes", strikesObj)
            result.put("allStrikes", allStrikesArr)
            result.put("atm", atmStrike)
            result.put("atmIv", if (atmCEIv > 0 && atmPEIv > 0) (atmCEIv + atmPEIv) / 2.0 else Math.max(atmCEIv, atmPEIv))
            
        } catch (e: Exception) {
            Log.e(TAG, "Error formatting chain for brain: ${e.message}")
        }
        return result
    }

    private suspend fun runBrainAnalysis(poll: JSONObject, bnfChain: JSONObject, nfChain: JSONObject,
                                  bnfSpot: Double, nfSpot: Double, vix: Double, stocksJson: JSONObject) {
        var brainSuccess = false
        var broadcastData: String? = null

        try {
            val ctxObj = JSONObject(prefs.getString("context", "{}") ?: "{}")
            
            // C1: Calculate DTE (Days to Expiry) for brain context
            val sdf = SimpleDateFormat("yyyy-MM-dd", Locale.US)
            val todayDate = sdf.parse(SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date()))
            
            val bnfExpDate = try { sdf.parse(ctxObj.optString("bnfExpiry")) } catch(e: Exception) { null }
            val nfExpDate = try { sdf.parse(ctxObj.optString("nfExpiry")) } catch(e: Exception) { null }
            
            val bnfDTE = bnfExpDate?.let { (it.time - todayDate.time) / (24 * 60 * 60 * 1000L) } ?: 3
            val nfDTE = nfExpDate?.let { (it.time - todayDate.time) / (24 * 60 * 60 * 1000L) } ?: 3
            
            ctxObj.put("bnfDTE", bnfDTE)
            ctxObj.put("nfDTE",  nfDTE)

            Log.d(TAG, "BRAIN_START: Loading Chaquopy brain module")
            val py = Python.getInstance()
            val brain = py.getModule("brain")
            
            val pollsJson    = prefs.getString("poll_history",      "[]") ?: "[]"
            val baselineJson = prefs.getString("morning_baseline",  "{}") ?: "{}"
            val openTradesJson   = prefs.getString("open_trades",   "[]") ?: "[]"
            val closedTradesJson = prefs.getString("closed_trades", "[]") ?: "[]"
            
            // CHAIN MERGING (Phase C: Format raw chains for Python)
            fun mergeChain(key: String, liveChainRaw: JSONObject, spot: Double, 
                           cwPoll: Double, pwPoll: Double, pcrPoll: Double) {
                val formattedLive = formatChainForBrain(liveChainRaw, spot)
                val existingChain = ctxObj.optJSONObject(key)
                
                if (existingChain != null && existingChain.has("atm")) {
                    // Rich chain exists from WebView — refresh only live intraday fields
                    existingChain.put("strikes",         formattedLive.optJSONObject("strikes"))
                    existingChain.put("atm",             formattedLive.optDouble("atm", 0.0))
                    existingChain.put("callWallStrike",  cwPoll)
                    existingChain.put("putWallStrike",   pwPoll)
                } else {
                    // No rich data — use full formatted live chain
                    formattedLive.put("callWallStrike", cwPoll)
                    formattedLive.put("putWallStrike",  pwPoll)
                    formattedLive.put("pcr",            pcrPoll)
                    ctxObj.put(key, formattedLive)
                }
            }
            
            // Calculate NF walls
            var nfCw = 0.0; var nfPw = 0.0; var nfMaxC = 0.0; var nfMaxP = 0.0
            val nfData = nfChain.optJSONArray("data")
            if (nfData != null) {
                for (i in 0 until nfData.length()) {
                    val item = nfData.getJSONObject(i)
                    val s = item.optDouble("strike_price", 0.0)
                    val coi = item.optJSONObject("call_options")?.optJSONObject("market_data")?.optDouble("oi", 0.0) ?: 0.0
                    val poi = item.optJSONObject("put_options")?.optJSONObject("market_data")?.optDouble("oi", 0.0) ?: 0.0
                    if (coi > nfMaxC) { nfMaxC = coi; nfCw = s }
                    if (poi > nfMaxP) { nfMaxP = poi; nfPw = s }
                }
            }

            mergeChain("bnfChain", bnfChain, bnfSpot, poll.optDouble("cw", 0.0), poll.optDouble("pw", 0.0), poll.optDouble("pcr", 0.0))
            ctxObj.optJSONObject("bnfChain")?.put("bnf_spot", bnfSpot)
            mergeChain("nfChain",  nfChain,  nfSpot,  nfCw, nfPw, 0.0)
            ctxObj.optJSONObject("nfChain")?.put("nf_spot", nfSpot)

            // Phase C: Inject OHLC for profile calculations
            val ohlcStr = prefs.getString("yesterday_ohlc", null)
            if (ohlcStr != null) {
                try {
                    val ohlcData = JSONObject(ohlcStr).optJSONObject("data")
                    if (ohlcData != null) {
                        ctxObj.put("bnfOHLC", ohlcData.optJSONObject("NSE_INDEX|Nifty Bank"))
                        ctxObj.put("nfOHLC",  ohlcData.optJSONObject("NSE_INDEX|Nifty 50"))
                    }
                } catch (e: Exception) { Log.e(TAG, "OHLC parse fail: ${e.message}") }
            }

            // Data Parity Overlays
            val breadth = calculateBreadth(stocksJson)
            ctxObj.put("bnfBreadth", breadth)
            
            // Phase B: populate morning input + yesterday history for brain.py
            val morningInputStr = prefs.getString("morning_input", null)
            if (morningInputStr != null) {
                ctxObj.put("morning_input", JSONObject(morningInputStr))
            }
            val ydayHistStr = prefs.getString("premium_history", "[]") ?: "[]"
            ctxObj.put("yesterdayHistory", JSONArray(ydayHistStr))
            
            // Phase B: accuracy stats from Supabase (refresh once per day or per session)
            val accuracyStats = SupabaseClient.getSignalAccuracyStats()
            ctxObj.put("signalAccuracy", accuracyStats)
            
            // Phase B: evening close data for overnight delta
            val eveningCloseStr = prefs.getString("evening_close_baseline", null)
            if (eveningCloseStr != null) {
                ctxObj.put("eveningClose", JSONObject(eveningCloseStr))
            }
            
            // Phase B: global direction for overnight delta
            val globalDirStr = prefs.getString("global_direction", null)
            if (globalDirStr != null) {
                ctxObj.put("globalDirection", JSONObject(globalDirStr))
            }
            
            val gapObj = JSONObject()
            if (ohlcStr != null) {
                val ohlc = JSONObject(ohlcStr)
                val calculatedGap = computeGapObject(ohlc, bnfSpot)
                gapObj.put("type", calculatedGap.optString("type", "FLAT"))
                gapObj.put("pct", calculatedGap.optDouble("pct", 0.0))
                gapObj.put("sigma", calculatedGap.optDouble("sigma", 0.0))
            } else {
                gapObj.put("type", "FLAT").put("pct", 0.0).put("sigma", 0.0)
            }
            ctxObj.put("gap", gapObj)
            
            val ySigStr = prefs.getString("yesterday_signal", null)
            if (ySigStr != null) {
                val ySig = JSONObject(ySigStr)
                val sigObj = JSONObject()
                sigObj.put("signal", ySig.optString("tomorrow_signal", "NEUTRAL").replace("Tomorrow: ", "").split(" ")[0])
                sigObj.put("strength", ySig.optString("tomorrow_signal", "").let { if (it.contains("4/5")) 4 else 2 })
                ctxObj.put("yesterdaySignal", sigObj)
            }
            
            // Always overlay history from SharedPreferences for brain.py (Persistence)
            val premHist = JSONArray(prefs.getString("premium_history", "[]") ?: "[]")
            val vixHist = JSONArray()
            for (i in 0 until premHist.length()) {
                val v = premHist.getJSONObject(i).optDouble("vix", 0.0)
                if (v > 0) vixHist.put(v)
            }
            if (vixHist.length() > 0) ctxObj.put("vixHistory", vixHist)
            
            val fiiHist = extractFiiHistory()
            if (fiiHist.length() > 0) ctxObj.put("fiiHistory", fiiHist)
            
            ctxObj.put("ivPercentile", calculateIvPercentile(vix))

            ctxObj.put("capital",    prefs.getInt("capital", 250000))
            ctxObj.put("bnfExpiry",  prefs.getString("expiry_bnf", ""))
            ctxObj.put("nfExpiry",   prefs.getString("expiry_nf", ""))
            ctxObj.put("vix",        vix)
            ctxObj.put("bnfSpot",    bnfSpot)
            ctxObj.put("nfSpot",     nfSpot)
            ctxObj.put("bnfLtpMap",  extractLtpMap(bnfChain))
            ctxObj.put("nfLtpMap",   extractLtpMap(nfChain))

            // PHASE E: Populate context with new fields
            val ist = TimeZone.getTimeZone("Asia/Kolkata")
            val cal = Calendar.getInstance(ist)
            val minsSinceOpen = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE) - 555
            ctxObj.put("mins_since_open", minsSinceOpen)
            ctxObj.put("now_ms", System.currentTimeMillis())
            ctxObj.put("today_ist", SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = ist }.format(Date()))
            
            val lastRoutine = prefs.getLong("last_routine_dispatch_ms", 0L)
            ctxObj.put("last_routine_dispatch_ms", lastRoutine)
            
            // Phase E #27 — significantMove gate (port-first from app.js L5550-5552)
            val openTradesCount = org.json.JSONArray(prefs.getString("open_trades", "[]") ?: "[]").length()
            val sigmaThreshold = if (openTradesCount > 0) 1.0 else 1.5
            
            val bnfSpotSigma = poll.optDouble("spotSigma", 0.0)
            val vixSigma = poll.optDouble("vixSigma", 0.0)
            
            val sigMove = Math.abs(bnfSpotSigma) > sigmaThreshold || Math.abs(vixSigma) > sigmaThreshold
            
            ctxObj.put("significant_move", sigMove)
            ctxObj.put("abs_spot_sigma", Math.abs(bnfSpotSigma))
            ctxObj.put("abs_vix_sigma", Math.abs(vixSigma))

            val snap2pmStr = prefs.getString("snap_2pm_today", null)
            if (snap2pmStr != null) {
                ctxObj.put("snap_2pm_today", JSONObject(snap2pmStr))
            }

            if (!ctxObj.has("ivPercentile")) ctxObj.put("ivPercentile", 50)
            
            Log.d("BRAIN_CTX_CHECK",
                "bnfChain.atm=${ctxObj.optJSONObject("bnfChain")?.opt("atm")}, " +
                "nfChain.atm=${ctxObj.optJSONObject("nfChain")?.opt("atm")}, " +
                "bnfProfile.maxPain=${ctxObj.optJSONObject("bnfProfile")?.opt("maxPain")}")
            Log.d("BRAIN_INPUT_SUMMARY",
                "polls=${JSONArray(pollsJson).length()}, " +
                "ivPercentile=${ctxObj.optDouble("ivPercentile")}, " +
                "vix=${ctxObj.optDouble("vix")}, " +
                "tradeMode=${ctxObj.optString("tradeMode")}"
            )
            
            Log.d(TAG, "BRAIN_CALLING: analyze() with ${JSONArray(pollsJson).length()} polls")
            
            // Persistence: Must save merged context back to SharedPreferences for next poll cycle
            prefs.edit().putString("context", ctxObj.toString()).apply()

            // Call brain.analyze(poll_json, trades_json, baseline_json, open_trades_json, candidates_json, strike_oi_json, context_json)
            val result = runBlocking {
                withTimeoutOrNull(10_000L) {
                    brain.callAttr("analyze",
                        pollsJson,
                        closedTradesJson,
                        baselineJson,
                        openTradesJson,
                        "[]",
                        "{}",
                        ctxObj.toString()
                    ).toString()
                }
            }
            
            if (result != null) {
                val resultObj = JSONObject(result)
                broadcastData = result
                brainSuccess = true
                
                // Phase E: Capture snapshots using Python-computed data
                captureChainSnapshots(ctxObj, py)

                // Decision #17/#18/#Issue9: Persist brain-computed P&L and metrics back to open_trades
                val posLive = resultObj.optJSONObject("position_live")
                if (posLive != null) {
                    val tradesStr = prefs.getString("open_trades", "[]") ?: "[]"
                    val trades = JSONArray(tradesStr)
                    var changed = false
                    for (i in 0 until trades.length()) {
                        val t = trades.getJSONObject(i)
                        val tid = t.optString("id")
                        val live = posLive.optJSONObject(tid)
                        if (live != null) {
                            t.put("current_pnl", live.optDouble("current_pnl"))
                            t.put("current_spot", live.optDouble("current_spot"))
                            t.put("peak_pnl", live.optDouble("peak_pnl"))
                            t.put("trough_pnl", live.optDouble("trough_pnl"))
                            t.put("peak_erosion", live.optDouble("peak_erosion"))
                            t.put("vix_change", live.optDouble("vix_change"))
                            t.put("journey", live.optJSONArray("journey"))
                            
                            val posData = resultObj.optJSONObject("positions")?.optJSONObject(tid)
                            if (posData != null) {
                                t.put("controlIndex", posData.optInt("controlIndex", 0))
                                t.put("wallDrift", posData.optJSONObject("wallDrift"))
                            }
                            changed = true
                        }
                    }
                    if (changed) {
                        prefs.edit().putString("open_trades", trades.toString()).commit()
                    }
                }

                // --- NEW Diagnostic Result Parsing ---
                try {
                    val candidateError = resultObj.optString("candidate_error", "")
                    if (candidateError.isNotEmpty()) {
                        Log.e("BRAIN_PHASE3_ERROR", "Phase 3 exception: $candidateError")
                    }
                    val generated = resultObj.optJSONArray("generated_candidates")
                    val watchlist = resultObj.optJSONArray("watchlist")
                    val actualCandidates = resultObj.optJSONObject("candidates")
                    Log.d("BRAIN_CANDIDATES_DETAIL",
                        "generated=${generated?.length() ?: 0}, " +
                        "watchlist=${watchlist?.length() ?: 0}, " +
                        "candidates_in_result=${actualCandidates?.length() ?: 0}"
                    )
                } catch(e: Exception) {
                    Log.w("BRAIN_RESULT_PARSE", "Failed to parse candidate details: ${e.message}")
                }

                // --- v2.2.6 ML Scoring Integration ---
                val generatedCands = resultObj.optJSONArray("generated_candidates")
                if (generatedCands != null && generatedCands.length() > 0 && isMLModelReady()) {
                    val py = Python.getInstance()
                    val brainMod = py.getModule("brain")
                    for (i in 0 until generatedCands.length()) {
                        val cand = generatedCands.getJSONObject(i)
                        val mlResult = scoreCandidate(cand, brainMod)
                        if (mlResult != null) {
                            cand.put("p_ml", mlResult.optDouble("p_ml"))
                            cand.put("mlAction", mlResult.optString("ml_action"))
                            cand.put("mlEdge", mlResult.optDouble("ml_edge"))
                            cand.put("mlOod", mlResult.optBoolean("ml_ood", false))
                            cand.put("mlOodConf", mlResult.optDouble("ml_ood_conf", 1.0))
                            cand.put("mlOodWarn", mlResult.optJSONArray("ml_ood_warn") ?: JSONArray())
                            cand.put("mlOodBlocked", mlResult.optBoolean("ml_ood_blocked", false))
                            cand.put("mlRegime", mlResult.optString("ml_regime", ""))
                        }
                    }
                    Log.d("BRAIN_ML_SCORING", "Scored ${generatedCands.length()} background candidates")
                }


                Log.d(TAG, "SAVING_BRAIN_RESULT: result length=${result.length}")
                
                // WS19: resultObj was mutated with ML scoring, 'result' string was NOT.
                // Save resultObj to ensure ML-scored data persists in getBrainResult()
                val finalBrainString = resultObj.toString()
                prefs.edit().putString("brain_result", finalBrainString).commit()
                
                val candidates = resultObj.optJSONArray("generated_candidates")
                    ?: resultObj.optJSONArray("candidates")
                if (candidates != null) {
                    prefs.edit().putString("candidates", candidates.toString()).commit()
                }
                Log.d(TAG, "BRAIN_COMPLETE: candidates=${candidates?.length() ?: 0}")
                
                processBrainAlerts(resultObj)
                
                // Phase E: Persist routine alert timestamp if routine alert was fired
                val alerts = resultObj.optJSONArray("alerts")
                if (alerts != null) {
                    for (i in 0 until alerts.length()) {
                        if (alerts.getJSONObject(i).optString("category") == "ROUTINE") {
                            prefs.edit().putLong("last_routine_dispatch_ms", System.currentTimeMillis()).apply()
                            break
                        }
                    }
                }
                
                brainSuccess = true
                
                // Build the data payload for syncFromNative() in WebView
                val pollCount = prefs.getInt("poll_count", 0)
                val openTradesUpdated = prefs.getString("open_trades", "[]") ?: "[]"
                val historyStr = prefs.getString("poll_history", "[]") ?: "[]"
                
                broadcastData = JSONObject().apply {
                    put("dateISO", SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US).format(Date())) // A4
                    put("spots", JSONObject().apply {
                        put("bnfSpot", bnfSpot)
                        put("nfSpot",  nfSpot)
                        put("vix",     vix)
                    })
                    put("brainResult",  resultObj) // WS19: use mutated object
                    put("candidates",   candidates ?: JSONArray())
                    put("pollCount",    pollCount)
                    put("pollHistory",  JSONArray(historyStr)) // A4
                    put("openTrades",   JSONArray(openTradesUpdated))
                }.toString()
            } else {
                Log.w(TAG, "BRAIN_TIMEOUT: brain.analyze timed out after 10s")
            }
            
        } catch (e: Exception) {
            Log.e(TAG, "BRAIN_ERROR: ${e.message}\n${e.stackTraceToString()}")
        } finally {
            // CRITICAL: Always send broadcast so WebView wakes up, even if brain threw
            val tickIntent = Intent("com.marketradar.POLL_TICK")
            broadcastData?.let { tickIntent.putExtra("data", it) }
            sendBroadcast(tickIntent)
            val pollCount = prefs.getInt("poll_count", 0)
            Log.d(TAG, "BROADCAST_SENT: Poll #$pollCount (brain success=$brainSuccess)")
        }
    }

    private fun processBrainAlerts(result: JSONObject) {
        val alerts = result.optJSONArray("alerts") ?: return
        val currentAlertKeys = mutableSetOf<String>()

        for (i in 0 until alerts.length()) {
            val alert = alerts.getJSONObject(i)
            val key = alert.optString("key")
            val priority = alert.optString("priority")
            val category = alert.optString("category")
            
            currentAlertKeys.add(key)
            
            if (!lastAlertKeys.contains(key)) {
                NotificationHelper.send(this,
                    alert.optString("title"),
                    alert.optString("body"),
                    priority,
                    if (category == "POSITION") "positions" else "main"
                )
            }
        }
        
        lastAlertKeys.clear()
        lastAlertKeys.addAll(currentAlertKeys)
    }

    private fun fetchSync(url: String, token: String): JSONObject? {
        val request = Request.Builder()
            .url(url)
            .addHeader("Authorization", "Bearer $token")
            .addHeader("Accept", "application/json")
            .build()

        return try {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    if (response.code == 401) {
                        token401Counter++
                        if (token401Counter == 3) {
                            // WS26: Only notify once on the 3rd fail to avoid spam
                            NotificationHelper.send(this, "🔑 Auth Expired", "Upstox token invalid. Open app to refresh.", "urgent")
                            stopPolling()
                        }
                    }
                    null
                } else {
                    token401Counter = 0
                    JSONObject(response.body?.string() ?: "{}")
                }
            }
        } catch (e: Exception) {
            null
        }
    }

    private val NSE_HOLIDAYS_2026 = setOf(
        "2026-01-26",  // Republic Day
        "2026-03-03",  // Maha Shivaratri
        "2026-03-26",  // Holi
        "2026-03-31",  // Id-Ul-Fitr
        "2026-04-03",  // Ram Navami
        "2026-04-14",  // Dr. Ambedkar Jayanti
        "2026-05-01",  // Maharashtra Day
        "2026-05-28",  // Buddha Purnima
        "2026-06-26",  // Bakri Id
        "2026-09-14",  // Milad-un-Nabi
        "2026-10-02",  // Mahatma Gandhi Jayanti
        "2026-10-20",  // Diwali (Laxmi Puja)
        "2026-11-10",  // Prakash Gurpurab Sri Guru Nanak Dev
        "2026-11-24",  // Guru Gobind Singh Jayanti
        "2026-12-25"   // Christmas
    )

    private fun isMarketDay(): Boolean {
        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        val today = SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = ist }.format(Date())
        return !NSE_HOLIDAYS_2026.contains(today)
    }

    private fun isMarketOpen(): Boolean {
        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        val cal = Calendar.getInstance(ist)
        val day = cal.get(Calendar.DAY_OF_WEEK)
        if (day == Calendar.SATURDAY || day == Calendar.SUNDAY) return false
        if (!isMarketDay()) return false
        val mins = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE)
        return mins in 555..930 // 9:15 AM to 3:30 PM IST
    }

    private fun createNotification(title: String, text: String): Notification {
        val intent = packageManager.getLaunchIntentForPackage(packageName)
        val pendingIntent = PendingIntent.getActivity(this, 0, intent, PendingIntent.FLAG_IMMUTABLE)
        
        val stopIntent = Intent(this, MarketWatchService::class.java).apply { action = "STOP" }
        val stopPending = PendingIntent.getService(this, 1, stopIntent, PendingIntent.FLAG_IMMUTABLE)

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_menu_manage)
            .setContentIntent(pendingIntent)
            .addAction(android.R.drawable.ic_media_pause, "Stop", stopPending)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }

    private fun updateForegroundNotification(title: String, text: String) {
        val manager = getSystemService(NotificationManager::class.java)
        manager?.notify(NOTIFICATION_ID, createNotification(title, text))
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(CHANNEL_ID, "Market Radar Background", NotificationManager.IMPORTANCE_LOW)
            getSystemService(NotificationManager::class.java)?.createNotificationChannel(channel)
        }
    }

    private fun acquirePartialWakeLock() {
        // C7: Release before acquire to prevent leak if previously held
        releaseWakeLock()
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "MarketRadar::Poll").apply { acquire(30*1000L) }
    }

    private fun releaseWakeLock() {
        wakeLock?.let { if (it.isHeld) it.release() }
    }

    private fun captureChainSnapshots(ctx: JSONObject, py: Python) {
        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        val cal = Calendar.getInstance(ist)
        val mins = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE)
        
        // 2:00 PM Window (13:45 - 14:30)
        if (mins in 825..870 && !prefs.getBoolean("has2pmSnapshot", false)) {
            Log.d(TAG, "SNAPSHOT_TRIGGER: Capturing 2pm snapshot")
            serviceScope.launch(Dispatchers.IO) {
                try {
                    val brain = py.getModule("brain")
                    val snapJson = brain.callAttr("build_chain_snapshot_data", ctx.toString()).toString()
                    val data = JSONObject(snapJson)
                    
                    if (SupabaseClient.saveChainSnapshot("2pm", data)) {
                        prefs.edit().putString("snap_2pm_today", snapJson).apply()
                        prefs.edit().putBoolean("has2pmSnapshot", true).apply()
                        Log.i(TAG, "SNAPSHOT_SAVED: 2pm snapshot synced to Supabase & Prefs")
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "SNAPSHOT_ERROR 2pm: ${e.message}")
                }
            }
        }
        
        // 3:15 PM Window (15:00 - 15:30)
        if (mins in 900..930 && !prefs.getBoolean("has315pmSnapshot", false)) {
            Log.d(TAG, "SNAPSHOT_TRIGGER: Capturing 315pm snapshot")
            serviceScope.launch(Dispatchers.IO) {
                try {
                    val brain = py.getModule("brain")
                    val snapJson = brain.callAttr("build_chain_snapshot_data", ctx.toString()).toString()
                    val data = JSONObject(snapJson)
                    
                    if (SupabaseClient.saveChainSnapshot("315pm", data)) {
                        prefs.edit().putBoolean("has315pmSnapshot", true).apply()
                        Log.i(TAG, "SNAPSHOT_SAVED: 315pm snapshot synced to Supabase")
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "SNAPSHOT_ERROR 315pm: ${e.message}")
                }
            }
        }
    }

    private fun isMLModelReady(): Boolean {
        return File(filesDir, "ml_model.json").exists()
    }

    private fun scoreCandidate(cand: JSONObject, brain: com.chaquo.python.PyObject): JSONObject? {
        return try {
            val result = brain.callAttr("ml_score_bridge", cand.toString()).toString()
            JSONObject(result)
        } catch (e: Exception) {
            null
        }
    }

    override fun onDestroy() {
        // E5: Ensure wakelock is released when service is killed
        releaseWakeLock()
        serviceScope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?) = null

    // C4: Expiry fallback helper
    private fun getNextThursday(): String {
        val cal = Calendar.getInstance()
        while (cal.get(Calendar.DAY_OF_WEEK) != Calendar.THURSDAY) {
            cal.add(Calendar.DATE, 1)
        }
        return SimpleDateFormat("yyyy-MM-dd", Locale.US).format(cal.time)
    }
}
