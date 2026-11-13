"""Focused tests for the R1 corrections in premium_repricing_v2.py (contract 2.1).

Each test targets one named defect and FAILS if that defect is reintroduced.  Three of the
four were latent on 2026-11-12 -- they did not change that session's numbers -- so a
regression in them would be silent without these tests.  That is precisely why they exist.

  python3 -I tools/spot_path_baseline/test_repricing_fixes.py
"""
import collections
import datetime
import json
import math
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import premium_repricing_v2 as M   # noqa: E402

FAIL = []


def check(name, cond, detail=''):
    print('%-58s %s%s' % (name, 'PASS' if cond else 'FAIL', '  ' + detail if detail else ''))
    if not cond:
        FAIL.append(name)


def quote(mid, aid='A1', minute=560):
    return {'mid': mid, 'aid': aid, 'cutoff': M.ist('2026-11-12', minute), 'iv': 18.0,
            'bid': mid - 1, 'ask': mid + 1, 'bid_qty': 100, 'ask_qty': 100,
            'event_clock_verified': False}


def contract(ikey, kind, strike, expiry, und='NF'):
    return {'instrument_key': ikey, 'option_type': kind, 'strike': strike,
            'expiry': expiry, 'underlying': und}


# -- D1: parity must not pair legs from different expiries -------------------------------
# Two strikes, but each strike's call and put sit on DIFFERENT expiries, so there is NO
# valid pair at all.  Keying by strike alone (the 2.0 defect) would find two "pairs".
pins = {c['instrument_key']: c for c in [
    contract('ce1', 'CE', 25000, '2026-11-17'),
    contract('pe1', 'PE', 25000, '2026-11-24'),
    contract('ce2', 'CE', 25100, '2026-11-17'),
    contract('pe2', 'PE', 25100, '2026-11-24'),
]}
qs = {('ce1', 'A1'): quote(300.0), ('pe1', 'A1'): quote(100.0),
      ('ce2', 'A1'): quote(250.0), ('pe2', 'A1'): quote(150.0)}
g = M.implied_forwards(qs, pins, 'A1', 'NF')
check('D1 cross-expiry legs are not paired', g == {}, 'groups=%s' % sorted(g))

# Same strikes, same expiry -> two genuine pairs, forward is produced.
pins_ok = {c['instrument_key']: c for c in [
    contract('ce1', 'CE', 25000, '2026-11-17'),
    contract('pe1', 'PE', 25000, '2026-11-17'),
    contract('ce2', 'CE', 25100, '2026-11-17'),
    contract('pe2', 'PE', 25100, '2026-11-17'),
]}
g = M.implied_forwards(qs, pins_ok, 'A1', 'NF')
one_g = g.get(('NF', '2026-11-17'))
check('D1 same-expiry legs pair normally',
      one_g is not None and one_g['strikes'] == 2 and abs(one_g['F'] - 25200.0) < 1e-9,
      'group=%s' % one_g)

# Different underlyings at the same strike/expiry must not pair either.
pins_und = {c['instrument_key']: c for c in [
    contract('ce1', 'CE', 25000, '2026-11-17', und='NF'),
    contract('pe1', 'PE', 25000, '2026-11-17', und='BNF'),
    contract('ce2', 'CE', 25100, '2026-11-17', und='NF'),
    contract('pe2', 'PE', 25100, '2026-11-17', und='BNF'),
]}
g = M.implied_forwards(qs, pins_und, 'A1', 'NF')
check('D1 cross-underlying legs are not paired', g == {}, 'groups=%s' % sorted(g))


# -- D2: a single pair must not pass the dispersion gate vacuously -----------------------
one = {('ce1', 'A1'): quote(300.0), ('pe1', 'A1'): quote(100.0)}
g = M.implied_forwards(one, pins_ok, 'A1', 'NF')
check('D2 one strike is refused despite zero dispersion', g == {}, 'groups=%s' % sorted(g))
check('D2 MIN_FWD_STRIKES is at least 2', M.MIN_FWD_STRIKES >= 2,
      'value=%d' % M.MIN_FWD_STRIKES)

# A genuinely inconsistent book (dispersion above the gate) is still refused.
bad = {('ce1', 'A1'): quote(300.0), ('pe1', 'A1'): quote(100.0),
       ('ce2', 'A1'): quote(250.0), ('pe2', 'A1'): quote(10.0)}
g = M.implied_forwards(bad, pins_ok, 'A1', 'NF')
check('D2 dispersion gate still refuses an inconsistent book', g == {},
      'groups=%s' % sorted(g))

