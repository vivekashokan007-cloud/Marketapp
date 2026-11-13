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
F, disp, n = M.implied_forward(qs, pins, 'A1', 'NF')
check('D1 cross-expiry legs are not paired', F is None and n == 0, 'pairs=%d' % n)

# Same strikes, same expiry -> two genuine pairs, forward is produced.
pins_ok = {c['instrument_key']: c for c in [
    contract('ce1', 'CE', 25000, '2026-11-17'),
    contract('pe1', 'PE', 25000, '2026-11-17'),
    contract('ce2', 'CE', 25100, '2026-11-17'),
    contract('pe2', 'PE', 25100, '2026-11-17'),
]}
F, disp, n = M.implied_forward(qs, pins_ok, 'A1', 'NF')
check('D1 same-expiry legs pair normally', F is not None and n == 2,
      'F=%s disp=%s pairs=%d' % (F, disp, n))

# Different underlyings at the same strike/expiry must not pair either.
pins_und = {c['instrument_key']: c for c in [
    contract('ce1', 'CE', 25000, '2026-11-17', und='NF'),
    contract('pe1', 'PE', 25000, '2026-11-17', und='BNF'),
    contract('ce2', 'CE', 25100, '2026-11-17', und='NF'),
    contract('pe2', 'PE', 25100, '2026-11-17', und='BNF'),
]}
F, _d, n = M.implied_forward(qs, pins_und, 'A1', 'NF')
check('D1 cross-underlying legs are not paired', F is None and n == 0, 'pairs=%d' % n)


# -- D2: a single pair must not pass the dispersion gate vacuously -----------------------
one = {('ce1', 560, 'A1'): quote(300.0), ('pe1', 560, 'A1'): quote(100.0)}
F, disp, n = M.implied_forward(one, pins_ok, 'A1', 'NF')
check('D2 one pair is refused despite zero dispersion',
      F is None and n == 1, 'pairs=%d disp=%s' % (n, disp))
check('D2 MIN_FWD_PAIRS is at least 2', M.MIN_FWD_PAIRS >= 2, 'value=%d' % M.MIN_FWD_PAIRS)

# A genuinely inconsistent book (dispersion above the gate) is still refused.
bad = {('ce1', 560, 'A1'): quote(300.0), ('pe1', 560, 'A1'): quote(100.0),
       ('ce2', 560, 'A1'): quote(250.0), ('pe2', 560, 'A1'): quote(10.0)}
F, disp, n = M.implied_forward(bad, pins_ok, 'A1', 'NF')
check('D2 dispersion gate still refuses an inconsistent book',
      F is None and n == 2 and disp > M.FWD_MAX_STRIKE_DISPERSION, 'disp=%.1f' % (disp or -1))

# Legs from a different envelope are not borrowed.
other = dict(one)
other[('ce2', 560, 'B9')] = quote(250.0, aid='B9')
other[('pe2', 560, 'B9')] = quote(150.0, aid='B9')
F, _d, n = M.implied_forward(other, pins_ok, 'A1', 'NF')
check('D2 pairs are not borrowed across envelopes', F is None and n == 1, 'pairs=%d' % n)


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
check('D6 the forward basis is labelled a diagnostic', 'basis (F - spot) DIAGNOSTIC' in src)
check('D6 nothing refuses a minute on the basis sign',
      'basis' not in src.split('exclusions by reason')[0].split('rej_')[-1][:200]
      and 'rej_basis' not in src)


# -- D7: ranking hygiene ------------------------------------------------------------------
check('D7 the primary comparison is E0_FWD', M.PRIMARY == 'E0_FWD')
check('D7 oracles are excluded from the ranked set',
      all(d not in M.RANKED for d in M.DIAG))
check('D7 every oracle is a point forecast', all(d in M.POINT_MODELS for d in M.DIAG))
check('D7 persistence is a point forecast', 'PERSIST' in M.POINT_MODELS)

print()
if FAIL:
    print('%d FAILED: %s' % (len(FAIL), ', '.join(FAIL)))
    sys.exit(1)
print('all focused tests passed')
sys.exit(0)
