"""Batch B — valuation parity contracts B1–B5 (2026-09-23)."""
from __future__ import annotations

import os
import sys
import unittest

PY_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)

import quote_observation_quality as qoq
import path_quality_evaluator as pqe
import position_state_contract as psc
import advice_parity_instrumentation as api
import replay_path_guards as rpg
import brain

ROOT = os.path.abspath(os.path.join(PY_DIR, "..", "..", "..", ".."))
PTS = os.path.join(
    ROOT, "app", "src", "main", "java", "com", "marketradar", "app", "PositionTickService.kt"
)


class B1QuoteObservationQualityTests(unittest.TestCase):
    def test_ok_two_sided(self):
        r = qoq.classify_quote_observation(bid=100, ask=102, ltp=101)
        self.assertEqual(r["classification"], qoq.CLASS_OK)
        self.assertFalse(r["structural_payoff_veto_applied"])
        self.assertFalse(r["teacher_production_filter"])

    def test_malformed(self):
        self.assertEqual(
            qoq.classify_quote_observation(bid="n/a", ask=10)["classification"],
            qoq.CLASS_MALFORMED,
        )

    def test_nonfinite(self):
        self.assertEqual(
            qoq.classify_quote_observation(bid=float("nan"), ask=10)["classification"],
            qoq.CLASS_NONFINITE,
        )

    def test_crossed(self):
        self.assertEqual(
            qoq.classify_quote_observation(bid=120, ask=100)["classification"],
            qoq.CLASS_CROSSED,
        )

    def test_missing_side(self):
        self.assertEqual(
            qoq.classify_quote_observation(bid=100, ask=None)["classification"],
            qoq.CLASS_MISSING_SIDE,
        )

    def test_stale(self):
        self.assertEqual(
            qoq.classify_quote_observation(
                bid=100, ask=101, quote_age_seconds=500
            )["classification"],
            qoq.CLASS_STALE,
        )

    def test_wide_but_possible(self):
        self.assertEqual(
            qoq.classify_quote_observation(bid=80, ask=120)["classification"],
            qoq.CLASS_WIDE_BUT_POSSIBLE,
        )

    def test_raw_row_preserved_not_mutated(self):
        raw = {"bid": 10, "ask": 11, "extra": "keep"}
        r = qoq.classify_quote_observation(10, 11, raw_row=raw)
        self.assertTrue(r["raw_row_preserved"])
        self.assertEqual(raw["extra"], "keep")
        self.assertFalse(r["loss_clamped_to_payoff"])

    def test_classify_rows_batch(self):
        rows = [
            {"bid": 100, "ask": 101},
            {"bid": 5, "ask": 4},
            {"bid": None, "ask": 3},
        ]
        out = qoq.classify_quote_rows(rows)
        self.assertEqual(
            [o["classification"] for o in out],
            [qoq.CLASS_OK, qoq.CLASS_CROSSED, qoq.CLASS_MISSING_SIDE],
        )


class B2PathQualityEvaluatorTests(unittest.TestCase):
    def _full_evidence(self):
        return {
            "oi": [1],
            "momentum": [1],
            "vix": 12,
            "breadth": 1,
            "quote_timestamps": ["a"],
            "leg_quotes": [{"bid": 1, "ask": 2}],
            "leg_quote_ages": [0.5],
        }

    def test_full_when_complete(self):
        r = pqe.evaluate_path_quality(
            points=[{"poll_ts": f"t{i}"} for i in range(3)],
            required_interval_count=3,
            evidence=self._full_evidence(),
            quote_classifications=[{"classification": "OK"}],
        )
        self.assertEqual(r["fidelity"], pqe.FIDELITY_FULL)
        self.assertFalse(r["structural_payoff_veto_applied"])
        self.assertFalse(r["legacy_teacher_mutated"])

    def test_limited_on_missing_intervals(self):
        r = pqe.evaluate_path_quality(
            points=[{"poll_ts": "t1"}],
            required_interval_count=5,
            missing_intervals=["gap1", "gap2"],
            evidence=self._full_evidence(),
        )
        self.assertEqual(r["fidelity"], pqe.FIDELITY_LIMITED_FIXTURE)

    def test_not_possible_when_unavailable(self):
        r = pqe.evaluate_path_quality(unavailable_reasons=["chain_absent"])
        self.assertEqual(r["fidelity"], pqe.FIDELITY_NOT_POSSIBLE)

    def test_structural_missing_is_limited_not_neutral(self):
        r = pqe.evaluate_path_quality(
            points=[{"poll_ts": "t1"}],
            required_interval_count=1,
            evidence={
                "oi": None,
                "momentum": None,
                "vix": 12,
                "breadth": 1,
                "quote_timestamps": ["a"],
            },
        )
        self.assertEqual(r["fidelity"], pqe.FIDELITY_LIMITED_FIXTURE)
        self.assertIn("oi", r["structural_missing"])
        self.assertTrue(any("structural_evidence_absent:oi" in x for x in r["reasons"]))

    def test_legacy_teacher_entrypoints_still_importable(self):
        present = pqe.legacy_teacher_entrypoints_present()
        self.assertTrue(all(present.values()), present)
        self.assertEqual(brain._build_candidate_path([], {}, {}), [])
        self.assertIsNone(brain._teacher_execution_basis({}, {"type": "BEAR_CALL"}, {}))


