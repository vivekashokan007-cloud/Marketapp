"""Focused checks for afternoon-positioning retirement + R1 factual close (2.6.66)."""
import json
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
import brain


def _hist_row(date, vix, t="15:30"):
    return {"date": date, "vix": vix, "t": t}


def _ctx(mode, today="2026-10-05", rows=None, **extra):
    ctx = {
        "today_ist": today,
        "executionMode": mode,
        "bnfDTE": 3,
        "nfDTE": 3,
        "ivPercentile": 50,
        "vixDailyHistory": rows if rows is not None else [
            _hist_row("2026-10-01", 13.8),
            _hist_row("2026-09-30", 14.1),
        ],
    }
    ctx.update(extra)
    return ctx


class TestRetireAfternoonPositioning(unittest.TestCase):
    def test_brain_version(self):
        self.assertEqual(brain.BRAIN_VERSION, "2.6.67")

    def test_retired_symbols_gone(self):
        for name in (
            "yesterday_signal_prior",
            "validate_yesterday_signal",
            "build_chain_snapshot_data",
            "compute_positioning",
            "compute_global_boost",
        ):
            self.assertFalse(hasattr(brain, name), name)

    def test_prior_score_no_longer_contributes(self):
        polls = [{"bnf": 50000, "nf": 22000, "vix": 14.0, "t": "10:00"}]
        baseline = {"bnfSpot": 50000, "nfSpot": 22000, "vix": 14.0}
        ctx_base = {
            "today_ist": "2026-10-05",
            "bnfDTE": 3,
            "nfDTE": 3,
            "tradeMode": "paper",
            "bnfBreadth": {"pct": 50},
            "bnfProfile": {},
            "fiiHistory": [],
            "ivPercentile": 50,
        }
        v0 = brain.synthesize_verdict([], {"type": "range"}, dict(ctx_base), polls, baseline)
        prior_like = {
            "type": "prior",
            "icon": "📡",
            "label": "Yesterday: BULLISH (3/5)",
            "detail": "legacy",
            "impact": "bullish",
            "strength": 3,
        }
        v_with = brain.synthesize_verdict(
            [prior_like], {"type": "range"}, dict(ctx_base), polls, baseline
        )
        delta = float(v_with.get("bull") or 0) - float(v0.get("bull") or 0)
        self.assertAlmostEqual(delta, 0.6, places=5)

    def test_analyze_output_drops_retired_keys(self):
        """Output-level: analyze result must not emit retired positioning keys."""
        polls = [
            {"bnf": 50000.0, "nf": 22000.0, "vix": 13.5, "t": "10:00", "pcr": 1.0,
             "futuresPremBnf": 20, "nearAtmPCR": 1.0},
            {"bnf": 50010.0, "nf": 22005.0, "vix": 13.6, "t": "10:05", "pcr": 1.01,
             "futuresPremBnf": 21, "nearAtmPCR": 1.01},
        ]
        baseline = {
            "bnfSpot": 50000.0, "nfSpot": 22000.0, "vix": 13.5, "date": "2026-10-05",
            "pcr": 1.0, "futuresPremBnf": 20,
        }
        ctx = {
            "today_ist": "2026-10-05",
            "mins_since_open": 400,
            "snap_2pm_today": {
                "bnf_total_call_oi": 100, "bnf_total_put_oi": 100, "bnf_pcr": 1.0,
                "bnf_near_atm_pcr": 1.0, "vix": 13.0, "bnf_max_pain": 50000,
                "bnf_breadth_pct": 50, "bnf_spot": 50000,
                "nf_total_call_oi": 50, "nf_total_put_oi": 50,
            },
            "yesterdaySignal": {"signal": "BEARISH", "strength": 4, "tomorrow_signal": "BEARISH"},
            "signalAccuracy": {"pct": 70, "total": 20},
            "bnfChain": {"atm": 50000, "strikes": {}, "pcr": 1.0},
            "nfChain": {"atm": 22000, "strikes": {}, "pcr": 1.0},
            "bnfBreadth": {"pct": 50},
            "tradeMode": "paper",
            "executionMode": "paper",
            "bnfDTE": 3,
            "nfDTE": 3,
            "ivPercentile": 40,
            "vixDailyHistory": [
                _hist_row("2026-10-01", 14.2),
                _hist_row("2026-09-30", 13.8),
            ],
            "candidates": [],
            "open_trades": [],
            "closed_trades": [],
        }
        result = json.loads(brain.analyze(
            json.dumps(polls),
            json.dumps([]),
            json.dumps(baseline),
            json.dumps([]),
            json.dumps([]),
            json.dumps({}),
            json.dumps(ctx),
        ))
        self.assertIsInstance(result, dict)
        for k in (
            "chain_snapshot_now", "positioning", "tomorrow_signal", "signalValidation",
        ):
            self.assertNotIn(k, result)

    def test_signal_accuracy_slot_null_in_context_percentiles(self):
        """C3 current signal_accuracy must be None even when stale context supplies a value."""
        polls = [{"bnf": 50000, "nf": 22000, "vix": 14.0, "t": "10:00"}]
        ctx = {
            "today_ist": "2026-10-05",
            "signalAccuracy": {"pct": 99, "total": 10},
            "bnfChain": {},
            "nfChain": {},
            "bnfBreadth": {"pct": 50},
            "ivPercentile": 40,
            "executionMode": "paper",
        }
        result = {
            "signalAccuracy": {"pct": 99},
            "signal_accuracy": 99,
            "verdict": {"confidence": 0.5, "bull": 1, "bear": 1},
            "watchlist": [],
        }
        pack = brain._build_context_percentiles(ctx, polls, [], [], result=result, open_trades=[])
        current = (pack or {}).get("current_values") or {}
        self.assertIsNone(current.get("signal_accuracy"))

    def test_factual_close_paper_and_real_modes(self):
        rows = [_hist_row("2026-10-01", 13.85), _hist_row("2026-09-30", 14.0)]
        for mode in ("paper", "live", "sandbox"):
            ctx = _ctx(mode, rows=rows)
            factual = brain._vix_immediate_previous_close(ctx)
            self.assertTrue(factual["previous_close_verified"], mode)
            self.assertEqual(factual["previous_close_sessions_behind"], 0)
            self.assertEqual(factual["previous_close"], 13.85)
            self.assertEqual(factual["previous_close_date"], "2026-10-01")

            summary = brain._pc2_vix_regime_summary(ctx, {"vix": 14.2})
            self.assertTrue(summary["previous_close_verified"], mode)
            self.assertEqual(summary["previous_close"], 13.85)
            self.assertEqual(summary["previous_close_date"], "2026-10-01")
            self.assertEqual(summary["previous_close_sessions_behind"], 0)

            if mode != "paper":
                # Real decision policy / legacy regime history unchanged
                self.assertEqual(summary["decision_scope"], "real_legacy_unchanged")
                self.assertEqual(summary["history_status"], "LEGACY_UNVERIFIED")
                self.assertIsNone(summary["history_sessions_behind"])
            else:
                self.assertEqual(summary["decision_scope"], "paper_corrected")
                # Paper may publish FRESH history_sessions_behind from regime path
                self.assertEqual(summary.get("history_status"), "FRESH")

    def test_factual_close_rejects_invalid_inputs(self):
        today = "2026-10-05"
        cases = [
            ("missing", [], None),
            ("stale", [_hist_row("2026-09-30", 13.8)], None),  # Fri Sep 30 → Mon Oct 5 skips Oct 1 → behind>=1
            ("same_day", [_hist_row("2026-10-05", 13.8)], None),
            ("future", [_hist_row("2026-10-06", 13.8)], None),
            ("invalid_time", [_hist_row("2026-10-01", 13.8, t="12:00")], None),
            ("non_positive", [_hist_row("2026-10-01", 0.0)], None),
            ("negative", [_hist_row("2026-10-01", -1.0)], None),
            ("non_finite", [_hist_row("2026-10-01", float("nan"))], None),
        ]
        for label, rows, _ in cases:
            ctx = _ctx("live", today=today, rows=rows)
            factual = brain._vix_immediate_previous_close(ctx)
            self.assertFalse(factual["previous_close_verified"], label)
            self.assertIsNone(factual["previous_close"], label)
            self.assertIsNone(factual["previous_close_date"], label)

        # Explicit valid control
        ok = brain._vix_immediate_previous_close(
            _ctx("paper", rows=[_hist_row("2026-10-01", 12.5)])
        )
        self.assertTrue(ok["previous_close_verified"])
        self.assertEqual(ok["previous_close"], 12.5)


if __name__ == "__main__":
    unittest.main()
