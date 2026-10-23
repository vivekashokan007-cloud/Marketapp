"""Executable regressions for tools/research_m0_export/verify_export.py (offline; fixture produced by the real Kotlin
export in M0Stage2ExportStatusTest). Run: python3 -m unittest discover -s tools/research_m0_export -p 'test_*.py'"""
import io, json, os, shutil, subprocess, sys, tempfile, unittest, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import verify_export as V  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "export_6polls.zip")


def manifest(z):
    return json.loads(json.loads(z.read("research_m0/manifest_2026-10-22.json"))["body"])


def server_rows(z, index="NF", ordinal="1", start_id=1000):
    a = manifest(z)["indices"][index]["slots"][ordinal]["attempts"][0]
    ts = a["capture"]["poll_ts"]
    rows, i = [], start_id
    for k in (100, 200):
        for t in ("CE", "PE"):
            rows.append({"id": i, "poll_ts": ts.replace("T", " ").replace("Z", "+00"), "index_key": index, "expiry": "2026-10-27",
                         "strike": k, "option_type": t, "bid": 9.5, "ask": 10.5, "ltp": 10})
            i += 1
    return a, rows


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def w(self, name, obj):
        p = os.path.join(self.d, name)
        with open(p, "w") as f:
            json.dump(obj, f)
        return p

    def cli(self, *a):
        return subprocess.run([sys.executable, os.path.join(HERE, "verify_export.py"), *a], capture_output=True, text=True)

    def test_vector_matches_kotlin(self):
        rows = [V.canonical_row("2026-10-27", 100.0, "CE", "NSE_FO|NF100CE", 9.5, 10.5, 10.0),
                V.canonical_row("2026-10-27", 100, "put", "NSE_FO|NF100PE", "-0.0", None, "1E+1")]
        self.assertEqual(rows, ["2026-10-27|100|CE|NSE_FO|NF100CE|9.5|10.5|10", "2026-10-27|100|PE|NSE_FO|NF100PE|0|null|10"])
        self.assertEqual(V.rows_digest("NF", rows), "4cdf7037105f02e6c918168ae2b516d6e336e546fb33ea5e6bada51b68ed532d")

    def test_verify_real_kotlin_export(self):
        r = self.cli("verify", FIX)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(json.loads(r.stdout)["result"], "VERIFIED")

    def test_verify_detects_tampered_entry(self):
        out = os.path.join(self.d, "t.zip")
        with zipfile.ZipFile(FIX) as zi, zipfile.ZipFile(out, "w") as zo:
            for n in zi.namelist():
                b = zi.read(n)
                if n == "research_m0/seq.json":
                    b = b.replace(b"seq", b"Seq", 1)
                zo.writestr(n, b)
        r = self.cli("verify", out)
        self.assertEqual(r.returncode, 3)
        self.assertIn("bytes differ", r.stdout)

    def test_verify_malformed_inputs_are_invalid_exit_3(self):
        bad = os.path.join(self.d, "x.zip")
        with open(bad, "wb") as f: f.write(b"not a zip")
        self.assertEqual(self.cli("verify", bad).returncode, 3)
        nm = os.path.join(self.d, "nm.zip")
        with zipfile.ZipFile(nm, "w") as z: z.writestr("export_manifest.json", "{not json")
        self.assertEqual(self.cli("verify", nm).returncode, 3)

    def test_b9_match_with_independent_second_readback(self):
        z = zipfile.ZipFile(FIX)
        a, rows = server_rows(z)
        r1 = self.w("r1.json", rows)
        r2 = self.w("r2.json", list(reversed(rows)))     # second extraction by source row ids, other order
        r = self.cli("b9", FIX, "--slot-id", "NF|2026-10-22|1", "--attempt-id", a["attempt_id"], "--rows", r1, "--rows2", r2)
        self.assertEqual(r.returncode, 0, r.stdout)
        o = json.loads(r.stdout)
        self.assertEqual(o["digest_1"], a["capture"]["source_rows_sha256"])
        self.assertTrue(o["ids_equal"])

    def test_b9_rejects_other_poll_marks_wrong_value_and_missing_key(self):
        z = zipfile.ZipFile(FIX)
        a, rows = server_rows(z)
        _, other = server_rows(z, ordinal="2", start_id=2000)
        args = ["--slot-id", "NF|2026-10-22|1", "--attempt-id", a["attempt_id"]]
        r = self.cli("b9", FIX, *args, "--rows", self.w("o.json", rows + other[:1]))
        self.assertEqual(r.returncode, 2); self.assertIn("another poll", r.stdout)
        bad = [dict(x) for x in rows]; bad[0]["ltp"] = 10.05
        self.assertEqual(self.cli("b9", FIX, *args, "--rows", self.w("v.json", bad)).returncode, 2)
        miss = [dict(x) for x in rows]; miss[0]["strike"] = 300
        r = self.cli("b9", FIX, *args, "--rows", self.w("m.json", miss))
        self.assertEqual(r.returncode, 2); self.assertIn("missing from map", r.stdout)
        dup = rows + [dict(rows[0], id=9999)]
        r = self.cli("b9", FIX, *args, "--rows", self.w("d.json", dup))
        self.assertEqual(r.returncode, 2); self.assertIn("duplicate", r.stdout)
        # second readback with a different id set is a mismatch
        r = self.cli("b9", FIX, *args, "--rows", self.w("a.json", rows), "--rows2", self.w("b.json", rows[:3]))
        self.assertEqual(r.returncode, 2)

    def test_b9_invalid_inputs(self):
        z = zipfile.ZipFile(FIX)
        a, rows = server_rows(z)
        args = ["--slot-id", "NF|2026-10-22|1", "--attempt-id", a["attempt_id"]]
        self.assertEqual(self.cli("b9", FIX, *args, "--rows", self.w("n.json", {"x": 1})).returncode, 3)
        wt = [dict(x) for x in rows]; wt[0]["strike"] = "100"
        self.assertEqual(self.cli("b9", FIX, *args, "--rows", self.w("t.json", wt)).returncode, 3)
        bt = [dict(x) for x in rows]; bt[0]["poll_ts"] = "2026-02-30T17:00+05:30"
        self.assertEqual(self.cli("b9", FIX, *args, "--rows", self.w("bt.json", bt)).returncode, 3)
        self.assertEqual(self.cli("b9", FIX, "--slot-id", "NF|2026-10-22|1", "--attempt-id", "nope", "--rows", self.w("ok.json", rows)).returncode, 3)

    def test_b9_ambiguous_map_rejected(self):
        out = os.path.join(self.d, "amb.zip")
        z = zipfile.ZipFile(FIX)
        a, rows = server_rows(z)
        imv = a["capture"]["imap_version"]
        name = "research_m0/imap_%s.json" % imv
        with zipfile.ZipFile(out, "w") as zo:
            for n in z.namelist():
                b = z.read(n)
                if n == name:
                    o = json.loads(b); body = json.loads(o["body"])
                    first = body["lines"].split("\n")[0]
                    body["lines"] = body["lines"] + "\n" + first + "X"
                    nb = json.dumps(body); o["body"] = nb; o["sha256"] = V.sha(nb.encode()); b = json.dumps(o).encode()
                zo.writestr(n, b)
        r = self.cli("b9", out, "--slot-id", "NF|2026-10-22|1", "--attempt-id", a["attempt_id"], "--rows", self.w("r.json", rows))
        self.assertEqual(r.returncode, 2); self.assertIn("ambiguous", r.stdout)


if __name__ == "__main__":
    unittest.main()
