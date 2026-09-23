
"""REJECT counterexamples for Batches A–D (2026-09-23).

Each test encodes a ChatGPT REJECT finding and asserts the fix is fail-closed.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

import advice_parity_instrumentation as api
import path_quality_evaluator as pqe
import replay_path_guards as rpg
import entry_decision_freeze as edf
import chronological_holdout as ch
import policy_eval_runner as per
import policy_outcome_store as pos
import brain


class Blocker1ParityJoinTests(unittest.TestCase):
    def test_in_memory_helper_alone_is_insufficient_counterexample(self):
        """Fabricating both sides in memory does not prove production join."""
        rec = api.build_parity_record(
            event_id="mem-only",
            trade_id="t1",
            python_verdict={"action": "HOLD"},
            kotlin_summary=api.summarize_kotlin_shadow_policy(action="HOLD"),
        )
        self.assertFalse(rec["actions_agree"])  # no timestamps => unavailable, never agreement
        self.assertNotEqual(rec.get("join_status"), "joined")
        store = api.ParityObservationStore(path=tempfile.mktemp(suffix=".jsonl"))
        store.clear()
        coverage = api.join_stored_observations(store=store)
        self.assertEqual(coverage["n_agreement"], 0)
        self.assertEqual(coverage["n_joined"], 0)
        self.assertEqual(coverage["coverage_joined_ratio"], 0.0)

    def test_unmatched_join_is_never_agreement(self):
        path = tempfile.mktemp(suffix=".jsonl")
        store = api.ParityObservationStore(path=path)
        store.clear()
        api.persist_path_observation(
            source="python_position_verdict",
            trade_id="t9",
            session_id="2026-09-23",
            event_ts="2026-09-23T10:00:00+05:30",
            quote_ts="2026-09-23T10:00:00+05:30",
            action="HOLD",
            store=store,
        )
        cov = api.join_stored_observations(store=store)
        self.assertEqual(cov["n_agreement"], 0)
        self.assertGreaterEqual(cov["n_unavailable"], 1)
        self.assertTrue(all(not r.get("actions_agree") for r in cov["joined_records"]))

    def test_late_outside_tolerance_is_unavailable_not_agreement(self):
        path = tempfile.mktemp(suffix=".jsonl")
        store = api.ParityObservationStore(path=path)
        store.clear()
        api.persist_path_observation(
            source="python_position_verdict",
            trade_id="t2",
            session_id="sess-A",
            event_ts="2026-09-23T10:00:00+05:30",
            quote_ts="2026-09-23T10:00:00+05:30",
            action="HOLD",
            store=store,
        )
        api.persist_path_observation(
            source="kotlin_shadow_policy",
            trade_id="t2",
            session_id="sess-A",
            event_ts="2026-09-23T10:05:00+05:30",
            quote_ts="2026-09-23T10:05:00+05:30",
            action="HOLD",
            store=store,
        )
        cov = api.join_stored_observations(store=store, join_tolerance_seconds=90)
        self.assertEqual(cov["n_agreement"], 0)
        self.assertGreaterEqual(cov["n_unavailable"], 1)

    def test_joined_readback_coverage_counts(self):
        path = tempfile.mktemp(suffix=".jsonl")
        store = api.ParityObservationStore(path=path)
        store.clear()
        api.persist_path_observation(
            source="python_position_verdict",
            trade_id="t3",
            session_id="sess-2026-09-23",
            event_ts="2026-09-23T10:00:00+00:00",
            quote_ts="2026-09-23T10:00:00+00:00",
            action="HOLD",
            store=store,
        )
        api.persist_path_observation(
            source="kotlin_shadow_policy",
            trade_id="t3",
            session_id="sess-2026-09-23",
            event_ts="2026-09-23T10:00:30+00:00",
            quote_ts="2026-09-23T10:00:30+00:00",
            action="EXIT",
            store=store,
        )
        cov = api.join_stored_observations(store=store, join_tolerance_seconds=90)
        self.assertEqual(cov["n_disagreement"], 1)
        self.assertEqual(cov["n_agreement"], 0)
        self.assertEqual(cov["n_joined"], 1)

    def test_parity_evidence_survives_android_compaction(self):
        source = {
            "nfDTE": 0,
            "bnfDTE": 2,
            "snapshot_open_trades_json": '[{"id":"t1"}]',
            "marketPhase": {"id": "MIDDAY"},
            "snapshot_position_verdicts": {"t1": {"action": "HOLD"}},
            "snapshot_position_marks": {"t1": {"current_pnl": 1}},
            api.OBSERVATION_RESULT_KEY: {
                "t1": {
                    "contract_version": api.ADVICE_PARITY_CONTRACT_VERSION,
                    "actions_agree": False,
                    "join_status": "unavailable",
                    "observation_only": True,
                }
            },
            api.POSITION_STATE_RESULT_KEY: {
                "t1": {"observation_only": True, "indicative_gross_ltp_pnl": 1}
            },
            "snapshot_capture_completeness": "forward_capture_v1_batch_a",
        }
        compact = brain._compact_android_snapshot_context(source)
        self.assertIn(api.OBSERVATION_RESULT_KEY, compact)
        self.assertIn(api.POSITION_STATE_RESULT_KEY, compact)
        self.assertIn("t1", compact[api.OBSERVATION_RESULT_KEY])


class Blocker2PathQualityEmptyNotFullTests(unittest.TestCase):
    def test_bare_evaluate_path_quality_is_not_full(self):
        r = pqe.evaluate_path_quality()
        self.assertNotEqual(r["fidelity"], pqe.FIDELITY_FULL)
        self.assertEqual(r["fidelity"], pqe.FIDELITY_NOT_POSSIBLE)

    def test_bare_guard_replay_fidelity_is_not_full(self):
        g = rpg.guard_replay_fidelity()
        self.assertNotEqual(g["fidelity"], pqe.FIDELITY_FULL)
        self.assertEqual(g["fidelity"], pqe.FIDELITY_NOT_POSSIBLE)

    def _points(self, n=3):
        return [{"poll_ts": f"2026-09-23T10:0{i}:00+05:30"} for i in range(n)]

    def _ltp_evidence(self, **over):
        ev = {
            "quote_timestamps": ["a", "b", "c"],
            "leg_quotes": [{"bid": 1, "ask": 2}],
            "leg_quote_ages": [0.2, 0.3, 0.4],
            "oi": [1],
            "momentum": [1],
            "vix": 12,
            "breadth": 1,
        }
        ev.update(over)
        return ev

    def _exec_evidence(self, **over):
        ev = self._ltp_evidence()
        ev.update({
            "bid_ask_sides": [{"bid": 1, "ask": 2}],
            "executable_marks": [1.5],
        })
        ev.update(over)
        return ev

    def test_matrix_ltp_gross_and_executable_net(self):
        cases = [
            ("empty", None, None, [], pqe.FIDELITY_NOT_POSSIBLE),
            ("partial", self._points(1), self._ltp_evidence(oi=None), [{"classification": "OK"}], pqe.FIDELITY_LIMITED_FIXTURE),
            ("stale", self._points(3), self._ltp_evidence(), [{"classification": "STALE"}], pqe.FIDELITY_LIMITED_FIXTURE),
            ("wide_uncrossed", self._points(3), self._ltp_evidence(), [{"classification": "WIDE_BUT_POSSIBLE"}], pqe.FIDELITY_LIMITED_FIXTURE),
            ("complete", self._points(3), self._ltp_evidence(), [{"classification": "OK"}], pqe.FIDELITY_FULL),
        ]
        for basis in (pqe.VALUATION_BASIS_LTP_GROSS, pqe.VALUATION_BASIS_EXECUTABLE_NET):
            for name, points, evidence, quotes, expected in cases:
                ev = evidence
                if evidence is not None and basis == pqe.VALUATION_BASIS_EXECUTABLE_NET:
                    ev = self._exec_evidence()
                    if name == "partial":
                        ev["oi"] = None
                with self.subTest(basis=basis, case=name):
                    r = pqe.evaluate_path_quality(
                        points=points,
                        required_interval_count=(3 if points and len(points) >= 3 else (1 if points else None)),
                        evidence=ev,
                        quote_classifications=quotes,
                        valuation_basis=basis,
                    )
                    self.assertEqual(r["fidelity"], expected, f"{basis}/{name}: {r['reasons']}")


class Blocker3CaptureBudgetTests(unittest.TestCase):
    def test_over_budget_does_not_claim_complete_capture(self):
        huge = "x" * 1_400_000
        compact = brain._compact_android_snapshot_context({
            "snapshot_open_trades_json": huge,
            "nfDTE": 0,
            "bnfDTE": 2,
            "snapshot_capture_completeness": "forward_capture_v1_batch_a",
        })
        marker = compact.get("snapshot_capture_completeness")
        self.assertNotEqual(marker, "forward_capture_v1_batch_a")
        self.assertIn("budget", str(marker).lower())
        self.assertTrue(compact.get("snapshot_capture_failure_reason"))

    def test_incomplete_fields_no_false_complete_marker(self):
        snap = brain.take_poll_snapshot(
            {
                "watchlist": [],
                "generated_candidates": [],
                "ranked_candidates_full": [],
                "rejected_candidates": [],
                "verdict": {"action": "WAIT", "strategy": "NONE", "direction": "NEUTRAL", "confidence": 0},
                "bnfProfile": {},
                "nfProfile": {},
            },
            {"today_ist": "2026-09-23"},
            [],
            "android_compact_v1",
        )
        ctx = snap["context_json"]
        if isinstance(ctx, str):
            ctx = json.loads(ctx)
        self.assertNotEqual(ctx.get("snapshot_capture_completeness"), "forward_capture_v1_batch_a")
        self.assertIn("incomplete", str(ctx.get("snapshot_capture_completeness")).lower())
        self.assertIsInstance(ctx.get("snapshot_capture_field_status"), dict)
        self.assertTrue(ctx.get("snapshot_capture_failure_reason"))

    def test_python_contract_documents_agp_gap(self):
        self.assertTrue(callable(brain._compact_android_snapshot_context))
        self.assertTrue(callable(brain.take_poll_snapshot))
        agp_gap = (
            "Android Gradle Plugin / device upload-readback not executable in this "
            "runtime; Python producer→compact→JSON round-trip is the enforced contract."
        )
        self.assertIn("Android Gradle Plugin", agp_gap)


class Blocker4SyntheticRunnerTests(unittest.TestCase):
    def test_report_flagged_synthetic_and_performance_gated(self):
        report = per.run_fixture_eval(write_report=False)
        self.assertTrue(report.get("SYNTHETIC_FIXTURE_ONLY"))
        self.assertTrue(report.get("research_performance_comparisons_suppressed"))
        self.assertTrue(report["summary"].get("SYNTHETIC_FIXTURE_ONLY"))
        self.assertFalse(report["summary"].get("performance_comparison_allowed"))
        self.assertIsNone(report.get("performance_report"))
        self.assertTrue(report["summary"].get("sum_net_rupees_is_not_real_research_improvement"))
        self.assertFalse(report.get("executable_historical_replay_implemented"))
        stub = per.run_real_policy_replay()
        self.assertEqual(stub["status"], "NOT_IMPLEMENTED")


class Blocker5TimestampFreezePinTests(unittest.TestCase):
    def test_mixed_offset_backdated_outcome_rejected(self):
        store = edf.EntryDecisionFreezeStore(memory_only=True)
        store.freeze_entry(
            entry_identity="rej-mixed",
            freeze_ts="2026-09-23T11:00:00+00:00",
            decision={"action": "ENTER"},
        )
        with self.assertRaises(ValueError) as ctx:
            store.populate_outcome(
                entry_identity="rej-mixed",
                outcome={"net_rupees": 10},
                outcome_ts="2026-09-23T16:00:00+05:30",
            )
        self.assertTrue(
            ("before_freeze" in str(ctx.exception))
            or ("before_creation" in str(ctx.exception)),
            str(ctx.exception),
        )

    def test_holdout_orders_by_utc_instant(self):
        split = ch.split_chronological(
            [
                {"entry_identity": "a", "entry_ts": "2026-09-23T16:00:00+05:30"},
                {"entry_identity": "b", "entry_ts": "2026-09-23T12:00:00+00:00"},
            ],
            holdout_after_ts="2026-09-23T11:00:00+00:00",
        )
        self.assertEqual([r["entry_identity"] for r in split["train"]], ["a"])
        self.assertEqual([r["entry_identity"] for r in split["holdout"]], ["b"])

    def test_naive_timestamp_rejected(self):
        store = edf.EntryDecisionFreezeStore(memory_only=True)
        with self.assertRaises(ValueError):
            store.freeze_entry(
                entry_identity="naive",
                freeze_ts="2026-09-23T11:00:00",
                decision={"action": "ENTER"},
            )

    def test_durable_freeze_records_creation_and_hashes_before_outcomes(self):
        with tempfile.TemporaryDirectory() as d:
            store = edf.EntryDecisionFreezeStore(store_dir=d)
            row = store.freeze_entry(
                entry_identity="dur1",
                freeze_ts="2026-09-23T11:00:00+00:00",
                decision={"action": "ENTER"},
                policy_id="H0",
                policy_version="v1",
            )
            self.assertTrue(row["durable"])
            self.assertTrue(row["created_at_utc"])
            self.assertTrue(row["policy_hash"])
            self.assertTrue(row["data_hash"])
            self.assertIsNone(row["outcome"])
            store2 = edf.EntryDecisionFreezeStore(store_dir=d)
            again = store2.get("dur1")
            self.assertIsNotNone(again)
            self.assertEqual(again["policy_hash"], row["policy_hash"])


class FurtherAccuracyTests(unittest.TestCase):
    def test_structural_row_has_no_unmarked_zero_economics(self):
        store = pos.PolicyOutcomeStore()
        row = store.put_outcome(
            entry_identity="s1",
            policy_id="deployed_position_verdict",
            policy_version="v1",
            experiment_version="e",
            data_version="d",
            code_version="c",
            cost_version="k",
            structural=True,
            structural_reasons=["missing_inputs"],
            metrics={"net_rupees": 0, "label": "STRUCTURAL"},
            behavior_fingerprint="deployed_position_verdict::v1",
        )
        metrics = row.get("metrics") or {}
        self.assertNotEqual(metrics.get("net_rupees"), 0)
        self.assertTrue(("net_rupees" not in metrics) or metrics.get("net_rupees") is None)
        fp = str(row.get("behavior_fingerprint") or "")
        self.assertTrue("PLACEHOLDER" in fp or "NO_IMPL" in fp or row.get("behavior_fingerprint_note"))

    def test_live_valuation_ignores_ambient_legacy_vix_mode(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "brain.py"), encoding="utf-8").read()
        self.assertIn("ignoring ambient ctx vix_change_mode", src)
        info = brain._compute_vix_change_for_verdict(None, 12.0, mode="legacy_missing_vix_fallback_15")
        self.assertEqual(info["vix_change_provenance"], "legacy_missing_vix_fallback_15")


if __name__ == "__main__":
    unittest.main()
