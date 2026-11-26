-- Allow signed-in phone captures into the existing raw source table without
-- exposing manually seeded research rows or a service-role credential.
alter table public.mr_market_source_captures
  add column owner_id uuid default auth.uid();

create index mr_market_source_captures_owner_time
  on public.mr_market_source_captures(owner_id, response_received_at desc)
  where owner_id is not null;

alter table public.mr_market_source_captures
  add constraint mr_live_capture_compressed_limit
  check (owner_id is null or
    (original_body is null and result <> 'RECEIVED') or
    (original_body is not null and stored_bytes between 1 and 4194304));

create policy mr_market_source_owner_insert on public.mr_market_source_captures
  for insert to authenticated
  with check (owner_id = (select auth.uid()));
create policy mr_market_source_owner_verify on public.mr_market_source_captures
  for select to authenticated
  using (owner_id = (select auth.uid()));

-- The phone can insert and verify its own immutable captures, not edit or delete.
grant insert on public.mr_market_source_captures to authenticated;
grant select (capture_id, owner_id, original_sha256, stored_bytes)
  on public.mr_market_source_captures to authenticated;
