"""PM1-3: per-decision diagnostic ('why', schema mr.decision_why.v1) attached write-once to NEW retained
authority observations.  Pure, bounded, never raises.

Sampling contract (Codex PM1 s5): a 'why' exists only for a decision that created a NEW retained authority
history record (source-poll deduplication unchanged).  A screen refresh that produces no new record (for
example the same poll re-analysed after a toggle change) gets no 'why'; uncaptured refreshes are NOT zero
blocks.  decision_id and observation semantics are unchanged; no observation is added to carry a 'why'.

Enablement (Codex PM1 s3): 'en' records the ACTUAL singleLegPaperEnabled preference supplied in ctx and the
explicit execution mode, separately from whether this decision's policy permits a single-leg structure
('sl_pol') and whether a plan was computed ('sl_plan').  'sl_state' distinguishes DISABLED_BY_USER,
NOT_PAPER_MODE, DATA_UNAVAILABLE, NO_DIRECTIONAL_PROPOSAL, PLAN_NOT_COMPUTED, BLOCKED_BY_EXECUTION_EVIDENCE
and ENTRY_CAPABLE (ADMISSIBLE or ELIGIBLE_PENDING_SNAPSHOT; the latter still needs a fresh quote and the
final admission rechecks).  No proposal is manufactured when the policy produces none.

Size (Codex PM1 s4): canonical UTF-8 JSON (the authority store's _canon) <= WHY_MAX_BYTES, by a fixed reduction
ladder ending in a minimal valid fallback.  Any reduction sets trunc=True; '*_n' fields keep the original
counts so a truncated tally is never presented as complete.
"""
from __future__ import annotations

import json

WHY_SCHEMA = 'mr.decision_why.v1'
WHY_MAX_BYTES = 600
ENTRY_CAPABLE = ('ADMISSIBLE', 'ELIGIBLE_PENDING_SNAPSHOT')
SINGLE_LEG_ACTIONS = ('BUY_CE', 'BUY_PE', 'SELL_CE', 'SELL_PE')
SAMPLING = 'NEW_RETAINED_AUTHORITY_OBSERVATIONS_ONLY'


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), default=str)


def size(obj):
    return len(_canon(obj).encode('utf-8'))


def _s(v, n):
    if v is None:
        return None
    v = v if isinstance(v, str) else str(v)
    return v[:n]


def _num(v):
    try:
        f = float(v)
        return f if f == f and abs(f) != float('inf') else None
    except Exception:
        return None


def _int(v):
    try:
        if isinstance(v, bool):
            return None
        return int(v)
    except Exception:
        return None


def enablement(ctx, mode):
    raw = (ctx or {}).get('singleLegPaperEnabled') if isinstance(ctx, dict) else None
    pref = None if raw is None else bool(raw)
    m = mode if mode in ('paper', 'sandbox', 'live') else None
    return {'pref': pref, 'mode': m, 'eff': bool(pref) and m == 'paper'}


def sl_state(en, dec, policy_permits, plan):
    if not en.get('pref'):
        return 'DISABLED_BY_USER'
    if en.get('mode') != 'paper':
        return 'NOT_PAPER_MODE'
    if (dec or {}).get('availability') != 'AVAILABLE':
        return 'DATA_UNAVAILABLE'
    if not policy_permits:
        return 'NO_DIRECTIONAL_PROPOSAL'
    if not isinstance(plan, dict):
        return 'PLAN_NOT_COMPUTED'
    if plan.get('status') in ENTRY_CAPABLE:
        return 'ENTRY_CAPABLE'
    return 'BLOCKED_BY_EXECUTION_EVIDENCE'


