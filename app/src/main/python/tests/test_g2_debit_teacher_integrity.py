"""G2: H2 teacher integrity for index_construction_v1 DEBIT structures (BULL_CALL / BEAR_PUT).

Claude's probe_debit_teacher_integrity.py was NOT available to this change; this is an equivalent replay built
from snapshot-shaped fixtures produced by the real projection (brain.analyze -> take_poll_snapshot
android_compact_v1, tools/g1_authority_chain) and graded through the production evaluation job API.
The uploader half (teacher P&L kept on evaluation + recommendation rows) is G2DebitTeacherUploaderTest.
"""
import copy
import json
import math
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..', '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'tools', 'g1_authority_chain'))

import brain  # noqa: E402
import chain as G  # noqa: E402
import test_evaluation_chain_quote_contract as T  # noqa: E402

DEBIT = ('BULL_CALL', 'BEAR_PUT')
CREDIT = ('BULL_PUT', 'BEAR_CALL', 'IRON_CONDOR', 'IRON_BUTTERFLY')


def _ctxj(snap):
    c = snap['context_json']
    return json.loads(c) if isinstance(c, str) else c


def _grade(snap, rows):
    d = tempfile.mkdtemp(prefix='g2_')
    sp, cp = os.path.join(d, 's.json'), os.path.join(d, 'c.json')
    with open(sp, 'w') as f:
        json.dump([snap], f)
    with open(cp, 'w') as f:
        json.dump(rows, f)
    brain.evaluation_job_prepare('g2', sp, cp)
    res = json.loads(brain.evaluation_job_run_batch('g2', 0, 10))
    brain.evaluation_job_finalize('g2')
    return res


class G2DebitTeacher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graded = {}
        cls.cands = {}
        for name, polls in (('bull', G._std_polls()), ('bear', G._bear_polls())):
            r, snap, c, _ = G._run(name, polls)
            snap['id'] = 'g2-' + name
            res = _grade(snap, G._later_chain_rows(snap, c))
            assert res['fatal_snapshot_error_count'] == 0, res['errors']
            for o in res['outcomes']:
                if str(o.get('candidate_id', '')).endswith('_index_construction_v1'):
                    cls.graded[o['candidate_id']] = o
            for x in _ctxj(snap)['snapshot_generated_candidates']:
                cls.cands[x['id']] = x

    def test_debit_structures_present_with_signed_negative_premium(self):
        types = {c['type'] for c in self.cands.values()}
        self.assertTrue(set(DEBIT) <= types, types)
        for c in self.cands.values():
            if c['type'] in DEBIT:
                self.assertIs(c['isCredit'], False)
                self.assertLess(c['netPremium'], 0)          # signed premium retained in the snapshot
                self.assertEqual(c['constructionVersion'], 'index_construction_v1')

    def test_debit_h2_integrity_ok_and_finite(self):
        n = 0
        for cid, o in self.graded.items():
            c = self.cands[cid]
            if c['type'] not in DEBIT:
                continue
            n += 1
            self.assertEqual(o['price_integrity'], 'OK', (cid, o.get('h2_price_integrity_reason')))
            self.assertTrue(math.isfinite(o['sim_pnl_h2']))
            self.assertAlmostEqual(o['h2_entry_basis_points'], abs(c['netPremium']), places=4)
            self.assertEqual(o['h2_entry_basis_source'], brain.IA_DEBIT_COST_BASIS)
            self.assertEqual(o['h2_formula'], '(close_value - entry_debit) * lot')
            for k in ('managed_pnl', 'r_multiple'):
                self.assertTrue(o.get(k) is not None and math.isfinite(o[k]), (cid, k))
            self.assertEqual(o['policy_selector_version'], 'index_managed_v1')
        self.assertEqual(n, 3)  # NF BULL_CALL (BNF is NEUTRAL on the bull path), NF+BNF BEAR_PUT

    def test_credit_ic_ib_unchanged(self):
        n = 0
        for cid, o in self.graded.items():
            c = self.cands[cid]
            if c['type'] not in CREDIT:
                continue
            n += 1
            self.assertEqual(o['price_integrity'], 'OK')
            self.assertNotIn('h2_entry_basis_source', o)
            self.assertAlmostEqual(o['h2_entry_basis_points'], c['netPremium'], places=4)
            self.assertEqual(o['h2_formula'], '(entry_credit - close_cost) * lot')
            self.assertIsNotNone(o.get('managed_pnl'))
        self.assertEqual(n, 5)  # NF BULL_PUT, BNF IC + IB, NF+BNF BEAR_CALL

    def test_entry_premium_rule_is_scoped(self):
        base = {'netPremium': -13.93, 'isCredit': False, 'constructionVersion': 'index_construction_v1'}
        self.assertEqual(brain._candidate_entry_premium(base), 13.93)
        # no construction version (legacy) -> never abs()'d
        self.assertEqual(brain._candidate_entry_premium(dict(base, constructionVersion=None)), -13.93)
        # credit / unknown credit flag -> unchanged
        self.assertEqual(brain._candidate_entry_premium(dict(base, isCredit=True)), -13.93)
        self.assertEqual(brain._candidate_entry_premium({k: v for k, v in base.items() if k != 'isCredit'}), -13.93)
        # positive premiums untouched everywhere
        self.assertEqual(brain._candidate_entry_premium(dict(base, netPremium=5.27, isCredit=True)), 5.27)

    def test_legacy_fixture_unchanged(self):
        c = T.candidate()                       # real 2026-08-31 legacy BEAR_PUT (positive netPremium 35.6)
        rows = T.chain_rows(True) + [           # + an H2 end-of-day mark (15:20 IST)
            {'index_key': 'NF', 'strike': k, 'option_type': 'PE', 'expiry': '2026-09-01',
             'poll_ts': '2026-08-31T09:50:00Z', 'ltp': ltp, 'bid': ltp - 0.1, 'ask': ltp + 0.1,
             'session_date': '2026-08-31'} for k, ltp in ((23950, 33.0), (24050, 70.0))]
        h2 = brain._legacy_h2_structure_valuation(rows, c)
        self.assertEqual(h2['price_integrity'], 'OK')
        self.assertEqual(h2['h2_entry_basis_points'], 35.6)
        self.assertEqual(h2['sim_pnl_h2'], round((70.0 - 33.0 - 35.6) * 65, 2))
        self.assertNotIn('h2_entry_basis_source', h2)
        # a legacy debit with a negative premium still fails closed (no global abs)
        bad = dict(copy.deepcopy(c), netPremium=-35.6)
        self.assertEqual(brain._legacy_h2_structure_valuation(rows, bad)['h2_price_integrity_reason'],
                         'MISSING_ENTRY_PREMIUM_OR_INVALID_LOT')

    def test_candidate_economics_not_mutated(self):
        for c in self.cands.values():
            if c['type'] in DEBIT:
                before = copy.deepcopy(c)
                brain._legacy_h2_structure_valuation([], c)
                self.assertEqual(c, before)


if __name__ == '__main__':
    unittest.main()
