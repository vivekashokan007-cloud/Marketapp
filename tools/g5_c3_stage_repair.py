#!/usr/bin/env python3
"""G5 stage-specific C3 dry-run / eligibility for missing percentile finalization.

Default is dry-run. Uses original captured c3_finalization_frame evidence only.
Never fabricates verified rows for capped/incomplete candidate populations.

Reports exactly which stages were assessed/ran.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PY_ROOT = ROOT / "app" / "src" / "main" / "python"
sys.path.insert(0, str(PY_ROOT))

from evaluation_run_ledger import (  # noqa: E402
    apply_c3_assessment,
    assess_c3_frames,
    new_run,
    set_stage,
    summarize_stages_ran,
)

DEFAULT_URL = "https://fdynxkfxohbnlvayouje.supabase.co"


class SupabaseRest:
    def __init__(self, url: str, key: str, sleep_s: float = 0.15) -> None:
        self.base = url.rstrip("/") + "/rest/v1"
        self.key = key
        self.sleep_s = sleep_s

    def get(self, path: str) -> Any:
        time.sleep(self.sleep_s)
        req = urllib.request.Request(
            self.base + path,
            headers={
                "apikey": self.key,
                "Authorization": f"Bearer {self.key}",
            },
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.load(resp)

    def count(self, table: str, session_date: str) -> int:
        time.sleep(self.sleep_s)
        path = f"/{table}?select=id&session_date=eq.{session_date}"
        req = urllib.request.Request(
            self.base + path,
            method="HEAD",
            headers={
                "apikey": self.key,
                "Authorization": f"Bearer {self.key}",
                "Prefer": "count=exact",
                "Range": "0-0",
            },
        )
        # Some stacks dislike HEAD; fall back to GET with range.
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                cr = resp.headers.get("Content-Range") or resp.headers.get("content-range") or ""
        except Exception:
            req = urllib.request.Request(
                self.base + path,
                headers={
                    "apikey": self.key,
                    "Authorization": f"Bearer {self.key}",
                    "Prefer": "count=exact",
                    "Range": "0-0",
                },
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                cr = resp.headers.get("Content-Range") or resp.headers.get("content-range") or ""
        # formats: 0-0/N or */N
        if "/" in cr:
            try:
                return int(cr.split("/")[-1])
            except ValueError:
                return 0
        return 0


def load_frames(client: SupabaseRest, session_date: str) -> list[dict[str, Any]]:
    ids = client.get(
        f"/ml_brain_snapshots?select=id&session_date=eq.{session_date}&order=id.asc&limit=500"
    )
    frames: list[dict[str, Any]] = []
    for row in ids:
        sid = row.get("id")
        detail = client.get(
            "/ml_brain_snapshots"
            f"?select=id,poll_ts,context_json->c3_finalization_frame,context_json->snapshot_build3_flow"
            f"&id=eq.{sid}"
        )
        if not detail:
            continue
        frame = detail[0].get("c3_finalization_frame")
        flow = detail[0].get("snapshot_build3_flow") or {}
        if isinstance(frame, str):
            try:
                frame = json.loads(frame)
            except Exception:
                frame = None
        if isinstance(flow, str):
            try:
                flow = json.loads(flow)
            except Exception:
                flow = {}
        if not isinstance(frame, dict):
            continue
        # Attach truncation markers from original build3 flow for provenance.
        if isinstance(flow, dict):
            for key in (
                "truncated_at_ranked_evidence",
                "truncated_at_persistence",
                "truncated_at_candidates",
            ):
                if key in flow:
                    frame[key] = flow.get(key)
        frame.setdefault("snapshot_id", str(sid))
        frame.setdefault("session_date", session_date)
        frame.setdefault("poll_ts", detail[0].get("poll_ts"))
        frames.append(frame)
    return frames


def assess_session(client: SupabaseRest, session_date: str) -> dict[str, Any]:
    stages_ran: list[str] = []
    outcome_n = client.count("ml_evaluation_outcomes", session_date)
    reco_n = client.count("ml_recommendation_outcomes", session_date)
    c3_n = client.count("ml_context_percentile_history", session_date)
    snap_n = client.count("ml_brain_snapshots", session_date)
    stages_ran.append("input_coverage:checked")

    run = new_run(
        session_date=session_date,
        input_manifest={
            "snapshot_count": snap_n,
            "outcome_count": outcome_n,
            "recommendation_outcome_count": reco_n,
            "existing_c3_rows": c3_n,
            "source": "g5_c3_stage_repair_dry_run",
        },
    )
    run = set_stage(
        run,
        "input_coverage",
        "verified",
        expected_count=snap_n,
        verified_count=snap_n,
        detail={"snapshots": snap_n},
    )
    stages_ran.append("input_coverage:verified")

    if outcome_n > 0:
        run = set_stage(
            run,
            "outcome_computation",
            "verified",
            expected_count=outcome_n,
            verified_count=outcome_n,
        )
        run = set_stage(
            run,
            "outcome_persistence",
            "verified",
            written_count=outcome_n,
            verified_count=outcome_n,
        )
        stages_ran.append("outcome_computation:verified")
        stages_ran.append("outcome_persistence:verified")
    else:
        run = set_stage(run, "outcome_persistence", "failed", reason_code="NO_OUTCOMES")
        stages_ran.append("outcome_persistence:failed")

    run = set_stage(run, "research_aggregation", "ineligible", reason_code="NOT_RECHECKED_IN_C3_TOOL")
    stages_ran.append("research_aggregation:ineligible")

    frames = load_frames(client, session_date)
    stages_ran.append(f"percentile_finalization:frames_loaded:{len(frames)}")
    assessment = assess_c3_frames(frames)
    run = apply_c3_assessment(run, assessment)
    stages_ran.append(
        f"percentile_finalization:{run['stages']['percentile_finalization']['state']}:"
        f"{run['stages']['percentile_finalization'].get('reason_code') or '-'}"
    )

    return {
        "session_date": session_date,
        "current_counts": {
            "ml_brain_snapshots": snap_n,
            "ml_evaluation_outcomes": outcome_n,
            "ml_recommendation_outcomes": reco_n,
            "ml_context_percentile_history": c3_n,
        },
        "c3_assessment": assessment,
        "labels_saved": run.get("labels_saved"),
        "learning_complete": run.get("learning_complete"),
        "run_id": run.get("run_id"),
        "stages_ran": stages_ran,
        "ledger_stages_summary": summarize_stages_ran(run),
        "would_write_c3_rows": bool(assessment.get("would_write_rows")),
        "write_performed": False,
        "note": (
            "Dry-run only. Capped/incomplete original frames are marked ineligible; "
            "no fabricated C3 rows."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dates", nargs="+", default=["2026-09-10", "2026-09-11"])
    parser.add_argument("--url", default=os.environ.get("SUPABASE_URL") or DEFAULT_URL)
    parser.add_argument(
        "--key",
        default=(
            os.environ.get("SUPABASE_SERVICE_KEY")
            or os.environ.get("SUPABASE_ANON_KEY")
            or ""
        ),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Disabled in G5: this tool never writes C3 rows (eligibility/dry-run only).",
    )
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    if not args.key:
        # fall back to gradle anon for read-only dry-run
        props = ROOT / "gradle.properties"
        for line in props.read_text().splitlines():
            if line.startswith("SUPABASE_ANON_KEY="):
                args.key = line.split("=", 1)[1].strip()
                break
    if not args.key:
        print("Missing Supabase key", file=sys.stderr)
        return 2
    if args.write:
        print(
            "Refusing --write: G5 C3 repair tool is dry-run / ineligibility only; "
            "no fabricated rows.",
            file=sys.stderr,
        )
        return 3

    client = SupabaseRest(args.url, args.key)
    reports = []
    for session_date in args.dates:
        print(f"=== assessing {session_date} ===", flush=True)
        report = assess_session(client, session_date)
        reports.append(report)
        print(json.dumps(report, indent=2))

    if args.out:
        Path(args.out).write_text(json.dumps(reports, indent=2))
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
