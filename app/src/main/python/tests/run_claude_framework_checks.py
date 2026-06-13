"""
Run the local Claude-directed framework checks that are safe before live-market work.

Current scope:
  - Gate 1 fixture baseline regression
  - Gate 5 trace smoke
  - Gate 6 replay endpoint verification
  - Round 0 Elephant qualitative schema
  - Candidate parity contract (only when rich fixtures exist)

This runner is intentionally local/offline. It does not call Supabase, Oracle, or Android.
"""

import os
import subprocess
import sys


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

REQUIRED_CHECKS = [
    ("gate5_trace", "test_gate5_trace_smoke.py"),
    ("round0_elephant_schema", "test_round0_elephant_schema.py"),
    ("candidate_parity_contract", "test_candidate_parity_contract.py"),
]

LEGACY_CHECKS = [
    ("gate1_baselines", "test_gate1_fixture_baselines.py"),
    ("gate6_replay", "test_gate6_replay.py"),
]


def main():
    hard_failures = []
    legacy_failures = []

    for label, filename in REQUIRED_CHECKS + LEGACY_CHECKS:
        path = os.path.join(SCRIPT_DIR, filename)
        print(f"\n=== RUN {label} ===")
        result = subprocess.run([sys.executable, path], cwd=os.path.dirname(path))
        if result.returncode != 0:
            if (label, filename) in REQUIRED_CHECKS:
                hard_failures.append((label, result.returncode))
            else:
                legacy_failures.append((label, result.returncode))

    print("\n=== SUMMARY ===")
    if hard_failures:
        for label, code in hard_failures:
            print(f"FAIL: {label} (exit {code})")
    if legacy_failures:
        for label, code in legacy_failures:
            print(f"WARN: legacy {label} failed (exit {code})")

    if hard_failures:
        sys.exit(1)

    print("PASS: required Claude framework checks passed")
    sys.exit(0)


if __name__ == "__main__":
    main()
