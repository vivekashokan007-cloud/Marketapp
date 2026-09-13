"""G8 — training export integrity contracts (offline / shadow only).

Defects repaired (file:line evidence on Marketapp tip ~f03cd2a / 2.6.41/b472):
  - MarketMLService.exportAppTrades used filter paper=eq.REAL (string) against a
    boolean paper column, and order=date.asc which is not the trades_v2 timestamp
    schema (exit_date / created_at). See MarketMLService.kt:~2065-2071.
  - exportCanonicalEvaluationInputs pulled independent 1000-row caps on outcomes
    and snapshots (MarketMLService.kt:~2085-2091; SupabaseClient
    fetchRecentEvaluationOutcomes default limit=1000; fetchRecentBrainSnapshots
    default was 200 but call site passes 1000). That does not guarantee a usable
    joined primary-decision cohort and silently truncates.

This module encodes the corrected query contract, deterministic page cursors,
and explicit incomplete-data status so callers never treat a capped page as a
complete cohort. Does not enable ml_train / online_update.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

# Correct PostgREST filter / order for trades_v2 paper training export.
# paper is boolean; REAL was a legacy string mistaken for an enum value.
PAPER_TRADES_FILTER = "status=eq.CLOSED&paper=eq.true"
LIVE_TRADES_FILTER = "status=eq.CLOSED&paper=eq.false"
# Prefer exit_date (close time); fall back key documented for callers without it.
TRADES_ORDER_ASC = "exit_date.asc,created_at.asc"
TRADES_ORDER_DESC = "exit_date.desc,created_at.desc"

DEFAULT_PAGE_SIZE = 500
STATUS_COMPLETE = "complete"
STATUS_INCOMPLETE = "incomplete_truncated"
STATUS_EMPTY = "empty"

DEFECT_EXPORT_PAPER_FILTER = {
    "id": "exportAppTrades_paper_eq_REAL",
    "file": "app/src/main/java/com/marketradar/app/MarketMLService.kt",
    "approx_line": 2069,
    "was": "paper=eq.REAL",
    "fixed": PAPER_TRADES_FILTER,
    "reason": "paper column is boolean; REAL is not a valid eq target",
}
DEFECT_EXPORT_DATE_ORDER = {
    "id": "exportAppTrades_date_asc",
    "file": "app/src/main/java/com/marketradar/app/MarketMLService.kt",
    "approx_line": 2070,
    "was": "date.asc",
    "fixed": TRADES_ORDER_ASC,
    "reason": "trades_v2 uses exit_date/created_at, not date",
}
DEFECT_INDEPENDENT_CAPS = {
    "id": "independent_1000_row_caps",
    "file": "app/src/main/java/com/marketradar/app/MarketMLService.kt",
    "approx_line": 2085,
    "was": "fetchRecentEvaluationOutcomes(1000) + fetchRecentBrainSnapshots(1000)",
    "fixed": "deterministic paging + explicit incomplete status",
    "reason": "independent caps do not guarantee joined primary decisions",
}


def trades_export_query(cohort: str = "paper", *, page_size: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
    """Return PostgREST select args for a typed paper/live closed-trade export."""
    mode = str(cohort or "paper").strip().lower()
    if mode in ("live", "broker", "real"):
        filt = LIVE_TRADES_FILTER
        cohort_name = "live"
    else:
        filt = PAPER_TRADES_FILTER
        cohort_name = "paper"
    return {
        "table": "trades_v2",
        "filter": filt,
        "order": TRADES_ORDER_ASC,
        "limit": int(page_size),
        "cohort": cohort_name,
        "paper_boolean": cohort_name == "paper",
    }


def page_cursor_key(row: Mapping[str, Any]) -> str:
    """Stable cursor from exit_date then created_at then id."""
    exit_date = str(row.get("exit_date") or row.get("closed_at") or "")
    created = str(row.get("created_at") or "")
    rid = str(row.get("id") or row.get("trade_id") or "")
    return f"{exit_date}|{created}|{rid}"


def sort_trades_chronologically(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic ascending order by exit_date, created_at, id."""
    def key(r: Mapping[str, Any]):
        return (
            str(r.get("exit_date") or r.get("closed_at") or ""),
            str(r.get("created_at") or ""),
            str(r.get("id") or r.get("trade_id") or ""),
        )
    return [dict(r) for r in sorted(rows, key=key)]


def filter_trades_by_paper_boolean(
    rows: Iterable[Mapping[str, Any]],
    *,
    paper: bool = True,
) -> list[dict[str, Any]]:
    """Keep rows matching boolean paper (never string REAL/PAPER enums)."""
    out: list[dict[str, Any]] = []
    for row in rows:
        val = row.get("paper")
        if isinstance(val, bool):
            if val is paper:
                out.append(dict(row))
            continue
        if val is None:
            # Fall back to execution_mode text only when paper column absent.
            mode = str(row.get("execution_mode") or row.get("trade_mode") or "").lower()
            is_paper = mode in ("", "paper", "paper_intraday", "sandbox")
            if is_paper is paper:
                out.append(dict(row))
            continue
        text = str(val).strip().lower()
        # Reject legacy mistaken REAL/PAPER string equality — coerce carefully.
        if text in ("1", "true", "yes", "t"):
            is_paper = True
        elif text in ("0", "false", "no", "f"):
            is_paper = False
        else:
            # Unknown non-boolean (e.g. "REAL") — exclude from typed cohort.
            continue
        if is_paper is paper:
            out.append(dict(row))
    return out


