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
        prefs = getSharedPreferences("market_radar", Context.MODE_PRIVATE)
        createNotificationChannel()
        
        // Initialize Python if not already started
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == "STOP") {
            stopPolling()
            return START_NOT_STICKY
        }

        startForeground(NOTIFICATION_ID, createNotification("Service Starting", "Initializing poll loop..."))
        prefs.edit().putBoolean("service_running", true).apply()
        
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
            // Resolve futures key one-time
            resolveFuturesKey(token)
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
        val cal = Calendar.getInstance()
        cal.add(Calendar.DATE, -1)
        if (cal.get(Calendar.DAY_OF_WEEK) == Calendar.SUNDAY) cal.add(Calendar.DATE, -2)
        else if (cal.get(Calendar.DAY_OF_WEEK) == Calendar.SATURDAY) cal.add(Calendar.DATE, -1)
        return SimpleDateFormat("yyyy-MM-dd", Locale.US).format(cal.time)
    }

    private suspend fun resolveFuturesKey(token: String) {
        // Find near-month futures key for BNF and NF
        // Standard practice: Instrument list can be huge, so we guess or fetch /instrument/details
        // For simplicity and to match the plan's 'dynamic resolver', we'll look for 'FUT' in a targeted search
        // Implementation omitted for brevity in this chunk, will add in next
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
                
                // Wait for 5 minutes
                delay(5 * 60 * 1000L)
            }
        }
    }

    private fun stopPolling() {
        prefs.edit().putBoolean("service_running", false).apply()
        serviceScope.cancel()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private suspend fun performPoll(token: String) {
        Log.d(TAG, "POLL_START: performPoll() entered")
        
        val bnfExpiry = prefs.getString("expiry_bnf", "2026-04-17") ?: "2026-04-17"
        
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

        // Step 4: Update Trade P&L
        Log.d(TAG, "POLL_STEP4: Updating open trade P&L")
        updateOpenTradesPnL(bnfChainJson, bnfSpot)

        // Step 5: Build poll object and save
        Log.d(TAG, "POLL_STEP5: Saving poll object")
        val pollObj = parsePollData(quotesJson, bnfChainJson, bnfStocksJson, bnfSpot)
        savePoll(pollObj)

        // Step 6: Run Python Brain (POLL_TICK broadcast is inside runBrainAnalysis finally block)
        Log.d(TAG, "POLL_STEP6: Launching brain analysis")
        runBrainAnalysis(pollObj, bnfChainJson, nfChainJson, bnfSpot, nfSpot, vix, bnfStocksJson)
    }

    private fun getFuturesKey(symbol: String): String {
        val cal = Calendar.getInstance()
        val yy = SimpleDateFormat("yy", Locale.US).format(cal.time)
        val mmm = SimpleDateFormat("MMM", Locale.US).format(cal.time).uppercase()
        return "NSE_FO|${symbol}${yy}${mmm}FUT"
    }

    private fun updateOpenTradesPnL(chain: JSONObject, spot: Double) {
        val openTradesStr = prefs.getString("open_trades", "[]") ?: "[]"
        if (openTradesStr == "[]") return

        try {
            val openTrades = JSONArray(openTradesStr)
            val chainData = chain.getJSONArray("data")
            
            // Map strikes to prices for fast lookup
            val pCache = mutableMapOf<Double, Pair<Double, Double>>() // Strike -> [CallLTP, PutLTP]
            for (i in 0 until chainData.length()) {
                val item = chainData.getJSONObject(i)
                val strike = item.getDouble("strike_price")
                val callLtp = item.optJSONObject("call_options")?.optJSONObject("market_data")?.optDouble("last_price", 0.0) ?: 0.0
                val putLtp = item.optJSONObject("put_options")?.optJSONObject("market_data")?.optDouble("last_price", 0.0) ?: 0.0
                pCache[strike] = Pair(callLtp, putLtp)
            }

            var changed = false
            for (i in 0 until openTrades.length()) {
                val trade = openTrades.getJSONObject(i)
                val stype = trade.optString("strategy_type", "")
                val lotSize = trade.optInt("lot_size", 0)
                val entryPremium = trade.optDouble("entry_premium", 0.0)
                
                val sellS = trade.optDouble("sell_strike", 0.0)
                val buyS = trade.optDouble("buy_strike", 0.0)
                val sellS2 = trade.optDouble("sell_strike2", 0.0)
                val buyS2 = trade.optDouble("buy_strike2", 0.0)
                
                var currentNet = 0.0
                val isCredit = listOf("BEAR_CALL", "BULL_PUT", "IRON_CONDOR", "IRON_BUTTERFLY").contains(stype)
                
                if (stype == "BEAR_CALL") {
                    currentNet = (pCache[sellS]?.first ?: 0.0) - (pCache[buyS]?.first ?: 0.0)
                } else if (stype == "BULL_PUT") {
                    currentNet = (pCache[sellS]?.second ?: 0.0) - (pCache[buyS]?.second ?: 0.0)
                } else if (stype == "IRON_CONDOR" || stype == "IRON_BUTTERFLY") {
                    currentNet = ((pCache[sellS]?.first ?: 0.0) - (pCache[buyS]?.first ?: 0.0)) +
                                 ((pCache[sellS2]?.second ?: 0.0) - (pCache[buyS2]?.second ?: 0.0))
                } else if (stype == "BULL_CALL") {
                    currentNet = (pCache[buyS]?.first ?: 0.0) - (pCache[sellS]?.first ?: 0.0)
                } else if (stype == "BEAR_PUT") {
                    currentNet = (pCache[buyS]?.second ?: 0.0) - (pCache[sellS]?.second ?: 0.0)
                }

                if (currentNet != 0.0) {
                    val pnl = if (isCredit) {
                        (entryPremium - currentNet) * lotSize
                    } else {
                        (currentNet - entryPremium) * lotSize
                    }
                    
                    trade.put("current_pnl", pnl)
                    trade.put("current_spot", spot)
                    
                    val peak = trade.optDouble("peak_pnl", -999999.0)
                    if (pnl > peak) trade.put("peak_pnl", pnl)
                    
                    // GAP 13: Append journey point (HH:mm, pnl, spot)
                    val time = SimpleDateFormat("HH:mm", Locale.getDefault()).apply { timeZone = TimeZone.getTimeZone("Asia/Kolkata") }.format(Date())
                    val journey = trade.optJSONArray("journey") ?: JSONArray().apply { trade.put("journey", this) }
                    
                    // Throttle journey points: only add if last point was > 10 min ago or first point
                    val lastPoint = if (journey.length() > 0) journey.getJSONObject(journey.length() - 1) else null
                    if (lastPoint == null || minutesSince(lastPoint.getString("t"), time) >= 10) {
                        val point = JSONObject()
                        point.put("t", time)
                        point.put("pnl", pnl)
                        point.put("spot", spot)
                        journey.put(point)
                    }
                    
                    changed = true
                }
            }
            if (changed) {
                prefs.edit().putString("open_trades", openTrades.toString()).apply()
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error updating P&L: ${e.message}")
        }
    }

    private fun minutesSince(oldT: String, newT: String): Int {
        try {
            val sdf = SimpleDateFormat("HH:mm", Locale.getDefault())
            val d1 = sdf.parse(oldT)
            val d2 = sdf.parse(newT)
            return ((d2.time - d1.time) / (1000 * 60)).toInt()
        } catch (e: Exception) { return 99 }
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
            if (Math.abs(strikePrice - bnf) <= 500) { // ±10 strikes (50 pts each in BNF)
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
        val historyStr = prefs.getString("poll_history", "[]") ?: "[]"
        val history = JSONArray(historyStr)
        
        // Append new poll
        history.put(poll)
        
        // Keep last 100
        val trimmed = JSONArray()
        val start = if (history.length() > 100) history.length() - 100 else 0
        for (i in start until history.length()) {
            trimmed.put(history.get(i))
        }

        val pollCount = trimmed.length()

        prefs.edit().apply {
            putString("latest_poll", poll.toString())
            putString("poll_history", trimmed.toString())
            putString("last_poll_time", poll.getString("t"))
            putInt("poll_count", pollCount)
        }.apply()
        
        // GAP 12: Institutional Positioning
        checkInstitutionalPositioning(poll)

        // GAP 6: Upsert to Supabase every 3rd poll
        if (pollCount > 0 && pollCount % 3 == 0) {
            serviceScope.launch(Dispatchers.IO) {
                val today = SimpleDateFormat("yyyy-MM-dd", Locale.getDefault()).format(Date())
                SupabaseClient.upsertPollHistory(today, trimmed)
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
        if (mins in 915..930) {
            if (!prefs.contains("tomorrow_signal") && prefs.contains("afternoon_baseline")) {
                computeTomorrowSignal(poll)
            }
        }
    }

    private fun computeChainProfile(chain: JSONObject, spot: Double, vix: Double): JSONObject {
        val profile = JSONObject()
        try {
            val data = chain.optJSONArray("data") ?: return profile
            val sigma = spot * (vix / 100.0) / Math.sqrt(252.0)
            
            var totalGamma = 0.0
            var atmGamma = 0.0
            var ceVolAtm = 0.0
            var peVolAtm = 0.0
            
            val strikes = mutableListOf<Double>()
            val callOI = mutableMapOf<Double, Double>()
            val putOI = mutableMapOf<Double, Double>()
            val callIV = mutableMapOf<Double, Double>()
            val putIV = mutableMapOf<Double, Double>()
            val callVol = mutableMapOf<Double, Double>()
            val putVol = mutableMapOf<Double, Double>()

            for (i in 0 until data.length()) {
                val item = data.getJSONObject(i)
                val strike = item.getDouble("strike_price")
                strikes.add(strike)
                
                val call = item.optJSONObject("call_options")
                val put = item.optJSONObject("put_options")
                
                val cOI = call?.optJSONObject("market_data")?.optDouble("oi", 0.0) ?: 0.0
                val pOI = put?.optJSONObject("market_data")?.optDouble("oi", 0.0) ?: 0.0
                val cIV = call?.optJSONObject("market_data")?.optDouble("iv", 0.0) ?: 0.0
                val pIV = put?.optJSONObject("market_data")?.optDouble("iv", 0.0) ?: 0.0
                val cV = call?.optJSONObject("market_data")?.optDouble("volume", 0.0) ?: 0.0
                val pV = put?.optJSONObject("market_data")?.optDouble("volume", 0.0) ?: 0.0
                val gamma = call?.optJSONObject("option_greeks")?.optDouble("gamma", 0.0) ?: 0.0
                
                callOI[strike] = cOI
                putOI[strike] = pOI
                callIV[strike] = cIV
                putIV[strike] = pIV
                callVol[strike] = cV
                putVol[strike] = pV
                
                totalGamma += gamma
                if (Math.abs(strike - spot) <= 0.3 * sigma) {
                    atmGamma += gamma
                }
                if (Math.abs(strike - spot) <= 1.5 * sigma) {
                    ceVolAtm += cV
                    peVolAtm += pV
                }
            }
            
            // 7 Metrics
            // 1. ivSlope: putIV(-1s) - callIV(+1s)
            val sNeg1 = strikes.minByOrNull { Math.abs(it - (spot - sigma)) } ?: 0.0
            val sPos1 = strikes.minByOrNull { Math.abs(it - (spot + sigma)) } ?: 0.0
            profile.put("ivSlope", (putIV[sNeg1] ?: 0.0) - (callIV[sPos1] ?: 0.0))
            
            // 2. gammaCluster
            profile.put("gammaCluster", if (totalGamma > 0) atmGamma / totalGamma else 0.0)
            
            // 3. volRatio
            profile.put("volRatio", if (peVolAtm > 0) ceVolAtm / peVolAtm else 1.0)
            
            // 4/5. Freshness
            // Note: poll object is not available here, so we skip poll-dependent freshness
            
            // 6/7. Depth (Top 3 OI in 1.5s range)
            val nearStrikes = strikes.filter { Math.abs(it - spot) <= 1.5 * sigma }
            val top3C = nearStrikes.map { callOI[it] ?: 0.0 }.sortedDescending().take(3).sum()
            val totalC = nearStrikes.sumOf { callOI[it] ?: 0.0 }
            val top3P = nearStrikes.map { putOI[it] ?: 0.0 }.sortedDescending().take(3).sum()
            val totalP = nearStrikes.sumOf { putOI[it] ?: 0.0 }
            
            profile.put("callClusterDepth", if (totalC > 0) top3C / totalC * 5 else 0.0)
            profile.put("putClusterDepth", if (totalP > 0) top3P / totalP * 5 else 0.0)
            
            // Other expected fields
            profile.put("spot", spot)
            profile.put("vix", vix)
            
        } catch (e: Exception) {
            Log.e(TAG, "Error computing chain profile: ${e.message}")
        }
        return profile
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

    private fun computeTomorrowSignal(current: JSONObject) {
        try {
            val baseline = JSONObject(prefs.getString("afternoon_baseline", "{}"))
            val cOI0 = baseline.optDouble("totalCOI", 0.0)
            val pOI0 = baseline.optDouble("totalPOI", 0.0)
            val cOI1 = current.optDouble("totalCOI", 0.0)
            val pOI1 = current.optDouble("totalPOI", 0.0)

            if (cOI0 == 0.0 || pOI0 == 0.0) return

            val dCall = cOI1 - cOI0
            val dPut = pOI1 - pOI0
            
            val direction = if (dCall > dPut * 1.5) "BEARISH" else if (dPut > dCall * 1.5) "BULLISH" else "NEUTRAL"
            val strength = if (Math.abs(dCall - dPut) > (cOI0 + pOI0) * 0.05) "4/5" else "2/5"
            val reason = if (direction == "BEARISH") "heavy call writing" else if (direction == "BULLISH") "heavy put writing" else "balanced positioning"

            val signal = "Tomorrow: $direction ($strength) — $reason"
            prefs.edit().putString("tomorrow_signal", signal).apply()
            
            NotificationHelper.send(this, "📊 Institutional Positioning", signal, "info")
            Log.d(TAG, "Computed tomorrow signal: $signal")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to compute tomorrow signal: ${e.message}")
        }
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
                    put("bid", md?.optDouble("bid_price", 0.0) ?: 0.0)
                    put("ask", md?.optDouble("ask_price", 0.0) ?: 0.0)
                    put("oi", md?.optDouble("oi", 0.0) ?: 0.0)
                    put("volume", md?.optDouble("volume", 0.0) ?: 0.0)
                    put("iv", md?.optDouble("iv", 0.0) ?: 0.0)
                    
                    val gr = call?.optJSONObject("option_greeks")
                    put("delta", gr?.optDouble("delta", 0.0) ?: 0.0)
                    put("gamma", gr?.optDouble("gamma", 0.0) ?: 0.0)
                    put("theta", gr?.optDouble("theta", 0.0) ?: 0.0)
                })
                strikeObj.put("PE", JSONObject().apply {
                    val md = put?.optJSONObject("market_data")
                    put("ltp", md?.optDouble("last_price", 0.0) ?: 0.0)
                    put("bid", md?.optDouble("bid_price", 0.0) ?: 0.0)
                    put("ask", md?.optDouble("ask_price", 0.0) ?: 0.0)
                    put("oi", md?.optDouble("oi", 0.0) ?: 0.0)
                    put("volume", md?.optDouble("volume", 0.0) ?: 0.0)
                    put("iv", md?.optDouble("iv", 0.0) ?: 0.0)
                    
                    val gr = put?.optJSONObject("option_greeks")
                    put("delta", gr?.optDouble("delta", 0.0) ?: 0.0)
                    put("gamma", gr?.optDouble("gamma", 0.0) ?: 0.0)
                    put("theta", gr?.optDouble("theta", 0.0) ?: 0.0)
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
            Log.d(TAG, "BRAIN_START: Loading Chaquopy brain module")
            val py = Python.getInstance()
            val brain = py.getModule("brain")
            
            val pollsJson    = prefs.getString("poll_history",      "[]") ?: "[]"
            val baselineJson = prefs.getString("morning_baseline",  "{}") ?: "{}"
            val openTradesJson   = prefs.getString("open_trades",   "[]") ?: "[]"
            val closedTradesJson = prefs.getString("closed_trades", "[]") ?: "[]"
            val contextJson  = prefs.getString("context",           "{}") ?: "{}"
            
            // profile merge strategy (Fix 2 - 3-way merge)
            val ctxObj = JSONObject(contextJson)
            
            fun mergeProfile(key: String, spot: Double, v: Double) {
                val profile = ctxObj.optJSONObject(key)
                if (profile != null && profile.length() > 2) {
                    // Case 1 & 2: Rich profile (new or stored) — update live spot/vix
                    profile.put("spot", spot)
                    profile.put("vix", v)
                } else {
                    // Case 3: No rich data — remove to avoid stub confusion
                    ctxObj.remove(key)
                }
            }
            mergeProfile("bnfProfile", bnfSpot, vix)
            mergeProfile("nfProfile", nfSpot, vix)

            // CHAIN MERGING (Fix 1 - Preservation)
            fun mergeChain(key: String, liveChainRaw: JSONObject, spot: Double, 
                           cwPoll: Double, pwPoll: Double, pcrPoll: Double) {
                val formattedLive = formatChainForBrain(liveChainRaw, spot)
                val existingChain = ctxObj.optJSONObject(key)
                
                if (existingChain != null && existingChain.has("atm")) {
                    // Rich chain exists — refresh only live intraday fields
                    existingChain.put("strikes",         formattedLive.optJSONObject("strikes"))
                    existingChain.put("atm",             formattedLive.optDouble("atm", 0.0))
                    existingChain.put("callWallStrike",  cwPoll)
                    existingChain.put("putWallStrike",   pwPoll)
                    // allStrikes, atmIv, maxPain kept from WebView
                } else {
                    // No rich data — use full formatted live chain
                    formattedLive.put("callWallStrike", cwPoll)
                    formattedLive.put("putWallStrike",  pwPoll)
                    formattedLive.put("pcr",            pcrPoll)
                    ctxObj.put(key, formattedLive)
                }
            }
            
            // Calculate NF walls since they aren't in the main 'poll' object
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
            mergeChain("nfChain",  nfChain,  nfSpot,  nfCw, nfPw, 0.0)
            
            // GAP 15: Institutional Positioning Snapshots
            captureChainSnapshots(ctxObj.optJSONObject("bnfProfile"), ctxObj.optJSONObject("nfProfile"))

            // Data Parity Overlays
            val breadth = calculateBreadth(stocksJson)
            ctxObj.put("bnfBreadth", breadth)
            
            val ohlcStr = prefs.getString("yesterday_ohlc", null)
            val gapObj = JSONObject()
            if (ohlcStr != null) {
                val ohlc = JSONObject(ohlcStr)
                val calculatedGap = computeGapObject(ohlc, bnfSpot)
                gapObj.put("type", calculatedGap.optString("type", "FLAT"))
                gapObj.put("pct", calculatedGap.optDouble("pct", 0.0))
                gapObj.put("sigma", calculatedGap.optDouble("sigma", 0.0))
                
                // closeChar inside morningBias (if bias exists)
                val mb = ctxObj.optJSONObject("morningBias")
                if (mb != null) {
                    val bnfOhlc = ohlc.optJSONObject("data")?.optJSONObject("NSE_INDEX:Nifty Bank")?.optJSONObject("ohlc")
                    if (bnfOhlc != null) {
                        val c = bnfOhlc.getDouble("close")
                        val h = bnfOhlc.getDouble("high")
                        val l = bnfOhlc.getDouble("low")
                        val closeChar = if (c > h - (h-l)*0.2) 2 else if (c < l + (h-l)*0.2) -2 else 0
                        mb.put("closeChar", closeChar)
                    }
                }
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
            if (!ctxObj.has("ivPercentile")) ctxObj.put("ivPercentile", 50)
            
            Log.d("BRAIN_CTX_CHECK",
                "bnfChain.atm=${ctxObj.optJSONObject("bnfChain")?.opt("atm")}, " +
                "nfChain.atm=${ctxObj.optJSONObject("nfChain")?.opt("atm")}, " +
                "bnfProfile.maxPain=${ctxObj.optJSONObject("bnfProfile")?.opt("maxPain")}, " +
                "nfProfile.maxPain=${ctxObj.optJSONObject("nfProfile")?.opt("maxPain")}, " +
                "tradeMode=${ctxObj.optString("tradeMode")}, " +
                "ivPctl=${ctxObj.optDouble("ivPercentile")}, " +
                "bias=${ctxObj.optJSONObject("morningBias")?.optString("label")}, " +
                "biasNet=${ctxObj.optJSONObject("morningBias")?.optInt("net")}, " +
                "bnfExpiry=${ctxObj.optString("bnfExpiry")}, " +
                "nfExpiry=${ctxObj.optString("nfExpiry")}"
            )
            
            Log.d("BRAIN_INPUT_SUMMARY",
                "polls=${JSONArray(pollsJson).length()}, " +
                "morningBias=${ctxObj.optJSONObject("morningBias")?.toString()}, " +
                "ivPercentile=${ctxObj.optDouble("ivPercentile")}, " +
                "vix=${ctxObj.optDouble("vix")}, " +
                "tradeMode=${ctxObj.optString("tradeMode")}, " +
                "bnfChain_strikes_count=${ctxObj.optJSONObject("bnfChain")?.optJSONObject("strikes")?.length() ?: 0}, " +
                "bnfExpiry=${ctxObj.optString("bnfExpiry")}"
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
            
            if (result == null) {
                Log.w(TAG, "BRAIN_TIMEOUT: brain.analyze timed out after 10s")
                return
            }
            
            val resultObj = JSONObject(result)
            
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
                    }
                }
                Log.d("BRAIN_ML_SCORING", "Scored ${generatedCands.length()} background candidates")
            }

            Log.d(TAG, "SAVING_BRAIN_RESULT: result length=${result.length}, starts_with=${if (result.length > 20) result.substring(0, 20) else result}")
            prefs.edit().putString("brain_result", result).apply()
            
            val candidates = resultObj.optJSONArray("generated_candidates")
                ?: resultObj.optJSONArray("candidates")
            if (candidates != null) {
                prefs.edit().putString("candidates", candidates.toString()).apply()
            }
            Log.d(TAG, "BRAIN_COMPLETE: candidates=${candidates?.length() ?: 0}")
            
            processBrainAlerts(resultObj)
            brainSuccess = true
            
            // Build the data payload for syncFromNative() in WebView
            val pollCount = prefs.getInt("poll_count", 0)
            val openTradesUpdated = prefs.getString("open_trades", "[]") ?: "[]"
            broadcastData = JSONObject().apply {
                put("spots", JSONObject().apply {
                    put("bnfSpot", bnfSpot)
                    put("nfSpot",  nfSpot)
                    put("vix",     vix)
                })
                put("brainResult",  resultObj)
                put("candidates",   candidates ?: JSONArray())
                put("pollCount",    pollCount)
                put("openTrades",   JSONArray(openTradesUpdated))
            }.toString()
            
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
        val currentAlertKeys = mutableSetOf<String>()

        // 1. Check Main Verdict
        val verdict = result.optJSONObject("verdict")
        if (verdict != null) {
            val action = verdict.optString("action", "WAIT")
            val confidence = verdict.optInt("confidence", 0)
            if (action != "WAIT" && confidence >= 50) {
                val alertKey = "VERDICT_$action"
                currentAlertKeys.add(alertKey)
                if (!lastAlertKeys.contains(alertKey)) {
                    NotificationHelper.send(this, 
                        "Trade Signal: $action", 
                        "${verdict.optString("strategy")} @ $confidence% conf: ${verdict.optString("reasoning")}", 
                        "urgent")
                }
            }
        }

        // 2. Check Position Verdicts (High Priority)
        val positions = result.optJSONObject("positions")
        if (positions != null) {
            val keys = positions.keys()
            while (keys.hasNext()) {
                val tid = keys.next()
                val posData = positions.getJSONObject(tid)
                val pVerdict = posData.optJSONObject("verdict")
                if (pVerdict != null) {
                    val pAction = pVerdict.optString("action", "HOLD")
                    val pReason = pVerdict.optString("reason", "")
                    if (pAction == "EXIT" || pAction == "BOOK") {
                        val alertKey = "POS_${tid}_$pAction"
                        currentAlertKeys.add(alertKey)
                        if (!lastAlertKeys.contains(alertKey)) {
                            NotificationHelper.send(this, 
                                "$pAction Signal ($tid)", 
                                pReason, 
                                "urgent",
                                "positions")
                        }
                    }
                }
            }
        }

        // 3. High Strength Market/Risk Insights
        val sections = listOf("market", "risk", "timing")
        for (section in sections) {
            val insights = result.optJSONArray(section)
            if (insights != null) {
                for (i in 0 until insights.length()) {
                    val ins = insights.getJSONObject(i)
                    val strength = ins.optInt("strength", 0)
                    if (strength >= 4) {
                        val label = ins.optString("label")
                        val alertKey = "INSIGHT_${section}_${label}"
                        currentAlertKeys.add(alertKey)
                        
                        if (!lastAlertKeys.contains(alertKey)) {
                            var detail = ins.optString("detail")
                            if (label.contains("FII") && section == "market") {
                                try {
                                    val contextJson = prefs.getString("context", "{}") ?: "{}"
                                    val ctxObj = JSONObject(contextJson)
                                    val fiiHistory = ctxObj.optJSONArray("fiiHistory")
                                    if (fiiHistory != null && fiiHistory.length() > 0) {
                                        val todayFii = fiiHistory.getJSONObject(0).optDouble("fiiCash", 0.0)
                                        detail = "Today FII Cash: ₹${todayFii.toInt()}Cr"
                                    }
                                } catch (e: Exception) {
                                    Log.d(TAG, "FII lookup failed (skipping): ${e.message}")
                                }
                            }
                            
                            NotificationHelper.send(this, 
                                "${ins.optString("icon")} ${ins.optString("label")}", 
                                detail, 
                                if (ins.optString("impact") == "caution") "urgent" else "info"
                            )
                        }
                    }
                }
            }
        }

        // 4. Candidate Opportunities (Summary)
        val candidates = result.optJSONArray("generated_candidates") ?: result.optJSONArray("candidates")
        if (candidates != null && candidates.length() > 0) {
            val best = mutableListOf<String>()
            for (i in 0 until candidates.length()) {
                val c = candidates.getJSONObject(i)
                val forces = c.optJSONObject("forces")
                val aligned = forces?.optInt("aligned", 0) ?: 0
                val prob = c.optDouble("probProfit", 0.0)
                
                // Alert on high-confidence candidates (aligned >= 3, prob >= 70%)
                if (aligned >= 3 && prob >= 0.70) {
                    val index = c.optString("index", "BNF")
                    val type = c.optString("type", "")
                    best.add("$index $type $aligned/3")
                }
            }
            
            if (best.isNotEmpty()) {
                val alertKey = "CANDIDATES_${best.joinToString("-")}"
                currentAlertKeys.add(alertKey)
                if (!lastAlertKeys.contains(alertKey)) {
                    val title = "🎯 ${best.size} High-Confidence Opportunities"
                    // Join top 3 into the body for readability
                    val body = "${best.take(3).joinToString(", ")}${if (best.size > 3) " and more" else ""} — open app to review."
                    NotificationHelper.send(this, title, body, "important")
                }
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
                        if (token401Counter >= 3) {
                            NotificationHelper.send(this, "🔑 Auth Expired", "3 failed attempts. Open app to refresh Upstox token.", "urgent")
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
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "MarketRadar::Poll").apply { acquire(10*1000L) }
    }

    private fun releaseWakeLock() {
        wakeLock?.let { if (it.isHeld) it.release() }
    }

    private fun captureChainSnapshots(bnf: JSONObject?, nf: JSONObject?) {
        val ist = TimeZone.getTimeZone("Asia/Kolkata")
        val cal = Calendar.getInstance(ist)
        val mins = cal.get(Calendar.HOUR_OF_DAY) * 60 + cal.get(Calendar.MINUTE)
        
        // 2:00 PM Window (13:45 - 14:30)
        if (mins in 825..870 && !prefs.getBoolean("has2pmSnapshot", false)) {
            Log.d(TAG, "SNAPSHOT_TRIGGER: Capturing 2pm snapshot")
            serviceScope.launch(Dispatchers.IO) {
                val data = JSONObject().apply {
                    put("bnf", bnf)
                    put("nf",  nf)
                }
                if (SupabaseClient.saveChainSnapshot("2pm", data)) {
                    prefs.edit().putBoolean("has2pmSnapshot", true).apply()
                    Log.i(TAG, "SNAPSHOT_SAVED: 2pm snapshot synced to Supabase")
                }
            }
        }
        
        // 3:15 PM Window (15:00 - 15:30)
        if (mins in 900..930 && !prefs.getBoolean("has315pmSnapshot", false)) {
            Log.d(TAG, "SNAPSHOT_TRIGGER: Capturing 315pm snapshot")
            serviceScope.launch(Dispatchers.IO) {
                val data = JSONObject().apply {
                    put("bnf", bnf)
                    put("nf",  nf)
                }
                if (SupabaseClient.saveChainSnapshot("315pm", data)) {
                    prefs.edit().putBoolean("has315pmSnapshot", true).apply()
                    Log.i(TAG, "SNAPSHOT_SAVED: 315pm snapshot synced to Supabase")
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
        serviceScope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?) = null
}
