#!/usr/bin/env python3
"""The planned opportunity set, derived from AUTHORITATIVE rules the export cannot influence.

WHY THIS MODULE EXISTS, AND WHY IT WAS REWRITTEN TWICE.

v1 counted intended opportunities as the entry candidates left AFTER quote admission.  That
denominator was circular: a session that stopped capturing had fewer "intended" opportunities,
so its coverage ratio rose as its capture got worse.  A capture truncated at 12:00 reported
126/198 = 63.6% and passed a 60% gate against an honest 126/342 = 36.8%.

v2 derived the plan from the export's own `slot_manifest` and validated that manifest against
ITSELF.  Better, and still wrong, because self-consistency is cheap: crop the rows to 12:00 and
reset BOTH `expected_slots` and `observed_slots` to the surviving 33, and the manifest agrees
with itself perfectly.  v2 then reported `VERIFIED`, a 126-key plan, **126/126 = 100%** and
`eligible=true`, for a capture that had lost more than half its session.  A second route in:
a manifest row reading `15:99` parsed to minute 999, extended the grid past midnight and pushed
the 60-minute denominator from 342 to 414.  Codex found both.

**v3 takes the plan from rules the export cannot touch** -- the producing build's capture-window
rules and a versioned session calendar -- and then uses the manifest only as something to
VALIDATE AGAINST that plan.  The plan therefore does not move when the export's counts, rows or
quotes move, which is the property the whole coverage gate rests on.  Every source and version
used is recorded on the result.

WHAT IS AUTHORITATIVE, AND WHAT IS NOT

| input | role |
| --- | --- |
| `CAPTURE_WINDOW_RULES` (versioned, mirrored from `PremiumCaptureCohort`) | authoritative: the slot grid |
| `SESSION_CALENDAR` (versioned, holidays read from the app's own `brain.py`) | authoritative: whether this date is a regular session at all |
| `cohort.pinned_contracts` + `contracts_expected` | the contract IDENTITIES, cross-checked against each other |
| `slot_manifest` | **validated against** the authoritative grid; never the source of it |
| records, envelopes, quotes | never consulted here |

WHAT CANNOT BE DETERMINED IS SAID, NOT GUESSED.  A date outside the calendar's coverage, a
cohort that cannot be resolved, a pinned count that disagrees with the cohort, a horizon with no
room in the grid: each returns its own explicit INCOMPLETE state and the session is excluded.
A manifest that contradicts the authoritative grid is a NAMED MISMATCH: the plan is still known,
so diagnostic coverage is still reported, and the session is still excluded.
"""
import argparse
import collections
import datetime
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# HERE is inserted explicitly so `python3 -I` works: isolated mode drops the script's own
# directory from sys.path, so a sibling import fails without this.
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'spot_path_baseline'))

import premium_repricing_v2 as R7          # noqa: E402

SCHEDULE_VERSION = 'premium_range_planned_schedule_v3_authoritative_rules'
SCHEDULE_VERSION_V1 = 'premium_range_planned_schedule_v1'

# ── authoritative source 1: the producing build's capture-window rules ───────────────────────
#
# Mirrored from `PremiumCaptureCohort` in the app. Inclusive of the open, EXCLUSIVE of the
# close, five-minute steps, so the grid is 09:20..15:00 and holds 69 slots. The 12 November
# export's own manifest independently declares 69 expected slots on exactly this grid, and
# Codex's independent count of the 16 November plan (57 eligible entry slots at 60 minutes,
# x 6 pinned contracts = 342) agrees with it.
CAPTURE_WINDOW_RULES_VERSION = 'premium_capture_cohort_pilot_window_0920_1505_exclusive_v1'
CAPTURE_WINDOW_RULES_SOURCE = ('mirrored from PremiumCaptureCohort.WINDOW_OPEN_MIN / '
                               'WINDOW_CLOSE_MIN / the five-minute poll grid')
