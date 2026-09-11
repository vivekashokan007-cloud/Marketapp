"""Cross-language wiring guards; executable identity cases live in Kotlin JUnit."""
import pathlib
import unittest
from unittest.mock import patch

import brain

ROOT = pathlib.Path(__file__).resolve().parents[5]
JAVA = ROOT / 'app/src/main/java/com/marketradar/app'


class EvaluationIdentityContractTests(unittest.TestCase):
    def test_reconciliation_precedes_python_prepare_even_for_cached_inputs(self):
        source = (JAVA / 'MarketMLService.kt').read_text()
        start = source.index('val preparedInputs = ensureEvaluationInputFiles(sessionDate)')
        end = source.index('"evaluation_job_prepare"', start)
        self.assertIn('reconcileEvaluationSnapshotIds(sessionDate, snapshotsFile)', source[start:end])
        self.assertIn('EVAL_IDENTITY_REPLACE_FAILED: original inputs retained', source)

    def test_bad_identity_checkpoint_is_archived_before_reset(self):
        source = (JAVA / 'MarketMLService.kt').read_text()
        start = source.index('var invalidIdentityRows = 0')
        end = source.index('EVAL_RESUME_CHECKPOINT', start)
        fragment = source[start:end]
        self.assertIn('EvaluationIdentity.hasSnapshotIdentity(row, evaluationSnapshotIds)', fragment)
        self.assertLess(fragment.index('archiveEvaluationOutput'), fragment.index('writeJsonArrayFile'))
        self.assertIn('canResume = false', fragment)

    def test_all_uploads_use_prevalidated_distinct_rows(self):
        source = (JAVA / 'SupabaseClient.kt').read_text()
        start = source.index('fun saveEvaluationOutcomes(')
        fragment = source[start:source.index('fun outcomeWritePath(', start)]
        self.assertIn('EvaluationIdentity.validatedDistinctOutcomes(sessionDate, body)', fragment)
        self.assertIn('buildEvaluationRows(distinct)', fragment)
        self.assertIn('buildRecommendationRows(sessionDate, distinct)', fragment)
        self.assertIn('buildRejectedEvaluationRows(sessionDate, distinct)', fragment)
        self.assertIn('EVAL_REJECTED_ID_COLLISION', fragment)

    def test_cache_preserves_id_if_available(self):
        source = (JAVA / 'EvaluationLocalCache.kt').read_text()
        start = source.index('private fun compactBrainSnapshot(')
        end = source.index('for (key in scalarKeys)', start)
        self.assertIn('"id"', source[start:end])

    def test_missing_snapshot_replay_uses_existing_writer_before_input_replacement(self):
        source = (JAVA / 'MarketMLService.kt').read_text()
        fragment = source.split('private fun reconcileEvaluationSnapshotIds(')[1].split('private fun archiveEvaluationOutput')[0]
        self.assertIn('EvaluationIdentity.SnapshotReconciler', fragment)
        self.assertIn('SupabaseClient.saveBrainSnapshot(snapshot)', fragment)
        self.assertLess(fragment.index('reconciler.resolve(row)'), fragment.index('temp.renameTo(file)'))
        self.assertNotIn('file.delete()', fragment)

    def test_identity_lookup_is_complete_and_read_only(self):
        source = (JAVA / 'SupabaseClient.kt').read_text()
        fragment = source.split('internal fun fetchEvaluationSnapshotIdentities(')[1].split('fun fetchChainSlices')[0]
        self.assertIn('EVAL_IDENTITY_LOOKUP_FAILED', fragment)
        self.assertIn('EVAL_IDENTITY_LOOKUP_CAPPED', fragment)
        self.assertIn('select=id,poll_ts,session_date,recommendation_id', fragment)
        self.assertNotIn('postArray', fragment)

    def test_common_outcome_carries_exact_snapshot_provenance_for_primary_too(self):
        snapshot = {'id': 5688, 'session_date': '2026-09-10', 'poll_ts': '2026-09-10T15:20:44+0530'}
        candidate = {'id': 'c', 'poll_ts': '2026-09-10T15:20:00+0530'}
        with patch.object(brain, '_legacy_h2_structure_valuation', return_value={'sim_pnl_h2': 1}), \
             patch.object(brain, '_managed_teacher_outcome', return_value={}):
            outcome = brain._eval_single_candidate([], snapshot, candidate, role='primary')
        self.assertEqual(outcome['snapshot_poll_ts'], snapshot['poll_ts'])
        self.assertEqual(outcome['snapshot_id'], 5688)


if __name__ == '__main__':
    unittest.main()
