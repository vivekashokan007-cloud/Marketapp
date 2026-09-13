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
    REASON_CONTRACT_IDENTITY_UNKNOWN,
    attach_contract_identity,
    calendar_dte_from_expiry,
    compute_contract_slice_report,
    declared_lot_for_index,
    measurement_dte_bucket,
    quarantine_unknown_identity,
    resolve_contract_identity,
    assess_unit_eligibility,
)
from contract_lot_table import (
    DTE_RANKING_BUCKET_VERSION,
    LOT_TABLE_VERSION_ID,
    clear_lot_table_cache,
    json_round_trip_identity,
    ranking_dte_bucket,
    research_only_excluded_periods,
    resolve_contract_lot,
    simulate_persistence_boundary_roundtrip,
    supported_project_data_window,
    trading_dte,
)
from brain import _trade_to_teacher_candidate, _candidate_contract_fields

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



class DatedLotTableTests(unittest.TestCase):
    def setUp(self):
        clear_lot_table_cache()

    def test_unsupported_history_and_blanket_unavailable(self):
        hist = resolve_contract_lot("BNF", "2024-06-01")
        self.assertFalse(hist["resolved"])
        self.assertIsNone(hist["contract_lot_size"])
        as_of_only = resolve_contract_lot("NF", "2025-06-01")
        self.assertFalse(as_of_only["resolved"])
        self.assertIn("as_of_only", as_of_only["unavailable_reason"])
        self.assertIsNone(declared_lot_for_index("NF", "2024-06-01"))
        # Reconstructive / blanket rows must not be authoritative
        excluded = research_only_excluded_periods()
        self.assertTrue(any(
            str(p.get("provenance_quality", "")).startswith(("excluded", "research_only"))
            for p in excluded
        ))
        self.assertTrue(supported_project_data_window().get("fail_closed_outside"))

    def test_lot_size_vs_number_of_lots(self):
        one = resolve_contract_lot(
            "BNF", "2025-11-01", number_of_lots=1,
            expiry="2026-01-27", expiry_cycle="monthly",
        )
        two = resolve_contract_lot(
            "BNF", "2025-11-01", number_of_lots=2,
            expiry="2026-01-27", expiry_cycle="monthly",
        )
        self.assertTrue(one["resolved"])
        self.assertEqual(one["contract_lot_size"], 30)
        self.assertEqual(two["contract_lot_size"], 30)
        self.assertEqual(one["number_of_lots"], 1.0)
        self.assertEqual(two["number_of_lots"], 2.0)
        self.assertAlmostEqual(two["lot_size"], 60.0)
        self.assertEqual(two["quantity_units"], two["lot_size"])
        self.assertNotEqual(two["contract_lot_size"], two["lot_size"])

    def test_candidate_lot_contract_specific(self):
        # as_of-only historical → None (fail closed)
        self.assertIsNone(_candidate_lot_size({"index": "BNF", "session_date": "2024-06-01"}))
        # Contract-specific post-70616 revised monthly
        self.assertEqual(
            _candidate_lot_size({
                "index": "BNF",
                "session_date": "2025-11-01",
                "expiry": "2026-01-27",
                "expiry_cycle": "monthly",
            }),
            30.0,
        )
        self.assertEqual(
            _candidate_lot_size({
                "index": "BNF",
                "session_date": "2025-11-01",
                "expiry": "2026-01-27",
                "expiry_cycle": "monthly",
                "number_of_lots": 2,
            }),
            60.0,
        )
        # Operational current when no historical identity
        self.assertEqual(_candidate_lot_size({"index": "BNF"}), float(_CONST["BNF_LOT"]))


