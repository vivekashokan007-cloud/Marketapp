#!/usr/bin/env python3
"""E1 in-context model bake-off preparation.

Read-only. Builds the frozen primary-label dataset from S1 shadow outcomes,
computes pre-model baselines, and records local package availability.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_ROOT = REPO_ROOT / "app" / "src" / "main" / "python"
if str(PY_ROOT) not in sys.path:
    sys.path.insert(0, str(PY_ROOT))

import ml_train  # type: ignore  # noqa: E402


ROOT = REPO_ROOT.parent
GRADLE_PROPS = REPO_ROOT / "gradle.properties"
OUT_DIR = REPO_ROOT / "reports" / "e1_bakeoff_20260723"
DATASET_CSV = OUT_DIR / "e1_primary_dataset.csv"
DATASET_JSONL = OUT_DIR / "e1_primary_dataset.jsonl"
REPORT_MD = OUT_DIR / "E1_PREP_REPORT_20260723.md"
MANIFEST_JSON = OUT_DIR / "e1_manifest.json"
SNAPSHOT_CACHE_JSONL = OUT_DIR / "snapshot_cache.jsonl"
PAGE_SIZE = int(os.environ.get("E1_PAGE_SIZE") or "250")
SLEEP_SEC = float(os.environ.get("E1_SLEEP_SEC") or "0.45")
SNAPSHOT_CHUNK_SIZE = int(os.environ.get("E1_SNAPSHOT_CHUNK_SIZE") or "5")
STOP_CODES = {429, 503, 504}


class ThrottleStop(RuntimeError):
    pass


def _load_gradle_property(name: str) -> str:
    text = GRADLE_PROPS.read_text()
    match = re.search(rf"^{re.escape(name)}=(.*)$", text, re.M)
    return match.group(1).strip() if match else ""


SUPABASE_URL = (os.environ.get("SUPABASE_URL") or _load_gradle_property("SUPABASE_URL")).rstrip("/")
SUPABASE_KEY = (
    os.environ.get("SUPABASE_SERVICE_KEY")
    or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_KEY")
    or _load_gradle_property("SUPABASE_ANON_KEY")
)


def _require_supabase() -> None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase URL/key unavailable")


def _request_json(path: str, params: dict[str, Any], *, timeout: int = 120) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{path}?{query}",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            payload = json.loads(raw) if raw else []
            if not isinstance(payload, list):
                raise RuntimeError(f"Unexpected Supabase payload for {path}")
            return [row for row in payload if isinstance(row, dict)]
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code in STOP_CODES or "57014" in body or "statement timeout" in body.lower():
            raise ThrottleStop(f"HTTP {exc.code} on {path}: {body}") from exc
        raise RuntimeError(f"HTTP {exc.code} on {path}: {body}") from exc


def _paged_rows(table: str, select: str, filters: dict[str, str], order: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = _request_json(
            table,
            {
                "select": select,
                "order": order,
                "limit": str(PAGE_SIZE),
                "offset": str(offset),
                **filters,
            },
        )
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE
        time.sleep(SLEEP_SEC)


def _chunked(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _append_snapshot_cache(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with SNAPSHOT_CACHE_JSONL.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n")


def _load_snapshot_cache() -> dict[int, dict[str, Any]]:
    cached: dict[int, dict[str, Any]] = {}
    if not SNAPSHOT_CACHE_JSONL.exists():
        return cached
    with SNAPSHOT_CACHE_JSONL.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                cached[int(row["id"])] = row
            except Exception:
                continue
    return cached


def _jsonish(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(default, dict) and isinstance(parsed, dict):
                return parsed
            if isinstance(default, list) and isinstance(parsed, list):
                return parsed
        except Exception:
            return default
    return default


def _bool_label(value: Any) -> int | None:
    if value is True or value == 1 or str(value).lower() in {"1", "true", "yes"}:
        return 1
    if value is False or value == 0 or str(value).lower() in {"0", "false", "no"}:
        return 0
    return None


def _normalize_outcome(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "session_date": row.get("effective_session_date") or row.get("session_date"),
        "snapshot_id": row.get("snapshot_id"),
        "candidate_id": row.get("candidate_id"),
        "role": row.get("role") or "primary",
        "sim_pnl_h2": row.get("new_sim_pnl_h2"),
        "outcome_h2": row.get("new_outcome_h2"),
        "canonical_won": row.get("new_canonical_won"),
        "managed_pnl": row.get("new_sim_pnl_h2"),
        "price_integrity": row.get("new_price_integrity"),
    }


def _candidate_from_snapshot(snapshot: dict[str, Any], candidate_id: Any) -> dict[str, Any]:
    primary = _jsonish(snapshot.get("primary_candidate_json"), {})
    candidate_id_text = str(candidate_id or "").strip()
    if isinstance(primary, dict):
        primary_id = str(primary.get("id") or primary.get("candidate_id") or "").strip()
        if not candidate_id_text or primary_id == candidate_id_text:
            return primary
    ctx = _jsonish(snapshot.get("context_json"), {})
    candidates = _jsonish(ctx.get("snapshot_generated_candidates"), [])
    if not candidates:
        candidates = _jsonish(snapshot.get("top_candidates_json"), [])
    for cand in candidates if isinstance(candidates, list) else []:
        if not isinstance(cand, dict):
            continue
        cand_id = str(cand.get("id") or cand.get("candidate_id") or "").strip()
        if cand_id == candidate_id_text:
            return cand
    return primary if isinstance(primary, dict) else {}


def _fetch_s1_primary_rows() -> list[dict[str, Any]]:
    return _paged_rows(
        "ml_evaluation_outcomes_s1",
        ",".join(
            [
                "id",
                "effective_session_date",
                "session_date",
                "snapshot_id",
                "candidate_id",
                "role",
                "new_sim_pnl_h2",
                "new_outcome_h2",
                "new_canonical_won",
                "new_price_integrity",
                "new_h2_price_integrity_reason",
                "regen_batch_id",
            ]
        ),
        {
            "role": "eq.primary",
            "new_price_integrity": "eq.OK",
            "new_canonical_won": "not.is.null",
        },
        "effective_session_date.asc,snapshot_id.asc",
    )


def _fetch_snapshots(snapshot_ids: list[int]) -> dict[int, dict[str, Any]]:
    out = _load_snapshot_cache()
    missing_ids = [sid for sid in snapshot_ids if sid not in out]
    if not missing_ids:
        return out
    chunk_size = max(1, SNAPSHOT_CHUNK_SIZE)
    for chunk in _chunked(missing_ids, chunk_size):
        ids = ",".join(str(i) for i in chunk)
        rows = _request_json(
            "ml_brain_snapshots",
            {
                "select": "id,poll_ts,session_date,primary_candidate_json,top_candidates_json,context_json,poll_summary_json",
                "id": f"in.({ids})",
                "order": "id.asc",
                "limit": str(len(chunk)),
            },
        )
        _append_snapshot_cache(rows)
        for row in rows:
            try:
                out[int(row["id"])] = row
            except Exception:
                continue
        time.sleep(SLEEP_SEC)
    return out


def _auc(labels: list[int], scores: list[float]) -> float | None:
    pairs = [(s, y) for s, y in zip(scores, labels) if s is not None and y in (0, 1)]
    pos = sum(1 for _, y in pairs if y == 1)
    neg = sum(1 for _, y in pairs if y == 0)
    if pos == 0 or neg == 0:
        return None
    pairs.sort(key=lambda item: item[0])
    rank_sum = 0.0
    i = 0
    while i < len(pairs):
        j = i + 1
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        rank_sum += avg_rank * sum(1 for _, y in pairs[i:j] if y == 1)
        i = j
    return (rank_sum - pos * (pos + 1) / 2.0) / (pos * neg)


def _log_loss(labels: list[int], scores: list[float]) -> float | None:
    vals = []
    for y, s in zip(labels, scores):
        if y not in (0, 1) or s is None:
            continue
        p = min(max(float(s), 1e-6), 1.0 - 1e-6)
        vals.append(-(y * math.log(p) + (1 - y) * math.log(1 - p)))
    return sum(vals) / len(vals) if vals else None


def _brier(labels: list[int], scores: list[float]) -> float | None:
    vals = []
    for y, s in zip(labels, scores):
        if y in (0, 1) and s is not None:
            vals.append((float(s) - y) ** 2)
    return sum(vals) / len(vals) if vals else None


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return float(value)
    except Exception:
        return None


def _candidate_model_score(row: dict[str, Any]) -> float | None:
    for key in ("p_ml", "ml_prob", "prob_ml", "model_prob", "mlProbability", "pWin", "probProfit"):
        val = _safe_float(row.get(key))
        if val is None:
            continue
        if val > 1.0:
            val /= 100.0
        return min(max(val, 0.0), 1.0)
    return None


def _paired_labels_scores(labels: list[int | None], scores: list[float | None]) -> tuple[list[int], list[float]]:
    paired = [(int(y), float(s)) for y, s in zip(labels, scores) if y in (0, 1) and s is not None]
    return [y for y, _ in paired], [s for _, s in paired]


def _walk_forward_base_rate(rows: list[dict[str, Any]]) -> list[float | None]:
    history: list[int] = []
    preds: list[float | None] = []
    last_date = None
    day_buffer: list[int] = []
    for row in rows:
        row_date = row["date"]
        if last_date is not None and row_date != last_date:
            history.extend(day_buffer)
            day_buffer = []
        pred = (sum(history) / len(history)) if history else None
        preds.append(pred)
        label = _bool_label(row.get("canonical_won"))
        if label is not None:
            day_buffer.append(label)
        last_date = row_date
    return preds


def _package_probe() -> dict[str, dict[str, Any]]:
    probes = {}
    for name in ("tabicl", "tabpfn", "torch", "sklearn", "numpy", "pandas"):
        spec = importlib.util.find_spec(name)
        probes[name] = {"available": spec is not None, "origin": spec.origin if spec else None}
    return probes


def main() -> None:
    _require_supabase()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    s1_rows = _fetch_s1_primary_rows()
    snapshot_ids = sorted({int(r["snapshot_id"]) for r in s1_rows if r.get("snapshot_id") is not None})
    snapshots = _fetch_snapshots(snapshot_ids)

    dataset: list[dict[str, Any]] = []
    skipped = Counter()
    for row in s1_rows:
        outcome = _normalize_outcome(row)
        try:
            sid = int(outcome["snapshot_id"])
        except Exception:
            skipped["missing_snapshot_id"] += 1
            continue
        snap = snapshots.get(sid)
        if not snap:
            skipped["snapshot_not_found"] += 1
            continue
        ctx = _jsonish(snap.get("context_json"), {})
        cand = _candidate_from_snapshot(snap, outcome.get("candidate_id"))
        feature_row = ml_train._snapshot_candidate_to_row(cand, ctx, outcome, snap)
        if not feature_row:
            skipped["feature_row_none"] += 1
            continue
        feature_row["effective_session_date"] = outcome["session_date"]
        feature_row["source_s1_id"] = row.get("id")
        feature_row["regen_batch_id"] = row.get("regen_batch_id")
        feature_row["frozen_model_score"] = _candidate_model_score(cand)
        dataset.append(feature_row)

    dataset.sort(key=lambda r: (str(r.get("effective_session_date") or r.get("date") or ""), str(r.get("snapshot_id") or "")))
    labels = [_bool_label(r.get("canonical_won")) for r in dataset]
    numeric_labels = [int(x) for x in labels if x in (0, 1)]
    base_preds = _walk_forward_base_rate(dataset)
    frozen_scores = [_candidate_model_score(r) for r in dataset]
    base_labels, base_scores = _paired_labels_scores(labels, base_preds)
    frozen_labels, frozen_model_scores = _paired_labels_scores(labels, frozen_scores)
    fieldnames = sorted({key for row in dataset for key in row.keys()})

    with DATASET_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(dataset)
    with DATASET_JSONL.open("w", encoding="utf-8") as f:
        for row in dataset:
            f.write(json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n")

    dataset_sha = hashlib.sha256(DATASET_JSONL.read_bytes()).hexdigest()
    by_day = Counter(str(r.get("effective_session_date") or r.get("date") or "") for r in dataset)
    wins_by_day = defaultdict(int)
    for row in dataset:
        if _bool_label(row.get("canonical_won")) == 1:
            wins_by_day[str(row.get("effective_session_date") or row.get("date") or "")] += 1

    manifest = {
        "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dataset_csv": str(DATASET_CSV),
        "dataset_jsonl": str(DATASET_JSONL),
        "dataset_sha256": dataset_sha,
        "rows": len(dataset),
        "source_s1_rows": len(s1_rows),
        "snapshot_ids": len(snapshot_ids),
        "snapshots_fetched": len(snapshots),
        "skipped": dict(skipped),
        "feature_count": len(fieldnames),
        "label_counts": dict(Counter(str(x) for x in labels)),
        "package_probe": _package_probe(),
        "base_rate_baseline": {
            "auc": _auc(base_labels, base_scores),
            "log_loss": _log_loss(base_labels, base_scores),
            "brier": _brier(base_labels, base_scores),
            "coverage_rows": len(base_scores),
        },
        "frozen_model_baseline": {
            "auc": _auc(frozen_labels, frozen_model_scores),
            "log_loss": _log_loss(frozen_labels, frozen_model_scores),
            "brier": _brier(frozen_labels, frozen_model_scores),
            "coverage_rows": len(frozen_model_scores),
        },
    }
    MANIFEST_JSON.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = [
        "# E1 Prep Report - 2026-07-23",
        "",
        "## Scope",
        "",
        "- Offline E1 prep only.",
        "- No phone code.",
        "- No live ranking authority.",
        "- Source table: `ml_evaluation_outcomes_s1`.",
        "- Filter: `role=primary`, `new_price_integrity=OK`, `new_canonical_won not null`.",
        "",
        "## Dataset",
        "",
        f"- Rows: `{len(dataset)}`",
        f"- Source S1 rows: `{len(s1_rows)}`",
        f"- Distinct days: `{len(by_day)}`",
        f"- Feature columns: `{len(fieldnames)}`",
        f"- Dataset SHA256: `{dataset_sha}`",
        f"- CSV: `{DATASET_CSV}`",
        f"- JSONL: `{DATASET_JSONL}`",
        f"- Skipped: `{dict(skipped)}`",
        "",
        "## Label Base",
        "",
        f"- Wins: `{sum(numeric_labels)}`",
        f"- Losses: `{len(numeric_labels) - sum(numeric_labels)}`",
        f"- Win rate: `{round((sum(numeric_labels) / len(numeric_labels)) * 100, 2) if numeric_labels else None}%`",
        "",
        "## Days",
        "",
    ]
    for day in sorted(by_day):
        wins = wins_by_day[day]
        total = by_day[day]
        lines.append(f"- `{day}`: rows `{total}`, wins `{wins}`, win_rate `{round(wins * 100.0 / total, 2) if total else 0.0}`")
    lines.extend(
        [
            "",
            "## Baselines",
            "",
            f"- Walk-forward base-rate: `{manifest['base_rate_baseline']}`",
            f"- Frozen model score availability: `{manifest['frozen_model_baseline']}`",
            "",
            "## Package Probe",
            "",
        ]
    )
    for name, probe in manifest["package_probe"].items():
        lines.append(f"- `{name}`: available `{probe['available']}`, origin `{probe['origin']}`")
    lines.extend(
        [
            "",
            "## Self-Audit - Deviations From Pre-Registration",
            "",
            "- TabICL/TabPFN model runs were not started in this prep step.",
            "- TabFM ceiling run was not started.",
            "- This step freezes the dataset and baselines only.",
            "- If a package is unavailable locally, installation/runtime feasibility must be handled as the next E1 step before any result claim.",
        ]
    )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(REPORT_MD), "manifest": str(MANIFEST_JSON), "rows": len(dataset), "sha256": dataset_sha}, indent=2))


if __name__ == "__main__":
    main()
