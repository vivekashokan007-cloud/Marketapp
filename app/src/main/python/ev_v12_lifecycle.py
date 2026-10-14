"""paper_rule_b_v1 position lifecycle, acceptance revalidation, comparison trackers and the bounded
final outcome amendment (C5/C6). Pure functions; Kotlin mirrors rule_b_exit_step (RuleBExitPolicy.kt)
and both are pinned to the shared vectors app/src/test/resources/ev_v12/rule_b_vectors.json.

Rule B (frozen at actual entry):
  * planned exit = d0+2 sessions if dte>=3, d0+1 if dte==2; always before expiry (calendar).
  * exit fill = first tick in [15:15:00, 15:28:00) IST of the planned session with a valid
    executable quote on EVERY leg (short closes at ask, long closes at bid).
  * no valid tick by 15:28:00 -> LATE_EXIT_PENDING; retry each later session [09:20:00, 15:28:00)
    through the expiry-day 15:28:00 deadline; phone offline -> first valid allowed tick after resume.
  * never valid -> UNRESOLVED (no manufactured fill). Planned exits are before expiry; outage
    exceptions can carry into expiry day and are separately classified.
  * positions are unmanaged to the planned exit; TP/SL is informational only.
  * unknown policy id -> POLICY_UNKNOWN (fail closed; never falls into legacy intraday/EOD).
"""
from __future__ import annotations

import math
from datetime import datetime

import implied_ev_v12 as M
import ev_v12_engine as ENG
import ev_v12_fees as FEES
import ev_v12_calendar as CAL

POLICY_ID = "paper_rule_b_v1"
DECISION_VALIDITY_MS = 10 * 60 * 1000
MAX_OPEN_ENGINE_POSITIONS = 2
EXIT_WINDOW = ((15, 15, 0), (15, 28, 0))
LATE_WINDOW = ((9, 20, 0), (15, 28, 0))
MAX_LEG_QUOTE_AGE_MS = 120_000
TP_MULT, SL_MULT = 0.50, 0.60   # PositionPolicyV1 constants, informational comparison only

FAMILY_TYPE = {"IB": "IRON_BUTTERFLY", "IC": "IRON_CONDOR", "BP": "BULL_PUT", "BC": "BEAR_CALL",
               "BCD": "BULL_CALL", "BPD": "BEAR_PUT"}


def _secs(dt):
    return dt.hour * 3600 + dt.minute * 60 + dt.second


def _win(win):
    (h0, m0, s0), (h1, m1, s1) = win
    return h0 * 3600 + m0 * 60 + s0, h1 * 3600 + m1 * 60 + s1


def planned_exit(cal, d0, expiry):
    ste = cal.sessions_between(d0, expiry)
    if not (2 <= ste <= 4):
        return None, ste
    xs = cal.next_session(d0, 2 if ste >= 3 else 1)
    if not xs < CAL._d(expiry):
        return None, ste
    return xs, ste


def leg_quote_valid(q, now_ms):
    """ts_ms must be the SOURCE book time of the quote (R2): the native adapter never substitutes the receipt
    time. An adapter-declared inexact key / invalid source time is never valid."""
    if not isinstance(q, dict):
        return False
    if q.get("exact_key") is False or q.get("source_time_valid") is False:
        return False
    b, a = M._finite(q.get("bid")), M._finite(q.get("ask"))
    ts = M._finite(q.get("ts_ms"))
    if b is None or a is None or ts is None:
        return False
    if b <= 0 or a <= 0 or a < b:
        return False
    if ts > now_ms + 5_000 or now_ms - ts > MAX_LEG_QUOTE_AGE_MS:
        return False
    return True