def build(ix, dec, cands, managed_ix, plan, ctx, mode):
    """Full (unbounded) diagnostic.  Callers use build_bounded."""
    dec = dec if isinstance(dec, dict) else {}
    act = dec.get('action') or {}
    allowed = (dec.get('structure_policy') or {}).get('allowed') or []
    pol = any(a in SINGLE_LEG_ACTIONS for a in allowed)
    en = enablement(ctx, mode)
    ar = [str(r) for r in (act.get('reasons') or [])]
    w = {'v': WHY_SCHEMA, 'act': _s(act.get('value'), 24), 'act_r': ar, 'act_n': len(ar),
         'exp': _s((ctx or {}).get('nfExpiry' if ix == 'NF' else 'bnfExpiry'), 10),
         'dte': _int((ctx or {}).get('nfDTE' if ix == 'NF' else 'bnfDTE')),
         'en': en, 'sl_pol': pol, 'sl_plan': isinstance(plan, dict),
         'sl_state': sl_state(en, dec, pol, plan), 'sl': None, 'ml': None}
    if isinstance(plan, dict):
        q = plan.get('entry_quote') or {}
        pr = [str(r) for r in (plan.get('reasons') or [])]
        w['sl'] = {'st': _s(plan.get('status'), 28), 'a': _s(plan.get('action'), 8), 'k': _num(plan.get('strike')),
                   'ot': _s(plan.get('option_type'), 2), 'r': pr, 'r_n': len(pr), 'rs': _num(plan.get('rel_spread')),
                   'qb': _s(q.get('basis'), 24) or 'NONE', 'qa': _num(q.get('age_s'))}
    if cands:
        gates = (managed_ix or {}).get('gates') or []
        g_by = {g.get('candidate_id'): g for g in gates if isinstance(g, dict)}
        sel_id = ((managed_ix or {}).get('selected') or {}).get('candidate_id')
        by = {}
        for c in cands:
            t = str(c.get('type'))
            g = g_by.get(c.get('id'))
            if c.get('entryEligible') and g is not None and g.get('admit'):
                by[t] = 'OK'
            elif not c.get('entryEligible'):
                rs = ((c.get('entryEligibility') or {}).get('reasons') or ['NOT_ELIGIBLE'])
                by[t] = str(rs[0])
            else:
                by[t] = str(((g or {}).get('reasons') or ['GATE_NOT_EVALUATED'])[0])
        w['ml'] = {'n': len(cands), 'el': sum(1 for c in cands if c.get('entryEligible')),
                   'ad': sum(1 for g in gates if isinstance(g, dict) and g.get('admit')),
                   'sel': next((str(c.get('type')) for c in cands if sel_id and c.get('id') == sel_id), None),
                   'r': _s((managed_ix or {}).get('reason'), 40), 'by': by, 'by_n': len(by)}
    return w


def _shape(w, nlist, nstr, by_n, keep_sl_r=True, keep_act_r=True):
    o = dict(w)
    o['act_r'] = [_s(r, nstr) for r in (w.get('act_r') or [])[:nlist]] if keep_act_r else []
    if isinstance(w.get('sl'), dict):
        sl = dict(w['sl'])
        sl['r'] = [_s(r, nstr) for r in (sl.get('r') or [])[:nlist]] if keep_sl_r else []
        sl['qb'] = _s(sl.get('qb'), nstr)
        o['sl'] = sl
    if isinstance(w.get('ml'), dict):
        ml = dict(w['ml'])
        items = sorted((ml.get('by') or {}).items())[:by_n]
        ml['by'] = {_s(k, nstr): _s(v, nstr) for k, v in items}
        ml['r'] = _s(ml.get('r'), nstr)
        o['ml'] = ml
    return o


# deterministic reduction ladder: (max list items, max string chars, max per-structure entries, sl.r, act_r)
LADDER = ((8, 64, 8, True, True), (8, 64, 4, True, True), (4, 64, 4, True, True), (4, 32, 4, True, True),
          (2, 32, 2, True, True), (2, 16, 2, True, True), (1, 16, 1, True, True), (1, 16, 0, False, True),
          (1, 12, 0, False, False))


def _lossless(w, o):
    return _canon(o) == _canon(w)


def bound(w):
    """Return a dict whose canonical UTF-8 size is <= WHY_MAX_BYTES.  Idempotent on bounded input."""
    try:
        if not isinstance(w, dict):
            return fallback(None, 'NOT_A_DICT')
        if size(w) <= WHY_MAX_BYTES and w.get('v') == WHY_SCHEMA and all(
                isinstance(x, str) and len(x) <= 64 for x in (w.get('act_r') or [])):
            return w
        for step in LADDER:
            o = _shape(w, *step)
            if not _lossless(w, o):
                o['trunc'] = True
            if size(o) <= WHY_MAX_BYTES:
                return o
        return fallback(w, 'SIZE')
    except Exception as exc:
        return fallback(None, 'BOUND_ERROR:' + type(exc).__name__)


def fallback(w, why):
    w = w if isinstance(w, dict) else {}
    en = w.get('en') if isinstance(w.get('en'), dict) else {}
    o = {'v': WHY_SCHEMA, 'act': _s(w.get('act'), 24), 'en': {'pref': en.get('pref') if isinstance(en.get('pref'), bool) else None,
                                                              'mode': _s(en.get('mode'), 8), 'eff': bool(en.get('eff'))},
         'sl_state': _s(w.get('sl_state'), 32), 'trunc': True, 'omitted': _s(why, 40)}
    if size(o) > WHY_MAX_BYTES:   # cannot happen with the bounds above; kept as a hard guarantee
        o = {'v': WHY_SCHEMA, 'trunc': True, 'omitted': 'SIZE'}
    return o


def build_bounded(ix, dec, cands, managed_ix, plan, ctx, mode):
    """Never raises; a construction error becomes a small valid record (live analysis is never affected)."""
    try:
        return bound(build(ix, dec, cands, managed_ix, plan, ctx, mode))
    except Exception as exc:
        try:
            a = _s(((dec or {}).get('action') or {}).get('value'), 24)
        except Exception:
            a = None
        return {'v': WHY_SCHEMA, 'act': a, 'error': type(exc).__name__[:40], 'trunc': True}
