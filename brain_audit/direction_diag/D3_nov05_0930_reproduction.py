"""D3 - reproduce the 5 November 09:30 records (handoff s48/s78).

Inputs are the handoff's own published 5 November table.  The phone export itself is NOT
available in this session, so m is not read from a record - it is SOLVED from the
published z_open and then checked against the deployed bounds.  Everything derived that
way is labelled solved/reconstructed below, never phone-verified.

Published (handoff s46-54), per index:
                        NF            BNF
  09:20 reference A     24356.85      56904.35
  09:30 spot S          24342.75      56829.70
  09:30 z_open          -0.063        -0.151
  09:30 z_30            -0.388        -0.926
  09:30 ER               0.493         1.000
  production direction  NEUTRAL/WEAK  NEUTRAL/WEAK

Four things are tested:
  T1  the published (z_open, z_30) pair is self-consistent under the DEPLOYED forms
  T2  the solved m lies inside SCALE_M_BOUNDS
  T3  the TIME-SCALED reading of z_open is EXCLUDED by SCALE_M_BOUNDS
  T4  compute_direction, run with the solved m, reproduces NEUTRAL/WEAK and both z values
"""
import math
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _ia import at, banner, ia, series_polls  # noqa: E402

D = date(2026, 11, 5)
SESSION = D.isoformat()
A_DT, T_DT = at(D, 9, 20), at(D, 9, 30)
ELAPSED_MIN = 10.0

PUB = {
    'NF':  {'A': 24356.85, 'S': 24342.75, 'z_open': -0.063, 'z_30': -0.388, 'er': 0.493},
    'BNF': {'A': 56904.35, 'S': 56829.70, 'z_open': -0.151, 'z_30': -0.926, 'er': 1.000},
}

banner('D3  5 Nov 09:30 reproduction   (pinned 3ffe72bd, index_authority_v1)')
print('Z30_LOOKBACK_MIN=%s  -> anchor target = 09:30 minus %s min = 09:02:30, which is before'
      % (ia.Z30_LOOKBACK_MIN, ia.Z30_LOOKBACK_MIN))
print('the 09:20 open reference, so the z_30 anchor CLAMPS TO A at 09:30 and delta = %.1f min.'
      % ELAPSED_MIN)
print('Z30_WARMUP_MIN=%s, so delta=%.1f is exactly at the boundary and passes (test is "<").'
      % (ia.Z30_WARMUP_MIN, ELAPSED_MIN))
print('With the anchor clamped, the deployed forms reduce to')
print('    z_open = log(S/A)/m        z_30 = log(S/A)/(m*sqrt(delta/375)) = z_open/sqrt(delta/375)')
print()

TSCALE = math.sqrt(ELAPSED_MIN / 375.0)
print('sqrt(delta/375) = %.9f      1/sqrt(delta/375) = %.6f' % (TSCALE, 1.0 / TSCALE))
print()

rows = []
for idx in ('NF', 'BNF'):
    p = PUB[idx]
    lg = math.log(p['S'] / p['A'])
    # T1: the published pair must satisfy z_30 = z_open / sqrt(delta/375)
    ratio_pub = p['z_30'] / p['z_open']
    ratio_exp = 1.0 / TSCALE
    # recover z_open from the (more precise, 3-dp) z_30 and re-round
    z_open_from_z30 = p['z_30'] * TSCALE
    # T2/T3: solve m under each reading
    m_deployed = lg / z_open_from_z30
    m_timescaled = lg / (z_open_from_z30 * TSCALE)
    rows.append((idx, lg, ratio_pub, ratio_exp, z_open_from_z30, m_deployed, m_timescaled))

print('T1  self-consistency of the published pair (expected ratio %.6f)' % (1.0 / TSCALE))
for idx, lg, rp, re_, zo, md, mt in rows:
    print('    %-4s z_30/z_open = %.4f   vs %.4f   delta=%.4f   (3-dp inputs)'
          % (idx, rp, re_, abs(rp - re_)))
print()

print('T1b round-trip: z_open recovered from z_30, then re-rounded to 3 dp')
t1b = True
for idx, lg, rp, re_, zo, md, mt in rows:
    ok = round(zo, 3) == PUB[idx]['z_open']
    t1b &= ok
    print('    %-4s z_open = %.6f -> rounds to %+.3f   published %+.3f   match=%s'
          % (idx, zo, round(zo, 3), PUB[idx]['z_open'], ok))
print()