def rule_b_exit_step(position, tick, cal=None):
    """position: {policy_id, state, planned_exit_session, expiry, legs:[{qty, instrument_key}]}
    tick: {ts_iso (aware), quotes:{instrument_key:{bid,ask,ts_ms}}}
    Returns {'state', 'action', 'fill'?, 'reason'}. Idempotent: terminal states never change."""
    cal = cal or CAL.DEFAULT_CALENDAR
    if position.get("policy_id") != POLICY_ID:
        return {"state": position.get("state"), "action": "NONE", "reason": "POLICY_UNKNOWN"}
    state = position.get("state") or "OPEN"
    if state in ("EXIT_FILLED", "UNRESOLVED"):
        return {"state": state, "action": "NONE", "reason": "TERMINAL"}
    ts = datetime.fromisoformat(tick["ts_iso"]).astimezone(CAL.IST)
    now_ms = ts.timestamp() * 1000.0
    day = ts.date()
    planned = CAL._d(position["planned_exit_session"])
    expiry = CAL._d(position["expiry"])
    t = _secs(ts)
    w0, w1 = _win(EXIT_WINDOW)
    l0, l1 = _win(LATE_WINDOW)
    try:
        session = cal.is_session(day)
    except CAL.CalendarUnavailable:
        return {"state": state, "action": "NONE", "reason": "CALENDAR_UNAVAILABLE"}
    if day > expiry or (day == expiry and t >= l1):
        return {"state": "UNRESOLVED", "action": "MARK_UNRESOLVED", "reason": "NO_VALID_QUOTE_BY_EXPIRY_DEADLINE"}
    if state == "OPEN":
        if day < planned:
            return {"state": "OPEN", "action": "NONE", "reason": "BEFORE_PLANNED_SESSION"}
        if day == planned and t < w0:
            return {"state": "OPEN", "action": "NONE", "reason": "BEFORE_EXIT_WINDOW"}
        if day == planned and t < w1:
            kind = "PLANNED_EXIT"
        else:
            state = "LATE_EXIT_PENDING"
            kind = None
    if state == "LATE_EXIT_PENDING":
        if day == planned or not session or not (l0 <= t < l1):
            return {"state": "LATE_EXIT_PENDING", "action": "MARK_LATE" if position.get("state") != "LATE_EXIT_PENDING" else "NONE",
                    "reason": "OUTSIDE_LATE_WINDOW"}
        kind = "LATE_EXIT"
    if not session:
        return {"state": state, "action": "NONE", "reason": "NOT_A_SESSION"}
    quotes = tick.get("quotes") or {}
    fill_legs = []
    for leg in position["legs"]:
        q = quotes.get(leg["instrument_key"])
        if not leg_quote_valid(q, now_ms):
            if kind == "PLANNED_EXIT":
                return {"state": "OPEN", "action": "NONE", "reason": "LEG_QUOTE_INVALID"}
            return {"state": "LATE_EXIT_PENDING", "action": "MARK_LATE" if position.get("state") != "LATE_EXIT_PENDING" else "NONE",
                    "reason": "LEG_QUOTE_INVALID"}
        px = float(q["ask"]) if leg["qty"] < 0 else float(q["bid"])
        fill_legs.append({"instrument_key": leg["instrument_key"], "qty": leg["qty"], "price": px,
                          "bid": float(q["bid"]), "ask": float(q["ask"]), "quote_ts_ms": float(q["ts_ms"])})
    close_value = sum(l["qty"] * l["price"] for l in fill_legs)  # value received to close (points/unit)
    return {"state": "EXIT_FILLED", "action": "FILL", "reason": kind,
            "fill": {"kind": kind, "ts_iso": ts.isoformat(), "legs": fill_legs, "close_value_pts": close_value}}


