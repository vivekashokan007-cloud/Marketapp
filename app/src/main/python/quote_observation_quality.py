"""Batch B B1 — quote / observation classification (shared vocabulary).

Does not mutate raw rows and is not a teacher production veto.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

QUOTE_OBSERVATION_QUALITY_VERSION = "quote_observation_quality_v1_batch_b_20260923"

CLASS_MALFORMED = "MALFORMED"
CLASS_NONFINITE = "NONFINITE"
CLASS_CROSSED = "CROSSED"
CLASS_MISSING_SIDE = "MISSING_SIDE"
CLASS_STALE = "STALE"
CLASS_WIDE_BUT_POSSIBLE = "WIDE_BUT_POSSIBLE"
CLASS_OK = "OK"

DEFAULT_WIDE_SPREAD_RATIO = 0.25
DEFAULT_STALE_AGE_SECONDS = 120.0


def _finite(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def classify_quote_observation(
    bid: Any = None,
    ask: Any = None,
    ltp: Any = None,
    *,
    quote_age_seconds: Any = None,
    stale_after_seconds: float = DEFAULT_STALE_AGE_SECONDS,
    wide_spread_ratio: float = DEFAULT_WIDE_SPREAD_RATIO,
    raw_row: Any = None,
) -> Dict[str, Any]:
    reasons: List[str] = []
    bid_present = bid is not None and bid != ""
    ask_present = ask is not None and ask != ""

    for label, raw in (("bid", bid), ("ask", ask), ("ltp", ltp)):
        if raw is None or raw == "":
            continue
        try:
            float(raw)
        except (TypeError, ValueError):
            reasons.append(f"{label}_malformed")
    if reasons:
        return _pack(CLASS_MALFORMED, reasons, bid, ask, ltp, quote_age_seconds, raw_row)

    bid_f, ask_f, ltp_f = _finite(bid), _finite(ask), _finite(ltp)
    for label, raw, parsed in (("bid", bid, bid_f), ("ask", ask, ask_f), ("ltp", ltp, ltp_f)):
        if raw is None or raw == "":
            continue
        if parsed is None:
            reasons.append(f"{label}_nonfinite")
    if reasons:
        return _pack(CLASS_NONFINITE, reasons, bid, ask, ltp, quote_age_seconds, raw_row)

    if not bid_present or not ask_present:
        if not bid_present:
            reasons.append("bid_missing")
        if not ask_present:
            reasons.append("ask_missing")
        return _pack(CLASS_MISSING_SIDE, reasons, bid, ask, ltp, quote_age_seconds, raw_row)

    assert bid_f is not None and ask_f is not None
    if bid_f <= 0 or ask_f <= 0:
        if bid_f <= 0:
            reasons.append("bid_non_positive")
        if ask_f <= 0:
            reasons.append("ask_non_positive")
        return _pack(CLASS_MISSING_SIDE, reasons, bid, ask, ltp, quote_age_seconds, raw_row)

    if bid_f > ask_f:
        reasons.append("bid_gt_ask")
        return _pack(CLASS_CROSSED, reasons, bid, ask, ltp, quote_age_seconds, raw_row)

    age = _finite(quote_age_seconds)
    if age is not None and age > float(stale_after_seconds):
        reasons.append(f"quote_age_seconds>{stale_after_seconds}")
        return _pack(CLASS_STALE, reasons, bid, ask, ltp, quote_age_seconds, raw_row)

    mid = (bid_f + ask_f) / 2.0
    spread = ask_f - bid_f
    ratio = (spread / mid) if mid > 0 else None
    if ratio is not None and ratio >= float(wide_spread_ratio):
        reasons.append(f"spread_ratio>={wide_spread_ratio}")
        return _pack(
            CLASS_WIDE_BUT_POSSIBLE,
            reasons,
            bid,
            ask,
            ltp,
            quote_age_seconds,
            raw_row,
            extra={"spread": spread, "mid": mid, "spread_ratio": round(ratio, 6)},
        )

    return _pack(CLASS_OK, ["two_sided_finite_non_crossed"], bid, ask, ltp, quote_age_seconds, raw_row)


def classify_quote_rows(
    rows: Sequence[Dict[str, Any]],
    *,
    bid_key: str = "bid",
    ask_key: str = "ask",
    ltp_key: str = "ltp",
    age_key: str = "quote_age_seconds",
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            out.append(_pack(CLASS_MALFORMED, ["row_not_dict"], None, None, None, None, row))
            continue
        out.append(
            classify_quote_observation(
                row.get(bid_key),
                row.get(ask_key),
                row.get(ltp_key),
                quote_age_seconds=row.get(age_key),
                raw_row=row,
            )
        )
    return out


def _pack(
    classification: str,
    reasons: Sequence[str],
    bid: Any,
    ask: Any,
    ltp: Any,
    quote_age_seconds: Any,
    raw_row: Any,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = {
        "contract_version": QUOTE_OBSERVATION_QUALITY_VERSION,
        "classification": classification,
        "reasons": list(reasons),
        "bid": bid,
        "ask": ask,
        "ltp": ltp,
        "quote_age_seconds": quote_age_seconds,
        "raw_row_preserved": raw_row is not None,
        "structural_payoff_veto_applied": False,
        "loss_clamped_to_payoff": False,
        "teacher_production_filter": False,
    }
    if extra:
        payload.update(extra)
    return payload