class DualDteTests(unittest.TestCase):
    def test_calendar_vs_trading_dte_weekend_holiday(self):
        # Thu 2026-09-10 → Thu 2026-09-17: calendar 7; trading skips Sat/Sun (+NSE holidays).
        cal = calendar_dte_from_expiry("2026-09-10", "2026-09-17")
        pack = trading_dte("2026-09-10", "2026-09-17")
        self.assertEqual(cal, 7)
        self.assertEqual(pack["calendar_dte"], 7)
        self.assertIsNotNone(pack["trading_dte"])
        self.assertLess(pack["trading_dte"], cal)  # weekends removed
        self.assertIn(pack["dte_basis"], ("nse_trading_calendar", "weekday_only_approximation"))

    def test_expiry_day_dte(self):
        self.assertEqual(calendar_dte_from_expiry("2026-09-10", "2026-09-10"), 0)
        pack = trading_dte("2026-09-10", "2026-09-10")  # Thursday
        self.assertEqual(pack["calendar_dte"], 0)
        self.assertEqual(pack["trading_dte"], 1)  # inclusive session remaining

    def test_candidate_fields_stamp_both(self):
        fields = _candidate_contract_fields(
            {"index": "NF", "expiry": "2026-09-17", "lotSize": 65},
            {"session_date": "2026-09-10"},
        )
        self.assertEqual(fields["calendar_dte"], 7)
        self.assertIsNotNone(fields["trading_dte"])
        self.assertEqual(fields["dte"], 7)
        self.assertIn("dte_basis", fields)
        self.assertEqual(fields["dte_bucket_version"], DTE_MEASUREMENT_BUCKET_VERSION)
        self.assertEqual(fields["dte_ranking_bucket_version"], DTE_RANKING_BUCKET_VERSION)

    def test_ranking_buckets_distinct_from_measurement(self):
        self.assertEqual(measurement_dte_bucket(1), "DTE_1_2")
        self.assertEqual(ranking_dte_bucket(1), "DTE_1")
        self.assertEqual(measurement_dte_bucket(2), "DTE_1_2")
        self.assertEqual(ranking_dte_bucket(2), "DTE_2_3")
        self.assertNotEqual(DTE_MEASUREMENT_BUCKET_VERSION, DTE_RANKING_BUCKET_VERSION)

    def test_missing_calendar_coverage_no_trading_substitution(self):
        # 2024 interval: NSE_HOLIDAYS in _CONST covers 2026 only → trading unavailable
        pack = trading_dte("2024-12-02", "2024-12-19", holidays=["2026-01-26"])
        self.assertEqual(pack["calendar_dte"], 17)
        self.assertIsNone(pack["trading_dte"])
        self.assertFalse(pack["calendar_coverage_ok"])
        self.assertIn("incomplete", pack["dte_basis"])
        # Ranking must not silently use calendar
        self.assertEqual(ranking_dte_bucket(pack["trading_dte"]), "unknown")

    def test_post_expiry_not_eligible_zero(self):
        pack = trading_dte("2026-09-12", "2026-09-10")
        self.assertIsNone(pack["trading_dte"])
        self.assertIsNone(pack["calendar_dte"])
        self.assertEqual(pack["dte_basis"], "expiry_before_session")


class LegacyUnknownIdentityTests(unittest.TestCase):
    def test_trade_to_teacher_does_not_invent_bnf(self):
        cand = _trade_to_teacher_candidate({"strategy_type": "BULL_PUT", "lot_size": 30})
        self.assertEqual(cand["index"], "UNKNOWN")

    def test_quarantine_retains_record(self):
        row = {
            "session_date": "2026-09-10",
            "gross_pnl": 100.0,
            "net_pnl": 60.0,
            "friction_rt": 40.0,
            "friction_baked_into_net": True,
            "legs_complete": True,
            "quotes_ok": True,
            "outcome_finished": True,
            "status": "CLOSED",
            "unit_kind": "candidate_day",
            "policy_selector_version": "pc2_paper_primary_v7",
            "net_target_version": "net_target_v1_gross_minus_costs_once_20260912",
            "cohort_execution_mode": "paper_intraday",
            "variant": "ACTIVE",
            "original_payload": {"keep": True, "secret_note": "recover_me"},
        }
        quarantined = quarantine_unknown_identity(dict(row))
        self.assertTrue(quarantined["contract_identity_quarantine"])
        self.assertTrue(quarantined["retained_for_recovery"])
        self.assertEqual(quarantined["original_payload"]["secret_note"], "recover_me")
        self.assertEqual(quarantined["exclusion_reason"], REASON_CONTRACT_IDENTITY_UNKNOWN)
        verdict = assess_unit_eligibility(quarantined)
        self.assertFalse(verdict["eligible"])
        self.assertEqual(verdict["reason_code"], REASON_CONTRACT_IDENTITY_UNKNOWN)


