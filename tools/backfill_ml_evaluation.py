#!/usr/bin/env python3
"""
backfill_ml_evaluation.py — server-side ML-evaluation backfill for Market Radar.

WHY THIS EXISTS
    On 2026-08-31 the app produced 77 complete brain snapshots and 38,688 complete
    option-chain rows, and ZERO rows in ml_evaluation_outcomes. Root cause:
    SupabaseClient.normalizedChainRow() dropped bid/ask, and since v2.6.3 the teacher
    runs require_executable_quotes=True / allow_ltp_quote_fallback=False, so every
    candidate failed closed with `entry_round_trip_cost_unavailable`.

    This backfill reads chain rows STRAIGHT FROM SUPABASE (ml_option_chain_snapshots),
    where bid/ask are intact, so it bypasses the app bug entirely. It works today,
    with no app update — the v2.6.11 fix only prevents FUTURE blackouts.

WHAT IT DOES
    1. Pulls ml_brain_snapshots for the target date.
    2. Pulls ml_option_chain_snapshots for the target date (raw — bid/ask intact).
    3. Runs brain.evening_evaluator() — the exact same pure-Python grader the app uses.
    4. Writes outcomes into ml_evaluation_outcomes / ml_recommendation_outcomes /
       ml_rejected_candidate_outcomes, mirroring SupabaseClient.buildEvaluationRows /
       buildRecommendationRows / buildRejectedEvaluationRows column-for-column, with the
       SAME on_conflict keys — so a row written here is byte-compatible with a normal
       session and re-running is idempotent.

FAITHFULNESS
    Column sets, role routing, price_integrity=FAIL sanitisation and the on_conflict
    keys are copied from app/src/main/java/com/marketradar/app/SupabaseClient.kt as of
    v2.6.11. If that file changes, re-check the constants below.

USAGE
    export SUPABASE_URL=https://fdynxkfxohbnlvayouje.supabase.co
    export SUPABASE_SERVICE_KEY=<service_role_key>          # service role: bypasses RLS for writes
    # dry run (no writes) — prints exactly what WOULD be written:
    python3 backfill_ml_evaluation.py --date 2026-08-31
    # commit:
    python3 backfill_ml_evaluation.py --date 2026-08-31 --write

    Run it from a checkout so `brain` is importable, or pass
      --brain-dir app/src/main/python
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone

# ── Column contracts, copied verbatim from SupabaseClient.kt (v2.6.11) ────────────
OUTCOME_BASE_COLUMNS = [
    "snapshot_id", "session_date", "candidate_id", "lane", "index_key",
    "trade_mode", "strategy_type", "role", "sim_pnl_h2", "outcome_h2", "canonical_won",
]
SHADOW_TEACHER_KEYS = [
    "managed_pnl", "managed_gross_pnl", "friction_cost", "exit_reason", "exit_step",
    "exit_ts", "r_multiple", "captured_pct", "peak_pnl", "trough_pnl", "max_capture_pct",
    "near_target_pct", "target_gap_pnl", "time_to_peak_step", "target_was_reached",
    "is_success", "risk_at_entry", "regime_bucket", "label_version",
    "teacher_config_version", "tp_threshold", "sl_threshold", "break_even_win_rate_pct",
    "price_integrity", "h2_price_integrity_reason", "h2_later_value_points",
    "h2_entry_basis_points", "h2_bound_width_points", "h2_formula",
]
OUTCOME_FULL_COLUMNS = OUTCOME_BASE_COLUMNS + SHADOW_TEACHER_KEYS + ["created_at"]

REJECTED_COLUMNS = [
    "id", "snapshot_id", "session_date", "poll_ts", "candidate_id", "lane", "index_key",
    "trade_mode", "strategy_type", "role", "sim_pnl_h2", "outcome_h2", "canonical_won",
    "managed_pnl", "managed_gross_pnl", "friction_cost", "exit_reason", "exit_step",
    "exit_ts", "path_points_count", "r_multiple", "captured_pct", "is_success",
    "peak_pnl", "trough_pnl", "max_capture_pct", "near_target_pct", "target_gap_pnl",
    "time_to_peak_step", "target_was_reached", "risk_at_entry", "regime_bucket",
    "label_version", "teacher_config_version", "tp_threshold", "sl_threshold",
    "break_even_win_rate_pct", "price_integrity", "h2_price_integrity_reason",
    "premium_edge", "credit_width_ratio", "sigma_otm", "created_at",
]
# Fields stripped when price_integrity == FAIL (sanitizeFailedIntegrityTeacherRow)
FAIL_INTEGRITY_STRIP = [
    "managed_pnl", "managed_gross_pnl", "friction_cost", "r_multiple", "captured_pct",
    "peak_pnl", "trough_pnl", "max_capture_pct", "near_target_pct", "target_gap_pnl",
    "time_to_peak_step", "target_was_reached", "is_success",
]

CHUNK = 250  # rows per PostgREST request; deliberately conservative for Supabase.
WRITE_DELAY_SECONDS = 0.75
READ_DELAY_SECONDS = 0.10
SNAPSHOT_READ_PAGE_SIZE = 10
CHAIN_READ_PAGE_SIZE = 250
MAX_RETRY_ATTEMPTS = 5
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
CHECKPOINT_TOOL_VERSION = "snapshot-v1"


# ── Supabase REST helpers (stdlib only) ───────────────────────────────────────────
def _headers(key, write=False):
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if write:
        h["Prefer"] = "resolution=merge-duplicates,return=minimal"
    return h


def _retry_delay(error, attempt):
    retry_after = error.headers.get("Retry-After") if getattr(error, "headers", None) else None
    if retry_after:
        try:
            return min(float(retry_after), 60.0)
        except ValueError:
            pass
    return min(1.5 * (2 ** attempt), 30.0)


def request_with_retry(request, timeout, operation):
    """Run a bounded, retry-safe REST request without hiding permanent errors."""
    for attempt in range(MAX_RETRY_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_HTTP_CODES or attempt == MAX_RETRY_ATTEMPTS - 1:
                detail = error.read().decode(errors="replace")[:400]
                raise RuntimeError(f"{operation} failed with HTTP {error.code}: {detail}") from error
            delay = _retry_delay(error, attempt)
            sys.stderr.write(
                f"[{operation}] HTTP {error.code}; retrying in {delay:.1f}s "
                f"({attempt + 1}/{MAX_RETRY_ATTEMPTS})\n"
            )
            time.sleep(delay)
        except urllib.error.URLError as error:
            if attempt == MAX_RETRY_ATTEMPTS - 1:
                raise RuntimeError(f"{operation} failed after retries: {error.reason}") from error
            delay = min(1.5 * (2 ** attempt), 30.0)
            sys.stderr.write(
                f"[{operation}] network error; retrying in {delay:.1f}s "
                f"({attempt + 1}/{MAX_RETRY_ATTEMPTS})\n"
            )
            time.sleep(delay)


def get_all(base, key, table, query, page_size=CHAIN_READ_PAGE_SIZE):
    """Paginated GET via Range headers."""
    rows, offset, page = [], 0, page_size
    while True:
        url = f"{base}/rest/v1/{table}?{query}"
        req = urllib.request.Request(url, headers={
            **_headers(key),
            "Range-Unit": "items",
            "Range": f"{offset}-{offset + page - 1}",
        })
        payload = request_with_retry(req, timeout=120, operation=f"read {table} offset={offset}")
        batch = json.loads(payload.decode())
        rows.extend(batch)
        if offset and offset % (page * 20) == 0:
            print(f"read {table}: {len(rows)} rows")
        if len(batch) < page:
            break
        offset += page
        time.sleep(READ_DELAY_SECONDS)
    return rows


def upsert(base, key, table_with_conflict, rows, write):
    if not rows:
        return 0, "empty"
    if not write:
        return len(rows), "dry-run"
    url = f"{base}/rest/v1/{table_with_conflict}"
    total = 0
    for i in range(0, len(rows), CHUNK):
        chunk = rows[i:i + CHUNK]
        data = json.dumps(chunk).encode()
        req = urllib.request.Request(url, data=data, headers=_headers(key, write=True), method="POST")
        request_with_retry(
            req,
            timeout=180,
            operation=f"upsert {table_with_conflict} rows={i}-{i + len(chunk) - 1}",
        )
        total += len(chunk)
        if i + CHUNK < len(rows):
            time.sleep(WRITE_DELAY_SECONDS)
    return total, "written"


# ── Row builders (mirror the Kotlin builders exactly) ─────────────────────────────
def _project(outcome, columns, now_iso, session_date):
    row = {}
    for col in columns:
        if col == "created_at":
            row[col] = now_iso
            continue
        if col == "session_date":
            row[col] = session_date
            continue
        v = outcome.get(col)
        if v is not None:
            row[col] = v
    return row


def build_eval_rows(outcomes, now_iso, session_date):
    """ml_evaluation_outcomes — roles primary+secondary only."""
    out = []
    for o in outcomes:
        role = (o.get("role") or "secondary")
        if role not in ("primary", "secondary"):
            continue
        row = _project(o, OUTCOME_FULL_COLUMNS, now_iso, session_date)
        row["role"] = role
        if str(row.get("price_integrity", "")).upper() == "FAIL":
            for k in FAIL_INTEGRITY_STRIP:
                row.pop(k, None)
        # required keys
        if row.get("snapshot_id") is None or row.get("candidate_id") is None:
            continue
        out.append(row)
    return out


def build_reco_rows(outcomes, now_iso, session_date):
    """ml_recommendation_outcomes — everything except rejected."""
    out = []
    for o in outcomes:
        role = (o.get("role") or "secondary")
        if role == "rejected":
            continue
        row = _project(o, OUTCOME_FULL_COLUMNS, now_iso, session_date)
        row["role"] = role
        if str(row.get("price_integrity", "")).upper() == "FAIL":
            for k in FAIL_INTEGRITY_STRIP:
                row.pop(k, None)
        if row.get("snapshot_id") is None or row.get("candidate_id") is None:
            continue
        out.append(row)
    return out


def _rejected_id(session_date, o, idx):
    snap = str(o.get("snapshot_id") or "snapshot_unknown")
    cand = str(o.get("candidate_id") or f"candidate_{idx}")
    label = str(o.get("label_version") or "teacher_v1")
    raw = f"{session_date}:{snap}:{cand}:{label}"
    return "_".join(raw.split())[:300]


def build_rejected_rows(outcomes, now_iso, session_date):
    """ml_rejected_candidate_outcomes — role rejected only, deterministic id."""
    out = []
    for idx, o in enumerate(outcomes):
        if (o.get("role") or "").strip().lower() != "rejected":
            continue
        row = _project(o, REJECTED_COLUMNS, now_iso, session_date)
        row["id"] = _rejected_id(session_date, o, idx)
        row["snapshot_id"] = str(o.get("snapshot_id") or "snapshot_unknown")
        row["candidate_id"] = str(o.get("candidate_id") or f"candidate_{idx}")
        row["role"] = "rejected"
        out.append(row)
    return out


def _candidate_scope_pairs(brain, snap):
    """Return every index/expiry pair reachable by the evaluator for one snapshot.

    The evaluator only reads rows matching a candidate's own index and expiry. Keeping
    those pairs lets the backfill avoid re-scanning an unrelated full-day chain for
    every candidate without changing the data visible to any evaluated candidate.
    """
    snap = snap if isinstance(snap, dict) else {}
    candidates = []
    primary = brain._safe_json_field(snap.get("primary_candidate_json", "{}"), {})
    if isinstance(primary, dict):
        candidates.append(primary)

    context = brain._safe_json_field(snap.get("context_json", "{}"), {})
    context = context if isinstance(context, dict) else {}
    generated, _ = brain._snapshot_candidate_menu_for_evaluation(snap, context)
    candidates.extend(candidate for candidate in generated if isinstance(candidate, dict))

    supply_shadow = context.get("snapshot_pc2_supply_quality_shadow")
    if isinstance(supply_shadow, dict):
        candidates.extend(
            candidate for candidate in (supply_shadow.get("sample_candidates") or [])
            if isinstance(candidate, dict)
        )

    rejected = context.get("snapshot_rejected_candidates_full")
    if not isinstance(rejected, list) or not rejected:
        rejected = context.get("snapshot_rejected_candidates")
    if isinstance(rejected, list):
        preselection = context.get("snapshot_rejected_candidate_selection")
        if isinstance(preselection, dict) and preselection.get("selected") == len(rejected):
            selected_rejected = rejected
        else:
            selected_rejected, _ = brain._select_rejected_candidates_for_eval(rejected)
        candidates.extend(candidate for candidate in selected_rejected if isinstance(candidate, dict))

    pairs = set()
    for candidate in candidates:
        index_key = candidate.get("index") or candidate.get("index_key") or "BNF"
        expiry = str(candidate.get("expiry") or "").strip()
        if expiry:
            pairs.add((str(index_key), expiry))
    return pairs


def _chain_by_scope(chain_rows):
    scoped = {}
    for row in chain_rows:
        index_key = str(row.get("index_key") or "")
        expiry = str(row.get("expiry") or "").strip()
        scoped.setdefault((index_key, expiry), []).append(row)
    return scoped


def _scoped_chain_for_snapshot(brain, snap, chains_by_scope):
    rows = []
    for pair in _candidate_scope_pairs(brain, snap):
        rows.extend(chains_by_scope.get(pair, []))
    return rows


def _snapshot_checkpoint_key(snap):
    snap = snap if isinstance(snap, dict) else {}
    for field in ("snapshot_id", "id", "poll_ts"):
        value = snap.get(field)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return hashlib.sha1(json.dumps(snap, sort_keys=True, default=str).encode()).hexdigest()


def _checkpoint_path(checkpoint_dir, snapshot_key):
    digest = hashlib.sha1(snapshot_key.encode()).hexdigest()
    return os.path.join(checkpoint_dir, f"snapshot-{digest}.json")


def _read_snapshot_checkpoint(checkpoint_dir, date, snapshot_key):
    path = _checkpoint_path(checkpoint_dir, snapshot_key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("tool_version") != CHECKPOINT_TOOL_VERSION:
        return None
    if payload.get("date") != date or payload.get("snapshot_key") != snapshot_key:
        return None
    outcomes = payload.get("outcomes")
    drops = payload.get("teacher_drop_reasons") or {}
    if not isinstance(outcomes, list) or not isinstance(drops, dict):
        return None
    return {"outcomes": outcomes, "teacher_drop_reasons": drops}


def _write_snapshot_checkpoint(checkpoint_dir, date, snapshot_key, result):
    os.makedirs(checkpoint_dir, exist_ok=True)
    path = _checkpoint_path(checkpoint_dir, snapshot_key)
    tmp_path = f"{path}.tmp"
    payload = {
        "tool_version": CHECKPOINT_TOOL_VERSION,
        "date": date,
        "snapshot_key": snapshot_key,
        "outcomes": result.get("outcomes") or [],
        "teacher_drop_reasons": result.get("teacher_drop_reasons") or {},
    }
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
    os.replace(tmp_path, path)


# ── Main ──────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Backfill ML evaluation outcomes for one session date.")
    ap.add_argument("--date", help="Session date YYYY-MM-DD (default: yesterday IST).")
    ap.add_argument("--write", action="store_true", help="Actually write. Omit for a dry run.")
    ap.add_argument("--brain-dir", default="app/src/main/python", help="Dir containing brain.py.")
    ap.add_argument(
        "--checkpoint-dir",
        help="Optional resumable checkpoint directory. Defaults to /tmp/marketapp-backfill-<date>-checkpoint for writes.",
    )
    args = ap.parse_args()

    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not base or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SERVICE_KEY (service role).")

    if args.date:
        date = args.date
    else:
        ist = timezone(timedelta(hours=5, minutes=30))
        date = (datetime.now(ist) - timedelta(days=1)).strftime("%Y-%m-%d")
    checkpoint_dir = args.checkpoint_dir or os.path.join("/tmp", f"marketapp-backfill-{date}-checkpoint")

    sys.path.insert(0, args.brain_dir)
    import brain  # noqa

    print(f"== ML evaluation backfill for {date} ==  (write={args.write})")

    snaps = get_all(base, key, "ml_brain_snapshots",
                    f"session_date=eq.{date}&order=poll_ts.asc&select=*",
                    page_size=SNAPSHOT_READ_PAGE_SIZE)
    chain = get_all(base, key, "ml_option_chain_snapshots",
                    # The historic source date is immutable and the evaluator groups by
                    # poll_ts itself. Avoiding an unnecessary database sort keeps the
                    # large raw-chain read below Supabase's response-time limits.
                    f"session_date=eq.{date}&"
                    "select=index_key,strike,option_type,expiry,poll_ts,ltp,bid,ask,session_date",
                    page_size=CHAIN_READ_PAGE_SIZE)
    print(f"snapshots={len(snaps)}  chain_rows={len(chain)}  "
          f"chain_null_bid={sum(1 for r in chain if r.get('bid') is None)}")
    if not snaps:
        sys.exit("No snapshots for that date — nothing to do.")
    if not chain:
        sys.exit("No chain rows for that date — cannot grade. Aborting (fail closed).")

    # Normalise poll_ts to the ISO 'Z' form brain expects (PostgREST returns +00:00).
    for r in chain:
        ts = str(r.get("poll_ts", ""))
        if ts.endswith("+00:00"):
            r["poll_ts"] = ts[:-6] + "Z"

    cfg = brain._teacher_default_config()
    print(f"teacher: require_executable_quotes={cfg.get('require_executable_quotes', True)} "
          f"allow_ltp_quote_fallback={cfg.get('allow_ltp_quote_fallback', False)}")

    chains_by_scope = _chain_by_scope(chain)
    first_scoped_chain = _scoped_chain_for_snapshot(brain, snaps[0], chains_by_scope)
    print(f"scope proof: first snapshot sees {len(first_scoped_chain)}/{len(chain)} chain rows")
    full_first = brain._evaluate_snapshot_outcomes(snaps[0], chain, cfg)
    scoped_first = brain._evaluate_snapshot_outcomes(snaps[0], first_scoped_chain, cfg)
    if json.dumps(full_first, sort_keys=True, default=str) != json.dumps(scoped_first, sort_keys=True, default=str):
        sys.exit("Scoped-chain proof failed: refusing to grade or write different evaluator results.")
    print("scope proof: PASS (full-chain and scoped-chain results match)")

    # Grade snapshot-by-snapshot so one bad snapshot can't sink the batch.
    outcomes, graded_snaps, drops, checkpoint_hits = [], 0, {}, 0
    for snapshot_number, snap in enumerate(snaps, start=1):
        snapshot_key = _snapshot_checkpoint_key(snap)
        res = None
        if args.write:
            res = _read_snapshot_checkpoint(checkpoint_dir, date, snapshot_key)
            if res is not None:
                checkpoint_hits += 1
        if res is None:
            scoped_chain = _scoped_chain_for_snapshot(brain, snap, chains_by_scope)
            res = scoped_first if snapshot_number == 1 else brain._evaluate_snapshot_outcomes(snap, scoped_chain, cfg)
            if args.write:
                _write_snapshot_checkpoint(checkpoint_dir, date, snapshot_key, res)
        rows = res.get("outcomes") or []
        if rows:
            graded_snaps += 1
        outcomes.extend(rows)
        for k, v in (res.get("teacher_drop_reasons") or {}).items():
            drops[k] = drops.get(k, 0) + int(v or 0)
        if snapshot_number % 5 == 0 or snapshot_number == len(snaps):
            print(f"graded snapshots: {snapshot_number}/{len(snaps)} outcomes={len(outcomes)}")

    if args.write:
        print(f"checkpoint: {checkpoint_hits}/{len(snaps)} snapshots reused from {checkpoint_dir}")

    by_role = {}
    for o in outcomes:
        by_role[o.get("role")] = by_role.get(o.get("role"), 0) + 1
    print(f"graded_snapshots={graded_snaps}/{len(snaps)}  outcomes={len(outcomes)}  by_role={by_role}")
    print(f"teacher_drop_reasons={drops}")

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    eval_rows = build_eval_rows(outcomes, now_iso, date)
    reco_rows = build_reco_rows(outcomes, now_iso, date)
    rej_rows = build_rejected_rows(outcomes, now_iso, date)
    print(f"rows -> ml_evaluation_outcomes={len(eval_rows)}  "
          f"ml_recommendation_outcomes={len(reco_rows)}  "
          f"ml_rejected_candidate_outcomes={len(rej_rows)}")

    if not eval_rows:
        sys.exit("Refusing to proceed: 0 eval rows built. Investigate before writing.")

    n1, s1 = upsert(base, key, "ml_evaluation_outcomes?on_conflict=snapshot_id,candidate_id,role", eval_rows, args.write)
    n2, s2 = upsert(base, key, "ml_recommendation_outcomes?on_conflict=snapshot_id,candidate_id,role", reco_rows, args.write)
    n3, s3 = upsert(base, key, "ml_rejected_candidate_outcomes?on_conflict=id", rej_rows, args.write)
    print(f"ml_evaluation_outcomes: {n1} ({s1})")
    print(f"ml_recommendation_outcomes: {n2} ({s2})")
    print(f"ml_rejected_candidate_outcomes: {n3} ({s3})")

    if args.write:
        remaining = get_all(base, key, "ml_evaluation_outcomes",
                            f"session_date=eq.{date}&select=role",
                            page_size=CHAIN_READ_PAGE_SIZE)
        print(f"VERIFY: ml_evaluation_outcomes now has {len(remaining)} rows for {date}")
        if os.path.isdir(checkpoint_dir):
            shutil.rmtree(checkpoint_dir)
            print(f"checkpoint: removed {checkpoint_dir}")
        print("Next: recompute ml_daily_accuracy for this date (the app's accuracy loop, "
              "or your daily-accuracy job) so the dashboards pick it up.")
    else:
        print("\nDRY RUN complete. Re-run with --write to commit.")


if __name__ == "__main__":
    main()
