"""Regression fixtures for replay engine v2 (synthetic nf_quotes_v2 extracts only).

Each fixture is rendered to the real extract text format and parsed, so the parser, the Stage A
legacy emulation and the Stage B corrected engine are exercised end to end.
"""
import hashlib
import math
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import replay_engine as re_  # noqa: E402
import run_stage_a_b as runner  # noqa: E402

REPO = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
UTC = timezone.utc


# ---------------------------------------------------------------------------
# Synthetic extract builder
# ---------------------------------------------------------------------------
def _price(spot, k, opt, scale):
    intrinsic = max(spot - k, 0) if opt == 'CE' else max(k - spot, 0)
    return round(intrinsic + scale * math.exp(-((k - spot) / 300.0) ** 2) + 2.0, 2)


def _ts(d, hh, mm):
    return datetime.combine(d, time(hh, mm), re_.IST).astimezone(UTC)


def _fmt(ts):
    return ts.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _quotes(spot, scale=120.0):
    out = {}
    center = int(round(spot / 50.0)) * 50
    for k in range(center - 1000, center + 1001, 50):
        for opt in ('CE', 'PE'):
            p = _price(spot, k, opt, scale)
            out[(k, opt)] = [(round(p - 0.05, 2), round(p + 0.05, 2))]
    return out


def _atm_line(rows):
    """Exactly the extract's legacy ATM: valid rows, every CE x PE pair at a strike, min |mid diff|."""
    pairs = []
    for (k, opt), qs in rows.items():
        if opt != 'CE':
            continue
        for c in qs:
            for p in rows.get((k, 'PE'), []):
                if re_.valid_quote(c) and re_.valid_quote(p):
                    pairs.append((abs((c[0] + c[1]) / 2 - (p[0] + p[1]) / 2), k, c, p))
    if not pairs:
        return None
    g = min(x[0] for x in pairs)
    at = [x for x in pairs if x[0] == g]
    tied = sorted({x[1] for x in at})
    k0 = tied[0]
    best = sorted([x for x in at if x[1] == k0], key=lambda x: (x[0], x[2][0], x[2][1], x[3][0], x[3][1]))[0]
    _, _, c, p = best
    spot = k0 + (c[0] + c[1]) / 2 - (p[0] + p[1]) / 2
    straddle = (c[0] + c[1]) / 2 + (p[0] + p[1]) / 2
    npairs = len({(x[1], x[2], x[3]) for x in at})
    return k0, spot, straddle, tied, npairs


class Fixture:
    """polls[(d, slot)] = {'ts', 'chains': {expiry: rows}, 'index': 'NF'|'BNF'}"""

    def __init__(self, sessions, spots, expiry_for, observed=None):
        self.observed = list(observed if observed is not None else sessions)
        self.polls = {}
        for d in sessions:
            for slot, hh, mm in (('E', 12, 31), ('C', 15, 21)):
                self.polls[(d, slot)] = {'ts': _ts(d, hh, mm), 'index': 'NF',
                                         'chains': {expiry_for(d).isoformat(): _quotes(spots[d])}}
        self.any_override = {}
        self.omit_integrity = set()
        self.hidden = {}        # (d, slot) -> {expiry: rows}: stored rows the extract does not emit as R/A lines

    def overwrite_with_expiry(self, d, slot, expiry, keys, scale=160.0, spot=None):
        """Model the real upsert: ml_ocs_unique has no expiry, so writing another expiry's row at a
        (strike, type) REPLACES the stored row. The poll then holds an interleaved two-expiry chain."""
        chains = self.polls[(d, slot)]['chains']
        cur = [e for e in chains if e != expiry][0]
        donor = _quotes(spot if spot is not None else 23000.0, scale)
        for key in keys:
            chains[cur].pop(key, None)
            chains.setdefault(expiry, {})[key] = donor.get(key, [(1.0, 1.1)])

    def drop_poll(self, d, slot):
        self.polls.pop((d, slot), None)

    def drop_legs(self, d, slot, legs, expiry=None):
        chains = self.polls[(d, slot)]['chains']
        for e in ([expiry] if expiry else list(chains)):
            for leg in legs:
                chains[e].pop(leg, None)

    def set_rows(self, d, slot, expiry, key, rows):
        self.polls[(d, slot)]['chains'].setdefault(expiry, {})[key] = rows

    def render(self):
        lines = [f'S|{d.isoformat()}' for d in sorted(set(self.observed))]
        days = sorted({d for d, _ in self.polls} | set(self.observed))
        for d in days:
            for slot in ('E', 'C'):
                p = self.polls.get((d, slot))
                any_ts = self.any_override.get((d, slot), p['ts'] if p else None)
                if any_ts is not None:
                    n = sum(len(v) for ch in p['chains'].values() for v in ch.values()) \
                        if p and any_ts == p['ts'] else 0
                    lines.append(f'W|{d.isoformat()}|{slot}|any|{_fmt(any_ts)}|{n}')
                if p:
                    n = sum(len(v) for ch in p['chains'].values() for v in ch.values())
                    lines.append(f'W|{d.isoformat()}|{slot}|nf|{_fmt(p["ts"])}|{n}')
            if (d, 'C') not in self.polls and d.weekday() < 5:
                e = self.polls.get((d, 'E'))
                lines.append(f'W|{d.isoformat()}|L|nf|{_fmt(e["ts"]) if e else ""}|')
        for (d, slot), p in sorted(self.polls.items()):
            ts = _fmt(p['ts'])
            total, keys, non_date = 0, set(), 0
            for expiry, rows in sorted(p['chains'].items()):
                a = _atm_line(rows) if expiry != 'X' else None
                if a:
                    k0, spot, straddle, tied, npairs = a
                    lines.append(f'A|{ts}|{expiry}|{k0}|{spot:.6f}|{straddle:.6f}|'
                                 f'{",".join(map(str, tied))}|{npairs}')
                toks = []
                for k in sorted({k for k, _ in rows}):
                    parts = [str(k)]
                    for opt, tag in (('CE', 'C'), ('PE', 'P')):
                        for b, a_ in rows.get((k, opt), []):
                            parts.append(f'{tag}:{"" if b is None else b},{"" if a_ is None else a_}')
                            total += 1
                            non_date += 1 if expiry == 'X' else 0
                        if rows.get((k, opt)):
                            keys.add((k, opt))
                    toks.append('/'.join(parts))
                lines.append(f'R|{ts}|{expiry}|' + ';'.join(toks))
            for expiry, rows in self.hidden.get((d, slot), {}).items():
                for (k, opt), qs in rows.items():
                    total += len(qs)
                    non_date += len(qs) if expiry == 'X' else 0
                    keys.add((k, opt))
            inv = sorted({e for e, rows in list(p['chains'].items()) + list(self.hidden.get((d, slot), {}).items())
                          if e != 'X' and rows})
            if (d, slot) not in self.omit_integrity:
                lines.append(f'D|{ts}|{total}|{len(keys)}|{len(inv)}|{non_date}|{",".join(inv)}')
        return '\n'.join(sorted(lines))

    def dataset(self):
        return re_.parse_extract(self.render())


