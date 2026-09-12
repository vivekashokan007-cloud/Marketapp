"""G3 peak/trough persistence + dry-run repair acceptance tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gross_extrema import (  # noqa: E402
    apply_patch_is_safe,
    build_close_extrema_fields,
    merge_peak,
    merge_trough,
    normalize_gross_extrema,
    propose_repair,
)


class TestGrossExtremaNormalize(unittest.TestCase):
    def test_positive_peak_survives_close_payload_construction(self):
        fields = build_close_extrema_fields({"peak_pnl": 2400, "trough_pnl": -300})
        self.assertEqual(fields["peak_pnl"], 2400)
        self.assertEqual(fields["trough_pnl"], -300)
        self.assertEqual(fields["peak_pnl_validity"], "valid")
        self.assertEqual(fields["extrema_basis"], "GROSS_MTM")
        self.assertEqual(fields["extrema_unit"], "INR_TOTAL")

    def test_valid_zero_remains_zero(self):
        fields = build_close_extrema_fields({"peak_pnl": 0, "trough_pnl": 0})
        self.assertEqual(fields["peak_pnl"], 0)
        self.assertEqual(fields["trough_pnl"], 0)
        self.assertEqual(fields["peak_pnl_validity"], "valid")

    def test_unknown_stays_null_not_fabricated_zero(self):
        fields = build_close_extrema_fields({})  # keys absent
        self.assertIsNone(fields["peak_pnl"])
        self.assertIsNone(fields["trough_pnl"])
        self.assertEqual(fields["peak_pnl_validity"], "unknown")
        fields2 = build_close_extrema_fields(
            {"peak_pnl": 100, "peak_pnl_validity": "unknown", "trough_pnl": None}
        )
        self.assertIsNone(fields2["peak_pnl"])

    def test_merge_preserves_null_unknown(self):
        self.assertEqual(merge_peak(None, 50), 50)
        self.assertEqual(merge_peak(80, None), 80)
        self.assertIsNone(merge_peak(None, None))
        self.assertEqual(merge_trough(None, -20), -20)
        self.assertEqual(merge_trough(-40, -10), -40)


class TestProposeRepair(unittest.TestCase):
    def test_repair_from_verified_journey_when_no_ticks(self):
        trade = {
            "id": "t1",
            "peak_pnl": 0,
            "trough_pnl": 0,
            "pnl_engine": None,
            "journey_stats": {"peak_pnl": 1800, "trough_pnl": -400},
            "close_trace_json": {"peak_pnl": 1800, "trough_pnl": -400},
        }
        p = propose_repair(trade, tick_n=0)
        self.assertEqual(p["action"], "repair")
        self.assertEqual(p["new_peak_pnl"], 1800)
        self.assertEqual(p["source"], "journey_stats_verified")

    def test_conflicting_tick_vs_journey_goes_to_review(self):
        trade = {
            "id": "t2",
            "peak_pnl": 0,
            "trough_pnl": 0,
            "journey_stats": {"peak_pnl": 1800, "trough_pnl": -400},
            "close_trace_json": {"peak_pnl": 1800, "trough_pnl": -400},
        }
        p = propose_repair(trade, tick_peak=3200, tick_trough=-500, tick_n=12)
        self.assertEqual(p["action"], "review")
        self.assertEqual(p["reason"], "conflicting_tick_vs_journey")

    def test_tick_agreement_prefers_ticks(self):
        trade = {
            "id": "t3",
            "peak_pnl": 0,
            "trough_pnl": 0,
            "journey_stats": {"peak_pnl": 1800, "trough_pnl": -400},
            "close_trace_json": {"peak_pnl": 1800, "trough_pnl": -400},
        }
        p = propose_repair(trade, tick_peak=1800.5, tick_trough=-400.2, tick_n=8)
        self.assertEqual(p["action"], "repair")
        self.assertEqual(p["source"], "position_ticks")
        self.assertAlmostEqual(p["new_peak_pnl"], 1800.5)

    def test_untrusted_not_relabelled_trustworthy_when_peak_copied(self):
        trade = {
            "id": "t4",
            "peak_pnl": 0,
            "trough_pnl": 0,
            "pnl_engine": "UNTRUSTED_INCOMPLETE_STRUCTURE",
            "journey_stats": {"peak_pnl": 900, "trough_pnl": -100},
            "close_trace_json": {"peak_pnl": 900, "trough_pnl": -100},
        }
        p = propose_repair(trade, tick_n=0)
        self.assertEqual(p["action"], "repair")
        self.assertTrue(p["untrusted"])
        self.assertFalse(p["trust_label_changed"])
        self.assertIn(
            "untrusted_engine_peak_copy_does_not_upgrade_trust", p["exclusions"]
        )
        # Repair patch must not include pnl_engine change
        self.assertEqual(p["pnl_engine"], "UNTRUSTED_INCOMPLETE_STRUCTURE")

    def test_preserve_better_existing_nonzero_peak(self):
        trade = {
            "id": "t5",
            "peak_pnl": 5000,
            "trough_pnl": -100,
            "journey_stats": {"peak_pnl": 1800, "trough_pnl": -400},
            "close_trace_json": {"peak_pnl": 1800, "trough_pnl": -400},
        }
        p = propose_repair(trade, tick_n=0)
        # Peak must not be overwritten with a worse journey value; trough may deepen.
        self.assertNotEqual(p.get("new_peak_pnl"), 1800)
        self.assertEqual(p.get("new_peak_pnl"), 5000)
        if p["action"] == "repair":
            self.assertTrue(p.get("trough_changed"))
            self.assertFalse(p.get("peak_changed", False))
        self.assertIn("preserve_better_existing_peak", p.get("exclusions") or [])

    def test_idempotent_apply_check(self):
        trade = {"id": "t6", "peak_pnl": 0, "trough_pnl": 0}
        proposal = {
            "action": "repair",
            "expected_old_peak_pnl": 0,
            "expected_old_trough_pnl": 0,
            "new_peak_pnl": 1200,
            "new_trough_pnl": -50,
        }
        ok, reason = apply_patch_is_safe(trade, proposal)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        trade2 = {"id": "t6", "peak_pnl": 1200, "trough_pnl": -50}
        ok2, reason2 = apply_patch_is_safe(trade2, proposal)
        self.assertFalse(ok2)
        self.assertEqual(reason2, "already_applied")
        trade3 = {"id": "t6", "peak_pnl": 99, "trough_pnl": 0}
        ok3, reason3 = apply_patch_is_safe(trade3, proposal)
        self.assertFalse(ok3)
        self.assertEqual(reason3, "concurrent_change_skip")

    def test_dry_run_tool_second_pass_idempotent(self):
        tools = Path(__file__).resolve().parents[5] / "tools" / "g3_peak_trough_repair.py"
        self.assertTrue(tools.is_file())
        fixture = [
            {
                "id": "f1",
                "peak_pnl": 0,
                "trough_pnl": 0,
                "pnl_engine": None,
                "journey_stats": {"peak_pnl": 1500, "trough_pnl": -200},
                "close_trace_json": {"peak_pnl": 1500, "trough_pnl": -200},
                "_tick_extrema": {"n": 0},
            },
            {
                "id": "f2",
                "peak_pnl": 0,
                "trough_pnl": 0,
                "pnl_engine": "UNKNOWN",
                "journey_stats": {"peak_pnl": 700, "trough_pnl": -50},
                "close_trace_json": {"peak_pnl": 999, "trough_pnl": -50},
                "_tick_extrema": {"n": 0},
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            fix = Path(td) / "fix.json"
            fix.write_text(json.dumps(fixture), encoding="utf-8")
            out = Path(td) / "manifest.csv"
            summary = Path(td) / "summary.json"
            import subprocess

            r = subprocess.run(
                [
                    sys.executable,
                    str(tools),
                    "--fixture",
                    str(fix),
                    "--out",
                    str(out),
                    "--summary-out",
                    str(summary),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            data = json.loads(summary.read_text(encoding="utf-8"))
            self.assertEqual(data["mode"], "dry_run")
            self.assertFalse(data["production_write_ran"])
            self.assertEqual(data["repair"], 1)  # f1
            self.assertEqual(data["review"], 1)  # f2 conflict journey vs trace
            self.assertEqual(data["second_pass"]["repair"], 0)
            self.assertIn("Production repair write was NOT run", r.stdout)


class TestNormalizeContract(unittest.TestCase):
    def test_normalize_does_not_overwrite_gross_with_net_fields(self):
        # Ensure helper only speaks gross basis
        n = normalize_gross_extrema(100, -20)
        self.assertEqual(n["extrema_basis"], "GROSS_MTM")
        self.assertNotIn("net_peak_pnl", n)


if __name__ == "__main__":
    unittest.main()
