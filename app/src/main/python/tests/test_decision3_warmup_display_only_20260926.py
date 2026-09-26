"""Owner decision 3 (26 Sep 2026): warm-up Brain POSITION alerts are display-only
until the trade has a TRUSTED mark; thresholds publish from poll 1, labelled warm-up.

Real-behaviour note: first-poll monitoring is Paper-only (decision 4), so no
Real payload or Real notification changes. Drives analyze() + NotificationAgent.
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brain  # noqa: E402
from test_b3_item2_first_poll_monitoring_20260926 import POLL1, _ctx, _real, _run  # noqa: E402
from test_paper_brain_p1_bridge_20260924 import _p1_mark, _trade_284  # noqa: E402

TRUSTED = dict(mark_trust_state="TRUSTED", quote_validity_state="VALID")


def _keys(payload):
    return [c.get("alert_key") for c in payload.get("brain_notifications") or []]


class WarmupDisplayOnlyTests(unittest.TestCase):
    def _book(self, **mark):
        trade = _trade_284(force_alignment=1)
        return _run(POLL1, [trade], _ctx({"284": _p1_mark(pnl=1200.0, **mark)}))

    def test_trusted_mark_warmup_alert_notifies(self):
        r = self._book(**TRUSTED)
        alert = next(a for a in r["alerts"] if a["key"] == "POS_BOOK_284")
        self.assertFalse(alert["warmup_display_only"])
        self.assertIn("POS_BOOK_284", _keys(brain.NotificationAgent().process_contract(r, _ctx())))

    def test_untrusted_or_legacy_mark_warmup_alert_is_display_only(self):
        for mark in ({}, {"mark_trust_state": "UNTRUSTED", "mark_trust_cause": "WIDE_EXECUTABLE_BOOK",
                          "quote_validity_state": "VALID"}):
            r = self._book(**mark)
            alerts = [a for a in r["alerts"] if a["key"].endswith("_284")]
            for a in alerts:
                self.assertTrue(a["warmup_display_only"], (mark, a["key"]))
                self.assertEqual(brain.FIRST_POLL_WARMUP_DISPLAY_ONLY_REASON, a["warmup_display_only_reason"])
            agent = brain.NotificationAgent()
            payload = agent.process_contract(r, _ctx())
            self.assertEqual([], _keys(payload), mark)
            # Not acknowledged: it can still notify once a trusted mark exists.
            self.assertEqual({}, {k: v for k, v in agent.position_alert_states.items() if k.startswith("284:")})

    def test_display_only_then_first_trusted_mark_notifies_once(self):
        agent = brain.NotificationAgent()
        self.assertEqual([], _keys(agent.process_contract(self._book(), _ctx())))
        trusted = agent.process_contract(self._book(**TRUSTED), _ctx(now_ms=brain_now(60_000)))
        self.assertIn("POS_BOOK_284", _keys(trusted))

    def test_chain_valuation_without_p1_mark_is_not_a_trusted_mark(self):
        self.assertFalse(brain._first_poll_trade_has_trusted_mark(
            {"position_live": {"284": {"valuation_quality": "full", "current_pnl": 1.0}}}, "284"))
        self.assertTrue(brain._first_poll_trade_has_trusted_mark(
            {"position_live": {"284": {"p1_paper_brain_valuation": True, "p1_mark_trust_state": "TRUSTED"}}}, "284"))

    def test_thresholds_publish_at_poll_one_labelled_warmup(self):
        for mark, trusted in (({}, False), (TRUSTED, True)):
            r = _run(POLL1, [_trade_284()], _ctx({"284": _p1_mark(pnl=-6000.0, **mark)}))
            th = r["position_exit_thresholds"]["284"]
            self.assertEqual("FIRST_POLL_WARMUP", th["publication_mode"])
            self.assertTrue(th["warmup"])
            self.assertIs(trusted, th["warmup_trusted_mark"])
            self.assertIsNotNone(th["stop_pnl_at"])

    def test_real_only_payload_unchanged(self):
        r = _run(POLL1, [_real()], _ctx())
        self.assertNotIn("position_monitoring", r)
        for a in r.get("alerts") or []:
            self.assertNotIn("warmup_display_only", a)


def brain_now(delta):
    from test_b3_item2_first_poll_monitoring_20260926 import NOW_MS
    return NOW_MS + delta


if __name__ == "__main__":
    unittest.main()