# ---------------------------------------------------------------------------------------------
# Acceptance revalidation (user tap; no automatic entry)
# ---------------------------------------------------------------------------------------------
def revalidate_acceptance(decision, accept_quotes, now_ms, *, open_engine_positions, capital_available,
                          cal=None):
    """decision: the frozen local decision (slot 'acceptance_context').
    accept_quotes: {instrument_key: {bid, ask, ts_ms}} fetched at acceptance.
    Returns a linked acceptance record; never manufactures a fill at old quotes."""
    cal = cal or CAL.DEFAULT_CALENDAR
    rec = {"decision_id": decision["decision_id"], "accepted_at_ms": int(now_ms), "policy_id": POLICY_ID}
    if now_ms > decision["valid_until_ms"]:
        rec["status"] = "EXPIRED"
        rec["reason"] = "DECISION_VALIDITY_ELAPSED"
        return rec
    legs = decision["legs"]
    acc = {}
    for leg in legs:
        q = accept_quotes.get(leg["instrument_key"])
        if not leg_quote_valid(q, now_ms):
            rec["status"] = "EXPIRED"
            rec["reason"] = "ACCEPTANCE_QUOTE_INVALID"
            return rec
        dec_exec = leg["ask"] if leg["qty"] > 0 else leg["bid"]
        new_exec = float(q["ask"]) if leg["qty"] > 0 else float(q["bid"])
        tol = M.TICK + 0.5 * (leg["ask"] - leg["bid"])
        if abs(new_exec - dec_exec) > tol + 1e-9:
            rec["status"] = "EXPIRED"
            rec["reason"] = "PRICE_OUTSIDE_TOLERANCE"
            rec["leg"] = leg["instrument_key"]
            return rec
        acc[leg["instrument_key"]] = (float(q["bid"]), float(q["ask"]), float(q["ts_ms"]))
    now_dt = datetime.fromtimestamp(now_ms / 1000.0, CAL.IST)
    planned = CAL._d(decision["planned_exit_session"])
    if not (now_dt.date() < planned):
        rec["status"] = "REJECTED"
        rec["reason"] = "NO_REMAINING_HORIZON"
        return rec
    same = [p for p in open_engine_positions if p.get("structure") == decision["structure"]
            and p.get("expiry") == decision["expiry"]]
    if same:
        rec["status"] = "REJECTED"
        rec["reason"] = "DUPLICATE_STRUCTURE_SAME_EXPIRY"
        return rec
    if len(open_engine_positions) >= MAX_OPEN_ENGINE_POSITIONS:
        rec["status"] = "REJECTED"
        rec["reason"] = "MAX_OPEN_ENGINE_POSITIONS"
        return rec
    # Rebuild a minimal reference with the ACCEPTANCE quotes for the legs and recheck EV/fees/risk.
    model = decision["model"]
    q = {}
    meta = {}
    for leg in legs:
        b, a, ts = acc[leg["instrument_key"]]
        key = (float(leg["strike"]), "CE" if leg["cp"] == 1 else "PE")
        q[key] = (b, a)
        meta[key] = {"instrument_key": leg["instrument_key"], "quote_ts_ms": ts}
    atoms = [(float(s), float(p)) for s, p in model["atoms"]]
    ref = {"q": q, "meta": meta, "F": float(model["F"]), "flags": {float(k): "F" for k in model["flags"]},
           "diag": {"screen_flags": [float(k) for k in model["screen_flags"]]}, "atoms": atoms,
           "lat": M.latent_bounds(atoms)}
    kset = [float(k) for k in decision["kset"]]
    # R1: the horizon is recomputed from the ACTUAL acceptance instant to the frozen planned exit (15:15 IST);
    # the decision-time tau is kept only as evidence.
    try:
        tau = float(cal.tau(now_dt, CAL.ist_dt(decision["planned_exit_session"], 15, 15, 0), decision["expiry"]))
    except CAL.CalendarUnavailable:
        rec["status"] = "REJECTED"
        rec["reason"] = "CALENDAR_UNAVAILABLE"
        return rec
    if not (0.0 < tau < 1.0):
        rec["status"] = "REJECTED"
        rec["reason"] = "NO_REMAINING_HORIZON"
        return rec
    rec["tau_at_acceptance"] = tau
    rec["decision_tau"] = float(decision["tau"])
    pts = {k: M.ptilde(ref["lat"], k, tau) for k in kset}
    sl = [(int(l["qty"]), float(l["strike"]), int(l["cp"])) for l in legs]
    r = ENG.evaluate_candidate(ref, decision["structure"], decision["family"], sl, index=decision["index"],
                               expiry=decision["expiry"], tau=tau, kset=kset, pts=pts,
                               contract_lot=int(decision["contract_lot"]), n_lots=int(decision["n_lots"]),
                               entry_date=now_dt.date().isoformat(), exit_date=decision["planned_exit_session"],
                               band_lim=float(model["band_lim"]))
    rec["ev_robust_at_acceptance"] = r.get("ev_robust")
    rec["risk_allowance_at_acceptance"] = r.get("risk_allowance")
    if r.get("status") != "OK":
        rec["status"] = "REJECTED"
        rec["reason"] = "REVALIDATION_" + r.get("status", "UNKNOWN")
        return rec
    if not r["ev_robust"] > 0:
        rec["status"] = "REJECTED"
        rec["reason"] = "NONPOSITIVE_EDGE_AT_ACCEPTANCE"
        return rec
    if capital_available is None or not (r["risk_allowance"] <= capital_available):
        rec["status"] = "REJECTED"
        rec["reason"] = "CAPITAL_INSUFFICIENT"
        return rec
    rec.update(status="ACCEPTED", reason=None,
               entry_fill={"legs": [{"instrument_key": l["instrument_key"], "qty": l["qty"], "strike": l["strike"],
                                     "cp": l["cp"], "price": (acc[l["instrument_key"]][1] if l["qty"] > 0 else acc[l["instrument_key"]][0]),
                                     "bid": acc[l["instrument_key"]][0], "ask": acc[l["instrument_key"]][1],
                                     "quote_ts_ms": acc[l["instrument_key"]][2]} for l in legs],
                           "entry_cash_pts": r["entry_cash_pts"], "entry_fee": r["entry_fee"],
                           "units": r["units"], "n_orders": len(legs),
                           "structural_loss_pts": r["structural_loss_pts"],
                           "structural_max_profit_pts": r.get("structural_max_profit_pts"),
                           "tau_at_acceptance": tau, "accepted_at_ms": int(now_ms),
                           "fee_identity": FEES.fee_identity(now_dt.date().isoformat())},
               frozen_policy={"policy_id": POLICY_ID, "decision_id": decision["decision_id"],
                              "planned_exit_session": decision["planned_exit_session"], "expiry": decision["expiry"],
                              "engine_version": decision["engine_version"], "model_version": decision["model_version"],
                              "calibration_version": decision["calibration_version"],
                              "fee_contract_version": FEES.FEE_CONTRACT_VERSION,
                              "lot_identity": decision["lot_identity"], "contract_lot": decision["contract_lot"],
                              "n_lots": decision["n_lots"], "source_quote_sha256": decision["source_quote_sha256"],
                              "structure": decision["structure"], "family": decision.get("family"),
                              "tau_at_acceptance": tau, "decision_tau": float(decision["tau"]),
                              "margin": "NOT_MEASURED_PAPER"})
    return rec


