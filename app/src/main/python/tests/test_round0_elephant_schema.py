import importlib.util
import json
import os
import sys
import types
import unittest


def resolve_oracle_path():
    here = os.path.abspath(os.path.dirname(__file__))
    current = here
    for _ in range(8):
        candidate = os.path.join(current, "oracle_server", "evaluator_app.py")
        if os.path.exists(candidate):
            return candidate
        current = os.path.dirname(current)
    raise FileNotFoundError("Could not resolve oracle_server/evaluator_app.py from test path")


def load_oracle_module():
    oracle_path = resolve_oracle_path()
    if "fastapi" not in sys.modules:
        fastapi = types.ModuleType("fastapi")

        class DummyApp:
            def get(self, *args, **kwargs):
                def deco(fn):
                    return fn
                return deco

            def post(self, *args, **kwargs):
                def deco(fn):
                    return fn
                return deco

        fastapi.BackgroundTasks = object
        fastapi.FastAPI = lambda *args, **kwargs: DummyApp()
        fastapi.Request = object
        fastapi.HTTPException = Exception
        sys.modules["fastapi"] = fastapi
        responses = types.ModuleType("fastapi.responses")
        responses.JSONResponse = object
        sys.modules["fastapi.responses"] = responses

    if "google" not in sys.modules:
        google = types.ModuleType("google")
        generativeai = types.ModuleType("google.generativeai")

        class DummyModel:
            def __init__(self, *args, **kwargs):
                pass

        generativeai.configure = lambda **kwargs: None
        generativeai.GenerativeModel = DummyModel
        generativeai.types = types.SimpleNamespace(GenerationConfig=object)
        google.generativeai = generativeai
        sys.modules["google"] = google
        sys.modules["google.generativeai"] = generativeai

    spec = importlib.util.spec_from_file_location("evaluator_app", oracle_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Round0ElephantSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.oracle = load_oracle_module()

    def test_build_prompt_contains_qualitative_contract(self):
        body = {
            "lane": "NF_intraday",
            "poll_timestamp": "2026-06-13T10:00:00+00:00",
            "session_date": "2026-06-13",
            "trade_mode": "intraday",
            "decision_source": "BRAIN",
            "market_context": {"vix": 15.2, "nf_spot": 23500},
            "verdict_context": {"action": "SELL PREMIUM", "strategy": "BULL_PUT"},
            "signal_independence": {"score": 0.72},
            "coherence_signal": {"label": "Signals aligned", "impact": "bullish"},
            "candidate_counts": {"generated": 4, "watchlist": 3},
        }
        candidates = [{
            "candidate_id": "cand-1",
            "rank": 1,
            "lane": "NF_intraday",
            "index": "NF",
            "strategy_type": "BULL_PUT",
            "trade_mode": "intraday",
            "watchlist_rank": 1,
            "economics": {"risk_reward": "1:0.32"},
            "structure": {"sigma_otm": 0.63},
            "execution": {"execution_ready": True},
            "ml_overlay": {"decision_source": "BRAIN"},
        }]

        prompt = self.oracle.build_elephant_prompt(body, candidates)

        self.assertIn("distribution_signal", prompt)
        self.assertIn("coherence_read", prompt)
        self.assertIn("anomaly_flag", prompt)
        self.assertIn("candidate_notes", prompt)
        self.assertNotIn("approved", prompt)
        self.assertNotIn("sellStrike", prompt)

    def test_normalize_elephant_verdict_accepts_expected_enums(self):
        raw = {
            "distribution_signal": "genuine",
            "coherence_read": "aligned",
            "anomaly_flag": True,
            "anomaly_reason": "volatility spike against spot drift",
            "brief": "Market shape is genuine and internally aligned.",
            "candidate_notes": [
                {"candidate_id": "cand-1", "stance": "neutral", "reason": "structure fits move"},
                {"candidate_id": "cand-2", "stance": "caution", "reason": "premium edge thin"},
            ],
        }

        normalized = self.oracle.normalize_elephant_verdict(raw)

        self.assertEqual(normalized["schema_version"], self.oracle.QUALITATIVE_SCHEMA_VERSION)
        self.assertEqual(normalized["distribution_signal"], "genuine")
        self.assertEqual(normalized["coherence_read"], "aligned")
        self.assertTrue(normalized["anomaly_flag"])
        self.assertEqual(len(normalized["candidate_notes"]), 2)
        self.assertEqual(normalized["candidate_notes"][0]["stance"], "neutral")

    def test_normalize_elephant_verdict_fails_closed(self):
        raw = {
            "distribution_signal": "explosive",
            "coherence_read": "weird",
            "anomaly_flag": "yes",
            "anomaly_reason": "x" * 500,
            "brief": "y" * 800,
            "candidate_notes": [
                {"candidate_id": "", "stance": "boost", "reason": "bad"},
                "not-a-dict",
            ],
        }

        normalized = self.oracle.normalize_elephant_verdict(raw)

        self.assertEqual(normalized["distribution_signal"], "unclear")
        self.assertEqual(normalized["coherence_read"], "unclear")
        self.assertTrue(normalized["anomaly_flag"])
        self.assertLessEqual(len(normalized["anomaly_reason"]), 240)
        self.assertLessEqual(len(normalized["brief"]), 480)
        self.assertEqual(normalized["candidate_notes"], [])

    def test_normalized_flags_are_json_serializable(self):
        normalized = self.oracle.normalize_elephant_verdict({"brief": "ok"})
        json.dumps(normalized)


if __name__ == "__main__":
    unittest.main()
