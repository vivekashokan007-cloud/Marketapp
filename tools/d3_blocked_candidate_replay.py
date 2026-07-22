#!/usr/bin/env python3
"""D3 blocked-candidate replay analysis.

Read-only diagnostic tool for A8-blocked and A8-surviving candidates.
It reuses the shipped teacher replay path from `historical_replay_harness.py`
and never writes to Supabase.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = REPO_ROOT / "reports"
GRADLE_PROPERTIES = REPO_ROOT / "gradle.properties"
DEFAULT_URL = "https://fdynxkfxohbnlvayouje.supabase.co"
QUARANTINED_TRADE_IDS = {"176", "177", "178", "180", "181"}
SUPABASE_RETRY_ATTEMPTS = 3

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_gradle_property(key: str) -> str | None:
    if not GRADLE_PROPERTIES.exists():
        return None
    prefix = f"{key}="
    for line in GRADLE_PROPERTIES.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(prefix):
            value = line.split("=", 1)[1].strip()
            return value or None
    return None


def _ensure_supabase_env() -> None:
    url = os.environ.get("SUPABASE_URL") or _load_gradle_property("SUPABASE_URL") or DEFAULT_URL
    key = (
        os.environ.get("SUPABASE_SERVICE_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
        or os.environ.get("SUPABASE_ANON_KEY")
        or _load_gradle_property("SUPABASE_ANON_KEY")
    )
    if not key:
        raise RuntimeError("SUPABASE_ANON_KEY not found in environment or gradle.properties")
    os.environ["SUPABASE_URL"] = url
    os.environ["SUPABASE_SERVICE_KEY"] = key


_ensure_supabase_env()

import historical_replay_harness as hrh  # noqa: E402
import brain  # noqa: E402


@dataclass
class ReplayRow:
    session_date: str
    snapshot_id: str
    poll_ts_ist: str
    session_window: str
    candidate_id: str
    cohort: str
    index_key: str
    side: str
    strategy_family: str
    premium_edge_bucket: str
    vix_bucket: str
    fii_short_trend: str
    pcr_state: str
    wall_state: str
    anchor_type: str
    slippage_basis: str
    friction_total: float | None
    anchor_net_pnl: float | None
    anchor_r: float | None
    outcome_positive: int | None
    failure_class: str
    teacher_matched: int
    reco_matched: int
    simulated: int
    pricing_failed: int


def _json_load(value: Any, default: Any) -> Any:
    return hrh._json_load(value, default)


def _safe_float(value: Any) -> float | None:
    return hrh._safe_float(value)


def _parse_ist_time_label(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%H:%M:%S")
    except Exception:
        if "T" in text:
            text = text.split("T", 1)[1]
        return text[:8]


def _session_window(poll_ts: str) -> str:
    label = _parse_ist_time_label(poll_ts)
    if not label:
        return "OUT_OF_WINDOW"
    if "09:15:00" <= label < "10:30:00":
        return "MORNING_LOCK"
    if "10:30:00" <= label < "13:00:00":
        return "MIDDAY"
    if "13:00:00" <= label <= "15:30:00":
        return "LATE"
    return "OUT_OF_WINDOW"


def _index_key(candidate: dict[str, Any]) -> str:
    raw = str(candidate.get("index") or candidate.get("index_key") or "").upper()
    if raw in {"BNF", "NF"}:
        return raw
    return "OTHER"


def _side(candidate: dict[str, Any]) -> str:
    raw = candidate.get("isCredit")
    if raw is None:
        raw = candidate.get("is_credit")
    if raw is True:
        return "CREDIT"
    if raw is False:
        return "DEBIT"
    return "UNKNOWN_SIDE"


def _strategy_family(candidate: dict[str, Any]) -> str:
    return str(candidate.get("type") or candidate.get("strategy_type") or "OTHER").upper()


def _stored_premium_edge(candidate: dict[str, Any]) -> float | None:
    value = _safe_float(candidate.get("premiumEdge"))
    if value is None:
        value = _safe_float(candidate.get("premium_edge"))
    return value


def _premium_edge_bucket(candidate: dict[str, Any]) -> str:
    value = _stored_premium_edge(candidate)
    if value is None:
        return "EDGE_MISSING"
    if value < 0:
        return "EDGE_LT_0"
    if value < 10:
        return "EDGE_0_10"
    if value < 25:
        return "EDGE_10_25"
    return "EDGE_25_PLUS"


def _vix_bucket(snapshot: dict[str, Any], ctx: dict[str, Any]) -> str:
    latest = ctx.get("snapshot_latest_poll") if isinstance(ctx, dict) else {}
    latest = latest if isinstance(latest, dict) else {}
    vix = _safe_float(latest.get("vix"))
    if vix is None:
        vix = _safe_float(ctx.get("vix"))
    return str(brain._stage2a_vix_bucket(vix))


def _fii_short_trend(ctx: dict[str, Any]) -> str:
    out = brain.fii_short_trend(ctx)
    if not isinstance(out, dict):
        return "UNKNOWN"
    trend = str(out.get("trend") or "").upper()
    if trend in {"BUILDING", "COVERING", "INFLECTION"}:
        return trend
    return "FLAT_OR_OTHER"


def _pcr_value_for_candidate(snapshot: dict[str, Any], ctx: dict[str, Any], candidate: dict[str, Any]) -> float | None:
    latest = ctx.get("snapshot_latest_poll") if isinstance(ctx, dict) else {}
    latest = latest if isinstance(latest, dict) else {}
    index_key = _index_key(candidate)
    if index_key == "BNF":
        value = _safe_float(latest.get("bnfPcr"))
        if value is None:
            value = _safe_float(ctx.get("bnf_pcr"))
        if value is None:
            value = _safe_float(latest.get("pcr"))
        return value
    if index_key == "NF":
        value = _safe_float(latest.get("nfPcr"))
        if value is None:
            value = _safe_float(ctx.get("nf_pcr"))
        if value is None:
            value = _safe_float(latest.get("pcr"))
        return value
    return _safe_float(latest.get("pcr")) or _safe_float(ctx.get("bnf_pcr")) or _safe_float(ctx.get("nf_pcr"))


def _pcr_state(snapshot: dict[str, Any], ctx: dict[str, Any], candidate: dict[str, Any]) -> str:
    value = _pcr_value_for_candidate(snapshot, ctx, candidate)
    if value is None:
        return "PCR_UNKNOWN"
    if value < 0.95:
        return "PCR_LT_0_95"
    if value <= 1.05:
        return "PCR_0_95_1_05"
    return "PCR_GT_1_05"


def _wall_state(candidate: dict[str, Any]) -> str:
    wall_tag = str(candidate.get("wallTag") or "").upper()
    if "SAFE" in wall_tag or "STRONG" in wall_tag:
        return "WALL_STRONG"
    wall_score = _safe_float(candidate.get("wallScore"))
    if wall_score is None:
        return "WALL_UNKNOWN"
    if wall_score >= 2:
        return "WALL_STRONG"
    if wall_score > 0:
        return "WALL_PRESENT"
    return "WALL_WEAK_OR_NONE"


def _max_loss(candidate: dict[str, Any]) -> float | None:
    return _safe_float(candidate.get("maxLoss")) or _safe_float(candidate.get("max_loss"))


def _slippage_basis_from_cost_breakdown(cost_breakdown: dict[str, Any] | None) -> str:
    if not isinstance(cost_breakdown, dict):
        return "PRICING_FAILED"
    missing = cost_breakdown.get("missing_spread_labels") or []
    return "FALLBACK" if missing else "LIVE_BID_ASK"


def _extract_generated_candidates(snapshot: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    generated = ctx.get("snapshot_generated_candidates")
    if isinstance(generated, list):
        return [row for row in generated if isinstance(row, dict)]
    fallback = _json_load(snapshot.get("top_candidates_json"), [])
    if isinstance(fallback, list):
        return [row for row in fallback if isinstance(row, dict)]
    return []


def _java_string_hash(raw: str) -> int:
    value = 0
    for ch in raw:
        value = (31 * value + ord(ch)) & 0xFFFFFFFF
    if value >= 0x80000000:
        value -= 0x100000000
    return value


def _synthetic_rejected_id(poll_ts: str, candidate: dict[str, Any], ordinal: int) -> str:
    raw = "|".join(
        [
            poll_ts,
            str(candidate.get("index") or ""),
            str(candidate.get("lane") or ""),
            str(candidate.get("strategy_type") or ""),
            str(candidate.get("expiry") or ""),
            str(candidate.get("sellStrike") or ""),
            str(candidate.get("sellType") or ""),
            str(candidate.get("buyStrike") or ""),
            str(candidate.get("buyType") or ""),
            str(candidate.get("rejection_stage") or ""),
            str(candidate.get("rejection_reason") or ""),
            str(ordinal),
        ]
    )
    unsigned = _java_string_hash(raw) & 0xFFFFFFFF
    return f"rej_{unsigned:x}"


def _extract_a8_killed(snapshot: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    rejected = ctx.get("snapshot_rejected_candidates_full")
    if not isinstance(rejected, list):
        return []
    out: list[dict[str, Any]] = []
    poll_ts = str(snapshot.get("poll_ts") or "")
    for ordinal, row in enumerate(rejected):
        if not isinstance(row, dict):
            continue
        if str(row.get("rejection_stage") or "") == "ev_below_floor":
            candidate = dict(row)
            if not str(candidate.get("id") or candidate.get("candidate_id") or "").strip():
                candidate["candidate_id"] = _synthetic_rejected_id(poll_ts, candidate, ordinal)
            out.append(candidate)
    return out


def _fetch_outcome_rows(table: str, date_from: str, date_to: str) -> list[dict[str, Any]]:
    params = {
        "session_date": [f"gte.{date_from}", f"lte.{date_to}"],
        "select": "*",
        "order": "session_date.asc,created_at.asc",
        "limit": 1000,
    }
    rows = _supabase_get_with_retry(table, params)
    return [row for row in rows if isinstance(row, dict)]


def _supabase_get_with_retry(table: str, params: dict[str, Any], *, paged: bool = False) -> list[dict[str, Any]]:
    last_error: Exception | None = None
    for attempt in range(1, SUPABASE_RETRY_ATTEMPTS + 1):
        try:
            if paged:
                return hrh._supabase_get_page(table, params)
            return hrh._supabase_get(table, params)
        except Exception as exc:
            last_error = exc
            if attempt >= SUPABASE_RETRY_ATTEMPTS:
                break
            time.sleep(2 * attempt)
    raise RuntimeError(f"Supabase GET {table} failed after {SUPABASE_RETRY_ATTEMPTS} attempts: {last_error}") from last_error


def _fetch_snapshot_headers(session_date: str, page_size: int = 25) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = _supabase_get_with_retry(
            "ml_brain_snapshots",
            {
                "session_date": f"eq.{session_date}",
                "select": "id,poll_ts,session_date",
                "order": "poll_ts.asc",
                "limit": page_size,
                "offset": offset,
            },
            paged=True,
        )
        valid = [row for row in page if isinstance(row, dict)]
        rows.extend(valid)
        if len(page) < page_size:
            break
        offset += page_size
    return rows


def _fetch_snapshot_detail(snapshot_id: str) -> dict[str, Any] | None:
    page = _supabase_get_with_retry(
        "ml_brain_snapshots",
        {
            "id": f"eq.{snapshot_id}",
            "select": "id,poll_ts,session_date,context_json,top_candidates_json,primary_candidate_json",
            "limit": 1,
            "offset": 0,
        },
        paged=True,
    )
    if not page:
        return None
    row = page[0]
    return row if isinstance(row, dict) else None


def _fetch_snapshots_safely(session_date: str, *, offset: int = 0, limit: int = 0) -> list[dict[str, Any]]:
    headers = _fetch_snapshot_headers(session_date)
    if offset > 0:
        headers = headers[offset:]
    if limit > 0:
        headers = headers[:limit]
    rows: list[dict[str, Any]] = []
    for header in headers:
        snapshot_id = str(header.get("id") or "")
        if not snapshot_id:
            continue
        detail = _fetch_snapshot_detail(snapshot_id)
        if detail is not None:
            rows.append(detail)
    return rows


def _progress(enabled: bool, message: str) -> None:
    if enabled:
        print(message, file=sys.stderr, flush=True)


def _build_outcome_maps(date_from: str, date_to: str) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    eval_rows = _fetch_outcome_rows("ml_evaluation_outcomes", date_from, date_to)
    reco_rows = _fetch_outcome_rows("ml_recommendation_outcomes", date_from, date_to)
    eval_map: dict[tuple[str, str], dict[str, Any]] = {}
    reco_map: dict[tuple[str, str], dict[str, Any]] = {}
    for row in eval_rows:
        if str(row.get("trade_id") or "") in QUARANTINED_TRADE_IDS:
            continue
        key = (str(row.get("snapshot_id") or ""), str(row.get("candidate_id") or ""))
        if key[0] and key[1] and key not in eval_map:
            eval_map[key] = row
    for row in reco_rows:
        if str(row.get("trade_id") or "") in QUARANTINED_TRADE_IDS:
            continue
        key = (str(row.get("snapshot_id") or ""), str(row.get("candidate_id") or ""))
        if key[0] and key[1] and key not in reco_map:
            reco_map[key] = row
    return eval_map, reco_map


def _simulate_candidate(
    snapshots: list[dict[str, Any]],
    chain_rows: list[dict[str, Any]],
    snapshot: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any] | None:
    config = brain._teacher_default_config()
    # Rejected candidates can contain leg keys that were never present in the
    # generated menu, so snapshot-level prefiltering loses the very legs D3 must
    # replay. The teacher path builder filters candidate legs internally.
    return hrh._trace_candidate_path(chain_rows, snapshots, snapshot, candidate, config)


def _anchor_from_match(row: dict[str, Any], trace: dict[str, Any] | None, anchor_type: str) -> tuple[str, float | None, float | None, str, float | None]:
    anchor_net = _safe_float(row.get("sim_pnl_h2"))
    max_loss = _safe_float(row.get("max_loss"))
    if max_loss is None or max_loss <= 0:
        max_loss = None
    anchor_r = round(anchor_net / max_loss, 4) if anchor_net is not None and max_loss else None
    friction_total = None
    slippage_basis = "TEACHER_MATCH_NO_TRACE"
    if isinstance(trace, dict):
        path_rows = trace.get("path_rows") or []
        if path_rows:
            exit_idx = max(int(trace.get("outcome", {}).get("exit_step") or 1), 1) - 1
            exit_idx = min(exit_idx, len(path_rows) - 1)
            exit_row = path_rows[exit_idx]
            cost_breakdown = exit_row.get("cost_breakdown")
            friction_total = _safe_float((cost_breakdown or {}).get("total"))
            slippage_basis = _slippage_basis_from_cost_breakdown(cost_breakdown)
    return anchor_type, anchor_net, anchor_r, slippage_basis, friction_total


def _anchor_from_trace(trace: dict[str, Any] | None) -> tuple[str, float | None, float | None, str, float | None]:
    if not isinstance(trace, dict):
        return "pricing_failed", None, None, "PRICING_FAILED", None
    path_rows = trace.get("path_rows") or []
    if not path_rows:
        return "pricing_failed", None, None, "PRICING_FAILED", None
    exit_idx = max(int(trace.get("outcome", {}).get("exit_step") or 1), 1) - 1
    exit_idx = min(exit_idx, len(path_rows) - 1)
    exit_row = path_rows[exit_idx]
    cost_breakdown = exit_row.get("cost_breakdown")
    slippage_basis = _slippage_basis_from_cost_breakdown(cost_breakdown)
    friction_total = _safe_float((cost_breakdown or {}).get("total"))
    anchor_net = _safe_float(exit_row.get("net_pnl"))
    anchor_r = _safe_float(exit_row.get("unrealized_R"))
    return "simulated_trace", anchor_net, anchor_r, slippage_basis, friction_total


def _failure_class(cohort: str, anchor_type: str, outcome_positive: int | None) -> str:
    if anchor_type == "pricing_failed":
        return "F1_DATA_MISSING"
    if outcome_positive is None:
        return "F1_DATA_MISSING"
    if cohort == "A8_KILLED":
        return "F2_GATE_FALSE_NEGATIVE" if outcome_positive == 1 else "F3_GATE_CORRECT_REJECTION"
    return "F5_SURVIVOR_VALID" if outcome_positive == 1 else "F4_SURVIVOR_UNDERPERFORMANCE"


def _collect_for_session_date(
    session_date: str,
    eval_map: dict[tuple[str, str], dict[str, Any]],
    reco_map: dict[tuple[str, str], dict[str, Any]],
    *,
    progress_every: int = 0,
    max_snapshots: int = 0,
    snapshot_offset: int = 0,
) -> tuple[list[ReplayRow], dict[str, Any]]:
    progress_enabled = progress_every > 0
    _progress(progress_enabled, f"[d3] {session_date}: fetching snapshots")
    snapshots = _fetch_snapshots_safely(session_date, offset=snapshot_offset, limit=max_snapshots)
    if snapshot_offset > 0:
        _progress(progress_enabled, f"[d3] {session_date}: skipped first {snapshot_offset} snapshots")
    if max_snapshots > 0:
        _progress(progress_enabled, f"[d3] {session_date}: capped detail fetch to {len(snapshots)} snapshots")
    _progress(progress_enabled, f"[d3] {session_date}: fetched {len(snapshots)} snapshots; fetching chain rows")
    chain_rows = hrh._context_chain_rows_for_snapshots(snapshots)
    _progress(progress_enabled, f"[d3] {session_date}: fetched {len(chain_rows)} chain rows")
    rows: list[ReplayRow] = []
    coverage = {
        "session_date": session_date,
        "snapshots_fetched": len(snapshots),
        "snapshots_with_generated": 0,
        "snapshots_with_a8_killed": 0,
        "snapshots_out_of_window": 0,
        "teacher_matched_rows": 0,
        "recommendation_matched_rows": 0,
        "simulated_rows": 0,
        "pricing_failed_rows": 0,
        "candidate_rows": 0,
    }

    for snapshot_index, snapshot in enumerate(snapshots, start=1):
        ctx = _json_load(snapshot.get("context_json"), {})
        if not isinstance(ctx, dict):
            ctx = {}
        session_window = _session_window(str(snapshot.get("poll_ts") or ""))
        if session_window == "OUT_OF_WINDOW":
            coverage["snapshots_out_of_window"] += 1
            continue

        generated = _extract_generated_candidates(snapshot, ctx)
        a8_killed = _extract_a8_killed(snapshot, ctx)
        if generated:
            coverage["snapshots_with_generated"] += 1
        if a8_killed:
            coverage["snapshots_with_a8_killed"] += 1

        if progress_every > 0 and (snapshot_index == 1 or snapshot_index % progress_every == 0):
            _progress(
                True,
                "[d3] "
                + f"{session_date}: snapshot {snapshot_index}/{len(snapshots)} "
                + f"generated={len(generated)} a8_killed={len(a8_killed)} "
                + f"rows_so_far={coverage['candidate_rows']}",
            )

        for cohort, candidates in (("A8_SURVIVOR", generated), ("A8_KILLED", a8_killed)):
            for candidate in candidates:
                candidate_id = str(candidate.get("id") or candidate.get("candidate_id") or "")
                if not candidate_id:
                    continue
                trace = _simulate_candidate(snapshots, chain_rows, snapshot, candidate)
                match_key = (str(snapshot.get("id") or ""), candidate_id)
                if match_key in eval_map:
                    anchor_type, anchor_net, anchor_r, slippage_basis, friction_total = _anchor_from_match(
                        eval_map[match_key],
                        trace,
                        "teacher_eval_match",
                    )
                    coverage["teacher_matched_rows"] += 1
                    teacher_matched = 1
                    reco_matched = 0
                    simulated = 0
                    pricing_failed = 0
                elif match_key in reco_map:
                    anchor_type, anchor_net, anchor_r, slippage_basis, friction_total = _anchor_from_match(
                        reco_map[match_key],
                        trace,
                        "recommendation_match",
                    )
                    coverage["recommendation_matched_rows"] += 1
                    teacher_matched = 0
                    reco_matched = 1
                    simulated = 0
                    pricing_failed = 0
                else:
                    anchor_type, anchor_net, anchor_r, slippage_basis, friction_total = _anchor_from_trace(trace)
                    teacher_matched = 0
                    reco_matched = 0
                    simulated = 1 if anchor_type == "simulated_trace" else 0
                    pricing_failed = 1 if anchor_type == "pricing_failed" else 0
                    coverage["simulated_rows"] += simulated
                    coverage["pricing_failed_rows"] += pricing_failed

                outcome_positive = None if anchor_net is None else (1 if anchor_net > 0 else 0)
                coverage["candidate_rows"] += 1
                rows.append(
                    ReplayRow(
                        session_date=session_date,
                        snapshot_id=str(snapshot.get("id") or ""),
                        poll_ts_ist=_parse_ist_time_label(str(snapshot.get("poll_ts") or "")),
                        session_window=session_window,
                        candidate_id=candidate_id,
                        cohort=cohort,
                        index_key=_index_key(candidate),
                        side=_side(candidate),
                        strategy_family=_strategy_family(candidate),
                        premium_edge_bucket=_premium_edge_bucket(candidate),
                        vix_bucket=_vix_bucket(snapshot, ctx),
                        fii_short_trend=_fii_short_trend(ctx),
                        pcr_state=_pcr_state(snapshot, ctx, candidate),
                        wall_state=_wall_state(candidate),
                        anchor_type=anchor_type,
                        slippage_basis=slippage_basis,
                        friction_total=friction_total,
                        anchor_net_pnl=anchor_net,
                        anchor_r=anchor_r,
                        outcome_positive=outcome_positive,
                        failure_class=_failure_class(cohort, anchor_type, outcome_positive),
                        teacher_matched=teacher_matched,
                        reco_matched=reco_matched,
                        simulated=simulated,
                        pricing_failed=pricing_failed,
                    )
                )
    _progress(progress_enabled, f"[d3] {session_date}: completed {coverage['candidate_rows']} candidate rows")
    return rows, coverage


def _write_candidate_csv(path: Path, rows: list[ReplayRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(ReplayRow.__dataclass_fields__.keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)


def _branch_key(row: ReplayRow) -> tuple[str, ...]:
    return (
        row.session_window,
        row.index_key,
        row.side,
        row.strategy_family,
        row.premium_edge_bucket,
        row.vix_bucket,
        row.fii_short_trend,
        row.pcr_state,
        row.wall_state,
    )


def _write_markdown_report(path: Path, prereg_sha: str, coverage_rows: list[dict[str, Any]], replay_rows: list[ReplayRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# D3 Blocked-Candidate Replay Report")
    lines.append("")
    lines.append(f"- preregistration_sha: `{prereg_sha}`")
    lines.append(f"- generated_at_utc: `{datetime.now(timezone.utc).isoformat()}`")
    lines.append(f"- replay_row_count: `{len(replay_rows)}`")
    lines.append("")
    lines.append("## Coverage")
    lines.append("")
    for cov in coverage_rows:
        lines.append(
            "- "
            + ", ".join(
                [
                    f"session_date={cov['session_date']}",
                    f"snapshots_fetched={cov['snapshots_fetched']}",
                    f"snapshots_with_generated={cov['snapshots_with_generated']}",
                    f"snapshots_with_a8_killed={cov['snapshots_with_a8_killed']}",
                    f"candidate_rows={cov['candidate_rows']}",
                    f"teacher_matched_rows={cov['teacher_matched_rows']}",
                    f"recommendation_matched_rows={cov['recommendation_matched_rows']}",
                    f"simulated_rows={cov['simulated_rows']}",
                    f"pricing_failed_rows={cov['pricing_failed_rows']}",
                ]
            )
        )
    lines.append("")
    lines.append("## First Honest Table")
    lines.append("")
    lines.append("| session_window | index | side | strategy | premium_edge | vix | fii_short | pcr | wall | decision_days | candidate_rows | teacher_match | reco_match | simulated | pricing_failed | positive | avg_anchor_r | verdict |")
    lines.append("|---|---|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")

    grouped: dict[tuple[str, ...], list[ReplayRow]] = defaultdict(list)
    for row in replay_rows:
        grouped[_branch_key(row)].append(row)

    for key in sorted(grouped.keys()):
        items = grouped[key]
        decision_days = len({row.session_date for row in items})
        positive = sum(1 for row in items if row.outcome_positive == 1)
        anchor_r_values = [row.anchor_r for row in items if row.anchor_r is not None]
        avg_anchor_r = round(sum(anchor_r_values) / len(anchor_r_values), 4) if anchor_r_values else None
        verdict = "insufficient — no conclusion" if decision_days < 30 else "eligible_for_stats"
        lines.append(
            "| " + " | ".join(
                [
                    key[0],
                    key[1],
                    key[2],
                    key[3],
                    key[4],
                    key[5],
                    key[6],
                    key[7],
                    key[8],
                    str(decision_days),
                    str(len(items)),
                    str(sum(row.teacher_matched for row in items)),
                    str(sum(row.reco_matched for row in items)),
                    str(sum(row.simulated for row in items)),
                    str(sum(row.pricing_failed for row in items)),
                    str(positive),
                    "" if avg_anchor_r is None else f"{avg_anchor_r:.4f}",
                    verdict,
                ]
            ) + " |"
        )

    lines.append("")
    lines.append("## Failure Taxonomy")
    lines.append("")
    failure_counts: dict[str, int] = defaultdict(int)
    for row in replay_rows:
        failure_counts[row.failure_class] += 1
    for key in sorted(failure_counts.keys()):
        lines.append(f"- `{key}`: {failure_counts[key]}")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("- Outcome anchor preference order was: teacher evaluation match -> recommendation match -> simulated trace.")
    lines.append("- D7 removed signed probability-disagreement columns; `probProfit` is the canonical probability field.")
    lines.append("- Cells with fewer than 30 decision-days are explicitly non-conclusive by directive.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="D3 blocked-candidate replay analysis")
    parser.add_argument("--date-from", default="2026-07-21")
    parser.add_argument("--date-to", default="2026-07-21")
    parser.add_argument(
        "--candidate-csv",
        default=str(REPORTS_DIR / "d3_blocked_candidate_replay_rows.csv"),
    )
    parser.add_argument(
        "--report-md",
        default=str(REPORTS_DIR / "d3_blocked_candidate_replay_report.md"),
    )
    parser.add_argument(
        "--prereg-sha",
        required=True,
        help="Commit SHA that froze reports/D3_BLOCKED_CANDIDATE_REPLAY_PREREG_20260721.md",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=0,
        help="Print progress to stderr every N snapshots; 0 disables progress output.",
    )
    parser.add_argument(
        "--max-snapshots",
        type=int,
        default=0,
        help="Diagnostic cap for snapshot count; 0 means full replay.",
    )
    parser.add_argument(
        "--snapshot-offset",
        type=int,
        default=0,
        help="Skip the first N snapshots for chunked/restartable diagnostics.",
    )
    args = parser.parse_args()

    _progress(args.progress_every > 0, f"[d3] building outcome maps for {args.date_from}..{args.date_to}")
    eval_map, reco_map = _build_outcome_maps(args.date_from, args.date_to)
    _progress(
        args.progress_every > 0,
        f"[d3] outcome maps: teacher={len(eval_map)} recommendation={len(reco_map)}",
    )
    all_rows: list[ReplayRow] = []
    coverage_rows: list[dict[str, Any]] = []
    for session_date in hrh._iter_session_dates(args.date_from, args.date_to):
        rows, coverage = _collect_for_session_date(
            session_date,
            eval_map,
            reco_map,
            progress_every=args.progress_every,
            max_snapshots=args.max_snapshots,
            snapshot_offset=args.snapshot_offset,
        )
        all_rows.extend(rows)
        coverage_rows.append(coverage)

    _write_candidate_csv(Path(args.candidate_csv), all_rows)
    _write_markdown_report(Path(args.report_md), args.prereg_sha, coverage_rows, all_rows)
    print(
        json.dumps(
            {
                "ok": True,
                "date_from": args.date_from,
                "date_to": args.date_to,
                "replay_rows": len(all_rows),
                "candidate_csv": str(Path(args.candidate_csv).resolve()),
                "report_md": str(Path(args.report_md).resolve()),
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
