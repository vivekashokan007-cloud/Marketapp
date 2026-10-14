"""Pure dated canonical option fee contract, extracted for the implied-EV v1.2 experiment (C2).

Economics are IDENTICAL to brain._teacher_default_config() + brain._teacher_option_charge_rates()
+ the charge block of the teacher friction function (brain.py, 'brokerage = rates[...] * leg_count * 2.0'):
    brokerage = 20 per executed order
    exchange  = turnover * exchange_rate      (0.0003503 before 2026-03-01, 0.0003553 from)
    ipft      = turnover * 1e-9
    gst       = 18% * (brokerage + exchange + ipft)      <- GST is NOT applied to SEBI
    stt       = short-sell premium * stt_rate          (0.0010 before 2026-04-01, 0.0015 from)
    stamp     = buy-side premium * 0.00003
    sebi      = turnover * 1e-6
The research ev_ref.fees_rupees adds GST to SEBI and omits IPFT; it is NOT used.

Differences from the teacher function, all fail-closed:
  * the date is REQUIRED (no default to the STT cutover, no wall-clock fallback);
  * dates before FEE_SUPPORTED_FROM are rejected;
  * orders are counted per executed order (legs per side), independent of the number of lots;
    turnover scales with the actual quantity (units).
brain.py is not modified; tests/test_ev_v12_fees.py asserts equality with the teacher function.
"""
from __future__ import annotations

import math
from datetime import date

FEE_CONTRACT_VERSION = "ev_v12_fee_teacher_v2_dated_20261013"
TEACHER_ECONOMICS_REF = "teacher_friction_v2_executable_bid_ask_charges"
FEE_SUPPORTED_FROM = date(2026, 1, 1)
FEE_SUPPORTED_TO = date(2026, 12, 31)   # extend only with a reviewed rate-table entry

_BASE = {
    "brokerage_per_order": 20.0,
    "gst_rate": 0.18,
    "stamp_duty_buy_rate": 0.00003,
    "sebi_rate": 0.000001,
    "ipft_rate": 0.000000001,
}
_EXCHANGE_CUTOVER = date(2026, 3, 1)
_STT_CUTOVER = date(2026, 4, 1)


class FeeUnavailable(ValueError):
    pass


def _parse_date(d):
    if isinstance(d, date):
        return d
    if isinstance(d, str) and len(d) >= 10:
        try:
            return date.fromisoformat(d[:10])
        except ValueError:
            return None
    return None


def rates_for(trade_date):
    d = _parse_date(trade_date)
    if d is None:
        raise FeeUnavailable("FEE_DATE_UNAVAILABLE")
    if d < FEE_SUPPORTED_FROM or d > FEE_SUPPORTED_TO:
        raise FeeUnavailable("FEE_DATE_OUTSIDE_RATE_TABLE")
    r = dict(_BASE)
    r["exchange_rate"] = 0.0003553 if d >= _EXCHANGE_CUTOVER else 0.0003503
    r["stt_rate"] = 0.0015 if d >= _STT_CUTOVER else 0.0010
    r["rate_date"] = d.isoformat()
    r["exchange_rate_version"] = "exch_from_2026-03-01" if d >= _EXCHANGE_CUTOVER else "exch_pre_2026-03-01"
    r["stt_rate_version"] = "stt_from_2026-04-01" if d >= _STT_CUTOVER else "stt_pre_2026-04-01"
    return r


def one_side_fee(sell_premium_points, buy_premium_points, orders, units, trade_date):
    """Fee in rupees for ONE side (entry or exit) of a multi-leg order set.

    sell_premium_points / buy_premium_points: sums over legs of |qty_ratio| * price, points per unit.
    orders: executed orders on this side (one per leg; NOT multiplied by lots).
    units: contract lot size * number of lots.
    """
    for name, v in (("sell", sell_premium_points), ("buy", buy_premium_points), ("units", units)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise FeeUnavailable("FEE_BAD_INPUT_" + name.upper())
    if isinstance(orders, bool) or not isinstance(orders, int) or orders <= 0:
        raise FeeUnavailable("FEE_BAD_ORDER_COUNT")
    r = rates_for(trade_date)
    sell_r = sell_premium_points * units
    buy_r = buy_premium_points * units
    turnover = sell_r + buy_r
    brokerage = r["brokerage_per_order"] * orders
    exchange = turnover * r["exchange_rate"]
    ipft = turnover * r["ipft_rate"]
    gst = (brokerage + exchange + ipft) * r["gst_rate"]
    stt = sell_r * r["stt_rate"]
    stamp = buy_r * r["stamp_duty_buy_rate"]
    sebi = turnover * r["sebi_rate"]
    return brokerage + exchange + ipft + gst + stt + stamp + sebi


def fee_identity(trade_date):
    r = rates_for(trade_date)
    return {"fee_contract_version": FEE_CONTRACT_VERSION, "teacher_economics": TEACHER_ECONOMICS_REF,
            "rate_date": r["rate_date"], "exchange_rate_version": r["exchange_rate_version"],
            "stt_rate_version": r["stt_rate_version"]}