# Legs from a different envelope are not borrowed.
other = dict(one)
other[('ce2', 'B9')] = quote(250.0, aid='B9')
other[('pe2', 'B9')] = quote(150.0, aid='B9')
g = M.implied_forwards(other, pins_ok, 'A1', 'NF')
check('D2 pairs are not borrowed across envelopes', g == {}, 'groups=%s' % sorted(g))


# -- D3: bounds must hold AFTER the anchor is added --------------------------------------
# A deep-OTM call priced near zero with a large negative anchor would go below intrinsic,
# and for a deep-ITM option below its intrinsic value, unless bounds are re-applied.
U, K, T, sig = 25000.0, 24000.0, 0.01, 0.18
raw = M.bs(U, K, T, sig, 'CE')
anchored = M.bounds(raw - 5000.0, U, K, 'CE')
check('D3 anchored call is not below intrinsic',
      anchored >= max(0.0, U - K) - 1e-9, 'px=%.2f intrinsic=%.2f' % (anchored, U - K))
anchored_put = M.bounds(M.bs(U, 26000.0, T, sig, 'PE') - 5000.0, U, 26000.0, 'PE')
check('D3 anchored put is not below intrinsic',
      anchored_put >= max(0.0, 26000.0 - U) - 1e-9,
      'px=%.2f intrinsic=%.2f' % (anchored_put, 26000.0 - U))
check('D3 anchored price is never negative', M.bounds(-1e9, U, K, 'CE') >= 0.0)
check('D3 call is capped at the underlying', M.bounds(1e9, U, K, 'CE') <= U + 1e-9)
check('D3 put is capped at the strike', M.bounds(1e9, U, K, 'PE') <= K + 1e-9)
# and the un-anchored pricer still obeys them
check('D3 pricer itself stays inside bounds',
      max(0.0, U - K) - 1e-9 <= M.bs(U, K, T, sig, 'CE') <= U)


# -- D4: the clock runs on the forecast cutoff, not the nominal minute -------------------
nom = M.ist('2026-11-12', 9 * 60 + 20)
cut = nom + datetime.timedelta(seconds=5.8)
check('D4 a 60-minute hold consumes 60 calendar minutes',
      abs((M.tte(nom, '2026-11-17')
           - M.tte(nom + datetime.timedelta(minutes=60), '2026-11-17'))
          * M.YEAR_SECONDS / 60.0 - 60.0) < 1e-6)
check('D4 a later cutoff leaves strictly less time to expiry',
      M.tte(cut, '2026-11-17') < M.tte(nom, '2026-11-17'))
check('D4 the cutoff shift is the receipt lag, not a session fraction',
      abs((M.tte(nom, '2026-11-17') - M.tte(cut, '2026-11-17')) * M.YEAR_SECONDS - 5.8) < 1e-3)
check('D4 expiry is read per contract, so the two indices differ',
      M.tte(nom, '2026-11-23') > M.tte(nom, '2026-11-17'))
check('D4 timestamps are timezone-aware IST', nom.tzinfo is not None
      and nom.utcoffset() == datetime.timedelta(hours=5, minutes=30))
check('D4 horizon tolerance is tight enough to catch a missed slot',
      0 < M.HORIZON_TOL_S < 300, 'tol=%.1fs' % M.HORIZON_TOL_S)

# The withdrawn v1 clock, reconstructed, must NOT reproduce the calendar identity.
SESSION_MINUTES = 375.0
t0_old = (5 + (SESSION_MINUTES - (9 * 60 + 20) + 555) / SESSION_MINUTES) / 365.0
t1_old = t0_old - (60.0 / SESSION_MINUTES) / 365.0
check('D4 the withdrawn session-fraction clock is detectably wrong',
      abs((t0_old - t1_old) * M.YEAR_SECONDS / 60.0 - 60.0) > 100.0,
      'consumed %.1f min' % ((t0_old - t1_old) * M.YEAR_SECONDS / 60.0))


# -- D5: no inferential claim from six dependent contracts -------------------------------
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'premium_repricing_v2.py')).read()
check('D5 no sign-test p-value is computed from contracts',
      'sign-test' not in src and '2.0 ** -' not in src and '2 ** -len' not in src)
check('D5 per-contract output is labelled descriptive', 'descriptive only' in src)


# -- D6: basis sign is a diagnostic, not an eligibility gate -----------------------------
check('D6 the forward basis is labelled a diagnostic',
      any('basis' in ln and 'DIAGNOSTIC' in ln for ln in src.splitlines()))
check('D6 nothing refuses a minute on the basis sign',
      'basis' not in src.split('exclusions by reason')[0].split('rej_')[-1][:200]
      and 'rej_basis' not in src)


