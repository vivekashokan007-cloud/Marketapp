import unittest

from c3_percentile_finalizer import FRAME_VERSION, capture_frame, finalize_frames


class C3PercentileFinalizerTest(unittest.TestCase):
    def test_finalizer_uses_prior_values_and_appends_after_each_poll(self):
        catalog = {"market": ["vix"]}
        frames = [
            {"frame_version": FRAME_VERSION, "session_date": "2026-08-13", "poll_ts": "2026-08-13T09:15:00+05:30", "values": {"vix": 10.0}},
            {"frame_version": FRAME_VERSION, "session_date": "2026-08-13", "poll_ts": "2026-08-13T09:20:00+05:30", "values": {"vix": 20.0}},
        ]
        rows = finalize_frames(frames, {"vix": [5.0]}, {}, catalog)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["support_count_30"], 1)
        self.assertEqual(rows[0]["pct_30"], 100.0)
        self.assertEqual(rows[1]["support_count_30"], 2)
        self.assertEqual(rows[1]["pct_30"], 100.0)
        self.assertEqual(rows[0]["history_source"], "live")
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])

    def test_capture_frame_preserves_candidate_union_provenance(self):
        snapshot = {
            "session_date": "2026-08-13",
            "poll_ts": "2026-08-13T09:15:00+05:30",
            "context_json": {
                "vix": 12.0,
                "snapshot_generated_candidates": [{"ivRichness": 1.0, "isCredit": True, "creditWidthRatio": 0.2}],
                "snapshot_rejected_candidates": [{"ivRichness": 3.0, "sigmaOTM": 2.0}],
            },
            "verdict_json": {"confidence": 70},
        }
        frame = capture_frame(snapshot)
        self.assertEqual(frame["frame_version"], FRAME_VERSION)
        self.assertTrue(frame["rejected_capture_present"])
        self.assertEqual(frame["generated_population_count"], 1)
        self.assertEqual(frame["rejected_population_count"], 1)
        self.assertEqual(frame["values"]["iv_richness_menu_median"], 2.0)


if __name__ == "__main__":
    unittest.main()