class B3PositionStateContractTests(unittest.TestCase):
    def test_both_pnl_fields_preserved(self):
        state = psc.build_position_state(
            trade_id="t1",
            indicative_gross_ltp_pnl=1200,
            executable_net_liquidation_pnl=950,
            quote_time="2026-09-23T10:00:00+05:30",
            leg_completeness={"legs_quoted": 4, "legs_required": 4},
            costs={"friction": 80},
            quantity_authority={"lot_size": 65, "source": "contract_lot_table"},
            provenance={
                "gross_producer": "compute_position_live",
                "exec_producer": "valuePositionTick",
            },
        )
        self.assertEqual(state["contract_version"], psc.POSITION_STATE_CONTRACT_VERSION)
        self.assertEqual(state["indicative_gross_ltp_pnl"], 1200)
        self.assertEqual(state["executable_net_liquidation_pnl"], 950)
        self.assertTrue(state["observation_only"])
        self.assertFalse(state["collapsed_to_single_pnl"])
        self.assertFalse(state["live_advice_bridged"])
        policy = psc.build_exit_policy_contract(
            python_action="HOLD",
            kotlin_shadow_action="HOLD",
        )
        self.assertEqual(policy["notification_authority"], "UNSELECTED")
        self.assertFalse(policy["authority_selected"])

    def test_observation_attach_does_not_overwrite_live_keys(self):
        result = {"position_live": {"t1": {"current_pnl": 1200}}}
        state = psc.build_position_state(
            trade_id="t1",
            indicative_gross_ltp_pnl=1200,
            executable_net_liquidation_pnl=900,
        )
        psc.attach_position_state_observation(result, "t1", state)
        self.assertEqual(result["position_live"]["t1"]["current_pnl"], 1200)
        self.assertEqual(
            result["position_state_observed"]["t1"]["executable_net_liquidation_pnl"],
            900,
        )


class B4ParityInstrumentationTests(unittest.TestCase):
    def test_same_event_records_both_decisions(self):
        py = {"action": "HOLD", "urgency": "WATCH", "reason": "ok"}
        kt = api.summarize_kotlin_shadow_policy(
            action="HOLD",
            reason="no shadow exit rule matched",
            valuation_quality="OK",
            mark_basis="EXECUTABLE",
            current_pnl=100.0,
            policy_version="PositionPolicyV1",
            tick_ts="2026-09-23T10:00:00Z",
            trade_id="t1",
        )
        rec = api.build_parity_record(
            event_id="evt-1",
            trade_id="t1",
            python_verdict=py,
            kotlin_summary=kt,
            session_id="2026-09-23",
            python_event_ts="2026-09-23T10:00:00+00:00",
            python_quote_ts="2026-09-23T10:00:00+00:00",
            kotlin_event_ts="2026-09-23T10:00:00Z",
            kotlin_quote_ts="2026-09-23T10:00:00Z",
        )
        self.assertTrue(rec["observation_only"])
        self.assertFalse(rec["notification_authority_selected"])
        self.assertFalse(rec["notify_behavior_changed"])
        self.assertEqual(rec["python"]["action"], "HOLD")
        self.assertEqual(rec["kotlin_shadow"]["action"], "HOLD")
        self.assertTrue(rec["actions_agree"])
        result = {}
        api.attach_parity_observation(result, "t1", rec)
        self.assertIn("t1", result[api.OBSERVATION_RESULT_KEY])

    def test_disagreement_still_observation_only(self):
        rec = api.build_parity_record(
            event_id="evt-2",
            trade_id="t2",
            python_verdict={"action": "EXIT", "urgency": "NOW"},
            kotlin_summary=api.summarize_kotlin_shadow_policy(action="HOLD"),
        )
        self.assertFalse(rec["actions_agree"])
        self.assertFalse(rec["notification_authority_selected"])


