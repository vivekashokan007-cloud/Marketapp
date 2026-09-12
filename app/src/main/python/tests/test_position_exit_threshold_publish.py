"""Percentile context ported into the 60-second tick service, by publication.

D4 made `PositionTickService` authoritative for threshold position exits, which
cost the percentile-contextual sensitivity of `_pc2_position_alert_context` — the
tick path ran on `PositionPolicyV1`'s fixed constants alone.

Reimplementing the percentile statistics in Kotlin would put one policy in two
languages, which is the failure this codebase keeps paying for (nested vs flat
`forces`, the on-conflict target that did not match the real constraint, two
disagreeing exit threshold sets). brain.py instead resolves the levels it
already computes into plain rupee P&L and publishes them; the tick service
compares a number and falls back to its constants when nothing fresh is
published.

Covered here:

* `_pc2_capture_trigger_level` is the exact inverse of the `_percentile_rank`
  convention, not a quantile approximation — the published level reproduces the
  live arm's own trigger rather than a second, subtly different rule;
* a level is published only when the live percentile arm has authority (the same
  support / diversity / stability gate that governs `live_pass`);
* the effective level is `min(constant, percentile)`, so publication can only
  make an alert fire earlier — never less protective than the constants;
* the payload carries what the tick service needs and nothing it cannot use.
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import brain  # noqa: E402
from brain import _percentile_rank  # noqa: E402

# Resolved defensively so a tree without the port still collects and fails with
# named assertions instead of an import error that hides which piece is missing.
POSITION_EXIT_THRESHOLD_MAX_AGE_MS = getattr(brain, "POSITION_EXIT_THRESHOLD_MAX_AGE_MS", 0)
POSITION_EXIT_THRESHOLD_PUBLISH_VERSION = getattr(
    brain, "POSITION_EXIT_THRESHOLD_PUBLISH_VERSION", ""
)
_pc2_capture_trigger_level = getattr(
    brain, "_pc2_capture_trigger_level", lambda *_args, **_kwargs: None
)


class CaptureTriggerLevelTests(unittest.TestCase):
    def test_level_is_the_exact_inverse_of_percentile_rank(self):
        """For a fresh value x: x > level  <=>  rank(x) >= cutoff.

        Randomised over many histories and cutoffs, because an off-by-one in the
        rank convention would publish a level one order statistic away from the
        arm it is supposed to reproduce — silently, and only on some histories.
        """
        rng = random.Random(20260912)
        checked = 0
        for _ in range(300):
            n = rng.randint(2, 40)
            history = [round(rng.uniform(0.0, 2.0), 4) for _ in range(n)]
            cutoff = rng.choice([50.0, 70.0, 80.0, 85.0, 90.0, 95.0])
            level = _pc2_capture_trigger_level(history, cutoff)
            if level is None:
                continue
            for _ in range(10):
                x = round(rng.uniform(-0.2, 2.2), 4)
                if x in history:
                    continue  # boundary equality is documented as more sensitive
                rank = _percentile_rank(x, history)
                if rank is None:
                    continue
                checked += 1
                self.assertEqual(
                    x > level,
                    rank >= cutoff,
                    f"level={level} x={x} rank={rank} cutoff={cutoff} history={history}",
                )
        self.assertGreater(checked, 500, "the property must actually be exercised")

    def test_empty_or_missing_inputs_publish_nothing(self):
        self.assertIsNone(_pc2_capture_trigger_level([], 85.0))
        self.assertIsNone(_pc2_capture_trigger_level([0.1, 0.2], None))

    def test_single_value_history_returns_that_value(self):
        self.assertEqual(0.42, _pc2_capture_trigger_level([0.42], 85.0))


def _trade(trade_id="t1"):
    return {
        "id": trade_id,
        "index_key": "NF",
        "strategy_type": "IRON_BUTTERFLY",
        "sell_strike": 23400,
        "max_profit": 1000.0,
        "max_loss": 2000.0,
        "valuation_quality": "full",
        "force_alignment": 3,
        "controlIndexMeta": {"signal_completeness_pct": 100},
    }


class PublishedThresholdTests(unittest.TestCase):
    def _publish(self, current_pnl, ctx=None):
        result = {}
        trade = dict(_trade(), current_pnl=current_pnl)
        brain.evaluate_alerts(
            open_trades=[trade],
            watchlist=[],
            result=result,
            ctx=ctx
            or {
                "mins_since_open": 90,
                "now_ms": 1_700_000,
                "significant_move": False,
                "entry_window_active": False,
            },
        )
        return result.get("position_exit_thresholds") or {}

    def test_thresholds_are_published_per_trade(self):
        published = self._publish(400.0)
        self.assertIn("t1", published, "the tick service needs a per-trade entry")
        row = published["t1"]
        self.assertEqual(POSITION_EXIT_THRESHOLD_PUBLISH_VERSION, row["schema_version"])
        self.assertEqual(1_700_000, row["computed_at_ms"])

    def test_levels_are_absolute_pnl_not_ratios(self):
        row = self._publish(400.0)["t1"]
        # max_profit 1000 x TARGET_NEAR_RATIO 0.8 -> 800; max_loss 2000 x 0.7 -> -1400.
        self.assertAlmostEqual(800.0, row["target_pnl_at"], places=4)
        self.assertAlmostEqual(-1400.0, row["stop_pnl_at"], places=4)
        self.assertLess(row["stop_pnl_at"], 0, "a stop level must be negative P&L")

    def test_without_percentile_authority_the_constant_stands(self):
        # No poll history in ctx -> no live percentile authority -> constants only.
        row = self._publish(400.0)["t1"]
        self.assertEqual("constant_safety_floor", row["target_basis"])
        self.assertEqual("constant_safety_floor", row["stop_basis"])
        self.assertAlmostEqual(
            brain._CONST["TARGET_NEAR_RATIO"], row["target_capture_ratio"], places=6
        )

    def test_published_level_never_exceeds_the_constant(self):
        """min() composition: publication may only make an alert fire earlier."""
        row = self._publish(400.0)["t1"]
        self.assertLessEqual(
            row["target_capture_ratio"],
            brain._CONST["TARGET_NEAR_RATIO"] + 1e-9,
            "a published target level must never be less sensitive than the constant",
        )
        self.assertLessEqual(
            row["stop_capture_ratio"],
            brain._CONST["STOP_LOSS_RATIO"] + 1e-9,
            "a published stop level must never be less sensitive than the constant",
        )

    def test_trade_without_usable_bounds_publishes_nothing(self):
        result = {}
        trade = dict(_trade(), current_pnl=10.0, max_profit=0, max_loss=0)
        brain.evaluate_alerts(
            open_trades=[trade],
            watchlist=[],
            result=result,
            ctx={"mins_since_open": 90, "now_ms": 1_700_000},
        )
        self.assertEqual(
            {},
            result.get("position_exit_thresholds") or {},
            "no bounds means no level — the tick service must keep its constants",
        )

    def test_max_age_is_defined_and_covers_more_than_one_poll(self):
        self.assertGreater(
            POSITION_EXIT_THRESHOLD_MAX_AGE_MS,
            5 * 60 * 1000,
            "a single missed 5-minute poll must not drop the tick service back to "
            "constants",
        )


class AlertRowLevelTests(unittest.TestCase):
    def test_alert_row_exposes_the_levels_the_publisher_reads(self):
        context = brain._pc2_position_alert_context(
            trade=_trade(),
            current_pnl=400.0,
            max_profit=1000.0,
            max_loss=2000.0,
            ctx={"now_ms": 1_700_000},
            context_percentiles={},
        )
        for key in ("target_near", "stop_loss_near"):
            row = context[key]
            for field in (
                "constant_trigger_level",
                "percentile_trigger_level",
                "effective_trigger_level",
                "trigger_level_basis",
                "trigger_level_series",
            ):
                self.assertIn(field, row, f"{key} must expose {field}")

    def test_existing_trigger_semantics_are_untouched(self):
        # The published levels are additive: the live trigger decision must not
        # have moved, or D4's notification behaviour changes under the covers.
        context = brain._pc2_position_alert_context(
            trade=_trade(),
            current_pnl=900.0,  # 90% of max_profit, above the 0.8 constant
            max_profit=1000.0,
            max_loss=2000.0,
            ctx={"now_ms": 1_700_000},
            context_percentiles={},
        )
        self.assertTrue(context["target_near"]["triggered"])
        self.assertEqual("constant_safety_floor", context["target_near"]["basis"])
        self.assertFalse(context["stop_loss_near"]["triggered"])


if __name__ == "__main__":
    unittest.main()
