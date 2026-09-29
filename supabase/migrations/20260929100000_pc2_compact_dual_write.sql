-- DB-1 review migration: additive compact PC2 dual-write tables.
-- No historical rewrite or deletion is part of this migration.

create table if not exists public.ml_pc2_policy_registry (
    policy_hash text primary key check (policy_hash ~ '^[0-9a-f]{64}$'),
    policy_version text not null,
    schema_version text not null,
    digest_algorithm text not null default 'sha256' check (digest_algorithm = 'sha256'),
    canonical_bytes integer not null check (canonical_bytes >= 0),
    policy_json jsonb not null check (jsonb_typeof(policy_json) = 'object'),
    created_at timestamptz not null default now()
);

create table if not exists public.ml_pc2_decision_batches (
    batch_id text primary key check (batch_id ~ '^[0-9a-f]{64}$'),
    session_date date not null,
    poll_ts timestamptz not null,
    brain_version text not null,
    policy_hash text not null references public.ml_pc2_policy_registry(policy_hash) on delete restrict,
    policy_version text not null,
    authority_diagnostics_version text not null,
    grouping_schema_version text not null check (grouping_schema_version = 'pc2_exact_dedup_v1'),
    digest_algorithm text not null default 'sha256' check (digest_algorithm = 'sha256'),
    decision_count integer not null check (decision_count > 0),
    distinct_decision_count integer not null check (
        distinct_decision_count > 0 and distinct_decision_count <= decision_count
    ),
    decision_digest text not null check (decision_digest ~ '^[0-9a-f]{64}$'),
    grouped_decisions_json jsonb not null check (jsonb_typeof(grouped_decisions_json) = 'object'),
    complete boolean not null check (complete),
    created_at timestamptz not null default now(),
    unique (poll_ts, policy_hash, authority_diagnostics_version)
);

create index if not exists ml_pc2_decision_batches_session_poll_idx
    on public.ml_pc2_decision_batches (session_date, poll_ts);

alter table public.ml_pc2_policy_registry enable row level security;
alter table public.ml_pc2_decision_batches enable row level security;

revoke all on table public.ml_pc2_policy_registry from anon, authenticated;
revoke all on table public.ml_pc2_decision_batches from anon, authenticated;
grant select, insert on table public.ml_pc2_policy_registry to anon, authenticated;
grant select, insert on table public.ml_pc2_decision_batches to anon, authenticated;

do $$
begin
  if not exists (
    select 1 from pg_policies where schemaname = 'public'
      and tablename = 'ml_pc2_policy_registry'
      and policyname = 'ml_pc2_policy_registry_read'
  ) then
    create policy ml_pc2_policy_registry_read on public.ml_pc2_policy_registry
      for select to anon, authenticated using (true);
  end if;
  if not exists (
    select 1 from pg_policies where schemaname = 'public'
      and tablename = 'ml_pc2_policy_registry'
      and policyname = 'ml_pc2_policy_registry_insert'
  ) then
    create policy ml_pc2_policy_registry_insert on public.ml_pc2_policy_registry
      for insert to anon, authenticated with check (true);
  end if;
  if not exists (
    select 1 from pg_policies where schemaname = 'public'
      and tablename = 'ml_pc2_decision_batches'
      and policyname = 'ml_pc2_decision_batches_read'
  ) then
    create policy ml_pc2_decision_batches_read on public.ml_pc2_decision_batches
      for select to anon, authenticated using (true);
  end if;
  if not exists (
    select 1 from pg_policies where schemaname = 'public'
      and tablename = 'ml_pc2_decision_batches'
      and policyname = 'ml_pc2_decision_batches_insert'
  ) then
    create policy ml_pc2_decision_batches_insert on public.ml_pc2_decision_batches
      for insert to anon, authenticated with check (true);
  end if;
end $$;

comment on table public.ml_pc2_policy_registry is
  'Content-addressed PC2 policy registry used by lossless compact dual-write telemetry.';
comment on table public.ml_pc2_decision_batches is
  'One lossless exact-dedup PC2 decision batch per poll; legacy table remains authoritative during parity.';
