#!/usr/bin/env python3
"""Every headline count the memo and README state, checked against the committed results.

WHY THIS EXISTS. The first draft of the memo claimed the centre tilt cost CRPS in "11 of 16"
cells. The committed results say 9 of 16. Nothing in the code was wrong -- I miscounted my own
printed table by eye, and the wrong number reached a pushed deliverable and a commit message.

A number in prose that nobody recomputes is the same hazard as a hash nobody checks. So the
claims are now derived from `results/tilt_comparison_*.json` and asserted against the text of
the documents that state them. If a figure is restated, this test fails until the prose agrees
with the data -- or until the data is regenerated and the prose updated with it.

These tests skip, loudly, if the results files are absent: they check claims about committed
evidence, and without that evidence there is no claim to check.
"""
import glob
import json
import os
import re
import statistics
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(ROOT))
RESULTS = os.path.join(ROOT, 'results')
# the CURRENT memo. The 21 November results memo is superseded in part -- its economic
# forecast-arm figures were produced by the non-causal replay -- so the hashes that must
# match the files are the ones the current memo states.
MEMO = os.path.join(REPO, 'CLAUDE_CODE_REPLY_PREMIUM_CAUSAL_REPLAY_20261121.md')
SUPERSEDED_MEMO = os.path.join(
    REPO, 'CLAUDE_CODE_RESULTS_PREMIUM_MODEL_VALIDATION_20261121.md')
README = os.path.join(ROOT, 'README.md')

PRIMARY_HORIZON = 60


def load_cells():
    """[(session, index, horizon, tilt_cost, shape_value)] from every committed result."""
    cells = []
    for path in sorted(glob.glob(os.path.join(RESULTS, 'tilt_comparison_*.json'))):
        with open(path, encoding='utf-8') as fh:
            o = json.load(fh)
        for ix in sorted(o['indices']):
            for h in o['horizons_min']:
                node = o['indices'][ix].get('h%d' % h)
                if not node or not node.get('decomposition'):
                    continue
                d = node['decomposition']
                cells.append((o['session'], ix, h, d['tilt_cost_crps'],
                              d['shape_value_crps']))
    return cells


