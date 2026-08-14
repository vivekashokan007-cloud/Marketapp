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

    def test_capture_frame_prefers_ranked_full_candidate_evidence(self):
        snapshot = {
            "session_date": "2026-08-13",
            "poll_ts": "2026-08-13T09:15:00+05:30",
            "context_json": {
                "snapshot_generated_candidates": [{"id": "ui-only", "ivRichness": 1.0}],
                "snapshot_ranked_candidates_full": [
                    {"id": "ranked-one", "ivRichness": 2.0},
                    {"id": "ranked-two", "ivRichness": 4.0},
                ],
            },
        }

        frame = capture_frame(snapshot)

        self.assertEqual(frame["candidate_population_source"], "snapshot_ranked_candidates_full")
        self.assertEqual(frame["generated_population_count"], 2)
        self.assertEqual(frame["values"]["iv_richness_menu_median"], 3.0)

    def test_supply_shadow_emits_exact_slice_history_rows(self):
        snapshot = {
            "id": 42,
            "session_date": "2026-08-14",
            "poll_ts": "2026-08-14T09:20:00+05:30",
            "context_json": {
                "snapshot_pc2_supply_quality_shadow": {
                    "version": "pc2_supply_quality_shadow_v1",
                    "slices": [
                        {
                            "slice_key": "NF|BEAR|intraday",
                            "index_key": "NF",
                            "direction": "BEAR",
                            "trade_mode": "intraday",
                            "population_scope": "uncapped_generated_plus_rejected_live_memory",
                            "population_count": 100,
                            "generated_count": 90,
                            "rejected_count": 10,
                            "metrics": {
                                "credit_width_ratio": {"count": 100, "min": 0.001, "q10": 0.01, "median": 0.05, "max": 0.30},
                                "sigma_otm": {"count": 100, "median": 3.5},
                            },
                        }
                    ],
                }
            },
        }
        frame = capture_frame(snapshot)
        self.assertEqual(len(frame["candidate_slices"]), 1)
        history_key = "credit_width_ratio_menu_median|NF|BEAR|intraday"
        rows = finalize_frames(
            [frame],
            {history_key: [0.10, 0.20]},
            {},
            {"existing": ["credit_width_ratio_menu_median", "sigma_otm_menu_median"]},
        )
        sliced = [row for row in rows if row["index_key"] == "NF" and row["variable_name"] == "credit_width_ratio_menu_median"]
        self.assertEqual(len(sliced), 1)
        self.assertEqual(sliced[0]["lane"], "BEAR")
        self.assertEqual(sliced[0]["trade_mode"], "intraday")
        self.assertEqual(sliced[0]["support_count"], 2)
        self.assertEqual(sliced[0]["value"], 0.05)
        self.assertEqual(sliced[0]["extra_json"]["population_count"], 100)


if __name__ == "__main__":
    unittest.main()
