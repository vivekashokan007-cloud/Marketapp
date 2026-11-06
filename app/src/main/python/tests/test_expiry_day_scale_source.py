"""D9 repair: the direction scale may read a separate next-expiry chain on an index's own expiry day.

Required coverage (review instruction, 6 Nov 2026):
  1. the 3 November NF outage                       -> test_nov03_nf_outage_*
  2. normal sessions for both indices unchanged     -> test_normal_session_*
  3. a FAILED scale already stored                  -> test_stored_failure_*
  4. recovery applies to future polls only, never
     rewriting earlier decisions                    -> test_recovery_does_not_rewrite_*

Hard invariant throughout: the TRADING chain is never substituted or modified.
"""
import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import index_authority_v1 as ia  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def at(d, h, m, s=0):
    return datetime(d.year, d.month, d.day, h, m, s, tzinfo=IST)


def chain(spot, step, m=0.0095, n=4.0, width=12, tag=None):
    """A chain with a valid ATM straddle sized so compute_scale yields approximately `m`.

    `m` and `n` are targets, not assertions: the straddle total is set to
    m * SQRT_2_OVER_PI * spot * sqrt(n), which inverts compute_scale's own formula given that the
    legs below satisfy put-call parity exactly (ce_mid - pe_mid = spot - k), so k0 lands at the money
    and forward == spot.  Sizing it this way keeps fixtures inside SCALE_M_BOUNDS at realistic DTEs -
    a flat 0.85%-of-spot straddle gives m = 0.0015 at 14 sessions out, below the 0.002 floor, which is
    a fixture artefact rather than anything about the code under test.

    `tag` marks chain identity so substitution can be detected.
    """
    atm = round(spot / step) * step
    straddle = m * ia.SQRT_2_OVER_PI * spot * (n ** 0.5)
    half = straddle / 2.0
    strikes = {}
    for i in range(-width, width + 1):
        k = atm + i * step
        base = half * max(0.3, 1.0 - abs(i) * 0.05)
        tilt = (spot - k) / 2.0          # put-call parity: ce_mid - pe_mid = spot - k
        ce, pe = base + tilt, base - tilt
        if ce <= 0 or pe <= 0:
            continue                     # skip rather than floor: flooring would create |ce-pe| ties
        strikes[str(int(k))] = {'CE': {'bid': round(ce * 0.99, 2), 'ask': round(ce * 1.01, 2)},
                                'PE': {'bid': round(pe * 0.99, 2), 'ask': round(pe * 1.01, 2)}}
    out = {'strikes': strikes}
    if tag:
        out['_tag'] = tag
    return out


class Cal:
    version = 'test_cal_v1'
    covered_years = (2026, 2027)

    def is_session(self, d):
        return d.weekday() < 5

    def trading_seconds(self, start, end):
        if end <= start:
            return 0.0
        total, cur = 0.0, start
        while cur.date() < end.date():
            if self.is_session(cur.date()):
                close = at(cur.date(), 15, 30)
                if cur < close:
                    total += (close - max(cur, at(cur.date(), 9, 15))).total_seconds()
            cur = at(cur.date() + timedelta(days=1), 9, 15)
        if self.is_session(end.date()):
            total += max(0.0, (end - max(cur, at(end.date(), 9, 15))).total_seconds())
        return total


CAL = Cal()


