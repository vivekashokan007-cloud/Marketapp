import ast
import pathlib
import sys
import unittest


PYTHON_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

import brain


def _contains_string(node, value):
    return any(
        isinstance(child, ast.Constant) and child.value == value
        for child in ast.walk(node)
    )


class TestPc2AuthorityCiContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(pathlib.Path(brain.__file__).read_text(encoding="utf-8"))

    def _stable_authority_args(self):
        return {
            "ctx": {},
            "variable_name": "vix",
            "observed_value": 45,
            "history": list(range(1, 61)),
            "stability_target": 70.0,
            "constant": "TEST_CONSTANT",
            "slice_key": "vix|UNKNOWN|UNKNOWN|UNKNOWN",
            "execution_mode": "paper",
            "hard_threshold": 40.0,
            "percentile_threshold": 40.005,
            "session_date": "2026-08-16",
            "hard_outcome": True,
            "percentile_outcome": True,
        }

    def test_ta_percentile_gate_emitters_are_allowlisted(self):
        gate_basis_emitters = set()
        live_authority_assigners = set()
        for function in [node for node in ast.walk(self.tree) if isinstance(node, ast.FunctionDef)]:
            for child in ast.walk(function):
                if isinstance(child, (ast.Assign, ast.AnnAssign)):
                    targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                    value = child.value
                    names = {
                        target.id
                        for target in targets
                        if isinstance(target, ast.Name)
                    }
                    if "gate_basis" in names and _contains_string(value, "percentile"):
                        gate_basis_emitters.add(function.name)
                    if "live_percentile_authority" in names:
                        live_authority_assigners.add(function.name)

        self.assertEqual(
            gate_basis_emitters,
            {"_pc2_live_gate_decision", "_pc2_width_gate_decision"},
        )
        self.assertEqual(
            live_authority_assigners,
            {"_pc2_cross_market_move_context"},
        )

        for function_name in gate_basis_emitters | live_authority_assigners:
            function = next(
                node for node in ast.walk(self.tree)
                if isinstance(node, ast.FunctionDef) and node.name == function_name
            )
            calls = {
                child.func.id
                for child in ast.walk(function)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            }
            self.assertIn("_resolve_pc2_parameter_authority", calls)
            self.assertIn("_pc2_authority_allows_percentile", calls)

    def test_tb_ranking_context_is_scoped_to_ranking_path(self):
        callers = []
        for function in [node for node in ast.walk(self.tree) if isinstance(node, ast.FunctionDef)]:
            for call in [node for node in ast.walk(function) if isinstance(node, ast.Call)]:
                if not isinstance(call.func, ast.Name) or call.func.id != "_resolve_pc2_parameter_authority":
                    continue
                for keyword in call.keywords:
                    if (
                        keyword.arg == "authority_kind"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value == "ranking_context"
                    ):
                        callers.append(function.name)
        self.assertEqual(callers, ["_pc2_ranking_percentile_authority"])

    def test_tc_release_registry_is_empty_and_behavior_change_falls_back(self):
        self.assertEqual(brain.PC2_PROMOTION_RECORDS, {})
        args = self._stable_authority_args()
        args.update(
            hard_threshold=50.0,
            percentile_threshold=40.0,
            hard_outcome=False,
            percentile_outcome=True,
        )
        decision = brain._resolve_pc2_parameter_authority(**args)
        self.assertEqual(decision["authority_state"], "SHADOW")
        self.assertFalse(decision["authority_ready"])
        self.assertIn("promotion_record_missing", decision["authority_state_reason"])

    def test_td_malformed_authority_inputs_fail_closed(self):
        cases = {
            "unknown_execution_mode": {"execution_mode": "mystery"},
            "null_execution_mode": {"execution_mode": None},
            "missing_slice_key": {"slice_key": None},
            "missing_constant": {"constant": None},
            "unknown_authority_kind": {"authority_kind": "unknown"},
            "null_authority_kind": {"authority_kind": None},
        }
        for label, overrides in cases.items():
            with self.subTest(label=label):
                args = self._stable_authority_args()
                args.update(overrides)
                decision = brain._resolve_pc2_parameter_authority(**args)
                self.assertEqual(decision["authority_state"], "SHADOW")
                self.assertFalse(decision["authority_ready"])
                self.assertFalse(decision["input_contract_valid"])
                self.assertTrue(decision["authority_state_reason"])

    def test_diagnostics_are_versioned_and_explain_every_state(self):
        decision = brain._resolve_pc2_parameter_authority(**self._stable_authority_args())
        self.assertEqual(
            decision["authority_diagnostics_version"],
            brain.PC2_AUTHORITY_DIAGNOSTICS_VERSION,
        )
        self.assertEqual(decision["authority_state"], "BEHAVIOR_NEUTRAL")
        self.assertEqual(decision["authority_state_reason"], "neutrality_proof_passed")


if __name__ == "__main__":
    unittest.main()
