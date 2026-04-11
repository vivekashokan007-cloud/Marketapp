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

class MarketWatchService : Service() {

    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var wakeLock: PowerManager.WakeLock? = null
    private lateinit var prefs: SharedPreferences
    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()
    
    private var token401Counter = 0

    companion object {
        const val CHANNEL_ID = "market_radar_service"
        const val NOTIFICATION_ID = 1001
        const val TAG = "MarketWatchService"
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

                // 4. Poll History (today)
                val today = SimpleDateFormat("yyyy-MM-dd", Locale.getDefault()).format(Date())
                val history = SupabaseClient.getPollHistory(today)
                if (history.length() > 0) {
                    prefs.edit().putString("poll_history", history.toString()).apply()
                }

                prefs.edit().putLong("last_bootstrap_time", now).apply()
                Log.d(TAG, "Bootstrap complete")
            } catch (e: Exception) {
                Log.e(TAG, "Bootstrap failed: ${e.message}")
            }
        }
    }

    private fun startPolling() {
        serviceScope.launch {
            while (isActive) {
                if (isMarketOpen()) {
                    acquirePartialWakeLock()
                    try {
                        performPoll()
                    } catch (e: Exception) {
                        Log.e(TAG, "Poll failed: ${e.message}")
                    } finally {
                        releaseWakeLock()
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

    private suspend fun performPoll() {
        val token = prefs.getString("auth_token", null) ?: return
        val bnfExpiry = prefs.getString("expiry_bnf", "2026-04-17") ?: "2026-04-17"
        
        // GAP 3: Expiry Rollover check
        val sdfUTC = SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = TimeZone.getTimeZone("Asia/Kolkata") }
        val today = sdfUTC.format(Date())
        
        if (bnfExpiry < today) {
            Log.w(TAG, "Expiry passed: $bnfExpiry < $today. Skipping chain poll.")
            NotificationHelper.send(this, "⚠️ Expiry passed", "Poll skipped. Open app to refresh expiry dates.", "info")
            return
        }

        updateForegroundNotification("Polling Market", "Fetching Quotes...")

        // 1. Fetch Spot Prices
        val quotesUrl = "https://api.upstox.com/v2/market-quote/quotes?instrument_key=NSE_INDEX|Nifty Bank,NSE_INDEX|Nifty 50,NSE_INDEX|India VIX"
        val quotesJson = fetchSync(quotesUrl, token) ?: return
        
        // 2. Fetch Option Chains
        val nfExpiry = prefs.getString("expiry_nf", bnfExpiry) ?: bnfExpiry
        val bnfUrl = "https://api.upstox.com/v2/option/chain?instrument_key=NSE_INDEX|Nifty Bank&expiry_date=$bnfExpiry"
        val nfUrl = "https://api.upstox.com/v2/option/chain?instrument_key=NSE_INDEX|Nifty 50&expiry_date=$nfExpiry"
        
        val bnfChainJson = fetchSync(bnfUrl, token) ?: return
        val nfChainJson = fetchSync(nfUrl, token) ?: return

        val data = quotesJson.getJSONObject("data")
        val bnfSpot = data.getJSONObject("NSE_INDEX:Nifty Bank").getDouble("last_price")
        val nfSpot = data.getJSONObject("NSE_INDEX:Nifty 50").getDouble("last_price")
        val vix = data.getJSONObject("NSE_INDEX:India VIX").getDouble("last_price")

        // 3. Update Trade P&L (BNF only for now)
        updateOpenTradesPnL(bnfChainJson, bnfSpot)

        // 4. Process into Poll Object
        val pollObj = parsePollData(quotesJson, bnfChainJson)
        savePoll(pollObj)

        // 5. Run Python Brain
        runBrainAnalysis(pollObj, bnfChainJson, nfChainJson)
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

    private fun parsePollData(quotes: JSONObject, chain: JSONObject): JSONObject {
        val data = quotes.getJSONObject("data")
        val bnf = data.getJSONObject("NSE_INDEX:Nifty Bank").getDouble("last_price")
        val nf = data.getJSONObject("NSE_INDEX:Nifty 50").getDouble("last_price")
        val vix = data.getJSONObject("NSE_INDEX:India VIX").getDouble("last_price")
        
        val time = SimpleDateFormat("HH:mm", Locale.getDefault()).apply { timeZone = TimeZone.getTimeZone("Asia/Kolkata") }.format(Date())
        
        var maxCallOi = -1.0
        var maxPutOi = -1.0
        var cw = 0.0
        var pw = 0.0
        var totalCOI = 0.0
        var totalPOI = 0.0
        
        val chainData = chain.getJSONArray("data")
        for (i in 0 until chainData.length()) {
            val item = chainData.getJSONObject(i)
            val strikePrice = item.getDouble("strike_price")
            
            item.optJSONObject("call_options")?.optJSONObject("market_data")?.let { md ->
                val oi = md.optDouble("oi", 0.0)
                totalCOI += oi
                if (oi > maxCallOi) {
                    maxCallOi = oi
                    cw = strikePrice
                }
            }
            
            item.optJSONObject("put_options")?.optJSONObject("market_data")?.let { md ->
                val oi = md.optDouble("oi", 0.0)
                totalPOI += oi
                if (oi > maxPutOi) {
                    maxPutOi = oi
                    pw = strikePrice
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
        poll.put("totalCOI", totalCOI)
        poll.put("totalPOI", totalPOI)
        poll.put("pcr", if (totalPOI > 0) totalCOI / totalPOI else 0.0)
        
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
            
            for (i in 0 until data.length()) {
                val item = data.getJSONObject(i)
                val strike = item.getDouble("strike_price")
                allStrikesArr.put(strike)
                
                val call = item.optJSONObject("call_options")
                val put = item.optJSONObject("put_options")
                
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
            result.put("atm", strikesObj.keys().asSequence().map { it.toDouble() }.minByOrNull { Math.abs(it - spot) } ?: spot)
            
        } catch (e: Exception) {
            Log.e(TAG, "Error formatting chain for brain: ${e.message}")
        }
        return result
    }

    private fun runBrainAnalysis(poll: JSONObject, bnfChain: JSONObject, nfChain: JSONObject) {
        try {
            val py = Python.getInstance()
            val brain = py.getModule("brain")
            
            val pollsJson = prefs.getString("poll_history", "[]") ?: "[]"
            val baselineJson = prefs.getString("morning_baseline", "{}") ?: "{}"
            val openTradesJson = prefs.getString("open_trades", "[]") ?: "[]"
            val closedTradesJson = prefs.getString("closed_trades", "[]") ?: "[]"
            val contextJson = prefs.getString("context", "{}") ?: "{}"
            
            val bnfSpot = poll.optDouble("bnf", 0.0)
            val nfSpot = poll.optDouble("nf", 0.0)
            val vix = poll.optDouble("vix", 18.0)
            
            val bnfProfile = computeChainProfile(bnfChain, bnfSpot, vix)
            val bnfChainFormatted = formatChainForBrain(bnfChain, bnfSpot).apply {
                put("callWallStrike", poll.optDouble("cw", 0.0))
                put("putWallStrike", poll.optDouble("pw", 0.0))
                put("pcr", poll.optDouble("pcr", 0.0))
            }
            
            val nfChainFormatted = formatChainForBrain(nfChain, nfSpot)
            
            // Inject into context
            val ctxObj = JSONObject(contextJson)
            ctxObj.put("bnfProfile", bnfProfile)
            ctxObj.put("bnfChain", bnfChainFormatted)
            ctxObj.put("nfChain", nfChainFormatted)
            ctxObj.put("capital", prefs.getInt("capital", 250000))
            
            // Phase 3: Dynamic context components
            ctxObj.put("bnfExpiry", prefs.getString("expiry_bnf", ""))
            ctxObj.put("nfExpiry", prefs.getString("expiry_nf", ""))
            if (!ctxObj.has("ivPercentile")) {
                ctxObj.put("ivPercentile", 50) // Default per Claude's recommendation
            }
            
            val updatedContext = ctxObj.toString()
            
            // Call full brain.analyze(poll_json, trades_json, baseline_json, open_trades_json, candidates_json, strike_oi_json, context_json)
            val result = brain.callAttr("analyze", 
                pollsJson, 
                closedTradesJson, 
                baselineJson, 
                openTradesJson, 
                "[]",
                "{}",
                updatedContext
            ).toString()
            
            val resultObj = JSONObject(result)
            prefs.edit().putString("brain_result", result).apply()
            
            // Store candidates for UI (Phase 3 key is 'generated_candidates')
            val candidates = resultObj.optJSONArray("generated_candidates") ?: resultObj.optJSONArray("candidates")
            if (candidates != null) {
                prefs.edit().putString("candidates", candidates.toString()).apply()
            }
            
            processBrainAlerts(resultObj)
            
            // Phase 4: Notify active UI to sync from NativeBridge
            sendBroadcast(Intent("com.marketradar.POLL_TICK"))
            
        } catch (e: Exception) {
            Log.e(TAG, "Brain error: ${e.message}")
        }
    }

    private fun processBrainAlerts(result: JSONObject) {
        // 1. Check Main Verdict
        val verdict = result.optJSONObject("verdict")
        if (verdict != null) {
            val action = verdict.optString("action", "WAIT")
            val confidence = verdict.optInt("confidence", 0)
            if (action != "WAIT" && confidence >= 50) {
                NotificationHelper.send(this, 
                    "Trade Signal: $action", 
                    "${verdict.optString("strategy")} @ $confidence% conf: ${verdict.optString("reasoning")}", 
                    "urgent")
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
                        NotificationHelper.send(this, 
                            "$pAction Signal ($tid)", 
                            pReason, 
                            "urgent",
                            "positions")
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
                        NotificationHelper.send(this, 
                            "${ins.optString("icon")} ${ins.optString("label")}", 
                            ins.optString("detail"), 
                            if (ins.optString("impact") == "caution") "urgent" else "info"
                        )
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
                
                // Alert on high-confidence candidates (aligned ≥ 3, prob ≥ 70%)
                if (aligned >= 3 && prob >= 0.70) {
                    val index = c.optString("index", "BNF")
                    val type = c.optString("type", "")
                    best.add("$index $type $aligned/3")
                }
            }
            
            if (best.isNotEmpty()) {
                val title = "🎯 ${best.size} High-Confidence Opportunities"
                // Join top 3 into the body for readability
                val body = "${best.take(3).joinToString(", ")}${if (best.size > 3) " and more" else ""} — open app to review."
                NotificationHelper.send(this, title, body, "important")
            }
        }
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
        "2026-01-26", "2026-02-27", "2026-03-10", "2026-03-17", "2026-03-31",
        "2026-04-14", "2026-04-18", "2026-05-01", "2026-06-26", "2026-07-07",
        "2026-08-15", "2026-08-27", "2026-10-02", "2026-10-21", "2026-11-05"
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
        return mins in 555..935 // 9:15 AM to 3:35 PM IST
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

    override fun onDestroy() {
        serviceScope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?) = null
}
