import json
import sys
import os
import importlib.util

# GATE 2 — ERROR CAPTURE RESILIENCE TEST
# Choice for monkey-patch: get_time_mins()
# Rationale: This function is called early (during time-alignment) and frequently.
# Converting a known parsing warning into a real exception is a realistic failure mode.

def run_gate2_error_test():
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
    ctx['_debug'] = True # Must have debug enabled to see errors array
    
    # 3. TEST PATH A: FORCED EXCEPTION (Injected Error)
    print("Testing Path A: Forced Exception Injection...")
    
    original_get_time_mins = brain.get_time_mins
    
    def buggy_get_time_mins(t_str):
        raise ValueError("FORCED_ERROR: Timestamp parsing failed during audit.")
    
    # Apply monkey-patch
    brain.get_time_mins = buggy_get_time_mins
    
    try:
        result_str = brain.analyze(
            inp['poll_json'], inp['closed_trades_json'], inp['baseline_json'],
            inp['open_trades_json'], "[]", "{}", json.dumps(ctx)
        )
        
        # ASSERTIONS (Path A)
        assert isinstance(result_str, str), "analyze() must return a string even if helpers crash"
        print("PASS: analyze() returned a string (Resilience verified)")
        
        result = json.loads(result_str)
        print("PASS: String parsed as valid JSON")
        
        assert "_trace" in result, "Result contains '_trace' key"
        trace = result["_trace"]
        
        assert "errors" in trace, "Trace contains 'errors' key"
        assert isinstance(trace["errors"], list), "errors must be a list"
        assert len(trace["errors"]) > 0, "errors list must be non-empty when exceptions fire"
        print(f"PASS: Trace contains {len(trace['errors'])} captured errors")
        
        error_found = False
        for err in trace["errors"]:
            if "ValueError" in err and "FORCED_ERROR" in err:
                error_found = True
                print(f"PASS: Found specific injected error: {err}")
                break
        assert error_found, "The injected ValueError was not found in the trace errors list"

        # Check for partial trace survival
        required = ["meta", "verdict", "positions", "candidates"]
        for k in required:
            assert k in trace, f"Trace key '{k}' missing despite error capture"
        print("PASS: Partial trace survived (meta, verdict, positions, candidates exist)")

    finally:
        # 4. RESTORE monkey-patch
        brain.get_time_mins = original_get_time_mins
        print("Monkey-patch restored.")

    # 5. TEST PATH B: CLEAN RUN (Negative Check)
    print("\nTesting Path B: Clean Run (Verify no phantom errors)...")
    result_str_clean = brain.analyze(
        inp['poll_json'], inp['closed_trades_json'], inp['baseline_json'],
        inp['open_trades_json'], "[]", "{}", json.dumps(ctx)
    )
    result_clean = json.loads(result_str_clean)
    assert len(result_clean["_trace"]["errors"]) == 0, f"Clean run produced phantom errors: {result_clean['_trace']['errors']}"
    print("PASS: Clean run produced zero errors.")

    print("\nGATE 2 RESILIENCE TEST: PASSED")
    sys.exit(0)

if __name__ == "__main__":
    try:
        run_gate2_error_test()
    except AssertionError as e:
        print(f"\nGATE 2 FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nGATE 2 ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
