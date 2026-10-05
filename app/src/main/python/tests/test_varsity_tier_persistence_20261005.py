"""varsityTier must survive snapshot serialization (2026-10-05).

Production on 5 Oct: 0 of 29 persisted primary candidates carried
varsityTier, although the generator stamps it on every candidate and the
ranking sorts PRIMARY before ALLOWED. Two drop points existed in Python:
the primary_candidate_json whitelist in take_poll_snapshot and
_candidate_view (top / generated / ranked / below-cap evidence). Kotlin
compactCandidate already allowlists the key.

This is serialization only: no ranking, confidence, model or mode change.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brain  # noqa: E402


def _candidate(cid, tier):
    row = {
        "id": cid,
        "index": "NF",
        "type": "IRON_CONDOR",
        "expiry": "2026-10-06",
        "tDTE": 1,
        "netPremium": 100,
        "maxProfit": 1000,
        "maxLoss": 2000,
        "legs": [],
    }
    if tier is not None:
        row["varsityTier"] = tier
    return row


def _snapshot(mode, candidates):
    result = {
        "watchlist": [dict(c) for c in candidates],
        "generated_candidates": [dict(c) for c in candidates],
        "ranked_candidates_full": [dict(c) for c in candidates],
        "rejected_candidates": [],
        "verdict": {"action": "WAIT", "strategy": "NONE", "direction": "NEUTRAL", "confidence": 0},
        "marketPhase": {"id": "MIDDAY", "label": "Midday"},
        "bnfProfile": {},
        "nfProfile": {},
    }
    ctx = {"today_ist": "2026-10-05", "vix": 14.8, "nfDTE": 1, "bnfDTE": 22}
    snap = brain.take_poll_snapshot(result, ctx, [], mode)

    def load(key):
        value = snap[key]
        return json.loads(value) if isinstance(value, str) else value

    return load("primary_candidate_json"), load("top_candidates_json"), load("context_json")


class CandidateViewTests(unittest.TestCase):
    def test_view_keeps_the_stamped_tier(self):
        self.assertEqual(brain._candidate_view(_candidate("a", "PRIMARY"))["varsityTier"], "PRIMARY")
        self.assertEqual(brain._candidate_view(_candidate("b", "ALLOWED"))["varsityTier"], "ALLOWED")

    def test_view_adds_no_null_key_when_absent(self):
        self.assertNotIn("varsityTier", brain._candidate_view(_candidate("c", None)))

    def test_android_view_inherits_the_tier(self):
        view = brain._candidate_android_snapshot_view(_candidate("d", "PRIMARY"))
        self.assertEqual(view["varsityTier"], "PRIMARY")


class SnapshotPersistenceTests(unittest.TestCase):
    def test_primary_top_and_generated_keep_the_tier_in_both_modes(self):
        cands = [_candidate("p1", "PRIMARY"), _candidate("a2", "ALLOWED")]
        for mode in ("full", "android_compact_v1"):
            primary, top, context = _snapshot(mode, cands)
            self.assertEqual(primary.get("varsityTier"), "PRIMARY", mode)
            self.assertEqual([row.get("varsityTier") for row in top], ["PRIMARY", "ALLOWED"], mode)
            # The full-mode context keeps generated evidence; the Android
            # compact context may drop the list, but rows it keeps must
            # carry the tier.
            generated = context.get("snapshot_generated_candidates") or []
            if mode == "full":
                self.assertTrue(generated, mode)
            for row in generated:
                self.assertIn(row.get("varsityTier"), ("PRIMARY", "ALLOWED"), mode)

    def test_missing_tier_stays_missing_and_is_not_invented(self):
        primary, top, _ = _snapshot("android_compact_v1", [_candidate("x", None)])
        self.assertIsNone(primary.get("varsityTier"))
        self.assertNotIn("varsityTier", top[0])


class NoDecisionChangeTests(unittest.TestCase):
    def test_view_does_not_mutate_the_candidate(self):
        cand = _candidate("m", "ALLOWED")
        before = json.dumps(cand, sort_keys=True)
        brain._candidate_view(cand)
        self.assertEqual(json.dumps(cand, sort_keys=True), before)


if __name__ == "__main__":
    unittest.main()
