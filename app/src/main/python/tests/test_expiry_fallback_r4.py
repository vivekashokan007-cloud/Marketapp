"""expiry_fallback_v1 R4 (Codex R3 reply 21 Nov 2026): the Python side of the final-publication-gate decisions.

SYNTHETIC fixtures, deterministic clocks; no network, no phone observation.  The JVM test
ExpiryFallbackFinalGateTest drives the production AnalysisAuthority.publish and records exactly which
acknowledgements native queues (none before the gate; PUBLISHED only after a successful commit, with the time publish
returned; NOT_PUBLISHED + the authority's reason or the finalizer's reason; nothing at all when the gate threw or the
process died).  These tests replay those sequences through the production brain apply + reconcile entry points and
check the retained publication state."""
import copy
import json
import unittest

import brain
import expiry_fallback_v1 as efb
import test_expiry_fallback_r3 as T3

GATE_REFUSALS = ('SUPERSEDED_INPUT', 'STALE_MARKET', 'MARKET_IDENTITY_UNAVAILABLE', 'PERSIST_FAILED')
FINALIZER_REASONS = ('ABORTED_BEFORE_FINAL_PUBLISH:IllegalStateException', 'ANALYSIS_ENDED_BEFORE_FINAL_PUBLISH')


def ack(d, aid, gen, state, now_ms=None, reason=None):
    return json.loads(brain.single_leg_expiry_fallback_reconcile(d, aid, gen, state, now_ms, reason))


