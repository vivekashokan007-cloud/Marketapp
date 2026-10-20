"""S1a (b511) delete-authority containment: catalog model, fingerprints and SQL generators.

Input: the read-only production catalog captured on 18 Oct 2026 (docs/security/s1a_20261018/catalog). Output:
  * the forward migration  supabase/migrations/20261018090000_s1a_client_delete_containment.sql
  * the emergency rollback docs/security/s1a_20261018/rollback_s1a_client_delete_containment.sql
  * a scratch baseline that reproduces the captured policies/grants/RLS/default ACL (tests only)
The fingerprint definitions mirror tools/s1a_db_suite/fingerprint.sql exactly; reconstructing the production
fingerprint from the captured JSON proves the capture is complete for everything the migration touches.

    python3 tools/s1a_db_suite/s1a_catalog.py --write   # regenerate committed SQL
    python3 tools/s1a_db_suite/s1a_catalog.py --check   # fail if committed SQL differs
"""
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CAT = os.path.join(ROOT, 'docs', 'security', 's1a_20261018', 'catalog')
MIGRATION = os.path.join(ROOT, 'supabase', 'migrations', '20261018090000_s1a_client_delete_containment.sql')
ROLLBACK = os.path.join(ROOT, 'docs', 'security', 's1a_20261018', 'rollback_s1a_client_delete_containment.sql')
CLIENT_ROLES = ('anon', 'authenticated')
ACL_LETTERS = {'a': 'INSERT', 'r': 'SELECT', 'w': 'UPDATE', 'd': 'DELETE', 'D': 'TRUNCATE', 'x': 'REFERENCES',
               't': 'TRIGGER', 'm': 'MAINTAIN'}
SPLIT = (('SELECT', 'select'), ('INSERT', 'insert'), ('UPDATE', 'update'))


def _load(name):
    with open(os.path.join(CAT, name)) as f:
        return json.load(f)


def catalog():
    return {
        'policies': _load('prod_policies_20261018.json'),
        'relations': _load('prod_table_privileges_20261018.json'),
        'views': _load('prod_views_20261018.json'),
        'roles': _load('prod_roles_defacl_views_triggers_20261018.json'),
        'fingerprint': _load('prod_fingerprint_baseline_20261018.json'),
    }


def md5(text):
    return hashlib.md5(text.encode()).hexdigest()


def _b(v):
    return 't' if v else 'f'  # concat_ws uses boolean's output function


def parse_acl(acl):
    """'{postgres=arwdDxtm/postgres,anon=arwxtm/postgres}' -> {'anon': 'arwxtm', ...}"""
    out = {}
    if not acl:
        return out
    for item in acl.strip('{}').split(','):
        grantee, rest = item.split('=', 1)
        out[grantee or 'PUBLIC'] = rest.split('/', 1)[0]
    return out


def postgres_default_table_acl(cat):
    for d in cat['roles']['default_acl']:
        if d['owner'] == 'postgres' and d['schema'] == 'public' and d['objtype'] == 'r':
            return d['acl']
    raise AssertionError('postgres default table ACL missing from capture')


def fp_policies(policies):
    rows = sorted(policies, key=lambda p: (p['t'].encode(), p['p'].encode()))
    return md5('\n'.join('|'.join([p['t'], p['p'], p['perm'], '{%s}' % ','.join(p['roles']), p['cmd'],
                                   p['qual'] if p['qual'] is not None else '-',
                                   p['chk'] if p['chk'] is not None else '-']) for p in rows))


def fp_relations(relations):
    rows = sorted(relations, key=lambda r: r['t'].encode())
    return md5('\n'.join('|'.join([r['t'], r['kind'], _b(r['rls']), _b(r['force']),
                                   r['priv'].get('anon') or '-', r['priv'].get('authenticated') or '-'])
                         for r in rows))


def fp_default_acl(acl_text):
    acl = parse_acl(acl_text)
    items = sorted((g, ACL_LETTERS[ch]) for g in CLIENT_ROLES for ch in acl.get(g, ''))
    return md5(','.join('%s:%s' % i for i in items))


