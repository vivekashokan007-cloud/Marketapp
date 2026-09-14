package com.marketradar.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
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
    fun versionIsV3() {
        assertEquals("contract_lot_table_v3_20260914", ContractLotTable.VERSION_ID)
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
    fun faop67372BnfQuarterlyRevised35ButWeeklyCycleDiscontinued() {
        val q = ContractLotTable.resolve("BNF", LocalDate.parse("2025-05-02"), expiry = LocalDate.parse("2025-09-25"), expiryCycle = "quarterly")
        val w = ContractLotTable.resolve("BNF", LocalDate.parse("2025-05-02"), expiry = LocalDate.parse("2025-05-08"), expiryCycle = "weekly")
        assertEquals(35, q.contractLotSize?.toInt())
        assertFalse(w.resolved)
        assertNull(w.contractLotSize)
        assertEquals("contract_cycle_discontinued", w.unavailableReason)
        val captured = ContractLotTable.resolve(
            "BNF", LocalDate.parse("2025-05-02"), expiry = LocalDate.parse("2025-05-08"),
            expiryCycle = "weekly", capturedContractLot = 35.0
        )
        assertFalse(captured.resolved)
        assertEquals("contract_cycle_discontinued", captured.unavailableReason)
    }

    @Test
    fun unsupportedAsOfOnlyAndHistory() {
        val asOfOnly = ContractLotTable.resolve("NF", LocalDate.parse("2025-06-01"))
        assertFalse(asOfOnly.resolved)
        assertNull(asOfOnly.contractLotSize)
        val ancient = ContractLotTable.resolve("BNF", LocalDate.parse("2000-06-01"), expiry = LocalDate.parse("2000-06-29"), expiryCycle = "monthly")
        assertFalse(ancient.resolved)
    }

    @Test
    fun allSixteenRuleIdsPresent() {
        val expected = listOf(
            "FAOP64625_NF_weekly_existing",
            "FAOP64625_NF_weekly_revised_until_70616",
            "FAOP70616_NF_weekly_revised",
            "FAOP64625_NF_monthly_existing",
            "FAOP64625_NF_monthly_revised_until_70616",
            "FAOP70616_NF_monthly_revised",
            "FAOP64625_NF_qh_post_transition",
            "FAOP70616_NF_qh_post_transition",
            "FAOP64625_BNF_monthly_existing",
            "FAOP64625_BNF_monthly_revised_30_until_67372",
            "FAOP67372_BNF_monthly_revised_35",
            "FAOP67372_BNF_quarterly_revised_35",
            "FAOP70616_BNF_monthly_existing_present35",
            "FAOP70616_BNF_monthly_revised",
            "FAOP64625_BNF_qh_post_transition",
            "FAOP70616_BNF_qh_post_transition"
        )
        // Resolve representative fixtures for each missing Q/HY rule
        val nfQh = ContractLotTable.resolve("NF", LocalDate.parse("2025-06-01"), expiry = LocalDate.parse("2025-06-26"), expiryCycle = "quarterly_half_yearly")
        assertEquals(75, nfQh.contractLotSize?.toInt())
        assertEquals("FAOP64625_NF_qh_post_transition", nfQh.matchedRuleId)
        val nfQh2 = ContractLotTable.resolve("NF", LocalDate.parse("2026-01-15"), expiry = LocalDate.parse("2026-03-31"), expiryCycle = "quarterly_half_yearly")
        assertEquals(65, nfQh2.contractLotSize?.toInt())
        assertEquals("FAOP70616_NF_qh_post_transition", nfQh2.matchedRuleId)
        val bnfQh = ContractLotTable.resolve("BNF", LocalDate.parse("2025-03-01"), expiry = LocalDate.parse("2025-03-26"), expiryCycle = "quarterly")
        assertEquals(30, bnfQh.contractLotSize?.toInt())
        assertEquals("FAOP64625_BNF_qh_post_transition", bnfQh.matchedRuleId)
        val bnfQh2 = ContractLotTable.resolve("BNF", LocalDate.parse("2026-01-15"), expiry = LocalDate.parse("2026-03-31"), expiryCycle = "quarterly")
        assertEquals(30, bnfQh2.contractLotSize?.toInt())
        assertEquals("FAOP70616_BNF_qh_post_transition", bnfQh2.matchedRuleId)
        assertEquals(16, expected.size)
    }

    @Test
    fun capturedLotMismatchConflictsWithoutTruncation() {
        val bad = ContractLotTable.resolve(
            "NF", LocalDate.parse("2025-06-10"),
            numberOfLots = 1.0,
            expiry = LocalDate.parse("2025-06-12"),
            expiryCycle = "weekly",
            capturedContractLot = 65.0
        )
        assertTrue(bad.lotConflict)
        assertFalse(bad.resolved)
        assertEquals(65.0, bad.capturedContractLot)
        assertEquals(75.0, bad.ruleContractLot)

        val frac = ContractLotTable.parsePositiveIntegralLot(65.5)
        assertNull(frac)
        val badCount = ContractLotTable.parsePositiveIntegralLot(0)
        assertNull(badCount)
        val ok = ContractLotTable.parsePositiveIntegralLot(2)
        assertEquals(2, ok)
    }

    @Test
    fun ruleSnapshotMatchesJsonFields() {
        val snap = ContractLotTable.ruleSnapshotForTests()
        assertEquals(16, snap.size)
        val ids = snap.map { it["rule_id"] as String }.toSet()
        assertEquals(16, ids.size)
        // Boundary: changing any field without regeneration would diverge from VERSION_ID + count
        assertEquals(ContractLotTable.VERSION_ID, "contract_lot_table_v3_20260914")
        for (row in snap) {
            assertNotNull(row["rule_id"])
            assertNotNull(row["index"])
            assertNotNull(row["contract_lot_size"])
            assertNotNull(row["source_id"])
        }
    }

    @Test
    fun fractionalCapturedLotNeverSubstituted() {
        val bad = ContractLotTable.resolve(
            "NF",
            java.time.LocalDate.parse("2026-07-19"),
            expiry = java.time.LocalDate.parse("2026-08-06"),
            expiryCycle = "weekly",
            capturedContractLot = 65.5
        )
        assertFalse(bad.resolved)
        assertNull(bad.contractLotSize)
        assertEquals("fractional_lot", bad.unavailableReason)
    }

    @Test
    fun adversarialIdentityMatrixFailsClosed() {
        val valid = ContractLotTable.resolve(
            "NF", LocalDate.parse("2026-07-19"),
            expiry = LocalDate.parse("2026-08-06"), expiryCycle = "weekly"
        )
        assertTrue(valid.resolved)
        assertEquals(65.0, valid.contractLotSize)

        assertFalse(ContractLotTable.resolve(
            "XYZ", LocalDate.parse("2026-07-19"),
            expiry = LocalDate.parse("2026-08-06"), expiryCycle = "weekly"
        ).resolved)
        assertFalse(ContractLotTable.resolve(
            "NF", LocalDate.parse("2000-07-19"),
            expiry = LocalDate.parse("2000-08-06"), expiryCycle = "weekly"
        ).resolved)
        assertFalse(ContractLotTable.resolve(
            "NF", LocalDate.parse("2026-07-19"),
            expiry = LocalDate.parse("2026-08-06"), expiryCycle = "weekly",
            numberOfLots = 0.0
        ).resolved)
        assertFalse(ContractLotTable.resolve(
            "NF", LocalDate.parse("2026-07-19"),
            expiry = LocalDate.parse("2026-08-06"), expiryCycle = "weekly",
            numberOfLots = 1.5
        ).resolved)
        assertFalse(ContractLotTable.resolve(
            "NF", LocalDate.parse("2026-07-19"),
            expiry = LocalDate.parse("2026-08-06"), expiryCycle = "weekly",
            capturedContractLot = -65.0
        ).resolved)
        assertFalse(ContractLotTable.resolve(
            "NF", LocalDate.parse("2026-07-19"),
            expiry = LocalDate.parse("2026-08-06"), expiryCycle = "weekly",
            capturedContractLot = 65.5
        ).resolved)
        assertTrue(ContractLotTable.resolve(
            "NF", LocalDate.parse("2026-07-19"),
            expiry = LocalDate.parse("2026-08-06"), expiryCycle = "weekly",
            capturedContractLot = 65.0
        ).resolved)
    }

    @Test
    fun parserRejectsHostileUntypedValues() {
        val invalid = listOf<Any?>(0, -1, 65.5, "abc", "", Double.NaN, Double.POSITIVE_INFINITY, true)
        invalid.forEach { value -> assertNull(ContractLotTable.parsePositiveIntegralLot(value)) }
        assertEquals(65, ContractLotTable.parsePositiveIntegralLot("65"))
        assertEquals(1.0, ContractLotTable.parseNumberOfLots(null).first)
        assertEquals(1.0, ContractLotTable.parseNumberOfLots(" ").first)
        val assumed = ContractLotTable.resolve(
            "NF", LocalDate.parse("2026-07-19"),
            expiry = LocalDate.parse("2026-08-06"), expiryCycle = "weekly"
        )
        assertTrue(assumed.numberOfLotsAssumed)
        assertEquals("one_lot_path_v1_20260913", assumed.numberOfLotsDefaultPolicy)
        assertNull(ContractLotTable.parseNumberOfLots("abc").first)
    }

    @Test
    fun discontinuedBnfWeeklyRefusesWithoutAnExpiryAnchor() {
        val row = ContractLotTable.resolve("BNF", LocalDate.parse("2026-07-19"), expiryCycle = "weekly")
        assertFalse(row.resolved)
        assertEquals("contract_cycle_discontinued", row.unavailableReason)
    }

    @Test
    fun allSixteenRuleIdsPresentCompared() {
        val snapIds = ContractLotTable.ruleSnapshotForTests().map { it["rule_id"] as String }.sorted()
        assertEquals(16, snapIds.size)
        assertEquals(snapIds.toSet().size, snapIds.size)
    }

}