def _sessions(start, end):
    return re_.exchange_sessions(start, end)


def _next_tuesday(d):
    return d + timedelta(days=(1 - d.weekday()) % 7)


def _wiggle(sessions, base=23000.0):
    return {d: base + (40 if i % 2 else -40) + i * 5 for i, d in enumerate(sessions)}


OPEN_BAND = re_.Rule(name='logic', ivrv_lo=0.0, ivrv_hi=1e9)


def _lot65(index, session, expiry):
    return 65, {'lot_source': 'test_fixed_65'}


def _run_b(fx, rule=OPEN_BAND, start=date(2026, 9, 1), end=date(2026, 9, 18), cutoff=date(2026, 9, 25),
           resolver=_lot65):
    return re_.run_corrected(fx.dataset(), rule, start, end, cutoff, lot_resolver=resolver)


def _by_session(rows):
    return {r['session']: r for r in rows}


# ---------------------------------------------------------------------------
# Calendar, authority, parser
# ---------------------------------------------------------------------------
class CalendarAndAuthorityTests(unittest.TestCase):
    def test_holidays_match_brain(self):
        sys.path.insert(0, os.path.join(REPO, 'app', 'src', 'main', 'python'))
        import brain  # noqa: E402
        self.assertEqual(sorted(brain._CONST['NSE_HOLIDAYS']), sorted(re_.NSE_HOLIDAYS_2026))

    def test_holiday_crossing_exchange_calendar(self):
        cal = re_.Calendar(re_.exchange_sessions(date(2026, 9, 1), date(2026, 10, 15)))
        self.assertEqual(cal.offset(date(2026, 9, 30), 2), date(2026, 10, 5))   # 2 Oct holiday
        self.assertEqual(cal.offset(date(2026, 9, 11), 1), date(2026, 9, 15))   # 14 Sep holiday

    def test_project_lot_resolver_is_contract_specific_and_fails_closed(self):
        lot, prov = re_.project_lot_resolver('NF', date(2026, 9, 16), date(2026, 9, 22))
        self.assertEqual(lot, 65)
        self.assertEqual(prov['lot_source'], 'authoritative_contract_rule')
        self.assertEqual(prov['lot_provenance'], 'NSE_FAOP_70616')
        self.assertTrue(prov['lot_table_version'])
        # Expiry before the first revised contract (Dec-2025 weekly kept 75): never 65 by date alone.
        lot_old, _ = re_.project_lot_resolver('NF', date(2025, 12, 1), date(2025, 12, 23))
        self.assertNotEqual(lot_old, 65)
        lot_unknown, prov_unknown = re_.project_lot_resolver('FINNIFTY', date(2026, 9, 16), date(2026, 9, 22))
        self.assertIsNone(lot_unknown)
        self.assertTrue(prov_unknown['unavailable_reason'])

    def test_rule_rejects_unknown_structures_and_indices(self):
        for kw in ({'structure': 'BC'}, {'structure': 'ib'}, {'structure': 'IC', 'short_off': 0},
                   {'index': 'FINNIFTY'}, {'holding_sessions': -1}):
            with self.assertRaises(ValueError, msg=kw):
                re_.Rule(name='x', **kw)

    def test_parser_is_strict(self):
        with self.assertRaises(ValueError):
            re_.parse_extract('Q|x')
        with self.assertRaises(ValueError):
            re_.parse_extract('W|2026-09-01|E|nf|2026-09-01T07:01:00.000000Z|1\n'
                              'W|2026-09-01|E|nf|2026-09-01T07:01:00.000000Z|1')
        r = 'R|2026-09-01T07:01:00.000000Z|2026-09-08|23000/C:1,2/P:1,2'
        with self.assertRaises(ValueError):
            re_.parse_extract(r + '\n' + r)        # overlapping parts would fabricate duplicates
        with self.assertRaises(ValueError):
            re_.parse_extract('R|2026-09-01T07:01:00.000000Z|2026-09-08|23000/Z:1,2')


