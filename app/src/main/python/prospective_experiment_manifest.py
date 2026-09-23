"""Batch D D0 — prospective experiment manifest (freeze protocol)."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any, Dict, List, Optional

import policy_registry_batch_c as pr

PROSPECTIVE_MANIFEST_VERSION = "prospective_experiment_manifest_v1_batch_d_20260923"


def build_manifest(
    *,
    experiment_id: str,
    code_sha: str,
    dataset_cutoff_rule: str,
    policy_ids: Optional[List[str]] = None,
    entry_decision_freeze_protocol: str = (
        "Record candidate entry decisions with timestamp BEFORE outcome fields "
        "may populate. Holding-duration selection must not manufacture winners."
    ),
    notes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    pids = policy_ids or pr.required_core_policy_ids()
    policies = []
    for pid in pids:
        m = pr.get_policy(pid)
        policies.append(
            {
                "policy_id": m["policy_id"],
                "policy_version": m["policy_version"],
                "valuation_basis": m["valuation_basis"],
                "research_only": m["research_only"],
                "live_advice": m["live_advice"],
            }
        )
    return {
        "manifest_version": PROSPECTIVE_MANIFEST_VERSION,
        "experiment_id": experiment_id,
        "code_sha": code_sha,
        "dataset_cutoff_rule": dataset_cutoff_rule,
        "policy_versions_frozen": policies,
        "entry_decision_freeze_protocol": entry_decision_freeze_protocol,
        "holding_duration_selection_must_not_manufacture_winners": True,
        "auto_deploy_forbidden": True,
        "sizing_forbidden": True,
        "notes": list(
            notes
            or [
                "Framework only — does not claim statistical proof.",
                "Sep29 BNF cycle must remain PENDING until real expiry.",
            ]
        ),
    }


def default_batch_d_manifest(code_sha: str = "LOCAL_UNCOMMITTED") -> Dict[str, Any]:
    return build_manifest(
        experiment_id="batch_d_prospective_framework_20260923",
        code_sha=code_sha,
        dataset_cutoff_rule=(
            "Entries with entry_ts <= freeze_ts are eligible; outcomes with "
            "event_ts > freeze_ts must not backfill into pre-freeze decisions."
        ),
    )


def write_manifest_json(path: str, manifest: Optional[Dict[str, Any]] = None) -> str:
    m = manifest or default_batch_d_manifest()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, sort_keys=True)
        f.write("\n")
    return path


def load_manifest(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
