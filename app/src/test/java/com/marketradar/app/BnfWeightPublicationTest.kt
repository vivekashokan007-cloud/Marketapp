package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class BnfWeightPublicationTest {
    private val symbols = listOf("AUBANK","AXISBANK","BANKBARODA","CANBK","FEDERALBNK","HDFCBANK","ICICIBANK","IDFCFIRSTB","INDUSINDBK","KOTAKBANK","PNB","SBIN","UNIONBANK","YESBANK")
    private fun members() = JSONArray().also { a -> symbols.forEach { a.put(JSONObject().put("symbol", it)) } }
    private fun text() = javaClass.getResourceAsStream("/bnf_reference/official_oct2026_pdftotext.txt")!!.bufferedReader().use { it.readText() }
    @Test fun capturesAllFourteenOfficialWeightsAndPublicationDate() {
        val r = BnfWeightPublication.parse(text(), members(), "2026-11-26")
        assertEquals(14, r.getJSONArray("members").length())
        assertEquals("2026-10-30", r.getString("as_of_date"))
        assertEquals(100.0, r.getDouble("total_weight_pct"), 0.1)
        assertEquals(18.18, r.getJSONArray("members").getJSONObject(5).getDouble("weight_pct"), 0.0)
    }
    @Test(expected=IllegalStateException::class) fun incompletePublicationRejected() {
        BnfWeightPublication.parse(text().replace("YESBANK", "MISSING"), members(), "2026-11-26")
    }
    @Test(expected=IllegalArgumentException::class) fun futurePublicationRejected() {
        BnfWeightPublication.parse(text(), members(), "2026-10-01")
    }
}
