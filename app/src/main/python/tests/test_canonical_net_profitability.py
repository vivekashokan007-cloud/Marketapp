"""Canonical net profitability measurement — SPEC 2026-09-13 regression."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from canonical_net_profitability import (
    REASON_CAPPED_PATH,
    REASON_MIXED_COHORT,
    REASON_NET_UNAVAILABLE,
    REASON_QUOTE_INCOMPLETE,
    SPEC_VERSION,
    assess_unit_eligibility,
    classify_units,
    compute_canonical_net_metrics,
)


def _unit(**kwargs):
    base = {
        "unit_kind": "candidate_day",
        "session_date": "2026-09-10",
        "policy_selector_version": "pc2_paper_primary_v7",
        "net_target_version": "net_target_v1_gross_minus_costs_once_20260912",
        "cohort_execution_mode": "paper_intraday",
        "variant": "ACTIVE",
        "population_role": "paper",
        "legs_complete": True,
        "quotes_ok": True,
        "friction_rt": 40.0,
        "gross_pnl": 100.0,
        "net_pnl": 60.0,
        "managed_pnl": 60.0,
        "friction_baked_into_net": True,
        "status": "CLOSED",
        "outcome_finished": True,
        "role": "primary",
    }
    base.update(kwargs)
    return base


class EligibilityTests(unittest.TestCase):
    def test_complete_unit_eligible(self):
        a = assess_unit_eligibility(_unit())
        self.assertTrue(a["eligible"])
        self.assertEqual(a["net_pnl"], 60.0)

    def test_capped_ineligible(self):
        a = assess_unit_eligibility(_unit(capped=True))
        self.assertFalse(a["eligible"])
        self.assertIn(REASON_CAPPED_PATH, a["reasons"])

    def test_incomplete_export_ineligible(self):
        a = assess_unit_eligibility(_unit(export_status="incomplete_truncated"))
        self.assertFalse(a["eligible"])

    def test_missing_net_not_zero(self):
        u = _unit()
        u.pop("net_pnl")
        u.pop("managed_pnl")
        u.pop("gross_pnl")
        u["friction_baked_into_net"] = False
        u["friction_rt"] = None
        a = assess_unit_eligibility(u)
        self.assertFalse(a["eligible"])
        self.assertIsNone(a["net_pnl"])
        self.assertTrue(
            REASON_NET_UNAVAILABLE in a["reasons"]
            or any("FRICTION" in r or "NET" in r for r in a["reasons"])
        )

    def test_quote_incomplete_ineligible(self):
        a = assess_unit_eligibility(_unit(quote_incomplete=True))
        self.assertFalse(a["eligible"])
        self.assertIn(REASON_QUOTE_INCOMPLETE, a["reasons"])

    def test_never_fabricate(self):
        a = assess_unit_eligibility(_unit(fabricated=True, net_pnl=0.0, managed_pnl=0.0))
        self.assertFalse(a["eligible"])


class NoMixTests(unittest.TestCase):
    def test_mixed_realized_hypothetical_refused(self):
        units = [
            _unit(population_role="paper", managed_pnl=10),
            _unit(population_role="menu", managed_pnl=999, variant="SHADOW_A"),
        ]
        c = classify_units(units)
        self.assertFalse(c["ok"])
        self.assertEqual(c["reason_code"], REASON_MIXED_COHORT)
        m = compute_canonical_net_metrics(units)
        self.assertEqual(m["availability"], "unavailable")
        self.assertIsNone(m["sum_net_pnl"])


class MetricsTests(unittest.TestCase):
    def test_day_aggregation_and_drawdown(self):
        units = [
            _unit(session_date="2026-09-10", managed_pnl=100, net_pnl=100, friction_rt=0),
            _unit(session_date="2026-09-11", managed_pnl=-40, net_pnl=-40, friction_rt=0),
            _unit(session_date="2026-09-12", managed_pnl=20, net_pnl=20, friction_rt=0),
        ]
        m = compute_canonical_net_metrics(units, min_eligible=1)
        self.assertEqual(m["availability"], "available")
        self.assertEqual(m["spec_version"], SPEC_VERSION)
        self.assertEqual(m["n_eligible"], 3)
        self.assertEqual(m["sum_net_pnl"], 80.0)
        self.assertEqual(m["drawdown"]["max_drawdown_abs"], -40.0)
        self.assertEqual(m["win_loss"]["n_win"], 2)
        self.assertEqual(m["win_loss"]["n_loss"], 1)
        self.assertFalse(m["training_enabled"])
        self.assertFalse(m["promotion_enabled"])

    def test_ineligible_excluded_from_ev(self):
        units = [
            _unit(session_date="2026-09-10", managed_pnl=50, net_pnl=50, friction_rt=0),
            _unit(session_date="2026-09-11", managed_pnl=-999, net_pnl=-999, capped=True),
        ]
        m = compute_canonical_net_metrics(units)
        self.assertEqual(m["n_eligible"], 1)
        self.assertEqual(m["n_ineligible"], 1)
        self.assertEqual(m["sum_net_pnl"], 50.0)
        self.assertIn(REASON_CAPPED_PATH, m["reason_histogram"])

    def test_model_edge_separate_from_realized(self):
        units = [_unit(managed_pnl=10, net_pnl=10, friction_rt=0)]
        m = compute_canonical_net_metrics(units, model_edge=1234.5)
        self.assertEqual(m["mean_net_pnl"], 10.0)
        self.assertEqual(m["net_edge_model"], 1234.5)
        self.assertIn("not realized", m["note"])

    def test_no_zero_fill_missing_days(self):
        # Only two eligible days — curve length 2, no invented middle day
        units = [
            _unit(session_date="2026-09-10", managed_pnl=10, net_pnl=10, friction_rt=0),
            _unit(session_date="2026-09-12", managed_pnl=10, net_pnl=10, friction_rt=0),
        ]
        m = compute_canonical_net_metrics(units)
        dates = [p["session_date"] for p in m["drawdown"]["equity_curve"]]
        self.assertEqual(dates, ["2026-09-10", "2026-09-12"])
        self.assertNotIn("2026-09-11", dates)


if __name__ == "__main__":
    unittest.main()
