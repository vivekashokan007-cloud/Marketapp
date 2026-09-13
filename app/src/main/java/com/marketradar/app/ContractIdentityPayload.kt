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
    const val LOT_TABLE_VERSION = "contract_lot_table_v2_20260913"

    private val VERIFIED_LOT_PROVENANCE_REQUIREMENTS = mapOf(
        "authoritative_contract_rule" to listOf("lot_table_version", "source_ref"),
        "captured_metadata_consistent" to listOf("lot_table_version", "source_ref"),
        "captured_metadata" to listOf("source_ref", "source_digest"),
    )
    private val VERIFIED_DTE_BASIS_REQUIREMENTS = mapOf(
        "nse_trading_calendar" to listOf(
            "session_date", "expiry", "calendar_dte", "trading_dte", "calendar_version"
        ),
        "explicit_calendar_dte" to listOf(
            "session_date", "expiry", "calendar_dte", "dte_source", "source_ref"
        ),
        "explicit_trading_dte" to listOf(
            "session_date", "expiry", "trading_dte", "dte_source", "source_ref"
        ),
    )

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

    private fun parseIsoDate(raw: Any?): String? {
        if (raw == null || raw == JSONObject.NULL) return null
        val text = raw.toString().trim()
        if (text.length < 10) return null
        return try {
            java.time.LocalDate.parse(text.substring(0, 10)).toString()
        } catch (_: Exception) {
            null
        }
    }

    private fun parseIsoDateTime(raw: Any?): Boolean {
        if (raw == null || raw == JSONObject.NULL) return true
        var text = raw.toString().trim()
        if (text.isEmpty()) return true
        return try {
            if (text.endsWith("Z")) text = text.dropLast(1) + "+00:00"
            java.time.OffsetDateTime.parse(text)
            true
        } catch (_: Exception) {
            try {
                java.time.LocalDateTime.parse(text)
                true
            } catch (_: Exception) {
                false
            }
        }
    }

    private fun positiveInt(raw: Any?): Int? {
        if (raw == null || raw == JSONObject.NULL) return null
        return try {
            when (raw) {
                is Number -> {
                    val d = raw.toDouble()
                    if (!d.isFinite() || d % 1.0 != 0.0 || d <= 0.0) null else d.toInt()
                }
                else -> {
                    val d = raw.toString().toDouble()
                    if (!d.isFinite() || d % 1.0 != 0.0 || d <= 0.0) null else d.toInt()
                }
            }
        } catch (_: Exception) {
            null
        }
    }

    private fun nonNegInt(raw: Any?): Int? {
        if (raw == null || raw == JSONObject.NULL) return null
        return try {
            when (raw) {
                is Number -> {
                    val d = raw.toDouble()
                    if (!d.isFinite() || d % 1.0 != 0.0 || d < 0.0) null else d.toInt()
                }
                else -> {
                    val d = raw.toString().toDouble()
                    if (!d.isFinite() || d % 1.0 != 0.0 || d < 0.0) null else d.toInt()
                }
            }
        } catch (_: Exception) {
            null
        }
    }

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
        val allowedStatuses = setOf(
            "", STATUS_VERIFIED, STATUS_QUARANTINE, STATUS_LEGACY_NULL, STATUS_INCOMPLETE, STATUS_CONFLICT
        )
        if (status.isNotBlank() && status !in allowedStatuses) {
            errors.add("invalid_identity_status:$status")
        }
        if (has(payload, "index_key")) {
            val norm = ContractLotTable.normalizeIndexKey(payload.opt("index_key")?.toString())
            if (norm != "NF" && norm != "BNF") {
                errors.add("invalid_index_key:${payload.opt("index_key")}")
            }
        }
        for (dateKey in listOf("expiry", "session_date", "lot_as_of")) {
            if (has(payload, dateKey) && parseIsoDate(payload.opt(dateKey)) == null) {
                errors.add("invalid_$dateKey:unparseable_date")
            }
        }
        if (has(payload, "observed_at") && !parseIsoDateTime(payload.opt("observed_at"))) {
            errors.add("invalid_observed_at:unparseable_datetime")
        }
        val clsV = if (has(payload, "contract_lot_size")) positiveInt(payload.opt("contract_lot_size")) else null
        if (has(payload, "contract_lot_size") && clsV == null) errors.add("invalid_contract_lot_size:non_positive")
        val nV = if (has(payload, "number_of_lots")) positiveInt(payload.opt("number_of_lots")) else null
        if (has(payload, "number_of_lots") && nV == null) errors.add("invalid_number_of_lots:non_positive")
        val qV = if (has(payload, "quantity_units")) positiveInt(payload.opt("quantity_units")) else null
        if (has(payload, "quantity_units") && qV == null) errors.add("invalid_quantity_units:non_positive")
        if (clsV != null && nV != null && qV != null && clsV.toLong() * nV.toLong() != qV.toLong()) {
            errors.add("quantity_units_product_mismatch")
        }
        for (dteKey in listOf("calendar_dte", "trading_dte")) {
            if (has(payload, dteKey) && nonNegInt(payload.opt(dteKey)) == null) {
                errors.add("invalid_$dteKey:bad")
            }
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
        } else {
            val legs = payload.optJSONArray("legs")
            if (legs != null) {
                for (i in 0 until legs.length()) {
                    val leg = legs.optJSONObject(i)
                    if (leg == null) {
                        errors.add("leg_${i}_not_object")
                        continue
                    }
                    for (qk in listOf("contract_lot_size", "quantity_units", "ratio")) {
                        if (has(leg, qk) && positiveInt(leg.opt(qk)) == null) {
                            errors.add("leg_${i}_invalid_$qk")
                        }
                    }
                    if (has(leg, "expiry") && has(payload, "expiry")) {
                        val le = parseIsoDate(leg.opt("expiry"))
                        val pe = parseIsoDate(payload.opt("expiry"))
                        if (le != null && pe != null && le != pe) {
                            errors.add("leg_${i}_expiry_mismatch")
                        }
                    }
                }
            }
        }
        if (status == STATUS_VERIFIED) {
            if (version != SCHEMA_VERSION) {
                errors.add("verified_requires_exact_schema_version")
            }
            val missing = mutableListOf<String>()
            for (key in listOf(
                "index_key", "expiry", "contract_lot_size", "number_of_lots",
                "quantity_units", "quantity_basis"
            )) {
                if (!has(payload, key)) missing.add(key)
            }
            if (missing.isNotEmpty()) errors.add("verified_missing:" + missing.joinToString(","))
            if (!payload.optBoolean("identity_complete", false)) {
                errors.add("verified_requires_identity_complete")
            }
            if (payload.optBoolean("contract_identity_quarantine", false) ||
                payload.optBoolean("evaluation_ineligible", false)
            ) {
                errors.add("verified_with_quarantine_or_ineligible")
            }
            if (payload.optBoolean("lot_conflict", false)) {
                errors.add("verified_with_conflict")
            }
            val lotSource = payload.optString("lot_source", "")
            val provenanceRequired = VERIFIED_LOT_PROVENANCE_REQUIREMENTS[lotSource]
            if (provenanceRequired == null) {
                errors.add("verified_invalid_lot_source:${lotSource.ifBlank { "missing" }}")
            } else {
                for (key in provenanceRequired) {
                    if (!has(payload, key) || payload.optString(key).isBlank()) {
                        errors.add("verified_lot_provenance_missing:$key")
                    }
                }
                if ("lot_table_version" in provenanceRequired &&
                    payload.optString("lot_table_version") != LOT_TABLE_VERSION
                ) {
                    errors.add("verified_invalid_lot_table_version:${payload.optString("lot_table_version")}")
                }
            }
            val dteBasis = payload.optString("dte_basis", "")
            val dteRequired = VERIFIED_DTE_BASIS_REQUIREMENTS[dteBasis]
            if (dteRequired == null) {
                errors.add("verified_invalid_dte_basis:${dteBasis.ifBlank { "missing" }}")
            } else {
                for (key in dteRequired) {
                    if (!has(payload, key) || payload.optString(key).isBlank()) {
                        errors.add("verified_dte_provenance_missing:$key")
                    }
                }
            }
            if (dteBasis == "nse_trading_calendar" &&
                !payload.optBoolean("calendar_coverage_ok", false)
            ) {
                errors.add("verified_nse_calendar_coverage_required")
            }
            val sessionDate = parseIsoDate(payload.opt("session_date"))
            val expiryDate = parseIsoDate(payload.opt("expiry"))
            val calendarDte = if (has(payload, "calendar_dte")) nonNegInt(payload.opt("calendar_dte")) else null
            val tradingDte = if (has(payload, "trading_dte")) nonNegInt(payload.opt("trading_dte")) else null
            if (sessionDate != null && expiryDate != null && calendarDte != null) {
                val expected = java.time.temporal.ChronoUnit.DAYS.between(
                    java.time.LocalDate.parse(sessionDate),
                    java.time.LocalDate.parse(expiryDate)
                ).toInt()
                if (calendarDte != expected) errors.add("calendar_dte_session_expiry_mismatch")
            }
            if (calendarDte != null && tradingDte != null && tradingDte > calendarDte + 1) {
                errors.add("trading_dte_exceeds_calendar_window")
            }
            if (clsV == null || nV == null || qV == null) {
                errors.add("verified_requires_quantity_triplet")
            }
        }
        val out = JSONObject(payload.toString())
        if (errors.isNotEmpty()) {
            out.put("evaluation_ineligible", true)
            if (status == STATUS_VERIFIED) {
                out.put("identity_status", STATUS_QUARANTINE)
                out.put("contract_identity_quarantine", true)
            }
            out.put("validation_errors", org.json.JSONArray(errors))
        }
        val eligible = out.optString("identity_status", "") == STATUS_VERIFIED &&
            errors.isEmpty() &&
            !out.optBoolean("contract_identity_quarantine", false) &&
            out.optBoolean("identity_complete", false) &&
            !out.optBoolean("evaluation_ineligible", false)
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
