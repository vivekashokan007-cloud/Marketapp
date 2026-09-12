"""G3 gross extrema contract for trades_v2 peak_pnl / trough_pnl.

GROSS EXTREMA (top-level trades_v2.peak_pnl / trough_pnl)
========================================================
- Unit: INR (₹) total currency for the recorded position size (lots × lot_size),
  same currency basis as current_pnl / actual_pnl — not per-unit premium.
- Lot quantity: trade.lot_size × trade.lots (or resolved lot used by position valuation).
- Price basis: GROSS mark-to-market from option-chain quotes as used by brain
  position_live / position_ticks.current_pnl. NOT net-of-friction.
- Source (live): brain position valuation → native open_trades → PWA trade fields.
- Source (repair preference): position_ticks.current_pnl extrema, then verified
  nested journey_stats / close_trace_json values with matching identity/units/basis/period.
- Observation interval: entry (OPEN) through close (exit_date), marks while OPEN.
- Validity:
    null / "unknown" = unobserved / not recorded (never fabricate 0)
    0 = observed zero (valid when marks support it)
    finite number = observed extremum
- Preserve top-level field as GROSS. Do NOT overwrite with net extrema.
- Repair must NOT change actual_pnl / net_pnl, and must NOT auto-activate exit
  learning from repaired gross peaks. Copying a peak into an untrusted row does
  not relabel pnl_engine as trustworthy.
"""

from __future__ import annotations

from typing import Any

EXTREMA_BASIS = "GROSS_MTM"
EXTREMA_UNIT = "INR_TOTAL"
EXTREMA_CONTRACT_VERSION = "g3_gross_extrema_v1_20260912"

UNTRUSTED_ENGINES = frozenset(
    {
        "UNTRUSTED_INCOMPLETE_STRUCTURE",
        "PNL_BASIS_DIVERGENT",
        "UNKNOWN",
    }
)


def number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


def object_or_empty(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        import json

        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def validity_of(value: float | None, *, observed: bool) -> str:
    if value is None:
        return "unknown"
    if not observed and value == 0:
        # Explicit: caller must pass observed=True for a valid zero.
        return "unknown"
    return "valid"


def normalize_gross_extrema(
    peak: Any,
    trough: Any,
    *,
    peak_observed: bool | None = None,
    trough_observed: bool | None = None,
    source: str = "live_position",
) -> dict[str, Any]:
    """Normalize peak/trough without fabricating zeros for unknown.

    If peak_observed/trough_observed is None, a present finite number (including 0)
    is treated as observed; missing/non-finite is unknown.
    """
    peak_n = number_or_none(peak)
    trough_n = number_or_none(trough)

    if peak_observed is None:
        peak_observed = peak_n is not None
    if trough_observed is None:
        trough_observed = trough_n is not None

    if not peak_observed:
        peak_n = None
    if not trough_observed:
        trough_n = None

    return {
        "peak_pnl": peak_n,
        "trough_pnl": trough_n,
        "peak_pnl_validity": "valid" if peak_observed and peak_n is not None else "unknown",
        "trough_pnl_validity": "valid" if trough_observed and trough_n is not None else "unknown",
        "extrema_basis": EXTREMA_BASIS,
        "extrema_unit": EXTREMA_UNIT,
        "extrema_source": source,
        "extrema_contract_version": EXTREMA_CONTRACT_VERSION,
    }


def merge_peak(a: Any, b: Any) -> float | None:
    """Preserve unknown; take max of finite observations."""
    av = number_or_none(a)
    bv = number_or_none(b)
    if av is None:
        return bv
    if bv is None:
        return av
    return max(av, bv)


def merge_trough(a: Any, b: Any) -> float | None:
    av = number_or_none(a)
    bv = number_or_none(b)
    if av is None:
        return bv
    if bv is None:
        return av
    return min(av, bv)


def nested_extrema(trade: dict[str, Any]) -> dict[str, float | None]:
    journey = object_or_empty(trade.get("journey_stats"))
    trace = object_or_empty(trade.get("close_trace_json"))
    return {
        "journey_peak": number_or_none(journey.get("peak_pnl")),
        "journey_trough": number_or_none(journey.get("trough_pnl")),
        "trace_peak": number_or_none(trace.get("peak_pnl")),
        "trace_trough": number_or_none(trace.get("trough_pnl")),
    }


def tick_extrema_from_pnls(pnls: list[Any]) -> dict[str, Any]:
    vals = [v for v in (number_or_none(p) for p in pnls) if v is not None]
    if not vals:
        return {"peak": None, "trough": None, "n": 0, "observed": False}
    return {
        "peak": max(vals),
        "trough": min(vals),
        "n": len(vals),
        "observed": True,
    }


def values_conflict(a: float | None, b: float | None, *, tol: float = 1.0) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) > tol