# -- D7: ranking hygiene ------------------------------------------------------------------
check('D7 the primary comparison is E0_FWD', M.PRIMARY == 'E0_FWD')
check('D7 oracles are excluded from the ranked set',
      all(d not in M.RANKED for d in M.DIAG))
check('D7 every oracle is a point forecast', all(d in M.POINT_MODELS for d in M.DIAG))
check('D7 persistence is a point forecast', 'PERSIST' in M.POINT_MODELS)


# -- D8: parity estimates must never be pooled across expiries ---------------------------
# Codex's case: ONE complete strike pair in each of two expiries.  Pooling gives two
# estimates and passes a two-item count check, while the "dispersion" compares two
# genuinely different forwards.  Per group, each has only one strike, so neither qualifies.
pins_two_exp = {c['instrument_key']: c for c in [
    contract('a_ce', 'CE', 25000, '2026-11-17'),
    contract('a_pe', 'PE', 25000, '2026-11-17'),
    contract('b_ce', 'CE', 25100, '2026-11-24'),
    contract('b_pe', 'PE', 25100, '2026-11-24'),
]}
q_two_exp = {('a_ce', 'A1'): quote(300.0), ('a_pe', 'A1'): quote(100.0),
             ('b_ce', 'A1'): quote(250.0), ('b_pe', 'A1'): quote(150.0)}
g = M.implied_forwards(q_two_exp, pins_two_exp, 'A1', 'NF')
check('D8 one strike in each of two expiries yields no forward', g == {},
      'groups=%s' % sorted(g))

# Two strikes in one expiry and one in another: only the complete group qualifies, and the
# other expiry's contracts must get nothing rather than borrowing it.
pins_mixed = {c['instrument_key']: c for c in [
    contract('a_ce', 'CE', 25000, '2026-11-17'),
    contract('a_pe', 'PE', 25000, '2026-11-17'),
    contract('c_ce', 'CE', 25100, '2026-11-17'),
    contract('c_pe', 'PE', 25100, '2026-11-17'),
    contract('b_ce', 'CE', 25200, '2026-11-24'),
    contract('b_pe', 'PE', 25200, '2026-11-24'),
]}
q_mixed = dict(q_two_exp)
q_mixed[('c_ce', 'A1')] = quote(250.0)
q_mixed[('c_pe', 'A1')] = quote(150.0)
q_mixed[('b_ce', 'A1')] = quote(260.0)
q_mixed[('b_pe', 'A1')] = quote(140.0)
g = M.implied_forwards(q_mixed, pins_mixed, 'A1', 'NF')
check('D8 only the group with two strikes qualifies',
      set(g) == {('NF', '2026-11-17')}, 'groups=%s' % sorted(g))
check('D8 the qualifying group uses only its own strikes',
      g[('NF', '2026-11-17')]['strikes'] == 2 and abs(g[('NF', '2026-11-17')]['F'] - 25200.0) < 1e-9,
      'group=%s' % g[('NF', '2026-11-17')])
check('D8 a contract in the unqualified expiry gets no forward',
      g.get(M.group_key(pins_mixed['b_ce'], 'NF')) is None)

# A second expiry whose forward is far away must not inflate the first group's dispersion.
pins_far = {c['instrument_key']: c for c in [
    contract('a_ce', 'CE', 25000, '2026-11-17'),
    contract('a_pe', 'PE', 25000, '2026-11-17'),
    contract('c_ce', 'CE', 25100, '2026-11-17'),
    contract('c_pe', 'PE', 25100, '2026-11-17'),
    contract('d_ce', 'CE', 26000, '2026-12-29'),
    contract('d_pe', 'PE', 26000, '2026-12-29'),
    contract('e_ce', 'CE', 26100, '2026-12-29'),
    contract('e_pe', 'PE', 26100, '2026-12-29'),
]}
q_far = {('a_ce', 'A1'): quote(300.0), ('a_pe', 'A1'): quote(100.0),
         ('c_ce', 'A1'): quote(250.0), ('c_pe', 'A1'): quote(150.0),
         ('d_ce', 'A1'): quote(900.0), ('d_pe', 'A1'): quote(100.0),
         ('e_ce', 'A1'): quote(850.0), ('e_pe', 'A1'): quote(150.0)}
g = M.implied_forwards(q_far, pins_far, 'A1', 'NF')
check('D8 a distant second expiry does not refuse the first group',
      set(g) == {('NF', '2026-11-17'), ('NF', '2026-12-29')}, 'groups=%s' % sorted(g))
