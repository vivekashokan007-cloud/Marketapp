package com.marketradar.app

import org.json.JSONObject
import java.time.LocalDate
import java.util.Locale

/**
 * Contract-specific lot table — mirrored from
 * app/src/main/assets/contract_lot_table_v1.json and Python contract_lot_table.py.
 *
 * Contract lot size = units per lot. Distinct from number_of_lots (quantity)
 * and quantity_units (contract_lot_size * number_of_lots).
 * Never invent BNF when index is missing — fail-closed.
 * (index, as_of)-only is insufficient where contracts coexist.
 */
internal object ContractLotTable {
    const val VERSION_ID = "contract_lot_table_v2_20260913"

    data class ContractRule(
        val ruleId: String,
        val index: String,
        val expiryCycle: String?,
        val expiryOnOrAfter: LocalDate?,
        val expiryOnOrBefore: LocalDate?,
        val observationOnOrAfter: LocalDate?,
        val observationOnOrBefore: LocalDate?,
        val contractLotSize: Int,
        val sourceId: String
    )

    data class ResolvedLot(
        val indexKey: String,
        val indexKnown: Boolean,
        val contractLotSize: Double?,
        val numberOfLots: Double,
        val lotSize: Double?,
        val lotSource: String,
        val lotTableVersion: String,
        val lotAsOf: String?,
        val resolved: Boolean,
        val lotConflict: Boolean = false,
        val unavailableReason: String? = null,
        val matchedRuleId: String? = null,
        val capturedContractLot: Double? = null,
        val ruleContractLot: Double? = null,
        val expiry: String? = null,
        val expiryCycle: String? = null
    )

    private val aliases = mapOf(
        "BANKNIFTY" to "BNF",
        "NIFTY BANK" to "BNF",
        "NIFTY" to "NF",
        "NIFTY 50" to "NF"
    )

    private val cycleAliases = mapOf(
        "WEEKLY" to "weekly",
        "W" to "weekly",
        "MONTHLY" to "monthly",
        "M" to "monthly",
        "QUARTERLY" to "quarterly",
        "Q" to "quarterly",
        "HALF_YEARLY" to "quarterly_half_yearly",
        "HALF-YEARLY" to "quarterly_half_yearly",
        "HY" to "quarterly_half_yearly",
        "QUARTERLY_HALF_YEARLY" to "quarterly_half_yearly",
        "QH" to "quarterly_half_yearly"
    )

    private val operationalCurrent = mapOf("BNF" to 30, "NF" to 65)

    // Keep rules mirrored with assets/contract_lot_table_v1.json (authoritative only).
    private val rules = listOf(
        ContractRule("FAOP64625_NF_weekly_existing", "NF", "weekly", LocalDate.parse("2024-11-01"), LocalDate.parse("2024-12-19"), LocalDate.parse("2024-10-18"), null, 25, "NSE_FAOP_64625"),
        ContractRule("FAOP64625_NF_weekly_revised_until_70616", "NF", "weekly", LocalDate.parse("2025-01-02"), LocalDate.parse("2025-12-23"), LocalDate.parse("2024-11-20"), null, 75, "NSE_FAOP_64625"),
        ContractRule("FAOP70616_NF_weekly_revised", "NF", "weekly", LocalDate.parse("2026-01-06"), null, LocalDate.parse("2025-10-28"), null, 65, "NSE_FAOP_70616"),
        ContractRule("FAOP64625_NF_monthly_existing", "NF", "monthly", LocalDate.parse("2024-11-01"), LocalDate.parse("2025-01-30"), LocalDate.parse("2024-10-18"), null, 25, "NSE_FAOP_64625"),
        ContractRule("FAOP64625_NF_monthly_revised_until_70616", "NF", "monthly", LocalDate.parse("2025-02-27"), LocalDate.parse("2025-12-30"), LocalDate.parse("2024-11-20"), null, 75, "NSE_FAOP_64625"),
        ContractRule("FAOP70616_NF_monthly_revised", "NF", "monthly", LocalDate.parse("2026-01-27"), null, LocalDate.parse("2025-10-28"), null, 65, "NSE_FAOP_70616"),
        ContractRule("FAOP64625_BNF_monthly_existing", "BNF", "monthly", LocalDate.parse("2024-11-01"), LocalDate.parse("2025-01-29"), LocalDate.parse("2024-10-18"), null, 15, "NSE_FAOP_64625"),
        ContractRule("FAOP70616_BNF_monthly_existing_present35", "BNF", "monthly", LocalDate.parse("2025-10-28"), LocalDate.parse("2025-12-30"), LocalDate.parse("2025-10-03"), null, 35, "NSE_FAOP_70616"),
        ContractRule("FAOP70616_BNF_monthly_revised", "BNF", "monthly", LocalDate.parse("2026-01-27"), null, LocalDate.parse("2025-10-28"), null, 30, "NSE_FAOP_70616"),
        ContractRule("FAOP70616_BNF_weekly_existing_present35", "BNF", "weekly", LocalDate.parse("2025-10-28"), LocalDate.parse("2025-12-23"), LocalDate.parse("2025-10-03"), null, 35, "NSE_FAOP_70616"),
        ContractRule("FAOP70616_BNF_weekly_revised", "BNF", "weekly", LocalDate.parse("2026-01-06"), null, LocalDate.parse("2025-10-28"), null, 30, "NSE_FAOP_70616")
    )

