"""Regression fixtures for the offline replay engine (synthetic data only)."""
import math
import os
import sys
import unittest
from datetime import date, datetime, time, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import replay_engine as re_  # noqa: E402

REPO = os.path.abspath(os.path.join(HERE, '..', '..', '..'))


def _price(spot, k, opt, scale):
    intrinsic = max(spot - k, 0) if opt == 'CE' else max(k - spot, 0)
    return round(intrinsic + scale * math.exp(-((k - spot) / 300.0) ** 2) + 2.0, 2)


def _chain(d, hhmm, spot, expiry, scale=120.0, slot='E', index='NF', drop=(), override=None):
    h, m = hhmm
    ts = datetime.combine(d, time(h, m), re_.IST).astimezone(re_.timezone.utc)
    quotes = {}
    center = int(round(spot / 50.0)) * 50
    for k in range(center - 1000, center + 1001, 50):
        for opt in ('CE', 'PE'):
            if (k, opt) in drop:
                continue
            p = _price(spot, k, opt, scale)
            quotes[(k, opt)] = (round(p - 0.05, 2), round(p + 0.05, 2))
    if override:
        quotes.update(override)
    return re_.Chain(ts, slot, index, expiry, quotes)


def _dataset(sessions, spots, expiry_for, skip_close=(), skip_entry=(), extra=(), observed=None, scale_for=None):
    chains = []
    for d in sessions:
        exp = expiry_for(d)
        sc = scale_for(d) if scale_for else 120.0
        if d not in skip_entry:
            chains.append(_chain(d, (12, 31), spots[d], exp, sc, 'E'))
        if d not in skip_close:
            chains.append(_chain(d, (15, 21), spots[d], exp, sc, 'C'))
    chains.extend(extra)
    return re_.Dataset(observed if observed is not None else list(sessions), chains, {}, 'synthetic')


def _sessions(start, end):
    holidays = {date.fromisoformat(h) for h in re_.NSE_HOLIDAYS_2026}
    out, cur = [], start
    while cur <= end:
        if cur.weekday() < 5 and cur not in holidays:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def _next_tuesday(d):
    return d + timedelta(days=(1 - d.weekday()) % 7)


OPEN_BAND = re_.Rule(name='logic', ivrv_lo=0.0, ivrv_hi=1e9)


def _wiggle(sessions, base=23000.0):
    return {d: base + (40 if i % 2 else -40) + i * 5 for i, d in enumerate(sessions)}


class CalendarAndAuthorityTests(unittest.TestCase):
    def test_holidays_match_brain(self):
        sys.path.insert(0, os.path.join(REPO, 'app', 'src', 'main', 'python'))
        import brain  # noqa: E402
        self.assertEqual(sorted(brain._CONST['NSE_HOLIDAYS']), sorted(re_.NSE_HOLIDAYS_2026))

    def test_holiday_crossing_exchange_calendar(self):
        cal = re_.Calendar(re_.CORRECTED_V1, [], date(2026, 9, 1), date(2026, 10, 15))
        # Wed 30 Sep + 2 sessions: Thu 1 Oct, (Fri 2 Oct holiday), Mon 5 Oct.
        self.assertEqual(cal.offset(date(2026, 9, 30), 2), date(2026, 10, 5))
        # 14 Sep 2026 is a holiday: Fri 11 Sep + 1 -> Tue 15 Sep.
        self.assertEqual(cal.offset(date(2026, 9, 11), 1), date(2026, 9, 15))

    def test_observed_calendar_outage_shifts_legacy_only(self):
        observed = [date(2026, 9, 28), date(2026, 9, 29), date(2026, 10, 1)]   # 30 Sep missing (outage)
        legacy = re_.Calendar(re_.LEGACY_V0, observed, date(2026, 9, 20), date(2026, 10, 15))
        corrected = re_.Calendar(re_.CORRECTED_V1, observed, date(2026, 9, 20), date(2026, 10, 15))
        self.assertEqual(legacy.offset(date(2026, 9, 29), 1), date(2026, 10, 1))
        self.assertEqual(corrected.offset(date(2026, 9, 29), 1), date(2026, 9, 30))

    def test_unknown_index_and_unverified_date_fail_closed(self):
        self.assertIsNone(re_.lot_size('FINNIFTY', date(2026, 9, 1)))
        self.assertIsNone(re_.lot_size('NF', date(2025, 12, 1)))
        self.assertEqual(re_.lot_size('NF', date(2026, 9, 1)), 65)
        self.assertIsNone(re_.strike_step('XYZ'))


