-- G6: versioned evaluation-metrics ledger (experiment identity, not date-only).
-- Prefer this table over ml_performance for policy/target/cohort/variant metrics.
-- Legacy ml_performance remains untouched for dormant training accuracy writes.

create table if not exists public.ml_evaluation_metrics (
  metrics_id text primary key,
  run_id text not null,
  session_date date not null,
  model_hash text not null,
  feature_schema_version text not null,
  policy_selector_version text not null,
  net_target_version text not null,
  position_exit_policy_version text,
  cohort_execution_mode text not null,
  variant text not null,
  metrics_contract_version text not null default 'evaluation_metrics_ledger_v1_20260912',
  availability text not null default 'unavailable',
  reason_code text,
  n_eligible_predictions integer not null default 0,
  input_fingerprint text,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  check (char_length(metrics_id) >= 8),
  check (n_eligible_predictions >= 0),
  check (availability in ('available', 'unavailable'))
);

-- Idempotent upsert identity: full experiment key, NEVER date alone.
create unique index if not exists ml_evaluation_metrics_identity_uidx
  on public.ml_evaluation_metrics (
    run_id,
    session_date,
    model_hash,
    feature_schema_version,
    policy_selector_version,
    net_target_version,
    cohort_execution_mode,
    variant
  );

create index if not exists ml_evaluation_metrics_session_idx
  on public.ml_evaluation_metrics (session_date, variant, updated_at desc);

create index if not exists ml_evaluation_metrics_run_idx
  on public.ml_evaluation_metrics (run_id);

comment on table public.ml_evaluation_metrics is
  'G6 versioned performance ledger. Upsert by run+session+model+feature+policy+net-target+cohort+variant. Do not overwrite by date only. Shadow variants are log-only.';

comment on column public.ml_evaluation_metrics.variant is
  'ACTIVE | SHADOW_A_NET_CAL_BASELINE | SHADOW_B_NO_PML_CAP | SHADOW_C_ML_FREE_DETERMINISTIC';

comment on column public.ml_evaluation_metrics.payload is
  'JSON: confidence_decomposition, prediction_calibration (Brier/reliability; missing≠0), policy_economics, slices with thin_support, population_counts, shadow_comparison.';

alter table public.ml_evaluation_metrics enable row level security;

revoke all on table public.ml_evaluation_metrics from anon;
revoke all on table public.ml_evaluation_metrics from public;
grant select on table public.ml_evaluation_metrics to authenticated;
grant select, insert, update on table public.ml_evaluation_metrics to service_role;

drop policy if exists ml_evaluation_metrics_authenticated_read on public.ml_evaluation_metrics;
create policy ml_evaluation_metrics_authenticated_read
  on public.ml_evaluation_metrics
  for select
  to authenticated
  using (true);

drop trigger if exists ml_evaluation_metrics_touch_updated_at on public.ml_evaluation_metrics;
create trigger ml_evaluation_metrics_touch_updated_at
before update on public.ml_evaluation_metrics
for each row execute function public.touch_updated_at();
