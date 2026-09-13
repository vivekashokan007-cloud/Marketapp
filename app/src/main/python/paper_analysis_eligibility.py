"""Paper-analysis eligibility — structural admission only.

Paper is the sole experimental lane. Structurally recordable non-primary /
monitor-only / soft-OOD / ML-rejected candidates must remain analysable in
Paper without changing Real recommendations or live entry gates.

This contract deliberately ignores ranking, advisory ML action, p_ml caps,
and final-entry authority. Those remain Real-only gates.
"""

from __future__ import annotations

import math
from typing import Any, Optional

PAPER_ANALYSIS_ELIGIBILITY_VERSION = "paper_analysis_eligibility_v1_20260913"
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
        # Fall back to index defaults only when index is known — still require
        # a positive resolved lot for a meaningful paper label.
        index_key = str(candidate.get("index") or candidate.get("index_key") or "").upper()
        if index_key == "BNF":
            lot = 30.0
        elif index_key == "NF":
            lot = 65.0
        else:
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


def annotate_paper_analysis_eligibility(candidate, entry_eligibility=None):
    """Attach paper-analysis admission. Never mutates Real entryEligible/gate."""
    if not isinstance(candidate, dict):
        return candidate

    structural_reasons = structural_paper_analysis_reasons(candidate)
    allowed = not structural_reasons

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
        "schema": 1,
        "version": PAPER_ANALYSIS_ELIGIBILITY_VERSION,
        "allowed": allowed,
        "gate": PAPER_ANALYSIS_GATE if allowed else PAPER_ANALYSIS_BLOCKED_GATE,
        "reasons": structural_reasons,
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
    }
    candidate["paperAnalysisEligible"] = allowed
    candidate["paperAnalysisGate"] = payload["gate"]
    candidate["paperAnalysisEligibility"] = payload
    return candidate


def compact_paper_analysis_eligibility(raw):
    if not isinstance(raw, dict):
        return None
    out = {
        "schema": raw.get("schema"),
        "version": raw.get("version"),
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
    }
    reasons = raw.get("reasons")
    if isinstance(reasons, list) and reasons:
        out["reasons"] = [str(r)[:160] for r in reasons[:12]]
    ignored = raw.get("advisory_blocks_ignored")
    if isinstance(ignored, list) and ignored:
        out["advisory_blocks_ignored"] = [str(r)[:120] for r in ignored[:12]]
    return out