# --------------------------------------------------------------------------------------------
# select_scale_chain
# --------------------------------------------------------------------------------------------
class TestSelectScaleChain(unittest.TestCase):
    SESSION = '2026-11-03'

    def test_front_expiry_used_when_forward_dated(self):
        """Normal session: front expiry is forward-dated, so the trading chain is used unchanged."""
        trade = chain(24000.0, 50, tag='TRADE')
        nxt = chain(24000.0, 50, tag='SCALE')
        sel = ia.select_scale_chain('NF', self.SESSION, trade, '2026-11-10', nxt, '2026-11-17')
        self.assertEqual(sel['source'], ia.SCALE_SOURCE_FRONT)
        self.assertIs(sel['chain'], trade)
        self.assertEqual(sel['expiry'], '2026-11-10')
        self.assertIsNone(sel['reason'])

    def test_next_expiry_used_on_own_expiry_day(self):
        trade = chain(24000.0, 50, tag='TRADE')
        nxt = chain(24000.0, 50, tag='SCALE')
        sel = ia.select_scale_chain('NF', self.SESSION, trade, self.SESSION, nxt, '2026-11-09')
        self.assertEqual(sel['source'], ia.SCALE_SOURCE_NEXT)
        self.assertIs(sel['chain'], nxt)
        self.assertEqual(sel['chain']['_tag'], 'SCALE')
        self.assertEqual(sel['expiry'], '2026-11-09')
        self.assertEqual(sel['trade_expiry'], self.SESSION)

    def test_expiry_before_session_also_takes_next(self):
        """A stale front expiry (< session) is as unusable as one equal to it."""
        sel = ia.select_scale_chain('NF', self.SESSION, chain(24000.0, 50), '2026-10-27',
                                    chain(24000.0, 50), '2026-11-09')
        self.assertEqual(sel['source'], ia.SCALE_SOURCE_NEXT)

    def test_missing_next_expiry_fails_closed(self):
        sel = ia.select_scale_chain('NF', self.SESSION, chain(24000.0, 50), self.SESSION, None, None)
        self.assertEqual(sel['source'], ia.SCALE_SOURCE_UNAVAILABLE)
        self.assertEqual(sel['reason'], 'NEXT_EXPIRY_CHAIN_MISSING')
        self.assertIsNone(sel['chain'])

    def test_malformed_next_expiry_fails_closed(self):
        sel = ia.select_scale_chain('NF', self.SESSION, chain(24000.0, 50), self.SESSION,
                                    chain(24000.0, 50), 'not-a-date')
        self.assertEqual(sel['source'], ia.SCALE_SOURCE_UNAVAILABLE)
        self.assertEqual(sel['reason'], 'NEXT_EXPIRY_INVALID')

    def test_next_expiry_not_actually_forward_dated_is_rejected(self):
        """A 'next' expiry equal to the session date must NOT be accepted - that would reintroduce
        the exact failure this repair removes."""
        for bad in (self.SESSION, '2026-10-27'):
            sel = ia.select_scale_chain('NF', self.SESSION, chain(24000.0, 50), self.SESSION,
                                        chain(24000.0, 50), bad)
            self.assertEqual(sel['source'], ia.SCALE_SOURCE_UNAVAILABLE, bad)
            self.assertEqual(sel['reason'], 'NEXT_EXPIRY_INVALID', bad)

    def test_empty_next_chain_fails_closed(self):
        for empty in ({}, None, {'strikes': {}}, {'strikes': {'0': {}}}):
            sel = ia.select_scale_chain('NF', self.SESSION, chain(24000.0, 50), self.SESSION,
                                        empty, '2026-11-09')
            self.assertEqual(sel['source'], ia.SCALE_SOURCE_UNAVAILABLE, repr(empty))
            self.assertEqual(sel['reason'], 'NEXT_EXPIRY_CHAIN_EMPTY', repr(empty))

    def test_invalid_trade_expiry_passes_through_to_front(self):
        """compute_scale keeps sole ownership of its EXPIRY_INVALID branch (fail closed, one place)."""
        trade = chain(24000.0, 50, tag='TRADE')
        sel = ia.select_scale_chain('NF', self.SESSION, trade, None, chain(24000.0, 50), '2026-11-09')
        self.assertEqual(sel['source'], ia.SCALE_SOURCE_FRONT)
        self.assertIs(sel['chain'], trade)

    def test_trading_chain_object_never_mutated(self):
        trade = chain(24000.0, 50, tag='TRADE')
        before = repr(trade)
        ia.select_scale_chain('NF', self.SESSION, trade, self.SESSION, chain(24000.0, 50), '2026-11-09')
        self.assertEqual(repr(trade), before)


# --------------------------------------------------------------------------------------------
# the outage, end to end through compute_scale + compute_direction
# --------------------------------------------------------------------------------------------
def direction_with(scale, session, polls, index, decision_dt):
    return ia.compute_direction(index, polls, session, decision_dt, scale, receipt_dt=decision_dt)


