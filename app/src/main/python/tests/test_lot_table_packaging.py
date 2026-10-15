"""Exercise the real lot resolver/EV consumer without repository assets or a repository cwd."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PYTHON = Path(__file__).resolve().parents[1]
ASSET = PYTHON.parent / "assets" / "contract_lot_table_v2.json"


class PackagedLotTable(unittest.TestCase):
    def isolated(self, code, *, missing=False, corrupt=False):
        with tempfile.TemporaryDirectory() as root:
            modules = Path(root) / "chaquopy" / "app"
            modules.mkdir(parents=True)
            for source in PYTHON.glob("*.py"):
                if missing and source.name == "_contract_lot_table_data.py":
                    continue
                shutil.copyfile(source, modules / source.name)
            if corrupt:
                with (modules / "_contract_lot_table_data.py").open("a") as f:
                    f.write("\nTABLE_SHA256 = 'invalid'\n")
            env = dict(os.environ, PYTHONPATH=str(modules), PYTHONNOUSERSITE="1")
            return json.loads(subprocess.check_output([sys.executable, "-c", code], cwd=root, env=env, text=True))

    def test_generated_module_equals_the_entire_source_table(self):
        from _contract_lot_table_data import TABLE_JSON, TABLE_SHA256
        data = json.loads(ASSET.read_text())
        self.assertEqual(json.loads(TABLE_JSON), data)
        self.assertEqual(hashlib.sha256(TABLE_JSON.encode()).hexdigest(), TABLE_SHA256)
        self.assertEqual(len(data["authoritative_contract_rules"]), 16)

    def test_actual_ev_and_nf_bnf_resolution_in_an_assetless_device_layout(self):
        out = self.isolated('''
import json
import contract_lot_table as T
import ev_v12_integration as I
print(json.dumps({
    "nf": T.resolve_contract_lot("NF", as_of="2026-10-15", expiry="2026-10-19"),
    "bnf": T.resolve_contract_lot("BNF", as_of="2026-10-15", expiry="2026-10-27", expiry_cycle="monthly"),
    "ev": I._lot("2026-10-15", "2026-10-19", {}),
    "unknown": T.resolve_contract_lot("UNKNOWN", as_of="2026-10-15", expiry="2026-10-19"),
    "conflict": T.resolve_contract_lot("NF", as_of="2026-10-15", expiry="2026-10-19", captured_contract_lot=50)
}))
''')
        for key, lot in [("nf", 65), ("bnf", 30)]:
            self.assertTrue(out[key]["resolved"])
            self.assertEqual(out[key]["contract_lot_size"], lot)
            self.assertEqual(out[key]["lot_table_source"], "packaged_python_generated")
            self.assertEqual(len(out[key]["lot_table_sha256"]), 64)
            self.assertEqual(out[key]["lot_source"], "authoritative_contract_rule")
        self.assertEqual(out["ev"][0], 65)
        self.assertEqual(out["ev"][1]["status"], "OK")
        self.assertEqual(out["ev"][1]["lot_table_sha256"], out["nf"]["lot_table_sha256"])
        self.assertFalse(out["unknown"]["resolved"])
        self.assertTrue(out["conflict"]["lot_conflict"])
        self.assertFalse(out["conflict"]["resolved"])

    def test_missing_or_corrupt_device_module_fails_explicitly_without_claiming_authoritative_version(self):
        for kwargs in [{"missing": True}, {"corrupt": True}]:
            out = self.isolated('''
import json
import contract_lot_table as T
import ev_v12_integration as I
print(json.dumps({"resolved": T.resolve_contract_lot("NF", as_of="2026-10-15", expiry="2026-10-19"),
                  "ev": I._lot("2026-10-15", "2026-10-19", {})}))
''', **kwargs)
            result = out["resolved"]
            self.assertFalse(result["resolved"])
            self.assertEqual(result["unavailable_reason"], "lot_table_unavailable")
            self.assertEqual(result["lot_table_version"], "contract_lot_table_unavailable_v1")
            self.assertEqual(result["lot_table_source"], "unavailable")
            self.assertIsNone(out["ev"][0])
            self.assertEqual(out["ev"][1]["status"], "LOT_TABLE_UNAVAILABLE")

    def test_checkout_and_device_table_content_have_identical_digest_and_rules(self):
        import contract_lot_table as T
        out = self.isolated('''
import json, contract_lot_table as T
print(json.dumps(T.lot_table_provenance()))
''')
        T.clear_lot_table_cache()
        self.assertEqual(T.lot_table_provenance()["lot_table_source"], "asset_json")
        self.assertEqual(T.lot_table_provenance()["lot_table_sha256"], out["lot_table_sha256"])


if __name__ == "__main__":
    unittest.main()
