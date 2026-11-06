"""D9 repair, review round 3: a recovered scale must become DURABLE.

Codex ran the real authority path with a stored scale failure followed by a successful late recovery.
The poll returned BULL, but the saved scale stayed UNAVAILABLE, because index_authority_store.merge
wrote a scale only when that index had none ("scales are write-once"). The next poll without a fresh
chain went UNAVAILABLE again -- the recovery was per-poll and never persisted.

Reproduced before fixing, through brain.compute_index_authority on a real state dir:
    poll 1 (no chain)      -> UNAVAILABLE, SAVED status=UNAVAILABLE
    poll 2 (chain present) -> BULL, recovered=True, SAVED status=UNAVAILABLE   <-- the defect
    poll 3 (no chain)      -> UNAVAILABLE

merge now permits exactly one transition, recoverable failure -> OK, and nothing else. These tests
pin both halves: the upgrade happens, and an OK scale stays immutable.

Codex also noted that the Kotlin request-count test supplies a simulated saved OK state and so does
not exercise this transition. That is correct; persistence is Python-side, and the authority-path
tests below are what cover it. DirectionScaleNeedTest now says so and names this module.
"""
import json
import os
import re
import shutil
import tempfile
import unittest

import brain
import index_authority_fixtures as F
import index_authority_store as ias
import index_authority_v1 as ia

SESSION = '2026-11-09'          # NF's own expiry day (Tue 10 Nov 2026 is an NSE holiday)
NEXT = '2026-11-17'
BNF_EXPIRY = '2026-11-23'


def ok_scale(m=0.0095, **kw):
    d = {'index': 'NF', 'status': 'OK', 'reason': None, 'value': m, 'method': 'atm_straddle_sqrt_2_over_pi_v1'}
    d.update(kw)
    return d


def failed_scale(reason, **kw):
    d = {'index': 'NF', 'status': 'UNAVAILABLE', 'reason': reason, 'value': None}
    d.update(kw)
    return d


# ───────────────────────────────── store-level invariant ─────────────────────────────────
class TestScaleUpgradeInvariant(unittest.TestCase):
    def merged(self, cur, new):
        doc = {'scales': {'NF': cur}} if cur is not None else {}
        return ias.merge(doc, {'scales': {'NF': new}})['scales'].get('NF')

    def test_recoverable_failure_upgrades_to_ok(self):
        got = self.merged(failed_scale('EXPIRY_DAY_CHAIN_ONLY'), ok_scale(0.0091))
        self.assertEqual(got['status'], 'OK')
        self.assertEqual(got['value'], 0.0091)

    def test_every_recoverable_reason_upgrades(self):
        for reason in ia.SCALE_RECOVERABLE_REASONS:
            got = self.merged(failed_scale(reason), ok_scale(0.0088))
            self.assertEqual(got['status'], 'OK', reason)

    def test_ok_scale_is_immutable(self):
        """Freezing the open-reference observation is the point: a later poll must never move the
        yardstick a session has already been measured against."""
        got = self.merged(ok_scale(0.0095), ok_scale(0.0300))
        self.assertEqual(got['value'], 0.0095)

    def test_ok_scale_is_not_downgraded_by_a_failure(self):
        got = self.merged(ok_scale(0.0095), failed_scale('EXPIRY_DAY_CHAIN_ONLY'))
        self.assertEqual(got['status'], 'OK')
        self.assertEqual(got['value'], 0.0095)

    def test_failure_is_never_replaced_by_another_failure(self):
        """Otherwise a scale could churn between reasons poll after poll."""
        got = self.merged(failed_scale('EXPIRY_DAY_CHAIN_ONLY'), failed_scale('NEXT_EXPIRY_CHAIN_EMPTY'))
        self.assertEqual(got['reason'], 'EXPIRY_DAY_CHAIN_ONLY')

    def test_unrecoverable_failure_is_never_upgraded(self):
        for reason in ('OPEN_REF_MISSING', 'SPOT_INVALID', 'SCALE_INVALID', 'NO_VALID_PARITY_PAIR',
                       'EXPIRY_INVALID'):
            got = self.merged(failed_scale(reason), ok_scale())
            self.assertEqual(got['status'], 'UNAVAILABLE', reason)
            self.assertEqual(got['reason'], reason, reason)

    def test_absent_scale_is_written(self):
        self.assertEqual(self.merged(None, ok_scale(0.0091))['value'], 0.0091)
        self.assertEqual(self.merged(None, failed_scale('EXPIRY_DAY_CHAIN_ONLY'))['reason'],
                         'EXPIRY_DAY_CHAIN_ONLY')

    def test_non_dict_delta_is_ignored(self):
        for bad in (None, 'OK', 7, []):
            doc = {'scales': {'NF': failed_scale('EXPIRY_DAY_CHAIN_ONLY')}}
            got = ias.merge(doc, {'scales': {'NF': bad}})['scales']['NF']
            self.assertEqual(got['reason'], 'EXPIRY_DAY_CHAIN_ONLY', repr(bad))

    def test_upgrade_does_not_disturb_the_other_index(self):
        doc = {'scales': {'NF': failed_scale('EXPIRY_DAY_CHAIN_ONLY'),
                          'BNF': ok_scale(0.0092, index='BNF')}}
        out = ias.merge(doc, {'scales': {'NF': ok_scale(0.0091)}})
        self.assertEqual(out['scales']['NF']['status'], 'OK')
        self.assertEqual(out['scales']['BNF']['value'], 0.0092)

    def test_upgrade_does_not_rewrite_history(self):
        doc = {'scales': {'NF': failed_scale('EXPIRY_DAY_CHAIN_ONLY')},
               'history': {'NF': [{'decision_id': 'd1', 'direction': 'UNAVAILABLE'}]}}
        out = ias.merge(doc, {'scales': {'NF': ok_scale()}})
        self.assertEqual(out['history']['NF'], [{'decision_id': 'd1', 'direction': 'UNAVAILABLE'}])


