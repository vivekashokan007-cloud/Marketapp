"""Development-block (IN-SAMPLE, descriptive) replay of the v1.2 reference selector, NIFTY 18 Jun - 13 Oct 2026.
Causal k: only expiries finalized (all scheduled decision sessions past their planned exit) before the decision day,
eligible (>=2 completed sessions). R frozen from the training block (expiries <= 2026-08-11).
Fixed Rule B horizon; exit at the first 15:15-15:25 poll of the planned session at executable quotes; a missing exit poll
or exit leg is MISSING (counted, never scored). Illustrative 65-unit lot. NOT a profitability claim."""
import json, math, statistics as st, random, ev_ref as E
from calib_study import ent, ex, next_session, sessions_between, tmins
O = json.load(open('calib_study_out.json')); R = O['R_train']; obs = O['obs']
fin = {}
for o in obs: fin[o['e']] = max(fin.get(o['e'], ''), o['exit_session'])
agg = {}
for o in obs:
    if o['status'] == 'OK': agg.setdefault(o['e'], []).append(o['r'])
def kstate(decision_day, G_ss):
    lam = G_ss * G_ss / (1 - G_ss); q = lam * R; k = 1.0; P = 0.15 ** 2
    for e in sorted(agg):
        if fin[e] < decision_day and len(agg[e]) >= 2:
            x = st.mean(agg[e]); Pm = P + q; G = Pm / (Pm + R); k += G * (x - k); P = (1 - G) * Pm
    return k, math.sqrt(P)
rows = []
for d in sorted(ent):
    e = ent[d]['e']; ste = sessions_between(d, e)
    if not 2 <= ste <= 4: continue
    xs = next_session(d, 2) if ste >= 3 else next_session(d, 1)
    ks, ss = kstate(d, 1 - 0.5 ** 0.1); kf, sf = kstate(d, 1 - 0.5 ** 0.5)
    kset = [min(2.0, max(0.3, x)) for x in (ks - ss, ks + ss, kf - sf, kf + sf)]
    q, _ = E.validate_quotes(ent[d]['rows']); F, flags, s = E.parity_forward(q)
    atoms, st2, diag = E.reference_density(q, F, flags)
    if atoms is None: rows.append(dict(d=d, e=e, status=st2)); continue
    tau = tmins(d, ent[d]['t'], xs, (15, 15)) / tmins(d, ent[d]['t'], e)
    pick, out = E.evaluate(q, F, flags, atoms, diag, tau, kset)
    rec = dict(d=d, e=e, ste=ste, kset=[round(x, 3) for x in kset], status='ABSTAIN' if pick is None else 'PICK')
    if pick:
        rec.update(name=pick['name'], ev_robust=round(pick['ev_robust']))
        if xs not in ex or ex[xs]['e'] != e: rec['outcome'] = 'MISSING_EXIT_POLL'
        else:
            xq = {(float(K), ot): (float(b), float(a)) for K, ot, b, a in ex[xs]['rows']}
            try:
                exit_cash = sum((xq[(K, 'CE' if cp == 1 else 'PE')][0] if n > 0 else -xq[(K, 'CE' if cp == 1 else 'PE')][1]) * abs(n) for n, K, cp in pick['legs'])
                sellp = sum(xq[(K, 'CE' if cp == 1 else 'PE')][0] for n, K, cp in pick['legs'] if n > 0)
                buyp = sum(xq[(K, 'CE' if cp == 1 else 'PE')][1] for n, K, cp in pick['legs'] if n < 0)
                esell = sum(q[(K, 'CE' if cp == 1 else 'PE')][0] for n, K, cp in pick['legs'] if n < 0)
                ebuy = sum(q[(K, 'CE' if cp == 1 else 'PE')][1] for n, K, cp in pick['legs'] if n > 0)
                pnl = 65 * (pick['entry_cash'] + exit_cash) - E.fees_rupees(esell, ebuy, len(pick['legs']), 65) - E.fees_rupees(sellp, buyp, len(pick['legs']), 65)
                rec['outcome'] = round(pnl)
            except KeyError: rec['outcome'] = 'MISSING_EXIT_LEG'
    rows.append(rec)
from collections import Counter
print('REPLAY decision sessions', len(rows), 'status', dict(Counter(r['status'] for r in rows)))
print('REPLAY pick families', dict(Counter(''.join(c for c in r.get('name', '-') if c.isalpha()) for r in rows if r['status'] == 'PICK')))
done = [r for r in rows if isinstance(r.get('outcome'), int)]
print('REPLAY outcomes completed', len(done), 'missing', Counter(r['outcome'] for r in rows if r['status'] == 'PICK' and not isinstance(r.get('outcome'), int)))
v = [r['outcome'] for r in done]
g = {}
for r in done: g.setdefault(r['e'], []).append(r['outcome'])
rnd = random.Random(20261013); keys = sorted(g); bs = []
for _ in range(10000):
    smp = [x for k in (rnd.choice(keys) for _ in keys) for x in g[k]]; bs.append(sum(smp) / len(smp))
bs.sort()
print('REPLAY completed-case mean Rs %.0f (expiry-clustered 95%% CI %.0f to %.0f; B=10000, seed 20261013, %d clusters), median %.0f, win %.0f%%, worst %.0f, best %.0f' % (st.mean(v), bs[250], bs[9750], len(keys), st.median(v), 100 * sum(x > 0 for x in v) / len(v), min(v), max(v)))
print('REPLAY mean EV_robust of picks Rs %.0f' % st.mean(r['ev_robust'] for r in rows if r['status'] == 'PICK'))
json.dump(rows, open('replay_dev_block_out.json', 'w'), indent=0)
for r in rows: print('ROW', r)
