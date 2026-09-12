package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ClosedTradeLedgerTest {
    private fun close(id: String, pnl: Double = -2100.0): String = JSONObject()
        .put("id", id).put("status", "CLOSED")
        .put("exit_date", "2026-09-09T10:00:00+05:30").put("net_pnl", pnl).toString()

    private fun ids(json: String): List<String> {
        val rows = JSONArray(json)
        return List(rows.length()) { rows.getJSONObject(it).getString("id") }
    }

    @Test fun pendingCloseSurvivesEmptySuccessfulRemoteResponse() {
        val recorded = ClosedTradeLedger.record("[]", "[]", close("pending-1"))!!
        val reconciled = ClosedTradeLedger.reconcileRemote("[]", recorded.pendingTradesJson)!!
        assertEquals(listOf("pending-1"), ids(reconciled.closedTradesJson))
        assertEquals(listOf("pending-1"), ids(reconciled.pendingTradesJson))
    }

    @Test fun remoteAcknowledgementRemovesPendingCloseAndKeepsServerRow() {
        val recorded = ClosedTradeLedger.record("[]", "[]", close("pending-2"))!!
        val serverRow = JSONObject(close("pending-2", -2345.0)).put("server_field", true)
        val reconciled = ClosedTradeLedger.reconcileRemote(JSONArray().put(serverRow).toString(), recorded.pendingTradesJson)!!
        assertEquals(emptyList<String>(), ids(reconciled.pendingTradesJson))
        assertEquals(listOf("pending-2"), ids(reconciled.closedTradesJson))
        assertTrue(JSONArray(reconciled.closedTradesJson).getJSONObject(0).getBoolean("server_field"))
    }

    @Test fun uiPayloadNeverAcknowledgesPendingClose() {
        val recorded = ClosedTradeLedger.record("[]", "[]", close("pending-3"))!!
        val merged = ClosedTradeLedger.mergeUnacknowledged(
            JSONArray().put(JSONObject(close("pending-3"))).toString(), recorded.pendingTradesJson
        )!!
        assertEquals(listOf("pending-3"), ids(merged.pendingTradesJson))
    }

    @Test fun invalidRemotePayloadCannotBecomeAnEmptyLedger() {
        assertNull(ClosedTradeLedger.reconcileRemote("not-json", "[]"))
    }

    @Test fun pendingClosePreservesGrossExtremaAcrossReloadJournal() {
        val incoming = JSONObject()
            .put("id", "pending-peak")
            .put("status", "CLOSED")
            .put("exit_date", "2026-09-12T10:00:00+05:30")
            .put("net_pnl", -100.0)
            .put("peak_pnl", 2400.0)
            .put("trough_pnl", -800.0)
            .put("peak_pnl_validity", "valid")
            .put("extrema_basis", "GROSS_MTM")
            .toString()
        val recorded = ClosedTradeLedger.record("[]", "[]", incoming)!!
        val pending = JSONArray(recorded.pendingTradesJson).getJSONObject(0)
        assertEquals(2400.0, pending.getDouble("peak_pnl"), 0.001)
        assertEquals(-800.0, pending.getDouble("trough_pnl"), 0.001)
        assertEquals("valid", pending.getString("peak_pnl_validity"))
        assertEquals("GROSS_MTM", pending.getString("extrema_basis"))
        val reconciled = ClosedTradeLedger.reconcileRemote("[]", recorded.pendingTradesJson)!!
        val retained = JSONArray(reconciled.pendingTradesJson).getJSONObject(0)
        assertEquals(2400.0, retained.getDouble("peak_pnl"), 0.001)
    }
}
