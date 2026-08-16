-- PC2 authority telemetry. One row records one resolver decision made during
-- a poll, allowing post-close audits to distinguish fallback, neutral active,
-- and behavior-changing active percentile authority.

create table if not exists public.ml_pc2_authority_decisions (
    id text primary key,
    session_date date not null,
    poll_ts timestamptz not null,
    decision_index integer not null,
    variable_name text not null,
    authority_state text not null check (
        authority_state in ('FALLBACK', 'ACTIVE_NEUTRAL', 'ACTIVE_CHANGED')
    ),
    provenance_policy text not null,
    support_count integer not null default 0,
    stability_ratio double precision,
    fallback_reason text,
    brain_version text not null,
    policy_version text not null,
    policy_json jsonb not null default '{}'::jsonb,
    decision_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create unique index if not exists ml_pc2_authority_decisions_poll_index_uidx
    on public.ml_pc2_authority_decisions (poll_ts, policy_version, decision_index);

create index if not exists ml_pc2_authority_decisions_session_idx
    on public.ml_pc2_authority_decisions (session_date, poll_ts);

create index if not exists ml_pc2_authority_decisions_variable_idx
    on public.ml_pc2_authority_decisions (variable_name, authority_state, session_date);

alter table public.ml_pc2_authority_decisions enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename = 'ml_pc2_authority_decisions'
      and policyname = 'ml_pc2_authority_decisions_anon_read'
  ) then
    create policy ml_pc2_authority_decisions_anon_read
      on public.ml_pc2_authority_decisions
      for select to anon, authenticated
      using (true);
  end if;
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename = 'ml_pc2_authority_decisions'
      and policyname = 'ml_pc2_authority_decisions_anon_write'
  ) then
    create policy ml_pc2_authority_decisions_anon_write
      on public.ml_pc2_authority_decisions
      for insert to anon, authenticated
      with check (true);
  end if;
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename = 'ml_pc2_authority_decisions'
      and policyname = 'ml_pc2_authority_decisions_anon_update'
  ) then
    create policy ml_pc2_authority_decisions_anon_update
      on public.ml_pc2_authority_decisions
      for update to anon, authenticated
      using (true)
      with check (true);
  end if;
end $$;
