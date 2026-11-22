from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import index_authority_v1 as ia
import single_leg_store_v1 as store
import strong_hold_observer_v1 as observer
from test_index_single_leg_v1_delivery_b import chain, snapshots, D
from test_single_leg_store_e2e_x1_identity import OK, admissible_result


class HoldObserver(unittest.TestCase):
    def test_vix_uses_only_current_session_and_pre_cutoff_values(self):
        polls = [{'t': clock, 'vix': value} for clock, value in
                 [('09:29', 15.4), ('10:00', 15.6), ('10:05', 21.0)]]
        result = observer.vix_context(polls, D.date().isoformat(), D)
        self.assertEqual(result['change_30m'], .2)

    def test_durable_research_follows_closed_position_without_reopening(self):
        with TemporaryDirectory() as root:
            path = str(Path(root) / 'single_leg_v1')
            direction = {'value': 'BULL', 'strength': 'STRONG', 'er': .6}
            res = admissible_result(decision={'direction': direction}, strong_pilot=True)
            res['by_index']['NF']['direction'] = direction
            res['single_leg_by_index']['NF']['research_vix'] = {'change_30m': -.12}
            out = store.admit(path, res, 'NF', clock=lambda: D, **OK)
            self.assertEqual(out['status'], 'ADMITTED')
            row = store.read(path)['hold_observations'][0]
            self.assertEqual(row['research_vix']['change_30m'], -.12)
            key = row['instrument_key']

            def monitor(ts, valid=True):
                q = snapshots(chain(), gen=ts-timedelta(seconds=1)) if valid else {}
                store.monitor(path, q, {}, clock=lambda: ts)

            exit_time = D + timedelta(minutes=60, seconds=2)
            monitor(exit_time)
            self.assertEqual(store.open_positions(path), [])
            early = ia.parse_ts('2026-11-03T09:15:30+05:30')
            self.assertEqual(store.hold_observation_targets(path, clock=lambda: early), [key])
            monitor(early)
            later = ia.parse_ts('2026-11-03T10:15:30+05:30')
            monitor(later)
            first = store.read(path)['hold_observations'][0]
            monitor(later + timedelta(seconds=30))
            self.assertEqual(first, store.read(path)['hold_observations'][0])
            self.assertEqual([first['marks'][k]['status'] for k in ('same_60','near_close','next_open','next_60')],
                             ['SCORED','MISSING_QUOTE','SCORED','SCORED'])
            self.assertEqual(len(store.read(path)['closed']), 1)
            self.assertEqual(store.open_positions(path), [])

    def test_skip_next_session_on_expiry_and_exclude_quote_before_window(self):
        pos = dict(entry_ts=D.isoformat(), holding_plan={'exit_deadline': (D+timedelta(hours=1)).isoformat()},
                   expiry='2026-11-03', position_id='p', entry_decision_id='d', index='NF', instrument_key='x',
                   action='BUY_CE', strike=24_000, entry_fill=150, entry_fee=30, tick_size=.05, units=65)
        row = observer.new_observation(pos, None)
        self.assertEqual(row['next_session_refusal'], 'NO_PRE_EXPIRY_NEXT_SESSION')
        self.assertNotIn('next_open', row['marks'])
        self.assertFalse(observer.advance([row], {}, D+timedelta(hours=1, seconds=1)))
        self.assertEqual(row['marks']['same_60']['status'], 'PENDING')
