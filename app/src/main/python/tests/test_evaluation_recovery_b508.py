import copy
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import brain
import evaluation_metrics_ledger as metrics


class EvaluationRecoveryB508(unittest.TestCase):
    def test_indexed_chain_paths_and_h2_prices_match_original_nf_bnf_and_duplicates(self):
        rows = []
        for index, strike in [('NF', 23000), ('BNF', 55000)]:
            for minute in [20, 25, 30, 35, 40]:
                for offset in range(8):
                    for option in ['CE', 'PE']:
                        rows.append(dict(index_key=index, expiry='2026-10-19', strike=strike+offset*50,
                                         option_type=option, poll_ts=f'2026-10-16T15:{minute}:00+05:30',
                                         ltp=100+offset+minute, bid=99+offset+minute, ask=101+offset+minute,
                                         underlying_spot=strike+20))
        rows += [dict(rows[-1], ltp=180, bid=179, ask=181)]
        indexed = brain._EvaluationChainRows(rows)
        for index, strike in [('NF', 23000), ('BNF', 55000)]:
            for entry in ['15:00', '15:25', '15:40']:
                snap = dict(poll_ts=f'2026-10-16T{entry}:00+05:30')
                cand = dict(index=index, expiry='2026-10-19', sellStrike=strike, sellType='CE',
                            buyStrike=strike+50, buyType='CE', sellStrike2=strike+100, sellType2='PE',
                            buyStrike2=strike+150, buyType2='PE')
                before = copy.deepcopy(rows)
                self.assertEqual(brain._build_candidate_path(rows, snap, cand), brain._build_candidate_path(indexed, snap, cand))
                for side in ['sell', 'buy']:
                    for suffix in ['', '2']:
                        self.assertEqual(brain.get_price(rows, cand, side, suffix), brain.get_price(indexed, cand, side, suffix))
                self.assertEqual(rows, before)

    def test_successive_batches_scan_snapshots_once_and_resume_can_rewind(self):
        with tempfile.TemporaryDirectory() as root:
            snapshots = os.path.join(root, 'snapshots.json')
            chain = os.path.join(root, 'chain.json')
            with open(snapshots, 'w') as f: json.dump([{'id': i} for i in range(12)], f)
            with open(chain, 'w') as f: json.dump([], f)
            brain.evaluation_job_prepare('b508-cursor', snapshots, chain)
            original = brain._iter_json_array_file
            decoded = []
            def counted(path):
                for row in original(path):
                    if path == snapshots: decoded.append(row['id'])
                    yield row
            try:
                with patch.object(brain, '_iter_json_array_file', counted), patch.object(
                    brain, '_evaluate_snapshot_outcomes', lambda snap, *a: {'outcomes': [snap]}):
                    for start in (0, 4, 8):
                        batch = json.loads(brain.evaluation_job_run_batch('b508-cursor', start, 4))
                        self.assertEqual(batch['end'], start+4)
                    self.assertEqual(decoded, list(range(12)))
                    replay = json.loads(brain.evaluation_job_run_batch('b508-cursor', 8, 4))
                    self.assertEqual([r['id'] for r in replay['outcomes']], [8, 9, 10, 11])
            finally: brain.evaluation_job_finalize('b508-cursor')

    def test_metrics_file_bridge_preserves_full_input_fingerprint_and_metric_values(self):
        rows = [dict(candidate_id=f'nf-{i}', snapshot_id=i+1, session_date='2026-10-16', role='primary',
                     p_ml=0.65, learning_won_net=i % 2, managed_pnl=100 if i % 2 else -50,
                     friction_cost=10, trade_mode='paper', index_key='NF', strategy_type='BEAR_CALL',
                     expiry='2026-10-19', retained_evidence={'large': 'x'*10000}) for i in range(12)]
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'outcomes.json')
            with open(path, 'w') as f: json.dump(rows, f)
            compact = metrics._MetricFileRows(path)
            self.assertEqual(compact.input_fingerprint, hashlib.sha256(metrics._stable_json(rows).encode()).hexdigest())
            self.assertNotIn('retained_evidence', compact[0])
            ident = metrics.identity_dict(run_id='b508', session_date='2026-10-16')
            full = metrics.compute_metrics_record(identity=ident, rows=rows)
            streamed = metrics.compute_metrics_record(identity=ident, rows=compact)
            full.pop('computed_at'); streamed.pop('computed_at')
            self.assertEqual(full, streamed)

    def test_metrics_file_bridge_does_not_accept_truncated_input(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json') as f:
            f.write('[{"role":"primary"}'); f.flush()
            with self.assertRaises(ValueError): metrics._MetricFileRows(f.name)


if __name__ == '__main__': unittest.main()