def polls_for(index, d, points):
    key = 'nfSpot' if index == 'NF' else 'bnfSpot'
    return [{'poll_ts': at(d, hh, mm).isoformat(), key: sp} for hh, mm, sp in points]


class TestNov03NfOutage(unittest.TestCase):
    """3 Nov 2026 was NF's own weekly expiry; NF recorded SCALE_EXPIRY_DAY_CHAIN_ONLY on 77 of 78
    polls while BNF ran normally."""
    SESSION = '2026-11-03'
    D = date(2026, 11, 3)
    # a path that clears the BULL threshold once a usable scale exists
    PATH = [(9, 20, 24000.0), (9, 25, 24040.0), (9, 30, 24080.0), (9, 35, 24120.0),
            (9, 45, 24170.0), (10, 0, 24210.0)]

    def setUp(self):
        self.polls = polls_for('NF', self.D, self.PATH)
        self.decision = at(self.D, 10, 0)
        self.a_dt = at(self.D, 9, 20)

    def test_nov03_nf_outage_reproduced_with_front_chain(self):
        """Before the repair: front expiry == session date -> no direction, all session."""
        sc = ia.compute_scale('NF', chain(24000.0, 50), 24000.0, self.SESSION, self.a_dt,
                              self.SESSION, CAL)
        self.assertEqual(sc['status'], 'UNAVAILABLE')
        self.assertEqual(sc['reason'], 'EXPIRY_DAY_CHAIN_ONLY')
        d = direction_with(sc, self.SESSION, self.polls, 'NF', self.decision)
        self.assertEqual(d['value'], 'UNAVAILABLE')
        self.assertIn('SCALE_EXPIRY_DAY_CHAIN_ONLY', d['reason_codes'])

    def test_nov03_nf_outage_closed_with_next_expiry_chain(self):
        """After the repair: the separately supplied next-expiry chain yields a usable scale."""
        sel = ia.select_scale_chain('NF', self.SESSION, chain(24000.0, 50), self.SESSION,
                                    chain(24000.0, 50), '2026-11-09')
        sc = ia.scale_provenance(
            ia.compute_scale('NF', sel['chain'], 24000.0, self.SESSION, self.a_dt, sel['expiry'], CAL),
            sel, open_ref_observation=True)
        self.assertEqual(sc['status'], 'OK')
        self.assertIsNotNone(sc['value'])
        self.assertEqual(sc['chain_source'], ia.SCALE_SOURCE_NEXT)
        self.assertTrue(sc['open_ref_observation'])
        d = direction_with(sc, self.SESSION, self.polls, 'NF', self.decision)
        self.assertNotEqual(d['value'], 'UNAVAILABLE')
        self.assertIsNotNone(d['z_open'])
        # provenance must reach the published decision record
        self.assertEqual(d['inputs']['scale']['chain_source'], ia.SCALE_SOURCE_NEXT)
        self.assertTrue(d['inputs']['scale']['open_ref_observation'])
        self.assertEqual(d['inputs']['scale']['trade_expiry'], self.SESSION)

    def test_nov03_fail_closed_when_no_next_chain_available(self):
        """No usable next-expiry chain must still produce UNAVAILABLE, never a guessed scale."""
        sel = ia.select_scale_chain('NF', self.SESSION, chain(24000.0, 50), self.SESSION, None, None)
        sc = ia.scale_unavailable(sel, 'NF')
        self.assertEqual(sc['status'], 'UNAVAILABLE')
        self.assertIsNone(sc['value'])
        d = direction_with(sc, self.SESSION, self.polls, 'NF', self.decision)
        self.assertEqual(d['value'], 'UNAVAILABLE')