# ───────────────────────────── through the real authority path ─────────────────────────────
class ScalePathCase(unittest.TestCase):
    deferred = False

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sd = os.path.join(self.tmp, 'index_authority_v1')
        t = F.times('09:15', '11:00', 5)
        n = len(t)
        self.polls = F.polls(F.linear(24000, 9, n),
                             [52000 + ((-1) ** i) * 20 for i in range(n)], ts=t)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ctx(self, chain=None, expiry=None):
        c = F.ctx(session_date=SESSION, today_ist=SESSION, nfExpiry=SESSION, bnfExpiry=BNF_EXPIRY,
                  nfChain=F.chain(24000, 50, lot=65), bnfChain=F.chain(52000, 100, lot=30),
                  indexAuthorityStateDir=self.sd)
        if self.deferred:
            c['indexAuthorityDeferredWrite'] = True
        if chain is not None:
            c['nfScaleChain'] = chain
        if expiry is not None:
            c['nfScaleExpiry'] = expiry
        return c

    def run_poll(self, polls, c):
        return brain.compute_index_authority(polls, F.with_clock(c, polls))

    def saved_scale(self, ix='NF'):
        return (ias.load(self.sd, SESSION) or {}).get('scales', {}).get(ix)

    # the sequence Codex ran
    def test_recovery_is_durable_and_survives_a_later_poll_with_no_chain(self):
        first = self.run_poll(self.polls[:2], self.ctx())
        self.assertEqual(first['NF']['direction']['value'], 'UNAVAILABLE')
        self.assertEqual(self.saved_scale()['status'], 'UNAVAILABLE')

        second = self.run_poll(self.polls, self.ctx(F.chain(24180, 50, lot=65), NEXT))
        d2 = second['NF']['direction']
        self.assertNotEqual(d2['value'], 'UNAVAILABLE', second['NF']['reason_codes'])
        self.assertTrue(d2['inputs']['scale']['recovered'])

        # the recovery must now be in the SAVED state, not just in this poll's output
        saved = self.saved_scale()
        self.assertEqual(saved['status'], 'OK')
        self.assertIsNotNone(saved['value'])
        self.assertTrue(saved['recovered'])
        self.assertFalse(saved['open_ref_observation'])

        # a later poll with NO chain at all must still have a direction, read from saved state
        third = self.run_poll(self.polls, self.ctx())
        d3 = third['NF']['direction']
        self.assertNotEqual(d3['value'], 'UNAVAILABLE', third['NF']['reason_codes'])
        self.assertEqual(d3['inputs']['scale']['value'], saved['value'])

    def test_a_recovered_scale_is_then_immutable(self):
        self.run_poll(self.polls[:2], self.ctx())
        self.run_poll(self.polls, self.ctx(F.chain(24180, 50, lot=65), NEXT))
        m = self.saved_scale()['value']
        # a later poll offering a very differently priced chain must not move it
        self.run_poll(self.polls, self.ctx(F.chain(24180, 50, lot=65, tv_frac=0.05), NEXT))
        self.assertEqual(self.saved_scale()['value'], m)

    def test_an_open_reference_ok_scale_is_never_replaced_by_a_recovery(self):
        kw = dict(chain=F.chain(24000, 50, lot=65), expiry=NEXT)
        self.run_poll(self.polls[:2], self.ctx(**kw))
        saved = self.saved_scale()
        self.assertEqual(saved['status'], 'OK')
        self.assertTrue(saved['open_ref_observation'])
        m = saved['value']
        self.run_poll(self.polls, self.ctx(F.chain(24180, 50, lot=65, tv_frac=0.05), NEXT))
        again = self.saved_scale()
        self.assertEqual(again['value'], m)
        self.assertTrue(again['open_ref_observation'])
        self.assertNotIn('recovered', again)

    def test_history_records_written_before_the_upgrade_are_unchanged(self):
        first = self.run_poll(self.polls[:2], self.ctx())
        before = json.dumps((first['_meta']['history'] or {}).get('NF'), sort_keys=True)
        self.run_poll(self.polls, self.ctx(F.chain(24180, 50, lot=65), NEXT))
        after_doc = ias.load(self.sd, SESSION) or {}
        after = after_doc.get('history', {}).get('NF') or []
        self.assertEqual(json.dumps(after[:1], sort_keys=True),
                         json.dumps(json.loads(before)[:1], sort_keys=True))

    def test_unrecoverable_failure_stays_unavailable_across_polls(self):
        """A scale the chain cannot fix must not be quietly upgraded by this change."""
        doc = {'scales': {'NF': failed_scale('SCALE_INVALID')}}
        os.makedirs(self.sd, exist_ok=True)
        with open(os.path.join(self.sd, SESSION + '.json'), 'w') as f:
            json.dump(doc, f)
        out = self.run_poll(self.polls, self.ctx(F.chain(24180, 50, lot=65), NEXT))
        self.assertEqual(out['NF']['direction']['value'], 'UNAVAILABLE')
        self.assertEqual(self.saved_scale()['reason'], 'SCALE_INVALID')


