#!/usr/bin/env python3
"""The planned opportunity set, derived before any realised quote is read.

WHY THIS MODULE EXISTS.  The first version of the runner counted intended opportunities as
`len(entries)` -- the entry candidates left AFTER quote admission.  That denominator is
circular: a session that stopped capturing has fewer "intended" opportunities, so its coverage
ratio rises as its capture gets worse.  Codex demonstrated it by truncating a 16 November
capture at 12:00; the runner reported 126/198 = 63.6% and would have passed a 60% gate, while
the honest figure against the planned schedule is 126/342 = 36.8%.  Reproduced on the 12
November export: truncated at 12:00 it reported 120/192 = 62.5% (PASS) against a true
120/342 = 35.1% (FAIL).

WHAT THE PLAN IS DERIVED FROM, in order of preference, with the basis always reported:

  1. `slot_manifest` in the index node -- the export's OWN declared schedule.  It carries
     `expected_slots`, one row per slot and a per-slot state, so a slot the schedule expected
     but never captured is distinguishable from one that was captured and refused.  On the 12
     November export this reads 69 expected slots, 53 observed for NIFTY and 54 for BANKNIFTY,
     with the remainder `NOT_OBSERVED_UNEXPLAINED` from 13:45 onward.
  2. the declared pilot capture window, when no manifest is present.  The constants are
     mirrored from `PremiumCaptureCohort` and the basis is reported as a FALLBACK, because a
     window that the export does not itself declare is an assumption about the build that
     produced it.

Either way the plan is the cross product of **eligible entry slots** and **pinned contract
identities**, and an entry slot is eligible for a horizon only if its PLANNED TARGET is also
inside the planned grid.  At 60 minutes on a 09:20-15:00 grid that is 09:20..14:00, which is 57
slots; against six pinned contracts, 342 planned opportunities -- the figure Codex derived
independently.

WHAT CANNOT BE VERIFIED IS SAID, NOT GUESSED.  If the cohort cannot be resolved, or the
manifest and the cohort disagree about how many contracts were pinned, or neither basis is
available, the plan is returned in an explicitly INCOMPLETE state and the trial summary must
refuse the session rather than score it against a denominator nobody can defend.
"""
import argparse
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# HERE is inserted explicitly so `python3 -I` works: isolated mode drops the script's own
# directory from sys.path, so a sibling import fails without this.
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'spot_path_baseline'))

import premium_repricing_v2 as R7          # noqa: E402

SCHEDULE_VERSION = 'premium_range_planned_schedule_v1'

#: Mirrored from `PremiumCaptureCohort` for the FALLBACK basis only.  Inclusive of the open,
#: EXCLUSIVE of the close, so the last capturable slot is 15:00.  Asserted against the 12
#: November export's own `slot_manifest`, which declares 69 expected slots on exactly this grid.
PILOT_WINDOW_OPEN_MIN = 9 * 60 + 20
PILOT_WINDOW_CLOSE_MIN = 15 * 60 + 5
SLOT_STEP_MIN = 5
PILOT_WINDOW_LABEL = '09:20-15:05 exclusive, %d-minute slots' % SLOT_STEP_MIN

BASIS_MANIFEST = 'export_slot_manifest'
BASIS_WINDOW = 'declared_pilot_window_FALLBACK'

STATE_VERIFIED = 'VERIFIED'
STATE_INCOMPLETE_NO_COHORT = 'INCOMPLETE_COHORT_NOT_RESOLVED'
STATE_INCOMPLETE_NO_BASIS = 'INCOMPLETE_NO_SCHEDULE_BASIS'
STATE_INCOMPLETE_CONTRACT_COUNT = 'INCOMPLETE_PINNED_COUNT_DISAGREES_WITH_COHORT'
STATE_INCOMPLETE_EMPTY_GRID = 'INCOMPLETE_NO_ELIGIBLE_SLOT_FOR_THIS_HORIZON'
STATE_INCOMPLETE_MANIFEST = 'INCOMPLETE_SCHEDULE_MANIFEST_INCONSISTENT'


def window_grid(open_min=PILOT_WINDOW_OPEN_MIN, close_min=PILOT_WINDOW_CLOSE_MIN,
                step=SLOT_STEP_MIN):
    """Every slot of the declared capture grid.  Open inclusive, close exclusive."""
    return [m for m in range(open_min, close_min, step)]


