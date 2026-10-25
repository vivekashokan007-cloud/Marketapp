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


def rewrite_export(src, out, changes, record_override=None, drop=()):
    """Write a self-consistent export (inventory + SHA256SUMS updated) with [changes] {path: bytes}. Models an export
    that faithfully copied the given bytes; it is NOT a tampered export."""
    record_override = record_override or {}
    with zipfile.ZipFile(src) as zi:
        man = json.loads(zi.read("export_manifest.json"))
        data = {n: zi.read(n) for n in zi.namelist() if n not in ("export_manifest.json", "SHA256SUMS.txt")}
    data.update(changes)
    for d in drop:
        data.pop(d, None)
    files = []
    for f in man["files"]:
        if f["path"] in drop:
            continue
        b = data[f["path"]]
        f = dict(f, size=len(b), sha256=V.sha(b))
        if f["path"] in record_override:
            f.update(record_override[f["path"]])
        files.append(f)
    man["files"] = files
    mb = json.dumps(man, indent=2).encode()
    sums = "".join("%s  %s\n" % (f["sha256"], f["path"]) for f in files) + "%s  export_manifest.json\n" % V.sha(mb)
    with zipfile.ZipFile(out, "w") as zo:
        for n, b in data.items():
            zo.writestr(n, b)
        zo.writestr("export_manifest.json", mb)
        zo.writestr("SHA256SUMS.txt", sums)