    fun normalizeIndexKey(raw: String?): String? {
        if (raw.isNullOrBlank()) return null
        val text = raw.trim().uppercase(Locale.US)
        if (text in setOf("UNKNOWN", "NONE", "NULL")) return null
        aliases[text]?.let { return it }
        if (text == "BNF" || text == "NF") return text
        return null
    }

    fun normalizeExpiryCycle(raw: String?): String? {
        if (raw.isNullOrBlank()) return null
        val text = raw.trim().uppercase(Locale.US).replace(' ', '_').replace('-', '_')
        cycleAliases[text]?.let { return it }
        val lower = text.lowercase(Locale.US)
        if (lower in setOf("weekly", "monthly", "quarterly", "quarterly_half_yearly")) return lower
        return null
    }

    private fun inBounds(value: LocalDate?, onOrAfter: LocalDate?, onOrBefore: LocalDate?): Boolean {
        if (value == null) return false
        if (onOrAfter != null && value.isBefore(onOrAfter)) return false
        if (onOrBefore != null && value.isAfter(onOrBefore)) return false
        return true
    }

    private fun ruleMatches(
        rule: ContractRule,
        index: String,
        expiry: LocalDate?,
        cycle: String?,
        observation: LocalDate?
    ): Boolean {
        if (rule.index != index) return false
        if (rule.expiryCycle != null) {
            if (cycle == null) return false
            if (rule.expiryCycle != cycle) {
                val q = setOf("quarterly", "quarterly_half_yearly")
                if (!(rule.expiryCycle in q && cycle in q)) return false
            }
        }
        if (!inBounds(expiry, rule.expiryOnOrAfter, rule.expiryOnOrBefore)) return false
        if (rule.observationOnOrAfter != null || rule.observationOnOrBefore != null) {
            if (observation == null) return false
            if (!inBounds(observation, rule.observationOnOrAfter, rule.observationOnOrBefore)) return false
        }
        return true
    }

