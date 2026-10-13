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

EXPERIMENT_ID = "nf_paper_implied_ev_v1_2"
SOURCE_LABEL = "IMPLIED_EV_V12_EXPERIMENT"
WINDOW = ((12, 30, 0), (12, 40, 0))
ENGINE_DEADLINE_MS = 2000.0
MODES = ("off", "observe", "active")
TOP_OK = 8
MAX_REJECTED_SAMPLE = 9
KEEP_SLOT_SESSIONS = 10
MAX_PENDING_ENVELOPES = 3


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
    if not res.get("resolved") or not res.get("contract_lot_size") or res.get("lot_conflict"):
        return None, {"status": "LOT_AUTHORITY_UNRESOLVED", "reason": res.get("unavailable_reason")}
    return int(res["contract_lot_size"]), {"status": "OK", "contract_lot_size": int(res["contract_lot_size"]),
                                            "lot_table_version": res.get("lot_table_version"),
                                            "matched_rule_id": res.get("matched_rule_id"),
                                            "lot_source": res.get("lot_source")}


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
    for d in details:
        if d["name"] in es:
            d["es5"] = P.fx(es[d["name"]], 2)
    business.update(counts_by_status=by_status, ok_by_family=by_family, n_universe=len(records),
                    candidates=details, v7_comparator=v7, es_status=es_status, g4_version=ENG.G4_VERSION,
                    g4=[{"c": P.fx(x["c"], 2), "threshold": P.fx(x["threshold"], 2), "p_engine": P.fx(x["p_engine"], 6),
                         "p_k1": P.fx(x["p_k1"], 6), "inversion": x["inversion"]} for x in g4],
                    g4_k_engine=P.fx(k_central, 6))
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
    local = {"ref": ref, "pick": pick, "v7": v7_rec, "lot": lot, "tau": tau, "kset": kset, "band_lim": band_lim}
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


def recommendation_view(slot, now_ms, mode):
    """What the PWA may show. Only an unexpired, unconsumed, active-mode DECIDED pick is actionable."""
    env = slot.get("envelope") or {}
    try:
        biz = json.loads(env.get("envelope_text") or "{}").get("business") or {}
    except ValueError:
        biz = {}
    view = {"source_label": SOURCE_LABEL, "slot_state": slot.get("state"), "session_date": slot.get("session_date"),
            "decision_id": env.get("decision_id"), "status": biz.get("status") or slot.get("state"),
            "failure": biz.get("failure"), "mode": mode}
    pick = biz.get("pick")
    acc = slot.get("acceptance") or {}
    if pick:
        valid_until = pick.get("valid_until_ms")
        view["pick"] = {"structure": pick["name"], "family": pick["family"], "type": L.FAMILY_TYPE.get(pick["family"]),
                        "legs": pick["legs"], "ev_robust_per_lot": pick.get("ev_robust"),
                        "risk_allowance": pick.get("risk"), "entry_cash_pts": pick.get("entry_cash"),
                        "planned_exit": "%s 15:15 IST (Rule B)" % biz.get("planned_exit_session"),
                        "planned_exit_session": biz.get("planned_exit_session"), "expiry": biz.get("expiry"),
                        "calibration_version": biz.get("calibration_version"), "model_version": biz.get("model_version"),
                        "valid_until_ms": valid_until}
        actionable = (mode == "active" and not acc and valid_until is not None and now_ms <= valid_until)
        view["actionable"] = bool(actionable)
        view["expired"] = bool(valid_until is not None and now_ms > valid_until)
        view["acceptance_status"] = acc.get("status")
    else:
        view["actionable"] = False
    if biz.get("counts_by_status"):
        view["counts_by_status"] = biz["counts_by_status"]
    return view


def _tracker_path(state_dir, session_date):
    return os.path.join(state_dir, "tracker_NF_%s.json" % session_date)


def _init_trackers(state_dir, session_date, business, local, now_ms):
    trs = []
    units = local.get("lot")
    pick = local.get("pick")
    if pick is not None:
        trs.append(L.new_tracker("ENGINE_PICK", pick["name"], pick["legs"], pick["entry_cash_pts"], pick["units"],
                                 pick["entry_fee"], pick.get("structural_max_profit_pts"), pick["structural_loss_pts"],
                                 "INFORMATIONAL_TP_SL"))
    v7 = local.get("v7")
    if v7 is not None and v7.get("status") in ("OK", "RISK_BUDGET_EXCEEDED") and "entry_fee" in v7:
        trs.append(L.new_tracker("V7_COMPARATOR", v7["name"], v7["legs"], v7["entry_cash_pts"], v7["units"],
                                 v7["entry_fee"], v7.get("structural_max_profit_pts"), v7["structural_loss_pts"],
                                 "POSITION_POLICY_V1_INTRADAY"))
    doc = {"session_date": session_date, "expiry": business["expiry"],
           "planned_exit_session": business["planned_exit_session"], "decision_id": None,
           "trackers": trs, "created_ms": int(now_ms), "g4": business.get("g4"), "F_exit": None, "outcome": None}
    return doc


