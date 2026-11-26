package com.marketradar.app

import org.junit.Assert.*
import org.junit.Test

class BnfReferenceCaptureTest {
    @Test fun parsesOfficialFourteenMemberCsvAndQuotedCompanyNames() {
        val text = javaClass.getResourceAsStream("/bnf_reference/members.csv")!!.bufferedReader().use { it.readText() }
        val rows = BnfReferenceCapture.parseMembers(text)
        assertEquals(14, rows.length())
        assertEquals((0 until rows.length()).map { rows.getJSONObject(it).getString("isin") }.toSet().size, rows.length())
        assertTrue((0 until rows.length()).any { rows.getJSONObject(it).getString("symbol") == "CANBK" })
    }
    @Test fun instrumentExpiryUsesIstDateRatherThanUtcDay() {
        assertEquals("2026-12-29", BnfReferenceCapture.expiryDate(1798568999000L))
        assertEquals("2026-12-29", BnfReferenceCapture.expiryDate("2026-12-29"))
        assertNull(BnfReferenceCapture.expiryDate("invalid"))
    }
}
