"""Implied-EV v1.2 G1-G5 evaluator (Codex R5). Read-only: consumes SAVED evidence only - the exact decision
envelopes (public.ev_decisions.envelope_text) and outcome amendments (public.ev_outcomes.outcome_text), e.g. a
SELECT export or the phone's local slot/tracker files. No profitability assertion is made; every metric reports
its n and coverage, and cohorts are kept separate:
  RETROSPECTIVE  calibration family contains "retro" (seeded from history)
  FORWARD        calibration family contains "live" (published by the live builder)
  SYNTHETIC      anything else (fixtures/dev)
Observation kinds are never mixed: SCHEDULED_HYPOTHETICAL (ENGINE_PICK Rule B at the decision prices),
ACTUAL_ACCEPTED (the accepted trade, net with dated fees), COMMON_RULE_B (v7 comparator under Rule B),
V7_MANAGED (v7 comparator under the declared managed policy; gross thresholds, see threshold_basis) and
ENGINE_TP_SL (informational engine TP/SL first-hit).

G1 policy outcomes + decision/abstention/failure counts + coverage
G2 paired noon selector comparison under the common Rule B exit; full-policy (engine Rule B vs v7 managed);
   accepted vs scheduled. Noon cohort only - NOT all-day v7.
G3 valid EV_robust > 0 vs <= 0 members of the declared bounded cohort; invalid/missing reported separately.
G4 nine frozen forward-event forecasts: engine latent law vs k=1 baseline, Brier and log-loss on IDENTICAL labels.
G5b empirical horizon-price diagnostics: forward bias (F_exit - F_entry under the zero-rate martingale convention)
   and per-lot structure value error (realized Rule B net - EV at each calibration scenario k), MAE and tail.

usage: python3 tools/ev_v12_report.py --envelopes decisions.json --outcomes outcomes.json [--out report.json]
Each file: a JSON array of envelope/outcome objects, or of rows holding envelope_text / outcome_text.
"""
import argparse
import json
import math
import sys

REPORT_VERSION = "ev_v12_g1_g5_report_v1"
EPS = 1e-6


def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _load_items(rows, text_key):
    out = []
    for r in rows or []:
        if isinstance(r, str):
            r = json.loads(r)
        if isinstance(r, dict) and isinstance(r.get(text_key), str):
            r = json.loads(r[text_key])
        if isinstance(r, dict):
            out.append(r)
    return out


def cohort_of(business):
    fam = str(business.get("calibration_version") or "").split(":")[0]
    if "retro" in fam:
        return "RETROSPECTIVE"
    if "live" in fam:
        return "FORWARD"
    return "SYNTHETIC"


