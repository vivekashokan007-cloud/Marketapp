"""Bounded Paper decision-menu evidence (Item B) and normalized margin evidence (Item C).

Evidence only. Nothing here is read by ranking, entry gates, sizing, exits or primary selection; the
menu is built AFTER the live ranking from the same ``ranked`` population the Paper primary
(watchlist[0]) is selected from, and the margin evidence is attached after the existing broker
margin enrichment.

Capture policy (HANDOFF_R2 decision 4): scheduled target 12:30 IST; the first successfully completed
scheduled Brain decision slot in the inclusive 12:30-12:55 window is captured, independent of verdict
or P&L. One menu per session (menu_id = f(session_date, policy)); retries reuse that frozen menu.
Control (decision 2): one uniformly sampled eligible peer, same index + exact expiry + strategy family,
excluding the primary, eligibility judged only on information present at the frozen decision.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date

MENU_SCHEMA_VERSION = 'paper_decision_menu_v1'
CAPTURE_POLICY_VERSION = 'paper_menu_capture_v1_first_completed_1230_1255_ist'
CONTROL_SAMPLER_VERSION = 'uniform_peer_sha256_mod_v1'
MARGIN_EVIDENCE_VERSION = 'margin_evidence_v1'
STORAGE_STRATEGY = 'FULL_MENU_SINGLE_SNAPSHOT_QUOTE_REFS'
WINDOW_START_HHMM = (12, 30)
WINDOW_END_HHMM = (12, 55)
MARGIN_SCOPE = 'STANDALONE_CANDIDATE_ALL_LEGS_1_LOT_PRODUCT_D'
BROKER_MARGIN_SOURCE = 'UPSTOX_MARGIN_API'
VALID_FAMILIES = ('IRON_BUTTERFLY', 'IRON_CONDOR', 'BEAR_CALL', 'BULL_PUT', 'BULL_CALL', 'BEAR_PUT')


def menu_id_for(session_date: str, policy: str = CAPTURE_POLICY_VERSION) -> str:
    return 'pdm_' + hashlib.sha256(f'{session_date}|{policy}'.encode()).hexdigest()[:24]


def in_capture_window(hh: int, mm: int) -> bool:
    return WINDOW_START_HHMM <= (hh, mm) <= WINDOW_END_HHMM


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float('inf'), float('-inf')) else None


def _lot_authority(index, session_date, expiry, resolver=None):
    if resolver is None:
        try:
            from contract_lot_table import resolve_contract_lot, lot_table_version_id
        except Exception as e:  # pragma: no cover - module always bundled
            return {'resolved': False, 'reason': f'lot_table_import_error:{e}'}
        try:
            res = resolve_contract_lot(index, as_of=session_date, expiry=expiry, expiry_cycle=None,
                                       number_of_lots=1, captured_contract_lot=None)
        except Exception as e:
            return {'resolved': False, 'reason': f'lot_resolver_error:{e}'}
        out = {'resolved': bool(res.get('resolved') and res.get('contract_lot_size')),
               'lot': res.get('contract_lot_size'), 'rule_id': res.get('matched_rule_id'),
               'table_version': lot_table_version_id(), 'reason': res.get('unavailable_reason')}
        return out
    return resolver(index, session_date, expiry)


def _leg_rows(cand, lot, chain_poll_ts):
    rows, problems = [], []
    legs = cand.get('legs') if isinstance(cand.get('legs'), list) else []
    if not legs:
        problems.append('legs_missing')
    for i, leg in enumerate(legs):
        if not isinstance(leg, dict):
            problems.append(f'leg{i}_not_object')
            continue
        strike = _f(leg.get('strike'))
        opt = str(leg.get('option_type') or leg.get('type') or '').upper()
        action = str(leg.get('action') or '').upper()
        bid, ask = _f(leg.get('bid')), _f(leg.get('ask'))
        if strike is None or opt not in ('CE', 'PE') or action not in ('BUY', 'SELL'):
            problems.append(f'leg{i}_identity_incomplete')
        if leg.get('expiry') and cand.get('expiry') and str(leg.get('expiry')) != str(cand.get('expiry')):
            problems.append(f'leg{i}_expiry_mismatch')
        if bid is None or ask is None:
            qstat = 'MISSING_BID_ASK'
        elif bid <= 0 or ask <= 0 or ask < bid:
            qstat = 'INVALID_BID_ASK'
        else:
            qstat = 'VALID'
        if qstat != 'VALID':
            problems.append(f'leg{i}_{qstat.lower()}')
        rows.append({
            'leg_index': i, 'index': cand.get('index'), 'expiry': leg.get('expiry') or cand.get('expiry'),
            'strike': strike, 'option_type': opt, 'action': action,
            'quantity': lot if isinstance(lot, int) and lot > 0 else None, 'lots': 1,
            'instrument_key': leg.get('instrument_key'),
            'entry_bid': bid, 'entry_ask': ask, 'entry_ltp': _f(leg.get('ltp')), 'quote_validation': qstat,
            # Reference into ml_option_chain_snapshots (unique: poll_ts, index_key, strike, option_type).
            'quote_ref': {'table': 'ml_option_chain_snapshots', 'poll_ts': chain_poll_ts,
                          'index_key': cand.get('index'), 'strike': strike, 'option_type': opt}
            if chain_poll_ts else None,
        })
    return rows, problems


def _gate_reasons(c):
    reasons = []
    ee = c.get('entryEligibility') if isinstance(c.get('entryEligibility'), dict) else {}
    for k in ('reasons', 'block_reasons', 'pc2_quality_failure_stages'):
        v = ee.get(k)
        if isinstance(v, list):
            reasons += [str(x) for x in v]
    for g in c.get('pc2_gate_basis') or []:
        if isinstance(g, dict) and g.get('passed') is False and g.get('gate_name'):
            reasons.append('pc2_gate:' + str(g.get('gate_name')))
    for k in ('executionGateReason', 'entryBlockReason', 'blockReason'):
        if c.get(k):
            reasons.append(f'{k}:{c.get(k)}')
    return sorted(set(reasons))


def build_paper_decision_menu(ranked, result, ctx, request, lot_resolver=None):
    """Return {'manifest': {...}, 'candidates': [...]} for the frozen decision population.

    ``ranked`` is the final ranked population the watchlist/primary were selected from. ``None``
    means the population was not produced (distinct from an empty generated menu)."""
    request = request if isinstance(request, dict) else {}
    session_date = str(request.get('session_date') or ctx.get('today_ist') or '')
    menu_id = request.get('menu_id') or menu_id_for(session_date)
    chain_ref = ctx.get('chain_evidence_ref') if isinstance(ctx.get('chain_evidence_ref'), dict) else {}
    chain_poll_ts = chain_ref.get('chain_poll_ts') if chain_ref.get('saved') else None
    watchlist = [c for c in (result.get('watchlist') or []) if isinstance(c, dict)]
    watch_rank = {}
    for i, c in enumerate(watchlist, start=1):
        watch_rank.setdefault(c.get('id'), i)
    primary_id = watchlist[0].get('id') if watchlist else None
    verdict = result.get('verdict') if isinstance(result.get('verdict'), dict) else {}

    manifest = {
        'menu_id': menu_id, 'session_date': session_date,
        'menu_schema_version': MENU_SCHEMA_VERSION, 'capture_policy_version': CAPTURE_POLICY_VERSION,
        'brain_version': result.get('brain_version'), 'app_version': ctx.get('app_version') or ctx.get('appVersion'),
        'slot_key': request.get('slot_key'), 'slot_scheduled_ist': request.get('slot_scheduled_ist'),
        'dispatch_at': request.get('dispatch_at'), 'attempt_no': request.get('attempt_no'),
        'decision_poll_t': (ctx.get('live') or {}).get('t') if isinstance(ctx.get('live'), dict) else None,
        'chain_poll_ts': chain_poll_ts, 'chain_rows_saved': chain_ref.get('saved'),
        'spot_provenance': ctx.get('spot_provenance'),
        'storage_strategy': STORAGE_STRATEGY, 'truncation_reason': None,
        'verdict_action': verdict.get('action'), 'decision_source': result.get('decisionSource'),
        'primary_candidate_id': primary_id,
        'build3_flow_counts': {k: (result.get('build3_flow') or {}).get(k) for k in
                               ('candidate_total_built', 'rejected_total', 'after_lane_gate_count', 'ranked_before_persistence',
                                'generated_persisted')}
        if isinstance(result.get('build3_flow'), dict) else None,
    }
    if ranked is None:
        manifest.update({'population_status': 'POPULATION_UNAVAILABLE', 'generated_count': None,
                         'captured_count': 0, 'duplicate_count': 0, 'expected_candidate_rows': 0,
                         'control': {'status': 'NO_POPULATION'}})
        return {'manifest': manifest, 'candidates': []}

    rows, seen, dup = [], set(), 0
    lot_cache = {}
    for gen_rank, c in enumerate(ranked, start=1):
        if not isinstance(c, dict):
            continue
        cid = c.get('id')
        if not cid or cid in seen:
            dup += 1
            continue
        seen.add(cid)
        index, expiry = c.get('index'), c.get('expiry')
        key = (index, expiry)
        if key not in lot_cache:
            lot_cache[key] = _lot_authority(index, session_date, expiry, lot_resolver) \
                if index in ('NF', 'BNF') and expiry else {'resolved': False, 'reason': 'identity_missing'}
        la = lot_cache[key]
        cand_lot = c.get('lotSize')
        lot = la.get('lot') if la.get('resolved') else None
        lot_problem = None
        if not la.get('resolved'):
            lot_problem = 'lot_authority_unresolved'
        elif cand_lot is not None and int(cand_lot) != int(lot):
            lot_problem = 'candidate_lot_disagrees_with_authority'
        legs, leg_problems = _leg_rows(c, lot, chain_poll_ts)
        family = c.get('type')
        try:
            pre_expiry = bool(expiry) and date.fromisoformat(str(expiry)) > date.fromisoformat(session_date)
        except ValueError:
            pre_expiry = False
        control_reasons = list(leg_problems)
        if lot_problem:
            control_reasons.append(lot_problem)
        if not pre_expiry:
            control_reasons.append('not_pre_expiry_entry')
        if index not in ('NF', 'BNF'):
            control_reasons.append('index_identity_missing')
        rows.append({
            'menu_id': menu_id, 'candidate_id': cid, 'generated_rank': gen_rank,
            'watchlist_rank': watch_rank.get(cid), 'surfaced': cid in watch_rank,
            'selected_primary': cid == primary_id, 'control_role': None,
            'index_key': index, 'expiry': expiry, 'strategy_family': family, 'lane': c.get('lane'),
            'lot_size': lot, 'candidate_lot_size': cand_lot, 'lot_authority': la,
            'legs': legs, 'leg_count': len(legs),
            'candidate_poll_ts': c.get('poll_ts'),
            'entry_eligible': c.get('entryEligible'), 'entry_action': c.get('entryAction'),
            'execution_gate': c.get('executionGate') or c.get('entryGate'),
            'execution_ready': c.get('executionReady'), 'capital_blocked': c.get('capitalBlocked'),
            'direction_safe': c.get('directionSafe'), 'pc2_primary_eligible': c.get('pc2PaperPrimaryEligible'),
            'gate_reasons': _gate_reasons(c),
            'net_premium': _f(c.get('netPremium')), 'max_profit': _f(c.get('maxProfit')),
            'max_loss': _f(c.get('maxLoss')), 'net_premium_edge': _f(c.get('netPremiumEdge')),
            'control_eligible': not control_reasons, 'control_ineligible_reasons': control_reasons,
            'margin_evidence': None,
        })

    primary_row = next((r for r in rows if r['selected_primary']), None)
    control = register_control_peer(menu_id, primary_row, rows)
    if control.get('peer_candidate_id'):
        for r in rows:
            if r['candidate_id'] == control['peer_candidate_id']:
                r['control_role'] = 'REGISTERED_UNIFORM_PEER'
    if primary_row:
        primary_row['control_role'] = 'PRIMARY'

    manifest.update({
        'population_status': 'EMPTY_MENU' if not rows else 'MENU_BUILT',
        'generated_count': len([c for c in ranked if isinstance(c, dict)]),
        'captured_count': len(rows), 'duplicate_count': dup, 'expected_candidate_rows': len(rows),
        'candidate_ids_sha256': hashlib.sha256('\n'.join(r['candidate_id'] for r in rows).encode()).hexdigest(),
        'control': control,
    })
    return {'manifest': manifest, 'candidates': rows}


def register_control_peer(menu_id, primary_row, rows):
    base = {'sampler_version': CONTROL_SAMPLER_VERSION}
    if primary_row is None:
        return {**base, 'status': 'NO_PRIMARY', 'reason': 'no_selected_primary_at_frozen_decision'}
    if not primary_row['control_eligible']:
        return {**base, 'status': 'PRIMARY_INELIGIBLE', 'primary_candidate_id': primary_row['candidate_id'],
                'reason': primary_row['control_ineligible_reasons']}
    filt = {}
    pool = []
    for r in rows:
        if r is primary_row:
            continue
        why = None
        if r['index_key'] != primary_row['index_key']:
            why = 'different_index'
        elif r['expiry'] != primary_row['expiry']:
            why = 'different_expiry'
        elif r['strategy_family'] != primary_row['strategy_family']:
            why = 'different_family'
        elif not r['control_eligible']:
            why = 'ineligible:' + (r['control_ineligible_reasons'][0] if r['control_ineligible_reasons'] else '?')
        if why:
            filt[why] = filt.get(why, 0) + 1
        else:
            pool.append(r['candidate_id'])
    pool.sort()
    seed_material = f"{menu_id}|{primary_row['candidate_id']}|{CONTROL_SAMPLER_VERSION}"
    seed = hashlib.sha256(seed_material.encode()).hexdigest()
    out = {**base, 'primary_candidate_id': primary_row['candidate_id'], 'pool_definition':
           'same index_key + exact expiry + strategy_family; excludes primary; explicit legs, resolved dated '
           'lot authority equal to candidate lot, session_date < expiry, valid entry bid/ask on every leg',
           'eligible_count': len(pool), 'eligible_pool': pool, 'filter_reason_counts': filt,
           'seed_material': seed_material, 'seed_sha256': seed}
    if not pool:
        return {**out, 'status': 'NO_ELIGIBLE_PEER', 'reason': 'empty_eligible_pool'}
    pick = pool[int(seed, 16) % len(pool)]
    return {**out, 'status': 'REGISTERED', 'peer_candidate_id': pick, 'inclusion_probability': 1.0 / len(pool)}


def normalize_margin_evidence(candidate=None, quote=None, *, role=None, candidate_id=None, lot_authority=None,
                              requested_instruments=None, request_origin=None):
    """Broker-margin evidence for ONE standalone candidate (all legs, 1 dated lot, product D, INR).

    Accepts either an annotated candidate (keys written by MarketWatchService.annotateMarginQuote) or a
    raw quote dict (MarginQuote.toJson). Static/UI estimates and Brain fallbacks are never treated as
    broker margin: amounts stay null unless the broker quote succeeded with a positive amount."""
    c = candidate if isinstance(candidate, dict) else {}
    q = quote if isinstance(quote, dict) else (c.get('marginQuote') if isinstance(c.get('marginQuote'), dict) else {})
    raw_status = q.get('status') if q else c.get('marginQuoteStatus')
    source = c.get('marginQuoteSource') or (BROKER_MARGIN_SOURCE if q else None)
    required = _f(q.get('required_margin', q.get('requiredMargin'))) if q else _f(c.get('upstoxRequiredMargin'))
    final = _f(q.get('final_margin', q.get('finalMargin'))) if q else _f(c.get('upstoxFinalMargin'))
    quoted_at = (q.get('quoted_at') or q.get('quotedAt')) if q else c.get('marginQuotedAt')
    error = (q.get('error') if q else None) or c.get('marginQuoteError')
    s = str(raw_status or '')
    if not s or s.upper() == 'FALLBACK_STATIC' or (source and source != BROKER_MARGIN_SOURCE):
        status = 'NOT_ATTEMPTED'
    elif s.lower() == 'success':
        status = 'QUOTED_OK' if (final or required or 0) > 0 else 'QUOTE_EMPTY'
    elif s.upper() == 'ERROR':
        status = 'QUOTE_ERROR'
    elif s.upper() == 'SKIPPED':
        status = 'SKIPPED_' + str(error or 'unspecified').upper()[:60]
    else:
        status = 'UNRECOGNIZED_STATUS'
    ok = status == 'QUOTED_OK'
    return {
        'version': MARGIN_EVIDENCE_VERSION, 'role': role,
        'candidate_id': candidate_id or c.get('id'), 'scope': MARGIN_SCOPE,
        'portfolio_margin': False, 'currency': 'INR', 'product': 'D', 'lots': 1,
        'lot_authority': lot_authority,
        'requested_instruments': requested_instruments,
        'status': status, 'raw_status': raw_status, 'source': BROKER_MARGIN_SOURCE if ok else source,
        'quoted_at': quoted_at, 'quoted_at_clock': 'device_local_request_time' if quoted_at else None,
        'final_margin_inr': final if ok else None, 'required_margin_inr': required if ok else None,
        'span_inr': _f(q.get('span_margin', q.get('spanMargin'))) if ok and q else (_f(c.get('upstoxSpanMargin')) if ok else None),
        'exposure_inr': _f(q.get('exposure_margin', q.get('exposureMargin'))) if ok and q else (_f(c.get('upstoxExposureMargin')) if ok else None),
        'error': None if ok else error,
        'fallback_used': False, 'ui_estimates_excluded': True,
        'request_origin': request_origin,
        'affects_ranking': False,
    }


def attach_margin_evidence(menu, margin_by_candidate, request_stats=None):
    """Attach normalized margin to the primary and registered peer only (decision 3)."""
    menu = menu if isinstance(menu, dict) else {}
    mbc = margin_by_candidate if isinstance(margin_by_candidate, dict) else {}
    roles = {}
    for r in menu.get('candidates') or []:
        if r.get('control_role') in ('PRIMARY', 'REGISTERED_UNIFORM_PEER'):
            entry = mbc.get(r['candidate_id']) if isinstance(mbc.get(r['candidate_id']), dict) else None
            ev = normalize_margin_evidence(
                quote=entry.get('quote') if entry else None, role=r['control_role'], candidate_id=r['candidate_id'],
                lot_authority={k: (r.get('lot_authority') or {}).get(k) for k in ('lot', 'rule_id', 'table_version')},
                requested_instruments=entry.get('instruments') if entry else None,
                request_origin=entry.get('origin') if entry else 'NOT_REQUESTED')
            r['margin_evidence'] = ev
            roles[r['control_role']] = ev
    m = menu.setdefault('manifest', {})
    m['margin_evidence_by_role'] = roles
    m['margin_request_stats'] = request_stats
    return menu


def capture_digest(menu):
    return hashlib.sha256(json.dumps(menu, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def attach_margin_evidence_json(menu_json, margin_json, stats_json='null'):
    """Chaquopy bridge."""
    return json.dumps(attach_margin_evidence(json.loads(menu_json), json.loads(margin_json),
                                             json.loads(stats_json)), separators=(',', ':'), default=str)