class TestNormalSessionUnchanged(unittest.TestCase):
    """A forward-dated front expiry must behave EXACTLY as before the repair, both indices."""
    SESSION = '2026-11-04'
    D = date(2026, 11, 4)

    def _scale_pair(self, index, spot, step, expiry, n):
        """`before` = the pre-repair path (trading chain, front expiry).  `after` = the repaired path.
        The decoy next-expiry chain is deliberately priced to a very different m, so that if the
        repair wrongly consulted it on a normal session the equality assertions below would fail
        rather than pass vacuously."""
        trade = chain(spot, step, m=0.0095, n=n, tag='TRADE')
        decoy = chain(spot, step, m=0.030, n=n, tag='DECOY_NEXT')
        before = ia.compute_scale(index, trade, spot, self.SESSION, at(self.D, 9, 20), expiry, CAL)
        sel = ia.select_scale_chain(index, self.SESSION, trade, expiry, decoy, '2026-12-29')
        after = ia.compute_scale(index, sel['chain'], spot, self.SESSION, at(self.D, 9, 20),
                                 sel['expiry'], CAL)
        decoy_m = ia.compute_scale(index, decoy, spot, self.SESSION, at(self.D, 9, 20), expiry, CAL)
        return before, after, sel, decoy_m

    def test_normal_session_nf_scale_identical(self):
        before, after, sel, decoy_m = self._scale_pair('NF', 24000.0, 50, '2026-11-09', 4.0)
        self.assertEqual(sel['source'], ia.SCALE_SOURCE_FRONT)
        self.assertEqual(before['status'], 'OK')
        self.assertEqual(before, after)   # byte-identical: the next-expiry chain is NOT consulted
        # the decoy really would have changed the answer, so the equality above is not vacuous
        self.assertEqual(decoy_m['status'], 'OK')
        self.assertNotAlmostEqual(before['value'], decoy_m['value'], places=4)

    def test_normal_session_bnf_scale_identical(self):
        before, after, sel, decoy_m = self._scale_pair('BNF', 56000.0, 100, '2026-11-23', 14.0)
        self.assertEqual(sel['source'], ia.SCALE_SOURCE_FRONT)
        self.assertEqual(before['status'], 'OK')
        self.assertEqual(before, after)
        self.assertEqual(decoy_m['status'], 'OK')
        self.assertNotAlmostEqual(before['value'], decoy_m['value'], places=4)

    def test_normal_session_direction_identical(self):
        path = [(9, 20, 56000.0), (9, 25, 56100.0), (9, 30, 56200.0), (9, 35, 56300.0),
                (9, 45, 56400.0), (10, 0, 56500.0)]
        polls = polls_for('BNF', self.D, path)
        before, after, _, _ = self._scale_pair('BNF', 56000.0, 100, '2026-11-23', 14.0)
        d0 = direction_with(before, self.SESSION, polls, 'BNF', at(self.D, 10, 0))
        d1 = direction_with(after, self.SESSION, polls, 'BNF', at(self.D, 10, 0))
        self.assertEqual(d0['value'], d1['value'])
        self.assertEqual(d0['z_open'], d1['z_open'])
        self.assertEqual(d0['z_30'], d1['z_30'])
        self.assertEqual(d0['er'], d1['er'])


