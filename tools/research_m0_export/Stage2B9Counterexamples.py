"""Independent B9 fail-closed regressions. Run with --tool-dir PATH (contains verifier and fixtures)."""
from pathlib import Path
import argparse
import copy
import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument('--tool-dir', type=Path, required=True)
args, rest = parser.parse_known_args()
tool = args.tool_dir.resolve()
sys.path.insert(0, str(tool))
import verify_export as V

fixture = tool / 'fixtures/export_6polls.zip'


def inputs():
    with zipfile.ZipFile(fixture) as z:
        manifest = json.loads(json.loads(z.read('research_m0/manifest_2026-10-22.json'))['body'])
    attempt = manifest['indices']['NF']['slots']['1']['attempts'][0]
    rows = []
    for strike in (100, 200):
        for typ in ('CE', 'PE'):
            rows.append({'id': 1000 + len(rows), 'poll_ts': attempt['capture']['poll_ts'], 'index_key': 'NF',
                         'expiry': '2026-10-27', 'strike': strike, 'option_type': typ,
                         'bid': 9.5, 'ask': 10.5, 'ltp': 10})
    return attempt, rows


class Stage2B9Counterexamples(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.attempt, self.rows = inputs()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, obj):
        p = self.root / name
        p.write_text(json.dumps(obj))
        return str(p)

    def b9(self, archive, rows1, rows2=None):
        return V.b9(str(archive), 'NF|2026-10-22|1', self.attempt['attempt_id'],
                    self.write('first.json', rows1), self.write('second.json', rows2) if rows2 is not None else None)

    def test_b9_rejects_an_export_that_fails_its_own_inventory_verification(self):
        altered = self.root / 'tampered.zip'
        with zipfile.ZipFile(fixture) as src, zipfile.ZipFile(altered, 'w') as dst:
            for name in src.namelist():
                data = src.read(name)
                if name == 'research_m0/seq.json':
                    data += b' '  # valid JSON still, but exact bytes and SHA256SUMS no longer agree
                dst.writestr(name, data)
        self.assertEqual('INVALID', V.verify(str(altered))['result'])
        try:
            result = self.b9(altered, self.rows, list(reversed(self.rows)))
        except V.Invalid:
            return
        self.assertNotEqual('MATCH', result['result'], json.dumps(result))

    def test_independent_readback_rejects_an_extra_row_from_a_different_poll(self):
        extra = copy.deepcopy(self.rows[0])
        extra['id'] = 9000
        extra['poll_ts'] = '2026-10-22T09:20:05Z'
        result = self.b9(fixture, self.rows, self.rows + [extra])
        self.assertTrue(result['findings'])
        self.assertNotEqual('MATCH', result['result'], json.dumps(result))


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]] + rest)
