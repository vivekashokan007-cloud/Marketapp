"""Codex R3 adversarial acceptance tests."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest

from canonical_net_profitability import resolve_contract_identity
from contract_identity_schema import (
    CONTRACT_IDENTITY_SCHEMA_VERSION,
    validate_contract_identity,
)


class R31MultiLotValuation(unittest.TestCase):
    def test_pnl_scales_with_lots_nf_bnf(self):
        from brain import compute_position_live

        def trade(index, cls, n_lots):
            qty = cls * n_lots
            return {
                "index_key": index,
                "strategy_type": "BEAR_CALL",
                "is_credit": True,
                "entry_premium": 20.0,
                "sell_strike": 25000 if index == "NF" else 52000,
                "buy_strike": 25100 if index == "NF" else 52200,
                "sell_type": "CE",
                "buy_type": "CE",
                "lots": n_lots,
                "contract_lot_size": cls,
                "number_of_lots": n_lots,
                "quantity_units": qty,
                "entry_snapshot": {
                    "contract_lot_size": cls,
                    "number_of_lots": n_lots,
                    "quantity_units": qty,
                    "lot_size": qty,
                },
            }

        # Minimal chain cache via ctx ltp maps
        nf_cache = {25000: {"CE": 30, "PE": 0}, 25100: {"CE": 10, "PE": 0}}
        bnf_cache = {52000: {"CE": 40, "PE": 0}, 52200: {"CE": 15, "PE": 0}}
        ctx_nf = {"nfLtpMap": {str(k): v for k, v in nf_cache.items()}}
        ctx_bnf = {"bnfLtpMap": {str(k): v for k, v in bnf_cache.items()}}
        spots = {"nfSpot": 24900, "bnfSpot": 51900}

        results = {}
        for index, cls, ctx in (("NF", 65, ctx_nf), ("BNF", 30, ctx_bnf)):
            for n in (1, 2, 4):
                live = compute_position_live(
                    trade(index, cls, n), {}, {}, spots, 14, ctx, {}
                )
                self.assertIsNotNone(live, f"{index} {n} lots should resolve")
                results[(index, n)] = live["current_pnl"]
            self.assertAlmostEqual(results[(index, 2)], 2 * results[(index, 1)], places=4)
            self.assertAlmostEqual(results[(index, 4)], 4 * results[(index, 1)], places=4)

    def test_triplet_mismatch_fail_closed(self):
        from brain import compute_position_live
        trade = {
            "index_key": "NF",
            "strategy_type": "BEAR_CALL",
            "is_credit": True,
            "entry_premium": 20.0,
            "sell_strike": 25000,
            "buy_strike": 25100,
            "contract_lot_size": 65,
            "number_of_lots": 2,
            "quantity_units": 100,  # mismatch
            "entry_snapshot": {
                "contract_lot_size": 65,
                "number_of_lots": 2,
                "quantity_units": 100,
            },
        }
        live = compute_position_live(
            trade, {}, {}, {"nfSpot": 24900}, 14,
            {"nfLtpMap": {"25000": {"CE": 30}, "25100": {"CE": 10}}}, {}
        )
        self.assertIsNone(live)


class R32SemanticValidator(unittest.TestCase):
    def test_counterexample_ok_false(self):
        payload = {
            "schema_version": CONTRACT_IDENTITY_SCHEMA_VERSION,
            "identity_status": "verified",
            "index_key": "XYZ",
            "expiry": "not-a-date",
            "contract_lot_size": -65,
            "number_of_lots": 2,
            "quantity_units": 1,
            "calendar_dte": -3,
            "trading_dte": 1.5,
            "quantity_basis": "hypothetical_lots",
            "identity_complete": True,
        }
        result = validate_contract_identity(payload)
        self.assertFalse(result["ok"])
        self.assertFalse(result["eligible_for_contract_metrics"])
        self.assertTrue(result["errors"])

    def test_verified_missing_triplet_ineligible(self):
        payload = {
            "schema_version": CONTRACT_IDENTITY_SCHEMA_VERSION,
            "identity_status": "verified",
            "index_key": "NF",
            "expiry": "2026-07-21",
            "contract_lot_size": 65,
            "quantity_basis": "hypothetical_lots",
            "identity_complete": True,
            "lot_source": "captured_metadata",
            "lot_table_version": "v2",
        }
        result = validate_contract_identity(payload)
        self.assertFalse(result["ok"])
        self.assertFalse(result["eligible_for_contract_metrics"])

    def test_table_driven_invalids(self):
        cases = [
            {"index_key": "XYZ"},
            {"expiry": "not-a-date"},
            {"contract_lot_size": -1},
            {"number_of_lots": 0},
            {"quantity_units": 1.5},
            {"calendar_dte": -1},
            {"trading_dte": 1.5},
            {"identity_status": "bogus"},
        ]
        base = {
            "schema_version": CONTRACT_IDENTITY_SCHEMA_VERSION,
            "identity_status": "incomplete",
            "index_key": "NF",
            "expiry": "2026-07-21",
            "contract_lot_size": 65,
            "number_of_lots": 1,
            "quantity_units": 65,
            "quantity_basis": "hypothetical_lots",
        }
        for over in cases:
            with self.subTest(over=over):
                got = validate_contract_identity({**base, **over})
                self.assertFalse(got["ok"], msg=over)


class R33DteConsistency(unittest.TestCase):
    def test_18_99_fail_closed(self):
        got = resolve_contract_identity({
            "index": "NF",
            "session_date": "2026-07-19",
            "expiry": "2026-08-06",
            "expiry_cycle": "weekly",
            "contract_lot_size": 65,
            "number_of_lots": 1,
            "quantity_units": 65,
            "calendar_dte": 18,
            "trading_dte": 99,
            "dte": 18,
        })
        self.assertFalse(got["identity_complete"])
        self.assertTrue(got["contract_identity_quarantine"])
        self.assertIn("trading_dte_mismatch", got.get("dte_consistency_errors") or {})

    def test_matching_explicit_ok(self):
        got = resolve_contract_identity({
            "index": "NF",
            "session_date": "2026-07-19",
            "expiry": "2026-07-21",
            "expiry_cycle": "weekly",
            "contract_lot_size": 65,
            "number_of_lots": 1,
            "quantity_units": 65,
            "calendar_dte": 2,
            "trading_dte": 2,
            "dte": 2,
            "dte_basis": "nse_trading_calendar",
        })
        self.assertTrue(got["identity_complete"])


class R35ChecksumAndDefaults(unittest.TestCase):
    def test_string_checksum_mismatch_reason(self):
        from ml_train import run
        from ml_train import EXPORT_MANIFEST_SCHEMA_VERSION
        with tempfile.TemporaryDirectory() as td:
            trades = os.path.join(td, "paper_trades.json")
            with open(trades, "w", encoding="utf-8") as handle:
                handle.write("[]")
            bad = "0" * 64
            status = {
                "schema_version": EXPORT_MANIFEST_SCHEMA_VERSION,
                "status": "complete",
                "kind": "paper_trades_export",
                "dataset_label": "paper_research_not_live",
                "generation_id": "paper_test",
                "export_cutoff": "2026-09-13T00:00:00Z",
                "live_training_eligible": False,
                "file": "paper_trades.json",
                "checksum_sha256": bad,
                "row_count": 0,
            }
            with open(os.path.join(td, "manifest.json"), "w", encoding="utf-8") as handle:
                json.dump(status, handle)
            result = json.loads(run("missing.csv", trades, os.path.join(td, "model.json")))
            self.assertFalse(result.get("deployed"))
            self.assertIn("checksum_mismatch", result.get("reason", ""))

    def test_no_nf_dte_fabrication(self):
        from ml_train import _app_trade_to_row
        row = _app_trade_to_row({
            "actual_pnl": 100,
            "strategy": "BEAR_CALL",
            "max_profit": 100,
            "max_loss": 200,
            # missing index and dte
        })
        self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