def paginate_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    page_index: int = 0,
) -> dict[str, Any]:
    """Deterministic page slice with explicit incomplete status (no silent truncate)."""
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    ordered = sort_trades_chronologically(rows)
    total = len(ordered)
    start = page_index * page_size
    end = start + page_size
    page = ordered[start:end]
    if total == 0:
        status = STATUS_EMPTY
    elif end < total or (len(page) == page_size and end == total and total > page_size):
        # If we filled a page and more exist beyond, incomplete for "full cohort".
        status = STATUS_INCOMPLETE if end < total else STATUS_COMPLETE
    else:
        status = STATUS_COMPLETE if end >= total else STATUS_INCOMPLETE
    if end < total:
        status = STATUS_INCOMPLETE
    truncated_at_cap = len(page) == page_size and end < total
    return {
        "rows": page,
        "page_index": page_index,
        "page_size": page_size,
        "returned": len(page),
        "total_available": total,
        "status": status,
        "truncated_at_cap": truncated_at_cap,
        "next_page_index": (page_index + 1) if end < total else None,
        "cursor_after": page_cursor_key(page[-1]) if page else None,
    }


def join_outcomes_snapshots_coverage(
    outcomes: Sequence[Mapping[str, Any]],
    snapshots: Sequence[Mapping[str, Any]],
    *,
    page_size: int | None = None,
) -> dict[str, Any]:
    """Join outcomes→snapshots by snapshot_id; report usable primary coverage.

    Independent caps on each side are surfaced as incomplete when either input
    length equals a known cap or when join yield is below outcome primary count.
    """
    snap_by_id = {}
    for s in snapshots:
        sid = str(s.get("id") or s.get("snapshot_id") or "")
        if sid:
            snap_by_id[sid] = dict(s)

    joined = []
    missing_snapshot = 0
    primary = 0
    primary_joined = 0
    for o in outcomes:
        role = str(o.get("role") or o.get("training_role") or "secondary").lower()
        is_primary = role in ("primary", "selected", "recommendation")
        if is_primary:
            primary += 1
        sid = str(o.get("snapshot_id") or "")
        snap = snap_by_id.get(sid)
        if snap is None:
            missing_snapshot += 1
            continue
        row = {"outcome": dict(o), "snapshot": snap}
        joined.append(row)
        if is_primary:
            primary_joined += 1

    capped_hint = False
    if page_size is not None:
        capped_hint = len(outcomes) >= page_size or len(snapshots) >= page_size
    # Also detect classic independent 1000 caps.
    if len(outcomes) == 1000 or len(snapshots) == 1000:
        capped_hint = True

    if not outcomes and not snapshots:
        status = STATUS_EMPTY
    elif capped_hint or missing_snapshot > 0 or (primary > 0 and primary_joined < primary):
        status = STATUS_INCOMPLETE
    else:
        status = STATUS_COMPLETE

    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "n_outcomes": len(outcomes),
                "n_snapshots": len(snapshots),
                "n_joined": len(joined),
                "primary": primary,
                "primary_joined": primary_joined,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()

    return {
        "joined": joined,
        "n_outcomes": len(outcomes),
        "n_snapshots": len(snapshots),
        "n_joined": len(joined),
        "n_primary": primary,
        "n_primary_joined": primary_joined,
        "n_missing_snapshot": missing_snapshot,
        "status": status,
        "capped_or_incomplete": status == STATUS_INCOMPLETE,
        "coverage_fingerprint": fingerprint,
        "note": (
            "Do not treat independent row caps as a complete decision cohort; "
            "status=incomplete_truncated requires further pages or a narrower window."
            if status == STATUS_INCOMPLETE
            else "Joined cohort coverage is complete for the provided inputs."
        ),
    }


def build_export_status_payload(
    *,
    kind: str,
    query: Mapping[str, Any] | None = None,
    page: Mapping[str, Any] | None = None,
    coverage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Serializable status written beside export JSON (no silent truncate)."""
    status = STATUS_COMPLETE
    if page and page.get("status"):
        status = str(page["status"])
    if coverage and coverage.get("status") == STATUS_INCOMPLETE:
        status = STATUS_INCOMPLETE
    if coverage and coverage.get("status") == STATUS_EMPTY and status == STATUS_COMPLETE:
        status = STATUS_EMPTY
    return {
        "kind": kind,
        "status": status,
        "query": dict(query or {}),
        "page": {k: v for k, v in dict(page or {}).items() if k != "rows"},
        "coverage": {
            k: v for k, v in dict(coverage or {}).items() if k != "joined"
        },
        "defects_addressed": [
            DEFECT_EXPORT_PAPER_FILTER["id"],
            DEFECT_EXPORT_DATE_ORDER["id"],
            DEFECT_INDEPENDENT_CAPS["id"],
        ],
        "training_enabled": False,
    }
