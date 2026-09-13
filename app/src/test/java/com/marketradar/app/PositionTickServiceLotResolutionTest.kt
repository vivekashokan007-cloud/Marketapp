package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.LocalDate

class PositionTickServiceLotResolutionTest {
    @Test
    fun explicitTradeLotSizeWins() {
        val trade = JSONObject(
            """
            {
              "index_key": "BNF",
              "strategy_type": "BEAR_CALL",
              "lot_size": 60,
              "lots": 1
            }
            """.trimIndent()
        )

        val resolved = resolvePositionTickLotMeta(trade)

        requireNotNull(resolved)
        assertEquals(60.0, resolved.lotSize, 0.0001)
        assertFalse(resolved.assumed)
        assertEquals("trade", resolved.source)
    }

    @Test
    fun contractDefaultScalesByLots() {
        val trade = JSONObject(
            """
            {
              "index_key": "BNF",
              "strategy_type": "BEAR_CALL",
              "lots": 2
            }
            """.trimIndent()
        )

        val resolved = resolvePositionTickLotMeta(trade)

        requireNotNull(resolved)
        assertEquals(60.0, resolved.lotSize, 0.0001)
        assertEquals(true, resolved.assumed)
        assertEquals("operational_current_lots", resolved.source)
    }

    @Test
    fun entrySnapshotLotSizeSurvivesRestart() {
        val trade = JSONObject(
            """
            {
              "index_key": "BNF",
              "strategy_type": "BEAR_CALL",
              "lots": 1,
              "entry_snapshot": {
                "lot_size": 60
              }
            }
            """.trimIndent()
        )

        val resolved = resolvePositionTickLotMeta(trade)

        requireNotNull(resolved)
        assertEquals(60.0, resolved.lotSize, 0.0001)
        assertFalse(resolved.assumed)
        assertEquals("entry_snapshot", resolved.source)
    }

    @Test
    fun unknownIndexReturnsNullInsteadOfOneLotFallback() {
        val trade = JSONObject(
            """
            {
              "index_key": "UNKNOWN",
              "strategy_type": "BULL_CALL",
              "lots": 1
            }
            """.trimIndent()
        )

        assertNull(resolvePositionTickLotMeta(trade))
    }

    @Test
    fun currentPnlIsRupeesNotPointsForTrade179Numbers() {
        val pnl = computePositionTickCurrentPnl(
            entryPremium = 37.9,
            executableMarkValue = 55.35,
            isCredit = false,
            lotSize = 65.0
        )

        assertEquals(1134.25, pnl, 0.0001)
    }

    @Test
    fun unsupportedHistoryAsOfOnlyFailClosed() {
        val trade = JSONObject(
            """
            {
              "index_key": "BNF",
              "strategy_type": "BEAR_CALL",
              "lots": 1,
              "session_date": "2024-06-15"
            }
            """.trimIndent()
        )
        assertNull(resolvePositionTickLotMeta(trade))
    }

    @Test
    fun missingIndexFailClosedNull() {
        val trade = JSONObject(
            """
            {
              "strategy_type": "BEAR_CALL",
              "lots": 1
            }
            """.trimIndent()
        )
        assertNull(resolvePositionTickLotMeta(trade))
    }

    @Test
    fun numberOfLotsDistinctFromContractLot() {
        val trade = JSONObject(
            """
            {
              "index_key": "NF",
              "strategy_type": "BULL_PUT",
              "lots": 2,
              "session_date": "2025-11-01",
              "expiry": "2026-01-06",
              "expiry_cycle": "weekly"
            }
            """.trimIndent()
        )
        val resolved = resolvePositionTickLotMeta(trade)
        requireNotNull(resolved)
        assertEquals(130.0, resolved.lotSize, 0.0001)
        assertEquals(65.0, resolved.contractLotSize ?: -1.0, 0.0001)
        assertEquals(2.0, resolved.numberOfLots, 0.0001)
        assertEquals("authoritative_contract_rule", resolved.source)
    }

    @Test
    fun circularAnnexureCoexistenceSameObservation() {
        val oldWeekly = ContractLotTable.resolve(
            "NF",
            asOf = LocalDate.parse("2024-12-01"),
            expiry = LocalDate.parse("2024-12-19"),
            expiryCycle = "weekly"
        )
        val newWeekly = ContractLotTable.resolve(
            "NF",
            asOf = LocalDate.parse("2024-12-01"),
            expiry = LocalDate.parse("2025-01-02"),
            expiryCycle = "weekly"
        )
        assertTrue(oldWeekly.resolved)
        assertTrue(newWeekly.resolved)
        assertEquals(25.0, oldWeekly.contractLotSize ?: -1.0, 0.0001)
        assertEquals(75.0, newWeekly.contractLotSize ?: -1.0, 0.0001)
        // Would fail under old blanket 65/30
        assertTrue(oldWeekly.contractLotSize != 65.0)
        assertTrue(newWeekly.contractLotSize != 65.0)
    }

    @Test
    fun capturedConflictFlagsUnresolved() {
        val conflict = ContractLotTable.resolve(
            "NF",
            asOf = LocalDate.parse("2025-11-01"),
            expiry = LocalDate.parse("2025-12-23"),
            expiryCycle = "weekly",
            capturedContractLot = 65.0
        )
        assertFalse(conflict.resolved)
        assertTrue(conflict.lotConflict)
        assertEquals(65.0, conflict.capturedContractLot ?: -1.0, 0.0001)
        assertEquals(75.0, conflict.ruleContractLot ?: -1.0, 0.0001)
    }
}
