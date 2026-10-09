"""Dated research replay fee model ``replay_fee_v2`` (research only; never bundled in the APK).

``replay_fee_v1`` in ``replay_engine.fees`` is preserved unchanged for exact reproduction of earlier
results. ``replay_fee_v2`` keeps the same component definitions (the transaction sides are identical)
but takes every rate from a dated, versioned table with primary-source references, charges each
side of the round trip at the rates in force on that side's trade date, and raises
``UnknownHistoricalRate`` for any date outside the verified windows. It never invents an earlier or
later rate.

Transaction sides (invariants, tested in tests/test_fee_models.py):
  * STT (seller pays, on premium):   short-entry sales + long-exit sales
  * Stamp duty (buyer pays):         long-entry purchases + short-exit buybacks
  * Exchange txn / IPFT / SEBI:      all four amounts (turnover)
  * Brokerage (declared model):      one order per leg per side (entry, exit)
  * GST:                             18% of brokerage + exchange transaction charge + IPFT
  * Gross P&L:                       entry sales - entry purchases + exit sales - exit buybacks
Bid/ask execution already charges the spread; no further slippage is added.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, Optional, Tuple

REPLAY_FEE_V2 = 'replay_fee_v2'
RATES_TABLE_VERSION = 'nse_equity_index_options_rates_20261009_v1'
# Last date on which every rate below was checked against its source. A trade date after this is
# unknown (a later circular may have changed a rate) and fails explicitly.
RATES_VERIFIED_THROUGH = date(2026, 10, 9)

SOURCES = {
    'stt_2024_10_01': 'NSE/FATAX/63809 (9 Sep 2024): option sale 0.10% w.e.f. 1 Oct 2024 (was 0.0625%), payable by seller on premium. https://nsearchives.nseindia.com/content/circulars/FATAX63809.pdf',
    'stt_2026_04_01': 'NSE/FATAX/73524 (31 Mar 2026), Finance Act 2026: option sale 0.15% w.e.f. 1 Apr 2026 (was 0.10%), payable by seller. https://www.nseindia.com/static/products-services/equity-derivatives-securities-transaction-tax',
    'txn_2024_10_01': 'NSE/FA/64232 (27 Sep 2024): equity options Rs 35.03 per lakh of premium each side w.e.f. 1 Oct 2024 (Rs 3,503/crore); IPFT Rs 50/crore each side per NSE/FA/73061 table "existing outflow". https://nsearchives.nseindia.com/content/circulars/FA64232.pdf',
    'txn_2026_03_01': 'NSE/FA/73061 (27 Feb 2026): equity options transaction charge Rs 3,552.99/crore + NSE IPFT Rs 0.01/crore each side = Rs 3,553 w.e.f. 1 Mar 2026. https://nsearchives.nseindia.com/content/circulars/FA73061.pdf',
    'stamp_2020_07_01': 'Indian Stamp Act 1899 as amended by Finance Act 2019, notified 1 Jul 2020: equity options 0.003% on buyer. https://www.nseindia.com/static/invest/first-time-investor-stamp-duty-charges-taxes',
    'sebi_fee': 'SEBI (Stock Brokers) Regs Sch. V as amended by SEBI/LAD-NRO/GN/2019/03 w.e.f. 3 Apr 2019: 0.0001% of turnover (Rs 10/crore); options turnover = premium. https://www.nseindia.com/static/invest/first-time-investor-sebi-turnover-fees-stt-other-levies',
    'gst_and_brokerage': 'Declared broker model (Upstox tariff): equity options flat Rs 20 per executed order; GST 18% on brokerage + transaction charges + IPFT. https://upstox.com/brokerage-charges/ (broker tariff, not a statutory rate; checked 9 Oct 2026).',
}


class UnknownHistoricalRate(ValueError):
    """Raised when a required rate is not known for a trade date. Never defaulted."""


@dataclass(frozen=True)
class RateWindow:
    window_id: str
    start: date                     # inclusive
    end: date                       # inclusive
    stt_option_sale: float          # fraction of premium, seller
    exchange_txn: float             # fraction of premium turnover, each side
    ipft: float                     # fraction of premium turnover, each side
    sebi: float                     # fraction of premium turnover
    stamp_option_buy: float         # fraction of premium, buyer
    gst: float                      # on brokerage + exchange txn + ipft
    brokerage_per_order: float      # INR per executed order (declared broker model)
    sources: Tuple[str, ...]

    def as_dict(self) -> dict:
        return {'window_id': self.window_id, 'start': self.start.isoformat(), 'end': self.end.isoformat(),
                'stt_option_sale': self.stt_option_sale, 'exchange_txn': self.exchange_txn, 'ipft': self.ipft,
                'sebi': self.sebi, 'stamp_option_buy': self.stamp_option_buy, 'gst': self.gst,
                'brokerage_per_order': self.brokerage_per_order, 'sources': list(self.sources)}


_CRORE = 1e7
_COMMON = dict(sebi=10 / _CRORE, stamp_option_buy=0.00003, gst=0.18, brokerage_per_order=20.0)
RATE_WINDOWS: Tuple[RateWindow, ...] = (
    RateWindow('nse_idx_opt_20241001', date(2024, 10, 1), date(2026, 2, 28), stt_option_sale=0.0010,
               exchange_txn=3503 / _CRORE, ipft=50 / _CRORE,
               sources=('stt_2024_10_01', 'txn_2024_10_01', 'stamp_2020_07_01', 'sebi_fee', 'gst_and_brokerage'),
               **_COMMON),
    RateWindow('nse_idx_opt_20260301', date(2026, 3, 1), date(2026, 3, 31), stt_option_sale=0.0010,
               exchange_txn=3552.99 / _CRORE, ipft=0.01 / _CRORE,
               sources=('stt_2024_10_01', 'txn_2026_03_01', 'stamp_2020_07_01', 'sebi_fee', 'gst_and_brokerage'),
               **_COMMON),
    RateWindow('nse_idx_opt_20260401', date(2026, 4, 1), RATES_VERIFIED_THROUGH, stt_option_sale=0.0015,
               exchange_txn=3552.99 / _CRORE, ipft=0.01 / _CRORE,
               sources=('stt_2026_04_01', 'txn_2026_03_01', 'stamp_2020_07_01', 'sebi_fee', 'gst_and_brokerage'),
               **_COMMON),
)


def _check_windows(windows: Tuple[RateWindow, ...]) -> None:
    for a, b in zip(windows, windows[1:]):
        if (b.start - a.end).days != 1:
            raise AssertionError(f'rate windows not contiguous: {a.window_id} -> {b.window_id}')


_check_windows(RATE_WINDOWS)


def rates_for(trade_date: date, windows: Tuple[RateWindow, ...] = RATE_WINDOWS) -> RateWindow:
    if not isinstance(trade_date, date):
        raise UnknownHistoricalRate(f'trade date required, got {trade_date!r}')
    for w in windows:
        if w.start <= trade_date <= w.end:
            return w
    raise UnknownHistoricalRate(
        f'no verified NSE index-option rate window for {trade_date.isoformat()} '
        f'(known {windows[0].start.isoformat()}..{windows[-1].end.isoformat()}, table {RATES_TABLE_VERSION})')


def _side_charges(w: RateWindow, sales: float, purchases: float, orders: int) -> dict:
    """Charges for one trade date. ``sales``/``purchases`` are INR premium values on that date."""
    turnover = sales + purchases
    brokerage = w.brokerage_per_order * orders
    exchange = turnover * w.exchange_txn
    ipft = turnover * w.ipft
    gst = (brokerage + exchange + ipft) * w.gst
    stt = sales * w.stt_option_sale
    stamp = purchases * w.stamp_option_buy
    sebi = turnover * w.sebi
    total = brokerage + exchange + ipft + gst + stt + stamp + sebi
    return {'window_id': w.window_id, 'turnover': turnover, 'stt_base_sales': sales,
            'stamp_base_purchases': purchases, 'orders': orders, 'brokerage': brokerage, 'exchange': exchange,
            'ipft': ipft, 'gst': gst, 'stt': stt, 'stamp': stamp, 'sebi': sebi, 'total': total}


def fees_v2(entry_date: date, exit_date: date, legs: int, lot: int, entry: dict, exit_: dict, *,
            lot_authority: Optional[dict] = None, quote_ts: Optional[Dict[str, str]] = None,
            windows: Tuple[RateWindow, ...] = RATE_WINDOWS) -> dict:
    """``entry``/``exit_`` use replay_engine's per-unit point dicts:
    entry = {'sell_px': short-leg bids summed, 'buy_px': long-leg asks summed}
    exit_ = {'exit_sell_px': long-leg bids summed, 'exit_buy_px': short-leg asks summed}.
    """
    if not lot or lot <= 0:
        raise UnknownHistoricalRate(f'lot authority required, got {lot!r}')
    if exit_date < entry_date:
        raise ValueError('exit before entry')
    we, wx = rates_for(entry_date, windows), rates_for(exit_date, windows)
    e = _side_charges(we, entry['sell_px'] * lot, entry['buy_px'] * lot, legs)
    x = _side_charges(wx, exit_['exit_sell_px'] * lot, exit_['exit_buy_px'] * lot, legs)
    out = {'model': REPLAY_FEE_V2, 'rates_table_version': RATES_TABLE_VERSION,
           'entry_date': entry_date.isoformat(), 'exit_date': exit_date.isoformat(), 'lot': lot, 'legs': legs,
           'lot_authority': lot_authority, 'quote_ts': quote_ts,
           'entry_rates': we.as_dict(), 'exit_rates': wx.as_dict(), 'entry_side': e, 'exit_side': x,
           'sources': {k: SOURCES[k] for k in sorted(set(we.sources) | set(wx.sources))}}
    for k in ('brokerage', 'exchange', 'ipft', 'gst', 'stt', 'stamp', 'sebi', 'total', 'turnover'):
        out[k] = e[k] + x[k]
    out['stt_base'] = e['stt_base_sales'] + x['stt_base_sales']          # short-entry + long-exit sales
    out['stamp_base'] = e['stamp_base_purchases'] + x['stamp_base_purchases']  # long-entry + short-exit buys
    return out


def gross_inr(lot: int, entry: dict, exit_: dict) -> float:
    """entry sales - entry purchases + exit sales - exit buybacks."""
    return (entry['sell_px'] - entry['buy_px'] + exit_['exit_sell_px'] - exit_['exit_buy_px']) * lot
