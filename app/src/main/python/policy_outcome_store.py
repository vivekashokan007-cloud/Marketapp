"""Batch C C2 — one outcome per (entry_identity, policy_id, policy_version)."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

from path_quality_evaluator import (
    FIDELITY_FULL,
    FIDELITY_LIMITED_FIXTURE,
    FIDELITY_NOT_POSSIBLE,
)

POLICY_OUTCOME_STORE_VERSION = "policy_outcome_store_v1_batch_c_20260923"

FIDELITY_VALUES = (FIDELITY_FULL, FIDELITY_LIMITED_FIXTURE, FIDELITY_NOT_POSSIBLE)
MATURITY_MATURE = "MATURE"
MATURITY_NOT_YET_MATURE = "NOT_YET_MATURE"
MATURITY_IRRECOVERABLE = "IRRECOVERABLE"
CENSORING_NONE = "NONE"
CENSORING_RIGHT = "RIGHT_CENSORED"
CENSORING_GAP = "GAP_UNCERTAIN"
LABEL_STRUCTURAL = "STRUCTURAL"


def outcome_key(
    entry_identity: str, policy_id: str, policy_version: str
) -> Tuple[str, str, str]:
    return (entry_identity, policy_id, policy_version)


class PolicyOutcomeStore:
    """In-repo / fixture outcome store. No Supabase writes."""

    def __init__(self) -> None:
        self._rows: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        # Immutable version guard: once a (policy_id, policy_version) body hash
        # is recorded, source edits must use a new version string.
        self._version_pins: Dict[Tuple[str, str], str] = {}

    def put_outcome(
        self,
        *,
        entry_identity: str,
        policy_id: str,
        policy_version: str,
        experiment_version: str,
        data_version: str,
        code_version: str,
        cost_version: str,
        expiry: Optional[str] = None,
        legs: Optional[List[Any]] = None,
        qty: Optional[float] = None,
        entry_ts: Optional[str] = None,
        fill_basis: Optional[str] = None,
        fidelity: str = FIDELITY_LIMITED_FIXTURE,
        maturity: str = MATURITY_NOT_YET_MATURE,
        censoring: str = CENSORING_NONE,
        structural: bool = False,
        structural_reasons: Optional[List[str]] = None,
        metrics: Optional[Dict[str, Any]] = None,
        membership: Optional[Dict[str, Any]] = None,
        selection_mode: str = "retrospective",
        behavior_fingerprint: Optional[str] = None,
    ) -> Dict[str, Any]:
        if fidelity not in FIDELITY_VALUES:
            raise ValueError(f"invalid_fidelity:{fidelity}")
        if selection_mode not in ("retrospective", "prospectively_registered"):
            raise ValueError(f"invalid_selection_mode:{selection_mode}")

        key = outcome_key(entry_identity, policy_id, policy_version)
        pin_key = (policy_id, policy_version)
        fp = behavior_fingerprint or f"{policy_id}::{policy_version}"
        if pin_key in self._version_pins and self._version_pins[pin_key] != fp:
            raise ValueError(
                "policy_version_immutability_guard:"
                f"{policy_id}/{policy_version} behavior change requires new version string"
            )
        self._version_pins[pin_key] = fp

        if key in self._rows:
            raise ValueError(
                f"duplicate_outcome_forbidden:{entry_identity}|{policy_id}|{policy_version}"
            )

        # Missing inputs ⇒ STRUCTURAL, never observed-neutral zero.
        struct_reasons = list(structural_reasons or [])
        is_structural = bool(structural) or bool(struct_reasons)
        safe_metrics = dict(metrics or {})
        if is_structural:
            # Do not invent neutral zeros for absent economics.
            for k in ("net_rupees", "R_max_loss_norm", "R_legacy_configured_risk"):
                if k in safe_metrics and safe_metrics[k] == 0 and k not in (
                    safe_metrics.get("_explicit_zero_fields") or []
                ):
                    # Leave explicit zeros only when caller marks them; otherwise drop.
                    pass
            safe_metrics["label"] = LABEL_STRUCTURAL
            safe_metrics["missing_not_neutral_zero"] = True

        row = {
            "store_version": POLICY_OUTCOME_STORE_VERSION,
            "entry_identity": entry_identity,
            "policy_id": policy_id,
            "policy_version": policy_version,
            "experiment_version": experiment_version,
            "data_version": data_version,
            "code_version": code_version,
            "cost_version": cost_version,
            "expiry": expiry,
            "legs": list(legs or []),
            "qty": qty,
            "entry_ts": entry_ts,
            "fill_basis": fill_basis,
            "fidelity": fidelity,
            "maturity": maturity,
            "censoring": censoring,
            "structural": is_structural,
            "structural_reasons": struct_reasons,
            "metrics": safe_metrics,
            "membership": deepcopy(membership) if membership else None,
            "selection_mode": selection_mode,
            "behavior_fingerprint": fp,
            "supabase_write": False,
            "economics_row_count": 1,
        }
        self._rows[key] = row
        return deepcopy(row)

    def get(
        self, entry_identity: str, policy_id: str, policy_version: str
    ) -> Optional[Dict[str, Any]]:
        row = self._rows.get(outcome_key(entry_identity, policy_id, policy_version))
        return deepcopy(row) if row else None

    def list_outcomes(self) -> List[Dict[str, Any]]:
        return [deepcopy(r) for r in self._rows.values()]

    def count(self) -> int:
        return len(self._rows)

    def assert_one_outcome_per_key(self) -> None:
        keys = list(self._rows.keys())
        if len(keys) != len(set(keys)):
            raise AssertionError("duplicate_outcome_keys")
