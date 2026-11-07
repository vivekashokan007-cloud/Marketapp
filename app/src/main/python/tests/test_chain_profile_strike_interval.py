"""Commit A: chain_profile's strike interval (sparse-wing defect).

Defect, in brain.chain_profile before this commit:

    step = (all_k[1] - all_k[0]) if len(all_k) > 1 else (100 if spot > 30000 else 50)

all_k[1] - all_k[0] is the gap between the two LOWEST strikes. On an NSE monthly ladder those sit in
the sparse far wing, so a BNF monthly resolved ~1500 where the near-ATM interval is 100. `step` sets
the IV sampling distances (otm_dist = 0.5 sigma, otm_dist2 = 1.0 sigma), so ivSkew/ivSlope sampled the
empty outer wings and read 0.0. This is the same formula the 2026-09-01 candidate-generation bugfix
replaced via _infer_strike_step; chain_profile was never migrated.

The repair does NOT simply reuse _infer_strike_step. That helper returns min(gaps) over the whole
ladder -- its docstring says "modal/minimum" but the code is a pure minimum -- so one anomalous narrow
gap anywhere would silently set every sampling distance, and a global minimum need not be a local
interval. infer_strike_interval takes the modal gap NEAR THE MONEY, reports status explicitly, and
records whether the helper agrees.

Run:
    PYTHONPATH=app/src/main/python python3 app/src/main/python/tests/test_chain_profile_strike_interval.py
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain  # noqa: E402


# ───────────────────────── the brief's regression fixture, built explicitly ─────────────────────────
SPARSE_LOW = [40000, 41500, 43000, 44500]
DENSE = list(range(54000, 58001, 100))          # 54,000-58,000 inclusive, 100-pt steps
SPARSE_LADDER = SPARSE_LOW + DENSE
ATM = 56000
DAILY_SIGMA = 400.0
NEAR_IV_WINDOW = 500                            # call IV 20 / put IV 22 within +-500 of ATM


def leg(iv):
    """A chain leg. iv None means the field is absent, which is how the outer wings are missing."""
    d = {'oi': 1000, 'volume': 10, 'prev_oi': 900, 'ltp': 50.0, 'bid': 49.0, 'ask': 51.0}
    if iv is not None:
        d['iv'] = iv
    return d


def sparse_chain():
    """strikes [40000,41500,43000,44500] + 54000-58000/100; ATM 56000; daily sigma 400;
    nearby call IV 20 / put IV 22 within +-500 of ATM; outer-wing IV missing."""
    strikes = {}
    for k in SPARSE_LADDER:
        near = abs(k - ATM) <= NEAR_IV_WINDOW
        strikes[str(k)] = {'CE': leg(20 if near else None), 'PE': leg(22 if near else None)}
    return {'strikes': strikes, 'allStrikes': list(SPARSE_LADDER), 'atm': ATM,
            'dailySigma': DAILY_SIGMA, 'callWallStrike': 57000, 'putWallStrike': 55000,
            'expiry': '2026-11-23', 'requested_underlying_key': 'NSE_INDEX|Nifty Bank'}


def uniform_chain(atm, step, spot_unused=None, n=25, iv_near=20, iv_far=22):
    ladder = [atm + i * step for i in range(-n, n + 1) if atm + i * step > 0]
    strikes = {}
    for k in ladder:
        near = abs(k - atm) <= 5 * step
        strikes[str(k)] = {'CE': leg(iv_near if near else None), 'PE': leg(iv_far if near else None)}
    return {'strikes': strikes, 'allStrikes': ladder, 'atm': atm,
            'dailySigma': step * 4.0, 'callWallStrike': atm + step, 'putWallStrike': atm - step,
            'expiry': '2026-11-17', 'requested_underlying_key': 'NSE_INDEX|Nifty 50'}


def legacy_step(all_k, spot):
    """The pre-repair rule, kept here so the reproducer shows the old value rather than asserting it."""
    step = (all_k[1] - all_k[0]) if len(all_k) > 1 else (100 if spot > 30000 else 50)
    return step if step > 0 else (100 if spot > 30000 else 50)


# ───────────────────────────────── the defect and its repair ─────────────────────────────────
class TestSparseWingDefect(unittest.TestCase):
    def test_the_old_rule_really_does_pick_the_sparse_wing(self):
        """Reproducer: the pre-repair formula resolves the far-wing gap, not the near-ATM interval."""
        self.assertEqual(legacy_step(SPARSE_LADDER, 56000.0), 1500)
        self.assertEqual(SPARSE_LADDER[1] - SPARSE_LADDER[0], 1500)
        # and the true near-the-money interval is 100
        self.assertEqual(infer(SPARSE_LADDER, ATM)['value'], 100)

    def test_old_rule_samples_empty_wings_and_so_reads_zero(self):
        """With step=1500, otm_dist rounds to 0 and falls back to 1500, so the IV lookups land on
        strikes whose IV is absent -- producing 0.0 skew and 0.0 slope from a chain that does carry IV."""
        step = 1500
        otm = round(DAILY_SIGMA * 0.5 / step) * step or step
        self.assertEqual(otm, 1500)
        self.assertGreater(abs(otm), NEAR_IV_WINDOW)          # outside the populated band
        ch = sparse_chain()
        self.assertNotIn('iv', ch['strikes'][str(ATM + 1500)]['CE'])
        self.assertNotIn('iv', ch['strikes'][str(ATM - 1500)]['PE'])

    def test_repaired_profile_recovers_the_iv_features(self):
        """Run the REAL function and report what it actually produces; nothing is hardcoded from the
        earlier synthetic audit."""
        p = brain.chain_profile(sparse_chain(), 56000.0, {}, vix=20)
        self.assertIsNotNone(p)
        ev = p['strikeIntervalEvidence']
        self.assertEqual(ev['status'], 'OK')
        self.assertEqual(ev['value'], 100)
        self.assertEqual(ev['source'], 'NEAR_ATM_MODAL')
        self.assertFalse(ev['legacy_fallback_used'])
        # sampling distances now land inside the populated band
        self.assertEqual(round(DAILY_SIGMA * 0.5 / 100) * 100, 200)
        self.assertEqual(round(DAILY_SIGMA * 1.0 / 100) * 100, 400)
        # the features are non-zero because real IV is now read
        self.assertNotEqual(p['ivSkew'], 0.0)
        self.assertNotEqual(p['ivSlope'], 0.0)
        # record the measured values (put IV 22 - call IV 20 = 2.0; slope (22-20)/2 = 1.0)
        self.assertAlmostEqual(p['ivSkew'], 2.0, places=4)
        self.assertAlmostEqual(p['ivSlope'], 1.0, places=4)

    def test_helper_disagreement_is_recorded_not_absorbed(self):
        """min(gaps) over the whole sparse ladder happens to be 100 here; the point is that agreement
        is reported either way, so a future disagreement is visible."""
        ev = infer(SPARSE_LADDER, ATM)
        self.assertEqual(ev['helper_value'], 100)
        self.assertTrue(ev['helper_agrees'])


# ───────────────────────── why the existing helper is not reused as-is ─────────────────────────
def infer(*a, **kw):
    return brain.infer_strike_interval(*a, **kw)


class TestAnomalousGapRobustness(unittest.TestCase):
    def test_one_anomalous_narrow_gap_does_not_set_the_interval(self):
        """The brief's explicit requirement. A single tiny gap must not become the sampling distance."""
        ladder = sorted(set(list(range(55000, 57001, 100)) + [56050]))   # one stray 50-pt pair
        ev = infer(ladder, 56000)
        self.assertEqual(ev['value'], 100, 'modal near-ATM interval')
        self.assertEqual(ev['status'], 'OK')
        # the old helper WOULD have taken the anomaly
        self.assertEqual(brain._infer_strike_step(ladder, 100), 50)
        self.assertEqual(ev['helper_value'], 50)
        self.assertFalse(ev['helper_agrees'], 'the disagreement must be visible')

    def test_profile_is_not_derailed_by_the_anomalous_gap(self):
        ladder = sorted(set(list(range(54000, 58001, 100)) + [56050]))
        strikes = {}
        for k in ladder:
            near = abs(k - ATM) <= NEAR_IV_WINDOW
            strikes[str(k)] = {'CE': leg(20 if near else None), 'PE': leg(22 if near else None)}
        ch = {'strikes': strikes, 'allStrikes': ladder, 'atm': ATM, 'dailySigma': DAILY_SIGMA,
              'callWallStrike': 57000, 'putWallStrike': 55000}
        p = brain.chain_profile(ch, 56000.0, {}, vix=20)
        self.assertEqual(p['strikeIntervalEvidence']['value'], 100)
        self.assertAlmostEqual(p['ivSkew'], 2.0, places=4)


