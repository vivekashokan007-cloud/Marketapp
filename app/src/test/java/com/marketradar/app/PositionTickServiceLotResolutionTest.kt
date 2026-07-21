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
        assertEquals("contract_default", resolved.source)
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
}
