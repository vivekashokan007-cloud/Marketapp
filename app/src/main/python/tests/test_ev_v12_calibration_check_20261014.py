"""2.6.74 (Codex revised A): the per-poll calibration_check view field. It belongs to the publication the poll
actually received, never decides anything, and is absent when the experiment does not apply (Off / Real)."""
import shutil
import tempfile
import unittest

import ev_v12_integration as I
from test_ev_v12_integration import POLL_MS, causal_pub, ctx_for


class CalibrationCheck(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_valid_publication_reports_ok_for_that_version_and_poll(self):
        out = I.run(ctx_for(self.d, mode="observe"), {}, "paper")
        cc = out["calibration_check"]
        self.assertEqual(cc, {"calibration_version": causal_pub()["calibration_version"], "status": "OK",
                              "poll_ms": POLL_MS})

    def test_missing_and_tampered_cache(self):
        out = I.run(ctx_for(self.d, mode="observe", evCalibration=None), {}, "paper")
        self.assertEqual((out["calibration_check"]["calibration_version"], out["calibration_check"]["status"]),
                         (None, "NO_CALIBRATION"))
        bad = dict(causal_pub(), n_completed=0)
        out = I.run(ctx_for(self.d, mode="observe", evCalibration=bad), {}, "paper")
        self.assertEqual(out["calibration_check"]["status"], "NO_CALIBRATION_DIGEST_MISMATCH")
        out = I.run(ctx_for(self.d, mode="observe", evCalibration="junk"), {}, "paper")
        self.assertTrue(out["calibration_check"]["status"].startswith("NO_CALIBRATION"))

    def test_absent_when_off_or_real(self):
        self.assertIsNone(I.run(ctx_for(self.d, mode="off"), {}, "paper"))
        self.assertIsNone(I.run(ctx_for(self.d, mode="observe"), {}, "live"))


if __name__ == "__main__":
    unittest.main()
