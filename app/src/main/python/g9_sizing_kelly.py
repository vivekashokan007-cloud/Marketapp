"""G9 advisory Kelly sizing research (no promote).

Fractional / Bayesian / capped Kelly equivalents with exposure, margin,
loss, liquidity and concentration limits. Output is experimental and
advisory only — it must never silently change live trade quantity or
risk limits. The live p_ml gate is untouched.

Promotion: insufficient multi-session evidence => no promotion recommendation.
"""

from __future__ import annotations

import math
from typing import Any, Optional

G9_SIZING_VERSION = "g9_sizing_kelly_advisory_v1_20260913"
G9_EXPERIMENT_STATUS = "experimental_advisory_only"
G9_PROMOTION_STATUS_INSUFFICIENT = "no_promotion_insufficient_multi_session_evidence"
G9_PROMOTION_STATUS_BLOCKED = "promotion_blocked_by_contract"

DEFAULT_CAPITAL = 250_000.0
DEFAULT_MAX_RISK_PCT = 0.10
DEFAULT_FRACTIONAL_KELLY = 0.25  # quarter-Kelly safety fraction
DEFAULT_MAX_LOTS = 4
DEFAULT_MIN_LOTS = 1
DEFAULT_CONCENTRATION_PCT = 0.25  # max fraction of capital in one index
DEFAULT_LIQUIDITY_LOT_CAP = 6
MIN_SESSIONS_FOR_PROMOTION_REVIEW = 20
MIN_CLOSED_TRADES_FOR_PROMOTION_REVIEW = 60


def _finite(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        out = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out):
        return default
    return out


def raw_kelly_fraction(*, win_prob: float, win_loss_ratio: float) -> Optional[float]:
    """Classic Kelly f* = p - (1-p)/b for even-payoff generalization b=win/loss."""
    p = _finite(win_prob)
    b = _finite(win_loss_ratio)
    if p is None or b is None or b <= 0:
        return None
    if p < 0 or p > 1:
        return None
    return p - ((1.0 - p) / b)


def bayesian_kelly_fraction(
    *,
    wins: int,
    losses: int,
    win_loss_ratio: float,
    alpha_prior: float = 1.0,
    beta_prior: float = 1.0,
) -> Optional[float]:
    """Posterior-mean win probability under Beta prior, then Kelly."""
    try:
        w = max(int(wins), 0)
        l = max(int(losses), 0)
    except (TypeError, ValueError):
        return None
    alpha = float(alpha_prior) + w
    beta = float(beta_prior) + l
    p = alpha / (alpha + beta)
    return raw_kelly_fraction(win_prob=p, win_loss_ratio=win_loss_ratio)


