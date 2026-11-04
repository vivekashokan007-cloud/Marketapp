"""G1 authority-provenance chain (stages 1 and 3).

Stage 1 ``produce OUT``: real ``brain.analyze`` (index_direction_v1 authority) over synthetic-but-realistic
index_authority fixtures -> real ``brain.take_poll_snapshot(..., 'android_compact_v1')`` -> the exact JSON the
native CaptureEngine receives.  Writes ``OUT/<scenario>.python.json`` and ``OUT/<scenario>.chain_rows.json``.

Stage 2 is Kotlin (G1AuthorityProvenanceChainTest): EvaluationLocalCache compaction -> local cache row ->
BrainSnapshotPayloadSanitizer (the upload payload) -> ``OUT/<scenario>.uploaded.json``.

Stage 3 ``teach OUT``: the uploaded snapshots go through the production evaluation job API
(``evaluation_job_prepare`` / ``evaluation_job_run_batch``) and the teacher outcomes are written to
``OUT/<scenario>.teacher.json``.  Assertions live in the Kotlin test and in the Python unit test.

No network, broker or Supabase.  Read-only.
"""
import copy
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
PY = os.path.join(ROOT, 'app', 'src', 'main', 'python')
for p in (PY, os.path.join(PY, 'tests')):
    if p not in sys.path:
        sys.path.insert(0, p)

import brain  # noqa: E402
import index_authority_fixtures as F  # noqa: E402
import advice_parity_instrumentation as _api  # noqa: E402

# analyze() appends advice-parity observations to a default store inside the repo; keep the chain read-only.
_PARITY_TMP = tempfile.mkdtemp(prefix='g1chain_parity_')
_api._DEFAULT_STORE_PATH = os.path.join(_PARITY_TMP, 'parity_observations.jsonl')

IST = timezone(timedelta(hours=5, minutes=30))
MANAGED_OPEN = lambda ix: {'id': 'managed-open-' + ix, 'index_key': ix, 'strategy_type': 'BULL_PUT', 'status': 'OPEN',
                           'managed_policy_id': 'index_managed_v1', 'sell_strike': 0, 'buy_strike': 0, 'lots': 1}


def _ctx(state_dir, poll_list, **kw):
    clk = F.clock_ms(poll_list, session=kw.get('session_date', F.SESSION))
    c = F.ctx(indexAuthorityStateDir=state_dir,
              nfChain=F.chain(24000, 50, lot=65, ts=clk - 10000, qty=5000),
              bnfChain=F.chain(52000, 100, lot=30, ts=clk - 10000, qty=5000), **kw)
    return dict(c, analysisClockMs=clk)


def _std_polls(end='11:00'):
    ts = F.times('09:15', end)
    n = len(ts)
    return F.polls(F.linear(24000, 8, n), [52000 + ((-1) ** i) * 20 for i in range(n)], ts=ts)


def _stale_first_leg_of(result_probe, ix, chain):
    """Make the FIRST leg of the first constructed structure on ``ix`` stale (2h old exchange_ts)."""
    cands = (result_probe.get('candidates_by_index') or {}).get(ix) or []
    if not cands:
        return None
    first = cands[0]
    other_keys = {(l['strike'], l['option_type']) for c in cands[1:] for l in c.get('legs') or []}
    for leg in first.get('legs') or []:
        key = (leg['strike'], leg['option_type'])
        if key in other_keys:
            continue
        row = chain['strikes'][str(int(leg['strike']))][leg['option_type']]
        row['exchange_ts'] = row['exchange_ts'] - 2 * 3600 * 1000
        return first['id']
    return None


def _run(tag, polls, open_trades=None, mutate=None, **ctx_kw):
    tmp = tempfile.mkdtemp(prefix='g1chain_')
    try:
        sd = os.path.join(tmp, 'index_authority_v1')
        brain.compute_index_authority(polls[:2], _ctx(sd, polls[:2], **ctx_kw))
        c = _ctx(sd, polls, **ctx_kw)
        stale = {}
        if mutate:
            probe = F.run(brain, polls, copy.deepcopy(c), open_trades=open_trades)
            stale = mutate(probe, c) or {}
        r = F.run(brain, polls, c, open_trades=open_trades)
        snap = brain.take_poll_snapshot(json.dumps(r), json.dumps(c), json.dumps(polls),
                                        persistence_mode='android_compact_v1')
        # take_poll_snapshot stamps the WALL clock; a phone capture happens seconds after the analysis.
        # Freeze it to the fixture's trusted analysis clock so the later chain rows line up (fixture step only).
        snap['poll_ts'] = datetime.fromtimestamp(c['analysisClockMs'] / 1000.0, tz=IST).strftime('%Y-%m-%dT%H:%M:%S%z')
        return r, snap, c, stale
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _stale_mutator(indices):
    def m(probe, c):
        out = {}
        for ix in indices:
            out[ix] = _stale_first_leg_of(probe, ix, c['nfChain' if ix == 'NF' else 'bnfChain'])
        return out
    return m


def scenarios():
    std = _std_polls()
    late = _std_polls('15:20')
    return {
        # both structures of each index admissible -> STRUCTURE_CHOICE_RULE_UNSPECIFIED, no primary
        'pair_both_admissible': dict(polls=std),
        # one managed position open on each index -> MANAGED_POLICY_CAP_ONE_PER_INDEX, both blocked
        'managed_cap_both_blocked': dict(polls=std, open_trades=[MANAGED_OPEN('NF'), MANAGED_OPEN('BNF')]),
        # NF: one structure has a stale leg -> exactly one admitted; BNF not selected -> exactly one primary
        'exactly_one_admitted_stale_leg': dict(polls=std, mutate=_stale_mutator(['NF']), indexSelector='NF'),
        # one admitted on EACH index -> MULTIPLE_MANAGED_SELECTIONS
        'two_indices_selecting': dict(polls=std, mutate=_stale_mutator(['NF', 'BNF'])),
        # NF expiry day: expiry_day_contract -> NF not executable
        'nf_expiry_day': dict(polls=std, session_date=F.NF_EXPIRY, today_ist=F.NF_EXPIRY),
        # post-15:15 poll (15:20)
        'post_1515': dict(polls=late),
    }


