import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', '..', 'app', 'src', 'main', 'python'))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'app', 'src', 'main', 'python', 'tests'))
import brain
import index_authority_fixtures as F
import replay_coverage as R


class ReplayCoverage(unittest.TestCase):
    def test_missing_evidence_separated_from_policy(self):
        t = F.times('09:15', '10:30')
        polls = F.polls(F.linear(24000, 6, len(t)), F.linear(52000, 0, len(t)), t)
        for p in polls:
            p.pop('bnfSpot', None)          # BNF evidence absent for the whole session
            p.pop('bnf', None)
        recs = [{'polls': polls[:i + 1], 'ctx': F.ctx(polls[i]['nfSpot'], 52000.0),
                 'analysis_clock_ms': F.clock_ms(polls[:i + 1])} for i in range(len(polls))]
        out = R.replay({'sessions': [{'date': F.SESSION, 'decisions': recs}]}, brain)
        self.assertEqual(out['decisions'], len(polls))
        bnf, nf = out['by_index']['BNF'], out['by_index']['NF']
        self.assertEqual(bnf['MISSING_EVIDENCE'], len(polls))
        self.assertGreaterEqual(nf['POLICY_NO_TRADE'] + nf['POLICY_CANDIDATES'], len(polls) - 3)

    def test_frozen_clock_required_and_deterministic(self):
        t = F.times('09:15', '10:30')
        polls = F.polls(F.linear(24000, 6, len(t)), F.linear(52000, 0, len(t)), t)
        recs = [{'polls': polls[:i + 1], 'ctx': F.ctx(polls[i]['nfSpot'], 52000.0),
                 'analysis_clock_ms': F.clock_ms(polls[:i + 1])} for i in range(len(polls))]
        a = R.replay({'sessions': [{'decisions': recs}]}, brain)
        b = R.replay({'sessions': [{'decisions': recs}]}, brain)
        self.assertEqual(a, b)
        noclock = [dict(r, analysis_clock_ms=None) for r in recs]
        c = R.replay({'sessions': [{'decisions': noclock}]}, brain)
        self.assertEqual(c['by_index']['NF']['MISSING_EVIDENCE'], len(recs))
        self.assertIn('ANALYSIS_CLOCK_MISSING', c['by_index']['NF']['missing_codes'])
        # a replay clock two hours after each poll: every decision is stale
        late = [dict(r, analysis_clock_ms=r['analysis_clock_ms'] + 2 * 3600 * 1000) for r in recs]
        d = R.replay({'sessions': [{'decisions': late}]}, brain)
        self.assertEqual(d['by_index']['NF']['POLICY_NO_TRADE'] + d['by_index']['NF']['POLICY_CANDIDATES'], 0)

    def test_bounds(self):
        out = R.replay({'sessions': [{'decisions': []}] * (R.MAX_SESSIONS + 1)}, brain)
        self.assertTrue(out['truncated'])
        self.assertEqual(out['sessions'], R.MAX_SESSIONS)

    def test_unavailable_authority_is_missing_evidence(self):
        self.assertEqual(R.classify(None)[0], 'MISSING_EVIDENCE')


if __name__ == '__main__':
    unittest.main()