# ───────────────────────────────── normal ladders are unchanged ─────────────────────────────────
class TestNormalLaddersUnchanged(unittest.TestCase):
    def test_normal_nf_weekly_50pt(self):
        ev = infer([24000 + i * 50 for i in range(-20, 21)], 24000)
        self.assertEqual((ev['value'], ev['status']), (50, 'OK'))
        self.assertTrue(ev['helper_agrees'])

    def test_normal_bnf_100pt(self):
        ev = infer([56000 + i * 100 for i in range(-20, 21)], 56000)
        self.assertEqual((ev['value'], ev['status']), (100, 'OK'))

    def test_uniform_profile_identical_to_the_legacy_rule(self):
        """On a uniformly spaced ladder the old and new rules agree, so existing consumers see no
        change -- which is what makes the sparse case a repair rather than a behaviour change."""
        for atm, step, spot in ((24000, 50, 24000.0), (56000, 100, 56000.0)):
            ch = uniform_chain(atm, step)
            self.assertEqual(legacy_step(ch['allStrikes'], spot), step)
            p = brain.chain_profile(ch, spot, {}, vix=20)
            self.assertEqual(p['strikeIntervalEvidence']['value'], step)
            self.assertFalse(p['strikeIntervalEvidence']['legacy_fallback_used'])


