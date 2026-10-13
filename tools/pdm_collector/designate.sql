-- Designate THE single Paper-menu collector (owner only; run with psql against the intended project).
--   psql "$OWNER_URL" -v hash=<64-hex sha256 of the device key> -v label='Vivek phone' -f designate.sql
-- The raw device key is never needed or accepted here: only its sha256.
-- Outcomes (one NOTICE line each):
--   ALREADY_DESIGNATED  the same hash is already the active collector -> no change (idempotent no-op)
--   DESIGNATED          inserted as the active collector
-- Refusals (exception, transaction rolled back, nothing changed):
--   HASH_MALFORMED, NOT_AN_ACTIVE_INGEST_DEVICE, ANOTHER_COLLECTOR_ACTIVE (revoke it first with revoke.sql),
--   PREVIOUSLY_REVOKED (re-activation is a separate, deliberate decision)
\set ON_ERROR_STOP 1
begin;
select pg_advisory_xact_lock(hashtextextended('pdm_collector_designation', 0));
select set_config('pdm_designate.hash', :'hash', true), set_config('pdm_designate.label', :'label', true);
do $$
declare
    h text := current_setting('pdm_designate.hash');
    l text := current_setting('pdm_designate.label');
    cur pdm_private.collectors%rowtype;
begin
    if h !~ '^[0-9a-f]{64}$' then raise exception 'HASH_MALFORMED'; end if;
    perform 1 from pc2_private.ingest_devices d where d.device_key_hash = h and d.active;
    if not found then raise exception 'NOT_AN_ACTIVE_INGEST_DEVICE'; end if;
    select * into cur from pdm_private.collectors c where c.active for update;
    if found then
        if cur.device_key_hash = h then
            raise notice 'ALREADY_DESIGNATED hash=%… label=% designated_at=%', left(h, 12), cur.label, cur.designated_at;
            return;
        end if;
        raise exception 'ANOTHER_COLLECTOR_ACTIVE hash=%…', left(cur.device_key_hash, 12);
    end if;
    perform 1 from pdm_private.collectors c where c.device_key_hash = h;
    if found then raise exception 'PREVIOUSLY_REVOKED hash=%…', left(h, 12); end if;
    insert into pdm_private.collectors (device_key_hash, label) values (h, l);
    raise notice 'DESIGNATED hash=%… label=%', left(h, 12), l;
end $$;
commit;
