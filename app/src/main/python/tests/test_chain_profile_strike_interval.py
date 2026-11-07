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
        self.assertEqual(ev['reason'], 'NO_MODAL_INTERVAL')

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

    def test_metadata_interval_is_preferred_when_present(self):
        ev = infer(SPARSE_LADDER, ATM, metadata_step=100)
        self.assertEqual((ev['value'], ev['source'], ev['status']), (100, 'METADATA', 'OK'))

    def test_invalid_metadata_is_ignored_and_inference_continues(self):
        for bad in (0, -5, float('nan'), float('inf'), True, 'abc', None):
            ev = infer(SPARSE_LADDER, ATM, metadata_step=bad)
            self.assertEqual(ev['value'], 100, repr(bad))
            self.assertNotEqual(ev['source'], 'METADATA', repr(bad))

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
