-- Revoke exactly ONE collector designation (owner only). Narrow: touches one row, never deletes
-- (menu_writers references it), never touches pc2_private.ingest_devices or any evidence row.
--   psql "$OWNER_URL" -v hash=<64-hex hash> -f revoke.sql
-- After revocation, Paper-menu RPCs from that device answer PT403 and the app keeps menus queued.
\set ON_ERROR_STOP 1
begin;
select pg_advisory_xact_lock(hashtextextended('pdm_collector_designation', 0));
select set_config('pdm_designate.hash', :'hash', true);
do $$
declare h text := current_setting('pdm_designate.hash'); n integer;
begin
    if h !~ '^[0-9a-f]{64}$' then raise exception 'HASH_MALFORMED'; end if;
    update pdm_private.collectors set active = false, revoked_at = now() where device_key_hash = h and active;
    get diagnostics n = row_count;
    if n <> 1 then raise exception 'NOT_AN_ACTIVE_COLLECTOR (rows=%)', n; end if;
    raise notice 'REVOKED hash=%…', left(h, 12);
end $$;
commit;
