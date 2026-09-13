"""Notification delivery lineage and idempotency contract.

Covers NotificationAgent / PositionTickService / NativeBridge recovery
surfaces without changing live alert thresholds. Device restart must be
able to restore ack/cooldown state via a stable lineage key.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

NOTIFICATION_LINEAGE_VERSION = "notification_lineage_v1_20260913"
IDEMPOTENCY_SCOPE_POSITION = "position_alert"
IDEMPOTENCY_SCOPE_OPERATIONAL = "operational_alert"
IDEMPOTENCY_SCOPE_EVALUATION = "evaluation_status"


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def build_notification_lineage(
    *,
    scope: str,
    alert_key: str,
    trade_id: Any = None,
    decision_type: Optional[str] = None,
    owner: Optional[str] = None,
    session_date: Optional[str] = None,
    delivery_attempt: int = 1,
    posted_to_os: bool = False,
    acked: bool = False,
    device_id: Optional[str] = None,
) -> dict:
    scope_norm = str(scope or "").strip() or "unknown"
    key_norm = str(alert_key or "").strip()
    material = {
        "scope": scope_norm,
        "alert_key": key_norm,
        "trade_id": str(trade_id) if trade_id is not None else None,
        "decision_type": decision_type,
        "owner": owner,
        "session_date": session_date,
        "device_id": device_id,
    }
    digest = hashlib.sha256(_stable_json(material).encode("utf-8")).hexdigest()[:24]
    idempotency_key = f"{scope_norm}|{key_norm}|{digest}"
    return {
        "lineage_version": NOTIFICATION_LINEAGE_VERSION,
        "scope": scope_norm,
        "alert_key": key_norm,
        "trade_id": material["trade_id"],
        "decision_type": decision_type,
        "owner": owner,
        "session_date": session_date,
        "device_id": device_id,
        "delivery_attempt": int(delivery_attempt or 1),
        "posted_to_os": bool(posted_to_os),
        "acked": bool(acked),
        "idempotency_key": idempotency_key,
        "recovery_contract": (
            "restore agent_state.position_alert_states by state_key; "
            "identical idempotency_key must not double-post after device recovery"
        ),
    }


def merge_agent_state_for_recovery(existing: Optional[dict], incoming: Optional[dict]) -> dict:
    """Idempotent merge used after Android process restart."""
    base = dict(existing or {}) if isinstance(existing, dict) else {}
    nxt = dict(incoming or {}) if isinstance(incoming, dict) else {}
    out = {
        "notification_lineage_version": NOTIFICATION_LINEAGE_VERSION,
        "position_alert_keys": sorted(
            set(base.get("position_alert_keys") or [])
            | set(nxt.get("position_alert_keys") or [])
        ),
        "operational_alert_keys": sorted(
            set(base.get("operational_alert_keys") or [])
            | set(nxt.get("operational_alert_keys") or [])
        ),
        "position_alert_states": {},
    }
    states = {}
    for source in (base.get("position_alert_states"), nxt.get("position_alert_states")):
        if isinstance(source, dict):
            for key, value in source.items():
                prev = states.get(key)
                try:
                    prev_ms = int(prev) if prev is not None else None
                except (TypeError, ValueError):
                    prev_ms = None
                try:
                    cur_ms = int(value)
                except (TypeError, ValueError):
                    continue
                if prev_ms is None or cur_ms >= prev_ms:
                    states[str(key)] = cur_ms
        elif isinstance(source, list):
            # Legacy list form — ignore unknown shapes rather than inventing acks.
            continue
    out["position_alert_states"] = dict(sorted(states.items()))
    return out


def should_suppress_duplicate(lineage: dict, seen_keys: set) -> bool:
    key = str((lineage or {}).get("idempotency_key") or "")
    if not key:
        return False
    if key in seen_keys:
        return True
    seen_keys.add(key)
    return False
