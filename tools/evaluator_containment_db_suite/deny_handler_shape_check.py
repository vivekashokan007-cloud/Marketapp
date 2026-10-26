"""Static deny-shape check for the five evaluator Edge Function handlers. Never invokes live endpoints.

C1 handlers are fixed deny-all: every method returns 403 with evaluator_contained; they do not
need an Authorization gate because no request is authorized.
"""
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
        ok_403 = "403" in body
        ok_marker = "evaluator_contained" in body
        ok_no_service = "SERVICE_ROLE" not in body and "service_role" not in body
        ok_no_admin = "adminClient" not in body and "createClient" not in body
        status = "PASS" if (ok_403 and ok_marker and ok_no_service and ok_no_admin) else "FAIL"
        print(
            f"{status}  deny-shape/{slug}  403={ok_403} marker={ok_marker} "
            f"no_service_role={ok_no_service} no_admin_client={ok_no_admin}"
        )
        if status != "PASS":
            failed += 1
    print(f"evaluator_handler_deny_shape: {len(SLUGS)} checks, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
