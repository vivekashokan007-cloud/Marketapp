"""G8 — session/time-grouped splits, untouched test periods, manifests, champion/challenger.

Offline scaffolding only. Does not enable ml_train.run or automatic promotion.
Champion and challenger must be scored on identical untouched periods with the
same economics/constraints — never compare a new validation metric to an old
model's unrelated stored accuracy.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping, Sequence

MANIFEST_CONTRACT_VERSION = "training_manifest_v1_20260913"
SPLIT_CONTRACT_VERSION = "session_time_split_v1_20260913"


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def hash_payload(obj: Any) -> str:
    return hashlib.sha256(_stable_json(obj).encode()).hexdigest()


def session_key(row: Mapping[str, Any]) -> str:
    for k in ("session_date", "date", "effective_session_date", "exit_date"):
        v = row.get(k)
        if v is None or v == "":
            continue
        text = str(v).strip()
        # Normalize timestamps to YYYY-MM-DD when possible.
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            return text[:10]
        return text
    return ""


def group_rows_by_session(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = session_key(row)
        if not key:
            key = "_missing_session"
        groups.setdefault(key, []).append(dict(row))
    return dict(sorted(groups.items(), key=lambda kv: kv[0]))


def chronological_session_split(
    rows: Sequence[Mapping[str, Any]],
    *,
    train_frac: float = 0.70,
    calib_frac: float = 0.15,
    test_frac: float = 0.15,
) -> dict[str, Any]:
    """Split by whole sessions in chronological order (no row-level shuffle leak).

    Calibration/threshold selection is a separate middle window from the
    untouched final test periods. Fractions apply to session counts.
    """
    if abs((train_frac + calib_frac + test_frac) - 1.0) > 1e-9:
        raise ValueError("train_frac + calib_frac + test_frac must equal 1.0")
    groups = group_rows_by_session(rows)
    sessions = [s for s in groups.keys() if s != "_missing_session"]
    n = len(sessions)
    if n == 0:
        return {
            "contract_version": SPLIT_CONTRACT_VERSION,
            "sessions": [],
            "train_sessions": [],
            "calib_sessions": [],
            "test_sessions": [],
            "train_rows": [],
            "calib_rows": [],
            "test_rows": [],
            "held_out_sessions": [],
            "dropped_missing_session": len(groups.get("_missing_session", [])),
            "status": "empty",
        }

    n_test = max(1, int(round(n * test_frac))) if n >= 3 else (1 if n >= 2 else 0)
    n_calib = max(1, int(round(n * calib_frac))) if n >= 3 else 0
    if n_test + n_calib >= n:
        n_test = min(n_test, max(0, n - 1))
        n_calib = min(n_calib, max(0, n - n_test - 1))
    n_train = n - n_calib - n_test

    train_sessions = sessions[:n_train]
    calib_sessions = sessions[n_train:n_train + n_calib]
    test_sessions = sessions[n_train + n_calib:]

    def gather(keys: Sequence[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for k in keys:
            out.extend(groups[k])
        return out

    return {
        "contract_version": SPLIT_CONTRACT_VERSION,
        "sessions": sessions,
        "train_sessions": train_sessions,
        "calib_sessions": calib_sessions,
        "test_sessions": test_sessions,
        "train_rows": gather(train_sessions),
        "calib_rows": gather(calib_sessions),
        "test_rows": gather(test_sessions),
        "held_out_sessions": list(test_sessions),
        "dropped_missing_session": len(groups.get("_missing_session", [])),
        "status": "ok",
        "note": (
            "Test sessions are untouched: do not use for fitting, calibration, "
            "or threshold selection. Calibration window is separate from test."
        ),
    }


def assert_no_session_overlap(split: Mapping[str, Any]) -> None:
    train = set(split.get("train_sessions") or [])
    calib = set(split.get("calib_sessions") or [])
    test = set(split.get("test_sessions") or [])
    if train & calib:
        raise AssertionError(f"train/calib session overlap: {train & calib}")
    if train & test:
        raise AssertionError(f"train/test session overlap: {train & test}")
    if calib & test:
        raise AssertionError(f"calib/test session overlap: {calib & test}")


def build_immutable_manifest(
    *,
    model_id: str,
    model_hash: str,
    feature_schema_version: str,
    net_target_version: str,
    policy_selector_version: str,
    split: Mapping[str, Any],
    sequence_kind: str = "unspecified",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Immutable model/feature/target/policy manifest for offline compare."""
    body = {
        "contract_version": MANIFEST_CONTRACT_VERSION,
        "model_id": str(model_id),
        "model_hash": str(model_hash),
        "feature_schema_version": str(feature_schema_version),
        "net_target_version": str(net_target_version),
        "policy_selector_version": str(policy_selector_version),
        "sequence_kind": str(sequence_kind),
        "split_contract_version": SPLIT_CONTRACT_VERSION,
        "train_sessions": list(split.get("train_sessions") or []),
        "calib_sessions": list(split.get("calib_sessions") or []),
        "test_sessions": list(split.get("test_sessions") or []),
        "held_out_sessions": list(split.get("held_out_sessions") or []),
        "extra": dict(extra or {}),
    }
    body["manifest_hash"] = hash_payload({k: v for k, v in body.items() if k != "manifest_hash"})
    return body