# ───────────────────────────────── malformed and edge inputs ─────────────────────────────────
class TestMalformedInput(unittest.TestCase):
    def test_unsorted_and_duplicate_strikes(self):
        ev = infer([56200, 56000, 56100, 56000, 56200, 56300], 56000)
        self.assertEqual((ev['value'], ev['status']), (100, 'OK'))

    def test_single_strike_is_unavailable_not_guessed(self):
        ev = infer([56000], 56000)
        self.assertEqual((ev['value'], ev['status'], ev['reason']),
                         (None, 'UNAVAILABLE', 'INSUFFICIENT_STRIKES'))

    def test_empty_input_is_unavailable(self):
        for bad in ([], None):
            ev = infer(bad, 56000)
            self.assertEqual(ev['status'], 'UNAVAILABLE', repr(bad))
            self.assertIsNone(ev['value'])

    def test_nonfinite_and_nonpositive_are_rejected(self):
        ev = infer([float('nan'), float('inf'), -100, 0, 56000, 56100, 56200], 56000)
        self.assertEqual((ev['value'], ev['status']), (100, 'OK'))

    def test_booleans_are_rejected_not_read_as_one(self):
        """bool is an int subclass; True would otherwise normalise to strike 1 and invent a huge gap."""
        self.assertEqual(brain._normalise_strike_ladder([True, False, 56000, 56100]), [56000.0, 56100.0])
        ev = infer([True, 56000, 56100, 56200], 56000)
        self.assertEqual(ev['value'], 100)

    def test_numeric_strings_are_accepted(self):
        ev = infer(['56000', ' 56100 ', '56200'], 56000)
        self.assertEqual((ev['value'], ev['status']), (100, 'OK'))

    def test_garbage_values_are_skipped(self):
        ev = infer(['abc', {}, [], None, 56000, 56100, 56200], 56000)
        self.assertEqual((ev['value'], ev['status']), (100, 'OK'))

    def test_all_garbage_is_unavailable(self):
        ev = infer(['abc', {}, None, float('nan')], 56000)
        self.assertEqual(ev['status'], 'UNAVAILABLE')

    def test_ambiguous_ladder_is_declared_not_invented(self):
        """A ladder with no repeating gap has no single local interval; say so rather than pick one."""
        ev = infer([56000, 56100, 56350, 56800], 56100)
        self.assertEqual(ev['status'], 'AMBIGUOUS')
        self.assertEqual(ev['reason'], 'NO_REPEATED_INTERVAL')
        self.assertIsNone(ev['value'])
        self.assertIsNotNone(ev['candidate_value'], 'kept as a diagnostic, not as an applied value')

    def test_ambiguous_ladder_falls_back_explicitly_in_the_legacy_profile(self):
        ladder = [56000, 56100, 56350, 56800]
        strikes = {str(k): {'CE': leg(20), 'PE': leg(22)} for k in ladder}
        ch = {'strikes': strikes, 'allStrikes': ladder, 'atm': 56100, 'dailySigma': DAILY_SIGMA,
              'callWallStrike': 56800, 'putWallStrike': 56000}
        p = brain.chain_profile(ch, 56000.0, {}, vix=20)
        ev = p['strikeIntervalEvidence']
        self.assertEqual(ev['status'], 'AMBIGUOUS')
        self.assertTrue(ev['legacy_fallback_used'])
        self.assertEqual(ev['applied'], 100)        # the isolated legacy spot-based default

    def test_there_is_no_metadata_override_channel_at_all(self):
        """Codex R2 preferred deletion over an unverifiable contract, and the interface is gone.

        The history: the first version accepted ANY finite positive number as trusted OK/METADATA, so
        `strikeInterval = 1500` reinstated the exact sparse-wing failure this code removes. The
        binding contract that replaced it could not be established either -- a caller passing
        index/expiry/session_date=None skipped every check, a FUTURE `as_of` passed the lexical date
        comparison, a non-empty `source` string authenticated nothing, and "appears as an adjacent gap
        somewhere in the ladder" would still accept a sparse 1500 against a dense 100 local ladder.

        So nothing is carried: no parameter, no helper, no constant. This test fails if any of them
        is reintroduced without its own review.
        """
        import inspect
        params = list(inspect.signature(brain.infer_strike_interval).parameters)
        self.assertEqual(params, ['all_strikes', 'atm', 'window'])
        for gone in ('_bound_metadata_interval', 'STRIKE_INTERVAL_METADATA_REQUIRED'):
            self.assertFalse(hasattr(brain, gone), gone + ' was reintroduced')
        with self.assertRaises(TypeError):
            brain.infer_strike_interval(SPARSE_LADDER, ATM, metadata_step=1500)
        self.assertEqual(brain.STRIKE_INTERVAL_VERSION,
                         'strike_interval_v3_unique_repeated_mode_ladder_only')

    def test_the_interval_is_inferred_from_the_ladder_and_nothing_else(self):
        """With no override channel, the only inputs are the ladder and the ATM."""
        ev = infer(SPARSE_LADDER, ATM)
        self.assertEqual((ev['value'], ev['status'], ev['source']), (100, 'OK', 'NEAR_ATM_MODAL'))
        self.assertNotIn('metadata_rejected', ev)
        self.assertNotIn('metadata_binding', ev)
        self.assertNotEqual(ev['source'], 'METADATA')

    def test_no_producer_of_interval_metadata_exists_in_the_repository(self):
        """Kept after the channel's removal: if a producer is ever added, this fails, and whoever
        adds it is pointed at the review the override interface still needs."""
        import re
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
        writers = []
        for base, _dirs, files in os.walk(os.path.join(root, 'src', 'main')):
            if 'tests' in base:
                continue
            for fn in files:
                if not fn.endswith(('.py', '.kt')):
                    continue
                path = os.path.join(base, fn)
                with open(path, encoding='utf-8', errors='ignore') as fh:
                    for line in fh:
                        if re.search(r'(put|\[)\s*\(?["\']strike_?[Ii]nterval["\']', line):
                            writers.append((fn, line.strip()[:60]))
        self.assertEqual(writers, [], 'a producer now exists; re-trace the metadata claim')

    def test_missing_atm_falls_back_to_global_modal(self):
        ev = infer(SPARSE_LADDER, atm=None)
        self.assertEqual(ev['source'], 'GLOBAL_MODAL')
        self.assertEqual(ev['value'], 100)      # the dense section still dominates by count

    def test_spot_is_never_used_for_inference(self):
        """The inference function takes no spot argument at all, by design."""
        import inspect
        self.assertNotIn('spot', inspect.signature(brain.infer_strike_interval).parameters)

    def test_no_nan_or_inf_in_the_evidence(self):
        for ladder, atm in ((SPARSE_LADDER, ATM), ([56000], 56000), ([], None)):
            for v in infer(ladder, atm).values():
                if isinstance(v, float):
                    self.assertTrue(math.isfinite(v))


