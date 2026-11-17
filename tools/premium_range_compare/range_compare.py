#!/usr/bin/env python3
"""Compare an IV-only premium range with a historical premium range around ONE common centre.

WHAT THIS ANSWERS.  At entry we can state a central premium and a range around it.  The centre
is not in question: the entry bid/ask midpoint is observable and every variant here uses it.
The question is whether the RANGE built from the contract's own entry implied volatility is
improved on by a range built from what the underlying actually did at this time of day in prior
sessions.  Primary horizon 60 minutes; 15, 30 and 120 reported separately and never used to
pick the primary.

WHY A COMMON CENTRE.  An earlier 120-minute NIFTY comparison had the IV-only range scoring
WORSE than the frozen R7 model on CRPS (31.69 against 30.84) while its nominal 80% interval
covered only 41.4% of outcomes.  Both numbers came from the same cause: the distribution was
not centred on the observable entry price, so CRPS was paying for a location error and the
interval was in the wrong place.  Location and shape are separate questions, and mixing them
makes neither answerable.  Here every distribution is recentred so its MEDIAN equals the
observed entry midpoint, the pre-centring median is reported, and MAE is reported to CONFIRM
the resulting tie in the central comparison -- if MAE does not tie, the centring is broken and
the CRPS comparison is not about shape.

THE VARIANTS, all on the same accepted rows:

  FLAT       a single point at the entry midpoint.  No range.  The floor any range must beat.
  IV_ONLY    the contract's own entry IV, fitted from ITS midpoint under the parity forward,
             over a FIXED quantile grid declared before any run.
  HIST       the same contract repriced under historical forward scenarios, after subtracting
             the median historical return so the empirical drift cannot do the centring's job.
  R7_E0_FWD  the frozen R7 primary, as a REFERENCE.  Not recentred, not comparable on CRPS to
             the two above, and reported so this runner can be checked against the unchanged
             frozen replay.

EVERY VARIANT'S OWN COVERAGE IS REPORTED BESIDE THE COMMON SET.  A model that fails on hard
rows and is scored only on easy ones will show a better mean on its own rows; the headline
comparison therefore uses the COMMON scored rows, and the per-variant counts are printed so
that substitution is visible rather than silent.

WHAT IS REUSED, NOT REIMPLEMENTED.  `premium_repricing_v2.py` at the frozen R7 commit supplies
the pricer (`bs`, `bounds`), the clock (`tte`), the parity forward (`implied_forwards`), the
entry and exit selection rules (`select_entry`, `select_exit`), the pinned-contract resolver
and the scorer (`score`).  Nothing in that file is modified.  The admission pipeline (conflict
grouping, identity, two-sided book, envelope agreement) is NOT importable from it -- it lives
inside `score_index` -- so it is mirrored here and `tests/test_range_compare.py` asserts that
the mirror selects exactly the same (contract, minute, entry acquisition, exit acquisition)
rows as the frozen replay at 60 minutes.  If the mirror ever drifts, that test fails.

PROXIES AND LIMITS, unchanged from R7 and restated because they bound every number below.
Exchange quote event time is UNVERIFIED: the export carries the app's own acquisition receipt,
which bounds when a price COULD be known and never how old it was.  Fitted IV is a model
inverse of a midpoint, not the broker's own convention -- the quoted IV, the fitted IV and any
failure reason are kept apart and the mismatch between them is reported, not reconciled.
"""
import argparse
import collections
import datetime
import json
import math
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# HERE is inserted explicitly so the documented `python3 -I` invocation works: isolated mode
# drops the script's own directory from sys.path, so `import planned_schedule` fails without
# it. `CliSmoke` in the tests runs the isolated command itself rather than trusting this.
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'spot_path_baseline'))

import premium_repricing_v2 as R7          # noqa: E402  the frozen R7 module, unmodified
import planned_schedule as PS              # noqa: E402

COMPARISON_VERSION = 'premium_neutral_range_comparison_v2_planned_coverage'
COMPARISON_VERSION_V1 = 'premium_neutral_range_comparison_v1'
R7_CONTRACT_VERSION = R7.CONTRACT_VERSION           # read, never set

# ── declared before any run ───────────────────────────────────────────────────────────────────

PRIMARY_HORIZON_MIN = 60
HORIZONS_MIN = (15, 30, 60, 120)
HORIZON_TOL_S = R7.HORIZON_TOL_S                    # the frozen +/-5 s exit window
MIN_TRAIN = R7.MIN_TRAIN                            # the frozen minimum prior sessions

#: The quantile grid, fixed.  Midpoints of N equal probability intervals, so no grid point is
#: at p=0 or p=1 and no scenario needs an infinite z.  N is odd so the grid's own median is a
#: grid point, which makes the recentring exact rather than interpolated.
#:
#: WHY 201 AND NOT A SMALLER GRID.  Measured on 12 November (`--grid-sensitivity`), mean CRPS
#: is insensitive to N -- NIFTY at 120 minutes moves from 31.732 at N=21 to 31.686 at N=401, a
#: 0.15% spread -- but the NOMINAL 80% COVERAGE is not: it runs 35.6%, 37.4%, 38.5%, 39.1%,
#: 41.4%, 42.0%, 42.0% at N = 21, 31, 41, 51, 101, 201, 401.  A six-point swing is larger than
#: the distance between a passing and a failing calibration gate, so a protocol that gates on
#: coverage MUST pin the grid or the gate decides on an implementation detail.  201 is the
#: smallest grid at which coverage is converged (unchanged to 401) and CRPS is converged to
#: four significant figures.  `width80` still creeps up slowly with N, because the 10th and
#: 90th percentiles of a discrete grid approach the continuous quantiles from inside; it is
#: therefore reported WITH the grid pinned and never compared across grids.
#:
#: The historical range is unaffected by any of this: its scenarios are the historical sample
#: itself, so every HIST number above is identical at every N.
QUANTILE_GRID_N = 201
QUANTILE_GRID = tuple((i + 0.5) / QUANTILE_GRID_N for i in range(QUANTILE_GRID_N))

#: IV fit bracket and tolerance.  Black is strictly increasing in sigma wherever vega > 0, so
#: bisection on this bracket is sound; a target outside the bracket's own price range is
#: reported unsolvable rather than clamped to an endpoint.
IV_MIN, IV_MAX = 1e-6, 5.0
IV_PRICE_TOL = 1e-10
IV_MAX_ITERS = 200

VARIANTS = ('FLAT', 'IV_ONLY', 'HIST', 'R7_E0_FWD', 'IV_ONLY_RAW', 'HIST_RAW')
RANGE_VARIANTS = ('IV_ONLY', 'HIST')               #: the two the primary question is about
RECENTRED = ('IV_ONLY', 'HIST')                    #: FLAT is already the centre; R7 is a reference

#: The same two ranges WITHOUT the recentring, carried as diagnostics so the effect of the
#: recentring is measured on the same rows rather than argued about. They are never the
#: primary comparison: a distribution that is not on the observable centre is answering a
#: different question. `IV_ONLY_RAW` is what the 12 November 120-minute counterexample was
#: computed from, and keeping it is how that counterexample stays reproducible.
DIAGNOSTIC_UNCENTRED = ('IV_ONLY_RAW', 'HIST_RAW')

#: A session enters the trial cohort only if its usable coverage against the PLANNED schedule
#: reaches this, per index at the primary horizon.  Proposed, not frozen.  The denominator is
#: the schedule (`planned_schedule.py`), never the admitted quotes -- counting against admitted
#: quotes let a capture truncated at 12:00 report 62.5% and pass a 60% gate when its honest
#: figure was 35.1%.
MIN_USABLE_COVERAGE = 0.60

#: How a historical observation's minute is turned into the slot a join reads.
SLOT_POLICIES = ('exact', 'floor5', 'explicit')
DEFAULT_SLOT_POLICY = 'exact'                      #: what the frozen replay does

# ── the normal inverse CDF ────────────────────────────────────────────────────────────────────

_A = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
      1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
_B = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
      6.680131188771972e+01, -1.328068155288572e+01)
_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
      -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
      3.754408661907416e+00)


