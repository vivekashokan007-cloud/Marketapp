package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Test

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
        assertEquals("dated_contract_table", resolved.source)
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
    fun datedHistoricalLotUsesPeriodTable() {
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
        val resolved = resolvePositionTickLotMeta(trade)
        requireNotNull(resolved)
        assertEquals(25.0, resolved.lotSize, 0.0001)
        assertEquals(true, resolved.assumed)
        assertEquals(ContractLotTable.VERSION_ID, resolved.lotTableVersion)
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
              "session_date": "2026-09-01"
            }
            """.trimIndent()
        )
        val resolved = resolvePositionTickLotMeta(trade)
        requireNotNull(resolved)
        assertEquals(130.0, resolved.lotSize, 0.0001)
        assertEquals(65.0, resolved.contractLotSize ?: -1.0, 0.0001)
        assertEquals(2.0, resolved.numberOfLots, 0.0001)
    }

}