class B4NotifyGatingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(PTS, encoding="utf-8") as fh:
            cls.source = fh.read()

    def test_maybe_notify_still_present_and_gated(self):
        self.assertIn("private fun maybeNotifyShadowExit(", self.source)
        self.assertIn("SHADOW_DEGRADED_NOTIFY_COOLDOWN_MS", self.source)
        self.assertIn(
            "lastNotifyMs > 0L && now - lastNotifyMs < cooldownMs",
            self.source,
        )

    def test_parity_observation_does_not_alter_notify_gate(self):
        self.assertIn("batch_b_parity_observation", self.source)
        self.assertIn("batch_b_observation_only", self.source)
        self.assertIn("batch_b_notification_authority_selected", self.source)
        self.assertIn('batch_b_notification_authority_selected", false', self.source)
        self.assertIn("private fun maybeNotifyShadowExit(", self.source)
        self.assertIn(
            "lastNotifyMs > 0L && now - lastNotifyMs < cooldownMs",
            self.source,
        )


class B5ReplayGuardsTests(unittest.TestCase):
    def test_refuse_full_when_evidence_missing(self):
        g = rpg.guard_replay_fidelity(
            path_points=[],
            evidence={},
            claim_full=True,
        )
        self.assertEqual(g["fidelity"], pqe.FIDELITY_NOT_POSSIBLE)
        self.assertFalse(g["full_claim_honored"])
        self.assertTrue(any("full_claim_refused" in r for r in g["reasons"]))
        self.assertFalse(g["supabase_write"])

    def test_structural_label_when_oi_absent(self):
        g = rpg.guard_replay_fidelity(
            path_points=[{"poll_ts": "t1"}],
            evidence={
                "path_points": True,
                "quote_timestamps": ["a"],
                "leg_quotes": [{"bid": 1, "ask": 2}],
                "leg_quote_ages": [0.5],
                "oi": None,
                "momentum": None,
                "vix": 12,
                "breadth": 1,
            },
            required_interval_count=1,
            claim_full=True,
        )
        self.assertEqual(g["fidelity"], pqe.FIDELITY_LIMITED_FIXTURE)
        self.assertTrue(any("STRUCTURAL" in r for r in g["reasons"]))
        self.assertFalse(g["full_claim_honored"])

    def test_full_honored_when_evidence_complete(self):
        g = rpg.guard_replay_fidelity(
            path_points=[{"poll_ts": "t1"}, {"poll_ts": "t2"}],
            evidence={
                "path_points": True,
                "quote_timestamps": ["a", "b"],
                "leg_quotes": [{"bid": 1, "ask": 2}],
                "leg_quote_ages": [0.4, 0.6],
                "oi": [1],
                "momentum": [1],
                "vix": 12,
                "breadth": 1,
            },
            required_interval_count=2,
            quote_classifications=[{"classification": "OK"}],
            claim_full=True,
        )
        self.assertEqual(g["fidelity"], pqe.FIDELITY_FULL)
        self.assertTrue(g["full_claim_honored"])


class BatchAInvariantStillObservationOnly(unittest.TestCase):
    def test_a2_bridge_helpers_remain_observation_flagged(self):
        self.assertTrue(callable(getattr(brain, "_bridge_position_verdict_inputs", None)))
        src = open(os.path.join(PY_DIR, "brain.py"), encoding="utf-8").read()
        self.assertIn("observation_only_vix_erosion", src)
        self.assertIn("Do NOT feed into trade keys", src)


if __name__ == "__main__":
    unittest.main()