# --------------------------------------------------------------------------------------------
# a FAILED scale already stored
# --------------------------------------------------------------------------------------------
class TestStoredFailure(unittest.TestCase):
    SESSION = '2026-11-03'
    D = date(2026, 11, 3)

    def sel_next(self):
        return ia.select_scale_chain('NF', self.SESSION, chain(24000.0, 50), self.SESSION,
                                     chain(24000.0, 50), '2026-11-09')

    def sel_none(self):
        return ia.select_scale_chain('NF', self.SESSION, chain(24000.0, 50), self.SESSION, None, None)

    def test_stored_failure_is_recoverable_only_with_a_usable_next_chain(self):
        stored = {'status': 'UNAVAILABLE', 'reason': 'EXPIRY_DAY_CHAIN_ONLY'}
        self.assertTrue(ia.scale_is_recoverable(stored, self.sel_next()))
        self.assertFalse(ia.scale_is_recoverable(stored, self.sel_none()))

    def test_stored_ok_scale_is_never_recomputed(self):
        """A good frozen scale must stay frozen - that is the whole point of the open reference."""
        ok = {'status': 'OK', 'value': 0.009, 'reason': None}
        self.assertFalse(ia.scale_is_recoverable(ok, self.sel_next()))

    def test_unrecoverable_reasons_are_not_retried(self):
        for reason in ('OPEN_REF_MISSING', 'SPOT_INVALID', 'SCALE_INVALID',
                       'NO_VALID_PARITY_PAIR', 'EXPIRY_INVALID'):
            stored = {'status': 'UNAVAILABLE', 'reason': reason}
            self.assertFalse(ia.scale_is_recoverable(stored, self.sel_next()), reason)

    def test_every_recoverable_reason_is_a_real_failure_reason(self):
        self.assertNotIn('OK', ia.SCALE_RECOVERABLE_REASONS)
        self.assertIn('EXPIRY_DAY_CHAIN_ONLY', ia.SCALE_RECOVERABLE_REASONS)

    def test_recovered_scale_is_stamped_as_a_late_observation(self):
        sel = self.sel_next()
        obs_dt, obs_spot = at(self.D, 11, 0), 24210.0
        sc = ia.scale_provenance(
            ia.compute_scale('NF', sel['chain'], obs_spot, self.SESSION, obs_dt, sel['expiry'], CAL),
            sel, open_ref_observation=False, recovered_from='EXPIRY_DAY_CHAIN_ONLY',
            observed_dt=obs_dt, spot=obs_spot)
        self.assertEqual(sc['status'], 'OK')
        self.assertTrue(sc['recovered'])
        self.assertFalse(sc['open_ref_observation'])
        self.assertEqual(sc['recovered_from_reason'], 'EXPIRY_DAY_CHAIN_ONLY')
        self.assertEqual(sc['recovered_at'], obs_dt.isoformat())
        self.assertEqual(sc['recovered_spot'], obs_spot)

    def test_recovered_flag_reaches_the_published_decision(self):
        """A consumer must be able to tell a late scale from an open-reference one."""
        sel = self.sel_next()
        obs_dt = at(self.D, 10, 0)
        sc = ia.scale_provenance(
            ia.compute_scale('NF', sel['chain'], 24210.0, self.SESSION, obs_dt, sel['expiry'], CAL),
            sel, open_ref_observation=False, recovered_from='EXPIRY_DAY_CHAIN_ONLY',
            observed_dt=obs_dt, spot=24210.0)
        polls = polls_for('NF', self.D, [(9, 20, 24000.0), (9, 30, 24080.0), (9, 45, 24170.0),
                                         (10, 0, 24210.0)])
        d = direction_with(sc, self.SESSION, polls, 'NF', obs_dt)
        self.assertNotEqual(d['value'], 'UNAVAILABLE')
        self.assertTrue(d['inputs']['scale']['recovered'])
        self.assertFalse(d['inputs']['scale']['open_ref_observation'])

    def test_front_expiry_scale_is_not_stamped_at_all(self):
        """decision_identity hashes `direction` wholesale, so any key added to the scale changes every
        decision_id - and decision_id carries admission lineage (entry_decision_id) and history record
        keys.  The unchanged front-expiry path must therefore come back byte-identical, unstamped."""
        trade = chain(24000.0, 50, m=0.0095, n=4.0)
        sel = ia.select_scale_chain('NF', '2026-11-04', trade, '2026-11-09',
                                    chain(24000.0, 50, m=0.03, n=4.0), '2026-11-17')
        self.assertEqual(sel['source'], ia.SCALE_SOURCE_FRONT)
        raw = ia.compute_scale('NF', sel['chain'], 24000.0, '2026-11-04', at(date(2026, 11, 4), 9, 20),
                               sel['expiry'], CAL)
        stamped = ia.scale_provenance(dict(raw), sel, open_ref_observation=True)
        self.assertEqual(stamped, raw)
        for k in ('chain_source', 'chain_source_version', 'open_ref_observation', 'trade_expiry',
                  'recovered'):
            self.assertNotIn(k, stamped, k)

    def test_front_expiry_direction_record_gains_no_new_keys(self):
        trade = chain(24000.0, 50, m=0.0095, n=4.0)
        sel = ia.select_scale_chain('NF', '2026-11-04', trade, '2026-11-09', None, None)
        sc = ia.scale_provenance(
            ia.compute_scale('NF', sel['chain'], 24000.0, '2026-11-04', at(date(2026, 11, 4), 9, 20),
                             sel['expiry'], CAL),
            sel, open_ref_observation=True)
        polls = polls_for('NF', date(2026, 11, 4), [(9, 20, 24000.0), (9, 30, 24080.0),
                                                    (9, 45, 24170.0), (10, 0, 24210.0)])
        d = ia.compute_direction('NF', polls, '2026-11-04', at(date(2026, 11, 4), 10, 0), sc,
                                 receipt_dt=at(date(2026, 11, 4), 10, 0))
        self.assertEqual(sorted(d['inputs']['scale'].keys()),
                         sorted(['value', 'expiry', 'k0', 'ce_mid', 'pe_mid', 'forward',
                                 'n_sessions', 'observed_at', 'method']))

    def test_open_ref_scale_does_not_carry_recovery_keys(self):
        sel = self.sel_next()
        sc = ia.scale_provenance(
            ia.compute_scale('NF', sel['chain'], 24000.0, self.SESSION, at(self.D, 9, 20),
                             sel['expiry'], CAL),
            sel, open_ref_observation=True)
        self.assertNotIn('recovered', sc)
        self.assertTrue(sc['open_ref_observation'])