class StructureValidityTests(unittest.TestCase):
    def test_debit_cost_above_width_rejected(self):
        s = re_.vertical('BLC', 23000, 23100)
        self.assertEqual(re_.validate_structure(s), [])
        self.assertEqual(re_.price_validity(s, -150.0), ['debit_not_below_width'])
        self.assertEqual(re_.price_validity(s, -60.0), [])
        self.assertEqual(re_.price_validity(s, 10.0), ['debit_structure_nonpositive_debit'])

    def test_credit_bounds(self):
        s = re_.iron_butterfly(23000, 400)
        self.assertEqual(re_.price_validity(s, 450.0), ['credit_not_below_width'])
        self.assertEqual(re_.price_validity(s, -5.0), ['credit_structure_nonpositive_credit'])
        self.assertEqual(re_.price_validity(s, 180.0), [])

    def test_construction_checks(self):
        bad = re_.Structure('IB', (re_.Leg(23000, 'CE', -1), re_.Leg(23050, 'PE', -1),
                                   re_.Leg(23400, 'CE', 1), re_.Leg(22600, 'PE', 1)), 400, True)
        self.assertIn('ib_shorts_not_same_strike', re_.validate_structure(bad))
        flipped = re_.Structure('IC', (re_.Leg(23200, 'CE', 1), re_.Leg(23600, 'CE', -1),
                                       re_.Leg(22800, 'PE', -1), re_.Leg(22400, 'PE', 1)), 400, True)
        self.assertIn('wing_orientation_invalid', re_.validate_structure(flipped))
        self.assertIn('nonpositive_width', re_.validate_structure(re_.vertical('BC', 23000, 23000)))

    def test_invalid_quotes_never_priced(self):
        self.assertFalse(re_.valid_quote((None, 10.0)))
        self.assertFalse(re_.valid_quote((5.0, 4.0)))
        self.assertFalse(re_.valid_quote((0.0, 0.0)))
        self.assertTrue(re_.valid_quote((0.0, 0.05)))


