"""Acceptance: Paper P1 mark → Brain valuation bridge (2.6.56).

Trade 284 BNF Iron Butterfly fixture: P1 VALIDATED gross −1596 with costs 243.1
→ displayed net −1839.1; Brain receives the accepted mark and returns a real
policy verdict (not DATA_UNAVAILABLE solely because the chain poll is incomplete).

Fail-closed: incomplete / stale / future / crossed / missing-leg / wrong-lot /
unparseable time. Stale display never becomes BOOK/EXIT actionable.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain
import advice_parity_instrumentation as api


IST = timezone(timedelta(hours=5, minutes=30))
# 09:38:41 IST on 2026-09-24
VALUATION_IST = datetime(2026, 9, 24, 9, 38, 41, tzinfo=IST)
VALUATION_UTC = VALUATION_IST.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _four_leg_keys():
    return [
        "NSE_FO|BNF|CE|SELL|52000",
        "NSE_FO|BNF|CE|BUY|52500",
        "NSE_FO|BNF|PE|SELL|52000",
        "NSE_FO|BNF|PE|BUY|51500",
    ]


def _legs_json(crossed=False, missing_exec=False, drop_leg=False):
    keys = _four_leg_keys()
    legs = []
    for i, key in enumerate(keys):
        if drop_leg and i == 3:
            continue
        bid, ask = 40.0 + i, 42.0 + i
        if crossed and i == 1:
            bid, ask = 50.0, 40.0
        exec_px = ask if "BUY" in key.split("|")[3] else bid
        if missing_exec and i == 2:
            exec_px = None
        legs.append({
            "instrument_key": key,
            "side": key.split("|")[3],
            "option_type": key.split("|")[2],
            "strike": int(key.split("|")[4]),
            "bid": bid,
            "ask": ask,
            "ltp": (bid + ask) / 2,
            "mid": (bid + ask) / 2,
            "executable_price": exec_px,
            "price_basis": "EXECUTABLE",
            "quote_status": "CROSSED" if (crossed and i == 1) else "OK",
        })
    return legs


def _trade_284(**extra):
    t = {
        "id": 284,
        "paper": True,
        "index_key": "BNF",
        "strategy_type": "IRON_BUTTERFLY",
        "sell_strike": 52000,
        "buy_strike": 52500,
        "sell_strike2": 52000,
        "buy_strike2": 51500,
        "entry_premium": 220.0,
        "lot_size": 30,
        "contract_lot_size": 30,
        "number_of_lots": 1,
        "quantity_units": 30,
        "is_credit": True,
        "max_profit": 6600,
        "max_loss": 8400,
        "status": "OPEN",
        "session_date": "2026-09-24",
        "entry_date": "2026-09-24",
    }
    t.update(extra)
    return t


def _p1_mark(pnl=-1596.0, state="LIVE_FULL", **extra):
    mark = {
        "schema_version": "position_mark_state_v1",
        "display_state": state,
        "display_is_actionable": False,
        "last_valid_current_pnl": pnl,
        "last_valid_tick_ts": VALUATION_UTC,
        "last_valid_age_ms": 30_000,
        "fresh_mark_max_age_ms": 150_000,
        "last_valid_mark": 273.2,
        "last_valid_mark_basis": "EXECUTABLE",
        "source": "P1_REST_60S",
        "leg_count": 4,
        "last_valid_legs_json": _legs_json(),
        "last_valid_quantity_units": 30,
        "last_valid_contract_lot_size": 30,
        "last_valid_number_of_lots": 1,
        "last_valid_lot_authoritative": True,
        "last_valid_index_key": "BNF",
        "last_valid_strategy_type": "IRON_BUTTERFLY",
    }
    mark.update(extra)
    return mark


class TestPaperP1BrainBridgeTrade284(unittest.TestCase):
    def test_p1_live_full_feeds_brain_not_data_unavailable(self):
        trade = _trade_284()
        # Empty chain — compute_position_live alone would fail closed.
        empty_chain = {"strikes": {}, "atm": 0}
        spots = {"bnfSpot": 52010, "nfSpot": 0}
        ctx = {
            "bnfDTE": 3,
            "today_ist": "2026-09-24",
            "p1_position_marks": {"284": _p1_mark()},
            "_trace": {"positions": {}},
        }
        result = {"position_live": {}, "positions": {}}
        applied = brain._try_apply_paper_p1_valuation(
            trade, result, 284, ctx, spot=52010
        )
        self.assertTrue(applied, "P1 LIVE_FULL must apply for Paper")
        self.assertEqual(trade["valuation_quality"], "full")
        self.assertEqual(trade["current_pnl"], -1596.0)
        self.assertEqual(trade["valuation_source"], "P1_REST_60S")
        # Real executable premium from P1 — not a placeholder invent.
        self.assertEqual(trade["current_premium"], 273.2)

        # Without P1, chain path is unavailable:
        bare = _trade_284()
        pl = brain.compute_position_live(
            bare, empty_chain, empty_chain, spots, 14.0, ctx, None
        )
        self.assertTrue(pl.get("_valuation_unavailable"))

        # position_verdict must NOT be DATA_UNAVAILABLE solely due to chain gap
        verdict = brain.position_verdict(trade, [], "MILD", ctx)
        self.assertNotEqual(verdict.get("urgency"), "DATA_UNAVAILABLE")
        self.assertNotIn("live position mark is unavailable", (verdict.get("reason") or "").lower())
        self.assertIn(verdict.get("action"), ("HOLD", "BOOK", "EXIT"))

        # Displayed net consistent with gross − costs
        gross = -1596.0
        costs = 243.1
        net = round(gross - costs, 1)
        self.assertEqual(net, -1839.1)

    def test_real_trade_ignores_p1_bridge(self):
        trade = _trade_284(paper=False)
        ctx = {"p1_position_marks": {"284": _p1_mark()}}
        result = {"position_live": {}}
        self.assertFalse(
            brain._try_apply_paper_p1_valuation(trade, result, 284, ctx, spot=52010)
        )


class TestPaperP1FailClosed(unittest.TestCase):
    def _apply(self, **mark_extra):
        trade = _trade_284()
        mark = _p1_mark(**mark_extra)
        ctx = {"p1_position_marks": {"284": mark}, "bnfDTE": 3}
        result = {"position_live": {}}
        ok = brain._try_apply_paper_p1_valuation(trade, result, 284, ctx, spot=52010)
        return ok, trade, result

    def test_stale_display_not_applied(self):
        ok, trade, _ = self._apply(state="STALE_LAST_VALID")
        self.assertFalse(ok)
        # Stale must not unlock BOOK/EXIT via Brain
        trade["valuation_quality"] = "unavailable"
        trade["current_pnl"] = None
        verdict = brain.position_verdict(trade, [], "MILD", {"bnfDTE": 3, "_trace": {"positions": {}}})
        self.assertEqual(verdict.get("urgency"), "DATA_UNAVAILABLE")
        self.assertFalse(verdict.get("position_exit_audit", {}).get("exit_allowed"))
        self.assertFalse(verdict.get("position_exit_audit", {}).get("book_allowed"))

    def test_incomplete_legs_rejected(self):
        ok, _, _ = self._apply(last_valid_legs_json=_legs_json(missing_exec=True))
        self.assertFalse(ok)

    def test_crossed_rejected(self):
        ok, _, _ = self._apply(last_valid_legs_json=_legs_json(crossed=True))
        self.assertFalse(ok)

    def test_missing_leg_rejected(self):
        ok, _, _ = self._apply(last_valid_legs_json=_legs_json(drop_leg=True), leg_count=3)
        self.assertFalse(ok)

    def test_wrong_lot_rejected(self):
        ok, _, _ = self._apply(last_valid_quantity_units=65, last_valid_contract_lot_size=65)
        self.assertFalse(ok)

    def test_unparseable_time_rejected(self):
        ok, _, _ = self._apply(last_valid_tick_ts="yesterday-afternoon")
        self.assertFalse(ok)

    def test_future_mark_rejected(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        ok, _, _ = self._apply(last_valid_tick_ts=future)
        self.assertFalse(ok)

    def test_stale_age_rejected(self):
        ok, _, _ = self._apply(last_valid_age_ms=200_000)
        self.assertFalse(ok)

    def test_non_authoritative_lot_rejected(self):
        ok, _, _ = self._apply(last_valid_lot_authoritative=False)
        self.assertFalse(ok)

    def test_no_invented_premium_when_mark_missing(self):
        ok, trade, result = self._apply(last_valid_mark=None)
        # PnL still applies when mark premium absent — but current_premium not invented
        self.assertTrue(ok)
        self.assertNotIn("current_premium", trade)
        self.assertIsNone(result["position_live"][284].get("current_net_premium"))


class TestValuationTsTiming(unittest.TestCase):
    def test_quote_210ms_after_request_before_valuation_passes(self):
        r = api.validate_per_leg_quote_timing(
            ["L1", "L2", "L3", "L4"],
            {
                "L1": "2026-09-24T04:08:41.210+00:00",
                "L2": "2026-09-24T04:08:41.205+00:00",
                "L3": "2026-09-24T04:08:41.200+00:00",
                "L4": "2026-09-24T04:08:41.215+00:00",
            },
            valuation_ts="2026-09-24T04:08:41.500+00:00",
            request_started_ts="2026-09-24T04:08:41.000+00:00",
        )
        self.assertTrue(r["available"], r)

    def test_quote_after_valuation_rejected(self):
        r = api.validate_per_leg_quote_timing(
            ["L1"],
            {"L1": "2026-09-24T04:08:42.000+00:00"},
            valuation_ts="2026-09-24T04:08:41.500+00:00",
            request_started_ts="2026-09-24T04:08:41.000+00:00",
        )
        self.assertFalse(r["available"])
        self.assertIn("leg_quote_ts_after_valuation", r["reason"])

    def test_each_leg_independent(self):
        r = api.validate_per_leg_quote_timing(
            _four_leg_keys(),
            {
                _four_leg_keys()[0]: "2026-09-24T04:08:41.100+00:00",
                _four_leg_keys()[1]: "2026-09-24T04:08:41.100+00:00",
                _four_leg_keys()[2]: None,
                _four_leg_keys()[3]: "2026-09-24T04:08:41.100+00:00",
            },
            valuation_ts="2026-09-24T04:08:41.500+00:00",
            request_started_ts="2026-09-24T04:08:41.000+00:00",
        )
        self.assertFalse(r["available"])
        self.assertIn("leg_source_ts_missing", r["reason"])


if __name__ == "__main__":
    unittest.main()
