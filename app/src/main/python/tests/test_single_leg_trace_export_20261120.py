"""Codex reply 20 Nov 2026: optional `single_leg_trace` export part (mr.single_leg_trace.v1).  Synthetic records
only; no network, no Supabase.  Absent directory -> export byte-identical to before; missing file -> MISSING;
allow-list/privacy; per-index classification (NF and BNF separately); cap degradation never at the expense of the
released parts."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import session_evidence_v1 as se

DAY = '2026-11-18'
T0 = 1794975300000  # 2026-11-18 09:45:00 IST


def r(kind, **kv):
    d = {'kind': kind, 't_ms': T0}
    d.update(kv)
    return d


def doc(records, **extra):
    d = {'schema': 'mr.single_leg_trace.v1', 'session_date': DAY, 'records': records, 'dropped_renders': 0,
         'dropped_other': 0, 'deduped_renders': 0, 'rejected_records': 0, 'truncated': False}
    d.update(extra)
    return d


class TraceExport(unittest.TestCase):
    def setUp(self):
        self.fd = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.fd, 'index_authority_v1'))
        with open(os.path.join(self.fd, 'index_authority_v1', DAY + '.json'), 'w') as f:
            json.dump({'session_date': DAY, 'history': {'NF': [], 'BNF': []}}, f)

    def tearDown(self):
        shutil.rmtree(self.fd, ignore_errors=True)

    def build(self):
        return se.build(self.fd, DAY, None, {'pwa': 'T', 'app': 'T'}, require_exact=True)

    def write(self, d):
        os.makedirs(os.path.join(self.fd, se.TRACE_DIR), exist_ok=True)
        with open(os.path.join(self.fd, se.TRACE_DIR, DAY + '.json'), 'w') as f:
            json.dump(d, f)

    def test_absent_directory_leaves_export_unchanged(self):
        b, raw = self.build()
        self.assertNotIn('single_leg_trace', b['manifest']['parts'])
        self.assertNotIn('single_leg_trace', b.get('parts', {}))
        b2, raw2 = self.build()
        self.assertEqual(len(raw), len(raw2))

    def test_missing_file_for_session(self):
        os.makedirs(os.path.join(self.fd, se.TRACE_DIR))
        b, _ = self.build()
        self.assertEqual(b['manifest']['parts']['single_leg_trace']['status'], 'MISSING')
        self.assertEqual(b['parts']['single_leg_trace']['reason'], 'NO_TRACE_FOR_SESSION')

    def test_privacy_allow_list_in_export(self):
        self.write(doc([r('CONFIG', capital=250000, capital_set=True, pwa='b531', token='x', reason='Bearer abc'),
                        r('RENDER', index='NF', decision_id='idx1_a', trade_visible=True, action_exposed=True, qty=65)]))
        b, raw = self.build()
        p = b['parts']['single_leg_trace']
        self.assertNotIn(b'250000', raw)
        self.assertNotIn('token', p['records'][0])
        self.assertNotIn('qty', p['records'][1])
        self.assertEqual(p['records'][0]['reason'], '[REDACTED]')
        self.assertTrue(p['records'][0]['capital_set'])

    def test_classification_per_index(self):
        S = se.single_leg_trace_summary
        vis = dict(trade_visible=True, action_exposed=True)
        self.assertEqual(S([])['NF']['classification'], 'NOT_OBSERVED')
        self.assertIsNotNone(S([])['NF']['caveat'])
        s = S([r('RENDER', index='NF', **vis), r('RENDER', index='BNF', trade_visible=True, action_exposed=False, row_state='NO_DIRECTIONAL_PROPOSAL')])
        self.assertEqual((s['NF']['classification'], s['BNF']['classification']), ('ACTION_RENDERED_NO_TAP', 'NO_ELIGIBLE_PROPOSAL'))
        self.assertEqual(s['BNF']['primary_row_state'], 'NO_DIRECTIONAL_PROPOSAL')
        self.assertEqual(S([r('RENDER', index='NF', **vis), r('TAP', index='NF'), r('REFUSED', index='NF', reason='DISPLAYED_IDENTITY_REQUIRED')])['NF']['classification'],
                         'TAP_REFUSED_BEFORE_REQUEST')
        self.assertEqual(S([r('N_SUBMIT', index='NF', request_id='a'), r('N_OUTCOME', index='NF', request_id='a', status='REJECTED', quote_requested=False)])['NF']['classification'],
                         'TAP_REFUSED_BEFORE_QUOTE')
        self.assertEqual(S([r('N_SUBMIT', index='NF', request_id='a'), r('N_QUOTE_STARTED', index='NF', request_id='a'),
                            r('N_OUTCOME', index='NF', request_id='a', status='REJECTED')])['NF']['classification'], 'TAP_REFUSED_AFTER_QUOTE')
        self.assertEqual(S([r('SUBMITTED', index='BNF', request_id='b')])['BNF']['classification'], 'OUTCOME_UNCONFIRMED')
        self.assertEqual(S([r('RECOVERY_STARTED', index='BNF', request_id='b'), r('RECOVERY_OUTCOME', index='BNF', request_id='b', status='REJECTED', reason='CANCELLED_BEFORE_COMMIT')])['BNF']['classification'], 'CANCELLED')
        s = S([r('SUBMITTED', index='BNF', request_id='b'), r('OUTCOME', index='BNF', request_id='b', status='ADMITTED')])
        self.assertEqual((s['BNF']['classification'], s['NF']['classification']), ('ADMITTED', 'NOT_OBSERVED'))

    def test_incomplete_display_and_quote_evidence(self):
        summarize = se.single_leg_trace_summary
        hidden = r('RENDER', index='NF', trade_visible=False, action_exposed=True)
        missing = r('RENDER', index='BNF', trade_visible=True, row_state='ROW_UNAVAILABLE', reason='NO_AUTHORITY_RESULT')
        result = summarize([hidden, missing])
        self.assertEqual(result['NF']['classification'], 'NOT_OBSERVED')
        self.assertEqual(result['BNF']['classification'], 'ACTION_NOT_EXPOSED')
        self.assertEqual(result['BNF']['primary_reasons'], ['NO_AUTHORITY_RESULT'])

        tap = r('TAP', index='NF')
        submit = r('SUBMITTED', index='NF', request_id='r')
        pending = r('OUTCOME', index='NF', request_id='r', status='PENDING')
        result = summarize([tap, r('RENDER', index='NF', trade_visible=True, action_exposed=True)])
        self.assertEqual(result['NF']['classification'], 'OUTCOME_UNCONFIRMED')
        self.assertEqual(result['NF']['unconfirmed_taps'], 1)
        result = summarize([tap, submit, pending])
        self.assertEqual(result['NF']['classification'], 'OUTCOME_UNCONFIRMED')
        self.assertEqual(result['NF']['unresolved_request_ids'], ['r'])

        rejected = r('RECOVERY_OUTCOME', index='NF', request_id='r', status='REJECTED')
        result = summarize([tap, submit, rejected, pending])
        self.assertEqual(result['NF']['classification'], 'TAP_REFUSED_QUOTE_UNKNOWN')
        self.assertEqual(result['NF']['outcomes']['r']['status'], 'REJECTED')
        self.assertNotIn('quote_requested', result['NF']['outcomes']['r'])

    def test_trace_never_reduces_existing_c1_capture(self):
        def export_pilot(_directory, _day, level):
            return {'status': 'OK', 'level': level, 'payload': 'x' * (10000 if level == 'full' else 100)}

        with mock.patch('c1_capture_pilot_v1.export_part', side_effect=export_pilot):
            existing, existing_bytes = self.build()
            limit = len(existing_bytes) + 3000
            self.write(doc([r('RENDER', index='NF', row_state='ELIGIBLE', instrument_key='NSE_FO|' + '1' * 80)
                            for _ in range(40)], page_queue_dropped=3, truncated=True))
            with mock.patch.object(se, 'MAX_BUNDLE_BYTES', limit):
                bundle, raw = self.build()
            self.assertLessEqual(len(raw), limit)
            self.assertEqual(bundle['parts']['c1_capture'], existing['parts']['c1_capture'])
            self.assertEqual(bundle['manifest']['parts']['c1_capture'], existing['manifest']['parts']['c1_capture'])
            trace = bundle['manifest']['parts']['single_leg_trace']
            self.assertEqual(trace['trace_level'], 'no_render')
            self.assertEqual(trace['page_queue_dropped'], 3)

    def test_cap_degradation_never_truncates_released_parts(self):
        recs = [r('RENDER', index='NF', row_state='S%d' % i, trade_visible=True) for i in range(400)] + [r('TAP', index='NF', origin='tap%d' % i) for i in range(100)]
        self.write(doc(recs, truncated=True, dropped_renders=7))
        b0, raw0 = self.build()
        e = b0['manifest']['parts']['single_leg_trace']
        self.assertEqual(e['status'], 'TRUNCATED'); self.assertNotIn('trace_level', e)  # device-side truncation is reported
        self.assertTrue(e['truncated_on_device']); self.assertEqual(e['dropped_renders'], 7)
        full = len(raw0)
        os.rename(os.path.join(self.fd, se.TRACE_DIR), os.path.join(self.fd, 'hidden'))
        base = len(self.build()[1])                       # the export with no trace directory at all
        os.rename(os.path.join(self.fd, 'hidden'), os.path.join(self.fd, se.TRACE_DIR))
        levels = []
        for cap in (full - 10, base + 3000, base + 400, base + 5):
            with mock.patch.object(se, 'MAX_BUNDLE_BYTES', cap):
                b, raw = self.build()
                self.assertLessEqual(len(raw), cap)
                e = b['manifest']['parts'].get('single_leg_trace')
                levels.append(None if e is None else (e['status'], e.get('trace_level')))
                for k, v in b['manifest']['parts'].items():   # released parts never pay for the optional trace
                    if k != 'single_leg_trace':
                        self.assertEqual(v.get('status'), b0['manifest']['parts'][k]['status'], k)
        self.assertEqual(levels[0], ('TRUNCATED', 'no_render'))
        self.assertEqual(levels[1], ('TRUNCATED', 'summary'))
        self.assertEqual(levels[2], ('OMITTED_FOR_CAP', None))
        self.assertIsNone(levels[3])

    def test_unreadable_file(self):
        os.makedirs(os.path.join(self.fd, se.TRACE_DIR))
        with open(os.path.join(self.fd, se.TRACE_DIR, DAY + '.json'), 'w') as f:
            f.write('{bad')
        b, _ = self.build()
        self.assertEqual(b['manifest']['parts']['single_leg_trace']['status'], 'UNREADABLE')


if __name__ == '__main__':
    unittest.main()
