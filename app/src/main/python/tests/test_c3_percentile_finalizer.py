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


class C3OomSafeFinalizerTest(unittest.TestCase):
    """Parity + memory-shape tests for the C3 OOM-safe finalization path."""

    @staticmethod
    def _catalog():
        return {
            "existing": ["vix", "iv_richness_menu_median"],
            "candidate_quality": ["sigma_otm_menu_median"],
        }

    def test_ndjson_path_matches_finalize_frames_ids_and_values(self):
        import os
        import tempfile
        from c3_percentile_finalizer import finalize_frames_to_ndjson

        catalog = {
            "candidate_quality": ["iv_richness_menu_median", "sigma_otm_menu_median"],
            "existing": ["vix"],
        }
        frames = [
            {
                "frame_version": FRAME_VERSION,
                "session_date": "2026-09-24",
                "poll_ts": "2026-09-24T09:15:00+05:30",
                "snapshot_id": "a",
                "values": {"vix": 12.0, "iv_richness_menu_median": 1.0, "sigma_otm_menu_median": 2.0},
                "candidate_population_verified": True,
                "generated_capture_complete": True,
                "candidate_slices": [
                    {
                        "slice_key": "BNF|BULL|credit",
                        "index_key": "BNF",
                        "direction": "BULL",
                        "trade_mode": "credit",
                        "population_scope": "uncapped_generated_plus_rejected_live_memory",
                        "population_count": 10,
                        "generated_count": 5,
                        "rejected_count": 5,
                        "values": {"credit_width_ratio_menu_median": 0.2},
                        "quantiles": {"credit_width_ratio_menu_median": {"median": 0.2}},
                    }
                ],
            },
            {
                "frame_version": FRAME_VERSION,
                "session_date": "2026-09-24",
                "poll_ts": "2026-09-24T09:20:00+05:30",
                "snapshot_id": "b",
                "values": {"vix": 13.0, "iv_richness_menu_median": 1.4, "sigma_otm_menu_median": 2.2},
                "candidate_population_verified": True,
                "generated_capture_complete": True,
                "candidate_slices": [],
            },
        ]
        seed = {
            "vix": [10.0, 11.0],
            "iv_richness_menu_median": [0.8],
            "daily::iv_richness_menu_median": [0.9, 1.1],
            "credit_width_ratio_menu_median|BNF|BULL|credit": [0.1],
        }
        prior = {"menu_mean_pnl_prior_sessions_only": 100.0}
        expected = finalize_frames(frames, seed, prior, catalog)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rows.ndjson")
            meta = finalize_frames_to_ndjson(frames, seed, prior, catalog, path)
            self.assertTrue(meta["ok"])
            self.assertEqual(meta["row_count"], len(expected))
            streamed = []
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        import json
                        streamed.append(json.loads(line))
        self.assertEqual(len(streamed), len(expected))
        for left, right in zip(streamed, expected):
            self.assertEqual(left["id"], right["id"])
            self.assertEqual(left["variable_name"], right["variable_name"])
            self.assertEqual(left["value"], right["value"])
            self.assertEqual(left["pct_30"], right["pct_30"])
            self.assertEqual(left["pct_60"], right["pct_60"])
            self.assertEqual(left["support_count_30"], right["support_count_30"])
            self.assertEqual(left["support_count_60"], right["support_count_60"])
            self.assertEqual(left["history_source"], right["history_source"])
            self.assertEqual(left["source_quality"], right["source_quality"])
            self.assertEqual(left.get("poll_ts"), right.get("poll_ts"))
            self.assertEqual(left.get("index_key"), right.get("index_key"))
            self.assertEqual(left.get("extra_json"), right.get("extra_json"))

    def test_incident_scale_fixture_parity_and_bounded_peak(self):
        import json
        import os
        import tempfile
        import tracemalloc
        from c3_percentile_finalizer import finalize_frames_to_ndjson

        fixture_path = os.path.join(
            os.path.dirname(__file__), "fixtures", "c3_incident_77_frames.json"
        )
        if not os.path.exists(fixture_path):
            self.skipTest("incident fixture missing")
        with open(fixture_path, encoding="utf-8") as handle:
            fixture = json.load(handle)
        frames = fixture["frames"]
        seed = fixture["history_seed"]
        prior = fixture["outcome_prior"]
        # Use a catalog subset matching fixture values keys that exist in frames.
        catalog = {
            "existing": [
                "vix",
                "fii_short_pct",
                "iv_richness_menu_median",
                "realized_day_range",
                "sigma_otm_menu_median",
                "credit_width_ratio_menu_median",
                "menu_win_rate_prior_sessions_only",
                "rejected_sigma_otm_median",
            ],
            "candidate_economics": [
                "premium_edge_menu_median",
                "premium_edge_menu_best",
                "ev_per_1k_menu_median",
                "ev_per_1k_menu_best",
                "prob_profit_menu_median",
                "prob_profit_menu_best",
                "net_premium_menu_median",
                "net_premium_menu_best",
                "max_profit_menu_median",
                "max_profit_menu_best",
                "max_loss_menu_median",
                "max_loss_menu_best",
                "risk_reward_menu_median",
                "risk_reward_menu_best",
                "width_menu_median",
                "width_menu_best",
                "debit_breakeven_sigma_menu_median",
                "debit_breakeven_sigma_menu_best",
                "theta_friction_minutes_menu_median",
                "theta_friction_minutes_menu_best",
                "net_theta_menu_median",
                "net_theta_menu_best",
            ],
            "market_state": [
                "atm_iv", "iv_percentile", "daily_sigma", "pcr", "near_atm_pcr",
                "max_pain_distance", "call_wall_distance", "put_wall_distance",
                "total_call_oi", "total_put_oi", "oi_skew",
                "realized_vs_implied_range_ratio", "overnight_gap", "spot_vs_vwap",
                "abs_spot_sigma", "abs_nf_spot_sigma", "abs_vix_sigma",
                "bnf_atm_iv", "nf_atm_iv", "bnf_pcr", "nf_pcr",
                "bnf_near_atm_pcr", "nf_near_atm_pcr",
                "bnf_max_pain_distance", "nf_max_pain_distance",
                "bnf_call_wall_distance", "nf_call_wall_distance",
                "bnf_put_wall_distance", "nf_put_wall_distance",
                "bnf_total_call_oi", "nf_total_call_oi",
                "bnf_total_put_oi", "nf_total_put_oi",
                "bnf_oi_skew", "nf_oi_skew",
            ],
            "supply_process": [
                "generated_count", "rejected_count", "watchlist_survivors",
                "distinct_families_generated", "menu_size",
            ],
            "decision_state": [
                "confidence", "signal_independence_score", "bull_score",
                "bear_score", "signal_accuracy",
            ],
            "outcome_state": [
                "menu_mean_pnl_prior_sessions_only",
                "realized_r_prior_sessions_only",
                "notification_count_session",
            ],
            "position_state": [
                "open_position_profit_capture_max",
                "open_position_profit_capture_median",
                "open_position_loss_capture_max",
                "open_position_loss_capture_median",
            ],
        }
        self.assertEqual(len(frames), 77)
        expected = finalize_frames(frames, seed, prior, catalog)
        tracemalloc.start()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rows.ndjson")
            meta = finalize_frames_to_ndjson(frames, seed, prior, catalog, path)
            current, peak = tracemalloc.get_traced_memory()
            streamed_ids = []
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    streamed_ids.append(json.loads(line)["id"])
        tracemalloc.stop()
        self.assertEqual(meta["row_count"], len(expected))
        self.assertEqual(streamed_ids, [row["id"] for row in expected])
        # NDJSON path must stay well under a 256MB device heap budget for the
        # pure-Python portion (frames already compact; no full context_json).
        self.assertLess(peak, 64 * 1024 * 1024, f"peak={peak}")
        # Record for STOP report
        print(
            f"C3_STRESS peak_bytes={peak} current_bytes={current} "
            f"rows={len(expected)} frames=77 ndjson_ok=1"
        )

    def test_on_row_sink_does_not_retain_full_list(self):
        frames = [
            {
                "frame_version": FRAME_VERSION,
                "session_date": "2026-09-24",
                "poll_ts": "2026-09-24T09:15:00+05:30",
                "values": {"vix": 12.0},
                "candidate_population_verified": True,
            }
        ]
        seen = []
        retained = finalize_frames(
            frames, {"vix": [10.0]}, {}, {"existing": ["vix"]}, on_row=seen.append
        )
        self.assertEqual(retained, [])
        self.assertGreaterEqual(len(seen), 1)
        self.assertEqual(seen[0]["variable_name"], "vix")

    def test_ci_small_fixture_finalizes(self):
        catalog = {"existing": ["vix"]}
        frames = [
            {
                "frame_version": FRAME_VERSION,
                "session_date": "2026-09-24",
                "poll_ts": f"2026-09-24T09:{i:02d}:00+05:30",
                "values": {"vix": 10.0 + i},
                "candidate_population_verified": True,
            }
            for i in range(8)
        ]
        rows = finalize_frames(frames, {"vix": [5.0]}, {}, catalog)
        self.assertEqual(len([r for r in rows if r["poll_ts"]]), 8)
