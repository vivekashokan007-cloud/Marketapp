"""Paper-analysis eligibility — structural admission only.

Paper is the sole experimental lane. Structurally recordable non-primary /
monitor-only / soft-OOD / ML-rejected candidates must remain analysable in
Paper without changing Real recommendations or live entry gates.

This contract deliberately ignores ranking, advisory ML action, p_ml caps,
and final-entry authority. Those remain Real-only gates.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Optional

PAPER_ANALYSIS_ELIGIBILITY_VERSION = "paper_analysis_authorization_v1_20260913"
PAPER_ANALYSIS_GATE = "PAPER_ANALYSIS"
PAPER_ANALYSIS_BLOCKED_GATE = "PAPER_ANALYSIS_BLOCKED"

# Soft/advisory reasons that must NOT block paper analysis.
ADVISORY_ONLY_REASON_PREFIXES = (
    "ml_",
    "entry_confidence",
    "expected_value",
    "pc2_quality",
    "candidate_blocked",
    "direction_unsafe",
    "capital_blocked",
    "execution_not_ready",
)


def _finite_positive(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out <= 0:
        return None
    return out


def _option_type(value: Any) -> Optional[str]:
    text = str(value or "").strip().upper()
    if text in ("CE", "PE"):
        return text
    return None


def _canonical_digest(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _binding_value(candidate: dict, context: Optional[dict], *keys: str):
    for src in (candidate, context if isinstance(context, dict) else {}):
        for key in keys:
            value = src.get(key)
            if value not in (None, ""):
                return value
    return None


def _build_contract_identity(candidate: dict, context: Optional[dict]) -> tuple[Optional[dict], Optional[str], list[str]]:
    """Resolve and validate the exact identity bound into Paper authorization."""
    reasons: list[str] = []
    source = dict(candidate)
    session_date = _binding_value(candidate, context, "session_date", "today_ist", "sessionDate")
    scan_identity = _binding_value(candidate, context, "poll_ts", "scan_identity", "poll_timestamp")
    if session_date not in (None, ""):
        source["session_date"] = str(session_date)[:10]
    if scan_identity not in (None, ""):
        source["observed_at"] = str(scan_identity)
    if source.get("contract_lot_size") in (None, "") and source.get("lotSize") not in (None, ""):
        source["contract_lot_size"] = source.get("lotSize")
    if source.get("number_of_lots") in (None, ""):
        source["number_of_lots"] = 1
    if source.get("quantity_units") in (None, "") and source.get("contract_lot_size") not in (None, ""):
        try:
            source["quantity_units"] = int(source["contract_lot_size"]) * int(source["number_of_lots"])
        except (TypeError, ValueError):
            pass
    source.setdefault("quantity_basis", "hypothetical_lots")

    try:
        from canonical_net_profitability import resolve_contract_identity
        from contract_identity_schema import (
            CONTRACT_IDENTITY_SCHEMA_VERSION,
            build_canonical_contract_identity,
            validate_contract_identity,
        )
        resolved = resolve_contract_identity(source)
        canonical = build_canonical_contract_identity(source, resolved=resolved)
        checked = validate_contract_identity(canonical, require_version=True)
        identity = checked.get("payload") if isinstance(checked.get("payload"), dict) else canonical
        if identity.get("schema_version") != CONTRACT_IDENTITY_SCHEMA_VERSION:
            reasons.append("contract_identity_schema_unsupported")
        if not checked.get("eligible_for_contract_metrics"):
            reasons.extend(str(x) for x in checked.get("errors") or [])
            reasons.append("contract_identity_not_verified")
        if not identity.get("identity_complete"):
            reasons.append("contract_identity_incomplete")
        if reasons:
            return identity, None, list(dict.fromkeys(reasons))
        digest_fields = {
            key: identity.get(key)
            for key in (
                "schema_version", "index_key", "expiry", "expiry_cycle",
                "session_date", "contract_lot_size", "number_of_lots",
                "quantity_units", "quantity_basis", "lot_source",
                "lot_table_version", "lot_as_of", "source_ref", "source_digest",
                "calendar_dte", "trading_dte", "calendar_version", "dte_basis",
                "identity_status", "identity_complete",
            )
        }
        return identity, _canonical_digest(digest_fields), []
    except Exception as exc:
        return None, None, [f"contract_identity_resolution_error:{type(exc).__name__}"]


def candidate_leg_count(candidate: dict) -> int:
    if not isinstance(candidate, dict):
        return 0
    explicit = candidate.get("legCount") or candidate.get("leg_count")
    try:
        if explicit is not None and int(explicit) in (2, 4):
            return int(explicit)
    except (TypeError, ValueError):
        pass
    has_second = any(
        candidate.get(key) not in (None, "", 0)
        for key in (
            "sellStrike2",
            "buyStrike2",
            "sell_strike2",
            "buy_strike2",
            "sellType2",
            "buyType2",
        )
    )
    return 4 if has_second else 2


def structural_paper_analysis_reasons(candidate: dict) -> list:
    """Return structural blockers only. Empty => paper-analysis allowed."""
    reasons = []
    if not isinstance(candidate, dict):
        return ["candidate_not_object"]

    leg_count = candidate_leg_count(candidate)
    if leg_count not in (2, 4):
        reasons.append("strategy_leg_count_invalid")

    expiry = str(candidate.get("expiry") or "").strip()
    if not expiry:
        reasons.append("expiry_missing")

    lot = _finite_positive(
        candidate.get("lotSize")
        if candidate.get("lotSize") is not None
        else candidate.get("lot_size")
    )
    if lot is None:
        reasons.append("lot_size_invalid")

    legs = [
        ("sell", "", "sell"),
        ("buy", "", "buy"),
    ]
    if leg_count == 4:
        legs.extend([("sell", "2", "sell2"), ("buy", "2", "buy2")])

    for side, suffix, label in legs:
        camel_strike = f"{side}Strike{suffix}"
        snake_strike = f"{side}_strike{suffix}"
        camel_type = f"{side}Type{suffix}"
        snake_type = f"{side}_type{suffix}"
        camel_ltp = f"{side}LTP{suffix}"
        snake_ltp = f"{side}_ltp{suffix}"

        strike = _finite_positive(
            candidate.get(camel_strike)
            if candidate.get(camel_strike) is not None
            else candidate.get(snake_strike)
        )
        opt = _option_type(
            candidate.get(camel_type)
            if candidate.get(camel_type) is not None
            else candidate.get(snake_type)
        )
        ltp = _finite_positive(
            candidate.get(camel_ltp)
            if candidate.get(camel_ltp) is not None
            else candidate.get(snake_ltp)
        )
        if strike is None:
            reasons.append(f"{label}_strike_invalid")
        if opt is None:
            reasons.append(f"{label}_option_type_invalid")
        if ltp is None:
            reasons.append(f"{label}_entry_quote_unavailable")

    # Deduplicate while preserving order.
    return list(dict.fromkeys(reasons))


def annotate_paper_analysis_eligibility(
    candidate,
    entry_eligibility=None,
    *,
    context=None,
    brain_version=None,
):
    """Attach paper-analysis admission. Never mutates Real entryEligible/gate."""
    if not isinstance(candidate, dict):
        return candidate

    structural_reasons = structural_paper_analysis_reasons(candidate)
    candidate_id = _binding_value(candidate, context, "id", "candidate_id")
    session_date = _binding_value(candidate, context, "session_date", "today_ist", "sessionDate")
    scan_identity = _binding_value(candidate, context, "poll_ts", "scan_identity", "poll_timestamp")
    expiry = _binding_value(candidate, context, "expiry", "expiry_date")
    resolved_brain_version = brain_version or _binding_value(candidate, context, "brain_version", "brainVersion")
    identity, identity_digest, identity_reasons = _build_contract_identity(candidate, context)
    binding_reasons = []
    for name, value in (
        ("candidate_id", candidate_id),
        ("session_date", session_date),
        ("scan_identity", scan_identity),
        ("expiry", expiry),
        ("brain_version", resolved_brain_version),
        ("contract_identity_digest", identity_digest),
    ):
        if value in (None, ""):
            binding_reasons.append(f"authorization_{name}_missing")
    all_reasons = list(dict.fromkeys(structural_reasons + identity_reasons + binding_reasons))
    allowed = not all_reasons

    entry = entry_eligibility if isinstance(entry_eligibility, dict) else candidate.get("entryEligibility")
    entry_reasons = []
    if isinstance(entry, dict) and isinstance(entry.get("reasons"), list):
        entry_reasons = [str(r) for r in entry.get("reasons") if r is not None]

    advisory_blocks_ignored = [
        reason
        for reason in entry_reasons
        if any(str(reason).startswith(prefix) for prefix in ADVISORY_ONLY_REASON_PREFIXES)
        or str(reason) in (
            "max_profit_not_positive",
            "max_loss_not_positive",
            "strategy_direction_unknown",
            "strategy_market_fit_unavailable",
            "friction_unavailable",
            "quote_incomplete",
        )
    ]

    primary_eligible = bool(candidate.get("pc2PaperPrimaryEligible"))
    entry_eligible = bool(candidate.get("entryEligible"))
    monitor_only = (not entry_eligible) or str(candidate.get("entryGate") or "").upper() == "MONITOR"

    payload = {
        "schema_version": PAPER_ANALYSIS_ELIGIBILITY_VERSION,
        "allowed": allowed,
        "gate": PAPER_ANALYSIS_GATE if allowed else PAPER_ANALYSIS_BLOCKED_GATE,
        "reasons": all_reasons,
        "structural_contract": (
            "require 2/4 legs, expiry, positive lot, CE/PE types, and positive entry quotes; "
            "ranking/ML/advisory vetoes never block paper analysis"
        ),
        "real_gate_unchanged": True,
        "does_not_change_live_recommendation": True,
        "primary_eligible": primary_eligible,
        "entry_eligible": entry_eligible,
        "monitor_only": monitor_only,
        "non_primary": not primary_eligible,
        "advisory_blocks_ignored": advisory_blocks_ignored[:24],
        "selection_source_if_taken": "operator_test",
        "evidence_source_if_taken": "operator_paper_test",
        "brain_version": str(resolved_brain_version) if resolved_brain_version not in (None, "") else None,
        "candidate_id": str(candidate_id) if candidate_id not in (None, "") else None,
        "session_date": str(session_date)[:10] if session_date not in (None, "") else None,
        "scan_identity": str(scan_identity) if scan_identity not in (None, "") else None,
        "expiry": str(expiry)[:10] if expiry not in (None, "") else None,
        "contract_identity_schema_version": identity.get("schema_version") if isinstance(identity, dict) else None,
        "contract_identity_digest": identity_digest,
    }
    authorization_body = {
        key: payload.get(key)
        for key in (
            "schema_version", "allowed", "gate", "brain_version", "candidate_id",
            "session_date", "scan_identity", "expiry",
            "contract_identity_schema_version", "contract_identity_digest",
            "real_gate_unchanged",
        )
    }
    authorization_body["reasons"] = payload["reasons"]
    payload["authorization_id"] = "pa1_" + _canonical_digest(authorization_body)

    if isinstance(identity, dict):
        candidate["contract_identity"] = identity
        candidate["contract_identity_digest"] = identity_digest
    if session_date not in (None, ""):
        candidate["session_date"] = str(session_date)[:10]
    if scan_identity not in (None, ""):
        candidate["poll_ts"] = str(scan_identity)
    if resolved_brain_version not in (None, ""):
        candidate["brain_version"] = str(resolved_brain_version)
    candidate["paperAnalysisEligible"] = allowed
    candidate["paperAnalysisGate"] = payload["gate"]
    candidate["paperAnalysisEligibility"] = payload
    return candidate


def compact_paper_analysis_eligibility(raw):
    if not isinstance(raw, dict):
        return None
    out = {
        "schema_version": raw.get("schema_version"),
        "allowed": raw.get("allowed"),
        "gate": raw.get("gate"),
        "real_gate_unchanged": raw.get("real_gate_unchanged"),
        "does_not_change_live_recommendation": raw.get("does_not_change_live_recommendation"),
        "primary_eligible": raw.get("primary_eligible"),
        "entry_eligible": raw.get("entry_eligible"),
        "monitor_only": raw.get("monitor_only"),
        "non_primary": raw.get("non_primary"),
        "selection_source_if_taken": raw.get("selection_source_if_taken"),
        "evidence_source_if_taken": raw.get("evidence_source_if_taken"),
        "authorization_id": raw.get("authorization_id"),
        "brain_version": raw.get("brain_version"),
        "candidate_id": raw.get("candidate_id"),
        "session_date": raw.get("session_date"),
        "scan_identity": raw.get("scan_identity"),
        "expiry": raw.get("expiry"),
        "contract_identity_schema_version": raw.get("contract_identity_schema_version"),
        "contract_identity_digest": raw.get("contract_identity_digest"),
    }
    reasons = raw.get("reasons")
    if isinstance(reasons, list) and reasons:
        out["reasons"] = [str(r)[:160] for r in reasons[:12]]
    ignored = raw.get("advisory_blocks_ignored")
    if isinstance(ignored, list) and ignored:
        out["advisory_blocks_ignored"] = [str(r)[:120] for r in ignored[:12]]
    return out
