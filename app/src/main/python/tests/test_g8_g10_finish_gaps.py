"""G8/G10 finish gaps: multi-page export contracts, E3 honesty, champion CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import e3_persistence_contract as e3
import training_export_integrity as tei
import training_split_hygiene as tsh

MARKETAPP = Path(__file__).resolve().parents[5]
CLI = MARKETAPP / "tools" / "champion_challenger_compare.py"
SB = MARKETAPP / "app/src/main/java/com/marketradar/app/SupabaseClient.kt"
ML = MARKETAPP / "app/src/main/java/com/marketradar/app/MarketMLService.kt"


class MultiPageExportContractTests(unittest.TestCase):
    def test_collect_all_pages_empty(self):
        got = tei.collect_all_pages([], page_size=10, max_pages=3)
        self.assertEqual(got["status"], tei.STATUS_EMPTY)
        self.assertEqual(got["returned"], 0)

    def test_supabase_client_has_paged_helpers(self):
        src = SB.read_text(encoding="utf-8")
        self.assertIn("fun selectAllPages(", src)
        self.assertIn("offset: Int? = null", src)
        self.assertIn("fun fetchRecentEvaluationOutcomesPaged(", src)
        self.assertIn("fun fetchRecentBrainSnapshotsPaged(", src)
        self.assertIn("truncated_at_max_pages", src)

    def test_market_ml_service_multi_page_and_e3_honesty(self):
        src = ML.read_text(encoding="utf-8")
        self.assertIn("selectAllPages", src)
        self.assertIn("fetchRecentEvaluationOutcomesPaged", src)
        self.assertIn("END-OF-RUN", src)
        self.assertIn("mid-loop remote upsert is NOT", src)


class E3RuntimeHonestyTests(unittest.TestCase):
    def test_runtime_flags_document_end_of_run(self):
        self.assertEqual(e3.RUNTIME_REMOTE_SAVE_PATH, "end_of_run_saveEvaluationOutcomes")
        self.assertFalse(e3.RUNTIME_MID_LOOP_REMOTE_UPSERT)
        self.assertTrue(e3.RUNTIME_LOCAL_CHECKPOINT)


class ChampionChallengerCliTests(unittest.TestCase):
    def test_cli_compares_compatible_manifests(self):
        split = {
            "train_sessions": ["2026-09-01"],
            "calib_sessions": ["2026-09-02"],
            "test_sessions": ["2026-09-03"],
            "held_out_sessions": [],
        }
        champ = tsh.build_immutable_manifest(
            model_id="champ",
            model_hash="aaa",
            feature_schema_version="fs1",
            net_target_version="net1",
            policy_selector_version="pol1",
            split=split,
        )
        chall = tsh.build_immutable_manifest(
            model_id="chall",
            model_hash="bbb",
            feature_schema_version="fs1",
            net_target_version="net1",
            policy_selector_version="pol1",
            split=split,
        )
        scores_a = [{"y_true": 1, "y_prob": 0.8}, {"y_true": 0, "y_prob": 0.2}]
        scores_b = [{"y_true": 1, "y_prob": 0.6}, {"y_true": 0, "y_prob": 0.4}]
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            cpath = td_path / "champ.json"
            dpath = td_path / "chall.json"
            sa = td_path / "sa.json"
            sb = td_path / "sb.json"
            out = td_path / "out.json"
            cpath.write_text(json.dumps(champ), encoding="utf-8")
            dpath.write_text(json.dumps(chall), encoding="utf-8")
            sa.write_text(json.dumps(scores_a), encoding="utf-8")
            sb.write_text(json.dumps(scores_b), encoding="utf-8")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "--champion",
                    str(cpath),
                    "--challenger",
                    str(dpath),
                    "--champion-scores",
                    str(sa),
                    "--challenger-scores",
                    str(sb),
                    "--out",
                    str(out),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(out.read_text(encoding="utf-8"))
            self.assertTrue(result["ok"])
            self.assertEqual(result["promotion"], "not_requested")
            self.assertTrue(result["read_only"])
            self.assertFalse(result["writes_model"])

    def test_cli_blocks_incompatible_test_sessions(self):
        a = tsh.build_immutable_manifest(
            model_id="a",
            model_hash="1",
            feature_schema_version="fs1",
            net_target_version="net1",
            policy_selector_version="pol1",
            split={"train_sessions": [], "calib_sessions": [], "test_sessions": ["2026-09-01"], "held_out_sessions": []},
        )
        b = tsh.build_immutable_manifest(
            model_id="b",
            model_hash="2",
            feature_schema_version="fs1",
            net_target_version="net1",
            policy_selector_version="pol1",
            split={"train_sessions": [], "calib_sessions": [], "test_sessions": ["2026-09-02"], "held_out_sessions": []},
        )
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            ap = td_path / "a.json"
            bp = td_path / "b.json"
            ap.write_text(json.dumps(a), encoding="utf-8")
            bp.write_text(json.dumps(b), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(CLI), "--champion", str(ap), "--challenger", str(bp)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 1)
            result = json.loads(proc.stdout)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "incompatible_test_sessions")


if __name__ == "__main__":
    unittest.main()
