# -*- coding: utf-8 -*-
"""Guards for the Paper-intent feasibility probe.

The probe's answer is "infeasible", and the one way that answer could be wrong is if the
matching were too strict -- so the tests construct a case that MUST match and check it does,
before asserting that the real data does not.
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import paper_intent_feasibility as PF      # noqa: E402

RESULTS = os.path.join(ROOT, 'results', 'span')


def export(intents, ladder=((25400, 'CE'), (25450, 'CE'), (25400, 'PE'))):
    return {'manifest': {'session_date_used': '2026-11-20'},
            'parts': {'authority_doc': {'history': {'NF': [
                {'why': {'sl': {'a': a, 'k': k, 'ot': t}}} for a, k, t in intents]}}},
            'premium': {'indices': {'NF': {'cohort': {'pinned_contracts': [
                {'strike': s, 'option_type': t} for s, t in ladder]}}}}}


class Matching(unittest.TestCase):
    def test_the_strike_and_the_option_type_must_both_match(self):
        """The first case here MUST match. If it did not, "infeasible" would be an artefact of
        an over-strict matcher rather than a fact about the capture."""
        import tempfile

        def probe(intents):
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, 'export_2026-11-20.json')
                with open(p, 'w', encoding='utf-8') as fh:
                    json.dump(export(intents), fh)
                return PF.run([p])

        # the exact pinned contract matches
        got = probe([('BUY_CE', 25450, 'CE')])
        self.assertEqual(got['pooled']['debit_priceable'], 1)
        self.assertEqual(got['verdict'], 'PARTIALLY_FEASIBLE')
        # the right strike with the WRONG option type must not match
        got = probe([('BUY_PE', 25450, 'PE')])
        self.assertEqual(got['pooled']['debit_priceable'], 0)
        self.assertEqual(got['verdict'], 'INFEASIBLE_NO_QUOTE_FOR_ANY_INTENT')
        # a strike off the ladder records its offset rather than snapping to a neighbour
        got = probe([('BUY_CE', 25600, 'CE')])
        self.assertEqual(got['pooled']['debit_priceable'], 0)
        self.assertEqual(got['sessions']['2026-11-20']['indices']['NF']
                         ['offsets_from_ladder'], [150])
        # credit intents are counted apart from debit ones
        got = probe([('SELL_CE', 25450, 'CE')])
        self.assertEqual(got['pooled']['credit_intents'], 1)
        self.assertEqual(got['pooled']['debit_intents'], 0)
        self.assertEqual(got['verdict'], 'NO_DEBIT_INTENTS_RECORDED')

    def test_a_missing_trail_is_reported_not_counted_as_zero_intents(self):
        import tempfile
        ex = export([])
        ex['parts']['authority_doc'] = None
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'export_2026-11-20.json')
            with open(p, 'w', encoding='utf-8') as fh:
                json.dump(ex, fh)
            got = PF.run([p])
        self.assertEqual(got['sessions']['2026-11-20']['state'], 'NO_DECISION_TRAIL')


class AgainstTheSpan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        p = os.path.join(RESULTS, 'paper_intent_feasibility.json')
        if not os.path.exists(p):
            raise unittest.SkipTest('no committed paper_intent_feasibility.json')
        with open(p, encoding='utf-8') as fh:
            cls.out = json.load(fh)

    def test_not_one_intent_can_be_priced(self):
        p = self.out['pooled']
        self.assertEqual(self.out['verdict'], 'INFEASIBLE_NO_QUOTE_FOR_ANY_INTENT')
        self.assertGreater(p['debit_intents'], 100)
        self.assertEqual(p['debit_priceable'], 0)
        self.assertEqual(p['credit_priceable'], 0)
        self.assertEqual(p['debit_priceable_share'], 0.0)

    def test_the_offset_grows_with_the_move(self):
        """20 November BANKNIFTY rose 2,007 points and is the worst offset in the span. That is
        the mechanism, as a number."""
        self.assertGreaterEqual(self.out['pooled']['worst_debit_offset_points'], 1000)
        bnf20 = self.out['sessions']['2026-11-20']['indices']['BNF']
        self.assertEqual(bnf20['debit_priceable'], 0)
        self.assertGreaterEqual(bnf20['worst_offset_points'], 1000)

    def test_no_substitution_is_offered(self):
        self.assertIn('DIFFERENT trade', self.out['refusal'])


if __name__ == '__main__':
    unittest.main()
