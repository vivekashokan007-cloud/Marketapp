import unittest
from pathlib import Path

import brain


def _record(kind="ranking_context", constant="RANKING_CONTEXT::vix", observed=12.5, outcome=True):
    return {
        "variable_name": "vix",
        "constant": constant,
        "context_variable": "vix",
        "slice_key": "vix|UNKNOWN|UNKNOWN|UNKNOWN",
        "authority_kind": kind,
        "authority_state": "SHADOW",
        "observed_value": observed,
        "hard_passed": outcome,
        "percentile_passed": outcome,
        "decision_json_marker": "exact-body",
    }


class Pc2RecordingCompactionTests(unittest.TestCase):
    def test_fallback_candidate_reference_distinguishes_second_iron_leg(self):
        first = {
            "index": "BNF", "type": "IRON_CONDOR", "expiry": "2026-10-01",
            "sellStrike": 56000, "buyStrike": 56200,
            "sellStrike2": 55000, "buyStrike2": 54800,
        }
        second = dict(first, sellStrike2=55100, buyStrike2=54900)
        self.assertNotEqual(
            brain._pc2_candidate_reference(first),
            brain._pc2_candidate_reference(second),
        )

    def test_exact_duplicates_aggregate_and_preserve_ordered_candidate_mapping(self):
        ctx = {}
        for ref in ("C1", "C2", "C3"):
            brain._pc2_record_authority_decision(ctx, _record(), candidate_ref=ref)

        decisions, meta = brain._pc2_finalize_authority_recording(ctx)
        self.assertEqual(1, len(decisions))
        self.assertEqual(3, decisions[0]["evaluation_count"])
        self.assertEqual(["C1", "C2", "C3"], decisions[0]["candidate_refs"])
        self.assertEqual("C1", decisions[0]["first_candidate_ref"])
        self.assertEqual("C3", decisions[0]["last_candidate_ref"])
        self.assertEqual(3, meta["call_count"])
        self.assertEqual(2, meta["aggregated_duplicate_count"])
        self.assertFalse(meta["truncated"])

    def test_missing_candidate_reference_is_counted_not_hidden(self):
        ctx = {}
        brain._pc2_record_authority_decision(ctx, _record(), candidate_ref=None)
        decisions, meta = brain._pc2_finalize_authority_recording(ctx)
        self.assertEqual(1, decisions[0]["missing_candidate_ref_count"])
        self.assertEqual([], decisions[0]["candidate_refs"])
        self.assertEqual(1, meta["call_count"])

    def test_different_inputs_or_outcomes_never_collapse(self):
        ctx = {}
        brain._pc2_record_authority_decision(ctx, _record(observed=12.5, outcome=True), "C1")
        brain._pc2_record_authority_decision(ctx, _record(observed=13.0, outcome=True), "C2")
        brain._pc2_record_authority_decision(ctx, _record(observed=12.5, outcome=False), "C3")
        decisions, meta = brain._pc2_finalize_authority_recording(ctx)
        self.assertEqual(3, len(decisions))
        self.assertEqual(3, meta["unique_decision_count"])
        self.assertEqual(3, len({row["decision_signature"] for row in decisions}))

    def test_more_than_128_unique_records_keep_gate_evidence(self):
        ctx = {}
        for i in range(150):
            brain._pc2_record_authority_decision(
                ctx,
                _record(constant=f"RANKING::{i}", observed=float(i)),
                candidate_ref=f"C{i}",
            )
        brain._pc2_record_authority_decision(
            ctx,
            _record(kind="parameter_threshold", constant="MIN_PREMIUM_EDGE", outcome=False),
            candidate_ref="GATE-CANDIDATE",
        )
        decisions, meta = brain._pc2_finalize_authority_recording(ctx)
        self.assertEqual(151, len(decisions))
        self.assertIn("parameter_threshold", meta["authority_kinds"])
        self.assertTrue(any(row["authority_kind"] == "parameter_threshold" for row in decisions))

    def test_real_resolver_path_aggregates_exact_calls_without_changing_decision(self):
        ctx = {}
        kwargs = {
            "variable_name": "vix",
            "observed_value": 45,
            "history": list(range(1, 61)),
            "stability_target": 70.0,
            "constant": "TEST_CONSTANT",
            "slice_key": "vix|UNKNOWN|UNKNOWN|UNKNOWN",
            "execution_mode": "paper",
            "hard_threshold": 40.0,
            "percentile_threshold": 40.005,
            "session_date": "2026-09-27",
            "hard_outcome": True,
            "percentile_outcome": True,
        }
        first = brain._resolve_pc2_parameter_authority(ctx, candidate_ref="C1", **kwargs)
        second = brain._resolve_pc2_parameter_authority(ctx, candidate_ref="C2", **kwargs)
        decisions, meta = brain._pc2_finalize_authority_recording(ctx)
        self.assertEqual(first, second)
        self.assertEqual(1, len(decisions))
        self.assertEqual(2, decisions[0]["evaluation_count"])
        self.assertEqual(["C1", "C2"], decisions[0]["candidate_refs"])
        self.assertEqual(2, meta["call_count"])

    def test_brain_no_longer_tail_truncates_authority_records(self):
        source = Path(brain.__file__).read_text(encoding="utf-8")
        self.assertNotIn("list(ctx.get('_pc2_authority_decisions') or [])[-128:]", source)
        self.assertIn("_pc2_finalize_authority_recording(ctx)", source)


if __name__ == "__main__":
    unittest.main()
