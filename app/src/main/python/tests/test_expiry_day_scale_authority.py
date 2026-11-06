"""D9 repair, through brain.compute_index_authority (the real wiring, not just the helpers).

Covers the review instruction's required cases end to end:
  1. the 3 November NF outage, reproduced and then closed
  2. normal sessions for both indices, proven unaffected
  3. a FAILED scale already stored, then recovered on a later poll
  4. recovery applies to future polls only and never rewrites an appended history record

and the standing constraint: the TRADING chain is never substituted or modified.
"""
import copy
import os
import shutil
import tempfile
import unittest

import brain
import index_authority_fixtures as F
import index_authority_v1 as ia

NF_OWN_EXPIRY_SESSION = '2026-11-09'   # Mon 9 Nov 2026 is NF's expiry (Tue 10 Nov is an NSE holiday)
NF_NEXT_EXPIRY = '2026-11-17'          # the following Tuesday
BNF_EXPIRY = '2026-11-23'


def nf_times():
    return F.times(start='09:15', end='11:00', step=5)


def std_polls():
    t = nf_times()
    n = len(t)
    # NF rises enough to clear the BULL threshold once a usable scale exists; BNF chops
    return F.polls(F.linear(24000, 9, n), [52000 + ((-1) ** i) * 20 for i in range(n)], ts=t)


class ScaleAuthorityCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_dir = os.path.join(self.tmp, 'index_authority_v1')
        self.polls = std_polls()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ctx(self, session, nf_expiry, *, scale_chain=None, scale_expiry=None, state=True, **kw):
        c = F.ctx(session_date=session, today_ist=session, nfExpiry=nf_expiry, bnfExpiry=BNF_EXPIRY,
                  nfChain=F.chain(24000, 50, lot=65), bnfChain=F.chain(52000, 100, lot=30), **kw)
        if state:
            c['indexAuthorityStateDir'] = self.state_dir
        if scale_chain is not None:
            c['nfScaleChain'] = scale_chain
        if scale_expiry is not None:
            c['nfScaleExpiry'] = scale_expiry
        return c

    def run_authority(self, polls, c):
        return brain.compute_index_authority(polls, F.with_clock(c, polls))

    def observe_then(self, polls_later, c_open, c_later):
        """Observe the scale at the open reference (09:20 = polls[:2]), then evaluate later."""
        self.run_authority(self.polls[:2], c_open)
        return self.run_authority(polls_later, c_later)