class TestIntervalNumericType(unittest.TestCase):
    """A real bug caught while building this commit: the normaliser works in float, so an interval of
    100.0 made `step` a float, `atm + otm_dist` a float, and str() produced "56200.0" -- missing a
    strikes dict keyed "56200" and silently reproducing the exact zero-IV symptom being repaired. An
    integral interval must come back as int."""

    def test_integral_interval_is_an_int(self):
        for ladder, atm in ((SPARSE_LADDER, ATM),
                            ([24000 + i * 50 for i in range(-10, 11)], 24000)):
            ev = infer(ladder, atm)
            self.assertIsInstance(ev['value'], int, ladder[:3])
            self.assertIsInstance(ev['helper_value'], int)

    def test_non_integral_interval_is_preserved(self):
        ev = infer([100.0, 100.5, 101.0, 101.5], 101.0)
        self.assertAlmostEqual(ev['value'], 0.5, places=6)

    def test_strike_keys_resolve_with_the_inferred_step(self):
        """The end-to-end guard: the sampled strike must actually be found in the chain."""
        ch = sparse_chain()
        step = infer(ch['allStrikes'], ch['atm'])['value']
        otm = round(DAILY_SIGMA * 0.5 / step) * step
        self.assertIsNotNone(brain.get_strike(ch['strikes'], ch['atm'] + otm))
        self.assertIsNotNone(brain.get_strike(ch['strikes'], ch['atm'] - otm))


class TestCodexR1AmbiguityProbes(unittest.TestCase):
    """Codex R1 correction 1, reproduced as tests. Both populations lack a unique REPEATED interval.
    Returning OK introduced a tighter-interval preference as though it were established geometry,
    which on another valid ladder samples strikes that do not exist and recreates zero features."""

    def test_probe_one_no_repeated_interval(self):
        ev = infer([56000, 56100, 56300], 56100)
        self.assertEqual(ev['status'], 'AMBIGUOUS')
        self.assertEqual(ev['reason'], 'NO_REPEATED_INTERVAL')
        self.assertIsNone(ev['value'], 'no applied value may be published')
        self.assertEqual(ev['candidate_value'], 100, 'retained only as a diagnostic')
        self.assertEqual(ev['gaps_considered'], 2)
        self.assertEqual(ev['modal_count'], 1)

    def test_probe_two_tied_modes(self):
        ev = infer([55800, 55900, 56000, 56200, 56400], 56000)
        self.assertEqual(ev['status'], 'AMBIGUOUS')
        self.assertEqual(ev['reason'], 'TIED_MODES')
        self.assertIsNone(ev['value'])
        self.assertEqual(ev['tied_modes'], [100, 200])
        self.assertEqual(ev['modal_count'], 2)

    def test_both_probes_fall_back_explicitly_in_the_profile(self):
        for ladder, atm in (([56000, 56100, 56300], 56100),
                            ([55800, 55900, 56000, 56200, 56400], 56000)):
            strikes = {str(k): {'CE': leg(20), 'PE': leg(22)} for k in ladder}
            ch = {'strikes': strikes, 'allStrikes': ladder, 'atm': atm,
                  'dailySigma': DAILY_SIGMA, 'callWallStrike': ladder[-1],
                  'putWallStrike': ladder[0]}
            p = brain.chain_profile(ch, 56000.0, {}, vix=20)
            ev = p['strikeIntervalEvidence']
            self.assertEqual(ev['status'], 'AMBIGUOUS', ladder)
            self.assertTrue(ev['legacy_fallback_used'], ladder)
            self.assertIsNone(ev['value'], ladder)
            self.assertIsNotNone(ev['applied'], 'the legacy fallback still supplies a value')

    def test_a_unique_repeated_mode_is_still_ok(self):
        """Ordinary ladders must not regress into ambiguity."""
        for ladder, atm, step in (([24000 + i * 50 for i in range(-10, 11)], 24000, 50),
                                  ([56000 + i * 100 for i in range(-10, 11)], 56000, 100),
                                  (SPARSE_LADDER, ATM, 100)):
            ev = infer(ladder, atm)
            self.assertEqual((ev['value'], ev['status']), (step, 'OK'), ladder[:3])
            self.assertEqual(ev['evidence'], 'REPEATED_UNIQUE_MODE')
            self.assertGreaterEqual(ev['modal_count'], 2)

    def test_single_gap_is_distinguished_from_a_supported_interval(self):
        ev = infer([56000, 56100], 56000)
        self.assertEqual((ev['status'], ev['reason']), ('AMBIGUOUS', 'SINGLE_GAP_ONLY'))
        self.assertEqual(ev['evidence'], 'SINGLE_GAP')
        self.assertIsNone(ev['value'])


class TestAtmWindowContract(unittest.TestCase):
    """The +-6 strike window is a proposed geometric heuristic, not a market threshold. Its bounded
    purpose: the interval is used to sample near the money, so it is inferred there."""

    def test_window_constant_is_explicit(self):
        self.assertEqual(brain.STRIKE_INTERVAL_ATM_WINDOW, 6)

    def test_window_excludes_a_distant_density_change(self):
        """Dense 100-pt near ATM, 500-pt far above. With the window the near interval wins."""
        ladder = [56000 + i * 100 for i in range(0, 12)] + [58000 + j * 500 for j in range(1, 8)]
        ev = infer(ladder, 56300)
        self.assertEqual((ev['value'], ev['status'], ev['source']), (100, 'OK', 'NEAR_ATM_MODAL'))

    def test_window_follows_the_atm_into_a_different_density(self):
        """With ATM inside the sparse region the local interval there is what gets reported, which is
        the point of a LOCAL interval."""
        ladder = [56000 + i * 100 for i in range(0, 12)] + [58000 + j * 500 for j in range(1, 10)]
        ev = infer(ladder, 61000)
        self.assertEqual((ev['value'], ev['status']), (500, 'OK'))

    def test_window_size_is_honoured(self):
        ladder = [56000 + i * 100 for i in range(0, 4)] + [56400 + j * 700 for j in range(1, 12)]
        narrow = infer(ladder, 56000, window=2)
        wide = infer(ladder, 56000, window=10)
        self.assertEqual(narrow['value'], 100)
        self.assertNotEqual(wide['value'], 100)

    def test_boundary_at_exactly_two_near_gaps_still_uses_the_window(self):
        ladder = [55900, 56000, 56100, 57000, 58500, 60000]
        ev = infer(ladder, 56000, window=1)
        self.assertEqual(ev['source'], 'NEAR_ATM_MODAL')
        self.assertEqual(ev['value'], 100)


