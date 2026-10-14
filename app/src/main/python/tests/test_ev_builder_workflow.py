"""Workflow-file guard for .github/workflows/ev-calibration-builder.yml: the build job (which receives the
service-role secrets) runs only for refs/heads/main with EV_BUILDER_ENABLED, manual dispatch only, no cron."""
import os
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "..")


class BuilderWorkflow(unittest.TestCase):
    def setUp(self):
        self.w = open(os.path.join(ROOT, ".github", "workflows", "ev-calibration-builder.yml")).read()
        self.code = "\n".join(l for l in self.w.splitlines() if not l.lstrip().startswith("#"))

    def test_build_job_requires_main_ref_and_enable_flag(self):
        build = self.code.split("\n  build:")[1]
        self.assertEqual(build.split("\n")[1].strip(),
                         "if: ${{ vars.EV_BUILDER_ENABLED == 'true' && github.ref == 'refs/heads/main' }}")
        self.assertEqual(self.code.count("\n  build:"), 1)
        self.assertEqual(self.code.count("refs/heads/main"), 1)

    def test_dispatch_only_no_cron_dry_run_default(self):
        on = self.code.split("\non:")[1].split("\npermissions:")[0]
        self.assertIn("workflow_dispatch:", on)
        for t in ("push:", "pull_request", "schedule:", "cron:"):
            self.assertNotIn(t, on)
        self.assertIn('default: "false"', on)
        self.assertIn("contents: read", self.code)
        self.assertIn("group: ev-calibration-builder", self.code)
        self.assertIn("cancel-in-progress: false", self.code)


if __name__ == "__main__":
    unittest.main()
