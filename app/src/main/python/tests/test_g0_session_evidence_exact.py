"""G0: a user-selected historical session export is exact (no silent fallback to another day)."""
import json
import os
import shutil
import tempfile
import unittest

import brain
import session_evidence_v1 as se

NOV2, NOV3 = '2026-11-02', '2026-11-03'


class G0ExactSession(unittest.TestCase):
    def setUp(self):
        self.fd = tempfile.mkdtemp()
        for d in ('index_authority_v1', 'learning_ledger_v1', 'self_study_v1', 'forensics'):
            os.makedirs(os.path.join(self.fd, d))
        self.addCleanup(shutil.rmtree, self.fd, True)

    def auth(self, day):
        with open(os.path.join(self.fd, 'index_authority_v1', day + '.json'), 'w') as f:
            json.dump({'session_date': day, 'history': {'NF': [{'decision_id': 'd-' + day, 'why': {}}], 'BNF': []}}, f)

    def archive(self, day):
        with open(os.path.join(self.fd, 'learning_ledger_v1', day + '.json'), 'w') as f:
            json.dump({'session_date': day, 'rows': [{'decision_id': 'a-' + day}]}, f)

    def bundle(self, day, exact=True):
        return json.loads(brain.session_evidence_bundle(self.fd, day, None, '{}', exact))

    def test_exact_present_session_ok_and_content_matches_date(self):
        self.auth(NOV3)
        b = self.bundle(NOV3)
        m = b['manifest']
        self.assertEqual((m['status'], m['session_date_requested'], m['session_date_used']), ('OK', NOV3, NOV3))
        self.assertTrue(m['session_date_exact_required'])
        self.assertEqual(b['parts']['authority_doc']['session_date'], NOV3)
        self.assertEqual(m['parts']['authority_doc']['status'], 'PRESENT')
        self.assertEqual(m['parts']['learning_ledger']['status'], 'MISSING')   # missing stays MISSING

    def test_mismatched_fallback_is_explicit_failure_without_parts(self):
        self.auth(NOV2)                         # 3 Nov not retained; legacy resolver would fall back to 2 Nov
        self.assertEqual(se.resolve_session(self.fd, NOV3), NOV2)
        b = self.bundle(NOV3)
        m = b['manifest']
        self.assertEqual(m['status'], 'SESSION_DATE_MISMATCH')
        self.assertEqual((m['session_date_requested'], m['session_date_used']), (NOV3, NOV2))
        self.assertNotIn('parts', b)
        self.assertNotIn('d-' + NOV2, json.dumps(b))

    def test_missing_date_is_explicit_failure(self):
        b = self.bundle(NOV2)
        self.assertEqual(b['manifest']['status'], 'SESSION_DATE_MISSING')
        self.assertIsNone(b['manifest']['session_date_used'])
        self.assertNotIn('parts', b)

    def test_nov2_archive_with_expired_short_authority_source(self):
        self.archive(NOV2)
        self.auth(NOV3)                         # 2 Nov authority file already pruned by short retention
        b = self.bundle(NOV2)
        m = b['manifest']
        self.assertEqual((m['status'], m['session_date_used']), ('OK', NOV2))
        self.assertEqual(m['parts']['authority_doc']['status'], 'MISSING')
        self.assertEqual(m['parts']['learning_ledger']['status'], 'PRESENT')
        self.assertEqual(b['parts']['learning_ledger']['session_date'], NOV2)
        self.assertNotIn('d-' + NOV3, json.dumps(b))
        self.assertTrue(os.path.exists(os.path.join(self.fd, 'learning_ledger_v1', NOV2 + '.json')))   # kept

    def test_both_retained_days_selectable(self):
        self.auth(NOV2); self.auth(NOV3)
        for d in (NOV2, NOV3):
            b = self.bundle(d)
            self.assertEqual((b['manifest']['status'], b['manifest']['session_date_used']), ('OK', d))
            self.assertEqual(b['parts']['authority_doc']['session_date'], d)

    def test_default_non_exact_behaviour_unchanged(self):
        self.auth(NOV2)
        b = self.bundle(NOV3, exact=False)
        self.assertEqual((b['manifest']['status'], b['manifest']['session_date_used']), ('OK', NOV2))

    def test_files_untouched(self):
        self.auth(NOV2)
        before = sorted(os.listdir(os.path.join(self.fd, 'index_authority_v1')))
        self.bundle(NOV3); self.bundle('2026-10-01')
        self.assertEqual(sorted(os.listdir(os.path.join(self.fd, 'index_authority_v1'))), before)

    def test_native_bridge_requests_exact_and_refuses_mismatch_before_save(self):
        base = os.path.join(os.path.dirname(__file__), '..', '..', 'java', 'com', 'marketradar', 'app')
        with open(os.path.join(base, 'NativeBridge.kt')) as f:
            nb = f.read()
        body = nb[nb.index('fun exportSessionEvidence('):]
        body = body[:body.index('\n    }\n')]
        self.assertIn('IndexAuthorityContext.reviewTrades(prefs).json, meta, true)', body)
        guard = body.index('if (used != date')
        self.assertLess(guard, body.index('saveExportBytes('))
        self.assertIn('saveExportBytes("mr_session_evidence_$used.json", "application/json", bytes)', body)


if __name__ == '__main__':
    unittest.main()
