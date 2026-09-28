package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class BrainSnapshotPayloadSanitizerTest {
    @Test
    fun pythonNoneTokensBecomeJsonNullOnlyForNumericColumns() {
        val source = JSONObject()
            .put("confidence", "None")
            .put("b1a_bnf_rv_to_iv_daily_ratio", "NaN")
            .put("b1a_nf_rv_to_iv_daily_ratio", "Infinity")
            .put("action", "None")
            .put("context_json", JSONObject().put("research_label", "None"))

        val sanitized = BrainSnapshotPayloadSanitizer.sanitize(source)

        assertTrue(sanitized.isNull("confidence"))
        assertTrue(sanitized.isNull("b1a_bnf_rv_to_iv_daily_ratio"))
        assertTrue(sanitized.isNull("b1a_nf_rv_to_iv_daily_ratio"))
        assertEquals("None", sanitized.getString("action"))
        assertEquals("None", sanitized.getJSONObject("context_json").getString("research_label"))
    }

    @Test
    fun finiteNumericValuesAndNumericStringsRemainNumbers() {
        val sanitized = BrainSnapshotPayloadSanitizer.sanitize(
            JSONObject()
                .put("confidence", 72)
                .put("b1a_bnf_rv_to_iv_daily_ratio", "1.25")
                .put("b1a_nf_rv_to_iv_daily_ratio", 0.75)
        )

        assertEquals(72.0, sanitized.getDouble("confidence"), 0.0)
        assertEquals(1.25, sanitized.getDouble("b1a_bnf_rv_to_iv_daily_ratio"), 0.0)
        assertEquals(0.75, sanitized.getDouble("b1a_nf_rv_to_iv_daily_ratio"), 0.0)
    }
}
