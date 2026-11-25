"""Active BNF-only scope; historical/position identity parsers remain multi-index."""
ACTIVE_INDICES = ('BNF',)
RETIRED_REASON = 'INDEX_RETIRED_BNF_ONLY'


def active_index(value):
    return str(value or '').strip().upper() == 'BNF'


def without_nifty_fields(value):
    if not isinstance(value, dict):
        return value
    return {k: v for k, v in value.items()
            if not str(k).lower().startswith('nf') and str(k) != 'candle_nf'}


def isolate_inputs(polls, baseline, ctx):
    """Never relabel NF values as BNF, and never touch persisted position evidence."""
    polls = [without_nifty_fields(p) for p in polls] if isinstance(polls, list) else polls
    baseline = without_nifty_fields(baseline)
    ctx = without_nifty_fields(ctx)
    ctx['indexSelector'] = 'BNF'
    ctx['activeIndices'] = ['BNF']
    return polls, baseline, ctx


def new_candidates(rows):
    return [r for r in rows or [] if isinstance(r, dict)
            and active_index(r.get('index') or r.get('index_key'))]


def active_result(result):
    """Prune new analysis only. Existing position/close evidence is intentionally retained."""
    for k in list(result):
        if str(k).lower().startswith('nf') or k == 'candle_nf':
            result.pop(k, None)
    result['market_scope'] = {'version': 'bnf_only_v1', 'active_indices': ['BNF'],
                              'legacy_positions': 'CLOSE_ONLY'}
    return result
