"""D9 repair, review round 2: acquisition bound and stale-chain clearing.

Codex review of fb3b13f raised two Kotlin defects:
  A. a failed fetch could leave an earlier session's scale chain in the persisted brain context, so a
     later expiry-day poll could pair a NEW expiry with an OLD chain;
  B. the extra chain was fetched on every expiry-day poll (~78 requests) even after a usable scale had
     been frozen and the brain had stopped reading it.

Kotlin cannot be compiled in this environment, so these tests assert the Kotlin SOURCE contracts and
the cross-language invariant that the two recoverable-reason lists stay equal. The behavioural Kotlin
tests live in app/src/test/java/com/marketradar/app/DirectionScaleNeedTest.kt and run in CI.
"""
import os
import re
import unittest

import index_authority_v1 as ia

JAVA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    '..', '..', 'java', 'com', 'marketradar', 'app')


def src(name):
    with open(os.path.join(JAVA, name)) as f:
        return f.read()


class TestRecoverableReasonParity(unittest.TestCase):
    """The Kotlin acquisition gate decides whether to retry using the same reason list the Python
    recovery path accepts.  If they drift, Kotlin either stops fetching a chain Python would still
    recover from, or keeps fetching one it never will."""

    def test_kotlin_and_python_recoverable_reasons_are_identical(self):
        k = src('DirectionScaleNeed.kt')
        block = k[k.index('val RECOVERABLE_REASONS'):]
        block = block[:block.index(')')]
        kotlin_reasons = set(re.findall(r'"([A-Z_]+)"', block))
        self.assertEqual(kotlin_reasons, set(ia.SCALE_RECOVERABLE_REASONS))

    def test_python_list_is_non_empty_and_excludes_ok(self):
        self.assertTrue(ia.SCALE_RECOVERABLE_REASONS)
        self.assertNotIn('OK', ia.SCALE_RECOVERABLE_REASONS)

    def test_unrecoverable_reasons_are_absent_from_both(self):
        k = src('DirectionScaleNeed.kt')
        for reason in ('OPEN_REF_MISSING', 'SPOT_INVALID', 'SCALE_INVALID', 'NO_VALID_PARITY_PAIR',
                       'EXPIRY_INVALID'):
            self.assertNotIn(reason, ia.SCALE_RECOVERABLE_REASONS, reason)
            self.assertNotIn('"%s"' % reason, k, reason)


class TestStaleChainCleared(unittest.TestCase):
    """Finding A: ctxObj is loaded from SharedPreferences and saved back, so scale keys persist across
    polls and across sessions."""

    def test_ctx_is_cleared_before_this_polls_values_are_inserted(self):
        s = src('MarketWatchService.kt')
        clear = s.index('DirectionScaleNeed.clearCtx(ctxObj)')
        loop = s.index('for ((scaleIx, fetched) in scaleChains)')
        self.assertLess(clear, loop, 'the clear must precede the insert loop')

    def test_clear_covers_both_keys_for_both_indices(self):
        k = src('DirectionScaleNeed.kt')
        block = k[k.index('val CTX_KEYS'):]
        block = block[:block.index(')')]
        self.assertEqual(set(re.findall(r'"(\w+)"', block)),
                         {'nfScaleChain', 'nfScaleExpiry', 'bnfScaleChain', 'bnfScaleExpiry'})

    def test_clear_runs_unconditionally_not_inside_the_loop(self):
        """If the clear were inside the per-index loop it would not run when nothing was fetched, and a
        previous session's keys would survive a normal session untouched."""
        s = src('MarketWatchService.kt')
        line = [ln for ln in s.splitlines() if 'DirectionScaleNeed.clearCtx(ctxObj)' in ln]
        self.assertEqual(len(line), 1)
        # same indentation as the surrounding statements, i.e. not nested in the for-loop body
        self.assertEqual(len(line[0]) - len(line[0].lstrip()), 12)

    def test_a_failed_fetch_publishes_no_chain(self):
        """The failure path writes the expiry only; with the clear above, no chain can accompany it."""
        s = src('MarketWatchService.kt')
        self.assertIn('ScaleChainFetch(next, null)', s)
        blk = s[s.index('if (raw == null) {'):]
        blk = blk[:blk.index('continue')]
        self.assertIn('ScaleExpiry', blk)
        self.assertNotIn('ScaleChain"', blk)

    def test_python_fails_closed_on_expiry_without_chain(self):
        """The contract the clearing relies on: an expiry with no chain must not fall back to the
        trading chain."""
        sel = ia.select_scale_chain('NF', '2026-11-09', {'strikes': {'24000': {}}}, '2026-11-09',
                                    None, '2026-11-17')
        self.assertEqual(sel['source'], ia.SCALE_SOURCE_UNAVAILABLE)
        self.assertEqual(sel['reason'], 'NEXT_EXPIRY_CHAIN_EMPTY')
        self.assertIsNone(sel['chain'])


class TestAcquisitionBound(unittest.TestCase):
    """Finding B: the fetch must stop once a usable scale is frozen."""

    def test_fetch_is_gated_before_any_network_call(self):
        s = src('MarketWatchService.kt')
        body = s[s.index('private fun fetchDirectionScaleChain('):]
        body = body[:body.index('\n    private fun ', 10) if '\n    private fun ' in body[10:]
                    else body.index('\n    private val ')]
        gate = body.index('DirectionScaleNeed.decide(')
        # both the contract-list lookup and the chain fetch must come after the gate
        self.assertLess(gate, body.index('resolveNextExpiryAfter('))
        self.assertLess(gate, body.index('/v2/option/chain'))
        self.assertIn('if (!need.fetch)', body)
        self.assertIn('return null', body[gate:])

    def test_gate_reads_the_authority_state_not_a_separate_mirror(self):
        """A second copy of 'is the scale frozen' in SharedPreferences could drift from the authority's
        own state; reading the authority file cannot."""
        s = src('MarketWatchService.kt')
        self.assertIn('DirectionScaleNeed.readState(filesDir, today)', s)
        k = src('DirectionScaleNeed.kt')
        self.assertIn('IndexAuthorityContext.stateDir(filesDir)', k)
        self.assertIn('"$session.json"', k)
        gate_obj = k[k.index('fun decide('):k.index('fun readState(')]
        self.assertNotIn('prefs', gate_obj)
        self.assertNotIn('SharedPreferences', gate_obj)

    def test_frozen_ok_scale_stops_the_fetch(self):
        k = src('DirectionScaleNeed.kt')
        body = k[k.index('fun decide('):k.index('fun readState(')]
        self.assertIn('if (status == "OK") return Decision(false, SCALE_FROZEN_OK)', body)

    def test_unreadable_state_still_fetches(self):
        """Failing to read state must not silently suppress the acquisition that closes the outage."""
        k = src('DirectionScaleNeed.kt')
        body = k[k.index('fun decide('):k.index('fun readState(')]
        self.assertIn('Decision(true, STATE_UNREADABLE)', body)
        self.assertIn('Decision(true, NO_SCALE_YET)', body)

    def test_state_filename_matches_the_python_store(self):
        """The gate reads <session>.json; index_authority_store._path must agree."""
        import index_authority_store as ias
        self.assertEqual(os.path.basename(ias._path('/d', '2026-11-09')), '2026-11-09.json')

    def test_state_dir_name_matches_python_expectation(self):
        k = src('IndexAuthorityContext.kt')
        self.assertIn('STATE_DIR_NAME = "index_authority_v1"', k)


if __name__ == '__main__':
    unittest.main(verbosity=2)