def all_policies(cat):
    return [p for p in cat['policies'] if p['cmd'] == 'ALL']


def delete_holders(cat):
    """Relations where a client role currently holds DELETE or TRUNCATE (effective, incl. views)."""
    out = {r: [] for r in CLIENT_ROLES}
    for rel in cat['relations']:
        for role in CLIENT_ROLES:
            priv = rel['priv'].get(role) or ''
            for ch, word in (('D', 'DELETE'), ('T', 'TRUNCATE')):
                if ch in priv:
                    out[role].append((rel['t'], word))
    return out


def split_name(policy_name, suffix):
    name = '%s (%s)' % (policy_name, suffix)
    assert len(name.encode()) <= 63, name
    return name


def post_state(cat):
    """Catalog state after the migration (policies split, client DELETE/TRUNCATE revoked)."""
    policies = []
    for p in cat['policies']:
        if p['cmd'] != 'ALL':
            policies.append(p)
            continue
        for cmd, suffix in SPLIT:
            policies.append({'t': p['t'], 'p': split_name(p['p'], suffix), 'perm': p['perm'], 'roles': p['roles'],
                             'cmd': cmd, 'qual': p['qual'] if cmd in ('SELECT', 'UPDATE') else None,
                             'chk': p['chk'] if cmd in ('INSERT', 'UPDATE') else None})
    relations = []
    for r in cat['relations']:
        priv = dict(r['priv'])
        for role in CLIENT_ROLES:
            if priv.get(role):
                priv[role] = priv[role].replace('D', '').replace('T', '') or None
        relations.append(dict(r, priv=priv))
    acl = parse_acl(postgres_default_table_acl(cat))
    for role in CLIENT_ROLES:
        acl[role] = acl.get(role, '').replace('d', '').replace('D', '')
    acl_text = '{' + ','.join('%s=%s/postgres' % (g, v) for g, v in acl.items()) + '}'
    return policies, relations, acl_text


def fingerprints(cat):
    base = (fp_policies(cat['policies']), fp_relations(cat['relations']), fp_default_acl(postgres_default_table_acl(cat)))
    pol, rel, acl = post_state(cat)
    post = (fp_policies(pol), fp_relations(rel), fp_default_acl(acl))
    return base, post


def q_ident(name):
    return '"%s"' % name.replace('"', '""')


def q_lit(text):
    return "'%s'" % text.replace("'", "''")


def _roles_sql(roles):
    return ', '.join(r if r == 'public' else q_ident(r) for r in roles)


def create_policy_sql(p):
    sql = 'create policy %s on public.%s as %s for %s to %s' % (
        q_ident(p['p']), q_ident(p['t']), p['perm'].lower(), p['cmd'].lower(), _roles_sql(p['roles']))
    if p['qual'] is not None:
        sql += ' using (%s)' % p['qual']
    if p['chk'] is not None:
        sql += ' with check (%s)' % p['chk']
    return sql


FP_SQL = """
  select
   md5(coalesce((select string_agg(concat_ws('|', tablename, policyname, permissive, roles::text, cmd,
        coalesce(qual, '-'), coalesce(with_check, '-')), E'\\n' order by tablename, policyname)
      from pg_catalog.pg_policies where schemaname = 'public'), '')),
   md5(coalesce((select string_agg(concat_ws('|', c.relname, c.relkind, c.relrowsecurity, c.relforcerowsecurity,
        coalesce((select string_agg(v.p, '' order by v.o) from (values (1,'S','SELECT'),(2,'I','INSERT'),(3,'U','UPDATE'),(4,'D','DELETE'),(5,'T','TRUNCATE')) v(o,p,priv)
                  where pg_catalog.has_table_privilege('anon', c.oid, v.priv)), '-'),
        coalesce((select string_agg(v.p, '' order by v.o) from (values (1,'S','SELECT'),(2,'I','INSERT'),(3,'U','UPDATE'),(4,'D','DELETE'),(5,'T','TRUNCATE')) v(o,p,priv)
                  where pg_catalog.has_table_privilege('authenticated', c.oid, v.priv)), '-')), E'\\n' order by c.relname)
      from pg_catalog.pg_class c join pg_catalog.pg_namespace n on n.oid = c.relnamespace
      where n.nspname = 'public' and c.relkind in ('r','p','v','m')), '')),
   md5(coalesce((select string_agg(a.grantee::regrole::text || ':' || a.privilege_type, ',' order by a.grantee::regrole::text, a.privilege_type)
      from pg_catalog.pg_default_acl d, pg_catalog.aclexplode(d.defaclacl) a
      where d.defaclnamespace = 'public'::regnamespace and d.defaclrole = 'postgres'::regrole and d.defaclobjtype = 'r'
        and a.grantee in ('anon'::regrole, 'authenticated'::regrole)), ''))"""


