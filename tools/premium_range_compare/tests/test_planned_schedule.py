"""Checks for the planned opportunity set.

The defect these exist for: the denominator used to be the entry candidates left AFTER quote
admission, which rises as a capture gets worse. A capture truncated at 12:00 reported 62.5% and
passed a 60% gate when its honest figure against the schedule was 35.1%.

What is pinned here: the plan is derived from the export's own declared schedule, it never reads
a quote, an eligible entry slot is one whose PLANNED TARGET is still inside the grid, and
anything that cannot be verified is returned as an explicit INCOMPLETE state rather than
guessed.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), '..', 'spot_path_baseline'))

import premium_repricing_v2 as R7          # noqa: E402
import planned_schedule as PS              # noqa: E402

SLOTS_69 = ['%02d:%02d' % (m // 60, m % 60)
            for m in range(9 * 60 + 20, 15 * 60 + 5, 5)]


def node(slots=SLOTS_69, contracts=6, observed=None, expected_slots=None,
         contracts_expected=None, with_manifest=True):
    observed = set(slots if observed is None else observed)
    pins = [{'instrument_key': 'NSE_FO|%d' % i, 'strike': 25000.0 + 50 * (i // 2),
             'option_type': 'CE' if i % 2 == 0 else 'PE', 'expiry': '2026-11-17',
             'underlying': 'NIFTY'} for i in range(contracts)]
    out = {'cohort': {'pinned_contracts': pins,
                      'contracts_expected': (contracts if contracts_expected is None
                                             else contracts_expected)}}
    if with_manifest:
        out['slot_manifest'] = {
            'expected_slots': len(slots) if expected_slots is None else expected_slots,
            'observed_slots': len(observed),
            'rows': [{'slot': s, 'state': 'OBSERVED' if s in observed
                      else 'NOT_OBSERVED_UNEXPLAINED'} for s in slots]}
    return out


class Grid(unittest.TestCase):

    def test_the_declared_pilot_window_is_69_five_minute_slots(self):
        """And the last of them is 15:00, because the close minute is excluded. This is the
        figure the real export's own manifest declares, which is why the fallback is usable."""
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

    def test_a_horizon_longer_than_the_grid_leaves_nothing_eligible(self):
        self.assertEqual([], PS.eligible_entry_slots(PS.window_grid(), 24 * 60))
        self.assertEqual([], PS.eligible_entry_slots([], 60))


