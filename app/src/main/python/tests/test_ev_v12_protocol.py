"""Implied-EV v1.2 protocol (C5/C6): canonical JSON, envelope caps, ACK matching, durable slot crash/upgrade/retry."""
import json
import os
import shutil
import tempfile
import unittest

import ev_v12_testutil  # noqa: F401
import ev_v12_protocol as P

V1 = {"engine": "e1", "model": "m1"}
V2 = {"engine": "e2", "model": "m1"}


class Canon(unittest.TestCase):
    def test_sorted_compact_and_float_refused(self):
        self.assertEqual(P.canon({"b": "1", "a": ["x", {"d": None, "c": True}]}), '{"a":["x",{"c":true,"d":null}],"b":"1"}')
        for bad in ({"x": 1.5}, {"x": float("nan")}, [0.1]):
            with self.assertRaises(P.CanonError):
                P.canon(bad)
        self.assertEqual(P.fx(-0.0001, 2), "0.00")
        self.assertEqual(P.fx(1.005, 3), "1.005")
        self.assertIsNone(P.fx(None, 2))

    def test_envelope_digests_and_cap(self):
        b = {"session_date": "2026-10-08", "pick": "IB400", "ev": "963.97"}
        e1 = P.build_envelope(b, {"ms": "12"}, "ev12:NF:2026-10-08")
        e2 = P.build_envelope(b, {"ms": "99"}, "ev12:NF:2026-10-08")
        self.assertEqual(e1["decision_id"], e2["decision_id"])        # business digest independent of metrics
        self.assertNotEqual(e1["envelope_sha256"], e2["envelope_sha256"])
        self.assertTrue(e1["decision_id"].startswith("ev12:NF:2026-10-08:"))
        self.assertEqual(len(e1["decision_id"].split(":")[-1]), 16)
        self.assertEqual(json.loads(e1["envelope_text"])["business_sha256"], e1["business_sha256"])
        with self.assertRaises(P.CanonError):
            P.build_envelope({"blob": "x" * (P.MAX_ENVELOPE_BYTES + 1)}, {}, "p")

    def test_ack_matching(self):
        loc = {"decision_id": "d", "envelope_sha256": "h"}
        self.assertTrue(P.ack_matches(loc, {"status": "ACCEPTED", "decision_id": "d", "envelope_sha256": "h"}))
        self.assertTrue(P.ack_matches(loc, {"status": "DUPLICATE", "decision_id": "d", "envelope_sha256": "h"}))
        for bad in ({"status": "ACCEPTED", "decision_id": "d", "envelope_sha256": "x"},
                    {"status": "CONFLICT", "decision_id": "d", "envelope_sha256": "h"},
                    {"status": "ACCEPTED", "decision_id": "e", "envelope_sha256": "h"}, None, "ok"):
            self.assertFalse(P.ack_matches(loc, bad))


class Slot(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_first_attempt_committed_before_compute(self):
        a, s = P.begin_or_resume(self.d, "NF", "2026-10-08", True, V1, "t0")
        self.assertEqual(a, "COMPUTE")
        self.assertEqual(P.load_slot(self.d, "NF", "2026-10-08")["state"], "ATTEMPT_1")

    def test_crash_then_one_retry_then_expire(self):
        P.begin_or_resume(self.d, "NF", "2026-10-08", True, V1, "t0")
        a, s = P.begin_or_resume(self.d, "NF", "2026-10-08", True, V1, "t1")   # process died in attempt 1
        self.assertEqual((a, s["state"]), ("COMPUTE", "ATTEMPT_2"))
        self.assertEqual(s["history"][0]["outcome"], "INTERRUPTED")
        a, s = P.begin_or_resume(self.d, "NF", "2026-10-08", True, V1, "t2")   # died again
        self.assertEqual(a, "EXPIRE")
        s = P.finish(self.d, s, "FAILED", failure="ATTEMPTS_EXHAUSTED", now_iso="t2")
        self.assertEqual(P.begin_or_resume(self.d, "NF", "2026-10-08", True, V1, "t3")[0], "SLOT_CONSUMED")

    def test_terminal_survives_version_upgrade(self):
        _, s = P.begin_or_resume(self.d, "NF", "2026-10-08", True, V1, "t0")
        env = P.build_envelope({"x": "1"}, {}, "ev12:NF:2026-10-08")
        P.finish(self.d, s, "DECIDED", envelope=env, now_iso="t1")
        a, s2 = P.begin_or_resume(self.d, "NF", "2026-10-08", True, V2, "t2")
        self.assertEqual(a, "SLOT_CONSUMED")
        self.assertEqual(s2["envelope"]["envelope_sha256"], env["envelope_sha256"])

    def test_retryable_failure_once(self):
        _, s = P.begin_or_resume(self.d, "NF", "2026-10-08", True, V1, "t0")
        s = P.finish(self.d, s, "FAILED", failure="QUOTE_STALE", now_iso="t1", retryable=True)
        self.assertEqual(s["state"], "RETRYABLE_FAILED")
        a, s = P.begin_or_resume(self.d, "NF", "2026-10-08", True, V1, "t2")
        self.assertEqual(a, "COMPUTE")
        s = P.finish(self.d, s, "FAILED", failure="QUOTE_STALE", now_iso="t3", retryable=True)
        self.assertEqual(s["state"], "FAILED")

    def test_outside_window_and_corrupt(self):
        a, s = P.begin_or_resume(self.d, "NF", "2026-10-08", False, V1, "t0")
        self.assertEqual(a, "EXPIRE")
        self.assertIsNone(P.load_slot(self.d, "NF", "2026-10-08"))
        with open(P.slot_path(self.d, "NF", "2026-10-09"), "w") as f:
            f.write("{trunc")
        self.assertEqual(P.begin_or_resume(self.d, "NF", "2026-10-09", True, V1, "t0")[0], "SLOT_CONSUMED")

    def test_atomic_write_leaves_no_tmp_and_prune(self):
        for d in ("2026-10-01", "2026-10-07", "2026-10-08"):
            P.begin_or_resume(self.d, "NF", d, True, V1, "t")
        self.assertFalse([f for f in os.listdir(self.d) if f.endswith(".tmp")])
        P.prune_slots(self.d, {"2026-10-08"})
        self.assertEqual(sorted(os.listdir(self.d)), ["slot_NF_2026-10-08.json"])


if __name__ == "__main__":
    unittest.main()
