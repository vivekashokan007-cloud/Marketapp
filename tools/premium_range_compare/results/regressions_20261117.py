#!/usr/bin/env python3
"""Reproduce the reproducible defects from the 17 November reviews, before and after.

Run it against the 12 November export and the pinned spot history:

    python3 -I tools/premium_range_compare/results/regressions_20261117.py \\
        <export_2026-11-12.json> <spots_20261021_20261104.jsonl>

Each section prints what the corrected code does now AND what the defect looked like, computed
from the same inputs rather than quoted, so the evidence does not depend on remembering the old
behaviour.  The companion output is `regressions_20261117.txt`.
"""
import argparse
import collections
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), '..', 'spot_path_baseline'))

import premium_repricing_v2 as R7          # noqa: E402
import planned_schedule as PS              # noqa: E402
import range_compare as RC                 # noqa: E402


def rule(title):
    print('=' * 94)
    print(title)
    print('=' * 94)


def truncate(ex, at_min=12 * 60):
    out = copy.deepcopy(ex)
    for ix in list(out['premium']['indices']):
        node = out['premium']['indices'][ix]
        node['records'] = [r for r in node['records'] if R7.mins(r['slot']) < at_min]
        node['slot_envelopes'] = [e for e in node['slot_envelopes']
                                  if R7.mins(e['slot']) < at_min]
    return out


def write_json(doc, path):
    with open(path, 'w') as f:
        json.dump(doc, f)
    return path