def apply_risk_caps(
    kelly_f: float,
    *,
    capital: float = DEFAULT_CAPITAL,
    max_risk_pct: float = DEFAULT_MAX_RISK_PCT,
    fractional: float = DEFAULT_FRACTIONAL_KELLY,
    per_trade_max_loss: Optional[float] = None,
    margin_required: Optional[float] = None,
    open_exposure: float = 0.0,
    index_exposure: float = 0.0,
    concentration_pct: float = DEFAULT_CONCENTRATION_PCT,
    liquidity_lot_cap: int = DEFAULT_LIQUIDITY_LOT_CAP,
    lot_max_loss: Optional[float] = None,
    min_lots: int = DEFAULT_MIN_LOTS,
    max_lots: int = DEFAULT_MAX_LOTS,
) -> dict:
    caps_hit = []
    f_raw = max(0.0, float(kelly_f))
    f_frac = f_raw * float(fractional)
    if f_frac > max_risk_pct:
        f_frac = max_risk_pct
        caps_hit.append("max_risk_pct")

    budget = capital * f_frac
    remaining_capital_risk = max(capital * max_risk_pct - max(0.0, open_exposure), 0.0)
    if budget > remaining_capital_risk:
        budget = remaining_capital_risk
        caps_hit.append("portfolio_exposure")

    concentration_budget = max(capital * concentration_pct - max(0.0, index_exposure), 0.0)
    if budget > concentration_budget:
        budget = concentration_budget
        caps_hit.append("concentration")

    if margin_required is not None and margin_required > 0:
        margin_budget_lots = int(remaining_capital_risk // margin_required) if margin_required else 0
    else:
        margin_budget_lots = max_lots

    if lot_max_loss is not None and lot_max_loss > 0 and budget > 0:
        loss_budget_lots = int(budget // lot_max_loss)
    elif per_trade_max_loss is not None and per_trade_max_loss > 0 and lot_max_loss is None:
        # Treat per_trade_max_loss as 1-lot loss proxy when lot loss unknown.
        loss_budget_lots = int(budget // per_trade_max_loss)
    else:
        loss_budget_lots = max_lots

    advisory_lots = min(
        max_lots,
        liquidity_lot_cap,
        max(margin_budget_lots, 0),
        max(loss_budget_lots, 0),
    )
    if advisory_lots < min_lots:
        # Below minimum meaningful size — advise abstention rather than forcing 1 lot.
        advisory_lots = 0
        caps_hit.append("below_min_lot_after_caps")
    if advisory_lots > liquidity_lot_cap:
        advisory_lots = liquidity_lot_cap
        caps_hit.append("liquidity")
    if advisory_lots == max_lots:
        caps_hit.append("max_lots")

    return {
        "kelly_f_raw": round(f_raw, 6),
        "kelly_f_fractional": round(f_frac, 6),
        "risk_budget_rupees": round(budget, 2),
        "advisory_lots": int(advisory_lots),
        "caps_hit": list(dict.fromkeys(caps_hit)),
        "margin_budget_lots": int(max(margin_budget_lots, 0)),
        "loss_budget_lots": int(max(loss_budget_lots, 0)),
    }


def advise_kelly_size(
    *,
    win_prob: Optional[float] = None,
    win_loss_ratio: Optional[float] = None,
    wins: Optional[int] = None,
    losses: Optional[int] = None,
    method: str = "fractional",
    capital: float = DEFAULT_CAPITAL,
    max_risk_pct: float = DEFAULT_MAX_RISK_PCT,
    fractional: float = DEFAULT_FRACTIONAL_KELLY,
    per_trade_max_loss: Optional[float] = None,
    margin_required: Optional[float] = None,
    open_exposure: float = 0.0,
    index_exposure: float = 0.0,
    lot_max_loss: Optional[float] = None,
    observed_sessions: int = 0,
    observed_closed_trades: int = 0,
    live_quantity: Optional[int] = None,
) -> dict:
    method_norm = str(method or "fractional").strip().lower()
    if method_norm in ("bayesian", "bayes", "bayesian_kelly"):
        kelly = bayesian_kelly_fraction(
            wins=int(wins or 0),
            losses=int(losses or 0),
            win_loss_ratio=float(win_loss_ratio or 0.0),
        )
        method_used = "bayesian_kelly"
    else:
        kelly = raw_kelly_fraction(
            win_prob=float(win_prob or 0.0),
            win_loss_ratio=float(win_loss_ratio or 0.0),
        )
        method_used = "fractional_kelly"

    if kelly is None or kelly <= 0:
        sized = {
            "kelly_f_raw": 0.0 if kelly is None else round(float(kelly), 6),
            "kelly_f_fractional": 0.0,
            "risk_budget_rupees": 0.0,
            "advisory_lots": 0,
            "caps_hit": ["non_positive_kelly"],
            "margin_budget_lots": 0,
            "loss_budget_lots": 0,
        }
    else:
        sized = apply_risk_caps(
            kelly,
            capital=capital,
            max_risk_pct=max_risk_pct,
            fractional=fractional,
            per_trade_max_loss=per_trade_max_loss,
            margin_required=margin_required,
            open_exposure=open_exposure,
            index_exposure=index_exposure,
            lot_max_loss=lot_max_loss,
        )

    enough_sessions = int(observed_sessions or 0) >= MIN_SESSIONS_FOR_PROMOTION_REVIEW
    enough_trades = int(observed_closed_trades or 0) >= MIN_CLOSED_TRADES_FOR_PROMOTION_REVIEW
    if enough_sessions and enough_trades:
        promotion = G9_PROMOTION_STATUS_BLOCKED  # evidence may exist but adoption still requires separate review
        promotion_note = (
            "Support thresholds met for review only; G9 remains advisory and must not "
            "auto-promote into the live quantity path."
        )
    else:
        promotion = G9_PROMOTION_STATUS_INSUFFICIENT
        promotion_note = (
            f"Need >= {MIN_SESSIONS_FOR_PROMOTION_REVIEW} sessions and "
            f">= {MIN_CLOSED_TRADES_FOR_PROMOTION_REVIEW} closed trades before any "
            "promotion review; currently insufficient multi-session evidence."
        )

    return {
        "version": G9_SIZING_VERSION,
        "status": G9_EXPERIMENT_STATUS,
        "method": method_used,
        "advisory_lots": sized["advisory_lots"],
        "kelly": sized,
        "live_path": {
            "mutates_trade_quantity": False,
            "mutates_risk_limits": False,
            "p_ml_gate_unchanged": True,
            "live_quantity_passthrough": live_quantity if live_quantity is not None else 1,
            "label": "EXPERIMENTAL_ADVISORY_ONLY — do not apply to Real orders",
        },
        "promotion": {
            "status": promotion,
            "note": promotion_note,
            "observed_sessions": int(observed_sessions or 0),
            "observed_closed_trades": int(observed_closed_trades or 0),
            "min_sessions_required": MIN_SESSIONS_FOR_PROMOTION_REVIEW,
            "min_closed_trades_required": MIN_CLOSED_TRADES_FOR_PROMOTION_REVIEW,
            "recommend_promote": False,
        },
        "inputs_basis": {
            "real_only_for_live_risk_inputs": True,
            "paper_for_forward_experiment": True,
            "capital": capital,
            "max_risk_pct": max_risk_pct,
            "fractional_kelly": fractional,
        },
    }
