"""Deterministic C3 recording-frame and post-close finalization helpers.

This module has no network or Android dependencies.  The phone captures a
compact frame at poll time, then calls ``finalize_frames`` after teacher
evaluation.  Keeping the calculation here makes the ordering/leakage rule
unit-testable and keeps ML evaluation independent from C3 persistence.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from statistics import median
from typing import Any


WINDOWS = (30, 60)
FRAME_VERSION = "c3_finalization_frame_v1"
SUPPLY_SLICE_VARIABLES = {
    "credit_width_ratio": "credit_width_ratio_menu_median",
    "credit_to_risk": "credit_to_risk_menu_median",
    "sigma_otm": "sigma_otm_menu_median",
    "premium_edge": "premium_edge_menu_median",
}


def _obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            pass
    return {}


def _array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            pass
    return []


def _number(value: Any) -> float | None:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _first(*sources: tuple[dict[str, Any], tuple[str, ...]]) -> float | None:
    for source, keys in sources:
        for key in keys:
            value = _number(source.get(key))
            if value is not None:
                return value
    return None


def _positive(*sources: tuple[dict[str, Any], tuple[str, ...]]) -> float | None:
    for source, keys in sources:
        for key in keys:
            value = _number(source.get(key))
            if value is not None and value > 0:
                return value
    return None


def _median(values: list[float | None]) -> float | None:
    clean = [value for value in values if value is not None and math.isfinite(value)]
    return float(median(clean)) if clean else None


def _best(values: list[float | None]) -> float | None:
    clean = [value for value in values if value is not None and math.isfinite(value)]
    return max(clean) if clean else None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def _metric(candidate: dict[str, Any], name: str) -> float | None:
    aliases = {
        "premium_edge": ("premium_edge", "premiumEdge"),
        "prob_profit": ("prob_profit", "probProfit", "prob"),
        "net_premium": ("net_premium", "netPremium"),
        "max_profit": ("max_profit", "maxProfit"),
        "max_loss": ("max_loss", "maxLoss"),
        "risk_reward": ("risk_reward", "riskReward"),
        "width": ("width",),
        "debit_breakeven_sigma": ("debit_breakeven_sigma", "debitBreakevenSigma"),
        "net_theta": ("net_theta", "netTheta"),
        "sigma_otm": ("sigma_otm", "sigmaOTM"),
        "iv_richness": ("iv_richness", "ivRichness"),
        "credit_width_ratio": ("credit_width_ratio", "creditWidthRatio"),
    }
    if name == "ev_per_1k":
        direct = _first((candidate, ("ev_per_1k", "evPer1k")))
        if direct is not None:
            return direct
        premium = _first((candidate, ("premium_edge", "premiumEdge")))
        loss = _first((candidate, ("max_loss", "maxLoss")))
        return _ratio(premium * 1000.0 if premium is not None else None, abs(loss) if loss is not None else None)
    if name == "theta_friction_minutes":
        cost = _first((candidate, ("est_cost", "estCost")))
        theta = _first((candidate, ("net_theta", "netTheta")))
        return _ratio(cost, abs(theta) / 390.0 if theta is not None else None)
    return _first((candidate, aliases.get(name, (name,))))


def capture_frame(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Extract the fixed, poll-time C3 values from a canonical brain snapshot."""
    context = _obj(snapshot.get("context_json"))
    verdict = _obj(snapshot.get("verdict_json"))
    forces = _obj(snapshot.get("market_forces_json"))
    summary = _obj(snapshot.get("poll_summary_json"))
    morning = _obj(context.get("morning_input") or context.get("morningInput"))
    latest = _obj(context.get("snapshot_latest_poll"))
    profiles = _obj(context.get("snapshot_market_profiles") or context.get("market_profiles"))
    bnf_profile = _obj(profiles.get("bnfProfile") or profiles.get("bnf_profile"))
    nf_profile = _obj(profiles.get("nfProfile") or profiles.get("nf_profile"))
    bnf_chain = _obj(context.get("bnfChain") or context.get("bnf_chain"))
    nf_chain = _obj(context.get("nfChain") or context.get("nf_chain"))
    ranked_full = _array(context.get("snapshot_ranked_candidates_full"))
    generated = ranked_full or _array(context.get("snapshot_generated_candidates"))
    candidate_population_source = "snapshot_ranked_candidates_full" if ranked_full else "snapshot_generated_candidates"
    rejected = _array(context.get("snapshot_rejected_candidates") or context.get("snapshot_rejected_candidates_full"))
    generated = [row for row in generated if isinstance(row, dict)]
    rejected = [row for row in rejected if isinstance(row, dict)]
    population = generated + rejected
    supply_shadow = _obj(context.get("snapshot_pc2_supply_quality_shadow"))
    credit = [row for row in population if str(row.get("is_credit", row.get("isCredit", ""))).lower() in {"true", "1", "yes"}]
    debit = [row for row in generated if row not in credit]
    menu = lambda metric, rows=None: [_metric(row, metric) for row in (generated if rows is None else rows)]

    bnf_spot = _first((context, ("bnfSpot", "bnf_spot", "bnf")), (latest, ("bnfSpot", "bnf_spot", "bnf", "BNF")), (summary, ("bnf_spot", "bnf")))
    nf_spot = _first((context, ("nfSpot", "nf_spot", "nf")), (latest, ("nfSpot", "nf_spot", "nf", "NF")), (summary, ("nf_spot", "nf")))
    vix = _first((context, ("vix", "VIX")), (latest, ("vix", "VIX")), (summary, ("vix", "VIX")), (forces, ("vix", "VIX")))
    bnf_daily_sigma = bnf_spot * (vix / 100.0) / math.sqrt(252) if bnf_spot and vix else None
    nf_daily_sigma = nf_spot * (vix / 100.0) / math.sqrt(252) if nf_spot and vix else None
    bnf_atm_iv = _positive((context, ("bnfAtmIv", "bnf_atm_iv", "atmIv", "atm_iv")), (latest, ("bnfAtmIv", "bnf_atm_iv")), (bnf_chain, ("atmIv", "atm_iv")), (bnf_profile, ("atmIv", "atm_iv")))
    nf_atm_iv = _positive((context, ("nfAtmIv", "nf_atm_iv")), (latest, ("nfAtmIv", "nf_atm_iv")), (nf_chain, ("atmIv", "atm_iv")), (nf_profile, ("atmIv", "atm_iv")))
    bnf_pcr = _first((context, ("bnfPcr", "bnf_pcr", "pcr")), (latest, ("bnfPcr", "bnf_pcr", "pcr")), (bnf_chain, ("pcr",)), (bnf_profile, ("pcr",)))
    nf_pcr = _first((context, ("nfPcr", "nf_pcr")), (latest, ("nfPcr", "nf_pcr")), (nf_chain, ("pcr",)), (nf_profile, ("pcr",)))
    bnf_call_oi = _first((bnf_chain, ("totalCallOI", "totalCallOi")), (forces, ("bnf_total_call_oi", "bnfTotalCallOi")), (context, ("bnfTotalCallOi", "bnf_total_call_oi")))
    nf_call_oi = _first((nf_chain, ("totalCallOI", "totalCallOi")), (forces, ("nf_total_call_oi", "nfTotalCallOi")), (context, ("nfTotalCallOi", "nf_total_call_oi")))
    bnf_put_oi = _first((bnf_chain, ("totalPutOI", "totalPutOi")), (forces, ("bnf_total_put_oi", "bnfTotalPutOi")), (context, ("bnfTotalPutOi", "bnf_total_put_oi")))
    nf_put_oi = _first((nf_chain, ("totalPutOI", "totalPutOi")), (forces, ("nf_total_put_oi", "nfTotalPutOi")), (context, ("nfTotalPutOi", "nf_total_put_oi")))

    def distance(level: float | None, spot: float | None) -> float | None:
        return None if level is None or spot is None else level - spot

    bnf_max_pain = _first((context, ("bnfMaxPain", "maxPain")), (bnf_chain, ("maxPain", "max_pain")), (bnf_profile, ("maxPain", "max_pain")))
    nf_max_pain = _first((context, ("nfMaxPain",)), (nf_chain, ("maxPain", "max_pain")), (nf_profile, ("maxPain", "max_pain")))
    bnf_call_wall = _first((context, ("bnfCallWall",)), (latest, ("bnfCallWall", "bnf_call_wall")), (bnf_chain, ("callWallStrike", "callWall", "call_wall")), (bnf_profile, ("callWallStrike", "callWall", "call_wall")))
    nf_call_wall = _first((context, ("nfCallWall",)), (latest, ("nfCallWall", "nf_call_wall")), (nf_chain, ("callWallStrike", "callWall", "call_wall")), (nf_profile, ("callWallStrike", "callWall", "call_wall")))
    bnf_put_wall = _first((context, ("bnfPutWall",)), (latest, ("bnfPutWall", "bnf_put_wall")), (bnf_chain, ("putWallStrike", "putWall", "put_wall")), (bnf_profile, ("putWallStrike", "putWall", "put_wall")))
    nf_put_wall = _first((context, ("nfPutWall",)), (latest, ("nfPutWall", "nf_put_wall")), (nf_chain, ("putWallStrike", "putWall", "put_wall")), (nf_profile, ("putWallStrike", "putWall", "put_wall")))

    values: dict[str, float | None] = {
        "vix": vix, "fii_short_pct": _first((context, ("fiiShort", "fii_short_pct", "fii_short")), (morning, ("fiiShortPct", "fii_short_pct", "fiiShort", "fii_short")), (forces, ("fiiShort", "fiiShortPct", "fii_short_pct"))),
        "iv_richness_menu_median": _median(menu("iv_richness", population)), "realized_day_range": _first((context, ("rangeSigma", "dayRangeSigma", "day_range_sigma")), (summary, ("day_range_sigma",))),
        "sigma_otm_menu_median": _median(menu("sigma_otm", population)), "credit_width_ratio_menu_median": _median(menu("credit_width_ratio", credit)),
        "rejected_sigma_otm_median": _median([_first((row, ("sigmaOTM", "sigma_otm"))) for row in rejected]),
        "premium_edge_menu_median": _median(menu("premium_edge")), "premium_edge_menu_best": _best(menu("premium_edge")),
        "ev_per_1k_menu_median": _median(menu("ev_per_1k")), "ev_per_1k_menu_best": _best(menu("ev_per_1k")),
        "prob_profit_menu_median": _median(menu("prob_profit", population)), "prob_profit_menu_best": _best(menu("prob_profit")),
        "net_premium_menu_median": _median(menu("net_premium")), "net_premium_menu_best": _best(menu("net_premium")),
        "max_profit_menu_median": _median(menu("max_profit")), "max_profit_menu_best": _best(menu("max_profit")),
        "max_loss_menu_median": _median(menu("max_loss")), "max_loss_menu_best": _best(menu("max_loss")),
        "risk_reward_menu_median": _median(menu("risk_reward")), "risk_reward_menu_best": _best(menu("risk_reward")),
        "width_menu_median": _median(menu("width")), "width_menu_best": _best(menu("width")),
        "debit_breakeven_sigma_menu_median": _median(menu("debit_breakeven_sigma", debit)), "debit_breakeven_sigma_menu_best": _best(menu("debit_breakeven_sigma", debit)),
        "theta_friction_minutes_menu_median": _median(menu("theta_friction_minutes")), "theta_friction_minutes_menu_best": _best(menu("theta_friction_minutes")),
        "net_theta_menu_median": _median(menu("net_theta")), "net_theta_menu_best": _best(menu("net_theta")),
        "atm_iv": bnf_atm_iv or nf_atm_iv, "iv_percentile": _first((context, ("ivPercentile", "iv_percentile"))), "daily_sigma": bnf_daily_sigma,
        "pcr": bnf_pcr, "near_atm_pcr": _first((context, ("bnfNearAtmPcr", "bnf_near_atm_pcr", "nearAtmPCR", "near_atm_pcr")), (bnf_chain, ("nearAtmPCR", "near_atm_pcr")), (bnf_profile, ("nearAtmPCR", "near_atm_pcr"))),
        "max_pain_distance": _first((context, ("maxPainDistance", "max_pain_distance"))) or distance(bnf_max_pain, bnf_spot),
        "call_wall_distance": _first((context, ("callWallDistance", "call_wall_distance"))) or distance(bnf_call_wall, bnf_spot),
        "put_wall_distance": _first((context, ("putWallDistance", "put_wall_distance"))) or distance(bnf_put_wall, bnf_spot),
        "total_call_oi": bnf_call_oi, "total_put_oi": bnf_put_oi, "oi_skew": _ratio((bnf_put_oi or 0.0) - (bnf_call_oi or 0.0), (bnf_put_oi or 0.0) + (bnf_call_oi or 0.0)),
        "realized_vs_implied_range_ratio": _ratio(_first((context, ("rangeSigma", "dayRangeSigma"))), bnf_daily_sigma), "overnight_gap": _first((context, ("gapSigma", "overnight_gap"))),
        "spot_vs_vwap": _first((context, ("spotVsVwap", "spot_vs_vwap"))), "abs_spot_sigma": _first((context, ("absSpotSigma", "abs_spot_sigma"))), "abs_nf_spot_sigma": _first((context, ("absNfSpotSigma", "abs_nf_spot_sigma"))),
        "bnf_atm_iv": bnf_atm_iv, "nf_atm_iv": nf_atm_iv, "bnf_pcr": bnf_pcr, "nf_pcr": nf_pcr,
        "bnf_near_atm_pcr": _first((context, ("bnfNearAtmPcr", "bnf_near_atm_pcr"))), "nf_near_atm_pcr": _first((context, ("nfNearAtmPcr", "nf_near_atm_pcr"))),
        "bnf_max_pain_distance": distance(bnf_max_pain, bnf_spot), "nf_max_pain_distance": distance(nf_max_pain, nf_spot),
        "bnf_call_wall_distance": distance(bnf_call_wall, bnf_spot), "nf_call_wall_distance": distance(nf_call_wall, nf_spot),
        "bnf_put_wall_distance": distance(bnf_put_wall, bnf_spot), "nf_put_wall_distance": distance(nf_put_wall, nf_spot),
        "bnf_total_call_oi": bnf_call_oi, "nf_total_call_oi": nf_call_oi, "bnf_total_put_oi": bnf_put_oi, "nf_total_put_oi": nf_put_oi,
        "bnf_oi_skew": _ratio((bnf_put_oi or 0.0) - (bnf_call_oi or 0.0), (bnf_put_oi or 0.0) + (bnf_call_oi or 0.0)),
        "nf_oi_skew": _ratio((nf_put_oi or 0.0) - (nf_call_oi or 0.0), (nf_put_oi or 0.0) + (nf_call_oi or 0.0)),
        "generated_count": float(len(generated)), "rejected_count": float(len(rejected)),
        "watchlist_survivors": float(sum(1 for row in generated if _number(row.get("watchlist_rank")) not in (None, 0))),
        "distinct_families_generated": float(len({str(row.get("strategy_type") or row.get("type") or "") for row in generated})), "menu_size": float(len(generated)),
        "confidence": _first((verdict, ("confidence",)), (snapshot, ("confidence",))),
        "signal_independence_score": _first((verdict, ("signalIndependenceScore", "signal_independence_score")), (summary, ("signal_independence_score",))),
        "bull_score": _first((context, ("bullScore", "bull_score")), (verdict, ("bullScore", "bull_score", "bull"))), "bear_score": _first((context, ("bearScore", "bear_score")), (verdict, ("bearScore", "bear_score", "bear"))),
        "signal_accuracy": _first((summary, ("signal_accuracy",))), "notification_count_session": _first((summary, ("notification_count_session",))),
        "menu_win_rate_prior_sessions_only": None, "menu_mean_pnl_prior_sessions_only": None, "realized_r_prior_sessions_only": None,
    }
    stage_counts: dict[str, float] = defaultdict(float)
    for row in rejected:
        stage = str(row.get("stage") or row.get("rejectionStage") or row.get("rejection_stage") or "unknown")
        stage = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in stage).strip("_").lower() or "unknown"
        stage_counts[f"rejection_stage_count__{stage}"] += 1.0
    values.update(stage_counts)
    candidate_slices = []
    for slice_row in _array(supply_shadow.get("slices")):
        if not isinstance(slice_row, dict):
            continue
        metrics = _obj(slice_row.get("metrics"))
        slice_values = {}
        quantiles = {}
        for metric_name, variable_name in SUPPLY_SLICE_VARIABLES.items():
            summary = _obj(metrics.get(metric_name))
            value = _number(summary.get("median"))
            if value is not None:
                slice_values[variable_name] = value
                quantiles[variable_name] = summary
        if not slice_values:
            continue
        candidate_slices.append({
            "slice_key": slice_row.get("slice_key"),
            "index_key": str(slice_row.get("index_key") or "UNKNOWN").upper(),
            "direction": str(slice_row.get("direction") or "UNKNOWN").upper(),
            "trade_mode": str(slice_row.get("trade_mode") or "UNKNOWN").lower(),
            "population_scope": slice_row.get("population_scope"),
            "population_count": int(_number(slice_row.get("population_count")) or 0),
            "generated_count": int(_number(slice_row.get("generated_count")) or 0),
            "rejected_count": int(_number(slice_row.get("rejected_count")) or 0),
            "values": slice_values,
            "quantiles": quantiles,
        })
    return {
        "frame_version": FRAME_VERSION,
        "snapshot_id": str(snapshot.get("id") or ""),
        "session_date": str(snapshot.get("session_date") or ""),
        "poll_ts": str(snapshot.get("poll_ts") or ""),
        "values": {name: (round(value, 6) if value is not None else None) for name, value in values.items()},
        "generated_population_count": len(generated),
        "rejected_population_count": len(rejected),
        "candidate_population_source": candidate_population_source,
        "rejected_capture_present": "snapshot_rejected_candidates" in context or "snapshot_rejected_candidates_full" in context,
        "candidate_slices": candidate_slices,
        "supply_shadow_version": supply_shadow.get("version"),
    }


