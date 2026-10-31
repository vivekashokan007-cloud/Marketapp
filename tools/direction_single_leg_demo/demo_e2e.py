"""End-to-end SYNTHETIC local Paper demonstration of index_single_leg_v1 (Delivery B, R5 review s7).

proposal -> exact-instrument dedicated v3 quote (synthetic fixture, paper_broker_snapshot_v1) -> admission
(final recheck at the deterministic execution clock) -> persisted position -> RESTART (separate OS process) ->
open-position quote first -> monitored exit -> net costs -> Daily review.
Deterministic clocks: single_leg_store_v1._wall_clock is replaced by the simulated wall clock.  Synthetic fixtures only (tests/index_authority_fixtures.py); no network, no broker, no Supabase.
This demonstrates the integrated code path; it says NOTHING about profitability.

Usage:  python demo_e2e.py <work_dir>         (prints a JSON transcript; also writes <work_dir>/transcript.json)
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.normpath(os.path.join(HERE, '..', '..', 'app', 'src', 'main', 'python'))
sys.path.insert(0, PY)
sys.path.insert(0, os.path.join(PY, 'tests'))

SESSION = '2026-11-02'


def _setup(work):
    import advice_parity_instrumentation as api
    api._DEFAULT_STORE_PATH = os.path.join(work, 'parity_observations.jsonl')
    return {'ia': os.path.join(work, 'index_authority_v1'), 'sl': os.path.join(work, 'single_leg_v1'),
            'rep': os.path.join(work, 'self_study_v1')}


def _path():
    import index_authority_fixtures as F
    t1 = F.times('09:15', '10:00', 5)
    t2 = F.times('10:15', '15:00', 15) + ['15:05']
    ts = t1 + t2
    nf = [round(24000 + 25 * i, 2) for i in range(len(t1))] + [round(24225 + 5 * i, 2) for i in range(len(t2))]
    bnf = [52000.0 + ((-1) ** i) * 20 for i in range(len(ts))]
    return F.polls(nf, bnf, ts)


def _evidence(chains):
    import managed_evidence_v1 as me
    import index_authority_fixtures as F
    by = {}
    for ix, c, exp, lot in (('NF', chains['NF'], F.NF_EXPIRY, 65), ('BNF', chains['BNF'], F.BNF_EXPIRY, 30)):
        rows = [{'instrument_key': s[ot]['instrument_key'], 'expiry': exp, 'strike_price': float(k),
                 'instrument_type': ot, 'lot_size': lot, 'minimum_lot': lot, 'tick_size': 5,
                 'underlying_key': me.UNDERLYING_KEYS[ix]} for k, s in c['strikes'].items() for ot in ('CE', 'PE')]
        by[ix], _ = me.parse_option_contracts(ix, {'status': 'success', 'data': rows})
    return me.build_context(by, {}, [])


def _ctx(dirs, poll_list):
    import index_authority_fixtures as F
    clk = F.clock_ms(poll_list)
    last = poll_list[-1]
    nf = F.chain(last['nfSpot'], 50, lot=65, ts=clk - 15000, qty=900, rel_spread=0.02)
    bnf = F.chain(last['bnfSpot'], 100, lot=30, ts=clk - 15000, qty=900, rel_spread=0.02)
    # instrument keys are per (index, strike, side) - make them index-specific like a real feed
    for ix, c in (('NF', nf), ('BNF', bnf)):
        for k, s in c['strikes'].items():
            for ot in ('CE', 'PE'):
                s[ot]['instrument_key'] = f'SYNTH_FO|{ix}{k}{ot}'
    c = F.ctx(last['nfSpot'], last['bnfSpot'], nfChain=nf, bnfChain=bnf,
              indexAuthorityStateDir=dirs['ia'], singleLegStateDir=dirs['sl'], singleLegPaperEnabled=True,
              executionMode='paper', indexAuthorityDeferredWrite=True, analysisClockMs=clk)
    c['managedEvidence'] = _evidence({'NF': nf, 'BNF': bnf})
    return c, clk


def _v3(chains, keys, gen_ms):
    """Synthetic dedicated v3 full-quote response (documented shape) for exactly `keys`."""
    data = {}
    for ix, c in chains.items():
        for k, s in c['strikes'].items():
            for ot in ('CE', 'PE'):
                leg = s[ot]
                if leg['instrument_key'] in keys:
                    data['NSE_FO:SYNTH%s%s%s' % (ix, k, ot)] = {
                        'instrument_token': leg['instrument_key'], 'timestamp': str(int(gen_ms)),
                        'depth': {'buy': [{'price': leg['bid'], 'quantity': 900, 'orders': 3}],
                                  'sell': [{'price': leg['ask'], 'quantity': 900, 'orders': 3}]}}
    return {'status': 'success', 'data': data}


def _fetch_quotes(brain, dirs, c, keys, clk):
    """Native-equivalent bounded fetch: request at clk, response generated clk+300, received clk+600."""
    if not keys:
        return None
    resp = _v3({'NF': c['nfChain'], 'BNF': c['bnfChain']}, keys, clk + 300)
    return json.dumps(keys), json.dumps(resp), clk, clk + 600


def _set_clock(ms):
    import single_leg_store_v1 as sls
    import index_authority_v1 as ia
    sls._wall_clock = lambda: ia.parse_ts(int(ms))


def _analyze_publish(brain, dirs, poll_list):
    """analyze (no file I/O) -> native-equivalent publication -> IO worker persists the delta stamped."""
    import index_authority_fixtures as F
    c, clk = _ctx(dirs, poll_list)
    r = F.run(brain, poll_list, c)
    p = r.get('index_authority_persist')
    if p:
        brain.index_authority_persist(dirs['ia'], p['session_date'], json.dumps(p['delta']), clk + 500)
    return r, c, clk


def phase1(work):
    import brain
    dirs = _setup(work)
    polls = _path()
    log = []
    admitted = None
    for i in range(1, len(polls) + 1):
        if polls[i - 1]['t'] > '10:00':
            break
        r, c, clk = _analyze_publish(brain, dirs, polls[:i])
        plan = (r.get('single_leg_by_index') or {}).get('NF') or {}
        log.append({'t': polls[i - 1]['t'], 'nf_direction': r['by_index']['NF']['direction']['value'],
                    'nf_er': r['by_index']['NF']['direction'].get('er'), 'proposal_action': plan.get('action'),
                    'proposal_admit': plan.get('admit'), 'block_reasons': plan.get('reasons')})
        log[-1]['proposal_status'] = plan.get('status')
        if plan.get('status') in ('ADMISSIBLE', 'ELIGIBLE_PENDING_SNAPSHOT') and admitted is None:
            # user taps Admit: native fetches the exact instrument's dedicated v3 quote, then the final check
            tgt = json.loads(brain.single_leg_quote_targets(dirs['sl'], json.dumps(r)))
            req, resp, rs, rc = _fetch_quotes(brain, dirs, c, tgt['keys'], clk + 1000)
            _set_clock(clk + 1700)
            res = json.loads(brain.single_leg_admit(dirs['sl'], json.dumps(r), 'NF', '[]', True, True, 'BOTH',
                                                    req, resp, rs, rc, 1))
            _set_clock(clk + 2500)
            again = json.loads(brain.single_leg_admit(dirs['sl'], json.dumps(r), 'NF', '[]', True, True, 'BOTH',
                                                      req, resp, rs, rc, 2))
            pos = res.get('position') or {}
            admitted = {'t': polls[i - 1]['t'], 'admit': res['status'], 'retry': again['status'],
                        'reasons': res.get('reasons'), 'quote_targets': tgt,
                        'position_id': res.get('position_id'), 'proposal': {k: pos.get(k) for k in (
                            'action', 'strike', 'option_type', 'instrument_key', 'entry_fill', 'entry_fee', 'units',
                            'capital_required', 'capital_basis', 'contract', 'entry_quote', 'exit_contract')}}
    return {'phase': 1, 'pid': os.getpid(), 'polls': log, 'admission': admitted,
            'store_after_phase1': json.loads(brain.single_leg_read(dirs['sl']))}


def phase2(work):
    import brain
    dirs = _setup(work)
    polls = _path()
    start = next(i for i, p in enumerate(polls) if p['t'] > '10:00')
    before = json.loads(brain.single_leg_read(dirs['sl']))
    events = []
    for i in range(start + 1, len(polls) + 1):
        r, c, clk = _analyze_publish(brain, dirs, polls[:i])
        # IO worker: open-position quotes first (exact instruments), then monitor at the worker's own clock
        tgt = json.loads(brain.single_leg_quote_targets(dirs['sl'], json.dumps(r)))
        fq = _fetch_quotes(brain, dirs, c, tgt['open'], clk + 1000)
        if fq:
            brain.single_leg_put_quotes(dirs['sl'], *fq)
        _set_clock(clk + 2000)
        out = json.loads(brain.single_leg_monitor(dirs['sl'], dirs['ia'], SESSION,
                                                  json.dumps({'NF': polls[i - 1]['nfSpot']})))
        for e in out.get('events') or []:
            events.append(dict(e, t=polls[i - 1]['t']))
    review = json.loads(brain.daily_review_run(dirs['ia'], dirs['rep'], SESSION, '[]', '[]'))
    rep = json.loads(brain.daily_review_read(dirs['rep'], SESSION))
    nf = ((rep.get('latest') or {}).get('by_index') or {}).get('NF') or {}
    return {'phase': 2, 'pid': os.getpid(), 'restart_loaded_positions': len(before.get('positions') or []),
            'events': events, 'store_after_phase2': json.loads(brain.single_leg_read(dirs['sl'])),
            'daily_review': review, 'daily_review_nf_paper_outcomes': nf.get('paper_outcomes'),
            'daily_review_status': (rep.get('latest') or {}).get('report_status')}


def main(work):
    os.makedirs(work, exist_ok=True)
    out = []
    for ph in ('phase1', 'phase2'):          # separate OS processes: phase 2 is a real restart
        proc = subprocess.run([sys.executable, os.path.abspath(__file__), ph, work], capture_output=True, text=True,
                              timeout=1200)
        if proc.returncode != 0:
            raise SystemExit(proc.stderr[-2000:])
        out.append(json.loads(proc.stdout.strip().splitlines()[-1]))
    transcript = {'demo': 'index_single_leg_v1 synthetic e2e', 'synthetic_only': True,
                  'profitability_claim': 'NONE', 'phases': out}
    with open(os.path.join(work, 'transcript.json'), 'w') as f:
        json.dump(transcript, f, indent=1, sort_keys=True)
    return transcript


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] in ('phase1', 'phase2'):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = (phase1 if sys.argv[1] == 'phase1' else phase2)(sys.argv[2])
        print(json.dumps(res, sort_keys=True, default=str))
    else:
        print(json.dumps(main(sys.argv[1]), indent=1, sort_keys=True, default=str))
