"""Offline regressions for CI-3. Synthetic records only; immutable protocol bytes are never changed."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
INPUTS = ROOT / 'freeze_inputs'
CHECKER = INPUTS / 'verify_freeze_record_20261023.py'
PROTOCOL = INPUTS / 'SPECIAL_SESSION_PROTOCOL_V1_20261023.md'
SHA = 'cf65c2ada8f91da1f077836935eb6fbb15e78e39b8689bedbbae2679a4b74aa5'


class FreezeVerifierRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.record = Path(self.temp.name) / 'synthetic.json'
        self.valid = {'record_schema': 'special_session_decision_record_v1', 'protocol_sha256': SHA,
                      'decision': 'FREEZE', 'decided_by': 'Vivek',
                      'decision_ist_datetime': '2026-10-23T17:49+05:30',
                      'codex_review_reference': 'TEST_ONLY_NOT_AN_APPROVAL'}

    def tearDown(self):
        self.temp.cleanup()

    def run_record(self, obj=None, raw=None):
        self.record.write_text(raw if raw is not None else json.dumps(obj))
        return subprocess.run([sys.executable, '-I', str(CHECKER), str(PROTOCOL), str(self.record)], capture_output=True, text=True)

    def test_valid_record_and_immutable_protocol(self):
        self.assertEqual(SHA, hashlib.sha256(PROTOCOL.read_bytes()).hexdigest())
        result = self.run_record(self.valid)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('FROZEN: decision IST date 2026-10-23', result.stdout)

    def test_blank_record_is_not_frozen(self):
        result = self.run_record({})
        self.assertEqual(2, result.returncode)
        self.assertNotIn('FROZEN: decision', result.stdout)

    def test_impossible_date_is_invalid_exit_3(self):
        result = self.run_record({**self.valid, 'decision_ist_datetime': '2026-02-30T17:00+05:30'})
        self.assertEqual(3, result.returncode, result.stdout + result.stderr)

    def test_malformed_json_is_invalid_exit_3(self):
        result = self.run_record(raw='{bad json')
        self.assertEqual(3, result.returncode, result.stdout + result.stderr)

    def test_wrong_types_are_invalid_exit_3(self):
        for field, value in [('protocol_sha256', 7), ('decision_ist_datetime', []), ('decided_by', {})]:
            with self.subTest(field=field):
                result = self.run_record({**self.valid, field: value})
                self.assertEqual(3, result.returncode, result.stdout + result.stderr)

    def test_wrong_hash_schema_and_decider_rejected(self):
        for changes in [{'protocol_sha256': '0' * 64}, {'record_schema': 'wrong'}, {'decided_by': 'Other'}]:
            result = self.run_record({**self.valid, **changes})
            self.assertEqual(3, result.returncode, result.stdout + result.stderr)

    def test_same_day_boundary_is_post_hoc(self):
        result = self.run_record({**self.valid, 'decision_ist_datetime': '2026-10-28T08:00+05:30'})
        self.assertEqual(0, result.returncode)
        self.assertIn('BNF H5/H5b: first spanning entry 2026-10-28 -> RULE_POST_HOC', result.stdout)

    def test_do_not_freeze(self):
        result = self.run_record({**self.valid, 'decision': 'DO_NOT_FREEZE'})
        self.assertEqual(4, result.returncode)


if __name__ == '__main__':
    unittest.main()
