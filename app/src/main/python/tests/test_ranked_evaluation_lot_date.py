"""A retained ranked row without lotSize still has dated contract authority."""
import copy
import unittest

import brain
from test_evaluation_chain_quote_contract import candidate, chain_rows, snapshot


class RankedEvaluationLotDateTests(unittest.TestCase):
    def test_compaction_preserves_explicit_retained_tail_capture_scope(self):
        capture = {'source_count': 700, 'retained_count': 128, 'source_tail_cap': 128,
                   'truncated': True, 'scope': 'RETAINED_TAIL_TELEMETRY'}
        compact = brain._compact_android_snapshot_context({'snapshot_pc2_authority_capture': capture})
        self.assertEqual(compact['snapshot_pc2_authority_capture'], capture)

    def test_missing_ranked_lot_matches_captured_contract_lot(self):
        full = candidate()
        ranked = copy.deepcopy(full)
        ranked.pop('lotSize')
        before = copy.deepcopy(ranked)
        reference = brain._eval_single_candidate(chain_rows(True), snapshot(full), full, role='secondary')
        actual = brain._eval_single_candidate(chain_rows(True), snapshot(ranked), ranked, role='secondary')
        self.assertIsNotNone(reference)
        self.assertIsNotNone(actual)
        for key in ('sim_pnl_h2', 'managed_pnl', 'friction_cost', 'exit_reason',
                    'r_multiple', 'lotSize', 'contract_lot_size'):
            self.assertEqual(actual[key], reference[key], key)
        self.assertEqual(ranked, before)

    def test_unknown_index_is_not_replaced_by_a_default_lot(self):
        ranked = candidate()
        ranked.pop('lotSize')
        ranked['index'] = 'UNKNOWN'
        drops = {'reasons': {}, 'samples': []}
        self.assertIsNone(brain._eval_single_candidate(
            chain_rows(True), snapshot(ranked), ranked, role='secondary', drop_sink=drops))
        self.assertTrue(drops['reasons'])

    def test_missing_snapshot_date_cannot_resolve_expiry_only_lot(self):
        ranked = candidate()
        ranked.pop('lotSize')
        snap = snapshot(ranked)
        snap.pop('session_date', None)
        self.assertIsNone(brain._eval_single_candidate(chain_rows(True), snap, ranked, role='secondary'))


if __name__ == '__main__':
    unittest.main()
