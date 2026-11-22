"""Regression coverage for the opt-in managed STRONG Paper pilot."""
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import index_single_leg_v1 as sl
import single_leg_store_v1 as store
import test_index_single_leg_v1_delivery_b as fixtures
from test_single_leg_store_e2e_x1_identity import OK, admissible_result


def signal(value='BULL', strength='STRONG', er=0.5):
    return {'value': value, 'strength': strength, 'er': er}


class StrongPaperPilot(unittest.TestCase):
    def test_symmetric_entry_and_wait_conditions(self):
        for direction, action in (('BULL', 'BUY_CE'), ('BEAR', 'BUY_PE')):
            with self.subTest(direction=direction):
                plan = fixtures.plan(decision={'direction': signal(direction)}, strong_pilot=True)
                self.assertTrue(plan['admit'], plan['reasons'])
                self.assertEqual(plan['action'], action)
        for direction, reason in (
            (signal(strength='MODERATE'), 'PAPER_PILOT_WAIT_FOR_STRONG'),
            (signal('BEAR', er=.2), 'PAPER_PILOT_BUY_ONLY'),
        ):
            plan = fixtures.plan(decision={'direction': direction}, strong_pilot=True)
            self.assertEqual(plan['reasons'], [reason])
            self.assertFalse(plan['admit'])
            self.assertFalse(plan['quote_needed'])

    def test_hold_uses_session_feasibility_and_exact_seconds(self):
        now = fixtures.D.replace(hour=9, minute=20, second=23)
        self.assertEqual(sl.pilot_deadline(now), now + timedelta(minutes=60))
        self.assertIsNone(sl.pilot_deadline(now.replace(hour=14, minute=30)))
        self.assertIsNone(sl.pilot_deadline(now.replace(day=7)))
        self.assertIsNone(sl.pilot_deadline(now.replace(year=2027)))

    def test_bearish_phone_lifecycle_retry_and_cross_index_cap(self):
        d0 = fixtures.D
        chain = fixtures.chain()
        result = admissible_result(decision={'direction': signal('BEAR')}, strong_pilot=True)
        result['by_index']['NF']['direction'] = signal('BEAR')
        plan = result['single_leg_by_index']['NF']
        with TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'single_leg_v1')
            old = store.admit(path, admissible_result(), 'NF', clock=lambda: d0,
                              require_strong_pilot=True, **OK)
            self.assertEqual(old['reason'], 'PAPER_PILOT_CURRENT_PROPOSAL_REQUIRED')
            out = store.admit(path, result, 'NF', clock=lambda: d0, request_id='test-put',
                              require_strong_pilot=True, **OK)
            self.assertEqual(out['status'], 'ADMITTED', out)
            deadline = d0 + timedelta(minutes=60)
            pos = store.read(path)['positions'][0]
            self.assertEqual(pos['holding_plan']['exit_deadline'], deadline.isoformat())
            self.assertTrue(store.admit(path, result, 'NF', clock=lambda: d0, request_id='test-put',
                                        require_strong_pilot=True, **OK)['replayed'])
            prior = deadline - timedelta(seconds=1)
            store.monitor(path, fixtures.snapshots(chain, gen=prior - timedelta(seconds=1)), {}, clock=lambda: prior)
            self.assertEqual(store.read(path)['positions'][0]['status'], 'OPEN')
            pending = store.monitor(path, {}, {}, clock=lambda: deadline)
            self.assertEqual(pending['events'][0]['event'], 'EXIT_PENDING')
            recovered = deadline + timedelta(seconds=10)
            closed = store.monitor(path, fixtures.snapshots(chain, gen=recovered - timedelta(seconds=1)),
                                   {}, clock=lambda: recovered)
            self.assertEqual(closed['events'][0]['event'], 'EXIT')
            self.assertEqual(store.monitor(path, {}, {}, clock=lambda: recovered)['events'], [])
            outcome = store.outcomes(path, fixtures.F.SESSION)[0]
            self.assertEqual((outcome['action'], outcome['policy_version'], outcome['signal_strength']),
                             ('BUY_PE', sl.STRONG_PILOT, 'STRONG'))
            self.assertIsNotNone(outcome['net_pnl'])
            existing = store.read(path)['history']
            self.assertIn('PAPER_PILOT_SESSION_CAP',
                          sl._pilot_entry_reasons(fixtures.F.SESSION, recovered, existing))
            other = dict(plan, index='BNF')
            _, reasons = sl.revalidate_for_admission(other, result['by_index']['NF'], recovered,
                                                       history=existing, **OK)
            self.assertIn('PAPER_PILOT_SESSION_CAP', reasons)

    def test_revalidation_rejects_a_signal_that_is_no_longer_strong(self):
        plan = fixtures.plan(decision={'direction': signal()}, strong_pilot=True)
        accepted, reasons = sl.revalidate_for_admission(plan, {'direction': signal(strength='MODERATE')},
                                                         fixtures.D, **OK)
        self.assertIsNone(accepted)
        self.assertIn('PAPER_PILOT_WAIT_FOR_STRONG', reasons)
