"""Regression for sampled-only expiries and the storage-to-outcome path."""
import copy
import json
import os
import sys
import tempfile
import unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import brain
from test_evaluation_chain_quote_contract import candidate, snapshot, chain_rows
from evaluation_run_ledger import new_run, set_stage, STAGE_ORDER


class LearningEvidencePathTest(unittest.TestCase):
    def test_sample_only_expiry_survives_file_batch_and_produces_same_outcome(self):
        c = candidate()
        c.update(final_rank=205, evidence_source=brain.RANKED_EVIDENCE_SOURCE_BELOW_CAP,
                 sampling_inclusion_probability=0.5, sampling_hash_prefix='abcdef')
        snap = snapshot(c)
        snap['primary_candidate_json'] = '{}'
        snap['context_json'] = json.dumps({'snapshot_ranked_below_cap_sample': [c]})
        original = brain._evaluate_snapshot_outcomes(snap, chain_rows(True), brain._teacher_default_config())
        self.assertEqual(len(original['outcomes']), 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'chain.json')
            with open(path, 'w') as f:
                json.dump(chain_rows(True), f)
            retained = brain._evaluation_batch_chain_rows(path, [snap])
        self.assertEqual(len(retained), len(chain_rows(True)))
        compact = brain._compact_android_snapshot_context(json.loads(snap['context_json']))
        snap['context_json'] = json.dumps(compact)
        actual = brain._evaluate_snapshot_outcomes(snap, retained, brain._teacher_default_config())
        self.assertEqual(actual['outcomes'], original['outcomes'])
        self.assertGreater(actual['outcomes'][0]['rank_in_snapshot'], 200)

    def test_200_ranked_and_50_sampled_retain_quotes_under_unchanged_budget(self):
        rows = []
        for i in range(250):
            c = candidate()
            c.update(id=f'candidate-{i}', final_rank=i+1,
                     evidence_source=brain.RANKED_EVIDENCE_SOURCE_TOP_CAP if i < 200 else brain.RANKED_EVIDENCE_SOURCE_BELOW_CAP,
                     pc2BatchFCandleComponents={'diagnostic': 'x' * 9000})
            rows.append(c)
        source = {'snapshot_ranked_candidates_full': rows[:200],
                  'snapshot_ranked_below_cap_sample': rows[200:],
                  'snapshot_ranked_below_cap_sampling': {'sampling_selected_count': 50}}
        before = copy.deepcopy(source)
        compact = brain._compact_android_snapshot_context(source)
        self.assertEqual(source, before)
        self.assertLess(len(json.dumps(compact).encode()), 1_350_000)
        self.assertEqual(len(compact['snapshot_ranked_candidates_full']), 200)
        self.assertEqual(len(compact['snapshot_ranked_below_cap_sample']), 50)
        for raw, reduced in zip(rows, compact['snapshot_ranked_candidates_full'] + compact['snapshot_ranked_below_cap_sample']):
            snap = snapshot(raw)
            a = brain._eval_single_candidate(chain_rows(True), snap, raw)
            b = brain._eval_single_candidate(chain_rows(True), snap, reduced)
            self.assertIsNotNone(a)
            self.assertEqual(a, b)

    def test_evidence_ready_does_not_mean_model_learned(self):
        run = new_run(session_date='2026-10-07', input_manifest={'snapshot_ids': [1]})
        for stage in STAGE_ORDER:
            run = set_stage(run, stage, 'verified')
        self.assertTrue(run['evidence_ready'])
        for key in ('model_trained', 'model_validated', 'paper_model_active', 'learning_complete'):
            self.assertFalse(run[key], key)