def text_of(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


class ReportedCounts(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.cells = load_cells()
        if not cls.cells:
            raise unittest.SkipTest('no results/tilt_comparison_*.json committed yet')

    def test_there_are_sixteen_scored_cells(self):
        """Four horizons x two indices x two sessions. The denominator every claim divides by."""
        self.assertEqual(len(self.cells), 16,
                         'the memo divides by 16; found %d cells' % len(self.cells))

    def test_the_shape_value_is_negative_in_every_cell(self):
        """The one result that holds everywhere, and the basis for recommending the range."""
        bad = [(s, i, h, sv) for s, i, h, _tc, sv in self.cells if sv >= 0]
        self.assertEqual(bad, [], 'the memo claims 16 of 16; these are not negative: %r' % bad)

    def test_the_stated_shape_value_range_matches_the_data(self):
        svs = [sv for *_x, sv in self.cells]
        self.assertAlmostEqual(max(svs), -1.828, places=3)      # smallest gain
        self.assertAlmostEqual(min(svs), -14.752, places=3)     # largest gain
        for claim in ('16 of 16',):
            self.assertIn(claim, text_of(MEMO))
            self.assertIn(claim, text_of(README))

    def test_the_tilt_cost_counts_are_exactly_as_stated(self):
        """THE MISCOUNT THIS TEST WAS WRITTEN FOR. 9 positive, 7 negative -- not 11 and 5."""
        tcs = [tc for *_x, tc, _sv in self.cells]
        pos = [x for x in tcs if x > 0]
        neg = [x for x in tcs if x < 0]
        self.assertEqual(len(pos), 9, 'positives: %r' % sorted(pos))
        self.assertEqual(len(neg), 7, 'negatives: %r' % sorted(neg))
        self.assertAlmostEqual(sum(pos), 23.834, places=2)
        self.assertAlmostEqual(sum(neg), -4.931, places=2)
        self.assertAlmostEqual(sum(tcs), 18.903, places=2)
        self.assertAlmostEqual(statistics.fmean(tcs), 1.181, places=2)

    def test_the_asymmetry_the_conclusion_rests_on_is_real(self):
        """The conclusion is not the cell count -- 9 of 16 is a bare majority. It is that the
        harm is an order of magnitude larger than the help."""
        tcs = [tc for *_x, tc, _sv in self.cells]
        pos = sum(x for x in tcs if x > 0)
        neg = abs(sum(x for x in tcs if x < 0))
        self.assertGreater(pos, 4 * neg,
                           'the positives must dominate for the asymmetry claim to hold')
        self.assertLess(max(abs(x) for x in tcs if x < 0), 2.0,
                        'the claim that every negative is small')

    def test_the_tilt_costs_in_three_of_the_four_primary_cells(self):
        prim = [tc for _s, _i, h, tc, _sv in self.cells if h == PRIMARY_HORIZON]
        self.assertEqual(len(prim), 4, 'four primary cells expected, got %d' % len(prim))
        self.assertEqual(sum(1 for x in prim if x > 0), 3, '%r' % sorted(prim))

    def test_the_superseded_memo_is_marked_as_such(self):
        """Its economic forecast-arm figures were produced by the non-causal replay. A reader who
        opens it must be told before reading them, not after."""
        if not os.path.exists(SUPERSEDED_MEMO):
            self.skipTest('the superseded memo is not present')
        text = text_of(SUPERSEDED_MEMO)
        self.assertIn('SUPERSEDED IN PART', text)
        self.assertIn('CLAUDE_CODE_REPLY_PREMIUM_CAUSAL_REPLAY_20261121.md', text,
                      'the banner must point at the memo that replaces it')

    def test_no_document_still_states_the_withdrawn_count(self):
        """"11 of 16" may appear only inside the sentence that withdraws it."""
        for path in (MEMO, SUPERSEDED_MEMO, README):
            if not os.path.exists(path):
                continue
            for line in text_of(path).split('\n'):
                if '11 of 16' in line:
                    self.assertIn('miscount', line,
                                  '%s restates the withdrawn count: %r'
                                  % (os.path.basename(path), line.strip()))


class ReportedEconomics(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.found = sorted(glob.glob(os.path.join(RESULTS, 'economic_check_*.json')))
        if not cls.found:
            raise unittest.SkipTest('no results/economic_check_*.json committed yet')
        cls.runs, cls.legacy = [], []
        for p in cls.found:
            with open(p, encoding='utf-8') as fh:
                o = json.load(fh)
            # A `--legacy-noncausal` run reproduces the superseded defects ON PURPOSE. It is
            # reconciliation evidence, never a result, and no claim may be read off it. Mixing
            # the two is how a deliberately defective number becomes a reported one.
            (cls.legacy if o.get('legacy_noncausal') else cls.runs).append(o)
        if not cls.runs:
            raise unittest.SkipTest('only legacy reconciliation runs are committed')

    def test_a_legacy_run_is_never_mistaken_for_a_result(self):
        for o in self.runs:
            self.assertFalse(o.get('legacy_noncausal'), o.get('session'))

    def test_the_legacy_runs_still_exhibit_the_defect_they_document(self):
        """The reconciliation is only evidence if the superseded path really misbehaves. Under
        the non-causal join a MID-CENTRED forecast buys, because the fill midpoint can sit above
        the decision ask -- which is the leak, shown rather than described."""
        if not self.legacy:
            self.skipTest('no legacy reconciliation run committed')
        bought = 0
        for o in self.legacy:
            for _ix, n in (o.get('indices') or {}).items():
                a = n['arms'].get('FORECAST_RULE_E0_TILT_0') or {}
                bought += a.get('buys', a.get('trades')) or 0
        self.assertGreater(bought, 0,
                           'the legacy path bought nothing, so it does not demonstrate the leak')

    def test_wait_is_never_beaten_in_any_cell(self):
        """The central economic claim. WAIT earns zero, so any arm beating it must earn > 0."""
        beaten = []
        for o in self.runs:
            for ix, n in (o.get('indices') or {}).items():
                for name, a in n['arms'].items():
                    if name == 'WAIT':
                        continue
                    if (a['total_net_inr'] or 0) > 0:
                        beaten.append((o['session'], ix, name, a['total_net_inr']))
        self.assertEqual(beaten, [], 'the memo claims WAIT wins everywhere: %r' % beaten)

    def test_the_mid_centred_arms_take_no_trades_anywhere(self):
        """Structural: a buy pays the ask, so a mid-centred forecast cannot predict a profit."""
        for o in self.runs:
            for ix, n in (o.get('indices') or {}).items():
                for v in ('E0_TILT_0', 'IV_ONLY', 'HIST'):
                    a = n['arms'].get('FORECAST_RULE_%s' % v)
                    if not a:
                        continue
                    # the causal v2 counts `buys`; v1 called the same thing `trades`
                    taken = a.get('buys', a.get('trades'))
                    self.assertEqual(taken, 0, '%s %s %s' % (o['session'], ix, v))

    def test_every_committed_economic_result_declares_its_tree_state(self):
        """These figures came from a patched working tree. The file must say so itself."""
        for o in self.runs:
            tree = (o.get('ledger') or {}).get('tree') or {}
            self.assertIsNotNone(tree.get('revision'),
                                 '%s records no ledger revision' % o.get('session'))
            if tree.get('modified'):
                self.assertIn('MODIFIED', tree.get('note') or '',
                              'a dirty tree must be called out in the note')

    def test_the_fee_table_covers_the_session_these_figures_were_scored_on(self):
        """If it does not, the rows failed closed and the numbers would not exist."""
        for o in self.runs:
            through = (o.get('ledger') or {}).get('rates_verified_through')
            self.assertIsNotNone(through)
            self.assertGreaterEqual(through[:10], o['session'],
                                    '%s scored past the verified fee date %s'
                                    % (o['session'], through))


class MemoIntegrity(unittest.TestCase):
    """The memo cites commits, hashes and digests. The checkable ones are checked."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(MEMO):
            raise unittest.SkipTest('the memo is not present')
        cls.text = text_of(MEMO)

    def test_the_frozen_module_still_hashes_to_the_value_the_memo_cites(self):
        import hashlib
        p = os.path.join(REPO, 'tools', 'spot_path_baseline', 'premium_repricing_v2.py')
        if not os.path.exists(p):
            self.skipTest('the frozen module is not in this tree')
        with open(p, 'rb') as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        self.assertIn(digest, self.text,
                      'the memo cites a stale hash for the frozen module; it is now %s' % digest)

    def test_the_memo_hashes_match_the_files_it_names(self):
        import hashlib
        for name in ('tilt_compare.py', 'economic_check.py', 'PREDECLARATION_20261121.md'):
            p = os.path.join(ROOT, name)
            if not os.path.exists(p):
                continue
            with open(p, 'rb') as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            self.assertIn(digest, self.text,
                          'the memo cites a stale hash for %s; it is now %s' % (name, digest))

    def test_every_sha256_the_memo_prints_is_well_formed(self):
        for m in re.findall(r'`([0-9a-f]{64})`', self.text):
            self.assertEqual(len(m), 64)
        self.assertGreaterEqual(len(re.findall(r'`[0-9a-f]{64}`', self.text)), 6,
                                'the memo should carry the input and code hashes in full')


if __name__ == '__main__':
    unittest.main(verbosity=2)
