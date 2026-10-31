"""R7 (Codex R6 s6): polling-independent Daily review trigger ledger: eligibility, PENDING/ATTEMPTED/COMPLETED,
retry after a missed evening / failure / restart, bounded runs, teacher-independent.  Deterministic clocks."""
import json
import os
import shutil
import tempfile
import unittest

import brain
import daily_review_ledger_v1 as drl
import index_authority_v1 as ia


def T(s):
    return ia.parse_ts(s)


class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sd = os.path.join(self.tmp, 'index_authority_v1')
        self.rd = os.path.join(self.tmp, 'self_study_v1')
        os.makedirs(self.sd)
        for sid in ('2026-10-28', '2026-10-29', '2026-10-30'):
            open(os.path.join(self.sd, sid + '.json'), 'w').write('{}')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_eligibility_due_time_and_backfill_of_missed_evenings(self):
        self.assertEqual(drl.eligible_sessions(self.sd, T('2026-10-30T15:39:00+05:30')), ['2026-10-28', '2026-10-29'])
        self.assertEqual(drl.eligible_sessions(self.sd, T('2026-10-30T15:40:00+05:30')),
                         ['2026-10-28', '2026-10-29', '2026-10-30'])

    def test_attempted_completed_and_retry_after_failure_or_restart(self):
        calls = []
        res = iter(['WRITTEN', 'STORE_UNAVAILABLE', 'WRITTEN'])

        def run(sid):
            calls.append(sid)
            return next(res)
        out = drl.catch_up(self.sd, self.rd, T('2026-10-30T16:00:00+05:30'), run, trigger='evening_alarm')
        self.assertEqual(out['pending'], ['2026-10-29'])
        led = drl.read(self.rd)['sessions']
        self.assertEqual((led['2026-10-28']['state'], led['2026-10-29']['state'], led['2026-10-30']['state']),
                         ('COMPLETED', 'ATTEMPTED', 'COMPLETED'))
        # "restart" next morning: only the failed session is retried, completed ones are not re-run
        calls.clear()
        out = drl.catch_up(self.sd, self.rd, T('2026-10-31T08:00:00+05:30'), lambda s: calls.append(s) or 'WRITTEN',
                           trigger='app_start')
        self.assertEqual(calls, ['2026-10-29'])
        self.assertEqual(out['pending'], [])
        self.assertEqual(drl.read(self.rd)['sessions']['2026-10-29']['attempts'], 2)

    def test_crash_mid_run_leaves_attempted_and_is_retried(self):
        def boom(sid):
            raise RuntimeError('x')
        out = drl.catch_up(self.sd, self.rd, T('2026-10-30T10:00:00+05:30'), boom, max_runs=1)
        self.assertEqual(out['ran'][0]['status'], 'ERROR:RuntimeError')
        self.assertEqual(drl.read(self.rd)['sessions']['2026-10-28']['state'], 'ATTEMPTED')

    def test_bounded_runs_and_attempts(self):
        n = []
        drl.catch_up(self.sd, self.rd, T('2026-10-30T16:00:00+05:30'), lambda s: n.append(s) or 'BUDGET_EXCEEDED', max_runs=2)
        self.assertEqual(len(n), 2)
        for _ in range(20):
            drl.catch_up(self.sd, self.rd, T('2026-10-30T16:00:00+05:30'), lambda s: 'BUDGET_EXCEEDED')
        led = drl.read(self.rd)['sessions']
        self.assertTrue(all(v['attempts'] <= drl.MAX_ATTEMPTS for v in led.values()))

    def test_corrupt_ledger_fails_closed(self):
        os.makedirs(self.rd, exist_ok=True)
        open(os.path.join(self.rd, drl.LEDGER), 'w').write('{bad')
        self.assertEqual(drl.catch_up(self.sd, self.rd, T('2026-10-30T16:00:00+05:30'), lambda s: 'WRITTEN')['status'],
                         'LEDGER_CORRUPT')

    def test_bridge_runs_review_without_teacher(self):
        out = json.loads(brain.daily_review_catchup(self.sd, self.rd, int(T('2026-10-30T16:00:00+05:30').timestamp() * 1000),
                                                    5.0, None, 'test'))
        self.assertEqual(out['status'], 'OK')
        self.assertEqual(len(out['ran']), 3)
        self.assertTrue(all(r['status'] for r in out['ran']))


if __name__ == '__main__':
    unittest.main()
