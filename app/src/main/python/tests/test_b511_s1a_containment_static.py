"""b511 S1a: static guarantees of the generated client-DELETE containment migration (no database needed).

The scratch PostgreSQL/REST behaviour is proven by tools/s1a_db_suite/suite.py; these tests pin the generated SQL
and the capture so a stale or hand-edited migration fails the ordinary Python suite."""
import os
import re
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'tools', 's1a_db_suite'))
import s1a_catalog as C  # noqa: E402


class S1aContainmentStatic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = C.catalog()
        cls.mig = open(C.MIGRATION).read()
        cls.rb = open(C.ROLLBACK).read()

    def test_capture_reconstructs_production_fingerprint(self):
        base, _ = C.fingerprints(self.cat)
        fp = self.cat['fingerprint']
        self.assertEqual(base, (fp['policies_md5'], fp['relations_md5'], fp['default_acl_md5']))

    def test_committed_sql_is_generated(self):
        self.assertEqual(self.mig, C.migration_sql(self.cat))
        self.assertEqual(self.rb, C.rollback_sql(self.cat))

    def test_counts_match_codex_inventory(self):
        alls = C.all_policies(self.cat)
        self.assertEqual(len(alls), 17)
        self.assertEqual(len({p['t'] for p in alls}), 16)
        holders = C.delete_holders(self.cat)
        self.assertEqual(holders['anon'], [])  # anon already lacks DELETE/TRUNCATE everywhere
        self.assertEqual(len([t for t, w in holders['authenticated'] if w == 'DELETE']), 44)
        self.assertFalse([t for t, w in holders['authenticated'] if w == 'TRUNCATE'])

    def test_split_preserves_roles_predicates_and_never_creates_delete(self):
        pol, _, _ = C.post_state(self.cat)
        self.assertFalse([p for p in pol if p['cmd'] in ('ALL', 'DELETE')])
        for orig in C.all_policies(self.cat):
            parts = [p for p in pol if p['t'] == orig['t'] and p['p'].startswith(orig['p'] + ' (')]
            self.assertEqual(sorted(p['cmd'] for p in parts), ['INSERT', 'SELECT', 'UPDATE'])
            for p in parts:
                self.assertEqual(p['roles'], orig['roles'])
                self.assertEqual(p['perm'], orig['perm'])
                if p['cmd'] in ('SELECT', 'UPDATE'):
                    self.assertEqual(p['qual'], orig['qual'])
                if p['cmd'] in ('INSERT', 'UPDATE'):
                    self.assertEqual(p['chk'], orig['chk'])

    def test_forward_touches_only_delete_authority(self):
        stmts = re.findall(r"execute '(.*)';", self.mig)
        for s in stmts:
            low = s.lower()
            self.assertTrue(low.startswith(('drop policy', 'create policy', 'revoke delete', 'alter default privileges')), s)
            self.assertNotIn('service_role', low)
            self.assertNotIn('storage.', low)
            self.assertNotRegex(low, r'revoke (select|insert|update)')
            if low.startswith('create policy'):
                self.assertRegex(low, r' for (select|insert|update) ')
        self.assertIn('S1A_CATALOG_DRIFT', self.mig)
        self.assertIn('S1A_POSTCONDITION_FAILED', self.mig)
        self.assertIn("current_user <> 'postgres'", self.mig)

    def test_rollback_restores_exact_originals(self):
        for p in C.all_policies(self.cat):
            self.assertIn(C.create_policy_sql(p), self.rb)
        self.assertIn('grant delete on table', self.rb)
        self.assertIn('grant delete on tables to authenticated', self.rb)


if __name__ == '__main__':
    unittest.main()