print('T2  m solved under the DEPLOYED (non-time-scaled) z_open; bounds %s' % (ia.SCALE_M_BOUNDS,))
t2 = True
for idx, lg, rp, re_, zo, md, mt in rows:
    inside = ia.SCALE_M_BOUNDS[0] <= md <= ia.SCALE_M_BOUNDS[1]
    t2 &= inside
    print('    %-4s log(S/A) = %+.9f   m = %.8f   inside bounds = %s' % (idx, lg, md, inside))
print('    for reference, m read from device history: NF 2 Nov 0.00778287, 4 Nov 0.01017554;')
print('                                               BNF 3 Nov 0.00897558, 4 Nov 0.00942820')
print()

print('T3  m solved under a TIME-SCALED z_open (the R2 convention)')
t3 = True
for idx, lg, rp, re_, zo, md, mt in rows:
    inside = ia.SCALE_M_BOUNDS[0] <= mt <= ia.SCALE_M_BOUNDS[1]
    t3 &= not inside
    print('    %-4s m = %.8f   inside bounds = %s   %s'
          % (idx, mt, inside, 'EXCLUDED (> %s)' % ia.SCALE_M_BOUNDS[1] if not inside else 'admissible'))
print('    A time-scaled z_open would require m above SCALE_M_BOUNDS, so compute_scale would have')
print('    returned SCALE_INVALID and direction would be UNAVAILABLE - not the observed NEUTRAL/WEAK.')
print()

print('T4  compute_direction run with the solved m')
t4 = True
for idx, lg, rp, re_, zo, md, mt in rows:
    p = PUB[idx]
    # ER: solve the 09:25 spot that reproduces the published ER on a 3-poll path.
    # net = |S-A|; path = |s1-A| + |S-s1|; ER = net/path.  For a single reversal this is
    # one equation in one unknown.  NOT unique: more polls admit other solutions.
    net = abs(p['S'] - p['A'])
    path = net / p['er']
    overshoot = (path - net) / 2.0
    s1 = p['A'] + overshoot if p['S'] < p['A'] else p['A'] - overshoot
    pts = [(9, 20, p['A']), (9, 25, s1), (9, 30, p['S'])]
    polls = series_polls(D, idx, pts)
    scale = {'status': 'OK', 'value': md, 'expiry': None, 'k0': None, 'ce_mid': None,
             'pe_mid': None, 'forward': None, 'n_sessions': None,
             'observed_at': A_DT.isoformat(), 'method': 'solved_from_published_z_open'}
    r = ia.compute_direction(idx, polls, SESSION, T_DT, scale, receipt_dt=T_DT)
    hit_v = r['value'] == 'NEUTRAL' and r['reason_codes'] == ['WEAK']
    hit_zo = round(r['z_open'], 3) == p['z_open']
    hit_z30 = round(r['z_30'], 3) == p['z_30']
    hit_er = r['er'] is not None and round(r['er'], 3) == round(p['er'], 3)
    t4 &= hit_v and hit_zo and hit_z30 and hit_er
    print('    %-4s solved 09:25 spot = %.2f (reconstructed, NOT unique)' % (idx, s1))
    print('         value=%s reasons=%s        -> matches NEUTRAL/WEAK : %s'
          % (r['value'], r['reason_codes'], hit_v))
    print('         z_open=%+.6f -> %+.3f  published %+.3f  match=%s'
          % (r['z_open'], round(r['z_open'], 3), p['z_open'], hit_zo))
    print('         z_30  =%+.6f -> %+.3f  published %+.3f  match=%s'
          % (r['z_30'], round(r['z_30'], 3), p['z_30'], hit_z30))
    print('         er    = %.6f -> %.3f   published %.3f   match=%s'
          % (r['er'], round(r['er'], 3), p['er'], hit_er))
    print('         anchor clamped_to_open_ref=%s  delta=%s min'
          % (r['inputs']['anchor_30']['clamped_to_open_ref'], r['inputs']['anchor_30']['minutes']))
print()

banner('D3 verdict')
print('T1  published (z_open, z_30) self-consistent under deployed forms : %s' % t1b)
print('T2  solved m inside SCALE_M_BOUNDS                                : %s' % t2)
print('T3  time-scaled reading of z_open EXCLUDED by SCALE_M_BOUNDS      : %s' % t3)
print('T4  compute_direction reproduces label, z_open, z_30 and ER       : %s' % t4)
print()
print('Labels: A, S, z_open, z_30, ER, direction  = phone-verified (handoff s46-54, Codex')
print('        reproduced all 78 polls at b524).   m, the 09:25 spot = SOLVED/RECONSTRUCTED')
print('        here, not read from a record.  The 09:25 spot is one of several paths that')
print('        give the published ER and is not claimed as the observed value.')
sys.exit(0 if (t1b and t2 and t3 and t4) else 1)