def is_untrusted_engine(trade: dict[str, Any]) -> bool:
    engine = str(trade.get("pnl_engine") or "").strip().upper()
    return engine in UNTRUSTED_ENGINES


def propose_repair(
    trade: dict[str, Any],
    *,
    tick_peak: float | None = None,
    tick_trough: float | None = None,
    tick_n: int = 0,
    tol: float = 1.0,
) -> dict[str, Any]:
    """Propose peak/trough repair without mutating P&L or trust labels.

    Preference: consistent tick evidence → verified journey/trace values.
    Conflicting evidence → review queue (no auto write).
    """
    trade_id = trade.get("id") or trade.get("trade_id")
    old_peak = number_or_none(trade.get("peak_pnl"))
    old_trough = number_or_none(trade.get("trough_pnl"))
    nested = nested_extrema(trade)
    untrusted = is_untrusted_engine(trade)

    # Only consider empty/zero top-level as candidates when nested/tick has signal.
    # Nonzero top-level peaks are preserved (do not overwrite a better valid peak).
    result: dict[str, Any] = {
        "trade_id": trade_id,
        "action": "skip",
        "reason": None,
        "old_peak_pnl": old_peak,
        "old_trough_pnl": old_trough,
        "new_peak_pnl": old_peak,
        "new_trough_pnl": old_trough,
        "source": None,
        "basis": EXTREMA_BASIS,
        "unit": EXTREMA_UNIT,
        "pnl_engine": trade.get("pnl_engine"),
        "untrusted": untrusted,
        "trust_label_changed": False,
        "exclusions": [],
        "nested": nested,
        "tick_n": tick_n,
        "tick_peak": tick_peak,
        "tick_trough": tick_trough,
    }

    if untrusted:
        result["exclusions"].append("untrusted_engine_peak_copy_does_not_upgrade_trust")

    journey_peak = nested["journey_peak"]
    journey_trough = nested["journey_trough"]
    trace_peak = nested["trace_peak"]
    trace_trough = nested["trace_trough"]

    # Prefer tick when available
    candidate_peak = None
    candidate_trough = None
    source = None

    if tick_n > 0 and tick_peak is not None:
        candidate_peak = tick_peak
        candidate_trough = tick_trough
        source = "position_ticks"
        # Conflict with verified journey?
        if values_conflict(tick_peak, journey_peak, tol=tol) or values_conflict(
            tick_trough, journey_trough, tol=tol
        ):
            result["action"] = "review"
            result["reason"] = "conflicting_tick_vs_journey"
            return result
        if values_conflict(tick_peak, trace_peak, tol=tol) or values_conflict(
            tick_trough, trace_trough, tol=tol
        ):
            result["action"] = "review"
            result["reason"] = "conflicting_tick_vs_trace"
            return result
    else:
        # Fall back to journey if verified against trace (or alone if only one present)
        if journey_peak is not None or journey_trough is not None:
            if values_conflict(journey_peak, trace_peak, tol=tol) or values_conflict(
                journey_trough, trace_trough, tol=tol
            ):
                result["action"] = "review"
                result["reason"] = "conflicting_journey_vs_trace"
                return result
            candidate_peak = journey_peak if journey_peak is not None else trace_peak
            candidate_trough = journey_trough if journey_trough is not None else trace_trough
            source = "journey_stats_verified" if journey_peak is not None else "close_trace_json"
        elif trace_peak is not None or trace_trough is not None:
            candidate_peak = trace_peak
            candidate_trough = trace_trough
            source = "close_trace_json"
        else:
            result["action"] = "skip"
            result["reason"] = "no_repair_evidence"
            result["exclusions"].append("no_tick_no_journey_no_trace")
            return result

    # Decide whether top-level needs update
    peak_needs = (old_peak is None or old_peak == 0) and candidate_peak is not None and candidate_peak != 0
    # Also allow repairing when old is 0 and candidate is valid 0 with tick observation? No — no-op.
    trough_needs = (old_trough is None or old_trough == 0) and candidate_trough is not None and (
        candidate_trough != 0 or (source == "position_ticks" and tick_n > 0 and candidate_trough == 0)
    )
    # Simpler trough: update empty/zero top when candidate is strictly more extreme (more negative) or first observation
    if old_trough is None:
        trough_needs = candidate_trough is not None
    elif candidate_trough is not None and candidate_trough < old_trough:
        trough_needs = True
    elif old_trough == 0 and candidate_trough is not None and candidate_trough < 0:
        trough_needs = True
    else:
        trough_needs = False

    # Do not overwrite a better (higher) valid peak
    if old_peak is not None and old_peak != 0 and candidate_peak is not None:
        if old_peak > 0 and abs(candidate_peak - old_peak) <= tol:
            peak_needs = False
        elif old_peak > 0 and candidate_peak < old_peak:
            peak_needs = False
            result["exclusions"].append("preserve_better_existing_peak")
        elif old_peak > 0 and candidate_peak > old_peak + tol:
            result["action"] = "review"
            result["reason"] = "candidate_peak_exceeds_existing_nonzero"
            return result

    if not peak_needs and not trough_needs:
        result["action"] = "skip"
        result["reason"] = "already_populated_or_no_delta"
        return result

    new_peak = candidate_peak if peak_needs else old_peak
    new_trough = candidate_trough if trough_needs else old_trough

    # Positive-only copy must not invent peak for a negative-only series under declared convention.
    # If candidate peak is None/0 and trough is negative, leave peak as-is (unknown or 0).
    if peak_needs and (new_peak is None or new_peak == 0) and new_trough is not None and new_trough < 0:
        if source != "position_ticks":
            peak_needs = False
            new_peak = old_peak
            result["exclusions"].append("no_fabricated_peak_for_negative_only_series")

    if not peak_needs and not trough_needs:
        result["action"] = "skip"
        result["reason"] = "filtered_negative_only_or_no_delta"
        return result

    result["action"] = "repair"
    result["reason"] = "top_level_missing_or_zero_with_evidence"
    result["new_peak_pnl"] = new_peak
    result["new_trough_pnl"] = new_trough
    result["source"] = source
    result["peak_changed"] = peak_needs
    result["trough_changed"] = trough_needs
    # Expected-old for idempotent apply
    result["expected_old_peak_pnl"] = old_peak
    result["expected_old_trough_pnl"] = old_trough
    return result