def _iso_utc(ms):
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _later_chain_rows(snap, ctx, steps=6):
    """Later quotes for every candidate leg: the fixture chain drifted by a small spot move per step."""
    cj = snap['context_json'] if isinstance(snap['context_json'], dict) else json.loads(snap['context_json'])
    cands = list(cj.get('snapshot_generated_candidates') or [])
    prim = snap.get('primary_candidate_json')
    prim = json.loads(prim) if isinstance(prim, str) and prim else prim
    if isinstance(prim, dict) and prim:
        cands.append(prim)
    want = {}
    for c in cands:
        for leg in c.get('legs') or []:
            want[(c['index'], str(leg['expiry']), int(leg['strike']), leg['option_type'])] = True
    session = snap.get('session_date') or ctx.get('session_date')
    base_ms = int(ctx['analysisClockMs'])
    rows = []
    # intraday path after entry, plus the H2 end-of-day window marks (15:20 / 15:30 IST)
    day = datetime.fromtimestamp(base_ms / 1000.0, tz=IST).replace(second=0, microsecond=0)
    eod = [int(day.replace(hour=15, minute=m).timestamp() * 1000) for m in (20, 30)]
    stamps = [base_ms + i * 5 * 60 * 1000 for i in range(1, steps + 1)] + [t for t in eod if t > base_ms]
    for i, ms in enumerate(stamps, start=1):
        for ix, spot0, step, lot in (('NF', 24000, 50, 65), ('BNF', 52000, 100, 30)):
            ch = F.chain(spot0 + 6 * i * (1 if ix == 'NF' else -1), step, lot=lot)
            for (cix, exp, k, ot) in want:
                if cix != ix:
                    continue
                q = ch['strikes'].get(str(k), {}).get(ot)
                if not q:
                    continue
                rows.append({'index_key': ix, 'strike': k, 'option_type': ot, 'expiry': exp,
                             'poll_ts': _iso_utc(ms), 'ltp': q['ltp'], 'bid': q['bid'], 'ask': q['ask'],
                             'session_date': session})
    return rows


def produce(out):
    os.makedirs(out, exist_ok=True)
    index = {}
    for name, spec in scenarios().items():
        spec = dict(spec)
        polls = spec.pop('polls')
        ot = spec.pop('open_trades', None)
        mut = spec.pop('mutate', None)
        r, snap, c, stale = _run(name, polls, open_trades=ot, mutate=mut, **spec)
        snap.setdefault('id', 'g1-' + name)
        if not snap.get('id'):
            snap['id'] = 'g1-' + name
        with open(os.path.join(out, name + '.python.json'), 'w', encoding='utf-8') as f:
            json.dump({'snapshot': snap}, f)
        with open(os.path.join(out, name + '.chain_rows.json'), 'w', encoding='utf-8') as f:
            json.dump(_later_chain_rows(snap, c), f)
        index[name] = {
            'stale_candidate': stale,
            'managed': {ix: {'reason': r['managed_policy'][ix].get('reason'),
                             'selected': (r['managed_policy'][ix].get('selected') or {}).get('candidate_id'),
                             'gates': [(g['candidate_id'], g['admit'], g['reasons']) for g in r['managed_policy'][ix]['gates']]}
                        for ix in ('NF', 'BNF')},
            'candidates': {ix: [{'id': x['id'], 'type': x['type'], 'isCredit': x['isCredit'],
                                 'netPremium': x['netPremium'], 'entryEligible': x['entryEligible'],
                                 'indexDecisionId': x.get('indexDecisionId')}
                                for x in (r.get('candidates_by_index') or {}).get(ix) or []]
                           for ix in ('NF', 'BNF')},
            'decision_ids': {ix: r['by_index'][ix].get('decision_id') for ix in ('NF', 'BNF')},
        }
    with open(os.path.join(out, 'index.json'), 'w', encoding='utf-8') as f:
        json.dump(index, f, indent=1, sort_keys=True)
    return index


def teach(out):
    with open(os.path.join(out, 'index.json'), encoding='utf-8') as f:
        index = json.load(f)
    for name in index:
        up = os.path.join(out, name + '.uploaded.json')
        if not os.path.exists(up):
            raise SystemExit('MISSING_KOTLIN_STAGE:' + name)
        with open(up, encoding='utf-8') as f:
            snap = json.load(f)
        snaps_path = os.path.join(out, name + '.eval_snapshots.json')
        with open(snaps_path, 'w', encoding='utf-8') as f:
            json.dump([snap], f)
        run_id = 'g1-' + name
        prep = json.loads(brain.evaluation_job_prepare(run_id, snaps_path, os.path.join(out, name + '.chain_rows.json')))
        res = json.loads(brain.evaluation_job_run_batch(run_id, 0, 10))
        brain.evaluation_job_finalize(run_id)
        with open(os.path.join(out, name + '.teacher.json'), 'w', encoding='utf-8') as f:
            json.dump({'prepare': prep, 'batch': res}, f)
    return 0


if __name__ == '__main__':
    cmd, out = sys.argv[1], sys.argv[2]
    if cmd == 'produce':
        produce(out)
    elif cmd == 'teach':
        sys.exit(teach(out))
    else:
        raise SystemExit('usage: chain.py produce|teach OUT')