class TestEndToEndLimitsOfThisRepair(unittest.TestCase):
    """Codex R1 correction 3. The helper normalises its OWN private ladder, but chain_profile keeps
    using the original all_k for zone iteration and other arithmetic. These cases therefore still
    raise end to end. They are PRE-EXISTING limits of the profile, NOT regressions introduced here --
    the old code was equally intolerant -- and they are recorded rather than silently widened into a
    rewrite of every profile calculation."""

    def _chain_with(self, all_strikes):
        ch = sparse_chain()
        ch['allStrikes'] = all_strikes
        return ch

    def test_string_strikes_still_raise_in_the_whole_profile(self):
        with self.assertRaises(TypeError):
            brain.chain_profile(self._chain_with(['56000', '56100', '56200']), 56000.0, {}, vix=20)

    def test_none_in_the_ladder_still_raises(self):
        with self.assertRaises(TypeError):
            brain.chain_profile(self._chain_with([56000, 56100, None, 56200]), 56000.0, {}, vix=20)

    def test_nan_in_the_ladder_no_longer_aborts_the_profile(self):
        """This one WAS a raise out of get_strike (`int(nan)` -> ValueError) and is now contained.

        It is the one malformed-input case that belonged to the lookup rather than to the surrounding
        legacy arithmetic: a NaN survives `abs(nan - atm)` and every comparison, so it reached
        `get_strike` and aborted the whole profile. It now resolves to "row not found" and the profile
        builds, degraded, from the strikes that are real."""
        p = brain.chain_profile(self._chain_with([56000, 56100, float('nan'), 56200]),
                                56000.0, {}, vix=20)
        self.assertIsNotNone(p)
        self.assertIn('ivSkew', p)

    def test_the_helper_itself_tolerates_all_three(self):
        """The boundary is exactly here: inference is safe, the surrounding legacy profile is not."""
        for bad in (['56000', '56100', '56200'], [56000, 56100, None, 56200],
                    [56000, 56100, float('nan'), 56200]):
            ev = infer(bad, 56100)
            self.assertIn(ev['status'], ('OK', 'AMBIGUOUS'), repr(bad))


class TestProducerStrikeRepresentation(unittest.TestCase):
    """RETRACTION, with the evidence that corrects it.

    I previously told the reviewer -- and the reviewer authorised a repair commit on the strength of
    it -- that `chain_profile` sees a FLOAT atm in production, because
    `OptionChainBrainFormatter` declares `var atmStrike = 0.0` and keys strikes as
    `strike.toInt().toString()`; and therefore that the sparse-interval repair might be inert in
    production. **That conclusion was wrong.** The Kotlin types are as described, but I stopped at the
    producer and never followed the payload across the language boundary.

    The boundary normalises it. `MarketWatchService` hands the brain `ctxObj.toString()` and
    `brain.analyze` does `json.loads`, so every number is re-rendered as JSON text first -- and both
    org.json implementations in use print an integral double as an integer literal. The committed
    golden formatter output proves it directly, and these tests read that file rather than restating
    the claim.

    Commit A's interval repair therefore DOES take effect on production input, and the
    representation hardening is hardening, not a prerequisite. See TestFourWayMatrix.
    """

    # tests/ -> python/ -> main/ -> src/, then src/test/resources/...
    GOLDEN = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))), 'test', 'resources', 'contracts',
        'x1_upstox_formatted_golden.json')

    def _golden_chains(self):
        import json
        with open(self.GOLDEN, encoding='utf-8') as fh:
            doc = json.load(fh)
        found = []

        def walk(node):
            if isinstance(node, dict):
                if 'allStrikes' in node and 'atm' in node and 'strikes' in node:
                    found.append(node)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(doc)
        return found

    def test_the_producer_kotlin_really_does_hold_doubles(self):
        """The half of the original claim that was true, kept so the retraction is checkable."""
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), 'java', 'com', 'marketradar', 'app')
        src = open(os.path.join(base, 'OptionChainBrainFormatter.kt'), encoding='utf-8').read()
        self.assertIn('var atmStrike = 0.0', src)
        self.assertIn('result.put("atm", atmStrike)', src)
        self.assertIn('strikesObj.put(strike.toInt().toString(), strikeObj)', src)

    def test_the_context_crosses_into_python_as_json_text(self):
        """The step the original claim skipped: there is a serialisation boundary in between."""
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), 'java', 'com', 'marketradar', 'app')
        src = open(os.path.join(base, 'MarketWatchService.kt'), encoding='utf-8').read()
        self.assertIn('ctxObj.toString()', src)
        brain_src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'brain.py'), encoding='utf-8').read()
        self.assertIn("ctx = json.loads(context_json) if context_json else {}", brain_src)

    def test_the_golden_formatter_output_carries_integers_not_floats(self):
        """Decisive: real captured formatter output, after serialisation. atm and every strike in
        allStrikes parse as int, and no strike key carries a decimal point."""
        chains = self._golden_chains()
        self.assertGreaterEqual(len(chains), 1, 'golden fixture shape changed')
        for ch in chains:
            self.assertIsInstance(ch['atm'], int)
            self.assertFalse(isinstance(ch['atm'], bool))
            for k in ch['allStrikes']:
                self.assertIsInstance(k, int, repr(k))
            for key in ch['strikes']:
                self.assertNotIn('.', str(key), key)

    def test_the_lookup_resolves_the_golden_payload_as_the_live_path_would(self):
        """End to end on real captured input: atm +- a modal-interval multiple resolves."""
        for ch in self._golden_chains():
            ev = infer(ch['allStrikes'], ch['atm'])
            if ev['status'] != 'OK':
                continue
            step = ev['value']
            for d in (0, step, 2 * step):
                for k in (ch['atm'] + d, ch['atm'] - d):
                    if str(int(k)) in ch['strikes']:
                        self.assertIsNotNone(brain.get_strike(ch['strikes'], k), k)


