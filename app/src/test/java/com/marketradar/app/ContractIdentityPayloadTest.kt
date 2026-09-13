package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ContractIdentityPayloadTest {
    @Test
    fun buildsCanonicalWithHypotheticalBasis() {
        val src = JSONObject()
            .put("index_key", "NF")
            .put("expiry", "2026-07-28")
            .put("session_date", "2026-07-20")
            .put("contract_lot_size", 65)
            .put("number_of_lots", 1)
            .put("lane", "paper")
            .put("identity_complete", true)
        val canonical = ContractIdentityPayload.buildCanonical(src)
        assertNotNull(canonical)
        assertEquals(ContractIdentityPayload.SCHEMA_VERSION, canonical!!.getString("schema_version"))
        assertEquals(ContractIdentityPayload.QUANTITY_BASIS_HYPOTHETICAL_LOTS, canonical.getString("quantity_basis"))
        assertTrue(canonical.getJSONArray("legs").length() >= 1)
        val validated = ContractIdentityPayload.validate(canonical)
        assertTrue(validated.schemaCompatible)
    }

    @Test
    fun incompatibleSchemaRetainedNotStripped() {
        val bad = JSONObject()
            .put("schema_version", "contract_identity_v0_incompatible")
            .put("index_key", "BNF")
            .put("keep_me", true)
        val src = JSONObject().put("contract_identity", bad)
        val uploaded = ContractIdentityPayload.extractForUpload(src)
        assertNotNull(uploaded)
        assertFalse(uploaded!!.optBoolean("schema_compatible", true) && uploaded.optString("schema_error").isBlank())
        assertTrue(uploaded.has("keep_me") || uploaded.has("index_key"))
        assertTrue(uploaded.optString("schema_error").contains("incompatible_schema_version"))
    }
}
