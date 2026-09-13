"""Contract-specific lot resolution — SSOT shared with Kotlin.

Contract lot size = units per lot (e.g. NF 65). Distinct from:
  - number_of_lots (quantity / how many lots)
  - quantity_units / lot_size (total units = contract_lot_size * number_of_lots)

A resolver keyed only by (index, as_of) is insufficient where contracts coexist
with different lots. Prefer consistent captured metadata; on conflict flag and
exclude authoritative calc while retaining original values. Fail closed outside
the verified supported project-data window.

SSOT file: app/src/main/assets/contract_lot_table_v1.json
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Optional

LOT_TABLE_VERSION_ID = "contract_lot_table_v2_20260913"

# Ranking DTE buckets (stage2a / teacher prior) — NOT measurement partitions.
DTE_RANKING_BUCKET_VERSION = "dte_ranking_buckets_v1_stage2a_0_1_2_3_4_7_8plus_20260913"
DTE_RANKING_BUCKETS = ("DTE_0", "DTE_1", "DTE_2_3", "DTE_4_7", "DTE_8_PLUS", "unknown")

# Measurement DTE buckets — reporting only.
DTE_MEASUREMENT_BUCKET_VERSION = "dte_measurement_buckets_v1_0_1_2_3_7_8plus_20260913"
DTE_MEASUREMENT_BUCKETS = ("DTE_0", "DTE_1_2", "DTE_3_7", "DTE_8_PLUS", "UNKNOWN")

# Calendar coverage label — only claim nse_trading_calendar when holidays cover
# the full session→expiry interval years (not merely because NSE_HOLIDAYS exists).
DTE_CALENDAR_VERSION_PREFIX = "nse_holiday_years"

_EMBEDDED_TABLE: dict[str, Any] = {
    "version_id": LOT_TABLE_VERSION_ID,
    "schema": "contract_lot_rules_contract_specific_v2",
    "unit": "units_per_lot",
    "indices": ["BNF", "NF"],
    "supported_project_data_window": {
        "start": "2024-11-20",
        "end": None,
        "fail_closed_outside": True,
    },
    "operational_current_lots": {"BNF": 30, "NF": 65},
    "authoritative_contract_rules": [],
    "instrument_master_snapshots": [
        {
            "snapshot_id": "upstox_20260719_near90d",
            "as_of": "2026-07-19",
            "expiry_within_days": 90,
            "lots": {"BNF": 30, "NF": 65},
            "provenance": "upstox_instrument_master_verified_20260719",
            "provenance_quality": "verified_snapshot_scope_only",
        }
    ],
    "research_only_excluded": [
        {
            "as_of_start": "2025-01-01",
            "as_of_end": None,
            "lots": {"BNF": 30, "NF": 65},
            "provenance_quality": "excluded_unsupported_blanket",
            "authoritative": False,
        },
        {
            "as_of_start": "2000-01-01",
            "as_of_end": "2024-11-19",
            "lots": {"BNF": 25, "NF": 50},
            "provenance_quality": "research_only_excluded",
            "authoritative": False,
        },
        {
            "as_of_start": "2024-11-20",
            "as_of_end": "2024-12-31",
            "lots": {"BNF": 15, "NF": 25},
            "provenance_quality": "research_only_excluded",
            "authoritative": False,
        },
    ],
    "aliases": {
        "BANKNIFTY": "BNF",
        "NIFTY BANK": "BNF",
        "NIFTY": "NF",
        "NIFTY 50": "NF",
    },
    "cycle_aliases": {
        "WEEKLY": "weekly",
        "W": "weekly",
        "MONTHLY": "monthly",
        "M": "monthly",
        "QUARTERLY": "quarterly",
        "Q": "quarterly",
        "HALF_YEARLY": "quarterly_half_yearly",
        "HALF-YEARLY": "quarterly_half_yearly",
        "HY": "quarterly_half_yearly",
        "QUARTERLY_HALF_YEARLY": "quarterly_half_yearly",
        "QH": "quarterly_half_yearly",
    },
}

_TABLE_CACHE: dict[str, Any] | None = None


def _parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()[:10]
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def normalize_index_key(raw: Any) -> str | None:
    """Map aliases to BNF/NF. Missing / unknown → None (never invent BNF)."""
    if raw is None or raw == "":
        return None
    text = str(raw).strip().upper()
    if text in ("", "UNKNOWN", "NONE", "NULL"):
        return None
    table = load_lot_table()
    aliases = table.get("aliases") or {}
    if text in aliases:
        return str(aliases[text])
    if text in ("BNF", "NF"):
        return text
    return None


def normalize_expiry_cycle(raw: Any) -> str | None:
    if raw is None or raw == "":
        return None
    text = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
    table = load_lot_table()
    aliases = table.get("cycle_aliases") or {}
    upper = text.upper()
    if upper in aliases:
        return str(aliases[upper])
    if text in aliases.values() or text in (
        "weekly",
        "monthly",
        "quarterly",
        "quarterly_half_yearly",
    ):
        return text
    return None


def load_lot_table() -> dict[str, Any]:
    """Load lot table from assets if present, else embedded mirror."""
    global _TABLE_CACHE
    if _TABLE_CACHE is not None:
        return _TABLE_CACHE
    candidates = [
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "assets",
            "contract_lot_table_v1.json",
        ),
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "assets",
            "contract_lot_table_v1.json",
        ),
        os.path.join("app", "src", "main", "assets", "contract_lot_table_v1.json"),
    ]
    for path in candidates:
        try:
            abs_path = os.path.abspath(path)
            if os.path.isfile(abs_path):
                with open(abs_path, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                if isinstance(payload, dict) and (
                    payload.get("authoritative_contract_rules") is not None
                    or payload.get("periods")
                ):
                    _TABLE_CACHE = payload
                    return _TABLE_CACHE
        except Exception:
            continue
    _TABLE_CACHE = dict(_EMBEDDED_TABLE)
    return _TABLE_CACHE


def clear_lot_table_cache() -> None:
    global _TABLE_CACHE
    _TABLE_CACHE = None


def lot_table_version_id() -> str:
    return str(load_lot_table().get("version_id") or LOT_TABLE_VERSION_ID)


def supported_project_data_window() -> dict[str, Any]:
    table = load_lot_table()
    return dict(table.get("supported_project_data_window") or {})


def research_only_excluded_periods() -> list[dict[str, Any]]:
    """Non-authoritative reconstructive / blanket rows — never used in resolution."""
    table = load_lot_table()
    return list(table.get("research_only_excluded") or [])


def current_declared_lots() -> dict[str, int]:
    """Operational current lots (CONST alignment) — not blanket historical authority."""
    table = load_lot_table()
    ops = table.get("operational_current_lots") or {}
    out = {}
    for k in ("BNF", "NF"):
        if k in ops:
            out[k] = int(ops[k])
    if out:
        return out
    # Legacy periods schema fallback (should not hit on v2)
    for period in table.get("periods") or []:
        if period.get("as_of_end") in (None, "") and period.get("authoritative") is not False:
            if str(period.get("provenance_quality") or "").startswith("excluded"):
                continue
            lots = period.get("lots") or {}
            return {k: int(v) for k, v in lots.items()}
    return {"BNF": 30, "NF": 65}


def _date_in_bounds(
    value: date | None,
    on_or_after: Any,
    on_or_before: Any,
) -> bool:
    if value is None:
        return False
    start = _parse_date(on_or_after)
    end = _parse_date(on_or_before)
    if start is not None and value < start:
        return False
    if end is not None and value > end:
        return False
    return True


def _rule_matches(
    rule: Mapping[str, Any],
    *,
    index: str,
    expiry: date | None,
    cycle: str | None,
    observation: date | None,
) -> bool:
    if str(rule.get("index") or "") != index:
        return False
    rule_cycle = normalize_expiry_cycle(rule.get("expiry_cycle"))
    if rule_cycle:
        if cycle is None:
            return False
        # quarterly matches quarterly_half_yearly rule and vice-versa for BNF/NF Q
        if rule_cycle != cycle:
            if not (
                {rule_cycle, cycle}
                <= {"quarterly", "quarterly_half_yearly"}
            ):
                return False
    if expiry is None:
        return False
    if not _date_in_bounds(
        expiry,
        rule.get("expiry_on_or_after"),
        rule.get("expiry_on_or_before"),
    ):
        return False
    # Observation bounds optional — if rule specifies them, observation required
    obs_after = rule.get("observation_on_or_after")
    obs_before = rule.get("observation_on_or_before")
    if obs_after not in (None, "") or obs_before not in (None, ""):
        if observation is None:
            return False
        if not _date_in_bounds(observation, obs_after, obs_before):
            return False
    return True


def _match_authoritative_rules(
    *,
    index: str,
    expiry: date | None,
    cycle: str | None,
    observation: date | None,
) -> list[dict[str, Any]]:
    table = load_lot_table()
    matches = []
    for rule in table.get("authoritative_contract_rules") or []:
        if _rule_matches(
            rule,
            index=index,
            expiry=expiry,
            cycle=cycle,
            observation=observation,
        ):
            matches.append(dict(rule))
    return matches


def _match_instrument_master_snapshot(
    *,
    index: str,
    expiry: date | None,
    observation: date | None,
) -> dict[str, Any] | None:
    if expiry is None or observation is None:
        return None
    table = load_lot_table()
    for snap in table.get("instrument_master_snapshots") or []:
        as_of = _parse_date(snap.get("as_of"))
        if as_of is None:
            continue
        # Snapshot verifies contracts near as_of; observation should be near as_of too
        if abs((observation - as_of).days) > 14:
            continue
        within = int(snap.get("expiry_within_days") or 90)
        if abs((expiry - as_of).days) > within:
            continue
        lots = snap.get("lots") or {}
        if index not in lots:
            continue
        return {
            "contract_lot_size": int(lots[index]),
            "snapshot_id": snap.get("snapshot_id"),
            "provenance": snap.get("provenance"),
            "provenance_quality": snap.get("provenance_quality"),
            "source_id": snap.get("snapshot_id"),
        }
    return None


def _unavailable(
    *,
    index_key: str | None,
    index_known: bool,
    n_lots: float,
    as_of_text: str | None,
    reason: str,
    version: str,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    out = {
        "index_key": index_key if index_known and index_key else "UNKNOWN",
        "index_known": index_known,
        "contract_lot_size": None,
        "number_of_lots": n_lots,
        "lot_size": None,
        "quantity_units": None,
        "lot_source": reason,
        "lot_table_version": version,
        "lot_as_of": as_of_text,
        "lot_period_start": None,
        "lot_period_end": None,
        "lot_provenance": None,
        "lot_provenance_quality": None,
        "resolved": False,
        "unavailable_reason": reason,
        "lot_conflict": False,
        "authoritative": False,
    }
    if extra:
        out.update(dict(extra))
    return out


def resolve_contract_lot(
    index_key: Any,
    as_of: Any = None,
    *,
    number_of_lots: Any = 1,
    expiry: Any = None,
    expiry_cycle: Any = None,
    captured_contract_lot: Any = None,
    instrument_key: Any = None,
    allow_operational_current: bool = False,
) -> dict[str, Any]:
    """Resolve units-per-lot with contract-specific identity.

    Prefer consistent captured_contract_lot. On conflict with an authoritative
    rule: flag conflict, return unresolved, retain both values. Fail closed
    when identity is insufficient (e.g. as_of-only during coexistence).

    Returns stamped provenance. Distinguishes:
      - contract_lot_size: units per lot
      - number_of_lots: quantity
      - lot_size / quantity_units: contract_lot_size * number_of_lots
    """
    idx = normalize_index_key(index_key)
    as_of_date = _parse_date(as_of)
    as_of_text = as_of_date.isoformat() if as_of_date else None
    expiry_date = _parse_date(expiry)
    cycle = normalize_expiry_cycle(expiry_cycle)
    table = load_lot_table()
    version = str(table.get("version_id") or LOT_TABLE_VERSION_ID)

    try:
        n_lots = float(number_of_lots) if number_of_lots not in (None, "") else 1.0
    except (TypeError, ValueError):
        n_lots = 1.0
    if n_lots <= 0:
        n_lots = 1.0

    captured = None
    if captured_contract_lot not in (None, ""):
        try:
            captured = int(float(captured_contract_lot))
            if captured <= 0:
                captured = None
        except (TypeError, ValueError):
            captured = None

    if idx is None:
        return _unavailable(
            index_key=None,
            index_known=False,
            n_lots=n_lots,
            as_of_text=as_of_text,
            reason="unknown_index",
            version=version,
            extra={
                "instrument_key": instrument_key,
                "expiry": expiry_date.isoformat() if expiry_date else None,
                "expiry_cycle": cycle,
                "captured_contract_lot": captured,
            },
        )

    matches = _match_authoritative_rules(
        index=idx,
        expiry=expiry_date,
        cycle=cycle,
        observation=as_of_date,
    )
    # If cycle missing, try matching any cycle for this expiry (unique lot only)
    if not matches and expiry_date is not None and cycle is None:
        trial_cycles = ("weekly", "monthly", "quarterly", "quarterly_half_yearly")
        lot_values: dict[int, list[dict[str, Any]]] = {}
        for c in trial_cycles:
            for rule in _match_authoritative_rules(
                index=idx,
                expiry=expiry_date,
                cycle=c,
                observation=as_of_date,
            ):
                lot_i = int(rule["contract_lot_size"])
                lot_values.setdefault(lot_i, []).append(rule)
        if len(lot_values) == 1:
            matches = next(iter(lot_values.values()))
        elif len(lot_values) > 1:
            return _unavailable(
                index_key=idx,
                index_known=True,
                n_lots=n_lots,
                as_of_text=as_of_text,
                reason="ambiguous_expiry_cycle_coexistence",
                version=version,
                extra={
                    "instrument_key": instrument_key,
                    "expiry": expiry_date.isoformat(),
                    "expiry_cycle": None,
                    "captured_contract_lot": captured,
                    "candidate_lots": sorted(lot_values.keys()),
                },
            )

    snap = _match_instrument_master_snapshot(
        index=idx, expiry=expiry_date, observation=as_of_date
    )

    authoritative_lot = None
    provenance = None
    provenance_quality = None
    source_id = None
    rule_id = None
    if matches:
        lots_found = {int(m["contract_lot_size"]) for m in matches}
        if len(lots_found) > 1:
            return _unavailable(
                index_key=idx,
                index_known=True,
                n_lots=n_lots,
                as_of_text=as_of_text,
                reason="conflicting_authoritative_rules",
                version=version,
                extra={
                    "instrument_key": instrument_key,
                    "expiry": expiry_date.isoformat() if expiry_date else None,
                    "expiry_cycle": cycle,
                    "captured_contract_lot": captured,
                    "candidate_lots": sorted(lots_found),
                    "matched_rule_ids": [m.get("rule_id") for m in matches],
                },
            )
        best = matches[0]
        authoritative_lot = int(best["contract_lot_size"])
        provenance = best.get("source_id")
        provenance_quality = best.get("quality") or "circular_annexure"
        source_id = best.get("source_id")
        rule_id = best.get("rule_id")
    elif snap is not None:
        authoritative_lot = int(snap["contract_lot_size"])
        provenance = snap.get("provenance")
        provenance_quality = snap.get("provenance_quality")
        source_id = snap.get("source_id")
        rule_id = snap.get("snapshot_id")

    # Captured metadata preference / conflict handling
    if captured is not None:
        if authoritative_lot is not None and captured != authoritative_lot:
            return {
                "index_key": idx,
                "index_known": True,
                "contract_lot_size": None,
                "number_of_lots": n_lots,
                "lot_size": None,
                "quantity_units": None,
                "lot_source": "captured_vs_rule_conflict",
                "lot_table_version": version,
                "lot_as_of": as_of_text,
                "lot_period_start": None,
                "lot_period_end": None,
                "lot_provenance": provenance,
                "lot_provenance_quality": "conflict",
                "resolved": False,
                "unavailable_reason": "captured_vs_rule_conflict",
                "lot_conflict": True,
                "authoritative": False,
                "captured_contract_lot": captured,
                "rule_contract_lot": authoritative_lot,
                "matched_rule_id": rule_id,
                "source_id": source_id,
                "instrument_key": instrument_key,
                "expiry": expiry_date.isoformat() if expiry_date else None,
                "expiry_cycle": cycle,
                "exclude_authoritative_calc": True,
                "original_retained": True,
            }
        # Captured consistent with rule, or no rule — prefer captured
        contract_lot = captured
        lot_source = (
            "captured_metadata_consistent"
            if authoritative_lot is not None
            else "captured_metadata"
        )
        return {
            "index_key": idx,
            "index_known": True,
            "contract_lot_size": int(contract_lot),
            "number_of_lots": n_lots,
            "lot_size": float(contract_lot) * n_lots,
            "quantity_units": float(contract_lot) * n_lots,
            "lot_source": lot_source,
            "lot_table_version": version,
            "lot_as_of": as_of_text,
            "lot_period_start": None,
            "lot_period_end": None,
            "lot_provenance": provenance or "captured",
            "lot_provenance_quality": provenance_quality or "captured",
            "resolved": True,
            "unavailable_reason": None,
            "lot_conflict": False,
            "authoritative": authoritative_lot is not None,
            "captured_contract_lot": captured,
            "matched_rule_id": rule_id,
            "source_id": source_id,
            "instrument_key": instrument_key,
            "expiry": expiry_date.isoformat() if expiry_date else None,
            "expiry_cycle": cycle,
        }

    if authoritative_lot is not None:
        return {
            "index_key": idx,
            "index_known": True,
            "contract_lot_size": int(authoritative_lot),
            "number_of_lots": n_lots,
            "lot_size": float(authoritative_lot) * n_lots,
            "quantity_units": float(authoritative_lot) * n_lots,
            "lot_source": "authoritative_contract_rule",
            "lot_table_version": version,
            "lot_as_of": as_of_text,
            "lot_period_start": None,
            "lot_period_end": None,
            "lot_provenance": provenance,
            "lot_provenance_quality": provenance_quality,
            "resolved": True,
            "unavailable_reason": None,
            "lot_conflict": False,
            "authoritative": True,
            "matched_rule_id": rule_id,
            "source_id": source_id,
            "instrument_key": instrument_key,
            "expiry": expiry_date.isoformat() if expiry_date else None,
            "expiry_cycle": cycle,
        }

    # Operational current fallback: only when explicitly allowed and no historical
    # identity was supplied (no as_of / expiry). Never invent historical lots.
    if allow_operational_current and as_of_date is None and expiry_date is None:
        ops = current_declared_lots()
        if idx in ops:
            contract_lot = int(ops[idx])
            return {
                "index_key": idx,
                "index_known": True,
                "contract_lot_size": contract_lot,
                "number_of_lots": n_lots,
                "lot_size": float(contract_lot) * n_lots,
                "quantity_units": float(contract_lot) * n_lots,
                "lot_source": "operational_current_lots",
                "lot_table_version": version,
                "lot_as_of": None,
                "lot_period_start": None,
                "lot_period_end": None,
                "lot_provenance": "operational_current_lots",
                "lot_provenance_quality": "operational_not_historical_authority",
                "resolved": True,
                "unavailable_reason": None,
                "lot_conflict": False,
                "authoritative": False,
                "instrument_key": instrument_key,
                "expiry": None,
                "expiry_cycle": cycle,
            }

    # as_of-only during coexistence / unsupported history → unavailable
    # Never consult research_only_excluded rows.
    reason = "unsupported_or_ambiguous_contract"
    if expiry_date is None and as_of_date is not None:
        reason = "as_of_only_insufficient_coexistence"
    elif as_of_date is not None:
        window = supported_project_data_window()
        start = _parse_date(window.get("start"))
        end = _parse_date(window.get("end"))
        if start and as_of_date < start:
            reason = "outside_supported_project_data_window"
        elif end and as_of_date > end:
            reason = "outside_supported_project_data_window"

    return _unavailable(
        index_key=idx,
        index_known=True,
        n_lots=n_lots,
        as_of_text=as_of_text,
        reason=reason,
        version=version,
        extra={
            "instrument_key": instrument_key,
            "expiry": expiry_date.isoformat() if expiry_date else None,
            "expiry_cycle": cycle,
            "captured_contract_lot": captured,
            "research_only_excluded_consulted": False,
        },
    )


def holiday_years_covered(holidays: Any) -> set[str]:
    years: set[str] = set()
    for h in holidays or []:
        text = str(h)[:10]
        if len(text) >= 4 and text[:4].isdigit():
            years.add(text[:4])
    return years


def calendar_coverage_for_interval(
    session_date: Any,
    expiry: Any,
    *,
    holidays: Any = None,
) -> dict[str, Any]:
    """Validate holiday calendar coverage across session→expiry (incl. year boundaries)."""
    sd = _parse_date(session_date)
    ed = _parse_date(expiry)
    if sd is None or ed is None:
        return {
            "coverage_ok": False,
            "dte_basis": "unknown",
            "holiday_calendar_used": False,
            "missing_years": [],
            "covered_years": [],
            "calendar_version": None,
        }
    if holidays is None:
        try:
            import brain as _brain  # type: ignore

            holidays = (_brain._CONST or {}).get("NSE_HOLIDAYS") or []
        except Exception:
            holidays = []
    covered = holiday_years_covered(holidays)
    needed: set[str] = set()
    y = sd.year
    while y <= ed.year:
        needed.add(str(y))
        y += 1
    missing = sorted(needed - covered)
    ok = len(missing) == 0 and len(needed) > 0
    cal_version = (
        f"{DTE_CALENDAR_VERSION_PREFIX}:{','.join(sorted(covered))}" if covered else None
    )
    if ok:
        basis = "nse_trading_calendar"
    elif covered:
        basis = "nse_trading_calendar_incomplete"
    else:
        basis = "weekday_only_approximation"
    return {
        "coverage_ok": ok,
        "dte_basis": basis,
        "holiday_calendar_used": bool(covered),
        "missing_years": missing,
        "covered_years": sorted(covered),
        "calendar_version": cal_version,
        "needed_years": sorted(needed),
    }


def calendar_dte(session_date: Any, expiry: Any) -> int | None:
    """Calendar DTE = max(expiry − session, 0). Fail-closed on parse miss.

    Post-expiry (expiry < session) → None (do not silently treat as eligible 0).
    """
    sd = _parse_date(session_date)
    ed = _parse_date(expiry)
    if sd is None or ed is None:
        return None
    if ed < sd:
        return None
    return (ed - sd).days


def trading_dte(
    session_date: Any,
    expiry: Any,
    *,
    holidays: Any = None,
) -> dict[str, Any]:
    """Trading DTE: count session..expiry inclusive, skipping weekends/holidays.

    Does NOT silently substitute calendar DTE when trading coverage is missing —
    trading_dte is None and calendar_dte may still be present under its own name.
    On expiry day (trading session): trading_dte=1; calendar_dte=0.
    Post-expiry: both None (expired contracts not eligible via zero-day).
    """
    sd = _parse_date(session_date)
    ed = _parse_date(expiry)
    if sd is None or ed is None:
        return {
            "trading_dte": None,
            "calendar_dte": None,
            "dte_basis": "unknown",
            "holiday_calendar_used": False,
            "calendar_coverage_ok": False,
            "calendar_version": None,
            "trading_dte_unavailable_reason": "unparseable_dates",
        }
    if ed < sd:
        return {
            "trading_dte": None,
            "calendar_dte": None,
            "dte_basis": "expiry_before_session",
            "holiday_calendar_used": False,
            "calendar_coverage_ok": False,
            "calendar_version": None,
            "trading_dte_unavailable_reason": "post_expiry",
        }

    cal = (ed - sd).days
    if holidays is None:
        try:
            import brain as _brain  # type: ignore

            raw = (_brain._CONST or {}).get("NSE_HOLIDAYS") or []
            holiday_list = list(raw)
        except Exception:
            holiday_list = []
    else:
        holiday_list = list(holidays or [])

    coverage = calendar_coverage_for_interval(sd, ed, holidays=holiday_list)
    holiday_set = {str(h)[:10] for h in holiday_list}

    if not coverage["coverage_ok"]:
        # Calendar DTE remains available; trading DTE unavailable — no substitution.
        return {
            "trading_dte": None,
            "calendar_dte": cal,
            "dte_basis": coverage["dte_basis"],
            "holiday_calendar_used": coverage["holiday_calendar_used"],
            "calendar_coverage_ok": False,
            "calendar_version": coverage.get("calendar_version"),
            "missing_holiday_years": coverage.get("missing_years"),
            "trading_dte_unavailable_reason": "holiday_calendar_coverage_incomplete",
        }

    count = 0
    current = sd
    while current <= ed:
        if current.weekday() < 5 and current.isoformat() not in holiday_set:
            count += 1
        current += timedelta(days=1)

    return {
        "trading_dte": int(count),
        "calendar_dte": cal,
        "dte_basis": "nse_trading_calendar",
        "holiday_calendar_used": True,
        "calendar_coverage_ok": True,
        "calendar_version": coverage.get("calendar_version"),
        "trading_dte_unavailable_reason": None,
    }


def measurement_dte_bucket(dte: Any, *, dte_kind: str = "measurement") -> str:
    """Measurement bucket from a DTE value. Callers must pass the correct kind.

    Do not pass calendar_dte into a trading-DTE measurement path silently —
    use dte_kind to document intent; unknown/missing → UNKNOWN.
    """
    if dte is None or dte == "":
        return "UNKNOWN"
    try:
        d = int(float(dte))
    except (TypeError, ValueError):
        return "UNKNOWN"
    if d < 0:
        return "UNKNOWN"
    if d <= 0:
        return "DTE_0"
    if d <= 2:
        return "DTE_1_2"
    if d <= 7:
        return "DTE_3_7"
    return "DTE_8_PLUS"


def ranking_dte_bucket(dte: Any) -> str:
    """Stage2a ranking buckets — keep separate from measurement."""
    if dte is None or dte == "":
        return "unknown"
    try:
        d = int(max(round(float(dte)), 0))
    except (TypeError, ValueError):
        return "unknown"
    if d <= 0:
        return "DTE_0"
    if d == 1:
        return "DTE_1"
    if d <= 3:
        return "DTE_2_3"
    if d <= 7:
        return "DTE_4_7"
    return "DTE_8_PLUS"


def contract_identity_fields_for_json(identity: Mapping[str, Any]) -> dict[str, Any]:
    """Stable subset for JSON lineage / snapshot persistence round-trip."""
    keys = (
        "index_key",
        "index_known",
        "expiry",
        "expiry_cycle",
        "instrument_key",
        "calendar_dte",
        "trading_dte",
        "dte",
        "tDTE",
        "dte_source",
        "dte_basis",
        "dte_bucket",
        "dte_bucket_version",
        "dte_ranking_bucket",
        "dte_ranking_bucket_version",
        "calendar_version",
        "calendar_coverage_ok",
        "contract_lot_size",
        "number_of_lots",
        "lot_size",
        "quantity_units",
        "lot_source",
        "lot_table_version",
        "lot_as_of",
        "lot_size_assumed",
        "lot_size_source",
        "lot_conflict",
        "lot_provenance",
        "lot_provenance_quality",
        "matched_rule_id",
        "source_id",
        "captured_contract_lot",
        "rule_contract_lot",
        "exclude_authoritative_calc",
        "identity_complete",
        "contract_identity_quarantine",
        "evaluation_ineligible",
        "calibration_ineligible",
        "unavailable_reason",
    )
    out = {}
    for k in keys:
        if k in identity:
            out[k] = identity[k]
    return out


def json_round_trip_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize → deserialize contract identity; prove field preservation."""
    payload = contract_identity_fields_for_json(identity)
    return json.loads(json.dumps(payload, default=str))