class FinalGatePython(unittest.TestCase):
    SESSION, NEXT = T3.S3Publication.SESSION, T3.S3Publication.NEXT
    setUpClass = classmethod(T3.S3Publication.setUpClass.__func__)
    tearDownClass = classmethod(T3.S3Publication.tearDownClass.__func__)
    evidence = T3.S3Publication.evidence
    newdir = T3.S3Publication.newdir
    setUp = T3.S3Publication.setUp
    apply = T3.S3Publication.apply
    outcomes = T3.S3Publication.outcomes

    def applied(self, aid, gen, d=None):
        """One poll's apply step as native runs it: retained, returned, UNCONFIRMED (APPLY_ACCEPTED natively)."""
        ev, _ = self.evidence(self.at, self.spot)
        d = d or self.newdir()
        out = self.apply(copy.deepcopy(self.r), ev, d, attempt={'attempt_id': aid, 'generation': gen})
        self.assertNotIn('error', out)
        self.assertEqual(out['attempt']['publication_state'], efb.PUB_UNCONFIRMED)
        return d, out

    def pub_of(self, d, aid):
        doc, oc = self.outcomes(d)
        rows = [o['publication'] for o in oc['NF'] if o['publication'].get('attempt_id') == aid]
        self.assertEqual(len(rows), 1)
        return doc, rows[0]

    def test_gate_refusals_and_finalizer_are_not_published_with_their_reason(self):
        for i, why in enumerate(GATE_REFUSALS + FINALIZER_REASONS):
            with self.subTest(reason=why):
                aid = 'efb-%d-g' % (100 + i)
                d, _ = self.applied(aid, 100 + i)
                res = ack(d, aid, 100 + i, 'NOT_PUBLISHED', 1_700_000_000_123 + i, why)
                self.assertEqual((res['accepted'], res['state']), (True, 'NOT_PUBLISHED'))
                doc, p = self.pub_of(d, aid)
                self.assertEqual((p['state'], p['reason'], p['decided_ms']), ('NOT_PUBLISHED', why, 1_700_000_000_123 + i))
                cnt = efb.publication_counts(doc['indices']['NF'])
                self.assertNotIn('PUBLISHED', cnt)
                self.assertEqual(cnt['NOT_PUBLISHED'], {'outcomes': 1, 'computed_eligible': 1})
                late = ack(d, aid, 100 + i, 'PUBLISHED', 1_700_000_009_000)       # terminal: never promoted
                self.assertEqual((late['accepted'], late['state'], late['reason']), (False, 'NOT_PUBLISHED', 'ALREADY_NOT_PUBLISHED'))
                self.assertEqual(self.pub_of(d, aid)[1]['state'], 'NOT_PUBLISHED')

    def test_not_published_requires_a_reason_and_only_native_sends_it(self):
        d, _ = self.applied('efb-120-g', 120)
        self.assertEqual(ack(d, 'efb-120-g', 120, 'NOT_PUBLISHED', 1, None)['reason'], 'INVALID_TRANSITION_REQUEST')
        self.assertEqual(efb.reconcile(d, 'efb-120-g', 120, 'NOT_PUBLISHED', 1, source='python', reason='X')['reason'],
                         'INVALID_TRANSITION_REQUEST')
        self.assertEqual(self.pub_of(d, 'efb-120-g')[1]['state'], 'UNCONFIRMED')

    def test_success_is_published_with_the_actual_decision_time_and_is_immutable(self):
        d, _ = self.applied('efb-130-g', 130)
        res = ack(d, 'efb-130-g', 130, 'PUBLISHED', 1_700_000_012_345)
        self.assertEqual((res['accepted'], res['state']), (True, 'PUBLISHED'))
        doc, p = self.pub_of(d, 'efb-130-g')
        self.assertEqual((p['state'], p['decided_ms']), ('PUBLISHED', 1_700_000_012_345))
        self.assertNotIn('reason', p)
        self.assertEqual(doc['publication']['attempts']['efb-130-g']['decided_by'], 'native')
        for st, why in (('NOT_PUBLISHED', 'PERSIST_FAILED'), ('CANCELLED', None), ('TIMED_OUT', None)):
            r = ack(d, 'efb-130-g', 130, st, 1_700_000_020_000, why)
            self.assertEqual((r['accepted'], r['state']), (False, 'PUBLISHED'))
        self.assertEqual(self.pub_of(d, 'efb-130-g')[1]['state'], 'PUBLISHED')
        self.assertEqual(self.outcomes(d)[0]['publication']['late_acks_refused'], 3)

    def test_gate_exception_process_death_or_lost_ack_stay_unconfirmed_through_restart(self):
        """No acknowledgement arrives (gate threw -> GATE_UNRESOLVED natively; or the process died before the
        commit or before the queued acknowledgement ran).  After restart a newer attempt is applied and published:
        the old outcome stays UNCONFIRMED -- never inferred PUBLISHED from the completed apply, the newer attempt or
        the computed plan."""
        d, _ = self.applied('efb-140-g', 140)
        self.assertEqual(self.pub_of(d, 'efb-140-g')[1]['state'], 'UNCONFIRMED')
        self.applied('efb-141-g', 141, d)                          # restart; next poll
        self.assertEqual(ack(d, 'efb-141-g', 141, 'PUBLISHED', 1_700_000_030_000)['state'], 'PUBLISHED')
        doc, oc = self.outcomes(d)
        self.assertEqual([(o['publication']['attempt_id'], o['publication']['state']) for o in oc['NF']],
                         [('efb-140-g', 'UNCONFIRMED'), ('efb-141-g', 'PUBLISHED')])
        self.assertEqual(efb.publication_counts(doc['indices']['NF'])['UNCONFIRMED'], {'outcomes': 1, 'computed_eligible': 1})

    def test_late_ack_of_an_older_timed_out_attempt_cannot_alter_the_newer_result(self):
        d, _ = self.applied('efb-150-g', 150)
        self.assertEqual(ack(d, 'efb-150-g', 150, 'TIMED_OUT', 1)['state'], 'TIMED_OUT')
        self.applied('efb-151-g', 151, d)
        self.assertEqual(ack(d, 'efb-151-g', 151, 'PUBLISHED', 2)['state'], 'PUBLISHED')
        for st, why in (('PUBLISHED', None), ('NOT_PUBLISHED', 'SUPERSEDED_INPUT')):
            r = ack(d, 'efb-150-g', 150, st, 3, why)
            self.assertEqual((r['accepted'], r['state']), (False, 'TIMED_OUT'))
        _, oc = self.outcomes(d)
        self.assertEqual([(o['publication']['attempt_id'], o['publication']['state']) for o in oc['NF']],
                         [('efb-150-g', 'TIMED_OUT'), ('efb-151-g', 'PUBLISHED')])

    def test_successful_poll_after_a_failure_reconciles_its_own_attempt_once(self):
        d, _ = self.applied('efb-160-g', 160)
        self.assertTrue(ack(d, 'efb-160-g', 160, 'NOT_PUBLISHED', 10_000, 'SUPERSEDED_INPUT')['accepted'])
        self.applied('efb-161-g', 161, d)
        self.assertTrue(ack(d, 'efb-161-g', 161, 'PUBLISHED', 20_000)['accepted'])
        again = ack(d, 'efb-161-g', 161, 'PUBLISHED', 20_001)        # idempotent repeat, not a late ack
        self.assertEqual((again['accepted'], again['state']), (False, 'PUBLISHED'))
        doc, oc = self.outcomes(d)
        self.assertEqual([(o['publication']['attempt_id'], o['publication']['state'], o['publication'].get('reason'),
                           o['publication'].get('decided_ms')) for o in oc['NF']],
                         [('efb-160-g', 'NOT_PUBLISHED', 'SUPERSEDED_INPUT', 10_000), ('efb-161-g', 'PUBLISHED', None, 20_000)])
        self.assertEqual(doc['publication']['late_acks_refused'], 0)
        self.assertEqual(efb.publication_counts(doc['indices']['NF'])['PUBLISHED'], {'outcomes': 1, 'computed_eligible': 1})

    def test_codex_probe_terminal_state_is_why_the_ack_must_move_not_the_fence(self):
        """Codex R3 reply probe: an early PUBLISHED cannot be corrected later (ALREADY_PUBLISHED).  The terminal-state
        rule is kept; R4 moves native's PUBLISHED acknowledgement to after the final gate instead."""
        d, _ = self.applied('synthetic-final-gate', 1)
        self.assertEqual(ack(d, 'synthetic-final-gate', 1, 'PUBLISHED', 1)['state'], 'PUBLISHED')
        r = ack(d, 'synthetic-final-gate', 1, 'CANCELLED', 2)
        self.assertEqual((r['accepted'], r['state'], r['reason']), (False, 'PUBLISHED', 'ALREADY_PUBLISHED'))


if __name__ == '__main__':
    unittest.main()
