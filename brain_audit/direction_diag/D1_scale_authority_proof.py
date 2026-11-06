"""D1 - the option/calendar scale is a hard single point of failure for direction.

Claim under test: compute_direction returns UNAVAILABLE for the whole remainder of a
session whenever compute_scale's single open-reference observation is unusable, for any
of its three failure reasons, regardless of how good the spot path is.

Each case below feeds the SAME healthy 09:20-11:00 spot path and changes only the
scale input.  Executed, not reasoned.
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _ia import Cal, at, banner, broken_chain, flat_chain, ia, series_polls, show  # noqa: E402

D = date(2026, 11, 4)          # a Wednesday; NF weekly expiry is not this day
GOOD_EXPIRY = date(2026, 11, 9)
SESSION = D.isoformat()

# one healthy path, shared by every case: a steady rise of ~0.9% off the open reference
PATH = [(9, 20, 24000.0), (9, 25, 24030.0), (9, 30, 24065.0), (9, 35, 24100.0),
        (9, 45, 24140.0), (10, 0, 24175.0), (10, 30, 24205.0), (11, 0, 24220.0)]
POLLS = series_polls(D, 'NF', PATH)
DECISION = at(D, 11, 0)
OBSERVED = at(D, 9, 20)        # the scale's single observation time


def direction_for(scale):
    return ia.compute_direction('NF', POLLS, SESSION, DECISION, scale, receipt_dt=DECISION)


banner('D1  scale failure -> direction UNAVAILABLE   (pinned 3ffe72bd, index_authority_v1)')
print('session=%s  decision=%s  polls=%d  path=+%.2f%% off open ref'
      % (SESSION, DECISION.isoformat(), len(POLLS),
         100.0 * (PATH[-1][2] / PATH[0][2] - 1.0)))
print()

results = {}

# --- case 1: everything valid -------------------------------------------------------
s1 = ia.compute_scale('NF', flat_chain(24000.0, 50), 24000.0, SESSION, OBSERVED, GOOD_EXPIRY, Cal())
print('case 1  healthy chain + calendar')
print('        scale status=%s reason=%s m=%s n=%s k0=%s'
      % (s1['status'], s1['reason'], s1['value'], s1['n_sessions'], s1['k0']))
results['1_scale_ok'] = show('        direction', direction_for(s1))
print()

# --- case 2: the index's own expiry day ---------------------------------------------
# expiry == session date; the collector holds only the front expiry, so there is no chain
s2 = ia.compute_scale('NF', flat_chain(24000.0, 50), 24000.0, SESSION, OBSERVED, D, Cal())
print('case 2  expiry == session date (own expiry day, D9)')
print('        scale status=%s reason=%s m=%s' % (s2['status'], s2['reason'], s2['value']))
results['2_expiry_day'] = show('        direction', direction_for(s2))
print()

# --- case 3: calendar does not cover the contract -----------------------------------
s3 = ia.compute_scale('NF', flat_chain(24000.0, 50), 24000.0, SESSION, OBSERVED,
                      GOOD_EXPIRY, Cal(covered_years=(2024,)))
print('case 3  calendar year not covered')
print('        scale status=%s reason=%s' % (s3['status'], s3['reason']))
results['3_calendar'] = show('        direction', direction_for(s3))
print()

# --- case 4: no strike passes the executable-quote parity gate ----------------------
s4 = ia.compute_scale('NF', broken_chain(24000.0, 50), 24000.0, SESSION, OBSERVED, GOOD_EXPIRY, Cal())
print('case 4  no valid parity pair (every leg fails 0<bid<=ask)')
print('        scale status=%s reason=%s' % (s4['status'], s4['reason']))
results['4_no_parity'] = show('        direction', direction_for(s4))
print()

# --- case 5: m outside SCALE_M_BOUNDS -----------------------------------------------
s5 = ia.compute_scale('NF', flat_chain(24000.0, 50, atm_straddle_frac=0.40), 24000.0,
                      SESSION, OBSERVED, GOOD_EXPIRY, Cal())
print('case 5  m outside SCALE_M_BOUNDS %s' % (ia.SCALE_M_BOUNDS,))
print('        scale status=%s reason=%s m=%s' % (s5['status'], s5['reason'], s5['value']))
results['5_m_bounds'] = show('        direction', direction_for(s5))
print()

banner('D1 verdict')
ok = results['1_scale_ok']['value'] != 'UNAVAILABLE'
failed = {k: v for k, v in results.items() if k != '1_scale_ok'}
all_unavail = all(v['value'] == 'UNAVAILABLE' for v in failed.values())
print('healthy scale yields a direction        : %s  (%s)' % (ok, results['1_scale_ok']['value']))
print('every scale failure yields UNAVAILABLE  : %s' % all_unavail)
print('identical spot path in all five cases   : True')
print()
print('CONCLUSION: the spot path is never consulted once the scale is unusable.  The scale is')
print('observed ONCE, in the %s-%s window, and frozen; a single unusable observation'
      % ('%02d:%02d' % ia.OPEN_REF_WINDOW[0][:2], '%02d:%02d' % ia.OPEN_REF_WINDOW[1][:2]))
print('removes direction for that index for the rest of the session.')
for k, v in sorted(results.items()):
    print('  %-14s %-12s %s' % (k, v['value'], v['reason_codes']))
