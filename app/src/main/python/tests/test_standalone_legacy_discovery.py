"""Collect historical top-level test functions under the standard unittest gate."""

import importlib.util
import inspect
import os
import sys
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON_DIR = os.path.dirname(TEST_DIR)
if PYTHON_DIR not in sys.path:
    sys.path.insert(0, PYTHON_DIR)

LEGACY_MODULES = (
    "test_bs_primitives_phaseA", "test_build3_a8_nf_ab", "test_chain_interp_a8",
    "test_context_a6", "test_gamma_a4", "test_net_economics_authority",
    "test_net_objective_backtest_tool", "test_phase_b", "test_phase_c",
    "test_stage1_strike_pair_truncation", "test_wall_a5",
)

def _load_module(name):
    spec = importlib.util.spec_from_file_location(f"legacy_{name}", os.path.join(TEST_DIR, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for module_name in LEGACY_MODULES:
        module = _load_module(module_name)
        for name, function in sorted(inspect.getmembers(module, inspect.isfunction)):
            if name.startswith("test_") and function.__module__ == module.__name__:
                suite.addTest(unittest.FunctionTestCase(function, description=f"{module_name}.{name}"))
    return suite
