"""The scratch suite must preserve a dated fixture's FAILED freshness receipt."""
import importlib.util
from pathlib import Path
import unittest


class SeedSuiteFixtureAge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[5] / 'tools/ev_db_suite/seed_runner_suite.py'
        spec = importlib.util.spec_from_file_location('scratch_seed_fixture_age', path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_only_the_real_stale_rejection_with_otherwise_verified_protocol_is_expected(self):
        receipt = dict(status='FAILED', reason='READBACK_MISMATCH', ack_status='SEEDED',
                       detail={'failed': ['phone_validator']},
                       _protocol_evidence={'phone_validator_status': 'NO_CALIBRATION_STALE'})
        self.assertTrue(self.module.protocol_ok(7, receipt, 'SEEDED'))
        self.assertEqual(receipt['status'], 'FAILED')
        for failed in [[], ['payload_digest_recomputed'], ['phone_validator', 'observation_set_exact']]:
            self.assertFalse(self.module.protocol_ok(7, dict(receipt, detail={'failed': failed}), 'SEEDED'))
        for code in [0, 4, 6]:
            self.assertFalse(self.module.protocol_ok(code, receipt, 'SEEDED'))
        self.assertFalse(self.module.protocol_ok(7, dict(receipt, _protocol_evidence={
            'phone_validator_status': 'NO_CALIBRATION_NOT_YET_AVAILABLE'}), 'SEEDED'))

    def test_success_requires_real_checks_and_exact_ack_and_reconciliation_remains_distinct(self):
        self.assertFalse(self.module.protocol_ok(0, {'status': 'SEEDED'}, 'SEEDED'))
        self.assertTrue(self.module.protocol_ok(0, {'status': 'SEEDED', 'checks': {'a': True}}, 'SEEDED'))
        stale = dict(status='FAILED', reason='EXISTING_STATE_INCOMPATIBLE', phase='reconcile',
                     detail={'failed': ['phone_validator']}, _protocol_evidence={'phone_validator_status': 'NO_CALIBRATION_STALE'})
        self.assertTrue(self.module.protocol_ok(4, stale, 'ALREADY_SEEDED_CONSISTENT'))
        self.assertFalse(self.module.protocol_ok(4, stale, 'SEEDED'))


if __name__ == '__main__': unittest.main()
