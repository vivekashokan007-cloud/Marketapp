import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from brain import (
    PC2_COMPOSITE_SHADOW_VERSION,
    annotate_pc2_composite_shadow,
    select_pc2_paper_primary,
)


def candidate(candidate_id, edge, context):
    return {
        "id": candidate_id,
        "index": "NF",
        "type": "BEAR_CALL",
        "tDTE": 4,
        "directionSafe": True,
        "capitalBlocked": False,
        "entryEligible": True,
        "adjustedEdgePerRisk": edge,
        "contextPercentileScore": context,
        "probProfit": 0.60,
    }


REFERENCE = {
    "version": "test_historical_reference_v1",
    "groups": {
        "NF|BEAR|DTE_4_7": {
            "adjusted_edge_per_risk": {"low": -0.30, "high": 0.30},
            "context_percentile_score": {"low": -0.35, "high": 0.35},
        }
    },
}


class Pc2CompositeShadowTest(unittest.TestCase):
    def test_composite_uses_frozen_reference_and_prioritizes_economics(self):
        strong_economics = candidate("strong-economics", 0.20, 0.00)
        strong_context = candidate("strong-context", -0.10, 0.10)

        summary = annotate_pc2_composite_shadow([strong_context, strong_economics], REFERENCE)

        self.assertEqual(summary["version"], PC2_COMPOSITE_SHADOW_VERSION)
        self.assertEqual(summary["scored_count"], 2)
        self.assertEqual(strong_economics["pc2CompositeShadow"]["research_rank"], 1)
        self.assertEqual(strong_context["pc2CompositeShadow"]["research_rank"], 2)
        self.assertEqual(
            strong_economics["pc2CompositeShadow"]["reference_scope"],
            "NF|BEAR|DTE_4_7",
        )

    def test_composite_refuses_current_menu_normalization_without_reference(self):
        row = candidate("no-reference", 0.20, 0.10)

        summary = annotate_pc2_composite_shadow([row])

        self.assertEqual(summary["scored_count"], 0)
        self.assertEqual(row["pc2CompositeShadow"]["status"], "REFERENCE_UNAVAILABLE")
        self.assertIsNone(row["pc2CompositeShadow"]["score"])

    def test_composite_annotation_never_changes_live_pc2_paper_selection(self):
        economics = candidate("economics", 0.30, -0.20)
        context = candidate("context", 0.10, 0.25)

        annotate_pc2_composite_shadow([economics, context], REFERENCE)
        ordered, summary = select_pc2_paper_primary([economics, context], "paper")

        self.assertEqual(economics["pc2CompositeShadow"]["research_rank"], 1)
        self.assertEqual(ordered[0]["id"], "context")
        self.assertEqual(summary["pc2_primary_candidate_id"], "context")


if __name__ == "__main__":
    unittest.main()