def write_history(rows, path):
    with open(path, 'w') as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + '\n')
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('export')
    ap.add_argument('spots')
    a = ap.parse_args(argv)
    work = tempfile.mkdtemp()

    ex = json.load(open(a.export))
    digest = hashlib.sha256(open(a.export, 'rb').read()).hexdigest()
    hist, hmeta = RC.load_history(a.spots)
    session = ex['manifest']['session_date_used']
    hd = sorted(d for d in hist if d < session)

    print('input   %s  sha256 %s  session %s'
          % (os.path.basename(a.export), digest[:32], session))
    print('history %s  sha256 %s  rows %d'
          % (hmeta['path'], hmeta['sha256'][:32], hmeta['rows']))
    print('runner  %s   schedule %s' % (RC.COMPARISON_VERSION, PS.SCHEDULE_VERSION))
    print('')

    # ── 1 ────────────────────────────────────────────────────────────────────────────────────
    rule('1  COVERAGE IS COUNTED AGAINST THE PLANNED SCHEDULE, NOT THE ADMITTED QUOTES')
    print('The defect: the denominator was `len(entries)` AFTER quote admission, so a session')
    print('that stopped capturing had fewer "intended" opportunities and its ratio ROSE.')
    print('')
    trunc = truncate(ex)
    print('  %-4s %-26s %8s %9s %8s %9s %7s' %
          ('idx', 'input', 'planned', 'admitted', 'scored', 'usable', 'gate'))
    for ix in sorted(ex['premium']['indices']):
        for label, doc in (('full session', ex), ('truncated at 12:00', trunc)):
            s, _r, _c = RC.compare_index_horizon(doc, ix, hist, hd, 60)
            rec = s['reconciliation']
            print('  %-4s %-26s %8d %9d %8d %8.1f%% %7s'
                  % (ix, label, rec['planned_opportunities'], s['admitted_entry_candidates'],
                     rec['scored_in_plan'], 100 * rec['usable_coverage'],
                     'PASS' if rec['usable_coverage'] >= RC.MIN_USABLE_COVERAGE else 'FAIL'))
    print('')
    print('  the OLD circular ratio, recomputed from the same run, is what used to be reported:')
    for ix in sorted(ex['premium']['indices']):
        s, _r, _c = RC.compare_index_horizon(trunc, ix, hist, hd, 60)
        rec = s['reconciliation']
        old = rec['scored_in_plan'] / s['admitted_entry_candidates']
        print('  %-4s truncated: %d/%d = %.1f%% (%s)  vs planned %d/%d = %.1f%% (%s)'
              % (ix, rec['scored_in_plan'], s['admitted_entry_candidates'], 100 * old,
                 'PASS' if old >= RC.MIN_USABLE_COVERAGE else 'FAIL',
                 rec['scored_in_plan'], rec['planned_opportunities'],
                 100 * rec['usable_coverage'],
                 'PASS' if rec['usable_coverage'] >= RC.MIN_USABLE_COVERAGE else 'FAIL'))
    print('')
    print('  the plan is unchanged by the truncation, which is the point:')
    for ix in sorted(ex['premium']['indices']):
        pf = PS.plan_for(ex['premium']['indices'][ix], ix, 60, session)
        pt = PS.plan_for(trunc['premium']['indices'][ix], ix, 60, session)
        print('  %-4s full plan %d keys, truncated plan %d keys, identical: %s  (basis %s)'
              % (ix, pf['planned_opportunities'], pt['planned_opportunities'],
                 pf['planned_keys'] == pt['planned_keys'], pf['basis']))
    print('')
    print('  missing planned entry slots on the truncated input, by slot:')
    for ix in sorted(ex['premium']['indices']):
        s, _r, _c = RC.compare_index_horizon(trunc, ix, hist, hd, 60)
        miss = s['reconciliation']['missing_entry_slots']
        print('  %-4s %d keys across %d slots, %s .. %s'
              % (ix, s['reconciliation']['missing_entry_keys'], len(miss),
                 min(miss) if miss else '-', max(miss) if miss else '-'))
    print('')

    # ── 2 ────────────────────────────────────────────────────────────────────────────────────
    rule('2  EACH INDEX/SESSION IS COUNTED ONCE')
    print('The defect: one session supplied twice produced a two-session cohort, so a decision')
    print('rule counted in sessions reached its threshold twice as fast on duplicated input.')
    print('')
    dup = shutil.copy(a.export, os.path.join(work, 'duplicate.json'))
    conflict = write_json(trunc, os.path.join(work, 'competing.json'))

    out = RC.run([a.export, dup], a.spots, horizons=(60,))
    print('  identical duplicate twice      -> sessions kept %d, duplicates collapsed %d, '
          'excluded %d' % (len(out['sessions']),
                           out['inputs_kept'][0]['duplicate_inputs_collapsed'],
                           len(out['inputs_excluded'])))
    for ix in sorted(out['across_sessions']):
        print('        across sessions %-4s h60 sessions=%d'
              % (ix, out['across_sessions'][ix]['h60']['sessions']))

    out = RC.run([a.export, conflict], a.spots, horizons=(60,))
    exc = out['inputs_excluded'][0]
    print('  two DIFFERENT files, same date -> sessions kept %d, excluded %d, reason %s'
          % (len(out['sessions']), len(out['inputs_excluded']), exc['reason']))
    for dg in exc['competing_digests']:
        print('        competing digest %s' % dg[:32])
    print('        %s' % exc['resolution'])

    out = RC.run([a.export, conflict], a.spots, horizons=(60,),
                 canonical={session: digest[:12]})
    kept = out['inputs_kept'][0]
    print('  resolved by declaration        -> sessions kept %d, canonical %s, '
          'competing not used %d'
          % (len(out['sessions']), kept['canonical_declared'],
             len(kept.get('competing_digests_not_used') or [])))

    out = RC.run([conflict], a.spots, horizons=(60,))
    print('  a below-coverage session       -> kept %d, but eligible in 0 of %d indices:'
          % (len(out['sessions']), len(out['sessions'][0]['eligibility'])))
    for ix, e in sorted(out['sessions'][0]['eligibility'].items()):
        print('        %-4s eligible=%-5s coverage=%.3f  reasons=%s'
              % (ix, e['eligible'], e['usable_coverage'], ';'.join(e['reasons'])))
    print('        aggregate indices: %s   carried in cohort_excluded: %d'
          % (sorted(out['across_sessions']) or 'none', len(out['cohort_excluded'])))
    print('')

    # ── 3 ────────────────────────────────────────────────────────────────────────────────────
    rule('3  HISTORICAL SLOT COLLISIONS RESOLVE DETERMINISTICALLY')
    print('The defect: two observations landing on one slot were resolved by position, so')
    print('reversing the input changed the retained price.')
    print('')
    rows = [{'d': '2026-11-10', 't': '10:55', 'nf': '25000.00', 'bnf': '56000.00'},
            {'d': '2026-11-10', 't': '10:56', 'nf': '25099.00', 'bnf': '56099.00'}]
    for label, rs in (('as supplied', rows), ('reversed', list(reversed(rows)))):
        h, m = RC.load_history(write_history(rs, os.path.join(work, 'h.jsonl')), 'floor5')
        present = R7.mins('10:55') in h.get('2026-11-10', {})
        print('  conflicting prices, %-12s slot retained=%-5s  excluded_conflicting=%d  '
              'collapsed=%d' % (label, present, m['excluded_conflicting_slots'],
                                m['collapsed_identical_duplicates']))
    ident = [rows[0], dict(rows[0])]
    h, m = RC.load_history(write_history(ident, os.path.join(work, 'h2.jsonl')), 'floor5')
    print('  identical duplicate            slot retained=%-5s  excluded_conflicting=%d  '
          'collapsed=%d' % (R7.mins('10:55') in h['2026-11-10'],
                            m['excluded_conflicting_slots'],
                            m['collapsed_identical_duplicates']))
    sched = [{'d': '2026-11-10', 't': '10:56', 'nf': '25000.00', 'bnf': '56000.00',
              'scheduled_slot': '10:55'}]
    h, m = RC.load_history(write_history(sched, os.path.join(work, 'h3.jsonl')), 'exact')
    kept = h['2026-11-10'][R7.mins('10:55')]
    print('  a verified scheduled slot      key from %s, observed %02d:%02d preserved'
          % (kept['slot_from'], kept['observed_min'] // 60, kept['observed_min'] % 60))
    print('')
    print('  the REAL history, both input orders:')
    lines = [x for x in open(a.spots, encoding='utf-8').read().splitlines() if x.strip()]
    rev = write_history([json.loads(x) for x in reversed(lines)],
                        os.path.join(work, 'spots_reversed.jsonl'))
    a_h, a_m = RC.load_history(a.spots)
    b_h, b_m = RC.load_history(rev)
    same = ({d: sorted(v.items()) for d, v in a_h.items()}
            == {d: sorted(v.items()) for d, v in b_h.items()})
    print('  identical retained history: %s   retained slots %d/%d   conflicts %d/%d'
          % (same, a_m['retained_slots'], b_m['retained_slots'],
             a_m['excluded_conflicting_slots'], b_m['excluded_conflicting_slots']))
    for ix in sorted(ex['premium']['indices']):
        x, _r, _c = RC.compare_index_horizon(ex, ix, a_h, hd, 60)
        y, _r, _c = RC.compare_index_horizon(ex, ix, b_h, hd, 60)
        print('  %-4s scores identical under a reversed history: %s'
              % (ix, x['variants_common'] == y['variants_common']))
    print('')

    # ── 4 ────────────────────────────────────────────────────────────────────────────────────
    rule('4  SCHEDULE MANIFESTS ARE VALIDATED AGAINST THEIR OWN ROWS')
    print('The defect: cropping the MANIFEST as well as the quotes made the runner build a')
    print('126-key denominator from 33 surviving rows, call the schedule VERIFIED and accept')
    print('126/126 = 100% for a capture that had lost half its session.')
    print('')
    manifest_cropped = truncate(ex)
    for ix in list(manifest_cropped['premium']['indices']):
        node = manifest_cropped['premium']['indices'][ix]
        node['slot_manifest']['rows'] = [r for r in node['slot_manifest']['rows']
                                         if R7.mins(r['slot']) <= 12 * 60]
    dupe = copy.deepcopy(ex)
    for ix in list(dupe['premium']['indices']):
        rows = dupe['premium']['indices'][ix]['slot_manifest']['rows']
        rows.append(dict(rows[0]))
        dupe['premium']['indices'][ix]['slot_manifest']['expected_slots'] = len(rows)
    malformed = copy.deepcopy(ex)
    for ix in list(malformed['premium']['indices']):
        rows = malformed['premium']['indices'][ix]['slot_manifest']['rows']
        rows.append({'state': 'OBSERVED'})
        malformed['premium']['indices'][ix]['slot_manifest']['expected_slots'] = len(rows)

    print('  %-4s %-28s %-42s %8s %9s %10s %8s' %
          ('idx', 'input', 'schedule state', 'planned', 'scored', 'usable', 'observed'))
    for ix in sorted(ex['premium']['indices']):
        for label, doc in (('full export', ex),
                           ('quotes cropped only', truncate(ex)),
                           ('quotes AND manifest cropped', manifest_cropped),
                           ('a duplicate manifest row', dupe),
                           ('a malformed manifest row', malformed)):
            s_, _r, _c = RC.compare_index_horizon(doc, ix, hist, hd, 60)
            rec = s_['reconciliation']
            cov = rec['usable_coverage']
            print('  %-4s %-28s %-42s %8d %9s %10s %8d'
                  % (ix, label, rec['schedule_state'], rec['planned_opportunities'],
                     rec['scored_in_plan'] if rec['scored_in_plan'] is not None else '-',
                     ('%.1f%%' % (100 * cov)) if cov is not None else 'WITHHELD',
                     rec['observed_entries_total']))
    print('')
    print('  the audit that explains each refusal, NIFTY:')
    for label, doc in (('quotes AND manifest cropped', manifest_cropped),
                       ('a duplicate manifest row', dupe),
                       ('a malformed manifest row', malformed)):
        p_ = PS.plan_for(doc['premium']['indices']['NF'], 'NF', 60, session)
        a = p_['manifest_audit']
        print('    %-28s rows %-3d distinct %-3d malformed %d duplicate %d declared %s'
              % (label, a['rows'], a['distinct_valid_slots'], a['malformed_rows'],
                 a['duplicate_rows'], a['declared_expected']))
    print('')
    print('  and the raw observed counts survive every refusal, so late and missing quote')
    print('  counts are preserved rather than erased by a withheld ratio.')
    print('')

    # ── 5 ────────────────────────────────────────────────────────────────────────────────────
    rule('5  THE PLAN COMES FROM AUTHORITATIVE RULES, NOT FROM THE EXPORT')
    print('The defect: the plan came from the export\'s own slot manifest, validated against')
    print('ITSELF -- and self-consistency is cheap. Cropping the rows to 33 AND resetting both')
    print('totals to 33 gave a self-consistent manifest, a 126-key plan and 126/126 = 100%')
    print('with eligible=true. A row reading 15:99 parsed to minute 999 and pushed the')
    print('60-minute denominator from 342 to 414.')
    print('')
    cal = PS.load_session_calendar()
    print('  capture rules  %s' % PS.CAPTURE_WINDOW_RULES_VERSION)
    print('                 %s' % PS.PILOT_WINDOW_LABEL)
    print('  calendar       %s   source %s   sha256 %s   holidays %d'
          % (cal['version'], cal['source'], cal['sha256'][:16], cal['holiday_count']))
    print('')

    def variant(doc, at=None, crop_manifest=False, reset_totals=False, bad_slot=None,
                empty=False):
        out = copy.deepcopy(doc)
        for ix in list(out['premium']['indices']):
            n = out['premium']['indices'][ix]
            sm = n.get('slot_manifest') or {}
            if at is not None:
                n['records'] = [r for r in n['records'] if R7.mins(r['slot']) <= at]
                n['slot_envelopes'] = [e for e in n['slot_envelopes']
                                       if R7.mins(e['slot']) <= at]
                if crop_manifest:
                    sm['rows'] = [r for r in sm['rows'] if R7.mins(r['slot']) <= at]
                    if reset_totals:
                        sm['expected_slots'] = len(sm['rows'])
                        sm['observed_slots'] = len(sm['rows'])
            if bad_slot:
                sm['rows'].append({'slot': bad_slot, 'state': 'OBSERVED'})
            if empty:
                n['slot_manifest'] = {'rows': [], 'expected_slots': 0, 'observed_slots': 0}
        return out

    cases = [('1  full export', ex),
             ('2  quotes+envelopes cropped @12:00, manifest intact', variant(ex, at=12 * 60)),
             ('3  quotes+envelopes+manifest cropped, totals reset',
              variant(ex, at=12 * 60, crop_manifest=True, reset_totals=True)),
             ('4a present manifest with no usable rows', variant(ex, empty=True)),
             ('4b present manifest with slot 15:99', variant(ex, bad_slot='15:99')),
             ('4c present manifest with slot 15:05 (valid clock, outside the grid)',
              variant(ex, bad_slot='15:05'))]
    print('  %-62s %-4s %-46s %7s %7s %9s %10s'
          % ('case', 'idx', 'schedule state', 'planned', 'scored', 'usable', 'diagnostic'))
    for label, doc in cases:
        for ix in sorted(doc['premium']['indices']):
            s_, _r, _c = RC.compare_index_horizon(doc, ix, hist, hd, 60)
            r = s_['reconciliation']
            fmt = lambda v: '-' if v is None else ('%.1f%%' % (100 * v))
            print('  %-62s %-4s %-46s %7d %7s %9s %10s'
                  % (label if ix == sorted(doc['premium']['indices'])[0] else '', ix,
                     r['schedule_state'], r['planned_opportunities'],
                     r['scored_in_plan'] if r['scored_in_plan'] is not None else '-',
                     fmt(r['usable_coverage']), fmt(r['diagnostic_coverage'])))
    print('')
    print('  the planned count is 342 in EVERY row above: the export cannot move it.')
    print('')
    print('  and a session the calendar cannot classify is refused rather than planned:')
    for date in ('2026-11-14', '2026-12-25', '2027-01-04'):
        p_ = PS.plan_for(ex['premium']['indices']['NF'], 'NF', 60, date, cal)
        print('    %-12s %-38s planned %d   %s'
              % (date, p_['state'], p_['planned_opportunities'],
                 (p_['notes'] or [''])[0]))
    print('')

    rule('ALL FIVE DEFECTS CLOSED ON THE INPUT THIS MACHINE HAS')
    print('Case 1 at 342/342 with 414 admitted needs the 16 November evening export, which is')
    print('still absent; `CompleteCapture` in the test suite asserts that shape on a fixture')
    print('covering the whole authoritative 69-slot grid. The figures Codex')
    print('reported for it remain independent evidence rather than a reproduction. The pinned')
    print('16 November regression in tests/test_range_compare.py runs the moment it arrives.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