class TestOwnExpiryDayOutage(ScaleAuthorityCase):
    def test_outage_reproduced_without_a_scale_chain(self):
        """Pre-repair behaviour: NF's own expiry day leaves NF with no direction."""
        c = self.ctx(NF_OWN_EXPIRY_SESSION, NF_OWN_EXPIRY_SESSION)
        out = self.observe_then(self.polls, c, self.ctx(NF_OWN_EXPIRY_SESSION, NF_OWN_EXPIRY_SESSION))
        dec = out['NF']
        self.assertEqual(dec['direction']['value'], 'UNAVAILABLE')
        # the precise reason is kept, so a missing chain is distinguishable from an invalid one
        self.assertTrue(any(r.startswith('SCALE_NEXT_EXPIRY') or r == 'SCALE_EXPIRY_DAY_CHAIN_ONLY'
                            for r in dec['reason_codes']), dec['reason_codes'])

    def test_outage_closed_with_a_next_expiry_chain(self):
        kw = dict(scale_chain=F.chain(24000, 50, lot=65), scale_expiry=NF_NEXT_EXPIRY)
        out = self.observe_then(
            self.polls,
            self.ctx(NF_OWN_EXPIRY_SESSION, NF_OWN_EXPIRY_SESSION, **kw),
            self.ctx(NF_OWN_EXPIRY_SESSION, NF_OWN_EXPIRY_SESSION, **kw))
        nf = out['NF']['direction']
        self.assertNotEqual(nf['value'], 'UNAVAILABLE', out['NF']['reason_codes'])
        self.assertIsNotNone(nf['z_open'])
        sc = nf['inputs']['scale']
        self.assertEqual(sc['chain_source'], ia.SCALE_SOURCE_NEXT)
        self.assertTrue(sc['open_ref_observation'])
        self.assertEqual(sc['expiry'], NF_NEXT_EXPIRY)
        self.assertEqual(sc['trade_expiry'], NF_OWN_EXPIRY_SESSION)

    def test_bnf_unaffected_while_nf_is_on_its_own_expiry_day(self):
        """Only the affected index changes path - the 3 Nov pattern was NF dark, BNF normal."""
        kw = dict(scale_chain=F.chain(24000, 50, lot=65), scale_expiry=NF_NEXT_EXPIRY)
        out = self.observe_then(
            self.polls,
            self.ctx(NF_OWN_EXPIRY_SESSION, NF_OWN_EXPIRY_SESSION, **kw),
            self.ctx(NF_OWN_EXPIRY_SESSION, NF_OWN_EXPIRY_SESSION, **kw))
        bnf = out['BNF']['direction']
        # BNF stays on the front path, so its scale is unstamped and its decision identity unmoved
        self.assertNotIn('chain_source', bnf['inputs']['scale'])
        self.assertEqual(bnf['inputs']['scale']['expiry'], BNF_EXPIRY)
        self.assertNotEqual(bnf['value'], 'UNAVAILABLE', out['BNF']['reason_codes'])
        # while NF, on its own expiry day, IS stamped as having used the next expiry
        self.assertEqual(out['NF']['direction']['inputs']['scale']['chain_source'],
                         ia.SCALE_SOURCE_NEXT)

    def test_trading_chain_is_never_substituted(self):
        """The scale chain must not reach the trading chain, and must not be the trading chain."""
        scale_chain = F.chain(24000, 50, lot=65)
        c = self.ctx(NF_OWN_EXPIRY_SESSION, NF_OWN_EXPIRY_SESSION,
                     scale_chain=scale_chain, scale_expiry=NF_NEXT_EXPIRY)
        trade_before = copy.deepcopy(c['nfChain'])
        self.run_authority(self.polls[:2], c)
        self.assertEqual(c['nfChain'], trade_before)
        self.assertIsNot(c['nfChain'], c['nfScaleChain'])

    def test_malformed_next_expiry_fails_closed_not_open(self):
        for bad in (NF_OWN_EXPIRY_SESSION, '2026-10-27', 'not-a-date'):
            kw = dict(scale_chain=F.chain(24000, 50, lot=65), scale_expiry=bad)
            out = self.observe_then(
                self.polls,
                self.ctx(NF_OWN_EXPIRY_SESSION, NF_OWN_EXPIRY_SESSION, **kw),
                self.ctx(NF_OWN_EXPIRY_SESSION, NF_OWN_EXPIRY_SESSION, **kw))
            self.assertEqual(out['NF']['direction']['value'], 'UNAVAILABLE', bad)
            shutil.rmtree(self.state_dir, ignore_errors=True)

    def test_empty_next_chain_fails_closed_not_open(self):
        kw = dict(scale_chain={'strikes': {}}, scale_expiry=NF_NEXT_EXPIRY)
        out = self.observe_then(
            self.polls,
            self.ctx(NF_OWN_EXPIRY_SESSION, NF_OWN_EXPIRY_SESSION, **kw),
            self.ctx(NF_OWN_EXPIRY_SESSION, NF_OWN_EXPIRY_SESSION, **kw))
        self.assertEqual(out['NF']['direction']['value'], 'UNAVAILABLE')


class TestNormalSessionUnaffected(ScaleAuthorityCase):
    """A forward-dated front expiry must behave identically whether or not a scale chain is present."""

    def _decisions(self, **kw):
        shutil.rmtree(self.state_dir, ignore_errors=True)
        c_open = self.ctx(F.SESSION, F.NF_EXPIRY, **kw)
        c_late = self.ctx(F.SESSION, F.NF_EXPIRY, **kw)
        out = self.observe_then(self.polls, c_open, c_late)
        return {ix: out[ix] for ix in ('NF', 'BNF')}

    def test_normal_session_identical_with_and_without_a_scale_chain(self):
        without = self._decisions()
        # a scale chain priced very differently: if it were consulted, these would diverge
        with_ = self._decisions(scale_chain=F.chain(24000, 50, lot=65, tv_frac=0.05),
                                scale_expiry='2026-12-29')
        for ix in ('NF', 'BNF'):
            for key in ('value', 'strength', 'z_open', 'z_30', 'er'):
                self.assertEqual(without[ix]['direction'][key], with_[ix]['direction'][key], f'{ix}.{key}')
            # decision identity is content-addressed over `direction`; it must not move either,
            # because entry_decision_id lineage and history record keys are built from it
            self.assertEqual(without[ix]['reason_codes'], with_[ix]['reason_codes'], ix)
            self.assertEqual(without[ix]['decision_id'], with_[ix]['decision_id'], ix)

    def test_normal_session_uses_the_front_expiry(self):
        d = self._decisions(scale_chain=F.chain(24000, 50, lot=65), scale_expiry='2026-12-29')
        sc = d['NF']['direction']['inputs']['scale']
        # the front path is deliberately UNSTAMPED: absence means 'front expiry, as before the repair'
        self.assertEqual(sc['expiry'], F.NF_EXPIRY)
        self.assertNotIn('chain_source', sc)
        self.assertNotIn('recovered', sc)
        self.assertNotIn('open_ref_observation', sc)

    def test_normal_session_nf_direction_is_actually_available(self):
        """Guard against the comparison above passing because both sides are UNAVAILABLE."""
        d = self._decisions()
        self.assertNotEqual(d['NF']['direction']['value'], 'UNAVAILABLE', d['NF']['reason_codes'])
        self.assertIsNotNone(d['NF']['direction']['inputs']['scale']['value'])


