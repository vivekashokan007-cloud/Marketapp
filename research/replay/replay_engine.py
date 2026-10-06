"""Offline holding-horizon replay engine (research only; never bundled in the APK).

Scope (Codex review gate, 6 Oct 2026, Batch 2):
- Two explicit definition sets: LEGACY_V0 reproduces the 5 Oct exploratory
  sweep (Stage A, provenance fixture) and CORRECTED_V1 applies the corrected
  research contract (Stage B). Neither is a production decision path.
- Candidate eligibility at decision time is computed first; future outcomes are
  joined afterwards with explicit statuses, so a missing exit never erases an
  entry opportunity.
- holding_sessions, exit_on_expiry, planned exit and actual valuation time are
  stored separately. Expiry identity is preserved through every step.
- Sequential (one position at a time) occupancy never frees capital on an
  unpriced exit: conservative blocking until a declared resolution.
- Conservation counts are published for every run.

Input: a compact, hash-verified quote extract (see EXTRACT_FORMAT.md).
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30))
ENGINE_VERSION = 'replay_engine_v1_20261006'

# NSE trading holidays 2026, copied from brain.py _CONST['NSE_HOLIDAYS'] at 857dedc.
# A test asserts equality with brain.py so the two cannot drift silently.
NSE_HOLIDAYS_2026 = (
    '2026-01-26', '2026-03-03', '2026-03-26', '2026-03-31', '2026-04-03', '2026-04-14',
    '2026-05-01', '2026-05-28', '2026-06-26', '2026-09-14', '2026-10-02', '2026-10-20',
    '2026-11-10', '2026-11-24', '2026-12-25',
)

# Dated contract authority. Only explicitly verified windows are allowed; an
# unknown index or date fails closed (no `else BNF` fallback).
# NF 65 / BNF 30 verified from trades_v2 entry_snapshot.contract_lot_size and
# friction_breakdown_json.lot_size for paper trades in Aug-Oct 2026, and from the
# lot size implied by max_profit/net_premium on 2026-09-17 generated candidates.
CONTRACT_AUTHORITY = {
    'NF': {'strike_step': 50, 'lots': [(date(2026, 6, 1), date(2026, 12, 31), 65)]},
    'BNF': {'strike_step': 100, 'lots': [(date(2026, 6, 1), date(2026, 12, 31), 30)]},
}


def lot_size(index: str, on: date) -> Optional[int]:
    spec = CONTRACT_AUTHORITY.get(index)
    if not spec:
        return None
    for start, end, lot in spec['lots']:
        if start <= on <= end:
            return lot
    return None


def strike_step(index: str) -> Optional[int]:
    spec = CONTRACT_AUTHORITY.get(index)
    return spec['strike_step'] if spec else None


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Definitions:
    name: str
    calendar: str              # 'observed' (brain snapshot dates) | 'exchange' (weekdays minus NSE holidays)
    td_time_basis: str         # 'nominal_slot' | 'actual_poll'
    td_day_basis: str          # 'weekdays' | 'exchange_sessions'
    rv_rule: str               # 'any_available' | 'five_consecutive'
    fee_model: str             # 'legacy_sweep_v0' | 'teacher_v1' | 'replay_fee_v1'
    entry_slot: time = time(12, 30)
    close_slot: time = time(15, 20)
    slot_window_minutes: int = 25
    nominal_entry_time: time = time(12, 30)


LEGACY_V0 = Definitions(
    name='legacy_v0_sweep_20261005', calendar='observed', td_time_basis='nominal_slot',
    td_day_basis='weekdays', rv_rule='any_available', fee_model='legacy_sweep_v0')

CORRECTED_V1 = Definitions(
    name='corrected_v1_20261006', calendar='exchange', td_time_basis='actual_poll',
    td_day_basis='exchange_sessions', rv_rule='five_consecutive', fee_model='replay_fee_v1')


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Chain:
    poll_ts: datetime          # aware UTC
    slot: str                  # 'E' | 'C' | 'L' (as extracted)
    index: str
    expiry: date
    quotes: Dict[Tuple[int, str], Tuple[Optional[float], Optional[float]]]

    @property
    def session_date(self) -> date:
        return self.poll_ts.astimezone(IST).date()

    @property
    def ist_time(self) -> time:
        return self.poll_ts.astimezone(IST).time()


@dataclass
class Dataset:
    observed_sessions: List[date]
    chains: List[Chain]
    meta: Dict[str, str]
    sha256: str


def _num(text: str) -> Optional[float]:
    text = text.strip()
    if text == '':
        return None
    value = float(text)
    return value if math.isfinite(value) else None


def parse_extract(text: str) -> Dataset:
    """Parse the compact extract. Raises on malformed lines (never guesses)."""
    observed, chains, meta = [], [], {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#'):
            if '=' in line:
                k, v = line[1:].split('=', 1)
                meta[k.strip()] = v.strip()
            continue
        kind, rest = line.split('|', 1)
        if kind == 'S':
            observed.append(date.fromisoformat(rest.strip()))
        elif kind == 'P':
            ts, slot, index, expiry, body = rest.split('|', 4)
            quotes = {}
            for item in body.split(';'):
                if not item:
                    continue
                k, vals = item.split(':')
                cb, ca, pb, pa = vals.split(',')
                quotes[(int(k), 'CE')] = (_num(cb), _num(ca))
                quotes[(int(k), 'PE')] = (_num(pb), _num(pa))
            chains.append(Chain(
                poll_ts=datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone(timezone.utc),
                slot=slot, index=index, expiry=date.fromisoformat(expiry), quotes=quotes))
        else:
            raise ValueError(f'unknown extract line kind: {kind!r}')
    return Dataset(sorted(set(observed)), chains, meta, hashlib.sha256(text.encode('utf-8')).hexdigest())


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------
class Calendar:
    def __init__(self, defs: Definitions, observed: List[date], start: date, end: date):
        self.defs = defs
        if defs.calendar == 'observed':
            self.sessions = [d for d in sorted(set(observed))]
        else:
            holidays = {date.fromisoformat(h) for h in NSE_HOLIDAYS_2026}
            out, cur = [], start
            while cur <= end:
                if cur.weekday() < 5 and cur not in holidays:
                    out.append(cur)
                cur += timedelta(days=1)
            self.sessions = out
        self.pos = {d: i for i, d in enumerate(self.sessions)}

    def offset(self, d: date, n: int) -> Optional[date]:
        i = self.pos.get(d)
        if i is None or not (0 <= i + n < len(self.sessions)):
            return None
        return self.sessions[i + n]

    def days_after_until(self, d: date, expiry: date) -> int:
        """Sessions strictly after d up to and including expiry."""
        if self.defs.td_day_basis == 'weekdays':
            n, cur = 0, d + timedelta(days=1)
            while cur <= expiry:
                if cur.weekday() < 5:
                    n += 1
                cur += timedelta(days=1)
            return n
        holidays = {date.fromisoformat(h) for h in NSE_HOLIDAYS_2026}
        n, cur = 0, d + timedelta(days=1)
        while cur <= expiry:
            if cur.weekday() < 5 and cur not in holidays:
                n += 1
            cur += timedelta(days=1)
        return n


# ---------------------------------------------------------------------------
# Chain selection, quote validity, ATM
# ---------------------------------------------------------------------------
def valid_quote(q: Optional[Tuple[Optional[float], Optional[float]]]) -> bool:
    if not q:
        return False
    bid, ask = q
    return bid is not None and ask is not None and bid >= 0 and ask > 0 and ask >= bid


def select_chain(chains: List[Chain], index: str, session: date) -> Tuple[Optional[Chain], List[str]]:
    """One explicitly selected expiry per index/poll: the nearest expiry >= session.
    Conflicting duplicate rows for the same poll/expiry invalidate the poll."""
    flags: List[str] = []
    cands = [c for c in chains if c.index == index and c.expiry >= session]
    if not cands:
        return None, ['no_chain_for_index']
    expiries = sorted({c.expiry for c in cands})
    if len(expiries) > 1:
        flags.append('multi_expiry_nearest_selected')
    chosen = [c for c in cands if c.expiry == expiries[0]]
    if len(chosen) > 1:
        first = chosen[0].quotes
        if any(c.quotes != first for c in chosen[1:]):
            return None, flags + ['duplicate_chain_conflict']
        flags.append('duplicate_chain_identical')
    return chosen[0], flags


def atm(chain: Chain) -> Optional[Tuple[int, float, float]]:
    """(K0, synthetic spot, ATM straddle mid). Ties broken by lower strike."""
    best = None
    for (k, opt), q in chain.quotes.items():
        if opt != 'CE':
            continue
        p = chain.quotes.get((k, 'PE'))
        if not (valid_quote(q) and valid_quote(p)):
            continue
        cm, pm = (q[0] + q[1]) / 2, (p[0] + p[1]) / 2
        key = (abs(cm - pm), k)
        if best is None or key < best[0]:
            best = (key, k, k + cm - pm, cm + pm)
    if best is None:
        return None
    return best[1], best[2], best[3]


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Leg:
    strike: int
    opt: str      # 'CE' | 'PE'
    side: int     # -1 sell, +1 buy


@dataclass(frozen=True)
class Structure:
    kind: str     # IB | IC | BC | BP | BLC | BRP
    legs: Tuple[Leg, ...]
    width: int    # points at risk per side (wing width)
    credit_expected: bool


def iron_butterfly(k0: int, wing: int) -> Structure:
    return Structure('IB', (Leg(k0, 'CE', -1), Leg(k0, 'PE', -1), Leg(k0 + wing, 'CE', 1), Leg(k0 - wing, 'PE', 1)),
                     wing, True)


def iron_condor(k0: int, short_off: int, wing: int) -> Structure:
    return Structure('IC', (Leg(k0 + short_off, 'CE', -1), Leg(k0 + short_off + wing, 'CE', 1),
                            Leg(k0 - short_off, 'PE', -1), Leg(k0 - short_off - wing, 'PE', 1)), wing, True)


def vertical(kind: str, a: int, b: int) -> Structure:
    """BC: sell CE a, buy CE b (b>a). BP: sell PE a, buy PE b (b<a).
    BLC: buy CE a, sell CE b (b>a). BRP: buy PE a, sell PE b (b<a)."""
    if kind == 'BC':
        return Structure('BC', (Leg(a, 'CE', -1), Leg(b, 'CE', 1)), abs(b - a), True)
    if kind == 'BP':
        return Structure('BP', (Leg(a, 'PE', -1), Leg(b, 'PE', 1)), abs(b - a), True)
    if kind == 'BLC':
        return Structure('BLC', (Leg(a, 'CE', 1), Leg(b, 'CE', -1)), abs(b - a), False)
    if kind == 'BRP':
        return Structure('BRP', (Leg(a, 'PE', 1), Leg(b, 'PE', -1)), abs(b - a), False)
    raise ValueError(kind)


def validate_structure(s: Structure) -> List[str]:
    """Construction validity (independent of prices)."""
    reasons = []
    if s.width <= 0:
        reasons.append('nonpositive_width')
    strikes = [l.strike for l in s.legs]
    if len(set((l.strike, l.opt, l.side) for l in s.legs)) != len(s.legs):
        reasons.append('duplicate_leg')
    if s.kind in ('IB', 'IC'):
        if len(s.legs) != 4 or sum(1 for l in s.legs if l.side == -1) != 2:
            reasons.append('four_leg_shape_invalid')
        ce = sorted((l.strike, l.side) for l in s.legs if l.opt == 'CE')
        pe = sorted((l.strike, l.side) for l in s.legs if l.opt == 'PE')
        if len(ce) != 2 or len(pe) != 2 or ce[0][1] != -1 or ce[1][1] != 1 or pe[0][1] != 1 or pe[1][1] != -1:
            reasons.append('wing_orientation_invalid')
        if s.kind == 'IB' and len({l.strike for l in s.legs if l.side == -1}) != 1:
            reasons.append('ib_shorts_not_same_strike')
    else:
        if len(s.legs) != 2 or {l.opt for l in s.legs} .__len__() != 1:
            reasons.append('vertical_shape_invalid')
    if any(k <= 0 for k in strikes):
        reasons.append('nonpositive_strike')
    return reasons


def price_validity(s: Structure, credit_pts: float) -> List[str]:
    """Economic validity after pricing: sign and payoff bounds."""
    reasons = []
    if s.credit_expected:
        if not credit_pts > 0:
            reasons.append('credit_structure_nonpositive_credit')
        elif not credit_pts < s.width:
            reasons.append('credit_not_below_width')
    else:
        debit = -credit_pts
        if not debit > 0:
            reasons.append('debit_structure_nonpositive_debit')
        elif not debit < s.width:
            reasons.append('debit_not_below_width')
    return reasons


# ---------------------------------------------------------------------------
# Pricing and fees
# ---------------------------------------------------------------------------
def price_entry(s: Structure, chain: Chain) -> Tuple[Optional[dict], List[str]]:
    sells, buys, missing = 0.0, 0.0, []
    for l in s.legs:
        q = chain.quotes.get((l.strike, l.opt))
        if l.side == -1:
            if not (valid_quote(q) and q[0] > 0):
                missing.append(f'{l.opt}{l.strike}_bid')
                continue
            sells += q[0]
        else:
            if not valid_quote(q):
                missing.append(f'{l.opt}{l.strike}_ask')
                continue
            buys += q[1]
    if missing:
        return None, missing
    return {'credit_pts': sells - buys, 'sell_px': sells, 'buy_px': buys}, []


def price_exit(s: Structure, chain: Chain) -> Tuple[Optional[dict], List[str]]:
    buyback, sellout, missing = 0.0, 0.0, []
    for l in s.legs:
        q = chain.quotes.get((l.strike, l.opt))
        if not valid_quote(q):
            missing.append(f'{l.opt}{l.strike}')
            continue
        if l.side == -1:
            buyback += q[1]
        else:
            sellout += q[0]
    if missing:
        return None, missing
    return {'exit_cash_pts': sellout - buyback, 'exit_sell_px': sellout, 'exit_buy_px': buyback}, []


def fees(model: str, legs: int, lot: int, entry: dict, exit_: dict) -> dict:
    sell_entry = entry['sell_px'] * lot          # short legs sold at entry
    buy_entry = entry['buy_px'] * lot            # long legs bought at entry
    sell_exit = exit_['exit_sell_px'] * lot      # long legs sold at exit
    buy_exit = exit_['exit_buy_px'] * lot        # short legs bought back at exit
    turnover = sell_entry + buy_entry + sell_exit + buy_exit
    if model == 'legacy_sweep_v0':
        total = (legs * 2 * 20 * 1.18 + 0.0015 * (sell_entry + sell_exit)
                 + 0.0003553 * 1.18 * turnover + 0.00003 * (buy_entry + buy_exit))
        return {'model': model, 'total': total}
    if model not in ('teacher_v1', 'replay_fee_v1'):
        raise ValueError(model)
    brokerage = 20.0 * legs * 2
    exchange = turnover * 0.0003553
    ipft = turnover * 0.000000001
    gst = (brokerage + exchange + ipft) * 0.18
    stt_base = sell_entry if model == 'teacher_v1' else (sell_entry + sell_exit)
    stt = stt_base * 0.0015
    stamp = (buy_entry + buy_exit) * 0.00003
    sebi = turnover * 0.000001
    total = brokerage + exchange + ipft + gst + stt + stamp + sebi
    return {'model': model, 'total': total, 'brokerage': brokerage, 'exchange': exchange, 'gst': gst,
            'stt': stt, 'stamp': stamp, 'sebi': sebi, 'ipft': ipft, 'stt_base': stt_base}


# ---------------------------------------------------------------------------
# Rule and run
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Rule:
    name: str
    index: str = 'NF'
    structure: str = 'IB'      # IB | IC
    wing: int = 400
    short_off: int = 0
    ivrv_lo: float = 1.3
    ivrv_hi: float = 1.7
    holding_sessions: int = 2


def _first_in_window(chains: List[Chain], session: date, slot_t: time, minutes: int, index: str):
    start = datetime.combine(session, slot_t, IST)
    end = start + timedelta(minutes=minutes)
    pool = [c for c in chains if c.index == index and start <= c.poll_ts.astimezone(IST) < end]
    if not pool:
        return None
    first_ts = min(c.poll_ts for c in pool)
    return [c for c in pool if c.poll_ts == first_ts]


def run(dataset: Dataset, defs: Definitions, rule: Rule, start: date, end: date, cutoff: date) -> dict:
    by_day: Dict[date, List[Chain]] = {}
    for c in dataset.chains:
        by_day.setdefault(c.session_date, []).append(c)
    cal_start = start - timedelta(days=20)
    cal = Calendar(defs, dataset.observed_sessions, cal_start, cutoff + timedelta(days=10))

    def chain_at(session: date, slot_t: time):
        pool = _first_in_window(by_day.get(session, []), session, slot_t, defs.slot_window_minutes, rule.index)
        if pool is None:
            return None, ['no_poll_in_window']
        return select_chain(pool, rule.index, session)

    close_cache: Dict[date, Tuple[Optional[float], List[str]]] = {}

    def close_spot(session: date):
        if session not in close_cache:
            ch, flags = chain_at(session, defs.close_slot)
            a = atm(ch) if ch else None
            close_cache[session] = (a[1] if a else None, flags + ([] if a else ['close_atm_unavailable']))
        return close_cache[session]

    def rv_for(session: date):
        prior = [cal.offset(session, -i) for i in range(6, 0, -1)]   # sn-6 .. sn-1
        if any(p is None for p in prior):
            return None, 0, 'calendar_history_short'
        spots = [close_spot(p)[0] for p in prior]
        rets = []
        for a, b in zip(spots[:-1], spots[1:]):
            if a is not None and b is not None and a > 0 and b > 0:
                rets.append(math.log(b / a))
        if defs.rv_rule == 'five_consecutive':
            if len(rets) != 5:
                return None, len(rets), 'rv_insufficient_consecutive_closes'
        elif not rets:
            return None, 0, 'rv_no_returns'
        rv = math.sqrt(sum(r * r for r in rets) / len(rets))
        if rv <= 0 or not math.isfinite(rv):
            return None, len(rets), 'rv_zero_or_invalid'
        return rv, len(rets), None

    window = [d for d in cal.sessions if start <= d <= end]
    ledger = []
    counts = {k: 0 for k in ('sessions', 'no_entry_poll', 'chain_unavailable', 'atm_unavailable', 'lot_unverified',
                             'rv_unavailable', 'ivrv_outside_band', 'horizon_beyond_expiry', 'structure_invalid',
                             'entry_quote_incomplete', 'entry_price_invalid', 'eligible')}
    for d in window:
        counts['sessions'] += 1
        row = {'session': d.isoformat(), 'rule': rule.name, 'definitions': defs.name}
        ch, flags = chain_at(d, defs.entry_slot)
        row['entry_flags'] = flags
        if ch is None:
            key = 'no_entry_poll' if 'no_poll_in_window' in flags else 'chain_unavailable'
            row['status'] = key
            counts[key] += 1
            ledger.append(row)
            continue
        row['entry_poll_ts'] = ch.poll_ts.isoformat()
        row['expiry'] = ch.expiry.isoformat()
        a = atm(ch)
        if a is None:
            row['status'] = 'atm_unavailable'; counts['atm_unavailable'] += 1; ledger.append(row); continue
        k0, spot, straddle = a
        row.update({'k0': k0, 'spot': round(spot, 4), 'straddle': round(straddle, 4)})
        lot = lot_size(rule.index, d)
        if lot is None:
            row['status'] = 'lot_unverified'; counts['lot_unverified'] += 1; ledger.append(row); continue
        if defs.td_time_basis == 'nominal_slot':
            t_entry = defs.nominal_entry_time
        else:
            t_entry = ch.ist_time
        mins_left = (datetime.combine(d, time(15, 30)) - datetime.combine(d, t_entry)).total_seconds() / 60.0
        td = cal.days_after_until(d, ch.expiry) + max(0.05, mins_left / 375.0)
        row['td'] = round(td, 6)
        iv_daily = straddle / 0.798 / spot / math.sqrt(td)
        rv, nret, rv_reason = rv_for(d)
        row.update({'iv_daily': round(iv_daily, 8), 'rv_daily': None if rv is None else round(rv, 8),
                    'rv_returns': nret})
        if rv is None:
            row['status'] = 'rv_unavailable'; row['reason'] = rv_reason
            counts['rv_unavailable'] += 1; ledger.append(row); continue
        ivrv = iv_daily / rv
        row['ivrv'] = round(ivrv, 6)
        if not (rule.ivrv_lo <= ivrv < rule.ivrv_hi):
            row['status'] = 'ivrv_outside_band'; counts['ivrv_outside_band'] += 1; ledger.append(row); continue
        exit_date = cal.offset(d, rule.holding_sessions)
        row['planned_exit_date'] = exit_date.isoformat() if exit_date else None
        row['holding_sessions'] = rule.holding_sessions
        if exit_date is not None and exit_date > ch.expiry:
            row['status'] = 'horizon_beyond_expiry'; counts['horizon_beyond_expiry'] += 1; ledger.append(row); continue
        row['exit_on_expiry'] = bool(exit_date is not None and exit_date == ch.expiry)
        s = iron_butterfly(k0, rule.wing) if rule.structure == 'IB' else iron_condor(k0, rule.short_off, rule.wing)
        bad = validate_structure(s)
        if bad:
            row['status'] = 'structure_invalid'; row['reason'] = bad
            counts['structure_invalid'] += 1; ledger.append(row); continue
        entry, missing = price_entry(s, ch)
        if entry is None:
            row['status'] = 'entry_quote_incomplete'; row['reason'] = missing
            counts['entry_quote_incomplete'] += 1; ledger.append(row); continue
        bad = price_validity(s, entry['credit_pts'])
        if bad:
            row['status'] = 'entry_price_invalid'; row['reason'] = bad
            counts['entry_price_invalid'] += 1; ledger.append(row); continue
        counts['eligible'] += 1
        row['status'] = 'eligible'
        row['credit_pts'] = round(entry['credit_pts'], 4)
        row['lot'] = lot
        row['max_loss_rs'] = round((s.width - entry['credit_pts']) * lot, 2)
        # ---- outcome (joined after eligibility; never removes the entry) ----
        if exit_date is None or exit_date > cutoff:
            row['outcome_status'] = 'pending_not_matured'
        else:
            xch, xflags = chain_at(exit_date, defs.close_slot)
            row['exit_flags'] = xflags
            if xch is None:
                day_polls = [c for c in by_day.get(exit_date, []) if c.index == rule.index]
                row['outcome_status'] = 'session_outage' if not day_polls else 'missing_close'
                if day_polls:
                    last = max(c.poll_ts for c in day_polls)
                    row['last_poll_on_exit_date'] = last.isoformat()
            elif xch.expiry != ch.expiry:
                row['outcome_status'] = 'exit_chain_expiry_mismatch'
            else:
                ex, xmissing = price_exit(s, xch)
                if ex is None:
                    row['outcome_status'] = 'missing_exit_legs'; row['missing_exit_legs'] = xmissing
                else:
                    gross = (entry['credit_pts'] + ex['exit_cash_pts']) * lot
                    fee = fees(defs.fee_model, len(s.legs), lot, entry, ex)
                    row.update({'outcome_status': 'resolved', 'exit_poll_ts': xch.poll_ts.isoformat(),
                                'gross_rs': round(gross, 2), 'fees_rs': round(fee['total'], 2),
                                'net_rs': round(gross - fee['total'], 2), 'fee_model': fee['model']})
        ledger.append(row)

    seq = sequential_ledger(ledger, cal, by_day, rule, defs, chain_at)
    outcome_counts: Dict[str, int] = {}
    for r in ledger:
        if r.get('status') == 'eligible':
            outcome_counts[r['outcome_status']] = outcome_counts.get(r['outcome_status'], 0) + 1
    resolved = [r for r in ledger if r.get('outcome_status') == 'resolved']
    summary = {
        'eligible': counts['eligible'], 'outcomes': outcome_counts,
        'resolved_total_net_rs': round(sum(r['net_rs'] for r in resolved), 2),
        'resolved_positive': sum(1 for r in resolved if r['net_rs'] > 0),
        'resolved_expiry_exits': sum(1 for r in resolved if r.get('exit_on_expiry')),
        'sequential': seq['summary'],
    }
    assert counts['sessions'] == sum(v for k, v in counts.items() if k != 'sessions'), 'conservation violated'
    out = {'engine_version': ENGINE_VERSION, 'definitions': asdict(defs), 'rule': asdict(rule),
           'window': [start.isoformat(), end.isoformat()], 'cutoff': cutoff.isoformat(),
           'dataset_sha256': dataset.sha256, 'counts': counts, 'summary': summary,
           'ledger': ledger, 'sequential_ledger': seq['rows']}
    out['output_sha256'] = hashlib.sha256(canonical(out).encode('utf-8')).hexdigest()
    return out


def sequential_ledger(ledger, cal, by_day, rule, defs, chain_at):
    """One position at a time. Occupied through the planned exit date. If the
    exit cannot be priced, capital stays blocked until the first later session
    whose scheduled close prices every leg (declared conservative resolution);
    that mark is reported separately, never added to resolved P&L."""
    rows, occupied_through = [], None
    accepted = skipped = unresolved = 0
    resolved_net = 0.0
    eligible = [r for r in ledger if r.get('status') == 'eligible']
    for r in eligible:
        d = date.fromisoformat(r['session'])
        if occupied_through is not None and d <= occupied_through:
            rows.append({'session': r['session'], 'action': 'skipped_occupied',
                         'occupied_through': occupied_through.isoformat()})
            skipped += 1
            continue
        accepted += 1
        st = r['outcome_status']
        if st == 'resolved':
            occupied_through = date.fromisoformat(r['planned_exit_date'])
            resolved_net += r['net_rs']
            rows.append({'session': r['session'], 'action': 'taken', 'outcome_status': st, 'net_rs': r['net_rs'],
                         'occupied_through': occupied_through.isoformat()})
        elif st == 'pending_not_matured':
            occupied_through = date.max
            rows.append({'session': r['session'], 'action': 'taken', 'outcome_status': st,
                         'occupied_through': 'open_at_cutoff'})
        else:
            unresolved += 1
            planned = date.fromisoformat(r['planned_exit_date'])
            k = 1
            resolution = None
            while True:
                nxt = cal.offset(planned, k)
                if nxt is None:
                    break
                ch, _ = chain_at(nxt, defs.close_slot)
                if ch is not None:
                    resolution = nxt
                    break
                k += 1
            occupied_through = resolution or date.max
            rows.append({'session': r['session'], 'action': 'taken', 'outcome_status': st, 'net_rs': None,
                         'occupied_through': resolution.isoformat() if resolution else 'unresolved_open',
                         'note': 'unpriced planned exit: capital blocked until first later priced close'})
    return {'rows': rows, 'summary': {'accepted': accepted, 'skipped_occupied': skipped,
                                      'accepted_unresolved': unresolved, 'resolved_net_rs': round(resolved_net, 2)}}


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), default=str)