# ---------------------------------------------------------------------------
# Stage B corrected engine
# ---------------------------------------------------------------------------
class CorrectedLedgerTests(unittest.TestCase):
    def setUp(self):
        self.sessions = _sessions(date(2026, 8, 20), date(2026, 9, 25))
        self.spots = _wiggle(self.sessions)

    def fx(self):
        return Fixture(self.sessions, self.spots, _next_tuesday)

    def test_missing_scheduled_close_keeps_entry(self):
        fx = self.fx()
        fx.drop_poll(date(2026, 9, 7), 'C')
        row = _by_session(_run_b(fx)['ledger'])['2026-09-03']
        self.assertEqual((row['status'], row['outcome_status']), ('eligible', 'missing_close'))
        self.assertNotIn('net_rs', row)

    def test_b1_codex_counterexample_partial_later_close_does_not_free_capital(self):
        # Entry 2 Sep (expiry 8 Sep), planned exit 4 Sep missing. 7 Sep close exists but lacks three legs.
        fx = self.fx()
        fx.drop_poll(date(2026, 9, 4), 'C')
        k0 = _by_session(_run_b(self.fx())['ledger'])['2026-09-02']['k0']
        fx.drop_legs(date(2026, 9, 7), 'C', [(k0, 'CE'), (k0 + 400, 'CE'), (k0 - 400, 'PE')])
        out = _run_b(fx)
        seq = _by_session(out['sequential_ledger'])
        taken = seq['2026-09-02']
        self.assertNotEqual(taken['occupied_through'], '2026-09-07')
        self.assertEqual(taken['mark_attempts'][0]['result'], 'missing_legs')
        # The first COMPLETE later close of the same expiry is 8 Sep (expiry day).
        self.assertEqual(taken['resolution'], 'deferred_mark')
        self.assertEqual(taken['occupied_through'], '2026-09-08')
        # 3 and 4 Sep entries (exits 7 and 8 Sep) are skipped; capital frees only after 8 Sep. (7-15 Sep
        # entries lack five consecutive closes because 4 Sep has none; 16 Sep is the next accepted entry.)
        self.assertEqual(seq['2026-09-03']['action'], 'skipped_occupied')
        self.assertEqual(seq['2026-09-04']['action'], 'skipped_occupied')
        self.assertEqual(seq['2026-09-16']['action'], 'taken')
        # The mark is reported separately and never added to resolved P&L.
        s = out['summary']['sequential']
        self.assertEqual(s['accepted_deferred_mark'], 1)
        self.assertNotIn('2026-09-02', [r['session'] for r in out['ledger'] if r.get('outcome_status') == 'resolved'])

    def test_b1_later_close_with_wrong_expiry_or_no_usable_quotes(self):
        fx = self.fx()
        fx.drop_poll(date(2026, 9, 4), 'C')
        # 7 Sep close carries only the NEXT expiry; 8 Sep close has every quote invalid.
        c7 = fx.polls[(date(2026, 9, 7), 'C')]['chains']
        c7['2026-09-15'] = c7.pop('2026-09-08')
        c8 = fx.polls[(date(2026, 9, 8), 'C')]['chains']['2026-09-08']
        for key in list(c8):
            c8[key] = [(None, None)]
        out = _run_b(fx)
        taken = _by_session(out['sequential_ledger'])['2026-09-02']
        self.assertEqual([a['result'] for a in taken['mark_attempts']], ['exit_expiry_absent', 'missing_legs'])
        self.assertEqual(taken['resolution'], 'unresolved_contract_expired')
        self.assertEqual(taken['occupied_through'], 'unresolved_blocked')
        s = out['summary']['sequential']
        self.assertEqual(s['accepted_unresolved'], 1)
        self.assertGreater(s['unresolved_max_loss_rs'], 0)
        # Capital never frees: every later eligible entry is skipped.
        later = [r for r in out['sequential_ledger'] if r['session'] > '2026-09-02']
        self.assertTrue(later)
        self.assertTrue(all(r['action'] == 'skipped_occupied' for r in later))

    def test_b1_unresolved_at_cutoff(self):
        fx = self.fx()
        fx.drop_poll(date(2026, 9, 4), 'C')
        out = _run_b(fx, cutoff=date(2026, 9, 4))
        taken = _by_session(out['sequential_ledger'])['2026-09-02']
        self.assertEqual(taken['resolution'], 'unresolved_open_at_cutoff')

    def test_expiry_coincident_exit_and_horizon_beyond_expiry(self):
        rows = _by_session(_run_b(self.fx())['ledger'])
        r = rows['2026-09-10']                   # Thu 10 + 2 sessions = Tue 15 (14 Sep holiday) = expiry
        self.assertEqual(r['planned_exit_date'], '2026-09-15')
        self.assertTrue(r['exit_on_expiry'])
        self.assertEqual(r['outcome_status'], 'resolved')
        self.assertEqual(rows['2026-09-11']['status'], 'horizon_beyond_expiry')

    def test_zero_rv_is_not_bucketed(self):
        fx = Fixture(self.sessions, {d: 23000.0 for d in self.sessions}, _next_tuesday)
        out = _run_b(fx)
        reasons = {r.get('reason') for r in out['ledger'] if r['status'] == 'rv_unavailable'}
        self.assertIn('rv_zero_or_invalid', reasons)
        self.assertEqual(out['counts']['eligible'], 0)

    def test_five_consecutive_rule(self):
        fx = self.fx()
        fx.drop_poll(date(2026, 8, 31), 'C')
        row = _by_session(_run_b(fx)['ledger'])['2026-09-02']
        self.assertEqual((row['status'], row['reason']), ('rv_unavailable', 'rv_insufficient_consecutive_closes'))

    def test_session_outage_distinguished_from_missing_close(self):
        fx = self.fx()
        fx.drop_poll(date(2026, 9, 7), 'C')
        fx.drop_poll(date(2026, 9, 7), 'E')
        self.assertEqual(_by_session(_run_b(fx)['ledger'])['2026-09-03']['outcome_status'], 'session_outage')

    def test_b2_lot_comes_from_resolver_never_a_blanket(self):
        def r75(index, session, expiry):
            return 75, {'lot_source': 'test_75'}
        a = _by_session(_run_b(self.fx())['ledger'])['2026-09-03']
        b = _by_session(_run_b(self.fx(), resolver=r75)['ledger'])['2026-09-03']
        self.assertEqual((a['lot'], b['lot']), (65, 75))
        self.assertNotEqual(a['net_rs'], b['net_rs'])
        calls = []

        def none(index, session, expiry):
            calls.append((index, session, expiry))
            return None, {'lot_source': 'ambiguous_expiry_cycle_coexistence',
                          'unavailable_reason': 'ambiguous_expiry_cycle_coexistence'}
        out = _run_b(self.fx(), resolver=none)
        self.assertEqual(out['counts']['eligible'], 0)
        self.assertGreater(out['counts']['lot_unresolved'], 0)
        self.assertTrue(all(isinstance(c[2], date) for c in calls))   # expiry is part of the identity

    def test_b4_duplicate_stored_keys_quarantine_the_poll(self):
        # Impossible under ml_ocs_unique; if the extract ever shows it, the poll is quarantined (identical or not).
        d = date(2026, 9, 3)
        k0 = _by_session(_run_b(self.fx())['ledger'])['2026-09-03']['k0']
        for dup in ('identical', 'conflicting'):
            fx = self.fx()
            rows = fx.polls[(d, 'E')]['chains']['2026-09-08']
            b, a = rows[(k0 + 400, 'CE')][0]
            rows[(k0 + 400, 'CE')] = [(b, a), (b, a) if dup == 'identical' else (b + 1.0, a + 1.0)]
            row = _by_session(_run_b(fx)['ledger'])['2026-09-03']
            self.assertEqual((row['status'], row['reason']), ('entry_poll_quarantined', 'poll_duplicate_keys'), dup)

    def test_b4_conflicting_rows_never_combined_even_without_integrity_check(self):
        p = re_.Poll(datetime(2026, 9, 3, 7, 1, tzinfo=UTC))
        p.rows[('2026-09-08', 23000, 'CE')] = [(10.0, 10.5), (11.0, 11.5)]
        self.assertEqual(re_.resolved_quote(p, '2026-09-08', 23000, 'CE'), (None, 'conflict'))
        self.assertEqual(re_.chain_conflicts(p, '2026-09-08'), 1)

    def test_b4_nf_poll_used_not_any_index_poll(self):
        d = date(2026, 9, 3)
        fx = self.fx()
        fx.any_override[(d, 'E')] = _ts(d, 12, 30)       # a BNF-only poll at 12:30 precedes the NF poll
        out_b = _by_session(_run_b(fx)['ledger'])['2026-09-03']
        self.assertEqual(out_b['status'], 'eligible')
        self.assertEqual(out_b['entry_poll_ts'], _ts(d, 12, 31).isoformat())

    def test_b4_interleaved_two_expiry_poll_is_quarantined(self):
        # The unique key has no expiry: a second expiry overwrites the rows it shares. The nearest-expiry ATM
        # would then come from an incomplete strike set, so the whole poll is unusable for Stage B.
        d = date(2026, 9, 3)
        k0 = _by_session(_run_b(self.fx())['ledger'])['2026-09-03']['k0']
        fx = self.fx()
        fx.overwrite_with_expiry(d, 'E', '2026-09-15', [(k0, 'CE'), (k0, 'PE'), (k0 + 50, 'CE')],
                                 spot=self.spots[d])
        row = _by_session(_run_b(fx)['ledger'])['2026-09-03']
        self.assertEqual((row['status'], row['reason']), ('entry_poll_quarantined', 'poll_mixed_expiries'))
        # Without the quarantine the A line for the near expiry would point at a different strike.
        ds = fx.dataset()
        near = ds.polls[_ts(d, 12, 31)].atm['2026-09-08']
        self.assertNotIn(k0, near.tied)

    def test_b4_interleaved_close_poll_excluded_from_rv_and_exit(self):
        d = date(2026, 9, 7)                      # planned exit of the 3 Sep entry, and an RV close
        fx = self.fx()
        fx.overwrite_with_expiry(d, 'C', '2026-09-15', [(23000, 'CE')], spot=self.spots[d])
        out = _run_b(fx)
        row = _by_session(out['ledger'])['2026-09-03']
        self.assertEqual(row['outcome_status'], 'exit_poll_mixed_expiries')
        later = _by_session(out['ledger'])['2026-09-09']
        self.assertEqual((later['status'], later.get('reason')), ('rv_unavailable', 'rv_insufficient_consecutive_closes'))

    def test_b4_missing_integrity_line_or_non_date_rows_fail_closed(self):
        d = date(2026, 9, 3)
        fx = self.fx()
        fx.omit_integrity.add((d, 'E'))
        row = _by_session(_run_b(fx)['ledger'])['2026-09-03']
        self.assertEqual(row['reason'], 'poll_integrity_missing')
        fx = self.fx()
        fx.polls[(d, 'E')]['chains']['X'] = {(30000, 'CE'): [(1.0, 1.1)]}
        row = _by_session(_run_b(fx)['ledger'])['2026-09-03']
        self.assertEqual(row['reason'], 'poll_non_date_expiry_rows')

    def test_f3_omitted_earlier_expiry_without_atm_is_quarantined_not_skipped(self):
        # Codex interim case: an earlier-dated expiry has stored NF rows but no valid ATM pair, the extract emits
        # R/A lines only for the later expiry, and only the D line (whole chain) shows two valid expiries.
        d = date(2026, 9, 3)
        fx = self.fx()
        fx.hidden[(d, 'E')] = {'2026-09-03': {(26000, 'CE'): [(None, 5.0)], (26050, 'PE'): [(3.0, 2.0)]}}
        ds = fx.dataset()
        p = ds.polls[_ts(d, 12, 31)]
        self.assertEqual(p.valid_expiries(), ['2026-09-08'])            # the later one is all the rows show
        self.assertEqual(p.expiry_inventory, ('2026-09-03', '2026-09-08'))
        row = _by_session(_run_b(fx)['ledger'])['2026-09-03']
        self.assertEqual((row['status'], row['reason']), ('entry_poll_quarantined', 'poll_mixed_expiries'))
        self.assertNotIn('expiry', row)                                  # never silently took the later one

    def test_f3_single_incomplete_expiry(self):
        d = date(2026, 9, 3)
        fx = self.fx()
        rows = fx.polls[(d, 'E')]['chains']['2026-09-08']
        for key in [k for k in rows if k[1] == 'PE']:
            del rows[key]                                                # no PE at all: no ATM pair, no A line
        row = _by_session(_run_b(fx)['ledger'])['2026-09-03']
        self.assertEqual(row['status'], 'atm_unavailable')
        fx = self.fx()
        k0 = _by_session(_run_b(self.fx())['ledger'])['2026-09-03']['k0']
        fx.drop_legs(d, 'E', [(k0 - 400, 'PE')])                           # ATM fine, one wing missing
        row = _by_session(_run_b(fx)['ledger'])['2026-09-03']
        self.assertEqual(row['status'], 'entry_quote_incomplete')

    def test_f3_inventory_must_cover_emitted_expiries(self):
        p = re_.Poll(datetime(2026, 9, 3, 7, 1, tzinfo=UTC))
        p.rows[('2026-09-08', 23000, 'CE')] = [(10.0, 10.5)]
        p.integrity, p.expiry_inventory = (1, 1, 1, 0), ('2026-09-15',)
        self.assertEqual(re_.poll_integrity_problem(p), 'poll_inventory_mismatch')

    def test_f3_f4_malformed_d_lines_and_impossible_dates_fail_explicitly(self):
        ts = '2026-09-03T07:01:00.000000Z'
        bad_lines = [
            f'D|{ts}|10|10|1|0',                          # old 5-field shape
            f'D|{ts}|10|x|1|0|2026-09-08',               # non-integer
            f'D|{ts}|10|10|-1|0|',                       # negative
            f'D|{ts}|10|10|2|0|2026-09-08',              # inventory shorter than count
            f'D|{ts}|10|10|1|0|2026-99-99',              # shaped but impossible
            f'R|{ts}|2026-02-30|23000/C:1,2',            # impossible date in a row line
            f'A|{ts}|X|23000|1|1|23000|1',               # ATM on a non-date expiry
        ]
        for line in bad_lines:
            with self.assertRaises(re_.ExtractFormatError, msg=line):
                re_.parse_extract(line)
        ok = re_.parse_extract(f'D|{ts}|10|10|1|2|2026-09-08')
        self.assertEqual(ok.polls[re_._ts(ts)].integrity, (10, 10, 1, 2))

    # ---- expiry-boundary fixtures --------------------------------------------------------------
    def test_expiry_day_entry_is_horizon_beyond_expiry(self):
        rows = _by_session(_run_b(self.fx())['ledger'])
        self.assertEqual(rows['2026-09-01']['expiry'], '2026-09-01')
        self.assertEqual(rows['2026-09-01']['status'], 'horizon_beyond_expiry')

    def test_stale_expired_rows_are_never_selected(self):
        d = date(2026, 9, 3)
        fx = self.fx()
        chains = fx.polls[(d, 'E')]['chains']
        chains['2026-09-01'] = chains.pop('2026-09-08')     # chain stored with an already-expired date
        row = _by_session(_run_b(fx)['ledger'])['2026-09-03']
        self.assertEqual(row['status'], 'no_valid_expiry')

    def test_exit_on_expiry_day_with_rolled_chain_stays_unresolved(self):
        # Entry Thu 10 Sep, expiry Tue 15 Sep, planned exit 15 Sep (14 Sep holiday). The 15 Sep close carries
        # only the next expiry (rolled). No later close can price an expired contract: blocked, unresolved.
        fx = self.fx()
        chains = fx.polls[(date(2026, 9, 15), 'C')]['chains']
        chains['2026-09-22'] = chains.pop('2026-09-15')
        out = _run_b(fx, start=date(2026, 9, 10))           # 10 Sep is then the first accepted entry
        row = _by_session(out['ledger'])['2026-09-10']
        self.assertTrue(row['exit_on_expiry'])
        self.assertEqual(row['outcome_status'], 'exit_expiry_absent')
        seq = _by_session(out['sequential_ledger'])
        taken = seq['2026-09-10']
        self.assertEqual(taken['resolution'], 'unresolved_contract_expired')
        self.assertEqual(taken['mark_attempts'], [])
        self.assertEqual(taken['occupied_through'], 'unresolved_blocked')
        self.assertTrue(all(r['action'] == 'skipped_occupied' for r in out['sequential_ledger']
                            if r['session'] > '2026-09-10'))
        self.assertGreater(out['summary']['sequential']['unresolved_max_loss_rs'], 0)

    def test_exit_on_expiry_day_missing_close_is_not_marked_on_a_later_day(self):
        fx = self.fx()
        fx.drop_poll(date(2026, 9, 15), 'C')
        out = _run_b(fx, start=date(2026, 9, 10))
        self.assertEqual(_by_session(out['ledger'])['2026-09-10']['outcome_status'], 'missing_close')
        taken = _by_session(out['sequential_ledger'])['2026-09-10']
        self.assertEqual((taken['resolution'], taken['mark_attempts']), ('unresolved_contract_expired', []))

    def test_conservation_and_determinism(self):
        fx = self.fx()
        fx.drop_poll(date(2026, 9, 7), 'C')
        a, b = _run_b(fx), _run_b(fx)
        self.assertEqual(a['output_sha256'], b['output_sha256'])
        counts = a['counts']
        self.assertEqual(counts['sessions'], sum(v for k, v in counts.items() if k != 'sessions'))
        seq = a['summary']['sequential']
        self.assertEqual(seq['accepted'] + seq['skipped_occupied'], counts['eligible'])
        self.assertEqual(seq['accepted'], seq['accepted_resolved'] + seq['accepted_pending']
                         + seq['accepted_deferred_mark'] + seq['accepted_unresolved'])

    def test_pending_at_cutoff(self):
        out = _run_b(self.fx(), end=date(2026, 9, 25), cutoff=date(2026, 9, 23))
        st = {r['session']: r.get('outcome_status') for r in out['ledger'] if r['status'] == 'eligible'}
        self.assertEqual(st.get('2026-09-23'), 'pending_not_matured')