check('D8 each group keeps its own forward',
      abs(g[('NF', '2026-11-17')]['F'] - 25200.0) < 1e-9
      and abs(g[('NF', '2026-12-29')]['F'] - 26800.0) < 1e-9,
      '%.1f / %.1f' % (g[('NF', '2026-11-17')]['F'], g[('NF', '2026-12-29')]['F']))
check('D8 in-group dispersion is computed within the group',
      g[('NF', '2026-11-17')]['disp'] < 1e-9 and g[('NF', '2026-12-29')]['disp'] < 1e-9,
      '%.2f / %.2f' % (g[('NF', '2026-11-17')]['disp'], g[('NF', '2026-12-29')]['disp']))


# -- D9: spot must be established as available before use --------------------------------
cut = M.ist('2026-11-12', 560) + datetime.timedelta(seconds=5.8)
tl = [(M.ist('2026-11-12', 555) + datetime.timedelta(seconds=2.4), 555, 25209.55, 'AVAILABLE'),
      (M.ist('2026-11-12', 560) + datetime.timedelta(seconds=7.8), 560, 25306.05, 'AVAILABLE')]
av = M.spot_as_of(tl, cut)
check('D9 a spot received after the cutoff is not used',
      av is not None and av[1] == 555 and abs(av[2] - 25209.55) < 1e-9,
      'picked asof minute %s' % (av[1] if av else None))
check('D9 the same-minute reading would have been look-ahead',
      tl[1][0] > cut, 'receipt %s > cutoff %s' % (tl[1][0].time(), cut.time()))
check('D9 no qualifying reading returns None', M.spot_as_of(tl[1:], cut) is None)
check('D9 the most recent qualifying reading wins',
      M.spot_as_of(tl, cut + datetime.timedelta(seconds=10))[1] == 560)
check('D9 a record without a receipt clock cannot establish availability',
      M.parse_ts(None) is None)
check('D9 naive timestamps are read as IST',
      M.parse_ts('2026-11-12T09:20:00').utcoffset() == datetime.timedelta(hours=5, minutes=30))
check('D9 E0_FWD is not in the spot-dependent set', 'E0_FWD' not in M.SPOT_DEPENDENT)
check('D9 every spot-based model is declared spot-dependent',
      M.SPOT_DEPENDENT == frozenset(('E0_IV', 'E0_IV_ANCH', 'E0RV_IV_ANCH')),
      '%s' % sorted(M.SPOT_DEPENDENT))


# -- D10: forecast target is planned at entry, not read from the exit --------------------
entry = M.ist('2026-11-12', 560) + datetime.timedelta(seconds=5.804)
target = entry + datetime.timedelta(minutes=60)
exit_recv = entry + datetime.timedelta(seconds=3600 + 1.09)   # realistic jitter
check('D10 the planned target is exactly 60 calendar minutes after entry',
      abs((M.tte(entry, '2026-11-17') - M.tte(target, '2026-11-17')) * M.YEAR_SECONDS - 3600.0)
      < 1e-6)
check('D10 the planned target differs from the actual exit receipt',
      abs((exit_recv - target).total_seconds() - 1.09) < 1e-6)
check('D10 T1 from the planned target is knowable at entry',
      M.tte(target, '2026-11-17') != M.tte(exit_recv, '2026-11-17'))
check('D10 jitter inside tolerance is admitted',
      abs((exit_recv - target).total_seconds()) <= M.HORIZON_TOL_S)
late = entry + datetime.timedelta(seconds=3600 + 42)
check('D10 an exit quote outside tolerance is refused',
      abs((late - target).total_seconds()) > M.HORIZON_TOL_S)
src2 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'premium_repricing_v2.py')).read()
check('D10 T1 is computed from the target, not the exit cutoff',
      'T1 = tte(target' in src2 and 'T1 = tte(x[' not in src2)
check('D10 the exit receipt is used for the tolerance test',
      "dev = (best['cutoff'] - target).total_seconds()" in src2
      and 'abs(dev) <= tol_s' in src2)


# -- D11: one acquisition per forecast; deterministic selection; order independence -------
# Build a minimal full replay: two acquisitions share nominal minute 09:20.  A1 is complete
# and earlier; A2 arrives 2 s later.  The forward implied by A2 differs from A1's, so if the
# earlier forecast could reach the later acquisition's forward the score would change.

