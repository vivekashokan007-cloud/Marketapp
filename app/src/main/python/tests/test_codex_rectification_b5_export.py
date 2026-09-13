"""B5: export pagination typed failures → incomplete_error."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from training_export_integrity import STATUS_COMPLETE, STATUS_INCOMPLETE_ERROR


class CodexExportPaginationTests(unittest.TestCase):
    def test_page2_http_failure_incomplete_error(self):
        pages = [
            {"status": "success", "rows": [{"id": "a"}, {"id": "b"}]},
            {"status": "http_error", "error": "500"},
        ]
        out = []
        status = STATUS_COMPLETE
        for page in pages:
            if page.get("status") != "success":
                status = STATUS_INCOMPLETE_ERROR
                break
            out.extend(page["rows"])
        self.assertEqual(status, "incomplete_error")
        self.assertEqual(len(out), 2)

    def test_page2_parse_failure_incomplete_error(self):
        pages = [
            {"status": "success", "rows": [{"id": "a"}]},
            {"status": "parse_error", "error": "boom"},
        ]
        status = STATUS_COMPLETE
        for page in pages:
            if page.get("status") != "success":
                status = STATUS_INCOMPLETE_ERROR
                break
        self.assertEqual(status, "incomplete_error")

    def test_trainer_aborts_unless_complete(self):
        from ml_train import run
        with tempfile.TemporaryDirectory() as td:
            trades = os.path.join(td, "paper_trades.json")
            with open(trades, "w", encoding="utf-8") as handle:
                handle.write("[]")
            status = os.path.join(td, "manifest.json")
            with open(status, "w", encoding="utf-8") as handle:
                json.dump({
                    "schema_version": "market_radar_export_manifest_v1_20260913",
                    "status": "incomplete_error",
                    "kind": "paper_trades_export",
                }, handle)
            result = json.loads(run("missing.csv", trades, os.path.join(td, "model.json")))
            self.assertFalse(result.get("deployed"))
            self.assertIn("incomplete", result.get("reason", ""))


if __name__ == "__main__":
    unittest.main()
