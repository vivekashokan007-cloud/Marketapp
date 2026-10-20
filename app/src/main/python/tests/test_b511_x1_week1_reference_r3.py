"""b511 R3 (Codex): week-one counts must separate the previous-session reference CONSUMED by a row
(previous_session_reference_used) from the per-date/index reference CAPTURE result proven by retained
journal evidence (reference_capture). Unretained history is UNKNOWN / NOT_RETAINED with a required
snapshot cross-check; another session's reference is never assigned. Real collector harness only."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..'))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..', '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import x1_report  # noqa: E402
from test_b510_paper_buying_x1 import Harness, FRI, MON, WED  # noqa: E402
from test_b511_x1_week1_counts import export, PROHIBITED_KEY_RE, PROHIBITED_TEXT, numbers  # noqa: E402

TOOL = os.path.join(ROOT, 'tools', 'x1_week1_counts.py')
CAPTURE_KEYS = {'status', 'session', 'quote_ts_ms', 'decision_ts_ms', 'evidence', 'cross_check_required'}


class ReferenceCaptureR3(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def report(self):
        state = self.h.state()
        failures, _ = x1_report.validate(state)
        self.assertEqual(failures, [])
        d = tempfile.mkdtemp()
        src, out = os.path.join(d, 'export.json'), os.path.join(d, 'out.json')
        with open(src, 'w') as fh:
            json.dump(export(state), fh)
        p = subprocess.run([sys.executable, TOOL, src, '--json', out], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        saved = open(out).read()
        self.assertEqual(p.stdout.strip(), saved.strip())
        money = set()
        for r in state['records']:
            money |= {r[k] for k in ('entry_ask', 'exit_bid', 'gross', 'net', 'fees', 'entry_fee') if k in r}
        for ref in state['references'].values():
            money |= {ref['forward'], ref['straddle'], ref['strike']}
        for text in (p.stdout, saved):
            self.assertIsNone(PROHIBITED_KEY_RE.search(text), PROHIBITED_KEY_RE.search(text))
            for token in PROHIBITED_TEXT:
                self.assertNotIn(token, text)
            self.assertNotIn('instrument_key', text)
            self.assertFalse(set(numbers(json.loads(text))) & money)
        doc = json.loads(saved)
        self.assertTrue(doc['integrity_ok'])
        return state, {(x['session_date'], x['index']): x for x in doc['days']}

    def assert_capture_shape(self, cap):
        self.assertLessEqual(set(cap), CAPTURE_KEYS)

    def test_first_monday_morning_skip_and_afternoon_capture_are_separate(self):
        h = self.h                                    # Codex sequence: no Friday reference
        h.run(h.ctx(MON, '09:19:30'))
        h.run(h.ctx(MON, '09:20:40', 24560, 54880))
        h.run(h.ctx(MON, '09:25:40', 24600, 54850))
        view = h.run(h.ctx(MON, '15:20:30', 24600, 54850))
        self.assertEqual(view['reference_capture'], {'NF': 'CAPTURED', 'BNF': 'CAPTURED'})
        state, days = self.report()
        for ix in ('NF', 'BNF'):
            day = days[(MON, ix)]
            prim, dly = day['arms']['primary'], day['arms']['delayed']
            self.assertEqual(prim['reason'], 'PREVIOUS_SESSION_REFERENCE_UNAVAILABLE')
            self.assertEqual(prim['previous_session_reference_used'], {'state': 'NONE_RECORDED'})
            self.assertEqual(dly['previous_session_reference_used'], {'state': 'NONE_RECORDED'})
            self.assertNotIn('reference_capture', prim)
            cap = day['reference_capture']
            self.assert_capture_shape(cap)
            self.assertEqual(cap['status'], 'CAPTURED')
            self.assertEqual(cap['session'], MON)
            self.assertEqual(cap['evidence'], 'JOURNAL_REFERENCE')
            self.assertEqual(cap['quote_ts_ms'], state['references'][ix]['quote_ts_ms'])
            self.assertEqual(cap['decision_ts_ms'], state['references'][ix]['decision_ts_ms'])
            self.assertFalse(cap['cross_check_required'])

    def test_skipped_primary_with_captured_reference_and_evidence_only_date(self):
        h = self.h
        h.reference()                                 # Friday capture; no rows on Friday
        h.run(h.ctx(MON, '09:20:40', 24502, 55003))   # tiny gaps -> GAP_BELOW_THRESHOLD (row keeps no reference)
        h.run(h.ctx(MON, '09:25:40', 24502, 55003))
        _, days = self.report()
        for ix in ('NF', 'BNF'):
            fri = days[(FRI, ix)]                       # evidence-only date must not disappear
            self.assertEqual(fri['arms'], {})
            self.assertEqual((fri['reference_capture']['status'], fri['reference_capture']['session'],
                              fri['reference_capture']['evidence']), ('CAPTURED', FRI, 'JOURNAL_REFERENCE'))
            self.assert_capture_shape(fri['reference_capture'])
            mon = days[(MON, ix)]
            self.assertEqual(mon['arms']['primary']['reason'], 'GAP_BELOW_THRESHOLD')
            self.assertEqual(mon['arms']['primary']['previous_session_reference_used'], {'state': 'USED_NOT_RETAINED'})
            # Absence of a row reference is not proof the Mon afternoon capture failed: not yet attempted.
            self.assertEqual((mon['reference_capture']['status'], mon['reference_capture']['evidence']),
                             ('UNKNOWN', 'NOT_RETAINED'))
        h.run(h.ctx(MON, '15:20:30', 24510, 55010))   # skipped primary, then a captured reference
        _, days = self.report()
        for ix in ('NF', 'BNF'):
            mon = days[(MON, ix)]
            self.assertEqual(mon['arms']['primary']['status'], 'SKIP')
            self.assertEqual((mon['reference_capture']['status'], mon['reference_capture']['session']), ('CAPTURED', MON))
            fri = days[(FRI, ix)]['reference_capture']  # Fri reference overwritten by Mon: no longer provable
            self.assertEqual((fri['status'], fri['evidence'], fri['session']), ('UNKNOWN', 'NOT_RETAINED', None))
            self.assertTrue(fri['cross_check_required'])
            self.assertNotIn('quote_ts_ms', fri)        # Mon reference is never assigned to Fri

    def test_entered_primary_proves_previous_capture_after_overwrite(self):
        h = self.h
        h.standard_day()
        h.run(h.ctx(MON, '15:20:30', 24700, 54750))   # Mon capture overwrites Fri reference
        state, days = self.report()
        for ix in ('NF', 'BNF'):
            used = days[(MON, ix)]['arms']['primary']['previous_session_reference_used']
            self.assertEqual((used['state'], used['session_date']), ('RETAINED', FRI))
            fri = days[(FRI, ix)]['reference_capture']
            self.assertEqual((fri['status'], fri['session'], fri['evidence']),
                             ('CAPTURED', FRI, 'CONSUMED_BY_NEXT_SESSION_ROW'))
            self.assertEqual(fri['quote_ts_ms'], used['quote_ts_ms'])
            self.assertFalse(fri['cross_check_required'])
            mon = days[(MON, ix)]['reference_capture']
            self.assertEqual((mon['status'], mon['session'], mon['quote_ts_ms']),
                             ('CAPTURED', MON, state['references'][ix]['quote_ts_ms']))

    def test_overwritten_history_is_unknown_not_retained_and_never_borrows_other_session(self):
        h = self.h
        h.reference()                                  # Fri captured
        h.run(h.ctx(MON, '09:20:40', 24502, 55003))
        h.run(h.ctx(MON, '15:20:30'))                  # Mon capture fails (attempt recorded) ...
        h.run(h.ctx(WED, '09:20:40', 24502, 55003))    # Wed consumes Fri reference (Mon none)
        h.run(h.ctx(WED, '15:20:30'))                  # ... and Wed failure overwrites the Mon attempt
        state = h.state()
        self.assertEqual(state['attempts']['NF:reference']['session'], WED)
        self.assertEqual(state['references']['NF']['session_date'], FRI)
        _, days = self.report()
        for ix in ('NF', 'BNF'):
            mon = days[(MON, ix)]['reference_capture']
            self.assertEqual(mon['status'], 'UNKNOWN')
            self.assertEqual(mon['evidence'], 'NOT_RETAINED')
            self.assertTrue(mon['cross_check_required'])
            self.assertIsNone(mon['session'])
            self.assertNotIn('quote_ts_ms', mon)        # Fri reference is not assigned to Mon
            wed = days[(WED, ix)]['reference_capture']
            self.assertEqual((wed['status'], wed['session'], wed['evidence']),
                             ('QUOTE_TIME_UNAVAILABLE', WED, 'JOURNAL_ATTEMPT'))
            self.assertFalse(wed['cross_check_required'])
            self.assertEqual(days[(FRI, ix)]['reference_capture']['status'], 'CAPTURED')
            self.assertEqual(days[(FRI, ix)]['reference_capture']['session'], FRI)


if __name__ == '__main__':
    unittest.main()
