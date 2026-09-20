"""Batch A: compute_position_live must verify every quantity path against the
dated authoritative contract-lot table. Wrong-but-consistent lots fail closed.
Paper capture remains allowed via unavailable valuation stamping by the caller.
"""
from __future__ import annotations

import unittest

from brain import compute_position_live, _stamp_unavailable_position_valuation, is_position_live_available


def _ctx_nf():
    return {"nfLtpMap": {"25000": {"CE": 30, "PE": 0}, "25100": {"CE": 10, "PE": 0}}}


def _ctx_bnf():
    return {"bnfLtpMap": {"52000": {"CE": 40, "PE": 0}, "52200": {"CE": 15, "PE": 0}}}


def _base_nf(**over):
    t = {
        "index_key": "NF",
        "strategy_type": "BEAR_CALL",
        "is_credit": True,
        "entry_premium": 20.0,
        "sell_strike": 25000,
        "buy_strike": 25100,
    }
    t.update(over)
    return t


def _base_bnf(**over):
    t = {
        "index_key": "BNF",
        "strategy_type": "BEAR_CALL",
        "is_credit": True,
        "entry_premium": 30.0,
        "sell_strike": 52000,
        "buy_strike": 52200,
    }
    t.update(over)
    return t


SPOTS = {"nfSpot": 24900, "bnfSpot": 51900}