def rule_b_close_economics(entry_fill, fill):
    """R4: accepted-trade economics from the FROZEN entry fill and the frozen Rule B exit fill, with the same dated
    canonical fee contract (ev_v12_fees) used everywhere else. Returns gross/fees/net and fee identities (rupees).
    Raises FEES.FeeUnavailable / KeyError / ValueError when inputs are incomplete (caller records UNKNOWN)."""
    units = int(entry_fill["units"])
    entry_cash = float(entry_fill["entry_cash_pts"])
    entry_fee = float(entry_fill["entry_fee"])
    by_key = {l["instrument_key"]: l for l in entry_fill["legs"]}
    xl = fill["legs"]
    if sorted(l["instrument_key"] for l in xl) != sorted(by_key) or any(
            int(l["qty"]) != int(by_key[l["instrument_key"]]["qty"]) for l in xl):
        raise ValueError("EXIT_LEGS_DO_NOT_MATCH_FROZEN_ENTRY")
    close_value = sum(int(l["qty"]) * float(l["price"]) for l in xl)
    exit_day = datetime.fromisoformat(fill["ts_iso"]).astimezone(CAL.IST).date().isoformat()
    sell = sum(abs(int(l["qty"])) * float(l["price"]) for l in xl if int(l["qty"]) > 0)   # longs sold
    buy = sum(abs(int(l["qty"])) * float(l["price"]) for l in xl if int(l["qty"]) < 0)    # shorts bought back
    exit_fee = FEES.one_side_fee(sell, buy, len(xl), units, exit_day)
    gross = units * (entry_cash + close_value)
    return {"economics_version": "ev_v12_rule_b_close_economics_v1", "units": units,
            "entry_cash_pts": entry_cash, "close_value_pts": close_value, "gross_pnl": gross,
            "entry_fee": entry_fee, "exit_fee": exit_fee, "fees_total": entry_fee + exit_fee,
            "net_pnl": gross - entry_fee - exit_fee,
            "entry_fee_identity": entry_fill.get("fee_identity"), "exit_fee_identity": FEES.fee_identity(exit_day)}