def reseal(raw, body=None, **outer):
    o = json.loads(raw)
    if body is not None:
        o["body"] = json.dumps(body)
        o["sha256"] = V.sha(o["body"].encode())
    o.update(outer)
    return json.dumps(o).encode()


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

    def _imap(self):
        z = zipfile.ZipFile(FIX)
        a, rows = server_rows(z)
        name = "research_m0/imap_%s.json" % a["capture"]["imap_version"]
        return z, a, rows, name

    def test_b9_ambiguous_map_rejected(self):
        z, a, rows, name = self._imap()
        out = os.path.join(self.d, "amb.zip")
        body = json.loads(json.loads(z.read(name))["body"])
        body["lines"] = body["lines"] + "\n" + body["lines"].split("\n")[0] + "X"
        rewrite_export(FIX, out, {name: reseal(z.read(name), body)})
        self.assertEqual(V.verify(out)["result"], "VERIFIED")
        r = self.cli("b9", out, "--slot-id", "NF|2026-10-22|1", "--attempt-id", a["attempt_id"], "--rows", self.w("r.json", rows))
        self.assertEqual(r.returncode, 2); self.assertIn("ambiguous", r.stdout)

    # ---- R1a: export trust prerequisite and referenced-record fail states
    def test_r1a_tampered_export_is_export_invalid_exit_3_even_with_matching_rows(self):
        z, a, rows, _ = self._imap()
        out = os.path.join(self.d, "t.zip")
        with zipfile.ZipFile(FIX) as zi, zipfile.ZipFile(out, "w") as zo:
            for n in zi.namelist():
                zo.writestr(n, zi.read(n) + (b" " if n == "research_m0/seq.json" else b""))
        r = self.cli("b9", out, "--slot-id", "NF|2026-10-22|1", "--attempt-id", a["attempt_id"], "--rows", self.w("r1.json", rows), "--rows2", self.w("r2.json", rows))
        self.assertEqual(r.returncode, 3, r.stdout)
        o = json.loads(r.stdout); self.assertEqual(o["result"], "EXPORT_INVALID"); self.assertTrue(o["export_problems"])

    def test_r1a_faithfully_copied_corrupt_reference_is_unverifiable_not_invalid(self):
        z, a, rows, name = self._imap()
        out = os.path.join(self.d, "c.zip")
        bad = z.read(name).replace(b"NSE_FO", b"NSE_FX", 1)        # original-on-phone corruption: digest mismatch
        rewrite_export(FIX, out, {name: bad}, {name: {"record": "CORRUPT", "reason": "digest_mismatch"}})
        self.assertEqual(V.verify(out)["result"], "VERIFIED")        # the copy is faithful
        o = V.b9(out, "NF|2026-10-22|1", a["attempt_id"], self.w("r.json", rows))
        self.assertEqual(o["result"], "EVIDENCE_UNVERIFIABLE"); self.assertEqual(o["fail_state"], "REFERENCED_RECORD_CORRUPT_AT_SOURCE")

    def test_r1a_wrong_kind_or_owner_claimed_ok_is_export_invalid_and_recorded_corrupt_is_unverifiable(self):
        z, a, rows, name = self._imap()
        for outer, state in (({"kind": "env"}, "WRONG_KIND"), ({"owner": "other"}, "WRONG_OWNER"), ({"m0_sealed": 0}, "NOT_SEALED")):
            out = os.path.join(self.d, "k.zip")
            rewrite_export(FIX, out, {name: reseal(z.read(name), **outer)})
            self.assertEqual(V.verify(out)["result"], "INVALID", state)
            self.assertEqual(V.b9(out, "NF|2026-10-22|1", a["attempt_id"], self.w("r.json", rows))["result"], "EXPORT_INVALID")
            self.assertEqual(V.seal_check(reseal(z.read(name), **outer), name)[1], state)
        out = os.path.join(self.d, "m.zip")
        rewrite_export(FIX, out, {}, drop=(name,))
        o = V.b9(out, "NF|2026-10-22|1", a["attempt_id"], self.w("r.json", rows))
        self.assertEqual((o["result"], o["fail_state"]), ("EVIDENCE_UNVERIFIABLE", "REFERENCED_RECORD_MISSING"))
        out = os.path.join(self.d, "u.zip")
        rewrite_export(FIX, out, {}, {name: {"record": "SEALED_NOT_CHECKED_SIZE"}})
        self.assertTrue(V.b9(out, "NF|2026-10-22|1", a["attempt_id"], self.w("r.json", rows))["fail_state"].startswith("REFERENCED_RECORD_UNCHECKED"))

    # ---- R1b: both readbacks fully checked, failures attributed
    def test_r1b_bad_second_readback_is_mismatch_attributed_to_readback_2(self):
        z, a, rows, _ = self._imap()
        _, other = server_rows(z, ordinal="2", start_id=2000)
        o = V.b9(FIX, "NF|2026-10-22|1", a["attempt_id"], self.w("r1.json", rows), self.w("r2.json", rows + other[:1]))
        self.assertEqual(o["result"], "MISMATCH")
        self.assertTrue(o["readback_1"]["ok"]); self.assertFalse(o["readback_2"]["ok"])
        self.assertIn("readback_2:findings", o["failures"]); self.assertIn("readback_2:rows_discarded", o["failures"])
        self.assertFalse(any(f.startswith("readback_1") for f in o["failures"]))
        # reversed roles: the defect is attributed to readback 1
        o = V.b9(FIX, "NF|2026-10-22|1", a["attempt_id"], self.w("r1.json", rows + other[:1]), self.w("r2.json", rows))
        self.assertEqual(o["result"], "MISMATCH"); self.assertIn("readback_1:findings", o["failures"])
        # a wrong value in readback 2 only
        bad = [dict(x) for x in rows]; bad[1]["bid"] = 9.4
        o = V.b9(FIX, "NF|2026-10-22|1", a["attempt_id"], self.w("r1.json", rows), self.w("r2.json", bad))
        self.assertEqual(o["failures"], ["readback_2:digest_mismatch"])
        # a subset in readback 2 (count + ids)
        o = V.b9(FIX, "NF|2026-10-22|1", a["attempt_id"], self.w("r1.json", rows), self.w("r2.json", rows[:3]))
        self.assertIn("readback_2:row_count_3_expected_4", o["failures"]); self.assertIn("readback_2:source_row_ids_differ_from_readback_1", o["failures"])


if __name__ == "__main__":
    unittest.main()
