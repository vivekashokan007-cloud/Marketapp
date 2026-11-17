#!/usr/bin/env python3
"""Exact error levels for the trial's stopping rule.  Computed, never asserted.

WHY THIS MODULE EXISTS.  The first protocol draft said that `HIST` winning 7 of 10 paired
sessions was "the one-sided sign test at roughly 5% under a fair coin".  That is wrong, and
wrong in the dangerous direction: the exact tail is **17.19%**, more than three times the level
claimed.  Codex caught it.  Rather than correct one number and move on, every error level the
protocol quotes is now computed here from exact rational arithmetic, with a CLI that prints the
table, so a reader can check the protocol against the arithmetic instead of against prose.

WHAT IS COMPUTED

  `tail(n, k)`            P(X >= k) for n independent fair signs.  Exact, as a Fraction.
  `multi_look(looks)`     the probability of crossing AT LEAST ONE threshold in a sequence of
                          NESTED looks at the same accumulating sign sequence -- which is what
                          an interim-analysis rule actually faces.  Exact, by dynamic
                          programming over partial sums, not simulation.
  `single_look(n, alpha)` the smallest threshold whose exact tail is at or below `alpha`.
  `simulate(looks, ...)`  an independent Monte Carlo check on `multi_look`, because a DP with
                          an off-by-one in its boundary conditions is exactly the kind of error
                          this module exists to catch.

THE RESULT THAT DECIDED THE REVISION.  For the draft's three thresholds -- 7 of 10, 11 of 15,
14 of 20 -- the individual tails are 17.19%, 5.92% and 5.77%, and the probability of crossing at
least one of them under fair signs is **18.91%**.  That is the figure Codex reported and this
module reproduces independently.  A rule whose nominal level is 5% and whose actual level is
18.91%, before the separate CRPS and calibration requirements are even applied, is not a 5%
rule.  Hence the revision to ONE fixed decision point.
"""
import argparse
import functools
import random
import sys
from fractions import Fraction
from math import comb

STOPPING_RULE_VERSION = 'premium_range_stopping_rule_v1_exact'

#: The draft's interim thresholds, kept so the error level that forced the revision stays
#: reproducible rather than becoming a sentence in a changelog.
DRAFT_LOOKS = ((10, 7), (15, 11), (20, 14))

#: The revision: one fixed decision point.  The threshold is derived, not chosen -- see
#: `single_look(20, 0.05)`.
DEFAULT_DECISION_SESSIONS = 20
DEFAULT_ALPHA = 0.05


def tail(n, k):
    """P(X >= k) for n independent fair signs, exactly."""
    if k > n:
        return Fraction(0)
    if k <= 0:
        return Fraction(1)
    return Fraction(sum(comb(n, j) for j in range(k, n + 1)), 2 ** n)


def single_look(n, alpha=DEFAULT_ALPHA):
    """(k, exact_tail) -- the smallest threshold at or below `alpha` for one fixed look."""
    for k in range(n // 2 + 1, n + 1):
        t = tail(n, k)
        if t <= Fraction(alpha).limit_denominator(10 ** 9):
            return k, t
    return n, tail(n, n)


def multi_look(looks=DRAFT_LOOKS):
    """Exact P(cross at least one threshold) for NESTED looks on one sign sequence.

    `looks` is ((n1, k1), (n2, k2), ...) with n increasing: the same accumulating sequence of
    fair signs is examined at each n, and crossing at any look ends the trial.  The looks are
    NOT independent tests -- they share every sign up to the earlier n -- so multiplying or
    adding the individual tails is wrong in both directions.  This walks the partial sums.
    """
    looks = tuple(sorted(looks))
    if not looks:
        return Fraction(0)
    final_n = looks[-1][0]
    at = dict(looks)

    @functools.lru_cache(maxsize=None)
    def walk(i, wins):
        if i in at and wins >= at[i]:
            return Fraction(1)
        if i == final_n:
            return Fraction(0)
        return (walk(i + 1, wins + 1) + walk(i + 1, wins)) / 2

    out = walk(0, 0)
    walk.cache_clear()
    return out


def simulate(looks=DRAFT_LOOKS, trials=200000, seed=20261117):
    """An independent Monte Carlo estimate of `multi_look`, to catch a DP boundary error."""
    looks = tuple(sorted(looks))
    final_n = looks[-1][0]
    at = dict(looks)
    rnd = random.Random(seed)
    crossed = 0
    for _ in range(trials):
        wins = 0
        for i in range(1, final_n + 1):
            wins += rnd.getrandbits(1)
            if i in at and wins >= at[i]:
                crossed += 1
                break
    return crossed / trials


def describe(looks=DRAFT_LOOKS):
    """The table the protocol quotes, as data."""
    rows = [{'n': n, 'k': k, 'exact_tail': str(tail(n, k)),
             'exact_tail_pct': round(100 * float(tail(n, k)), 4)}
            for n, k in sorted(looks)]
    m = multi_look(looks)
    return {'version': STOPPING_RULE_VERSION,
            'looks': rows,
            'combined_exact': str(m),
            'combined_pct': round(100 * float(m), 4),
            'combined_basis': 'nested looks on one accumulating fair-sign sequence; exact by '
                              'dynamic programming over partial sums',
            'note': 'the individual tails cannot be added or multiplied: the looks share every '
                    'sign up to the earlier n'}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--alpha', type=float, default=DEFAULT_ALPHA)
    ap.add_argument('--sessions', type=int, default=DEFAULT_DECISION_SESSIONS)
    ap.add_argument('--simulate', type=int, default=0,
                    help='Monte Carlo trials for an independent check (0 = skip)')
    a = ap.parse_args(argv)

    print('=' * 88)
    print('STOPPING RULE   %s' % STOPPING_RULE_VERSION)
    print('=' * 88)
    print("the first draft's three interim thresholds, under independent fair signs:")
    for n, k in DRAFT_LOOKS:
        t = tail(n, k)
        print('  >= %2d wins of %2d   exact one-sided tail %-14s = %6.2f%%'
              % (k, n, t, 100 * float(t)))
    m = multi_look(DRAFT_LOOKS)
    print('  crossing AT LEAST ONE of them: %-14s = %6.2f%%' % (m, 100 * float(m)))
    print('  (the draft called the first of these "roughly 5%"; it is 17.19%)')
    if a.simulate:
        est = simulate(DRAFT_LOOKS, a.simulate)
        print('  Monte Carlo check over %d trials: %6.2f%%  (exact %6.2f%%)'
              % (a.simulate, 100 * est, 100 * float(m)))

    print('')
    print('ONE FIXED DECISION POINT at %d eligible sessions per index:' % a.sessions)
    for k in range(a.sessions // 2 + 1, a.sessions + 1):
        t = tail(a.sessions, k)
        mark = ''
        kk, _tt = single_look(a.sessions, a.alpha)
        if k == kk:
            mark = '   <- smallest threshold at or below alpha=%.3f' % a.alpha
        print('  >= %2d of %2d   %6.2f%%%s' % (k, a.sessions, 100 * float(t), mark))
        if 100 * float(t) < 0.5 and k > kk:
            break
    k, t = single_look(a.sessions, a.alpha)
    print('')
    print('  DEFAULT: adopt only at >= %d of %d paired session wins, exact level %.2f%%, '
          'ONE look.' % (k, a.sessions, 100 * float(t)))
    print('  No interim adoption. A rule with interim looks must publish its combined level,')
    print('  computed as above, and that level -- not the per-look level -- is the rule\'s.')
    print('=' * 88)
    return 0


if __name__ == '__main__':
    sys.exit(main())