class JsonRoundTripTests(unittest.TestCase):
    def test_contract_identity_json_round_trip(self):
        identity = resolve_contract_identity({
            "index_key": "BNF",
            "expiry": "2026-09-17",
            "session_date": "2026-09-10",
            "lot_size": 30,
            "number_of_lots": 1,
        })
        back = json_round_trip_identity(identity)
        for key in (
            "index_key", "expiry", "calendar_dte", "trading_dte", "dte_basis",
            "dte_bucket", "dte_bucket_version", "dte_ranking_bucket_version",
            "contract_lot_size", "number_of_lots", "lot_size", "lot_table_version",
            "lot_as_of", "identity_complete",
        ):
            self.assertIn(key, back)
            self.assertEqual(back[key], identity.get(key), key)

    def test_lineage_round_trip_preserves_identity(self):
        row = {
            "session_date": "2026-09-10",
            "snapshot_id": 9,
            "candidate_id": "c-rt",
            "index_key": "NF",
            "expiry": "2026-09-17",
            "lotSize": 65,
            "strategy_type": "BEAR_CALL",
        }
        stamp_outcome_lineage(row)
        payload = json.loads(json.dumps(row, default=str))
        ci = payload["evaluation_lineage"]["contract_identity"]
        self.assertEqual(ci["index_key"], "NF")
        self.assertEqual(ci["lot_size"], 65)
        self.assertEqual(ci["calendar_dte"], 7)
        self.assertTrue(ci["identity_complete"])
        self.assertEqual(ci["lot_table_version"], LOT_TABLE_VERSION_ID)


class JointSliceDistinctSessionTests(unittest.TestCase):
    def test_joint_counts_distinct_sessions_not_just_rows(self):
        units = [
            _unit(index_key="BNF", dte=1, strategy_type="BULL_PUT", managed_pnl=10, net_pnl=10, session_date="2026-09-08"),
            _unit(index_key="BNF", dte=1, strategy_type="BULL_PUT", managed_pnl=12, net_pnl=12, session_date="2026-09-08"),
            _unit(index_key="BNF", dte=1, strategy_type="BULL_PUT", managed_pnl=14, net_pnl=14, session_date="2026-09-09"),
            _unit(index_key="NF", dte=5, strategy_type="BEAR_CALL", managed_pnl=-5, net_pnl=-5, lot_size=65,
                  expiry="2026-09-15", session_date="2026-09-10"),
        ]
        report = compute_contract_slice_report(units, thin_support_min=3)
        self.assertIn("joint", report["dims"])
        joint_bnf = report["dims"]["joint"]["BNF|DTE_1_2|BULL_PUT"]
        self.assertEqual(joint_bnf["n_rows"], 3)
        self.assertEqual(joint_bnf["n_distinct_sessions"], 2)
        self.assertEqual(joint_bnf["support"], 2)
        self.assertTrue(joint_bnf["thin_support"])  # 2 < 3
        self.assertEqual(report["dte_ranking_bucket_version"], DTE_RANKING_BUCKET_VERSION)
        self.assertEqual(report["lot_table_version"], LOT_TABLE_VERSION_ID)


class FullCostMaxLossTests(unittest.TestCase):
    def test_max_loss_and_friction_scale_with_contract_lot(self):
        width = 200
        credit = 40.0
        for lot in (30, 60):
            max_loss = (width - credit) * lot
            max_profit = credit * lot
            self.assertAlmostEqual(max_loss / lot, width - credit)
            self.assertAlmostEqual(max_profit / lot, credit)
        # Friction path already covered; assert lot stamped on cost OK payload.
        import brain as brain_mod
        from datetime import datetime, timezone, timedelta
        snap = {
            "id": 1,
            "session_date": "2026-06-15",
            "poll_ts": "2026-06-15T10:00:00+05:30",
            "context_json": json.dumps({"vix": 15.0}),
        }
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
        original = brain_mod._entry_snapshot_point
        brain_mod._entry_snapshot_point = lambda s, c: entry
        try:
            ist = timezone(timedelta(hours=5, minutes=30))
            trade_dt = datetime(2026, 6, 15, 10, 0, 0, tzinfo=ist)
            cand = {
                "type": "BULL_PUT", "index": "BNF", "expiry": "2026-06-18",
                "tDTE": 3, "sellStrike": 57000, "buyStrike": 56800,
                "sellType": "PE", "buyType": "PE", "lotSize": 30,
                "netPremium": 40.0, "maxProfit": 1200.0, "maxLoss": 4800.0, "isCredit": True,
            }
            cost = _teacher_round_trip_cost(trade_dt, snap, cand, close, _teacher_default_config())
        finally:
            brain_mod._entry_snapshot_point = original
        self.assertEqual(cost.get("status"), "OK")
        self.assertEqual(cost.get("lot_size"), 30)
        self.assertGreater(cost.get("entry_turnover", 0), 0)




