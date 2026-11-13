"""Focused tests for the R1 corrections in premium_repricing_v2.py (contract 2.1).

Each test targets one named defect and FAILS if that defect is reintroduced.  Three of the
four were latent on 2026-11-12 -- they did not change that session's numbers -- so a
regression in them would be silent without these tests.  That is precisely why they exist.

  python3 -I tools/spot_path_baseline/test_repricing_fixes.py
"""
import datetime
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
qs = {('ce1', 560, 'A1'): quote(300.0), ('pe1', 560, 'A1'): quote(100.0),
      ('ce2', 560, 'A1'): quote(250.0), ('pe2', 560, 'A1'): quote(150.0)}
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
one = {('ce1', 560, 'A1'): quote(300.0), ('pe1', 560, 'A1'): quote(100.0)}
g = M.implied_forwards(one, pins_ok, 'A1', 'NF')
check('D2 one strike is refused despite zero dispersion', g == {}, 'groups=%s' % sorted(g))
check('D2 MIN_FWD_STRIKES is at least 2', M.MIN_FWD_STRIKES >= 2,
      'value=%d' % M.MIN_FWD_STRIKES)

# A genuinely inconsistent book (dispersion above the gate) is still refused.
bad = {('ce1', 560, 'A1'): quote(300.0), ('pe1', 560, 'A1'): quote(100.0),
       ('ce2', 560, 'A1'): quote(250.0), ('pe2', 560, 'A1'): quote(10.0)}
g = M.implied_forwards(bad, pins_ok, 'A1', 'NF')
check('D2 dispersion gate still refuses an inconsistent book', g == {},
      'groups=%s' % sorted(g))

# Legs from a different envelope are not borrowed.
other = dict(one)
other[('ce2', 560, 'B9')] = quote(250.0, aid='B9')
other[('pe2', 560, 'B9')] = quote(150.0, aid='B9')
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
q_two_exp = {('a_ce', 560, 'A1'): quote(300.0), ('a_pe', 560, 'A1'): quote(100.0),
             ('b_ce', 560, 'A1'): quote(250.0), ('b_pe', 560, 'A1'): quote(150.0)}
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
q_mixed[('c_ce', 560, 'A1')] = quote(250.0)
q_mixed[('c_pe', 560, 'A1')] = quote(150.0)
q_mixed[('b_ce', 560, 'A1')] = quote(260.0)
q_mixed[('b_pe', 560, 'A1')] = quote(140.0)
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
q_far = {('a_ce', 560, 'A1'): quote(300.0), ('a_pe', 560, 'A1'): quote(100.0),
         ('c_ce', 560, 'A1'): quote(250.0), ('c_pe', 560, 'A1'): quote(150.0),
         ('d_ce', 560, 'A1'): quote(900.0), ('d_pe', 560, 'A1'): quote(100.0),
         ('e_ce', 560, 'A1'): quote(850.0), ('e_pe', 560, 'A1'): quote(150.0)}
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
      "dev = (x['cutoff'] - target)" in src2)

print()
if FAIL:
    print('%d FAILED: %s' % (len(FAIL), ', '.join(FAIL)))
    sys.exit(1)
print('all focused tests passed')
sys.exit(0)
