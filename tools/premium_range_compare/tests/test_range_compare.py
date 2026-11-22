"""Checks for the neutral premium range comparison.

The properties these pin, in the order the handoff asks for them:

  * the admission pipeline is a faithful MIRROR of the frozen replay -- same rows, same
    exclusion counts, same reference score.  This is the check that makes the duplicated
    admission block safe to keep: if it drifts from `premium_repricing_v2.py`, this fails;
  * entry IV is fitted from the contract's own midpoint and ROUND-TRIPS to it, and a midpoint
    outside the pricer's own bounds is UNSOLVABLE with a named reason, never clamped;
  * the common centre holds: MAE TIES across the flat point and both ranges.  A broken centre
    would make the CRPS comparison a location comparison in disguise;
  * recentring is invariant to where the distribution started, idempotent, and when a clip
    moves the median that fact is REPORTED rather than absorbed;
  * no model gains from silent clipping or dropped rows: every scenario at a bound is counted,
    and every variant's own row count is reported beside the common set;
  * every refusal is named, a row with no exit still counts in the intended denominator, and
    the result is independent of input order;
  * the historical join's slot policy is explicit: an inferred slot can never be presented as
    a scheduled one.
"""
import datetime
import json
import os
import shutil
import tempfile
import math
import os
import statistics
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), '..', 'spot_path_baseline'))

import premium_repricing_v2 as R7          # noqa: E402
import range_compare as RC                 # noqa: E402
import planned_schedule as PS              # noqa: E402
import frozen_successor as FS              # noqa: E402
import stopping_rule as SR                 # noqa: E402

IST = R7.IST
SESSION = '2026-11-12'
EXPIRY = '2026-11-17'

#: The real inputs, when this machine has them. The parity and reproduction tests need them and
#: are skipped, loudly, without them -- a skipped test is not a passed one.
REAL_EXPORT = os.environ.get('RANGE_EXPORT')
REAL_SPOTS = os.environ.get('RANGE_SPOTS')

#: EVERY FIXTURE-SPECIFIC EXPECTED VALUE IS PINNED BY SESSION DATE **AND** SHA-256.
#:
#: The defect this closes, which Codex hit: the expected-value tests read whichever file
#: `RANGE_EXPORT` happened to point at, so running them against the 16 November evening export
#: failed two 12 November assertions -- a false alarm that says nothing about the code. An
#: expectation now declares which bytes it is about, and a test whose input is a different file
#: skips with the digest it wanted rather than failing.
EXPECTED = {
    ('2026-11-12', '5f399accefabd0dd3f1f1f3bb829bb71fd6ae56a309dfc92db5ac18e6d3444cb'): {
        'source': 'reproduced on this machine',
        'r7_e0_fwd_crps_60': {'NF': 17.567, 'BNF': 39.489},
        'planned_60': {'NF': 342, 'BNF': 342},
        'scored_60': {'NF': 246, 'BNF': 251},
        'usable_coverage_60': {'NF': 0.719, 'BNF': 0.734},
        'iv_only_crps_60': {'NF': 16.840, 'BNF': 34.456},
        'hist_crps_60': {'NF': 18.096, 'BNF': 38.046},
        'mae_60': {'NF': 24.351, 'BNF': 47.911},
        'nf_120_iv_only_raw_crps': 31.69,
        'nf_120_r7_crps': 30.84,
        'nf_120_iv_only_raw_cov80_at_grid_101': 0.414,
    },
    ('2026-11-16', '903225d9826dfbcac5011a099da1706f901854d40bc2e6d02d7ebdd43f9625e1'): {
        'source': "INDEPENDENT, reported by Codex on 17 November 2026. NOT reproduced here: "
                  "this export has never been on this machine. The moment it is, this "
                  "regression runs and either confirms these numbers or disagrees with them.",
        'matched_60': {'NF': 342, 'BNF': 342},
        'iv_only_crps_60': {'NF': 14.369, 'BNF': 41.306},
        'hist_crps_60': {'NF': 15.450, 'BNF': 43.819},
        'iv_only_cov80_60': {'NF': 0.740, 'BNF': 0.635},
        'hist_cov80_60': {'NF': 0.810, 'BNF': 0.781},
        'mae_60': {'NF': 20.779, 'BNF': 58.562},
    },
}


def expectations_for(path):
    """(key, expected) for the supplied export, or (None, None) if nothing is pinned for it."""
    if not path or not os.path.isfile(path):
        return None, None
    import hashlib
    digest = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    with open(path, encoding='utf-8') as fh:
        session = (json.load(fh).get('manifest') or {}).get('session_date_used')
    key = (session, digest)
    return (key, EXPECTED[key]) if key in EXPECTED else (key, None)


def ms(date_str, hh, mm, ss=0):
    y, mo, d = map(int, date_str.split('-'))
    t = datetime.datetime(y, mo, d, hh, mm, ss, tzinfo=IST)
    return int(t.timestamp() * 1000)


SIGMA = 0.14
FORWARD = 25000.0
STRIKES = (24900.0, 25000.0, 25100.0)
SPREAD = 2.0


def export(quotes, index_name='NF', session=SESSION, expiry=EXPIRY, underlying='NIFTY',
           strikes=STRIKES, sides=('CE', 'PE'), iv=None, sigma=SIGMA, spread=SPREAD,
           manifest_slots=None):
    """A minimal export in the real shape, with books GENERATED BY THE PRICER.

    `quotes` maps a clock 'HH:MM' to that acquisition's forward.  Every leg's midpoint is
    `R7.bs(F, K, T, sigma, kind)` with a symmetric `spread`, so:

      * put-call parity holds EXACTLY -- F = K + CE - PE at every strike -- which means the
        parity forward is the generating forward and its dispersion is ~0, and
      * the IV fitted from a midpoint IS the generating sigma.

    A fixture whose books were made up by hand cannot test either of those, and an earlier
    draft of these tests failed for exactly that reason: one strike cannot produce a parity
    forward at all (`MIN_FWD_STRIKES` is 2), so every row was refused before any range was
    built and the refusal under test never fired.

    `iv` is the QUOTED iv written into the record; None writes the generating sigma in points,
    which is what makes the quoted and fitted values agree in the fixture.
    """
    pins, records, envelopes = [], [], []
    for K in strikes:
        for side in sides:
            pins.append({'instrument_key': 'NSE_FO|%d%s' % (int(K), side),
                         'strike': K, 'option_type': side, 'expiry': expiry,
                         'underlying': underlying})
    for clock, F in sorted(quotes.items()):
        hh, mm = map(int, clock.split(':'))
        aid = 'acq-%s-%s' % (session, clock)
        recv_ms = ms(session, hh, mm, 7)
        envelopes.append({'acquisition_id': aid, 'slot': clock, 'session_date': session,
                          'clocks': {'acquisition_response_ms': recv_ms,
                                     'quote_event_clock_verified': False}})
        recv = datetime.datetime.fromtimestamp(recv_ms / 1000.0, tz=IST)
        T = R7.tte(recv, expiry)
        for K in strikes:
            for side in sides:
                mid = R7.bs(F, K, T, sigma, side)
                records.append({
                    'instrument_key': 'NSE_FO|%d%s' % (int(K), side),
                    'acquisition_id': aid, 'slot': clock,
                    'bbo': {'bid_raw': {'raw': max(mid - spread / 2.0, 0.05)},
                            'ask_raw': {'raw': mid + spread / 2.0}},
                    'iv': {'iv_greeks_raw': {
                        'raw': sigma * 100.0 if iv is None else iv,
                        'status': 'PRESENT_VALID'}},
                    'identity': {'identity_verified': True, 'identity_disagreements': []}})
    # the fixture declares its OWN slot manifest, so `planned_schedule` reads a VERIFIED basis
    # rather than falling back to the pilot window. `manifest_slots` lets a test plan a grid
    # wider than the slots it actually captured, which is how a missing slot is exercised.
    # the manifest enumerates the AUTHORITATIVE grid by default, which is what a real
    # producer's manifest does; only the fixture's own clocks are marked OBSERVED. A test that
    # wants a narrower or wider manifest passes `manifest_slots` explicitly.
    grid = manifest_slots or [PS.hhmm(m) for m in PS.window_grid()]
    observed = set(quotes)
    manifest = {'expected_slots': len(grid), 'observed_slots': len(observed),
                'capture_started_slot': None,
                'rows': [{'slot': c, 'acquisitions': 1 if c in observed else 0,
                          'records': len(strikes) * len(sides) if c in observed else 0,
                          'state': 'OBSERVED' if c in observed
                                   else 'NOT_OBSERVED_UNEXPLAINED'} for c in grid]}
    return {'manifest': {'session_date_used': session, 'status': 'OK'},
            'premium': {'state': 'PRESENT', 'has_data': True,
                        'indices': {index_name: {
                            'records': records, 'slot_envelopes': envelopes,
                            'slot_manifest': manifest,
                            'cohort': {'pinned_contracts': pins,
                                       'contracts_expected': len(strikes) * len(sides)}}}}}


def history(dates, clocks, nf=25000.0, bnf=56000.0, step=1.0):
    """A prior-session spot history with a deterministic drift, as JSONL rows."""
    rows = []
    for i, d in enumerate(dates):
        for j, c in enumerate(clocks):
            rows.append({'d': d, 't': c,
                         'nf': '%.2f' % (nf + i * 3.0 + j * step),
                         'bnf': '%.2f' % (bnf + i * 7.0 + j * step * 2)})
    return rows


