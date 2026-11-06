"""D2 - per-index isolation (handoff s78: "Perturb BNF alone and verify NF output isolation").

Two separate claims, because the first attempt at this test was vacuous:

  A. Perturbing BNF's path SHAPE leaves NF's output byte-identical, and BNF responds.
  B. Scaling BNF's whole series by a constant changes NOTHING, for either index.

(B) is not a failed perturbation - it is a real design property.  z_open and z_30 are
log ratios against a reference drawn from the same series, so a uniform rescaling
cancels.  A test that rescales uniformly therefore proves nothing about isolation,
which is why (A) changes the shape instead.
"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _ia import Cal, at, banner, flat_chain, ia, series_polls  # noqa: E402

D = date(2026, 11, 4)
SESSION = D.isoformat()
EXPIRY = date(2026, 11, 9)
DECISION = at(D, 11, 0)
OBSERVED = at(D, 9, 20)

TIMES = [(9, 20), (9, 25), (9, 30), (9, 35), (9, 45), (10, 0), (10, 30), (11, 0)]
NF_BASE = [24000.0, 24010.0, 24005.0, 24015.0, 24008.0, 24012.0, 24006.0, 24010.0]
BNF_BASE = [56000.0, 56020.0, 56010.0, 56030.0, 56015.0, 56025.0, 56012.0, 56020.0]
# a SHAPE change: a strong directional leg, not a rescaling
BNF_SHAPED = [56000.0, 56120.0, 56260.0, 56400.0, 56560.0, 56700.0, 56840.0, 56980.0]
RESCALE = 0.95


def polls(nf, bnf):
    """One combined poll list carrying both indices, as the real payload does."""
    out = []
    for (hh, mm), a, b in zip(TIMES, nf, bnf):
        out.append({'poll_ts': at(D, hh, mm).isoformat(), 'nfSpot': a, 'bnfSpot': b})
    return out


def scale_for(index, spot):
    step = 50 if index == 'NF' else 100
    return ia.compute_scale(index, flat_chain(spot, step, atm_straddle_frac=0.0085),
                            spot, SESSION, OBSERVED, EXPIRY, Cal())


SC = {'NF': scale_for('NF', NF_BASE[0]), 'BNF': scale_for('BNF', BNF_BASE[0])}


def run(index, p):
    r = ia.compute_direction(index, p, SESSION, DECISION, SC[index], receipt_dt=DECISION)
    return {k: r.get(k) for k in ('value', 'strength', 'z_open', 'z_30', 'er', 'er_reason', 'reason_codes')}


def fingerprint(d):
    return json.dumps(d, sort_keys=True)


banner('D2  per-index isolation   (pinned 3ffe72bd, index_authority_v1)')
print('scale NF  status=%s m=%s' % (SC['NF']['status'], SC['NF']['value']))
print('scale BNF status=%s m=%s' % (SC['BNF']['status'], SC['BNF']['value']))
print()

base = polls(NF_BASE, BNF_BASE)
base_nf, base_bnf = run('NF', base), run('BNF', base)

# ---- A: BNF shape change -----------------------------------------------------------
shaped = polls(NF_BASE, BNF_SHAPED)
a_nf, a_bnf = run('NF', shaped), run('BNF', shaped)

print('A. BNF path SHAPE changed (chop -> steady +1.75%% leg), NF untouched')
print('   NF  before : %s' % fingerprint(base_nf))
print('   NF  after  : %s' % fingerprint(a_nf))
print('   BNF before : %s' % fingerprint(base_bnf))
print('   BNF after  : %s' % fingerprint(a_bnf))
nf_identical = fingerprint(base_nf) == fingerprint(a_nf)
bnf_responded = fingerprint(base_bnf) != fingerprint(a_bnf)
print('   NF output byte-identical          : %s' % nf_identical)
print('   BNF output responded              : %s   (%s/%s -> %s/%s)'
      % (bnf_responded, base_bnf['value'], base_bnf['strength'], a_bnf['value'], a_bnf['strength']))
print('   BNF z_open                        : %s -> %s' % (base_bnf['z_open'], a_bnf['z_open']))
print()

# ---- B: BNF uniform rescaling ------------------------------------------------------
rescaled = polls(NF_BASE, [x * RESCALE for x in BNF_BASE])
b_nf, b_bnf = run('NF', rescaled), run('BNF', rescaled)

print('B. BNF series multiplied by %.2f (uniform rescale, no shape change)' % RESCALE)
print('   NF  output unchanged              : %s' % (fingerprint(base_nf) == fingerprint(b_nf)))
print('   BNF output unchanged              : %s' % (fingerprint(base_bnf) == fingerprint(b_bnf)))
print('   BNF z_open                        : %s -> %s' % (base_bnf['z_open'], b_bnf['z_open']))
print('   -> invariance to uniform rescaling, NOT a failed perturbation.')
print()

banner('D2 verdict')
print('isolation holds (NF identical under a BNF shape change) : %s' % nf_identical)
print('perturbation was real (BNF responded)                   : %s' % bnf_responded)
print('direction is scale-invariant in the index level          : %s'
      % (fingerprint(base_bnf) == fingerprint(b_bnf)))
sys.exit(0 if (nf_identical and bnf_responded) else 1)
