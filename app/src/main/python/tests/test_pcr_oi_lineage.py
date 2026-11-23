# -*- coding: utf-8 -*-
"""Fix 3b — `_traced_row_value` must record the winning tier without changing any value.

The defect this guards (E8, `tools/pcr_vix_iv/TEST5_PCR_OI_PROVENANCE_20261123.md`): on 11-23
November 2026 `bnf_pcr` disagreed with `bnf_total_put_oi / bnf_total_call_oi` at 695 of 695 polls
while `nf_pcr` agreed at 695 of 695, and the cause could only be narrowed by elimination because
the five-tier OI fallback and the three-tier PCR fallback both resolved silently.

Two properties are tested, and the first is the one that matters: tracing must be observationally
equivalent to the untraced resolver, or the lineage would be telemetry about a different number
than the one the app used.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain  # noqa: E402


def _untraced(triples):
    return brain._first_row_value(*[(src, keys) for _, src, keys in triples])


def _traced(triples):
    return brain._traced_row_value(*triples)


class TracedResolverEquivalence(unittest.TestCase):
    """The traced resolver must return exactly what the untraced one returns."""

    CASES = [
        # (name, triples)
        ('first tier wins', [
            ('chain', {'pcr': 1.25}, ('pcr',)),
            ('market_forces', {'pcr': 0.80}, ('pcr',)),
        ]),
        ('first tier empty, second wins', [
            ('chain', {}, ('pcr',)),
            ('market_forces', {'pcr': 0.80}, ('pcr',)),
        ]),
        ('first tier lacks the key', [
            ('profile', {'pcrZ1': 0.82, 'pcrZ2': 1.87}, ('pcr',)),
            ('chain', {'pcr': 1.0147}, ('pcr',)),
        ]),
        ('no tier has it', [
            ('profile', {'pcrZ1': 0.82}, ('pcr',)),
            ('chain', {}, ('pcr',)),
        ]),
        ('non-dict tier is skipped', [
            ('profile', None, ('pcr',)),
            ('chain', {'pcr': 0.9}, ('pcr',)),
        ]),
        ('alternate key spelling', [
            ('chain', {'totalCallOi': 17220000}, ('totalCallOI', 'totalCallOi')),
        ]),
        ('zero is a value, not a miss', [
            ('chain', {'pcr': 0.0}, ('pcr',)),
            ('market_forces', {'pcr': 1.5}, ('pcr',)),
        ]),
        ('string numeric is accepted the same way', [
            ('chain', {'pcr': '1.25'}, ('pcr',)),
        ]),
    ]

    def test_values_identical_to_untraced(self):
        for name, triples in self.CASES:
            with self.subTest(name):
                self.assertEqual(_traced(triples)[0], _untraced(triples),
                                 'traced value diverged from _first_row_value: ' + name)

    def test_winning_label_and_key(self):
        value, label, key, skipped = _traced(self.CASES[2][1])     # profile lacks 'pcr'
        self.assertAlmostEqual(value, 1.0147)
        self.assertEqual(label, 'chain')
        self.assertEqual(key, 'pcr')
        self.assertEqual(skipped, ['profile'])

    def test_alternate_key_is_named(self):
        value, label, key, _ = _traced(self.CASES[5][1])
        self.assertEqual(value, 17220000)
        self.assertEqual(label, 'chain')
        self.assertEqual(key, 'totalCallOi', 'must name the spelling that actually matched')

    def test_nothing_found_reports_every_tier_skipped(self):
        value, label, key, skipped = _traced(self.CASES[3][1])
        self.assertIsNone(value)
        self.assertIsNone(label)
        self.assertIsNone(key)
        self.assertEqual(skipped, ['profile', 'chain'])


class ReproducesTheBnfDivergence(unittest.TestCase):
    """The shape of E8: the PCR and the OI totals resolve from different tiers.

    This is the diagnostic the lineage block publishes as `pcr_and_oi_totals_same_source`.
    Before fix 3b nothing recorded it, so a 0.20-0.69 residual could only be attributed by
    eliminating alternatives one query at a time.
    """

    def setUp(self):
        # BNF: the profile carries pcrZ1/Z2/Z3 but NO plain 'pcr' (verified against a live
        # 23 November snapshot_market_profiles row), so the PCR drops to the live chain while
        # the totals come from a different tier.
        self.bnf_profile = {'pcrZ1': 0.82, 'pcrZ2': 1.87, 'pcrZ3': 0.68}
        self.bnf_chain = {'pcr': 1.0147}                      # no totals on this tier
        self.bnf_forces = {'bnf_total_call_oi': 20000000, 'bnf_total_put_oi': 16952000}
        # NF: both land on the chain, which is why NF reconciles on every poll.
        self.nf_chain = {'pcr': 1.20, 'totalCallOI': 155000000, 'totalPutOI': 186000000}

    def test_bnf_pcr_and_totals_come_from_different_tiers(self):
        _, pcr_src, _, _ = _traced([
            ('profile', self.bnf_profile, ('pcr',)),
            ('chain', self.bnf_chain, ('pcr',)),
        ])
        _, call_src, _, _ = _traced([
            ('chain', self.bnf_chain, ('totalCallOI', 'totalCallOi')),
            ('market_forces', self.bnf_forces, ('bnf_total_call_oi',)),
        ])
        self.assertEqual(pcr_src, 'chain')
        self.assertEqual(call_src, 'market_forces')
        self.assertNotEqual(pcr_src, call_src,
                            'this is the E8 shape: same-source must be reported False')

    def test_bnf_residual_is_material(self):
        pcr, _, _, _ = _traced([('profile', self.bnf_profile, ('pcr',)),
                                ('chain', self.bnf_chain, ('pcr',))])
        call, _, _, _ = _traced([('chain', self.bnf_chain, ('totalCallOI',)),
                                 ('market_forces', self.bnf_forces, ('bnf_total_call_oi',))])
        put, _, _, _ = _traced([('chain', self.bnf_chain, ('totalPutOI',)),
                                ('market_forces', self.bnf_forces, ('bnf_total_put_oi',))])
        residual = abs(put / call - pcr)
        self.assertGreater(residual, 0.1, 'the live residual ran 0.20-0.69; 0.0005 is the '
                                          'reconciliation bar, so this must be well above it')

    def test_nf_reconciles_from_one_tier(self):
        pcr, pcr_src, _, _ = _traced([('profile', {}, ('pcr',)),
                                      ('chain', self.nf_chain, ('pcr',))])
        call, call_src, _, _ = _traced([('chain', self.nf_chain, ('totalCallOI',))])
        put, put_src, _, _ = _traced([('chain', self.nf_chain, ('totalPutOI',))])
        self.assertEqual(pcr_src, call_src)
        self.assertEqual(call_src, put_src)
        # same tier, but the stored pcr is still only as good as the chain that wrote it:
        # agreement of SOURCE is not agreement of VALUE, and the lineage reports both.
        self.assertAlmostEqual(put / call, 1.2, places=1)

    def test_near_atm_absence_is_recorded_not_inferred(self):
        value, label, _, skipped = _traced([
            ('profile', self.bnf_profile, ('nearAtmPCR', 'near_atm_pcr')),
            ('chain', self.bnf_chain, ('nearAtmPCR', 'near_atm_pcr')),
        ])
        self.assertIsNone(value, 'bnf_near_atm_pcr was null at all 695 observed polls')
        self.assertIsNone(label)
        self.assertEqual(skipped, ['profile', 'chain'],
                         'absence must name the tiers that were tried, not fall back silently')


if __name__ == '__main__':
    unittest.main()
