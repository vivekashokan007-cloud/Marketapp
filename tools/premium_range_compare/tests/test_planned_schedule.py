"""Checks for the planned opportunity set, under the v3 authoritative model.

THE THREE DEFECTS THESE EXIST FOR, in the order they were found:

  1. the denominator was the entry candidates left AFTER quote admission, so it shrank as a
     capture got worse: a truncation at 12:00 reported 62.5% and passed a 60% gate against an
     honest 35.1%;
  2. the denominator then came from the export's own `slot_manifest`, validated against ITSELF
     -- and self-consistency is cheap. Cropping the rows to 33 and resetting BOTH totals to 33
     gave a self-consistent manifest, a 126-key plan and **126/126 = 100%** for a session that
     had lost more than half its capture;
  3. a manifest row reading `15:99` parsed to minute 999 and pushed the 60-minute denominator
     from 342 to 414.

What v3 pins instead: the plan comes from the capture-window RULES and a versioned session
CALENDAR, and the manifest is only ever validated against it. So the plan does not move when the
export's counts, rows or quotes move -- which every test below asserts in one way or another.
"""
import datetime
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), '..', 'spot_path_baseline'))

import premium_repricing_v2 as R7          # noqa: E402
import planned_schedule as PS              # noqa: E402

SESSION = '2026-11-12'                     # a Thursday, not an NSE holiday
SLOTS_69 = [PS.hhmm(m) for m in PS.window_grid()]
CAL = PS.load_session_calendar()