    fun resolve(
        indexRaw: String?,
        asOf: LocalDate? = null,
        numberOfLots: Double = 1.0,
        expiry: LocalDate? = null,
        expiryCycle: String? = null,
        capturedContractLot: Double? = null,
        allowOperationalCurrent: Boolean = false
    ): ResolvedLot {
        val idx = normalizeIndexKey(indexRaw)
        val asOfText = asOf?.toString()
        val nLots = if (numberOfLots > 0.0) numberOfLots else 1.0
        val cycle = normalizeExpiryCycle(expiryCycle)
        val captured = if (capturedContractLot != null && capturedContractLot > 0.0) capturedContractLot else null

        if (idx == null) {
            return ResolvedLot(
                indexKey = "UNKNOWN",
                indexKnown = false,
                contractLotSize = null,
                numberOfLots = nLots,
                lotSize = null,
                lotSource = "unknown_index",
                lotTableVersion = VERSION_ID,
                lotAsOf = asOfText,
                resolved = false,
                unavailableReason = "unknown_index",
                expiry = expiry?.toString(),
                expiryCycle = cycle
            )
        }

        var matched = rules.filter { ruleMatches(it, idx, expiry, cycle, asOf) }
        if (matched.isEmpty() && expiry != null && cycle == null) {
            val byLot = linkedMapOf<Int, MutableList<ContractRule>>()
            for (c in listOf("weekly", "monthly", "quarterly", "quarterly_half_yearly")) {
                for (r in rules.filter { ruleMatches(it, idx, expiry, c, asOf) }) {
                    byLot.getOrPut(r.contractLotSize) { mutableListOf() }.add(r)
                }
            }
            if (byLot.size == 1) {
                matched = byLot.values.first()
            } else if (byLot.size > 1) {
                return ResolvedLot(
                    indexKey = idx,
                    indexKnown = true,
                    contractLotSize = null,
                    numberOfLots = nLots,
                    lotSize = null,
                    lotSource = "ambiguous_expiry_cycle_coexistence",
                    lotTableVersion = VERSION_ID,
                    lotAsOf = asOfText,
                    resolved = false,
                    unavailableReason = "ambiguous_expiry_cycle_coexistence",
                    expiry = expiry.toString(),
                    expiryCycle = null
                )
            }
        }

        // Instrument-master snapshot scope (near 2026-07-19, expiry within 90d)
        var snapLot: Int? = null
        var snapId: String? = null
        if (expiry != null && asOf != null) {
            val snapAsOf = LocalDate.parse("2026-07-19")
            if (kotlin.math.abs(asOf.toEpochDay() - snapAsOf.toEpochDay()) <= 14L &&
                kotlin.math.abs(expiry.toEpochDay() - snapAsOf.toEpochDay()) <= 90L
            ) {
                snapLot = operationalCurrent[idx]
                snapId = "upstox_20260719_near90d"
            }
        }

        val authLot: Int?
        val ruleId: String?
        if (matched.isNotEmpty()) {
            val lots = matched.map { it.contractLotSize }.toSet()
            if (lots.size > 1) {
                return ResolvedLot(
                    indexKey = idx,
                    indexKnown = true,
                    contractLotSize = null,
                    numberOfLots = nLots,
                    lotSize = null,
                    lotSource = "conflicting_authoritative_rules",
                    lotTableVersion = VERSION_ID,
                    lotAsOf = asOfText,
                    resolved = false,
                    unavailableReason = "conflicting_authoritative_rules",
                    expiry = expiry?.toString(),
                    expiryCycle = cycle
                )
            }
            authLot = matched.first().contractLotSize
            ruleId = matched.first().ruleId
        } else if (snapLot != null) {
            authLot = snapLot
            ruleId = snapId
        } else {
            authLot = null
            ruleId = null
        }

        if (captured != null) {
            if (authLot != null && captured.toInt() != authLot) {
                return ResolvedLot(
                    indexKey = idx,
                    indexKnown = true,
                    contractLotSize = null,
                    numberOfLots = nLots,
                    lotSize = null,
                    lotSource = "captured_vs_rule_conflict",
                    lotTableVersion = VERSION_ID,
                    lotAsOf = asOfText,
                    resolved = false,
                    lotConflict = true,
                    unavailableReason = "captured_vs_rule_conflict",
                    matchedRuleId = ruleId,
                    capturedContractLot = captured,
                    ruleContractLot = authLot.toDouble(),
                    expiry = expiry?.toString(),
                    expiryCycle = cycle
                )
            }
            return ResolvedLot(
                indexKey = idx,
                indexKnown = true,
                contractLotSize = captured,
                numberOfLots = nLots,
                lotSize = captured * nLots,
                lotSource = if (authLot != null) "captured_metadata_consistent" else "captured_metadata",
                lotTableVersion = VERSION_ID,
                lotAsOf = asOfText,
                resolved = true,
                matchedRuleId = ruleId,
                capturedContractLot = captured,
                expiry = expiry?.toString(),
                expiryCycle = cycle
            )
        }

        if (authLot != null) {
            val c = authLot.toDouble()
            return ResolvedLot(
                indexKey = idx,
                indexKnown = true,
                contractLotSize = c,
                numberOfLots = nLots,
                lotSize = c * nLots,
                lotSource = "authoritative_contract_rule",
                lotTableVersion = VERSION_ID,
                lotAsOf = asOfText,
                resolved = true,
                matchedRuleId = ruleId,
                expiry = expiry?.toString(),
                expiryCycle = cycle
            )
        }

        if (allowOperationalCurrent && asOf == null && expiry == null) {
            val ops = operationalCurrent[idx]
            if (ops != null) {
                val c = ops.toDouble()
                return ResolvedLot(
                    indexKey = idx,
                    indexKnown = true,
                    contractLotSize = c,
                    numberOfLots = nLots,
                    lotSize = c * nLots,
                    lotSource = "operational_current_lots",
                    lotTableVersion = VERSION_ID,
                    lotAsOf = null,
                    resolved = true,
                    expiry = null,
                    expiryCycle = cycle
                )
            }
        }

        val reason = when {
            expiry == null && asOf != null -> "as_of_only_insufficient_coexistence"
            else -> "unsupported_or_ambiguous_contract"
        }
        return ResolvedLot(
            indexKey = idx,
            indexKnown = true,
            contractLotSize = null,
            numberOfLots = nLots,
            lotSize = null,
            lotSource = reason,
            lotTableVersion = VERSION_ID,
            lotAsOf = asOfText,
            resolved = false,
            unavailableReason = reason,
            expiry = expiry?.toString(),
            expiryCycle = cycle
        )
    }

    fun stampOnto(tradeMeta: JSONObject, resolved: ResolvedLot) {
        tradeMeta.put("lot_table_version", resolved.lotTableVersion)
        if (resolved.lotAsOf != null) tradeMeta.put("lot_as_of", resolved.lotAsOf)
        tradeMeta.put("lot_source", resolved.lotSource)
        if (resolved.contractLotSize != null) {
            tradeMeta.put("contract_lot_size", resolved.contractLotSize)
        }
        tradeMeta.put("number_of_lots", resolved.numberOfLots)
        if (resolved.lotSize != null) {
            tradeMeta.put("lot_size", resolved.lotSize)
            tradeMeta.put("quantity_units", resolved.lotSize)
        }
        if (resolved.lotConflict) tradeMeta.put("lot_conflict", true)
        if (resolved.unavailableReason != null) {
            tradeMeta.put("lot_unavailable_reason", resolved.unavailableReason)
        }
        if (resolved.matchedRuleId != null) tradeMeta.put("matched_rule_id", resolved.matchedRuleId)
        if (resolved.expiry != null) tradeMeta.put("expiry", resolved.expiry)
        if (resolved.expiryCycle != null) tradeMeta.put("expiry_cycle", resolved.expiryCycle)
    }
}
