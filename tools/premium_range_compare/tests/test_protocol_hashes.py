"""The protocol's claimed source hashes must match the files it governs.

THE DEFECT THIS CLOSES.  The v3 draft listed `19ce42fa…` for `stopping_rule.py` while the
committed file hashed to `8b89535a…`: I had added the `ADOPTED_*` constants after writing the
draft and refreshed only `range_compare.py`'s hash. Codex caught it by comparing the draft, the
file and the input manifest. A hash that nobody checks is decoration, so it is checked here, and
the three places that carry hashes -- the protocol, the manifest and the files themselves -- are
required to agree.

This also covers the manifest's `protocol.sha256`, which is the one hash the protocol cannot
state about itself.
"""
import hashlib
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(TOOL))

PROTOCOL = os.path.join(TOOL, 'PROTOCOL_RANGE_TRIAL_DRAFT_v4.md')
MANIFEST = os.path.join(TOOL, 'INPUT_MANIFEST_20261117.json')

#: Every file the protocol claims a hash for, as it writes the path.
GOVERNED = {
    'tools/premium_range_compare/range_compare.py':
        os.path.join(TOOL, 'range_compare.py'),
    'tools/premium_range_compare/planned_schedule.py':
        os.path.join(TOOL, 'planned_schedule.py'),
    'tools/premium_range_compare/stopping_rule.py':
        os.path.join(TOOL, 'stopping_rule.py'),
    'tools/premium_range_compare/frozen_successor.py':
        os.path.join(TOOL, 'frozen_successor.py'),
    'tools/spot_path_baseline/premium_repricing_v2.py':
        os.path.join(ROOT, 'tools', 'spot_path_baseline', 'premium_repricing_v2.py'),
}

_ROW = re.compile(r'^\|\s*`([^`]+?)`[^|]*\|\s*`([0-9a-f]{64})`\s*\|', re.M)


def sha256(path):
    with open(path, 'rb') as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def protocol_rows():
    """{path: claimed hash} from the protocol's governed-files table."""
    with open(PROTOCOL, encoding='utf-8') as fh:
        text = fh.read()
    head = text.split('## 0 ·')[0]                 # the table sits above the changelog
    return {m.group(1): m.group(2) for m in _ROW.finditer(head)}


class ProtocolHashes(unittest.TestCase):

    def test_the_protocol_exists_and_names_every_governed_file(self):
        self.assertTrue(os.path.isfile(PROTOCOL), PROTOCOL)
        claimed = protocol_rows()
        self.assertEqual(set(GOVERNED), set(claimed),
                         'the governed-files table must list exactly the governed files')

    def test_every_claimed_hash_matches_the_file(self):
        for path, claim in sorted(protocol_rows().items()):
            with self.subTest(path=path):
                self.assertEqual(sha256(GOVERNED[path]), claim,
                                 '%s: the protocol claims %s' % (path, claim[:16]))

    def test_the_manifest_agrees_with_the_protocol(self):
        with open(MANIFEST, encoding='utf-8') as fh:
            man = json.load(fh)
        claimed = protocol_rows()
        gov = man['governed_sources']
        # the manifest keys the local modules by basename and the frozen R7 one by path
        for path, claim in sorted(claimed.items()):
            key = path if path in gov else os.path.basename(path)
            self.assertIn(key, gov, path)
            self.assertEqual(claim, gov[key], path)

    def test_the_manifest_states_the_protocol_own_hash(self):
        """The one hash the protocol cannot state about itself."""
        with open(MANIFEST, encoding='utf-8') as fh:
            man = json.load(fh)
        self.assertEqual(os.path.basename(PROTOCOL), man['protocol']['file'])
        self.assertEqual(sha256(PROTOCOL), man['protocol']['sha256'])

    def test_the_frozen_r7_module_is_the_one_the_branch_was_cut_from(self):
        """Its hash is the base commit's. If this changes, the frozen replay has been touched."""
        self.assertEqual('95e53e25d24ae0369f2c8d8ab63026a0cda499443ddae4041c6ee7ffb373233a',
                         sha256(GOVERNED['tools/spot_path_baseline/premium_repricing_v2.py']))

    def test_the_versions_the_protocol_names_are_the_versions_in_the_code(self):
        sys.path.insert(0, TOOL)
        import planned_schedule as PS
        import range_compare as RC
        import stopping_rule as SR
        with open(PROTOCOL, encoding='utf-8') as fh:
            text = fh.read()
        for version in (RC.COMPARISON_VERSION, PS.SCHEDULE_VERSION,
                        SR.STOPPING_RULE_VERSION, PS.CAPTURE_WINDOW_RULES_VERSION,
                        PS.SESSION_CALENDAR_VERSION):
            self.assertIn(version, text, version)


if __name__ == '__main__':
    unittest.main()
