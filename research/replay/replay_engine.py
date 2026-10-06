"""Offline holding-horizon replay engine v2 (research only; never bundled in the APK).

Two separate paths, never mixed:

* ``run_legacy_sql_v0`` (Stage A) re-implements the 5 Oct exploratory sweep
  (``executed_3way_NF_neutral_C2_includes_expiry_exits.sql``) step by step,
  including its quirks: first poll of ANY index in a slot window, every valid
  expiry at that poll, the expiry x ATM cross join, observed (brain-snapshot)
  calendar, nominal-slot td, RV over every cross-expiry close pair, the fixed
  65-unit NF lot (a historical ASSUMPTION, not an authority) and the sweep fee
  formula. Records whose result depends on non-deterministic SQL behaviour
  (ATM ties) or on the cross join are flagged ``parity_unproven``.
* ``run_corrected`` (Stage B) applies the corrected research contract: first NF
  poll, one explicit nearest expiry, duplicate-conflict quarantine, exchange
  calendar, actual poll time, exactly five consecutive close returns,
  contract-specific lot authority (index + session + expiry, fail closed) and
  replay_fee_v1. Unpriced exits keep capital blocked until the SAME structure
  on the SAME expiry is fully repriced on a later scheduled close; an expired
  contract without such a close stays unresolved (no settlement assumption).

Input: the raw-row extract produced by extract_nf_quotes.sql (format nf_quotes_v2).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30))
ENGINE_VERSION = 'replay_engine_v2_20261006'
EXTRACT_FORMAT = 'nf_quotes_v2'

# NSE trading holidays 2026, copied from brain.py _CONST['NSE_HOLIDAYS'] at 857dedc.
# A test asserts equality with brain.py so the two cannot drift silently.
NSE_HOLIDAYS_2026 = (
    '2026-01-26', '2026-03-03', '2026-03-26', '2026-03-31', '2026-04-03', '2026-04-14',
    '2026-05-01', '2026-05-28', '2026-06-26', '2026-09-14', '2026-10-02', '2026-10-20',
    '2026-11-10', '2026-11-24', '2026-12-25',
)
_HOLIDAYS = frozenset(date.fromisoformat(h) for h in NSE_HOLIDAYS_2026)
STRIKE_STEP = {'NF': 50, 'BNF': 100}

# Stage A only: the sweep hard-coded `case ix when 'NF' then 65 else 30 end`. Reproduced as a labelled
# historical assumption. It is never used by Stage B.
LEGACY_FIXED_LOT = {'NF': 65}
LEGACY_LOT_LABEL = 'legacy_fixed_65_historical_assumption'


# ---------------------------------------------------------------------------
# Contract lot authority (Stage B)
# ---------------------------------------------------------------------------
def project_lot_resolver(index: str, session: date, expiry: date) -> Tuple[Optional[int], dict]:
    """Contract-specific lot from the project SSOT (contract_lot_table.resolve_contract_lot).

    Identity = index + observation session + expiry; cycle is not captured in the extract, so the
    resolver must find a single unambiguous lot across cycles or it fails closed. The chain table
    stores no captured lot, so none is passed."""
    here = os.path.dirname(os.path.abspath(__file__))
    py = os.path.normpath(os.path.join(here, '..', '..', 'app', 'src', 'main', 'python'))
    if py not in sys.path:
        sys.path.insert(0, py)
    from contract_lot_table import resolve_contract_lot, lot_table_version_id  # noqa: E402
    res = resolve_contract_lot(index, as_of=session.isoformat(), expiry=expiry.isoformat(),
                               expiry_cycle=None, number_of_lots=1, captured_contract_lot=None)
    prov = {k: res.get(k) for k in ('lot_source', 'lot_provenance', 'lot_provenance_quality',
                                    'unavailable_reason', 'lot_conflict', 'authoritative')}
    prov['lot_table_version'] = lot_table_version_id()
    prov['rule_id'] = res.get('rule_id') or res.get('lot_rule_id')
    if not res.get('resolved') or not res.get('contract_lot_size'):
        return None, prov
    return int(res['contract_lot_size']), prov


# ---------------------------------------------------------------------------
# Extract model and parser
# ---------------------------------------------------------------------------
Quote = Tuple[Optional[float], Optional[float]]


@dataclass
class AtmLine:
    k0: int
    spot: float
    straddle: float
    tied: Tuple[int, ...]
    npairs: int


@dataclass
class Poll:
    poll_ts: datetime
    rows: Dict[Tuple[str, int, str], List[Quote]] = field(default_factory=dict)   # (expiry|'X', strike, CE/PE)
    atm: Dict[str, AtmLine] = field(default_factory=dict)
    integrity: Optional[Tuple[int, int, int, int]] = None
    expiry_inventory: Optional[Tuple[str, ...]] = None

    @property
    def ist_time(self) -> time:
        return self.poll_ts.astimezone(IST).time()

    def valid_expiries(self) -> List[str]:
        return sorted({k[0] for k in self.rows if k[0] != 'X'})


@dataclass
class Dataset:
    observed_sessions: List[date]
    windows: Dict[Tuple[date, str, str], Tuple[Optional[datetime], Optional[int]]]
    polls: Dict[datetime, Poll]
    sha256: str
    line_counts: Dict[str, int]


def _num(text: str) -> Optional[float]:
    text = text.strip()
    if text == '':
        return None
    value = float(text)
    return value if math.isfinite(value) else None


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace('Z', '+00:00')).astimezone(timezone.utc)


class ExtractFormatError(ValueError):
    """Explicit, line-numbered extract failure (never a silent skip, never a guess)."""


def _real_date(text: str) -> bool:
    if len(text) != 10:
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _check_expiry(text: str, line_no: int, allow_x: bool = True) -> str:
    if (allow_x and text == 'X') or _real_date(text):
        return text
    raise ExtractFormatError(f'line {line_no}: malformed expiry {text!r} (the extract maps non-dates to X)')


def parse_extract(text: str) -> Dataset:
    """Strict parser for nf_quotes_v2. Raises on malformed or repeated keys; never guesses."""
    observed, windows, polls, seen_r = set(), {}, {}, set()
    counts = {'S': 0, 'W': 0, 'A': 0, 'R': 0, 'D': 0}

    def poll(ts_text):
        ts = _ts(ts_text)
        return polls.setdefault(ts, Poll(ts))

    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if '|' not in line:
            raise ExtractFormatError(f'line {line_no}: no field separator')
        kind, rest = line.split('|', 1)
        if kind not in counts:
            raise ValueError(f'unknown extract line kind: {kind!r}')
        counts[kind] += 1
        if kind == 'S':
            d = date.fromisoformat(rest)
            if d in observed:
                raise ValueError(f'duplicate S line {d}')
            observed.add(d)
        elif kind == 'W':
            session, slot, rule, ts_text, nrows = rest.split('|')
            key = (date.fromisoformat(session), slot, rule)
            if key in windows:
                raise ValueError(f'duplicate W line {key}')
            if slot not in ('E', 'C', 'L') or rule not in ('any', 'nf'):
                raise ValueError(f'bad W line {line!r}')
            windows[key] = (_ts(ts_text) if ts_text else None, int(nrows) if nrows else None)
        elif kind == 'A':
            ts_text, expiry, k0, spot, straddle, tied, npairs = rest.split('|')
            _check_expiry(expiry, line_no, allow_x=False)
            p = poll(ts_text)
            if expiry in p.atm:
                raise ValueError(f'duplicate A line {ts_text} {expiry}')
            p.atm[expiry] = AtmLine(int(k0), float(spot), float(straddle),
                                    tuple(int(x) for x in tied.split(',')), int(npairs))
        elif kind == 'R':
            ts_text, expiry, body = rest.split('|', 2)
            _check_expiry(expiry, line_no)
            p = poll(ts_text)
            if (ts_text, expiry) in seen_r:
                raise ValueError(f'repeated R line {ts_text} {expiry} (overlapping parts would fabricate duplicates)')
            seen_r.add((ts_text, expiry))
            for item in body.split(';'):
                strike_text, *toks = item.split('/')
                strike = int(strike_text)
                for tok in toks:
                    opt, vals = tok.split(':')
                    bid, ask = vals.split(',')
                    name = {'C': 'CE', 'P': 'PE'}.get(opt)
                    if name is None:
                        raise ValueError(f'unknown option type token {tok!r}')
                    key = (expiry, strike, name)
                    p.rows.setdefault(key, []).append((_num(bid), _num(ask)))
        elif kind == 'D':
            fields = rest.split('|')
            if len(fields) != 6:
                raise ExtractFormatError(f'line {line_no}: malformed D line (expected 6 fields): {line!r}')
            ts_text, *nums, inventory = fields
            try:
                vals = tuple(int(v) for v in nums)
            except ValueError:
                raise ExtractFormatError(f'line {line_no}: malformed D counts: {line!r}') from None
            if any(v < 0 for v in vals):
                raise ExtractFormatError(f'line {line_no}: negative D count: {line!r}')
            inv = tuple(x for x in inventory.split(',') if x) if inventory else ()
            for e in inv:
                _check_expiry(e, line_no, allow_x=False)
            if len(inv) != vals[2] or len(set(inv)) != len(inv):
                raise ExtractFormatError(f'line {line_no}: D inventory {inv} disagrees with count {vals[2]}')
            p = poll(ts_text)
            if p.integrity is not None:
                raise ValueError(f'duplicate D line {ts_text}')
            p.integrity = vals
            p.expiry_inventory = inv
    return Dataset(sorted(observed), windows, polls, hashlib.sha256(text.encode('utf-8')).hexdigest(), counts)


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------
def valid_quote(q: Optional[Quote]) -> bool:
    if not q:
        return False
    bid, ask = q
    return bid is not None and ask is not None and bid >= 0 and ask > 0 and ask >= bid


def resolved_quote(p: Poll, expiry: str, strike: int, opt: str) -> Tuple[Optional[Quote], Optional[str]]:
    """Corrected rule: one stored row, or identical duplicates. Conflicting rows are never combined."""
    rows = p.rows.get((expiry, strike, opt))
    if not rows:
        return None, 'missing'
    if any(r != rows[0] for r in rows[1:]):
        return None, 'conflict'
    return rows[0], None


def chain_conflicts(p: Poll, expiry: str) -> int:
    return sum(1 for (e, _, _), rows in p.rows.items() if e == expiry and any(r != rows[0] for r in rows[1:]))


def poll_integrity_problem(p: Poll) -> Optional[str]:
    """Stage B poll-level quarantine, from the extract's D line (whole NF chain, not just emitted strikes).

    ml_ocs_unique is (poll_ts, index_key, strike, option_type) with NO expiry, so a poll whose NF rows carry
    more than one valid expiry holds an interleaved chain: each strike keeps whichever expiry was written last.
    The ATM of either expiry would then be chosen from an incomplete strike set, so the poll is unusable.
    Non-date-expiry rows can likewise shadow strikes. Missing integrity counts fail closed."""
    if p.integrity is None:
        return 'poll_integrity_missing'
    rows, distinct_keys, valid_expiries, non_date = p.integrity
    if rows != distinct_keys:
        return 'poll_duplicate_keys'
    if valid_expiries > 1:
        return 'poll_mixed_expiries'
    if non_date > 0:
        return 'poll_non_date_expiry_rows'
    emitted = {k[0] for k in p.rows if k[0] != 'X'} | set(p.atm)
    if p.expiry_inventory is not None and not emitted <= set(p.expiry_inventory):
        return 'poll_inventory_mismatch'
    return None


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------
def exchange_sessions(start: date, end: date) -> List[date]:
    out, cur = [], start
    while cur <= end:
        if cur.weekday() < 5 and cur not in _HOLIDAYS:
            out.append(cur)
        cur += timedelta(days=1)
    return out


class Calendar:
    def __init__(self, sessions: List[date]):
        self.sessions = sorted(set(sessions))
        self.pos = {d: i for i, d in enumerate(self.sessions)}

    def offset(self, d: date, n: int) -> Optional[date]:
        i = self.pos.get(d)
        if i is None or not (0 <= i + n < len(self.sessions)):
            return None
        return self.sessions[i + n]


def weekdays_after_until(d: date, expiry: date) -> int:
    return sum(1 for i in range(1, (expiry - d).days + 1) if (d + timedelta(days=i)).weekday() < 5)


def exchange_sessions_after_until(d: date, expiry: date) -> int:
    return sum(1 for i in range(1, (expiry - d).days + 1)
               if (d + timedelta(days=i)).weekday() < 5 and (d + timedelta(days=i)) not in _HOLIDAYS)


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
    kind: str
    legs: Tuple[Leg, ...]
    width: int
    credit_expected: bool


def iron_butterfly(k0: int, wing: int) -> Structure:
    return Structure('IB', (Leg(k0, 'CE', -1), Leg(k0, 'PE', -1), Leg(k0 + wing, 'CE', 1), Leg(k0 - wing, 'PE', 1)),
                     wing, True)


def iron_condor(k0: int, short_off: int, wing: int) -> Structure:
    return Structure('IC', (Leg(k0 + short_off, 'CE', -1), Leg(k0 + short_off + wing, 'CE', 1),
                            Leg(k0 - short_off, 'PE', -1), Leg(k0 - short_off - wing, 'PE', 1)), wing, True)


def vertical(kind: str, a: int, b: int) -> Structure:
    """Helper only (validity tests). Directional families are NOT wired into run_corrected."""
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
    reasons = []
    if s.width <= 0:
        reasons.append('nonpositive_width')
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
    elif len(s.legs) != 2 or len({l.opt for l in s.legs}) != 1:
        reasons.append('vertical_shape_invalid')
    if any(l.strike <= 0 for l in s.legs):
        reasons.append('nonpositive_strike')
    return reasons


def price_validity(s: Structure, credit_pts: float) -> List[str]:
    if s.credit_expected:
        if not credit_pts > 0:
            return ['credit_structure_nonpositive_credit']
        if not credit_pts < s.width:
            return ['credit_not_below_width']
        return []
    debit = -credit_pts
    if not debit > 0:
        return ['debit_structure_nonpositive_debit']
    if not debit < s.width:
        return ['debit_not_below_width']
    return []


# ---------------------------------------------------------------------------
# Pricing (corrected) and fees
# ---------------------------------------------------------------------------
def price_entry(s: Structure, p: Poll, expiry: str) -> Tuple[Optional[dict], List[str]]:
    sells, buys, problems = 0.0, 0.0, []
    for l in s.legs:
        q, why = resolved_quote(p, expiry, l.strike, l.opt)
        if why:
            problems.append(f'{l.opt}{l.strike}_{why}')
            continue
        if l.side == -1:
            if not (valid_quote(q) and q[0] > 0):
                problems.append(f'{l.opt}{l.strike}_bid')
                continue
            sells += q[0]
        else:
            if not valid_quote(q):
                problems.append(f'{l.opt}{l.strike}_ask')
                continue
            buys += q[1]
    if problems:
        return None, problems
    return {'credit_pts': sells - buys, 'sell_px': sells, 'buy_px': buys}, []


def price_exit(s: Structure, p: Poll, expiry: str) -> Tuple[Optional[dict], List[str]]:
    buyback, sellout, problems = 0.0, 0.0, []
    for l in s.legs:
        q, why = resolved_quote(p, expiry, l.strike, l.opt)
        if why or not valid_quote(q):
            problems.append(f'{l.opt}{l.strike}_{why or "invalid"}')
            continue
        if l.side == -1:
            buyback += q[1]
        else:
            sellout += q[0]
    if problems:
        return None, problems
    return {'exit_cash_pts': sellout - buyback, 'exit_sell_px': sellout, 'exit_buy_px': buyback}, []


def fees(model: str, legs: int, lot: int, entry: dict, exit_: dict) -> dict:
    sell_entry = entry['sell_px'] * lot
    buy_entry = entry['buy_px'] * lot
    sell_exit = exit_['exit_sell_px'] * lot
    buy_exit = exit_['exit_buy_px'] * lot
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
# Rule
# ---------------------------------------------------------------------------
STRUCTURE_BUILDERS: Dict[str, Callable[['Rule', int], Structure]] = {
    'IB': lambda rule, k0: iron_butterfly(k0, rule.wing),
    'IC': lambda rule, k0: iron_condor(k0, rule.short_off, rule.wing),
}


@dataclass(frozen=True)
class Rule:
    name: str
    index: str = 'NF'
    structure: str = 'IB'
    wing: int = 400
    short_off: int = 0
    ivrv_lo: float = 1.3
    ivrv_hi: float = 1.7
    holding_sessions: int = 2
    entry_slot: str = 'E'
    close_slot: str = 'C'

    def __post_init__(self):
        if self.structure not in STRUCTURE_BUILDERS:
            raise ValueError(f'unsupported structure {self.structure!r}; directional families are not wired')
        if self.index not in STRIKE_STEP:
            raise ValueError(f'unsupported index {self.index!r}')
        if self.structure == 'IC' and self.short_off <= 0:
            raise ValueError('IC needs short_off > 0')
        if self.holding_sessions < 0:
            raise ValueError('holding_sessions must be >= 0')

    def build(self, k0: int) -> Structure:
        return STRUCTURE_BUILDERS[self.structure](self, k0)


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), default=str)


def _iso(ts: Optional[datetime]) -> Optional[str]:
    return ts.isoformat() if ts else None


# ---------------------------------------------------------------------------
# Stage B: corrected engine
# ---------------------------------------------------------------------------
CORRECTED_DEFINITIONS = {
    'name': 'corrected_v2_20261006',
    'poll': 'first poll with NF rows in [12:30,12:55) / [15:20,15:45) IST',
    'expiry': 'entry: nearest valid expiry >= session; exit/mark: the entry expiry explicitly; a poll whose NF '
              'rows carry >1 valid expiry, non-date expiries, duplicate keys or no integrity line is quarantined '
              '(unique key has no expiry, so such chains are interleaved)',
    'duplicates': 'a poll whose NF rows exceed its distinct (strike, type) keys is quarantined (impossible under '
                  'ml_ocs_unique, so it signals a broken extract); conflicting rows are never combined',
    'atm': 'legacy ATM metric, ties to the lowest strike',
    'calendar': 'NSE exchange sessions (weekdays minus NSE_HOLIDAYS_2026)',
    'td': 'exchange sessions after entry to expiry + (15:30 - actual entry poll time)/375 min, floor 0.05',
    'rv': 'RMS of exactly 5 consecutive close-to-close log returns ending at the previous session',
    'lot': 'contract-specific project authority (index+session+expiry), fail closed',
    'fees': 'replay_fee_v1 (teacher_v1 rates + STT on every option sale)',
    'unpriced_exit': 'capital blocked until the same legs on the same expiry are fully priced on a later '
                     'scheduled close; expired without one = unresolved',
}


def run_corrected(ds: Dataset, rule: Rule, start: date, end: date, cutoff: date,
                  lot_resolver=project_lot_resolver) -> dict:
    step = STRIKE_STEP[rule.index]
    if rule.wing % step or rule.short_off % step:
        raise ValueError('rule strikes not on the strike grid')
    cal = Calendar(exchange_sessions(start - timedelta(days=30), cutoff + timedelta(days=30)))

    def nf_poll(d: date, slot: str) -> Optional[Poll]:
        ts = ds.windows.get((d, slot, 'nf'), (None, None))[0]
        return ds.polls.get(ts) if ts else None

    def entry_chain(d: date) -> Tuple[Optional[Poll], Optional[str], List[str]]:
        p = nf_poll(d, rule.entry_slot)
        if p is None:
            return None, None, ['no_nf_poll_in_window']
        exps = [e for e in p.valid_expiries() if date.fromisoformat(e) >= d]
        if not exps:
            return p, None, ['no_valid_expiry']
        return p, exps[0], []

    def atm_of(p: Poll, expiry: str) -> Optional[Tuple[int, float, float]]:
        a = p.atm.get(expiry)
        if a is None:
            return None
        k0 = min(a.tied)
        c, _ = resolved_quote(p, expiry, k0, 'CE')
        q, _ = resolved_quote(p, expiry, k0, 'PE')
        if not (valid_quote(c) and valid_quote(q)):
            return None
        cm, pm = (c[0] + c[1]) / 2, (q[0] + q[1]) / 2
        return k0, k0 + cm - pm, cm + pm

    close_cache: Dict[date, Optional[float]] = {}

    def close_spot(d: date) -> Optional[float]:
        if d not in close_cache:
            p = nf_poll(d, rule.close_slot)
            spot = None
            if p is not None and poll_integrity_problem(p) is None:
                exps = [e for e in p.valid_expiries() if date.fromisoformat(e) >= d]
                if exps and not chain_conflicts(p, exps[0]):
                    a = atm_of(p, exps[0])
                    spot = a[1] if a else None
            close_cache[d] = spot
        return close_cache[d]

    def rv_for(d: date):
        prior = [cal.offset(d, -i) for i in range(6, 0, -1)]
        if any(x is None for x in prior):
            return None, 0, 'calendar_history_short'
        spots = [close_spot(x) for x in prior]
        rets = [math.log(b / a) for a, b in zip(spots[:-1], spots[1:])
                if a is not None and b is not None and a > 0 and b > 0]
        if len(rets) != 5:
            return None, len(rets), 'rv_insufficient_consecutive_closes'
        rv = math.sqrt(sum(r * r for r in rets) / 5)
        if rv <= 0 or not math.isfinite(rv):
            return None, 5, 'rv_zero_or_invalid'
        return rv, 5, None

    def close_poll_for_expiry(d: date, expiry: str) -> Tuple[Optional[Poll], str]:
        p = nf_poll(d, rule.close_slot)
        if p is None:
            last = ds.windows.get((d, 'L', 'nf'))
            if last is None or last[0] is None:
                return None, 'session_outage'
            return None, 'missing_close'
        bad = poll_integrity_problem(p)
        if bad:
            return None, f'exit_{bad}'
        if not any(k[0] == expiry for k in p.rows):
            return None, 'exit_expiry_absent'
        if chain_conflicts(p, expiry):
            return None, 'exit_chain_quarantined_conflict'
        return p, 'ok'

    keys = ('sessions', 'no_entry_poll', 'entry_poll_quarantined', 'no_valid_expiry', 'chain_quarantined_conflict',
            'atm_unavailable',
            'lot_unresolved', 'rv_unavailable', 'ivrv_outside_band', 'horizon_beyond_expiry',
            'horizon_beyond_calendar', 'structure_invalid', 'entry_quote_incomplete', 'entry_price_invalid',
            'eligible')
    counts = {k: 0 for k in keys}
    ledger, positions = [], {}
    for d in [x for x in cal.sessions if start <= x <= end]:
        counts['sessions'] += 1
        row = {'session': d.isoformat(), 'rule': rule.name}

        def stop(status, **extra):
            row['status'] = status
            row.update(extra)
            counts[status] += 1
            ledger.append(row)

        p, expiry, flags = entry_chain(d)
        row['entry_flags'] = flags
        if p is None:
            stop('no_entry_poll')
            continue
        row['entry_poll_ts'] = _iso(p.poll_ts)
        bad = poll_integrity_problem(p)
        if bad:
            stop('entry_poll_quarantined', reason=bad, integrity=list(p.integrity) if p.integrity else None)
            continue
        if expiry is None:
            stop('no_valid_expiry')
            continue
        row['expiry'] = expiry
        exp_d = date.fromisoformat(expiry)
        if chain_conflicts(p, expiry):
            stop('chain_quarantined_conflict', conflicts=chain_conflicts(p, expiry))
            continue
        a = atm_of(p, expiry)
        if a is None:
            stop('atm_unavailable')
            continue
        k0, spot, straddle = a
        tied = p.atm[expiry].tied
        row.update({'k0': k0, 'spot': round(spot, 4), 'straddle': round(straddle, 4), 'atm_tied': list(tied)})
        lot, lot_prov = lot_resolver(rule.index, d, exp_d)
        row['lot_provenance'] = lot_prov
        if lot is None:
            stop('lot_unresolved')
            continue
        mins_left = (datetime.combine(d, time(15, 30)) - datetime.combine(d, p.ist_time)).total_seconds() / 60.0
        td = exchange_sessions_after_until(d, exp_d) + max(0.05, mins_left / 375.0)
        iv_daily = straddle / 0.798 / spot / math.sqrt(td)
        rv, nret, rv_reason = rv_for(d)
        row.update({'td': round(td, 6), 'iv_daily': round(iv_daily, 8), 'rv_returns': nret,
                    'rv_daily': None if rv is None else round(rv, 8)})
        if rv is None:
            stop('rv_unavailable', reason=rv_reason)
            continue
        ivrv = iv_daily / rv
        row['ivrv'] = round(ivrv, 6)
        if not (rule.ivrv_lo <= ivrv < rule.ivrv_hi):
            stop('ivrv_outside_band')
            continue
        exit_date = cal.offset(d, rule.holding_sessions)
        row['holding_sessions'] = rule.holding_sessions
        if exit_date is None:
            stop('horizon_beyond_calendar')
            continue
        row['planned_exit_date'] = exit_date.isoformat()
        if exit_date > exp_d:
            stop('horizon_beyond_expiry')
            continue
        row['exit_on_expiry'] = exit_date == exp_d
        s = rule.build(k0)
        bad = validate_structure(s)
        if bad:
            stop('structure_invalid', reason=bad)
            continue
        entry, missing = price_entry(s, p, expiry)
        if entry is None:
            stop('entry_quote_incomplete', reason=missing)
            continue
        bad = price_validity(s, entry['credit_pts'])
        if bad:
            stop('entry_price_invalid', reason=bad)
            continue
        counts['eligible'] += 1
        row.update({'status': 'eligible', 'credit_pts': round(entry['credit_pts'], 4), 'lot': lot,
                    'legs': [[l.strike, l.opt, l.side] for l in s.legs],
                    'max_loss_rs': round((s.width - entry['credit_pts']) * lot, 2)})
        positions[d] = (s, expiry, lot, entry)
        if exit_date > cutoff:
            row['outcome_status'] = 'pending_not_matured'
        else:
            xp, why = close_poll_for_expiry(exit_date, expiry)
            if xp is None:
                row['outcome_status'] = why
                last = ds.windows.get((exit_date, 'L', 'nf'))
                if last and last[0]:
                    row['last_poll_on_exit_date'] = _iso(last[0])
            else:
                ex, xmissing = price_exit(s, xp, expiry)
                if ex is None:
                    row['outcome_status'] = 'missing_exit_legs'
                    row['missing_exit_legs'] = xmissing
                else:
                    gross = (entry['credit_pts'] + ex['exit_cash_pts']) * lot
                    fee = fees('replay_fee_v1', len(s.legs), lot, entry, ex)
                    row.update({'outcome_status': 'resolved', 'exit_poll_ts': _iso(xp.poll_ts),
                                'gross_rs': round(gross, 2), 'fees_rs': round(fee['total'], 2),
                                'net_rs': round(gross - fee['total'], 2), 'fee_model': fee['model']})
        ledger.append(row)

    assert counts['sessions'] == sum(v for k, v in counts.items() if k != 'sessions'), 'conservation violated'

    def mark_later(d_planned: date, s: Structure, expiry: str, lot: int, entry: dict):
        """First later scheduled close (same expiry, all legs) on or before expiry and cutoff."""
        exp_d = date.fromisoformat(expiry)
        attempts = []
        k = 1
        while True:
            nxt = cal.offset(d_planned, k)
            if nxt is None or nxt > cutoff:
                return None, 'unresolved_open_at_cutoff', attempts
            if nxt > exp_d:
                return None, 'unresolved_contract_expired', attempts
            xp, why = close_poll_for_expiry(nxt, expiry)
            attempt = {'session': nxt.isoformat(), 'result': why}
            if xp is not None:
                ex, missing = price_exit(s, xp, expiry)
                if ex is not None:
                    gross = (entry['credit_pts'] + ex['exit_cash_pts']) * lot
                    fee = fees('replay_fee_v1', len(s.legs), lot, entry, ex)
                    attempts.append({'session': nxt.isoformat(), 'result': 'priced'})
                    return {'session': nxt.isoformat(), 'poll_ts': _iso(xp.poll_ts),
                            'net_rs': round(gross - fee['total'], 2)}, 'deferred_mark', attempts
                attempt['result'] = 'missing_legs'
                attempt['missing'] = missing
            attempts.append(attempt)
            k += 1

    seq_rows, occupied_through = [], None
    seq = {'accepted': 0, 'skipped_occupied': 0, 'accepted_resolved': 0, 'accepted_pending': 0,
           'accepted_deferred_mark': 0, 'accepted_unresolved': 0, 'resolved_net_rs': 0.0,
           'deferred_mark_net_rs': 0.0, 'unresolved_max_loss_rs': 0.0}
    for r in [x for x in ledger if x['status'] == 'eligible']:
        d = date.fromisoformat(r['session'])
        if occupied_through is not None and d <= occupied_through:
            seq_rows.append({'session': r['session'], 'action': 'skipped_occupied',
                             'occupied_through': 'open' if occupied_through == date.max else occupied_through.isoformat()})
            seq['skipped_occupied'] += 1
            continue
        seq['accepted'] += 1
        st = r['outcome_status']
        out = {'session': r['session'], 'action': 'taken', 'outcome_status': st}
        if st == 'resolved':
            occupied_through = date.fromisoformat(r['planned_exit_date'])
            seq['accepted_resolved'] += 1
            seq['resolved_net_rs'] += r['net_rs']
            out.update({'net_rs': r['net_rs'], 'occupied_through': occupied_through.isoformat()})
        elif st == 'pending_not_matured':
            occupied_through = date.max
            seq['accepted_pending'] += 1
            out['occupied_through'] = 'open_at_cutoff'
        else:
            s, expiry, lot, entry = positions[d]
            mark, resolution, attempts = mark_later(date.fromisoformat(r['planned_exit_date']), s, expiry, lot, entry)
            out['mark_attempts'] = attempts
            out['resolution'] = resolution
            if mark is not None:
                occupied_through = date.fromisoformat(mark['session'])
                seq['accepted_deferred_mark'] += 1
                seq['deferred_mark_net_rs'] += mark['net_rs']
                out.update({'deferred_mark': mark, 'occupied_through': occupied_through.isoformat()})
            else:
                occupied_through = date.max
                seq['accepted_unresolved'] += 1
                seq['unresolved_max_loss_rs'] += r['max_loss_rs']
                out['occupied_through'] = 'unresolved_blocked'
        seq_rows.append(out)
    for k in ('resolved_net_rs', 'deferred_mark_net_rs', 'unresolved_max_loss_rs'):
        seq[k] = round(seq[k], 2)

    outcome_counts: Dict[str, int] = {}
    for r in ledger:
        if r['status'] == 'eligible':
            outcome_counts[r['outcome_status']] = outcome_counts.get(r['outcome_status'], 0) + 1
    resolved = [r for r in ledger if r.get('outcome_status') == 'resolved']
    summary = {'eligible': counts['eligible'], 'outcomes': outcome_counts,
               'resolved_total_net_rs': round(sum(r['net_rs'] for r in resolved), 2),
               'resolved_positive': sum(1 for r in resolved if r['net_rs'] > 0),
               'resolved_expiry_exits': sum(1 for r in resolved if r.get('exit_on_expiry')),
               'sequential': seq}
    out = {'engine_version': ENGINE_VERSION, 'stage': 'B_corrected', 'definitions': CORRECTED_DEFINITIONS,
           'rule': asdict(rule), 'window': [start.isoformat(), end.isoformat()], 'cutoff': cutoff.isoformat(),
           'dataset_sha256': ds.sha256, 'counts': counts, 'summary': summary, 'ledger': ledger,
           'sequential_ledger': seq_rows}
    out['output_sha256'] = hashlib.sha256(canonical(out).encode('utf-8')).hexdigest()
    return out


# ---------------------------------------------------------------------------
# Stage A: legacy sweep emulation
# ---------------------------------------------------------------------------
LEGACY_DEFINITIONS = {
    'name': 'legacy_sql_v0_sweep_20261005',
    'source': 'executed_3way_NF_neutral_C2_includes_expiry_exits.sql',
    'poll': 'first poll of ANY index in the slot window (cp CTE)',
    'quotes': 'valid rows only (ask>0, bid>=0, ask>=bid, date expiry); duplicates kept as separate rows',
    'expiry': 'every valid expiry at the poll; entry rows cross-joined with every expiry ATM (ent2 join)',
    'atm': 'DISTINCT ON min |mid(CE)-mid(PE)|, tie order undefined in SQL (flagged; lowest used)',
    'calendar': 'dates with ml_brain_snapshots rows from 2026-06-15 (observed)',
    'td': 'weekdays after entry to expiry + (15:30 - nominal 12:30)/375 = +0.48',
    'rv': 'RMS over every (close sn, any expiry) -> (close sn+1, any expiry) pair, sn in [s-6, s-2]',
    'band': 'vb = v1.3: 1.3 <= ivd/rv < 1.7; rv null/0 -> vna',
    'exit': 'C2 = observed session s+2 on or before expiry, first ANY-index poll in [15:20,15:45); '
            'all legs valid at that poll or no row (inner join)',
    'lot': LEGACY_LOT_LABEL,
    'fees': 'legacy_sweep_v0',
}
_LEGACY_IB_LEGS = (('CE', 0, -1), ('PE', 0, -1), ('CE', 1, 1), ('PE', -1, 1))   # ko in wing units


def _legacy_valid_rows(p: Poll, expiry: str, strike: int, opt: str) -> List[Quote]:
    return [q for q in p.rows.get((expiry, strike, opt), []) if valid_quote(q)]


def run_legacy_sql_v0(ds: Dataset, rule: Rule, start: date, end: date, cutoff: date,
                      calendar_start: date = date(2026, 6, 15)) -> dict:
    if rule.structure != 'IB' or rule.index != 'NF' or rule.holding_sessions != 2:
        raise ValueError('legacy emulation reproduces only the NF IB C2 cell of the 5 Oct sweep')
    step = STRIKE_STEP['NF']
    lot = LEGACY_FIXED_LOT['NF']
    cal = Calendar([d for d in ds.observed_sessions if d >= calendar_start])

    def any_poll(d: date, slot: str) -> Optional[Poll]:
        # A first-any-index poll with no NF rows (e.g. a BNF-only poll) exists but carries no NF data.
        ts = ds.windows.get((d, slot, 'any'), (None, None))[0]
        return (ds.polls.get(ts) or Poll(ts)) if ts else None

    def atm_rows(p: Optional[Poll]) -> Dict[str, AtmLine]:
        return dict(p.atm) if p is not None else {}

    def rv_for(d: date) -> Optional[float]:
        sn = cal.pos.get(d)
        if sn is None:
            return None
        sq = []
        for s1 in range(sn - 6, sn - 1):
            if s1 < 0 or s1 + 1 >= len(cal.sessions):
                continue
            c1 = atm_rows(any_poll(cal.sessions[s1], 'C'))
            c2 = atm_rows(any_poll(cal.sessions[s1 + 1], 'C'))
            for a in c1.values():
                for b in c2.values():
                    sq.append(math.log(b.spot / a.spot) ** 2)
        if not sq:
            return None
        return math.sqrt(sum(sq) / len(sq))

    def vband(ivd: float, rv: Optional[float]) -> str:
        if rv is None or rv == 0:
            return 'vna'
        x = ivd / rv
        return 'v<1.0' if x < 1.0 else 'v1.0' if x < 1.3 else 'v1.3' if x < 1.7 else 'v1.7'

    ledger = []
    counts = {k: 0 for k in ('sessions', 'not_observed', 'no_entry_poll', 'no_nf_atm', 'not_in_cell',
                             'horizon_beyond_calendar', 'horizon_beyond_expiry', 'eligible')}
    days = sorted(set(exchange_sessions(start, end)) | {d for d in cal.sessions if start <= d <= end})
    for d in days:
        counts['sessions'] += 1
        row = {'session': d.isoformat(), 'rule': rule.name, 'flags': []}
        if d not in set(exchange_sessions(d, d)):
            row['flags'].append('observed_non_exchange_day')
        if d not in cal.pos:
            row['status'] = 'not_observed'
            counts['not_observed'] += 1
            ledger.append(row)
            continue
        p = any_poll(d, rule.entry_slot)
        if p is None:
            row['status'] = 'no_entry_poll'
            counts['no_entry_poll'] += 1
            ledger.append(row)
            continue
        row['entry_poll_ts'] = _iso(p.poll_ts)
        nf_ts = ds.windows.get((d, rule.entry_slot, 'nf'), (None, None))[0]
        if nf_ts != p.poll_ts:
            row['flags'].append('any_index_poll_differs_from_first_nf_poll')
        atms = atm_rows(p)
        if not atms:
            row['status'] = 'no_nf_atm'
            counts['no_nf_atm'] += 1
            ledger.append(row)
            continue
        if len(atms) > 1:
            row['flags'].append('legacy_multi_expiry_cross_join')
        if any(len(a.tied) > 1 or a.npairs > 1 for a in atms.values()):
            row['flags'].append('legacy_atm_tie_nondeterministic')
        rv = rv_for(d)
        sn = cal.pos[d]
        xd = cal.sessions[sn + 2] if sn + 2 < len(cal.sessions) else None
        feat = []
        for e_ex, e in sorted(atms.items()):
            e_d = date.fromisoformat(e_ex)
            td = weekdays_after_until(d, e_d) + max(0.05, (15.5 - 12.5) * 60 / 375.0)
            ivd = e.straddle / 0.798 / e.spot / math.sqrt(td)
            for a_ex, a in sorted(atms.items()):
                feat.append({'e_ex': e_ex, 'a_ex': a_ex, 'k0': min(a.tied), 'td': td, 'ivd': ivd,
                             'vb': vband(ivd, rv)})
        credit = sell_px = buy_px = 0.0
        nl = 0
        for f in feat:
            for opt, ko, side in _LEGACY_IB_LEGS:
                for q in _legacy_valid_rows(p, f['e_ex'], f['k0'] + ko * rule.wing, opt):
                    if (side == -1 and q[0] > 0) or (side == 1 and q[1] > 0):
                        nl += 1
                        if side == -1:
                            credit += q[0]
                            sell_px += q[0]
                        else:
                            credit -= q[1]
                            buy_px += q[1]
        row.update({'rv_daily': None if rv is None else round(rv, 8),
                    'feat': [{k: (round(v, 8) if isinstance(v, float) else v) for k, v in f.items()} for f in feat],
                    'nl': nl, 'credit_pts': round(credit, 4)})
        st_ok = nl == 4 and credit != 0 and (rule.wing - credit) > 0
        cell = [f for f in feat if f['vb'] == 'v1.3'] if st_ok else []
        if not cell:
            row['status'] = 'not_in_cell'
            row['reason'] = 'structure_not_priced' if not st_ok else 'iv_rv_not_v1.3'
            counts['not_in_cell'] += 1
            ledger.append(row)
            continue
        # xp rows: one per feat row whose C2 exit day exists and is on/before that row's entry expiry.
        xrows = [f for f in feat if xd is not None and xd <= date.fromisoformat(f['e_ex'])]
        if xd is None:
            # The observed calendar ends before s+2: the sweep had no C2 row (not yet matured at its cutoff).
            row['status'] = 'horizon_beyond_calendar'
            counts['horizon_beyond_calendar'] += 1
            ledger.append(row)
            continue
        if not any(xd <= date.fromisoformat(f['e_ex']) for f in cell):
            row['status'] = 'horizon_beyond_expiry'
            counts['horizon_beyond_expiry'] += 1
            ledger.append(row)
            continue
        counts['eligible'] += 1
        row['status'] = 'eligible'
        row['planned_exit_date'] = xd.isoformat()
        row['exit_on_expiry'] = any(xd == date.fromisoformat(f['e_ex']) for f in cell)
        row['lot'] = lot
        row['lot_label'] = LEGACY_LOT_LABEL
        if len(cell) > 1 or len(feat) > 1:
            row['flags'].append('parity_unproven')
        if 'legacy_atm_tie_nondeterministic' in row['flags'] and 'parity_unproven' not in row['flags']:
            row['flags'].append('parity_unproven')
        if xd > cutoff:
            row['outcome_status'] = 'pending_not_matured'
            ledger.append(row)
            continue
        xp = any_poll(xd, rule.close_slot)
        if xp is None:
            row['outcome_status'] = 'missing_close'
            last = ds.windows.get((xd, 'L', 'nf'))
            if last and last[0]:
                row['last_poll_on_exit_date'] = _iso(last[0])
            ledger.append(row)
            continue
        row['exit_poll_ts'] = _iso(xp.poll_ts)
        groups: Dict[bool, dict] = {}
        for f in xrows:
            g = groups.setdefault(xd == date.fromisoformat(f['e_ex']),
                                  {'exit_cash': 0.0, 'exit_sell_px': 0.0, 'exit_buy_px': 0.0, 'nl': 0})
            for opt, ko, side in _LEGACY_IB_LEGS:
                for q in _legacy_valid_rows(xp, f['e_ex'], f['k0'] + ko * rule.wing, opt):
                    g['nl'] += 1
                    if side == -1:
                        g['exit_cash'] -= q[1]
                        g['exit_buy_px'] += q[1]
                    else:
                        g['exit_cash'] += q[0]
                        g['exit_sell_px'] += q[0]
        nets = []
        for f in cell:
            for is_exp, g in sorted(groups.items()):
                if g['nl'] != nl:
                    continue
                net = ((credit + g['exit_cash']) * lot
                       - (nl * 2 * 20 * 1.18 + 0.0015 * (sell_px + g['exit_sell_px']) * lot
                          + 0.0003553 * 1.18 * (sell_px + buy_px + g['exit_sell_px'] + g['exit_buy_px']) * lot
                          + 0.00003 * (buy_px + g['exit_buy_px']) * lot))
                nets.append(net)
        if not nets:
            row['outcome_status'] = 'missing_exit_legs'
            row['exit_nl'] = {str(k): v['nl'] for k, v in groups.items()}
        else:
            row['outcome_status'] = 'resolved'
            row['out_rows'] = len(nets)
            row['net_rs'] = round(sum(nets), 2)
            if len(nets) > 1 and 'parity_unproven' not in row['flags']:
                row['flags'].append('parity_unproven')
        ledger.append(row)

    assert counts['sessions'] == sum(v for k, v in counts.items() if k != 'sessions'), 'conservation violated'
    elig = [r for r in ledger if r['status'] == 'eligible']
    outcome_counts: Dict[str, int] = {}
    for r in elig:
        outcome_counts[r['outcome_status']] = outcome_counts.get(r['outcome_status'], 0) + 1
    resolved = [r for r in elig if r['outcome_status'] == 'resolved']
    summary = {'eligible': len(elig), 'outcomes': outcome_counts,
               'resolved_total_net_rs': round(sum(r['net_rs'] for r in resolved), 2),
               'resolved_positive': sum(1 for r in resolved if r['net_rs'] > 0),
               'resolved_expiry_exits': sum(1 for r in resolved if r.get('exit_on_expiry')),
               'parity_unproven_sessions': [r['session'] for r in ledger if 'parity_unproven' in r['flags']],
               'flag_counts': {f: sum(1 for r in ledger if f in r['flags'])
                               for f in ('any_index_poll_differs_from_first_nf_poll', 'legacy_multi_expiry_cross_join',
                                         'legacy_atm_tie_nondeterministic', 'parity_unproven')}}
    out = {'engine_version': ENGINE_VERSION, 'stage': 'A_legacy_sql_v0', 'definitions': LEGACY_DEFINITIONS,
           'rule': asdict(rule), 'window': [start.isoformat(), end.isoformat()], 'cutoff': cutoff.isoformat(),
           'dataset_sha256': ds.sha256, 'counts': counts, 'summary': summary, 'ledger': ledger}
    out['output_sha256'] = hashlib.sha256(canonical(out).encode('utf-8')).hexdigest()
    return out