# ---------------------------------------------------------------------------------------------
# Comparison trackers (poll resolution) and the bounded final outcome amendment
# ---------------------------------------------------------------------------------------------
def structure_mark(legs, chain_q, contract_units):
    """Executable close value (points/unit) of signed legs from a chain quote map, or None."""
    v = 0.0
    quotes = []
    for qty, K, cp in legs:
        ba = chain_q.get((float(K), "CE" if cp == 1 else "PE"))
        if ba is None or ba[0] <= 0 or ba[1] < ba[0]:
            return None, None
        px = ba[1] if qty < 0 else ba[0]
        v += qty * px
        quotes.append([round(ba[0], 2), round(ba[1], 2)])
    return v, quotes


def net_close_pnl(entry_cash_pts, close_value_pts, units, entry_fee, n_orders, legs_prices, exit_date):
    """Rupee net P&L for a close at executable prices. legs_prices: [(qty, price)]."""
    sell = sum(abs(q) * p for q, p in legs_prices if q > 0)   # longs sold
    buy = sum(abs(q) * p for q, p in legs_prices if q < 0)    # shorts bought back
    xfee = FEES.one_side_fee(sell, buy, n_orders, units, exit_date)
    gross = units * (entry_cash_pts + close_value_pts)
    return gross - entry_fee - xfee, gross, xfee


MANAGED_V7_POLICY = "HYPOTHETICAL_POSITION_POLICY_V1_GROSS_FIXED_THRESHOLDS"
ENGINE_TPSL_POLICY = "ENGINE_INFORMATIONAL_TP_SL_GROSS_FIXED_THRESHOLDS"
THRESHOLD_BASIS = ("fixed PositionPolicyV1 constants TP_MULT=0.50 / SL_MULT=0.60 on GROSS P&L vs structural gross "
                   "max profit/loss; published percentile thresholds of the live SHADOW path are NOT reproduced")
INTRADAY_TYPES = ("IRON_CONDOR", "IRON_BUTTERFLY")


