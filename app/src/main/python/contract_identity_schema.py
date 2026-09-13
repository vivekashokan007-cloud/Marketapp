"""Canonical contract_identity JSONB serializer / validator.

Shared schema for Kotlin writers (SupabaseClient) and Python lineage/metrics.
Incompatible schema → retain local results + schema_error; NEVER drop identity
fields to retry.
"""

from __future__ import annotations

from typing import Any, Mapping, MutableMapping, Optional, Sequence

CONTRACT_IDENTITY_SCHEMA_VERSION = "contract_identity_v1_20260913"

# quantity_basis must distinguish explicit hypothetical lots vs recorded fills.
QUANTITY_BASIS_HYPOTHETICAL_LOTS = "hypothetical_lots"
QUANTITY_BASIS_RECORDED_FILLS = "recorded_fills"
QUANTITY_BASIS_UNKNOWN = "unknown"

IDENTITY_STATUS_VERIFIED = "verified"
IDENTITY_STATUS_QUARANTINE = "quarantine"
IDENTITY_STATUS_LEGACY_NULL = "legacy_null"
IDENTITY_STATUS_INCOMPLETE = "incomplete"
IDENTITY_STATUS_CONFLICT = "conflict"

CANONICAL_TOP_LEVEL_KEYS: tuple[str, ...] = (
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
    "legs",
)

LEG_KEYS: tuple[str, ...] = (
    "instrument_id",
    "expiry",
    "ratio",
    "contract_lot_size",
    "quantity_units",
    "side",
)

# Extra diagnostic keys allowed alongside canonical (not stripped on validate).
ALLOWED_EXTRA_KEYS: frozenset[str] = frozenset(
    {
        "index_known",
        "dte",
        "tDTE",
        "dte_source",
        "dte_ranking_bucket",
        "dte_ranking_bucket_version",
        "calendar_coverage_ok",
        "lot_size",
        "lot_size_assumed",
        "lot_size_source",
        "lot_conflict",
        "lot_provenance",
        "lot_provenance_quality",
        "matched_rule_id",
        "source_id",
        "captured_contract_lot",
        "rule_contract_lot",
        "exclude_authoritative_calc",
        "identity_complete",
        "contract_identity_quarantine",
        "evaluation_ineligible",
        "calibration_ineligible",
        "unavailable_reason",
        "instrument_key",
        "schema_error",
        "schema_compatible",
    }
)


def _nullish(value: Any) -> bool:
    return value is None or value == ""


def infer_quantity_basis(src: Mapping[str, Any] | None) -> str:
    """Distinguish hypothetical lots from actual recorded fills."""
    if not isinstance(src, Mapping):
        return QUANTITY_BASIS_UNKNOWN
    explicit = src.get("quantity_basis")
    if explicit in (
        QUANTITY_BASIS_HYPOTHETICAL_LOTS,
        QUANTITY_BASIS_RECORDED_FILLS,
        QUANTITY_BASIS_UNKNOWN,
    ):
        return str(explicit)
    fill_markers = (
        src.get("fill_qty"),
        src.get("filled_quantity"),
        src.get("recorded_fill_qty"),
        src.get("actual_fill_quantity"),
        src.get("broker_fill_qty"),
    )
    if any(not _nullish(v) for v in fill_markers):
        return QUANTITY_BASIS_RECORDED_FILLS
    if src.get("is_recorded_fill") in (True, 1, "1", "true", "True"):
        return QUANTITY_BASIS_RECORDED_FILLS
    if src.get("hypothetical") in (True, 1, "1", "true", "True"):
        return QUANTITY_BASIS_HYPOTHETICAL_LOTS
    # Teacher / paper / sim paths are explicit hypothetical unless fills present.
    lane = str(src.get("lane") or src.get("trade_mode") or src.get("cohort_execution_mode") or "").lower()
    if lane in ("paper", "sim", "teacher", "research", "shadow"):
        return QUANTITY_BASIS_HYPOTHETICAL_LOTS
    if src.get("number_of_lots") is not None or src.get("lots") is not None:
        return QUANTITY_BASIS_HYPOTHETICAL_LOTS
    return QUANTITY_BASIS_UNKNOWN


