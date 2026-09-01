"""Regression guard for the 2026-08-26 Bank Nifty candidate blackout.

`generate_candidates` computed the strike step as ``all_strikes[1] - all_strikes[0]``
— the gap between the two LOWEST strikes. That is only correct for a uniformly
spaced chain. NSE monthly chains (Bank Nifty, which no longer has weekly expiries)
are dense near the money (100-pt) and sparse in the far wings (500-1500-pt), and the
lowest two strikes sit in the sparse wing, so the step resolved to 1500. A 1500 step
makes `_pc2_batch_f_width_ladder` return an empty width list, which silently produced
ZERO Bank Nifty candidates every poll from 2026-08-26 (when BNF rolled off its last
weekly onto the Sep monthly) onward — while the option-chain data was fully present.

These tests pin the true-interval inference and that a realistic BNF monthly chain
yields candidates again, while a uniformly-spaced Nifty chain is unchanged.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import brain


class StrikeStepInferenceTest(unittest.TestCase):
    def test_uniform_chain_unchanged(self):
        # Nifty weekly: uniform 50-pt spacing across the whole chain.
        strikes = list(range(22000, 27250, 50))
        self.assertEqual(brain._infer_strike_step(strikes, 50), 50)

    def test_sparse_wings_do_not_inflate_step(self):
        # Bank Nifty monthly: sparse far wings, dense 100-pt core — the exact 08-31
        # shape. The two lowest strikes are 1500 apart; the true interval is 100.
        wing_low = [43500, 45000, 46500, 48000]          # 1500-pt wing
        core = list(range(49000, 61050, 100))            # 100-pt near-money core
        wing_high = [62500, 64000, 65500, 67500, 69000]  # sparse upper wing
        strikes = sorted(set(wing_low + core + wing_high))
        # The OLD formula: strikes[1] - strikes[0] == 1500 (the bug).
        self.assertEqual(strikes[1] - strikes[0], 1500)
        # The FIX: the true modal/minimum interval.
        self.assertEqual(brain._infer_strike_step(strikes, 100), 100)

    def test_degenerate_chain_falls_back_to_default(self):
        self.assertEqual(brain._infer_strike_step([57000], 100), 100)
        self.assertEqual(brain._infer_strike_step([], 50), 50)

    def test_width_ladder_nonempty_at_true_step_and_empty_at_buggy_step(self):
        """The exact mechanism of the blackout: the width ladder.

        `generate_candidates` iterates ``for width in widths``; if the ladder is
        empty, no candidate is ever built and none is even recorded as rejected —
        which is precisely what production showed for BNF from 2026-08-26. The bug
        fed the ladder a 1500-pt step; the fix feeds it the true 100-pt interval.
        """
        base_widths = brain._CONST["BNF_WIDTHS"]

        buggy_step = 1500  # what all_strikes[1]-all_strikes[0] returned for BNF monthly
        empty = brain._pc2_batch_f_width_ladder("BNF", base_widths, buggy_step, "paper")
        self.assertEqual(
            list(empty["widths"]), [],
            "sanity: the 1500-pt step is what silently emptied the ladder",
        )

        true_step = 100
        healthy = brain._pc2_batch_f_width_ladder("BNF", base_widths, true_step, "paper")
        self.assertGreater(
            len(healthy["widths"]), 0,
            "with the true 100-pt interval the BNF width ladder must be non-empty",
        )


if __name__ == "__main__":
    unittest.main()