class TestStrikeKeyRepresentation(unittest.TestCase):
    """The representation hardening in `get_strike` / `_strike_key_candidates`.

    The previous body had no `str(int(k))` spelling, so a float request missed an integer-string key,
    and `int(k)` raised on NaN. Production is unaffected (above), so this is containment for an
    un-collapsed payload, a non-integral interval, or a bad entry in `allStrikes`.
    """

    STRIKES = {'56000': {'CE': {'iv': 20}}, '56200': {'CE': {'iv': 21}},
               '56250': {'CE': {'iv': 22}}}

    def test_equivalent_integral_spellings_all_resolve_to_one_row(self):
        rows = [brain.get_strike(self.STRIKES, k) for k in (56200, 56200.0, '56200', '56200.0')]
        for r in rows:
            self.assertIsNotNone(r)
        self.assertEqual([r['CE']['iv'] for r in rows], [21, 21, 21, 21])

    def test_float_atm_plus_integral_offset_now_resolves(self):
        """The exact case the earlier test asserted as broken; it is now contained."""
        self.assertIsNotNone(brain.get_strike(self.STRIKES, 56000.0 + 200))
        self.assertIsNotNone(brain.get_strike(self.STRIKES, 56200.0))

    def test_numeric_keys_still_work(self):
        """Existing numeric-dictionary behaviour is preserved, both int- and float-keyed."""
        self.assertIsNotNone(brain.get_strike({56200: {'CE': {}}, 'x': 1}, 56200))
        self.assertIsNotNone(brain.get_strike({56200: {'CE': {}}}, 56200.0))
        self.assertIsNotNone(brain.get_strike({56200.0: {'CE': {}}}, 56200))
        self.assertIsNotNone(brain.get_strike({56200.0: {'CE': {}}}, '56200'))

    def test_a_genuinely_fractional_strike_is_never_truncated(self):
        """56250.5 is NOT 56250. Truncating would silently read a different contract."""
        self.assertIsNone(brain.get_strike(self.STRIKES, 56250.5))
        self.assertIsNone(brain.get_strike(self.STRIKES, '56250.5'))
        frac = {'56250.5': {'CE': {'iv': 30}}, '56250': {'CE': {'iv': 22}}}
        self.assertEqual(brain.get_strike(frac, 56250.5)['CE']['iv'], 30)
        self.assertEqual(brain.get_strike(frac, 56250)['CE']['iv'], 22)

    def test_a_fractional_request_resolves_a_float_key(self):
        self.assertIsNotNone(brain.get_strike({56250.5: {'CE': {}}}, 56250.5))
        self.assertIsNotNone(brain.get_strike({56250.5: {'CE': {}}}, '56250.5'))

    def test_absent_strike_is_none_not_an_error(self):
        self.assertIsNone(brain.get_strike(self.STRIKES, 99999))
        self.assertIsNone(brain.get_strike({}, 56200))
        self.assertIsNone(brain.get_strike(None, 56200))

    def test_invalid_and_nonfinite_requests_return_none_instead_of_raising(self):
        """The previous body raised on NaN via int(nan), out of the middle of chain_profile."""
        for bad in (float('nan'), float('inf'), float('-inf'), 'abc', '', None, True, False,
                    [56200], {'k': 1}, object()):
            self.assertIsNone(brain.get_strike(self.STRIKES, bad), repr(bad))

    def test_candidate_spellings_and_their_refusal_reasons(self):
        cands, reason = brain._strike_key_candidates(56200.0)
        self.assertIsNone(reason)
        self.assertEqual(cands, [56200.0, 56200, '56200', '56200.0'])
        cands, reason = brain._strike_key_candidates(56250.5)
        self.assertIsNone(reason)
        self.assertEqual(cands, [56250.5, '56250.5'])
        self.assertNotIn(56250, cands)
        self.assertNotIn('56250', cands)
        for bad, expected in ((True, 'BOOLEAN_NOT_A_STRIKE'), ('abc', 'NOT_NUMERIC'),
                              (float('nan'), 'NOT_FINITE'), (None, 'UNSUPPORTED_TYPE')):
            cands, reason = brain._strike_key_candidates(bad)
            self.assertEqual((cands, reason), ([], expected), repr(bad))

    def test_no_epsilon_tolerance_is_applied(self):
        """A near-miss must not resolve: it names a strike that was not asked for."""
        self.assertIsNone(brain.get_strike(self.STRIKES, 56200.0001))
        self.assertIsNone(brain.get_strike(self.STRIKES, 56199.9999))

    def test_conflicting_spellings_resolve_in_documented_priority_order(self):
        """Request's own spelling first, then int, integer-string, float, float-string."""
        d = {56200: {'CE': {'iv': 1}}, '56200': {'CE': {'iv': 2}}, '56200.0': {'CE': {'iv': 3}}}
        self.assertEqual(brain.get_strike(d, '56200')['CE']['iv'], 2)
        self.assertEqual(brain.get_strike(d, '56200.0')['CE']['iv'], 3)
        self.assertEqual(brain.get_strike(d, 56200)['CE']['iv'], 1)

    def test_present_but_falsy_rows_still_read_as_absent(self):
        """Unchanged on purpose. Every call site applies `or {}`, so this is inert; changing it is a
        separate decision rather than a side effect of this repair."""
        self.assertIsNone(brain.get_strike({'56200': {}}, 56200))
        self.assertIsNone(brain.get_strike({'56200': None}, 56200))
        self.assertEqual(brain.get_strike({'56200': {}, 56200.0: {'CE': {'iv': 9}}},
                                          56200)['CE']['iv'], 9)


