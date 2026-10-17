"""b510 R2: the X1 offline report blocks on any integrity/protocol failure BEFORE scoring.

Each case starts from a valid, complete fixture (60 resolved NF primaries, all controls) that the
report scores normally, then breaks exactly one thing. JSON, Markdown and CLI behaviour are checked.
"""
import copy
import hashlib
import io
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(HERE, '..', '..', '..', '..', '..', 'tools'))
sys.path.insert(0, HERE)

import paper_buying_research as pbr  # noqa: E402
import x1_report  # noqa: E402
from x1_report_fixtures import journal, row  # noqa: E402

BLOCKED = 'BLOCKED_INTEGRITY_FAILURE'


def valid():
    return journal({'NF': {'resolved': 60, 'controls': 60, 'unresolved': 2},
                    'BNF': {'resolved': 10, 'controls': 10}})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, obj, name='in.json', raw=None):
        p = os.path.join(self.tmp, name)
        with open(p, 'w') as fh:
            fh.write(raw if raw is not None else json.dumps(obj))
        return p

    def export(self, st, sha='auto', status='OK'):
        text = json.dumps(st)
        env = {'schema': 'x1_export_v1', 'status': status, 'journal_text': text}
        if sha == 'auto':
            env['journal_sha256'] = hashlib.sha256(text.encode()).hexdigest()
        elif sha is not None:
            env['journal_sha256'] = sha
        return env

    def cli(self, path):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = x1_report.main([path, '--json', os.path.join(self.tmp, 'out.json')])
        with open(os.path.join(self.tmp, 'out.json')) as fh:
            return code, buf.getvalue(), json.load(fh)

    def assert_blocked(self, rep, failure):
        self.assertTrue(rep['blocked'], rep.get('blocking_failures'))
        self.assertIn(failure, rep['blocking_failures'])
        self.assertEqual(rep['overall_verdict'], BLOCKED)
        for ix in pbr.INDICES:
            self.assertEqual(rep['indices'][ix]['verdict'], BLOCKED)
            self.assertNotIn('primary', rep['indices'][ix])               # nothing scored
        self.assertFalse(rep['trading_authority'])

    def broken(self, mutate, failure):
        st = valid()
        mutate(st)
        self.assert_blocked(x1_report.report(st), failure)
        code, md, js = self.cli(self.write(st))
        self.assertEqual(code, 2)
        self.assertIn('**BLOCKED — %s' % BLOCKED, md)
        self.assertIn(failure, md)
        self.assertNotIn('CRITERIA_MET', md)
        self.assert_blocked(js, failure)


class ValidFixture(Base):
    def test_valid_complete_fixture_is_scored_deterministically(self):
        st = valid()
        a = x1_report.report(st)
        self.assertFalse(a['blocked'])
        self.assertEqual(a['blocking_failures'], [])
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(x1_report.report(copy.deepcopy(st)), sort_keys=True))
        self.assertEqual(a['indices']['NF']['primary']['resolved'], 60)
        self.assertEqual(a['indices']['BNF']['verdict'], 'INSUFFICIENT_EVIDENCE')
        code, md, js = self.cli(self.write(st))
        self.assertEqual(code, 0)
        self.assertNotIn('BLOCKED', md)
        self.assertEqual(js['integrity']['hash_check'], 'NOT_APPLICABLE_RAW_JOURNAL')    # raw journal, no hash

    def test_export_with_correct_hash_is_scored(self):
        code, md, js = self.cli(self.write(self.export(valid())))
        self.assertEqual((code, js['integrity']['input_kind'], js['integrity']['hash_check']), (0, 'EXPORT', 'OK'))
        self.assertIn('hash check OK', md)


class ExportIntegrity(Base):
    def run_export(self, env, failure):
        code, md, js = self.cli(self.write(env))
        self.assertEqual(code, 2)
        self.assert_blocked(js, failure)
        self.assertIn(failure, md)

    def test_wrong_hash_blocks(self):
        self.run_export(self.export(valid(), sha='0' * 64), 'EXPORT_HASH_MISMATCH')

    def test_missing_or_malformed_hash_blocks(self):
        self.run_export(self.export(valid(), sha=None), 'EXPORT_HASH_MISSING')
        self.run_export(self.export(valid(), sha='abc'), 'EXPORT_HASH_MISSING')

    def test_tampered_text_blocks(self):
        env = self.export(valid())
        env['journal_text'] = env['journal_text'].replace('"net": 4', '"net": 9', 1)
        self.run_export(env, 'EXPORT_HASH_MISMATCH')

    def test_export_status_not_ok_blocks(self):
        self.run_export(self.export(valid(), status='JOURNAL_SIZE_LIMIT'), 'EXPORT_STATUS_NOT_OK')

    def test_unparseable_inputs_block(self):
        code, md, _ = self.cli(self.write(None, raw='{not json'))
        self.assertEqual(code, 2)
        self.assertIn('INPUT_UNPARSEABLE', md)
        env = self.export(valid())
        env['journal_text'] = '{bad'
        env['journal_sha256'] = hashlib.sha256(b'{bad').hexdigest()
        self.run_export(env, 'JOURNAL_UNPARSEABLE')

    def test_caller_reported_false_hash_blocks_direct_report(self):
        self.assert_blocked(x1_report.report(valid(), {'journal_sha256_ok': False}), 'EXPORT_HASH_MISMATCH')


