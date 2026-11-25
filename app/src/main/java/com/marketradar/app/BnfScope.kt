package com.marketradar.app

import org.json.JSONObject

/** Active product scope. Never used to filter existing positions or historical evidence. */
object BnfScope {
    const val RETIRED = "INDEX_RETIRED_BNF_ONLY"
    val indices = listOf("BNF")
    fun permitsNew(index: String?) = index?.trim()?.uppercase() == "BNF"
    fun stripNiftyFields(value: JSONObject): JSONObject {
        val keys = value.keys().asSequence().toList()
        for (key in keys) if (key.lowercase().startsWith("nf") || key == "candle_nf") value.remove(key)
        return value
    }
}
