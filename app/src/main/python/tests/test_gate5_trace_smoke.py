import json
import sys
import os
import importlib.util

# GATE 5 — TRACE INIT/EMBED SMOKE TEST
# Verfies Task 5.2 wiring: _debug=True --> _trace embedded in result.

def run_gate5_smoke_test():
    # 1. Load brain.py module safely
    # Assumes directory structure: app/src/main/python/tests/test_gate5_trace_smoke.py
    # So brain.py is at ../brain.py
    current_dir = os.path.dirname(os.path.abspath(__file__))
    brain_dir = os.path.dirname(current_dir)
    brain_path = os.path.join(brain_dir, "brain.py")
    
    if not os.path.exists(brain_path):
        # Fallback for environments with different CWD
        brain_path = "app/src/main/python/brain.py"
        if not os.path.exists(brain_path):
            print(f"FAILED: brain.py not found at {brain_path}")
            sys.exit(1)

    spec = importlib.util.spec_from_file_location("brain", brain_path)
    brain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(brain)

    # 2. Build minimal valid analyze() input fixture
    # Needs at least 3 polls to pass the len(polls) < 3 check (line 4215 approx)
    synthetic_polls = [
        {"t": "2026-04-19 10:00:00", "spot": 48000, "vix": 15, "call_iv": 16, "put_iv": 17, "atm": 48000},
        {"t": "2026-04-19 10:01:00", "spot": 48010, "vix": 15.1, "call_iv": 16.1, "put_iv": 17.1, "atm": 48000},
        {"t": "2026-04-19 10:02:00", "spot": 48020, "vix": 15.2, "call_iv": 16.2, "put_iv": 17.2, "atm": 48000}
    ]
    
    poll_json = json.dumps(synthetic_polls)
    trades_json = "[]"
    baseline_json = "{}"
    open_trades_json = "[]"
    candidates_json = "[]"
    strike_oi_json = "{}"
    
    # 3. TEST PATH: DEBUG ENABLED
    print("Testing DEBUG-ENABLED path...")
    context_debug = {
        "_debug": True, 
        "tradeMode": "swing", 
        "bnfDTE": 3, 
        "nfDTE": 5, 
        "dailyPnl": 0, 
        "dailyTradeCount": 0, 
        "vix": 15
    }
    
    result_str_debug = brain.analyze(
        poll_json, trades_json, baseline_json, 
        open_trades_json, candidates_json, strike_oi_json, 
        json.dumps(context_debug)
    )
    
    result_debug = json.loads(result_str_debug)
    
    # ASSERTIONS (DEBUG ENABLED)
    assert isinstance(result_debug, dict), "Result must be a dict"
    print("PASS: Result is a dict")
    
    assert "_trace" in result_debug, "Result must contain '_trace' key when debug=True"
    print("PASS: Result contains '_trace' key")
    
    trace = result_debug["_trace"]
    required_keys = ["meta", "verdict", "positions", "candidates", "ml_budget", "errors"]
    for k in required_keys:
        assert k in trace, f"Trace missing required key: {k}"
    print(f"PASS: Trace contains all required keys: {required_keys}")
    
    assert trace["meta"]["brain_version"] == "2.3.0", f"Wrong brain_version: {trace['meta']['brain_version']}"
    assert trace["meta"]["trace_schema_version"] == "1.0", f"Wrong schema_version: {trace['meta']['trace_schema_version']}"
    assert trace["meta"]["source"] == "live", f"Wrong source: {trace['meta']['source']}"
    assert trace["meta"]["truncated"] == False, "Trace should not be truncated"
    print("PASS: Trace meta fields verified (v2.3.0, schema 1.0, live, not truncated)")
    
    assert isinstance(trace["verdict"]["inputs"], dict) and len(trace["verdict"]["inputs"]) > 0, "verdict['inputs'] must be non-empty dict"
    print("PASS: verdict['inputs'] populated via TASK 5.3 snapshot")
    
    assert isinstance(trace["verdict"]["confidence_adjustments"], list), "confidence_adjustments must be a list"
    print("PASS: confidence_adjustments initialized as list")
    
    # Round-trip verify (Safe JSON check)
    try:
        json.dumps(result_debug)
        print("PASS: Result dict stringifies to JSON without error (Safe JSON check)")
    except Exception as e:
        print(f"FAILED: Safe JSON check failed: {e}")
        sys.exit(1)

    # 4. TEST PATH: DEBUG DISABLED
    print("\nTesting DEBUG-DISABLED path...")
    context_no_debug = dict(context_debug)
    del context_no_debug["_debug"]
    
    result_str_no_debug = brain.analyze(
        poll_json, trades_json, baseline_json, 
        open_trades_json, candidates_json, strike_oi_json, 
        json.dumps(context_no_debug)
    )
    result_no_debug = json.loads(result_str_no_debug)
    
    assert "_trace" not in result_no_debug, "Result must NOT contain '_trace' key when debug=False"
    print("PASS: Result lacks '_trace' key (Zero-cost guard verified)")

    print("\nGATE 5 SMOKE TEST: PASSED")
    sys.exit(0)

if __name__ == "__main__":
    try:
        run_gate5_smoke_test()
    except AssertionError as e:
        print(f"\nGATE 5 FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nGATE 5 ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
