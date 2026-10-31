"""PM1 behavioural fixture: direction, construction, admission (managed gates + single-leg plans) and
existing-position exits on fixed synthetic sessions.  The digest must equal the value produced by the released
code (a763dc4 / v2.6.85).  Regenerate the reference only by running this file against a763dc4:
    python3 -I pm1_behaviour_fixture.py <a763dc4 checkout>/app/src/main/python
"""
import hashlib, json, os, shutil, sys, tempfile
from datetime import timedelta

if __name__ == '__main__':
    _py = os.path.abspath(sys.argv[1])
    sys.path[:0] = [_py, os.path.join(_py, 'tests')]




def _canon(o):
    return json.dumps(o, sort_keys=True, separators=(',', ':'), default=str)


def sessions():
    import index_authority_fixtures as F
    ts = F.times('09:15', '14:40')
    n = len(ts)
    bull = F.polls([24000 + 7 * i for i in range(n)], [52000 - 11 * i for i in range(n)], ts)
    rng = F.polls([24000 + (15 if (i // 3) % 2 else -15) for i in range(n)],
                  [52000 + (25 if (i // 4) % 2 else -25) for i in range(n)], ts)
    return {'trend': bull, 'range': rng}


def observe(brain, polls, sl_on, tmp, spread=0.04):
    import index_authority_fixtures as F
    sd = os.path.join(tmp, 'index_authority_v1')
    out = []
    for i in range(2, len(polls) + 1, 3):
        p = polls[:i]
        clk = F.clock_ms(p)
        c = F.ctx(indexAuthorityStateDir=sd, analysisClockMs=clk, pollReceiptMs=clk - 5000, singleLegPaperEnabled=sl_on,
                  nfChain=F.chain(p[-1]['nfSpot'], 50, lot=65, ts=clk - 10000, qty=5000, rel_spread=spread),
                  bnfChain=F.chain(p[-1]['bnfSpot'], 100, lot=30, ts=clk - 10000, qty=5000, rel_spread=spread))
        r = F.run(brain, p, c)
        row = {}
        for ix in ('NF', 'BNF'):
            d = (r.get('by_index') or {}).get(ix) or {}
            di = d.get('direction') or {}
            row[ix] = {'id': d.get('decision_id'), 'avail': d.get('availability'),
                       'dir': [di.get('value'), di.get('strength'), di.get('z_open'), di.get('z_30'), di.get('er')],
                       'regime': (d.get('regime') or {}).get('value'), 'allowed': (d.get('structure_policy') or {}).get('allowed'),
                       'action': d.get('action'),
                       'cands': [[c_.get('id'), c_.get('type'), c_.get('entryEligible'),
                                  (c_.get('entryEligibility') or {}).get('reasons')] for c_ in (r.get('candidates_by_index') or {}).get(ix) or []],
                       'managed': {k: v for k, v in ((r.get('managed_policy') or {}).get(ix) or {}).items() if k != 'exposure'},
                       'sl': {k: v for k, v in (((r.get('single_leg_by_index') or {}).get(ix)) or {}).items()
                              if k in ('status', 'action', 'admit', 'reasons', 'strike', 'option_type', 'paper_enabled')}}
        out.append(row)
    return out


def exits():
    import index_authority_v1 as ia
    import index_single_leg_v1 as sl
    import test_index_single_leg_v1_delivery_b as B
    rows = []
    for structure in ('BULL_CALL', 'BEAR_PUT', 'BULL_PUT'):
        t = {'exit_contract': ia.managed_exit_contract('NF', structure, 'd1')}
        for hist in (['BEAR', 'BEAR'], ['BEAR', 'UNAVAILABLE', 'BEAR'], ['NEUTRAL', 'BULL'], ['BULL', 'BULL'],
                     [('2026-11-02T10:00:00+05:30', 'UNAVAILABLE'), ('2026-11-02T10:40:00+05:30', 'UNAVAILABLE')]):
            rows.append([structure, _canon(hist), ia.managed_invalidation(t, hist)])
    rows.append(['legacy', ia.managed_invalidation({}, ['BEAR', 'BEAR'])])
    pos = B.opened()
    tq = B.D + timedelta(minutes=15)
    quote = B.q(pos, pos['entry_fill'], pos['entry_fill'] + 1, tq)
    for obs in (['BEAR'], ['BEAR', 'BEAR'], ['NEUTRAL', 'NEUTRAL']):
        p, ev = sl.evaluate_exit(pos, quote, obs, tq)
        rows.append(['sl', obs, ev, p.get('exit_reason'), p.get('status')])
    t5 = B.D.replace(hour=15, minute=5)
    p, ev = sl.evaluate_exit(pos, None, ['BULL'], t5)
    rows.append(['sl_time', ev, p.get('status'), p.get('mark')])
    return rows


def digest(verbose=False):
    import brain
    out = {}
    for name, polls in sessions().items():
        for sl_on in (False, True):
            for spread in (0.04, 0.5):
                tmp = tempfile.mkdtemp()
                try:
                    out[f'{name}|sl={sl_on}|spread={spread}'] = observe(brain, polls, sl_on, tmp, spread)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
    out['exits'] = exits()
    h = hashlib.sha256(_canon(out).encode()).hexdigest()
    return (h, out) if verbose else h


if __name__ == '__main__':
    os.chdir(tempfile.mkdtemp())
    h, out = digest(verbose=True)
    acts = sorted({_canon(r[ix]['action'].get('value') if r[ix]['action'] else None) for k, v in out.items() if k != 'exits' for r in v for ix in r})
    print(json.dumps({'digest': h, 'actions_seen': acts, 'observations': sum(len(v) for k, v in out.items() if k != 'exits')}))
