"""OC utility reproducibility tests. Need numpy+scipy (skipped where absent, e.g. stdlib-only CI)."""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
try:
    import numpy  # noqa: F401
    import scipy  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False


@unittest.skipUnless(HAVE, 'numpy/scipy not installed (stdlib-only environment)')
class OCSim(unittest.TestCase):
    def setUp(self):
        import dr_oc_sim
        self.S = dr_oc_sim

    def small(self, **kw):
        cfg = dict(self.S.registry()[1]['cfg'])
        cfg.update(reps=40, boot=199, n_eval=60)
        cfg.update(kw)
        return cfg

    def test_same_seed_same_result_and_seed_depends_only_on_id(self):
        cfg = self.small()
        a = self.S.run_scenario(cfg, 123)
        b = self.S.run_scenario(cfg, 123)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertEqual(self.S.scenario_seed('x', 'calib'), self.S.scenario_seed('x', 'calib'))
        self.assertNotEqual(self.S.scenario_seed('x', 'calib'), self.S.scenario_seed('x', 'valid'))

    def test_registry_ids_unique_and_hashes_stable(self):
        reg = self.S.registry()
        ids = [r['id'] for r in reg]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([self.S.config_hash(r['cfg']) for r in reg],
                         [self.S.config_hash(r['cfg']) for r in self.S.registry()])

    def test_resume_skips_completed_scenarios(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, 'r.jsonl')
            self.S.main(['--out', out, '--only', 'anchor_base_N220_d0', '--reps', '5'])
            self.S.main(['--out', out, '--only', 'anchor_base_N220_d0', '--reps', '5'])
            with open(out) as fh:
                self.assertEqual(len(fh.read().splitlines()), 1)

    def test_p0_ceiling_visible_even_in_small_run(self):
        cfg = self.small(reps=300, n_eval=600, delta_m=0.10, delta_t=0.10, boot=199)
        r = self.S.run_scenario(cfg, 7)
        self.assertLess(r['P0_single_M']['rate'], 0.62)       # ~0.5 ceiling for one comparator
        self.assertLess(r['P0']['rate'], 0.50)                # both comparators: lower still

    def test_committed_results_match_registry(self):
        path = os.path.join(ROOT, 'oc_results', 'oc_results_calib.jsonl')
        if not os.path.exists(path):
            self.skipTest('no committed results')
        reg = {r['id']: self.S.config_hash(r['cfg']) for r in self.S.registry()}
        with open(path) as fh:
            for line in fh:
                rec = json.loads(line)
                self.assertEqual(reg[rec['id']], rec['config_hash'], rec['id'])


if __name__ == '__main__':
    unittest.main()