PILOT_WINDOW_OPEN_MIN = 9 * 60 + 20
PILOT_WINDOW_CLOSE_MIN = 15 * 60 + 5
SLOT_STEP_MIN = 5
PILOT_WINDOW_LABEL = '09:20 inclusive to 15:05 exclusive, %d-minute steps' % SLOT_STEP_MIN

# ── authoritative source 2: the session calendar ─────────────────────────────────────────────
#
# A session gets the regular grid only if the calendar can classify it as a regular trading
# session. Holidays come from the app's OWN list in `brain.py`, so this module cannot disagree
# with the application about which days traded; the file and its digest are recorded on every
# result. A date in a year the list does not cover is NOT assumed to be regular.
SESSION_CALENDAR_VERSION = 'nse_regular_session_calendar_v1_app_holidays'
BRAIN_PATH = os.path.join(HERE, '..', '..', 'app', 'src', 'main', 'python', 'brain.py')

#: Sessions known NOT to follow the regular window (a muhurat session, a shortened day). Each
#: would need its own declared grid before it could be planned; none is declared, so such a
#: session is refused rather than planned on the regular grid.
NON_REGULAR_SESSIONS = {}

BASIS_AUTHORITATIVE = 'authoritative_capture_rules_and_session_calendar'

STATE_VERIFIED = 'VERIFIED'
STATE_VERIFIED_NO_MANIFEST = 'VERIFIED_NO_MANIFEST_CROSS_CHECK'
STATE_INCOMPLETE_NO_COHORT = 'INCOMPLETE_COHORT_NOT_RESOLVED'
STATE_INCOMPLETE_CONTRACT_COUNT = 'INCOMPLETE_PINNED_COUNT_DISAGREES_WITH_COHORT'
STATE_INCOMPLETE_EMPTY_GRID = 'INCOMPLETE_NO_ELIGIBLE_SLOT_FOR_THIS_HORIZON'
STATE_INCOMPLETE_SESSION_UNKNOWN = 'INCOMPLETE_SESSION_NOT_IN_CALENDAR'
STATE_INCOMPLETE_SESSION_NOT_REGULAR = 'INCOMPLETE_SESSION_NOT_A_REGULAR_SESSION'
STATE_MANIFEST_MISMATCH = 'MANIFEST_DISAGREES_WITH_AUTHORITATIVE_SCHEDULE'
STATE_MANIFEST_INVALID = 'MANIFEST_PRESENT_BUT_INVALID'

#: States in which the plan is known and a coverage ratio is therefore computable, whether or
#: not the session is eligible.
PLAN_KNOWN = (STATE_VERIFIED, STATE_VERIFIED_NO_MANIFEST, STATE_MANIFEST_MISMATCH,
              STATE_MANIFEST_INVALID)
#: States in which a session may enter the trial cohort, coverage permitting.
ELIGIBLE_STATES = (STATE_VERIFIED, STATE_VERIFIED_NO_MANIFEST)

_HOLIDAY_BLOCK = re.compile(r"'NSE_HOLIDAYS'\s*:\s*\[(.*?)\]", re.S)
_DATE = re.compile(r"'(\d{4}-\d{2}-\d{2})'")


def load_session_calendar(brain_path=BRAIN_PATH):
    """The app's own holiday list, with its provenance.  Never a hardcoded copy.

    Returns {'holidays', 'covered_years', 'source', 'sha256', 'version'}.  Reading it from
    `brain.py` rather than restating it means this module cannot disagree with the application
    about which days traded, and the digest makes the version used auditable after the fact.
    """
    out = {'version': SESSION_CALENDAR_VERSION, 'source': None, 'sha256': None,
           'holidays': frozenset(), 'covered_years': ()}
    try:
        raw = open(brain_path, 'rb').read()
    except OSError as exc:
        out['error'] = 'cannot read the app holiday list: %s' % exc
        return out
    out['source'] = os.path.relpath(brain_path, os.path.join(HERE, '..', '..'))
    out['sha256'] = hashlib.sha256(raw).hexdigest()
    m = _HOLIDAY_BLOCK.search(raw.decode('utf-8', 'replace'))
    if not m:
        out['error'] = 'no NSE_HOLIDAYS block found in %s' % out['source']
        return out
    days = frozenset(_DATE.findall(m.group(1)))
    if not days:
        out['error'] = 'the NSE_HOLIDAYS block carries no dates'
        return out
    years = sorted({d[:4] for d in days})
    out['holidays'] = days
    out['covered_years'] = (years[0], years[-1])
    out['holiday_count'] = len(days)
    return out