def node(slots=SLOTS_69, contracts=6, observed=None, expected_slots=None,
         observed_slots=None, contracts_expected=None, with_manifest=True, extra_rows=()):
    observed = set(slots if observed is None else observed)
    pins = [{'instrument_key': 'NSE_FO|%d' % i, 'strike': 25000.0 + 50 * (i // 2),
             'option_type': 'CE' if i % 2 == 0 else 'PE', 'expiry': '2026-11-17',
             'underlying': 'NIFTY'} for i in range(contracts)]
    out = {'cohort': {'pinned_contracts': pins,
                      'contracts_expected': (contracts if contracts_expected is None
                                             else contracts_expected)}}
    if with_manifest:
        rows = [{'slot': s, 'state': 'OBSERVED' if s in observed
                 else 'NOT_OBSERVED_UNEXPLAINED'} for s in slots]
        rows.extend(extra_rows)
        out['slot_manifest'] = {
            'expected_slots': len(slots) if expected_slots is None else expected_slots,
            'observed_slots': len(observed) if observed_slots is None else observed_slots,
            'rows': rows}
    return out


def plan(session=SESSION, **kw):
    return PS.plan_for(node(**kw), 'NF', 60, session, CAL)


class SessionCalendar(unittest.TestCase):

    def test_the_calendar_comes_from_the_app_and_carries_its_provenance(self):
        """Read from brain.py rather than restated, so this module cannot disagree with the
        application about which days traded. The digest makes the version auditable."""
        self.assertIsNone(CAL.get('error'), CAL.get('error'))
        self.assertEqual(PS.SESSION_CALENDAR_VERSION, CAL['version'])
        self.assertTrue(CAL['source'].endswith('brain.py'), CAL['source'])
        self.assertEqual(64, len(CAL['sha256']))
        self.assertGreater(CAL['holiday_count'], 0)
        self.assertIn('2026-12-25', CAL['holidays'])

    def test_a_regular_weekday_is_a_regular_session(self):
        ok, why, _c = PS.classify_session('2026-11-12', CAL)
        self.assertTrue(ok, why)
        self.assertIsNone(why)

    def test_a_weekend_a_holiday_and_an_uncovered_year_are_each_distinguished(self):
        for date, fragment in (('2026-11-14', 'weekend'),          # Saturday
                               ('2026-11-15', 'weekend'),          # Sunday
                               ('2026-12-25', 'NSE holiday'),
                               ('2027-01-04', 'outside the calendar years'),
                               ('2025-01-02', 'outside the calendar years')):
            ok, why, _c = PS.classify_session(date, CAL)
            self.assertFalse(ok, date)
            self.assertIn(fragment, why, date)

    def test_an_unparseable_session_date_is_refused(self):
        for bad in (None, '', 'yesterday', '2026-13-40', 20261112):
            ok, why, _c = PS.classify_session(bad, CAL)
            self.assertFalse(ok, bad)
            self.assertTrue('unparseable' in why or 'outside' in why, (bad, why))

    def test_a_session_the_calendar_cannot_classify_is_refused_not_planned(self):
        p = PS.plan_for(node(), 'NF', 60, '2026-12-25', CAL)
        self.assertEqual(PS.STATE_INCOMPLETE_SESSION_UNKNOWN, p['state'])
        self.assertEqual(0, p['planned_opportunities'])
        self.assertNotIn(p['state'], PS.ELIGIBLE_STATES)


class Grid(unittest.TestCase):

    def test_the_authoritative_window_is_69_five_minute_slots(self):
        grid = PS.window_grid()
        self.assertEqual(69, len(grid))
        self.assertEqual(R7.mins('09:20'), grid[0])
        self.assertEqual(R7.mins('15:00'), grid[-1])

    def test_an_eligible_entry_slot_is_one_whose_target_is_still_in_the_grid(self):
        grid = PS.window_grid()
        for horizon, want_last, want_n in ((15, '14:45', 66), (30, '14:30', 63),
                                           (60, '14:00', 57), (120, '13:00', 45)):
            slots = PS.eligible_entry_slots(grid, horizon)
            self.assertEqual(want_n, len(slots), horizon)
            self.assertEqual(R7.mins(want_last), slots[-1], horizon)

    def test_the_capture_rules_are_versioned_and_sourced(self):
        p = plan()
        self.assertEqual(PS.CAPTURE_WINDOW_RULES_VERSION,
                         p['capture_window_rules']['version'])
        self.assertIn('PremiumCaptureCohort', p['capture_window_rules']['source'])
        self.assertEqual(PS.BASIS_AUTHORITATIVE, p['basis'])


class TheAuthoritativePlan(unittest.TestCase):

    def test_it_is_57_x_6_at_60_minutes(self):
        """342, which is what Codex derived independently for the 16 November capture."""
        p = plan()
        self.assertEqual(PS.STATE_VERIFIED, p['state'])
        self.assertEqual(69, p['grid_slots'])
        self.assertEqual(57, len(p['eligible_entry_slots']))
        self.assertEqual(342, p['planned_opportunities'])
        self.assertEqual(12, p['slots_outside_horizon'])

    def test_it_does_not_move_when_the_export_does(self):
        """THE PROPERTY THE COVERAGE GATE RESTS ON, asserted against every way the previous
        versions could be pushed around: fewer observed slots, fewer manifest rows, reset
        totals, a bad clock, no manifest at all."""
        cropped = [s for s in SLOTS_69 if R7.mins(s) <= 12 * 60]
        variants = {
            'full': node(),
            'fewer observed': node(observed=cropped),
            'manifest cropped': node(slots=cropped, expected_slots=33),
            'cropped and totals reset': node(slots=cropped, expected_slots=33,
                                             observed_slots=33),
            'an invalid slot clock': node(extra_rows=[{'slot': '15:99',
                                                       'state': 'OBSERVED'}]),
            'no manifest': node(with_manifest=False),
        }
        for label, nd in variants.items():
            p = PS.plan_for(nd, 'NF', 60, SESSION, CAL)
            self.assertEqual(342, p['planned_opportunities'], label)
            self.assertEqual(57, len(p['eligible_entry_slots']), label)

    def test_an_unresolvable_cohort_is_an_explicit_incomplete_state(self):
        p = PS.plan_for({'slot_manifest': {'rows': [{'slot': '09:20', 'state': 'OBSERVED'}]}},
                        'NF', 60, SESSION, CAL)
        self.assertEqual(PS.STATE_INCOMPLETE_NO_COHORT, p['state'])
        self.assertEqual(0, p['planned_opportunities'])

    def test_a_cohort_that_disagrees_with_its_own_expected_count_is_incomplete(self):
        p = plan(contracts=6, contracts_expected=8)
        self.assertEqual(PS.STATE_INCOMPLETE_CONTRACT_COUNT, p['state'])
        self.assertEqual(0, p['planned_opportunities'])
        self.assertTrue(any('expects 8 contracts, 6 pinned' in n for n in p['notes']))

    def test_a_horizon_with_no_room_in_the_grid_is_incomplete(self):
        p = PS.plan_for(node(), 'NF', 24 * 60, SESSION, CAL)
        self.assertEqual(PS.STATE_INCOMPLETE_EMPTY_GRID, p['state'])
        self.assertEqual(0, p['planned_opportunities'])

    def test_no_incomplete_state_is_an_eligible_state(self):
        for state in (PS.STATE_INCOMPLETE_NO_COHORT, PS.STATE_INCOMPLETE_CONTRACT_COUNT,
                      PS.STATE_INCOMPLETE_EMPTY_GRID, PS.STATE_INCOMPLETE_SESSION_UNKNOWN,
                      PS.STATE_INCOMPLETE_SESSION_NOT_REGULAR,
                      PS.STATE_MANIFEST_MISMATCH, PS.STATE_MANIFEST_INVALID):
            self.assertNotIn(state, PS.ELIGIBLE_STATES, state)
        for state in (PS.STATE_VERIFIED, PS.STATE_VERIFIED_NO_MANIFEST):
            self.assertIn(state, PS.ELIGIBLE_STATES, state)


class ManifestValidation(unittest.TestCase):
    """The manifest is a CLAIM about the plan, checked against it -- never its source."""

    def test_a_consistent_manifest_verifies(self):
        p = plan()
        self.assertEqual(PS.STATE_VERIFIED, p['state'])
        a = p['manifest_audit']
        self.assertTrue(a['covers_authoritative_grid'])
        self.assertEqual(69, a['distinct_valid_slots'])
        self.assertEqual([], a['reasons'])

    def test_the_self_consistent_crop_is_a_NAMED_MISMATCH_and_keeps_the_plan(self):
        """Codex's reproduction. 33 rows with BOTH totals reset to 33 is self-consistent, and
        v2 accepted it as VERIFIED with a 126-key plan and 100% coverage."""
        cropped = [s for s in SLOTS_69 if R7.mins(s) <= 12 * 60]
        self.assertEqual(33, len(cropped))
        p = plan(slots=cropped, expected_slots=33, observed_slots=33)
        self.assertEqual(PS.STATE_MANIFEST_MISMATCH, p['state'])
        self.assertEqual(342, p['planned_opportunities'], 'the plan is preserved')
        a = p['manifest_audit']
        self.assertFalse(a['covers_authoritative_grid'])
        self.assertEqual(33, a['distinct_valid_slots'])
        self.assertEqual(69, a['authoritative_slots'])
        self.assertTrue(any('covers 33 of the 69 authoritative slots' in r
                            for r in a['reasons']), a['reasons'])
        self.assertTrue(any('declares 33 expected slots against the authoritative 69' in r
                            for r in a['reasons']), a['reasons'])

    def test_an_invalid_slot_clock_invalidates_the_manifest_instead_of_the_plan(self):
        """`15:99` parses to minute 999 and used to push the denominator to 414."""
        p = plan(extra_rows=[{'slot': '15:99', 'state': 'OBSERVED'}])
        self.assertEqual(PS.STATE_MANIFEST_INVALID, p['state'])
        self.assertEqual(342, p['planned_opportunities'])
        a = p['manifest_audit']
        self.assertEqual(1, a['slots_outside_authoritative_grid'])
        self.assertIn('15:99', a['offending_slots'])

    def test_a_valid_clock_outside_the_window_is_also_refused(self):
        for bad in ('08:00', '15:05', '16:00', '00:00'):
            p = plan(extra_rows=[{'slot': bad, 'state': 'OBSERVED'}])
            self.assertEqual(PS.STATE_MANIFEST_INVALID, p['state'], bad)
            self.assertIn(bad, p['manifest_audit']['offending_slots'], bad)

    def test_a_present_manifest_with_no_usable_rows_is_invalid(self):
        for rows in ([], [{'state': 'OBSERVED'}], [{'slot': None}],
                     [{'slot': 'not-a-clock'}], ['not-a-dict']):
            nd = node()
            nd['slot_manifest'] = {'rows': rows, 'expected_slots': 0, 'observed_slots': 0}
            p = PS.plan_for(nd, 'NF', 60, SESSION, CAL)
            self.assertEqual(PS.STATE_MANIFEST_INVALID, p['state'], rows)
            self.assertEqual(342, p['planned_opportunities'], rows)
            self.assertTrue(any('no row with a usable slot' in r
                                for r in p['manifest_audit']['reasons']), rows)

    def test_a_manifest_with_no_row_list_at_all_is_invalid(self):
        nd = node()
        nd['slot_manifest'] = {'expected_slots': 69, 'observed_slots': 69}
        p = PS.plan_for(nd, 'NF', 60, SESSION, CAL)
        self.assertEqual(PS.STATE_MANIFEST_INVALID, p['state'])
        self.assertIn('carries no row list', ' '.join(p['manifest_audit']['reasons']))

    def test_a_duplicate_row_is_invalid_and_cannot_conceal_a_missing_slot(self):
        slots = SLOTS_69[:-1]
        nd = node(slots=slots, expected_slots=69)
        nd['slot_manifest']['rows'].append(dict(nd['slot_manifest']['rows'][0]))
        self.assertEqual(69, len(nd['slot_manifest']['rows']), 'the row COUNT adds up')
        p = PS.plan_for(nd, 'NF', 60, SESSION, CAL)
        self.assertEqual(PS.STATE_MANIFEST_INVALID, p['state'])
        self.assertEqual(1, p['manifest_audit']['duplicate_rows'])
        self.assertEqual(68, p['manifest_audit']['distinct_valid_slots'])
        self.assertEqual(342, p['planned_opportunities'])

    def test_a_manifest_claiming_more_observed_than_it_carries_is_a_mismatch(self):
        p = plan(observed_slots=70)
        self.assertEqual(PS.STATE_MANIFEST_MISMATCH, p['state'])
        self.assertTrue(any('more than its 69 valid rows' in r
                            for r in p['manifest_audit']['reasons']))

    def test_an_absent_manifest_leaves_the_plan_authoritative_and_eligible(self):
        """Absent is not the same as present-but-broken: there is simply no cross-check."""
        p = plan(with_manifest=False)
        self.assertEqual(PS.STATE_VERIFIED_NO_MANIFEST, p['state'])
        self.assertIn(p['state'], PS.ELIGIBLE_STATES)
        self.assertEqual(342, p['planned_opportunities'])
        self.assertEqual('MANIFEST_ABSENT', p['manifest_audit']['state'])
        self.assertTrue(any('could not be cross-checked' in n for n in p['notes']))

    def test_the_per_slot_state_is_carried_so_a_missing_slot_has_its_own_reason(self):
        missing = [s for s in SLOTS_69 if R7.mins(s) >= R7.mins('13:45')]
        p = plan(observed=[s for s in SLOTS_69 if s not in missing])
        self.assertEqual('NOT_OBSERVED_UNEXPLAINED', p['slot_states']['13:45'])
        self.assertEqual('OBSERVED', p['slot_states']['09:20'])


class Reconcile(unittest.TestCase):

    def plan(self):
        return plan()

    def test_full_coverage_is_one(self):
        p = self.plan()
        rec = PS.reconcile(p, p['planned_keys'], p['planned_keys'])
        self.assertEqual(1.0, rec['usable_coverage'])
        self.assertEqual(1.0, rec['diagnostic_coverage'])
        self.assertEqual(0, rec['missing_entry_keys'])

    def test_a_missing_slot_is_counted_per_key_and_named_by_slot(self):
        p = self.plan()
        keys = {k for k in p['planned_keys'] if k[1] != R7.mins('09:20')}
        rec = PS.reconcile(p, keys, keys)
        self.assertEqual(6, rec['missing_entry_keys'])
        self.assertEqual({'09:20': 6}, rec['missing_entry_slots'])
        self.assertAlmostEqual(336 / 342, rec['usable_coverage'], places=9)

    def test_an_observed_entry_outside_the_plan_does_not_inflate_coverage(self):
        p = self.plan()
        extra = set(p['planned_keys']) | {('NSE_FO|0', R7.mins('14:30'))}
        rec = PS.reconcile(p, extra, extra)
        self.assertEqual(1.0, rec['usable_coverage'])
        self.assertEqual(1, rec['observed_entries_outside_plan'])

    def test_entries_whose_target_falls_past_the_grid_are_counted_separately(self):
        p = self.plan()
        outside = {('NSE_FO|0', R7.mins('14:30')), ('NSE_FO|1', R7.mins('14:35'))}
        rec = PS.reconcile(p, p['planned_keys'], p['planned_keys'], outside)
        self.assertEqual(2, rec['admitted_entries_outside_planned_window'])
        self.assertEqual(1.0, rec['usable_coverage'])

    def test_a_refused_MANIFEST_still_reports_diagnostic_coverage_against_342(self):
        """Codex: preserve the independently planned count; if diagnostic coverage is shown it
        is 126/342 = 36.8%. `usable_coverage` is withheld because the session cannot enter the
        cohort, and the two are named differently so neither can be read as the other."""
        cropped = [s for s in SLOTS_69 if R7.mins(s) <= 12 * 60]
        p = plan(slots=cropped, expected_slots=33, observed_slots=33)
        # an entry at slot s can only score if its s+60 exit was captured, so a capture that
        # stops at 12:00 scores entries up to 11:00: 21 slots x 6 contracts = 126
        scored = {k for k in p['planned_keys'] if k[1] + 60 <= 12 * 60}
        self.assertEqual(126, len(scored))
        rec = PS.reconcile(p, scored, scored)
        self.assertEqual(342, rec['planned_opportunities'])
        self.assertEqual(126, rec['scored_in_plan'])
        self.assertAlmostEqual(0.368, rec['diagnostic_coverage'], places=3)
        self.assertIsNone(rec['usable_coverage'])
        self.assertIn('DIAGNOSTIC ONLY', rec['coverage_basis'])
        self.assertEqual(PS.STATE_MANIFEST_MISMATCH, rec['schedule_state'])

    def test_an_INCOMPLETE_plan_withholds_both_ratios_but_keeps_the_raw_counts(self):
        p = plan(contracts=6, contracts_expected=8)
        observed = {('NSE_FO|0', R7.mins('09:20'))}
        rec = PS.reconcile(p, observed, observed, observed)
        self.assertEqual(PS.STATE_INCOMPLETE_CONTRACT_COUNT, rec['schedule_state'])
        self.assertIsNone(rec['usable_coverage'])
        self.assertIsNone(rec['diagnostic_coverage'])
        self.assertEqual(1, rec['observed_entries_total'])
        self.assertEqual(1, rec['admitted_entries_outside_planned_window'])
        self.assertIn('WITHHELD', rec['coverage_basis'])

    def test_the_corrected_invariant_holds_on_a_verified_plan(self):
        """scored_in_plan <= observed_entries_in_plan <= planned_opportunities, with late
        quotes counted apart. `planned >= admitted` is NOT an invariant: a complete capture
        admits 414 quotes against 342 sixty-minute opportunities."""
        p = self.plan()
        in_plan = set(sorted(p['planned_keys'])[:100])
        late = {('NSE_FO|0', R7.mins('14:30')), ('NSE_FO|1', R7.mins('14:35'))}
        rec = PS.reconcile(p, in_plan | late, set(sorted(in_plan)[:40]), late)
        self.assertLessEqual(rec['scored_in_plan'], rec['observed_entries_in_plan'])
        self.assertLessEqual(rec['observed_entries_in_plan'], rec['planned_opportunities'])
        self.assertEqual(rec['observed_entries_total'],
                         rec['observed_entries_in_plan'] + rec['observed_entries_outside_plan'])
        self.assertEqual(2, rec['admitted_entries_outside_planned_window'])

    def test_the_versions_used_travel_with_the_reconciliation(self):
        """So a result can be tied to the rules and calendar that produced it."""
        rec = PS.reconcile(self.plan(), (), ())
        self.assertEqual(PS.SCHEDULE_VERSION, rec['schedule_version'])
        self.assertEqual(PS.CAPTURE_WINDOW_RULES_VERSION,
                         rec['capture_window_rules_version'])
        self.assertEqual(PS.SESSION_CALENDAR_VERSION, rec['session_calendar_version'])
        self.assertEqual(PS.BASIS_AUTHORITATIVE, rec['schedule_basis'])


if __name__ == '__main__':
    unittest.main()
