package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject

/**
 * Pure formatter: raw Upstox /v2/option/chain response -> brain chain shape.
 * Extracted verbatim from MarketWatchService.formatChainForBrain (b510 R1) so the exact native
 * adapter can be exercised in JVM tests. Adds only documented row identity per leg
 * (`underlying_key`, `contract_expiry`); `trading_symbol` is still copied when present but is not
 * part of the documented response.
 */
object OptionChainBrainFormatter {
    fun format(chain: JSONObject, spot: Double, onError: (String) -> Unit = {}): JSONObject {
        val result = JSONObject()
        try {
            val data = chain.optJSONArray("data") ?: return result
            val strikesObj = JSONObject()
            val allStrikesArr = JSONArray()
            val oiRows = mutableListOf<DoubleArray>()

            var atmStrike = 0.0
            var atmMinDist = Double.MAX_VALUE
            var atmCEIv = 0.0
            var atmPEIv = 0.0
            var totalCallOI = 0.0
            var totalPutOI = 0.0
            var maxCallOI = 0.0
            var maxPutOI = 0.0
            var callWallStrike = 0.0
            var putWallStrike = 0.0

            for (i in 0 until data.length()) {
                val item = data.getJSONObject(i)
                val strike = item.getDouble("strike_price")
                allStrikesArr.put(strike)

                val call = item.optJSONObject("call_options")
                val put = item.optJSONObject("put_options")
                val callMd = call?.optJSONObject("market_data")
                val putMd = put?.optJSONObject("market_data")
                val callInstrumentKey = call?.optString("instrument_key", "")?.trim().orEmpty()
                val putInstrumentKey = put?.optString("instrument_key", "")?.trim().orEmpty()
                val callSymbol = call?.optString("trading_symbol", "")?.trim().orEmpty()
                val putSymbol = put?.optString("trading_symbol", "")?.trim().orEmpty()
                // b510 R1: documented /v2/option/chain row identity (underlying_key, expiry), copied
                // verbatim onto each leg so contract identity never depends on an undocumented symbol.
                val rowUnderlyingKey = item.optString("underlying_key", "").trim()
                val rowExpiry = item.optString("expiry", "").trim()
                val callOI = callMd?.optDouble("oi", 0.0) ?: 0.0
                val putOI = putMd?.optDouble("oi", 0.0) ?: 0.0
                totalCallOI += callOI
                totalPutOI += putOI
                oiRows.add(doubleArrayOf(strike, callOI, putOI))
                if (callOI > maxCallOI) {
                    maxCallOI = callOI
                    callWallStrike = strike
                }
                if (putOI > maxPutOI) {
                    maxPutOI = putOI
                    putWallStrike = strike
                }

                val dist = Math.abs(strike - spot)
                if (dist < atmMinDist) {
                    atmMinDist = dist
                    atmStrike = strike
                    atmCEIv = optionIv(call, callMd)
                    atmPEIv = optionIv(put, putMd)
                }

                val strikeObj = JSONObject()
                strikeObj.put("CE", JSONObject().apply {
                    val md = callMd
                    val bid = optionQuote(md, "bid_price")
                    val ask = optionQuote(md, "ask_price")
                    put("ltp", optionLtp(md))
                    putNullableFiniteNumber("bid", bid)
                    putNullableFiniteNumber("ask", ask)
                    put("mid", optionMid(md))
                    put("oi", callOI)
                    put("volume", md?.optDouble("volume", 0.0) ?: 0.0)
                    put("iv", optionIv(call, md))
                    put("prev_oi", md?.optDouble("prev_oi", 0.0) ?: 0.0)  // PHASE C STEP 7.0
                    put("instrument_key", callInstrumentKey)
                    put("instrumentKey", callInstrumentKey)
                    put("trading_symbol", callSymbol)
                    put("symbol", callSymbol)
                    put("underlying_key", rowUnderlyingKey)
                    put("contract_expiry", rowExpiry)

                    val gr = call?.optJSONObject("option_greeks")
                    put("delta", gr?.optDouble("delta", 0.0) ?: 0.0)
                    put("gamma", gr?.optDouble("gamma", 0.0) ?: 0.0)
                    put("theta", gr?.optDouble("theta", 0.0) ?: 0.0)
                    put("vega", gr?.optDouble("vega", 0.0) ?: 0.0)        // PHASE C STEP 7.0
                    put("pop", gr?.optDouble("pop", 0.0) ?: 0.0)          // PHASE C STEP 7.0
                })
                strikeObj.put("PE", JSONObject().apply {
                    val md = putMd
                    val bid = optionQuote(md, "bid_price")
                    val ask = optionQuote(md, "ask_price")
                    put("ltp", optionLtp(md))
                    putNullableFiniteNumber("bid", bid)
                    putNullableFiniteNumber("ask", ask)
                    put("mid", optionMid(md))
                    put("oi", putOI)
                    put("volume", md?.optDouble("volume", 0.0) ?: 0.0)
                    put("iv", optionIv(put, md))
                    put("prev_oi", md?.optDouble("prev_oi", 0.0) ?: 0.0)  // PHASE C STEP 7.0
                    put("instrument_key", putInstrumentKey)
                    put("instrumentKey", putInstrumentKey)
                    put("trading_symbol", putSymbol)
                    put("symbol", putSymbol)
                    put("underlying_key", rowUnderlyingKey)
                    put("contract_expiry", rowExpiry)

                    val gr = put?.optJSONObject("option_greeks")
                    put("delta", gr?.optDouble("delta", 0.0) ?: 0.0)
                    put("gamma", gr?.optDouble("gamma", 0.0) ?: 0.0)
                    put("theta", gr?.optDouble("theta", 0.0) ?: 0.0)
                    put("vega", gr?.optDouble("vega", 0.0) ?: 0.0)        // PHASE C STEP 7.0
                    put("pop", gr?.optDouble("pop", 0.0) ?: 0.0)          // PHASE C STEP 7.0
                })
                strikesObj.put(strike.toInt().toString(), strikeObj)
            }

            var nearTotalCallOI = 0.0
            var nearTotalPutOI = 0.0
            val nearWindow = Math.max(spot * 0.015, 1.0)
            for (row in oiRows) {
                if (Math.abs(row[0] - atmStrike) <= nearWindow) {
                    nearTotalCallOI += row[1]
                    nearTotalPutOI += row[2]
                }
            }

            var maxPain = 0.0
            var minPain = Double.MAX_VALUE
            for (candidate in oiRows) {
                var pain = 0.0
                for (row in oiRows) {
                    pain += row[1] * Math.max(candidate[0] - row[0], 0.0)
                    pain += row[2] * Math.max(row[0] - candidate[0], 0.0)
                }
                if (pain < minPain) {
                    minPain = pain
                    maxPain = candidate[0]
                }
            }

            result.put("strikes", strikesObj)
            result.put("allStrikes", allStrikesArr)
            result.put("atm", atmStrike)
            result.put("atmIv", if (atmCEIv > 0 && atmPEIv > 0) (atmCEIv + atmPEIv) / 2.0 else Math.max(atmCEIv, atmPEIv))
            result.put("totalCallOI", totalCallOI)
            result.put("totalPutOI", totalPutOI)
            result.put("nearTotalCallOI", nearTotalCallOI)
            result.put("nearTotalPutOI", nearTotalPutOI)
            result.put("pcr", if (totalCallOI > 0.0) totalPutOI / totalCallOI else 0.0)
            result.put("nearAtmPCR", if (nearTotalCallOI > 0.0) nearTotalPutOI / nearTotalCallOI else 0.0)
            result.put("maxPain", maxPain)
            result.put("callWallStrike", callWallStrike)
            result.put("callWallOI", maxCallOI)
            result.put("putWallStrike", putWallStrike)
            result.put("putWallOI", maxPutOI)

        } catch (e: Exception) {
            onError("Error formatting chain for brain: ${e.message}")
        }
        return result
    }