class TestFourWayMatrix(unittest.TestCase):
    """{old, new} interval x {old, fixed} lookup, on the real serialised payload shape AND on the
    hypothetical un-serialised Double shape. This is what separates the sparse-wing defect from the
    representation defect -- and it shows the representation fix changes nothing in production.
    """

    FIELDS = ('ivSkew', 'ivSlope', 'netDelta', 'volRatio', 'avgTheta', 'avgVega', 'pcrOi')

    @staticmethod
    def _greek_leg(iv):
        """This class needs its own leg builder: the shared `leg()` carries no greeks, so netDelta /
        avgTheta / avgVega would read 0.0 for a FIXTURE reason and could not evidence anything about
        the lookup. (An earlier version of this test asserted on them anyway and was wrong.)"""
        d = {'oi': 1000, 'volume': 10, 'prev_oi': 900, 'ltp': 50.0, 'bid': 49.0, 'ask': 51.0,
             'delta': 0.5, 'theta': -3.0, 'vega': 8.0}
        if iv is not None:
            d['iv'] = iv
        return d

    @staticmethod
    def _old_get_strike(strikes_dict, k):
        """The pre-repair body, verbatim."""
        if not strikes_dict:
            return None
        return (strikes_dict.get(k) or strikes_dict.get(str(k)) or strikes_dict.get(str(float(k)))
                or strikes_dict.get(int(k) if isinstance(k, (int, float)) else None))

    @staticmethod
    def _old_interval(all_strikes, atm=None, **kw):
        """The pre-repair step: all_k[1] - all_k[0], i.e. the sparse far wing."""
        ladder = list(all_strikes or [])
        v = (ladder[1] - ladder[0]) if len(ladder) > 1 else None
        if not v or v <= 0:
            return {'value': None, 'status': 'UNAVAILABLE', 'reason': 'FORCED', 'source': None,
                    'candidate_value': None, 'evidence': None, 'gaps_considered': 0,
                    'modal_count': 0, 'distinct_gaps': 0, 'helper_value': None,
                    'helper_agrees': None, 'version': 'forced-legacy-wing-gap'}
        return {'value': int(v) if float(v).is_integer() else v, 'status': 'OK', 'reason': None,
                'source': 'FORCED_LEGACY_WING_GAP', 'candidate_value': v, 'evidence': 'FORCED',
                'gaps_considered': 0, 'modal_count': 0, 'distinct_gaps': 0, 'helper_value': v,
                'helper_agrees': True, 'version': 'forced-legacy-wing-gap'}

    def _chain(self, floaty):
        """The brief's sparse fixture, keyed exactly as the producer keys it (`str(int(strike))`)."""
        strikes = {}
        for k in SPARSE_LADDER:
            near = abs(k - ATM) <= NEAR_IV_WINDOW
            strikes[str(int(k))] = {'CE': self._greek_leg(20 if near else None),
                                    'PE': self._greek_leg(22 if near else None)}
        cast = float if floaty else int
        return {'strikes': strikes, 'allStrikes': [cast(k) for k in SPARSE_LADDER],
                'atm': cast(ATM), 'dailySigma': DAILY_SIGMA, 'callWallStrike': 57000,
                'putWallStrike': 55000, 'expiry': '2026-11-23',
                'requested_underlying_key': 'NSE_INDEX|Nifty Bank'}

    def _cell(self, interval_fn, lookup_fn, floaty):
        real_i, real_g = brain.infer_strike_interval, brain.get_strike
        brain.infer_strike_interval, brain.get_strike = interval_fn, lookup_fn
        try:
            p = brain.chain_profile(self._chain(floaty), float(ATM),
                                    {'open': ATM, 'high': ATM, 'low': ATM}, vix=12.0) or {}
        finally:
            brain.infer_strike_interval, brain.get_strike = real_i, real_g
        return {f: p.get(f) for f in self.FIELDS}

    def _matrix(self, floaty):
        return {
            'old_interval_old_lookup': self._cell(self._old_interval, self._old_get_strike, floaty),
            'new_interval_old_lookup': self._cell(brain.infer_strike_interval,
                                                  self._old_get_strike, floaty),
            'old_interval_new_lookup': self._cell(self._old_interval, brain.get_strike, floaty),
            'new_interval_new_lookup': self._cell(brain.infer_strike_interval,
                                                  brain.get_strike, floaty),
        }

    def test_on_the_real_payload_the_interval_is_what_matters_and_the_lookup_is_inert(self):
        m = self._matrix(floaty=False)
        # the interval repair is what moves the features
        self.assertEqual((m['old_interval_old_lookup']['ivSkew'],
                          m['old_interval_old_lookup']['ivSlope']), (0, 0.0))
        self.assertEqual((m['new_interval_new_lookup']['ivSkew'],
                          m['new_interval_new_lookup']['ivSlope']), (2, 1.0))
        # and the lookup change is inert: the two lookups agree in BOTH interval columns
        self.assertEqual(m['old_interval_old_lookup'], m['old_interval_new_lookup'])
        self.assertEqual(m['new_interval_old_lookup'], m['new_interval_new_lookup'])

    def test_the_real_payload_profile_is_unchanged_field_for_field_by_the_lookup_repair(self):
        """Full-field, not just the matrix columns: no production field moves."""
        ch = self._chain(floaty=False)
        real_g = brain.get_strike
        brain.get_strike = self._old_get_strike
        try:
            before = brain.chain_profile(ch, float(ATM), {}, vix=12.0)
        finally:
            brain.get_strike = real_g
        after = brain.chain_profile(ch, float(ATM), {}, vix=12.0)
        self.assertEqual(set(before), set(after))
        for k in before:
            self.assertEqual(before[k], after[k], k)

    def test_on_an_unserialised_double_payload_the_lookup_repair_is_what_matters(self):
        """The shape the retracted claim described. It does not occur in production, but if it ever
        did, EVERY get_strike-derived feature would be degenerate until the lookup is fixed."""
        m = self._matrix(floaty=True)
        for cell in ('old_interval_old_lookup', 'new_interval_old_lookup'):
            self.assertEqual(m[cell]['ivSkew'], 0, cell)
            self.assertEqual(m[cell]['netDelta'], 0.0, cell)
            self.assertEqual(m[cell]['avgVega'], 0.0, cell)
        self.assertEqual(m['new_interval_new_lookup']['ivSkew'], 2)
        self.assertEqual(m['new_interval_new_lookup']['ivSlope'], 1.0)
        self.assertNotEqual(m['new_interval_new_lookup']['netDelta'], 0.0)
        # both repairs are needed on that shape: neither alone recovers the features
        self.assertNotEqual(m['new_interval_old_lookup'], m['new_interval_new_lookup'])
        self.assertNotEqual(m['old_interval_new_lookup'], m['new_interval_new_lookup'])