class TestRecoveryDoesNotRewriteHistory(unittest.TestCase):
    """Recovery must apply to future polls only."""
    SESSION = '2026-11-03'
    D = date(2026, 11, 3)

    def test_recovery_does_not_rewrite_earlier_decisions(self):
        polls_early = polls_for('NF', self.D, [(9, 20, 24000.0), (9, 30, 24080.0)])
        failed = {'status': 'UNAVAILABLE', 'reason': 'EXPIRY_DAY_CHAIN_ONLY'}
        early = direction_with(failed, self.SESSION, polls_early, 'NF', at(self.D, 9, 30))
        early_snapshot = dict(early)
        self.assertEqual(early['value'], 'UNAVAILABLE')

        # a later poll recovers the scale
        sel = ia.select_scale_chain('NF', self.SESSION, chain(24000.0, 50), self.SESSION,
                                    chain(24000.0, 50), '2026-11-09')
        obs_dt = at(self.D, 11, 0)
        recovered = ia.scale_provenance(
            ia.compute_scale('NF', sel['chain'], 24210.0, self.SESSION, obs_dt, sel['expiry'], CAL),
            sel, open_ref_observation=False, recovered_from='EXPIRY_DAY_CHAIN_ONLY',
            observed_dt=obs_dt, spot=24210.0)
        polls_late = polls_for('NF', self.D, [(9, 20, 24000.0), (9, 30, 24080.0), (9, 45, 24170.0),
                                              (10, 30, 24200.0), (11, 0, 24210.0)])
        late = direction_with(recovered, self.SESSION, polls_late, 'NF', obs_dt)

        # the later poll now has a direction ...
        self.assertNotEqual(late['value'], 'UNAVAILABLE')
        # ... and recomputing the EARLIER poll with the EARLIER scale is untouched by any of it
        again = direction_with(failed, self.SESSION, polls_early, 'NF', at(self.D, 9, 30))
        self.assertEqual(again['value'], early_snapshot['value'])
        self.assertEqual(again['reason_codes'], early_snapshot['reason_codes'])
        self.assertIsNone(again['z_open'])

    def test_recovery_never_backdates_its_observation_time(self):
        """The recovered observation must be stamped at the time it was actually taken, so it can
        never be read as having been available earlier than it was."""
        sel = ia.select_scale_chain('NF', self.SESSION, chain(24000.0, 50), self.SESSION,
                                    chain(24000.0, 50), '2026-11-09')
        obs_dt = at(self.D, 13, 15)
        sc = ia.scale_provenance(
            ia.compute_scale('NF', sel['chain'], 24210.0, self.SESSION, obs_dt, sel['expiry'], CAL),
            sel, open_ref_observation=False, recovered_from='EXPIRY_DAY_CHAIN_ONLY',
            observed_dt=obs_dt, spot=24210.0)
        self.assertEqual(sc['observed_at'], obs_dt.isoformat())
        self.assertEqual(sc['recovered_at'], obs_dt.isoformat())
        self.assertGreater(sc['recovered_at'], at(self.D, 9, 35).isoformat())


if __name__ == '__main__':
    unittest.main(verbosity=2)
