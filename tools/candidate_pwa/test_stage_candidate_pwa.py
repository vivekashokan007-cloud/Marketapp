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
        (self.dir / "index.html").write_text('<script src="app.js?v=1380"></script>\n')
        (self.dir / "app.js").write_text("const M0_PWA_IDENTITY = { version: '2.6.85', build: 516, app_js_v: 1380 };\n")
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
        self.assertEqual(m["pwa_identity"], {"version": "2.6.85", "build": 516, "app_js_v": 1380, "index_app_js_v": 1380})

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
    def test_committed_pin_names_the_accepted_marketvivi_commit(self):
        m = json.loads(PIN.read_text())
        self.assertEqual(m["marketvivi_sha"], "aabccc26b828b03935ff93f3b54ae50c97ce9e84")
        self.assertEqual(sorted(m["files"]), sorted(S.RUNTIME_FILES))
        self.assertEqual(m["files"]["app.js"]["sha256"], "ca0d7a352939e93cd67bb12dce7a37c575748523731796c6d886d2d3b0df426b")
        self.assertEqual(m["document_base"], S.DOCUMENT_BASE)


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
        self.assertEqual(tpl["approved_by"], "")

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