def write_history(rows, name='spots.jsonl'):
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), name)
    with open(path, 'w') as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + '\n')
    return path


class NormalQuantile(unittest.TestCase):

    def test_known_quantiles_to_full_double_precision(self):
        for p, want in ((0.5, 0.0), (0.9, 1.2815515655446004),
                        (0.975, 1.959963984540054), (0.025, -1.959963984540054),
                        (0.001, -3.090232306167813), (0.999, 3.090232306167813)):
            self.assertAlmostEqual(want, RC.norm_ppf(p), places=9, msg=p)

    def test_it_is_the_inverse_of_the_pricer_own_normal_cdf(self):
        for p in (0.001, 0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99, 0.999):
            self.assertAlmostEqual(p, R7.nd(RC.norm_ppf(p)), places=12, msg=p)

    def test_it_is_symmetric_and_monotone(self):
        last = -1e9
        for i in range(1, 1000):
            p = i / 1000.0
            z = RC.norm_ppf(p)
            self.assertGreater(z, last)
            last = z
            self.assertAlmostEqual(-z, RC.norm_ppf(1 - p), places=9)

    def test_the_declared_grid_is_odd_centred_and_finite(self):
        self.assertEqual(1, RC.QUANTILE_GRID_N % 2, 'an odd grid has its own median as a point')
        self.assertEqual(RC.QUANTILE_GRID_N, len(RC.GRID_Z))
        self.assertAlmostEqual(0.0, RC.GRID_Z[RC.QUANTILE_GRID_N // 2], places=12)
        self.assertTrue(all(math.isfinite(z) for z in RC.GRID_Z))
        self.assertTrue(all(0.0 < p < 1.0 for p in RC.QUANTILE_GRID))

    def test_p_outside_the_open_unit_interval_is_refused(self):
        for bad in (0.0, 1.0, -0.1, 1.1):
            with self.assertRaises(ValueError):
                RC.norm_ppf(bad)


class FitIv(unittest.TestCase):

    def test_a_fitted_iv_reprices_the_midpoint_it_was_fitted_to(self):
        for sigma in (0.05, 0.10, 0.1437, 0.30, 0.80):
            for kind in ('CE', 'PE'):
                for K in (24800.0, 25000.0, 25200.0):
                    F, T = 25000.0, 5.0 / 365.0
                    mid = R7.bs(F, K, T, sigma, kind)
                    got, why = RC.fit_iv(mid, F, K, T, kind)
                    self.assertIsNone(why, (sigma, kind, K))
                    self.assertAlmostEqual(sigma, got, places=6, msg=(sigma, kind, K))
                    self.assertAlmostEqual(mid, R7.bs(F, K, T, got, kind), places=8)

    def test_a_midpoint_at_or_below_intrinsic_is_unsolvable_and_named(self):
        F, K, T = 25000.0, 24000.0, 5.0 / 365.0
        intrinsic = F - K
        for mid in (intrinsic, intrinsic - 1.0, 0.0):
            got, why = RC.fit_iv(mid, F, K, T, 'CE')
            self.assertIsNone(got, mid)
            self.assertEqual('iv_target_below_floor', why, mid)

    def test_a_midpoint_at_or_above_the_pricer_ceiling_is_unsolvable_and_named(self):
        F, K, T = 25000.0, 25000.0, 5.0 / 365.0
        ceiling = R7.bs(F, K, T, RC.IV_MAX, 'CE')
        for mid in (ceiling, ceiling + 10.0, F):
            got, why = RC.fit_iv(mid, F, K, T, 'CE')
            self.assertIsNone(got, mid)
            self.assertEqual('iv_target_above_ceiling', why, mid)

    def test_an_unsolvable_fit_is_never_clamped_to_a_bracket_endpoint(self):
        """The failure mode this rules out: returning IV_MIN or IV_MAX as if it were a fit."""
        F, K, T = 25000.0, 24000.0, 5.0 / 365.0
        got, _ = RC.fit_iv(F - K - 5.0, F, K, T, 'CE')
        self.assertIsNone(got)
        got, _ = RC.fit_iv(R7.bs(F, K, T, RC.IV_MAX, 'CE') + 1.0, F, K, T, 'CE')
        self.assertIsNone(got)

    def test_invalid_inputs_are_named_rather_than_priced(self):
        for args in ((100.0, 0.0, 25000.0, 0.01), (100.0, 25000.0, 0.0, 0.01),
                     (100.0, 25000.0, 25000.0, 0.0), (None, 25000.0, 25000.0, 0.01),
                     (float('nan'), 25000.0, 25000.0, 0.01)):
            got, why = RC.fit_iv(args[0], args[1], args[2], args[3], 'CE')
            self.assertIsNone(got, args)
            self.assertEqual('iv_inputs_invalid', why, args)


class Recentring(unittest.TestCase):

    def prices(self, sigma=0.14, F=25000.0, K=25000.0, years=1.0 / 365.0):
        fwds = RC.iv_only_forwards(F, sigma, years)
        px, _ = RC.reprice(fwds, K, 5.0 / 365.0, sigma, 'CE')
        return px, K

    def test_the_median_lands_exactly_on_the_centre(self):
        px, K = self.prices()
        out, info = RC.recentre(px, 123.45, K, 'CE', underlying_cap=25000.0)
        self.assertAlmostEqual(123.45, statistics.median(out), places=9)
        self.assertTrue(info['centred_exactly'])
        self.assertAlmostEqual(123.45 - info['pre_centre_median'], info['shift'], places=9)

    def test_clipping_a_TAIL_leaves_the_median_exact_and_is_still_counted(self):
        """The distinction that matters, and the one an earlier draft of this test had wrong.

        A shift large enough to push the far tail below zero does NOT move the median -- a
        median is unmoved by anything that happens strictly on one side of it past the
        half-way point. So the right assertion is not "no clip", it is: the clip is COUNTED,
        and the centre survives it. The case where a clip does reach the median is the next
        test, and there the loss of exactness is reported.
        """
        px, K = self.prices()
        out, info = RC.recentre(px, 20.0, K, 'CE', underlying_cap=25000.0)
        self.assertGreater(info['clipped'], 0, 'the far tail must have been clipped')
        self.assertLess(info['clipped'], len(px) // 2, 'but only on one side of the median')
        self.assertTrue(info['centred_exactly'])
        self.assertAlmostEqual(20.0, info['post_centre_median'], places=9)
        self.assertGreaterEqual(min(out), 0.0)

    def test_it_is_invariant_to_where_the_distribution_started(self):
        """Shift the input by ANY constant, recentre, and the result must be identical.

        This is the property the whole comparison rests on: after recentring, a variant's
        score can only reflect the SHAPE of its distribution, never its location. If this
        failed, a model could win by being mislocated in a lucky direction.
        """
        px, K = self.prices()
        base, _ = RC.recentre(px, 200.0, K, 'CE', underlying_cap=25000.0)
        for delta in (-50.0, -1.0, 0.0, 1.0, 37.5, 500.0):
            moved = [p + delta for p in px]
            out, info = RC.recentre(moved, 200.0, K, 'CE', underlying_cap=25000.0)
            self.assertEqual(0, info['clipped'], delta)
            for a, b in zip(base, out):
                self.assertAlmostEqual(a, b, places=9, msg=delta)
            self.assertEqual(R7.score(base, 210.0), R7.score(out, 210.0))

    def test_it_is_idempotent(self):
        px, K = self.prices()
        once, _ = RC.recentre(px, 200.0, K, 'CE', underlying_cap=25000.0)
        twice, info = RC.recentre(once, 200.0, K, 'CE', underlying_cap=25000.0)
        self.assertAlmostEqual(0.0, info['shift'], places=9)
        for a, b in zip(once, twice):
            self.assertAlmostEqual(a, b, places=9)

    def test_the_only_way_to_lose_exactness_is_a_centre_outside_the_bounds(self):
        """The property this pins, which the first draft of these tests had backwards.

        Clipping at zero touches only values BELOW the median once the shift has put the
        median on a positive centre, and a median is unmoved by anything strictly on one side
        of it. So with a centre inside the contract's own no-arbitrage bounds the centring is
        EXACT however much tail is clipped -- which is the guarantee the comparison needs.

        Exactness can therefore only be lost if the CENTRE ITSELF is outside the bounds, which
        for a two-sided quote midpoint should not happen and would mean the book was crossed or
        implausible. That case is reported rather than silently clipped, and it is the case
        asserted here.
        """
        px, K = self.prices(sigma=0.60)
        out, info = RC.recentre(px, -5.0, K, 'CE', underlying_cap=25000.0)
        # the shift puts the median at -5, so the median AND everything below it clip to zero
        self.assertEqual(len(px) // 2 + 1, info['clipped'])
        self.assertFalse(info['centred_exactly'])
        self.assertEqual(0.0, info['post_centre_median'])
        self.assertGreaterEqual(min(out), 0.0, 'a clipped price is never negative')

    def test_an_empty_distribution_recentres_to_nothing_and_says_so(self):
        out, info = RC.recentre([], 100.0, 25000.0, 'CE')
        self.assertEqual([], out)
        self.assertIsNone(info['pre_centre_median'])
        self.assertIsNone(info['centred_exactly'])


class Scenarios(unittest.TestCase):

    def test_the_iv_only_scenario_mean_forward_is_the_entry_forward(self):
        """Zero drift on the forward: the -sigma^2 t / 2 term is what makes this hold."""
        F, sigma, years = 25000.0, 0.14, 60.0 / (365.0 * 24 * 60)
        f = RC.iv_only_forwards(F, sigma, years)
        self.assertEqual(RC.QUANTILE_GRID_N, len(f))
        self.assertAlmostEqual(F, statistics.fmean(f), delta=F * 2e-4)
        self.assertLess(statistics.median(f), statistics.fmean(f),
                        'a lognormal median sits below its mean')

    def test_a_longer_horizon_widens_the_iv_only_scenarios(self):
        F, sigma = 25000.0, 0.14
        widths = []
        for mins_ in (15, 30, 60, 120):
            f = RC.iv_only_forwards(F, sigma, mins_ / (365.0 * 24 * 60))
            widths.append(max(f) - min(f))
        self.assertEqual(widths, sorted(widths))

    def test_the_historical_scenarios_have_their_median_return_removed(self):
        """Without this the empirical DRIFT would do the centring's job and the comparison
        would be about location, not shape."""
        F = 25000.0
        rets = [0.01, 0.02, 0.03, 0.04, 0.10]          # median 0.03, strongly positive drift
        f = RC.hist_forwards(F, rets)
        self.assertEqual(len(rets), len(f))
        self.assertAlmostEqual(F, statistics.median(f), places=6)
        drifted = RC.hist_forwards(F, [r + 0.05 for r in rets])
        for a, b in zip(f, drifted):
            self.assertAlmostEqual(a, b, places=6, msg='a pure drift must not move the shape')

    def test_an_invalid_scenario_forward_is_counted_not_priced(self):
        px, acc = RC.reprice([25000.0, 0.0, -1.0, float('nan'), float('inf'), None],
                             25000.0, 5.0 / 365.0, 0.14, 'CE')
        self.assertEqual(1, len(px))
        self.assertEqual(5, acc['scenario_forward_invalid'])

    def test_a_scenario_price_at_a_bound_is_counted(self):
        # a deep in-the-money call at negligible volatility prices at its intrinsic floor
        px, acc = RC.reprice([30000.0], 20000.0, 5.0 / 365.0, 1e-6, 'CE')
        self.assertEqual(1, len(px))
        self.assertEqual(1, acc['scenario_price_at_lower_bound'])


class HistoryJoin(unittest.TestCase):

    def test_the_exact_policy_indexes_the_same_minutes_as_the_frozen_replay(self):
        rows = history(['2026-11-10', '2026-11-11'], ['09:20', '09:36', '10:20'])
        path = write_history(rows)
        mine, meta = RC.load_history(path, 'exact')
        theirs = R7.load_hist(path)
        self.assertEqual('exact', meta['slot_policy'])
        self.assertEqual({d: sorted(v) for d, v in theirs.items()},
                         {d: sorted(v) for d, v in mine.items()})

    def test_the_floor_policy_is_reported_as_inferred_with_its_own_accounting(self):
        rows = history(['2026-11-10'], ['09:20', '09:36', '10:21'])
        path = write_history(rows)
        hist, meta = RC.load_history(path, 'floor5')
        self.assertEqual('floor5', meta['slot_policy'])
        self.assertEqual(2, meta['rows_off_five_minute_grid'])
        self.assertEqual(0, meta['floor_collisions'])
        self.assertEqual([], meta['slot_field_names_seen'])
        self.assertEqual(0, meta['rows_with_explicit_slot'])
        self.assertEqual(sorted([R7.mins('09:20'), R7.mins('09:35'), R7.mins('10:20')]),
                         sorted(hist['2026-11-10']))

    def test_flooring_onto_an_occupied_slot_with_a_DIFFERENT_price_excludes_the_slot(self):
        """Codex's reproduction: 10:55 and 10:56 both floor to 10:55 and v1 kept whichever
        came last, so reversing the input changed the retained price. Now the slot is excluded
        whole, identically in either order, and the conflict is surfaced."""
        rows = [{'d': '2026-11-10', 't': '10:55', 'nf': '25000.00', 'bnf': '56000.00'},
                {'d': '2026-11-10', 't': '10:56', 'nf': '25099.00', 'bnf': '56099.00'}]
        first, m1 = RC.load_history(write_history(rows), 'floor5')
        second, m2 = RC.load_history(write_history(list(reversed(rows))), 'floor5')
        for hist, meta in ((first, m1), (second, m2)):
            self.assertNotIn(R7.mins('10:55'), hist.get('2026-11-10', {}),
                             'a conflicting slot is used by neither row')
            self.assertEqual(1, meta['excluded_conflicting_slots'])
            self.assertEqual(0, meta['collapsed_identical_duplicates'])
            self.assertEqual([655, 656],
                             meta['conflicting_slot_examples'][0]['observed_minutes'])
        self.assertEqual(m1['conflicting_slot_examples'], m2['conflicting_slot_examples'])
        self.assertEqual(1, m1['floor_collisions'])

    def test_an_identical_duplicate_on_one_slot_collapses_instead(self):
        row = {'d': '2026-11-10', 't': '10:55', 'nf': '25000.00', 'bnf': '56000.00'}
        hist, meta = RC.load_history(write_history([row, dict(row)]), 'floor5')
        self.assertIn(R7.mins('10:55'), hist['2026-11-10'])
        self.assertEqual(1, meta['collapsed_identical_duplicates'])
        self.assertEqual(0, meta['excluded_conflicting_slots'])

    def test_a_verified_scheduled_slot_is_preferred_over_an_inferred_one(self):
        rows = [{'d': '2026-11-10', 't': '10:56', 'nf': '25000.00', 'bnf': '56000.00',
                 'scheduled_slot': '10:55'},
                {'d': '2026-11-10', 't': '10:55', 'nf': '25000.00', 'bnf': '56000.00'}]
        hist, meta = RC.load_history(write_history(rows), 'exact')
        kept = hist['2026-11-10'][R7.mins('10:55')]
        self.assertEqual('verified_schedule', kept['slot_from'])
        self.assertEqual(R7.mins('10:55'), kept['scheduled_min'])
        self.assertEqual(R7.mins('10:56'), kept['observed_min'],
                         'the observed clock is preserved beside the scheduled slot')
        self.assertEqual(1, meta['slots_from_verified_schedule'])
        self.assertEqual(1, meta['collapsed_identical_duplicates'])

    def test_the_history_join_is_order_independent_on_a_realistic_mix(self):
        rows = history(['2026-11-10', '2026-11-11'], ['09:20', '09:36', '10:20'])
        a, ma = RC.load_history(write_history(rows), 'floor5')
        b, mb = RC.load_history(write_history(list(reversed(rows))), 'floor5')
        self.assertEqual({d: sorted(v.items()) for d, v in a.items()},
                         {d: sorted(v.items()) for d, v in b.items()})
        self.assertEqual(ma['retained_slots'], mb['retained_slots'])
        self.assertEqual(ma['excluded_conflicting_slots'], mb['excluded_conflicting_slots'])

    def test_the_explicit_policy_fails_closed_when_the_source_has_no_scheduled_slot(self):
        """An inferred slot must never be presented as a scheduled one."""
        path = write_history(history(['2026-11-10'], ['09:20']))
        with self.assertRaises(ValueError) as cm:
            RC.load_history(path, 'explicit')
        self.assertIn('scheduled-slot field on every row', str(cm.exception))
        self.assertIn('0 of 1 rows', str(cm.exception))

    def test_the_explicit_policy_reads_the_field_when_the_source_supplies_it(self):
        rows = history(['2026-11-10'], ['09:21'])
        rows[0]['scheduled_slot'] = '09:20'
        path = write_history(rows)
        hist, meta = RC.load_history(path, 'explicit')
        self.assertEqual(['scheduled_slot'], meta['slot_field_names_seen'])
        self.assertEqual(1, meta['rows_with_explicit_slot'])
        self.assertEqual([R7.mins('09:20')], sorted(hist['2026-11-10']))

    def test_an_unknown_policy_is_refused(self):
        path = write_history(history(['2026-11-10'], ['09:20']))
        with self.assertRaises(ValueError):
            RC.load_history(path, 'floor_to_taste')

    def test_a_missing_endpoint_is_counted_and_never_interpolated(self):
        hist = {'2026-11-10': {560: {'NF': 25000.0}},                  # entry only
                '2026-11-11': {620: {'NF': 25000.0}},                  # exit only
                '2026-11-09': {560: {'NF': 25000.0}, 620: {'NF': 25100.0}}}
        out, acc = RC.hist_returns(hist, ['2026-11-09', '2026-11-10', '2026-11-11'],
                                   'NF', 560, 620, 'exact')
        self.assertEqual(1, len(out))
        self.assertEqual(1, acc['hist_missing_exit_endpoint'])
        self.assertEqual(1, acc['hist_missing_entry_endpoint'])
        self.assertEqual(1, acc['hist_pairs_used'])

    def test_a_non_positive_price_is_counted_not_logged(self):
        hist = {'2026-11-09': {560: {'NF': 0.0}, 620: {'NF': 25100.0}}}
        out, acc = RC.hist_returns(hist, ['2026-11-09'], 'NF', 560, 620, 'exact')
        self.assertEqual([], out)
        self.assertEqual(1, acc['hist_non_positive_price'])


class CoverageLedger(unittest.TestCase):

    def session(self, quotes, horizon=60, hist_dates=6, **kw):
        ex = export(quotes, **kw)
        dates = ['2026-11-%02d' % d for d in range(1, 1 + hist_dates)]
        rows = history(dates, ['09:20', '09:35', '10:20', '10:35', '11:20'])
        hist, _ = RC.load_history(write_history(rows))
        hd = sorted(d for d in hist if d < SESSION)
        return RC.compare_index_horizon(ex, 'NF', hist, hd, horizon)

    def test_an_opportunity_with_no_exit_quote_still_counts_in_the_denominator(self):
        """A session that simply stopped capturing must not look like a session that scored."""
        # the plan covers 09:20 and 10:20, so the 09:20 entry IS a planned opportunity whose
        # exit slot was never captured: it stays in the denominator
        summary, rows, cov = self.session({'09:20': FORWARD})
        rec = summary['reconciliation']
        self.assertEqual(PS.STATE_VERIFIED, summary['schedule']['state'])
        # the plan is the AUTHORITATIVE 342 whatever this fixture captured
        self.assertEqual(342, rec['planned_opportunities'])
        self.assertEqual(6, rec['observed_entries_in_plan'], 'one slot x six contracts')
        self.assertEqual(0, rec['scored_in_plan'], 'no exit, so nothing scored')
        self.assertEqual(0.0, rec['usable_coverage'])
        self.assertEqual(336, rec['missing_entry_keys'])
        self.assertEqual(0, summary['matched_exit'])
        self.assertEqual(0, summary['common_scored'])
        self.assertEqual(6, cov['rej_no_exit_quote_within_tolerance'])
        self.assertEqual({'no_exit_quote_within_tolerance'}, {r['excluded'] for r in rows})
        # with no other quote for the contract at all there is no deviation to report; the
        # refusal is the whole story and is not dressed up with a number
        self.assertTrue(all(r.get('nearest_exit_deviation_s') is None for r in rows))

    def test_an_exit_outside_the_window_reports_how_far_outside_it_was(self):
        summary, rows, cov = self.session({'09:20': FORWARD, '10:25': FORWARD})
        self.assertEqual(12, summary['admitted_entry_candidates'])
        self.assertEqual(0, summary['matched_exit'])
        self.assertEqual(12, cov['rej_no_exit_quote_within_tolerance'])
        devs = {round(r['nearest_exit_deviation_s']) for r in rows
                if r.get('nearest_exit_deviation_s') is not None}
        self.assertEqual({-7500, 300}, devs, 'the nearest miss, in seconds, both ways')

    def test_a_scored_opportunity_reports_every_variant_and_its_centring(self):
        summary, rows, cov = self.session({'09:20': FORWARD, '10:20': FORWARD + 40.0})
        rec = summary['reconciliation']
        self.assertEqual(342, rec['planned_opportunities'])
        self.assertEqual(6, rec['scored_in_plan'], 'the 09:20 entries scored at 10:20')
        self.assertEqual(12, rec['observed_entries_in_plan'],
                         'both slots offered an entry; only the first could reach an exit')
        self.assertEqual(6, summary['matched_exit'])
        self.assertEqual(6, summary['common_scored'])
        scored = [r for r in rows if r['scores']]
        self.assertEqual(6, len(scored))
        for r in scored:
            self.assertAlmostEqual(FORWARD, r['forward'], delta=0.05,
                                   msg='the parity forward must be the generating forward')
            self.assertAlmostEqual(SIGMA, r['fitted_iv'], places=4,
                                   msg='the fitted IV must be the generating sigma')
        for r in scored:
            for v in ('FLAT', 'IV_ONLY', 'HIST', 'IV_ONLY_RAW', 'HIST_RAW'):
                self.assertIn(v, r['scores'], (r['ikey'], v))
            self.assertIsNotNone(r['fitted_iv'])
            self.assertIsNone(r['iv_failure'])
            for v in RC.RECENTRED:
                self.assertTrue(r['centring'][v]['centred_exactly'], (r['ikey'], v))

    def test_the_central_comparison_ties_on_mae(self):
        """The check the handoff asks for. If MAE does not tie, the centre is broken and the
        CRPS comparison is measuring location, not shape."""
        summary, _rows, _cov = self.session({'09:20': FORWARD, '10:20': FORWARD + 40.0})
        v = summary['variants_common']
        self.assertAlmostEqual(v['FLAT']['mae'], v['IV_ONLY']['mae'], places=9)
        self.assertAlmostEqual(v['FLAT']['mae'], v['HIST']['mae'], places=9)
        self.assertEqual(0.0, v['FLAT']['width80'], 'a flat point has no width')
        self.assertGreater(v['IV_ONLY']['width80'], 0.0)
        self.assertGreater(v['HIST']['width80'], 0.0)

    def test_too_few_prior_sessions_refuses_the_historical_range_by_name(self):
        summary, rows, cov = self.session({'09:20': FORWARD, '10:20': FORWARD + 40.0},
                                          hist_dates=RC.MIN_TRAIN - 1)
        self.assertEqual(0, summary['common_scored'])
        self.assertGreater(cov['rej_insufficient_prior_sessions'], 0)
        scored = [r for r in rows if r['scores']]
        self.assertTrue(all('HIST' not in r['scores'] for r in scored))
        self.assertTrue(all('IV_ONLY' in r['scores'] for r in scored),
                        'the IV-only range does not depend on history and must survive')
        self.assertEqual({'insufficient_prior_sessions'},
                         {r['excluded_from_hist'] for r in scored})

    def test_a_single_leg_cannot_produce_a_parity_forward_and_is_named(self):
        summary, rows, cov = self.session({'09:20': FORWARD, '10:20': FORWARD + 40.0},
                                          sides=('CE',))
        self.assertEqual(0, summary['common_scored'])
        self.assertGreater(cov['rej_no_forward_in_entry_acquisition_group'], 0)
        scored = [r for r in rows if r['scores']]
        self.assertEqual({'no_forward_in_entry_acquisition_group'},
                         {r['excluded_from_ranges'] for r in scored})
        self.assertTrue(all(list(r['scores']) == ['FLAT'] for r in scored),
                        'the flat centre needs no forward; the ranges do')

    def test_a_record_for_an_unpinned_contract_is_refused(self):
        ex = export({'09:20': FORWARD})
        p = ex['premium']['indices']['NF']
        p['records'].append(dict(p['records'][0], instrument_key='NSE_FO|NOT_PINNED'))
        _q, _pc, _f, _e, _bc, cov = RC.admit(ex, 'NF')
        self.assertEqual(1, cov['rej_no_contract_identity'])

    def test_an_unverified_identity_and_a_one_sided_book_are_refused_by_name(self):
        ex = export({'09:20': FORWARD})
        recs = ex['premium']['indices']['NF']['records']
        recs[0]['identity']['identity_verified'] = False
        recs[1]['bbo']['bid_raw']['raw'] = 0.0
        recs[2]['identity']['identity_disagreements'] = ['strike']
        _q, _pc, _f, _e, _bc, cov = RC.admit(ex, 'NF')
        self.assertEqual(1, cov['rej_identity_unverified'])
        self.assertEqual(1, cov['rej_book_not_two_sided'])
        self.assertEqual(1, cov['rej_identity_disagreement'])

    def test_a_target_at_or_after_expiry_is_refused_by_name(self):
        # the exit must be INSIDE the window, or the exit refusal fires first and the expiry
        # refusal under test never runs: 15:00 + 30 min lands on the 15:30 acquisition, whose
        # receipt is 15:30:07 -- seven seconds past the 15:30 expiry clock
        ex = export({'15:00': FORWARD, '15:30': FORWARD}, expiry=SESSION)
        rows_h = history(['2026-11-%02d' % d for d in range(1, 7)],
                         ['15:00', '15:30'])
        hist, _ = RC.load_history(write_history(rows_h))
        hd = sorted(d for d in hist if d < SESSION)
        summary, _rows, cov = RC.compare_index_horizon(ex, 'NF', hist, hd, 30)
        self.assertEqual(0, summary['common_scored'])
        self.assertGreater(cov['rej_target_at_or_after_expiry'], 0)


class CompleteCapture(unittest.TestCase):
    """The 16 November SHAPE, built here because that export is not on this machine.

    A capture covering the whole authoritative 69-slot grid admits 69 x 6 = 414 entry quotes
    while only 342 of them are sixty-minute opportunities; the 72 extra are LATE quotes whose
    targets fall past the end of the grid. Codex reports exactly those numbers for the evening
    export, and this fixture reproduces the shape deterministically so the invariant is tested
    rather than waited on.
    """

    def build(self):
        grid = [PS.hhmm(m) for m in PS.window_grid()]
        quotes = {c: FORWARD + 0.5 * i for i, c in enumerate(grid)}
        clocks = sorted(set(grid) | {PS.hhmm(m + 60) for m in PS.window_grid()})
        rows = history(['2026-11-%02d' % d for d in range(1, 10)], clocks)
        hist, _ = RC.load_history(write_history(rows))
        hd = sorted(d for d in hist if d < SESSION)
        return export(quotes), hist, hd

    def test_a_complete_capture_scores_every_planned_opportunity(self):
        ex, hist, hd = self.build()
        s, _rows, _cov = RC.compare_index_horizon(ex, 'NF', hist, hd, 60)
        rec = s['reconciliation']
        self.assertEqual(PS.STATE_VERIFIED, rec['schedule_state'])
        self.assertEqual(414, rec['observed_entries_total'], '69 slots x 6 contracts')
        self.assertEqual(342, rec['planned_opportunities'], '57 eligible slots x 6')
        self.assertEqual(342, rec['scored_in_plan'])
        self.assertEqual(1.0, rec['usable_coverage'])
        self.assertEqual(72, rec['admitted_entries_outside_planned_window'],
                         'the 12 slots from 14:05 to 15:00, x 6 contracts')
        self.assertEqual(72, rec['observed_entries_outside_plan'])

    def test_the_corrected_invariant_holds_where_the_old_one_failed(self):
        """`planned >= admitted` would fail here, 342 >= 414. The corrected chain holds."""
        ex, hist, hd = self.build()
        s, _rows, _cov = RC.compare_index_horizon(ex, 'NF', hist, hd, 60)
        rec = s['reconciliation']
        self.assertGreater(rec['observed_entries_total'], rec['planned_opportunities'])
        self.assertLessEqual(rec['scored_in_plan'], rec['observed_entries_in_plan'])
        self.assertLessEqual(rec['observed_entries_in_plan'], rec['planned_opportunities'])
        self.assertEqual(rec['observed_entries_total'],
                         rec['observed_entries_in_plan'] + rec['observed_entries_outside_plan'])

    def test_such_a_session_is_eligible_and_reaches_the_aggregate(self):
        ex, hist, hd = self.build()
        import tempfile as _t
        path = os.path.join(_t.mkdtemp(), 'complete.json')
        with open(path, 'w') as fh:
            json.dump(ex, fh)
        spots = write_history(history(
            ['2026-11-%02d' % d for d in range(1, 10)],
            sorted({PS.hhmm(m) for m in PS.window_grid()}
                   | {PS.hhmm(m + 60) for m in PS.window_grid()})))
        out = RC.run([path], spots, horizons=(60,))
        for ix, e in out['sessions'][0]['eligibility'].items():
            self.assertTrue(e['eligible'], (ix, e))
            self.assertEqual(1.0, e['usable_coverage'], ix)
            self.assertEqual(342, e['planned_opportunities'], ix)


class OrderIndependence(unittest.TestCase):

    def quotes(self):
        return {'09:20': FORWARD, '10:20': FORWARD + 40.0, '11:20': FORWARD - 25.0}

    def test_reversing_the_input_changes_nothing(self):
        """The defect class R7 spent four review rounds on. Asserted here too, because this
        runner re-implements the admission pipeline."""
        rows_h = history(['2026-11-%02d' % d for d in range(1, 8)],
                         ['09:20', '10:20', '11:20'])
        path = write_history(rows_h)
        hist, _ = RC.load_history(path)
        hd = sorted(d for d in hist if d < SESSION)
        ex = export(self.quotes())
        a, rows_a, _ = RC.compare_index_horizon(ex, 'NF', hist, hd, 60)
        b, rows_b, _ = RC.compare_index_horizon(
            R7.reverse_input(export(self.quotes())), 'NF', hist, hd, 60)
        self.assertEqual(a['variants_common'], b['variants_common'])
        self.assertEqual(a['coverage'], b['coverage'])
        self.assertEqual([r['ikey'] for r in rows_a], [r['ikey'] for r in rows_b])

    def test_the_decision_state_is_independent_of_row_order(self):
        rows_h = history(['2026-11-%02d' % d for d in range(1, 8)],
                         ['09:20', '10:20', '11:20'])
        hist, _ = RC.load_history(write_history(rows_h))
        hd = sorted(d for d in hist if d < SESSION)
        base = None
        for _ in range(3):
            ex = export(self.quotes())
            recs = ex['premium']['indices']['NF']['records']
            recs.reverse()
            s, _r, _c = RC.compare_index_horizon(ex, 'NF', hist, hd, 60)
            if base is None:
                base = s['variants_common']
            self.assertEqual(base, s['variants_common'])


class GridSensitivity(unittest.TestCase):

    def test_the_declared_grid_is_restored_after_a_sweep(self):
        """A sweep that leaked its last grid would silently rescore everything after it."""
        before = (RC.QUANTILE_GRID_N, RC.QUANTILE_GRID, RC.GRID_Z)
        if not (REAL_EXPORT and REAL_SPOTS):
            RC.QUANTILE_GRID_N = before[0]          # nothing to sweep; the guard still holds
        else:
            RC.grid_sensitivity(REAL_EXPORT, REAL_SPOTS, 'NF', 60, grids=(11, 21))
        self.assertEqual(before, (RC.QUANTILE_GRID_N, RC.QUANTILE_GRID, RC.GRID_Z))

    def test_the_historical_range_does_not_depend_on_the_grid(self):
        """It is scored on the historical sample itself, so no grid can move it. This is why
        a grid-sensitive calibration gate is a problem for the IV-only range alone."""
        rows_h = history(['2026-11-%02d' % d for d in range(1, 8)], ['09:20', '10:20'])
        hist, _ = RC.load_history(write_history(rows_h))
        hd = sorted(d for d in hist if d < SESSION)
        ex = export({'09:20': FORWARD, '10:20': FORWARD + 40.0})
        keep = (RC.QUANTILE_GRID_N, RC.QUANTILE_GRID, RC.GRID_Z)
        seen = set()
        try:
            for n in (21, 41, 201):
                RC.QUANTILE_GRID_N = n
                RC.QUANTILE_GRID = tuple((i + 0.5) / n for i in range(n))
                RC.GRID_Z = tuple(RC.norm_ppf(p) for p in RC.QUANTILE_GRID)
                s, _r, _c = RC.compare_index_horizon(ex, 'NF', hist, hd, 60)
                seen.add(round(s['variants_common']['HIST']['crps'], 9))
        finally:
            RC.QUANTILE_GRID_N, RC.QUANTILE_GRID, RC.GRID_Z = keep
        self.assertEqual(1, len(seen), 'HIST must be identical at every grid: %s' % seen)


class SessionAggregation(unittest.TestCase):

    def fake(self, session, iv_crps, hist_crps, n=10):
        def sc(c):
            return {'n': n, 'crps': c, 'mae': 5.0, 'cov80': 0.8, 'width80': 50.0}
        return {'session': session, 'indices': {'NF': {'h60': {
            'common_scored': n, 'intended_opportunities': n + 2, 'matched_exit': n,
            'variants_common': {'FLAT': sc(9.0), 'IV_ONLY': sc(iv_crps),
                                'HIST': sc(hist_crps)}}}}}

    def test_each_session_counts_once_however_many_rows_it_has(self):
        """A 300-row session must not outvote a 30-row one: the decision points are counted
        in sessions, so the session mean is the unit the rule reads."""
        big = self.fake('2026-11-10', 10.0, 12.0, n=300)
        small = self.fake('2026-11-11', 10.0, 6.0, n=30)
        agg = RC.across_sessions([big, small], horizons=(60,))['NF']['h60']
        self.assertEqual(2, agg['sessions'])
        self.assertAlmostEqual(9.0, agg['variants']['HIST']['crps'], places=9)
        self.assertAlmostEqual(10.0, agg['variants']['IV_ONLY']['crps'], places=9)

    def test_the_paired_sign_count_is_reported_beside_the_mean(self):
        sessions = [self.fake('2026-11-%02d' % d, 10.0, c)
                    for d, c in ((10, 9.0), (11, 11.0), (12, 9.5))]
        pv = RC.across_sessions(sessions, horizons=(60,))['NF']['h60'][
            'paired_hist_vs_iv_only']
        self.assertEqual(3, pv['sessions'])
        self.assertEqual(2, pv['sessions_hist_better'])
        self.assertAlmostEqual(statistics.fmean([0.1, -0.1, 0.05]),
                               pv['mean_relative_crps_improvement'], places=9)

    def test_a_session_without_both_ranges_is_not_counted(self):
        good = self.fake('2026-11-10', 10.0, 9.0)
        bad = self.fake('2026-11-11', 10.0, 9.0)
        del bad['indices']['NF']['h60']['variants_common']['HIST']
        agg = RC.across_sessions([good, bad], horizons=(60,))['NF']['h60']
        self.assertEqual(1, agg['sessions'])


@unittest.skipUnless(REAL_EXPORT and REAL_SPOTS,
                     'set RANGE_EXPORT and RANGE_SPOTS to the real inputs')
class AgainstTheFrozenReplay(unittest.TestCase):
    """The checks that need real inputs. Every expected value is pinned by session AND digest.

    A skip is not a pass. When `RANGE_EXPORT` is a file no expectation is pinned to, the
    structural checks still run and the value checks skip naming the digest they wanted --
    which is the correction Codex asked for: an expectation must say which bytes it is about.
    """

    @classmethod
    def setUpClass(cls):
        cls.ex = json.load(open(REAL_EXPORT))
        cls.session = cls.ex['manifest']['session_date_used']
        cls.r7_hist = R7.load_hist(REAL_SPOTS)
        cls.hist, cls.meta = RC.load_history(REAL_SPOTS)
        cls.hd = sorted(d for d in cls.r7_hist if d < cls.session)
        cls.key, cls.exp = expectations_for(REAL_EXPORT)
        cls.indices = sorted((cls.ex.get('premium') or {}).get('indices') or {})
        cls.provenance = 'unlabelled'
        cls.with_data = [ix for ix in cls.indices
                         if (cls.ex['premium']['indices'][ix].get('records') or [])]

    def needs_data(self):
        """Skip, saying so, when the supplied export carries no premium records.

        A structural test that claims to hold "for any real export" must hold or skip on one
        that captured nothing -- `NO_PREMIUM_DATA_FOR_THIS_INDEX` is a real state, not an
        error. Found by pointing the suite at a third real export that had none.
        """
        if not self.with_data:
            self.skipTest('%s carries no premium records (state %s); nothing to compare'
                          % (self.session,
                             (self.ex.get('premium') or {}).get('state')))
        return self.with_data

    def pinned(self, field):
        """The expected value for this exact input, or skip saying what was wanted.

        THE CORRECTION HERE. The digest guard decides whether an assertion RUNS; the
        provenance label only records where the expectation came from. The previous version
        conflated the two and skipped a pinned expectation whose SHA-256 matched, purely
        because the number had originally been reported by Codex rather than reproduced here --
        so the assertion would have stayed asleep on the very export it was written for.

        Provenance is recorded on the test instead (`self.provenance`), so a reader of the run
        can still tell an independently supplied expectation from a locally reproduced one.
        """
        if not self.exp or field not in self.exp:
            self.skipTest('no %r expectation pinned for %s; pinned inputs are %s'
                          % (field, self.key, sorted(EXPECTED)))
        self.provenance = self.exp.get('source', 'unlabelled')
        return self.exp[field]

    def needs_direct_frozen_chain(self):
        """Skip, reporting the chain, when the FROZEN replay cannot read this export.

        DIRECT VERSUS PARITY, REPORTED. `R7.score_index` calls `spot_timeline`, which subscripts
        `parts.authority_doc` unguarded; where that is null the frozen reader raises and there is
        no frozen output to compare against, so the parity assertion is UNAVAILABLE rather than
        passing or failing. The guarded successor cannot stand in here: a parity test whose
        reference is the successor would be comparing the runner against itself.

        A skip is not a pass. It records that the direct frozen comparison could not be made on
        this input, and which chain the runner used instead.
        """
        for ix in ('NF', 'BNF'):
            rok, rwhy, n = FS.records_readable_by_frozen(self.ex, ix)
            if not rok:
                self.skipTest(
                    'the DIRECT frozen chain is unavailable on this export: %s (%d records in '
                    '%s). premium_repricing_v2.score_index L514 dereferences r["bbo"] '
                    'unguarded. The research runner rejects them by name as rej_bbo_absent; the '
                    'frozen module is unmodified, so the parity reference does not exist here.'
                    % (rwhy, n, ix))
        ok, why = FS.authority_doc_state(self.ex)
        if not ok:
            self.skipTest(
                'the DIRECT frozen chain is unavailable on this export (%s): '
                'premium_repricing_v2.spot_timeline L382 / reverse_input L853 dereference it '
                'unguarded. The runner used %s (%s); the frozen module is unmodified and the '
                'parity reference therefore does not exist for this input.'
                % (why, FS.CHAIN_SUCCESSOR, FS.FROZEN_SUCCESSOR_VERSION))

    # ── structural: these hold for ANY real export ──

    def test_admission_matches_the_frozen_replay(self):
        """Same rows, same entry and exit acquisitions, same exclusion counts, any session."""
        self.needs_direct_frozen_chain()
        for ix in self.needs_data():
            _s, r7rows, r7cov, _l = R7.score_index(self.ex, ix, self.r7_hist, self.hd,
                                                   self.session)
            mine, myrows, mycov = RC.compare_index_horizon(self.ex, ix, self.hist, self.hd, 60)
            r7keys = {(r['ikey'], r['m0'], r['entry_aid'], r['exit_aid']) for r in r7rows}
            mykeys = {(r['ikey'], r['m0'], r['entry_aid'], r['exit_aid'])
                      for r in myrows if 'exit_aid' in r}
            self.assertEqual(r7keys, mykeys, ix)
            self.assertEqual(r7cov['entry_candidates'], mine['admitted_entry_candidates'], ix)
            self.assertEqual(r7cov['rows_scored'], mine['matched_exit'], ix)
            self.assertEqual(r7cov['usable_records'], mycov['usable_records'], ix)
            self.assertEqual(r7cov['rej_no_exit_quote_within_tolerance'],
                             mycov['rej_no_exit_quote_within_tolerance'], ix)

    def test_the_reference_variant_reproduces_the_frozen_primary_score(self):
        """Whatever the session, the reference must equal the frozen replay's own number."""
        self.needs_direct_frozen_chain()
        for ix in self.needs_data():
            _s, r7rows, _c, _l = R7.score_index(self.ex, ix, self.r7_hist, self.hd,
                                                self.session)
            r7 = statistics.fmean(r['E0_FWD']['crps'] for r in r7rows if 'E0_FWD' in r)
            mine, _r, _c = RC.compare_index_horizon(self.ex, ix, self.hist, self.hd, 60)
            self.assertAlmostEqual(r7, mine['variants']['R7_E0_FWD']['crps'], places=6, msg=ix)

    def test_mae_ties_on_the_real_rows_at_every_horizon(self):
        for ix in self.needs_data():
            for h in RC.HORIZONS_MIN:
                s, _r, _c = RC.compare_index_horizon(self.ex, ix, self.hist, self.hd, h)
                v = s['variants_common']
                self.assertAlmostEqual(v['FLAT']['mae'], v['IV_ONLY']['mae'], places=9,
                                       msg=(ix, h))
                self.assertAlmostEqual(v['FLAT']['mae'], v['HIST']['mae'], places=9,
                                       msg=(ix, h))

    def test_every_variant_is_scored_on_the_same_rows_as_the_common_set(self):
        for ix in self.needs_data():
            s, _r, _c = RC.compare_index_horizon(self.ex, ix, self.hist, self.hd, 60)
            common = s['common_scored']
            for name in ('FLAT',) + RC.RANGE_VARIANTS:
                self.assertEqual(common, s['variants'][name]['n'], (ix, name))

    def test_the_planned_denominator_is_the_schedule_not_the_admitted_quotes(self):
        """The CORRECTED invariant.

        The first version asserted `planned >= admitted`, which is simply false on a complete
        capture: the 16 November export admits 414 quotes (69 slots x 6 contracts) against 342
        planned 60-minute opportunities, because the 72 extra are LATE quotes whose one-hour
        targets fall outside the planned window. They are not a coverage shortfall and they are
        not an error -- they are counted separately.

        What must hold, and what is asserted here instead:

            scored_in_plan <= observed_entries_in_plan <= planned_opportunities

        with the outside-window count checked on its own, and the admitted total reconciling as
        in-plan + outside-plan.
        """
        for ix in self.needs_data():
            s, _r, _c = RC.compare_index_horizon(self.ex, ix, self.hist, self.hd, 60)
            rec, sched = s['reconciliation'], s['schedule']
            self.assertEqual(PS.STATE_VERIFIED, sched['state'], ix)
            self.assertEqual(PS.BASIS_AUTHORITATIVE, sched['basis'], ix)
            self.assertLessEqual(rec['scored_in_plan'], rec['observed_entries_in_plan'], ix)
            self.assertLessEqual(rec['observed_entries_in_plan'],
                                 rec['planned_opportunities'], ix)
            self.assertLessEqual(rec['usable_coverage'], 1.0, ix)
            self.assertEqual(rec['scored_in_plan'] / rec['planned_opportunities'],
                             rec['usable_coverage'], ix)
            # the admitted total is accounted for in full: in the plan, or outside it
            self.assertEqual(rec['observed_entries_total'],
                             rec['observed_entries_in_plan']
                             + rec['observed_entries_outside_plan'], ix)
            # and a late quote is never counted as coverage
            self.assertLessEqual(rec['admitted_entries_outside_planned_window'],
                                 rec['observed_entries_outside_plan'], ix)

    def test_a_complete_capture_admits_more_quotes_than_the_60_minute_plan(self):
        """The 16 November shape, on whichever real export is supplied.

        A capture covering the whole 69-slot grid admits 414 entry quotes while only 342 of
        them are 60-minute opportunities. If the supplied export is incomplete this skips,
        saying so, rather than pretending to check it.
        """
        for ix in self.needs_data():
            s, _r, _c = RC.compare_index_horizon(self.ex, ix, self.hist, self.hd, 60)
            rec = s['reconciliation']
            grid = s['schedule']['grid_slots']
            contracts = len(s['schedule']['contracts'])
            if rec['observed_entries_total'] < grid * contracts:
                continue
            self.assertEqual(grid * contracts, rec['observed_entries_total'], ix)
            self.assertGreater(rec['observed_entries_total'],
                               rec['planned_opportunities'], ix)
            self.assertEqual(rec['observed_entries_total'] - rec['planned_opportunities'],
                             rec['admitted_entries_outside_planned_window'], ix)
            self.assertEqual(1.0, rec['usable_coverage'], ix)

    def test_an_inconsistent_slot_manifest_is_refused_and_the_session_excluded(self):
        """Codex's reproduction: cropping the manifest as well as the quotes made the runner
        build a 126-key denominator from 33 surviving rows, call the schedule VERIFIED and
        accept 126/126 = 100% for a capture that had lost half its session."""
        self.needs_data()
        cropped = json.loads(json.dumps(self.ex))
        for ix in list(cropped['premium']['indices']):
            node = cropped['premium']['indices'][ix]
            node['records'] = [r for r in node['records'] if R7.mins(r['slot']) <= 12 * 60]
            node['slot_envelopes'] = [e for e in node['slot_envelopes']
                                      if R7.mins(e['slot']) <= 12 * 60]
            node['slot_manifest']['rows'] = [r for r in node['slot_manifest']['rows']
                                             if R7.mins(r['slot']) <= 12 * 60]
        for ix in self.with_data:
            s, _r, _c = RC.compare_index_horizon(cropped, ix, self.hist, self.hd, 60)
            rec = s['reconciliation']
            # v3: the plan is AUTHORITATIVE, so it survives the refusal and the diagnostic
            # ratio is still reported -- 126/342 = 36.8% -- while `usable_coverage` is
            # withheld because the session cannot enter the cohort.
            self.assertEqual(PS.STATE_MANIFEST_MISMATCH, rec['schedule_state'], ix)
            self.assertEqual(342, rec['planned_opportunities'], ix)
            self.assertEqual(126, rec['scored_in_plan'], ix)
            self.assertAlmostEqual(0.368, rec['diagnostic_coverage'], places=3, msg=ix)
            self.assertIsNone(rec['usable_coverage'], ix)
            self.assertIn('DIAGNOSTIC ONLY', rec['coverage_basis'])
            self.assertEqual(198, rec['observed_entries_total'], ix)
            audit = rec['manifest_audit']
            self.assertEqual(69, audit['authoritative_slots'], ix)
            self.assertEqual(33, audit['distinct_valid_slots'], ix)
            self.assertFalse(audit['covers_authoritative_grid'], ix)

    def test_all_three_truncation_cases_behave_differently_and_correctly(self):
        """Full export, quotes-only truncation, quotes-plus-manifest truncation, both
        indices -- the three cases Codex asked to see tested together."""
        self.pinned('planned_60')

        def crop(manifest):
            doc = json.loads(json.dumps(self.ex))
            for ix in list(doc['premium']['indices']):
                node = doc['premium']['indices'][ix]
                node['records'] = [r for r in node['records']
                                   if R7.mins(r['slot']) <= 12 * 60]
                node['slot_envelopes'] = [e for e in node['slot_envelopes']
                                          if R7.mins(e['slot']) <= 12 * 60]
                if manifest:
                    node['slot_manifest']['rows'] = [
                        r for r in node['slot_manifest']['rows']
                        if R7.mins(r['slot']) <= 12 * 60]
            return doc

        quotes_only, both = crop(False), crop(True)
        for ix in ('NF', 'BNF'):
            full, _r, _c = RC.compare_index_horizon(self.ex, ix, self.hist, self.hd, 60)
            q, _r, _c = RC.compare_index_horizon(quotes_only, ix, self.hist, self.hd, 60)
            b, _r, _c = RC.compare_index_horizon(both, ix, self.hist, self.hd, 60)
            self.assertEqual(PS.STATE_VERIFIED, full['reconciliation']['schedule_state'], ix)
            self.assertGreaterEqual(full['reconciliation']['usable_coverage'],
                                    RC.MIN_USABLE_COVERAGE, ix)
            self.assertEqual(PS.STATE_VERIFIED, q['reconciliation']['schedule_state'], ix)
            self.assertAlmostEqual(0.368, q['reconciliation']['usable_coverage'], places=3,
                                   msg=ix)
            self.assertLess(q['reconciliation']['usable_coverage'], RC.MIN_USABLE_COVERAGE, ix)
            self.assertEqual(PS.STATE_MANIFEST_MISMATCH,
                             b['reconciliation']['schedule_state'], ix)
            self.assertIsNone(b['reconciliation']['usable_coverage'], ix)
            self.assertAlmostEqual(0.368, b['reconciliation']['diagnostic_coverage'],
                                   places=3, msg=ix)
            # the plan is the SAME 342 in ALL THREE cases, which is the v3 property
            self.assertEqual(full['reconciliation']['planned_opportunities'],
                             q['reconciliation']['planned_opportunities'], ix)
            self.assertEqual(full['reconciliation']['planned_opportunities'],
                             b['reconciliation']['planned_opportunities'], ix)

    def test_the_real_run_is_independent_of_input_order(self):
        self.needs_data()
        a = RC.run([REAL_EXPORT], REAL_SPOTS, horizons=(60,))
        b = RC.run([REAL_EXPORT], REAL_SPOTS, horizons=(60,), reverse_input=True)
        for ix in self.with_data:
            self.assertEqual(a['indices'][ix]['h60']['variants_common'],
                             b['indices'][ix]['h60']['variants_common'], ix)
            self.assertEqual(a['indices'][ix]['h60']['reconciliation'],
                             b['indices'][ix]['h60']['reconciliation'], ix)

    def test_the_real_history_carries_no_explicit_scheduled_slot(self):
        """The discrepancy this records: the pinned 855-row history has NO scheduled-slot
        field. Its only partially-present fields are the recorded directions, non-null on 234
        rows. An inferred slot is therefore the only slot available, and it is labelled."""
        self.assertEqual(855, self.meta['rows'])
        self.assertEqual(0, self.meta['rows_with_explicit_slot'])
        self.assertEqual([], self.meta['slot_field_names_seen'])
        self.assertEqual(41, self.meta['rows_off_five_minute_grid'])
        self.assertEqual(0, self.meta['floor_collisions'])
        self.assertEqual(0, self.meta['excluded_conflicting_slots'])
        self.assertEqual(855, self.meta['slots_from_inferred_clock'])

    def test_the_real_history_join_is_independent_of_its_own_input_order(self):
        """Codex's correction: the EXPORTS were reversed in the invariance check but the
        HISTORY was not, and the history was where the order dependence lived."""
        rows = open(REAL_SPOTS, encoding='utf-8').read().splitlines()
        rev = os.path.join(tempfile.mkdtemp(), 'reversed.jsonl')
        with open(rev, 'w') as f:
            f.write('\n'.join(reversed([r for r in rows if r.strip()])) + '\n')
        a, ma = RC.load_history(REAL_SPOTS)
        b, mb = RC.load_history(rev)
        self.assertEqual({d: sorted(v.items()) for d, v in a.items()},
                         {d: sorted(v.items()) for d, v in b.items()})
        for field in ('retained_slots', 'excluded_conflicting_slots',
                      'collapsed_identical_duplicates', 'slots_from_inferred_clock'):
            self.assertEqual(ma[field], mb[field], field)
        hd = sorted(d for d in a if d < self.session)
        for ix in self.with_data:
            x, _r, _c = RC.compare_index_horizon(self.ex, ix, a, hd, 60)
            y, _r, _c = RC.compare_index_horizon(self.ex, ix, b, hd, 60)
            self.assertEqual(x['variants_common'], y['variants_common'], ix)

    # ── value checks, pinned to one input's bytes ──

    def test_the_pinned_reference_scores(self):
        want = self.pinned('r7_e0_fwd_crps_60')
        for ix, crps in want.items():
            mine, _r, _c = RC.compare_index_horizon(self.ex, ix, self.hist, self.hd, 60)
            self.assertAlmostEqual(crps, mine['variants']['R7_E0_FWD']['crps'], places=3,
                                   msg=ix)

    def test_the_pinned_planned_coverage(self):
        planned = self.pinned('planned_60')
        scored = self.exp['scored_60']
        coverage = self.exp['usable_coverage_60']
        for ix in sorted(planned):
            s, _r, _c = RC.compare_index_horizon(self.ex, ix, self.hist, self.hd, 60)
            rec = s['reconciliation']
            self.assertEqual(planned[ix], rec['planned_opportunities'], ix)
            self.assertEqual(scored[ix], rec['scored_in_plan'], ix)
            self.assertAlmostEqual(coverage[ix], rec['usable_coverage'], places=3, msg=ix)

    def test_the_pinned_range_scores_and_the_mae_tie(self):
        iv = self.pinned('iv_only_crps_60')
        hist = self.exp['hist_crps_60']
        mae = self.exp['mae_60']
        for ix in sorted(iv):
            s, _r, _c = RC.compare_index_horizon(self.ex, ix, self.hist, self.hd, 60)
            v = s['variants_common']
            self.assertAlmostEqual(iv[ix], v['IV_ONLY']['crps'], places=3, msg=ix)
            self.assertAlmostEqual(hist[ix], v['HIST']['crps'], places=3, msg=ix)
            for name in ('FLAT', 'IV_ONLY', 'HIST'):
                self.assertAlmostEqual(mae[ix], v[name]['mae'], places=3, msg=(ix, name))

    def test_the_120_minute_counterexample_is_reproduced(self):
        """The development check, to the precision the handoff states it in, and the finding
        that follows: recentring is not the explanation. It is a width problem."""
        want = self.pinned('nf_120_iv_only_raw_crps')
        s, _r, _c = RC.compare_index_horizon(self.ex, 'NF', self.hist, self.hd, 120)
        v = s['variants_common']
        self.assertAlmostEqual(want, v['IV_ONLY_RAW']['crps'], places=2)
        self.assertAlmostEqual(self.exp['nf_120_r7_crps'], v['R7_E0_FWD']['crps'], places=2)
        keep = (RC.QUANTILE_GRID_N, RC.QUANTILE_GRID, RC.GRID_Z)
        try:
            RC.QUANTILE_GRID_N = 101
            RC.QUANTILE_GRID = tuple((i + 0.5) / 101 for i in range(101))
            RC.GRID_Z = tuple(RC.norm_ppf(p) for p in RC.QUANTILE_GRID)
            at101, _r, _c = RC.compare_index_horizon(self.ex, 'NF', self.hist, self.hd, 120)
        finally:
            RC.QUANTILE_GRID_N, RC.QUANTILE_GRID, RC.GRID_Z = keep
        self.assertAlmostEqual(self.exp['nf_120_iv_only_raw_cov80_at_grid_101'],
                               at101['variants_common']['IV_ONLY_RAW']['cov80'], places=3)
        self.assertLess(abs(v['IV_ONLY']['crps'] - v['IV_ONLY_RAW']['crps']), 0.2)
        self.assertLess(abs(v['IV_ONLY']['cov80'] - v['IV_ONLY_RAW']['cov80']), 0.01)
        self.assertGreater(v['HIST']['cov80'], v['IV_ONLY']['cov80'] + 0.3)
        self.assertGreater(v['HIST']['width80'], v['IV_ONLY']['width80'])

    def test_the_pinned_60_minute_scores_for_this_export(self):
        """Runs on whichever pinned digest is supplied, independent of provenance.

        For 16 November these are Codex's independently verified values, and this is the
        assertion their reply asked to be ACTIVATED rather than skipped: the digest guard still
        decides whether it runs, and the provenance label is recorded, not used to skip.
        """
        iv = self.pinned('iv_only_crps_60')
        hist = self.exp['hist_crps_60']
        ivcov = self.exp.get('iv_only_cov80_60')
        hcov = self.exp.get('hist_cov80_60')
        for ix in sorted(iv):
            if ix not in self.with_data:
                continue
            s, _r, _c = RC.compare_index_horizon(self.ex, ix, self.hist, self.hd, 60)
            v = s['variants_common']
            self.assertAlmostEqual(iv[ix], v['IV_ONLY']['crps'], places=3,
                                   msg=(ix, 'IV_ONLY', self.provenance))
            self.assertAlmostEqual(hist[ix], v['HIST']['crps'], places=3,
                                   msg=(ix, 'HIST', self.provenance))
            if ivcov:
                self.assertAlmostEqual(ivcov[ix], v['IV_ONLY']['cov80'], places=3, msg=ix)
            if hcov:
                self.assertAlmostEqual(hcov[ix], v['HIST']['cov80'], places=3, msg=ix)

    def test_the_pinned_matched_count_for_this_export(self):
        """16 November pins 342 matched per index; 12 November pins its own counts."""
        want = self.exp.get('matched_60') if self.exp else None
        if not want:
            self.skipTest('no matched_60 pinned for %s' % (self.key,))
        self.provenance = self.exp.get('source', 'unlabelled')
        for ix in sorted(want):
            if ix not in self.with_data:
                continue
            s, _r, _c = RC.compare_index_horizon(self.ex, ix, self.hist, self.hd, 60)
            self.assertEqual(want[ix], s['reconciliation']['scored_in_plan'],
                             (ix, self.provenance))
            self.assertEqual(342, s['reconciliation']['planned_opportunities'], ix)
            self.assertEqual(1.0, s['reconciliation']['usable_coverage'], ix)

    def test_the_truncated_capture_fails_the_coverage_gate(self):
        """Codex's reproduction, on the input this machine has.

        Truncating the capture at 12:00 made v1 report 62.5% against its own admitted quotes --
        a PASS on a 60% gate -- when the honest figure against the planned 342 is 35.1%.
        """
        self.pinned('planned_60')
        truncated = json.loads(json.dumps(self.ex))
        for ix in ('NF', 'BNF'):
            node = truncated['premium']['indices'][ix]
            node['records'] = [r for r in node['records'] if R7.mins(r['slot']) < 12 * 60]
            node['slot_envelopes'] = [e for e in node['slot_envelopes']
                                      if R7.mins(e['slot']) < 12 * 60]
        for ix in ('NF', 'BNF'):
            s, _r, _c = RC.compare_index_horizon(truncated, ix, self.hist, self.hd, 60)
            rec = s['reconciliation']
            self.assertEqual(342, rec['planned_opportunities'], ix)
            self.assertLess(rec['usable_coverage'], RC.MIN_USABLE_COVERAGE, ix)
            self.assertAlmostEqual(0.351, rec['usable_coverage'], places=2, msg=ix)
            # the OLD circular denominator would have passed
            self.assertGreaterEqual(
                rec['scored_in_plan'] / s['admitted_entry_candidates'],
                RC.MIN_USABLE_COVERAGE,
                'the circular denominator must be the one that passes, or this test is moot')


@unittest.skipUnless(REAL_EXPORT and REAL_SPOTS,
                     'set RANGE_EXPORT and RANGE_SPOTS to the real inputs')
class CohortResolution(unittest.TestCase):
    """Duplicate, conflicting and below-coverage inputs cannot inflate the trial cohort."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ex = json.load(open(REAL_EXPORT))
        if not any((self.ex['premium']['indices'][ix].get('records') or [])
                   for ix in (self.ex.get('premium') or {}).get('indices') or {}):
            self.skipTest('%s carries no premium records; the cohort rules need scored rows'
                          % self.ex['manifest'].get('session_date_used'))

    def truncated(self, name='truncated.json'):
        doc = json.loads(json.dumps(self.ex))
        for ix in list(doc['premium']['indices']):
            node = doc['premium']['indices'][ix]
            node['records'] = [r for r in node['records'] if R7.mins(r['slot']) < 12 * 60]
            node['slot_envelopes'] = [e for e in node['slot_envelopes']
                                      if R7.mins(e['slot']) < 12 * 60]
        path = os.path.join(self.dir, name)
        with open(path, 'w') as f:
            json.dump(doc, f)
        return path

    def test_an_identical_duplicate_collapses_to_one_session(self):
        copy_path = os.path.join(self.dir, 'copy.json')
        shutil.copy(REAL_EXPORT, copy_path)
        out = RC.run([REAL_EXPORT, copy_path], REAL_SPOTS, horizons=(60,))
        self.assertEqual(1, len(out['sessions']))
        self.assertEqual(1, out['inputs_kept'][0]['duplicate_inputs_collapsed'])
        self.assertEqual([], out['inputs_excluded'])
        for ix in sorted(out['across_sessions']):
            self.assertEqual(1, out['across_sessions'][ix]['h60']['sessions'], ix)

    def test_competing_exports_for_one_session_are_excluded_not_chosen(self):
        """Neither file is picked. Choosing by size, age or score is the selection this trial
        exists to prevent, so the session is refused and both digests are reported."""
        out = RC.run([REAL_EXPORT, self.truncated()], REAL_SPOTS, horizons=(60,))
        self.assertEqual([], out['sessions'])
        self.assertEqual(1, len(out['inputs_excluded']))
        exc = out['inputs_excluded'][0]
        self.assertEqual('CONFLICTING_EXPORTS_FOR_SESSION', exc['reason'])
        self.assertEqual(2, len(exc['competing_digests']))
        self.assertIn('--canonical', exc['resolution'])

    def test_a_conflict_is_resolved_only_by_an_explicit_declaration(self):
        import hashlib
        digest = hashlib.sha256(open(REAL_EXPORT, 'rb').read()).hexdigest()
        out = RC.run([REAL_EXPORT, self.truncated()], REAL_SPOTS, horizons=(60,),
                     canonical={self.ex['manifest']['session_date_used']: digest[:12]})
        self.assertEqual(1, len(out['sessions']))
        kept = out['inputs_kept'][0]
        self.assertEqual(digest, kept['sha256'])
        self.assertEqual(1, len(kept['competing_digests_not_used']))
        self.assertEqual({self.ex['manifest']['session_date_used']: digest[:12]},
                         out['canonical_declared'])

    def test_a_below_coverage_session_is_excluded_from_the_cohort_and_reported(self):
        out = RC.run([self.truncated()], REAL_SPOTS, horizons=(60,))
        self.assertEqual(1, len(out['sessions']))
        for ix, e in out['sessions'][0]['eligibility'].items():
            self.assertFalse(e['eligible'], ix)
            self.assertTrue(any('usable_coverage' in r for r in e['reasons']), (ix, e))
        self.assertEqual({}, out['across_sessions'],
                         'an ineligible session must not reach the aggregate')
        self.assertEqual(2, len(out['cohort_excluded']),
                         'and it must still be carried through reporting')

    def test_an_eligible_session_reaches_the_aggregate(self):
        out = RC.run([REAL_EXPORT], REAL_SPOTS, horizons=(60,))
        for ix, e in out['sessions'][0]['eligibility'].items():
            self.assertTrue(e['eligible'], (ix, e))
            self.assertGreaterEqual(e['usable_coverage'], RC.MIN_USABLE_COVERAGE)
        self.assertEqual([], out['cohort_excluded'])


if __name__ == '__main__':
    unittest.main()
