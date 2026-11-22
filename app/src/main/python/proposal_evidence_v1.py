"""Bounded research-only exact selected-contract ledger from the acquired live chains.

Records raw field presence separately from null; no clock or quote is invented.
A server chain observation is never a simulated fill. 32 tracked identities/session,
1 MiB/session, ten session files; truncation and missing-contract coverage explicit.
"""
import hashlib
import json
import os
import re

from local_lock_v1 import FileLock

VERSION = 'mr.proposal_evidence.v1'
MAX_BYTES = 1024 * 1024
MAX_TRACKED = 32
KEEP_SESSIONS = 10
FIELDS = ('instrument_key', 'lot_size', 'tick_size', 'expiry', 'underlying_key')
MARKET = ('bid_price', 'ask_price', 'bid_qty', 'ask_qty', 'ltp', 'oi', 'volume',
          'timestamp', 'last_trade_time', 'exchange_timestamp')
GREEKS = ('iv', 'delta', 'gamma', 'theta', 'vega', 'rho')


def _fields(obj, names):
    obj = obj if isinstance(obj, dict) else {}
    return {k: {'status': 'absent'} if k not in obj else
        {'status': 'null'} if obj[k] is None else
        {'status': 'supplied', 'value': obj[k]} for k in names}


def record(files_dir, result_json, chains_json, clocks_json):
    result, chains, clocks = map(json.loads, (result_json, chains_json, clocks_json))
    session = ((result.get('direction_authority') or {}).get('state') or {}).get('session_date')
    if not isinstance(session, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', session):
        return json.dumps({'status': 'SESSION_UNAVAILABLE'})
    directory = os.path.join(files_dir, 'proposal_evidence_v1')
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, session + '.json')
    with FileLock(os.path.join(directory, '.lock'), timeout_s=1):
        try:
            with open(path, 'rb') as src:
                raw = src.read(MAX_BYTES + 1)
            doc = json.loads(raw)
            if len(raw) > MAX_BYTES:
                return json.dumps({'status': 'EXISTING_OVER_CAP'})
        except FileNotFoundError:
            doc = {'schema': VERSION, 'session_date': session, 'tracked': {}, 'records': [],
                   'coverage': {}, 'dropped': 0, 'truncated': False,
                   'clock_semantics': 'request/receipt are client clocks; provider fields retain original names',
                   'price_source': 'RAW_CHAIN_RESEARCH_PROXY_NOT_FILL'}
        except (ValueError, OSError):
            return json.dumps({'status': 'EXISTING_UNREADABLE'})
        for index in ('NF', 'BNF'):
            plan = (result.get('single_leg_by_index') or {}).get(index) or {}
            contract = plan.get('contract') or {}
            key = plan.get('instrument_key')
            if not key or contract.get('index') != index:
                continue
            identity = {k: plan.get(k) for k in ('index', 'instrument_key', 'strike', 'option_type')}
            identity.update(index=index, expiry=contract.get('expiry'), contract_metadata=contract)
            ident = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            if ident not in doc['tracked'] and len(doc['tracked']) >= MAX_TRACKED:
                doc['dropped'] += 1
                doc['truncated'] = True
                continue
            if ident not in doc['tracked']:
                doc['tracked'][ident] = dict(identity, decision_id=plan.get('decision_id'),
                    original_action=plan.get('action'), holding_contract=plan.get('holding_contract'))
        for ident, target in doc['tracked'].items():
            index = target['index']
            source = (chains.get(index) or {}).get('data') or []
            match = None
            for row in source:
                if (str(row.get('expiry')) != str(target.get('expiry'))
                        or row.get('strike_price') != target.get('strike')):
                    continue
                leg = row.get('call_options' if target['option_type'] == 'CE' else 'put_options') or {}
                if leg.get('instrument_key') == target['instrument_key']:
                    match = leg
                    break
            status = 'PRESENT' if match is not None else 'CONTRACT_ABSENT_FROM_THIS_ACQUISITION'
            cov = doc['coverage'].setdefault(index, {'present': 0, 'missing': 0, 'policy_blocked': 0})
            cov['present' if match is not None else 'missing'] += 1
            # Count disallowed branch occurrences once per publication, separately from tracked quote rows.
            rec = {'identity': ident, 'decision_id': target['decision_id'], 'status': status,
                   'source': result.get('input_identity'), 'chain_clocks': clocks.get(index),
                   'brain_version': result.get('brain_version'), 'producer_versions': result.get('producer_versions'),
                   'fields': _fields(match, FIELDS),
                   'market_data': _fields((match or {}).get('market_data'), MARKET),
                   'option_greeks': _fields((match or {}).get('option_greeks'), GREEKS)}
            # Bounded dedupe: a refreshed result for the same acquisition adds no repeated quote.
            if doc['records'] and any(r['identity'] == ident and r['source'] == rec['source']
                                     for r in doc['records'][-MAX_TRACKED:]):
                cov['present' if match is not None else 'missing'] -= 1
                continue
            doc['records'].append(rec)
            if len(json.dumps(doc).encode()) > MAX_BYTES - 8192:
                doc['records'].pop()
                doc['dropped'] += 1
                doc['truncated'] = True
        for ix, p in (result.get('single_leg_by_index') or {}).items():
            if str(p.get('action') or '').startswith('SELL'):
                doc['coverage'].setdefault(ix, {'present': 0, 'missing': 0, 'policy_blocked': 0})['policy_blocked'] += 1
        raw = json.dumps(doc, allow_nan=False).encode()
        if len(raw) > MAX_BYTES:
            return json.dumps({'status': 'OVER_CAP'})
        with open(path + '.tmp', 'wb') as dst:
            dst.write(raw)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(path + '.tmp', path)
        sessions = sorted(n for n in os.listdir(directory) if re.fullmatch(r'\d{4}-\d{2}-\d{2}\.json', n))
        for name in sessions[:-KEEP_SESSIONS]:
            os.unlink(os.path.join(directory, name))
    return json.dumps({'status': 'RECORDED', 'bytes': len(raw), 'coverage': doc['coverage'],
                       'dropped': doc['dropped'], 'truncated': doc['truncated']})
