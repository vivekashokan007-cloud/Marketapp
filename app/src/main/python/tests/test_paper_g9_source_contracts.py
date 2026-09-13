import os
import unittest
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
# Marketapp root: .../Marketapp
MARKETAPP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
# tests -> python -> main -> src -> app -> Marketapp  (5 levels up from tests is app? )
# __file__ = Marketapp/app/src/main/python/tests/test_*.py
# parents: tests, python, main, src, app, Marketapp
MARKETAPP = os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 5)))
MR_ROOT = os.path.abspath(os.path.join(MARKETAPP, ".."))
PWA = os.path.join(MR_ROOT, "MarketVivi", "app.js")
BRIDGE = os.path.join(MARKETAPP, "app", "src", "main", "java", "com", "marketradar", "app", "NativeBridge.kt")
ML = os.path.join(MARKETAPP, "app", "src", "main", "java", "com", "marketradar", "app", "MarketMLService.kt")


class PaperG9SourceContractTests(unittest.TestCase):
    def test_pwa_exposes_paper_analysis_alternatives(self):
        self.assertTrue(os.path.exists(PWA), PWA)
        text = Path(PWA).read_text(encoding="utf-8")
        self.assertIn("paperAnalysisAlternatives", text)
        self.assertIn("PAPER ANALYSIS — non-primary alternatives", text)
        self.assertIn("finalEntryAuthorization", text)
        # Real path still gated
        self.assertIn("takeTrade('${cand.id}', false)", text)

    def test_native_bridge_compacts_paper_analysis_fields(self):
        text = Path(BRIDGE).read_text(encoding="utf-8")
        self.assertIn("paperAnalysisEligible", text)
        self.assertIn("paperAnalysisEligibility", text)

    def test_market_ml_service_keeps_atomic_checkpoint_language(self):
        text = Path(ML).read_text(encoding="utf-8")
        self.assertIn("atomically", text)
        self.assertIn("saveEvaluationOutcomes", text)

    def test_pwa_exposes_experimental_kelly_advisory_readout(self):
        text = Path(PWA).read_text(encoding="utf-8")
        self.assertIn("experimentalKellyAdvisoryReadout", text)
        self.assertIn("EXPERIMENTAL Kelly", text)
        self.assertIn("not order qty", text)
        self.assertIn("p_ml gate unchanged", text)
        # Must not wire into takeTrade quantity
        self.assertIn("takeTrade('${cand.id}', false)", text)

    def test_market_ml_service_documents_e3_end_of_run(self):
        text = Path(ML).read_text(encoding="utf-8")
        self.assertIn("END-OF-RUN", text)
        self.assertIn("selectAllPages", text)


if __name__ == "__main__":
    unittest.main()
