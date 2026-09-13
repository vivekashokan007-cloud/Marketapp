"""Codex R2 acceptance: invalid lot/qty/DTE fail-closed; field_absent vs invalid; multi-lot."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from canonical_net_profitability import (
    parse_non_negative_integral_dte,
    resolve_contract_identity,
)


class R2InvalidLotQuantityTests(unittest.TestCase):
    BASE = {
        "index": "NF",
        "session_date": "2026-07-19",
        "expiry": "2026-08-06",
        "expiry_cycle": "weekly",
        "number_of_lots": 1,
        "tDTE": 2,
        "dte": 2,
    }

    def _got(self, **over):
        return resolve_contract_identity({**self.BASE, **over})

    def test_invalid_values_fail_closed(self):
        cases = [
            {"lot_size": 65.5},
            {"lot_size": 0},
            {"lot_size": "abc"},
            {"quantity_units": 65.5},
            {"quantity_units": -5},
        ]
        for c in cases:
            with self.subTest(c=c):
                got = self._got(**c)
                self.assertFalse(got["identity_complete"])
                self.assertTrue(got["contract_identity_quarantine"])
                self.assertTrue(got["explicit_lot_invalid"])
                # Never substituted by authority
                self.assertIsNone(got["contract_lot_size"])

    def test_missing_vs_invalid_diagnostics_differ(self):
        missing = self._got(contract_lot_size=65)  # qty absent → derived ok
        invalid = self._got(lot_size=65.5)
        self.assertEqual(missing["field_diagnostics"]["quantity_units"]["status"], "field_absent")
        self.assertEqual(invalid["field_diagnostics"]["lot_size"]["status"], "field_present_but_invalid")
        self.assertNotEqual(
            missing["field_diagnostics"]["quantity_units"]["status"],
            invalid["field_diagnostics"]["lot_size"]["status"],
        )
        self.assertTrue(missing["identity_complete"])
        self.assertFalse(invalid["identity_complete"])
        self.assertEqual(invalid["retained_invalid_observations"]["lot_size"], 65.5)

    def test_valid_nf_bnf_one_lot(self):
        nf = self._got(contract_lot_size=65, quantity_units=65)
        self.assertTrue(nf["identity_complete"])
        self.assertEqual(nf["contract_lot_size"], 65)
        bnf = resolve_contract_identity({
            "index": "BNF",
            "session_date": "2026-07-19",
            "expiry": "2026-07-30",
            "expiry_cycle": "weekly",
            "contract_lot_size": 30,
            "number_of_lots": 1,
            "quantity_units": 30,
            "tDTE": 2,
            "dte": 2,
        })
        self.assertTrue(bnf["identity_complete"])
        self.assertEqual(bnf["contract_lot_size"], 30)

    def test_two_and_four_lot_consistency(self):
        two = self._got(contract_lot_size=65, number_of_lots=2, quantity_units=130)
        self.assertTrue(two["identity_complete"])
        self.assertEqual(two["contract_lot_size"] * two["number_of_lots"], two["quantity_units"])
        four = self._got(contract_lot_size=65, number_of_lots=4, quantity_units=260)
        self.assertTrue(four["identity_complete"])
        self.assertEqual(four["quantity_units"], 260)

    def test_legacy_lotsize_two_lot_policy(self):
        # Legacy per-contract lotSize=65 with number_of_lots=2 → quantity 130, not 32.5
        got = self._got(lot_size=65, number_of_lots=2)
        self.assertTrue(got["identity_complete"])
        self.assertEqual(got["contract_lot_size"], 65)
        self.assertEqual(got["number_of_lots"], 2)
        self.assertEqual(got["quantity_units"], 130)
        self.assertEqual(got["lot_size_interpretation"], "lot_size_interpretation_v1_20260913")


class R2DteTests(unittest.TestCase):
    BASE = {
        "index": "NF",
        "session_date": "2026-07-19",
        "expiry": "2026-08-06",
        "expiry_cycle": "weekly",
        "contract_lot_size": 65,
        "number_of_lots": 1,
        "quantity_units": 65,
    }

    def test_negative_and_fractional_quarantined(self):
        for over in ({"tDTE": -2, "dte": -2}, {"tDTE": 1.5, "dte": 1.5}, {"dte": "abc"}, {"tDTE": float("inf")}):
            with self.subTest(over=over):
                got = resolve_contract_identity({**self.BASE, **over})
                self.assertFalse(got["identity_complete"])
                self.assertTrue(got["contract_identity_quarantine"])
                self.assertTrue(got["explicit_dte_invalid"])

    def test_parse_helper(self):
        self.assertEqual(parse_non_negative_integral_dte(None)["status"], "field_absent")
        self.assertEqual(parse_non_negative_integral_dte(-1)["error"], "negative_dte")
        self.assertEqual(parse_non_negative_integral_dte(1.5)["error"], "fractional_dte")
        self.assertEqual(parse_non_negative_integral_dte(2)["value"], 2)

    def test_expiry_day_convention(self):
        from contract_lot_table import trading_dte as td
        pack = td("2026-09-10", "2026-09-10")
        self.assertEqual(pack["calendar_dte"], 0)
        self.assertEqual(pack["trading_dte"], 1)


class R2ManifestFailClosed(unittest.TestCase):
    def test_malformed_manifest_fail_closed(self):
        from ml_train import run
        with tempfile.TemporaryDirectory() as td:
            trades = os.path.join(td, "paper_trades.json")
            open(trades, "w").write("[]")
            open(os.path.join(td, "paper_trades_export_status.json"), "w").write("{not-json")
            result = json.loads(run("missing.csv", trades, os.path.join(td, "model.json")))
            self.assertFalse(result.get("deployed"))
            self.assertIn("malformed", result.get("reason", ""))

    def test_missing_manifest_beside_paper_fail_closed(self):
        from ml_train import run
        with tempfile.TemporaryDirectory() as td:
            trades = os.path.join(td, "paper_trades.json")
            open(trades, "w").write("[]")
            # no status file
            result = json.loads(run("missing.csv", trades, os.path.join(td, "model.json")))
            self.assertFalse(result.get("deployed"))
            self.assertIn("missing", result.get("reason", ""))


if __name__ == "__main__":
    unittest.main()
