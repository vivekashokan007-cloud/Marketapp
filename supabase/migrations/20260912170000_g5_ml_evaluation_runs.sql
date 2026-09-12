-- G5: durable evening evaluation-run ledger.
-- Prefs may cache status; this table is the preferred completion record.
-- Local devices should also mirror the same JSON identity.

create table if not exists public.ml_evaluation_runs (
  run_id text primary key,
  revision integer not null default 1,
  session_date date not null,
  scope text not null default 'owner_device_paper',
  policy_label_contract text not null,
  input_manifest_hash text not null,
  input_manifest jsonb not null default '{}'::jsonb,
  evaluator_version text not null,
  ledger_contract_version text not null default 'evaluation_run_ledger_v1_20260912',
  lease_holder text,
  lease_expires_at timestamptz,
  stages jsonb not null default '{}'::jsonb,
  labels_saved boolean not null default false,
  learning_complete boolean not null default false,
  active boolean not null default true,
  last_error text,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  check (revision >= 1),
  check (char_length(run_id) >= 8)
);

create unique index if not exists ml_evaluation_runs_identity_uidx
  on public.ml_evaluation_runs (
    session_date,
    scope,
    policy_label_contract,
    input_manifest_hash,
    evaluator_version
  );

create index if not exists ml_evaluation_runs_session_idx
  on public.ml_evaluation_runs (session_date, updated_at desc);

create index if not exists ml_evaluation_runs_active_lease_idx
  on public.ml_evaluation_runs (active, lease_expires_at)
  where active = true;

comment on table public.ml_evaluation_runs is
  'G5 durable post-close evaluation-run identity and per-stage completion. Labels saved must not imply learning complete when C3/research failed.';

comment on column public.ml_evaluation_runs.labels_saved is
  'True when outcome_persistence stage is verified (including truthful empty).';

comment on column public.ml_evaluation_runs.learning_complete is
  'True only when every applicable stage is verified or explicitly ineligible/disabled. Failed C3 blocks this flag.';

alter table public.ml_evaluation_runs enable row level security;

-- Staging grants: authenticated read of own scope later; writes via controlled path.
-- Keep compatible with G1 containment: no public anon write of evaluation attestation.
revoke all on table public.ml_evaluation_runs from anon;
revoke all on table public.ml_evaluation_runs from public;
grant select on table public.ml_evaluation_runs to authenticated;
-- Inserts/updates intended for service/evaluator role after G1 cutover.
grant select, insert, update on table public.ml_evaluation_runs to service_role;

drop policy if exists ml_evaluation_runs_authenticated_read on public.ml_evaluation_runs;
create policy ml_evaluation_runs_authenticated_read
  on public.ml_evaluation_runs
  for select
  to authenticated
  using (true);

drop trigger if exists ml_evaluation_runs_touch_updated_at on public.ml_evaluation_runs;
create trigger ml_evaluation_runs_touch_updated_at
before update on public.ml_evaluation_runs
for each row execute function public.touch_updated_at();