def _mk_export(a2_partial, session='2026-11-13'):
    def rec(ikey, slot, aid, bid, ask, iv=18.0):
        return {'instrument_key': ikey, 'slot': slot, 'acquisition_id': aid,
                'identity': {'identity_verified': True, 'identity_disagreements': []},
                'bbo': {'bid_raw': {'raw': bid}, 'ask_raw': {'raw': ask},
                        'bid_qty_raw': {'raw': 500}, 'ask_qty_raw': {'raw': 500}},
                'iv': {'iv_greeks_raw': {'raw': iv, 'status': 'PRESENT_VALID'}}}

    def envp(slot, aid, secs):
        base = M.ist(session, M.mins(slot))
        return {'acquisition_id': aid, 'slot': slot,
                'clocks': {'acquisition_response_ms':
                           (base + datetime.timedelta(seconds=secs)).timestamp() * 1000.0,
                           'quote_event_clock_verified': False}}

    pins = [contract('ce0', 'CE', 25000, '2026-11-17'),
            contract('pe0', 'PE', 25000, '2026-11-17'),
            contract('ce1', 'CE', 25100, '2026-11-17'),
            contract('pe1', 'PE', 25100, '2026-11-17')]
    # A1 at 09:20 +5s  -> parity F = 25000 + 300 - 100 = 25200 on both strikes
    recs = [rec('ce0', '09:20', 'A1', 299, 301), rec('pe0', '09:20', 'A1', 99, 101),
            rec('ce1', '09:20', 'A1', 249, 251), rec('pe1', '09:20', 'A1', 149, 151)]
    envs = [envp('09:20', 'A1', 5.0), envp('09:20', 'A2', 7.0), envp('10:20', 'A3', 5.0)]
    # A2 at 09:20 +7s -> would imply F = 25400, i.e. 200 points away from A1's
    recs += [rec('ce0', '09:20', 'A2', 499, 501), rec('pe0', '09:20', 'A2', 99, 101)]
    if not a2_partial:
        recs += [rec('ce1', '09:20', 'A2', 449, 451), rec('pe1', '09:20', 'A2', 149, 151)]
    # exit acquisition exactly 3600 s after A1's cutoff
    recs += [rec('ce0', '10:20', 'A3', 309, 311), rec('pe0', '10:20', 'A3', 95, 97),
             rec('ce1', '10:20', 'A3', 259, 261), rec('pe1', '10:20', 'A3', 145, 147)]
    hist_recs = []
    for mi, val in ((555, 25180.0), (560, 25200.0), (620, 25240.0)):
        base = M.ist(session, mi)
        hist_recs.append({'spot': {'value': val,
                                   'asof': base.isoformat()},
                          'receipt_ts': (base + datetime.timedelta(seconds=2.4)).isoformat(),
                          'availability': 'AVAILABLE'})
    return {'manifest': {'session_date_used': session},
            'premium': {'indices': {'NF': {'state': 'OK', 'records': recs,
                                           'slot_envelopes': envs,
                                           'pinned_contracts': pins}}},
            'parts': {'authority_doc': {'history': {'NF': hist_recs}}}}


_hist = collections.defaultdict(dict)
for _i, _d in enumerate(['2026-11-%02d' % n for n in (2, 3, 4, 5, 6, 9)]):
    for _m, _v in ((555, 25100.0), (560, 25100.0), (620, 25100.0 * (1 + 0.001 * (_i - 2)))):
        _hist[_d][_m] = {'NF': _v, 'BNF': _v * 2}
_hd = sorted(_hist)

ex_partial = _mk_export(True)
s1, rows1, _c1, _l1 = M.score_index(ex_partial, 'NF', _hist, _hd, '2026-11-13')
check('D11 two acquisitions in one minute are detected',
      s1['coverage'].get('contract_minutes_with_multiple_acquisitions') == 2
      and s1['coverage'].get('slots_with_multiple_acquisitions') == 1,
      'cm=%s slots=%s' % (s1['coverage'].get('contract_minutes_with_multiple_acquisitions'),
                          s1['coverage'].get('slots_with_multiple_acquisitions')))
check('D11 the earlier acquisition is selected as entry',
      s1['selected_entry_acquisitions'] == ['A1'],
      'selected=%s' % s1['selected_entry_acquisitions'])
check('D11 the entry uses its own acquisition forward',
      all(abs(r['fwd'] - 25200.0) < 1e-9 for r in rows1 if 'fwd' in r),
      'forwards=%s' % sorted({round(r['fwd'], 2) for r in rows1 if 'fwd' in r}))
check('D11 a partial later acquisition yields no forward of its own',
      s1['coverage'].get('fwd_rej_too_few_strikes_in_group', 0) >= 1,
      'rej=%s' % s1['coverage'].get('fwd_rej_too_few_strikes_in_group'))

