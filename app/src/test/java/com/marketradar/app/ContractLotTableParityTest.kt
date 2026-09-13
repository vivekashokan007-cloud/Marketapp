package com.marketradar.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.LocalDate

/**
 * Parity fixtures aligned with Python tests/nse_lot_transition_fixtures_v2.json.
 * Expected lots are from NSE circular annexures, not from the table under test.
 */
class ContractLotTableParityTest {
    @Test
    fun versionIsV2() {
        assertEquals("contract_lot_table_v2_20260913", ContractLotTable.VERSION_ID)
    }

    @Test
    fun faop64625NfWeeklyCoexistence() {
        val a = ContractLotTable.resolve("NF", LocalDate.parse("2024-12-01"), expiry = LocalDate.parse("2024-12-19"), expiryCycle = "weekly")
        val b = ContractLotTable.resolve("NF", LocalDate.parse("2024-12-01"), expiry = LocalDate.parse("2025-01-02"), expiryCycle = "weekly")
        assertEquals(25, a.contractLotSize?.toInt())
        assertEquals(75, b.contractLotSize?.toInt())
    }

    @Test
    fun faop70616NfWeeklyTransition() {
        val retain = ContractLotTable.resolve("NF", LocalDate.parse("2025-11-01"), expiry = LocalDate.parse("2025-12-23"), expiryCycle = "weekly")
        val revised = ContractLotTable.resolve("NF", LocalDate.parse("2025-11-01"), expiry = LocalDate.parse("2026-01-06"), expiryCycle = "weekly")
        assertEquals(75, retain.contractLotSize?.toInt())
        assertEquals(65, revised.contractLotSize?.toInt())
    }

    @Test
    fun faop70616BnfMonthlyPresent35() {
        val retain = ContractLotTable.resolve("BNF", LocalDate.parse("2025-11-01"), expiry = LocalDate.parse("2025-12-30"), expiryCycle = "monthly")
        val revised = ContractLotTable.resolve("BNF", LocalDate.parse("2025-11-01"), expiry = LocalDate.parse("2026-01-27"), expiryCycle = "monthly")
        assertEquals(35, retain.contractLotSize?.toInt())
        assertEquals(30, revised.contractLotSize?.toInt())
    }

    @Test
    fun faop67372BnfMonthlyCoexistenceSameObservation() {
        val obs = LocalDate.parse("2025-05-02")
        val retainApr = ContractLotTable.resolve("BNF", obs, expiry = LocalDate.parse("2025-04-24"), expiryCycle = "monthly")
        val retainMay = ContractLotTable.resolve("BNF", obs, expiry = LocalDate.parse("2025-05-29"), expiryCycle = "monthly")
        val retainJun = ContractLotTable.resolve("BNF", obs, expiry = LocalDate.parse("2025-06-26"), expiryCycle = "monthly")
        val revisedJul = ContractLotTable.resolve("BNF", obs, expiry = LocalDate.parse("2025-07-31"), expiryCycle = "monthly")
        assertEquals(30, retainApr.contractLotSize?.toInt())
        assertEquals(30, retainMay.contractLotSize?.toInt())
        assertEquals(30, retainJun.contractLotSize?.toInt())
        assertEquals(35, revisedJul.contractLotSize?.toInt())
        assertTrue(revisedJul.resolved)
    }

    @Test
    fun faop67372BnfQuarterlyAndNewWeeklyRevised35() {
        val q = ContractLotTable.resolve("BNF", LocalDate.parse("2025-05-02"), expiry = LocalDate.parse("2025-09-25"), expiryCycle = "quarterly")
        val w = ContractLotTable.resolve("BNF", LocalDate.parse("2025-05-02"), expiry = LocalDate.parse("2025-05-08"), expiryCycle = "weekly")
        assertEquals(35, q.contractLotSize?.toInt())
        assertEquals(35, w.contractLotSize?.toInt())
    }

    @Test
    fun unsupportedAsOfOnlyAndHistory() {
        val asOfOnly = ContractLotTable.resolve("NF", LocalDate.parse("2025-06-01"))
        assertFalse(asOfOnly.resolved)
        assertNull(asOfOnly.contractLotSize)
        val ancient = ContractLotTable.resolve("BNF", LocalDate.parse("2000-06-01"), expiry = LocalDate.parse("2000-06-29"), expiryCycle = "monthly")
        assertFalse(ancient.resolved)
    }
}