def _guarded(kind, cat, steps, from_fp, to_fp, header):
    from_label, to_label = ('baseline', 'contained') if kind == 'forward' else ('contained', 'baseline')
    body = '\n'.join('    execute %s;' % q_lit(s) for s in steps)
    return """%s
begin;

do $s1a$
declare
  fp text[];
begin
  if current_user <> 'postgres' then
    raise exception 'S1A_WRONG_ROLE: run as postgres (owner of the public tables and of the default ACL), not %%', current_user;
  end if;
  select array[p, r, d] into fp from (%s
  ) x(p, r, d);
  if fp = array[%s, %s, %s] then
    raise notice 'S1A_%s: catalog already %s; nothing to do';
    return;
  end if;
  if fp is distinct from array[%s, %s, %s] then
    raise exception 'S1A_CATALOG_DRIFT: expected the captured %s state; found policies=%% relations=%% default_acl=%%',
      fp[1], fp[2], fp[3];
  end if;
%s
  select array[p, r, d] into fp from (%s
  ) x(p, r, d);
  if fp is distinct from array[%s, %s, %s] then
    raise exception 'S1A_POSTCONDITION_FAILED: policies=%% relations=%% default_acl=%%', fp[1], fp[2], fp[3];
  end if;
  raise notice 'S1A_%s: applied (%s -> %s)';
end
$s1a$;

commit;
""" % (header, FP_SQL, q_lit(to_fp[0]), q_lit(to_fp[1]), q_lit(to_fp[2]), kind.upper(), to_label,
       q_lit(from_fp[0]), q_lit(from_fp[1]), q_lit(from_fp[2]), from_label, body, FP_SQL,
       q_lit(to_fp[0]), q_lit(to_fp[1]), q_lit(to_fp[2]), kind.upper(), from_label, to_label)


def forward_steps(cat):
    steps = []
    for p in all_policies(cat):
        steps.append('drop policy %s on public.%s' % (q_ident(p['p']), q_ident(p['t'])))
        for cmd, suffix in SPLIT:
            steps.append(create_policy_sql({'t': p['t'], 'p': split_name(p['p'], suffix), 'perm': p['perm'],
                                            'roles': p['roles'], 'cmd': cmd,
                                            'qual': p['qual'] if cmd in ('SELECT', 'UPDATE') else None,
                                            'chk': p['chk'] if cmd in ('INSERT', 'UPDATE') else None}))
    holders = delete_holders(cat)
    for role in CLIENT_ROLES:
        for word in ('DELETE', 'TRUNCATE'):
            rels = sorted(t for t, w in holders[role] if w == word)
            if rels:
                steps.append('revoke %s on table %s from %s' % (
                    word.lower(), ', '.join('public.' + q_ident(t) for t in rels), role))
    steps.append('alter default privileges for role postgres in schema public revoke delete, truncate on tables '
                 'from anon, authenticated')
    return steps


