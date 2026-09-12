package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CalibrationInputGateTest {
    private fun row(vararg pairs: Pair<String, Any?>): JSONObject {
        val o = JSONObject()
        o.put("id", "t1")
        o.put("status", "CLOSED")
        o.put("paper", true)
        o.put("actual_pnl", 100.0)
        o.put("friction_cost", 200.0)
        o.put("net_pnl", -100.0)
        for ((k, v) in pairs) {
            if (v == null) o.put(k, JSONObject.NULL) else o.put(k, v)
        }
        return o
    }

    @Test
    fun grossPlusCostMakesNetLossEligible() {
        val v = CalibrationInputGate.validate(row())
        assertTrue(v.eligible)
        assertEquals(-100.0, v.netPnl!!, 0.001)
    }

    @Test
    fun excludedEnginesRejected() {
        for (engine in listOf("UNTRUSTED_INCOMPLETE_STRUCTURE", "PNL_BASIS_DIVERGENT", "UNKNOWN")) {
            val v = CalibrationInputGate.validate(row("pnl_engine" to engine))
            assertFalse(engine, v.eligible)
        }
    }

    @Test
    fun missingCostsRejected() {
        val o = row()
        o.remove("friction_cost")
        o.remove("net_pnl")
        assertFalse(CalibrationInputGate.validate(o).eligible)
    }

    @Test
    fun filterKeepsPaperSeparateAndDedupes() {
        val arr = JSONArray()
        arr.put(row("id" to "a", "net_pnl" to 10.0, "friction_cost" to 40.0, "actual_pnl" to 50.0))
        arr.put(row("id" to "a", "net_pnl" to -5.0, "friction_cost" to 55.0, "actual_pnl" to 50.0))
        arr.put(row("id" to "b", "paper" to false, "trade_mode" to "live", "net_pnl" to 1.0, "friction_cost" to 1.0, "actual_pnl" to 2.0))
        val filtered = JSONArray(CalibrationInputGate.filterLearningEligible(arr.toString(), "paper"))
        assertEquals(1, filtered.length())
        assertEquals("a", filtered.getJSONObject(0).getString("id"))
    }
}