def simulate_persistence_boundary_roundtrip(
    identity: Mapping[str, Any],
    *,
    role: str = "primary",
) -> dict[str, Any]:
    """Exercise candidate→lineage→upload-payload→JSON storage→readback (mock).

    Does not touch production DB. Identifies the untested DB boundary explicitly.
    """
    stamped = contract_identity_fields_for_json(identity)
    lineage = {
        "evaluation_lineage": {
            "contract_identity": stamped,
            "role": role,
            "persistence_mock": True,
        },
        "role": role,
        **{k: stamped[k] for k in ("index_key", "expiry", "contract_lot_size",
                                    "number_of_lots", "lot_size", "calendar_dte",
                                    "trading_dte", "lot_table_version")
           if k in stamped},
    }
    # Kotlin compaction analogue: keep identity keys, drop bulky diagnostics
    compacted = {
        "role": role,
        "contract_identity": stamped,
        "index_key": stamped.get("index_key"),
        "expiry": stamped.get("expiry"),
        "contract_lot_size": stamped.get("contract_lot_size"),
        "number_of_lots": stamped.get("number_of_lots"),
        "lot_size": stamped.get("lot_size"),
        "calendar_dte": stamped.get("calendar_dte"),
        "trading_dte": stamped.get("trading_dte"),
        "lot_source": stamped.get("lot_source"),
        "lot_table_version": stamped.get("lot_table_version"),
        "identity_complete": stamped.get("identity_complete"),
    }
    upload_payload = {"outcomes": [compacted], "schema": "mock_upload_v1"}
    stored = json.loads(json.dumps(upload_payload, default=str))
    readback = stored["outcomes"][0]
    metrics_slice = {
        "index_key": readback.get("index_key"),
        "dte_bucket": measurement_dte_bucket(
            readback.get("calendar_dte")
            if readback.get("calendar_dte") is not None
            else readback.get("trading_dte")
        ),
        "lot_size": readback.get("lot_size"),
        "lot_table_version": readback.get("lot_table_version"),
        "support_definition": "rows_in_mock_slice",
    }
    return {
        "lineage": lineage,
        "compacted": compacted,
        "upload_payload": upload_payload,
        "readback": readback,
        "metrics_slice": metrics_slice,
        "fields_preserved": all(
            readback.get(k) == stamped.get(k)
            for k in (
                "index_key",
                "expiry",
                "contract_lot_size",
                "number_of_lots",
                "lot_size",
                "calendar_dte",
                "trading_dte",
                "lot_table_version",
            )
            if k in stamped
        ),
        "untested_db_boundary": (
            "Production Supabase/Postgres outcome columns and "
            "saveEvaluationOutcomes remote upsert are NOT exercised; "
            "JSON mock only. Mid-loop remote persistence remains end-of-run."
        ),
        "db_boundary_tested": False,
    }


__all__ = [
    "LOT_TABLE_VERSION_ID",
    "DTE_RANKING_BUCKET_VERSION",
    "DTE_RANKING_BUCKETS",
    "DTE_MEASUREMENT_BUCKET_VERSION",
    "DTE_MEASUREMENT_BUCKETS",
    "normalize_index_key",
    "normalize_expiry_cycle",
    "load_lot_table",
    "clear_lot_table_cache",
    "lot_table_version_id",
    "supported_project_data_window",
    "research_only_excluded_periods",
    "current_declared_lots",
    "resolve_contract_lot",
    "calendar_dte",
    "trading_dte",
    "calendar_coverage_for_interval",
    "holiday_years_covered",
    "measurement_dte_bucket",
    "ranking_dte_bucket",
    "contract_identity_fields_for_json",
    "json_round_trip_identity",
    "simulate_persistence_boundary_roundtrip",
]
