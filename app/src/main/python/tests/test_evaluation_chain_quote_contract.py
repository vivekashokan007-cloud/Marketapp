"""Regression guard for the 2026-08-31 evaluation blackout.

On 2026-08-31 the app wrote 77 complete brain snapshots and 38,688 complete
option-chain rows to Supabase, and produced ZERO rows in
``ml_evaluation_outcomes``. The inputs were never the problem: the Kotlin
normaliser (``SupabaseClient.normalizedChainRow``) dropped ``bid``/``ask`` while
building the evaluation chain file, and since v2.6.3 the teacher runs with
``require_executable_quotes = True`` / ``allow_ltp_quote_fallback = False``.
With no executable quotes every close-side price is unresolvable,
``_teacher_round_trip_cost`` fails closed, and ``_managed_teacher_outcome``
returns ``None`` with ``entry_round_trip_cost_unavailable`` — for every
candidate, in every snapshot, silently.

These tests pin both directions of that contract:
  * quotes present  -> a graded outcome row is produced;
  * quotes absent   -> the candidate is dropped, and dropped for exactly that
    reason, so the failure is diagnosable instead of looking like "no data".

The fixture below is real production data from snapshot id 5012 (2026-08-31
09:30:15 IST) and the matching NF 2026-09-01 PE chain rows.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import brain


ENTRY_TS = "2026-08-31T04:00:15Z"

# (poll_ts, strike, ltp, bid, ask) — real NF 2026-09-01 PE prints from 2026-08-31.
CHAIN_PRINTS = [
    ("2026-08-31T04:05:14Z", 23950, 29.95, 29.80, 29.90),
    ("2026-08-31T04:05:14Z", 24050, 65.05, 64.85, 65.05),
    ("2026-08-31T04:10:15Z", 23950, 31.20, 31.10, 31.20),
    ("2026-08-31T04:10:15Z", 24050, 66.90, 66.25, 66.45),
    ("2026-08-31T04:15:14Z", 23950, 34.70, 34.80, 34.85),
    ("2026-08-31T04:15:14Z", 24050, 73.55, 73.40, 73.55),
    ("2026-08-31T04:20:14Z", 23950, 33.00, 33.10, 33.20),
    ("2026-08-31T04:20:14Z", 24050, 70.35, 70.50, 70.65),
    ("2026-08-31T04:25:18Z", 23950, 34.30, 34.15, 34.25),
    ("2026-08-31T04:25:18Z", 24050, 72.35, 72.20, 72.35),
    ("2026-08-31T04:30:19Z", 23950, 34.25, 34.15, 34.25),
    ("2026-08-31T04:30:19Z", 24050, 71.80, 71.85, 72.05),
]


def candidate():
    return {
        "id": "BEAR_PUT_NF_23950_24050_W100",
        "index": "NF",
        "lane": "NF_intraday",
        "type": "BEAR_PUT",
        "width": 100,
        "expiry": "2026-09-01",
        "lotSize": 65,
        "legCount": 2,
        "sellType": "PE",
        "buyType": "PE",
        "sellStrike": 23950,
        "buyStrike": 24050,
        "netPremium": 35.6,
        "maxProfit": 4186,
        "maxLoss": 2314,
        "estCost": 2314,
        "premiumEdge": 364,
        "legs": [
            {
                "action": "BUY",
                "strike": 24050,
                "option_type": "PE",
                "expiry": "2026-09-01",
                "ltp": 67.8,
                "bid": 67.2,
                "ask": 67.25,
            },
            {
                "action": "SELL",
                "strike": 23950,
                "option_type": "PE",
                "expiry": "2026-09-01",
                "ltp": 31.6,
                "bid": 31.65,
                "ask": 31.75,
            },
        ],
    }


def snapshot(cand):
    return {
        "id": 5012,
        "poll_ts": ENTRY_TS,
        "session_date": "2026-08-31",
        "primary_candidate_json": json.dumps(cand),
    }


def chain_rows(with_quotes):
    rows = []
    for poll_ts, strike, ltp, bid, ask in CHAIN_PRINTS:
        row = {
            "index_key": "NF",
            "strike": strike,
            "option_type": "PE",
            "expiry": "2026-09-01",
            "poll_ts": poll_ts,
            "ltp": ltp,
            "session_date": "2026-08-31",
        }
        if with_quotes:
            row["bid"] = bid
            row["ask"] = ask
        rows.append(row)
    return rows


class EvaluationChainQuoteContractTest(unittest.TestCase):
    def setUp(self):
        self.cand = candidate()
        self.snap = snapshot(self.cand)
        self.config = brain._teacher_default_config()

    def test_teacher_defaults_still_require_executable_quotes(self):
        """The fail-closed contract itself must not be relaxed silently."""
        self.assertTrue(self.config.get("require_executable_quotes", True))
        self.assertFalse(self.config.get("allow_ltp_quote_fallback", False))

    def test_path_is_built_identically_with_and_without_quotes(self):
        """The path length is the same either way — only the quotes differ.

        This is what made the blackout so hard to see: coverage/telemetry looked
        healthy because the chain rows *were* there and *did* match legs.
        """
        with_q = brain._build_candidate_path(chain_rows(True), self.snap, self.cand)
        without_q = brain._build_candidate_path(chain_rows(False), self.snap, self.cand)
        self.assertEqual(len(with_q), len(without_q))
        self.assertEqual(len(with_q), 6)
        self.assertIsNotNone(with_q[0].get("sell_bid"))
        self.assertIsNotNone(with_q[0].get("buy_ask"))
        self.assertIsNone(without_q[0].get("sell_bid"))
        self.assertIsNone(without_q[0].get("buy_ask"))

    def test_outcome_is_produced_when_chain_rows_carry_bid_ask(self):
        sink = {"reasons": {}, "samples": [], "sample_cap": 12}
        outcome = brain._eval_single_candidate(
            chain_rows(True), self.snap, self.cand, self.config, drop_sink=sink, role="primary"
        )
        self.assertIsNotNone(
            outcome,
            "chain rows carrying executable quotes must grade; drops=%s" % sink["reasons"],
        )
        self.assertIsNotNone(outcome.get("managed_pnl"))
        self.assertIsNotNone(outcome.get("friction_cost"))
        self.assertGreater(outcome.get("friction_cost"), 0.0)
        self.assertIsNotNone(outcome.get("exit_reason"))

    def test_outcome_is_dropped_for_the_named_reason_when_quotes_are_stripped(self):
        sink = {"reasons": {}, "samples": [], "sample_cap": 12}
        outcome = brain._eval_single_candidate(
            chain_rows(False), self.snap, self.cand, self.config, drop_sink=sink, role="primary"
        )
        self.assertIsNone(outcome)
        self.assertIn(
            "entry_round_trip_cost_unavailable",
            sink["reasons"],
            "quote-stripped rows must fail closed with a diagnosable reason, got %s"
            % sink["reasons"],
        )

    def test_snapshot_evaluation_yields_no_rows_when_quotes_are_stripped(self):
        """End-to-end shape of the 2026-08-31 blackout, at snapshot level."""
        graded = brain._evaluate_snapshot_outcomes(self.snap, chain_rows(True), self.config)
        blind = brain._evaluate_snapshot_outcomes(self.snap, chain_rows(False), self.config)
        graded_rows = graded.get("outcomes") if isinstance(graded, dict) else graded
        blind_rows = blind.get("outcomes") if isinstance(blind, dict) else blind
        self.assertTrue(graded_rows, "quotes present must yield at least one outcome row")
        self.assertFalse(blind_rows, "quotes stripped must yield zero outcome rows")


if __name__ == "__main__":
    unittest.main()
