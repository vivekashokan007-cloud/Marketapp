"""b515 CANDIDATE-ONLY: staging/verification of the pinned PWA bundle and the artifact-only workflow contract."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import stage_candidate_pwa as S  # noqa: E402

WORKFLOW = ROOT / ".github/workflows/candidate-apk-artifact.yml"
PIN = ROOT / "app/src/candidate/pwa_expected_manifest.json"


def git(repo: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()


class FakeMarketVivi:
    """A throwaway git repo with the six runtime files plus documents that must never be staged."""

    def __init__(self, tmp: Path):
        self.dir = tmp / "mv"
        self.dir.mkdir()
        git(self.dir, "init", "-q")
        git(self.dir, "config", "user.email", "t@example.invalid")
        git(self.dir, "config", "user.name", "t")
        (self.dir / "index.html").write_text('<script src="app.js?v=1381"></script>\n')
        (self.dir / "app.js").write_text("const M0_PWA_IDENTITY = { version: '2.6.86', build: 517, app_js_v: 1381 };\n")
        for n in ("api.js", "log-viewer.js", "style.css", "manifest.json"):
            (self.dir / n).write_text(f"/* {n} */\n")
        (self.dir / "CLAUDE.md").write_text("secret-ish notes\n")
        (self.dir / "docs").mkdir()
        (self.dir / "docs" / "audit.md").write_text("audit\n")
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-qm", "pin")
        self.sha = git(self.dir, "rev-parse", "HEAD")


class StageTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)
        self.mv = FakeMarketVivi(self.tmp)
        self.pin = self.tmp / "pin.json"
        S.main(["--marketvivi-dir", str(self.mv.dir), "--expected-manifest", str(self.pin), "--write-expected", self.mv.sha])

    def tearDown(self):
        self._t.cleanup()

    def stage(self, out: Path, **kw):
        return S.stage(self.mv.dir, self.pin, out, **kw)

    def test_stages_exactly_the_runtime_allowlist_with_matching_hashes(self):
        out = self.tmp / "out"
        lines = self.stage(out)
        self.assertEqual(len(lines), 6)
        self.assertEqual(sorted(p.name for p in out.iterdir()), sorted([*S.RUNTIME_FILES, "candidate_manifest.json"]))
        self.assertEqual((out / "candidate_manifest.json").read_bytes(), self.pin.read_bytes())
        m = json.loads(self.pin.read_text())
        for n in S.RUNTIME_FILES:
            self.assertEqual(hashlib.sha256((out / n).read_bytes()).hexdigest(), m["files"][n]["sha256"])
        self.assertFalse((out / "CLAUDE.md").exists())
        self.assertFalse((out / "docs").exists())
        self.assertEqual(m["pwa_identity"], {"version": "2.6.86", "build": 517, "app_js_v": 1381, "index_app_js_v": 1381})

    def test_tampered_pin_fails_closed(self):
        m = json.loads(self.pin.read_text())
        m["files"]["app.js"]["sha256"] = "0" * 64
        self.pin.write_text(json.dumps(m))
        with self.assertRaisesRegex(SystemExit, "hash mismatch for app.js"):
            self.stage(self.tmp / "out")

    def test_tampered_bytes_at_a_new_commit_fail(self):
        (self.mv.dir / "app.js").write_text("// tampered\n")
        git(self.mv.dir, "commit", "-qam", "tamper")
        with self.assertRaisesRegex(SystemExit, "HEAD .* != pinned"):
            self.stage(self.tmp / "out")

    def test_working_tree_edits_are_never_staged(self):
        (self.mv.dir / "app.js").write_text("// uncommitted local edit\n")
        out = self.tmp / "out"
        self.stage(out)
        self.assertIn("M0_PWA_IDENTITY", (out / "app.js").read_text())

    def test_expect_sha_cross_check(self):
        with self.assertRaisesRegex(SystemExit, "requested PWA sha"):
            self.stage(self.tmp / "out", expect_sha="f" * 40)

    def test_missing_runtime_file_fails(self):
        git(self.mv.dir, "rm", "-q", "style.css")
        git(self.mv.dir, "commit", "-qm", "drop")
        with self.assertRaisesRegex(SystemExit, "missing"):
            S.build_expected(self.mv.dir, git(self.mv.dir, "rev-parse", "HEAD"))

    def test_symlink_runtime_file_rejected(self):
        (self.mv.dir / "api.js").unlink()
        (self.mv.dir / "api.js").symlink_to("CLAUDE.md")
        git(self.mv.dir, "add", "-A")
        git(self.mv.dir, "commit", "-qm", "link")
        with self.assertRaisesRegex(SystemExit, "not a regular blob"):
            S.build_expected(self.mv.dir, git(self.mv.dir, "rev-parse", "HEAD"))

    def test_nonempty_output_refused(self):
        out = self.tmp / "out"
        out.mkdir()
        (out / "stale.js").write_text("x")
        with self.assertRaisesRegex(SystemExit, "absent or empty"):
            self.stage(out)

    def test_manifest_must_be_exact_allowlist(self):
        m = json.loads(self.pin.read_text())
        m["files"]["README.md"] = {"sha256": "a" * 64, "bytes": 1}
        self.pin.write_text(json.dumps(m))
        with self.assertRaisesRegex(SystemExit, "runtime allowlist"):
            self.stage(self.tmp / "out")

    def test_short_sha_rejected(self):
        with self.assertRaisesRegex(SystemExit, "40-hex"):
            S.build_expected(self.mv.dir, self.mv.sha[:12])


class CommittedPinTests(unittest.TestCase):
    """The committed pin must name the REVIEWED MarketVivi commit.

    Deliberately hardcoded rather than derived from the manifest: this is a tripwire, so that the
    bundled runtime cannot be re-pointed without someone editing this expectation and having that
    edit reviewed. Reading the value out of the manifest would make the assertion vacuous.

    Updated for the 2.6.95 paired candidate. It had been left at 877b02fd while the committed
    manifest said 7005a751, so this test was ALREADY FAILING at the accepted implementation head
    b9aa75de — it runs only in the manually dispatched candidate workflow, never in the
    review-validation run, which is why a green review never caught it.
    """

    ACCEPTED_MARKETVIVI_SHA = "524caa77f9548e9911bc41d0dd3a06a03cfc74af"
    ACCEPTED_APP_JS_SHA256 = \
        "f93a0a1854f76b82563cd0b449c66ed0d8b9ac506d91f75ab3beeb430c20a50d"

    def test_committed_pin_names_the_accepted_marketvivi_commit(self):
        m = json.loads(PIN.read_text())
        self.assertEqual(m["marketvivi_sha"], self.ACCEPTED_MARKETVIVI_SHA)
        self.assertEqual(sorted(m["files"]), sorted(S.RUNTIME_FILES))
        self.assertEqual(m["files"]["app.js"]["sha256"], self.ACCEPTED_APP_JS_SHA256)
        self.assertEqual(m["document_base"], S.DOCUMENT_BASE)

    def test_the_pin_identity_is_the_paired_native_release(self):
        """The bundled runtime must be the PAIRED interface, not an older one.

        The packaging issue this candidate exists to fix: the pin named 2.6.92/523 while the
        native source is 2.6.95/526, so a candidate built unchanged would have bundled the older
        interface against the newer native code.
        """
        m = json.loads(PIN.read_text())
        gradle = (ROOT / "app/build.gradle.kts").read_text()
        name = re.search(r'versionName\s*=\s*"([^"]+)"', gradle)
        code = re.search(r"versionCode\s*=\s*(\d+)", gradle)
        self.assertIsNotNone(name)
        self.assertIsNotNone(code)
        self.assertEqual(m["pwa_identity"]["version"], name.group(1))
        self.assertEqual(m["pwa_identity"]["build"], int(code.group(1)))
        self.assertEqual(m["pwa_identity"]["app_js_v"], m["pwa_identity"]["index_app_js_v"])

    def test_every_hardcoded_copy_of_the_pin_agrees(self):
        """The guard that was missing, and the reason this drifted at all.

        Two tripwires are kept on purpose - this one, and the Kotlin literals in
        B515CandidatePwaTest, which run in every unit-test variant on every push. Hardcoding is
        what makes each a tripwire, but nothing previously asserted that the copies AGREED, so
        they diverged to four values across seven places and only the push-checked ones were ever
        noticed. Duplication is acceptable here only because this test binds the duplicates
        together: edit one and this fails until the other matches.
        """
        kt = (ROOT / "app/src/test/java/com/marketradar/app/B515CandidatePwaTest.kt").read_text()

        def kotlin_literal(name, pattern=r'"([^"]+)"'):
            m = re.search(r"val\s+%s\s*=\s*%s" % (re.escape(name), pattern), kt)
            self.assertIsNotNone(m, "%s not found in B515CandidatePwaTest.kt" % name)
            return m.group(1)

        self.assertEqual(kotlin_literal("acceptedMarketViviSha"), self.ACCEPTED_MARKETVIVI_SHA)
        self.assertEqual(kotlin_literal("acceptedAppJsSha256"), self.ACCEPTED_APP_JS_SHA256)

        # And no stale copy of a superseded identity may survive anywhere in the tracked tree.
        superseded = ("877b02fd6d703ca43148a0f119b60216d9b509ce",
                      "7005a751b166666c1f4e22f5d3e4607390b904e4",
                      "3f3f46ad01cee8dd5fcdb04817472d15968b53bc")
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                                 capture_output=True, text=True, check=True).stdout.split("\0")
        offenders = {}
        for rel in filter(None, tracked):
            p = ROOT / rel
            if not p.is_file() or p.suffix.lower() in (".png", ".jpg", ".webp", ".jar", ".keystore"):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            # Prose that documents the drift is allowed to name the old values; code and data are not.
            if p.suffix in (".md", ".txt"):
                continue
            hits = [s for s in superseded if s in text]
            # Only this file is exempt, because it must contain the values it searches for. Keeping the exemption
            # list to the guard itself is deliberate: a broad exemption would hide exactly what this looks for.
            if hits and rel != "tools/candidate_pwa/test_stage_candidate_pwa.py":
                offenders[rel] = hits
        self.assertEqual(offenders, {}, "superseded pin SHAs still present: %s" % offenders)


class WorkflowContractTests(unittest.TestCase):
    """The artifact-only workflow: manual trigger only, read-only token, no release/tag/Pages/push, pinned actions."""

    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text()
        try:
            import yaml  # type: ignore
            cls.y = yaml.safe_load(cls.text)
        except ImportError:  # pragma: no cover
            cls.y = None

    def test_triggers_are_manual_dispatch_or_isolated_authorisation_push_only(self):
        if self.y is not None:
            on = self.y.get("on", self.y.get(True))
            self.assertEqual(sorted(on.keys()), ["push", "workflow_dispatch"])
            self.assertEqual(on["push"], {"branches": ["candidate-build/**"], "paths": ["candidate/BUILD_AUTHORIZATION.json"]})
            ins = on["workflow_dispatch"]["inputs"]
            for k in ("native_sha", "pwa_sha", "expected_cert_sha256", "expected_version_code", "expected_version_name",
                      "confirm_candidate_artifact_only"):
                self.assertIn(k, ins)
            job = self.y["jobs"]["candidate"]
            self.assertIn("startsWith(github.ref, 'refs/heads/candidate-build/')", job["if"])
            self.assertIn("inputs.confirm_candidate_artifact_only == true", job["if"])
        for bad in ("\n  pull_request:", "\n  schedule:", "workflow_call", "repository_dispatch", "pull_request_target",
                    "tags:", "'review/", "branches: [main", "- main"):
            self.assertNotIn(bad, self.text)

    def test_isolated_route_builds_the_parent_of_a_single_file_authorisation_commit(self):
        for must in ('test "$parents" = 2', 'test "$changed" = "candidate/BUILD_AUTHORIZATION.json"',
                     'a["native_sha"] == sys.argv[1]', 'a["artifact_only"] is True', "codex_final_check_reference",
                     "approval_reference", "approved_by", "ref: ${{ env.NATIVE_SHA }}",
                     "test ! -e candidate/BUILD_AUTHORIZATION.json",
                     'git merge-base --is-ancestor "$ACCEPTED_R9_NATIVE" "$NATIVE_SHA"',
                     'git merge-base --is-ancestor "$ACCEPTED_ROUTE_NATIVE" "$NATIVE_SHA"'):
            self.assertIn(must, self.text)
        # Build/stage steps never take raw dispatch inputs directly; only the resolver maps them.
        after = self.text.split("- name: Validate inputs", 1)[1]
        self.assertNotIn("inputs.", after)

    def test_authorisation_template_is_not_live_and_not_on_trigger_path(self):
        root = WORKFLOW.parents[2]
        self.assertFalse((root / "candidate" / "BUILD_AUTHORIZATION.json").exists())
        tpl = json.loads((root / "tools/candidate_pwa/BUILD_AUTHORIZATION.template.json").read_text())
        self.assertEqual(tpl["schema"], "marketradar.candidate_build_authorization.v1")
        self.assertIs(tpl["artifact_only"], True)
        # Every value the filler must supply stays blank. pwa_sha/version_code/version_name were
        # prefilled with 877b02fd/517/2.6.86 under a note saying to fill only the EMPTY fields,
        # which read as an instruction to keep three stale values.
        for k in ("native_sha", "pwa_sha", "version_code", "version_name", "cert_sha256",
                  "approved_by", "approval_reference", "codex_final_check_reference"):
            self.assertEqual(tpl[k], "", "template field %s must ship blank" % k)

    def test_permissions_read_only(self):
        if self.y is not None:
            self.assertEqual(self.y["permissions"], {"contents": "read"})
            for job in self.y["jobs"].values():
                self.assertNotIn("permissions", job)
        self.assertNotIn("write", re.sub(r"#.*", "", self.text).split("jobs:")[0])

    def test_no_release_tag_pages_push_or_production_calls(self):
        body = "\n".join(l for l in self.text.splitlines() if not l.strip().startswith("#"))
        for bad in ("release.yml", "action-gh-release", "gh release", "git push", "git tag", "deploy-pages", "upload-pages-artifact",
                    "configure-pages", "/releases", "/dispatches", "supabase.co", "SUPABASE_SERVICE", "contents: write",
                    "actions: write", "GITHUB_TOKEN", "gh workflow", "pages:"):
            self.assertNotIn(bad, body)
        self.assertIn("actions/upload-artifact@", body)

    def test_all_actions_pinned_to_full_commit_shas(self):
        uses = re.findall(r"uses:\s*([^\s#]+)", self.text)
        self.assertTrue(uses)
        for u in uses:
            self.assertRegex(u, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$", u)

    def test_inputs_validated_as_full_hex_and_checkouts_pinned(self):
        self.assertIn("^[0-9a-f]{40}$", self.text)
        self.assertIn("persist-credentials: false", self.text)
        self.assertIn('test "$(git rev-parse HEAD)" = "$NATIVE_SHA"', self.text)
        self.assertIn('test "$(git -C _marketvivi rev-parse HEAD)" = "$PWA_SHA"', self.text)
        self.assertIn("merge-base --is-ancestor 3768131f6329f2aeb2580f93aabcde6bfd64b140", self.text)

    def test_secrets_never_echoed(self):
        for line in self.text.splitlines():
            if "secrets." in line:
                self.assertNotRegex(line, r"\b(echo|printf|cat|tee)\b.*secrets\.", line)
                self.assertRegex(line.strip(), r"^[A-Z0-9_]+:\s*\$\{\{\s*secrets\.[A-Z0-9_]+\s*\}\}$", line)

    def test_builds_only_the_candidate_and_verifies_identity(self):
        self.assertIn(":app:assembleCandidate", self.text)
        self.assertNotIn("assembleRelease", self.text)
        self.assertNotIn("bundleRelease", self.text)
        self.assertIn("apksigner", self.text)
        self.assertIn("dump badging", self.text)
        self.assertIn("package: name='com.marketradar.app'", self.text)


if __name__ == "__main__":
    unittest.main()