def manifest_grid(index_node):
    """(slots, states, audit) from the export's own slot manifest, or (None, None, audit).

    `audit` is what decides whether the manifest may be trusted, and it is computed from the
    ROWS rather than from the header:

      `rows`                   how many rows the manifest carries
      `distinct_valid_slots`   distinct, parseable slot keys among them
      `malformed_rows`         rows with no slot, or a slot that will not parse
      `duplicate_rows`         rows repeating a slot key already seen
      `declared_expected`      the manifest's own `expected_slots`

    THE DEFECT THIS CLOSES.  A manifest cropped at 12:00 declared 69 expected slots and carried
    33 rows.  The old code noted the disagreement and carried on, so the plan was built from the
    33 surviving rows: a 126-key denominator against 126 scored rows, reported as VERIFIED and
    100% coverage, for a capture that had in fact lost more than half its session.  A header
    that disagrees with its own rows means the schedule cannot be reconstructed from this
    export, and the only safe answer is to say so.

    Validating against DISTINCT VALID keys, not the raw row count, is what stops a malformed or
    duplicated row from making the arithmetic add up while a real slot is missing.
    """
    audit = {'rows': 0, 'distinct_valid_slots': 0, 'malformed_rows': 0,
             'duplicate_rows': 0, 'declared_expected': None, 'present': False}
    sm = (index_node or {}).get('slot_manifest') or {}
    rows = sm.get('rows')
    audit['declared_expected'] = sm.get('expected_slots')
    if not isinstance(rows, list) or not rows:
        return None, None, audit
    audit['present'] = True
    audit['rows'] = len(rows)
    states, order = {}, []
    for r in rows:
        if not isinstance(r, dict) or not r.get('slot'):
            audit['malformed_rows'] += 1
            continue
        try:
            m = R7.mins(r['slot'])
        except (ValueError, AttributeError, TypeError):
            audit['malformed_rows'] += 1
            continue
        if m in states:
            audit['duplicate_rows'] += 1
            continue
        states[m] = r.get('state') or 'STATE_ABSENT'
        order.append(m)
    audit['distinct_valid_slots'] = len(order)
    if not order:
        return None, None, audit
    return sorted(order), states, audit


def eligible_entry_slots(grid, horizon_min):
    """Slots whose PLANNED TARGET is also inside the planned grid.

    An entry whose target lies beyond the last planned slot was never an opportunity this
    schedule could have scored, so it does not belong in the denominator.  Entries that are
    admitted but whose target is outside the grid are counted SEPARATELY by `plan_for`, because
    they are evidence about the capture, not a missing score.
    """
    if not grid:
        return []
    last = grid[-1]
    return [m for m in grid if m + horizon_min <= last]