# ---------------------------------------------------------------------------
# Stage A legacy emulation
# ---------------------------------------------------------------------------
LEGACY_RULE = re_.Rule(name='legacy', ivrv_lo=1.3, ivrv_hi=1.7)


class LegacyEmulationTests(unittest.TestCase):
    def setUp(self):
        self.sessions = _sessions(date(2026, 8, 20), date(2026, 9, 25))
        self.spots = _wiggle(self.sessions)

    def run_a(self, fx, rule=LEGACY_RULE):
        return re_.run_legacy_sql_v0(fx.dataset(), rule, date(2026, 9, 1), date(2026, 9, 18), date(2026, 9, 25),
                                     calendar_start=date(2026, 8, 20))

    _scale = None

    def _fixture_at(self, scale):
        fx = Fixture(self.sessions, self.spots, _next_tuesday)
        for p in fx.polls.values():
            for e in p['chains']:
                p['chains'][e] = _quotes(self.spots[p['ts'].astimezone(re_.IST).date()], scale)
        return fx

    def _force_in_band(self):
        # Find (once) a premium scale that puts 3 Sep inside the legacy v1.3 band and makes it eligible.
        scales = [LegacyEmulationTests._scale] if LegacyEmulationTests._scale else [x / 2.0 for x in range(40, 2000)]
        for scale in scales:
            fx = self._fixture_at(scale)
            out = self.run_a(fx)
            if _by_session(out['ledger'])['2026-09-03']['status'] == 'eligible':
                LegacyEmulationTests._scale = scale
                return fx, out
        self.fail('no scale puts 3 Sep in band')

    def test_reproduces_hand_computed_legacy_net(self):
        fx, out = self._force_in_band()
        row = _by_session(out['ledger'])['2026-09-03']
        self.assertEqual(row['outcome_status'], 'resolved')
        self.assertEqual(row['lot_label'], re_.LEGACY_LOT_LABEL)
        # Independent hand computation from the fixture's own rows.
        k0 = row['feat'][0]['k0']
        e = fx.polls[(date(2026, 9, 3), 'E')]['chains']['2026-09-08']
        x = fx.polls[(date(2026, 9, 7), 'C')]['chains']['2026-09-08']
        sell = e[(k0, 'CE')][0][0] + e[(k0, 'PE')][0][0]
        buy = e[(k0 + 400, 'CE')][0][1] + e[(k0 - 400, 'PE')][0][1]
        xsell = x[(k0 + 400, 'CE')][0][0] + x[(k0 - 400, 'PE')][0][0]
        xbuy = x[(k0, 'CE')][0][1] + x[(k0, 'PE')][0][1]
        gross = (sell - buy + xsell - xbuy) * 65
        fee = (4 * 2 * 20 * 1.18 + 0.0015 * (sell + xsell) * 65 + 0.0003553 * 1.18 * (sell + buy + xsell + xbuy) * 65
               + 0.00003 * (buy + xbuy) * 65)
        self.assertAlmostEqual(row['net_rs'], round(gross - fee, 2), places=2)
        self.assertNotIn('parity_unproven', row['flags'])

    def test_23_jul_style_missing_close_is_eligible_not_dropped(self):
        fx, _ = self._force_in_band()
        fx.drop_poll(date(2026, 9, 7), 'C')
        row = _by_session(self.run_a(fx)['ledger'])['2026-09-03']
        self.assertEqual((row['status'], row['outcome_status']), ('eligible', 'missing_close'))

    def test_any_index_poll_rule_is_reproduced(self):
        fx, _ = self._force_in_band()
        d = date(2026, 9, 3)
        fx.any_override[(d, 'E')] = _ts(d, 12, 30)        # BNF-only poll first: the sweep saw no NF rows
        row = _by_session(self.run_a(fx)['ledger'])['2026-09-03']
        self.assertEqual(row['status'], 'no_nf_atm')
        self.assertIn('any_index_poll_differs_from_first_nf_poll', row['flags'])
        self.assertEqual(_by_session(_run_b(fx, rule=OPEN_BAND)['ledger'])['2026-09-03']['status'], 'eligible')

    def test_duplicate_rows_double_count_like_the_sql(self):
        fx, _ = self._force_in_band()
        d = date(2026, 9, 3)
        k0 = _by_session(self.run_a(fx)['ledger'])['2026-09-03']['feat'][0]['k0']
        rows = fx.polls[(d, 'E')]['chains']['2026-09-08']
        rows[(k0 + 400, 'CE')] = rows[(k0 + 400, 'CE')] * 2
        row = _by_session(self.run_a(fx)['ledger'])['2026-09-03']
        self.assertEqual(row['nl'], 5)
        self.assertEqual((row['status'], row['reason']), ('not_in_cell', 'structure_not_priced'))

    def test_multi_expiry_cross_join_flags_parity_unproven(self):
        fx, _ = self._force_in_band()
        d = date(2026, 9, 3)
        # A second expiry whose strikes are disjoint from the first (the unique key has no expiry column).
        far = {(k + 25, o): v for (k, o), v in _quotes(self.spots[d], 200.0).items()}
        fx.polls[(d, 'E')]['chains']['2026-09-15'] = far
        row = _by_session(self.run_a(fx)['ledger'])['2026-09-03']
        self.assertIn('legacy_multi_expiry_cross_join', row['flags'])
        self.assertEqual(len(row['feat']), 4)

    def test_observed_calendar_outage_shifts_legacy_exit_only(self):
        fx, _ = self._force_in_band()
        fx.observed.remove(date(2026, 9, 4))           # brain outage on 4 Sep (chain data still exists)
        a = _by_session(self.run_a(fx)['ledger'])['2026-09-02']
        b = _by_session(_run_b(fx)['ledger'])['2026-09-02']
        if a['status'] == 'eligible':
            self.assertEqual(a['planned_exit_date'], '2026-09-07')
        self.assertEqual(b['planned_exit_date'], '2026-09-04')

    def test_legacy_expiry_day_exit_with_rolled_chain_is_missing_exit_legs(self):
        fx, out = self._force_in_band()
        on_exp = [r for r in out['ledger'] if r['status'] == 'eligible' and r.get('exit_on_expiry')]
        self.assertTrue(on_exp, 'fixture must contain an expiry-day C2 exit')
        r = on_exp[0]
        xd = date.fromisoformat(r['planned_exit_date'])
        chains = fx.polls[(xd, 'C')]['chains']
        exp = xd.isoformat()
        chains[(xd + timedelta(days=7)).isoformat()] = chains.pop(exp)
        row = _by_session(self.run_a(fx)['ledger'])[r['session']]
        # The sweep's inner join on the entry expiry finds no exit legs: no row, never a fabricated close.
        self.assertEqual((row['status'], row['outcome_status']), ('eligible', 'missing_exit_legs'))
        self.assertNotIn('net_rs', row)

    def test_legacy_rejects_other_rules(self):
        fx = Fixture(self.sessions, self.spots, _next_tuesday)
        with self.assertRaises(ValueError):
            self.run_a(fx, rule=re_.Rule(name='ic', structure='IC', short_off=100))

    def test_conservation_and_determinism(self):
        fx, a = self._force_in_band()
        b = self.run_a(fx)
        self.assertEqual(a['output_sha256'], b['output_sha256'])
        c = a['counts']
        self.assertEqual(c['sessions'], sum(v for k, v in c.items() if k != 'sessions'))