def compatible_manifests(a: Mapping[str, Any], b: Mapping[str, Any]) -> tuple[bool, str]:
    """Feature/target/policy compatibility gate before loading/comparing."""
    for key in (
        "feature_schema_version",
        "net_target_version",
        "policy_selector_version",
        "split_contract_version",
    ):
        if str(a.get(key) or "") != str(b.get(key) or ""):
            return False, f"incompatible_{key}"
    if list(a.get("test_sessions") or []) != list(b.get("test_sessions") or []):
        return False, "incompatible_test_sessions"
    return True, "ok"


def champion_challenger_compare(
    *,
    champion_manifest: Mapping[str, Any],
    challenger_manifest: Mapping[str, Any],
    champion_scores: Sequence[Mapping[str, Any]],
    challenger_scores: Sequence[Mapping[str, Any]],
    metric_fn: Callable[[Sequence[Mapping[str, Any]]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare both models on the same untouched test period.

    Rejects compares when manifests disagree on test sessions / contracts.
    Does not promote. A better accuracy alone is not acceptance.
    """
    ok, reason = compatible_manifests(champion_manifest, challenger_manifest)
    if not ok:
        return {
            "ok": False,
            "reason": reason,
            "promotion": "blocked",
            "note": "Champion/challenger must share identical untouched test periods and contracts.",
        }

    def default_metric(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        labs = []
        for r in rows:
            y = r.get("y_true")
            p = r.get("y_prob")
            if y is None or p is None:
                continue
            try:
                labs.append((float(p), int(y)))
            except (TypeError, ValueError):
                continue
        if not labs:
            return {"n": 0, "accuracy": None, "brier": None}
        acc = sum(1 for p, y in labs if (1 if p >= 0.5 else 0) == y) / len(labs)
        brier = sum((p - y) ** 2 for p, y in labs) / len(labs)
        return {"n": len(labs), "accuracy": round(acc, 6), "brier": round(brier, 8)}

    fn = metric_fn or default_metric
    champ = fn(champion_scores)
    chall = fn(challenger_scores)
    return {
        "ok": True,
        "reason": "compared_on_identical_test",
        "promotion": "not_requested",
        "test_sessions": list(champion_manifest.get("test_sessions") or []),
        "champion": {
            "model_id": champion_manifest.get("model_id"),
            "model_hash": champion_manifest.get("model_hash"),
            "metrics": champ,
        },
        "challenger": {
            "model_id": challenger_manifest.get("model_id"),
            "model_hash": challenger_manifest.get("model_hash"),
            "metrics": chall,
        },
        "note": (
            "Do not compare challenger validation metadata against champion's "
            "unrelated stored accuracy. Promotion is an explicit separate step."
        ),
    }


def reject_future_feature_leak(
    feature_ts: str,
    label_session: str,
) -> bool:
    """Return True when feature timestamp session is strictly after label session."""
    ft = str(feature_ts or "")[:10]
    ls = str(label_session or "")[:10]
    if not ft or not ls:
        return False
    return ft > ls
