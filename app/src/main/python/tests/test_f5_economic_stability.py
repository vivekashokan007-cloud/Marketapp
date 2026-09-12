"""F5 — economic candidate stability and choppy mute identity."""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout


PY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)

from brain import (  # noqa: E402
    NotificationAgent,
    _candidates_economically_equivalent,
    _parse_candidate_economic_identity,
    brain_notification_process,
    reset_notification_agent,
)


def _call_contract(result, ctx):
    return json.loads(brain_notification_process(result, ctx))


def _watchlist_candidate(candidate_id, cand_type="IRON_BUTTERFLY", index_key="NF", **extra):
    return {
        "id": candidate_id,
        "type": cand_type,
        "index": index_key,
        "lane": f"{index_key} intraday",
        "executionReady": True,
        "capitalBlocked": False,
        "directionSafe": True,
        "entryEligible": True,
        "entryGate": "ENTRY",
        **extra,
    }


def _entry_result(candidate_id, action="SELL PREMIUM", strategy="IRON_BUTTERFLY", confidence=66):
    return {
        "verdict": {"action": action, "strategy": strategy, "confidence": confidence},
        "watchlist": [_watchlist_candidate(candidate_id, cand_type=strategy)],
        "alerts": [],
    }


def _ctx(now_ms, poll_id):
    return {
        "now_ms": now_ms,
        "entry_window_active": True,
        "session_date": "2026-09-11",
        "poll_id": poll_id,
    }


class ParseEconomicIdentityTests(unittest.TestCase):
    def test_parses_iron_butterfly(self):
        parsed = _parse_candidate_economic_identity("IB_NF_23300_W250")
        self.assertEqual(parsed["structure"], "IB")
        self.assertEqual(parsed["index"], "NF")
        self.assertEqual(parsed["strikes"], (23300,))
        self.assertEqual(parsed["width"], 250)
        self.assertEqual(parsed["step"], 50)

    def test_parses_directional_and_bnf_step(self):
        parsed = _parse_candidate_economic_identity("BULL_PUT_BNF_57600_57200_W400")
        self.assertEqual(parsed["structure"], "BULL_PUT")
        self.assertEqual(parsed["index"], "BNF")
        self.assertEqual(parsed["strikes"], (57600, 57200))
        self.assertEqual(parsed["width"], 400)
        self.assertEqual(parsed["step"], 100)

    def test_unparseable_returns_none(self):
        self.assertIsNone(_parse_candidate_economic_identity("c1"))
        self.assertIsNone(_parse_candidate_economic_identity(None))
        self.assertIsNone(_parse_candidate_economic_identity("IB_XX_23300_W250"))


class EconomicEquivalenceTests(unittest.TestCase):
    def test_strike_plus_minus_one_step_same_structure_is_equivalent(self):
        # NF step=50 → 23300 vs 23350 is ±1 step.
        self.assertTrue(
            _candidates_economically_equivalent("IB_NF_23300_W250", "IB_NF_23350_W250")
        )
        self.assertTrue(
            _candidates_economically_equivalent("IB_NF_23300_W250", "IB_NF_23250_W250")
        )

    def test_exact_id_still_equivalent(self):
        self.assertTrue(
            _candidates_economically_equivalent("IB_NF_23300_W250", "IB_NF_23300_W250")
        )
        self.assertTrue(_candidates_economically_equivalent("c1", "c1"))
        self.assertTrue(_candidates_economically_equivalent(None, None))

    def test_unrelated_structure_not_equivalent(self):
        self.assertFalse(
            _candidates_economically_equivalent("IB_NF_23300_W250", "IC_NF_23300_23200_W250")
        )
        self.assertFalse(
            _candidates_economically_equivalent(
                "BULL_PUT_NF_23400_23300_W100", "BEAR_CALL_NF_23400_23500_W100"
            )
        )

    def test_strike_beyond_one_step_not_equivalent(self):
        # 100 pts = 2 NF steps.
        self.assertFalse(
            _candidates_economically_equivalent("IB_NF_23300_W250", "IB_NF_23400_W250")
        )

    def test_width_mismatch_not_equivalent(self):
        self.assertFalse(
            _candidates_economically_equivalent("IB_NF_23300_W250", "IB_NF_23300_W300")
        )

    def test_unparseable_falls_back_to_exact_match_only(self):
        self.assertFalse(_candidates_economically_equivalent("c1", "c2"))
        self.assertFalse(_candidates_economically_equivalent("c1", "IB_NF_23300_W250"))


