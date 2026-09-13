"""Codex rectification acceptance: B3 conflict-bypass + B8 lot counts."""
from __future__ import annotations

import math
import unittest

from canonical_net_profitability import resolve_contract_identity
from contract_lot_table import parse_number_of_lots, parse_positive_integral_lot
from brain import _candidate_contract_fields


class CodexRectificationIdentityTests(unittest.TestCase):
    def test_nf_65_vs_75_conflict(self):
        got = resolve_contract_identity({
            "index": "NF",
            "session_date": "2025-06-10",
            "expiry": "2025-06-12",
            "expiry_cycle": "weekly",
            "lot_size": 65,
            "number_of_lots": 1,
            "dte": 2,
        })
        self.assertTrue(got["lot_conflict"])
        self.assertFalse(got["identity_complete"])
        self.assertTrue(got["contract_identity_quarantine"])
        self.assertEqual(got["captured_contract_lot"], 65)
        self.assertEqual(got["rule_contract_lot"], 75)

    def test_nf_75_vs_75_ok(self):
        got = resolve_contract_identity({
            "index": "NF",
            "session_date": "2025-06-10",
            "expiry": "2025-06-12",
            "expiry_cycle": "weekly",
            "lot_size": 75,
            "number_of_lots": 1,
            "dte": 2,
        })
        self.assertFalse(got["lot_conflict"])
        self.assertTrue(got["identity_complete"])
        self.assertEqual(got["contract_lot_size"], 75)

    def test_bnf_30_vs_35_conflict(self):
        got = resolve_contract_identity({
            "index": "BNF",
            "session_date": "2025-05-02",
            "expiry": "2025-07-31",
            "expiry_cycle": "monthly",
            "lot_size": 30,
            "number_of_lots": 1,
            "dte": 2,
        })
        self.assertTrue(got["lot_conflict"])
        self.assertFalse(got["identity_complete"])
        self.assertTrue(got["contract_identity_quarantine"])
        self.assertEqual(got["captured_contract_lot"], 30)
        self.assertEqual(got["rule_contract_lot"], 35)

    def test_bnf_35_vs_35_ok(self):
        got = resolve_contract_identity({
            "index": "BNF",
            "session_date": "2025-05-02",
            "expiry": "2025-07-31",
            "expiry_cycle": "monthly",
            "lot_size": 35,
            "number_of_lots": 1,
            "dte": 2,
        })
        self.assertFalse(got["lot_conflict"])
        self.assertTrue(got["identity_complete"])
        self.assertEqual(got["contract_lot_size"], 35)

    def test_two_lot_quantity(self):
        got = resolve_contract_identity({
            "index": "NF",
            "session_date": "2026-07-19",
            "expiry": "2026-08-06",
            "expiry_cycle": "weekly",
            "contract_lot_size": 65,
            "number_of_lots": 2,
            "quantity_units": 130,
            "calendar_dte": 18,
            "trading_dte": 14,
            "dte": 18,
            "dte_basis": "calendar",
        })
        self.assertTrue(got["identity_complete"])
        self.assertEqual(got["quantity_units"], 130)
        self.assertEqual(got["contract_lot_size"] * got["number_of_lots"], 130)

    def test_invalid_number_of_lots_fail_closed(self):
        for v in (0, -1, 1.5, "abc", float("nan"), float("inf")):
            got = resolve_contract_identity({
                "index": "NF",
                "session_date": "2026-07-19",
                "expiry": "2026-08-06",
                "expiry_cycle": "weekly",
                "number_of_lots": v,
                "tDTE": 2,
                "dte": 2,
            })
            self.assertFalse(got["identity_complete"], msg=repr(v))
            self.assertTrue(got["contract_identity_quarantine"], msg=repr(v))

    def test_missing_number_of_lots_defaults_with_stamp(self):
        pack = parse_number_of_lots(None)
        self.assertTrue(pack["valid"])
        self.assertEqual(pack["number_of_lots"], 1)
        self.assertTrue(pack["number_of_lots_assumed"])
        self.assertIn("one_lot_path", pack["number_of_lots_default_policy"])

    def test_fractional_contract_lot_rejected(self):
        v, err = parse_positive_integral_lot(65.7)
        self.assertIsNone(v)
        self.assertEqual(err, "fractional_lot")

    def test_brain_candidate_fields_conflict(self):
        got = _candidate_contract_fields(
            {
                "index": "NF",
                "expiry": "2025-06-12",
                "expiry_cycle": "weekly",
                "lotSize": 65,
                "number_of_lots": 1,
                "tDTE": 2,
                "dte": 2,
            },
            {"session_date": "2025-06-10"},
        )
        self.assertTrue(got["lot_conflict"])
        self.assertFalse(got["identity_complete"])

    def test_flat_nested_conflict(self):
        got = resolve_contract_identity({
            "index": "NF",
            "session_date": "2026-07-19",
            "expiry": "2026-08-06",
            "expiry_cycle": "weekly",
            "contract_lot_size": 65,
            "number_of_lots": 1,
            "tDTE": 2,
            "dte": 2,
            "contract_identity": {
                "index_key": "NF",
                "expiry": "2026-08-06",
                "contract_lot_size": 75,
                "number_of_lots": 1,
            },
        })
        self.assertTrue(got["lot_conflict"] or got.get("flat_nested_conflict"))
        self.assertFalse(got["identity_complete"])


class CodexRectificationTrainingIsolation(unittest.TestCase):
    def test_snapshot_missing_index_unknown(self):
        from ml_train import _snapshot_candidate_to_row
        row = _snapshot_candidate_to_row(
            {
                "type": "BEAR_CALL",
                "learning_won_net": 1,
                "managed_pnl": 100,
                "width": 100,
                "isCredit": True,
            },
            {"snapshot_latest_poll": {"vix": 14, "nfSpot": 25000}},
            {"sim_pnl_h2": 100, "learning_won_net": 1},
            {"session_date": "2026-09-13"},
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["index"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
