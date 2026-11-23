#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the brain's Python test suite module by module, distinguishing TIMEOUT from FAILURE.

Why this exists: `pytest` is not installed in the analysis sandbox, so the suite gets run with
ad-hoc shell loops. On 23 November 2026 one such loop reported

    FAIL/ERR: test_pm1_monday_evidence
    FULL SUITE: modules passed=171 failed_or_errored=1

and that was wrong. The loop treated any non-zero exit as a failure, but the module had been
KILLED by `timeout 300` (exit 143, SIGTERM) without a single assertion failing. "171 pass, 1
fails" and "171 pass, 1 did not finish in time" call for completely different responses, and
conflating them sent me looking for a defect that did not exist.

So this script reports four outcomes, never three:

    OK       every test in the module passed
    FAIL     at least one assertion failed
    ERROR    the module raised (import error, fixture error, unexpected exception)
    TIMEOUT  the module did not finish inside --timeout; NOTHING is known about correctness

and exits non-zero only for FAIL and ERROR.  A TIMEOUT is reported, counted and called
inconclusive, because that is what it is.

    python3 tools/run_python_suite.py                      # whole suite
    python3 tools/run_python_suite.py --timeout 600         # slower modules
    python3 tools/run_python_suite.py -k pm1 -k lineage     # substring filters
    python3 tools/run_python_suite.py --slowest 10          # show the slowest modules
    python3 tools/run_python_suite.py --compare-ref HEAD~1 --file app/src/main/python/brain.py

`--compare-ref` is the attribution mode this script was written for.  To decide whether a change
caused a failure, the ONLY reliable test is the same modules in the same directory with a single
file swapped.  It swaps the named file to the given git ref, runs the selected modules, restores
the file, and prints both columns side by side.  It restores the file even on Ctrl-C or an
exception; if it ever cannot, it says so loudly, because a half-restored working tree that gets
committed would silently revert the change under test.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_DIR = os.path.join(REPO, 'app', 'src', 'main', 'python')
TESTS_DIR = os.path.join(PY_DIR, 'tests')

# Run one module per process so a crash or a hang is contained, and so the outcome can be
# attributed to a named module rather than to "the suite".
RUNNER = r'''
import sys, unittest
sys.path.insert(0, '.')
sys.path.insert(0, 'tests')
mod = __import__(sys.argv[1])
res = unittest.TextTestRunner(verbosity=0).run(
    unittest.TestLoader().loadTestsFromModule(mod))
print('TESTS=%d FAILURES=%d ERRORS=%d SKIPPED=%d'
      % (res.testsRun, len(res.failures), len(res.errors), len(res.skipped)))
for case, tb in res.failures:
    print('FAILURE: %s' % case)
    print(tb.rstrip()[-1200:])
for case, tb in res.errors:
    print('ERROR: %s' % case)
    print(tb.rstrip()[-1200:])
sys.exit(0 if res.wasSuccessful() else 1)
'''

OK, FAIL, ERROR, TIMEOUT = 'OK', 'FAIL', 'ERROR', 'TIMEOUT'


def discover(filters):
    names = sorted(f[:-3] for f in os.listdir(TESTS_DIR)
                   if f.startswith('test_') and f.endswith('.py'))
    if filters:
        names = [n for n in names if any(f in n for f in filters)]
    return names


