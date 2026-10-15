"""brain.analyze() hook for the implied-EV v1.2 Paper experiment.

Activation contract
  * runs ONLY when brain._explicit_execution_mode(ctx) == 'paper' (unknown/blank/conflicting modes are
    NOT Paper) AND ctx['evExperimentMode'] in ('observe', 'active'). Otherwise this returns None and
    analyze() output is unchanged (Real/sandbox/off are byte-identical; tested).
  * 'off' / 'observe' / 'active' are INTERNAL experiment configuration inside Paper; there is no new
    user-facing trading mode. observe records decisions without showing a recommendation.
  * nothing here alters v7 ranking, gates, sizing, exits, the teacher fee or result['watchlist'];
    the v7 primary is only READ as the comparator.
  * a WAIT/ABSTAIN/NO_ELIGIBLE/FAILED here never locks Paper candidates or manual Paper actions.

Schedule: one decision per NF session, first eligible poll inside [12:30:00, 12:40:00) IST with one
optional retry on a genuine failure in that window. Scope: front NF weekly expiry with 2-4 sessions
to expiry (calendar). Every other session/time is not a scheduled slot.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta

import implied_ev_v12 as M
import ev_v12_engine as ENG
import ev_v12_fees as FEES
import ev_v12_calendar as CAL
import ev_v12_calibration as CB
import ev_v12_protocol as P
import ev_v12_lifecycle as L
import ev_v12_acceptance as A

EXPERIMENT_ID = "nf_paper_implied_ev_v1_2"
SOURCE_LABEL = "IMPLIED_EV_V12_EXPERIMENT"
WINDOW = ((12, 30, 0), (12, 40, 0))
ENGINE_DEADLINE_MS = 2000.0
MODES = ("off", "observe", "active")
TOP_OK = 8
MAX_REJECTED_SAMPLE = 9
KEEP_SLOT_SESSIONS = 10
MAX_PENDING_ENVELOPES = 3
# R5: declared bounded G3 cohort (frozen in the envelope at decision time)
G3_COHORT_VERSION = "ev_v12_g3_cohort_v1"
G3_COHORT_RULE = ("valid (status OK) candidates other than the pick, ranked by robust EV desc (risk, name tie-break): "
                  "the first 3 with EV>0 and the first 3 with EV<=0; invalid/rejected candidates are counted only")
G3_POS, G3_NONPOS = 3, 3
# R4: an outcome waits for an authoritative accepted component at most this long after the expiry deadline
ACCEPTED_MAX_WAIT_MS = 5 * 24 * 3600 * 1000
# R7: an unfinished tracker older than this after its expiry is evicted (coverage recorded, never zero P&L)
TRACKER_EVICT_AFTER_EXPIRY_DAYS = 20
COVERAGE_FILE = "ev_coverage.json"
MAX_COVERAGE_EVENTS = 50


def _fmt_leg(l):
    q, K, cp = l
    return [int(q), int(K), int(cp)]


def _now_iso(ms):
    return datetime.fromtimestamp(ms / 1000.0, CAL.IST).isoformat()


def experiment_mode(ctx, explicit_mode):
    if explicit_mode != "paper":
        return None
    m = ctx.get("evExperimentMode")
    if not isinstance(m, str) or m.strip().lower() not in MODES:
        return None
    m = m.strip().lower()
    return None if m == "off" else m


def chain_rows(chain, expiry, fetched_ms):
    rows = []
    strikes = (chain or {}).get("strikes") or {}
    for k, node in strikes.items():
        if not isinstance(node, dict):
            continue
        for cp in ("CE", "PE"):
            leg = node.get(cp)
            if not isinstance(leg, dict):
                continue
            rows.append({"strike": k, "cp": cp, "bid": leg.get("bid"), "ask": leg.get("ask"),
                         "instrument_key": leg.get("instrument_key") or leg.get("instrumentKey"),
                         "expiry": expiry, "index": "NF", "quote_ts_ms": fetched_ms})
    return rows


def _in_window(dt):
    t = dt.hour * 3600 + dt.minute * 60 + dt.second
    (h0, m0, s0), (h1, m1, s1) = WINDOW
    return h0 * 3600 + m0 * 60 + s0 <= t < h1 * 3600 + m1 * 60 + s1


def _after_window(dt):
    t = dt.hour * 3600 + dt.minute * 60 + dt.second
    (h1, m1, s1) = WINDOW[1]
    return t >= h1 * 3600 + m1 * 60 + s1


def _v7_legs(cand, expiry):
    legs = []
    for l in (cand or {}).get("legs") or []:
        try:
            K = float(l.get("strike"))
            cp = M.CP_SIGN[str(l.get("option_type") or l.get("type")).upper()]
            act = str(l.get("action")).upper()
            q = 1 if act == "BUY" else (-1 if act == "SELL" else None)
        except (TypeError, ValueError, KeyError):
            return None
        if q is None or (l.get("expiry") not in (None, expiry)):
            return None
        legs.append((q, K, cp))
    return legs or None


def _v7_comparator(result, ref, ctx_eval, expiry):
    prim = result.get("pc2_paper_primary") or {}
    cid = prim.get("pc2_primary_candidate_id")
    out = {"selector_version": "pc2_paper_primary_v7", "candidate_id": cid, "active": bool(prim.get("active"))}
    if not cid:
        out["status"] = "NO_V7_PRIMARY"
        return out, None
    cand = None
    for pool in ("watchlist", "generated_candidates"):
        for c in result.get(pool) or []:
            if isinstance(c, dict) and c.get("id") == cid:
                cand = c
                break
        if cand:
            break
    if cand is None:
        out["status"] = "V7_CANDIDATE_NOT_IN_RESULT"
        return out, None
    out["type"] = cand.get("type")
    out["index"] = cand.get("index")
    if cand.get("index") not in (None, "NF"):
        out["status"] = "V7_NOT_NF"
        return out, None
    legs = _v7_legs(cand, expiry)
    if not legs:
        out["status"] = "V7_LEGS_UNPARSEABLE"
        return out, None
    out["legs"] = [_fmt_leg(l) for l in legs]
    if ref is None:
        out["status"] = "ENGINE_MODEL_UNAVAILABLE"
        return out, None
    r = ENG.evaluate_candidate(ref, "V7:" + str(cid)[:80], "V7", legs, **ctx_eval)
    r["v7_type"] = cand.get("type")
    out["managed_mode"] = "intraday" if cand.get("type") in L.INTRADAY_TYPES else "UNVERIFIED_TRADE_MODE"
    out["engine_status"] = r.get("status")
    if r.get("status") == "OK":
        out["engine_ev_robust"] = P.fx(r["ev_robust"], 2)
        out["engine_ev"] = [P.fx(r["ev"][k], 2) for k in ctx_eval["kset"]]
        out["risk_allowance"] = P.fx(r["risk_allowance"], 2)
    out["status"] = "RECORDED"
    return out, r


def _cand_record(r, kset, role):
    rec = {"role": role, "name": r["name"], "family": r["family"], "status": r["status"],
           "legs": [_fmt_leg(l) for l in r["legs"]]}
    if "ev_robust" in r:
        rec.update(ev_robust=P.fx(r["ev_robust"], 2), ev=[P.fx(r["ev"][k], 2) for k in kset],
                   risk=P.fx(r["risk_allowance"], 2), entry_cash=P.fx(r["entry_cash_pts"], 2),
                   resid=P.fx(r["resid"], 2), entry_fee=P.fx(r["entry_fee"], 2))
    elif "resid" in r:
        rec["resid"] = P.fx(r["resid"], 2)
    return rec


def detail_records(pick, ok_sorted, records, kset):
    out = []
    names = set()
    if pick:
        out.append(_cand_record(pick, kset, "PICK"))
        names.add(pick["name"])
    for r in ok_sorted:
        if len([x for x in out if x["role"] == "TOP"]) >= TOP_OK:
            break
        if r["name"] not in names:
            out.append(_cand_record(r, kset, "TOP"))
            names.add(r["name"])
    rejected = [r for r in records if r["status"] != "OK"]
    seen = set()
    sample = []
    for fam in ("IB", "IC", "BP", "BC", "BCD", "BPD"):
        for r in rejected:
            key = (r["family"], r["status"])
            if r["family"] == fam and key not in seen:
                seen.add(key)
                sample.append(r)
                break
    for r in rejected:
        if len(sample) >= MAX_REJECTED_SAMPLE:
            break
        key = (r["family"], r["status"])
        if key not in seen:
            seen.add(key)
            sample.append(r)
    for r in sample[:MAX_REJECTED_SAMPLE]:
        out.append(_cand_record(r, kset, "REJECTED_SAMPLE"))
    return out[:P.MAX_DETAIL_RECORDS - 1]   # one slot reserved for the v7 comparator


def _counts(records):
    by_status, by_family = {}, {}
    for r in records:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        if r["status"] == "OK":
            by_family[r["family"]] = by_family.get(r["family"], 0) + 1
    return by_status, by_family


def _lot(session_date, expiry, chain):
    try:
        from contract_lot_table import resolve_contract_lot, lot_table_version_id
    except Exception:
        return None, {"status": "LOT_TABLE_UNAVAILABLE"}
    captured = (chain or {}).get("lotSize") or (chain or {}).get("lot_size")
    res = resolve_contract_lot("NF", as_of=session_date, expiry=expiry,
                               captured_contract_lot=captured if captured not in (0, None, "") else None)
    provenance = {key: res.get(key) for key in ("lot_table_version", "lot_table_source", "lot_table_sha256")}
    if not res.get("resolved") or not res.get("contract_lot_size") or res.get("lot_conflict"):
        status = "LOT_TABLE_UNAVAILABLE" if res.get("unavailable_reason") == "lot_table_unavailable" else "LOT_AUTHORITY_UNRESOLVED"
        return None, dict(provenance, status=status, reason=res.get("unavailable_reason"))
    return int(res["contract_lot_size"]), {"status": "OK", "contract_lot_size": int(res["contract_lot_size"]),
                                            **provenance,
                                            "matched_rule_id": res.get("matched_rule_id"),
                                            "lot_source": res.get("lot_source")}


def g3_cohort(pick, ok_sorted):
    others = [r for r in ok_sorted if not pick or r["name"] != pick["name"]]
    pos = [r for r in others if r["ev_robust"] > 0][:G3_POS]
    nonpos = [r for r in others if not r["ev_robust"] > 0][:G3_NONPOS]
    return pos + nonpos


def _g3_business(members, ok_sorted, records):
    return {"version": G3_COHORT_VERSION, "rule": G3_COHORT_RULE, "n_valid": len(ok_sorted),
            "n_valid_pos": sum(1 for r in ok_sorted if r["ev_robust"] > 0),
            "n_invalid": sum(1 for r in records if r.get("status") != "OK"),
            "members": [{"name": r["name"], "ev_robust": P.fx(r["ev_robust"], 2), "legs": [_fmt_leg(l) for l in r["legs"]],
                         "entry_cash": P.fx(r["entry_cash_pts"], 2), "entry_fee": P.fx(r["entry_fee"], 2)}
                        for r in members]}


def executable_entry(ref, legs, units, entry_date):
    """Entry cash/fee/structural bounds of signed legs at the decision book, independent of engine scoring
    (R5: a v7 comparator rejected only by engine scoring keeps a comparable executable path)."""
    q = ref["q"]
    exe = {}
    for n, K, cp in legs:
        ba = q.get((float(K), "CE" if cp == 1 else "PE"))
        if ba is None or ba[0] <= 0 or ba[1] < ba[0]:
            return None, "V7_LEG_QUOTES_UNAVAILABLE"
        exe[(K, cp)] = ba[1] if n > 0 else ba[0]
    lo, hi = M.structural_range(legs)
    if lo is None:
        return None, "V7_UNBOUNDED"
    entry_cash = -sum(n * exe[(K, cp)] for n, K, cp in legs)
    try:
        fee = FEES.one_side_fee(sum(abs(n) * exe[(K, cp)] for n, K, cp in legs if n < 0),
                                sum(abs(n) * exe[(K, cp)] for n, K, cp in legs if n > 0), len(legs), units, entry_date)
    except FEES.FeeUnavailable as e:
        return None, "V7_" + str(e)
    return {"entry_cash_pts": entry_cash, "entry_fee": fee, "structural_loss_pts": -(lo + entry_cash),
            "structural_max_profit_pts": (None if hi is None else hi + entry_cash), "units": units}, "OK"


def compute_decision(ctx, result, *, now_ms, session_date, expiry, ste, planned, cal, kset, cal_pub,
                     deadline=None):
    """Pure computation of one scheduled decision. Returns (status, business, local, timings)."""
    t_start = time.perf_counter()
    dl = deadline or M.Deadline(ENGINE_DEADLINE_MS)
    chain = ctx.get("nfChain") or {}
    fetched = ctx.get("evQuoteFetchedAtMs")
    fetched = float(fetched) if isinstance(fetched, (int, float)) and not isinstance(fetched, bool) else None
    business = {"schema_version": "ev_decision_business_v1", "experiment_id": EXPERIMENT_ID, "index": "NF",
                "session_date": session_date, "expiry": expiry, "sessions_to_expiry": ste,
                "planned_exit_session": planned, "policy_id": L.POLICY_ID,
                "engine_version": M.ENGINE_VERSION, "model_version": M.MODEL_VERSION,
                "selector_version": ENG.SELECTOR_VERSION, "universe_version": ENG.UNIVERSE_VERSION,
                "calendar_version": cal.version, "rate_convention": "zero_rate_D_h_1",
                "decision_poll_ms": int(now_ms),
                "calibration_version": (cal_pub or {}).get("calibration_version"),
                "calibration_sha256": (cal_pub or {}).get("payload_sha256"),
                # Codex C4: research provenance (a FORWARD claim needs a live capture AND a publication available
                # before this decision; the calibration family name alone is not proof of forward causality)
                "calibration_available_at": (cal_pub or {}).get("available_at"),
                "calibration_retrospective_seed": (cal_pub or {}).get("retrospective_seed"),
                "capture_source": str(ctx.get("evCaptureSource") or "UNDECLARED")[:32],
                "kset": [P.fx(k, 6) for k in kset] if kset else None}
    local = {}
    if fetched is None:
        return "FAILED", dict(business, status="FAILED", failure="QUOTE_TIMESTAMP_UNAVAILABLE"), local, {}
    lot, lot_id = _lot(session_date, expiry, chain)
    business["lot_identity"] = lot_id
    if lot is None:
        return "FAILED", dict(business, status="FAILED", failure=lot_id["status"]), local, {}
    try:
        business["fee_identity_entry"] = FEES.fee_identity(session_date)
        business["fee_identity_exit"] = FEES.fee_identity(planned)
    except FEES.FeeUnavailable as e:
        return "FAILED", dict(business, status="FAILED", failure="FEE_" + str(e)), local, {}
    rows = chain_rows(chain, expiry, fetched)
    business["quote_source_sha256"] = CB.quote_digest(rows) if rows else None
    try:
        ref = M.build_reference(rows, index="NF", expiry=expiry, decision_ms=now_ms, deadline=dl)
        business["reference_status"] = ref["status"]
        business["quote_rejects"] = len(ref.get("rejects") or [])
        if ref["status"] != "OK":
            return "FAILED", dict(business, status="FAILED", failure=ref["status"]), local, \
                {"engine_ms": dl.elapsed_ms()}
        business["F"] = P.fx(ref["F"], 2)
        business["density_status"] = ref["density_status"]
        business["n_atoms"] = ref["diag"]["n_atoms"]
        entry_dt = datetime.fromtimestamp(now_ms / 1000.0, CAL.IST)
        exit_target = CAL.ist_dt(planned, 15, 15, 0)
        tau = cal.tau(entry_dt, exit_target, expiry)
        business["tau"] = P.fx(tau, 6)
        ev_kwargs = dict(index="NF", expiry=expiry, tau=tau, kset=kset, contract_lot=lot, n_lots=1,
                         entry_date=session_date, exit_date=planned)
        records, K0 = ENG.evaluate_universe(ref, deadline=dl, **ev_kwargs)
        business["K0"] = K0
        pick, ok_sorted = ENG.select(records)
        # G4 engine k: the slow-filter central estimate (frozen in the publication)
        k_central = float(cal_pub["k_slow"]) if cal_pub and cal_pub.get("k_slow") else 1.0
        g4 = ENG.g4_forecasts(ref, tau, k_central, deadline=dl)
    except M.EngineTimeout as e:
        return "FAILED", dict(business, status="FAILED", failure="TIMEOUT", timeout_stage=e.stage), local, \
            {"engine_ms": dl.elapsed_ms()}
    except (ValueError, CAL.CalendarUnavailable) as e:
        return "FAILED", dict(business, status="FAILED", failure="INPUT_" + str(e)[:60]), local, \
            {"engine_ms": dl.elapsed_ms()}
    engine_select_ms = dl.elapsed_ms()
    es_status, es = ENG.expected_shortfall_top(ref, ok_sorted, tau, dl)
    by_status, by_family = _counts(records)
    band_lim = M.BAND_STRADDLES * M.atm_straddle(ref["q"], ref["F"])
    ctx_eval = dict(ev_kwargs, pts={k: M.ptilde(ref["lat"], k, tau) for k in kset}, band_lim=band_lim)
    ctx_eval.pop("deadline", None)
    v7, v7_rec = _v7_comparator(result, ref, ctx_eval, expiry)
    details = detail_records(pick, ok_sorted, records, kset)
    g3 = g3_cohort(pick, ok_sorted)
    for d in details:
        if d["name"] in es:
            d["es5"] = P.fx(es[d["name"]], 2)
    business.update(counts_by_status=by_status, ok_by_family=by_family, n_universe=len(records),
                    candidates=details, v7_comparator=v7, es_status=es_status, g4_version=ENG.G4_VERSION,
                    g4=[{"c": P.fx(x["c"], 2), "threshold": P.fx(x["threshold"], 2), "p_engine": P.fx(x["p_engine"], 6),
                         "p_k1": P.fx(x["p_k1"], 6), "inversion": x["inversion"]} for x in g4],
                    g4_k_engine=P.fx(k_central, 6), g3_cohort=_g3_business(g3, ok_sorted, records))
    if not by_status.get("OK"):
        status = "NO_ELIGIBLE"
    elif pick is None:
        status = "ABSTAIN"
    else:
        status = "DECIDED"
        business["pick"] = _cand_record(pick, kset, "PICK")
        business["pick"]["valid_until_ms"] = int(now_ms + L.DECISION_VALIDITY_MS)
        cal_days = (CAL._d(planned) - CAL._d(session_date)).days
        business["rate_sensitivity_diag_rupees"] = P.fx(ENG.rate_sensitivity(pick, cal_days), 2)
    business["status"] = status
    local = {"ref": ref, "pick": pick, "v7": v7_rec, "v7_view": v7, "lot": lot, "tau": tau, "kset": kset,
             "band_lim": band_lim, "g3": g3}
    return status, business, local, {"engine_ms": dl.elapsed_ms(), "engine_select_ms": engine_select_ms,
                                     "py_wall_ms": (time.perf_counter() - t_start) * 1000.0,
                                     "deadline_checks": dl.checks}


def acceptance_context(decision_id, business, local):
    pick = local["pick"]
    ref = local["ref"]
    legs = []
    for qn, K, cp in pick["legs"]:
        b, a = pick["bidask"][(K, cp)]
        legs.append({"qty": int(qn), "strike": float(K), "cp": int(cp), "bid": b, "ask": a,
                     "instrument_key": pick["instrument_keys"][(K, cp)]})
    return {"decision_id": decision_id, "structure": pick["name"], "family": pick["family"], "index": "NF",
            "expiry": business["expiry"], "planned_exit_session": business["planned_exit_session"],
            "valid_until_ms": business["pick"]["valid_until_ms"], "legs": legs, "kset": local["kset"],
            "tau": local["tau"], "contract_lot": local["lot"], "n_lots": 1,
            "lot_identity": business["lot_identity"], "engine_version": M.ENGINE_VERSION,
            "model_version": M.MODEL_VERSION, "calibration_version": business.get("calibration_version"),
            "source_quote_sha256": business.get("quote_source_sha256"),
            "model": {"atoms": [[s, p] for s, p in ref["atoms"]], "F": ref["F"],
                      "flags": sorted(ref["flags"]), "screen_flags": ref["diag"]["screen_flags"],
                      "band_lim": local["band_lim"]}}


def recommendation_view(slot, now_ms, mode, state_dir=None):
    """What the PWA may show. Only an unexpired, unconsumed, active-mode DECIDED pick is actionable. The acceptance
    status comes from the authoritative acceptance ledger (R1), not from a cached view."""
    env = slot.get("envelope") or {}
    try:
        biz = json.loads(env.get("envelope_text") or "{}").get("business") or {}
    except ValueError:
        biz = {}
    view = {"source_label": SOURCE_LABEL, "slot_state": slot.get("state"), "session_date": slot.get("session_date"),
            "decision_id": env.get("decision_id"), "status": biz.get("status") or slot.get("state"),
            "failure": biz.get("failure"), "mode": mode}
    pick = biz.get("pick")
    led = A.entry(state_dir, env.get("decision_id")) if (state_dir and env.get("decision_id")) else None
    if led and led.get("state") == "RESERVED" and now_ms > int(led.get("confirm_by_ms") or 0):
        led = None
    acc_state = (led or {}).get("state") or ("COMMITTED" if slot.get("acceptance") else None)
    if pick:
        valid_until = pick.get("valid_until_ms")
        view["pick"] = {"structure": pick["name"], "family": pick["family"], "type": L.FAMILY_TYPE.get(pick["family"]),
                        "legs": pick["legs"], "ev_robust_per_lot": pick.get("ev_robust"),
                        "risk_allowance": pick.get("risk"), "entry_cash_pts": pick.get("entry_cash"),
                        "planned_exit": "%s 15:15 IST (Rule B)" % biz.get("planned_exit_session"),
                        "planned_exit_session": biz.get("planned_exit_session"), "expiry": biz.get("expiry"),
                        "calibration_version": biz.get("calibration_version"), "model_version": biz.get("model_version"),
                        "valid_until_ms": valid_until}
        consumed = acc_state in ("COMMITTED", "PERSISTED", "CLOSED")
        actionable = (mode == "active" and not consumed and valid_until is not None and now_ms <= valid_until)
        view["actionable"] = bool(actionable)
        view["expired"] = bool(valid_until is not None and now_ms > valid_until)
        view["acceptance_status"] = {"COMMITTED": "PENDING_SYNC", "PERSISTED": "ACCEPTED", "CLOSED": "CLOSED",
                                     "RESERVED": "CONFIRMING"}.get(acc_state)
    else:
        view["actionable"] = False
    if biz.get("counts_by_status"):
        view["counts_by_status"] = biz["counts_by_status"]
    return view


def _tracker_path(state_dir, session_date):
    return os.path.join(state_dir, "tracker_NF_%s.json" % session_date)


def _init_trackers(state_dir, session_date, business, local, now_ms):
    trs = []
    pick = local.get("pick")
    if pick is not None:
        trs.append(L.new_tracker("ENGINE_PICK", pick["name"], pick["legs"], pick["entry_cash_pts"], pick["units"],
                                 pick["entry_fee"], pick.get("structural_max_profit_pts"), pick["structural_loss_pts"],
                                 L.ENGINE_TPSL_POLICY))
    v7v = local.get("v7_view") or {}
    v7 = local.get("v7") or {}
    v7_cov = {"status": v7v.get("status"), "engine_status": v7v.get("engine_status")}
    if v7v.get("legs") and local.get("ref") is not None:
        legs = [tuple(l) for l in v7v["legs"]]
        ent, st = executable_entry(local["ref"], legs, int(local["lot"]), session_date)
        v7_cov["path_status"] = st
        if ent is not None:
            trs.append(L.new_tracker("V7_COMPARATOR", v7.get("name") or ("V7:" + str(v7v.get("candidate_id"))[:60]),
                                     legs, ent["entry_cash_pts"], ent["units"], ent["entry_fee"],
                                     ent["structural_max_profit_pts"], ent["structural_loss_pts"],
                                     L.MANAGED_V7_POLICY, managed_mode=v7v.get("managed_mode")))
    for r in local.get("g3") or []:
        trs.append(L.new_tracker("G3_SAMPLE", r["name"], r["legs"], r["entry_cash_pts"], r["units"], r["entry_fee"],
                                 r.get("structural_max_profit_pts"), r["structural_loss_pts"], None, tpsl=False))
    doc = {"session_date": session_date, "expiry": business["expiry"],
           "planned_exit_session": business["planned_exit_session"], "decision_id": None,
           "trackers": trs, "created_ms": int(now_ms), "g4": business.get("g4"), "F_entry": business.get("F"),
           "F_exit": None, "outcome": None, "v7_coverage": v7_cov,
           # Codex D4: the decision origin of the horizon tau (the actual exit time is the Rule B fill poll)
           "decision_poll_ms": int(business.get("decision_poll_ms") or now_ms)}
    return doc


def _save_json(path, obj):
    P.atomic_write(path, json.dumps(obj, sort_keys=True, default=lambda o: list(o) if isinstance(o, tuple) else str(o)))


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _snapshot(t):
    es = t.get("entry_snapshot") if isinstance(t, dict) else None
    if isinstance(es, str):
        try:
            es = json.loads(es)
        except ValueError:
            es = None
    return es if isinstance(es, dict) else {}


def rule_b_ledger_entry(state_dir, trade_id):
    doc = _load_json(os.path.join(state_dir, "rule_b_ledger.json")) or {}
    return ((doc.get("trades") or {}).get(str(trade_id))) if trade_id else None


def _accepted_outcome(state_dir, decision_id, open_trades, closed_trades):
    """R4: (accepted component, authoritative?). Joins on the authoritative acceptance ledger and the native Rule B
    fill ledger (same state dir), not on a lossy closed-trade projection; computes net with ev_v12_fees."""
    e = A.entry(state_dir, decision_id) if state_dir else None

    def find(trades, uid):
        for t in trades or []:
            if isinstance(t, dict) and (_snapshot(t).get("ev_decision_id") == decision_id
                                        or (uid and str(t.get("id") or t.get("trade_id") or "") == uid)):
                return t
        return None
    uid = (e or {}).get("trade_uid")
    row_id = (e or {}).get("trade_id")

    def find2(trades):
        hit = find(trades, row_id) if row_id else None
        if hit is None:
            for t in trades or []:
                if isinstance(t, dict) and uid and _snapshot(t).get("ev_trade_uid") == uid:
                    return t
        return hit or find(trades, None)
    closed = find2(closed_trades)
    if not row_id:   # persisted but the PWA never reported it (crash): recover the row id from the uid-stamped row
        hit = closed or next((t for t in open_trades or [] if isinstance(t, dict) and uid
                              and _snapshot(t).get("ev_trade_uid") == uid), None)
        row_id = str(hit.get("id") or "") if hit and hit.get("id") is not None else None
    if e is None or e.get("state") == "RESERVED":
        if closed is not None or find(open_trades, None) is not None:
            return {"status": "ACCEPTED_LEDGER_MISSING", "evidence": "trade_identity_without_acceptance_ledger"}, False
        return {"status": "NEVER_ACCEPTED"}, True
    rec = e.get("record") or {}
    base = {"trade_id": row_id, "trade_uid": uid, "accepted_at_ms": rec.get("accepted_at_ms"),
            "entry_fee": P.fx(((rec.get("entry_fill") or {}).get("entry_fee")), 2)}
    rb = rule_b_ledger_entry(state_dir, row_id) if row_id else None
    delivery = (rb or {}).get("delivery") or "PENDING_APPLY"
    manual_won = rb is not None and (delivery == "MANUAL_CLOSE_CONFLICT"
                                     or (rb.get("state") == "MANUAL_CLOSED" and delivery == "REMOTE_CONFIRMED"))
    if manual_won:
        # Codex C1/C2: the manual writer won the shared conditional arbitration (remote proof recorded natively)
        if closed is None:
            return dict(base, status="MANUAL_EARLY_CLOSE", net_pnl=None,
                        net_status="MANUAL_CLOSE_PENDING_PROJECTION", local_fill=_local_fill(rec, rb)), False
        net = closed.get("net_pnl")
        out = dict(base, status="MANUAL_EARLY_CLOSE", gross_pnl=P.fx(closed.get("actual_pnl"), 2),
                   net_pnl=P.fx(net, 2) if isinstance(net, (int, float)) else None,
                   net_status="RECORDED" if isinstance(net, (int, float)) else "MANUAL_CLOSE_NET_NOT_RECORDED")
        lf = _local_fill(rec, rb)
        if lf:
            out["superseded_rule_b_fill"] = lf   # the competing frozen fill is kept as evidence, never the result
        return out, True
    if rb and rb.get("state") in ("EXIT_FILLED", "UNRESOLVED", "MANUAL_CLOSED"):
        if delivery != "REMOTE_CONFIRMED" or rb.get("manual_pending"):
            # Codex C1: a locally frozen fill (or a pending manual close) is evidence, not the final accepted result,
            # until the remote arbitration is proven. Never authoritative here.
            return dict(base, status="PENDING_ARBITRATION", delivery=delivery,
                        manual_pending=bool(rb.get("manual_pending")) or rb.get("state") == "MANUAL_CLOSED",
                        local_fill=_local_fill(rec, rb)), False
    if rb and rb.get("state") == "EXIT_FILLED" and rb.get("fill"):
        kind = "PLANNED_CLOSE" if (rb["fill"].get("kind") == "PLANNED_EXIT") else "LATE_CLOSE"
        try:
            econ = L.rule_b_close_economics(rec["entry_fill"], rb["fill"])
        except (KeyError, ValueError, TypeError, FEES.FeeUnavailable) as x:
            return dict(base, status=kind, net_pnl=None, economics_error=str(x)[:60],
                        delivery=delivery), True
        return dict(base, status=kind, gross_pnl=P.fx(econ["gross_pnl"], 2), exit_fee=P.fx(econ["exit_fee"], 2),
                    net_pnl=P.fx(econ["net_pnl"], 2), exit_ts=str(rb["fill"].get("ts_iso"))[:25],
                    delivery=delivery,
                    fee_identity=(econ["exit_fee_identity"] or {}).get("fee_contract_version")), True
    if rb and rb.get("state") == "UNRESOLVED":
        return dict(base, status="UNRESOLVED", reason=rb.get("reason"), delivery=delivery), True
    if closed is not None:
        reason = str(closed.get("exit_reason") or "")
        if "RULE_B_" not in reason:
            net = closed.get("net_pnl")
            return dict(base, status="MANUAL_EARLY_CLOSE", gross_pnl=P.fx(closed.get("actual_pnl"), 2),
                        net_pnl=P.fx(net, 2) if isinstance(net, (int, float)) else None,
                        net_status="RECORDED" if isinstance(net, (int, float)) else "MANUAL_CLOSE_NET_NOT_RECORDED"), True
    if e.get("state") == "COMMITTED":
        return dict(base, status="PENDING_COMMIT_SYNC"), False
    return dict(base, status="OPEN"), False


PRICING_DIAG_VERSION = "ev_v12_conditional_pricing_diag_v1"


def pricing_diagnostic(atoms, tau, legs, F_exit, quotes, exec_value_pts, contract_lot, exit_fee_rupees=None):
    """Codex C4: conditional horizon option-price diagnostic at the REALIZED exit state, separate from the EV
    forecast residual. Shared v1.2 pricing: latent state w inverted from the realized exit forward (invert_forward),
    model value = E[expiry payoff | w] (state_values, zero-rate). Observed: mid and executable structure values.
      model_price_residual = observed mid value - conditional model value   (gross pricing error)
      execution_residual   = executable value - mid value                   (spread / execution)
      exit_fee             reported separately (fees are never mixed into the pricing residual)
    Values are position values sum(qty * price) in points; per-lot rupees = points * contract_lot."""
    if not atoms or tau is None or F_exit is None or not quotes or len(quotes) != len(legs):
        return {"v": PRICING_DIAG_VERSION, "status": "UNAVAILABLE", "reason": "MODEL_OR_EXIT_STATE_MISSING"}
    try:
        lat = M.latent_bounds([(float(a), float(b)) for a, b in atoms])
        w, st = M.invert_forward(lat, float(tau), float(F_exit))
        if w is None:
            return {"v": PRICING_DIAG_VERSION, "status": "UNAVAILABLE", "reason": st}
        model_v, _sh = M.state_values(lat, float(tau), w, [(float(q), float(K), int(cp)) for q, K, cp in legs])
        mid_v = sum(float(q) * 0.5 * (float(ba[0]) + float(ba[1])) for (q, K, cp), ba in zip(legs, quotes))
    except (TypeError, ValueError, ZeroDivisionError, M.EngineTimeout) as e:
        return {"v": PRICING_DIAG_VERSION, "status": "UNAVAILABLE", "reason": "PRICING_ERROR:" + type(e).__name__}
    lot = float(contract_lot)
    out = {"v": PRICING_DIAG_VERSION, "status": "OK", "F_exit": P.fx(F_exit, 2), "tau": P.fx(tau, 6),
           "model_value_pts": P.fx(model_v, 4), "mid_value_pts": P.fx(mid_v, 4),
           "model_price_residual_per_lot_rupees": P.fx((mid_v - model_v) * lot, 2)}
    if exec_value_pts is not None:
        out["exec_value_pts"] = P.fx(exec_value_pts, 4)
        out["execution_residual_per_lot_rupees"] = P.fx((float(exec_value_pts) - mid_v) * lot, 2)
    if exit_fee_rupees is not None:
        out["exit_fee_rupees"] = P.fx(exit_fee_rupees, 2)
    return out


def _pick_pricing_diag(state_dir, doc, tr, q, cal=None):
    """Codex D4: the horizon tau runs from the decision poll to the ACTUAL tracker exit time (rule_b.ts, the poll
    that froze the Rule B exit), not the planned 15:15 tau stored in the acceptance context."""
    slot = P.load_slot(state_dir, "NF", doc["session_date"]) or {}
    ac = slot.get("acceptance_context") or {}
    model = ac.get("model") or {}
    rb = tr.get("rule_b") or {}
    origin_ms = doc.get("decision_poll_ms")
    if origin_ms is None:
        origin_ms = ((slot.get("business") or {}).get("decision_poll_ms"))
    exit_ts = rb.get("ts")
    if not isinstance(origin_ms, (int, float)) or isinstance(origin_ms, bool) or not exit_ts:
        return {"v": PRICING_DIAG_VERSION, "status": "UNAVAILABLE", "reason": "EXIT_OR_ORIGIN_TIME_MISSING"}
    try:
        cal = cal or CAL.DEFAULT_CALENDAR
        origin_dt = datetime.fromtimestamp(float(origin_ms) / 1000.0, CAL.IST)
        exit_dt = datetime.fromisoformat(str(exit_ts))
        if exit_dt.tzinfo is None:
            exit_dt = exit_dt.replace(tzinfo=CAL.IST)
        tau = cal.tau(origin_dt, exit_dt, doc["expiry"])
    except Exception:   # noqa: BLE001 - diagnostic only, coverage is reported instead
        return {"v": PRICING_DIAG_VERSION, "status": "UNAVAILABLE", "reason": "CALENDAR_UNAVAILABLE"}
    if tau is None or not (float(tau) > 0):
        return {"v": PRICING_DIAG_VERSION, "status": "UNAVAILABLE", "reason": "CALENDAR_UNAVAILABLE"}
    Fx = None
    try:
        Fx, _f, _s = M.parity_forward(q)
    except Exception:   # noqa: BLE001 - diagnostic only, coverage is reported instead
        Fx = None
    out = pricing_diagnostic(model.get("atoms"), tau, tr["legs"], Fx, rb.get("quotes"), rb.get("mark_pts"),
                             ac.get("contract_lot"), rb.get("exit_fee"))
    out["tau_basis"] = "DECISION_TO_ACTUAL_EXIT"
    return out


def _local_fill(rec, rb):
    """Small evidence record of a locally frozen Rule B fill (never the final result by itself)."""
    if not rb or rb.get("state") != "EXIT_FILLED" or not rb.get("fill"):
        return None
    out = {"kind": rb["fill"].get("kind"), "ts": str(rb["fill"].get("ts_iso"))[:25],
           "close_value_pts": P.fx(rb["fill"].get("close_value_pts"), 4)}
    try:
        econ = L.rule_b_close_economics(rec["entry_fill"], rb["fill"])
        out.update(gross_pnl=P.fx(econ["gross_pnl"], 2), net_pnl=P.fx(econ["net_pnl"], 2))
    except (KeyError, ValueError, TypeError, FEES.FeeUnavailable):
        out["net_pnl"] = None
    return out


def _ev(e, keep_q=True):
    if not e:
        return None
    out = {"ts": e["ts"][:19], "net": P.fx(e["net"], 2), "gross": P.fx(e.get("gross"), 2), "fee": P.fx(e["exit_fee"], 2),
           "kind": e.get("kind")}
    if keep_q:
        out["q"] = [[P.fx(b, 2), P.fx(a, 2)] for b, a in e["quotes"]]
    return out


def _tracker_summary(tr):
    if tr["role"] == "G3_SAMPLE":
        return {"role": "G3_SAMPLE", "structure": str(tr["structure"])[:60], "rule_b": _ev(tr["rule_b"]),
                "rule_b_status": tr.get("rule_b_status"), "polls_quoted": tr.get("polls_quoted", 0)}
    out = {"role": tr["role"], "structure": str(tr["structure"])[:60], "rule_b": _ev(tr["rule_b"]),
           "rule_b_status": tr.get("rule_b_status"), "tp_first": _ev(tr["tp_first"]), "sl_first": _ev(tr["sl_first"]),
           "polls_seen": tr["polls_seen"], "polls_quoted": tr.get("polls_quoted", 0),
           "path_resolution": "brain_poll_5min", "tpsl_policy": tr.get("managed_policy"),
           "entry": {"cash": P.fx(tr["entry_cash_pts"], 2), "fee": P.fx(tr["entry_fee"], 2), "units": tr["units"],
                     "legs": [_fmt_leg(l) for l in tr["legs"]]}}
    if tr["role"] == "ENGINE_PICK":
        out["pricing_diag"] = tr.get("pricing_diag") or {"v": PRICING_DIAG_VERSION, "status": "UNAVAILABLE",
                                                         "reason": "NO_RULE_B_EXIT_POLL" if tr.get("rule_b") is None
                                                         else "NOT_RECORDED"}
    if tr.get("managed_policy") == L.MANAGED_V7_POLICY:
        out["managed_exit"] = _ev(tr["managed_exit"])
        out["managed_mode"] = tr.get("managed_mode")
    return out


# Codex D3: deterministic staged compaction keeping every outcome inside the 4096-byte cap (the cap is never raised).
# Each stage removes only evidence DETAIL; every field the report reads (net, rule_b_status, tpsl_policy, structure,
# entry.units, pricing residuals/status, accepted, g4_labels, F_entry/F_exit) is kept. The applied stages are recorded
# in body["compaction"] as coverage metadata.
OUTCOME_COMPACTION_VERSION = "ev_v12_outcome_compaction_v1"
_EVENT_KEYS = ("tp_first", "sl_first", "managed_exit", "rule_b")


def _cmp_events(body, keep, roles=None):
    for c in body["components"]:
        if roles and c.get("role") not in roles:
            continue
        for k in _EVENT_KEYS:
            if isinstance(c.get(k), dict):
                c[k] = {kk: v for kk, v in c[k].items() if kk in keep}


def _cmp_pricing(body):
    for c in body["components"]:
        px = c.get("pricing_diag")
        if isinstance(px, dict):
            c["pricing_diag"] = {k: v for k, v in px.items() if k in (
                "v", "status", "reason", "tau", "tau_basis", "model_price_residual_per_lot_rupees",
                "execution_residual_per_lot_rupees", "exit_fee_rupees")}


def _cmp_entry_legs(body):
    for c in body["components"]:
        if isinstance(c.get("entry"), dict):
            c["entry"] = {k: v for k, v in c["entry"].items() if k != "legs"}


def _cmp_essential(body):
    keep = ("role", "structure", "rule_b", "rule_b_status", "tp_first", "sl_first", "managed_exit", "tpsl_policy",
            "entry", "pricing_diag")
    omitted = set()
    for c in body["components"]:
        omitted.update(k for k in c if k not in keep)
        for k in list(c):
            if k not in keep:
                del c[k]
        if isinstance(c.get("entry"), dict):
            c["entry"] = {"units": c["entry"].get("units")}
    _cmp_events(body, ("net",))
    if isinstance(body.get("v7_coverage"), dict):
        body["v7_coverage"] = {"status": body["v7_coverage"].get("status")}
    body["components_omitted"] = sorted(omitted)


OUTCOME_COMPACTION_STAGES = (
    ("Q_TP_SL_MANAGED_G3", lambda b: _stage1(b)),
    ("Q_ALL", lambda b: _cmp_events(b, ("ts", "net", "gross", "fee", "kind"))),
    ("EVENT_DETAIL_G3", lambda b: _cmp_events(b, ("net", "kind"), roles=("G3_SAMPLE",))),
    ("PRICING_DIAG_SUMMARY", _cmp_pricing),
    ("ENTRY_LEGS", _cmp_entry_legs),
    ("EVENT_DETAIL_ALL", lambda b: _cmp_events(b, ("net", "kind"))),
    ("ESSENTIAL_ONLY", _cmp_essential),
)


def _stage1(body):
    """The pre-D3 first stage (unchanged): quotes dropped from TP/SL/managed evidence and from G3 Rule B evidence."""
    for c in body["components"]:
        for k in ("tp_first", "sl_first", "managed_exit") + (("rule_b",) if c["role"] == "G3_SAMPLE" else ()):
            if c.get(k):
                c[k].pop("q", None)


def build_outcome(doc, accepted, now_ms):
    body = {"schema": P.OUTCOME_SCHEMA, "decision_id": doc["decision_id"], "index": "NF",
            "session_date": doc["session_date"], "expiry": doc["expiry"],
            "planned_exit_session": doc["planned_exit_session"], "policy_id": L.POLICY_ID,
            "outcome_time_ms": int(now_ms), "components": [_tracker_summary(t) for t in doc["trackers"]],
            "accepted": accepted, "F_entry": doc.get("F_entry"), "F_exit": P.fx(doc.get("F_exit"), 2),
            "v7_coverage": doc.get("v7_coverage"), "g3_cohort_version": G3_COHORT_VERSION,
            "threshold_basis": L.THRESHOLD_BASIS,
            "g4_labels": L.g4_labels(doc.get("g4") or [], doc.get("F_exit")) if doc.get("g4") else None}
    text = P.canon(body)
    dropped = []
    for name, stage in OUTCOME_COMPACTION_STAGES:
        if len(text.encode("utf-8")) <= P.MAX_OUTCOME_BYTES:
            break
        stage(body)
        dropped.append(name)
        body["compaction"] = {"v": OUTCOME_COMPACTION_VERSION, "level": len(dropped), "dropped": list(dropped)}
        body["truncated"] = "+".join(dropped)
        text = P.canon(body)
    if len(text.encode("utf-8")) > P.MAX_OUTCOME_BYTES:
        raise P.CanonError("OUTCOME_OVERSIZE")
    return {"decision_id": doc["decision_id"], "outcome_text": text, "outcome_sha256": P.sha256_text(text),
            "outcome_bytes": len(text.encode("utf-8"))}


def step_trackers(state_dir, ctx, now_ms, cal, open_trades, closed_trades):
    """Advance comparison trackers for recent decisions; return finalized outcome envelopes (frozen)."""
    out = []
    if not os.path.isdir(state_dir):
        return out
    now_dt = datetime.fromtimestamp(now_ms / 1000.0, CAL.IST)
    chain = ctx.get("nfChain") or {}
    expiry = ctx.get("nfExpiry")
    fetched = ctx.get("evQuoteFetchedAtMs")
    q = None
    for fn in sorted(os.listdir(state_dir)):
        if not (fn.startswith("tracker_NF_") and fn.endswith(".json")):
            continue
        path = os.path.join(state_dir, fn)
        doc = _load_json(path)
        if not doc:
            continue
        if doc.get("outcome"):
            out.append(doc["outcome"])
            continue
        if expiry == doc["expiry"] and isinstance(fetched, (int, float)):
            if q is None:
                q, _m, _r = M.validate_chain_quotes(chain_rows(chain, expiry, fetched), index="NF", expiry=expiry,
                                                    decision_ms=now_ms, require_identity=False)
            for tr in doc["trackers"]:
                had = tr.get("rule_b") is not None
                L.tracker_step(tr, q, now_dt, doc["planned_exit_session"], doc["expiry"], doc["session_date"])
                if tr["role"] == "ENGINE_PICK" and not had and tr.get("rule_b") is not None:
                    tr["pricing_diag"] = _pick_pricing_diag(state_dir, doc, tr, q, cal)   # same poll as the fill
            secs = now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second
            if now_dt.date().isoformat() == doc["planned_exit_session"] and 15 * 3600 + 15 * 60 <= secs < 15 * 3600 + 28 * 60 \
                    and doc.get("F_exit") is None:
                Fx, _f, _s = M.parity_forward(q)
                doc["F_exit"] = Fx
        deadline = CAL.ist_dt(doc["expiry"], 15, 28, 0)
        if now_dt >= deadline and doc.get("decision_id"):
            L.finalize_trackers(doc["trackers"], now_dt, doc["expiry"])
            accepted, authoritative = _accepted_outcome(state_dir, doc["decision_id"], open_trades, closed_trades)
            waited = now_ms - deadline.timestamp() * 1000.0
            if authoritative or waited >= ACCEPTED_MAX_WAIT_MS:
                if not authoritative:
                    # Codex C1: at the declared bounded timeout the accepted component is labelled UNKNOWN with
                    # coverage; a provisional local fill is never certified as final actual performance.
                    prior = accepted.get("status")
                    accepted = {k: v for k, v in accepted.items()
                                if k not in ("gross_pnl", "net_pnl", "exit_fee", "net_status")}
                    accepted.update(status="UNKNOWN_PENDING_ARBITRATION" if prior == "PENDING_ARBITRATION"
                                    else "UNKNOWN", unresolved_status=prior,
                                    finalized_without_authoritative_accepted=True,
                                    coverage={"waited_ms": int(waited), "max_wait_ms": ACCEPTED_MAX_WAIT_MS})
                doc["outcome"] = build_outcome(doc, accepted, now_ms)
                out.append(doc["outcome"])
            else:
                doc["awaiting_accepted_component"] = accepted.get("status")
        _save_json(path, doc)
    return out


def _coverage_event(state_dir, ev):
    path = os.path.join(state_dir, COVERAGE_FILE)
    doc = _load_json(path) or {"schema": "ev_v12_coverage_v1", "events": [], "counts": {}}
    doc["counts"][ev["kind"]] = doc["counts"].get(ev["kind"], 0) + 1
    doc["events"] = (doc["events"] + [ev])[-MAX_COVERAGE_EVENTS:]
    _save_json(path, doc)


def prune_trackers(state_dir, keep_dates, now_dt=None):
    """Finite local cache: a tracker whose outcome is built and whose session left the keep window is removed
    (its outcome text lives in the Kotlin outbox until ACKed). An UNFINISHED tracker is evicted only when it is
    TRACKER_EVICT_AFTER_EXPIRY_DAYS past its expiry; the eviction is recorded in the bounded coverage file."""
    removed = []
    try:
        names = os.listdir(state_dir)
    except OSError:
        return removed
    for fn in names:
        if not (fn.startswith("tracker_NF_") and fn.endswith(".json")):
            continue
        d = fn[len("tracker_NF_"):-5]
        if d in keep_dates:
            continue
        path = os.path.join(state_dir, fn)
        doc = _load_json(path)
        if doc is None:
            continue
        if doc.get("outcome"):
            os.remove(path)
            removed.append(fn)
        elif now_dt is not None and doc.get("expiry") and \
                (now_dt.date() - CAL._d(doc["expiry"])).days > TRACKER_EVICT_AFTER_EXPIRY_DAYS:
            os.remove(path)
            removed.append(fn)
            _coverage_event(state_dir, {"kind": "TRACKER_EVICTED_UNFINISHED", "session_date": doc.get("session_date"),
                                        "decision_id": doc.get("decision_id"),
                                        "awaiting": doc.get("awaiting_accepted_component"),
                                        "at": now_dt.isoformat()[:19]})
    return removed


def pending_envelopes(state_dir, session_dates):
    envs = []
    for d in session_dates[-MAX_PENDING_ENVELOPES:]:
        s = P.load_slot(state_dir, "NF", d)
        if s and s.get("envelope"):
            e = s["envelope"]
            envs.append({"decision_id": e["decision_id"], "envelope_sha256": e["envelope_sha256"],
                         "business_sha256": e["business_sha256"], "envelope_text": e["envelope_text"],
                         "session_date": d})
    return envs


def calibration_check(pub, now_dt, poll_ms, cal):
    """2.6.74 view-only: this poll's phone-validator result for the publication it received (bounded; display only,
    never used for the decision path, which validates again at COMPUTE)."""
    ver = pub.get("calibration_version") if isinstance(pub, dict) else None
    try:
        _k, st = CB.validate_publication(pub, now_dt=now_dt, cal=cal)
    except Exception:  # noqa: BLE001 - a malformed cache is a validation failure, never a poll failure
        st = "NO_CALIBRATION_INVALID"
    return {"calibration_version": ver[:96] if isinstance(ver, str) else None, "status": st, "poll_ms": poll_ms}


def run(ctx, result, explicit_mode, *, open_trades=None, closed_trades=None, cal=None, now_ms=None,
        deadline_factory=None):
    mode = experiment_mode(ctx, explicit_mode)
    if mode is None:
        return None
    cal = cal or CAL.DEFAULT_CALENDAR
    out = {"experiment_id": EXPERIMENT_ID, "source_label": SOURCE_LABEL, "mode": mode,
           "engine_version": M.ENGINE_VERSION, "policy_id": L.POLICY_ID}
    state_dir = ctx.get("evStateDir")
    poll_ms = now_ms if now_ms is not None else ctx.get("evPollMs")
    if not isinstance(state_dir, str) or not state_dir:
        out["status"] = "WAIT"
        out["wait_reason"] = "STATE_DIR_UNAVAILABLE"
        return out
    if not isinstance(poll_ms, (int, float)) or isinstance(poll_ms, bool):
        out["status"] = "WAIT"
        out["wait_reason"] = "POLL_TIMESTAMP_UNAVAILABLE"
        return out
    now_dt = datetime.fromtimestamp(poll_ms / 1000.0, CAL.IST)
    session_date = now_dt.date().isoformat()
    out["calibration_check"] = calibration_check(ctx.get("evCalibration"), now_dt, poll_ms, cal)
    try:
        is_session = cal.is_session(now_dt.date())
    except CAL.CalendarUnavailable:
        out["status"] = "WAIT"
        out["wait_reason"] = "CALENDAR_UNAVAILABLE"
        return out
    recent = [d.isoformat() for d in cal.sessions(now_dt.date() - timedelta(days=21), now_dt.date())]
    try:
        out["pending_outcomes"] = step_trackers(state_dir, ctx, poll_ms, cal, open_trades, closed_trades)
    except Exception as e:  # tracker evidence must never break the poll or the decision
        out["tracker_error"] = str(e)[:200]
    try:
        prune_trackers(state_dir, set(recent[-KEEP_SLOT_SESSIONS:]), now_dt)
    except OSError:
        pass
    if not is_session:
        out["status"] = "NOT_A_SESSION"
        return out
    expiry_cal = cal.nf_front_expiry(now_dt.date()).isoformat()
    expiry_ctx = ctx.get("nfExpiry")
    planned, ste = L.planned_exit(cal, now_dt.date(), expiry_cal)
    if planned is None:
        out["status"] = "NOT_SCHEDULED"
        out["sessions_to_expiry"] = ste
        out["pending_envelopes"] = pending_envelopes(state_dir, recent)
        return out
    in_window = _in_window(now_dt)
    action, slot = P.begin_or_resume(state_dir, "NF", session_date, in_window,
                                     {"engine": M.ENGINE_VERSION, "brain_app": ctx.get("appVersion")},
                                     now_dt.isoformat())
    if action == "EXPIRE":
        if not _after_window(now_dt):
            out["status"] = "BEFORE_WINDOW"
            out["pending_envelopes"] = pending_envelopes(state_dir, recent)
            return out
        prior = slot.get("state")
        status = "EXPIRED" if prior in ("NONE",) else "FAILED"
        detail = "NO_ATTEMPT_IN_WINDOW" if prior == "NONE" else ("RETRY_EXHAUSTED:" + str(slot.get("last_failure")
                                                                                      or "INTERRUPTED"))
        biz = {"schema_version": "ev_decision_business_v1", "experiment_id": EXPERIMENT_ID, "index": "NF",
               "session_date": session_date, "status": status, "failure": detail, "engine_version": M.ENGINE_VERSION,
               "policy_id": L.POLICY_ID, "attempts": slot.get("attempts", 0), "calendar_version": cal.version}
        env = P.build_envelope(biz, {"attempts": slot.get("attempts", 0)}, "ev12:NF:%s" % session_date)
        slot = P.finish(state_dir, slot, status, envelope=env, failure=detail, now_iso=now_dt.isoformat())
        action = "SLOT_CONSUMED"
    if action == "COMPUTE":
        if expiry_ctx != expiry_cal:
            status, biz, local, timings = "FAILED", {"schema_version": "ev_decision_business_v1",
                                                     "experiment_id": EXPERIMENT_ID, "index": "NF",
                                                     "session_date": session_date, "status": "FAILED",
                                                     "failure": "EXPIRY_CALENDAR_MISMATCH", "expiry_ctx": str(expiry_ctx)[:12],
                                                     "expiry_calendar": expiry_cal, "engine_version": M.ENGINE_VERSION,
                                                     "policy_id": L.POLICY_ID}, {}, {}
        else:
            kset, cst = CB.validate_publication(ctx.get("evCalibration"), now_dt=now_dt, cal=cal)
            cal_pub = ctx.get("evCalibration") if kset else None
            if kset is None:
                status, biz, local, timings = "FAILED", {"schema_version": "ev_decision_business_v1",
                                                         "experiment_id": EXPERIMENT_ID, "index": "NF",
                                                         "session_date": session_date, "status": "FAILED",
                                                         "failure": cst, "engine_version": M.ENGINE_VERSION,
                                                         "policy_id": L.POLICY_ID}, {}, {}
            else:
                dl = deadline_factory() if deadline_factory else None
                status, biz, local, timings = compute_decision(ctx, result, now_ms=poll_ms, session_date=session_date,
                                                               expiry=expiry_cal, ste=ste, planned=planned.isoformat(),
                                                               cal=cal, kset=kset, cal_pub=cal_pub, deadline=dl)
        biz["attempt"] = slot.get("attempts", 1)
        metrics = {k: P.fx(v, 1) if isinstance(v, float) else v for k, v in (timings or {}).items()}
        env = P.build_envelope(biz, metrics, "ev12:NF:%s" % session_date)
        retryable = status == "FAILED" and slot.get("attempts", 1) < 2
        if retryable:
            slot = P.finish(state_dir, slot, status, failure=biz.get("failure"), now_iso=now_dt.isoformat(),
                            retryable=True)
            out["status"] = "WAIT"
            out["wait_reason"] = biz.get("failure")
            out["attempt"] = slot.get("attempts")
            out["pending_envelopes"] = pending_envelopes(state_dir, recent)
            return out
        if status == "DECIDED":
            slot["acceptance_context"] = acceptance_context(env["decision_id"], biz, local)
        slot = P.finish(state_dir, slot, status, envelope=env, failure=biz.get("failure"), now_iso=now_dt.isoformat())
        if local.get("ref") is not None:
            doc = _init_trackers(state_dir, session_date, biz, local, poll_ms)
            doc["decision_id"] = env["decision_id"]
            _save_json(_tracker_path(state_dir, session_date), doc)
        out["timings"] = metrics
    P.prune_slots(state_dir, set(recent[-KEEP_SLOT_SESSIONS:]))
    out["recommendation"] = recommendation_view(slot, poll_ms, mode, state_dir)
    out["status"] = out["recommendation"]["status"]
    if action == "SLOT_CONSUMED":
        out["slot_consumed"] = True
    out["pending_envelopes"] = pending_envelopes(state_dir, recent)
    return out


# ---------------- bridge entry points (called from Kotlin on IO threads) ----------------
# Acceptance transaction: ev_v12_acceptance.{prepare,confirm,cancel,mark_persisted,mark_closed,recovery}_json.


def rule_b_close_economics_json(state_dir, decision_id, fill_json, entry_fill_json=None):
    """Net economics of a frozen Rule B fill. The frozen entry comes from the authoritative acceptance ledger;
    the trade's own entry_snapshot.entry_fill is used only when the ledger entry is absent (status says so)."""
    e = A.entry(state_dir, decision_id) if decision_id else None
    ef = ((e or {}).get("record") or {}).get("entry_fill")
    source = "ACCEPTANCE_LEDGER"
    if ef is None and entry_fill_json:
        ef = json.loads(entry_fill_json)
        source = "TRADE_ENTRY_SNAPSHOT"
    if ef is None:
        return json.dumps({"status": "UNKNOWN", "reason": "ENTRY_FILL_UNAVAILABLE"})
    try:
        econ = L.rule_b_close_economics(ef, json.loads(fill_json))
    except (KeyError, ValueError, TypeError, FEES.FeeUnavailable) as x:
        return json.dumps({"status": "UNKNOWN", "reason": str(x)[:80]})
    return json.dumps(dict(econ, status="OK", entry_source=source), default=float)


def rule_b_exit_step_json(position_json, tick_json):
    return json.dumps(L.rule_b_exit_step(json.loads(position_json), json.loads(tick_json)))