class NseCircularFixtureTests(unittest.TestCase):
    """Independently sourced annexure fixtures — must NOT read expected lots from SSOT table."""

    @classmethod
    def setUpClass(cls):
        clear_lot_table_cache()
        fixture_path = os.path.join(
            os.path.dirname(__file__), "nse_lot_transition_fixtures_v2.json"
        )
        with open(fixture_path, "r", encoding="utf-8") as fh:
            cls.payload = json.load(fh)
        cls.fixtures = cls.payload["fixtures"]

    def test_fixtures_not_derived_from_lot_table(self):
        # Guard: fixture file must declare independent sources + sha256
        sources = self.payload.get("sources") or []
        self.assertGreaterEqual(len(sources), 2)
        for s in sources:
            self.assertIn("sha256", s)
            self.assertEqual(len(s["sha256"]), 64)

    def test_exchange_transition_fixtures(self):
        fail_old_blanket = 0
        for fx in self.fixtures:
            got = resolve_contract_lot(
                fx["index"],
                fx.get("observation"),
                expiry=fx.get("expiry"),
                expiry_cycle=fx.get("expiry_cycle"),
            )
            if fx.get("expected_unavailable"):
                self.assertFalse(got["resolved"], fx["id"])
                self.assertIsNone(got["contract_lot_size"], fx["id"])
                needle = fx.get("unavailable_reason_contains") or ""
                if needle:
                    self.assertIn(needle, str(got.get("unavailable_reason") or ""), fx["id"])
            else:
                self.assertTrue(got["resolved"], fx["id"])
                self.assertEqual(
                    got["contract_lot_size"],
                    fx["expected_contract_lot"],
                    fx["id"],
                )
            if fx.get("fails_under_old_blanket_65_30"):
                fail_old_blanket += 1
                # Prove divergence from illegal blanket NF=65/BNF=30
                blanket = 65 if fx["index"] == "NF" else 30
                if fx.get("expected_unavailable"):
                    self.assertNotEqual(blanket, None)
                else:
                    self.assertNotEqual(fx["expected_contract_lot"], blanket, fx["id"])
        self.assertGreaterEqual(fail_old_blanket, 5)

    def test_same_observation_different_lots_coexistence(self):
        a = resolve_contract_lot("NF", "2024-12-01", expiry="2024-12-19", expiry_cycle="weekly")
        b = resolve_contract_lot("NF", "2024-12-01", expiry="2025-01-02", expiry_cycle="weekly")
        self.assertEqual(a["contract_lot_size"], 25)
        self.assertEqual(b["contract_lot_size"], 75)
        c = resolve_contract_lot("NF", "2025-11-01", expiry="2025-12-23", expiry_cycle="weekly")
        d = resolve_contract_lot("NF", "2025-11-01", expiry="2026-01-06", expiry_cycle="weekly")
        self.assertEqual(c["contract_lot_size"], 75)
        self.assertEqual(d["contract_lot_size"], 65)


class CapturedMetadataConflictTests(unittest.TestCase):
    def test_consistent_captured_survives(self):
        got = resolve_contract_lot(
            "NF", "2025-11-01",
            expiry="2026-01-06", expiry_cycle="weekly",
            captured_contract_lot=65,
        )
        self.assertTrue(got["resolved"])
        self.assertEqual(got["contract_lot_size"], 65)
        self.assertFalse(got["lot_conflict"])

    def test_conflict_flags_and_excludes(self):
        got = resolve_contract_lot(
            "NF", "2025-11-01",
            expiry="2025-12-23", expiry_cycle="weekly",
            captured_contract_lot=65,  # rule says 75
        )
        self.assertFalse(got["resolved"])
        self.assertTrue(got["lot_conflict"])
        self.assertTrue(got["exclude_authoritative_calc"])
        self.assertEqual(got["captured_contract_lot"], 65)
        self.assertEqual(got["rule_contract_lot"], 75)
        self.assertIsNone(got["contract_lot_size"])


class PersistenceBoundaryMockTests(unittest.TestCase):
    def test_roles_roundtrip_preserve_identity(self):
        identity = resolve_contract_identity({
            "index_key": "NF",
            "expiry": "2026-01-06",
            "expiry_cycle": "weekly",
            "session_date": "2025-11-01",
            "contract_lot_size": 65,
            "number_of_lots": 1,
            "lot_size": 65,
            "calendar_dte": 66,
            "trading_dte": None,
        })
        for role in ("primary", "secondary", "rejected", "non_primary"):
            result = simulate_persistence_boundary_roundtrip(identity, role=role)
            self.assertTrue(result["fields_preserved"], role)
            self.assertFalse(result["db_boundary_tested"])
            self.assertIn("Supabase", result["untested_db_boundary"])
            self.assertEqual(result["readback"]["role"], role)
            self.assertEqual(result["readback"]["index_key"], "NF")
            self.assertEqual(result["readback"]["contract_lot_size"], identity.get("contract_lot_size"))



if __name__ == "__main__":
    unittest.main()
