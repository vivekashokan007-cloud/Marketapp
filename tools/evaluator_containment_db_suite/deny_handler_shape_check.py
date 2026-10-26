"""Static deny-shape check for the five evaluator Edge Function handlers. Never invokes live endpoints."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SLUGS = [
    "evaluator-jobs-create",
    "evaluator-jobs-proposals",
    "evaluator-jobs-run",
    "evaluator-jobs-status",
    "evaluator-proposals-review",
]

def main() -> int:
    failed = 0
    for slug in SLUGS:
        path = ROOT / "supabase" / "functions" / slug / "index.ts"
        src = path.read_text()
        body = re.sub(r"\nDeno\.serve\([\s\S]*$", "\n", src)
        ok_auth = ("Authorization" in body) or ("authorization" in body)
        ok_deny = ("401" in body) or ("403" in body)
        status = "PASS" if (ok_auth and ok_deny) else "FAIL"
        print(f"{status}  deny-shape/{slug}  auth={ok_auth} deny_status={ok_deny}")
        if status != "PASS":
            failed += 1
    print(f"evaluator_handler_deny_shape: {len(SLUGS)} checks, {failed} failed")
    return 1 if failed else 0

if __name__ == "__main__":
    raise SystemExit(main())
