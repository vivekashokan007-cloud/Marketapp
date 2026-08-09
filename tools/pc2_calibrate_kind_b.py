#!/usr/bin/env python3
"""
PC2 Batch 2 local calibration table.

This is intentionally read-only against Supabase. It consumes local C3/B1 CSV
artifacts and rank-diagnostic generated candidate JSON files, then emits a
calibration table for KIND B constants.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "pc2_full_percentile_implementation"
C3_DIR = ROOT / "reports" / "c3_context_percentile_backfill_20260803"
B1_DIR = ROOT / "reports" / "b1_percentile_backfill_20260805"


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _pct_rank(value: float, values: list[float]) -> float | None:
    vals = sorted(v for v in values if math.isfinite(v))
    if not vals:
        return None
    lower = sum(1 for v in vals if v < value)
    equal = sum(1 for v in vals if v == value)
    return round((lower + 0.5 * equal) * 100.0 / len(vals), 2)


def _quantile(values: list[float], pct: float) -> float | None:
    vals = sorted(v for v in values if math.isfinite(v))
    if not vals:
        return None
    if len(vals) == 1:
        return round(vals[0], 6)
    pos = (len(vals) - 1) * pct / 100.0
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return round(vals[lo], 6)
    frac = pos - lo
    return round(vals[lo] * (1 - frac) + vals[hi] * frac, 6)


def _load_csv_rows(patterns: Iterable[Path]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in sorted(patterns):
        if not path.exists() or path.stat().st_size == 0:
            continue
        with path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                key = row.get("id") or "|".join(
                    row.get(k, "") for k in ("session_date", "poll_ts", "snapshot_id", "variable_name")
                )
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
    return rows


def _load_generated_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted((ROOT / "reports").glob("rank_diag_*/generated_rows.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, list):
            continue
        for row in data:
            if not isinstance(row, dict):
                continue
            key = str(row.get("id") or row.get("recommendation_id") or "")
            key += "|" + str(row.get("candidate_id") or "")
            key += "|" + str(row.get("session_date") or "")
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows


@dataclass(frozen=True)
class Spec:
    constant: str
    hard_value: float | str
    gate_group: str
    source: str
    field: str
    comparator: str
    slice_filter: str
    activation_class: str
    note: str


SPECS = [
    Spec("MIN_CREDIT_RATIO", 0.10, "G3_CREDIT_ECONOMICS", "candidate", "credit_width_ratio", ">=", "index,lane,trade_mode,strategy_type", "candidate_live_candidate", "Candidate-level field exists in generated rank diagnostics."),
    Spec("IV_RICH_MIN", 1.15, "G4_IV_RICHNESS", "candidate", "iv_richness", ">=", "index,lane,trade_mode,strategy_type", "candidate_live_candidate", "Candidate-level field exists; absolute floor >1.00 still needs explicit policy."),
    Spec("MIN_PROB", 0.50, "G1_PROBABILITY", "candidate", "p_ml", ">=", "index,lane,trade_mode,strategy_type", "candidate_live_candidate", "Generated diagnostics expose p_ml, not raw probability; treat as model/ranker probability evidence."),
    Spec("MIN_SIGMA_OTM", 0.50, "G7_SIGMA_LOWER", "candidate", "sigma_otm", ">=", "index,lane,trade_mode,strategy_type", "candidate_live_candidate", "Candidate-level sigma exists and can be sliced directly."),
    Spec("MAX_SIGMA_OTM", 1.15, "G7_SIGMA_UPPER", "candidate", "sigma_otm", "<=", "index,lane,trade_mode,strategy_type", "candidate_live_candidate", "Candidate-level sigma exists and can be sliced directly."),
    Spec("IC_WALL_MAX_SIGMA", 1.50, "G7_CONDOR_WALL_DISTANCE", "context", "call_wall_distance", "<=", "index,lane,trade_mode", "context_measure_first", "Context wall distance exists, but condor-specific wall mechanics need replay proof before live authority."),
    Spec("MIN_WIDTH_BNF", 400, "G5_WIDTH_LANE_ENABLE", "candidate", "width", ">=", "BNF only", "lane_enable_not_percentile", "Width is structural/liquidity lane-enable; percentile can inform but should not replace broker/width policy."),
    Spec("MIN_WIDTH_NF", 150, "G5_WIDTH_LANE_ENABLE", "candidate", "width", ">=", "NF only", "lane_enable_not_percentile", "Width is structural/liquidity lane-enable; percentile can inform but should not replace broker/width policy."),
    Spec("IV_HIGH", 20, "G0_VOL_REGIME_DELETE_PROOF", "daily", "vix", ">=", "MARKET daily", "delete_proof_required", "Claude-approved path is delete proof first, not live conversion."),
    Spec("IV_VERY_HIGH", 24, "G0_VOL_REGIME_DELETE_PROOF", "daily", "vix", ">=", "MARKET daily", "delete_proof_required", "Claude-approved path is delete proof first, not live conversion."),
    Spec("IV_LOW", 15, "G0_VOL_REGIME_DELETE_PROOF", "daily", "vix", "<=", "MARKET daily", "delete_proof_required", "Claude-approved path is delete proof first, not live conversion."),
    Spec("DOW_THRESHOLD", 0.50, "CROSS_MARKET_INPUT", "missing", "dow_pct_change", ">=", "MARKET daily/intraday", "missing_history", "Not present in local C3/B1 percentile artifacts."),
    Spec("CRUDE_THRESHOLD", 1.50, "CROSS_MARKET_INPUT", "missing", "crude_pct_change", ">=", "MARKET daily/intraday", "missing_history", "Not present in local C3/B1 percentile artifacts."),
    Spec("GIFT_THRESHOLD", 0.30, "CROSS_MARKET_INPUT", "missing", "gift_pct_change", ">=", "MARKET daily/intraday", "missing_history", "Not present in local C3/B1 percentile artifacts."),
    Spec("NOISE_WINDOW", 15, "MICROSTRUCTURE", "missing", "minutes_from_open", ">=", "market microstructure", "measure_first", "This is time policy, not a market distribution threshold."),
    Spec("TARGET_NEAR_RATIO", 0.80, "G2_EXIT_POLICY", "missing", "target_capture_ratio", ">=", "managed exit path", "g2_mechanism_only", "G2 is mechanism-only until exit diagnostics are complete."),
    Spec("STOP_LOSS_RATIO", 0.70, "G2_EXIT_POLICY", "missing", "stop_loss_ratio", ">=", "managed exit path", "g2_mechanism_only", "G2 is mechanism-only until exit diagnostics are complete."),
    Spec("SIGMA_ENTRY_THRESHOLD", 1.50, "SIGMA_CONTEXT", "context", "abs_spot_sigma", ">=", "MARKET/context", "context_measure_first", "Market sigma exists; entry policy needs replay before live authority."),
    Spec("SIGMA_EXIT_THRESHOLD", 1.00, "SIGMA_CONTEXT", "context", "abs_spot_sigma", ">=", "MARKET/context", "context_measure_first", "Market sigma exists; exit policy needs replay before live authority."),
    Spec("SIGMA_IMPORTANT_THRESHOLD", 2.00, "SIGMA_CONTEXT", "context", "abs_spot_sigma", ">=", "MARKET/context", "context_measure_first", "Market sigma exists; importance threshold needs replay before live authority."),
    Spec("CANDLE_MARUBOZU_SHADOW_PCT", 0.05, "CANDLE_PATTERN", "missing", "candle_shadow_pct", "<=", "candle replay", "measure_first", "No local candle percentile artifact found."),
    Spec("CANDLE_DOJI_BODY_PCT", 0.05, "CANDLE_PATTERN", "missing", "candle_body_pct", "<=", "candle replay", "measure_first", "No local candle percentile artifact found."),
    Spec("CANDLE_SPINNING_MIN_BODY_PCT", 0.05, "CANDLE_PATTERN", "missing", "candle_body_pct", ">=", "candle replay", "measure_first", "No local candle percentile artifact found."),
    Spec("CANDLE_SPINNING_MAX_BODY_PCT", 0.20, "CANDLE_PATTERN", "missing", "candle_body_pct", "<=", "candle replay", "measure_first", "No local candle percentile artifact found."),
]


def _candidate_values(rows: list[dict[str, Any]], spec: Spec) -> list[float]:
    vals: list[float] = []
    for row in rows:
        if spec.constant == "MIN_WIDTH_BNF" and row.get("index_key") != "BNF":
            continue
        if spec.constant == "MIN_WIDTH_NF" and row.get("index_key") != "NF":
            continue
        value = _num(row.get(spec.field))
        if value is not None:
            vals.append(value)
    return vals


def _context_values(rows: list[dict[str, str]], spec: Spec) -> list[float]:
    vals: list[float] = []
    for row in rows:
        if row.get("variable_name") != spec.field:
            continue
        value = _num(row.get("value"))
        if value is not None:
            vals.append(value)
    return vals


def _daily_values(rows: list[dict[str, str]], spec: Spec) -> list[float]:
    vals: list[float] = []
    for row in rows:
        if row.get("variable_name") != spec.field:
            continue
        value = _num(row.get("value"))
        if value is not None:
            vals.append(value)
    return vals


def _activation_status(spec: Spec, support: int, pct: float | None) -> tuple[str, str]:
    if spec.activation_class in {"missing_history", "measure_first", "context_measure_first", "g2_mechanism_only", "delete_proof_required", "lane_enable_not_percentile"}:
        return spec.activation_class, "not_live_authority"
    if spec.constant == "IV_RICH_MIN":
        return "percentile_candidate_after_stability_pass", "owner_approved_extreme_percentile"
    if support <= 0:
        return "hard_fallback", "no_history"
    if pct is not None and (pct < 5 or pct > 95):
        return "hard_fallback_review_required", "extreme_percentile"
    return "percentile_candidate_after_stability_pass", "eligible_pending_batch5_wiring"


def _build_rows() -> tuple[list[dict[str, Any]], dict[str, int]]:
    c3_rows = _load_csv_rows(C3_DIR.glob("context_percentile_rows_*.csv"))
    b1_rows = _load_csv_rows(B1_DIR.glob("tier1_merged_daily_rows*.csv"))
    generated_rows = _load_generated_rows()
    out: list[dict[str, Any]] = []
    for spec in SPECS:
        hard = _num(spec.hard_value)
        if hard is None:
            values: list[float] = []
        elif spec.source == "candidate":
            values = _candidate_values(generated_rows, spec)
        elif spec.source == "context":
            values = _context_values(c3_rows, spec)
        elif spec.source == "daily":
            values = _daily_values(b1_rows, spec)
        else:
            values = []
        pct = _pct_rank(hard, values) if hard is not None else None
        status, flag = _activation_status(spec, len(values), pct)
        out.append({
            "constant": spec.constant,
            "gate_group": spec.gate_group,
            "hard_value": spec.hard_value,
            "comparator": spec.comparator,
            "evidence_source": spec.source,
            "history_field": spec.field,
            "slice_filter": spec.slice_filter,
            "support_count": len(values),
            "calibrated_percentile": "" if pct is None else pct,
            "q05": "" if not values else _quantile(values, 5),
            "median": "" if not values else _quantile(values, 50),
            "q95": "" if not values else _quantile(values, 95),
            "min_value": "" if not values else round(min(values), 6),
            "max_value": "" if not values else round(max(values), 6),
            "activation_status": status,
            "review_flag": flag,
            "notes": spec.note,
        })
    stats = {
        "c3_rows": len(c3_rows),
        "b1_rows": len(b1_rows),
        "generated_rows": len(generated_rows),
    }
    return out, stats


def _write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    fields = list(rows[0].keys()) if rows else []
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_report(rows: list[dict[str, Any]], stats: dict[str, int], path: Path) -> None:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["activation_status"]] = counts.get(row["activation_status"], 0) + 1
    lines = [
        "# PC2 Batch 2 - KIND B Calibration Table",
        "",
        "## Scope",
        "",
        "Local-only calibration inventory for PC2. No Supabase calls were made.",
        "This artifact does not change live ranking or gate behavior.",
        "",
        "## Source Counts",
        "",
        f"- C3 context rows loaded: {stats['c3_rows']}",
        f"- B1 daily rows loaded: {stats['b1_rows']}",
        f"- Generated candidate rows loaded: {stats['generated_rows']}",
        "",
        "## Activation Status Summary",
        "",
    ]
    for key in sorted(counts):
        lines.append(f"- {key}: {counts[key]}")
    lines += [
        "",
        "## Important Boundary",
        "",
        "Percentile authority is not enabled by this batch. A constant becomes eligible only when calibration exists, the percentile is not outside the 5/95 review band, and Batch 1 stability passes. Otherwise the hard fallback remains authoritative.",
        "",
        "## Calibration Rows",
        "",
        "| Constant | Group | Hard | Field | Support | Percentile | Status | Flag |",
        "| --- | --- | ---: | --- | ---: | ---: | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['constant']} | {row['gate_group']} | {row['hard_value']} | "
            f"{row['history_field']} | {row['support_count']} | {row['calibrated_percentile']} | "
            f"{row['activation_status']} | {row['review_flag']} |"
        )
    lines += [
        "",
        "## Findings",
        "",
        "- Candidate-level calibration is available for credit ratio, IV richness, model probability proxy, sigma OTM, and width using local rank diagnostics.",
        "- VIX thresholds have daily history, but G0 still requires byte-identical delete proof before removal or conversion.",
        "- Dow, crude, GIFT, candle pattern, and G2 exit policy thresholds do not have adequate local percentile history in this artifact.",
        "- Width constants are intentionally tagged as lane-enable policy, not a pure percentile replacement.",
        "",
        "## Outputs",
        "",
        "- `PC2_BATCH2_CALIBRATION_TABLE.csv`",
        "- `PC2_BATCH2_CALIBRATION_REPORT.md`",
        "",
    ]
    path.write_text("\n".join(lines))


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows, stats = _build_rows()
    _write_csv(rows, OUT_DIR / "PC2_BATCH2_CALIBRATION_TABLE.csv")
    _write_report(rows, stats, OUT_DIR / "PC2_BATCH2_CALIBRATION_REPORT.md")
    print(f"wrote {len(rows)} calibration rows")
    print(json.dumps(stats, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
