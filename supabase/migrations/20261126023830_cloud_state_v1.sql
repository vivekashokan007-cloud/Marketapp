-- Account-private, immutable recovery objects and compare-and-swap checkpoint head.
-- Does not alter legacy trading, research tables, or the existing EXPORTS bucket.
create schema if not exists mr_cloud_private;
revoke all on schema mr_cloud_private from public, anon;
grant usage on schema mr_cloud_private to authenticated;
create table if not exists public.mr_cloud_heads (
 owner_id uuid primary key references auth.users(id),
 revision bigint not null default 0 check (revision >= 0),
 manifest_sha256 text check (manifest_sha256 ~ '^[a-f0-9]{64}$'),
 device_id uuid not null,
 lease_until timestamptz not null,
 committed_at timestamptz
);
alter table public.mr_cloud_heads enable row level security;
revoke all on public.mr_cloud_heads from public, anon, authenticated;
grant select on public.mr_cloud_heads to authenticated;
create policy mr_cloud_owner_read on public.mr_cloud_heads for select to authenticated
 using (owner_id = (select auth.uid()));
create table public.mr_cloud_checkpoints (
 owner_id uuid not null references auth.users(id), revision bigint not null,
 manifest_sha256 text not null check(manifest_sha256 ~ '^[a-f0-9]{64}$'),
 committed_at timestamptz not null, primary key(owner_id,revision)
);
alter table public.mr_cloud_checkpoints enable row level security;
revoke all on public.mr_cloud_checkpoints from public,anon,authenticated;
grant select on public.mr_cloud_checkpoints to authenticated;
create policy mr_cloud_checkpoint_owner on public.mr_cloud_checkpoints for select to authenticated
 using(owner_id=(select auth.uid()));

-- Only this small, explicitly authenticated function may update the head.
-- It lives outside exposed schemas; public wrappers run as the invoking user.
create or replace function mr_cloud_private.change_head(p_device uuid, p_expected bigint, p_manifest text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare u uuid := auth.uid(); h public.mr_cloud_heads; begin
 if u is null then raise exception 'AUTH_REQUIRED'; end if;
 if p_device is null or p_expected < 0 then raise exception 'INVALID_REQUEST'; end if;
 if p_manifest is not null and p_manifest !~ '^[a-f0-9]{64}$' then raise exception 'BAD_MANIFEST'; end if;
 insert into public.mr_cloud_heads(owner_id,device_id,lease_until)
 values (u,p_device,clock_timestamp() + interval '120 seconds') on conflict do nothing;
 select * into h from public.mr_cloud_heads where owner_id=u for update;
 if h.revision <> p_expected then raise exception 'CLOUD_REVISION_CONFLICT'; end if;
 if h.device_id <> p_device and h.lease_until > clock_timestamp() then raise exception 'OTHER_DEVICE_ACTIVE'; end if;
 update public.mr_cloud_heads set device_id=p_device, lease_until=clock_timestamp()+interval '120 seconds',
 revision=revision + case when p_manifest is null then 0 else 1 end,
 manifest_sha256=coalesce(p_manifest,manifest_sha256),
 committed_at=case when p_manifest is null then committed_at else clock_timestamp() end
 where owner_id=u returning * into h;
 if p_manifest is not null then
  if not exists(select 1 from storage.objects where bucket_id='mr-private-state' and name=u::text||'/'||p_manifest)
   then raise exception 'MANIFEST_OBJECT_MISSING'; end if;
  insert into public.mr_cloud_checkpoints(owner_id,revision,manifest_sha256,committed_at)
  values(u,h.revision,p_manifest,h.committed_at);
 end if;
 return to_jsonb(h) || jsonb_build_object('server_now',clock_timestamp());
end $$;
revoke all on function mr_cloud_private.change_head(uuid,bigint,text) from public, anon;
grant execute on function mr_cloud_private.change_head(uuid,bigint,text) to authenticated;
create or replace function public.mr_cloud_claim(p_device uuid,p_expected bigint)
returns jsonb language sql security invoker set search_path='' as $$
 select mr_cloud_private.change_head(p_device,p_expected,null);
$$;
create or replace function public.mr_cloud_commit(p_device uuid,p_expected bigint,p_manifest text)
returns jsonb language sql security invoker set search_path='' as $$
 select mr_cloud_private.change_head(p_device,p_expected,p_manifest);
$$;
revoke all on function public.mr_cloud_claim(uuid,bigint) from public, anon;
revoke all on function public.mr_cloud_commit(uuid,bigint,text) from public, anon;
grant execute on function public.mr_cloud_claim(uuid,bigint) to authenticated;
grant execute on function public.mr_cloud_commit(uuid,bigint,text) to authenticated;

insert into storage.buckets(id,name,public,file_size_limit,allowed_mime_types)
values ('mr-private-state','mr-private-state',false,9437184,array['application/octet-stream'])
on conflict(id) do nothing;
-- Existing production storage has a broad permissive anon ALL policy.
-- A restrictive policy is essential: a new permissive policy cannot narrow it.
create policy mr_cloud_no_anon on storage.objects as restrictive for all to anon
 using (bucket_id <> 'mr-private-state') with check (bucket_id <> 'mr-private-state');
create policy mr_cloud_owner_guard on storage.objects as restrictive for all to authenticated
 using (bucket_id <> 'mr-private-state' or (storage.foldername(name))[1]=(select auth.uid())::text)
 with check (bucket_id <> 'mr-private-state' or (storage.foldername(name))[1]=(select auth.uid())::text);
create policy mr_cloud_read on storage.objects for select to authenticated
 using (bucket_id='mr-private-state' and (storage.foldername(name))[1]=(select auth.uid())::text);
create policy mr_cloud_insert on storage.objects for insert to authenticated
 with check (bucket_id='mr-private-state' and (storage.foldername(name))[1]=(select auth.uid())::text
 and name ~ '^[0-9a-f-]{36}/[a-f0-9]{64}$');
-- Objects are immutable, including against unrelated broad UPDATE/DELETE policies.
create policy mr_cloud_immutable_update on storage.objects as restrictive for update to authenticated
 using (bucket_id <> 'mr-private-state') with check (bucket_id <> 'mr-private-state');
create policy mr_cloud_immutable_delete on storage.objects as restrictive for delete to authenticated
 using (bucket_id <> 'mr-private-state');
