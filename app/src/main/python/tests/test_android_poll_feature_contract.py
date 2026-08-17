import os
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
SERVICE_PATH = os.path.join(
    ROOT,
    "app",
    "src",
    "main",
    "java",
    "com",
    "marketradar",
    "app",
    "MarketWatchService.kt",
)
SUPABASE_PATH = os.path.join(
    ROOT, "app", "src", "main", "java", "com", "marketradar", "app", "SupabaseClient.kt"
)
ML_SERVICE_PATH = os.path.join(
    ROOT, "app", "src", "main", "java", "com", "marketradar", "app", "MarketMLService.kt"
)
LOCAL_CACHE_PATH = os.path.join(
    ROOT, "app", "src", "main", "java", "com", "marketradar", "app", "EvaluationLocalCache.kt"
)
MAIN_ACTIVITY_PATH = os.path.join(
    ROOT, "app", "src", "main", "java", "com", "marketradar", "app", "MainActivity.kt"
)


class AndroidPollFeatureContractTests(unittest.TestCase):
    def test_gap_sigma_is_not_daily_sigma_in_points(self):
        with open(SERVICE_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()

        self.assertNotIn('poll.put("gap_sigma", dailySigma)', source)
        self.assertIn('poll.put("daily_sigma", dailySigma)', source)
        self.assertIn('poll.put("gap_sigma", overnightGapSigma)', source)
        self.assertIn('((bnfOpen - bnfPrevClose) / bnfPrevClose)', source)

    def test_supply_quality_history_is_prior_session_and_shadow_only(self):
        with open(SERVICE_PATH, "r", encoding="utf-8") as handle:
            service = handle.read()
        with open(SUPABASE_PATH, "r", encoding="utf-8") as handle:
            supabase = handle.read()
        with open(ML_SERVICE_PATH, "r", encoding="utf-8") as handle:
            ml_service = handle.read()

        self.assertIn("getPc2SupplyQualityHistory(today)", service)
        self.assertIn('ctxObj.put("pc2SupplyQualityHistory", supplyQualityHistory)', service)
        self.assertIn("session_date=lt.$targetDate&index_key=neq.MARKET", supabase)
        self.assertIn("snapshot_pc2_supply_quality_shadow", ml_service)
        self.assertIn("sample_candidates", ml_service)
        self.assertIn("directionalGenerationShadow", ml_service)

    def test_experimental_outcome_roles_cannot_abort_teacher_persistence(self):
        with open(SUPABASE_PATH, "r", encoding="utf-8") as handle:
            supabase = handle.read()

        self.assertIn('if (role != "primary" && role != "secondary") continue', supabase)
        self.assertIn('if (role == "rejected") continue', supabase)
        self.assertIn('row.put("role", role)', supabase)

    def test_daily_percentile_history_preserves_variable_provenance_and_is_verified(self):
        with open(SUPABASE_PATH, "r", encoding="utf-8") as handle:
            supabase = handle.read()

        self.assertIn('put("daily::$name", ordered)', supabase)
        self.assertIn('dayObj.put("pct_${variableName}_population_scope", populationScope)', supabase)
        self.assertIn('dayObj.put("pct_${variableName}_population_version", calibrationVersion)', supabase)
        self.assertIn('&session_date=eq.$sessionDate&history_source=eq.live', supabase)
        self.assertNotIn('&session_date=eq.$sessionDate&poll_ts=not.is.null&history_source=eq.live', supabase)

    def test_c3_history_seed_accepts_only_clean_provenance_verified_rows(self):
        with open(SUPABASE_PATH, "r", encoding="utf-8") as handle:
            supabase = handle.read()

        self.assertIn('&source_quality=eq.PRE_T_CLEAN', supabase)
        self.assertIn('&source_quality=eq.DAILY_CALIBRATION_UNION_VERIFIED', supabase)
        self.assertIn('fun isVerifiedC3SeedRow(row: JSONObject)', supabase)
        self.assertIn('PC2_CALIBRATION_POPULATION_VERSION', supabase)

    def test_c3_existing_id_verification_is_complete_and_deterministically_ordered(self):
        with open(SUPABASE_PATH, "r", encoding="utf-8") as handle:
            supabase = handle.read()

        self.assertIn('&order=id.asc&limit=$pageSize&offset=$offset', supabase)
        self.assertIn('while (true)', supabase)
        self.assertNotIn('for (page in 0 until 10)', supabase)

    def test_brain_snapshot_persistence_is_compact_and_canonical(self):
        with open(SERVICE_PATH, "r", encoding="utf-8") as handle:
            service = handle.read()
        with open(SUPABASE_PATH, "r", encoding="utf-8") as handle:
            supabase = handle.read()
        with open(LOCAL_CACHE_PATH, "r", encoding="utf-8") as handle:
            cache = handle.read()

        self.assertIn("compactBrainSnapshotForPersistence(rawSnapObj)", service)
        self.assertIn('"android_compact_v1"', service)
        self.assertIn('listOf("ml_brain_snapshots")', supabase)
        self.assertNotIn('val tables = listOf("ml_brain_snapshots", "ml_poll_sequences")', supabase)
        self.assertIn('"snapshot_pc2_authority_policy"', cache)
        self.assertIn('"snapshot_pc2_authority_decisions"', cache)
        self.assertIn('"snapshot_evaluation_legs"', cache)
        self.assertIn('"context_percentiles"', cache)
        self.assertIn('"verdict_json"', cache)
        self.assertIn('"market_forces_json"', cache)
        self.assertIn('"poll_summary_json"', cache)
        self.assertIn('"b1a_intraday_rv_json"', cache)
        self.assertIn('"b1a_rv_status"', cache)
        self.assertIn('"expiry"', cache)
        self.assertIn('"sellType"', cache)
        self.assertIn('"legs"', cache)
        self.assertIn('if (rejectedFull != null) "snapshot_rejected_candidates_full"', cache)
        persistence_compactor = cache.split("private fun compactBrainSnapshot(snapshot: JSONObject)", 1)[1]
        persistence_compactor = persistence_compactor.split("fun compactBrainSnapshotForPersistence", 1)[0]
        self.assertNotIn('"brain_version"', persistence_compactor)
        self.assertNotIn('"app_version"', persistence_compactor)
        self.assertNotIn('"pre_alignment_action"', persistence_compactor)

    def test_pc2_telemetry_does_not_depend_on_snapshot_save(self):
        with open(SERVICE_PATH, "r", encoding="utf-8") as handle:
            service = handle.read()

        snapshot_save = service.index("val snapshotSaved = SupabaseClient.saveBrainSnapshot(snapObj)")
        authority_save = service.index("val authorityTelemetrySaved = SupabaseClient.savePc2AuthorityDecisions(snapObj)")
        generated_save = service.index("generatedSaved = persistCompactGeneratedCandidates(generatedFactPack, snapObj)")
        success_gate = service.index("if (snapshotSaved && generatedSaved)")
        self.assertLess(snapshot_save, authority_save)
        self.assertLess(authority_save, success_gate)
        self.assertLess(generated_save, success_gate)

    def test_webview_recreation_is_diagnosable_and_recovers(self):
        with open(MAIN_ACTIVITY_PATH, "r", encoding="utf-8") as handle:
            activity = handle.read()

        self.assertIn("onRenderProcessGone", activity)
        self.assertIn("WEBVIEW_RENDERER_GONE", activity)
        self.assertIn("removeView(deadView)", activity)
        self.assertIn("ACTIVITY_CREATE", activity)
        self.assertIn("ACTIVITY_DESTROY", activity)
        self.assertIn("if (!webStateRestored)", activity)


if __name__ == "__main__":
    unittest.main()
