-- S1a catalog fingerprint (read-only). Identical text is embedded in the migration/rollback preconditions.
select
 md5(coalesce((select string_agg(concat_ws('|', tablename, policyname, permissive, roles::text, cmd,
      coalesce(qual, '-'), coalesce(with_check, '-')), E'\n' order by tablename, policyname)
    from pg_policies where schemaname = 'public'), '')) as policies_md5,
 md5(coalesce((select string_agg(concat_ws('|', c.relname, c.relkind, c.relrowsecurity, c.relforcerowsecurity,
      coalesce((select string_agg(v.p, '' order by v.o) from (values (1,'S','SELECT'),(2,'I','INSERT'),(3,'U','UPDATE'),(4,'D','DELETE'),(5,'T','TRUNCATE')) v(o,p,priv)
                where has_table_privilege('anon', c.oid, v.priv)), '-'),
      coalesce((select string_agg(v.p, '' order by v.o) from (values (1,'S','SELECT'),(2,'I','INSERT'),(3,'U','UPDATE'),(4,'D','DELETE'),(5,'T','TRUNCATE')) v(o,p,priv)
                where has_table_privilege('authenticated', c.oid, v.priv)), '-')), E'\n' order by c.relname)
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind in ('r','p','v','m')), '')) as relations_md5,
 md5(coalesce((select string_agg(a.grantee::regrole::text || ':' || a.privilege_type, ',' order by a.grantee::regrole::text, a.privilege_type)
    from pg_default_acl d, aclexplode(d.defaclacl) a
    where d.defaclnamespace = 'public'::regnamespace and d.defaclrole = 'postgres'::regrole and d.defaclobjtype = 'r'
      and a.grantee in ('anon'::regrole, 'authenticated'::regrole)), '')) as default_acl_md5;
