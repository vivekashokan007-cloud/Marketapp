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

    def test_a_bare_numeric_metadata_value_is_refused(self):
        """Codex R1: an unbound number was applied as trusted METADATA, so strikeInterval=1500 or 7
        reinstated the sparse-wing failure and labelled it evidence. A bare value cannot be checked
        against the chain it is applied to, so it is refused."""
        for bare in (100, 1500, 7, 100.0, '100'):
            ev = infer(SPARSE_LADDER, ATM, metadata_step=bare)
            self.assertNotEqual(ev['source'], 'METADATA', repr(bare))
            self.assertEqual(ev['metadata_rejected'], 'UNBOUND_BARE_VALUE', repr(bare))
            self.assertEqual(ev['value'], 100, 'inference continues from the ladder')

    def test_bound_metadata_is_accepted_only_when_it_matches_this_chain(self):
        good = {'interval': 100, 'index': 'NSE_INDEX|Nifty Bank', 'expiry': '2026-11-23',
                'as_of': '2026-11-07', 'source': 'contract_master'}
        ev = infer(SPARSE_LADDER, ATM, metadata_step=good, index='NSE_INDEX|Nifty Bank',
                   expiry='2026-11-23', session_date='2026-11-07')
        self.assertEqual((ev['value'], ev['source'], ev['status']), (100, 'METADATA', 'OK'))
        self.assertEqual(ev['metadata_binding']['source'], 'contract_master')

    def test_bound_metadata_is_rejected_on_any_binding_mismatch(self):
        base = {'interval': 100, 'index': 'NSE_INDEX|Nifty Bank', 'expiry': '2026-11-23',
                'as_of': '2026-11-07', 'source': 'contract_master'}
        cases = {
            'INDEX_MISMATCH': dict(base, index='NSE_INDEX|Nifty 50'),
            'EXPIRY_MISMATCH': dict(base, expiry='2026-12-29'),
            'STALE_AS_OF': dict(base, as_of='2026-11-01'),
            'CONTRADICTS_OBSERVED_LADDER': dict(base, interval=7),
            'INTERVAL_NOT_POSITIVE_FINITE': dict(base, interval=0),
        }
        for expected, meta in cases.items():
            ev = infer(SPARSE_LADDER, ATM, metadata_step=meta, index='NSE_INDEX|Nifty Bank',
                       expiry='2026-11-23', session_date='2026-11-07')
            self.assertEqual(ev['metadata_rejected'], expected, expected)
            self.assertNotEqual(ev['source'], 'METADATA', expected)

    def test_metadata_missing_provenance_is_rejected(self):
        for drop in ('index', 'expiry', 'as_of', 'source', 'interval'):
            meta = {'interval': 100, 'index': 'X', 'expiry': 'Y', 'as_of': 'Z', 'source': 'S'}
            meta.pop(drop)
            ev = infer(SPARSE_LADDER, ATM, metadata_step=meta)
            self.assertTrue(ev['metadata_rejected'].startswith('MISSING_PROVENANCE'), drop)

    def test_no_producer_of_interval_metadata_exists_in_the_repository(self):
        """Codex R1: the earlier comment called this "dated metadata" while nothing writes it. If a
        producer is ever added, this test should fail and the claim be re-traced."""
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

    def test_integral_metadata_interval_is_an_int(self):
        self.assertIsInstance(infer(SPARSE_LADDER, ATM, metadata_step=100.0)['value'], int)

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

    def test_nan_in_the_ladder_still_raises(self):
        with self.assertRaises(ValueError):
            brain.chain_profile(self._chain_with([56000, 56100, float('nan'), 56200]),
                                56000.0, {}, vix=20)

    def test_the_helper_itself_tolerates_all_three(self):
        """The boundary is exactly here: inference is safe, the surrounding legacy profile is not."""
        for bad in (['56000', '56100', '56200'], [56000, 56100, None, 56200],
                    [56000, 56100, float('nan'), 56200]):
            ev = infer(bad, 56100)
            self.assertIn(ev['status'], ('OK', 'AMBIGUOUS'), repr(bad))

    def test_a_float_atm_defeats_the_strike_lookup_regardless_of_the_interval(self):
        """Codex R1 correction 3b, confirmed against the real producer: OptionChainBrainFormatter
        declares `var atmStrike = 0.0` and emits atm as a Double, while strikes are keyed
        `strike.toInt().toString()`. So atm +- otm_dist is a float, str() gives "56200.0", and
        get_strike misses the integer-string key -- even with an integral interval.

        This is a SEPARATE pre-existing representation defect. This commit does not fix it, and the
        sparse-interval repair alone is therefore necessary but NOT sufficient in production."""
        strikes = {'56000': {'CE': {'iv': 20}}, '56200': {'CE': {'iv': 20}}}
        self.assertIsNotNone(brain.get_strike(strikes, 56000 + 200))      # int ATM resolves
        self.assertIsNone(brain.get_strike(strikes, 56000.0 + 200))       # float ATM does not
        self.assertIsNone(brain.get_strike(strikes, 56200.0))

    def test_the_producer_really_emits_a_double_atm_and_integer_string_keys(self):
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), 'java', 'com', 'marketradar', 'app')
        src = open(os.path.join(base, 'OptionChainBrainFormatter.kt'), encoding='utf-8').read()
        self.assertIn('var atmStrike = 0.0', src)
        self.assertIn('result.put("atm", atmStrike)', src)
        self.assertIn('strikesObj.put(strike.toInt().toString(), strikeObj)', src)


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