    /** The request this chain answered (same values as the option/chain URL); used by mergeChain. */
    fun stampRequest(formatted: JSONObject, expiry: String, requestedUnderlyingKey: String): JSONObject {
        formatted.put("expiry", expiry)
        formatted.put("requested_underlying_key", requestedUnderlyingKey)
        formatted.put("requested_expiry", expiry)
        return formatted
    }

    fun optionLtp(md: JSONObject?): Double {
        if (md == null) return 0.0
        val ltp = md.optDouble("ltp", 0.0)
        return if (ltp > 0.0) ltp else md.optDouble("last_price", 0.0)
    }

    fun optionQuote(md: JSONObject?, key: String): Double? {
        if (md == null || !md.has(key) || md.isNull(key)) return null
        val value = when (val raw = md.opt(key)) {
            is Number -> raw.toDouble()
            is String -> raw.trim().toDoubleOrNull()
            else -> null
        }
        return value?.takeIf { it.isFinite() && it > 0.0 }
    }

    fun optionMid(md: JSONObject?): Double {
        if (md == null) return 0.0
        val bid = optionQuote(md, "bid_price")
        val ask = optionQuote(md, "ask_price")
        return if (bid != null && ask != null) {
            (bid + ask) / 2.0
        } else {
            val ltp = optionLtp(md)
            if (ltp > 0.0) ltp else Math.max(bid ?: 0.0, ask ?: 0.0)
        }
    }

    fun optionIv(option: JSONObject?, md: JSONObject?): Double {
        val greekIv = option?.optJSONObject("option_greeks")?.optDouble("iv", 0.0) ?: 0.0
        return if (greekIv > 0.0) greekIv else (md?.optDouble("iv", 0.0) ?: 0.0)
    }
}

private fun JSONObject.putNullableFiniteNumber(name: String, value: Double?) {
    if (value == null || !value.isFinite()) {
        put(name, JSONObject.NULL)
    } else {
        put(name, value)
    }
}