class TestOrdinaryLadderFullFieldParity(unittest.TestCase):
    """Codex R1 correction 4: compare ALL previous profile fields on an ordinary ladder, not merely
    the equality of the inferred step."""

    def _profile_with_forced_step(self, ch, spot, forced):
        """Recompute the profile as the pre-repair code would, by forcing the inferred interval."""
        real = brain.infer_strike_interval
        brain.infer_strike_interval = lambda *a, **k: {
            'value': forced, 'candidate_value': forced, 'status': 'OK', 'reason': None,
            'source': 'FORCED_LEGACY', 'evidence': 'FORCED', 'gaps_considered': 0,
            'modal_count': 0, 'distinct_gaps': 0, 'helper_value': forced, 'helper_agrees': True,
            'version': 'forced'}
        try:
            return brain.chain_profile(ch, spot, {}, vix=20)
        finally:
            brain.infer_strike_interval = real

    def test_every_pre_existing_field_is_identical_on_ordinary_ladders(self):
        for atm, step, spot in ((24000, 50, 24000.0), (56000, 100, 56000.0)):
            ch = uniform_chain(atm, step)
            self.assertEqual(legacy_step(ch['allStrikes'], spot), step, 'premise: rules agree here')
            old = self._profile_with_forced_step(uniform_chain(atm, step), spot, step)
            new = brain.chain_profile(uniform_chain(atm, step), spot, {}, vix=20)
            old.pop('strikeIntervalEvidence', None)
            new_cmp = dict(new)
            new_cmp.pop('strikeIntervalEvidence', None)
            self.assertEqual(sorted(old), sorted(new_cmp), 'field set unchanged')
            for key in sorted(old):
                self.assertEqual(old[key], new_cmp[key], 'field %s changed at atm=%s' % (key, atm))

    def test_only_the_provenance_field_is_added(self):
        ch = uniform_chain(56000, 100)
        new = brain.chain_profile(ch, 56000.0, {}, vix=20)
        old = self._profile_with_forced_step(uniform_chain(56000, 100), 56000.0, 100)
        added = set(new) - set(old)
        self.assertEqual(added, set(), 'the forced run also carries the provenance field')
        self.assertIn('strikeIntervalEvidence', new)

    def test_changed_paths_are_not_only_the_sparse_ladder(self):
        """Correcting an earlier overstatement: tied modes, no-repetition ladders and a refused
        metadata override are ALSO changed paths relative to the old rule."""
        changed = []
        for ladder, atm in (([56000, 56100, 56300], 56100),                 # tied/no repetition
                            ([55800, 55900, 56000, 56200, 56400], 56000),   # tied modes
                            (SPARSE_LADDER, ATM)):                          # sparse wing
            ev = infer(ladder, atm)
            if ev.get('value') != legacy_step(sorted(set(ladder)), 56000.0):
                changed.append((ladder[:3], ev['status']))
        self.assertEqual(len(changed), 3, 'three distinct changed paths, not one')


class TestVerdictConsumerTrace(unittest.TestCase):
    """Codex R1 correction 4: synthesize_verdict reads bnfProfile.ivSkew, so prove where a changed
    value actually lands."""

    SRC = None

    @classmethod
    def setUpClass(cls):
        cls.SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    'brain.py'), encoding='utf-8').read()

    def test_synthesize_verdict_reads_ivskew_at_the_documented_thresholds(self):
        self.assertIn("skew = profile.get('ivSkew', 0)", self.SRC)
        self.assertIn("'iv_skew': (ctx.get('bnfProfile') or {}).get('ivSkew', 0)", self.SRC)
        self.assertIn('if skew > 3:', self.SRC)

    def test_its_output_is_demoted_to_labelled_legacy_evidence(self):
        self.assertIn("legacy_verdict = result.get('verdict')", self.SRC)
        self.assertIn("'label': 'legacy_combined_v0 recorded evidence; "
                      "not used in any decision, ordering or alert'", self.SRC)
        self.assertIn("'verdict': {k: legacy_verdict.get(k) for k in "
                      "('action', 'strategy', 'direction', 'confidence')}", self.SRC)

    def test_the_canonical_verdict_is_replaced_after_the_legacy_capture(self):
        """The cutover captures the legacy verdict as evidence and then REPLACES result['verdict'],
        so a changed ivSkew cannot move the canonical displayed verdict."""
        capture = self.SRC.index("legacy_verdict = result.get('verdict')")
        replace = self.SRC.index("result['verdict'] = {", capture)
        self.assertGreater(replace, capture)
        self.assertIn("'label': 'legacy_combined_v0 recorded evidence", self.SRC[capture:replace])


class TestLegacyHelperUntouched(unittest.TestCase):
    def test_existing_helper_behaviour_is_not_modified(self):
        """Commit A must not change _infer_strike_step: candidate generation and the teaching band
        call it, and altering it would change those paths in the same commit as a profile repair."""
        self.assertEqual(brain._infer_strike_step([24000, 24050, 24100], 50), 50)
        self.assertEqual(brain._infer_strike_step(SPARSE_LADDER, 100), 100)
        self.assertEqual(brain._infer_strike_step([], 100), 100)
        self.assertEqual(brain._infer_strike_step([56000], 77), 77)


if __name__ == '__main__':
    unittest.main(verbosity=2)
