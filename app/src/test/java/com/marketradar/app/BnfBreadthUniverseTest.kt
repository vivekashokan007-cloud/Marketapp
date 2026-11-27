package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.nio.file.Files

class BnfBreadthUniverseTest {
    private val symbols = listOf("AUBANK", "AXISBANK", "BANKBARODA", "CANBK", "FEDERALBNK", "HDFCBANK",
        "ICICIBANK", "IDFCFIRSTB", "INDUSINDBK", "KOTAKBANK", "PNB", "SBIN", "UNIONBANK", "YESBANK")

    @Test fun liveUniverseUsesEveryOfficialMemberAndUpdatedPublicationWeights() {
        val root = Files.createTempDirectory("bnf-live-breadth").toFile()
        try {
            val dir = root.resolve("bnf_reference_v1").apply { mkdirs() }
            val members = JSONArray()
            symbols.forEachIndexed { i, symbol ->
                val isin = "IN${(i + 1).toString().padStart(10, '0')}"
                members.put(JSONObject().put("symbol", symbol).put("isin", isin).put("instrument_key", "NSE_EQ|$isin"))
            }
            dir.resolve("2026-11-27.json").writeText(JSONObject().put("members", members).toString())
            val publication = javaClass.getResourceAsStream("/bnf_reference/official_oct2026_pdftotext.txt")!!
                .bufferedReader().use { it.readText() }
            val weights = BnfWeightPublication.parse(publication, members, "2026-11-27")
            dir.resolve("weights_latest.json").writeText(weights.toString())
            val first = BnfBreadthUniverse.load(root, "2026-11-27")
            assertEquals(14, first.size)
            assertEquals(symbols, first.map { it.symbol })
            assertEquals(1.0, first.sumOf { it.weight }, 0.001)
            val hdfc = first.single { it.symbol == "HDFCBANK" }
            assertEquals(0.1818, hdfc.weight, 0.00001)

            // A new validated publication must change the live input without an app update.
            val newer = JSONObject(weights.toString())
            newer.getJSONArray("members").getJSONObject(5).put("weight_pct", 19.18)
            newer.getJSONArray("members").getJSONObject(6).put("weight_pct",
                newer.getJSONArray("members").getJSONObject(6).getDouble("weight_pct") - 1.0)
            dir.resolve("weights_latest.json").writeText(newer.toString())
            assertEquals(0.1918, BnfBreadthUniverse.load(root, "2026-11-27")
                .single { it.symbol == "HDFCBANK" }.weight, 0.00001)
        } finally { root.deleteRecursively() }
    }

    @Test fun changedMembershipOrMissingWeightsCannotSilentlyUseFiveHardcodedBanks() {
        val root = Files.createTempDirectory("bnf-live-breadth-missing").toFile()
        try {
            assertThrows(Exception::class.java) { BnfBreadthUniverse.load(root, "2026-11-27") }
            val dir = root.resolve("bnf_reference_v1").apply { mkdirs() }
            dir.resolve("2026-11-27.json").writeText(JSONObject().put("members", JSONArray()).toString())
            dir.resolve("weights_latest.json").writeText(JSONObject().put("schema", "bnf_monthly_weights_v1")
                .put("status", "VALIDATED").put("as_of_date", "2026-10-30")
                .put("members", JSONArray()).toString())
            assertThrows(IllegalArgumentException::class.java) { BnfBreadthUniverse.load(root, "2026-11-27") }
        } finally { root.deleteRecursively() }
    }
}
