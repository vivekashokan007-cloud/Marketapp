"""b511 E1: a candidate whose contract lot cannot be resolved must be an explicit,
accounted teacher drop, never a swallowed TypeError.

Root cause of the primary-only 9-15 Oct 2026 sessions: on Android builds <= 2.6.75 the
lot-table asset was unreachable (embedded fallback has no rules), ranked/rejected menu
projections (since b499) carry no lotSize, and `_legacy_h2_structure_valuation` compared
`None <= 0`. Every secondary and rejected candidate raised TypeError, which
`_evaluate_snapshot_outcomes` recorded as a non-fatal per-candidate error, so only the
primary (which keeps lotSize) produced an outcome. b507 packaged the table; this guard
keeps a future lot-resolution gap from silently erasing the menu again.
"""
import copy
import json
import os
import unittest

import brain
import contract_lot_table as clt

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'b511_e1_lot_unavailable.json')


def _load():
    with open(FIXTURE, encoding='utf-8') as fh:
        fx = json.load(fh)
    snap = copy.deepcopy(fx['snapshot'])
    snap['context_json'] = json.dumps(snap['context_json'])
    snap['primary_candidate_json'] = json.dumps(snap['primary_candidate_json'])
    snap['top_candidates_json'] = json.dumps(snap['top_candidates_json'])
    return snap, fx['chain']


class LotUnavailableAccounting(unittest.TestCase):
    def tearDown(self):
        clt.clear_lot_table_cache()

    def _run(self, table_available):
        clt.clear_lot_table_cache()
        if not table_available:
            clt._TABLE_CACHE = dict(clt._EMBEDDED_TABLE)   # the pre-b507 Android state
        snap, chain = _load()
        return brain._evaluate_snapshot_outcomes(snap, chain, brain._teacher_default_config())

    def test_fixture_reproduces_the_projection_shape(self):
        snap, _ = _load()
        primary = json.loads(snap['primary_candidate_json'])
        ranked = json.loads(snap['context_json'])['snapshot_ranked_candidates_full']
        self.assertIn('lotSize', primary)
        self.assertTrue(ranked and all('lotSize' not in c for c in ranked))

    def test_unavailable_lot_is_an_accounted_drop_not_a_swallowed_error(self):
        result = self._run(table_available=False)
        type_errors = [e for e in result['errors'] if "not supported between instances of 'NoneType'" in e.get('error', '')]
        self.assertEqual(type_errors, [], 'lot gap must not be hidden as a per-candidate exception')
        self.assertEqual([o['role'] for o in result['outcomes']], ['primary'])
        self.assertGreaterEqual(sum(result['teacher_drop_reasons'].values()), 1)

    def test_available_lot_table_still_values_the_secondary(self):
        result = self._run(table_available=True)
        self.assertEqual(sorted(o['role'] for o in result['outcomes']), ['primary', 'secondary'])
        self.assertEqual(result['errors'], [])
        self.assertTrue(all(o.get('price_integrity') == 'OK' for o in result['outcomes']))

    def test_batch_entry_point_is_deterministic(self):
        first = json.dumps(self._run(True)['outcomes'], sort_keys=True, default=str)
        second = json.dumps(self._run(True)['outcomes'], sort_keys=True, default=str)
        self.assertEqual(first, second)


if __name__ == '__main__':
    unittest.main()
