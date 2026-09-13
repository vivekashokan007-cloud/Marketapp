package com.marketradar.app

import org.json.JSONObject
import java.time.LocalDate
import java.util.Locale

/**
 * Dated contract lot table — mirrored from
 * app/src/main/assets/contract_lot_table_v1.json and Python contract_lot_table.py.
 *
 * Contract lot size = units per lot. Distinct from number_of_lots (quantity).
 * Never invent BNF when index is missing — fail-closed.
 */
internal object ContractLotTable {
    const val VERSION_ID = "contract_lot_table_v1_20260913"

    data class Period(
        val asOfStart: LocalDate,
        val asOfEnd: LocalDate?, // null = open-ended current
        val lots: Map<String, Int>,
        val provenance: String,
        val provenanceQuality: String
    )

    data class ResolvedLot(
        val indexKey: String,
        val indexKnown: Boolean,
        val contractLotSize: Double?,
        val numberOfLots: Double,
        val lotSize: Double?,
        val lotSource: String,
        val lotTableVersion: String,
        val lotAsOf: String,
        val resolved: Boolean
    )

    private val aliases = mapOf(
        "BANKNIFTY" to "BNF",
        "NIFTY BANK" to "BNF",
        "NIFTY" to "NF",
        "NIFTY 50" to "NF"
    )

    // Keep periods mirrored with assets/contract_lot_table_v1.json
    private val periods = listOf(
        Period(
            asOfStart = LocalDate.parse("2025-01-01"),
            asOfEnd = null,
            lots = mapOf("BNF" to 30, "NF" to 65),
            provenance = "upstox_instrument_master_verified_20260719",
            provenanceQuality = "verified"
        ),
        Period(
            asOfStart = LocalDate.parse("2024-11-20"),
            asOfEnd = LocalDate.parse("2024-12-31"),
            lots = mapOf("BNF" to 15, "NF" to 25),
            provenance = "nse_fo_lot_revision_2024_reconstructive",
            provenanceQuality = "reconstructive"
        ),
        Period(
            asOfStart = LocalDate.parse("2000-01-01"),
            asOfEnd = LocalDate.parse("2024-11-19"),
            lots = mapOf("BNF" to 25, "NF" to 50),
            provenance = "pre_2024_nse_index_lots_reconstructive",
            provenanceQuality = "reconstructive"
        )
    )

    fun normalizeIndexKey(raw: String?): String? {
        if (raw.isNullOrBlank()) return null
        val text = raw.trim().uppercase(Locale.US)
        if (text in setOf("UNKNOWN", "NONE", "NULL")) return null
        aliases[text]?.let { return it }
        if (text == "BNF" || text == "NF") return text
        return null
    }

    fun resolve(
        indexRaw: String?,
        asOf: LocalDate? = null,
        numberOfLots: Double = 1.0
    ): ResolvedLot {
        val idx = normalizeIndexKey(indexRaw)
        val asOfDate = asOf ?: LocalDate.now()
        val asOfText = asOfDate.toString()
        val nLots = if (numberOfLots > 0.0) numberOfLots else 1.0
        if (idx == null) {
            return ResolvedLot(
                indexKey = "UNKNOWN",
                indexKnown = false,
                contractLotSize = null,
                numberOfLots = nLots,
                lotSize = null,
                lotSource = "unknown",
                lotTableVersion = VERSION_ID,
                lotAsOf = asOfText,
                resolved = false
            )
        }
        val matched = periods.firstOrNull { period ->
            !asOfDate.isBefore(period.asOfStart) &&
                (period.asOfEnd == null || !asOfDate.isAfter(period.asOfEnd))
        }
        if (matched == null) {
            return ResolvedLot(
                indexKey = idx,
                indexKnown = true,
                contractLotSize = null,
                numberOfLots = nLots,
                lotSize = null,
                lotSource = "no_period_match",
                lotTableVersion = VERSION_ID,
                lotAsOf = asOfText,
                resolved = false
            )
        }
        val contract = matched.lots[idx]?.toDouble()
        if (contract == null || contract <= 0.0) {
            return ResolvedLot(
                indexKey = idx,
                indexKnown = true,
                contractLotSize = null,
                numberOfLots = nLots,
                lotSize = null,
                lotSource = "index_missing_in_period",
                lotTableVersion = VERSION_ID,
                lotAsOf = asOfText,
                resolved = false
            )
        }
        return ResolvedLot(
            indexKey = idx,
            indexKnown = true,
            contractLotSize = contract,
            numberOfLots = nLots,
            lotSize = contract * nLots,
            lotSource = "dated_contract_table",
            lotTableVersion = VERSION_ID,
            lotAsOf = asOfText,
            resolved = true
        )
    }

    fun stampOnto(tradeMeta: JSONObject, resolved: ResolvedLot) {
        tradeMeta.put("lot_table_version", resolved.lotTableVersion)
        tradeMeta.put("lot_as_of", resolved.lotAsOf)
        tradeMeta.put("lot_source", resolved.lotSource)
        if (resolved.contractLotSize != null) {
            tradeMeta.put("contract_lot_size", resolved.contractLotSize)
        }
        tradeMeta.put("number_of_lots", resolved.numberOfLots)
        if (resolved.lotSize != null) {
            tradeMeta.put("lot_size", resolved.lotSize)
        }
    }
}