# Now the later acquisition is COMPLETE, with a forward 200 points away.  The earlier
# forecast must still use 25200, not 25400.
ex_both = _mk_export(False)
s2, rows2, _c2, _l2 = M.score_index(ex_both, 'NF', _hist, _hd, '2026-11-13')
check('D11 a complete later acquisition does not displace the earlier entry',
      s2['selected_entry_acquisitions'] == ['A1'],
      'selected=%s' % s2['selected_entry_acquisitions'])
check('D11 the earlier forecast cannot reach the later forward',
      all(abs(r['fwd'] - 25200.0) < 1e-9 for r in rows2 if 'fwd' in r),
      'forwards=%s' % sorted({round(r['fwd'], 2) for r in rows2 if 'fwd' in r}))
check('D11 the later forward really would have differed',
      abs(M.implied_forwards({('ce0', 'A2'): {'mid': 500.0}, ('pe0', 'A2'): {'mid': 100.0},
                              ('ce1', 'A2'): {'mid': 450.0}, ('pe1', 'A2'): {'mid': 150.0}},
                             {c['instrument_key']: c for c in ex_both['premium']['indices']
                              ['NF']['pinned_contracts']},
                             'A2', 'NF')[('NF', '2026-11-17')]['F'] - 25400.0) < 1e-9)

# Order independence on the full replay, for both shapes.
for lbl, mk in (('partial', True), ('complete', False)):
    fwd_ex, rev_ex = _mk_export(mk), M.reverse_input(_mk_export(mk))
    sf, rf, _cf, _lf = M.score_index(fwd_ex, 'NF', _hist, _hd, '2026-11-13')
    sr, rr, _cr, _lr = M.score_index(rev_ex, 'NF', _hist, _hd, '2026-11-13')
    check('D11 reversing input leaves selected acquisitions identical (%s)' % lbl,
          sf['selected_entry_acquisitions'] == sr['selected_entry_acquisitions'],
          '%s vs %s' % (sf['selected_entry_acquisitions'], sr['selected_entry_acquisitions']))
    check('D11 reversing input leaves scores identical (%s)' % lbl,
          json.dumps(sf['models'], sort_keys=True) == json.dumps(sr['models'], sort_keys=True))
    check('D11 reversing input leaves coverage identical (%s)' % lbl,
          sf['coverage'] == sr['coverage'])
    check('D11 reversing input leaves the chosen exit identical (%s)' % lbl,
          [r['exit_aid'] for r in rf] == [r['exit_aid'] for r in rr])

# Exit selection is driven by the target, and refuses when nothing is near enough.
entry = {'cutoff': M.ist('2026-11-13', 560), 'aid': 'A1'}
tgt = entry['cutoff'] + datetime.timedelta(minutes=60)
near = {'cutoff': tgt + datetime.timedelta(seconds=1.0), 'aid': 'Z9'}
far = {'cutoff': tgt + datetime.timedelta(seconds=90), 'aid': 'A0'}
got, dev = M.select_exit([far, near], tgt, M.HORIZON_TOL_S)
check('D11 the exit nearest the planned target is chosen',
      got is near and abs(dev - 1.0) < 1e-6, 'dev=%s' % dev)
got, dev = M.select_exit([far], tgt, M.HORIZON_TOL_S)
check('D11 an exit outside tolerance is refused and its deviation reported',
      got is None and abs(dev - 90.0) < 1e-6, 'dev=%s' % dev)
tie_a = {'cutoff': tgt + datetime.timedelta(seconds=2), 'aid': 'B'}
tie_b = {'cutoff': tgt + datetime.timedelta(seconds=2), 'aid': 'A'}
check('D11 exit ties break on the smallest acquisition_id',
      M.select_exit([tie_a, tie_b], tgt, M.HORIZON_TOL_S)[0] is tie_b)
e_a = {'cutoff': M.ist('2026-11-13', 560), 'aid': 'B'}
e_b = {'cutoff': M.ist('2026-11-13', 560), 'aid': 'A'}
check('D11 entry ties break on the smallest acquisition_id',
      M.select_entry([e_a, e_b]) is e_b)
check('D11 entry selection prefers the earliest cutoff',
      M.select_entry([{'cutoff': M.ist('2026-11-13', 561), 'aid': 'A'},
                      {'cutoff': M.ist('2026-11-13', 560), 'aid': 'Z'}])['aid'] == 'Z')
src2 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'premium_repricing_v2.py')).read()
# search the CODE, not the module docstring, which names the removed structures on purpose
_code = src2.split('"""', 2)[-1]
check('D11 no nominal-minute keyed quote or forward index remains in the code',
      'by_minute' not in _code and 'lookup' not in _code,
      'docstring mentions are expected and excluded')
check('D11 the forward is looked up by the entry acquisition id',
      "fwd_by_acq.get(e['aid'])" in src2)


