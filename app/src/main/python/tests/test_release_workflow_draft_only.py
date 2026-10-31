"""PM1 release gate (Codex): release.yml creates a DRAFT release that is never marked latest, and no step in
the same run promotes it.  Static contract on the workflow text; signer, package and dispatch confirmation kept."""
import os
import re
import unittest

WF = os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', '..', '.github', 'workflows')


def body(name):
    with open(os.path.join(WF, name), encoding='utf-8') as f:
        return '\n'.join(l for l in f.read().splitlines() if not l.strip().startswith('#'))


class ReleaseWorkflowDraftOnly(unittest.TestCase):
    def setUp(self):
        self.t = body('release.yml')

    def test_single_gh_release_step_is_draft_not_latest(self):
        self.assertEqual(self.t.count('softprops/action-gh-release@'), 1)
        step = self.t[self.t.index('softprops/action-gh-release@'):]
        self.assertRegex(step, r'\n\s+draft:\s*true\b')
        self.assertRegex(step, r'\n\s+prerelease:\s*false\b')
        self.assertRegex(step, r'\n\s+make_latest:\s*"false"')
        self.assertNotRegex(self.t, r'draft:\s*false')
        self.assertNotRegex(self.t, r'make_latest:\s*"?(true|legacy)')

    def test_no_promotion_or_pages_after_upload(self):
        after = self.t[self.t.index('softprops/action-gh-release@'):]
        self.assertEqual(after.count('- name:'), 0, 'no step may follow the draft upload')
        for bad in ('gh release edit', 'gh release create', '/releases/', 'PATCH', 'make_latest: true', 'deploy-pages',
                    'upload-pages-artifact', 'configure-pages', 'pages:', 'gh workflow', '/dispatches', 'git push', 'git tag'):
            self.assertNotIn(bad, self.t, bad)

    def test_dispatch_confirmation_signing_and_tests_preserved(self):
        self.assertRegex(self.t, r'on:\s*\n\s+workflow_dispatch:')
        self.assertNotRegex(self.t, r'\n\s+(push|pull_request|schedule|release):')
        self.assertIn("if: ${{ inputs.confirm_release == true }}", self.t)
        for k in ('RELEASE_KEYSTORE_BASE64', 'RELEASE_KEYSTORE_PASSWORD', 'RELEASE_KEY_ALIAS', 'RELEASE_KEY_PASSWORD',
                  ':app:assembleRelease', ':app:testDebugUnitTest', "-p 'test_*.py'"):
            self.assertIn(k, self.t)


if __name__ == '__main__':
    unittest.main()
