package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject

/**
 * Canonical contract_identity JSONB serializer/validator — mirrors
 * app/src/main/python/contract_identity_schema.py.
 *
 * Incompatible schema → retain local payload + schema_error; never drop fields
 * to retry. quantity_basis distinguishes hypothetical_lots vs recorded_fills.
 */
internal object ContractIdentityPayload {
    const val SCHEMA_VERSION = "contract_identity_v1_20260913"

    const val QUANTITY_BASIS_HYPOTHETICAL_LOTS = "hypothetical_lots"
    const val QUANTITY_BASIS_RECORDED_FILLS = "recorded_fills"
    const val QUANTITY_BASIS_UNKNOWN = "unknown"

    const val STATUS_VERIFIED = "verified"
    const val STATUS_QUARANTINE = "quarantine"
    const val STATUS_LEGACY_NULL = "legacy_null"
    const val STATUS_INCOMPLETE = "incomplete"
    const val STATUS_CONFLICT = "conflict"

    val CANONICAL_KEYS = listOf(
        "schema_version",
        "index_key",
        "instrument_id",
        "expiry",
        "expiry_cycle",
        "observed_at",
        "session_date",
        "contract_lot_size",
        "number_of_lots",
        "quantity_units",
        "quantity_basis",
        "lot_source",
        "lot_table_version",
        "lot_as_of",
        "source_ref",
        "source_digest",
        "calendar_dte",
        "trading_dte",
        "calendar_version",
        "dte_basis",
        "dte_bucket",
        "dte_measurement_bucket_version",
        "identity_status",
        "reason_codes",
        "legs"
    )

    private fun has(src: JSONObject, key: String): Boolean = src.has(key) && !src.isNull(key)

    private fun optAny(src: JSONObject, vararg keys: String): Any? {
        for (key in keys) {
            if (has(src, key)) return src.opt(key)
        }
        return null
    }

    fun inferQuantityBasis(src: JSONObject): String {
        if (has(src, "quantity_basis")) {
            val qb = src.optString("quantity_basis")
            if (qb in setOf(
                    QUANTITY_BASIS_HYPOTHETICAL_LOTS,
                    QUANTITY_BASIS_RECORDED_FILLS,
                    QUANTITY_BASIS_UNKNOWN
                )
            ) return qb
        }
        for (key in listOf("fill_qty", "filled_quantity", "recorded_fill_qty", "actual_fill_quantity", "broker_fill_qty")) {
            if (has(src, key)) return QUANTITY_BASIS_RECORDED_FILLS
        }
        if (src.optBoolean("is_recorded_fill", false)) return QUANTITY_BASIS_RECORDED_FILLS
        if (src.optBoolean("hypothetical", false)) return QUANTITY_BASIS_HYPOTHETICAL_LOTS
        val lane = listOf("lane", "trade_mode", "cohort_execution_mode")
            .map { src.optString(it, "") }
            .firstOrNull { it.isNotBlank() }
            ?.lowercase()
            .orEmpty()
        if (lane in setOf("paper", "sim", "teacher", "research", "shadow")) {
            return QUANTITY_BASIS_HYPOTHETICAL_LOTS
        }
        if (has(src, "number_of_lots") || has(src, "lots")) return QUANTITY_BASIS_HYPOTHETICAL_LOTS
        return QUANTITY_BASIS_UNKNOWN
    }

    private fun normalizeLegs(src: JSONObject): JSONArray {
        val out = JSONArray()
        val raw = src.optJSONArray("legs")
        if (raw != null) {
            for (i in 0 until raw.length()) {
                val leg = raw.optJSONObject(i) ?: continue
                val item = JSONObject()
                for (key in listOf("instrument_id", "expiry", "ratio", "contract_lot_size", "quantity_units", "side")) {
                    if (has(leg, key)) item.put(key, leg.opt(key))
                }
                if (!has(item, "instrument_id")) {
                    for (alt in listOf("instrument_key", "instrumentKey", "symbol", "tradingsymbol")) {
                        if (has(leg, alt)) {
                            item.put("instrument_id", leg.opt(alt))
                            break
                        }
                    }
                }
                out.put(item)
            }
        }
        if (out.length() == 0) {
            val instrument = optAny(src, "instrument_id", "instrument_key", "instrumentKey")
            val expiry = optAny(src, "expiry", "expiry_date")
            val lot = optAny(src, "contract_lot_size")
            val qty = optAny(src, "quantity_units", "lot_size")
            if (instrument != null || expiry != null || lot != null) {
                val item = JSONObject()
                if (instrument != null) item.put("instrument_id", instrument)
                if (expiry != null) item.put("expiry", expiry)
                item.put("ratio", 1)
                if (lot != null) item.put("contract_lot_size", lot)
                if (qty != null) item.put("quantity_units", qty)
                val side = optAny(src, "side", "trade_side")
                if (side != null) item.put("side", side)
                out.put(item)
            }
        }
        return out
    }

