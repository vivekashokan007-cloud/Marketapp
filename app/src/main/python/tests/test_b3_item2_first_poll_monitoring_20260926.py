"""B3 item 2 (2026-09-26): first-poll Paper position monitoring.

analyze() keeps its three-poll warmup for new entries, but Paper open
positions are monitored from the first usable mark: valuation (P1 trusted
mark, else chain, else explicit DATA_UNAVAILABLE), Brain position verdict,
POSITION alerts and published exit levels. History-dependent signals are
explicitly unavailable. Real trades and Real-only payloads are unchanged.
Covers overnight positions, restarts and two trades on the same index.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brain  # noqa: E402
from test_paper_brain_p1_bridge_20260924 import _p1_mark, _trade_284  # noqa: E402

NOW_MS = 1_790_000_000_000
POLL1 = [{"t": "09:15", "bnf": 52010, "nf": 24800, "vix": 14}]
POLLS3 = [{"t": f"09:{15 + 5 * i}", "bnf": 52010 + i, "nf": 24800 + i, "vix": 14} for i in range(3)]


def _ctx(marks=None, **extra):
    ctx = {"bnfDTE": 3, "nfDTE": 3, "now_ms": NOW_MS, "mins_since_open": 1, "today_ist": "2026-09-24"}
    if marks is not None:
        ctx["p1_position_marks"] = marks
    ctx.update(extra)
    return ctx


def _run(polls, trades, ctx):
    return json.loads(brain.analyze(json.dumps(polls), "[]", "{}", json.dumps(trades), "[]", "{}", json.dumps(ctx)))


def _real(tid=900, **extra):
    t = _trade_284(paper=False)
    t["id"] = tid
    t.update(extra)
    return t


class TestFirstPollPaperMonitoring(unittest.TestCase):
    def test_paper_position_valued_and_verdicted_at_first_poll(self):
        r = _run(POLL1, [_trade_284()], _ctx({"284": _p1_mark(pnl=-1596.0)}))
        # New-entry warmup unchanged and explicit.
        self.assertEqual(r["verdict"]["action"], "WAIT")
        self.assertEqual(r["generated_candidates"], [])
        self.assertIn("Insufficient history (1 polls)", r["verdict"]["reasoning"])
        mon = r["position_monitoring"]
        self.assertEqual(mon["mode"], "FIRST_POLL_WARMUP")
        self.assertEqual(mon["scope"], "PAPER_ONLY")
        self.assertEqual(mon["monitored_trade_ids"], [284])
        self.assertEqual(mon["new_entry_warmup"], "UNAVAILABLE")
        for sig in ("regime", "market_phase", "position_momentum_threat", "new_entry_generation"):
            self.assertIn(sig, mon["history_dependent_signals_unavailable"])
        live = r["position_live"]["284"]
        self.assertEqual(live["current_pnl"], -1596.0)
        self.assertEqual(live["valuation_source"], "P1_REST_60S")
        self.assertEqual(live["monitoring_mode"], "FIRST_POLL_WARMUP")
        pos = r["positions"]["284"]
        self.assertIn(pos["verdict"]["action"], ("HOLD", "BOOK", "EXIT"))
        self.assertNotEqual(pos["verdict"].get("urgency"), "DATA_UNAVAILABLE")
        self.assertEqual(pos["monitoring_mode"], "FIRST_POLL_WARMUP")
        self.assertEqual(pos["snapshot_insights_evaluated"], ["position_wall_proximity"])

    def test_stop_condition_alerts_and_publishes_levels_at_first_poll(self):
        r = _run(POLL1, [_trade_284()], _ctx({"284": _p1_mark(pnl=-6000.0)}))
        keys = [a["key"] for a in r["alerts"]]
        self.assertIn("POS_STOP_284", keys)
        stop = next(a for a in r["alerts"] if a["key"] == "POS_STOP_284")
        self.assertEqual(stop["monitoring_mode"], "FIRST_POLL_WARMUP")
        self.assertIn("Warm-up: history-based signals unavailable.", stop["body"])
        self.assertTrue(all(a["category"] == "POSITION" for a in r["alerts"]))
        th = r["position_exit_thresholds"]["284"]
        self.assertEqual(th["computed_at_ms"], NOW_MS)
        # Same constants/percentile rule as the full path (no threshold change).
        full = _run(POLLS3, [_trade_284()], _ctx({"284": _p1_mark(pnl=-6000.0)}))
        self.assertEqual(th["stop_pnl_at"], full["position_exit_thresholds"]["284"]["stop_pnl_at"])
        self.assertEqual(th["target_pnl_at"], full["position_exit_thresholds"]["284"]["target_pnl_at"])

    def test_stop_alert_stays_tick_owned_and_brain_owns_verdicts(self):
        r = _run(POLL1, [_trade_284()], _ctx({"284": _p1_mark(pnl=-6000.0)}))
        agent = brain.NotificationAgent()
        payload = agent.process_contract(r, _ctx())
        notifying = [c.get("alert_key") for c in payload["brain_notifications"]]
        self.assertNotIn("POS_STOP_284", notifying)  # D4: tick service owns stop
        self.assertEqual(agent._position_alert_owner({"key": "POS_VERDICT_EXIT_284"}), "brain")
        self.assertEqual(agent._position_alert_owner({"key": "POS_BOOK_284"}), "brain")

    def test_no_usable_mark_is_explicit_data_unavailable_not_hold(self):
        r = _run(POLL1, [_trade_284()], _ctx({}))
        live = r["position_live"]["284"]
        self.assertIsNone(live["current_pnl"])
        self.assertEqual(live["valuation_quality"], "unavailable")
        self.assertEqual(r["positions"]["284"]["verdict"]["urgency"], "DATA_UNAVAILABLE")
        self.assertFalse(r["positions"]["284"]["verdict"]["position_exit_audit"]["exit_allowed"])
        self.assertIn("POS_DATA_QUALITY_284", [a["key"] for a in r["alerts"]])
        self.assertNotIn("284", r.get("position_exit_thresholds") or {})

    def test_untrusted_mark_is_not_used_at_first_poll(self):
        r = _run(POLL1, [_trade_284()], _ctx({"284": _p1_mark(
            pnl=-9000.0, mark_trust_state="UNTRUSTED", mark_trust_cause="WIDE_LIQUIDATION_BOOK",
            quote_validity_state="VALID")}))
        self.assertIsNone(r["position_live"]["284"]["current_pnl"])
        self.assertNotIn("POS_STOP_284", [a["key"] for a in r["alerts"]])

    def test_history_dependent_insights_are_not_evaluated(self):
        called = []
        saved = {}
        names = ("position_momentum_threat", "position_regime_fit", "position_vix_headwind",
                 "position_book_signal", "position_gamma_alert", "detect_regime")
        for n in names:
            saved[n] = getattr(brain, n)
            setattr(brain, n, (lambda nm: (lambda *a, **k: called.append(nm)))(n))
        try:
            _run(POLL1, [_trade_284()], _ctx({"284": _p1_mark()}))
        finally:
            for n, f in saved.items():
                setattr(brain, n, f)
        self.assertEqual(called, [])

    def test_full_path_after_warmup_has_no_monitoring_marker(self):
        r = _run(POLLS3, [_trade_284()], _ctx({"284": _p1_mark()}))
        self.assertNotIn("position_monitoring", r)
        self.assertNotIn("position_monitoring_scope", r)
        self.assertNotIn("monitoring_mode", r["positions"]["284"])


class TestRealParityAtFirstPoll(unittest.TestCase):
    LEGACY_EARLY_KEYS = {
        "app_version", "brain_version", "verdict", "market", "positions", "candidates", "timing",
        "risk", "learnedBranches", "dailyRiskState", "generated_candidates", "ranked_candidates_full",
        "watchlist", "candidateTrace", "decisionSource", "decision_source", "decisionReason",
        "decision_reason", "agent",
    }

    def test_real_only_early_payload_keeps_legacy_shape(self):
        r = _run(POLL1, [_real()], _ctx({"900": _p1_mark()}))
        self.assertEqual(set(r.keys()), self.LEGACY_EARLY_KEYS)
        self.assertEqual(r["positions"], {})

    def test_mixed_real_trade_is_not_monitored_or_alerted(self):
        r = _run(POLL1, [_real(), _trade_284()], _ctx({"284": _p1_mark(pnl=-6000.0), "900": _p1_mark(pnl=-6000.0)}))
        self.assertNotIn("900", r["position_live"])
        self.assertNotIn("900", r["positions"])
        self.assertNotIn("900", r.get("position_exit_thresholds") or {})
        self.assertFalse(any(a["key"].endswith("_900") for a in r["alerts"]))
        self.assertEqual(r["position_monitoring"]["unmonitored_real_trade_ids"], [900])
        self.assertIn("284", r["position_live"])


class TestOvernightPosition(unittest.TestCase):
    def test_overnight_paper_position_with_previous_session_mark(self):
        trade = _trade_284(entry_date="2026-09-23", session_date="2026-09-23")
        stale = _p1_mark(pnl=2500.0, state="STALE_LAST_VALID", last_valid_age_ms=18 * 3600 * 1000)
        # Previous-session context left in the persisted ctx must not be used.
        ctx = _ctx({"284": stale}, marketPhase="TREND_DAY",
                   regime={"type": "trend", "sigma": 1.2, "direction": 3, "trend_pct": 1.0})
        r = _run(POLL1, [trade], ctx)
        live = r["position_live"]["284"]
        self.assertIsNone(live["current_pnl"], "yesterday's mark must not value today's first poll")
        self.assertNotEqual(live.get("valuation_source"), "P1_REST_60S")
        self.assertEqual(r["positions"]["284"]["verdict"]["urgency"], "DATA_UNAVAILABLE")
        self.assertFalse(r["positions"]["284"]["verdict"]["position_exit_audit"]["exit_allowed"])
        self.assertNotIn("marketPhase", r)
        self.assertNotIn("regime", r)

    def test_overnight_position_monitored_once_today_mark_arrives(self):
        trade = _trade_284(entry_date="2026-09-23", session_date="2026-09-23")
        r = _run(POLL1, [trade], _ctx({"284": _p1_mark(pnl=2500.0, mark_trust_state="TRUSTED",
                                                     quote_validity_state="VALID")}))
        self.assertEqual(r["position_live"]["284"]["current_pnl"], 2500.0)
        self.assertEqual(r["position_monitoring"]["monitored_trade_ids"], [284])

    def test_stale_age_even_when_display_says_live_is_rejected(self):
        r = _run(POLL1, [_trade_284()], _ctx({"284": _p1_mark(last_valid_age_ms=10 * 3600 * 1000)}))
        self.assertIsNone(r["position_live"]["284"]["current_pnl"])


class TestRestart(unittest.TestCase):
    def test_restart_warmup_does_not_prune_real_alert_state(self):
        # Acknowledged states persisted before the restart; now < cooldown.
        state = {"position_alert_states": {"900:POS_STOP": NOW_MS - 60_000, "284:POS_STOP": NOW_MS - 60_000}}
        r = _run(POLL1, [_real(), _trade_284()], _ctx({"284": _p1_mark(pnl=-6000.0)}))
        agent = brain.NotificationAgent(copy.deepcopy(state))
        agent.process_contract(r, _ctx())
        self.assertIn("900:POS_STOP", agent.position_alert_states)
        self.assertIn("284:POS_STOP", agent.position_alert_states)

    def test_restart_warmup_real_state_pruning_matches_pre_b3_after_cooldown(self):
        old = NOW_MS - brain.POSITION_ALERT_REENTRY_COOLDOWN_MS - 1
        state = {"position_alert_states": {"900:POS_STOP": old}}
        mixed = _run(POLL1, [_real(), _trade_284()], _ctx({"284": _p1_mark(pnl=-6000.0)}))
        real_only = _run(POLL1, [_real()], _ctx())
        a = brain.NotificationAgent(copy.deepcopy(state)); a.process_contract(mixed, _ctx())
        b = brain.NotificationAgent(copy.deepcopy(state)); b.process_contract(real_only, _ctx())
        self.assertEqual("900:POS_STOP" in a.position_alert_states, "900:POS_STOP" in b.position_alert_states)

    def test_acknowledged_paper_alert_is_not_renotified_after_restart(self):
        # POS_BOOK is Brain-owned and notifies (forces weak while profitable).
        # Decision 3: warm-up alerts notify only with a TRUSTED mark.
        trade = _trade_284(force_alignment=1)
        r = _run(POLL1, [trade], _ctx({"284": _p1_mark(pnl=1200.0, mark_trust_state="TRUSTED",
                                                       quote_validity_state="VALID")}))
        a = brain.NotificationAgent()
        first = a.process_contract(r, _ctx())
        first_keys = [c.get("alert_key") for c in first["brain_notifications"]]
        self.assertIn("POS_BOOK_284", first_keys)
        # Not acknowledged yet (post failed) => still retryable on the next poll.
        retry = brain.NotificationAgent(json.loads(json.dumps(a.snapshot_state())))
        again = retry.process_contract(r, _ctx(now_ms=NOW_MS + 60_000))
        self.assertIn("POS_BOOK_284", [c.get("alert_key") for c in again["brain_notifications"]])
        # Posted => acknowledged; a restart restores it and does not re-notify.
        contract = next(c for c in first["brain_notifications"] if c.get("alert_key") == "POS_BOOK_284")
        a.acknowledge_delivery([contract])
        restored = brain.NotificationAgent(json.loads(json.dumps(a.snapshot_state())))
        payload = restored.process_contract(r, _ctx(now_ms=NOW_MS + 60_000))
        self.assertNotIn("POS_BOOK_284", [c.get("alert_key") for c in payload["brain_notifications"]])

    def test_restart_with_one_poll_mid_session_still_monitors(self):
        # Poll history lost on restart at 11:02 — only one poll available.
        r = _run([{"t": "11:02", "bnf": 52010, "nf": 24800, "vix": 14}], [_trade_284()],
                 _ctx({"284": _p1_mark(pnl=-1596.0)}, mins_since_open=107))
        self.assertEqual(r["position_live"]["284"]["current_pnl"], -1596.0)
        self.assertEqual(r["position_monitoring"]["poll_count"], 1)


class TestTwoTradesSameIndex(unittest.TestCase):
    def _t2(self):
        return _trade_284(id=285, sell_strike=52500, buy_strike=53000, sell_strike2=52500, buy_strike2=52000)

    def test_both_same_index_trades_monitored_independently(self):
        r = _run(POLL1, [_trade_284(), self._t2()],
                 _ctx({"284": _p1_mark(pnl=-6000.0), "285": _p1_mark(pnl=1200.0)}))
        self.assertEqual(sorted(r["position_monitoring"]["monitored_trade_ids"]), [284, 285])
        self.assertEqual(r["position_live"]["284"]["current_pnl"], -6000.0)
        self.assertEqual(r["position_live"]["285"]["current_pnl"], 1200.0)
        keys = [a["key"] for a in r["alerts"]]
        self.assertIn("POS_STOP_284", keys)
        self.assertNotIn("POS_STOP_285", keys)
        self.assertIn("284", r["position_exit_thresholds"])
        self.assertIn("285", r["position_exit_thresholds"])

    def test_one_untrusted_mark_does_not_affect_sibling(self):
        r = _run(POLL1, [_trade_284(), self._t2()],
                 _ctx({"284": _p1_mark(pnl=-9000.0, mark_trust_state="UNTRUSTED",
                                       mark_trust_cause="BOUND_REFERENCE_MISMATCH"),
                       "285": _p1_mark(pnl=1200.0, mark_trust_state="TRUSTED", quote_validity_state="VALID")}))
        self.assertIsNone(r["position_live"]["284"]["current_pnl"])
        self.assertEqual(r["position_live"]["285"]["current_pnl"], 1200.0)
        self.assertNotIn("POS_STOP_284", [a["key"] for a in r["alerts"]])

    def test_trade_failure_is_isolated(self):
        saved = brain.compute_wall_drift
        def boom(trade, chain):
            if str(trade.get("id")) == "284":
                raise RuntimeError("synthetic")
            return saved(trade, chain)
        brain.compute_wall_drift = boom
        try:
            r = _run(POLL1, [_trade_284(), self._t2()],
                     _ctx({"284": _p1_mark(), "285": _p1_mark(pnl=1200.0)}))
        finally:
            brain.compute_wall_drift = saved
        self.assertEqual(r["position_monitoring"]["monitored_trade_ids"], [285])
        self.assertIn("284", r["position_monitoring_errors"])


if __name__ == "__main__":
    unittest.main()