def classify_session(session_date, calendar=None):
    """(is_regular, reason, calendar) for one session date.

    A weekend, a listed holiday, a date outside the calendar's covered years and a session
    declared non-regular are each distinguished, because they are different facts and a trial
    report should say which one applied.
    """
    cal = calendar or load_session_calendar()
    if cal.get('error'):
        return False, 'session_calendar_unavailable: %s' % cal['error'], cal
    try:
        y, m, d = (int(x) for x in str(session_date).split('-'))
        day = datetime.date(y, m, d)
    except (ValueError, TypeError, AttributeError):
        return False, 'session_date_unparseable: %r' % (session_date,), cal
    lo, hi = cal['covered_years']
    if not (lo <= '%04d' % day.year <= hi):
        return False, ('session %s is outside the calendar years %s-%s'
                       % (session_date, lo, hi)), cal
    if day.weekday() >= 5:
        return False, 'session %s is a weekend' % session_date, cal
    if str(session_date) in cal['holidays']:
        return False, 'session %s is an NSE holiday' % session_date, cal
    if str(session_date) in NON_REGULAR_SESSIONS:
        return False, ('session %s is declared non-regular (%s) and has no declared grid'
                       % (session_date, NON_REGULAR_SESSIONS[str(session_date)])), cal
    return True, None, cal


def window_grid(open_min=PILOT_WINDOW_OPEN_MIN, close_min=PILOT_WINDOW_CLOSE_MIN,
                step=SLOT_STEP_MIN):
    """Every slot of the authoritative capture grid.  Open inclusive, close exclusive."""
    return [m for m in range(open_min, close_min, step)]


def eligible_entry_slots(grid, horizon_min):
    """Slots whose PLANNED TARGET is also inside the grid.

    An entry whose target lies beyond the last planned slot was never an opportunity this
    schedule could have scored, so it does not belong in the denominator.  Entries that are
    admitted but whose target is outside the grid are counted SEPARATELY by `reconcile`,
    because they are evidence about the capture, not a missing score.
    """
    if not grid:
        return []
    last = grid[-1]
    return [m for m in grid if m + horizon_min <= last]