    private fun identityStatus(src: JSONObject): String {
        if (src.optBoolean("lot_conflict", false) || src.optBoolean("exclude_authoritative_calc", false)) {
            return STATUS_CONFLICT
        }
        if (src.optBoolean("contract_identity_quarantine", false) || src.optBoolean("evaluation_ineligible", false)) {
            return if (src.optBoolean("identity_complete", false)) STATUS_QUARANTINE else STATUS_INCOMPLETE
        }
        if (src.optBoolean("identity_complete", false)) return STATUS_VERIFIED
        val empty = !has(src, "index_key") && !has(src, "expiry") && !has(src, "contract_lot_size")
        return if (empty) STATUS_LEGACY_NULL else STATUS_INCOMPLETE
    }

    fun buildCanonical(src: JSONObject?): JSONObject? {
        if (src == null) return null
        // Prefer nested contract_identity when already canonical.
        val nested = src.optJSONObject("contract_identity")
        val base = JSONObject(src.toString())
        if (nested != null) {
            val keys = nested.keys()
            while (keys.hasNext()) {
                val k = keys.next()
                if (!has(base, k) || base.isNull(k)) base.put(k, nested.opt(k))
            }
        }

        val reasons = JSONArray()
        val existing = base.optJSONArray("reason_codes")
        if (existing != null) {
            for (i in 0 until existing.length()) reasons.put(existing.opt(i))
        }
        if (has(base, "unavailable_reason")) reasons.put(base.opt("unavailable_reason"))
        if (base.optBoolean("lot_conflict", false)) reasons.put("lot_conflict")
        if (base.optBoolean("contract_identity_quarantine", false)) reasons.put("quarantine")

        val out = JSONObject()
        out.put("schema_version", SCHEMA_VERSION)
        out.put("index_key", optAny(base, "index_key", "index") ?: JSONObject.NULL)
        out.put("instrument_id", optAny(base, "instrument_id", "instrument_key", "instrumentKey") ?: JSONObject.NULL)
        out.put("expiry", optAny(base, "expiry", "expiry_date") ?: JSONObject.NULL)
        out.put("expiry_cycle", optAny(base, "expiry_cycle") ?: JSONObject.NULL)
        out.put("observed_at", optAny(base, "observed_at", "poll_ts") ?: JSONObject.NULL)
        out.put("session_date", optAny(base, "session_date", "lot_as_of") ?: JSONObject.NULL)
        out.put("contract_lot_size", optAny(base, "contract_lot_size") ?: JSONObject.NULL)
        out.put("number_of_lots", optAny(base, "number_of_lots") ?: JSONObject.NULL)
        out.put("quantity_units", optAny(base, "quantity_units", "lot_size") ?: JSONObject.NULL)
        out.put("quantity_basis", inferQuantityBasis(base))
        out.put("lot_source", optAny(base, "lot_source", "lot_size_source") ?: JSONObject.NULL)
        out.put("lot_table_version", optAny(base, "lot_table_version") ?: JSONObject.NULL)
        out.put("lot_as_of", optAny(base, "lot_as_of") ?: JSONObject.NULL)
        out.put("source_ref", optAny(base, "source_ref", "source_id", "matched_rule_id") ?: JSONObject.NULL)
        out.put("source_digest", optAny(base, "source_digest") ?: JSONObject.NULL)
        out.put("calendar_dte", optAny(base, "calendar_dte") ?: JSONObject.NULL)
        out.put("trading_dte", optAny(base, "trading_dte", "tDTE") ?: JSONObject.NULL)
        out.put("calendar_version", optAny(base, "calendar_version") ?: JSONObject.NULL)
        out.put("dte_basis", optAny(base, "dte_basis") ?: JSONObject.NULL)
        out.put("dte_bucket", optAny(base, "dte_bucket") ?: JSONObject.NULL)
        out.put(
            "dte_measurement_bucket_version",
            optAny(base, "dte_measurement_bucket_version", "dte_bucket_version") ?: JSONObject.NULL
        )
        val status = if (has(base, "identity_status")) base.optString("identity_status") else identityStatus(base)
        out.put("identity_status", status)
        out.put("reason_codes", reasons)
        out.put("legs", normalizeLegs(base))

        // Carry diagnostics without stripping.
        for (key in listOf(
            "identity_complete",
            "contract_identity_quarantine",
            "evaluation_ineligible",
            "calibration_ineligible",
            "lot_conflict",
            "matched_rule_id",
            "source_id",
            "lot_size",
            "dte",
            "tDTE",
            "dte_source",
            "calendar_coverage_ok",
            "unavailable_reason"
        )) {
            if (has(base, key) && !has(out, key)) out.put(key, base.opt(key))
        }
        return out
    }