def build_close_extrema_fields(trade_like: dict[str, Any]) -> dict[str, Any]:
    """Fields every close payload must include (PWA / native / retry)."""
    peak = trade_like.get("peak_pnl")
    trough = trade_like.get("trough_pnl")
    # If key missing entirely → unknown; if present as 0 → valid zero.
    peak_observed = "peak_pnl" in trade_like and number_or_none(peak) is not None
    trough_observed = "trough_pnl" in trade_like and number_or_none(trough) is not None
    # Also accept explicit validity flags if provided
    if trade_like.get("peak_pnl_validity") == "unknown":
        peak_observed = False
    if trade_like.get("trough_pnl_validity") == "unknown":
        trough_observed = False
    norm = normalize_gross_extrema(
        peak if peak_observed else None,
        trough if trough_observed else None,
        peak_observed=peak_observed,
        trough_observed=trough_observed,
        source=str(trade_like.get("extrema_source") or "live_position"),
    )
    return norm


def apply_patch_is_safe(
    trade: dict[str, Any],
    proposal: dict[str, Any],
) -> tuple[bool, str]:
    """Idempotent expected-old check before write."""
    if proposal.get("action") != "repair":
        return False, "not_a_repair"
    cur_peak = number_or_none(trade.get("peak_pnl"))
    cur_trough = number_or_none(trade.get("trough_pnl"))
    exp_peak = number_or_none(proposal.get("expected_old_peak_pnl"))
    exp_trough = number_or_none(proposal.get("expected_old_trough_pnl"))
    # Treat None and 0 as distinct: expected must match current exactly under number_or_none
    # If current already equals new → idempotent no-op success path
    new_peak = number_or_none(proposal.get("new_peak_pnl"))
    new_trough = number_or_none(proposal.get("new_trough_pnl"))
    if cur_peak == new_peak and cur_trough == new_trough:
        return False, "already_applied"
    if cur_peak != exp_peak or cur_trough != exp_trough:
        return False, "concurrent_change_skip"
    return True, "ok"
