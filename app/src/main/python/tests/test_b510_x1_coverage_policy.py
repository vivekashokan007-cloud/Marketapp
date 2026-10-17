"""b510 R3: frozen coverage policy (x1_coverage_policy_v1_20261017) is in the protocol digest and
enforced by the report: paired controls >= 80% of resolved primaries, unresolved primaries <= 20% of
attempted primaries, no OPEN rows at review; per index. Chosen before any data existed."""
import copy
import hashlib
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(HERE, '..', '..', '..', '..', '..', 'tools'))
sys.path.insert(0, HERE)

import paper_buying_research as pbr  # noqa: E402
import x1_report  # noqa: E402
from x1_report_fixtures import journal, row  # noqa: E402

MET, COV = 'CRITERIA_MET_REVIEW_REQUIRED', 'INSUFFICIENT_COVERAGE'


def verdict(spec, ix='NF'):
    rep = x1_report.report(journal(spec))
    assert not rep['blocked'], rep['blocking_failures']
    return rep['indices'][ix]


class PolicyFrozenInProtocol(unittest.TestCase):
    def test_policy_values_and_digest_binding(self):
        pol = pbr.PROTOCOL['coverage_policy']
        self.assertEqual((pol['version'], pol['paired_control_min_pct'], pol['unresolved_primary_max_pct']),
                         ('x1_coverage_policy_v1_20261017', 80, 20))
        self.assertTrue(pol['frozen_before_any_data'])
        changed = copy.deepcopy(pbr.PROTOCOL)
        changed['coverage_policy']['paired_control_min_pct'] = 79
        self.assertNotEqual(hashlib.sha256(json.dumps(changed, sort_keys=True, separators=(',', ':')).encode())
                            .hexdigest(), pbr.DIGEST)


class PairedControlCoverage(unittest.TestCase):
    def test_one_of_sixty_cannot_pass(self):
        r = verdict({'NF': {'resolved': 60, 'controls': 1}})
        self.assertEqual((r['verdict'], r['verdict_reason']), (COV, 'PAIRED_CONTROL_COVERAGE'))
        self.assertEqual((r['coverage']['paired_control']['numerator'],
                          r['coverage']['paired_control']['denominator']), (1, 60))

    def test_below_at_above_threshold(self):
        self.assertEqual(verdict({'NF': {'resolved': 60, 'controls': 47}})['verdict'], COV)     # 78.3%
        self.assertEqual(verdict({'NF': {'resolved': 60, 'controls': 48}})['verdict'], MET)     # exactly 80%
        self.assertEqual(verdict({'NF': {'resolved': 60, 'controls': 49}})['verdict'], MET)
        self.assertEqual(verdict({'NF': {'resolved': 60, 'controls': 0}})['verdict'], COV)

    def test_unresolved_controls_do_not_count_as_paired(self):
        st = journal({'NF': {'resolved': 60, 'controls': 60}})
        n = 0
        for i, r in enumerate(st['records']):
            if r['arm'] == 'delayed' and n < 13:
                st['records'][i] = row('NF', r['session_date'], 'delayed', status='UNRESOLVED')
                n += 1
        rep = x1_report.report(st)
        self.assertFalse(rep['blocked'])
        nf = rep['indices']['NF']
        self.assertEqual(nf['coverage']['paired_control']['numerator'], 47)
        self.assertEqual(nf['verdict'], COV)


class UnresolvedPrimaryShare(unittest.TestCase):
    def test_below_at_above_threshold(self):
        r = verdict({'NF': {'resolved': 60, 'controls': 60, 'unresolved': 15}})              # 15/75 = 20%
        self.assertEqual(r['verdict'], MET)
        self.assertEqual((r['coverage']['unresolved_primary']['numerator'],
                          r['coverage']['unresolved_primary']['denominator']), (15, 75))
        self.assertEqual(verdict({'NF': {'resolved': 60, 'controls': 60, 'unresolved': 14}})['verdict'], MET)
        r = verdict({'NF': {'resolved': 60, 'controls': 60, 'unresolved': 16}})              # 16/76 > 20%
        self.assertEqual((r['verdict'], r['verdict_reason']), (COV, 'UNRESOLVED_PRIMARY_SHARE'))

    def test_both_failures_reported(self):
        r = verdict({'NF': {'resolved': 60, 'controls': 10, 'unresolved': 30}})
        self.assertEqual(r['verdict_reason'], 'PAIRED_CONTROL_COVERAGE,UNRESOLVED_PRIMARY_SHARE')


class CohortsAndOpenRows(unittest.TestCase):
    def test_each_cohort_judged_on_its_own_numbers(self):
        rep = x1_report.report(journal({'NF': {'resolved': 60, 'controls': 60},
                                        'BNF': {'resolved': 60, 'controls': 40}}))
        self.assertEqual(rep['indices']['NF']['verdict'], MET)
        self.assertEqual(rep['indices']['BNF']['verdict'], COV)
        rep = x1_report.report(journal({'NF': {'resolved': 60, 'controls': 40},
                                        'BNF': {'resolved': 60, 'controls': 60, 'unresolved': 15}}))
        self.assertEqual(rep['indices']['NF']['verdict'], COV)
        self.assertEqual(rep['indices']['BNF']['verdict'], MET)

    def test_open_rows_block_review(self):
        r = verdict({'NF': {'resolved': 60, 'controls': 60, 'open': 1}})
        self.assertEqual((r['verdict'], r['verdict_reason']), ('INSUFFICIENT_EVIDENCE', 'OPEN_ROWS_PENDING'))

    def test_coverage_never_overrides_negative_criteria_or_endpoint(self):
        r = verdict({'NF': {'resolved': 60, 'controls': 60, 'net': lambda i: -100.0}})
        self.assertEqual(r['verdict'], 'CRITERIA_NOT_MET')
        r = verdict({'NF': {'resolved': 59, 'controls': 59}})
        self.assertEqual((r['verdict'], r['verdict_reason']), ('INSUFFICIENT_EVIDENCE', 'ENDPOINT_NOT_REACHED'))

    def test_markdown_and_json_export_policy_and_fractions(self):
        rep = x1_report.report(journal({'NF': {'resolved': 60, 'controls': 48, 'unresolved': 3}}))
        md = x1_report.markdown(rep)
        self.assertIn('coverage policy `x1_coverage_policy_v1_20261017`: paired control 48/60 (>= 80%: True); '
                      'unresolved primary 3/63 (<= 20%: True)', md)
        js = json.loads(json.dumps(rep))
        self.assertEqual(js['indices']['NF']['coverage']['policy_version'], 'x1_coverage_policy_v1_20261017')


if __name__ == '__main__':
    unittest.main()