def _normalize_legs(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for leg in raw:
        if not isinstance(leg, Mapping):
            continue
        item = {k: leg.get(k) for k in LEG_KEYS if k in leg}
        # Preserve instrument aliases without inventing ids.
        if "instrument_id" not in item:
            for alt in ("instrument_key", "instrumentKey", "symbol", "tradingsymbol"):
                if not _nullish(leg.get(alt)):
                    item["instrument_id"] = leg.get(alt)
                    break
        out.append(item)
    return out


def _identity_status_from_resolved(identity: Mapping[str, Any]) -> str:
    if identity.get("lot_conflict") or identity.get("exclude_authoritative_calc"):
        return IDENTITY_STATUS_CONFLICT
    if identity.get("contract_identity_quarantine") or identity.get("evaluation_ineligible"):
        if identity.get("identity_complete"):
            return IDENTITY_STATUS_QUARANTINE
        return IDENTITY_STATUS_INCOMPLETE
    if identity.get("identity_complete"):
        return IDENTITY_STATUS_VERIFIED
    if all(_nullish(identity.get(k)) for k in ("index_key", "expiry", "contract_lot_size")):
        return IDENTITY_STATUS_LEGACY_NULL
    return IDENTITY_STATUS_INCOMPLETE


def build_canonical_contract_identity(
    src: Mapping[str, Any] | None,
    *,
    resolved: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build canonical jsonb object from outcome row and/or resolve_contract_identity output."""
    base: dict[str, Any] = {}
    if isinstance(resolved, Mapping):
        base.update(dict(resolved))
    row = src if isinstance(src, Mapping) else {}

    # Prefer nested contract_identity if already present and version-compatible.
    nested = row.get("contract_identity")
    if isinstance(nested, Mapping) and nested.get("schema_version") == CONTRACT_IDENTITY_SCHEMA_VERSION:
        merged = dict(nested)
        for k, v in base.items():
            if k not in merged or _nullish(merged.get(k)):
                merged[k] = v
        base = merged

    reason_codes: list[str] = []
    existing_reasons = base.get("reason_codes") or row.get("reason_codes")
    if isinstance(existing_reasons, (list, tuple)):
        reason_codes.extend(str(x) for x in existing_reasons if x not in (None, ""))
    if base.get("unavailable_reason"):
        reason_codes.append(str(base["unavailable_reason"]))
    if base.get("lot_conflict"):
        reason_codes.append("lot_conflict")
    if base.get("contract_identity_quarantine"):
        reason_codes.append("quarantine")

    quantity_units = base.get("quantity_units")
    if quantity_units is None:
        quantity_units = base.get("lot_size")
    if quantity_units is None:
        quantity_units = row.get("quantity_units") or row.get("lot_size")

    instrument_id = (
        base.get("instrument_id")
        or base.get("instrument_key")
        or row.get("instrument_id")
        or row.get("instrument_key")
        or row.get("instrumentKey")
    )

    source_ref = (
        base.get("source_ref")
        or base.get("source_id")
        or base.get("matched_rule_id")
        or row.get("source_ref")
        or row.get("source_id")
    )
    source_digest = base.get("source_digest") or row.get("source_digest")

    legs = _normalize_legs(base.get("legs") if "legs" in base else row.get("legs"))
    # Single-leg default from top-level identity (do not invent multi-leg collapse).
    if not legs and (base.get("expiry") or instrument_id or base.get("contract_lot_size") is not None):
        legs = [
            {
                "instrument_id": instrument_id,
                "expiry": base.get("expiry") or row.get("expiry"),
                "ratio": 1,
                "contract_lot_size": base.get("contract_lot_size"),
                "quantity_units": quantity_units,
                "side": row.get("side") or row.get("trade_side"),
            }
        ]

    status = base.get("identity_status") or _identity_status_from_resolved(base)

    canonical: dict[str, Any] = {
        "schema_version": CONTRACT_IDENTITY_SCHEMA_VERSION,
        "index_key": base.get("index_key") or row.get("index_key") or row.get("index"),
        "instrument_id": instrument_id,
        "expiry": base.get("expiry") or row.get("expiry") or row.get("expiry_date"),
        "expiry_cycle": base.get("expiry_cycle") or row.get("expiry_cycle"),
        "observed_at": base.get("observed_at") or row.get("observed_at") or row.get("poll_ts"),
        "session_date": base.get("session_date") or row.get("session_date") or base.get("lot_as_of"),
        "contract_lot_size": base.get("contract_lot_size")
        if base.get("contract_lot_size") is not None
        else row.get("contract_lot_size"),
        "number_of_lots": base.get("number_of_lots")
        if base.get("number_of_lots") is not None
        else row.get("number_of_lots"),
        "quantity_units": quantity_units,
        "quantity_basis": infer_quantity_basis({**dict(row), **dict(base)}),
        "lot_source": base.get("lot_source") or base.get("lot_size_source") or row.get("lot_source"),
        "lot_table_version": base.get("lot_table_version") or row.get("lot_table_version"),
        "lot_as_of": base.get("lot_as_of") or row.get("lot_as_of"),
        "source_ref": source_ref,
        "source_digest": source_digest,
        "calendar_dte": base.get("calendar_dte")
        if base.get("calendar_dte") is not None
        else row.get("calendar_dte"),
        "trading_dte": base.get("trading_dte")
        if base.get("trading_dte") is not None
        else row.get("trading_dte"),
        "calendar_version": base.get("calendar_version") or row.get("calendar_version"),
        "dte_basis": base.get("dte_basis") or row.get("dte_basis"),
        "dte_bucket": base.get("dte_bucket") or row.get("dte_bucket"),
        "dte_measurement_bucket_version": (
            base.get("dte_measurement_bucket_version")
            or base.get("dte_bucket_version")
            or row.get("dte_measurement_bucket_version")
            or row.get("dte_bucket_version")
        ),
        "identity_status": status,
        "reason_codes": sorted(set(reason_codes)),
        "legs": legs,
    }

    # Carry useful diagnostics without dropping them (never strip to retry).
    for k in ALLOWED_EXTRA_KEYS:
        if k in CANONICAL_TOP_LEVEL_KEYS:
            continue
        if k in base and k not in canonical:
            canonical[k] = base[k]
        elif k in row and k not in canonical and row[k] is not None:
            # Avoid copying bulky non-identity row fields accidentally.
            if k in (
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
                "dte_ranking_bucket",
                "dte_ranking_bucket_version",
                "calendar_coverage_ok",
                "unavailable_reason",
                "instrument_key",
            ):
                canonical[k] = row[k]

    return canonical


def validate_contract_identity(
    payload: Any,
    *,
    require_version: bool = True,
) -> dict[str, Any]:
    """Validate canonical object. Never mutates by dropping fields.

    Returns {ok, schema_compatible, errors, payload}.
    On incompatible schema: ok=False, schema_compatible=False, original payload retained.
    """
    errors: list[str] = []
    if payload is None:
        return {
            "ok": True,
            "schema_compatible": True,
            "identity_status": IDENTITY_STATUS_LEGACY_NULL,
            "errors": [],
            "payload": None,
            "eligible_for_contract_metrics": False,
        }
    if not isinstance(payload, Mapping):
        return {
            "ok": False,
            "schema_compatible": False,
            "errors": ["payload_not_object"],
            "payload": payload,
            "schema_error": "payload_not_object",
            "eligible_for_contract_metrics": False,
        }

    version = payload.get("schema_version")
    if require_version and version not in (None, "", CONTRACT_IDENTITY_SCHEMA_VERSION):
        # Foreign / future incompatible — retain as-is, do not strip.
        return {
            "ok": False,
            "schema_compatible": False,
            "errors": [f"incompatible_schema_version:{version}"],
            "payload": dict(payload),
            "schema_error": f"incompatible_schema_version:{version}",
            "eligible_for_contract_metrics": False,
        }

    missing_required_when_verified = []
    status = payload.get("identity_status")
    if status == IDENTITY_STATUS_VERIFIED:
        for key in ("index_key", "expiry", "contract_lot_size", "quantity_basis"):
            if _nullish(payload.get(key)):
                missing_required_when_verified.append(key)
        if missing_required_when_verified:
            errors.append("verified_missing:" + ",".join(missing_required_when_verified))

    qb = payload.get("quantity_basis")
    if qb not in (
        None,
        "",
        QUANTITY_BASIS_HYPOTHETICAL_LOTS,
        QUANTITY_BASIS_RECORDED_FILLS,
        QUANTITY_BASIS_UNKNOWN,
    ):
        errors.append(f"invalid_quantity_basis:{qb}")

    legs = payload.get("legs")
    if legs is not None and not isinstance(legs, (list, tuple)):
        errors.append("legs_not_array")

    eligible = (
        status == IDENTITY_STATUS_VERIFIED
        and not errors
        and not payload.get("contract_identity_quarantine")
        and payload.get("identity_complete") is not False
    )

    out_payload = dict(payload)
    if version in (None, ""):
        # Backfill version on known-shape local objects without dropping fields.
        out_payload["schema_version"] = CONTRACT_IDENTITY_SCHEMA_VERSION

    return {
        "ok": len(errors) == 0,
        "schema_compatible": True,
        "errors": errors,
        "payload": out_payload,
        "schema_error": errors[0] if errors else None,
        "eligible_for_contract_metrics": bool(eligible),
        "identity_status": status or out_payload.get("identity_status"),
    }


def prefer_contract_identity_jsonb(
    row: Mapping[str, Any] | None,
) -> tuple[Optional[dict[str, Any]], bool]:
    """Readers/exporters: prefer top-level contract_identity jsonb when present.

    Returns (identity_or_none, eligible_for_contract_dependent_metrics).
    Legacy null identity → readable but ineligible.
    """
    if not isinstance(row, Mapping):
        return None, False
    ci = row.get("contract_identity")
    if ci is None:
        # Rejected path: nested under outcome_json.evaluation_lineage
        oj = row.get("outcome_json")
        if isinstance(oj, Mapping):
            lineage = oj.get("evaluation_lineage")
            if isinstance(lineage, Mapping) and isinstance(lineage.get("contract_identity"), Mapping):
                ci = lineage["contract_identity"]
            elif isinstance(oj.get("contract_identity"), Mapping):
                ci = oj["contract_identity"]
    if ci is None:
        return None, False
    result = validate_contract_identity(ci, require_version=True)
    if not result["schema_compatible"]:
        # Retain local; surface schema error; ineligible.
        retained = dict(ci) if isinstance(ci, Mapping) else {"raw": ci}
        retained["schema_error"] = result.get("schema_error")
        retained["schema_compatible"] = False
        return retained, False
    payload = result["payload"]
    return payload, bool(result.get("eligible_for_contract_metrics"))


def attach_canonical_to_row(row: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Ensure row['contract_identity'] is canonical; never drop on schema error."""
    existing = row.get("contract_identity") if isinstance(row.get("contract_identity"), Mapping) else None
    if existing and existing.get("schema_version") not in (None, "", CONTRACT_IDENTITY_SCHEMA_VERSION):
        # Incompatible — retain + flag; do not rebuild by stripping.
        flagged = dict(existing)
        flagged["schema_compatible"] = False
        flagged["schema_error"] = f"incompatible_schema_version:{existing.get('schema_version')}"
        row["contract_identity"] = flagged
        row["contract_identity_schema_error"] = flagged["schema_error"]
        return row
    canonical = build_canonical_contract_identity(row, resolved=existing)
    check = validate_contract_identity(canonical)
    if not check["schema_compatible"]:
        retained = dict(canonical)
        retained["schema_error"] = check.get("schema_error")
        retained["schema_compatible"] = False
        row["contract_identity"] = retained
        row["contract_identity_schema_error"] = check.get("schema_error")
        return row
    row["contract_identity"] = check["payload"]
    return row