def plan_for(index_node, index_name, horizon_min):
    """The planned opportunity set for one index at one horizon.

    Returns a dict carrying:
      `state`                  VERIFIED, or one of the INCOMPLETE_* reasons
      `basis`                  which schedule the plan came from
      `planned_keys`           frozenset of (instrument_key, slot_minute)
      `planned_opportunities`  len(planned_keys)
      `eligible_entry_slots`   the slots, as HH:MM
      `slots_outside_horizon`  planned slots whose target falls past the grid's end
      `slot_states`            the manifest's own per-slot state, when there is one
      `contracts`              the pinned instrument keys
    Nothing here reads a quote, a price or an exit, so the denominator cannot move with the
    outcome.
    """
    out = {'schedule_version': SCHEDULE_VERSION, 'index': index_name,
           'horizon_min': horizon_min, 'state': STATE_VERIFIED, 'basis': None,
           'planned_keys': frozenset(), 'planned_opportunities': 0,
           'grid_slots': 0, 'eligible_entry_slots': [], 'slots_outside_horizon': 0,
           'slot_states': {}, 'contracts': [], 'notes': []}

    pins, pin_path, pin_note = (None, None, None)
    try:
        pins, pin_path, pin_note = R7.resolve_pinned(index_node or {})
    except Exception as exc:                      # a refusal from the resolver is a real state
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
    if isinstance(expected, int) and expected != len(keys):
        out['state'] = STATE_INCOMPLETE_CONTRACT_COUNT
        out['notes'].append('cohort expects %d contracts, %d pinned' % (expected, len(keys)))
        return out
    out['contracts_expected'] = expected

    grid, states, audit = manifest_grid(index_node)
    out['manifest_audit'] = audit
    if grid:
        out['basis'] = BASIS_MANIFEST
        out['slot_states'] = {'%02d:%02d' % (m // 60, m % 60): states[m] for m in grid}
        sm = (index_node or {}).get('slot_manifest') or {}
        out['manifest_expected_slots'] = sm.get('expected_slots')
        out['manifest_observed_slots'] = sm.get('observed_slots')
        # ── the manifest must agree with ITSELF before a denominator is built from it ──
        bad = []
        declared = audit['declared_expected']
        if isinstance(declared, int) and declared != audit['distinct_valid_slots']:
            bad.append('declares %d expected slots but carries %d distinct valid slot rows'
                       % (declared, audit['distinct_valid_slots']))
        if audit['malformed_rows']:
            bad.append('%d malformed row(s) with no usable slot' % audit['malformed_rows'])
        if audit['duplicate_rows']:
            bad.append('%d duplicate slot row(s)' % audit['duplicate_rows'])
        if isinstance(sm.get('observed_slots'), int) and \
                sm['observed_slots'] > audit['distinct_valid_slots']:
            bad.append('declares %d observed slots, more than its %d distinct valid rows'
                       % (sm['observed_slots'], audit['distinct_valid_slots']))
        if bad:
            out['state'] = STATE_INCOMPLETE_MANIFEST
            out['notes'].append(
                'the slot manifest is inconsistent with itself, so the planned schedule cannot '
                'be reconstructed from this export: ' + '; '.join(bad))
            return out
    else:
        grid = window_grid()
        out['basis'] = BASIS_WINDOW
        out['notes'].append('no slot_manifest in this export; the planned grid is the declared '
                            'pilot window %s, which is an assumption about the producing build'
                            % PILOT_WINDOW_LABEL)
    out['grid_slots'] = len(grid)

    slots = eligible_entry_slots(grid, horizon_min)
    out['slots_outside_horizon'] = len(grid) - len(slots)
    if not slots:
        out['state'] = STATE_INCOMPLETE_EMPTY_GRID
        out['notes'].append('no slot in a %d-slot grid leaves room for a %d-minute horizon'
                            % (len(grid), horizon_min))
        return out
    out['eligible_entry_slots'] = ['%02d:%02d' % (m // 60, m % 60) for m in slots]
    out['planned_keys'] = frozenset((k, m) for k in keys for m in slots)
    out['planned_opportunities'] = len(out['planned_keys'])
    return out


def reconcile(plan, observed_entry_keys, scored_keys, outside_window_keys=()):
    """Compare the plan with what was observed.  Every count here is a difference of SETS.

    `observed_entry_keys`    (instrument_key, slot) that produced an admitted entry quote
    `scored_keys`            (instrument_key, slot) that produced a commonly scored row
    `outside_window_keys`    admitted entries whose planned target fell outside the grid

    The three numbers a trial summary needs -- planned, scored, and the ratio -- are computed
    here and nowhere else, so no caller can quietly substitute a denominator.
    """
    planned = plan['planned_keys']
    all_obs = frozenset(observed_entry_keys)
    all_scored = frozenset(scored_keys)
    obs = all_obs & planned
    scored = all_scored & planned
    missing_slots = collections.Counter()
    for key, m in sorted(planned - obs):
        missing_slots['%02d:%02d' % (m // 60, m % 60)] += 1
    out = {
        # raw totals, independent of the plan, so a refused schedule still reports what was
        # actually seen -- late quotes and missing ones alike
        'observed_entries_total': len(all_obs),
        'scored_total': len(all_scored),
        'admitted_entries_outside_planned_window': len(frozenset(outside_window_keys)),
        'planned_opportunities': len(planned),
        'schedule_state': plan['state'], 'schedule_basis': plan['basis'],
        'manifest_audit': plan.get('manifest_audit')}
    if not planned:
        # no defensible denominator: every plan-relative number is withheld rather than
        # computed against an empty set, which would read as "everything is outside the plan"
        out.update({'observed_entries_in_plan': None, 'scored_in_plan': None,
                    'missing_entry_keys': None, 'missing_entry_slots': {},
                    'observed_but_not_scored': None,
                    'observed_entries_outside_plan': None,
                    'usable_coverage': None,
                    'coverage_basis': 'WITHHELD: the planned schedule is %s, so no coverage '
                                      'ratio is defensible. The raw observed and scored totals '
                                      'above are preserved.' % plan['state']})
        return out
    out.update({
        'observed_entries_in_plan': len(obs),
        'scored_in_plan': len(scored),
        'missing_entry_keys': len(planned - obs),
        'missing_entry_slots': dict(missing_slots),
        'observed_but_not_scored': len(obs - scored),
        'observed_entries_outside_plan': len(all_obs - planned),
        'usable_coverage': len(scored) / len(planned),
        'coverage_basis': 'scored rows inside the plan, over planned opportunities; the '
                          'denominator is the schedule, never the admitted quotes'})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('export')
    ap.add_argument('--horizons', default='15,30,60,120')
    a = ap.parse_args(argv)
    ex = json.load(open(a.export))
    print('%s   %s' % (SCHEDULE_VERSION, os.path.basename(a.export)))
    for ix in sorted(((ex.get('premium') or {}).get('indices') or {})):
        node = ex['premium']['indices'][ix]
        for h in (int(x) for x in a.horizons.split(',')):
            p = plan_for(node, ix, h)
            print('  %-4s %4d min  %-24s basis %-28s grid %d  eligible %d  planned %d'
                  % (ix, h, p['state'], p['basis'], p['grid_slots'],
                     len(p['eligible_entry_slots']), p['planned_opportunities']))
            for n in p['notes']:
                print('        note: %s' % n)
    return 0


if __name__ == '__main__':
    sys.exit(main())