def _percentile(value: float | None, history: list[float]) -> float | None:
    if value is None or not history:
        return None
    sorted_history = sorted(history)
    below = sum(1 for item in sorted_history if item < value)
    equal = sum(1 for item in sorted_history if item == value)
    return round((below + 0.5 * equal) * 100.0 / len(sorted_history), 2)


def _row_id(row: dict[str, Any]) -> str:
    material = "|".join(str(row.get(key) or "") for key in ("session_date", "poll_ts", "index_key", "lane", "trade_mode", "variable_name", "history_source"))
    return "c3_" + hashlib.sha1(material.encode("utf-8")).hexdigest()


def _slice_history_key(variable_name: str, index_key: str, direction: str, trade_mode: str) -> str:
    return "|".join((variable_name, index_key.upper(), direction.upper(), trade_mode.lower()))


def finalize_frames(
    frames: list[dict[str, Any]], history_seed: dict[str, list[float]], outcome_prior: dict[str, float | None], catalog: dict[str, list[str]],
    *, history_source: str = "live", pre_t_clean: bool = True,
) -> list[dict[str, Any]]:
    """Build poll-level C3 rows; seed contains only sessions before this one."""
    history: dict[str, list[float]] = defaultdict(list)
    for name, values in (history_seed or {}).items():
        history[name].extend(value for value in values if _number(value) is not None)
    group_by_name = {name: group for group, names in catalog.items() for name in names}
    catalog_names = set(group_by_name)
    rows: list[dict[str, Any]] = []
    for frame in sorted(frames, key=lambda row: str(row.get("poll_ts") or "")):
        if frame.get("frame_version") != FRAME_VERSION:
            continue
        values = dict(frame.get("values") or {})
        values.update(outcome_prior or {})
        for name in sorted(catalog_names | set(values)):
            value = _number(values.get(name))
            support30 = len(history[name][-30:])
            support60 = len(history[name][-60:])
            row = {
                "session_date": frame.get("session_date"), "poll_ts": frame.get("poll_ts"), "snapshot_id": frame.get("snapshot_id") or None,
                "index_key": "MARKET", "lane": "MARKET", "trade_mode": "MARKET", "variable_name": name,
                "variable_group": group_by_name.get(name, "supply_process" if name.startswith("rejection_stage_") else "uncatalogued"),
                "value": round(value, 6) if value is not None else None, "pct_30": _percentile(value, history[name][-30:]), "pct_60": _percentile(value, history[name][-60:]),
                "support_count": max(support30, support60), "support_count_30": support30, "support_count_60": support60,
                "history_window_end": frame.get("poll_ts"), "history_source": history_source, "pre_t_clean": pre_t_clean,
                "schema_version": "context_percentiles_v1", "recording_version": "c3_percentile_recording_v1",
                "source_table": "ml_brain_snapshots:c3_finalization_frame", "source_quality": "PRE_T_CLEAN" if pre_t_clean else "PRE_T_DIRTY",
                "extra_json": {"candidate_population_scope": "generated_plus_rejected_candidate_population" if frame.get("rejected_capture_present") else "unverified_generated_population_only", "calibration_population_version": "pc2_generated_rejected_union_v1" if frame.get("rejected_capture_present") else "unverified", "generated_population_count": frame.get("generated_population_count", 0), "rejected_population_count": frame.get("rejected_population_count", 0)},
            }
            row["id"] = _row_id(row)
            rows.append(row)
            if value is not None:
                history[name].append(value)
        for slice_row in frame.get("candidate_slices") or []:
            if not isinstance(slice_row, dict):
                continue
            index_key = str(slice_row.get("index_key") or "UNKNOWN").upper()
            direction = str(slice_row.get("direction") or "UNKNOWN").upper()
            trade_mode = str(slice_row.get("trade_mode") or "UNKNOWN").lower()
            quantiles = _obj(slice_row.get("quantiles"))
            for name, raw_value in sorted(_obj(slice_row.get("values")).items()):
                value = _number(raw_value)
                history_key = _slice_history_key(name, index_key, direction, trade_mode)
                support30 = len(history[history_key][-30:])
                support60 = len(history[history_key][-60:])
                row = {
                    "session_date": frame.get("session_date"), "poll_ts": frame.get("poll_ts"), "snapshot_id": frame.get("snapshot_id") or None,
                    "index_key": index_key, "lane": direction, "trade_mode": trade_mode, "variable_name": name,
                    "variable_group": "candidate_supply_slice",
                    "value": round(value, 6) if value is not None else None,
                    "pct_30": _percentile(value, history[history_key][-30:]),
                    "pct_60": _percentile(value, history[history_key][-60:]),
                    "support_count": max(support30, support60), "support_count_30": support30, "support_count_60": support60,
                    "history_window_end": frame.get("poll_ts"), "history_source": history_source, "pre_t_clean": pre_t_clean,
                    "schema_version": "context_percentiles_v1", "recording_version": "c3_supply_slice_recording_v1",
                    "source_table": "ml_brain_snapshots:snapshot_pc2_supply_quality_shadow",
                    "source_quality": "PRE_T_CLEAN" if pre_t_clean else "PRE_T_DIRTY",
                    "extra_json": {
                        "candidate_population_scope": slice_row.get("population_scope") or "uncapped_generated_plus_rejected_live_memory",
                        "calibration_population_version": "pc2_uncapped_generated_rejected_union_v1",
                        "supply_shadow_version": frame.get("supply_shadow_version"),
                        "slice_key": slice_row.get("slice_key"),
                        "direction": direction,
                        "population_count": slice_row.get("population_count", 0),
                        "generated_population_count": slice_row.get("generated_count", 0),
                        "rejected_population_count": slice_row.get("rejected_count", 0),
                        "distribution": quantiles.get(name) or {},
                    },
                }
                row["id"] = _row_id(row)
                rows.append(row)
                if value is not None:
                    history[history_key].append(value)
    return rows