class StructureValidityTests(unittest.TestCase):
    def test_debit_cost_above_width_rejected(self):
        s = re_.vertical('BLC', 23000, 23100)          # 100-point debit spread
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


class ChainSelectionTests(unittest.TestCase):
    def test_duplicate_and_multi_expiry(self):
        d = date(2026, 9, 16)
        a = _chain(d, (12, 31), 23000, date(2026, 9, 22))
        b = _chain(d, (12, 31), 23000, date(2026, 9, 22))
        ch, flags = re_.select_chain([a, b], 'NF', d)
        self.assertIsNotNone(ch)
        self.assertIn('duplicate_chain_identical', flags)
        c = _chain(d, (12, 31), 23100, date(2026, 9, 22))
        ch, flags = re_.select_chain([a, c], 'NF', d)
        self.assertIsNone(ch)
        self.assertIn('duplicate_chain_conflict', flags)
        far = _chain(d, (12, 31), 23000, date(2026, 9, 29))
        ch, flags = re_.select_chain([far, a], 'NF', d)
        self.assertEqual(ch.expiry, date(2026, 9, 22))
        self.assertIn('multi_expiry_nearest_selected', flags)

    def test_invalid_quotes_never_priced(self):
        self.assertFalse(re_.valid_quote((None, 10.0)))
        self.assertFalse(re_.valid_quote((5.0, 4.0)))
        self.assertFalse(re_.valid_quote((0.0, 0.0)))
        self.assertTrue(re_.valid_quote((0.0, 0.05)))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.sessions = _sessions(date(2026, 8, 20), date(2026, 9, 25))
        self.spots = _wiggle(self.sessions)

    def _run(self, defs=re_.CORRECTED_V1, rule=OPEN_BAND, **kw):
        ds = _dataset(self.sessions, self.spots, _next_tuesday, **kw)
        return re_.run(ds, defs, rule, date(2026, 9, 1), date(2026, 9, 18), date(2026, 9, 25))

    def test_missing_scheduled_close_keeps_entry_and_blocks_capital(self):
        # Entry Thu 3 Sep (expiry Tue 8 Sep) -> planned exit Mon 7 Sep, whose close is missing.
        out = self._run(skip_close={date(2026, 9, 7)})
        row = {r['session']: r for r in out['ledger']}['2026-09-03']
        self.assertEqual(row['status'], 'eligible')
        self.assertEqual(row['outcome_status'], 'missing_close')
        self.assertNotIn('net_rs', row)
        seq = {r['session']: r for r in out['sequential_ledger']}
        self.assertIn('2026-09-03', seq)

    def test_unpriced_exit_blocks_capital_past_planned_exit(self):
        # Entry Wed 2 Sep -> planned exit Fri 4 Sep, close missing. First later priced close is Mon 7 Sep,
        # so capital stays blocked through 7 Sep and the 3, 4 and 7 Sep entries are skipped.
        out = self._run(skip_close={date(2026, 9, 4)})
        seq = {r['session']: r for r in out['sequential_ledger']}
        self.assertEqual(seq['2026-09-02']['action'], 'taken')
        self.assertIsNone(seq['2026-09-02']['net_rs'])
        self.assertEqual(seq['2026-09-02']['occupied_through'], '2026-09-07')
        for s in ('2026-09-03', '2026-09-04', '2026-09-07'):
            if s in seq:
                self.assertEqual(seq[s]['action'], 'skipped_occupied', s)
        self.assertEqual(out['summary']['sequential']['accepted_unresolved'], 1)

    def test_expiry_coincident_exit_is_flagged_not_dropped(self):
        out = self._run()
        rows = {r['session']: r for r in out['ledger']}
        # Thu 10 Sep + 2 sessions = Tue 15 Sep = expiry (14 Sep is a holiday: Fri 11, Tue 15).
        r = rows['2026-09-10']
        self.assertEqual(r['planned_exit_date'], '2026-09-15')
        self.assertTrue(r['exit_on_expiry'])
        self.assertEqual(r['outcome_status'], 'resolved')
        # Fri 11 Sep + 2 = Wed 16 Sep > expiry 15 Sep -> ineligible at decision time.
        self.assertEqual(rows['2026-09-11']['status'], 'horizon_beyond_expiry')

    def test_zero_rv_is_not_bucketed(self):
        flat = {d: 23000.0 for d in self.sessions}
        ds = _dataset(self.sessions, flat, _next_tuesday)
        out = re_.run(ds, re_.CORRECTED_V1, OPEN_BAND, date(2026, 9, 1), date(2026, 9, 18), date(2026, 9, 25))
        reasons = {r.get('reason') for r in out['ledger'] if r['status'] == 'rv_unavailable'}
        self.assertIn('rv_zero_or_invalid', reasons)
        self.assertEqual(out['counts']['eligible'], 0)

    def test_five_consecutive_rule_vs_legacy_any_available(self):
        gap = {date(2026, 8, 31)}                      # one missing close inside the RV window of early Sep
        corrected = self._run(skip_close=gap)
        legacy = self._run(defs=re_.LEGACY_V0, skip_close=gap)
        c = {r['session']: r for r in corrected['ledger']}['2026-09-02']
        l = {r['session']: r for r in legacy['ledger']}['2026-09-02']
        self.assertEqual(c['status'], 'rv_unavailable')
        self.assertEqual(c['reason'], 'rv_insufficient_consecutive_closes')
        self.assertNotEqual(l['status'], 'rv_unavailable')
        self.assertEqual(l['rv_returns'], 3)

    def test_session_outage_distinguished_from_missing_close(self):
        out = self._run(skip_close={date(2026, 9, 7)}, skip_entry={date(2026, 9, 7)})
        row = {r['session']: r for r in out['ledger']}['2026-09-03']
        self.assertEqual(row['outcome_status'], 'session_outage')

    def test_conservation_and_determinism(self):
        a = self._run(skip_close={date(2026, 9, 7)})
        b = self._run(skip_close={date(2026, 9, 7)})
        self.assertEqual(a['output_sha256'], b['output_sha256'])
        counts = a['counts']
        self.assertEqual(counts['sessions'], sum(v for k, v in counts.items() if k != 'sessions'))
        seq = a['summary']['sequential']
        self.assertEqual(seq['accepted'] + seq['skipped_occupied'], counts['eligible'])

    def test_band_filter_and_pending(self):
        out = re_.run(_dataset(self.sessions, self.spots, _next_tuesday), re_.CORRECTED_V1, OPEN_BAND,
                      date(2026, 9, 1), date(2026, 9, 25), date(2026, 9, 23))
        statuses = {r['session']: r.get('outcome_status') for r in out['ledger'] if r['status'] == 'eligible'}
        self.assertEqual(statuses.get('2026-09-22'), None)   # expiry day: horizon beyond expiry
        self.assertEqual(statuses.get('2026-09-23'), 'pending_not_matured')


class FeeTests(unittest.TestCase):
    def test_replay_fee_adds_stt_on_exit_sales_only(self):
        entry = {'sell_px': 250.0, 'buy_px': 60.0}
        exit_ = {'exit_sell_px': 5.0, 'exit_buy_px': 100.0}
        t = re_.fees('teacher_v1', 4, 65, entry, exit_)
        r = re_.fees('replay_fee_v1', 4, 65, entry, exit_)
        self.assertAlmostEqual(r['total'] - t['total'], 0.0015 * 5.0 * 65, places=9)
        self.assertEqual(t['brokerage'], 160.0)

    def test_teacher_fee_matches_trade_278_breakdown(self):
        # trades_v2 #278 friction_breakdown_json: total 226.5 (entry 22236.5 turnover, close 7013.5).
        entry = {'sell_px': 133.75 + 123.1, 'buy_px': 37.55 + 47.7}
        exit_ = {'exit_sell_px': 3.75 + 1.35, 'exit_buy_px': 7013.5 / 65 - (3.75 + 1.35)}
        t = re_.fees('teacher_v1', 4, 65, entry, exit_)
        self.assertAlmostEqual(t['total'], 226.5, delta=0.6)


if __name__ == '__main__':
    unittest.main()