# -- D12: a conflicting key is EXCLUDED, not resolved first-wins --------------------------
# R3 counted the conflict and kept the first record, so one injected conflicting quote moved
# persistence CRPS between 47.91 and 48.20 with the scan direction.  Reproduce that shape:
# inject a disagreeing duplicate, then assert both orders agree AND the key is gone.

def _with_conflict(field_path, newval, partial=False):
    ex = _mk_export(partial)
    recs = ex['premium']['indices']['NF']['records']
    orig = [r for r in recs if r['instrument_key'] == 'ce0' and r['slot'] == '09:20'
            and r['acquisition_id'] == 'A1'][0]
    dup = json.loads(json.dumps(orig))
    tgt = dup
    for k in field_path[:-1]:
        tgt = tgt[k]
    tgt[field_path[-1]] = newval
    recs.append(dup)
    return ex


for lbl, path, val in (
        ('bid', ['bbo', 'bid_raw', 'raw'], 555.0),
        ('ask', ['bbo', 'ask_raw', 'raw'], 777.0),
        ('bid_qty', ['bbo', 'bid_qty_raw', 'raw'], 999),
        ('iv', ['iv', 'iv_greeks_raw', 'raw'], 44.0),
        ('iv_status', ['iv', 'iv_greeks_raw', 'status'], 'ABSENT'),
        ('identity', ['identity', 'identity_verified'], False),
        ('null bid vs real', ['bbo', 'bid_raw', 'raw'], None),
        ('slot', ['slot'], '09:25')):
    exf = _with_conflict(path, val)
    exr = M.reverse_input(_with_conflict(path, val))
    sf, rf, _c, _l = M.score_index(exf, 'NF', _hist, _hd, '2026-11-13')
    sr, rr, _c2, _l2 = M.score_index(exr, 'NF', _hist, _hd, '2026-11-13')
    # the CONFLICTING KEY is ('ce0','A1'); ce0 also exists under A2, so entry selection
    # legitimately falls through to A2.  What must never happen is A1's ce0 being used.
    check('D12 conflicting %-16s -> the conflicting key is excluded' % lbl,
          sf['coverage'].get('rej_conflicting_duplicate_contract_acquisition') == 1
          and not any(r['ikey'] == 'ce0' and r['entry_aid'] == 'A1' for r in rf),
          'conflicts=%s ce0-from-A1 rows=%d'
          % (sf['coverage'].get('rej_conflicting_duplicate_contract_acquisition'),
             sum(1 for r in rf if r['ikey'] == 'ce0' and r['entry_aid'] == 'A1')))
    check('D12 conflicting %-16s -> both orders identical' % lbl,
          json.dumps(sf['models'], sort_keys=True) == json.dumps(sr['models'], sort_keys=True)
          and sf['coverage'] == sr['coverage']
          and sf['selected_entry_acquisitions'] == sr['selected_entry_acquisitions'])

# A conflicting key must not reach the parity calculation of its own acquisition either.
ex_p = _with_conflict(['bbo', 'bid_raw', 'raw'], 555.0, partial=False)
s_p, r_p, _c, _l = M.score_index(ex_p, 'NF', _hist, _hd, '2026-11-13')
check('D12 an excluded key cannot contribute to its acquisition parity group',
      all(not (r['entry_aid'] == 'A1' and r['ikey'] == 'ce0') for r in r_p)
      and all(abs(r['fwd'] - 25200.0) > 1e-9 or r['entry_aid'] != 'A1'
              or r['ikey'] != 'ce0' for r in r_p if 'fwd' in r))

# Excluding one leg can take its parity group below two strikes -- that must be visible.
ex_c = _with_conflict(['bbo', 'bid_raw', 'raw'], 555.0)
s_c, r_c, _c, _l = M.score_index(ex_c, 'NF', _hist, _hd, '2026-11-13')
check('D12 losing a leg to a conflict is reflected in the parity gate',
      s_c['coverage'].get('fwd_rej_too_few_strikes_in_group', 0) >= 1
      or s_c['coverage'].get('rej_no_forward_in_entry_acquisition_group', 0) >= 1,
      'too_few=%s no_fwd=%s' % (s_c['coverage'].get('fwd_rej_too_few_strikes_in_group'),
                                s_c['coverage'].get('rej_no_forward_in_entry_acquisition_group')))

# An IDENTICAL repeat is not a conflict: it collapses to one observation and still scores.
ex_id = _mk_export(True)
_recs = ex_id['premium']['indices']['NF']['records']
_orig = [r for r in _recs if r['instrument_key'] == 'ce0' and r['slot'] == '09:20'
         and r['acquisition_id'] == 'A1'][0]