# ---------------------------------------------------------------------------
# Runner (B3)
# ---------------------------------------------------------------------------
def _part_text(part, a, b, body):
    md5 = hashlib.md5(body.encode('utf-8')).hexdigest()
    n = len(body.split('\n')) if body else 0
    return f'#format=nf_quotes_v2\n#part={part}\n#range={a}..{b}\n#n={n}\n#md5={md5}\n#body\n{body}\n'


class RunnerTests(unittest.TestCase):
    MANIFEST = [('01', '2026-09-01', '2026-09-15'), ('02', '2026-09-16', '2026-10-05')]

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, part, text):
        with open(os.path.join(self.dir, f'part{part}.txt'), 'w', encoding='utf-8') as fh:
            fh.write(text)

    def good(self):
        self.write('01', _part_text('01', '2026-09-01', '2026-09-15', 'S|2026-09-01'))
        self.write('02', _part_text('02', '2026-09-16', '2026-10-05', 'S|2026-09-16'))

    def load(self, manifest=None):
        old = runner.CALENDAR_START
        runner.CALENDAR_START = date(2026, 9, 1)
        try:
            return runner.load_parts(self.dir, manifest or self.MANIFEST)
        finally:
            runner.CALENDAR_START = old

    def test_empty_dir_fails(self):
        with self.assertRaises(SystemExit):
            self.load()

    def test_good_parts_load(self):
        self.good()
        ds, report = self.load()
        self.assertEqual(ds.observed_sessions, [date(2026, 9, 1), date(2026, 9, 16)])
        self.assertEqual([r['part'] for r in report], ['01', '02'])

    def test_missing_extra_tampered_and_misranged_parts_fail(self):
        self.good()
        os.remove(os.path.join(self.dir, 'part02.txt'))
        with self.assertRaises(SystemExit):
            self.load()
        self.good()
        self.write('03', _part_text('03', '2026-10-06', '2026-10-06', 'S|2026-10-06'))
        with self.assertRaises(SystemExit):
            self.load()
        os.remove(os.path.join(self.dir, 'part03.txt'))
        text = _part_text('02', '2026-09-16', '2026-10-05', 'S|2026-09-16')
        self.write('02', text.replace('S|2026-09-16\n', 'S|2026-09-17\n'))           # body tampered
        with self.assertRaises(SystemExit):
            self.load()
        self.write('02', _part_text('02', '2026-09-16', '2026-10-05', 'S|2026-09-01'))   # line outside range
        with self.assertRaises(SystemExit):
            self.load()
        self.write('02', _part_text('02', '2026-09-17', '2026-10-05', 'S|2026-09-17'))   # range != manifest
        with self.assertRaises(SystemExit):
            self.load()

    def test_manifest_gap_or_overlap_fails(self):
        self.good()
        with self.assertRaises(SystemExit):
            self.load([('01', '2026-09-01', '2026-09-15'), ('02', '2026-09-17', '2026-10-05')])
        with self.assertRaises(SystemExit):
            self.load([('01', '2026-09-01', '2026-09-15'), ('02', '2026-09-15', '2026-10-05')])

    def test_malformed_extract_is_an_explicit_extract_failure(self):
        self.write('01', _part_text('01', '2026-09-01', '2026-09-15', 'R|2026-09-03T07:01:00.000000Z|2026-99-99|1/C:1,2'))
        self.write('02', _part_text('02', '2026-09-16', '2026-10-05', 'S|2026-09-16'))
        with self.assertRaises(runner.ExtractError) as cm:
            self.load()
        self.assertIn('malformed expiry', str(cm.exception))

    def test_stage_a_gate_fails_on_mismatch_extra_or_unproven(self):
        ledger = []
        for s, v in runner.PUBLISHED_STAGE_A.items():
            r = {'session': s, 'status': 'eligible', 'flags': []}
            r.update({'outcome_status': 'missing_close'} if v is None else {'outcome_status': 'resolved', 'net_rs': v})
            ledger.append(r)
        self.assertTrue(runner.stage_a_gate({'ledger': ledger})['pass'])
        bad = [dict(r) for r in ledger]
        bad[0]['net_rs'] = bad[0]['net_rs'] + 1.0
        self.assertFalse(runner.stage_a_gate({'ledger': bad})['pass'])
        extra = ledger + [{'session': '2026-07-09', 'status': 'eligible', 'flags': [], 'outcome_status': 'resolved',
                           'net_rs': 1.0}]
        self.assertFalse(runner.stage_a_gate({'ledger': extra})['pass'])
        unproven = [dict(r) for r in ledger]
        unproven[1]['flags'] = ['parity_unproven']
        self.assertFalse(runner.stage_a_gate({'ledger': unproven})['pass'])
        jul23 = [dict(r) for r in ledger]
        for r in jul23:
            if r['session'] == '2026-07-23':
                r.update({'outcome_status': 'resolved', 'net_rs': 10.0})
        self.assertFalse(runner.stage_a_gate({'ledger': jul23})['pass'])