class BatchAAuthoritativeLotGate(unittest.TestCase):
    def test_correct_nf_lot_resolves(self):
        live = compute_position_live(
            _base_nf(contract_lot_size=65, number_of_lots=1, quantity_units=65),
            {}, {}, SPOTS, 14, _ctx_nf(), {},
        )
        self.assertIsNotNone(live)
        self.assertEqual(live["lot_size_resolved"], 65)

    def test_wrong_but_consistent_nf_75_fail_closed(self):
        live = compute_position_live(
            _base_nf(contract_lot_size=75, number_of_lots=1, quantity_units=75),
            {}, {}, SPOTS, 14, _ctx_nf(), {},
        )
        self.assertFalse(is_position_live_available(live))

    def test_absurd_positive_integer_fail_closed(self):
        live = compute_position_live(
            _base_nf(contract_lot_size=9999, number_of_lots=1, quantity_units=9999),
            {}, {}, SPOTS, 14, _ctx_nf(), {},
        )
        self.assertFalse(is_position_live_available(live))

    def test_complete_triplet_wrong_lot_fail_closed(self):
        live = compute_position_live(
            _base_nf(
                contract_lot_size=75, number_of_lots=2, quantity_units=150,
                entry_snapshot={"contract_lot_size": 75, "number_of_lots": 2, "quantity_units": 150},
            ),
            {}, {}, SPOTS, 14, _ctx_nf(), {},
        )
        self.assertFalse(is_position_live_available(live))

    def test_derived_quantity_wrong_lot_fail_closed(self):
        live = compute_position_live(
            _base_nf(contract_lot_size=75, number_of_lots=2),
            {}, {}, SPOTS, 14, _ctx_nf(), {},
        )
        self.assertFalse(is_position_live_available(live))

    def test_legacy_explicit_wrong_lot_fail_closed(self):
        live = compute_position_live(
            _base_nf(lot_size=75, lots=1),
            {}, {}, SPOTS, 14, _ctx_nf(), {},
        )
        self.assertFalse(is_position_live_available(live))

    def test_snapshot_only_wrong_lot_fail_closed(self):
        live = compute_position_live(
            _base_nf(entry_snapshot={"lot_size": 75}),
            {}, {}, SPOTS, 14, _ctx_nf(), {},
        )
        self.assertFalse(is_position_live_available(live))

    def test_broken_triplet_zero_negative_fractional_nonnumeric(self):
        cases = [
            {"contract_lot_size": 65, "number_of_lots": 2, "quantity_units": 100},
            {"contract_lot_size": 0, "number_of_lots": 1, "quantity_units": 0},
            {"contract_lot_size": -65, "number_of_lots": 1, "quantity_units": -65},
            {"contract_lot_size": 65.5, "number_of_lots": 1, "quantity_units": 65.5},
            {"contract_lot_size": "abc", "number_of_lots": 1, "quantity_units": "abc"},
        ]
        for over in cases:
            with self.subTest(over=over):
                live = compute_position_live(_base_nf(**over), {}, {}, SPOTS, 14, _ctx_nf(), {})
                self.assertFalse(is_position_live_available(live))
                self.assertEqual(live.get("failure_reason"), "invalid_quantity_identity")

    def test_bnf_chain_correct_and_wrong(self):
        ok = compute_position_live(
            _base_bnf(
                contract_lot_size=30, number_of_lots=1, quantity_units=30,
                session_date="2026-07-19", expiry="2026-07-30", expiry_cycle="monthly",
            ),
            {}, {}, SPOTS, 14, _ctx_bnf(), {},
        )
        self.assertIsNotNone(ok)
        self.assertEqual(ok["lot_size_resolved"], 30)

        bad = compute_position_live(
            _base_bnf(
                contract_lot_size=35, number_of_lots=1, quantity_units=35,
                session_date="2026-07-19", expiry="2026-07-30", expiry_cycle="monthly",
            ),
            {}, {}, SPOTS, 14, _ctx_bnf(), {},
        )
        self.assertFalse(is_position_live_available(bad))

    def test_multi_lot_quantity_correct(self):
        one = compute_position_live(
            _base_nf(contract_lot_size=65, number_of_lots=1, quantity_units=65),
            {}, {}, SPOTS, 14, _ctx_nf(), {},
        )
        two = compute_position_live(
            _base_nf(contract_lot_size=65, number_of_lots=2, quantity_units=130),
            {}, {}, SPOTS, 14, _ctx_nf(), {},
        )
        self.assertIsNotNone(one)
        self.assertIsNotNone(two)
        self.assertEqual(two["lot_size_resolved"], 130)
        self.assertAlmostEqual(two["current_pnl"], 2 * one["current_pnl"], places=4)

    def test_dated_historical_nf_75_allowed_when_authoritative(self):
        live = compute_position_live(
            _base_nf(
                contract_lot_size=75, number_of_lots=1, quantity_units=75,
                session_date="2025-01-15", expiry="2025-01-23", expiry_cycle="weekly",
            ),
            {}, {}, SPOTS, 14, _ctx_nf(), {},
        )
        self.assertIsNotNone(live)
        self.assertEqual(live["lot_size_resolved"], 75)

    def test_unresolved_observation_saveable_but_ineligible_valuation(self):
        trade = _base_nf(contract_lot_size=75, number_of_lots=1, quantity_units=75)
        live = compute_position_live(trade, {}, {}, SPOTS, 14, _ctx_nf(), {})
        self.assertFalse(is_position_live_available(live))
        result = {"position_live": {}}
        stamped = _stamp_unavailable_position_valuation(
            trade, result, "T-unresolved", spot=24900, reason="lot_authority_conflict"
        )
        self.assertEqual(trade["valuation_quality"], "unavailable")
        self.assertIs(result["position_live"]["T-unresolved"], stamped)
        # Trade record itself remains (Paper capture not blocked).
        self.assertEqual(trade["index_key"], "NF")


    def test_wrong_lot_reason_is_lot_not_quotes(self):
        live = compute_position_live(
            _base_nf(contract_lot_size=75, number_of_lots=1, quantity_units=75),
            {}, {}, SPOTS, 14, _ctx_nf(), {},
        )
        self.assertFalse(is_position_live_available(live))
        self.assertIn(live.get('failure_reason'), {
            'contract_lot_conflict', 'contract_lot_unresolved', 'invalid_quantity_identity'
        })
        self.assertNotEqual(live.get('failure_reason'), 'missing_required_chain_quotes')

    def test_missing_quotes_reason_distinct(self):
        # Correct lot but empty chain/spot context → quote failure reason.
        live = compute_position_live(
            _base_nf(contract_lot_size=65, number_of_lots=1, quantity_units=65),
            {}, {}, {"nfSpot": 0, "bnfSpot": 0}, 14, {}, {},
        )
        self.assertFalse(is_position_live_available(live))
        self.assertEqual(live.get('failure_reason'), 'missing_required_chain_quotes')

    def test_resolver_only_without_dated_or_captured_identity_fails_closed(self):
        live = compute_position_live(
            _base_nf(), {}, {}, SPOTS, 14, _ctx_nf(), {},
        )
        self.assertFalse(is_position_live_available(live))
        self.assertEqual(live.get('failure_reason'), 'contract_lot_identity_missing')
        self.assertEqual(live.get('valuation_quality'), 'unavailable')


if __name__ == "__main__":
    unittest.main()
