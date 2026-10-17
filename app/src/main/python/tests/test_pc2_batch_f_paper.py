import copy
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import brain
from brain import (
    CANDLE_PAPER_POLICY_VERSION,
    PC2_BATCH_F_CANDLE_SCORE_CAP,
    _apply_pc2_batch_f_candle_context,
    _pc2_batch_f_width_ladder,
)

# b510 Batch B: explicit Paper is telemetry-only by default. The scoring math is still
# covered (a) with the reviewed ranking flag enabled and (b) on the unresolved-mode path,
# which historically defaulted to scoring and is intentionally left unchanged.
RANKING_ON = patch.object(brain, "CANDLE_INSIGHTS_IN_PAPER_RANKING", True)


class Pc2BatchFPaperTest(unittest.TestCase):
    def test_paper_width_ladder_only_adds_strike_valid_widths(self):
        paper = _pc2_batch_f_width_ladder("BNF", [200, 300], 100, "paper")
        sandbox = _pc2_batch_f_width_ladder("BNF", [200, 300], 100, "sandbox")

        self.assertEqual(paper["paper_expanded_widths"], [100, 700, 900, 1200])
        self.assertEqual(paper["widths"], [200, 300, 100, 700, 900, 1200])
        self.assertEqual(sandbox["widths"], [200, 300])
        self.assertFalse(sandbox["paper_active"])

    def _directional_and_bounded(self, **kw):
        candidates = [
            {"id": "bull", "index": "NF", "type": "BULL_CALL", "contextPercentileScore": 0.20},
            {"id": "bear", "index": "NF", "type": "BEAR_PUT", "contextPercentileScore": 0.20},
        ]
        candle_data = {
            "candle_nf": {
                "patterns": [
                    {"pattern": "BULLISH_ENGULFING", "impact": "bullish", "strength": 5, "timeframe": "15m"},
                    {"pattern": "DOJI", "impact": "caution", "strength": 2, "timeframe": "15m"},
                ]
            }
        }

        summary = _apply_pc2_batch_f_candle_context(candidates, candle_data, "paper", **kw)

        self.assertEqual(candidates[0]["pc2BatchFCandleScore"], PC2_BATCH_F_CANDLE_SCORE_CAP)
        self.assertEqual(candidates[1]["pc2BatchFCandleScore"], -PC2_BATCH_F_CANDLE_SCORE_CAP)
        self.assertEqual(candidates[0]["contextPercentileScore"], 0.23)
        self.assertEqual(candidates[1]["contextPercentileScore"], 0.17)
        self.assertEqual(candidates[0]["pc2BatchFCandleCautions"], ["DOJI"])
        self.assertEqual(summary["candidates_with_candle_influence"], 2)

    def test_candle_context_is_directional_and_bounded_when_ranking_flag_enabled(self):
        with RANKING_ON:
            self._directional_and_bounded()

    def test_unresolved_mode_keeps_previous_scoring_path(self):
        # Call site passes 'paper' as the historical default with explicit_paper=False.
        self._directional_and_bounded(explicit_paper=False)

    def test_default_explicit_paper_is_telemetry_only(self):
        base = [
            {"id": "bear", "index": "NF", "type": "BEAR_PUT", "contextPercentileScore": 0.21,
             "contextPercentileRawScore": 0.21},
            {"id": "bull", "index": "NF", "type": "BULL_CALL", "contextPercentileScore": 0.20,
             "contextPercentileRawScore": 0.20},
        ]
        candle_data = {"candle_nf": {"patterns": [
            {"pattern": "BULLISH_ENGULFING", "impact": "bullish", "strength": 5, "timeframe": "15m"},
            {"pattern": "HAMMER", "impact": "bullish", "strength": 5, "timeframe": "15m"},
            {"pattern": "DOJI", "impact": "caution", "strength": 2, "timeframe": "15m"},
        ]}}
        candidates = copy.deepcopy(base)
        summary = _apply_pc2_batch_f_candle_context(candidates, candle_data, "paper")
        self.assertFalse(summary["active"])
        self.assertEqual(summary["candidates_with_candle_influence"], 0)
        self.assertEqual(summary["candle_scoring_method"], CANDLE_PAPER_POLICY_VERSION)
        for before, after in zip(base, candidates):
            self.assertEqual(after["pc2BatchFCandleScore"], 0.0)
            self.assertEqual(after["contextPercentileScore"], before["contextPercentileScore"])
            self.assertEqual(after["contextPercentileRawScore"], before["contextPercentileRawScore"])
            self.assertNotIn("contextPercentileClamp", after)
            self.assertNotIn("contextPercentileComponents", after)
            self.assertEqual(after["pc2BatchFCandleComponents"], [])
            self.assertFalse(after["pc2BatchFActive"])
            self.assertEqual(after["pc2BatchFCandleTelemetryPatterns"], ["BULLISH_ENGULFING", "HAMMER", "DOJI"])
        # Ordering by context score is unchanged (with scoring ON the bull would overtake the bear).
        order = lambda rows: [c["id"] for c in sorted(rows, key=lambda c: -c["contextPercentileScore"])]
        self.assertEqual(order(candidates), order(base))
        with RANKING_ON:
            scored = copy.deepcopy(base)
            _apply_pc2_batch_f_candle_context(scored, candle_data, "paper")
        self.assertNotEqual(order(scored), order(base))

    def test_candle_context_is_inactive_outside_paper(self):
        candidate = {"id": "bull", "index": "NF", "type": "BULL_CALL", "contextPercentileScore": 0.20}
        candle_data = {
            "candle_nf": {
                "patterns": [
                    {"pattern": "BULLISH_MARUBOZU", "impact": "bullish", "strength": 5, "timeframe": "15m"}
                ]
            }
        }

        summary = _apply_pc2_batch_f_candle_context([candidate], candle_data, "sandbox")

        self.assertFalse(summary["active"])
        self.assertEqual(candidate["pc2BatchFCandleScore"], 0.0)
        self.assertEqual(candidate["contextPercentileScore"], 0.20)

    def test_candle_context_reclamps_the_final_context_score(self):
        candidate = {
            "id": "bull",
            "index": "NF",
            "type": "BULL_CALL",
            "contextPercentileScore": 0.34,
            "contextPercentileRawScore": 0.34,
        }
        candle_data = {
            "candle_nf": {
                "patterns": [
                    {"pattern": "BULLISH_ENGULFING", "impact": "bullish", "strength": 5, "timeframe": "15m"}
                ]
            }
        }

        with RANKING_ON:
            _apply_pc2_batch_f_candle_context([candidate], candle_data, "paper")

        self.assertEqual(candidate["contextPercentileRawScore"], 0.37)
        self.assertEqual(candidate["contextPercentileScore"], 0.35)
        self.assertTrue(candidate["contextPercentileClamp"]["applied"])
        self.assertEqual(candidate["contextPercentileClamp"]["stage"], "post_batch_f_candle")

    def test_wick_dependent_patterns_are_evidence_only_for_spot_proxy_candles(self):
        candidate = {"id": "bull", "index": "NF", "type": "BULL_CALL", "contextPercentileScore": 0.20}
        candle_data = {
            "candle_nf": {
                "patterns": [
                    {"pattern": "HAMMER", "impact": "bullish", "strength": 5, "timeframe": "15m"},
                    {"pattern": "BULLISH_MARUBOZU", "impact": "bullish", "strength": 5, "timeframe": "15m"},
                ]
            }
        }

        with RANKING_ON:
            summary = _apply_pc2_batch_f_candle_context([candidate], candle_data, "paper")

        self.assertEqual(candidate["pc2BatchFCandleScore"], 0.0)
        self.assertEqual(candidate["contextPercentileScore"], 0.20)
        self.assertEqual(candidate["pc2BatchFCandleExcludedPatterns"], ["HAMMER", "BULLISH_MARUBOZU"])
        self.assertEqual(summary["candle_scoring_method"], "body_pattern_proxy_only_v1")


if __name__ == "__main__":
    unittest.main()