_recs.append(json.loads(json.dumps(_orig)))
s_i, r_i, _c, _l = M.score_index(ex_id, 'NF', _hist, _hd, '2026-11-13')
check('D12 an identical repeat collapses to one observation',
      s_i['coverage'].get('collapsed_identical_quote_repeats') == 1
      and s_i['coverage'].get('rej_conflicting_duplicate_contract_acquisition') is None,
      'collapsed=%s' % s_i['coverage'].get('collapsed_identical_quote_repeats'))
check('D12 a collapsed repeat still scores its row',
      any(r['ikey'] == 'ce0' for r in r_i))
check('D12 a collapse leaves scores equal to the no-repeat run',
      json.dumps(s_i['models'], sort_keys=True) == json.dumps(s1['models'], sort_keys=True))

# Conflicting ENVELOPES: the acquisition and every quote depending on it are excluded.
for lbl, key, val in (('slot', 'slot', '09:25'),
                      ('receipt clock', 'clocks', {'acquisition_response_ms': 1.0,
                                                   'quote_event_clock_verified': False}),
                      ('event flag', 'clocks', {'acquisition_response_ms':
                                                (M.ist('2026-11-13', 560)
                                                 + datetime.timedelta(seconds=5.0)).timestamp()
                                                * 1000.0,
                                                'quote_event_clock_verified': True})):
    exf = _mk_export(True)
    envs = exf['premium']['indices']['NF']['slot_envelopes']
    a1 = [e for e in envs if e['acquisition_id'] == 'A1'][0]
    bad = json.loads(json.dumps(a1))
    bad[key] = val
    envs.append(bad)
    exr = M.reverse_input(json.loads(json.dumps(exf)))
    sf, rf, _c, _l = M.score_index(exf, 'NF', _hist, _hd, '2026-11-13')
    sr, rr, _c2, _l2 = M.score_index(exr, 'NF', _hist, _hd, '2026-11-13')
    check('D12 conflicting envelope (%s) excludes the acquisition' % lbl,
          sf['coverage'].get('rej_conflicting_envelope_acquisition_id') == 1
          and sf['coverage'].get('rej_quote_depends_on_excluded_acquisition', 0) >= 1,
          'conf=%s dependent=%s'
          % (sf['coverage'].get('rej_conflicting_envelope_acquisition_id'),
             sf['coverage'].get('rej_quote_depends_on_excluded_acquisition')))
    check('D12 conflicting envelope (%s) gives no A1 entry' % lbl,
          'A1' not in sf['selected_entry_acquisitions'],
          'selected=%s' % sf['selected_entry_acquisitions'])
    check('D12 conflicting envelope (%s) is order independent' % lbl,
          json.dumps(sf['models'], sort_keys=True) == json.dumps(sr['models'], sort_keys=True)
          and sf['coverage'] == sr['coverage'])

ex_ei = _mk_export(True)
_envs = ex_ei['premium']['indices']['NF']['slot_envelopes']
_envs.append(json.loads(json.dumps([e for e in _envs if e['acquisition_id'] == 'A1'][0])))
s_ei, _r, _c, _l = M.score_index(ex_ei, 'NF', _hist, _hd, '2026-11-13')
check('D12 an identical envelope repeat collapses',
      s_ei['coverage'].get('collapsed_identical_envelope_repeats') == 1
      and s_ei['coverage'].get('rej_conflicting_envelope_acquisition_id') is None,
      'collapsed=%s' % s_ei['coverage'].get('collapsed_identical_envelope_repeats'))
check('D12 an identical envelope repeat leaves scores unchanged',
      json.dumps(s_ei['models'], sort_keys=True) == json.dumps(s1['models'], sort_keys=True))

src3 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'premium_repricing_v2.py')).read()
_code3 = src3.split('"""', 2)[-1]
check('D12 conflicts are grouped before admission, not resolved first-wins',
      'rec_raw = collections.defaultdict(list)' in _code3
      and 'quote_signature' in _code3 and 'envelope_signature' in _code3)
check('D12 no first-wins keep remains for a duplicate key',
      'if key in q:' not in _code3)
check('D12 the quote signature covers every scored field',
      all(f in _code3.split('def quote_signature')[1].split('def ')[0]
          for f in ('bid', 'ask', 'bid_qty', 'ask_qty', 'iv', 'iv_status',
                    'identity_verified', 'identity_disagreements', 'slot')))

print()
if FAIL:
    print('%d FAILED: %s' % (len(FAIL), ', '.join(FAIL)))
    sys.exit(1)
print('all focused tests passed')
sys.exit(0)
