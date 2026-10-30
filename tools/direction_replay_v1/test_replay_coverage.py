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
        recs = [{'polls': polls[:i + 1], 'ctx': F.ctx(polls[i]['nfSpot'], 52000.0)} for i in range(len(polls))]
        out = R.replay({'sessions': [{'date': F.SESSION, 'decisions': recs}]}, brain)
        self.assertEqual(out['decisions'], len(polls))
        bnf, nf = out['by_index']['BNF'], out['by_index']['NF']
        self.assertEqual(bnf['MISSING_EVIDENCE'], len(polls))
        self.assertGreaterEqual(nf['POLICY_NO_TRADE'] + nf['POLICY_CANDIDATES'], len(polls) - 3)

    def test_bounds(self):
        out = R.replay({'sessions': [{'decisions': []}] * (R.MAX_SESSIONS + 1)}, brain)
        self.assertTrue(out['truncated'])
        self.assertEqual(out['sessions'], R.MAX_SESSIONS)

    def test_unavailable_authority_is_missing_evidence(self):
        self.assertEqual(R.classify(None)[0], 'MISSING_EVIDENCE')


if __name__ == '__main__':
    unittest.main()