def _stats(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0}
    xs_sorted = sorted(xs)
    n = len(xs)
    mean = sum(xs) / n
    return {"n": n, "mean": round(mean, 4), "min": round(xs_sorted[0], 4), "max": round(xs_sorted[-1], 4),
            "median": round(xs_sorted[n // 2] if n % 2 else 0.5 * (xs_sorted[n // 2 - 1] + xs_sorted[n // 2]), 4)}


def _err_stats(errs):
    errs = [e for e in errs if e is not None]
    if not errs:
        return {"n": 0}
    a = sorted(abs(e) for e in errs)
    return {"n": len(errs), "bias": round(sum(errs) / len(errs), 4), "mae": round(sum(a) / len(a), 4),
            "p90_abs": round(a[min(len(a) - 1, int(math.ceil(0.9 * len(a))) - 1)], 4), "max_abs": round(a[-1], 4)}


def _net(component, key="rule_b"):
    ev = (component or {}).get(key)
    return _f(ev.get("net")) if isinstance(ev, dict) else None


def evaluate(envelopes, outcomes):
    envs = _load_items(envelopes, "envelope_text")
    outs = _load_items(outcomes, "outcome_text")
    by_id = {}
    for e in envs:
        did = e.get("decision_id")
        if did:
            by_id[did] = {"env": e, "out": None}
    orphan_outcomes = 0
    for o in outs:
        did = o.get("decision_id")
        if did in by_id:
            by_id[did]["out"] = o
        else:
            orphan_outcomes += 1
    cohorts = {}
    for did, rec in sorted(by_id.items()):
        b = rec["env"].get("business") or {}
        c = cohorts.setdefault(cohort_of(b), {"items": []})
        c["items"].append((did, b, rec["out"]))
    report = {"report_version": REPORT_VERSION, "decisions": len(by_id), "outcomes_joined": sum(
        1 for r in by_id.values() if r["out"]), "orphan_outcomes": orphan_outcomes, "cohorts": {},
        "note": "evidence summary only; no profitability claim; noon cohort only (not all-day v7)"}
    for name, c in sorted(cohorts.items()):
        report["cohorts"][name] = _cohort_report(c["items"])
    return report


def _cohort_report(items):
    g1 = {"by_status": {}, "outcome_coverage": {}, "accepted_status": {}, "observations": {}}
    obs = {"SCHEDULED_HYPOTHETICAL": [], "ACTUAL_ACCEPTED": [], "COMMON_RULE_B": [], "V7_MANAGED": [],
           "ENGINE_TP_SL": []}
    g2_common, g2_full, g2_acc = [], [], []
    g3 = {"positive": [], "nonpositive": [], "invalid_or_missing": 0, "members": 0}
    g4 = {"pairs": 0, "brier_engine": 0.0, "brier_k1": 0.0, "logloss_engine": 0.0, "logloss_k1": 0.0, "decisions": 0}
    g5_fwd, g5_struct = [], {}
    for did, b, out in items:
        st = str(b.get("status"))
        g1["by_status"][st] = g1["by_status"].get(st, 0) + 1
        if out is None:
            g1["outcome_coverage"]["NO_OUTCOME_YET"] = g1["outcome_coverage"].get("NO_OUTCOME_YET", 0) + 1
            continue
        comps = out.get("components") or []
        roles = {}
        for comp in comps:
            roles.setdefault(comp.get("role"), []).append(comp)
            k = "%s:%s" % (comp.get("role"), comp.get("rule_b_status"))
            g1["outcome_coverage"][k] = g1["outcome_coverage"].get(k, 0) + 1
        acc = out.get("accepted") or {}
        g1["accepted_status"][str(acc.get("status"))] = g1["accepted_status"].get(str(acc.get("status")), 0) + 1
        pick = (roles.get("ENGINE_PICK") or [None])[0]
        v7 = (roles.get("V7_COMPARATOR") or [None])[0]
        sched = _net(pick)
        obs["SCHEDULED_HYPOTHETICAL"].append(sched)
        if pick:
            for k in ("tp_first", "sl_first"):
                if pick.get(k):
                    obs["ENGINE_TP_SL"].append(_net(pick, k))
        acc_net = _f(acc.get("net_pnl"))
        if acc.get("status") in ("PLANNED_CLOSE", "LATE_CLOSE", "MANUAL_EARLY_CLOSE") and acc_net is not None:
            obs["ACTUAL_ACCEPTED"].append(acc_net)
            if sched is not None:
                g2_acc.append(acc_net - sched)
        v7_rb, v7_mg = _net(v7), _net(v7, "managed_exit")
        obs["COMMON_RULE_B"].append(v7_rb)
        obs["V7_MANAGED"].append(v7_mg)
        if sched is not None and v7_rb is not None:
            g2_common.append(sched - v7_rb)
        if sched is not None and v7_mg is not None:
            g2_full.append(sched - v7_mg)
        # G3: declared cohort members (business.g3_cohort) joined by structure name to their outcome components
        members = ((b.get("g3_cohort") or {}).get("members") or [])
        byname = {comp.get("structure"): comp for comp in comps if comp.get("role") in ("G3_SAMPLE", "ENGINE_PICK")}
        for m in members:
            g3["members"] += 1
            comp = byname.get(m.get("name"))
            net = _net(comp) if comp and comp.get("rule_b_status") == "COMPLETE" else None
            evr = _f(m.get("ev_robust"))
            if net is None or evr is None:
                g3["invalid_or_missing"] += 1
            else:
                (g3["positive"] if evr > 0 else g3["nonpositive"]).append(net)
        # G4: identical labels for both forecasts
        labels = out.get("g4_labels")
        fc = b.get("g4") or []
        if isinstance(labels, list) and len(labels) == len(fc) and fc:
            g4["decisions"] += 1
            for e, y in zip(fc, labels):
                pe, pk = _f(e.get("p_engine")), _f(e.get("p_k1"))
                if pe is None or pk is None:
                    continue
                g4["pairs"] += 1
                g4["brier_engine"] += (pe - y) ** 2
                g4["brier_k1"] += (pk - y) ** 2
                g4["logloss_engine"] -= math.log(min(1 - EPS, max(EPS, pe if y else 1 - pe)))
                g4["logloss_k1"] -= math.log(min(1 - EPS, max(EPS, pk if y else 1 - pk)))
        # G5b
        fe, fx = _f(out.get("F_entry") or b.get("F")), _f(out.get("F_exit"))
        if fe is not None and fx is not None:
            g5_fwd.append(fx - fe)
        p = b.get("pick") or {}
        units = _f(((pick or {}).get("entry") or {}).get("units"))
        lot = _f((b.get("lot_identity") or {}).get("contract_lot_size"))
        if sched is not None and isinstance(p.get("ev"), list) and units and lot:
            per_lot = sched * lot / units
            for i, ev in enumerate(p["ev"]):
                if _f(ev) is not None:
                    g5_struct.setdefault("k%d" % i, []).append(per_lot - _f(ev))
            if _f(p.get("ev_robust")) is not None:
                g5_struct.setdefault("ev_robust", []).append(per_lot - _f(p.get("ev_robust")))
    g1["observations"] = {k: _stats(v) for k, v in obs.items()}
    if g4["pairs"]:
        for k in ("brier_engine", "brier_k1", "logloss_engine", "logloss_k1"):
            g4[k] = round(g4[k] / g4["pairs"], 6)
    else:
        g4 = {"pairs": 0, "decisions": 0, "status": "NO_LABELLED_FORECASTS"}
    return {
        "G1": g1,
        "G2": {"common_rule_b_engine_minus_v7": _stats(g2_common), "full_policy_engine_rule_b_minus_v7_managed":
               _stats(g2_full), "accepted_minus_scheduled": _stats(g2_acc),
               "pairing": "same session, noon decision, same exit policy unless named"},
        "G3": {"members": g3["members"], "invalid_or_missing": g3["invalid_or_missing"],
               "ev_robust_positive": _stats(g3["positive"]), "ev_robust_nonpositive": _stats(g3["nonpositive"]),
               "sampling": "declared bounded cohort (business.g3_cohort); not the full population"},
        "G4": g4,
        "G5b": {"forward_bias_points": _err_stats(g5_fwd), "structure_value_error_per_lot_rupees":
                {k: _err_stats(v) for k, v in sorted(g5_struct.items())}},
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--envelopes", required=True)
    ap.add_argument("--outcomes", required=True)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    with open(a.envelopes, encoding="utf-8") as f:
        envs = json.load(f)
    with open(a.outcomes, encoding="utf-8") as f:
        outs = json.load(f)
    rep = evaluate(envs, outs)
    text = json.dumps(rep, indent=1, sort_keys=True)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
