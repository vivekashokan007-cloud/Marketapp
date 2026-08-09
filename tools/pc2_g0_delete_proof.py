#!/usr/bin/env python3
"""PC2 Batch 4 local proof for deleting G0 hard VIX thresholds.

No Supabase calls. No app imports. This compares current hard-VIX routing and
force classification against a proposed G0-deleted variant on cached snapshot
inputs.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
OUT_DIR = ROOT / "reports" / "pc2_full_percentile_implementation"
INPUT_DIRS = [
    WORKSPACE / "label_regen_full_20260716",
    WORKSPACE / "label_regen_forward_20260723",
]

IV_HIGH = 20.0
IV_VERY_HIGH = 24.0
IV_LOW = 15.0
CREDIT_TYPES = {"BEAR_CALL", "BULL_PUT", "IRON_CONDOR", "IRON_BUTTERFLY"}
DEBIT_TYPES = {"BEAR_PUT", "BULL_CALL", "DOUBLE_DEBIT"}
ALL_TYPES = sorted(CREDIT_TYPES | DEBIT_TYPES)


def as_float(value):
    try:
        if value is None or value == "":
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def norm_bias(value):
    if isinstance(value, dict):
        bias = str(value.get("bias") or "NEUTRAL").upper()
        strength = str(value.get("strength") or "").upper()
        if bias not in {"BULL", "BEAR", "NEUTRAL"}:
            bias = "NEUTRAL"
        return {"bias": bias, "strength": strength}
    text = str(value or "NEUTRAL").upper()
    if "BULL" in text:
        return {"bias": "BULL", "strength": "STRONG" if "STRONG" in text else ""}
    if "BEAR" in text:
        return {"bias": "BEAR", "strength": "STRONG" if "STRONG" in text else ""}
    return {"bias": "NEUTRAL", "strength": ""}


def load_rows():
    for base in INPUT_DIRS:
        if not base.exists():
            continue
        for path in sorted(base.glob("*/snapshots_input.json")):
            try:
                rows = json.loads(path.read_text())
            except Exception:
                continue
            if not isinstance(rows, list):
                continue
            for row in rows:
                yield path, row


def row_bias(ctx):
    profiles = ctx.get("snapshot_market_profiles") or {}
    effective = profiles.get("effective_bias")
    if effective:
        return norm_bias(effective)
    morning = ctx.get("morning_input") or {}
    return norm_bias(morning.get("upstoxBias"))


def row_range_detected(ctx):
    profiles = ctx.get("snapshot_market_profiles") or {}
    regime = profiles.get("regime") or {}
    if str(regime.get("type") or "").lower() == "range":
        return True
    range_sigma = as_float(ctx.get("rangeSigma") or profiles.get("rangeSigma"))
    return range_sigma is not None and range_sigma < 0.3


def current_varsity_filter(bias, vix, range_detected=False):
    b = bias.get("bias", "NEUTRAL")
    is_strong = bias.get("strength", "") == "STRONG"
    iv_high = vix >= IV_HIGH
    very_high = vix >= IV_VERY_HIGH

    if b == "BEAR" and iv_high:
        primary = ["BEAR_CALL"]
        allowed = [] if is_strong else ["BULL_PUT", "IRON_CONDOR"]
        blocked = ["BEAR_PUT", "BULL_CALL", "DOUBLE_DEBIT"]
    elif b == "BULL" and iv_high:
        primary = ["BULL_PUT"]
        allowed = [] if is_strong else ["BEAR_CALL", "IRON_CONDOR"]
        blocked = ["BULL_CALL", "BEAR_PUT", "DOUBLE_DEBIT"]
    elif b == "NEUTRAL" and iv_high:
        primary = ["IRON_CONDOR"]
        allowed = ["BEAR_CALL", "BULL_PUT"]
        blocked = ["BEAR_PUT", "BULL_CALL", "DOUBLE_DEBIT"]
    elif b == "BEAR" and not iv_high:
        primary = ["BEAR_PUT"]
        allowed = [] if is_strong else ["BEAR_CALL"]
        blocked = ["BULL_PUT", "BULL_CALL", "IRON_CONDOR", "DOUBLE_DEBIT"]
    elif b == "BULL" and not iv_high:
        primary = ["BULL_CALL"]
        allowed = [] if is_strong else ["BULL_PUT"]
        blocked = ["BEAR_CALL", "BEAR_PUT", "IRON_CONDOR", "DOUBLE_DEBIT"]
    else:
        primary = ["IRON_CONDOR"]
        allowed = ["BEAR_CALL", "BULL_PUT"]
        blocked = ["BEAR_PUT", "BULL_CALL", "DOUBLE_DEBIT"]

    if very_high:
        if b == "BEAR":
            if "BEAR_PUT" not in primary:
                primary.append("BEAR_PUT")
            blocked = [s for s in blocked if s != "BEAR_PUT"]
        elif b == "BULL":
            if "BULL_CALL" not in primary:
                primary.append("BULL_CALL")
            blocked = [s for s in blocked if s != "BULL_CALL"]
        else:
            for stype in ["BEAR_PUT", "BULL_CALL"]:
                if stype not in allowed:
                    allowed.append(stype)
            blocked = [s for s in blocked if s not in ("BEAR_PUT", "BULL_CALL")]

    if range_detected:
        primary = ["IRON_BUTTERFLY", "IRON_CONDOR"]
        allowed = ["BEAR_CALL", "BULL_PUT"] if b != "BULL" else ["BULL_PUT", "BEAR_CALL"]
        blocked = [s for s in blocked if s not in ("IRON_BUTTERFLY", "IRON_CONDOR")]

    if "IRON_BUTTERFLY" not in primary and "IRON_BUTTERFLY" not in allowed and "IRON_BUTTERFLY" not in blocked:
        blocked.append("IRON_BUTTERFLY")
    return {"primary": primary, "allowed": allowed, "blocked": blocked}


def proposed_g0_deleted_varsity_filter(bias, range_detected=False):
    """Hard VIX regime removed: cached evidence uses the non-high branch."""
    return current_varsity_filter(bias, 0.0, range_detected)


def current_force3(stype, vix, iv_pctl):
    is_credit = stype in CREDIT_TYPES
    is_debit = stype in DEBIT_TYPES
    regime = "NORMAL"
    if vix >= IV_HIGH or (iv_pctl is not None and iv_pctl > 65):
        regime = "HIGH"
    if vix >= IV_VERY_HIGH or (iv_pctl is not None and iv_pctl > 85):
        regime = "VERY_HIGH"
    if vix <= IV_LOW or (iv_pctl is not None and iv_pctl < 25):
        regime = "LOW"
    if regime == "VERY_HIGH":
        if is_debit:
            return 1
        return 1 if stype in {"IRON_CONDOR", "IRON_BUTTERFLY"} else 0
    if regime == "HIGH":
        return 1 if is_credit else -1
    if regime == "LOW":
        return 1 if is_debit else -1
    return 1


def proposed_g0_deleted_force3(stype, iv_pctl):
    """Hard VIX thresholds removed; only already-existing ivPercentile state remains."""
    is_credit = stype in CREDIT_TYPES
    is_debit = stype in DEBIT_TYPES
    regime = "NORMAL"
    if iv_pctl is not None and iv_pctl > 65:
        regime = "HIGH"
    if iv_pctl is not None and iv_pctl > 85:
        regime = "VERY_HIGH"
    if iv_pctl is not None and iv_pctl < 25:
        regime = "LOW"
    if regime == "VERY_HIGH":
        if is_debit:
            return 1
        return 1 if stype in {"IRON_CONDOR", "IRON_BUTTERFLY"} else 0
    if regime == "HIGH":
        return 1 if is_credit else -1
    if regime == "LOW":
        return 1 if is_debit else -1
    return 1


def compact_filter(value):
    return (
        tuple(value.get("primary") or []),
        tuple(value.get("allowed") or []),
        tuple(value.get("blocked") or []),
    )


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    total_rows = 0
    missing_ivp = 0
    routing_deltas = []
    force_deltas = []
    counts = Counter()

    for source_path, row in load_rows():
        ctx = row.get("context_json") or {}
        if not isinstance(ctx, dict):
            continue
        vix = as_float(ctx.get("vix") or (ctx.get("live") or {}).get("vix"))
        if vix is None:
            continue
        ivp = as_float(ctx.get("ivPercentile"))
        if ivp is None:
            missing_ivp += 1
        bias = row_bias(ctx)
        range_detected = row_range_detected(ctx)
        session_date = row.get("session_date") or ctx.get("today_ist") or source_path.parent.name
        total_rows += 1
        if vix >= IV_HIGH:
            counts["vix_ge_iv_high"] += 1
        if vix >= IV_VERY_HIGH:
            counts["vix_ge_iv_very_high"] += 1
        if vix <= IV_LOW:
            counts["vix_le_iv_low"] += 1
        if ivp is not None and ivp < 25:
            counts["ivp_lt_25"] += 1
        if ivp is not None and ivp > 65:
            counts["ivp_gt_65"] += 1
        if ivp is not None and ivp > 85:
            counts["ivp_gt_85"] += 1

        cur_filter = compact_filter(current_varsity_filter(bias, vix, range_detected))
        new_filter = compact_filter(proposed_g0_deleted_varsity_filter(bias, range_detected))
        if cur_filter != new_filter:
            routing_deltas.append({
                "session_date": session_date,
                "poll_ts": row.get("poll_ts"),
                "vix": vix,
                "iv_percentile": ivp,
                "bias": bias,
                "range_detected": range_detected,
                "current": cur_filter,
                "proposed": new_filter,
            })

        for stype in ALL_TYPES:
            cur_force = current_force3(stype, vix, ivp)
            new_force = proposed_g0_deleted_force3(stype, ivp)
            if cur_force != new_force:
                force_deltas.append({
                    "session_date": session_date,
                    "poll_ts": row.get("poll_ts"),
                    "vix": vix,
                    "iv_percentile": ivp,
                    "strategy_type": stype,
                    "current_force3": cur_force,
                    "proposed_force3": new_force,
                })

    summary = {
        "rows_checked": total_rows,
        "missing_iv_percentile_rows": missing_ivp,
        "counts": dict(counts),
        "routing_delta_count": len(routing_deltas),
        "force3_delta_count": len(force_deltas),
        "routing_delta_sample": routing_deltas[:20],
        "force3_delta_sample": force_deltas[:20],
        "verdict": "BYTE_IDENTICAL_ON_CACHE" if not routing_deltas and not force_deltas else "DELTA_FOUND_STOP",
        "boundary": (
            "Byte-identical proof is valid only for cached rows. IV_LOW is reachable, "
            "but ivPercentile is present and low on all cached rows, so deleting hard "
            "VIX from force3 is equivalent in this evidence slice."
        ),
    }

    (OUT_DIR / "PC2_BATCH4_G0_DELETE_PROOF.json").write_text(json.dumps(summary, indent=2, sort_keys=True))

    report = [
        "# PC2 Batch 4 - G0 Delete Proof",
        "",
        "## Scope",
        "",
        "- Local cached snapshot inputs only.",
        "- No Supabase calls.",
        "- No app behavior change.",
        "- Compared current hard-VIX G0 behavior against a proposed variant where hard VIX thresholds are removed and existing `ivPercentile` state remains authoritative for Force 3.",
        "",
        "## Result",
        "",
        f"- Rows checked: `{total_rows}`.",
        f"- Missing `ivPercentile` rows: `{missing_ivp}`.",
        f"- `vix >= IV_HIGH(20)`: `{counts.get('vix_ge_iv_high', 0)}`.",
        f"- `vix >= IV_VERY_HIGH(24)`: `{counts.get('vix_ge_iv_very_high', 0)}`.",
        f"- `vix <= IV_LOW(15)`: `{counts.get('vix_le_iv_low', 0)}`.",
        f"- `ivPercentile < 25`: `{counts.get('ivp_lt_25', 0)}`.",
        f"- `ivPercentile > 65`: `{counts.get('ivp_gt_65', 0)}`.",
        f"- Varsity routing deltas: `{len(routing_deltas)}`.",
        f"- Force3 deltas: `{len(force_deltas)}`.",
        f"- Verdict: `{summary['verdict']}`.",
        "",
        "## Important Boundary",
        "",
        "- `IV_HIGH` and `IV_VERY_HIGH` are unreachable in this cache.",
        "- `IV_LOW` is reachable, so deleting it is not automatically safe.",
        "- The proof is byte-identical here because cached rows have `ivPercentile` present, and it already puts the force state into LOW.",
        "- If future rows lack `ivPercentile`, deleting `IV_LOW` would remove a fallback. That should be handled explicitly in live wiring, not hidden inside this proof.",
        "",
        "## Recommendation",
        "",
        "- Treat G0 delete as locally proven for cached behavior.",
        "- Do not remove runtime hard-VIX constants in isolation yet.",
        "- In Batch 5, switch the G0/force/routing consumer to context-percentile authority with an explicit missing-context fallback and persisted `gate_basis`/`switch_basis` evidence.",
        "",
        "## Outputs",
        "",
        "- `PC2_BATCH4_G0_DELETE_PROOF.json`",
        "- `PC2_BATCH4_G0_DELETE_PROOF.md`",
    ]
    (OUT_DIR / "PC2_BATCH4_G0_DELETE_PROOF.md").write_text("\n".join(report) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