def norm_ppf(p):
    """Inverse standard normal CDF (Acklam), refined by one Halley step on `R7.nd`.

    The refinement is what makes this safe to pin a protocol to: the rational approximation
    alone is good to ~1e-9 relative, and one Halley step against the SAME normal CDF the pricer
    uses drives it to full double precision.  Checked against known quantiles in the tests.
    """
    if not (0.0 < p < 1.0):
        raise ValueError('norm_ppf needs 0 < p < 1, got %r' % (p,))
    pl = 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        x = (((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / \
            ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1)
    elif p <= 1 - pl:
        q = p - 0.5
        r = q * q
        x = (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5]) * q / \
            (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1)
    else:
        q = math.sqrt(-2 * math.log(1 - p))
        x = -(((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / \
            ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1)
    e = R7.nd(x) - p
    u = e * math.sqrt(2 * math.pi) * math.exp(x * x / 2)
    return x - u / (1 + x * u / 2)


GRID_Z = tuple(norm_ppf(p) for p in QUANTILE_GRID)

# ── entry implied volatility, fitted from the contract's own midpoint ─────────────────────────


def fit_iv(mid, F, K, T, kind):
    """(iv, reason) -- the sigma under which `R7.bs` reproduces this midpoint, or why not.

    The quoted IV is NOT used here and is never substituted for a failed fit.  Three named
    failures, each counted and none of them clamped to a bracket endpoint:

      `iv_target_below_floor`    the midpoint is at or below the price at IV_MIN, i.e. at or
                                 below intrinsic.  No positive volatility produces it.
      `iv_target_above_ceiling`  the midpoint is at or above the price at IV_MAX.  A fit would
                                 be a floor/ceiling artefact, not a measurement.
      `iv_no_convergence`        the bracket did not close.  Not expected, reported if seen.

    The first two are the real quote-bound checks the handoff asks for, and they are the cases
    where a model-to-quote mismatch shows up as an unsolvable IV rather than a silent number.
    """
    if not (F > 0 and K > 0 and T > 0 and mid is not None and math.isfinite(mid)):
        return None, 'iv_inputs_invalid'
    lo_px = R7.bs(F, K, T, IV_MIN, kind)
    hi_px = R7.bs(F, K, T, IV_MAX, kind)
    if mid <= lo_px + IV_PRICE_TOL:
        return None, 'iv_target_below_floor'
    if mid >= hi_px - IV_PRICE_TOL:
        return None, 'iv_target_above_ceiling'
    lo, hi = IV_MIN, IV_MAX
    for _ in range(IV_MAX_ITERS):
        mid_sig = 0.5 * (lo + hi)
        px = R7.bs(F, K, T, mid_sig, kind)
        if abs(px - mid) <= IV_PRICE_TOL or hi - lo < 1e-12:
            return mid_sig, None
        if px < mid:
            lo = mid_sig
        else:
            hi = mid_sig
    return None, 'iv_no_convergence'


# ── scenarios ─────────────────────────────────────────────────────────────────────────────────


def iv_only_forwards(F0, sigma, years):
    """Terminal forwards under the fitted entry IV, on the fixed quantile grid.

    Risk-neutral drift on the forward is zero, so the scenario is the lognormal
    F0 * exp(-sigma^2 t / 2 + sigma sqrt(t) z) at the grid's z values.  The -sigma^2 t / 2 term
    makes the MEAN of the scenario forwards F0; the median is below it, which is one reason the
    recentring below is applied to the PRICE distribution and not to the forwards.
    """
    if not (F0 > 0 and sigma > 0 and years > 0):
        return []
    v = sigma * math.sqrt(years)
    return [F0 * math.exp(-0.5 * v * v + v * z) for z in GRID_Z]


def hist_returns(hist, hist_dates, index_name, m0, m1, slot_policy, reasons=None):
    """Log returns over (m0 -> m1) in prior sessions, and the join's own accounting.

    `slot_policy` decides what minute a historical observation answers to:

      `exact`     the observation's own minute, which is what the frozen replay reads.
      `floor5`    the minute floored to a five-minute slot.  A reproducible SENSITIVITY, and
                  labelled as inferred in the manifest -- not a scheduled slot.
      `explicit`  the source's own scheduled-slot field.  Fails closed with a named reason when
                  the source does not carry one, because an inferred slot must never be
                  presented as a scheduled one.

    A delayed, duplicate or missing endpoint is counted, never interpolated.
    """
    out, acc = [], collections.Counter()
    for d in hist_dates:
        day = hist.get(d) or {}
        s0, s1 = day.get(m0), day.get(m1)
        if s0 is None:
            acc['hist_missing_entry_endpoint'] += 1
            continue
        if s1 is None:
            acc['hist_missing_exit_endpoint'] += 1
            continue
        p0, p1 = s0.get(index_name), s1.get(index_name)
        if not (isinstance(p0, (int, float)) and isinstance(p1, (int, float))
                and p0 > 0 and p1 > 0):
            acc['hist_non_positive_price'] += 1
            continue
        r = math.log(p1 / p0)
        if not math.isfinite(r):
            acc['hist_non_finite_return'] += 1
            continue
        out.append(r)
        acc['hist_pairs_used'] += 1
    if reasons is not None:
        reasons.update(acc)
    return out, acc


def hist_forwards(F0, returns):
    """Terminal forwards from historical returns, after subtracting their MEDIAN.

    Subtracting the median is what makes this a RANGE model rather than a drift model: whatever
    the underlying's median move was at this time of day, it is removed, so the only thing the
    historical sample contributes is the SHAPE of the dispersion around the centre.  Without
    it, a sample with a persistent drift would appear to beat IV-only by relocating the
    distribution -- which is a different claim, and not the one being tested.
    """
    if not returns or not (F0 > 0):
        return []
    med = statistics.median(returns)
    return [F0 * math.exp(r - med) for r in returns]


def reprice(forwards, K, T, sigma, kind):
    """(prices, accounting) -- the scenario forwards repriced, with bound handling COUNTED.

    `R7.bs` already bounds its output, so an out-of-bound price cannot leave this function; the
    point of the accounting is that it cannot leave SILENTLY either.  A non-finite or
    non-positive scenario forward is dropped with a reason rather than priced.
    """
    px, acc = [], collections.Counter()
    for F in forwards:
        if not (isinstance(F, (int, float)) and math.isfinite(F) and F > 0):
            acc['scenario_forward_invalid'] += 1
            continue
        raw = R7.bs(F, K, T, sigma, kind)
        if not math.isfinite(raw):
            acc['scenario_price_non_finite'] += 1
            continue
        lo = max(0.0, (F - K) if kind == 'CE' else (K - F))
        hi = F if kind == 'CE' else K
        if raw <= lo + 1e-12:
            acc['scenario_price_at_lower_bound'] += 1
        if raw >= hi - 1e-12:
            acc['scenario_price_at_upper_bound'] += 1
        px.append(raw)
    return px, acc


def recentre(prices, centre, K, kind, underlying_cap=None):
    """Shift a price distribution so its MEDIAN is `centre`, and report what that cost.

    Returns (prices, info).  `info` carries the pre-centring median, the shift, the number of
    scenario prices the shift pushed outside the contract's own no-arbitrage bounds (which are
    then clipped), and the post-centring median.

    WHAT THE CLIP CAN AND CANNOT DO.  The shift puts the median exactly on `centre`; clipping
    at zero then touches only values BELOW that median, and a median is unmoved by anything
    strictly on one side of it.  So whenever `centre` lies inside the contract's own
    no-arbitrage bounds -- which a two-sided quote midpoint does unless the book is crossed --
    **the centring is exact however much tail is clipped**.  Exactness can only be lost if
    `centre` itself is outside those bounds.  `centred_exactly` states which case this was and
    `clipped` counts the scenarios either way, so a variant cannot gain from a clip that is
    never mentioned, and the guarantee the comparison rests on is checked rather than assumed
    (`Recentring.test_the_only_way_to_lose_exactness_is_a_centre_outside_the_bounds`).
    """
    info = {'pre_centre_median': None, 'shift': None, 'clipped': 0,
            'post_centre_median': None, 'centred_exactly': None}
    if not prices:
        return [], info
    pre = statistics.median(prices)
    shift = centre - pre
    info['pre_centre_median'] = pre
    info['shift'] = shift
    cap = underlying_cap if (kind == 'CE' and underlying_cap is not None) else (
        None if kind == 'CE' else K)
    out = []
    for p in prices:
        v = p + shift
        lo = 0.0
        clipped = False
        if v < lo:
            v, clipped = lo, True
        if cap is not None and v > cap:
            v, clipped = cap, True
        if clipped:
            info['clipped'] += 1
        out.append(v)
    post = statistics.median(out)
    info['post_centre_median'] = post
    info['centred_exactly'] = abs(post - centre) <= 1e-9
    return out, info


# ── admission: a mirror of the frozen replay's own pipeline ───────────────────────────────────


def admit(ex, ix):
    """(quotes, pinned, forwards_by_acquisition, entries, by_contract, coverage).

    A FAITHFUL MIRROR of the admission block inside `R7.score_index`, which is not importable
    on its own.  Every rule and every counter name is the frozen one: envelopes grouped by
    acquisition id before admission so a conflict excludes the whole id; quotes grouped by
    (contract, acquisition) and excluded entirely when they disagree; identity verified with no
    disagreements; a two-sided book with bid > 0 and ask >= bid; the envelope's slot equal to
    the record's.

    `tests/test_range_compare.py::test_admission_matches_the_frozen_replay` asserts the rows
    this produces are exactly the frozen replay's rows at 60 minutes, so the duplication is
    checked by the original rather than trusted.
    """
    cov = collections.Counter()
    p = ((ex.get('premium') or {}).get('indices') or {}).get(ix) or {}
    if not p.get('records'):
        return {}, {}, {}, [], {}, cov

    pins, pin_path, pin_note = R7.resolve_pinned(p)
    pc = {c['instrument_key']: c for c in pins}
    cov['pinned_contracts'] = len(pins)

    env_raw = collections.defaultdict(list)
    for e in p.get('slot_envelopes') or []:
        aid = e.get('acquisition_id')
        if aid is None:
            cov['rej_envelope_without_acquisition_id'] += 1
            continue
        env_raw[aid].append(e)

    env, recv, excluded_acq = {}, {}, {}
    for aid in sorted(env_raw, key=str):
        group = env_raw[aid]
        if len({R7.envelope_signature(e) for e in group}) > 1:
            cov['rej_conflicting_envelope_acquisition_id'] += 1
            excluded_acq[aid] = 'conflicting_envelope'
            continue
        if len(group) > 1:
            cov['collapsed_identical_envelope_repeats'] += len(group) - 1
        e = group[0]
        if not e.get('slot'):
            cov['rej_envelope_without_slot'] += 1
            excluded_acq[aid] = 'envelope_without_slot'
            continue
        ms = (e.get('clocks') or {}).get('acquisition_response_ms')
        if not ms:
            cov['rej_envelope_without_receipt_clock'] += 1
            excluded_acq[aid] = 'envelope_without_receipt_clock'
            continue
        env[aid] = e
        recv[aid] = datetime.datetime.fromtimestamp(ms / 1000.0, tz=R7.IST)

    rec_raw = collections.defaultdict(list)
    for r in p['records']:
        rec_raw[(r.get('instrument_key'), r.get('acquisition_id'))].append(r)

    q = {}
    for key in sorted(rec_raw, key=lambda z: (str(z[0]), str(z[1]))):
        group = rec_raw[key]
        if len({R7.quote_signature(r) for r in group}) > 1:
            cov['rej_conflicting_duplicate_contract_acquisition'] += 1
            continue
        if len(group) > 1:
            cov['collapsed_identical_quote_repeats'] += len(group) - 1
        r = group[0]
        aid = r.get('acquisition_id')
        if aid in excluded_acq:
            cov['rej_quote_depends_on_excluded_acquisition'] += 1
            continue
        e = env.get(aid)
        if e is None:
            cov['rej_envelope_unresolved'] += 1
            continue
        if e.get('slot') != r.get('slot'):
            cov['rej_envelope_slot_mismatch'] += 1
            continue
        if r['instrument_key'] not in pc:
            cov['rej_no_contract_identity'] += 1
            continue
        b = (r['bbo'].get('bid_raw') or {}).get('raw')
        k = (r['bbo'].get('ask_raw') or {}).get('raw')
        idn = r.get('identity') or {}
        if idn.get('identity_verified') is not True:
            cov['rej_identity_unverified'] += 1
            continue
        if idn.get('identity_disagreements'):
            cov['rej_identity_disagreement'] += 1
            continue
        if not (isinstance(b, (int, float)) and isinstance(k, (int, float))
                and b > 0 and k >= b):
            cov['rej_book_not_two_sided'] += 1
            continue
        iv = ((r.get('iv') or {}).get('iv_greeks_raw') or {})
        q[key] = {'ikey': r['instrument_key'], 'aid': aid, 'slot': R7.mins(r['slot']),
                  'bid': b, 'ask': k, 'mid': (b + k) / 2.0, 'cutoff': recv[aid],
                  'quoted_iv': iv.get('raw') if iv.get('status') == 'PRESENT_VALID' else None,
                  'event_clock_verified': bool(
                      (e.get('clocks') or {}).get('quote_event_clock_verified'))}
        cov['usable_records'] += 1

    cov['clock_event_time_verified'] = sum(1 for v in q.values() if v['event_clock_verified'])
    cov['clock_receipt_time_present'] = len(q)
    cov['acquisitions'] = len({v['aid'] for v in q.values()})

    fwd_by_acq = {}
    for aid in sorted({v['aid'] for v in q.values()}):
        g = R7.implied_forwards(q, pc, aid, ix, cov)
        if g:
            fwd_by_acq[aid] = g

    by_cm = collections.defaultdict(list)
    for key in sorted(q):
        v = q[key]
        by_cm[(v['ikey'], v['slot'])].append(v)
    entries = []
    for cm in sorted(by_cm):
        cands = by_cm[cm]
        if len(cands) > 1:
            cov['contract_minutes_with_multiple_acquisitions'] += 1
        entries.append(R7.select_entry(cands))

    by_contract = collections.defaultdict(list)
    for key in sorted(q):
        by_contract[q[key]['ikey']].append(q[key])

    cov['pinned_resolved_at'] = pin_path
    if pin_note:
        cov['pinned_note'] = pin_note
    return q, pc, fwd_by_acq, entries, by_contract, cov


# ── one index, one horizon ───────────────────────────────────────────────────────────────────


def compare_index_horizon(ex, ix, hist, hist_dates, horizon_min, slot_policy=DEFAULT_SLOT_POLICY,
                          session_date=None, calendar=None):
    """Score every variant for one index at one horizon.  Returns (summary, rows, coverage).

    THE COVERAGE LEDGER IS BUILT FROM INTENDED OPPORTUNITIES, not from what happened to score.
    An intended opportunity is one (pinned contract, nominal capture minute) entry candidate --
    known from the capture schedule and the pinned universe BEFORE any later price is read.  A
    row with no admissible exit quote has no computable CRPS and still counts in the intended
    denominator, so a session that simply stopped capturing cannot look like a session that
    scored.
    """
    node = ((ex.get('premium') or {}).get('indices') or {}).get(ix) or {}
    session_date = session_date or ((ex.get('manifest') or {}).get('session_date_used')
                                   or (ex.get('manifest') or {}).get('session_date_requested'))
    plan = PS.plan_for(node, ix, horizon_min, session_date, calendar)
    q, pc, fwd_by_acq, entries, by_contract, cov = admit(ex, ix)
    cov['planned_opportunities'] = plan['planned_opportunities']
    cov['schedule_state'] = plan['state']
    cov['schedule_basis'] = plan['basis']
    # kept under its old name as well, because it is what the ADMITTED quotes offered -- but it
    # is no longer the denominator, and the two are reported side by side on purpose
    cov['admitted_entry_candidates'] = len(entries)
    if not entries:
        # THE SAME KEYS AS THE NORMAL PATH.  An early return that omits fields the scored path
        # provides is a defect in its own right: a caller written against the normal shape
        # raises KeyError on an export that happens to carry no premium data, which is a real
        # state (`NO_PREMIUM_DATA_FOR_THIS_INDEX`) and not an error. Found by pointing the
        # suite at a third real export that had none.
        rec = PS.reconcile(plan, (), ())
        return ({'state': 'NO_ENTRY_CANDIDATES', 'horizon_min': horizon_min,
                 'index': ix, 'slot_policy': slot_policy,
                 'schedule': _plan_view(plan),
                 'planned_opportunities': plan['planned_opportunities'],
                 'reconciliation': rec,
                 'usable_coverage': rec['usable_coverage'],
                 'admitted_entry_candidates': 0,
                 'matched_exit': 0, 'common_scored': 0,
                 'coverage': dict(cov), 'iv_fit_failures': {},
                 'variants': {}, 'variants_common': {}}, [], cov)

    observed_keys, scored_keys, outside_keys = set(), set(), set()
    last_planned_min = (R7.mins(plan['eligible_entry_slots'][-1])
                        if plan['eligible_entry_slots'] else None)
    rows = []
    iv_fit_failures = collections.Counter()
    quoted_vs_fitted = []

    for e in sorted(entries, key=lambda z: (z['slot'], str(z['ikey']), str(z['aid']))):
        ikey, cutoff = e['ikey'], e['cutoff']
        c = pc[ikey]
        K, kind = float(c['strike']), c['option_type']
        target = cutoff + datetime.timedelta(minutes=horizon_min)
        observed_keys.add((str(ikey), e['slot']))
        if last_planned_min is not None and e['slot'] > last_planned_min:
            # admitted, but its planned target falls past the end of the planned grid: this is
            # evidence about the capture, not a score the schedule ever expected
            outside_keys.add((str(ikey), e['slot']))
        row = {'ikey': ikey, 'm0': e['slot'], 'strike': K, 'kind': kind,
               'expiry': c['expiry'], 'entry_aid': e['aid'], 'entry_mid': e['mid'],
               'entry_bid': e['bid'], 'entry_ask': e['ask'],
               'quoted_iv': e['quoted_iv'], 'fitted_iv': None, 'iv_failure': None,
               'event_clock_verified': e['event_clock_verified'], 'scores': {}, 'centring': {}}

        cands = [z for z in by_contract[ikey] if z is not e]
        x, dev = R7.select_exit(cands, target, HORIZON_TOL_S)
        if x is None:
            cov['rej_no_exit_quote_within_tolerance'] += 1
            row['excluded'] = 'no_exit_quote_within_tolerance'
            row['nearest_exit_deviation_s'] = dev
            rows.append(row)
            continue
        cov['matched_exit'] += 1
        row['exit_aid'] = x['aid']
        row['exit_deviation_s'] = dev
        row['realised_mid'] = x['mid']
        row['exit_bid'] = x['bid']
        realised = x['mid']

        T0 = R7.tte(cutoff, c['expiry'])
        T1 = R7.tte(target, c['expiry'])
        if T1 <= 0:
            cov['rej_target_at_or_after_expiry'] += 1
            row['excluded'] = 'target_at_or_after_expiry'
            rows.append(row)
            continue
        row['tte_entry_years'], row['tte_target_years'] = T0, T1
        row['days_to_expiry'] = T0 * 365.0

        # ── the common centre: the observable entry midpoint, for every variant ──
        row['scores']['FLAT'] = R7.score([e['mid']], realised)
        cov['FLAT_scored'] += 1

        g = (fwd_by_acq.get(e['aid']) or {}).get(R7.group_key(c, ix))
        if g is None:
            cov['rej_no_forward_in_entry_acquisition_group'] += 1
            row['excluded_from_ranges'] = 'no_forward_in_entry_acquisition_group'
            rows.append(row)
            continue
        F0 = g['F']
        row['forward'] = F0
        row['forward_dispersion'] = g['disp']
        cov['usable_forward'] += 1

        sigma, why = fit_iv(e['mid'], F0, K, T0, kind)
        row['fitted_iv'] = sigma
        row['iv_failure'] = why
        if sigma is None:
            iv_fit_failures[why] += 1
            cov['rej_%s' % why] += 1
            row['excluded_from_ranges'] = why
            rows.append(row)
            continue
        cov['usable_fitted_iv'] += 1
        if e['quoted_iv'] is not None:
            quoted_vs_fitted.append(sigma * 100.0 - float(e['quoted_iv']))

        # ── IV_ONLY ──
        # The scenario's own variance horizon is the HOLD, entry to target, which on an
        # ACT/365 calendar is T0 - T1 years. Using T1 (time left to expiry at the target)
        # here would diffuse the forward over the wrong interval entirely.
        hold_years = max(T0 - T1, 0.0)
        row['hold_years'] = hold_years
        iv_px, iv_acc = reprice(iv_only_forwards(F0, sigma, hold_years), K, T1, sigma, kind)
        cov.update({('iv_%s' % k): v for k, v in iv_acc.items()})
        if iv_px:
            row['scores']['IV_ONLY_RAW'] = R7.score(iv_px, realised)
            cov['IV_ONLY_RAW_scored'] += 1
            iv_px, info = recentre(iv_px, e['mid'], K, kind, underlying_cap=F0)
            row['centring']['IV_ONLY'] = info
            row['scores']['IV_ONLY'] = R7.score(iv_px, realised)
            cov['IV_ONLY_scored'] += 1
            if info['clipped']:
                cov['IV_ONLY_rows_with_clipped_scenarios'] += 1
            if not info['centred_exactly']:
                cov['IV_ONLY_rows_not_centred_exactly'] += 1
        else:
            cov['rej_iv_only_no_valid_scenarios'] += 1

        # ── HIST ──
        m0 = e['slot']
        m1 = m0 + horizon_min
        base, hacc = hist_returns(hist, hist_dates, ix, m0, m1, slot_policy, cov)
        if len(base) < MIN_TRAIN:
            cov['rej_insufficient_prior_sessions'] += 1
            row['excluded_from_hist'] = 'insufficient_prior_sessions'
        else:
            cov['usable_history'] += 1
            row['hist_pairs'] = len(base)
            row['hist_median_return'] = statistics.median(base)
            h_px, h_acc = reprice(hist_forwards(F0, base), K, T1, sigma, kind)
            cov.update({('hist_%s' % k): v for k, v in h_acc.items()})
            if h_px:
                row['scores']['HIST_RAW'] = R7.score(h_px, realised)
                cov['HIST_RAW_scored'] += 1
                h_px, info = recentre(h_px, e['mid'], K, kind, underlying_cap=F0)
                row['centring']['HIST'] = info
                row['scores']['HIST'] = R7.score(h_px, realised)
                cov['HIST_scored'] += 1
                if info['clipped']:
                    cov['HIST_rows_with_clipped_scenarios'] += 1
                if not info['centred_exactly']:
                    cov['HIST_rows_not_centred_exactly'] += 1
            else:
                cov['rej_hist_no_valid_scenarios'] += 1

            # ── R7_E0_FWD, the frozen reference: quoted IV, raw returns, NOT recentred ──
            if e['quoted_iv'] is not None:
                qs = float(e['quoted_iv']) / 100.0
                ref = [R7.bs(F0 * math.exp(r), K, T1, qs, kind) for r in base]
                row['scores']['R7_E0_FWD'] = R7.score(ref, realised)
                cov['R7_E0_FWD_scored'] += 1
            else:
                cov['rej_r7_reference_quoted_iv_absent'] += 1

        rows.append(row)

    scored = [r for r in rows if r['scores']]
    common = [r for r in scored if all(v in r['scores'] for v in ('FLAT',) + RANGE_VARIANTS)]
    cov['common_scored'] = len(common)
    for r in common:
        scored_keys.add((str(r['ikey']), r['m0']))

    rec = PS.reconcile(plan, observed_keys, scored_keys, outside_keys)
    cov['usable_coverage_against_plan'] = rec['usable_coverage']
    cov['missing_entry_keys'] = rec['missing_entry_keys']

    summary = {'horizon_min': horizon_min, 'index': ix,
               'slot_policy': slot_policy,
               'schedule': _plan_view(plan),
               'planned_opportunities': plan['planned_opportunities'],
               'reconciliation': rec,
               'usable_coverage': rec['usable_coverage'],
               'admitted_entry_candidates': len(entries),
               'matched_exit': cov['matched_exit'],
               'common_scored': len(common),
               'coverage': dict(cov),
               'iv_fit_failures': dict(iv_fit_failures),
               'variants': {}, 'variants_common': {}}

    if quoted_vs_fitted:
        summary['quoted_minus_fitted_iv_points'] = {
            'n': len(quoted_vs_fitted),
            'median': statistics.median([-d for d in quoted_vs_fitted]),
            'min': min(-d for d in quoted_vs_fitted),
            'max': max(-d for d in quoted_vs_fitted)}

    for v in VARIANTS:
        own = [r['scores'][v] for r in scored if v in r['scores']]
        if own:
            summary['variants'][v] = _agg(own)
        comm = [r['scores'][v] for r in common if v in r['scores']]
        if comm:
            summary['variants_common'][v] = _agg(comm)
    return summary, rows, cov


def _plan_view(plan):
    """The plan as it appears in a result: everything but the key set itself."""
    return {k: v for k, v in plan.items() if k != 'planned_keys'}


def _agg(scores):
    return {'n': len(scores),
            'crps': statistics.fmean(s['crps'] for s in scores),
            'mae': statistics.fmean(s['mae'] for s in scores),
            'cov80': statistics.fmean(s['cov80'] for s in scores),
            'width80': statistics.fmean(s['width80'] for s in scores)}


# ── history loading, with the slot policy stated ──────────────────────────────────────────────

def load_history(path, slot_policy=DEFAULT_SLOT_POLICY):
    """(hist, meta) -- prior-session spot observations, resolved DETERMINISTICALLY by slot.

    THE DEFECT THIS CLOSES.  v1 wrote `hist[date][slot] = row` as it read, so when two
    observations landed on one slot the survivor was whichever happened to come last.  Codex
    supplied 10:55 and 10:56 prices that both floor to the 10:55 slot and reversed the input:
    the retained NIFTY price changed from 25099.00 to 25000.00, which is order dependence in
    the denominator of every historical scenario -- the exact defect class the frozen replay
    spent four review rounds removing from the export side.

    THE RULE, applied in this order, and the same in either input order:

      1. rows are GROUPED by (date, slot) before anything is admitted;
      2. a group whose rows agree on every field scoring can read collapses to one
         observation, counted as `collapsed_identical_duplicates`;
      3. a group that DISAGREES is EXCLUDED whole -- not resolved by position, not averaged --
         counted as `excluded_conflicting_slots` and surfaced in coverage.  We cannot tell
         which row is authoritative, so neither is used;
      4. within a group, a row carrying a VERIFIED scheduled slot is preferred over one whose
         slot was inferred, and that preference is recorded.  A verified and an inferred row
         that agree on price still collapse; if they disagree the slot is excluded.

    Each retained observation keeps its own provenance: the observed clock, the scheduled slot
    when the source supplies one, and which of the two the join key came from.
    """
    import hashlib
    if slot_policy not in SLOT_POLICIES:
        raise ValueError('slot policy must be one of %s' % (SLOT_POLICIES,))
    raw = open(path, 'rb').read()
    meta = {'path': os.path.basename(path), 'sha256': hashlib.sha256(raw).hexdigest(),
            'slot_policy': slot_policy, 'rows': 0, 'rows_with_explicit_slot': 0,
            'rows_off_five_minute_grid': 0, 'floor_collisions': 0,
            'slot_field_names_seen': [], 'dates': 0,
            'collapsed_identical_duplicates': 0, 'excluded_conflicting_slots': 0,
            'conflicting_slot_examples': [], 'slots_from_verified_schedule': 0,
            'slots_from_inferred_clock': 0}
    rows = []
    for line in raw.decode('utf-8').splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    meta['rows'] = len(rows)

    slot_fields = ('scheduled_slot', 'slot', 'sched_slot', 'scheduled_t')
    seen = sorted({f for r in rows for f in slot_fields if r.get(f) is not None})
    meta['slot_field_names_seen'] = seen

    def explicit_slot(r):
        for f in slot_fields:
            if r.get(f) is not None:
                return R7.mins(r[f]), f
        return None, None

    meta['rows_with_explicit_slot'] = sum(
        1 for r in rows if explicit_slot(r)[0] is not None)

    on_grid = set()
    for r in rows:
        m = R7.mins(r['t'])
        if m % 5 == 0:
            on_grid.add((r['d'], m))
    for r in rows:
        m = R7.mins(r['t'])
        if m % 5:
            meta['rows_off_five_minute_grid'] += 1
            if (r['d'], m - m % 5) in on_grid:
                meta['floor_collisions'] += 1

    if slot_policy == 'explicit' and meta['rows_with_explicit_slot'] != len(rows):
        raise ValueError(
            'slot policy "explicit" requires a scheduled-slot field on every row; this source '
            'carries one on %d of %d rows (fields seen: %s). An inferred slot must not be '
            'presented as a scheduled one -- use "exact" or "floor5" and the manifest will '
            'label it inferred.'
            % (meta['rows_with_explicit_slot'], len(rows), seen or 'none'))

    # ── group by (date, slot) BEFORE admitting anything ──
    groups = collections.defaultdict(list)
    for r in rows:
        observed = R7.mins(r['t'])
        sched, sched_field = explicit_slot(r)
        if slot_policy == 'explicit':
            key_min, key_from = sched, 'verified_schedule'
        elif slot_policy == 'floor5':
            key_min, key_from = observed - observed % 5, 'inferred_floor5'
            if sched is not None:
                key_min, key_from = sched, 'verified_schedule'
        else:
            key_min, key_from = observed, 'observed_clock'
            if sched is not None:
                key_min, key_from = sched, 'verified_schedule'
        groups[(r['d'], key_min)].append({
            'nf': float(r['nf']), 'bnf': float(r['bnf']),
            'observed_min': observed, 'scheduled_min': sched,
            'scheduled_field': sched_field, 'slot_from': key_from})

    hist = collections.defaultdict(dict)
    for (d, m) in sorted(groups):
        g = groups[(d, m)]
        # the signature is every field a scenario can read; provenance is not part of it, so a
        # verified and an inferred row that agree on price collapse rather than conflict
        sig = {(round(o['nf'], 6), round(o['bnf'], 6)) for o in g}
        if len(sig) > 1:
            meta['excluded_conflicting_slots'] += 1
            if len(meta['conflicting_slot_examples']) < 10:
                meta['conflicting_slot_examples'].append(
                    {'date': d, 'slot': '%02d:%02d' % (m // 60, m % 60),
                     'distinct_prices': sorted(sig),
                     'observed_minutes': sorted(o['observed_min'] for o in g)})
            continue
        if len(g) > 1:
            meta['collapsed_identical_duplicates'] += len(g) - 1
        # prefer a verified scheduled slot deterministically; the rows agree on price, so this
        # only decides which provenance is recorded
        pick = min(g, key=lambda o: (0 if o['slot_from'] == 'verified_schedule' else 1,
                                     o['observed_min']))
        if pick['slot_from'] == 'verified_schedule':
            meta['slots_from_verified_schedule'] += 1
        else:
            meta['slots_from_inferred_clock'] += 1
        hist[d][m] = {'NF': pick['nf'], 'BNF': pick['bnf'],
                      'observed_min': pick['observed_min'],
                      'scheduled_min': pick['scheduled_min'],
                      'slot_from': pick['slot_from']}
    meta['dates'] = len(hist)
    meta['slot_key_collisions_overwritten'] = 0      # by construction: nothing is overwritten
    meta['retained_slots'] = sum(len(v) for v in hist.values())
    return hist, meta


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────

def run_session(export_path, spots_path, horizons=HORIZONS_MIN,
                slot_policy=DEFAULT_SLOT_POLICY, reverse_input=False):
    """One session's comparison, for every index and horizon."""
    import hashlib
    ex = json.load(open(export_path))
    if reverse_input:
        ex = R7.reverse_input(ex)
    session = ex['manifest']['session_date_used']
    hist, hmeta = load_history(spots_path, slot_policy)
    hist_dates = sorted(d for d in hist if d < session)
    calendar = PS.load_session_calendar()

    out = {'session': session,
           'session_calendar': {k: v for k, v in calendar.items() if k != 'holidays'},
           'export': {'path': os.path.basename(export_path),
                      'sha256': hashlib.sha256(open(export_path, 'rb').read()).hexdigest()},
           'history': hmeta,
           'historical_sessions': hist_dates,
           'prior_sessions_available': len(hist_dates),
           'indices': {}}
    for ix in sorted(((ex.get('premium') or {}).get('indices') or {})):
        out['indices'][ix] = {}
        for h in horizons:
            summary, _rows, _cov = compare_index_horizon(ex, ix, hist, hist_dates, h,
                                                         slot_policy, session, calendar)
            out['indices'][ix]['h%d' % h] = summary
    return out


def across_sessions(sessions, horizons=HORIZONS_MIN):
    """The SESSION-level aggregate: each session's mean first, then the mean of those.

    WHY SESSION MEANS AND NOT ROW MEANS.  Rows inside one session are not independent
    observations of a model's skill: neighbouring strikes and overlapping windows see the same
    few underlying moves, so a session with 300 matched rows would otherwise outvote a session
    with 30 for reasons that have nothing to do with which model is better.  The handoff's
    decision points are counted in SESSIONS, so the unit the decision rule reads has to be the
    session mean.  Per-session numbers are kept so a reader can see the spread rather than
    only its average, and the paired per-session sign count is reported because the primary
    question -- does the historical range add value OVER the IV-only range -- is a paired
    comparison on the same rows.
    """
    agg = {}
    for ix in sorted({i for s in sessions for i in s['indices']}):
        agg[ix] = {}
        for h in horizons:
            key = 'h%d' % h
            per = []
            for s in sessions:
                node = (s['indices'].get(ix) or {}).get(key)
                if not node:
                    continue
                v = node.get('variants_common') or {}
                if 'IV_ONLY' not in v or 'HIST' not in v:
                    continue
                per.append({'session': s['session'], 'n': node['common_scored'],
                            'planned': node.get('planned_opportunities'),
                            'usable_coverage': node.get('usable_coverage'),
                            'matched_exit': node['matched_exit'],
                            **{name: v[name] for name in v}})
            if not per:
                agg[ix][key] = {'sessions': 0}
                continue
            out = {'sessions': len(per), 'per_session': per, 'variants': {}}
            for name in VARIANTS:
                vals = [p[name] for p in per if name in p]
                if vals:
                    out['variants'][name] = {
                        'sessions': len(vals),
                        'crps': statistics.fmean(v['crps'] for v in vals),
                        'mae': statistics.fmean(v['mae'] for v in vals),
                        'cov80': statistics.fmean(v['cov80'] for v in vals),
                        'width80': statistics.fmean(v['width80'] for v in vals)}
            paired = [(p['IV_ONLY']['crps'], p['HIST']['crps']) for p in per
                      if 'IV_ONLY' in p and 'HIST' in p]
            if paired:
                rel = [(a - b) / a for a, b in paired if a > 0]
                out['paired_hist_vs_iv_only'] = {
                    'sessions': len(paired),
                    'sessions_hist_better': sum(1 for a, b in paired if b < a),
                    'mean_relative_crps_improvement': statistics.fmean(rel) if rel else None,
                    'improvement_sign': 'positive means the historical range is better'}
            agg[ix][key] = out
    return agg


def grid_sensitivity(export_path, spots_path, index_name, horizon_min,
                     grids=(21, 31, 41, 51, 101, 201, 401),
                     slot_policy=DEFAULT_SLOT_POLICY):
    """Re-score ONE index and horizon on several quantile grids.

    This exists because the calibration gate is grid-sensitive and the CRPS comparison is not,
    and that asymmetry has to be measurable from the runner rather than asserted in prose.
    Restores the declared grid before returning.
    """
    global QUANTILE_GRID_N, QUANTILE_GRID, GRID_Z
    keep = (QUANTILE_GRID_N, QUANTILE_GRID, GRID_Z)
    ex = json.load(open(export_path))
    session = ex['manifest']['session_date_used']
    hist, _ = load_history(spots_path, slot_policy)
    hist_dates = sorted(d for d in hist if d < session)
    rows = []
    try:
        for n in grids:
            QUANTILE_GRID_N = n
            QUANTILE_GRID = tuple((i + 0.5) / n for i in range(n))
            GRID_Z = tuple(norm_ppf(p) for p in QUANTILE_GRID)
            s, _r, _c = compare_index_horizon(ex, index_name, hist, hist_dates, horizon_min,
                                              slot_policy, session)
            v = (s.get('variants_common') or {})
            rows.append({'grid_n': n,
                         'IV_ONLY': v.get('IV_ONLY'), 'HIST': v.get('HIST'),
                         'IV_ONLY_RAW': v.get('IV_ONLY_RAW')})
    finally:
        QUANTILE_GRID_N, QUANTILE_GRID, GRID_Z = keep
    return {'index': index_name, 'horizon_min': horizon_min, 'session': session,
            'declared_grid_n': QUANTILE_GRID_N, 'rows': rows}


def resolve_inputs(export_paths, canonical=None):
    """Collapse the supplied exports into AT MOST ONE per session.  Returns (keep, excluded).

    THE DEFECT THIS CLOSES.  v1 built one session entry per supplied path, so passing the same
    file twice produced a two-session cohort out of one session's evidence -- and a decision
    rule counted in sessions would have reached its threshold twice as fast on duplicated
    input.

    THE RULE, applied before any score is computed:

      * identical content, by SHA-256, collapses to one session however many times it is
        supplied.  The repeat count is reported, not silently dropped;
      * two DIFFERENT files claiming the same session date are COMPETING EXPORTS.  Neither is
        chosen: the session is excluded as `CONFLICTING_EXPORTS_FOR_SESSION` and reported with
        both digests.  Picking the "better" one after scoring is exactly the selection this
        trial exists to prevent, and picking the larger or newer one is a guess about which
        capture is authoritative;
      * a conflict is resolved only by an EXPLICIT declaration -- `canonical={session: sha256}`,
        supplied on the command line BEFORE the run, which is recorded in the output. That
        makes the choice reviewable and dated rather than implicit.

    Excluded sessions are carried through reporting; they never vanish.
    """
    import hashlib
    canonical = dict(canonical or {})
    seen = []
    for path in export_paths:
        raw = open(path, 'rb').read()
        digest = hashlib.sha256(raw).hexdigest()
        doc = json.loads(raw.decode('utf-8'))
        session = ((doc.get('manifest') or {}).get('session_date_used')
                   or (doc.get('manifest') or {}).get('session_date_requested'))
        seen.append({'path': path, 'basename': os.path.basename(path),
                     'sha256': digest, 'session': session})

    by_session = collections.defaultdict(list)
    for it in seen:
        by_session[it['session']].append(it)

    keep, excluded = [], []
    for session in sorted(by_session, key=str):
        items = by_session[session]
        digests = sorted({it['sha256'] for it in items})
        if session is None:
            excluded.append({'session': None, 'reason': 'SESSION_DATE_NOT_DECLARED',
                             'inputs': [{'basename': i['basename'], 'sha256': i['sha256']}
                                        for i in items]})
            continue
        if len(digests) == 1:
            first = sorted(items, key=lambda i: i['basename'])[0]
            keep.append({'session': session, 'path': first['path'],
                         'basename': first['basename'], 'sha256': digests[0],
                         'duplicate_inputs_collapsed': len(items) - 1,
                         'supplied_paths': sorted(i['basename'] for i in items)})
            continue
        want = canonical.get(session)
        picked = [d for d in digests if want and d.startswith(want)]
        if len(picked) == 1:
            first = sorted((i for i in items if i['sha256'] == picked[0]),
                           key=lambda i: i['basename'])[0]
            keep.append({'session': session, 'path': first['path'],
                         'basename': first['basename'], 'sha256': picked[0],
                         'duplicate_inputs_collapsed': 0,
                         'canonical_declared': want,
                         'competing_digests_not_used': [d for d in digests if d != picked[0]],
                         'supplied_paths': sorted(i['basename'] for i in items)})
            continue
        excluded.append({
            'session': session, 'reason': 'CONFLICTING_EXPORTS_FOR_SESSION',
            'competing_digests': digests,
            'inputs': [{'basename': i['basename'], 'sha256': i['sha256']} for i in items],
            'resolution': 'declare one with --canonical %s=<sha256 prefix>; no export is '
                          'chosen by size, age or score' % session})
    return keep, excluded


def session_eligibility(session_result, horizon=PRIMARY_HORIZON_MIN,
                        min_coverage=MIN_USABLE_COVERAGE):
    """Per index: does this session enter the trial cohort?  (eligible, detail).

    Eligibility is decided on the PLANNED-schedule coverage at the primary horizon and on the
    schedule being verifiable at all.  A session that fails is reported with its ratio and its
    leading refusal reason, and is excluded WHOLE rather than partially counted.
    """
    out = {}
    for ix in sorted(session_result.get('indices') or {}):
        node = (session_result['indices'][ix] or {}).get('h%d' % horizon) or {}
        rec = node.get('reconciliation') or {}
        sched = (node.get('schedule') or {}).get('state')
        cov = rec.get('usable_coverage')
        reasons = []
        if sched not in PS.ELIGIBLE_STATES:
            reasons.append('schedule_%s' % str(sched).lower())
        if cov is None:
            diag = rec.get('diagnostic_coverage')
            reasons.append('no_usable_coverage_computed'
                           + ('' if diag is None else
                              ' (diagnostic coverage %.3f against the authoritative plan)'
                              % diag))
        elif cov < min_coverage:
            reasons.append('usable_coverage_%.3f_below_%.2f' % (cov, min_coverage))
        leading = None
        missing = rec.get('missing_entry_slots') or {}
        if missing:
            leading = 'missing_entry_slots=%d keys across %d slots' % (
                rec.get('missing_entry_keys', 0), len(missing))
        out[ix] = {'eligible': not reasons, 'usable_coverage': cov,
                   'diagnostic_coverage': rec.get('diagnostic_coverage'),
                   'planned_opportunities': rec.get('planned_opportunities'),
                   'scored_in_plan': rec.get('scored_in_plan'),
                   'schedule_state': sched, 'reasons': reasons,
                   'leading_shortfall': leading,
                   'manifest_state': (rec.get('manifest_audit') or {}).get('state'),
                   'manifest_reasons': (rec.get('manifest_audit') or {}).get('reasons'),
                   'min_usable_coverage': min_coverage}
    return out


def run(export_paths, spots_path, horizons=HORIZONS_MIN, slot_policy=DEFAULT_SLOT_POLICY,
        reverse_input=False, canonical=None, min_coverage=MIN_USABLE_COVERAGE):
    if isinstance(export_paths, str):
        export_paths = [export_paths]
    keep, excluded = resolve_inputs(export_paths, canonical)
    sessions = []
    for it in keep:
        res = run_session(it['path'], spots_path, horizons, slot_policy, reverse_input)
        res['input'] = {k: v for k, v in it.items() if k != 'path'}
        res['eligibility'] = session_eligibility(res, PRIMARY_HORIZON_MIN, min_coverage)
        sessions.append(res)

    eligible_by_index = collections.defaultdict(list)
    for res in sessions:
        for ix, e in (res.get('eligibility') or {}).items():
            if e['eligible']:
                eligible_by_index[ix].append(res)
    cohort_excluded = [
        {'session': res['session'], 'index': ix, 'reason': 'NOT_ELIGIBLE',
         'detail': e}
        for res in sessions for ix, e in sorted((res.get('eligibility') or {}).items())
        if not e['eligible']]

    out = {'comparison_version': COMPARISON_VERSION,
           'r7_contract_version': R7_CONTRACT_VERSION,
           'schedule_version': PS.SCHEDULE_VERSION,
           'capture_window_rules_version': PS.CAPTURE_WINDOW_RULES_VERSION,
           'session_calendar_version': PS.SESSION_CALENDAR_VERSION,
           'primary_horizon_min': PRIMARY_HORIZON_MIN,
           'horizons_min': list(horizons),
           'horizon_tolerance_s': HORIZON_TOL_S,
           'quantile_grid_n': QUANTILE_GRID_N,
           'min_prior_sessions': MIN_TRAIN,
           'min_usable_coverage': min_coverage,
           'input_order_reversed': bool(reverse_input),
           'centre': 'the observed entry bid/ask midpoint, identical for every variant',
           'recentred_variants': list(RECENTRED),
           'uncentred_diagnostics': list(DIAGNOSTIC_UNCENTRED),
           'reference_variant_not_recentred': 'R7_E0_FWD',
           'event_age': 'UNKNOWN -- no verified exchange quote event clock',
           'coverage_denominator': 'the AUTHORITATIVE capture-window rules and session '
                                   'calendar; never the export\'s counts, rows or quotes',
           'aggregation': 'rows -> session mean per index -> mean across ELIGIBLE sessions',
           'inputs_supplied': len(export_paths),
           'inputs_kept': [{k: v for k, v in it.items() if k != 'path'} for it in keep],
           'inputs_excluded': excluded,
           'canonical_declared': dict(canonical or {}),
           'sessions': sessions,
           'cohort_excluded': cohort_excluded,
           'across_sessions': {}}
    for ix, group in sorted(eligible_by_index.items()):
        agg = across_sessions(group, horizons)
        out['across_sessions'][ix] = agg.get(ix, {'sessions': 0})
    if len(sessions) == 1:
        # the single-session shape earlier runs read, kept so an existing reader does not break
        out['session'] = sessions[0]['session']
        out['export'] = sessions[0]['export']
        out['history'] = sessions[0]['history']
        out['historical_sessions'] = sessions[0]['historical_sessions']
        out['indices'] = sessions[0]['indices']
    return out


def _fmt(summary, label):
    rec = summary.get('reconciliation') or {}
    sched = summary.get('schedule') or {}
    cov = rec.get('usable_coverage')
    # `scored_in_plan` is deliberately None when the schedule is refused and the ratio is
    # WITHHELD, so every plan-relative number here is formatted through `num`. `%d` on None
    # raised TypeError and took the whole CLI down on an export that captured nothing.
    def num(v, width=4):
        return ('%*d' % (width, v)) if isinstance(v, int) else ('%*s' % (width, '-'))
    lines = ['  %-10s planned %s   admitted %s   matched exit %s   scored %s   usable %s'
             % (label, num(rec.get('planned_opportunities')),
                num(summary.get('admitted_entry_candidates')),
                num(summary.get('matched_exit')), num(rec.get('scored_in_plan')),
                ('%.1f%%' % (100 * cov)) if cov is not None
                else ('WITHHELD' if rec.get('schedule_state') not in (None, PS.STATE_VERIFIED)
                      else 'n/a'))]
    if sched.get('state') not in PS.ELIGIBLE_STATES:
        diag = rec.get('diagnostic_coverage')
        lines.append('             SCHEDULE %s%s'
                     % (sched.get('state'),
                        '' if diag is None
                        else '   diagnostic coverage %.1f%% of the authoritative plan'
                             % (100 * diag)))
        for why in ((rec.get('manifest_audit') or {}).get('reasons') or []):
            lines.append('               manifest: %s' % why)
    if rec.get('missing_entry_keys'):
        lines.append('             missing %d planned entry keys across %d slots; '
                     'admitted outside the planned window %d'
                     % (rec['missing_entry_keys'], len(rec.get('missing_entry_slots') or {}),
                        rec.get('admitted_entries_outside_planned_window', 0)))
    v = summary.get('variants_common') or {}
    if not v:
        lines.append('             no commonly scored row')
        return lines
    lines.append('             %-12s %9s %9s %8s %9s   %s'
                 % ('variant', 'CRPS', 'MAE', 'cov80', 'width80', 'own n'))
    for name in VARIANTS:
        if name not in v:
            continue
        own = (summary.get('variants') or {}).get(name) or {}
        lines.append('             %-12s %9.3f %9.3f %7.1f%% %9.3f   %d'
                     % (name, v[name]['crps'], v[name]['mae'], 100 * v[name]['cov80'],
                        v[name]['width80'], own.get('n', 0)))
    if 'IV_ONLY' in v and 'HIST' in v and v['IV_ONLY']['crps'] > 0:
        d = (v['IV_ONLY']['crps'] - v['HIST']['crps']) / v['IV_ONLY']['crps']
        lines.append('             historical range vs IV-only: %+.2f%% mean CRPS '
                     '(positive = historical better)' % (100 * d))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('exports', nargs='+', help='one or more session evidence exports')
    ap.add_argument('spots', help='the prior-session spot history (JSONL)')
    ap.add_argument('--out', help='directory for the machine-readable scores')
    ap.add_argument('--slot-policy', default=DEFAULT_SLOT_POLICY, choices=SLOT_POLICIES)
    ap.add_argument('--horizons', default=','.join(str(h) for h in HORIZONS_MIN))
    ap.add_argument('--reverse-input', action='store_true',
                    help='reverse record/envelope order; results must be identical')
    ap.add_argument('--grid-sensitivity', metavar='INDEX',
                    help='also re-score this index on several quantile grids')
    ap.add_argument('--canonical', action='append', default=[], metavar='SESSION=SHA256',
                    help='declare the canonical export for a session with competing files; '
                         'declared BEFORE the run and recorded in the output')
    ap.add_argument('--min-coverage', type=float, default=MIN_USABLE_COVERAGE,
                    help='usable-coverage floor against the PLANNED schedule (default %.2f)'
                         % MIN_USABLE_COVERAGE)
    ap.add_argument('--json-only', action='store_true')
    a = ap.parse_args(argv)
    horizons = tuple(int(x) for x in a.horizons.split(','))
    canonical = {}
    for spec in a.canonical:
        if '=' not in spec:
            ap.error('--canonical needs SESSION=SHA256, got %r' % spec)
        k, v = spec.split('=', 1)
        canonical[k.strip()] = v.strip()
    out = run(a.exports, a.spots, horizons, a.slot_policy, a.reverse_input, canonical,
              a.min_coverage)
    say = (lambda *x: None) if a.json_only else (lambda *x: print(*x))

    say('=' * 100)
    say('NEUTRAL PREMIUM RANGE COMPARISON   %s   (R7 pricing contract %s)'
        % (COMPARISON_VERSION, R7_CONTRACT_VERSION))
    say('=' * 100)
    say('sessions %s   quantile grid %d   min prior sessions %d   exit window +/-%.0f s'
        % (', '.join(s['session'] for s in out['sessions']) or 'none', QUANTILE_GRID_N,
           MIN_TRAIN, HORIZON_TOL_S))
    say('inputs supplied %d -> kept %d, excluded %d; coverage denominator: %s'
        % (out['inputs_supplied'], len(out['inputs_kept']), len(out['inputs_excluded']),
           out['coverage_denominator']))
    for it in out['inputs_kept']:
        if it.get('duplicate_inputs_collapsed'):
            say('  %s: %d identical duplicate input(s) collapsed'
                % (it['session'], it['duplicate_inputs_collapsed']))
        if it.get('canonical_declared'):
            say('  %s: canonical declared %s; %d competing export(s) not used'
                % (it['session'], it['canonical_declared'],
                   len(it.get('competing_digests_not_used') or [])))
    for ex_ in out['inputs_excluded']:
        say('  EXCLUDED %s: %s' % (ex_.get('session'), ex_['reason']))
        if ex_.get('competing_digests'):
            for dg in ex_['competing_digests']:
                say('      competing digest %s' % dg[:16])
        if ex_.get('resolution'):
            say('      %s' % ex_['resolution'])
    if not out['sessions']:
        say('')
        say('nothing to score: every supplied input was excluded above.')
        return 0
    h0 = out['sessions'][0]['history']
    say('history %s  %s  rows %d  slot policy %s  prior sessions used %s'
        % (h0['path'], h0['sha256'][:16], h0['rows'], h0['slot_policy'],
           ','.join(str(s['prior_sessions_available']) for s in out['sessions'])))
    say('centre: %s' % out['centre'])
    say('primary horizon %d min; %s reported separately and never used to pick the primary'
        % (PRIMARY_HORIZON_MIN,
           ', '.join('%d' % h for h in horizons if h != PRIMARY_HORIZON_MIN) or 'none'))
    if h0['rows_off_five_minute_grid']:
        say('history: %d of %d observation minutes are OFF the five-minute grid; flooring '
            'would collide with an existing on-grid observation %d time(s)'
            % (h0['rows_off_five_minute_grid'], h0['rows'], h0['floor_collisions']))
    if not h0['rows_with_explicit_slot']:
        say('history: NO explicit scheduled-slot field on any row (looked for %s) -- this '
            'join reads an INFERRED slot and is labelled as such'
            % ', '.join(('scheduled_slot', 'slot', 'sched_slot', 'scheduled_t')))

    for sess in out['sessions']:
        for ix in sorted(sess['indices']):
            say('')
            say('--- %s   session %s ---' % (ix, sess['session']))
            for h in horizons:
                s = sess['indices'][ix].get('h%d' % h) or {}
                tag = '%d min%s' % (h, '  PRIMARY' if h == PRIMARY_HORIZON_MIN else '')
                for ln in _fmt(s, tag):
                    say(ln)

    say('')
    say('--- session eligibility for the trial cohort (primary horizon, planned coverage) ---')
    for sess in out['sessions']:
        for ix, e in sorted((sess.get('eligibility') or {}).items()):
            say('  %-4s %s  %-8s coverage %s of planned %s  %s'
                % (ix, sess['session'], 'ELIGIBLE' if e['eligible'] else 'EXCLUDED',
                   ('%.1f%%' % (100 * e['usable_coverage']))
                   if e['usable_coverage'] is not None else 'n/a',
                   e['planned_opportunities'],
                   '' if e['eligible'] else '; '.join(e['reasons'])))
    if out['cohort_excluded']:
        say('  %d index/session pair(s) excluded from the cohort and carried here, not dropped'
            % len(out['cohort_excluded']))

    if len(out['sessions']) > 1:
        say('')
        say('--- across ELIGIBLE sessions (session means, then their mean) ---')
        for ix in sorted(out['across_sessions']):
            for h in horizons:
                if not isinstance(out['across_sessions'][ix], dict):
                    continue
                node = out['across_sessions'][ix].get('h%d' % h) or {}
                if not node.get('sessions'):
                    continue
                pv = node.get('paired_hist_vs_iv_only') or {}
                say('  %-4s %4d min  sessions %d   historical better in %d of %d   '
                    'mean relative CRPS improvement %s'
                    % (ix, h, node['sessions'], pv.get('sessions_hist_better', 0),
                       pv.get('sessions', 0),
                       ('%+.2f%%' % (100 * pv['mean_relative_crps_improvement']))
                       if pv.get('mean_relative_crps_improvement') is not None else 'n/a'))

    if a.grid_sensitivity:
        gs = grid_sensitivity(a.exports[0], a.spots, a.grid_sensitivity,
                              PRIMARY_HORIZON_MIN if PRIMARY_HORIZON_MIN in horizons
                              else horizons[0], slot_policy=a.slot_policy)
        out['grid_sensitivity'] = gs
        say('')
        say('--- quantile-grid sensitivity   %s   %d min   session %s ---'
            % (gs['index'], gs['horizon_min'], gs['session']))
        say('  %6s  %-28s  %-28s' % ('grid N', 'IV_ONLY crps/cov80/width80',
                                     'HIST crps/cov80/width80'))
        for r in gs['rows']:
            a_, b_ = r['IV_ONLY'], r['HIST']
            if not (a_ and b_):
                continue
            say('  %6d  %9.3f %7.1f%% %9.3f  %9.3f %7.1f%% %9.3f'
                % (r['grid_n'], a_['crps'], 100 * a_['cov80'], a_['width80'],
                   b_['crps'], 100 * b_['cov80'], b_['width80']))
        say('  the historical range is grid-INDEPENDENT by construction; the IV-only range\'s')
        say('  coverage is not, which is why the grid is pinned at %d in the protocol'
            % QUANTILE_GRID_N)

    say('')
    say('=' * 100)
    say('PROXIES: exchange quote event age is UNKNOWN. Fitted IV is a model inverse of a')
    say('midpoint, not the broker\'s convention; the quoted IV, the fitted IV and every fit')
    say('failure are reported apart. R7_E0_FWD is a REFERENCE: it uses the QUOTED IV and raw')
    say('historical returns and is NOT recentred, so its CRPS is not comparable to the two')
    say('recentred ranges. *_RAW are the same ranges WITHOUT recentring, diagnostics only.')
    say('Per-variant `own n` is printed beside the common set so a model cannot look better')
    say('by scoring fewer rows. MAE must TIE across FLAT, IV_ONLY and HIST: if it does not,')
    say('the common centre is broken and the CRPS comparison is not about shape.')
    say('=' * 100)

    if a.out:
        os.makedirs(a.out, exist_ok=True)
        path = os.path.join(a.out, 'range_comparison.json')
        with open(path, 'w') as f:
            json.dump(out, f, indent=1, sort_keys=True, default=str)
        say('wrote %s' % path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
