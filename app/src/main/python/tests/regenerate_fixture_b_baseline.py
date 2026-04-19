"""
Fixture B baseline regenerator.

What this does:
  1. Reads fixture_b_bear_debit_trend.json (the fixture INPUT)
  2. Loads brain.py and calls analyze() with the fixture input
  3. Writes the raw result to fixture_b_bear_debit_trend.baseline.json (NEW baseline)
  4. Prints a summary

What this does NOT do:
  - Modify brain.py in any way
  - Modify the fixture input file
  - Touch any other file anywhere

Run from the project root (E:\\APP\\Marketapp-main) as:
    python app\\src\\main\\python\\tests\\regenerate_fixture_b_baseline.py
"""

import json
import os
import sys
import importlib.util

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURES_DIR = os.path.join(SCRIPT_DIR, "fixtures")
BRAIN_PATH = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "brain.py"))

FIXTURE_INPUT = os.path.join(FIXTURES_DIR, "fixture_b_bear_debit_trend.json")
FIXTURE_BASELINE = os.path.join(FIXTURES_DIR, "fixture_b_bear_debit_trend.baseline.json")

def main():
    print("=== Fixture B Baseline Regenerator ===")
    print(f"Brain path:      {BRAIN_PATH}")
    print(f"Fixture input:   {FIXTURE_INPUT}")
    print(f"Fixture baseline: {FIXTURE_BASELINE}")
    print()

    # Safety checks
    for p in (BRAIN_PATH, FIXTURE_INPUT, FIXTURE_BASELINE):
        if not os.path.exists(p):
            print(f"ABORT: required file missing: {p}")
            sys.exit(1)

    # Load brain.py as a module (does not modify it)
    print("Loading brain.py...")
    spec = importlib.util.spec_from_file_location("brain", BRAIN_PATH)
    brain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(brain)
    print("Brain loaded.")

    # Load fixture input
    print("Loading Fixture B input...")
    with open(FIXTURE_INPUT, "r", encoding="utf-8") as f:
        fixture = json.load(f)

    inp = fixture["inputs"]
    print("Fixture B input loaded.")

    # Snapshot OLD baseline for side-by-side comparison
    with open(FIXTURE_BASELINE, "r", encoding="utf-8") as f:
        old_baseline = json.load(f)
    old_verdict = old_baseline.get("verdict", {})
    print(f"OLD baseline bear={old_verdict.get('bear')}, reasoning='{old_verdict.get('reasoning', '')[:80]}...'")

    # Run brain against current fixture (WITHOUT _debug; match original baseline capture mode)
    print("\nCalling brain.analyze() on Fixture B input...")
    result_str = brain.analyze(
        inp["poll_json"],
        inp["closed_trades_json"],
        inp["baseline_json"],
        inp["open_trades_json"],
        "[]",
        "{}",
        inp["ctx_json"],
    )
    result = json.loads(result_str)

    # Strip _trace if present (baselines should not contain trace data)
    if "_trace" in result:
        del result["_trace"]
        print("Removed _trace from result (baselines are non-trace captures).")

    new_verdict = result.get("verdict", {})
    print(f"NEW baseline bear={new_verdict.get('bear')}, reasoning='{new_verdict.get('reasoning', '')[:80]}...'")

    # Sanity report — show all verdict fields side-by-side
    print("\n=== Verdict field comparison ===")
    keys = sorted(set(list(old_verdict.keys()) + list(new_verdict.keys())))
    for k in keys:
        ov = old_verdict.get(k)
        nv = new_verdict.get(k)
        marker = " (changed)" if ov != nv else ""
        if k == "reasoning":
            # Truncate long strings
            ov_s = str(ov)[:60] + "..." if ov and len(str(ov)) > 60 else str(ov)
            nv_s = str(nv)[:60] + "..." if nv and len(str(nv)) > 60 else str(nv)
            print(f"  {k}{marker}:\n    OLD: {ov_s}\n    NEW: {nv_s}")
        else:
            print(f"  {k}{marker}: OLD={ov!r}  NEW={nv!r}")

    # Write new baseline
    print(f"\nWriting new baseline to: {FIXTURE_BASELINE}")
    with open(FIXTURE_BASELINE, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=True)
    print("Done.")
    print("\nReview the diff in git before committing:")
    print("    git diff -- app/src/main/python/tests/fixtures/fixture_b_bear_debit_trend.baseline.json")

if __name__ == "__main__":
    main()