def _save_json(path, obj):
    P.atomic_write(path, json.dumps(obj, sort_keys=True, default=lambda o: list(o) if isinstance(o, tuple) else str(o)))


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _accepted_outcome(decision_id, open_trades, closed_trades):
    def did(t):
        es = t.get("entry_snapshot") if isinstance(t, dict) else None
        if isinstance(es, str):
            try:
                es = json.loads(es)
            except ValueError:
                es = None
        return (es or {}).get("ev_decision_id") if isinstance(es, dict) else None
    for t in closed_trades or []:
        if isinstance(t, dict) and did(t) == decision_id:
            reason = str(t.get("exit_reason") or "")
            kind = ("RULE_B_" + reason.split("RULE_B_")[-1]) if "RULE_B_" in reason else "MANUAL_EARLY_CLOSE"
            return {"status": "CLOSED", "kind": kind, "net_pnl": P.fx(t.get("net_pnl"), 2) if t.get("net_pnl") is not None else None,
                    "trade_id": str(t.get("id") or t.get("trade_id") or "")[:64]}
    for t in open_trades or []:
        if isinstance(t, dict) and did(t) == decision_id:
            return {"status": "OPEN", "trade_id": str(t.get("id") or t.get("trade_id") or "")[:64]}
    return {"status": "NOT_ACCEPTED"}


def _tracker_summary(tr):
    def ev(e):
        if not e:
            return None
        return {"ts": e["ts"][:19], "net": P.fx(e["net"], 2), "fee": P.fx(e["exit_fee"], 2),
                "q": [[P.fx(b, 2), P.fx(a, 2)] for b, a in e["quotes"]], "kind": e.get("kind")}
    return {"role": tr["role"], "structure": str(tr["structure"])[:60], "rule_b": ev(tr["rule_b"]),
            "rule_b_status": tr.get("rule_b_status"), "tp_first": ev(tr["tp_first"]), "sl_first": ev(tr["sl_first"]),
            "managed_exit": ev(tr["managed_exit"]), "polls_seen": tr["polls_seen"],
            "path_resolution": "brain_poll_5min"}


def build_outcome(doc, accepted, now_ms):
    body = {"schema": P.OUTCOME_SCHEMA, "decision_id": doc["decision_id"], "index": "NF",
            "session_date": doc["session_date"], "expiry": doc["expiry"],
            "planned_exit_session": doc["planned_exit_session"], "policy_id": L.POLICY_ID,
            "outcome_time_ms": int(now_ms), "components": [_tracker_summary(t) for t in doc["trackers"]],
            "accepted": accepted, "F_exit": P.fx(doc.get("F_exit"), 2),
            "g4_labels": L.g4_labels(doc.get("g4") or [], doc.get("F_exit")) if doc.get("g4") else None}
    text = P.canon(body)
    if len(text.encode("utf-8")) > P.MAX_OUTCOME_BYTES:
        for c in body["components"]:
            for k in ("tp_first", "sl_first"):
                if c.get(k):
                    c[k].pop("q", None)
        body["truncated"] = "QUOTES_DROPPED_FROM_TP_SL_EVIDENCE"
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
                L.tracker_step(tr, q, now_dt, doc["planned_exit_session"], doc["expiry"], doc["session_date"])
            secs = now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second
            if now_dt.date().isoformat() == doc["planned_exit_session"] and 15 * 3600 + 15 * 60 <= secs < 15 * 3600 + 28 * 60 \
                    and doc.get("F_exit") is None:
                Fx, _f, _s = M.parity_forward(q)
                doc["F_exit"] = Fx
        deadline = CAL.ist_dt(doc["expiry"], 15, 28, 0)
        if now_dt >= deadline and doc.get("decision_id"):
            L.finalize_trackers(doc["trackers"], now_dt, doc["expiry"])
            accepted = _accepted_outcome(doc["decision_id"], open_trades, closed_trades)
            doc["outcome"] = build_outcome(doc, accepted, now_ms)
            out.append(doc["outcome"])
        _save_json(path, doc)
    return out


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
    out["recommendation"] = recommendation_view(slot, poll_ms, mode)
    out["status"] = out["recommendation"]["status"]
    if action == "SLOT_CONSUMED":
        out["slot_consumed"] = True
    out["pending_envelopes"] = pending_envelopes(state_dir, recent)
    return out


# ---------------- bridge entry points (called from Kotlin on IO threads) ----------------
def revalidate_acceptance_json(state_dir, session_date, quotes_json, now_ms, open_engine_json, capital_available):
    slot = P.load_slot(state_dir, "NF", session_date)
    if not slot or not slot.get("acceptance_context"):
        return json.dumps({"status": "REJECTED", "reason": "NO_DECIDED_SLOT"})
    if slot.get("acceptance"):
        return json.dumps(dict(slot["acceptance"], replay=True))
    ctx = slot["acceptance_context"]
    rec = L.revalidate_acceptance(ctx, json.loads(quotes_json or "{}"), float(now_ms),
                                  open_engine_positions=json.loads(open_engine_json or "[]"),
                                  capital_available=capital_available)
    # Linked acceptance record; the original decision/envelope is never modified. Only a successful
    # acceptance consumes the decision; a rejected tap (stale quote, price moved) may be retried
    # while the decision is still valid.
    if rec.get("status") == "ACCEPTED":
        slot["acceptance"] = {k: rec[k] for k in rec if k in ("status", "reason", "accepted_at_ms", "decision_id")}
        slot["acceptance_record"] = rec
        P.save_slot(state_dir, slot)
    return json.dumps(rec, default=float)


def rule_b_exit_step_json(position_json, tick_json):
    return json.dumps(L.rule_b_exit_step(json.loads(position_json), json.loads(tick_json)))