def run_module(name, timeout):
    """-> (outcome, seconds, detail). TIMEOUT is never reported as FAIL."""
    started = time.time()
    try:
        proc = subprocess.run([sys.executable, '-c', RUNNER, name],
                              cwd=PY_DIR, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except subprocess.TimeoutExpired:
        return TIMEOUT, time.time() - started, 'did not finish within %ss' % timeout
    elapsed = time.time() - started
    out = (proc.stdout or b'').decode('utf-8', 'replace')

    if proc.returncode == 0:
        return OK, elapsed, out.strip().splitlines()[-1] if out.strip() else ''
    # A negative return code means a signal, which is a kill, not an assertion failure.
    if proc.returncode < 0:
        sig = -proc.returncode
        try:
            sig_name = signal.Signals(sig).name
        except ValueError:
            sig_name = 'signal %d' % sig
        return TIMEOUT, elapsed, 'killed by %s' % sig_name

    # Classify from the runner's own summary line, not by sniffing the text.  If that line is
    # absent the module never got as far as running its tests at all -- an import error, a
    # collection error, a crash in module setup -- which is an ERROR, not a FAIL.  (A first
    # version of this sniffed for 'ERROR: ' in the output and therefore reported an
    # unimportable module as FAIL, because the import blows up before the runner can print
    # anything.  Caught by the self-check in tests/test_zzz_* during review.)
    summary = next((ln for ln in out.splitlines() if ln.startswith('TESTS=')), None)
    if summary is None:
        return ERROR, elapsed, out.strip()
    fields = dict(part.split('=', 1) for part in summary.split() if '=' in part)
    try:
        errors = int(fields.get('ERRORS', 0))
        failures = int(fields.get('FAILURES', 0))
    except ValueError:
        return ERROR, elapsed, out.strip()
    # Errors dominate: a module with both is reported as ERROR, because an exception is a
    # stronger signal about the module's state than a wrong assertion.
    return (ERROR if errors else FAIL if failures else ERROR), elapsed, out.strip()


def sweep(names, timeout, verbose):
    results = {}
    for name in names:
        outcome, elapsed, detail = run_module(name, timeout)
        results[name] = (outcome, elapsed, detail)
        if outcome != OK or verbose:
            print('%-7s %7.1fs  %s' % (outcome, elapsed, name), flush=True)
            if outcome in (FAIL, ERROR) and detail:
                for line in detail.splitlines():
                    print('        | %s' % line)
    return results


def tally(results):
    counts = {OK: 0, FAIL: 0, ERROR: 0, TIMEOUT: 0}
    for outcome, _, _ in results.values():
        counts[outcome] += 1
    return counts


def report(results, slowest):
    counts = tally(results)
    print('\n%s' % ('-' * 72))
    print('modules      %d' % len(results))
    print('  OK         %d' % counts[OK])
    print('  FAIL       %d' % counts[FAIL])
    print('  ERROR      %d' % counts[ERROR])
    print('  TIMEOUT    %d   (inconclusive: nothing is known about correctness)'
          % counts[TIMEOUT])
    for label, outcome in (('failed', FAIL), ('errored', ERROR), ('timed out', TIMEOUT)):
        named = sorted(n for n, (o, _, _) in results.items() if o == outcome)
        if named:
            print('%s: %s' % (label, ', '.join(named)))
    if slowest:
        ranked = sorted(results.items(), key=lambda kv: -kv[1][1])[:slowest]
        print('\nslowest %d:' % len(ranked))
        for name, (outcome, elapsed, _) in ranked:
            print('  %7.1fs  %-7s %s' % (elapsed, outcome, name))
    return counts


def compare_ref(names, timeout, verbose, ref, rel_path):
    """Run the modules with one file swapped to `ref`, then restore it.

    The restore is the part that matters.  A working tree left holding the OLD version of a
    file, then committed by a later step, silently reverts the change under test -- which very
    nearly happened on 23 November 2026 when a commit hook fired while a swap was in flight.
    """
    abs_path = os.path.join(REPO, rel_path)
    if not os.path.isfile(abs_path):
        sys.exit('no such file: %s' % rel_path)
    dirty = subprocess.run(['git', 'status', '--porcelain', '--', rel_path],
                           cwd=REPO, stdout=subprocess.PIPE).stdout.decode().strip()
    if dirty:
        sys.exit('refusing to swap %s: it has uncommitted changes, so the saved copy would not\n'
                 'be a clean baseline. Commit or stash first.' % rel_path)

    with open(abs_path, 'rb') as fh:
        current = fh.read()
    old = subprocess.run(['git', 'show', '%s:%s' % (ref, rel_path)],
                         cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if old.returncode != 0:
        sys.exit('cannot read %s at %s: %s' % (rel_path, ref, old.stderr.decode().strip()))

    def restore():
        try:
            with open(abs_path, 'wb') as fh:
                fh.write(current)
            return True
        except OSError as exc:
            print('\n*** COULD NOT RESTORE %s: %s' % (rel_path, exc), file=sys.stderr)
            print('*** The working tree holds the %s version. DO NOT COMMIT.' % ref,
                  file=sys.stderr)
            return False

    print('=== with the working-tree %s' % rel_path)
    after = sweep(names, timeout, verbose)
    try:
        with open(abs_path, 'wb') as fh:
            fh.write(old.stdout)
        print('\n=== with %s:%s' % (ref, rel_path))
        before = sweep(names, timeout, verbose)
    finally:
        restored = restore()

    print('\n%s' % ('-' * 72))
    print('%-46s %-9s %-9s' % ('module', ref, 'working'))
    changed = []
    for name in names:
        b = before.get(name, ('?', 0, ''))[0]
        a = after.get(name, ('?', 0, ''))[0]
        mark = '' if a == b else '   <-- CHANGED'
        if a != b:
            changed.append((name, b, a))
        print('%-46s %-9s %-9s%s' % (name, b, a, mark))
    print('')
    if changed:
        print('The working-tree version changes the outcome of %d module(s):' % len(changed))
        for name, b, a in changed:
            print('  %s: %s -> %s' % (name, b, a))
        print('That is attributable to the change in %s.' % rel_path)
    else:
        print('No outcome differs. The change in %s is not responsible for any of these '
              'results.' % rel_path)
    if not restored:
        return 2
    return 1 if changed else 0


SELF_CHECK_CASES = {
    # name suffix -> (module source, expected outcome)
    'ok': ('import unittest\n'
           'class C(unittest.TestCase):\n'
           '    def test_a(self): self.assertTrue(True)\n', OK),
    'fail': ('import unittest\n'
             'class C(unittest.TestCase):\n'
             '    def test_a(self): self.assertTrue(True)\n'
             '    def test_b(self): self.assertEqual(1, 2, "deliberate")\n', FAIL),
    'error': ('import unittest\n'
              'import _harness_self_check_missing_module  # noqa\n'
              'class C(unittest.TestCase):\n'
              '    def test_a(self): pass\n', ERROR),
    'raises': ('import unittest\n'
               'class C(unittest.TestCase):\n'
               '    def test_a(self): raise RuntimeError("deliberate")\n', ERROR),
    'slow': ('import time, unittest\n'
             'class C(unittest.TestCase):\n'
             '    def test_a(self): time.sleep(30)\n', TIMEOUT),
}


def self_check():
    """Prove the four outcomes are told apart, then clean up.

    This is built in rather than left as test modules in tests/, because a module that fails on
    purpose would make the real suite permanently red.  It writes throwaway modules, classifies
    them, checks each against its expected outcome, and removes them -- including on failure.

    It exists because the first version of this script reported an unimportable module as FAIL
    and a timed-out module as FAIL, and a harness that cannot tell "did not finish" from "is
    wrong" sends you hunting for defects that are not there.
    """
    written = []
    failures = []
    try:
        for suffix, (source, expected) in sorted(SELF_CHECK_CASES.items()):
            name = '_harness_self_check_%s' % suffix
            path = os.path.join(TESTS_DIR, name + '.py')
            with open(path, 'w') as fh:
                fh.write(source)
            written.append(path)
            # The slow case needs a budget it will certainly exceed, quickly.
            budget = 2.0 if expected is TIMEOUT else 60.0
            got, elapsed, _ = run_module(name, budget)
            ok = got == expected
            print('%-10s expected %-8s got %-8s  %5.1fs  %s'
                  % (suffix, expected, got, elapsed, 'ok' if ok else 'MISCLASSIFIED'))
            if not ok:
                failures.append('%s: expected %s, got %s' % (suffix, expected, got))
    finally:
        for path in written:
            try:
                os.remove(path)
            except OSError as exc:
                failures.append('could not remove %s: %s' % (path, exc))
        cache = os.path.join(TESTS_DIR, '__pycache__')
        if os.path.isdir(cache):
            for entry in os.listdir(cache):
                if entry.startswith('_harness_self_check_'):
                    try:
                        os.remove(os.path.join(cache, entry))
                    except OSError:
                        pass

    leftover = [f for f in os.listdir(TESTS_DIR) if f.startswith('_harness_self_check_')]
    if leftover:
        failures.append('left behind in tests/: %s' % ', '.join(sorted(leftover)))

    print('')
    if failures:
        for line in failures:
            print('SELF-CHECK FAILED: %s' % line)
        return 1
    print('self-check passed: OK, FAIL, ERROR and TIMEOUT are told apart, '
          'and nothing was left in tests/.')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--self-check', action='store_true',
                    help='verify this script tells OK / FAIL / ERROR / TIMEOUT apart, then exit')
    ap.add_argument('--timeout', type=float, default=300.0,
                    help='per-module seconds (default 300). A module that exceeds it is '
                         'TIMEOUT, not FAIL.')
    ap.add_argument('-k', dest='filters', action='append', default=[],
                    help='only modules whose name contains this (repeatable)')
    ap.add_argument('-v', '--verbose', action='store_true', help='print every module, not just problems')
    ap.add_argument('--slowest', type=int, default=0, help='also list the N slowest modules')
    ap.add_argument('--compare-ref', metavar='REF',
                    help='attribution mode: also run with --file swapped to this git ref')
    ap.add_argument('--file', metavar='PATH',
                    help='repo-relative file to swap for --compare-ref')
    args = ap.parse_args(argv)

    if args.self_check:
        return self_check()

    names = discover(args.filters)
    if not names:
        sys.exit('no test modules matched')

    if args.compare_ref:
        if not args.file:
            sys.exit('--compare-ref needs --file')
        return compare_ref(names, args.timeout, args.verbose, args.compare_ref, args.file)

    print('running %d module(s), %ss each\n' % (len(names), args.timeout))
    results = sweep(names, args.timeout, args.verbose)
    counts = report(results, args.slowest)
    # TIMEOUT deliberately does not fail the run: it is inconclusive, and treating it as a
    # failure is the exact mistake this script exists to prevent.
    return 1 if (counts[FAIL] or counts[ERROR]) else 0


if __name__ == '__main__':
    raise SystemExit(main())
