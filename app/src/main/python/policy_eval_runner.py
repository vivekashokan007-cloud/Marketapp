"""Batch C C4 — fixture-driven policy eval runner (research-only)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import entry_cohort_memberships as ecm
import policy_eval_metrics as pem
import policy_outcome_store as pos
import policy_registry_batch_c as pr

POLICY_EVAL_RUNNER_VERSION = "policy_eval_runner_v1_batch_c_20260923"


def _default_fixture_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "tests", "fixtures", "batch_c_entries")


def _default_report_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    # Prefer in-repo docs path; tools/output also acceptable.
    docs = os.path.abspath(
        os.path.join(here, "..", "..", "..", "..", "docs", "batch_c_policy_eval")
    )
    return os.path.join(docs, "sample_policy_eval_report_20260923.json")


def load_entry_fixtures(fixture_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    d = fixture_dir or _default_fixture_dir()
    path = os.path.join(d, "entries.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("entries_fixture_must_be_list")
    return data


def run_fixture_eval(
    *,
    fixture_dir: Optional[str] = None,
    report_path: Optional[str] = None,
    write_report: bool = True,
) -> Dict[str, Any]:
    """Deterministic fixture-driven runner. No Supabase. No live advice change."""
    pr.assert_registry_invariants()
    entries = load_entry_fixtures(fixture_dir)
    store = pos.PolicyOutcomeStore()
    policies = [pr.get_policy(pid) for pid in pr.required_core_policy_ids()]

    for entry in entries:
        eid = entry["entry_identity"]
        primary = ecm.classify_entry_cohort(
            action=entry.get("action"),
            confidence=entry.get("confidence"),
            recommendation=entry.get("recommendation"),
        )
        roles = entry.get("roles") or []
        membership = ecm.build_membership_record(
            entry_identity=eid,
            primary_cohort=primary,
            roles=roles,
            notification_delivered=entry.get("notification_delivered"),
        )
        for pol in policies:
            structural = bool(entry.get("structural"))
            struct_reasons = list(entry.get("structural_reasons") or [])
            fidelity = entry.get("fidelity") or (
                pos.FIDELITY_LIMITED_FIXTURE
                if not structural
                else pos.FIDELITY_LIMITED_FIXTURE
            )
            if structural and not struct_reasons:
                struct_reasons = ["fixture_marked_structural"]

            metrics = None
            if structural:
                metrics = pem.build_metric_bundle(
                    selection_mode=entry.get("selection_mode", "retrospective"),
                    structural=True,
                )
                metrics["label"] = pos.LABEL_STRUCTURAL
                metrics["missing_not_neutral_zero"] = True
            else:
                # Per-policy synthetic but deterministic research deltas from fixture.
                base_net = float(entry.get("net_rupees", 0.0))
                policy_delta = {
                    "legacy_teacher_v_frozen": 0.0,
                    "H0_fixed_exit": -50.0,
                    "H1_fixed_exit": 25.0,
                    "H2_fixed_exit": 10.0,
                    "expiry_time_grid": 5.0,
                    "deployed_position_verdict": 0.0,
                    "corrected_data_contract_position_verdict": 15.0,
                    "kotlin_tick_shadow_policy": -10.0,
                    "forced_exit_only_control": -100.0,
                }.get(pol["policy_id"], 0.0)
                net = base_net + policy_delta
                max_loss = float(entry.get("max_loss", 1000.0) or 1000.0)
                legacy_risk = float(entry.get("legacy_configured_risk", max_loss) or max_loss)
                metrics = pem.build_metric_bundle(
                    net_rupees=net,
                    R_max_loss_norm=(net / max_loss) if max_loss else None,
                    R_legacy_configured_risk=(net / legacy_risk) if legacy_risk else None,
                    tp_hit=bool(entry.get("tp_hit", False)),
                    net_profitable=net > 0,
                    drawdown=float(entry.get("drawdown", 0.0) or 0.0),
                    gaps=entry.get("gaps"),
                    missingness=entry.get("missingness"),
                    capital_usage=float(entry.get("capital_usage", 0.0) or 0.0),
                    selection_mode=entry.get("selection_mode", "retrospective"),
                    structural=False,
                )
                pem.assert_no_collapsed_success(metrics)

            row = store.put_outcome(
                entry_identity=eid,
                policy_id=pol["policy_id"],
                policy_version=pol["policy_version"],
                experiment_version="batch_c_fixture_eval_20260923",
                data_version=entry.get("data_version", "fixture_v1"),
                code_version=POLICY_EVAL_RUNNER_VERSION,
                cost_version=entry.get("cost_version", "cost_v1"),
                expiry=entry.get("expiry"),
                legs=entry.get("legs"),
                qty=entry.get("qty"),
                entry_ts=entry.get("entry_ts"),
                fill_basis=entry.get("fill_basis", "fixture"),
                fidelity=fidelity,
                maturity=entry.get("maturity", pos.MATURITY_MATURE),
                censoring=entry.get("censoring", pos.CENSORING_NONE),
                structural=structural,
                structural_reasons=struct_reasons,
                metrics=metrics,
                membership=membership,
                selection_mode=entry.get("selection_mode", "retrospective"),
                behavior_fingerprint=None,  # explicit placeholder — no real policy impl to hash
            )
            # Attach slice dims for reporting.
            store._rows[pos.outcome_key(eid, pol["policy_id"], pol["policy_version"])][
                "slice_dims"
            ] = {
                "underlying": entry.get("underlying"),
                "tenor": entry.get("tenor"),
                "dte": entry.get("dte"),
                "strategy": entry.get("strategy"),
                "expiry_cycle": entry.get("expiry_cycle"),
            }
            _ = row  # row already stored

    outcomes = store.list_outcomes()
    # Ensure slice_dims present on listed copies.
    for o in outcomes:
        key = pos.outcome_key(o["entry_identity"], o["policy_id"], o["policy_version"])
        stored = store._rows[key]
        o["slice_dims"] = dict(stored.get("slice_dims") or {})

    summary = pem.summarize_separate_metrics(outcomes)
    by_policy: Dict[str, Any] = {}
    for pol in policies:
        subset = [o for o in outcomes if o["policy_id"] == pol["policy_id"]]
        by_policy[pol["policy_id"]] = pem.summarize_separate_metrics(subset)

    report = {
        "SYNTHETIC_FIXTURE_ONLY": True,
        "research_performance_comparisons_suppressed": True,
        "executable_historical_replay_implemented": False,
        "real_policy_replay_status": "NOT_IMPLEMENTED",
        "performance_sections_gated": True,
        "runner_version": POLICY_EVAL_RUNNER_VERSION,
        "registry_version": pr.POLICY_REGISTRY_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_entries": len(entries),
        "n_policies": len(policies),
        "n_outcomes": store.count(),
        "live_advice_changed": False,
        "notifications_changed": False,
        "teacher_production_changed": False,
        "ranking_changed": False,
        "supabase_write": False,
        "summary": {
            **(summary if isinstance(summary, dict) else {}),
            "SYNTHETIC_FIXTURE_ONLY": True,
            "note": "Metrics below are hardcoded fixture deltas — NOT real research improvement.",
            "sum_net_rupees_is_not_real_research_improvement": True,
            "performance_comparison_allowed": False,
        },
        "by_policy": {
            **{k: {**(v if isinstance(v, dict) else {}), "SYNTHETIC_FIXTURE_ONLY": True,
                    "performance_comparison_allowed": False} for k, v in (by_policy or {}).items()},
            "SYNTHETIC_FIXTURE_ONLY": True,
        },
        "performance_report": None,  # suppressed outside testing — synthetic fixtures only

        "outcomes": outcomes,
        "synthetic_fixture_deltas_applied": True,
        "notes": [
            "SYNTHETIC_FIXTURE_ONLY — whole report is synthetic; do not treat as policy research performance.",
            "Research-only fixture eval. Not production advice.",
            "Noon in expiry_time_grid is a hypothesis, not an optimum.",
            "corrected_data_contract_position_verdict remains RESEARCH.",
            "Sep29 BNF cycle is NOT claimed complete inside Batch C.",
        ],
    }

    if write_report:
        out_path = report_path or _default_report_path()
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, sort_keys=True)
            f.write("\n")
        report["report_path"] = out_path
    return report


if __name__ == "__main__":
    r = run_fixture_eval()
    print(json.dumps({"n_outcomes": r["n_outcomes"], "report_path": r.get("report_path")}, indent=2))


def run_real_policy_replay(*_args, **_kwargs):
    """Stub for future version-pinned executable historical replay.

    NOT_IMPLEMENTED. Research-only marker. Registry of names is not replay.
    """
    return {
        "status": "NOT_IMPLEMENTED",
        "research_only": True,
        "executable_historical_replay_exists": False,
        "note": (
            "Real research requires version-pinned policy replay with event clock, "
            "correct valuation/cost basis, quote coverage, forced exits, censoring, "
            "and independent input/expected-output evidence."
        ),
    }
