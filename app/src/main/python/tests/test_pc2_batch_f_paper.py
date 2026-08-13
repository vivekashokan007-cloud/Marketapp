import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from brain import (
    PC2_BATCH_F_CANDLE_SCORE_CAP,
    _apply_pc2_batch_f_candle_context,
    _pc2_batch_f_width_ladder,
)


class Pc2BatchFPaperTest(unittest.TestCase):
    def test_paper_width_ladder_only_adds_strike_valid_widths(self):
        paper = _pc2_batch_f_width_ladder("BNF", [200, 300], 100, "paper")
        sandbox = _pc2_batch_f_width_ladder("BNF", [200, 300], 100, "sandbox")

        self.assertEqual(paper["paper_expanded_widths"], [100, 700, 900, 1200])
        self.assertEqual(paper["widths"], [200, 300, 100, 700, 900, 1200])
        self.assertEqual(sandbox["widths"], [200, 300])
        self.assertFalse(sandbox["paper_active"])

    def test_candle_context_is_directional_and_bounded_in_paper(self):
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

        summary = _apply_pc2_batch_f_candle_context(candidates, candle_data, "paper")

        self.assertEqual(candidates[0]["pc2BatchFCandleScore"], PC2_BATCH_F_CANDLE_SCORE_CAP)
        self.assertEqual(candidates[1]["pc2BatchFCandleScore"], -PC2_BATCH_F_CANDLE_SCORE_CAP)
        self.assertEqual(candidates[0]["contextPercentileScore"], 0.23)
        self.assertEqual(candidates[1]["contextPercentileScore"], 0.17)
        self.assertEqual(candidates[0]["pc2BatchFCandleCautions"], ["DOJI"])
        self.assertEqual(summary["candidates_with_candle_influence"], 2)

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


if __name__ == "__main__":
    unittest.main()
