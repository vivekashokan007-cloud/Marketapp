"""G5 durable evening evaluation-run ledger.

Truthful, restartable post-close completion. Prefs may cache status but are
never the sole completion record — prefer Supabase ``ml_evaluation_runs`` plus
a local mirror file.

Identity key: session + scope + policy/label contract + input manifest +
evaluator version. A changed manifest or evaluator creates a new revision.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional

LEDGER_CONTRACT_VERSION = "evaluation_run_ledger_v1_20260912"
EVALUATOR_VERSION = "evening_evaluator_g5_v1_20260912"
DEFAULT_SCOPE = "owner_device_paper"
DEFAULT_POLICY_LABEL_CONTRACT = (
    "teacher_v1|tc_2026_07_A|"
    "position_exit_policy_v1_net_20260912|"
    "net_target_v1_gross_minus_costs_once_20260912"
)

STAGE_ORDER = (
    "input_coverage",
    "outcome_computation",
    "outcome_persistence",
    "research_aggregation",
    "percentile_finalization",
    "performance_metrics",
)

# Tracked separately until their own gates enable them.
GATED_STAGES = (
    "training",
    "promotion",
)

STAGE_STATES = frozenset({
    "pending",
    "running",
    "verified",
    "failed",
    "ineligible",
    "disabled",
    "not_attempted",
})

# Terminal-ok for full learning completion: verified OR explicitly explained.
COMPLETION_OK_STATES = frozenset({"verified", "ineligible", "disabled", "not_attempted"})

LEASE_DEFAULT_MS = 45 * 60 * 1000

# Reason codes (stable for UI / backfill reporting)
REASON_CAPPED_POPULATION = "CAPPED_OR_INCOMPLETE_CANDIDATE_POPULATION"
REASON_NO_FRAMES = "NO_C3_FRAMES"
REASON_UNRECONSTRUCTABLE = "UNRECONSTRUCTABLE_ORIGINAL_EVIDENCE"
REASON_TRAINING_FROZEN = "TRAINING_FROZEN_NOT_ATTEMPTED"
REASON_PROMOTION_DISABLED = "PROMOTION_DISABLED_NOT_ATTEMPTED"
REASON_METRICS_DEFERRED_G6 = "PERFORMANCE_METRICS_DEFERRED_TO_G6"
REASON_NONLABELABLE = "NONLABELABLE_SNAPSHOTS_ACCOUNTED"
REASON_DUPLICATE_LEASE = "ACTIVE_LEASE_HELD"
REASON_CROSS_DATE = "CROSS_DATE_OUTCOME_REJECTED"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def hash_input_manifest(manifest: dict[str, Any] | None) -> str:
    """Stable SHA-256 of the evaluation input manifest (snapshot IDs, counts, coverage)."""
    payload = manifest or {}
    digest = hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()
    return digest


def build_run_identity(
    *,
    session_date: str,
    scope: str = DEFAULT_SCOPE,
    policy_label_contract: str = DEFAULT_POLICY_LABEL_CONTRACT,
    input_manifest_hash: str,
    evaluator_version: str = EVALUATOR_VERSION,
) -> str:
    material = "|".join([
        str(session_date).strip(),
        str(scope).strip(),
        str(policy_label_contract).strip(),
        str(input_manifest_hash).strip(),
        str(evaluator_version).strip(),
        LEDGER_CONTRACT_VERSION,
    ])
    return "erun_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _empty_stage(name: str, *, initial: str = "pending") -> dict[str, Any]:
    return {
        "name": name,
        "state": initial,
        "reason_code": "",
        "expected_count": 0,
        "written_count": 0,
        "verified_count": 0,
        "nonlabelable_count": 0,
        "started_at": None,
        "ended_at": None,
        "last_error": "",
        "detail": {},
    }


def new_run(
    *,
    session_date: str,
    scope: str = DEFAULT_SCOPE,
    policy_label_contract: str = DEFAULT_POLICY_LABEL_CONTRACT,
    input_manifest: dict[str, Any] | None = None,
    evaluator_version: str = EVALUATOR_VERSION,
    revision: int = 1,
) -> dict[str, Any]:
    manifest = dict(input_manifest or {})
    manifest_hash = hash_input_manifest(manifest)
    run_id = build_run_identity(
        session_date=session_date,
        scope=scope,
        policy_label_contract=policy_label_contract,
        input_manifest_hash=manifest_hash,
        evaluator_version=evaluator_version,
    )
    stages = {name: _empty_stage(name) for name in STAGE_ORDER}
    stages["training"] = _empty_stage("training", initial="not_attempted")
    stages["training"]["state"] = "disabled"
    stages["training"]["reason_code"] = REASON_TRAINING_FROZEN
    stages["promotion"] = _empty_stage("promotion", initial="not_attempted")
    stages["promotion"]["state"] = "disabled"
    stages["promotion"]["reason_code"] = REASON_PROMOTION_DISABLED
    # G6 owns the real metrics table; mark explicitly so completion is honest.
    stages["performance_metrics"]["state"] = "ineligible"
    stages["performance_metrics"]["reason_code"] = REASON_METRICS_DEFERRED_G6
    now = _utc_now_iso()
    return {
        "run_id": run_id,
        "revision": int(revision),
        "session_date": session_date,
        "scope": scope,
        "policy_label_contract": policy_label_contract,
        "input_manifest_hash": manifest_hash,
        "input_manifest": manifest,
        "evaluator_version": evaluator_version,
        "ledger_contract_version": LEDGER_CONTRACT_VERSION,
        "lease_holder": None,
        "lease_expires_at_ms": 0,
        "created_at": now,
        "updated_at": now,
        "stages": stages,
        "labels_saved": False,
        "learning_complete": False,
        "active": True,
    }


def stage_state(run: dict[str, Any], name: str) -> str:
    stage = (run.get("stages") or {}).get(name) or {}
    return str(stage.get("state") or "pending")


def set_stage(
    run: dict[str, Any],
    name: str,
    state: str,
    *,
    reason_code: str = "",
    expected_count: int | None = None,
    written_count: int | None = None,
    verified_count: int | None = None,
    nonlabelable_count: int | None = None,
    last_error: str = "",
    detail: dict[str, Any] | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    if state not in STAGE_STATES:
        raise ValueError(f"invalid stage state: {state}")
    if name not in STAGE_ORDER and name not in GATED_STAGES:
        raise ValueError(f"unknown stage: {name}")
    out = deepcopy(run)
    stages = out.setdefault("stages", {})
    stage = dict(stages.get(name) or _empty_stage(name))
    now = now_iso or _utc_now_iso()
    prev = stage.get("state")
    stage["state"] = state
    if reason_code:
        stage["reason_code"] = reason_code
    if expected_count is not None:
        stage["expected_count"] = int(expected_count)
    if written_count is not None:
        stage["written_count"] = int(written_count)
    if verified_count is not None:
        stage["verified_count"] = int(verified_count)
    if nonlabelable_count is not None:
        stage["nonlabelable_count"] = int(nonlabelable_count)
    if last_error or state in {"failed", "ineligible"}:
        stage["last_error"] = last_error or stage.get("last_error") or ""
    if detail:
        merged = dict(stage.get("detail") or {})
        merged.update(detail)
        stage["detail"] = merged
    if state == "running" and prev != "running":
        stage["started_at"] = now
        stage["ended_at"] = None
    if state in {"verified", "failed", "ineligible", "disabled", "not_attempted"}:
        if not stage.get("started_at"):
            stage["started_at"] = now
        stage["ended_at"] = now
    stages[name] = stage
    out["updated_at"] = now
    refresh_completion_flags(out)
    return out


def refresh_completion_flags(run: dict[str, Any]) -> dict[str, Any]:
    """Labels saved ≠ learning complete. C3 failure/ineligible blocks learning_complete."""
    stages = run.get("stages") or {}
    persistence = stages.get("outcome_persistence") or {}
    labels_saved = persistence.get("state") == "verified" and int(persistence.get("verified_count") or 0) >= 0
    # Even zero-outcome verified sessions count as labels-saved (truthfully empty).
    if persistence.get("state") == "verified":
        labels_saved = True
    run["labels_saved"] = bool(labels_saved)

    learning_ok = True
    for name in STAGE_ORDER:
        st = (stages.get(name) or {}).get("state")
        if st not in COMPLETION_OK_STATES:
            learning_ok = False
            break
    # Gated stages must remain disabled/not_attempted (not failed).
    for name in GATED_STAGES:
        st = (stages.get(name) or {}).get("state")
        if st not in {"disabled", "not_attempted"}:
            learning_ok = False
            break
    # Explicit: failed C3 cannot masquerade as full success even if somehow marked ok above.
    c3 = stages.get("percentile_finalization") or {}
    if c3.get("state") == "failed":
        learning_ok = False
    run["learning_complete"] = bool(learning_ok and labels_saved)
    return run


def next_resumable_stage(run: dict[str, Any]) -> Optional[str]:
    """Return the first incomplete applicable stage for crash/resume."""
    stages = run.get("stages") or {}
    for name in STAGE_ORDER:
        st = (stages.get(name) or {}).get("state")
        if st in {"pending", "running", "failed"}:
            # performance_metrics starts ineligible (G6) — skip if already terminal-ok
            if st in COMPLETION_OK_STATES:
                continue
            return name
    return None


def acquire_lease(
    run: dict[str, Any],
    holder: str,
    *,
    now_ms: int | None = None,
    lease_ms: int = LEASE_DEFAULT_MS,
    force: bool = False,
) -> tuple[dict[str, Any], bool, str]:
    """One active run per identity. Returns (run, acquired, reason)."""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    out = deepcopy(run)
    expires = int(out.get("lease_expires_at_ms") or 0)
    current = out.get("lease_holder")
    if current and current != holder and expires > now and not force:
        return out, False, REASON_DUPLICATE_LEASE
    out["lease_holder"] = holder
    out["lease_expires_at_ms"] = now + int(lease_ms)
    out["updated_at"] = _utc_now_iso()
    out["active"] = True
    return out, True, ""


def release_lease(run: dict[str, Any], holder: str) -> dict[str, Any]:
    out = deepcopy(run)
    if out.get("lease_holder") == holder:
        out["lease_holder"] = None
        out["lease_expires_at_ms"] = 0
        out["updated_at"] = _utc_now_iso()
    return out


def reject_cross_date_outcome(session_date: str, outcome_session_date: str) -> None:
    if str(session_date) != str(outcome_session_date):
        raise ValueError(f"{REASON_CROSS_DATE}: run={session_date} row={outcome_session_date}")


def assess_c3_frames(frames: list[dict[str, Any]]) -> dict[str, Any]:
    """Dry-run eligibility for percentile finalization from original frames only.

    Never invents rows. Capped / incomplete candidate populations fail provenance
    and are marked ineligible rather than written as verified uncapped history.
    """
    if not frames:
        return {
            "eligible": False,
            "reason_code": REASON_NO_FRAMES,
            "frame_count": 0,
            "verified_population_frames": 0,
            "capped_or_incomplete_frames": 0,
            "would_write_rows": False,
            "message": "No C3 recording frames in original evidence.",
        }
    verified = 0
    capped = 0
    for frame in frames:
        if not isinstance(frame, dict):
            capped += 1
            continue
        pop_ok = bool(frame.get("candidate_population_verified"))
        gen_ok = bool(frame.get("generated_capture_complete"))
        # Explicit truncation markers from build3 flow if present on frame detail.
        trunc = 0
        for key in (
            "truncated_at_ranked_evidence",
            "truncated_at_persistence",
            "truncated_at_candidates",
        ):
            try:
                trunc += int((frame.get("detail") or {}).get(key) or frame.get(key) or 0)
            except (TypeError, ValueError):
                pass
        if pop_ok and gen_ok and trunc == 0:
            verified += 1
        else:
            capped += 1
    if verified == 0:
        return {
            "eligible": False,
            "reason_code": REASON_CAPPED_POPULATION if capped else REASON_UNRECONSTRUCTABLE,
            "frame_count": len(frames),
            "verified_population_frames": verified,
            "capped_or_incomplete_frames": capped,
            "would_write_rows": False,
            "message": (
                "Original C3 frames exist but candidate population provenance is "
                "capped/incomplete; refusing to fabricate verified percentile rows."
            ),
        }
    if capped > 0:
        # Mixed session: only verified frames could be finalized; still refuse
        # marking the whole session as uncapped verified.
        return {
            "eligible": False,
            "reason_code": REASON_CAPPED_POPULATION,
            "frame_count": len(frames),
            "verified_population_frames": verified,
            "capped_or_incomplete_frames": capped,
            "would_write_rows": False,
            "message": (
                f"{capped} of {len(frames)} frames fail provenance; "
                "session cannot be marked uncapped verified."
            ),
        }
    return {
        "eligible": True,
        "reason_code": "",
        "frame_count": len(frames),
        "verified_population_frames": verified,
        "capped_or_incomplete_frames": 0,
        "would_write_rows": True,
        "message": f"{verified} frames have verified candidate-population provenance.",
    }


def apply_c3_assessment(run: dict[str, Any], assessment: dict[str, Any]) -> dict[str, Any]:
    if assessment.get("eligible"):
        return set_stage(
            run,
            "percentile_finalization",
            "pending",
            expected_count=int(assessment.get("frame_count") or 0),
            detail=assessment,
        )
    return set_stage(
        run,
        "percentile_finalization",
        "ineligible",
        reason_code=str(assessment.get("reason_code") or REASON_UNRECONSTRUCTABLE),
        expected_count=int(assessment.get("frame_count") or 0),
        written_count=0,
        verified_count=0,
        last_error=str(assessment.get("message") or ""),
        detail=assessment,
    )


def summarize_stages_ran(run: dict[str, Any]) -> list[str]:
    """Backfill/reporting helper: which stages were actually executed."""
    ran = []
    for name in list(STAGE_ORDER) + list(GATED_STAGES):
        stage = (run.get("stages") or {}).get(name) or {}
        st = stage.get("state")
        if st in {"running", "verified", "failed", "ineligible"} and stage.get("started_at"):
            ran.append(f"{name}:{st}:{stage.get('reason_code') or '-'}")
        elif st in {"disabled", "not_attempted"}:
            ran.append(f"{name}:{st}:{stage.get('reason_code') or '-'}")
    return ran


class InMemoryRunStore:
    """Process-local mirror + lease guard for tests and single-device use."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_id: dict[str, dict[str, Any]] = {}

    def upsert(self, run: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            rid = run["run_id"]
            self._by_id[rid] = deepcopy(run)
            return deepcopy(run)

    def get(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._by_id.get(run_id)
            return deepcopy(row) if row else None

    def get_active_for_session(self, session_date: str) -> Optional[dict[str, Any]]:
        with self._lock:
            for row in self._by_id.values():
                if row.get("session_date") == session_date and row.get("active"):
                    return deepcopy(row)
            return None

    def try_begin(
        self,
        *,
        session_date: str,
        holder: str,
        input_manifest: dict[str, Any] | None = None,
        policy_label_contract: str = DEFAULT_POLICY_LABEL_CONTRACT,
        evaluator_version: str = EVALUATOR_VERSION,
        scope: str = DEFAULT_SCOPE,
        now_ms: int | None = None,
    ) -> tuple[dict[str, Any], bool, str]:
        """Create or resume the identity run; reject duplicate concurrent leases."""
        with self._lock:
            manifest = dict(input_manifest or {})
            run_id = build_run_identity(
                session_date=session_date,
                scope=scope,
                policy_label_contract=policy_label_contract,
                input_manifest_hash=hash_input_manifest(manifest),
                evaluator_version=evaluator_version,
            )
            existing = self._by_id.get(run_id)
            if existing is None:
                run = new_run(
                    session_date=session_date,
                    scope=scope,
                    policy_label_contract=policy_label_contract,
                    input_manifest=manifest,
                    evaluator_version=evaluator_version,
                )
            else:
                run = deepcopy(existing)
            run, ok, reason = acquire_lease(run, holder, now_ms=now_ms)
            if not ok:
                return run, False, reason
            self._by_id[run_id] = deepcopy(run)
            return deepcopy(run), True, ""
