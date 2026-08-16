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
                "snapshot_build3_flow": {"truncated_at_persistence": 0},
                "snapshot_generated_candidates": [{"ivRichness": 1.0, "isCredit": True, "creditWidthRatio": 0.2}],
                "snapshot_rejected_candidates": [{"ivRichness": 3.0, "sigmaOTM": 2.0}],
                "snapshot_rejected_candidates_full": [{"ivRichness": 3.0, "sigmaOTM": 2.0}],
            },
            "verdict_json": {"confidence": 70},
        }
        frame = capture_frame(snapshot)
        self.assertEqual(frame["frame_version"], FRAME_VERSION)
        self.assertTrue(frame["rejected_capture_present"])
        self.assertTrue(frame["candidate_population_verified"])
        self.assertEqual(frame["generated_population_count"], 1)
        self.assertEqual(frame["rejected_population_count"], 1)
        self.assertEqual(frame["values"]["iv_richness_menu_median"], 2.0)

    def test_capture_frame_prefers_ranked_full_candidate_evidence(self):
        snapshot = {
            "session_date": "2026-08-13",
            "poll_ts": "2026-08-13T09:15:00+05:30",
            "context_json": {
                "snapshot_build3_flow": {"truncated_at_ranked_evidence": 0},
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

    def test_capture_frame_uses_full_rejected_population_not_compact_sample(self):
        full_rejected = [{"ivRichness": 100.0, "sigmaOTM": 2.0} for _ in range(80)]
        frame = capture_frame({
            "session_date": "2026-08-13",
            "poll_ts": "2026-08-13T09:15:00+05:30",
            "context_json": {
                "snapshot_build3_flow": {"truncated_at_ranked_evidence": 0},
                "snapshot_ranked_candidates_full": [{"id": "generated", "ivRichness": 1.0}],
                "snapshot_rejected_candidates": [{"ivRichness": 1.0} for _ in range(20)],
                "snapshot_rejected_candidates_full": full_rejected,
            },
        })

        self.assertTrue(frame["candidate_population_verified"])
        self.assertTrue(frame["rejected_capture_complete"])
        self.assertEqual(frame["rejected_population_count"], 80)
        self.assertEqual(frame["values"]["iv_richness_menu_median"], 100.0)

    def test_capture_frame_uses_full_candidate_union_for_width_metrics(self):
        frame = capture_frame({
            "session_date": "2026-08-13",
            "poll_ts": "2026-08-13T09:15:00+05:30",
            "context_json": {
                "snapshot_build3_flow": {"truncated_at_persistence": 0},
                "snapshot_generated_candidates": [{"id": "generated", "width": 100.0}],
                "snapshot_rejected_candidates_full": [{"id": "rejected", "width": 500.0}],
            },
        })

        self.assertTrue(frame["candidate_population_verified"])
        self.assertEqual(frame["values"]["width_menu_median"], 300.0)
        self.assertEqual(frame["values"]["width_menu_best"], 500.0)

    def test_capture_frame_does_not_verify_compact_rejected_sample(self):
        frame = capture_frame({
            "session_date": "2026-08-13",
            "poll_ts": "2026-08-13T09:15:00+05:30",
            "context_json": {
                "snapshot_build3_flow": {"truncated_at_persistence": 0},
                "snapshot_generated_candidates": [{"id": "generated", "ivRichness": 1.0}],
                "snapshot_rejected_candidates": [{"ivRichness": 3.0}],
            },
        })

        self.assertFalse(frame["rejected_capture_complete"])
        self.assertFalse(frame["candidate_population_verified"])

    def test_capture_frame_fails_provenance_closed_when_ranked_evidence_is_truncated(self):
        frame = capture_frame({
            "session_date": "2026-08-13",
            "poll_ts": "2026-08-13T09:15:00+05:30",
            "context_json": {
                "snapshot_build3_flow": {"truncated_at_ranked_evidence": 4},
                "snapshot_ranked_candidates_full": [{"id": "ranked-one", "ivRichness": 2.0}],
                "snapshot_rejected_candidates_full": [],
            },
        })

        self.assertFalse(frame["generated_capture_complete"])
        self.assertFalse(frame["candidate_population_verified"])

    def test_range_ratio_uses_the_existing_sigma_unit_without_dividing_twice(self):
        frame = capture_frame({
            "session_date": "2026-08-13",
            "poll_ts": "2026-08-13T09:15:00+05:30",
            "context_json": {
                "rangeSigma": 0.75,
                "bnfSpot": 57000,
                "vix": 14,
                "snapshot_build3_flow": {"truncated_at_persistence": 0},
                "snapshot_generated_candidates": [],
                "snapshot_rejected_candidates": [],
            },
        })

        self.assertEqual(frame["values"]["realized_vs_implied_range_ratio"], 0.75)

    def test_finalizer_emits_prior_day_seeded_daily_calibration_row(self):
        catalog = {"candidate_quality": ["iv_richness_menu_median"]}
        frames = [
            {
                "frame_version": FRAME_VERSION,
                "session_date": "2026-08-13",
                "poll_ts": "2026-08-13T09:15:00+05:30",
                "values": {"iv_richness_menu_median": 1.0},
                "candidate_population_verified": True,
            },
            {
                "frame_version": FRAME_VERSION,
                "session_date": "2026-08-13",
                "poll_ts": "2026-08-13T09:20:00+05:30",
                "values": {"iv_richness_menu_median": 1.4},
                "candidate_population_verified": True,
            },
        ]

        rows = finalize_frames(
            frames,
            {
                "iv_richness_menu_median": [0.8],
                "daily::iv_richness_menu_median": [0.9, 1.1],
            },
            {},
            catalog,
        )
        daily = [row for row in rows if row["poll_ts"] is None]

        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0]["value"], 1.2)
        self.assertEqual(daily[0]["support_count_30"], 2)
        self.assertEqual(daily[0]["pct_30"], 100.0)
        self.assertEqual(daily[0]["history_source"], "live")
        self.assertEqual(daily[0]["source_quality"], "DAILY_CALIBRATION_UNION_VERIFIED")
        self.assertEqual(daily[0]["extra_json"]["contributing_poll_count"], 2)

    def test_daily_calibration_fails_provenance_closed(self):
        rows = finalize_frames(
            [{
                "frame_version": FRAME_VERSION,
                "session_date": "2026-08-13",
                "poll_ts": "2026-08-13T09:15:00+05:30",
                "values": {"sigma_otm_menu_median": 2.0},
                "candidate_population_verified": False,
            }],
            {},
            {},
            {"candidate_quality": ["sigma_otm_menu_median"]},
        )
        daily = [row for row in rows if row["poll_ts"] is None]

        self.assertEqual(daily[0]["source_quality"], "DAILY_CALIBRATION_PROVENANCE_UNVERIFIED")
        self.assertEqual(daily[0]["extra_json"]["calibration_population_version"], "unverified")

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
