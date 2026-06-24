-- Market Radar: Supabase Gemini evaluator scaffold
-- Design-only migration. Safe to run multiple times.
-- This does not wire the app to Supabase yet.

create extension if not exists pgcrypto;

create table if not exists public.evaluator_jobs (
  job_id uuid primary key default gen_random_uuid(),
  status text not null default 'queued',
  requested_by text,
  runner_type text not null default 'supabase_edge',
  model_name text not null default 'gemini-2.5-pro',
  date_from date not null,
  date_to date not null,
  index_scope jsonb not null default '["BNF","NF"]'::jsonb,
  request_payload jsonb not null default '{}'::jsonb,
  summary jsonb not null default '{}'::jsonb,
  proposal_count integer not null default 0,
  error text,
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  check (
    status in ('queued', 'running', 'completed', 'failed', 'cancelled')
  ),
  check (date_to >= date_from)
);

create index if not exists evaluator_jobs_status_created_idx
  on public.evaluator_jobs (status, created_at desc);

create table if not exists public.evaluator_brief_artifacts (
  id uuid primary key default gen_random_uuid(),
  job_id uuid not null references public.evaluator_jobs(job_id) on delete cascade,
  artifact_kind text not null default 'prompt_bundle',
  prompt_version text,
  input_payload jsonb not null default '{}'::jsonb,
  snapshot_count integer not null default 0,
  decision_count integer not null default 0,
  created_at timestamptz not null default timezone('utc', now())
);

create index if not exists evaluator_brief_artifacts_job_idx
  on public.evaluator_brief_artifacts (job_id, created_at desc);

create table if not exists public.evaluator_verdict_artifacts (
  id uuid primary key default gen_random_uuid(),
  job_id uuid not null references public.evaluator_jobs(job_id) on delete cascade,
  verdict_kind text not null default 'gemini_response',
  model_name text,
  raw_text text,
  normalized_payload jsonb not null default '{}'::jsonb,
  token_usage jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default timezone('utc', now())
);

create index if not exists evaluator_verdict_artifacts_job_idx
  on public.evaluator_verdict_artifacts (job_id, created_at desc);

create table if not exists public.evaluator_proposals (
  id uuid primary key default gen_random_uuid(),
  job_id uuid not null references public.evaluator_jobs(job_id) on delete cascade,
  branch_key text not null,
  title text not null,
  summary text not null default '',
  rationale text not null default '',
  proposal_payload jsonb not null default '{}'::jsonb,
  status text not null default 'proposed',
  confidence numeric(5,2),
  priority integer not null default 100,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  check (
    status in ('proposed', 'approved', 'rejected', 'archived')
  )
);

create unique index if not exists evaluator_proposals_job_branch_key_idx
  on public.evaluator_proposals (job_id, branch_key);

create index if not exists evaluator_proposals_job_status_idx
  on public.evaluator_proposals (job_id, status, priority, created_at desc);

create table if not exists public.approved_branch_proposals (
  id uuid primary key default gen_random_uuid(),
  proposal_id uuid references public.evaluator_proposals(id) on delete set null,
  branch_key text not null unique,
  title text not null,
  summary text not null default '',
  rationale text not null default '',
  proposal_payload jsonb not null default '{}'::jsonb,
  approved_by text,
  approved_at timestamptz not null default timezone('utc', now()),
  created_at timestamptz not null default timezone('utc', now())
);

create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = timezone('utc', now());
  return new;
end;
$$;

drop trigger if exists evaluator_jobs_touch_updated_at on public.evaluator_jobs;
create trigger evaluator_jobs_touch_updated_at
before update on public.evaluator_jobs
for each row
execute function public.touch_updated_at();

drop trigger if exists evaluator_proposals_touch_updated_at on public.evaluator_proposals;
create trigger evaluator_proposals_touch_updated_at
before update on public.evaluator_proposals
for each row
execute function public.touch_updated_at();

alter table public.evaluator_jobs enable row level security;
alter table public.evaluator_brief_artifacts enable row level security;
alter table public.evaluator_verdict_artifacts enable row level security;
alter table public.evaluator_proposals enable row level security;
alter table public.approved_branch_proposals enable row level security;

do $$
begin
  if not exists (
    select 1
    from pg_policies
    where schemaname = 'public'
      and tablename = 'approved_branch_proposals'
      and policyname = 'approved_branch_proposals_read_anon'
  ) then
    create policy approved_branch_proposals_read_anon
      on public.approved_branch_proposals
      for select
      to anon, authenticated
      using (true);
  end if;
end $$;

comment on table public.evaluator_jobs is
  'Manual advisory Gemini evaluator jobs. Not connected to live trading.';
comment on table public.evaluator_brief_artifacts is
  'Structured prompt bundles and replay inputs for evaluator jobs.';
comment on table public.evaluator_verdict_artifacts is
  'Raw and normalized LLM outputs for evaluator jobs.';
comment on table public.evaluator_proposals is
  'Evaluator-produced advisory branch proposals pending human review.';
comment on table public.approved_branch_proposals is
  'Optional future mirror for live-approved proposals.';