class TestStoredFailureRecovery(ScaleAuthorityCase):
    """A scale failure stored at the open reference, then recovered once a chain arrives."""

    def _store_failure(self):
        c = self.ctx(NF_OWN_EXPIRY_SESSION, NF_OWN_EXPIRY_SESSION)
        first = self.run_authority(self.polls[:2], c)
        self.assertEqual(first['NF']['direction']['value'], 'UNAVAILABLE')
        return first

    def test_stored_failure_recovers_when_a_chain_becomes_available(self):
        self._store_failure()
        c = self.ctx(NF_OWN_EXPIRY_SESSION, NF_OWN_EXPIRY_SESSION,
                     scale_chain=F.chain(24180, 50, lot=65), scale_expiry=NF_NEXT_EXPIRY)
        later = self.run_authority(self.polls, c)
        nf = later['NF']['direction']
        self.assertNotEqual(nf['value'], 'UNAVAILABLE', later['NF']['reason_codes'])
        sc = nf['inputs']['scale']
        self.assertTrue(sc['recovered'])
        self.assertFalse(sc['open_ref_observation'])     # a LATE observation, and it says so
        self.assertEqual(sc['chain_source'], ia.SCALE_SOURCE_NEXT)
        self.assertIsNotNone(sc['recovered_at'])

    def test_recovery_does_not_rewrite_the_earlier_history_record(self):
        first = self._store_failure()
        hist_before = copy.deepcopy(first['_meta']['history']['NF'])
        self.assertTrue(hist_before)

        c = self.ctx(NF_OWN_EXPIRY_SESSION, NF_OWN_EXPIRY_SESSION,
                     scale_chain=F.chain(24180, 50, lot=65), scale_expiry=NF_NEXT_EXPIRY)
        later = self.run_authority(self.polls, c)
        hist_after = later['_meta']['history']['NF']

        # the already-appended records are byte-identical; recovery only ever appends
        self.assertEqual(hist_after[:len(hist_before)], hist_before)
        self.assertGreater(len(hist_after), len(hist_before))

    def test_a_good_scale_is_never_recomputed_once_frozen(self):
        kw = dict(scale_chain=F.chain(24000, 50, lot=65), scale_expiry=NF_NEXT_EXPIRY)
        c_open = self.ctx(NF_OWN_EXPIRY_SESSION, NF_OWN_EXPIRY_SESSION, **kw)
        first = self.run_authority(self.polls[:2], c_open)
        m_open = first['NF']['direction']['inputs']['scale']['value']
        self.assertIsNotNone(m_open)
        # a later poll offering a very different chain must not move the frozen scale
        c_late = self.ctx(NF_OWN_EXPIRY_SESSION, NF_OWN_EXPIRY_SESSION,
                          scale_chain=F.chain(24180, 50, lot=65, tv_frac=0.05),
                          scale_expiry=NF_NEXT_EXPIRY)
        later = self.run_authority(self.polls, c_late)
        self.assertEqual(later['NF']['direction']['inputs']['scale']['value'], m_open)
        self.assertFalse(later['NF']['direction']['inputs']['scale'].get('recovered', False))

    def test_failed_recovery_attempt_leaves_the_failure_retryable(self):
        """An attempt that cannot succeed must not poison the stored state."""
        self._store_failure()
        bad = self.ctx(NF_OWN_EXPIRY_SESSION, NF_OWN_EXPIRY_SESSION,
                       scale_chain={'strikes': {}}, scale_expiry=NF_NEXT_EXPIRY)
        mid = self.run_authority(self.polls, bad)
        self.assertEqual(mid['NF']['direction']['value'], 'UNAVAILABLE')
        good = self.ctx(NF_OWN_EXPIRY_SESSION, NF_OWN_EXPIRY_SESSION,
                        scale_chain=F.chain(24180, 50, lot=65), scale_expiry=NF_NEXT_EXPIRY)
        out = self.run_authority(self.polls, good)
        self.assertNotEqual(out['NF']['direction']['value'], 'UNAVAILABLE',
                            out['NF']['reason_codes'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