    data class Validation(
        val ok: Boolean,
        val schemaCompatible: Boolean,
        val errors: List<String>,
        val payload: JSONObject?,
        val schemaError: String?,
        val eligibleForContractMetrics: Boolean
    )

    fun validate(payload: JSONObject?, requireVersion: Boolean = true): Validation {
        if (payload == null) {
            return Validation(true, true, emptyList(), null, null, false)
        }
        val version = payload.optString("schema_version", "")
        if (requireVersion && version.isNotBlank() && version != SCHEMA_VERSION) {
            val retained = JSONObject(payload.toString())
            retained.put("schema_compatible", false)
            retained.put("schema_error", "incompatible_schema_version:$version")
            return Validation(
                ok = false,
                schemaCompatible = false,
                errors = listOf("incompatible_schema_version:$version"),
                payload = retained,
                schemaError = "incompatible_schema_version:$version",
                eligibleForContractMetrics = false
            )
        }
        val errors = mutableListOf<String>()
        val status = payload.optString("identity_status", "")
        if (status == STATUS_VERIFIED) {
            val missing = mutableListOf<String>()
            for (key in listOf("index_key", "expiry", "contract_lot_size", "quantity_basis")) {
                if (!has(payload, key)) missing.add(key)
            }
            if (missing.isNotEmpty()) errors.add("verified_missing:" + missing.joinToString(","))
        }
        val qb = payload.optString("quantity_basis", "")
        if (qb.isNotBlank() && qb !in setOf(
                QUANTITY_BASIS_HYPOTHETICAL_LOTS,
                QUANTITY_BASIS_RECORDED_FILLS,
                QUANTITY_BASIS_UNKNOWN
            )
        ) {
            errors.add("invalid_quantity_basis:$qb")
        }
        if (payload.has("legs") && !payload.isNull("legs") && payload.optJSONArray("legs") == null) {
            errors.add("legs_not_array")
        }
        val out = JSONObject(payload.toString())
        if (!has(out, "schema_version") || out.optString("schema_version").isBlank()) {
            out.put("schema_version", SCHEMA_VERSION)
        }
        val eligible = status == STATUS_VERIFIED &&
            errors.isEmpty() &&
            !out.optBoolean("contract_identity_quarantine", false) &&
            out.optBoolean("identity_complete", true)
        return Validation(
            ok = errors.isEmpty(),
            schemaCompatible = true,
            errors = errors,
            payload = out,
            schemaError = errors.firstOrNull(),
            eligibleForContractMetrics = eligible
        )
    }

    /**
     * Extract contract_identity for upload rows. On incompatible schema retain
     * original nested object with schema_error — never strip to retry.
     */
    fun extractForUpload(src: JSONObject): JSONObject? {
        val nested = src.optJSONObject("contract_identity")
        if (nested != null) {
            val version = nested.optString("schema_version", "")
            if (version.isNotBlank() && version != SCHEMA_VERSION) {
                val retained = JSONObject(nested.toString())
                retained.put("schema_compatible", false)
                retained.put("schema_error", "incompatible_schema_version:$version")
                return retained
            }
            val built = buildCanonical(src) ?: return nested
            val checked = validate(built)
            return checked.payload
        }
        // Build from flat identity fields when present.
        val hasIdentityHint = listOf(
            "contract_lot_size", "calendar_dte", "trading_dte", "lot_table_version",
            "identity_complete", "expiry_cycle", "lot_source"
        ).any { has(src, it) }
        if (!hasIdentityHint) return null
        val built = buildCanonical(src) ?: return null
        return validate(built).payload
    }
}