class TestDirectWritePath(ScalePathCase):
    deferred = False


class TestDeferredWritePath(ScalePathCase):
    """The same invariant through indexAuthorityDeferredWrite, where the delta lands in the pending
    accumulator and is merged on the next load."""
    deferred = True


class TestKotlinFixtureIsTheRealPayload(unittest.TestCase):
    """Codex's R3 review caught DirectionScaleNeedTest carrying the right 22-field SHAPE for a
    recovered scale but inventing its ce_mid, pe_mid, forward, n_sessions and recovered_spot while the
    comment claimed they were captured from a real run.  The literals were corrected to the real
    payload; this test re-captures that payload and asserts the Kotlin literals still match it, so the
    claim cannot quietly become false again."""

    # tests/ -> python -> main -> src, then src/test/java/...
    KOTLIN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          '..', '..', '..', 'test', 'java', 'com', 'marketradar', 'app',
                          'DirectionScaleNeedTest.kt')

    def captured(self):
        tmp = tempfile.mkdtemp()
        try:
            sd = os.path.join(tmp, 'index_authority_v1')
            t = F.times('09:15', '11:00', 5)
            n = len(t)
            polls = F.polls(F.linear(24000, 9, n),
                            [52000 + ((-1) ** i) * 20 for i in range(n)], ts=t)

            def ctx(chain=None, expiry=None):
                c = F.ctx(session_date=SESSION, today_ist=SESSION, nfExpiry=SESSION,
                          bnfExpiry=BNF_EXPIRY, nfChain=F.chain(24000, 50, lot=65),
                          bnfChain=F.chain(52000, 100, lot=30), indexAuthorityStateDir=sd)
                if chain is not None:
                    c['nfScaleChain'] = chain
                if expiry is not None:
                    c['nfScaleExpiry'] = expiry
                return c

            brain.compute_index_authority(polls[:2], F.with_clock(ctx(), polls[:2]))
            brain.compute_index_authority(
                polls, F.with_clock(ctx(F.chain(24180, 50, lot=65), NEXT), polls))
            return ias.load(sd, SESSION)['scales']['NF']
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_capture_is_a_successful_recovery(self):
        sc = self.captured()
        self.assertEqual(sc['status'], 'OK')
        self.assertTrue(sc['recovered'])
        self.assertFalse(sc['open_ref_observation'])

    def test_kotlin_literals_match_the_captured_payload(self):
        sc = self.captured()
        with open(self.KOTLIN) as f:
            src = f.read()
        block = src[src.index('fun frozenRecoveredScaleFromTheRealStoreStopsFetching'):]
        block = block[:block.index('val d = DirectionScaleNeed.decide')]
        # the fixture may reference the test's own constants instead of inlining a literal
        consts = dict(re.findall(r'private val (\w+)\s*=\s*"([^"]+)"', src))
        checked = 0
        for key in ('value', 'k0', 'ce_mid', 'pe_mid', 'forward', 'n_sessions', 'recovered_spot',
                    'method', 'expiry', 'trade_expiry', 'observed_at', 'recovered_at',
                    'calendar_rule_version', 'chain_source', 'chain_source_version',
                    'recovered_from_reason'):
            val = sc[key]
            if isinstance(val, bool):
                continue
            if isinstance(val, str):
                accepted = ['"%s"' % val] + [n for n, v in consts.items() if v == val]
            else:
                accepted = [repr(val)]
            self.assertTrue(any(a in block for a in accepted),
                            'Kotlin fixture is missing the real %s=%r (looked for %s)'
                            % (key, val, ' or '.join(accepted)))
            checked += 1
        self.assertGreaterEqual(checked, 14)
        # and the constants it leans on must actually hold the captured values
        self.assertEqual(consts.get('NEXT'), sc['expiry'])
        self.assertEqual(consts.get('TODAY'), sc['trade_expiry'])

    def test_the_fixture_comment_does_not_overclaim(self):
        with open(self.KOTLIN) as f:
            src = f.read()
        head = src[:src.index('fun frozenRecoveredScaleFromTheRealStoreStopsFetching')]
        doc = head[head.rindex('/**'):]
        self.assertIn('captured verbatim', doc)
        self.assertIn('test_expiry_day_scale_durability', doc)


# the base class is abstract only in intent; both subclasses run every case above.
del ScalePathCase


if __name__ == '__main__':
    unittest.main(verbosity=2)