class EndToEndRunnerTests(unittest.TestCase):
    """Synthetic fixture -> real part files -> runner.main: proves the whole pipeline before real data."""

    def test_pipeline_writes_outputs_and_fails_gate_on_unpublished_data(self):
        sessions = _sessions(date(2026, 8, 20), date(2026, 9, 25))
        fx = Fixture(sessions, _wiggle(sessions), _next_tuesday)
        text = fx.render()
        manifest = [('01', '2026-08-20', '2026-09-07'), ('02', '2026-09-08', '2026-09-25')]

        def line_day(line):
            kind, rest = line.split('|', 1)
            if kind in ('S', 'W'):
                return date.fromisoformat(rest.split('|')[0])
            return re_._ts(rest.split('|')[0]).astimezone(re_.IST).date()

        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, 'out')
        try:
            for part, a, b in manifest:
                a_d, b_d = date.fromisoformat(a), date.fromisoformat(b)
                body = '\n'.join(l for l in text.split('\n') if a_d <= line_day(l) <= b_d)
                with open(os.path.join(tmp, f'part{part}.txt'), 'w', encoding='utf-8') as fh:
                    fh.write(_part_text(part, a, b, body))
            saved = (runner.PART_MANIFEST, runner.WINDOW, runner.CUTOFF, runner.CALENDAR_START)
            runner.PART_MANIFEST = manifest
            runner.WINDOW = (date(2026, 9, 1), date(2026, 9, 18))
            runner.CUTOFF = date(2026, 9, 25)
            runner.CALENDAR_START = date(2026, 8, 20)
            try:
                import io
                import contextlib
                with contextlib.redirect_stdout(io.StringIO()):
                    code = runner.main(tmp, out)
            finally:
                runner.PART_MANIFEST, runner.WINDOW, runner.CUTOFF, runner.CALENDAR_START = saved
            self.assertEqual(code, 2)          # synthetic data cannot reproduce the published list
            for name in ('stage_a.json', 'stage_b.json', 'bridge.json', 'report.json', 'SHA256SUMS'):
                self.assertTrue(os.path.exists(os.path.join(out, name)), name)
            with open(os.path.join(out, 'SHA256SUMS')) as fh:
                sums = dict(reversed(l.split('  ')) for l in fh.read().strip().split('\n'))
            for name, digest in sums.items():
                with open(os.path.join(out, name.strip()), 'rb') as fh:
                    self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), digest)
            import json as _json
            with open(os.path.join(out, 'report.json')) as fh:
                rep = _json.load(fh)
            self.assertFalse(rep['stage_a_gate']['pass'])
            self.assertEqual(rep['dataset_sha256'], re_.parse_extract('\n'.join(
                b for b in [open(os.path.join(tmp, f'part{p}.txt')).read().split('#body\n', 1)[1].rstrip('\n')
                            for p, _, _ in manifest])).sha256)
            self.assertEqual(rep['integrity']['stage_b_poll_quarantine'], {})
        finally:
            shutil.rmtree(tmp)


class FeeTests(unittest.TestCase):
    def test_replay_fee_adds_stt_on_exit_sales_only(self):
        entry = {'sell_px': 250.0, 'buy_px': 60.0}
        exit_ = {'exit_sell_px': 5.0, 'exit_buy_px': 100.0}
        t = re_.fees('teacher_v1', 4, 65, entry, exit_)
        r = re_.fees('replay_fee_v1', 4, 65, entry, exit_)
        self.assertAlmostEqual(r['total'] - t['total'], 0.0015 * 5.0 * 65, places=9)
        self.assertEqual(t['brokerage'], 160.0)

    def test_teacher_fee_matches_trade_278_breakdown(self):
        entry = {'sell_px': 133.75 + 123.1, 'buy_px': 37.55 + 47.7}
        exit_ = {'exit_sell_px': 3.75 + 1.35, 'exit_buy_px': 7013.5 / 65 - (3.75 + 1.35)}
        t = re_.fees('teacher_v1', 4, 65, entry, exit_)
        self.assertAlmostEqual(t['total'], 226.5, delta=0.6)


if __name__ == '__main__':
    unittest.main()