class PlanFor(unittest.TestCase):

    def test_the_manifest_is_preferred_and_gives_57_x_6_at_60_minutes(self):
        """342, which is what Codex derived independently for the 16 November capture."""
        p = PS.plan_for(node(), 'NF', 60)
        self.assertEqual(PS.STATE_VERIFIED, p['state'])
        self.assertEqual(PS.BASIS_MANIFEST, p['basis'])
        self.assertEqual(69, p['grid_slots'])
        self.assertEqual(57, len(p['eligible_entry_slots']))
        self.assertEqual(342, p['planned_opportunities'])
        self.assertEqual(12, p['slots_outside_horizon'])

    def test_the_plan_does_not_shrink_when_the_capture_does(self):
        """The whole point. A capture that stopped at 12:00 has the SAME plan."""
        full = PS.plan_for(node(), 'NF', 60)
        stopped = PS.plan_for(node(observed=[s for s in SLOTS_69 if R7.mins(s) < 12 * 60]),
                              'NF', 60)
        self.assertEqual(full['planned_opportunities'], stopped['planned_opportunities'])
        self.assertEqual(full['planned_keys'], stopped['planned_keys'])

    def test_without_a_manifest_the_window_is_a_declared_FALLBACK_and_says_so(self):
        p = PS.plan_for(node(with_manifest=False), 'NF', 60)
        self.assertEqual(PS.STATE_VERIFIED, p['state'])
        self.assertEqual(PS.BASIS_WINDOW, p['basis'])
        self.assertEqual(342, p['planned_opportunities'])
        self.assertTrue(any('assumption about the producing build' in n for n in p['notes']))

    def test_an_unresolvable_cohort_is_an_explicit_incomplete_state(self):
        p = PS.plan_for({'slot_manifest': {'rows': [{'slot': '09:20', 'state': 'OBSERVED'}]}},
                        'NF', 60)
        self.assertEqual(PS.STATE_INCOMPLETE_NO_COHORT, p['state'])
        self.assertEqual(0, p['planned_opportunities'])

    def test_a_cohort_that_disagrees_with_its_own_expected_count_is_incomplete(self):
        p = PS.plan_for(node(contracts=6, contracts_expected=8), 'NF', 60)
        self.assertEqual(PS.STATE_INCOMPLETE_CONTRACT_COUNT, p['state'])
        self.assertEqual(0, p['planned_opportunities'])
        self.assertTrue(any('expects 8 contracts, 6 pinned' in n for n in p['notes']))

    def test_a_grid_with_no_room_for_the_horizon_is_incomplete(self):
        p = PS.plan_for(node(slots=['09:20', '09:25']), 'NF', 60)
        self.assertEqual(PS.STATE_INCOMPLETE_EMPTY_GRID, p['state'])
        self.assertEqual(0, p['planned_opportunities'])

    def test_a_manifest_whose_row_count_disagrees_with_its_own_header_is_noted(self):
        p = PS.plan_for(node(expected_slots=70), 'NF', 60)
        self.assertEqual(PS.STATE_VERIFIED, p['state'], 'noted, not fatal')
        self.assertTrue(any('declares 70 expected slots and carries 69' in n
                            for n in p['notes']))

    def test_the_per_slot_state_is_carried_so_a_missing_slot_has_its_own_reason(self):
        missing = [s for s in SLOTS_69 if R7.mins(s) >= R7.mins('13:45')]
        p = PS.plan_for(node(observed=[s for s in SLOTS_69 if s not in missing]), 'NF', 60)
        self.assertEqual('NOT_OBSERVED_UNEXPLAINED', p['slot_states']['13:45'])
        self.assertEqual('OBSERVED', p['slot_states']['09:20'])


class Reconcile(unittest.TestCase):

    def plan(self):
        return PS.plan_for(node(), 'NF', 60)

    def test_full_coverage_is_one(self):
        p = self.plan()
        rec = PS.reconcile(p, p['planned_keys'], p['planned_keys'])
        self.assertEqual(1.0, rec['usable_coverage'])
        self.assertEqual(0, rec['missing_entry_keys'])
        self.assertEqual(0, rec['observed_but_not_scored'])

    def test_a_missing_slot_is_counted_per_key_and_named_by_slot(self):
        p = self.plan()
        keys = {k for k in p['planned_keys'] if k[1] != R7.mins('09:20')}
        rec = PS.reconcile(p, keys, keys)
        self.assertEqual(6, rec['missing_entry_keys'])
        self.assertEqual({'09:20': 6}, rec['missing_entry_slots'])
        self.assertAlmostEqual(336 / 342, rec['usable_coverage'], places=9)

    def test_an_observed_entry_outside_the_plan_does_not_inflate_coverage(self):
        """A quote for a slot the schedule never planned is counted apart, never credited."""
        p = self.plan()
        extra = set(p['planned_keys']) | {('NSE_FO|0', R7.mins('14:30'))}
        rec = PS.reconcile(p, extra, extra)
        self.assertEqual(1.0, rec['usable_coverage'], 'capped by the plan, not above it')
        self.assertEqual(1, rec['observed_entries_outside_plan'])

    def test_entries_whose_target_falls_past_the_grid_are_counted_separately(self):
        p = self.plan()
        outside = {('NSE_FO|0', R7.mins('14:30')), ('NSE_FO|1', R7.mins('14:35'))}
        rec = PS.reconcile(p, p['planned_keys'], p['planned_keys'], outside)
        self.assertEqual(2, rec['admitted_entries_outside_planned_window'])
        self.assertEqual(1.0, rec['usable_coverage'])

    def test_the_schedule_state_travels_with_the_reconciliation(self):
        p = PS.plan_for(node(contracts=6, contracts_expected=8), 'NF', 60)
        rec = PS.reconcile(p, (), ())
        self.assertEqual(PS.STATE_INCOMPLETE_CONTRACT_COUNT, rec['schedule_state'])
        self.assertIsNone(rec['usable_coverage'])


if __name__ == '__main__':
    unittest.main()
