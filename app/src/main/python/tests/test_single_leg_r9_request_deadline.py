"""Codex R8 review: request/deadline correction - Codex's probe and regressions 1-5."""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from datetime import timedelta

import brain
import single_leg_store_v1 as sls
import test_index_single_leg_v1_delivery_b as TB
from test_single_leg_store_e2e_x1_identity import admissible_result, OK
from test_single_leg_r8_capital_state_bound import D, probe
from test_single_leg_r7_admission_monitor_snapshot import fresh_snap

DL = int((D + timedelta(seconds=15)).timestamp() * 1000)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = os.path.join(self.tmp, 'single_leg_v1')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class CodexProbeAndRegressions(Tmp):
    def test_codex_probe_stalled_state_probe(self):
        result = admissible_result()
        current = [D + timedelta(seconds=5)]

        def stalled_probe():
            current[0] = D + timedelta(seconds=30)
            return probe(result)()
        out = sls.admit(self.d, result, 'NF', clock=lambda: current[0], state_probe=stalled_probe,
                        config={'revision': 3}, request_id='deadline-probe', deadline_ms=DL, **OK)
        self.assertEqual(out['status'], 'REJECTED', out)
        self.assertEqual(out['reason'], 'ADMISSION_EXPIRED_BEFORE_COMMIT')
        self.assertEqual(out['checked_at'], (D + timedelta(seconds=30)).isoformat())
        self.assertEqual(sls.read(self.d)['positions'], [])
        self.assertEqual(sls.request_status(self.d, 'deadline-probe')['status'], 'REJECTED')

    def test_1_probe_completes_after_deadline_no_position(self):
        r = admissible_result()
        t = [D + timedelta(seconds=5)]

        def slow():
            t[0] = D + timedelta(seconds=16)
            return probe(r)()
        out = sls.admit(self.d, r, 'NF', clock=lambda: t[0], state_probe=slow, config={'revision': 3},
                        request_id='r1', deadline_ms=DL, **OK)
        self.assertEqual(out['reasons'], ['DEADLINE_PASSED_DURING_STATE_PROBE'])
        self.assertEqual(sls.read(self.d)['positions'], [])

    def test_2a_quote_stale_during_probe(self):
        r = admissible_result()                          # entry snapshot at D-20 s; 90 s max age
        t = [D + timedelta(seconds=5)]

        def slow():
            t[0] = D + timedelta(seconds=75)             # snapshot now 95 s old; no deadline given
            return probe(r)()
        out = sls.admit(self.d, r, 'NF', clock=lambda: t[0], state_probe=slow, config={'revision': 3}, **OK)
        self.assertIn('SNAPSHOT_STALE_AT_ADMISSION', out['reasons'])
        self.assertEqual(sls.read(self.d)['positions'], [])

    def test_2b_entry_cutoff_passes_during_probe(self):
        d1429 = D.replace(hour=14, minute=29, second=50)
        p = TB.plan(decision_dt=d1429, evidence=TB.evidence(TB.chain(), snap_gen=d1429 - timedelta(seconds=10)))
        r = admissible_result()
        r['single_leg_by_index']['NF'] = dict(p, decision_id='idx1_a')
        r['by_index']['NF']['analysis_ts'] = d1429.isoformat()
        t = [d1429 + timedelta(seconds=5)]

        def slow():
            t[0] = d1429 + timedelta(seconds=75)        # 14:31:05 (entry window ends 14:30 inclusive)
            return probe(r)()
        out = sls.admit(self.d, r, 'NF', clock=lambda: t[0], state_probe=slow, config={'revision': 3}, **OK)
        self.assertIn('ENTRY_WINDOW_CLOSED_AT_ADMISSION', out.get('reasons') or [out])

    def test_2c_clock_moves_during_revalidation(self):
        r = admissible_result()
        seq = iter([D + timedelta(seconds=5), D + timedelta(seconds=75), D + timedelta(seconds=75),
                    D + timedelta(seconds=75), D + timedelta(seconds=75)])
        out = sls.admit(self.d, r, 'NF', clock=lambda: next(seq), **OK)   # revalidated again at +75 s
        self.assertIn('SNAPSHOT_STALE_AT_ADMISSION', out['reasons'])
        seq2 = iter([D + timedelta(seconds=5), D + timedelta(seconds=16)])
        out = sls.admit(self.d, r, 'NF', clock=lambda: next(seq2), request_id='r2c', deadline_ms=DL, **OK)
        self.assertEqual(out['reasons'], ['DEADLINE_PASSED_DURING_REVALIDATION'])
        self.assertEqual(sls.read(self.d)['positions'], [])

    def test_3_commit_and_reconcile_interleave(self):
        """cancel_request is serialized with admit: never a terminal NOT_ADMITTED followed by a saved position."""
        r = admissible_result()
        entered, release = threading.Event(), threading.Event()

        def blocking():
            entered.set()
            release.wait(5)
            return probe(r)()
        res = {}
        th = threading.Thread(target=lambda: res.setdefault('a', sls.admit(
            self.d, r, 'NF', clock=lambda: D + timedelta(seconds=5), state_probe=blocking, config={'revision': 3},
            request_id='r3', **OK)))
        th.start()
        self.assertTrue(entered.wait(5))
        busy = sls.cancel_request(self.d, 'r3', lock_timeout_s=0.2)      # admission in flight holds the lock
        self.assertEqual(busy['status'], 'OUTCOME_UNCONFIRMED')
        self.assertFalse(busy['found'])
        release.set()
        th.join(5)
        self.assertEqual(res['a']['status'], 'ADMITTED')
        after = sls.cancel_request(self.d, 'r3')                       # definitive: the recorded outcome
        self.assertEqual((after['found'], after['status']), (True, 'ADMITTED'))
        self.assertEqual(len(sls.read(self.d)['positions']), 1)

    def test_3b_fence_blocks_later_admission(self):
        r = admissible_result()
        f = sls.cancel_request(self.d, 'r3b', clock=lambda: D)
        self.assertEqual((f['status'], f['reason'], f['fence']), ('REJECTED', 'CANCELLED_BEFORE_COMMIT', True))
        late = sls.admit(self.d, r, 'NF', clock=lambda: D + timedelta(seconds=5), request_id='r3b', **OK)
        self.assertTrue(late['replayed'])
        self.assertEqual(late['reason'], 'CANCELLED_BEFORE_COMMIT')
        self.assertEqual(sls.read(self.d)['positions'], [])

    def test_4_lost_response_reconciles_to_admitted(self):
        r = admissible_result()
        sls.admit(self.d, r, 'NF', clock=lambda: D + timedelta(seconds=5), request_id='r4', **OK)  # response dropped
        self.assertEqual(json.loads(brain.single_leg_request_status(self.d, 'r4'))['status'], 'ADMITTED')
        self.assertEqual(json.loads(brain.single_leg_request_cancel(self.d, 'r4'))['status'], 'ADMITTED')
        self.assertEqual(len(sls.read(self.d)['positions']), 1)

    def test_5_restart_during_processing(self):
        """A process dies holding nothing durable: after restart the id has no record (not a negative outcome);
        a retry with the same id is idempotent; a completed id never duplicates."""
        r = admissible_result()
        self.assertEqual(sls.request_status(self.d, 'r5')['status'], 'NOT_RECORDED')     # not "NOT_ADMITTED"
        a = sls.admit(self.d, r, 'NF', clock=lambda: D + timedelta(seconds=5), request_id='r5', **OK)
        b = sls.admit(self.d, r, 'NF', clock=lambda: D + timedelta(seconds=6), request_id='r5', **OK)  # retry after restart
        c = sls.admit(self.d, r, 'NF', clock=lambda: D + timedelta(seconds=7), request_id='r5b', **OK)  # new tap, same plan
        self.assertEqual((a['status'], b.get('replayed'), c['status']), ('ADMITTED', True, 'DUPLICATE_IGNORED'))
        self.assertEqual(len(sls.read(self.d)['positions']), 1)

    def test_probe_errors_named_fail_closed(self):
        r = admissible_result()

        def boom():
            raise ValueError('x')
        out = sls.admit(self.d, r, 'NF', clock=lambda: D + timedelta(seconds=5), state_probe=boom, **OK)
        self.assertEqual((out['reason'], out['reasons']), ('NATIVE_STATE_UNAVAILABLE', ['NATIVE_STATE_UNAVAILABLE:ValueError']))
        out = sls.admit(self.d, r, 'NF', clock=lambda: D + timedelta(seconds=5), state_probe=lambda: '[]', **OK)
        self.assertEqual(out['reason'], 'NATIVE_STATE_INVALID')          # not a dict -> named
        out = sls.admit(self.d, r, 'NF', clock=lambda: D + timedelta(seconds=5), state_probe=lambda: '{}', **OK)
        self.assertEqual(out['reason'], 'NATIVE_STATE_INVALID')
        self.assertEqual(sls.read(self.d)['positions'], [])

    def test_request_retention_bounded_with_fences(self):
        for i in range(sls.MAX_REQUESTS + 30):
            sls.cancel_request(self.d, f'x{i}', clock=lambda i=i: D + timedelta(seconds=i))
        self.assertEqual(len(sls.read(self.d)['requests']), sls.MAX_REQUESTS)


if __name__ == '__main__':
    unittest.main()
