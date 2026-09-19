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
        self.assertIn('prepareRejectedRowsForUpload(rejectedRowsRaw)', fragment)
        self.assertIn('REJECTED_RESEARCH_ROWS_FILTERED', source)

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

    def test_stable_lease_holder_and_release_in_finally(self):
        source = (JAVA / 'MarketMLService.kt').read_text()
        ledger = (JAVA / 'EvaluationRunLedger.kt').read_text()
        self.assertIn('stableLeaseHolder', ledger)
        self.assertIn('fun releaseLease(', ledger)
        self.assertIn('EvaluationRunLedger.stableLeaseHolder(android.os.Build.MODEL, sessionDate)', source)
        start = source.index('} finally {')
        # Prefer the runDayEvaluation finally that finalizes the python job.
        frag_start = source.index('brain?.callAttr("evaluation_job_finalize", runId)')
        fragment = source[frag_start:frag_start + 900]
        self.assertIn('EvaluationRunLedger.releaseLease(', fragment)
        self.assertIn('leaseAcquired', source)
        # In-process concurrency guard must remain.
        self.assertIn('claimEvaluationSession(sessionDate)', source)
        self.assertIn('activeEvaluationSession', source)

    def test_alarm_receiver_preserves_historical_continuation_date(self):
        source = (JAVA / 'MarketMLService.kt').read_text()
        start = source.index('class EvaluationAlarmReceiver')
        end = source.index('class MarketMLService', start)
        receiver = source[start:end]
        self.assertIn('getBooleanExtra("continuation"', receiver)
        self.assertIn('getStringExtra("session_date")', receiver)
        self.assertIn('resolveEvaluationAlarmSessionDate(', receiver)
        self.assertIn('shouldBypassEvaluationReminderWindow(isContinuation)', receiver)
        self.assertIn('putExtra("session_date", targetDate)', receiver)
        # Ordinary path still uses reminder window; bypass only for continuation.
        self.assertIn('shouldBypassEvaluationReminderWindow', receiver)
        # Assert against the helper definition (not the call site).
        defn = source.split('internal fun resolveEvaluationAlarmSessionDate(')[1].split(
            'internal fun shouldBypassEvaluationReminderWindow('
        )[0]
        self.assertIn('if (isContinuation && requested.isNotEmpty()) requested else todayIst', defn)
        bypass = source.split('internal fun shouldBypassEvaluationReminderWindow(')[1].split(
            'private fun nextEvaluationReminderAt('
        )[0]
        self.assertIn('isContinuation', bypass)

    def test_completed_session_not_rerun_guard_present(self):
        source = (JAVA / 'MarketMLService.kt').read_text()
        self.assertIn('evaluation_done_date', source)
        self.assertIn('EVAL_SKIP: already done for $sessionDate', source)
        # Idempotent upload path still validates distinct outcomes.
        supabase = (JAVA / 'SupabaseClient.kt').read_text()
        self.assertIn('EvaluationIdentity.validatedDistinctOutcomes(sessionDate, body)', supabase)


if __name__ == '__main__':
    unittest.main()
