"""Bind a managed structure's exact legs to broker contract lots and the dated NSE table.

No mutation of the chain or X1 context. A missing or disagreeing leg leaves the whole
structure unpriced in rupees; metadata is never borrowed from another index/expiry.
"""
import managed_evidence_v1 as me


def resolve(construction, evidence, session_date, captured_lot=None):
    legs = construction.get('legs') or []
    reasons, contracts = [], []
    if not legs:
        reasons.append('CONSTRUCTION_LEGS_UNAVAILABLE')
    for leg in legs:
        contract, why = me.resolve_contract(
            evidence, construction.get('index'), leg,
            expiry=construction.get('expiry'), strike=leg.get('strike'),
            option_type=leg.get('option_type'), session_date=session_date,
            captured_lot=captured_lot)
        if why:
            reasons.extend(why)
        elif contract:
            contracts.append(contract)
    lots = {c['lot_size'] for c in contracts}
    if len(lots) > 1:
        reasons.append('CONTRACT_LEG_LOTS_DISAGREE')
    ok = not reasons and len(contracts) == len(legs) and len(lots) == 1
    provenance = {
        'schema': 'managed_construction_lot_v1', 'status': 'VERIFIED' if ok else 'UNAVAILABLE',
        'source': 'upstox_v2_option_contract', 'session_date': session_date,
        'index': construction.get('index'), 'expiry': construction.get('expiry'),
        'reasons': sorted(set(reasons)),
        'legs': [{'instrument_key': c['instrument_key'], 'lot_size': c['lot_size'],
                  'fetched_at': c.get('fetched_at'), 'lot_table': c.get('lot_table')}
                 for c in contracts],
    }
    return (next(iter(lots)) if ok else None), provenance
