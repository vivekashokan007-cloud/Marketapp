"""Dated contract lot table — single source of truth (Python + Kotlin mirror).

Contract lot size = units per lot (e.g. BNF 30). Distinct from number_of_lots
(quantity). Never invent BNF when index is missing — fail-closed to None/UNKNOWN.

SSOT file: app/src/main/assets/contract_lot_table_v1.json
Mirrored constant below keeps resolution working when assets path is unavailable.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Optional

LOT_TABLE_VERSION_ID = "contract_lot_table_v1_20260913"

# Ranking DTE buckets (stage2a / teacher prior) — NOT measurement partitions.
DTE_RANKING_BUCKET_VERSION = "dte_ranking_buckets_v1_stage2a_0_1_2_3_4_7_8plus_20260913"
DTE_RANKING_BUCKETS = ("DTE_0", "DTE_1", "DTE_2_3", "DTE_4_7", "DTE_8_PLUS", "unknown")

# Measurement DTE buckets — reporting only.
DTE_MEASUREMENT_BUCKET_VERSION = "dte_measurement_buckets_v1_0_1_2_3_7_8plus_20260913"
DTE_MEASUREMENT_BUCKETS = ("DTE_0", "DTE_1_2", "DTE_3_7", "DTE_8_PLUS", "UNKNOWN")

# Embedded mirror of assets/contract_lot_table_v1.json (keep in sync).
_EMBEDDED_TABLE: dict[str, Any] = {
    "version_id": LOT_TABLE_VERSION_ID,
    "schema": "contract_lot_table_dated_periods",
    "unit": "units_per_lot",
    "note": (
        "Contract lot size = units per lot. Distinct from number_of_lots (quantity). "
        "Resolve by (index, as_of/session_date). Fail-closed when index unknown."
    ),
    "indices": ["BNF", "NF"],
    "periods": [
        {
            "as_of_start": "2025-01-01",
            "as_of_end": None,
            "lots": {"BNF": 30, "NF": 65},
            "provenance": "upstox_instrument_master_verified_20260719",
            "provenance_quality": "verified",
            "evidence": "reports/lot_size_verification_20260719.json",
        },
        {
            "as_of_start": "2024-11-20",
            "as_of_end": "2024-12-31",
            "lots": {"BNF": 15, "NF": 25},
            "provenance": "nse_fo_lot_revision_2024_reconstructive",
            "provenance_quality": "reconstructive",
            "evidence": "NSE F&O lot-size revision window (reconstructive)",
        },
        {
            "as_of_start": "2000-01-01",
            "as_of_end": "2024-11-19",
            "lots": {"BNF": 25, "NF": 50},
            "provenance": "pre_2024_nse_index_lots_reconstructive",
            "provenance_quality": "reconstructive",
            "evidence": "Pre-revision NSE index F&O lots (reconstructive)",
        },
    ],
    "aliases": {
        "BANKNIFTY": "BNF",
        "NIFTY BANK": "BNF",
        "NIFTY": "NF",
        "NIFTY 50": "NF",
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


def load_lot_table() -> dict[str, Any]:
    """Load dated lot table from assets if present, else embedded mirror."""
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
        # Marketapp/app/src/main/assets when cwd is repo root
        os.path.join("app", "src", "main", "assets", "contract_lot_table_v1.json"),
    ]
    for path in candidates:
        try:
            abs_path = os.path.abspath(path)
            if os.path.isfile(abs_path):
                with open(abs_path, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                if isinstance(payload, dict) and payload.get("periods"):
                    _TABLE_CACHE = payload
                    return _TABLE_CACHE
        except Exception:
            continue
    _TABLE_CACHE = dict(_EMBEDDED_TABLE)
    return _TABLE_CACHE


def lot_table_version_id() -> str:
    return str(load_lot_table().get("version_id") or LOT_TABLE_VERSION_ID)


def current_declared_lots() -> dict[str, int]:
    """Lots from the open-ended (current) period."""
    table = load_lot_table()
    for period in table.get("periods") or []:
        if period.get("as_of_end") in (None, ""):
            lots = period.get("lots") or {}
            return {k: int(v) for k, v in lots.items()}
    # Fall back to first period
    periods = table.get("periods") or []
    if periods:
        lots = periods[0].get("lots") or {}
        return {k: int(v) for k, v in lots.items()}
    return {}


def resolve_contract_lot(
    index_key: Any,
    as_of: Any = None,
    *,
    number_of_lots: Any = 1,
) -> dict[str, Any]:
    """Resolve units-per-lot by (index, as_of). Fail-closed when index unknown.

    Returns stamped provenance fields. Distinguishes:
      - contract_lot_size: units per lot
      - number_of_lots: quantity
      - lot_size: contract_lot_size * number_of_lots (total units for P&L scale)
    """
    idx = normalize_index_key(index_key)
    as_of_date = _parse_date(as_of) or date.today()
    as_of_text = as_of_date.isoformat()
    table = load_lot_table()
    version = str(table.get("version_id") or LOT_TABLE_VERSION_ID)

    try:
        n_lots = float(number_of_lots) if number_of_lots not in (None, "") else 1.0
    except (TypeError, ValueError):
        n_lots = 1.0
    if n_lots <= 0:
        n_lots = 1.0

    if idx is None:
        return {
            "index_key": "UNKNOWN",
            "index_known": False,
            "contract_lot_size": None,
            "number_of_lots": n_lots,
            "lot_size": None,
            "lot_source": "unknown",
            "lot_table_version": version,
            "lot_as_of": as_of_text,
            "lot_period_start": None,
            "lot_period_end": None,
            "lot_provenance": None,
            "lot_provenance_quality": None,
            "resolved": False,
        }

    matched = None
    for period in table.get("periods") or []:
        start = _parse_date(period.get("as_of_start"))
        end = _parse_date(period.get("as_of_end"))
        if start is None:
            continue
        if as_of_date < start:
            continue
        if end is not None and as_of_date > end:
            continue
        matched = period
        break

    if matched is None:
        return {
            "index_key": idx,
            "index_known": True,
            "contract_lot_size": None,
            "number_of_lots": n_lots,
            "lot_size": None,
            "lot_source": "no_period_match",
            "lot_table_version": version,
            "lot_as_of": as_of_text,
            "lot_period_start": None,
            "lot_period_end": None,
            "lot_provenance": None,
            "lot_provenance_quality": None,
            "resolved": False,
        }

    lots_map = matched.get("lots") or {}
    contract_lot = lots_map.get(idx)
    if contract_lot is None:
        return {
            "index_key": idx,
            "index_known": True,
            "contract_lot_size": None,
            "number_of_lots": n_lots,
            "lot_size": None,
            "lot_source": "index_missing_in_period",
            "lot_table_version": version,
            "lot_as_of": as_of_text,
            "lot_period_start": matched.get("as_of_start"),
            "lot_period_end": matched.get("as_of_end"),
            "lot_provenance": matched.get("provenance"),
            "lot_provenance_quality": matched.get("provenance_quality"),
            "resolved": False,
        }

    contract_lot_f = float(contract_lot)
    return {
        "index_key": idx,
        "index_known": True,
        "contract_lot_size": int(contract_lot),
        "number_of_lots": n_lots,
        "lot_size": contract_lot_f * n_lots,
        "lot_source": "dated_contract_table",
        "lot_table_version": version,
        "lot_as_of": as_of_text,
        "lot_period_start": matched.get("as_of_start"),
        "lot_period_end": matched.get("as_of_end"),
        "lot_provenance": matched.get("provenance"),
        "lot_provenance_quality": matched.get("provenance_quality"),
        "resolved": True,
    }


def calendar_dte(session_date: Any, expiry: Any) -> int | None:
    """Calendar DTE = max(expiry − session, 0). Fail-closed on parse miss."""
    sd = _parse_date(session_date)
    ed = _parse_date(expiry)
    if sd is None or ed is None:
        return None
    return max((ed - sd).days, 0)


def trading_dte(
    session_date: Any,
    expiry: Any,
    *,
    holidays: Any = None,
) -> dict[str, Any]:
    """Trading DTE: count session..expiry inclusive, skipping weekends/holidays.

    Uses NSE holiday list when provided. On expiry day (trading session): 1
    (sessions remaining including today). Calendar DTE on expiry day is 0.
    """
    sd = _parse_date(session_date)
    ed = _parse_date(expiry)
    if sd is None or ed is None:
        return {
            "trading_dte": None,
            "calendar_dte": None,
            "dte_basis": "unknown",
            "holiday_calendar_used": False,
        }
    cal = max((ed - sd).days, 0)
    if ed < sd:
        return {
            "trading_dte": None,
            "calendar_dte": None,
            "dte_basis": "expiry_before_session",
            "holiday_calendar_used": False,
        }

    holiday_set: set[str] = set()
    holiday_used = False
    if holidays is None:
        try:
            # Prefer brain._CONST when available (same process).
            import brain as _brain  # type: ignore

            raw = (_brain._CONST or {}).get("NSE_HOLIDAYS") or []
            holiday_set = {str(h)[:10] for h in raw}
            holiday_used = bool(holiday_set)
        except Exception:
            holiday_set = set()
            holiday_used = False
    else:
        holiday_set = {str(h)[:10] for h in (holidays or [])}
        holiday_used = True

    count = 0
    current = sd
    while current <= ed:
        if current.weekday() < 5 and current.isoformat() not in holiday_set:
            count += 1
        current += timedelta(days=1)

    basis = "nse_trading_calendar" if holiday_used else "weekday_only_approximation"
    return {
        "trading_dte": int(count),
        "calendar_dte": cal,
        "dte_basis": basis,
        "holiday_calendar_used": holiday_used,
    }


def measurement_dte_bucket(dte: Any) -> str:
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
        "contract_lot_size",
        "number_of_lots",
        "lot_size",
        "lot_source",
        "lot_table_version",
        "lot_as_of",
        "lot_size_assumed",
        "lot_size_source",
        "identity_complete",
        "contract_identity_quarantine",
        "evaluation_ineligible",
        "calibration_ineligible",
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


__all__ = [
    "LOT_TABLE_VERSION_ID",
    "DTE_RANKING_BUCKET_VERSION",
    "DTE_RANKING_BUCKETS",
    "DTE_MEASUREMENT_BUCKET_VERSION",
    "DTE_MEASUREMENT_BUCKETS",
    "normalize_index_key",
    "load_lot_table",
    "lot_table_version_id",
    "current_declared_lots",
    "resolve_contract_lot",
    "calendar_dte",
    "trading_dte",
    "measurement_dte_bucket",
    "ranking_dte_bucket",
    "contract_identity_fields_for_json",
    "json_round_trip_identity",
]