def hhmm(m):
    return '%02d:%02d' % (m // 60, m % 60)


def manifest_audit(index_node, authoritative_slots):
    """Validate the export's slot manifest AGAINST the authoritative grid.

    The manifest is never the source of the plan here; it is a claim about the plan, and this
    is where the claim is checked.  Returns an audit dict carrying `state` and `reasons`.

    Every check exists because of a way the previous version could be fooled:

      `MANIFEST_ABSENT`   no manifest at all.  Not an error: the plan is authoritative anyway,
                          so this is reported and the session stays eligible, with the state
                          saying no cross-check was possible.
      invalid             present but with no usable rows, or carrying a row whose slot is
                          unparseable or NOT IN the authoritative grid.  `15:99` parses to
                          minute 999 and used to extend the grid past midnight; a slot outside
                          the grid now invalidates the manifest instead of changing the plan.
      mismatch            the rows do not cover the authoritative grid, or `expected_slots`
                          disagrees with the authoritative grid size, or `observed_slots`
                          exceeds the rows present.  The 33-row crop with both totals reset to
                          33 is self-consistent and lands here, which is the point.
    """
    audit = {'present': False, 'rows': 0, 'distinct_valid_slots': 0, 'malformed_rows': 0,
             'duplicate_rows': 0, 'slots_outside_authoritative_grid': 0,
             'offending_slots': [], 'declared_expected': None, 'declared_observed': None,
             'authoritative_slots': len(authoritative_slots),
             'covers_authoritative_grid': False, 'state': 'MANIFEST_ABSENT', 'reasons': [],
             'slot_states': {}}
    sm = (index_node or {}).get('slot_manifest')
    if not isinstance(sm, dict):
        return audit
    audit['present'] = True
    audit['declared_expected'] = sm.get('expected_slots')
    audit['declared_observed'] = sm.get('observed_slots')
    rows = sm.get('rows')
    if not isinstance(rows, list):
        audit['state'] = STATE_MANIFEST_INVALID
        audit['reasons'].append('the manifest carries no row list')
        return audit
    audit['rows'] = len(rows)
    allowed = set(authoritative_slots)
    seen, states = set(), {}
    for r in rows:
        if not isinstance(r, dict) or not r.get('slot'):
            audit['malformed_rows'] += 1
            continue
        try:
            m = R7.mins(r['slot'])
        except (ValueError, AttributeError, TypeError):
            audit['malformed_rows'] += 1
            audit['offending_slots'].append(str(r.get('slot')))
            continue
        if m not in allowed:
            audit['slots_outside_authoritative_grid'] += 1
            audit['offending_slots'].append(str(r['slot']))
            continue
        if m in seen:
            audit['duplicate_rows'] += 1
            continue
        seen.add(m)
        states[m] = r.get('state') or 'STATE_ABSENT'
    audit['distinct_valid_slots'] = len(seen)
    audit['covers_authoritative_grid'] = (seen == allowed)
    audit['slot_states'] = {hhmm(m): states[m] for m in sorted(states)}
    audit['offending_slots'] = sorted(set(audit['offending_slots']))[:10]

    invalid, mismatch = [], []
    if not seen:
        invalid.append('present but carries no row with a usable slot in the authoritative '
                       'grid (%d row(s) supplied)' % len(rows))
    if audit['malformed_rows']:
        invalid.append('%d malformed row(s) with no usable slot' % audit['malformed_rows'])
    if audit['slots_outside_authoritative_grid']:
        invalid.append('%d row(s) name a slot outside the authoritative grid (%s)'
                       % (audit['slots_outside_authoritative_grid'],
                          ', '.join(audit['offending_slots']) or '?'))
    if audit['duplicate_rows']:
        invalid.append('%d duplicate slot row(s)' % audit['duplicate_rows'])

    if seen and not audit['covers_authoritative_grid']:
        mismatch.append('covers %d of the %d authoritative slots'
                        % (len(seen), len(allowed)))
    if isinstance(audit['declared_expected'], int) and \
            audit['declared_expected'] != len(allowed):
        mismatch.append('declares %d expected slots against the authoritative %d'
                        % (audit['declared_expected'], len(allowed)))
    if isinstance(audit['declared_observed'], int) and \
            audit['declared_observed'] > audit['distinct_valid_slots']:
        mismatch.append('declares %d observed slots, more than its %d valid rows'
                        % (audit['declared_observed'], audit['distinct_valid_slots']))

    if invalid:
        audit['state'] = STATE_MANIFEST_INVALID
        audit['reasons'] = invalid + mismatch
    elif mismatch:
        audit['state'] = STATE_MANIFEST_MISMATCH
        audit['reasons'] = mismatch
    else:
        audit['state'] = STATE_VERIFIED
    return audit


def plan_for(index_node, index_name, horizon_min, session_date=None, calendar=None):
    """The planned opportunity set for one index at one horizon.

    `session_date` is required for a VERIFIED plan: the calendar decides whether the regular
    grid applies at all, and a session it cannot classify is refused rather than planned.
    """
    out = {'schedule_version': SCHEDULE_VERSION, 'index': index_name,
           'horizon_min': horizon_min, 'session_date': session_date,
           'state': STATE_VERIFIED, 'basis': BASIS_AUTHORITATIVE,
           'capture_window_rules': {'version': CAPTURE_WINDOW_RULES_VERSION,
                                    'source': CAPTURE_WINDOW_RULES_SOURCE,
                                    'label': PILOT_WINDOW_LABEL},
           'planned_keys': frozenset(), 'planned_opportunities': 0,
           'grid_slots': 0, 'eligible_entry_slots': [], 'slots_outside_horizon': 0,
           'contracts': [], 'notes': [], 'manifest_audit': None}

    # ── the session calendar decides whether a regular grid applies ──
    is_regular, why, cal = classify_session(session_date, calendar)
    out['session_calendar'] = {k: v for k, v in cal.items() if k != 'holidays'}
    if not is_regular:
        out['state'] = (STATE_INCOMPLETE_SESSION_NOT_REGULAR
                        if str(session_date) in NON_REGULAR_SESSIONS
                        else STATE_INCOMPLETE_SESSION_UNKNOWN)
        out['notes'].append(why or 'the session could not be classified')
        return out

    # ── the contract identities, cross-checked ──
    try:
        pins, pin_path, pin_note = R7.resolve_pinned(index_node or {})
    except Exception as exc:
        out['state'] = STATE_INCOMPLETE_NO_COHORT
        out['notes'].append('pinned resolver refused: %s' % exc)
        return out
    if not pins:
        out['state'] = STATE_INCOMPLETE_NO_COHORT
        out['notes'].append('no pinned contracts could be resolved')
        return out
    keys = sorted(str(c['instrument_key']) for c in pins if c.get('instrument_key'))
    out['contracts'] = keys
    out['pinned_resolved_at'] = pin_path
    if pin_note:
        out['notes'].append(str(pin_note))
    expected = ((index_node or {}).get('cohort') or {}).get('contracts_expected')
    out['contracts_expected'] = expected
    if isinstance(expected, int) and expected != len(keys):
        out['state'] = STATE_INCOMPLETE_CONTRACT_COUNT
        out['notes'].append('cohort expects %d contracts, %d pinned' % (expected, len(keys)))
        return out

    # ── the authoritative grid, from the rules alone ──
    grid = window_grid()
    out['grid_slots'] = len(grid)
    slots = eligible_entry_slots(grid, horizon_min)
    out['slots_outside_horizon'] = len(grid) - len(slots)
    if not slots:
        out['state'] = STATE_INCOMPLETE_EMPTY_GRID
        out['notes'].append('no slot in a %d-slot grid leaves room for a %d-minute horizon'
                            % (len(grid), horizon_min))
        return out
    out['eligible_entry_slots'] = [hhmm(m) for m in slots]
    out['planned_keys'] = frozenset((k, m) for k in keys for m in slots)
    out['planned_opportunities'] = len(out['planned_keys'])

    # ── the manifest is validated against that grid, and cannot change it ──
    audit = manifest_audit(index_node, grid)
    out['manifest_audit'] = audit
    out['slot_states'] = audit['slot_states']
    if audit['state'] == 'MANIFEST_ABSENT':
        out['state'] = STATE_VERIFIED_NO_MANIFEST
        out['notes'].append('no slot manifest in this export, so the authoritative plan could '
                            'not be cross-checked against the producer\'s own record')
    elif audit['state'] != STATE_VERIFIED:
        out['state'] = audit['state']
        out['notes'].append('the slot manifest disagrees with the authoritative schedule: '
                            + '; '.join(audit['reasons']))
    return out


def reconcile(plan, observed_entry_keys, scored_keys, outside_window_keys=()):
    """Compare the plan with what was observed.  Every count here is a difference of SETS.

    The plan-relative numbers are reported whenever the PLAN IS KNOWN -- including when the
    manifest was refused -- because the authoritative denominator does not depend on the
    manifest.  That is what lets a refused session still report its diagnostic coverage
    (126/342 = 36.8%) instead of withholding the only number a reader wants.

    `usable_coverage` is the trial's ratio and is reported only for a state the session could
    be eligible in; `diagnostic_coverage` is the same arithmetic, always reported when the plan
    is known, and labelled so it is never read as an eligibility pass.
    """
    planned = plan['planned_keys']
    all_obs = frozenset(observed_entry_keys)
    all_scored = frozenset(scored_keys)
    obs = all_obs & planned
    scored = all_scored & planned
    missing_slots = collections.Counter()
    for _key, m in sorted(planned - obs):
        missing_slots[hhmm(m)] += 1
    state = plan['state']
    out = {
        'observed_entries_total': len(all_obs),
        'scored_total': len(all_scored),
        'admitted_entries_outside_planned_window': len(frozenset(outside_window_keys)),
        'planned_opportunities': len(planned),
        'schedule_state': state, 'schedule_basis': plan['basis'],
        'schedule_version': plan.get('schedule_version'),
        'capture_window_rules_version': (plan.get('capture_window_rules') or {}).get('version'),
        'session_calendar_version': (plan.get('session_calendar') or {}).get('version'),
        'manifest_audit': plan.get('manifest_audit')}
    if not planned:
        out.update({'observed_entries_in_plan': None, 'scored_in_plan': None,
                    'missing_entry_keys': None, 'missing_entry_slots': {},
                    'observed_but_not_scored': None,
                    'observed_entries_outside_plan': None,
                    'usable_coverage': None, 'diagnostic_coverage': None,
                    'coverage_basis': 'WITHHELD: the planned schedule is %s, so no denominator '
                                      'is defensible. The raw observed and scored totals above '
                                      'are preserved.' % state})
        return out
    ratio = len(scored) / len(planned)
    out.update({
        'observed_entries_in_plan': len(obs),
        'scored_in_plan': len(scored),
        'missing_entry_keys': len(planned - obs),
        'missing_entry_slots': dict(missing_slots),
        'observed_but_not_scored': len(obs - scored),
        'observed_entries_outside_plan': len(all_obs - planned),
        'diagnostic_coverage': ratio,
        'usable_coverage': ratio if state in ELIGIBLE_STATES else None,
        'coverage_basis': 'scored rows inside the plan, over planned opportunities; the '
                          'denominator comes from the authoritative capture rules and session '
                          'calendar, never from the export'})
    if state not in ELIGIBLE_STATES:
        out['coverage_basis'] = (
            'DIAGNOSTIC ONLY: the plan is authoritative and the ratio is %.1f%%, but the '
            'schedule state is %s so the session cannot enter the cohort.'
            % (100 * ratio, state))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('export')
    ap.add_argument('--horizons', default='15,30,60,120')
    a = ap.parse_args(argv)
    ex = json.load(open(a.export))
    session = ((ex.get('manifest') or {}).get('session_date_used')
               or (ex.get('manifest') or {}).get('session_date_requested'))
    cal = load_session_calendar()
    print('%s   %s   session %s' % (SCHEDULE_VERSION, os.path.basename(a.export), session))
    print('  capture rules  %s' % CAPTURE_WINDOW_RULES_VERSION)
    print('                 %s  (%s)' % (PILOT_WINDOW_LABEL, CAPTURE_WINDOW_RULES_SOURCE))
    print('  calendar       %s  source %s  sha256 %s  holidays %s'
          % (cal['version'], cal.get('source'), (cal.get('sha256') or '?')[:16],
             cal.get('holiday_count')))
    for ix in sorted(((ex.get('premium') or {}).get('indices') or {})):
        node = ex['premium']['indices'][ix]
        for h in (int(x) for x in a.horizons.split(',')):
            p = plan_for(node, ix, h, session, cal)
            print('  %-4s %4d min  %-45s grid %d  eligible %d  planned %d'
                  % (ix, h, p['state'], p['grid_slots'],
                     len(p['eligible_entry_slots']), p['planned_opportunities']))
            for n in p['notes']:
                print('        note: %s' % n)
    return 0


if __name__ == '__main__':
    sys.exit(main())