def rollback_steps(cat):
    steps = []
    for p in all_policies(cat):
        for _, suffix in SPLIT:
            steps.append('drop policy %s on public.%s' % (q_ident(split_name(p['p'], suffix)), q_ident(p['t'])))
        steps.append(create_policy_sql(p))
    holders = delete_holders(cat)
    for role in CLIENT_ROLES:
        for word in ('DELETE', 'TRUNCATE'):
            rels = sorted(t for t, w in holders[role] if w == word)
            if rels:
                steps.append('grant %s on table %s to %s' % (
                    word.lower(), ', '.join('public.' + q_ident(t) for t in rels), role))
    acl = parse_acl(postgres_default_table_acl(cat))
    for role in CLIENT_ROLES:
        words = [ACL_LETTERS[ch].lower() for ch in 'dD' if ch in acl.get(role, '')]
        if words:
            steps.append('alter default privileges for role postgres in schema public grant %s on tables to %s'
                         % (', '.join(words), role))
    return steps


FORWARD_HEADER = """-- b511 S1a: contain client DELETE authority on public tables (forward migration).
-- GENERATED by tools/s1a_db_suite/s1a_catalog.py from the read-only production catalog captured 18 Oct 2026
-- (docs/security/s1a_20261018/catalog). Do not edit by hand; regenerate and review instead.
--
-- What it does (and nothing else):
--   1. splits each of the 17 ALL policies on 16 tables into SELECT / INSERT / UPDATE policies with the same role
--      set, PERMISSIVE character, USING and WITH CHECK expressions (no DELETE policy is recreated);
--   2. revokes the DELETE table privilege from authenticated on every public relation that has it, including the
--      auto-updatable owner-rights view evaluator_proposals_app_view (anon already lacks DELETE/TRUNCATE);
--   3. removes DELETE/TRUNCATE for anon/authenticated from postgres' default privileges for future public tables.
-- Unchanged: SELECT/INSERT/UPDATE grants and policies, RLS flags, service_role, functions/RPC EXECUTE, storage,
-- supabase_admin default privileges (not alterable by postgres), every other schema.
-- Preconditions: runs as postgres; the catalog fingerprint must equal the captured baseline (then it applies) or
-- the contained state (then it is a no-op). Any other state aborts with S1A_CATALOG_DRIFT and changes nothing.
-- Postcondition: the fingerprint must equal the expected contained state or the transaction aborts.
-- PRODUCTION APPLICATION IS PENDING REVIEW. Emergency rollback: docs/security/s1a_20261018/rollback_s1a_client_delete_containment.sql"""

ROLLBACK_HEADER = """-- b511 S1a EMERGENCY ROLLBACK: restore the exact pre-containment policies and DELETE grants captured 18 Oct 2026.
-- GENERATED by tools/s1a_db_suite/s1a_catalog.py. Not a migration: run manually only after review, as postgres.
-- Precondition: the catalog must equal the contained state (applies) or the captured baseline (no-op);
-- anything else aborts with S1A_CATALOG_DRIFT. Postcondition: fingerprint equals the captured baseline."""


def migration_sql(cat):
    base, post = fingerprints(cat)
    return _guarded('forward', cat, forward_steps(cat), base, post, FORWARD_HEADER)


def rollback_sql(cat):
    base, post = fingerprints(cat)
    return _guarded('rollback', cat, rollback_steps(cat), post, base, ROLLBACK_HEADER)


def main(argv):
    cat = catalog()
    base, post = fingerprints(cat)
    prod = cat['fingerprint']
    captured = (prod['policies_md5'], prod['relations_md5'], prod['default_acl_md5'])
    print('baseline fingerprint (from captured JSON):', base)
    print('production fingerprint (read-only query) :', captured)
    print('contained fingerprint                     :', post)
    if base != captured:
        print('CAPTURE_INCOMPLETE: reconstructed fingerprint differs from production', file=sys.stderr)
        return 1
    outputs = {MIGRATION: migration_sql(cat), ROLLBACK: rollback_sql(cat)}
    if '--write' in argv:
        for path, text in outputs.items():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                f.write(text)
        print('written')
    if '--check' in argv:
        for path, text in outputs.items():
            if not os.path.exists(path) or open(path).read() != text:
                print('STALE:', os.path.relpath(path, ROOT), file=sys.stderr)
                return 1
        print('check ok')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
