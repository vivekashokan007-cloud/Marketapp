"""D4 — position-alert notification ownership.

Both engines emitted the same position alerts with identical titles from
different thresholds:

    brain.py POS_TARGET_*       "💰 Target Near"              TARGET_NEAR_RATIO 0.8 + percentile
    tick     SHADOW_TP          "💰 Target Near"              TP_MULT 0.50
    brain.py POS_STOP_*         "🛑 Stop Loss Near"           STOP_LOSS_RATIO 0.7 + percentile
    tick     SHADOW_SL          "🛑 Stop Loss Near"           SL_MULT 0.60
    brain.py POS_DATA_QUALITY_* "🧪 Position Data Incomplete" valuation_quality != full
    tick     SHADOW_DEGRADED    "🧪 Position Data Incomplete" valuation_quality != OK

so one target crossing produced two notifications at different P&L levels,
minutes to hours apart. The 60-second tick service is now authoritative for
those three; brain.py still *generates* them (the UI and the evidence trail
depend on `result['alerts']`) but no longer notifies.

POS_BOOK has no tick-side counterpart — "forces weak while profitable" needs
force alignment, which the tick service cannot see — so it stays brain-owned.
SHADOW_EOD is likewise tick-only.

These are behavioural tests: they run `evaluate_alerts` and the real
`NotificationAgent` over synthetic trades and assert on the emitted contracts.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import brain  # noqa: E402
from brain import NotificationAgent, evaluate_alerts  # noqa: E402

# Resolved defensively so a tree without the ownership split still collects and
# fails with named assertions rather than an import error that hides which
# contract is missing.
POSITION_ALERT_OWNERSHIP_VERSION = getattr(brain, "POSITION_ALERT_OWNERSHIP_VERSION", "")
POSITION_ALERT_PREFIXES_OWNED_BY_TICK_SERVICE = getattr(
    brain, "POSITION_ALERT_PREFIXES_OWNED_BY_TICK_SERVICE", ()
)


def _ctx():
    return {
        "mins_since_open": 90,
        "now_ms": 1_000_000,
        "significant_move": False,
        "entry_window_active": False,
    }


def _target_near_trade(trade_id="t_target"):
    # current_pnl / max_profit = 0.9, above TARGET_NEAR_RATIO (0.8)
    return {
        "id": trade_id,
        "index_key": "NF",
        "strategy_type": "IRON_BUTTERFLY",
        "sell_strike": 23400,
        "current_pnl": 900,
        "max_profit": 1000,
        "max_loss": 1500,
        "valuation_quality": "full",
        "force_alignment": 3,
        "controlIndexMeta": {"signal_completeness_pct": 100},
    }


def _book_profit_trade(trade_id="t_book"):
    # forces weak (1/3) while profitable -> POS_BOOK, which has no tick equivalent
    return {
        "id": trade_id,
        "index_key": "BNF",
        "strategy_type": "BEAR_CALL",
        "sell_strike": 58000,
        "current_pnl": 420,
        "max_profit": 5000,
        "max_loss": 1500,
        "valuation_quality": "full",
        "force_alignment": 1,
        "controlIndexMeta": {"signal_completeness_pct": 100},
    }


def _contracts_for(open_trades):
    alerts = evaluate_alerts(
        open_trades=open_trades, watchlist=[], result={}, ctx=_ctx()
    )
    agent = NotificationAgent()
    payload = agent.process_contract({"alerts": alerts, "verdict": {}}, _ctx())
    return alerts, payload


class PositionAlertOwnershipTests(unittest.TestCase):
    def test_owned_prefixes_cover_the_three_duplicated_alerts_only(self):
        self.assertEqual(
            ("POS_TARGET_", "POS_STOP_", "POS_DATA_QUALITY_"),
            POSITION_ALERT_PREFIXES_OWNED_BY_TICK_SERVICE,
        )
        # POS_BOOK must never be handed to the tick service: it cannot compute it.
        self.assertNotIn("POS_BOOK_", POSITION_ALERT_PREFIXES_OWNED_BY_TICK_SERVICE)

    def test_target_alert_is_still_generated_for_the_ui(self):
        alerts, _ = _contracts_for([_target_near_trade()])
        keys = [a.get("key") for a in alerts]
        self.assertIn(
            "POS_TARGET_t_target",
            keys,
            "the alert must survive for the UI and the evidence trail — only the "
            "notification is handed to the tick service",
        )

    def test_target_alert_does_not_notify(self):
        _, payload = _contracts_for([_target_near_trade()])
        delivered = payload.get("brain_notifications") or []
        self.assertEqual(
            [],
            [c for c in delivered if c.get("alert_key") == "POS_TARGET_t_target"],
            "POS_TARGET must not be in the delivery list — the tick service owns it",
        )
        contract = payload.get("brain_notification") or {}
        if contract.get("alert_key") == "POS_TARGET_t_target":
            self.assertFalse(contract.get("notify_user"))
            self.assertEqual(
                "POSITION_ALERT_OWNED_BY_TICK_SERVICE", contract.get("reason_code")
            )
            self.assertEqual("position_tick_service", contract.get("position_alert_owner"))

    def test_book_profit_still_notifies_from_the_brain(self):
        _, payload = _contracts_for([_book_profit_trade()])
        delivered = payload.get("brain_notifications") or []
        book = [c for c in delivered if c.get("alert_key") == "POS_BOOK_t_book"]
        self.assertEqual(
            1,
            len(book),
            "POS_BOOK has no tick-side counterpart and must keep notifying",
        )
        self.assertTrue(book[0].get("notify_user"))
        self.assertEqual("brain", book[0].get("position_alert_owner"))
        self.assertEqual("⚡ Book Profit", book[0].get("title"))

    def test_a_suppressed_alert_does_not_shadow_a_notifying_one(self):
        # Both trades alert in the same poll: the owned one is suppressed, the
        # brain-owned one must still be delivered rather than being dropped
        # behind it.
        _, payload = _contracts_for([_target_near_trade(), _book_profit_trade()])
        delivered = payload.get("brain_notifications") or []
        delivered_keys = [c.get("alert_key") for c in delivered]
        self.assertIn("POS_BOOK_t_book", delivered_keys)
        self.assertNotIn("POS_TARGET_t_target", delivered_keys)
        self.assertTrue(all(c.get("notify_user") for c in delivered))

    def test_ownership_is_version_stamped(self):
        _, payload = _contracts_for([_book_profit_trade()])
        delivered = payload.get("brain_notifications") or []
        self.assertEqual(
            POSITION_ALERT_OWNERSHIP_VERSION,
            delivered[0].get("position_alert_ownership_version"),
        )
        self.assertIn("tick_service_authoritative", POSITION_ALERT_OWNERSHIP_VERSION)


if __name__ == "__main__":
    unittest.main()
