"""Codex b510 R1 review: reproductions of findings R1-R4 (red at 0ce27b7, green after the fixes).

The Codex ZIP (reproduce_review_findings.py) was not available; these are equivalent probes
written from the review text, using only synthetic inputs and temporary journals.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(HERE, '..', '..', '..', '..', '..', 'tools'))
sys.path.insert(0, HERE)

import paper_buying_research as pbr  # noqa: E402
import x1_report  # noqa: E402
from test_b510_paper_buying_x1 import EXP, FRI, MON, Harness, ms  # noqa: E402

UNDERLYING = {'NF': 'NSE_INDEX|Nifty 50', 'BNF': 'NSE_INDEX|Nifty Bank'}


def documented_chain(ix, F, expiry=EXP, n=6, tv=100.0, spread=1.0):
    """What the native formatter emits for a DOCUMENTED /v2/option/chain response: no trading_symbol."""
    step = 50 if ix == 'NF' else 100
    base = round(F / step) * step
    strikes = {}
    for i in range(-n, n + 1):
        K = base + i * step
        row = {}
        for side, mid in (('CE', max(F - K, 0) + tv), ('PE', max(K - F, 0) + tv)):
            key = 'NSE_FO|%d%s%d' % (40000 + K // step * 2, side, 1 if ix == 'NF' else 2)   # opaque token
            row[side] = {'bid': mid - spread / 2, 'ask': mid + spread / 2, 'ltp': mid,
                         'instrument_key': key, 'instrumentKey': key, 'trading_symbol': '', 'symbol': '',
                         'underlying_key': UNDERLYING[ix], 'contract_expiry': expiry}
        strikes[str(int(K))] = row
    return {'expiry': expiry, 'requested_expiry': expiry, 'requested_underlying_key': UNDERLYING[ix],
            'strikes': strikes}


def journal_with(nf_controls, corrupt=False):
    st = pbr.new_state(ms(FRI, '15:20:30'), '2.6.79')
    recs = []
    d = datetime(2026, 6, 1)
    i = 0
    while i < 60:
        if d.weekday() < 5:
            s = d.date().isoformat()
            pid = '%s:NF:primary' % s
            recs.append({'id': pid, 'session_date': s, 'index': 'NF', 'arm': 'primary', 'status': 'RESOLVED',
                         'net': 500.0 + (i % 5) * 10, 'side': 'CE', 'expiry': EXP})
            if i < nf_controls:
                recs.append({'id': '%s:NF:delayed' % s, 'primary_id': pid, 'session_date': s, 'index': 'NF',
                             'arm': 'delayed', 'status': 'RESOLVED', 'net': 300.0, 'side': 'CE', 'expiry': EXP})
            i += 1
        d += timedelta(days=1)
    st['records'] = recs
    if corrupt:
        st['protocol_digest'] = '0' * 64
        st['schema'] = 'x1_journal_v0'
        st['stopped'] = {'reason': 'JOURNAL_CORRUPT', 'at_ms': 1}
    return st


class ReviewRepro(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_R1_documented_feed_without_trading_symbol_captures_reference(self):
        self.h.run(self.h.ctx(FRI, '15:20:30', documented_chain('NF', 24500), documented_chain('BNF', 55000)))
        refs = self.h.state()['references']
        self.assertEqual(sorted(refs), ['BNF', 'NF'], 'no reference captured from the documented feed')

    @unittest.expectedFailure     # red until the R2 fix commit
    def test_R2_integrity_failure_never_reports_criteria_met(self):
        st = journal_with(60, corrupt=True)
        rep = x1_report.report(st, {'journal_sha256_ok': False})
        self.assertNotEqual(rep['indices']['NF']['verdict'], 'CRITERIA_MET_REVIEW_REQUIRED')
        self.assertTrue(rep.get('blocked'))

    @unittest.expectedFailure     # red until the R3 fix commit
    def test_R3_one_control_of_sixty_cannot_pass(self):
        rep = x1_report.report(journal_with(1))
        self.assertNotEqual(rep['indices']['NF']['verdict'], 'CRITERIA_MET_REVIEW_REQUIRED')

    @unittest.expectedFailure     # red until the R4 fix commit
    def test_R4_hold_is_measured_from_entry_decision(self):
        self.h.run(self.h.ctx(FRI, '15:20:30', 24500, 55000))
        # quote received 09:20:00, entry decision 09:21:00, exit quote/decision 09:50:00 = 29 min hold
        self.h.run(self.h.ctx(MON, '09:21:00', 24560, 54880, q_lag_s=60))
        self.h.run(self.h.ctx(MON, '09:50:00', 24650, 54800, q_lag_s=0))
        self.assertEqual(self.h.row(index='NF', arm='primary')['status'], 'OPEN')


if __name__ == '__main__':
    unittest.main()