def new_tracker(role, structure, legs, entry_cash_pts, units, entry_fee, max_profit_pts, max_loss_pts,
                managed_policy, managed_mode=None, entry_quotes=None, tpsl=True):
    """managed_policy: None (Rule B only), ENGINE_TPSL_POLICY or MANAGED_V7_POLICY. managed_mode: 'intraday'
    (EOD 15:15 on the decision session applies, as the live PWA stamps IC/IB) or 'UNVERIFIED_TRADE_MODE'
    (no EOD is applied; the comparator's actual mode at decision time is unknown)."""
    return {"role": role, "structure": structure, "legs": legs, "entry_cash_pts": entry_cash_pts,
            "units": units, "entry_fee": entry_fee, "max_profit_pts": max_profit_pts,
            "max_loss_pts": max_loss_pts, "managed_policy": managed_policy, "managed_mode": managed_mode,
            "entry_quotes": entry_quotes, "tpsl": bool(tpsl),
            "rule_b": None, "tp_first": None, "sl_first": None, "managed_exit": None, "polls_seen": 0,
            "polls_quoted": 0}


def tracker_step(tr, chain_q, ts_dt, planned_exit_session, expiry, decision_session):
    """One poll. First-hit evidence keeps the executable leg quotes, gross, net and fees so P&L is reproducible.
    TP/SL use GROSS P&L like the live PositionPolicyV1 SHADOW reference."""
    if tr.get("final"):
        return tr
    mark, quotes = structure_mark(tr["legs"], chain_q, tr["units"])
    tr["polls_seen"] += 1
    if mark is None:
        return tr
    tr["polls_quoted"] = tr.get("polls_quoted", 0) + 1
    day = ts_dt.date().isoformat()
    lp = [(q, (ba[1] if q < 0 else ba[0])) for (q, K, cp), ba in zip(tr["legs"], quotes)]
    try:
        net, gross, xfee = net_close_pnl(tr["entry_cash_pts"], mark, tr["units"], tr["entry_fee"], len(tr["legs"]), lp, day)
    except FEES.FeeUnavailable:
        return tr
    ev = {"ts": ts_dt.isoformat(), "mark_pts": round(mark, 2), "net": round(net, 2), "gross": round(gross, 2),
          "exit_fee": round(xfee, 2), "quotes": quotes}
    if tr.get("tpsl", True):
        tp = TP_MULT * tr["max_profit_pts"] * tr["units"] if tr["max_profit_pts"] is not None else None
        sl = -SL_MULT * tr["max_loss_pts"] * tr["units"] if tr["max_loss_pts"] is not None else None
        if tr["tp_first"] is None and tp is not None and gross >= tp:
            tr["tp_first"] = ev
        if tr["sl_first"] is None and sl is not None and gross <= sl:
            tr["sl_first"] = ev
    hhmmss = _secs(ts_dt.astimezone(CAL.IST))
    w0, w1 = _win(EXIT_WINDOW)
    if tr["managed_policy"] == MANAGED_V7_POLICY and tr["managed_exit"] is None:
        if tr["tp_first"] is not None:
            tr["managed_exit"] = dict(tr["tp_first"], kind="TP")
        elif tr["sl_first"] is not None:
            tr["managed_exit"] = dict(tr["sl_first"], kind="SL")
        elif tr.get("managed_mode") == "intraday" and day == decision_session and hhmmss >= w0:
            tr["managed_exit"] = dict(ev, kind="EOD")
    if tr["rule_b"] is None and day == planned_exit_session and w0 <= hhmmss < w1:
        tr["rule_b"] = dict(ev, kind="PLANNED_EXIT")
    return tr


def finalize_trackers(trackers, now_dt, expiry):
    """After the expiry-day deadline everything unresolved is labelled MISSING (never zero P&L)."""
    for tr in trackers:
        if tr.get("final"):
            continue
        if tr["rule_b"] is None:
            tr["rule_b_status"] = "MISSING_EXIT_POLL" if tr.get("polls_quoted") or not tr.get("polls_seen") \
                else "QUOTES_UNAVAILABLE"
        else:
            tr["rule_b_status"] = "COMPLETE"
        tr["final"] = True
    return trackers


def g4_labels(g4, F_exit):
    if F_exit is None:
        return None
    return [1 if F_exit <= float(e["threshold"]) else 0 for e in g4]
