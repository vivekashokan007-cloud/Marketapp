"""G1: authority provenance survives the real Python snapshot projection (take_poll_snapshot) including the
documented Android-compaction trimming and the fail-closed budget stub.  The cross-language chain
(Python -> Kotlin compactor/local cache/upload sanitiser -> teacher) is G1AuthorityProvenanceChainTest."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..', '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'tools', 'g1_authority_chain'))

import brain  # noqa: E402
import chain as G  # noqa: E402


def _ctxj(snap):
    c = snap['context_json']
    return json.loads(c) if isinstance(c, str) else c


class G1Projection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r, cls.snap, cls.c, _ = G._run('pair', G._std_polls())

    def test_generated_candidates_keep_lineage_and_leg_freshness(self):
        gen = _ctxj(self.snap)['snapshot_generated_candidates']
        self.assertEqual(len(gen), 4)
        for c in gen:
            self.assertEqual(c['authorityPolicyVersion'], 'index_direction_v1')
            self.assertEqual(c['policySelectorVersion'], 'index_managed_v1')
            self.assertEqual(c['constructionVersion'], 'index_construction_v1')
            self.assertEqual(c['indexDecisionId'], self.r['by_index'][c['index']]['decision_id'])
            for leg in c['legs']:
                for k in ('exchange_ts', 'bid_qty', 'ask_qty'):
                    self.assertIn(k, leg)

    def test_managed_summary_bounded(self):
        mps = _ctxj(self.snap)['snapshot_managed_policy']
        self.assertEqual(mps['schema'], brain.IA_MANAGED_SUMMARY_SCHEMA)
        for ix in ('NF', 'BNF'):
            m = mps['by_index'][ix]
            self.assertEqual(m['reason'], 'STRUCTURE_CHOICE_RULE_UNSPECIFIED')
            self.assertEqual(m['admissible_count'], 2)
            self.assertLessEqual(len(m['gates']), brain.IA_MANAGED_GATE_CAP)
        self.assertLess(len(json.dumps(mps)), 4096)

    def test_legacy_candidates_gain_no_lineage_or_null_keys(self):
        legacy = {'id': 'x', 'legs': [{'action': 'SELL', 'strike': 1, 'option_type': 'PE', 'bid': 1, 'ask': 2}]}
        v = brain._candidate_view(legacy)
        for k in brain.IA_CANDIDATE_LINEAGE_KEYS + ('lotEvidence',):
            self.assertNotIn(k, v)
        self.assertEqual(set(v['legs'][0]), {'action', 'strike', 'option_type', 'bid', 'ask'})

    def test_lot_evidence_status_compacted(self):
        c = dict(self.r['candidates_by_index']['NF'][0])
        c['lotEvidence'] = {'schema': 'managed_construction_lot_v1', 'status': 'VERIFIED', 'source': 'upstox_v2_option_contract',
                            'session_date': '2026-11-02', 'reasons': [], 'legs': [{'instrument_key': 'a', 'lot_size': 65}] * 2}
        v = brain._candidate_view(c)
        self.assertEqual(v['lotEvidence'], {'schema': 'managed_construction_lot_v1', 'status': 'VERIFIED',
                                            'source': 'upstox_v2_option_contract', 'session_date': '2026-11-02',
                                            'reasons': [], 'leg_count': 2})

    def test_authority_keys_survive_python_fail_closed_stub(self):
        ctx = dict(_ctxj(self.snap))
        # snapshot_latest_poll is never dropped by the documented trimming -> forces the fail-closed stub
        ctx['snapshot_latest_poll'] = {'blob': 'z' * 1_500_000}
        compact = brain._compact_android_snapshot_context(ctx)
        self.assertTrue(compact['snapshot_android_compaction'].get('fail_closed'))
        self.assertIn('snapshot_index_authority_primary', compact)
        self.assertIn('snapshot_managed_policy', compact)


if __name__ == '__main__':
    unittest.main()