class ProtocolAndContract(Base):
    def test_wrong_digest(self):
        self.broken(lambda st: st.update(protocol_digest='f' * 64), 'PROTOCOL_DIGEST_MISMATCH')

    def test_wrong_schema(self):
        self.broken(lambda st: st.update(schema='x1_journal_v0'), 'JOURNAL_SCHEMA_MISMATCH')

    def test_wrong_protocol_id(self):
        self.broken(lambda st: st.update(protocol_id='X1_other'), 'PROTOCOL_ID_MISMATCH')

    def test_wrong_block_contract(self):
        self.broken(lambda st: st['block'].update(collector_contract_version='x1_collector_contract_v1_20261017'),
                    'BLOCK_CONTRACT_MISMATCH')
        self.broken(lambda st: st['block'].update(fee_contract_version='other'), 'BLOCK_CONTRACT_MISMATCH')
        self.broken(lambda st: st['block'].update(imported_records=5), 'BLOCK_CONTRACT_MISMATCH')

    def test_corruption_stop_blocks(self):
        for reason in ('JOURNAL_CORRUPT', 'PROTOCOL_DIGEST_MISMATCH', 'SOMETHING_ELSE'):
            self.broken(lambda st: st.update(stopped={'reason': reason, 'at_ms': 1}), 'CORRUPTION_OR_CONTRACT_STOP')

    def test_capacity_stop_is_descriptive_not_blocking(self):
        for reason in ('JOURNAL_FULL_REVIEW_REQUIRED', 'JOURNAL_SIZE_LIMIT'):
            st = valid()
            st['stopped'] = {'reason': reason, 'at_ms': 1}
            rep = x1_report.report(st)
            self.assertFalse(rep['blocked'])
            self.assertEqual(rep['capacity_stop'], reason)
            self.assertTrue(rep['indices']['NF']['capacity_stop_endpoint_reviewable'])
            self.assertFalse(rep['indices']['BNF']['capacity_stop_endpoint_reviewable'])
            self.assertIn('Capacity stop', rep['notes'][0])


class Records(Base):
    def first(self, st, **match):
        return next(r for r in st['records'] if all(r.get(k) == v for k, v in match.items()))

    def test_duplicate_record(self):
        self.broken(lambda st: st['records'].append(copy.deepcopy(st['records'][0])), 'DUPLICATE_RECORD_ID')

    def test_second_primary_same_session(self):
        def dup(st):
            r = copy.deepcopy(self.first(st, index='NF', arm='primary'))
            r['id'] = r['id'] + 'x'
            st['records'].append(r)
        self.broken(dup, 'INVALID_RECORD')

    def test_invalid_rows(self):
        self.broken(lambda st: self.first(st, index='NF').update(index='FIN'), 'INVALID_RECORD')
        self.broken(lambda st: self.first(st, index='NF').update(status='DONE'), 'INVALID_RECORD')
        self.broken(lambda st: self.first(st, index='NF').update(session_date='2026-13-01'), 'INVALID_RECORD')
        self.broken(lambda st: st['records'].append('junk'), 'INVALID_RECORD')
        self.broken(lambda st: self.first(st, status='UNRESOLVED').update(reason='WHATEVER'), 'INVALID_RECORD')

    def test_non_finite_or_missing_economics(self):
        for k, v in (('net', float('nan')), ('net', float('inf')), ('gross', None), ('exit_bid', 0.0),
                     ('contract_lot_size', 0), ('fees', '50')):
            self.broken(lambda st: self.first(st, status='RESOLVED').update({k: v}), 'NON_FINITE_OR_MISSING_ECONOMICS')

    def test_nan_in_file_blocks_via_cli(self):
        st = valid()
        self.first(st, status='RESOLVED')['net'] = float('nan')
        code, md, _ = self.cli(self.write(None, raw=json.dumps(st)))          # json emits NaN literal
        self.assertEqual(code, 2)
        self.assertIn('NON_FINITE_OR_MISSING_ECONOMICS', md)

    def test_inconsistent_economics(self):
        self.broken(lambda st: self.first(st, status='RESOLVED').update(net=99999.0), 'INCONSISTENT_ECONOMICS')
        self.broken(lambda st: self.first(st, status='RESOLVED').update(exit_bid=500.0), 'INCONSISTENT_ECONOMICS')

    def test_exit_timing_rule_violation(self):
        def early(st):
            r = self.first(st, status='RESOLVED', arm='primary')
            r['exit_quote_ts_ms'] = r['entry_decision_ts_ms'] + 29 * 60_000
        self.broken(early, 'EXIT_TIMING_RULE_VIOLATION')

        def late(st):
            r = self.first(st, status='RESOLVED', arm='delayed')
            r['exit_decision_ts_ms'] = r['entry_decision_ts_ms'] + 40 * 60_000 + 1
        self.broken(late, 'EXIT_TIMING_RULE_VIOLATION')

    def test_pairing_problems(self):
        self.broken(lambda st: self.first(st, arm='delayed', status='RESOLVED').update(primary_id='nope'),
                    'PAIRING_INVALID')
        self.broken(lambda st: self.first(st, arm='delayed', status='RESOLVED').update(strike=1.0), 'PAIRING_INVALID')
        self.broken(lambda st: self.first(st, arm='delayed', status='RESOLVED').update(
            instrument_key='NSE_FO|other'), 'PAIRING_INVALID')

        def orphan(st):
            st['records'] = [r for r in st['records'] if r['id'] != self.first(st, arm='delayed')['primary_id']]
        self.broken(orphan, 'PAIRING_INVALID')

    def test_more_than_sixty_resolved_blocks(self):
        def extra(st):
            st['records'].append(row('NF', '2026-12-01', 'primary'))
        self.broken(extra, 'ENDPOINT_EXCEEDED')


if __name__ == '__main__':
    unittest.main()
