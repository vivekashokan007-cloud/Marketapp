"""b511 R2: the week-one X1 procedure is counts-only (tools/x1_week1_counts.py), never x1_report.py."""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..', '..', '..'))
TOOL = os.path.join(ROOT, 'tools', 'x1_week1_counts.py')
REPORT = os.path.join(ROOT, 'tools', 'x1_report.py')
APPENDIX = os.path.join(ROOT, 'docs', 'research', 'X1_OPERATIONAL_APPENDIX_20261018.md')

import paper_buying_research as pbr  # noqa: E402
import x1_report_fixtures as FX  # noqa: E402

MONEY_KEYS = ('entry_ask', 'exit_bid', 'gross', 'net', 'fees', 'entry_fee')
PROHIBITED_KEY_RE = re.compile(r'"(premium|entry_ask|exit_bid|gross|net|fees|entry_fee|mean[a-z_]*|median|sd|ci[a-z_]*|'
                               r'bootstrap[a-z_]*|verdict|endpoint[a-z_]*|p_value|forward|straddle|gap[a-z_]*|strike|side)"\s*:')
PROHIBITED_TEXT = ('INSUFFICIENT_EVIDENCE', 'bootstrap', 'confidence', 'verdict')


def pre_endpoint_journal():
    # One resolved primary/control pair (the shape Codex used) plus unresolved/skip/open rows.
    return FX.journal({'NF': {'resolved': 1, 'controls': 1, 'unresolved': 1},
                       'BNF': {'resolved': 1, 'controls': 0, 'open': 1}})


def export(journal, tamper=False):
    text = json.dumps(journal, sort_keys=True)
    sha = hashlib.sha256(text.encode()).hexdigest()
    return {'schema': 'x1_export_v1', 'status': 'OK', 'journal_text': text,
            'journal_sha256': ('0' * 64) if tamper else sha}


def numbers(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from numbers(v)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        yield obj


class Week1CountsOnly(unittest.TestCase):
    def run_tool(self, tool, doc, *extra):
        d = tempfile.mkdtemp()
        src, out = os.path.join(d, 'export.json'), os.path.join(d, 'out.json')
        with open(src, 'w') as fh:
            json.dump(doc, fh)
        p = subprocess.run([sys.executable, tool, src, '--json', out, *extra], capture_output=True, text=True)
        saved = open(out).read() if os.path.exists(out) else ''
        return p, saved

    def assert_no_results(self, text, money):
        self.assertIsNone(PROHIBITED_KEY_RE.search(text), PROHIBITED_KEY_RE.search(text))
        for token in PROHIBITED_TEXT:
            self.assertNotIn(token, text)
        doc = json.loads(text)
        leaked = set(numbers(doc)) & money
        self.assertFalse(leaked, 'monetary values emitted: %s' % sorted(leaked))

    def test_prescribed_procedure_emits_counts_only_on_pre_endpoint_export(self):
        j = pre_endpoint_journal()
        money = {r[k] for r in j['records'] for k in MONEY_KEYS if k in r}
        self.assertTrue(money)
        p, saved = self.run_tool(TOOL, export(j))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), saved.strip())       # stdout and saved file are the same document
        for text in (p.stdout, saved):
            self.assert_no_results(text, money)
        doc = json.loads(saved)
        self.assertTrue(doc['integrity_ok'] and doc['counts_only'] and doc['journal']['protocol_digest_ok'])
        self.assertEqual(doc['integrity']['hash_check'], 'OK')
        nf = {(d['session_date'], d['index']): d for d in doc['days']}
        resolved = [r for r in j['records'] if r['status'] == 'RESOLVED' and r['arm'] == 'primary']
        for r in resolved:
            arm = nf[(r['session_date'], r['index'])]['arms']['primary']
            self.assertEqual(arm['hold_ms_from_entry_decision'], r['exit_quote_ts_ms'] - r['entry_decision_ts_ms'])
            self.assertEqual(arm['status'], 'RESOLVED')
        self.assertEqual(doc['status_counts']['NF']['primary'], {'resolved': 1, 'unresolved': 1})
        self.assertEqual(doc['status_counts']['BNF']['primary'], {'resolved': 1, 'open': 1})
        unresolved = [d for d in doc['days'] if d['arms'].get('primary', {}).get('status') == 'UNRESOLVED']
        self.assertEqual(unresolved[0]['failure_counts']['window'], 2)   # EXIT_QUOTE_DEADLINE + CONTROL_WINDOW_MISSED

    def test_endpoint_report_on_same_input_does_expose_running_statistics(self):
        # Sensitivity: the check above would catch what x1_report.py emits before the endpoint (Codex R2).
        p, saved = self.run_tool(REPORT, export(pre_endpoint_journal()))
        self.assertIsNotNone(PROHIBITED_KEY_RE.search(p.stdout + saved))

    def test_integrity_failure_emits_codes_only(self):
        j = pre_endpoint_journal()
        money = {r[k] for r in j['records'] for k in MONEY_KEYS if k in r}
        p, saved = self.run_tool(TOOL, export(j, tamper=True))
        self.assertEqual(p.returncode, 2)
        doc = json.loads(saved)
        self.assertFalse(doc['integrity_ok'])
        self.assertIn('EXPORT_HASH_MISMATCH', doc['integrity_failures'])
        self.assertNotIn('days', doc)
        self.assert_no_results(saved, money)

    def test_appendix_week_one_procedure_uses_counts_path_not_endpoint_report(self):
        text = open(APPENDIX).read()
        sec = text.split('## C.')[1].split('## D.')[0]
        self.assertIn('tools/x1_week1_counts.py', sec)
        self.assertNotIn('x1_report.py EXPORT', sec)
        self.assertEqual(pbr.DIGEST, '10a6b086f1ceefbccd6d6f8a87dacda5eca29818b1c2a999c649bbdb6dad0daa')


if __name__ == '__main__':
    unittest.main()
