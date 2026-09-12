"""G2 acceptance fixtures — trustworthy calibration input adapter."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import brain
from calibration_input import (
    CALIBRATION_INPUT_CONTRACT_VERSION,
    MIN_CALIBRATION_SUPPORT,
    admit_calibration_inputs,
    calibration_cache_signature,
    ist_hour_of,
    validate_calibration_trade,
)


def _base(**overrides):
    row = {
        "id": "t1",
        "status": "CLOSED",
        "paper": True,
        "index_key": "BNF",
        "lots": 1,
        "strategy_type": "BEAR_CALL",
        "sell_strike": 52000,
        "buy_strike": 52100,
        "actual_pnl": 100.0,
        "friction_cost": 40.0,
        "net_pnl": 60.0,
        "entry_date": "2026-09-10T04:30:00Z",  # 10:00 IST
        "exit_date": "2026-09-10T10:00:00+05:30",
        "entry_vix": 18.0,
        "trade_mode": "paper",
    }
    row.update(overrides)
    return row


class G2CalibrationInputTest(unittest.TestCase):
    def setUp(self):
        brain._calibration = None
        brain._cal_signature = None
        brain._cal_count = 0

    def test_gross_plus_100_cost_200_net_minus_100_is_loss(self):
        row = _base(actual_pnl=100.0, friction_cost=200.0, net_pnl=-100.0)
        v = validate_calibration_trade(row)
        self.assertTrue(v["eligible"], v)
        self.assertEqual(v["net_pnl"], -100.0)
        admitted = admit_calibration_inputs([row])["admitted"]
        self.assertEqual(len(admitted), 1)
        self.assertFalse(admitted[0]["learning_won"])
        cal = brain.build_calibration([row] * MIN_CALIBRATION_SUPPORT)
        # duplicate ids dedupe → insufficient unless unique ids
        rows = [_base(id=f"loss-{i}", actual_pnl=100.0, friction_cost=200.0, net_pnl=-100.0)
                for i in range(MIN_CALIBRATION_SUPPORT)]
        cal = brain.build_calibration(rows)
        self.assertFalse(cal.get("unavailable"))
        self.assertEqual(cal["strategy"]["BEAR_CALL"]["wins"], 0)
        self.assertEqual(cal["strategy"]["BEAR_CALL"]["total"], MIN_CALIBRATION_SUPPORT)

    def test_flagged_positives_excluded(self):
        for engine in ("UNTRUSTED_INCOMPLETE_STRUCTURE", "PNL_BASIS_DIVERGENT", "UNKNOWN"):
            row = _base(id=f"flag-{engine}", actual_pnl=500.0, net_pnl=400.0, friction_cost=100.0,
                        pnl_engine=engine)
            v = validate_calibration_trade(row)
            self.assertFalse(v["eligible"], engine)
            self.assertIn("excluded_engine", v["reason"])

    def test_absent_costs_never_become_zero(self):
        row = _base()
        row.pop("friction_cost")
        row.pop("net_pnl")
        v = validate_calibration_trade(row)
        self.assertFalse(v["eligible"])
        self.assertEqual(v["reason"], "null_engine_missing_cost_provenance")

    def test_valid_negative_and_zero_retained(self):
        neg = _base(id="neg", actual_pnl=-50.0, friction_cost=20.0, net_pnl=-70.0)
        zero = _base(id="zero", actual_pnl=20.0, friction_cost=20.0, net_pnl=0.0)
        self.assertTrue(validate_calibration_trade(neg)["eligible"])
        self.assertTrue(validate_calibration_trade(zero)["eligible"])
        batch = admit_calibration_inputs([neg, zero])
        self.assertEqual(batch["eligible_count"], 2)
        pnls = sorted(t["learning_pnl"] for t in batch["admitted"])
        self.assertEqual(pnls, [-70.0, 0.0])

    def test_duplicate_ids_deduped(self):
        a = _base(id="dup", net_pnl=10.0, actual_pnl=50.0, friction_cost=40.0, updated_at="2026-09-10T12:00:00+05:30")
        b = _base(id="dup", net_pnl=-30.0, actual_pnl=50.0, friction_cost=80.0, updated_at="2026-09-11T12:00:00+05:30")
        batch = admit_calibration_inputs([a, b])
        self.assertEqual(batch["eligible_count"], 1)
        self.assertEqual(batch["admitted"][0]["learning_pnl"], -30.0)

    def test_nonfinite_fail(self):
        row = _base(net_pnl=float("nan"), actual_pnl=100.0, friction_cost=10.0)
        # nan net with gross/cost should fail inconsistency or nonfinite path
        v = validate_calibration_trade(row)
        self.assertFalse(v["eligible"])

    def test_reconciled_missing_net_rejected(self):
        row = _base(pnl_engine="RECONCILED")
        row.pop("net_pnl")
        row.pop("friction_cost")
        row.pop("actual_pnl", None)
        row["actual_pnl"] = 100.0
        v = validate_calibration_trade(row)
        self.assertFalse(v["eligible"])
        self.assertIn("reconciled_but", v["reason"])

    def test_null_engine_requires_evidence_not_auto_pass(self):
        # missing costs → reject with recorded reason
        bad = _base()
        bad.pop("friction_cost")
        bad.pop("net_pnl")
        bad["pnl_engine"] = None
        v_bad = validate_calibration_trade(bad)
        self.assertFalse(v_bad["eligible"])
        self.assertTrue(v_bad["reason"].startswith("null_engine_"))
        # validated structure+cost+net → admit with reason recorded
        good = _base(pnl_engine=None)
        v_good = validate_calibration_trade(good)
        self.assertTrue(v_good["eligible"], v_good)
        self.assertIn("null_engine_validated", v_good["cost_provenance"])

    def test_paper_live_populations_separate(self):
        paper = [_base(id=f"p{i}", paper=True, net_pnl=10.0, actual_pnl=50.0, friction_cost=40.0)
                 for i in range(5)]
        live = [_base(id=f"l{i}", paper=False, trade_mode="live", execution_mode="live",
                      net_pnl=-100.0, actual_pnl=-50.0, friction_cost=50.0)
                for i in range(5)]
        paper_batch = admit_calibration_inputs(paper + live, cohort="paper")
        live_batch = admit_calibration_inputs(paper + live, cohort="live")
        self.assertEqual(paper_batch["eligible_count"], 5)
        self.assertEqual(live_batch["eligible_count"], 5)
        self.assertTrue(all(t["learning_pnl"] == 10.0 for t in paper_batch["admitted"]))
        self.assertTrue(all(t["learning_pnl"] == -100.0 for t in live_batch["admitted"]))
        cal = brain.build_calibration(paper + live, cohort="paper")
        self.assertEqual(cal["cohort"], "paper")
        self.assertEqual(cal["eligible_count"], 5)
        self.assertEqual(cal["strategy"]["BEAR_CALL"]["wins"], 5)

    def test_late_outcomes_excluded_from_prior_replay(self):
        row = _base(
            id="late",
            exit_date="2026-09-10T10:00:00+05:30",
            updated_at="2026-09-12T12:00:00+05:30",
            availability_time="2026-09-12T12:00:00+05:30",
        )
        v = validate_calibration_trade(row, decision_ts="2026-09-11T10:00:00+05:30")
        self.assertFalse(v["eligible"])
        self.assertEqual(v["reason"], "future_leakage_availability_after_decision")

    def test_cache_invalidates_on_net_or_quality_change(self):
        rows = [_base(id=f"c{i}", net_pnl=10.0, actual_pnl=50.0, friction_cost=40.0) for i in range(5)]
        cal1 = brain.build_calibration(rows)
        sig1 = brain._cal_signature
        self.assertFalse(cal1.get("unavailable"))
        # Change only net on one row — count and gross sum can be arranged to match old signature style
        rows2 = [dict(r) for r in rows]
        rows2[0]["net_pnl"] = -10.0
        rows2[0]["friction_cost"] = 60.0  # 50-60=-10
        cal2 = brain.build_calibration(rows2)
        self.assertNotEqual(sig1, brain._cal_signature)
        self.assertEqual(cal2["strategy"]["BEAR_CALL"]["wins"], 4)
        # Quality-only change
        rows3 = [dict(r) for r in rows]
        rows3[0]["pnl_engine"] = "UNKNOWN"
        cal3 = brain.build_calibration(rows3)
        self.assertNotEqual(sig1, brain._cal_signature)
        self.assertEqual(cal3["eligible_count"], 4)
        self.assertTrue(cal3.get("unavailable"))  # below min support

    def test_empty_support_never_reuses_stale_calibration(self):
        rows = [_base(id=f"s{i}", net_pnl=20.0, actual_pnl=50.0, friction_cost=30.0) for i in range(6)]
        cal_ok = brain.build_calibration(rows)
        self.assertFalse(cal_ok.get("unavailable"))
        stale_sig = brain._cal_signature
        # All become ineligible (missing costs)
        dirty = []
        for r in rows:
            d = dict(r)
            d.pop("friction_cost")
            d.pop("net_pnl")
            dirty.append(d)
        cal_bad = brain.build_calibration(dirty)
        self.assertTrue(cal_bad.get("unavailable"))
        self.assertNotEqual(stale_sig, brain._cal_signature)
        self.assertIsNone(brain._usable_calibration())

    def test_derive_net_from_gross_minus_cost(self):
        row = _base(id="derive")
        row.pop("net_pnl")
        v = validate_calibration_trade(row)
        self.assertTrue(v["eligible"], v)
        self.assertEqual(v["net_pnl"], 60.0)
        self.assertIn("derived_gross_minus_cost", v["cost_provenance"])

    def test_inconsistent_net_rejected(self):
        row = _base(actual_pnl=100.0, friction_cost=40.0, net_pnl=10.0)  # should be 60
        v = validate_calibration_trade(row)
        self.assertFalse(v["eligible"])
        self.assertEqual(v["reason"], "null_engine_inconsistent_net_vs_gross_minus_cost")

    def test_time_bucket_uses_asia_kolkata_not_raw_utc_hour(self):
        # 04:30Z = 10:00 IST → morning; raw UTC hour 4 would wrongly be morning too,
        # so use 10:30Z = 16:00 IST → late, while UTC hour 10 would be morning.
        rows = [
            _base(id=f"tod{i}", entry_date="2026-09-10T10:30:00Z",
                  net_pnl=10.0, actual_pnl=50.0, friction_cost=40.0)
            for i in range(5)
        ]
        self.assertEqual(ist_hour_of("2026-09-10T10:30:00Z"), 16)
        cal = brain.build_calibration(rows)
        self.assertIn("late", cal["time_of_day"])
        self.assertEqual(cal["time_of_day"]["late"]["total"], 5)
        self.assertNotIn("morning", cal.get("time_of_day", {}))

    def test_exit_capture_is_gross_diagnostic_not_learning(self):
        rows = []
        for i in range(5):
            rows.append(_base(
                id=f"ex{i}",
                net_pnl=50.0,
                actual_pnl=80.0,
                friction_cost=30.0,
                peak_pnl=100.0,
            ))
        cal = brain.build_calibration(rows)
        self.assertIsNone(cal.get("exit"))
        diag = cal.get("exit_gross_diagnostic")
        self.assertIsNotNone(diag)
        self.assertFalse(diag["learning_enabled"])
        self.assertIsNone(brain.risk_exit_analysis([], {}, [], rows))

    def test_no_edge_confirmed_language(self):
        rows = [_base(id=f"e{i}", net_pnl=30.0, actual_pnl=50.0, friction_cost=20.0,
                      strategy_type="BEAR_CALL", entry_vix=18) for i in range(3)]
        # Only 3 eligible — unavailable overall, pattern match returns None
        cal = brain.build_calibration(rows)
        self.assertTrue(cal.get("unavailable"))
        self.assertIsNone(brain.candidate_pattern_match({"type": "BEAR_CALL", "wallScore": 0}, [{"vix": 18}], {}, {}))
        rows = [_base(id=f"e{i}", net_pnl=30.0, actual_pnl=50.0, friction_cost=20.0,
                      strategy_type="BEAR_CALL", entry_vix=18) for i in range(6)]
        brain.build_calibration(rows)
        insight = brain.candidate_pattern_match({"type": "BEAR_CALL", "wallScore": 0}, [{"vix": 18}], {}, {})
        self.assertIsNotNone(insight)
        blob = (insight.get("detail") or "") + (insight.get("label") or "")
        self.assertNotIn("Edge confirmed", blob)
        self.assertIn("n=", blob)

    def test_all_learning_consumers_share_admitted_ids(self):
        rows = [_base(id=f"a{i}", net_pnl=15.0, actual_pnl=40.0, friction_cost=25.0) for i in range(5)]
        rows.append(_base(id="bad", pnl_engine="UNKNOWN", net_pnl=999.0, actual_pnl=1000.0, friction_cost=1.0))
        cal = brain.build_calibration(rows)
        self.assertEqual(set(cal["admitted_ids"]), {f"a{i}" for i in range(5)})
        self.assertNotIn("bad", cal["admitted_ids"])

    def test_signature_changes_when_only_bucket_field_changes(self):
        rows = [_base(id=f"b{i}", entry_date="2026-09-10T04:30:00Z") for i in range(5)]
        s1 = calibration_cache_signature(admit_calibration_inputs(rows)["admitted"])
        rows2 = [dict(r) for r in rows]
        rows2[0]["entry_date"] = "2026-09-10T10:30:00Z"  # IST hour changes
        s2 = calibration_cache_signature(admit_calibration_inputs(rows2)["admitted"])
        self.assertNotEqual(s1, s2)

    def test_contract_version_present(self):
        self.assertTrue(CALIBRATION_INPUT_CONTRACT_VERSION.startswith("calibration_input_v1"))
        rows = [_base(id=f"v{i}") for i in range(5)]
        cal = brain.build_calibration(rows)
        self.assertEqual(cal["contract_version"], CALIBRATION_INPUT_CONTRACT_VERSION)


if __name__ == "__main__":
    unittest.main()
