"""G8/G9 pre-complete: lot size, expiry/DTE, NF/BNF identity integrity.

Behavioral suite proving contract-specific rupee P&L / costs / risk use the
declared lot table, and that performance slices separate by index / DTE /
strategy with thin_support (no silent pooling).
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from brain import (
    _CONST,
    _candidate_lot_size,
    _eval_single_candidate,
    _teacher_default_config,
    _teacher_execution_basis,
    _teacher_round_trip_cost,
)
from canonical_net_profitability import (
    CURRENT_CONTRACT_LOT_TABLE,
    DTE_MEASUREMENT_BUCKET_VERSION,
    attach_contract_identity,
    calendar_dte_from_expiry,
    compute_contract_slice_report,
    declared_lot_for_index,
    measurement_dte_bucket,
    resolve_contract_identity,
)
from evaluation_metrics_ledger import _slice_key, compute_slices
from evaluation_outcome_lineage import stamp_outcome_lineage


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
        "index_key": "BNF",
        "strategy_type": "BULL_PUT",
        "expiry": "2026-09-12",
        "dte": 2,
        "lot_size": 30,
    }
    base.update(kwargs)
    return base


class DeclaredLotTableTests(unittest.TestCase):
    def test_lot_table_matches_brain_const(self):
        self.assertEqual(CURRENT_CONTRACT_LOT_TABLE["BNF"], _CONST["BNF_LOT"])
        self.assertEqual(CURRENT_CONTRACT_LOT_TABLE["NF"], _CONST["NF_LOT"])
        self.assertEqual(declared_lot_for_index("BNF"), 30)
        self.assertEqual(declared_lot_for_index("NF"), 65)
        self.assertIsNone(declared_lot_for_index("UNKNOWN"))

    def test_candidate_lot_fail_closed_without_index(self):
        self.assertIsNone(_candidate_lot_size({}))
        self.assertEqual(_candidate_lot_size({"index": "BNF"}), float(_CONST["BNF_LOT"]))
        self.assertEqual(_candidate_lot_size({"index": "NF"}), float(_CONST["NF_LOT"]))
        self.assertEqual(_candidate_lot_size({"lotSize": 50, "index": "NF"}), 50.0)


class DteMeasurementBucketTests(unittest.TestCase):
    def test_transparent_buckets(self):
        self.assertEqual(measurement_dte_bucket(0), "DTE_0")
        self.assertEqual(measurement_dte_bucket(1), "DTE_1_2")
        self.assertEqual(measurement_dte_bucket(2), "DTE_1_2")
        self.assertEqual(measurement_dte_bucket(3), "DTE_3_7")
        self.assertEqual(measurement_dte_bucket(7), "DTE_3_7")
        self.assertEqual(measurement_dte_bucket(8), "DTE_8_PLUS")
        self.assertEqual(measurement_dte_bucket(None), "UNKNOWN")
        self.assertIn("measurement", DTE_MEASUREMENT_BUCKET_VERSION)

    def test_calendar_dte_from_expiry(self):
        self.assertEqual(calendar_dte_from_expiry("2026-09-10", "2026-09-10"), 0)
        self.assertEqual(calendar_dte_from_expiry("2026-09-10", "2026-09-12"), 2)
        self.assertIsNone(calendar_dte_from_expiry("", "2026-09-12"))
        self.assertIsNone(calendar_dte_from_expiry("2026-09-10", None))


class ContractIdentityPreserveTests(unittest.TestCase):
    def test_resolve_fail_closed_unknown(self):
        got = resolve_contract_identity({"session_date": "2026-09-10"})
        self.assertEqual(got["index_key"], "UNKNOWN")
        self.assertFalse(got["index_known"])
        self.assertIsNone(got["expiry"])
        self.assertIsNone(got["dte"])
        self.assertEqual(got["dte_bucket"], "UNKNOWN")
        self.assertIsNone(got["lot_size"])
        self.assertFalse(got["identity_complete"])

    def test_attach_and_lineage_stamp(self):
        row = {
            "session_date": "2026-09-10",
            "snapshot_id": 1,
            "candidate_id": "c1",
            "index_key": "NF",
            "expiry": "2026-09-17",
            "lotSize": 65,
            "strategy_type": "BEAR_CALL",
        }
        stamp_outcome_lineage(row)
        self.assertEqual(row["dte"], 7)
        self.assertEqual(row["dte_bucket"], "DTE_3_7")
        self.assertEqual(row["lot_size"], 65)
        self.assertIn("contract_identity", row["evaluation_lineage"])
        self.assertTrue(row["evaluation_lineage"]["contract_identity"]["identity_complete"])


class RupeeLotIndexBehavioralTests(unittest.TestCase):
    """Swapping lot or index must change rupee P&L / cost / risk as expected."""

    def _snap(self):
        return {
            "id": 501,
            "session_date": "2026-06-15",
            "poll_ts": "2026-06-15T10:00:00+05:30",
            "context_json": json.dumps({"vix": 15.0}),
        }

    def _cand(self, *, index="BNF", lot=30, expiry="2026-06-18", tDTE=3):
        width = 200
        credit = 40.0
        return {
            "id": f"cand-{index}-{lot}",
            "type": "BULL_PUT",
            "lane": f"{index}_intraday",
            "index": index,
            "trade_mode": "intraday",
            "expiry": expiry,
            "tDTE": tDTE,
            "sellStrike": 57000 if index == "BNF" else 23500,
            "buyStrike": 56800 if index == "BNF" else 23400,
            "sellType": "PE",
            "buyType": "PE",
            "lotSize": lot,
            "netPremium": credit,
            "maxProfit": credit * lot,
            "maxLoss": (width - credit) * lot,
            "isCredit": True,
        }

    def _entry_close_points(self, cand):
        # Entry: sell 45 / buy 5 → credit 40. Close: sell 20 / buy 5 → cost 15.
        # Gross points = 25; rupee gross = 25 * lot.
        entry = {
            "sell": 45.0, "sell_bid": 45.0, "sell_ask": 45.5,
            "buy": 5.0, "buy_bid": 4.5, "buy_ask": 5.0,
            "poll_ts": "2026-06-15T10:00:00+05:30",
        }
        close = {
            "sell": 20.0, "sell_bid": 19.5, "sell_ask": 20.0,
            "buy": 5.0, "buy_bid": 5.0, "buy_ask": 5.5,
            "poll_ts": "2026-06-15T10:15:00+05:30",
        }
        return entry, close

    def test_gross_pnl_scales_with_lot(self):
        snap = self._snap()
        for lot in (30, 60):
            cand = self._cand(lot=lot)
            entry, close = self._entry_close_points(cand)
            # Monkey-patch entry via snap primary fields used by _entry_snapshot_point:
            # use explicit entry_point arg.
            basis = _teacher_execution_basis(snap, cand, close, entry_point=entry)
            self.assertIsNotNone(basis)
            self.assertEqual(basis["lot_size"], lot)
            self.assertAlmostEqual(basis["gross_pnl"], 25.0 * lot, places=2)

        b30 = _teacher_execution_basis(snap, self._cand(lot=30), self._entry_close_points(self._cand())[1],
                                       entry_point=self._entry_close_points(self._cand())[0])
        b60 = _teacher_execution_basis(snap, self._cand(lot=60), self._entry_close_points(self._cand(lot=60))[1],
                                       entry_point=self._entry_close_points(self._cand(lot=60))[0])
        self.assertAlmostEqual(b60["gross_pnl"], b30["gross_pnl"] * 2.0, places=2)
        self.assertAlmostEqual(b60["entry_basis_currency"], b30["entry_basis_currency"] * 2.0, places=2)

    def test_friction_turnover_scales_with_lot(self):
        snap = self._snap()
        cfg = _teacher_default_config()
        entry, close = self._entry_close_points(self._cand())
        # _teacher_round_trip_cost reads entry from _entry_snapshot_point(snap, cand).
        # Seed candidate entry quotes onto snap context via candidate fields the helper uses.
        # Use a minimal path: put entry quotes on cand-compatible snap by overriding
        # through cand keys that _entry_snapshot_point reads — fall back to calling with
        # a snap that embeds entry bid/ask on the candidate snapshot point builder.
        from brain import _entry_snapshot_point

        # Build a synthetic snap whose entry point matches our entry quotes by placing
        # them on the candidate and using a chain-free path. _entry_snapshot_point pulls
        # from cand / snap; for unit test we patch by setting fields the normalizer uses.
        cand30 = self._cand(lot=30)
        cand65 = self._cand(index="NF", lot=65)
        # Provide entry quotes via snap primary embedding: context with no chain, so we
        # inject via monkeypatch of _entry_snapshot_point return by using cand + point only
        # through execution basis (already tested) and direct cost with patched entry.
        import brain as brain_mod
        original = brain_mod._entry_snapshot_point

        def _fake_entry(snap_arg, cand_arg):
            return entry

        brain_mod._entry_snapshot_point = _fake_entry
        try:
            from datetime import datetime, timezone, timedelta
            ist = timezone(timedelta(hours=5, minutes=30))
            trade_dt = datetime(2026, 6, 15, 10, 0, 0, tzinfo=ist)
            c30 = _teacher_round_trip_cost(trade_dt, snap, cand30, close, cfg)
            c65 = _teacher_round_trip_cost(trade_dt, snap, cand65, close, cfg)
        finally:
            brain_mod._entry_snapshot_point = original

        self.assertEqual(c30.get("status"), "OK")
        self.assertEqual(c65.get("status"), "OK")
        self.assertAlmostEqual(c30["lot_size"], 30)
        self.assertAlmostEqual(c65["lot_size"], 65)
        # Turnover and variable costs scale with lot; brokerage is per-order (not lot).
        self.assertGreater(c65["entry_turnover"], c30["entry_turnover"])
        ratio = c65["entry_turnover"] / c30["entry_turnover"]
        self.assertAlmostEqual(ratio, 65 / 30, places=4)

    def test_eval_outcome_preserves_lot_expiry_dte_index(self):
        rows = [
            {
                "index_key": "BNF",
                "strike": 57000,
                "option_type": "PE",
                "expiry": "2026-06-18",
                "poll_ts": "2026-06-15T10:05:00+05:30",
                "ltp": 20.0,
                "bid": 20.0,
                "ask": 20.0,
                "underlying_spot": 56900,
                "session_date": "2026-06-15",
            },
            {
                "index_key": "BNF",
                "strike": 56800,
                "option_type": "PE",
                "expiry": "2026-06-18",
                "poll_ts": "2026-06-15T10:05:00+05:30",
                "ltp": 5.0,
                "bid": 5.0,
                "ask": 5.0,
                "underlying_spot": 56900,
                "session_date": "2026-06-15",
            },
            {
                "index_key": "BNF",
                "strike": 57000,
                "option_type": "PE",
                "expiry": "2026-06-18",
                "poll_ts": "2026-06-15T10:20:00+05:30",
                "ltp": 5.0,
                "bid": 5.0,
                "ask": 5.0,
                "underlying_spot": 56900,
                "session_date": "2026-06-15",
            },
            {
                "index_key": "BNF",
                "strike": 56800,
                "option_type": "PE",
                "expiry": "2026-06-18",
                "poll_ts": "2026-06-15T10:20:00+05:30",
                "ltp": 0.5,
                "bid": 0.5,
                "ask": 0.5,
                "underlying_spot": 56900,
                "session_date": "2026-06-15",
            },
        ]
        snap = {
            "id": 101,
            "session_date": "2026-06-15",
            "poll_ts": "2026-06-15T10:00:00+05:30",
            "context_json": json.dumps({
                "vix": 15.2,
                "bnfChain": {
                    "strikes": {
                        "57000": {"PE": {"ltp": 45.0, "bid": 45.0, "ask": 45.0}},
                        "56800": {"PE": {"ltp": 5.0, "bid": 5.0, "ask": 5.0}},
                    }
                },
            }),
        }
        cand = self._cand(lot=30, tDTE=3, expiry="2026-06-18")
        outcome = _eval_single_candidate(rows, snap, cand, _teacher_default_config())
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome["index_key"], "BNF")
        self.assertEqual(outcome["expiry"], "2026-06-18")
        self.assertEqual(outcome["tDTE"], 3)
        self.assertEqual(outcome["dte"], 3)
        self.assertEqual(outcome["dte_bucket"], "DTE_3_7")
        self.assertEqual(outcome["lot_size"], 30)
        self.assertIn("contract_identity", outcome)
        self.assertTrue(outcome["contract_identity"]["identity_complete"])

        # Swap lot → rupee managed_pnl / risk scale (same points path).
        cand60 = self._cand(lot=60, tDTE=3, expiry="2026-06-18")
        cand60["maxProfit"] = 40.0 * 60
        cand60["maxLoss"] = (200 - 40.0) * 60
        out60 = _eval_single_candidate(rows, snap, cand60, _teacher_default_config())
        self.assertIsNotNone(out60)
        self.assertEqual(out60["lot_size"], 60)
        # Gross and managed should roughly double (friction has fixed brokerage component).
        self.assertGreater(abs(out60["managed_gross_pnl"]), abs(outcome["managed_gross_pnl"]) * 1.5)
        self.assertAlmostEqual(
            out60["managed_gross_pnl"] / outcome["managed_gross_pnl"],
            2.0,
            places=1,
        )
        self.assertGreater(out60["risk_at_entry"], outcome["risk_at_entry"])

    def test_nf_vs_bnf_declared_lots_differ(self):
        self.assertNotEqual(_CONST["NF_LOT"], _CONST["BNF_LOT"])
        nf = _teacher_execution_basis(
            self._snap(),
            self._cand(index="NF", lot=_CONST["NF_LOT"]),
            self._entry_close_points(self._cand(index="NF", lot=_CONST["NF_LOT"]))[1],
            entry_point=self._entry_close_points(self._cand(index="NF", lot=_CONST["NF_LOT"]))[0],
        )
        bnf = _teacher_execution_basis(
            self._snap(),
            self._cand(index="BNF", lot=_CONST["BNF_LOT"]),
            self._entry_close_points(self._cand())[1],
            entry_point=self._entry_close_points(self._cand())[0],
        )
        self.assertAlmostEqual(nf["gross_pnl"] / bnf["gross_pnl"], _CONST["NF_LOT"] / _CONST["BNF_LOT"], places=4)


class SliceReportTests(unittest.TestCase):
    def test_separate_by_index_dte_strategy_with_thin_support(self):
        units = [
            _unit(index_key="BNF", dte=0, strategy_type="BULL_PUT", managed_pnl=10, net_pnl=10),
            _unit(index_key="BNF", dte=0, strategy_type="BULL_PUT", managed_pnl=20, net_pnl=20, session_date="2026-09-11"),
            _unit(index_key="NF", dte=5, strategy_type="BEAR_CALL", managed_pnl=-5, net_pnl=-5, lot_size=65, expiry="2026-09-15"),
        ]
        report = compute_contract_slice_report(units)
        self.assertIn("BNF", report["dims"]["index"])
        self.assertIn("NF", report["dims"]["index"])
        self.assertTrue(report["dims"]["index"]["BNF"]["thin_support"])
        self.assertTrue(report["dims"]["index"]["NF"]["thin_support"])
        self.assertIn("DTE_0", report["dims"]["dte_bucket"])
        self.assertIn("DTE_3_7", report["dims"]["dte_bucket"])
        self.assertIn("BULL_PUT", report["dims"]["strategy"])
        self.assertIn("BEAR_CALL", report["dims"]["strategy"])
        # Sparse cells stay separate — NF not pooled into BNF.
        self.assertNotEqual(
            report["dims"]["index"]["BNF"]["sum_net_pnl"],
            report["dims"]["index"]["NF"]["sum_net_pnl"],
        )
        sample = {
            "index": report["dims"]["index"],
            "dte_bucket": report["dims"]["dte_bucket"],
            "strategy": {k: {"support": v["support"], "thin_support": v["thin_support"]}
                         for k, v in report["dims"]["strategy"].items()},
        }
        # Expose for audit doc consumers
        self.assertIsInstance(sample["index"]["BNF"]["mean_net_ci"], dict)

    def test_metrics_ledger_dte_buckets_aligned(self):
        rows = [
            {"index_key": "BNF", "dte": 1, "strategy_type": "X", "managed_pnl": 1.0, "population_role": "paper"},
            {"index_key": "BNF", "dte": 4, "strategy_type": "X", "managed_pnl": 2.0, "population_role": "paper"},
            {"index_key": "NF", "expiry": "2026-09-20", "session_date": "2026-09-10",
             "strategy_type": "Y", "managed_pnl": 3.0, "population_role": "paper"},
        ]
        self.assertEqual(_slice_key(rows[0], "dte"), "DTE_1_2")
        self.assertEqual(_slice_key(rows[1], "dte"), "DTE_3_7")
        self.assertEqual(_slice_key(rows[2], "dte"), "DTE_8_PLUS")
        slices = compute_slices(rows)
        self.assertIn("DTE_1_2", slices["dte"])
        self.assertTrue(slices["dte"]["DTE_1_2"]["thin_support"])


class LiveSizingDisabledTests(unittest.TestCase):
    def test_g9_remains_advisory(self):
        import g9_sizing_kelly as g9
        self.assertEqual(g9.G9_EXPERIMENT_STATUS, "experimental_advisory_only")
        self.assertIn("never silently change live", g9.__doc__)


if __name__ == "__main__":
    unittest.main()
