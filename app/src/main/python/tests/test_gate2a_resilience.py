"""
GATE 2a — RESILIENCE TEST

Scope: verify that when exceptions fire inside analyze()'s helper functions,
the call does NOT crash, returns valid JSON, and the trace skeleton remains
structurally intact.

Out of scope (Gate 2b, deferred): verify that _trace['errors'] gets populated.
That requires Task 5.8 (Insight error logging) wiring, which is not yet built.
The except blocks currently print DEBUG messages; wiring them into
_trace_append(..., 'errors', ...) is Task 5.8 scope.

This test will be expanded into Gate 2b once Task 5.8 ships.
"""
import json
import sys
import os
import importlib.util


def run_gate2a_resilience_test():
    # 1. Load brain.py module safely
    current_dir = os.path.dirname(os.path.abspath(__file__))
    brain_dir = os.path.dirname(current_dir)
    brain_path = os.path.join(brain_dir, "brain.py")
    fixtures_dir = os.path.join(current_dir, "fixtures")

    if not os.path.exists(brain_path):
        brain_path = "app/src/main/python/brain.py"
        fixtures_dir = "app/src/main/python/tests/fixtures"

    spec = importlib.util.spec_from_file_location("brain", brain_path)
    brain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(brain)

    # 2. Load Fixture B (bear_debit_trend)
    fixture_path = os.path.join(fixtures_dir, "fixture_b_bear_debit_trend.json")
    with open(fixture_path, 'r', encoding='utf-8') as f:
        fx = json.load(f)

    inp = fx['inputs']
    ctx = json.loads(inp['ctx_json'])
    ctx['_debug'] = True

    # 3. TEST PATH A — RESILIENCE UNDER INJECTED FAULT
    print("Testing Path A: Resilience under forced exception injection...")

    original_get_time_mins = brain.get_time_mins

    def buggy_get_time_mins(t_str):
        raise ValueError("FORCED_ERROR: Timestamp parsing failed during audit.")

    brain.get_time_mins = buggy_get_time_mins

    try:
        result_str = brain.analyze(
            inp['poll_json'], inp['closed_trades_json'], inp['baseline_json'],
            inp['open_trades_json'], "[]", "{}", json.dumps(ctx)
        )

        # Core resilience assertions
        assert isinstance(result_str, str), "analyze() must return a string even if helpers crash"
        print("PASS: analyze() returned a string (no crash)")

        result = json.loads(result_str)
        print("PASS: Return value parsed as valid JSON")

        assert isinstance(result, dict), "Result must be a dict"
        print("PASS: Result is a dict")

        # Trace skeleton survival
        assert "_trace" in result, "Result must contain '_trace' key when debug=True"
        trace = result["_trace"]
        print("PASS: Result contains '_trace' key (debug mode)")

        required_keys = ["meta", "verdict", "positions", "candidates", "ml_budget", "errors"]
        for k in required_keys:
            assert k in trace, f"Trace missing required key: {k}"
        print(f"PASS: Trace contains all required top-level keys")

        # Note: we do NOT assert on trace['errors'] content here.
        # Task 5.8 (Insight error logging) will wire the except blocks into
        # _trace_append(..., 'errors', ...), at which point Gate 2b will
        # assert len(trace['errors']) > 0 under fault injection.
        print(f"INFO: trace['errors'] currently has {len(trace['errors'])} entries — Gate 2b deferred to post-Task-5.8.")

    finally:
        brain.get_time_mins = original_get_time_mins
        print("Monkey-patch restored.")

    # 4. TEST PATH B — CLEAN RUN
    print("\nTesting Path B: Clean run (no faults injected)...")
    result_str_clean = brain.analyze(
        inp['poll_json'], inp['closed_trades_json'], inp['baseline_json'],
        inp['open_trades_json'], "[]", "{}", json.dumps(ctx)
    )
    result_clean = json.loads(result_str_clean)
    assert "_trace" in result_clean
    assert len(result_clean["_trace"]["errors"]) == 0, \
        f"Clean run must produce zero trace errors, got: {result_clean['_trace']['errors']}"
    print("PASS: Clean run produced zero trace errors (negative control)")

    print("\nGATE 2a RESILIENCE TEST: PASSED")
    print("(Gate 2b — trace.errors population — deferred to post-Task-5.8)")
    sys.exit(0)


if __name__ == "__main__":
    try:
        run_gate2a_resilience_test()
    except AssertionError as e:
        print(f"\nGATE 2a FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nGATE 2a ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)