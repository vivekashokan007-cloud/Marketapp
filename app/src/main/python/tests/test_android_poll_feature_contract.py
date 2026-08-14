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


class AndroidPollFeatureContractTests(unittest.TestCase):
    def test_gap_sigma_is_not_daily_sigma_in_points(self):
        with open(SERVICE_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()

        self.assertNotIn('poll.put("gap_sigma", dailySigma)', source)
        self.assertIn('poll.put("daily_sigma", dailySigma)', source)
        self.assertIn('poll.put("gap_sigma", overnightGapSigma)', source)
        self.assertIn('((bnfOpen - bnfPrevClose) / bnfPrevClose)', source)


if __name__ == "__main__":
    unittest.main()
