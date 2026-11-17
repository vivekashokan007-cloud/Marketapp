"""The documented commands must actually run, in the form the README documents them.

THE DEFECT THIS CLOSES.  The README documents `python3 -I tools/premium_range_compare/…`, and
that is the form the environment's own guidance prescribes for a tool that reads supplied data.
It failed with `ModuleNotFoundError: No module named 'planned_schedule'` while ordinary `python3`
worked, because isolated mode drops the script's own directory from `sys.path` and the sibling
import had been relying on it. A unit test that imports the modules directly cannot catch that:
by then the path is already set up. So these run the real commands in a subprocess, isolated,
from the repository root, exactly as documented.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(TOOL))

REAL_EXPORT = os.environ.get('RANGE_EXPORT')
REAL_SPOTS = os.environ.get('RANGE_SPOTS')

#: Every command the README documents, as a path relative to the repository root.
SCRIPTS = ('tools/premium_range_compare/range_compare.py',
           'tools/premium_range_compare/planned_schedule.py',
           'tools/premium_range_compare/stopping_rule.py',
           'tools/premium_range_compare/results/regressions_20261117.py')


def isolated(args, cwd=ROOT):
    """Run `python3 -I <args>` from the repository root and return the completed process."""
    return subprocess.run([sys.executable, '-I'] + list(args), cwd=cwd,
                          capture_output=True, text=True, timeout=600)


class IsolatedImports(unittest.TestCase):
    """These need no inputs: a sibling-import failure shows up at `--help`."""

    def test_every_documented_script_runs_isolated_with_help(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                p = isolated([script, '--help'])
                self.assertEqual(0, p.returncode,
                                 '%s failed isolated:\n%s' % (script, p.stderr))
                self.assertNotIn('ModuleNotFoundError', p.stderr)
                self.assertIn('usage:', p.stdout)

    def test_the_sibling_import_is_resolved_explicitly_not_by_luck(self):
        """`import planned_schedule` must work with the script directory NOT on sys.path."""
        p = isolated(['-c',
                      'import sys, os; sys.path.insert(0, %r); '
                      'import range_compare; '
                      'print(range_compare.PS.SCHEDULE_VERSION)' % TOOL])
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertIn('premium_range_planned_schedule', p.stdout)

    def test_the_stopping_rule_prints_its_levels_isolated(self):
        p = isolated(['tools/premium_range_compare/stopping_rule.py'])
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertIn('17.19%', p.stdout)
        self.assertIn('18.91%', p.stdout)
        self.assertIn('ONE FIXED DECISION POINT', p.stdout)


@unittest.skipUnless(REAL_EXPORT and REAL_SPOTS,
                     'set RANGE_EXPORT and RANGE_SPOTS to the real inputs')
class IsolatedWithRealInputs(unittest.TestCase):

    def has_data(self):
        with open(REAL_EXPORT, encoding='utf-8') as fh:
            doc = json.load(fh)
        node = (doc.get('premium') or {}).get('indices') or {}
        return any((node[ix].get('records') or []) for ix in node)

    def test_the_documented_comparison_command_runs_isolated(self):
        out = tempfile.mkdtemp()
        p = isolated(['tools/premium_range_compare/range_compare.py',
                      REAL_EXPORT, REAL_SPOTS, '--horizons', '60', '--out', out])
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertNotIn('ModuleNotFoundError', p.stderr)
        self.assertIn('NEUTRAL PREMIUM RANGE COMPARISON', p.stdout)
        if self.has_data():
            self.assertIn('planned', p.stdout)
        with open(os.path.join(out, 'range_comparison.json'), encoding='utf-8') as fh:
            doc = json.load(fh)
        self.assertIn('across_sessions', doc)
        self.assertIn('coverage_denominator', doc)

    def test_the_documented_schedule_command_runs_isolated(self):
        p = isolated(['tools/premium_range_compare/planned_schedule.py', REAL_EXPORT])
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertNotIn('ModuleNotFoundError', p.stderr)
        self.assertIn('planned', p.stdout)
        if self.has_data():
            self.assertIn('VERIFIED', p.stdout)

    def test_the_documented_regression_command_runs_isolated(self):
        if not self.has_data():
            self.skipTest('the regression script compares scored rows; this export has none')
        p = isolated(['tools/premium_range_compare/results/regressions_20261117.py',
                      REAL_EXPORT, REAL_SPOTS])
        self.assertEqual(0, p.returncode, p.stderr)
        for marker in ('COVERAGE IS COUNTED AGAINST THE PLANNED SCHEDULE',
                       'EACH INDEX/SESSION IS COUNTED ONCE',
                       'HISTORICAL SLOT COLLISIONS RESOLVE DETERMINISTICALLY',
                       'SCHEDULE MANIFESTS ARE VALIDATED AGAINST THEIR OWN ROWS'):
            self.assertIn(marker, p.stdout)

    def test_a_multi_export_isolated_run_collapses_the_duplicate(self):
        """The documented multi-session form, isolated, with the cohort rule applied."""
        import shutil
        d = tempfile.mkdtemp()
        copy_path = os.path.join(d, 'copy.json')
        shutil.copy(REAL_EXPORT, copy_path)
        out = tempfile.mkdtemp()
        p = isolated(['tools/premium_range_compare/range_compare.py',
                      REAL_EXPORT, copy_path, REAL_SPOTS,
                      '--horizons', '60', '--out', out])
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertIn('identical duplicate input(s) collapsed', p.stdout)
        with open(os.path.join(out, 'range_comparison.json'), encoding='utf-8') as fh:
            doc = json.load(fh)
        self.assertEqual(1, len(doc['sessions']))
        self.assertEqual(2, doc['inputs_supplied'])


if __name__ == '__main__':
    unittest.main()