class SetupReadyEconomicStabilityTests(unittest.TestCase):
    def setUp(self):
        reset_notification_agent()

    def test_strike_step_same_structure_counts_stable_for_setup_ready(self):
        first = _call_contract(
            _entry_result("IB_NF_23300_W250"),
            _ctx(1000, 1),
        )
        self.assertEqual(first["brain_notification"]["reason_code"], "SETUP_NOT_STABLE")

        second = _call_contract(
            _entry_result("IB_NF_23350_W250"),
            _ctx(2000, 2),
        )
        contract = second["brain_notification"]
        diag = contract["entry_contract_diagnostics"]
        self.assertTrue(diag["eligible"], diag)
        self.assertNotIn("two_poll_stability", diag["failed_conditions"])
        self.assertEqual(contract["reason_code"], "SETUP_READY")
        self.assertTrue(contract["notify_user"])

    def test_unrelated_structure_does_not_count_stable(self):
        _call_contract(_entry_result("IB_NF_23300_W250"), _ctx(1000, 1))
        second = _call_contract(
            _entry_result("IC_NF_23400_23200_W250", strategy="IRON_CONDOR"),
            _ctx(2000, 2),
        )
        contract = second["brain_notification"]
        diag = contract["entry_contract_diagnostics"]
        self.assertIn("two_poll_stability", diag["failed_conditions"])
        # Strategy also mismatches verdict vs candidate type path may fail earlier,
        # but stability itself must not pass.
        observed = next(c for c in diag["conditions"] if c["name"] == "two_poll_stability")
        self.assertFalse(observed["passed"])
        self.assertFalse(observed["observed"]["stable_candidate"])

    def test_exact_id_regression_still_stable(self):
        result = _entry_result("IB_NF_23300_W250")
        _call_contract(result, _ctx(1000, 1))
        second = _call_contract(result, _ctx(2000, 2))
        self.assertEqual(second["brain_notification"]["reason_code"], "SETUP_READY")
        self.assertTrue(second["brain_notification"]["entry_contract_diagnostics"]["eligible"])


class ChoppyEconomicFlipTests(unittest.TestCase):
    def setUp(self):
        reset_notification_agent()

    def test_choppy_ignores_strike_step_churn_across_wait_dips(self):
        """SELL → WAIT → SELL(±1 strike) → WAIT → SELL(±1) must not trip mute.

        Pre-F5 action-only flip counting treated every re-entry into non-WAIT as
        a flip, so three WAIT dips around the same economic IB tripped mute.
        """
        agent = NotificationAgent()
        agent.verdict_history = [
            "SELL PREMIUM",
            "WAIT",
            "SELL PREMIUM",
            "WAIT",
            "SELL PREMIUM",
            "WAIT",
        ]
        agent.best_candidate_history = [
            "IB_NF_23300_W250",
            None,
            "IB_NF_23350_W250",
            None,
            "IB_NF_23300_W250",  # back one step from 23350 — still ±1 chain
            None,
        ]
        self.assertEqual(agent._economic_choppy_flip_count(), 0)
        self.assertFalse(agent._is_market_choppy())

    def test_choppy_counts_economically_distinct_setup_flips(self):
        agent = NotificationAgent()
        agent.verdict_history = [
            "SELL PREMIUM",
            "WAIT",
            "SELL PREMIUM",
            "WAIT",
            "BUY PREMIUM",
            "SELL PREMIUM",
        ]
        agent.best_candidate_history = [
            "IB_NF_23300_W250",
            None,
            "IC_NF_23400_23200_W250",  # distinct structure
            None,
            "BULL_PUT_NF_23400_23300_W100",  # action+structure change
            "BEAR_CALL_NF_23500_23600_W100",  # another distinct setup
        ]
        # non-WAIT landings after the first: IC (flip), BULL_PUT (flip), BEAR_CALL (flip)
        self.assertGreaterEqual(agent._economic_choppy_flip_count(), 3)
        self.assertTrue(agent._is_market_choppy())

    def test_choppy_engage_logs_warn_with_flip_count(self):
        agent = NotificationAgent()
        # Seed histories so the first process_contract append makes flip count >= 3.
        agent.verdict_history = ["SELL PREMIUM", "BUY PREMIUM", "SELL PREMIUM"]
        agent.best_candidate_history = [
            "IB_NF_23300_W250",
            "BULL_PUT_NF_23400_23300_W100",
            "BEAR_CALL_NF_23500_23600_W100",
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            payload = agent.process_contract(
                {
                    "verdict": {
                        "action": "BUY PREMIUM",
                        "strategy": "BULL_CALL",
                        "confidence": 60,
                    },
                    "watchlist": [
                        _watchlist_candidate(
                            "BULL_CALL_NF_23600_23700_W100",
                            cand_type="BULL_CALL",
                        )
                    ],
                    "alerts": [],
                },
                _ctx(10_000, 99),
            )
        log = buf.getvalue()
        self.assertIn("WARN: MARKET_WHIPSAW / COOLDOWN_ACTIVE engage", log)
        self.assertIn("economic_flips=", log)
        self.assertEqual(payload["brain_notification"]["reason_code"], "MARKET_WHIPSAW")
        self.assertGreater(payload["agent_state"]["cooldown_until"], 10_000)

    def test_choppy_clear_logs_warn(self):
        agent = NotificationAgent(
            {
                "cooldown_until": 5_000,
                "verdict_history": ["WAIT"],
                "best_candidate_history": [None],
            }
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            agent.process_contract(
                {
                    "verdict": {"action": "WAIT", "strategy": None, "confidence": 0},
                    "watchlist": [],
                    "alerts": [],
                },
                _ctx(6_000, 100),
            )
        self.assertIn("WARN: COOLDOWN_ACTIVE cleared", buf.getvalue())
        self.assertEqual(agent.last_state["cooldown_until"], 0)


if __name__ == "__main__":
    unittest.main()
