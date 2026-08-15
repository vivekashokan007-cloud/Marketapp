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


if __name__ == "__main__":
    unittest.main()
