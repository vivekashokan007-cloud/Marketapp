-- Scratch fixture (never production): the exact live shape of the evaluator / ai_* objects relevant to
-- CDX-GROK-CONTAINMENT-PREP-1, transcribed from the read-only catalog capture of 26 Oct 2026 06:30 IST
-- (docs/security/evaluator_containment_20261026/live_baseline/catalog). Run as role postgres (owner).
-- Exception (labelled): public.ml_evaluation_outcomes is REDUCED to the three columns evaluator-jobs-run reads;
-- it is not a containment target. No production rows, keys or secrets are used anywhere.

create table public.ai_evaluation_jobs (
  id uuid not null default gen_random_uuid() primary key,
  created_at timestamptz not null default now(),
  date_from date not null,
  date_to date not null,
  brain_version text not null,
  dataset_version text not null,
  model_name text not null,
  status text not null default 'queued'::text,
  error_text text,
  prompt_hash text,
  response_hash text
);
create index idx_ai_eval_jobs_status on public.ai_evaluation_jobs (status);

create table public.ai_branch_proposals (
  id uuid not null default gen_random_uuid() primary key,
  job_id uuid not null references public.ai_evaluation_jobs(id) on delete cascade,
  proposal_id text not null,
  status text not null default 'proposed'::text,
  index_key text not null,
  category text not null,
  priority text,
  proposal_json jsonb not null,
  validation_notes jsonb,
  approved_by uuid references auth.users(id),
  approved_at timestamptz,
  constraint ai_branch_proposals_job_id_proposal_id_key unique (job_id, proposal_id)
);
create index idx_ai_branch_prop_job_id on public.ai_branch_proposals (job_id);
create index idx_ai_branch_prop_status on public.ai_branch_proposals (status);

create table public.ai_evaluation_examples (
  id uuid not null default gen_random_uuid() primary key,
  job_id uuid not null references public.ai_evaluation_jobs(id) on delete cascade,
  example_id text not null,
  example_json jsonb not null,
  constraint ai_evaluation_examples_job_id_example_id_key unique (job_id, example_id)
);

create table public.evaluator_jobs (
  job_id uuid not null default gen_random_uuid() primary key,
  month_key text not null,
  requested_by text not null default 'android_app'::text,
  status text not null default 'queued'::text,
  started_at timestamptz,
  completed_at timestamptz,
  proposal_count integer not null default 0,
  summary_json jsonb not null default '{}'::jsonb,
  artifact_json jsonb not null default '{}'::jsonb,
  error_text text,
  created_at timestamptz not null default timezone('utc'::text, now()),
  updated_at timestamptz not null default timezone('utc'::text, now()),
  runner_type text not null default 'supabase_edge'::text,
  model_name text not null default 'gemini-2.5-pro'::text,
  date_from date,
  date_to date,
  index_scope jsonb not null default '["BNF", "NF"]'::jsonb,
  request_payload jsonb not null default '{}'::jsonb,
  summary jsonb not null default '{}'::jsonb,
  error text,
  run_attempt integer not null default 1
);

create table public.evaluator_proposals (
  id uuid not null default gen_random_uuid() primary key,
  job_id uuid not null references public.evaluator_jobs(job_id) on delete cascade,
  branch_key text not null,
  title text not null,
  summary text not null default ''::text,
  diff_json jsonb not null default '[]'::jsonb,
  metrics_json jsonb not null default '{}'::jsonb,
  risk_flags jsonb not null default '[]'::jsonb,
  status text not null default 'proposed'::text,
  created_at timestamptz not null default timezone('utc'::text, now()),
  updated_at timestamptz not null default timezone('utc'::text, now()),
  rationale text not null default ''::text,
  proposal_payload jsonb not null default '{}'::jsonb,
  confidence numeric(5,2),
  priority integer not null default 100
);
create unique index evaluator_proposals_job_branch_key_idx on public.evaluator_proposals (job_id, branch_key);

create table public.evaluator_brief_artifacts (
  id uuid not null default gen_random_uuid() primary key,
  job_id uuid not null references public.evaluator_jobs(job_id) on delete cascade,
  artifact_kind text not null default 'prompt_bundle'::text,
  prompt_version text,
  input_payload jsonb not null default '{}'::jsonb,
  snapshot_count integer not null default 0,
  decision_count integer not null default 0,
  created_at timestamptz not null default timezone('utc'::text, now()),
  run_attempt integer not null default 1
);
create unique index evaluator_brief_artifacts_job_attempt_idx on public.evaluator_brief_artifacts (job_id, run_attempt);

create table public.evaluator_verdict_artifacts (
  id uuid not null default gen_random_uuid() primary key,
  job_id uuid not null references public.evaluator_jobs(job_id) on delete cascade,
  verdict_kind text not null default 'gemini_response'::text,
  model_name text,
  raw_text text,
  normalized_payload jsonb not null default '{}'::jsonb,
  token_usage jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default timezone('utc'::text, now()),
  run_attempt integer not null default 1
);
create unique index evaluator_verdict_artifacts_job_attempt_idx on public.evaluator_verdict_artifacts (job_id, run_attempt);

create table public.approved_branch_proposals (
  id uuid not null default gen_random_uuid() primary key,
  proposal_id uuid references public.evaluator_proposals(id) on delete set null,
  branch_key text not null,
  title text not null,
  summary text not null default ''::text,
  diff_json jsonb not null default '[]'::jsonb,
  metrics_json jsonb not null default '{}'::jsonb,
  approved_by text not null default 'android_app'::text,
  approval_note text not null default ''::text,
  created_at timestamptz not null default timezone('utc'::text, now()),
  updated_at timestamptz not null default timezone('utc'::text, now()),
  rationale text not null default ''::text,
  proposal_payload jsonb not null default '{}'::jsonb,
  approved_at timestamptz not null default timezone('utc'::text, now())
);
create unique index approved_branch_proposals_branch_key_idx on public.approved_branch_proposals (branch_key);

-- REDUCED (see header)
create table public.ml_evaluation_outcomes (
  id bigint generated by default as identity primary key,
  session_date date,
  teacher_config_version text
);

create function public.touch_updated_at() returns trigger language plpgsql as $f$
begin
  new.updated_at = timezone('utc', now());
  return new;
end;
$f$;
create trigger trg_evaluator_jobs_updated_at before update on public.evaluator_jobs for each row execute function public.touch_updated_at();
create trigger evaluator_jobs_touch_updated_at before update on public.evaluator_jobs for each row execute function public.touch_updated_at();
create trigger trg_evaluator_proposals_updated_at before update on public.evaluator_proposals for each row execute function public.touch_updated_at();
create trigger evaluator_proposals_touch_updated_at before update on public.evaluator_proposals for each row execute function public.touch_updated_at();

-- Owner-rights (no security_invoker), auto-updatable view: verbatim live definition.
create view public.evaluator_proposals_app_view as
 SELECT id,
    job_id,
    status,
    COALESCE(proposal_payload ->> 'category'::text, title, 'proposal'::text) AS category,
    COALESCE((proposal_payload ->> 'priority'::text)::integer, priority) AS priority,
    COALESCE(proposal_payload ->> 'validation_notes'::text, proposal_payload ->> 'validationNotes'::text, ''::text) AS validation_notes,
        CASE
            WHEN status = 'approved'::text THEN updated_at
            ELSE NULL::timestamp with time zone
        END AS approved_at,
    COALESCE(proposal_payload ->> 'index_key'::text, proposal_payload ->> 'index'::text, 'ALL'::text) AS index_key,
    jsonb_build_object('proposal_id', COALESCE(proposal_payload ->> 'proposal_id'::text, id::text), 'status', status, 'index', COALESCE(proposal_payload ->> 'index'::text, proposal_payload ->> 'index_key'::text, 'ALL'::text), 'index_key', COALESCE(proposal_payload ->> 'index_key'::text, proposal_payload ->> 'index'::text, 'ALL'::text), 'category', COALESCE(proposal_payload ->> 'category'::text, title, 'proposal'::text), 'priority', COALESCE((proposal_payload ->> 'priority'::text)::integer, priority), 'hypothesis', COALESCE(proposal_payload ->> 'hypothesis'::text, ''::text), 'explanation', COALESCE(proposal_payload ->> 'explanation'::text, rationale, ''::text), 'conditions', COALESCE(proposal_payload -> 'conditions'::text, '{}'::jsonb), 'action', COALESCE(proposal_payload -> 'action'::text, '{}'::jsonb), 'evidence', COALESCE(proposal_payload -> 'evidence'::text, '{}'::jsonb)) AS proposal_json
   FROM evaluator_proposals p;

-- RLS flags (live: enabled, not forced, on every table here; the view has none).
alter table public.ai_evaluation_jobs enable row level security;
alter table public.ai_branch_proposals enable row level security;
alter table public.ai_evaluation_examples enable row level security;
alter table public.evaluator_jobs enable row level security;
alter table public.evaluator_proposals enable row level security;
alter table public.evaluator_brief_artifacts enable row level security;
alter table public.evaluator_verdict_artifacts enable row level security;
alter table public.approved_branch_proposals enable row level security;
alter table public.ml_evaluation_outcomes enable row level security;

-- Policies: verbatim live set (pg_policies 26 Oct 06:30 IST).
create policy "Enable insert for authenticated users only" on public.ai_branch_proposals as permissive for insert to public with check ((auth.role() = 'authenticated'::text));
create policy "Enable read access for all users" on public.ai_branch_proposals as permissive for select to public using (true);
create policy "Enable update for authenticated users only" on public.ai_branch_proposals as permissive for update to public using ((auth.role() = 'authenticated'::text));
create policy "Enable insert for authenticated users only" on public.ai_evaluation_examples as permissive for insert to public with check ((auth.role() = 'authenticated'::text));
create policy "Enable read access for all users" on public.ai_evaluation_examples as permissive for select to public using (true);
create policy "Enable insert for authenticated users only" on public.ai_evaluation_jobs as permissive for insert to public with check ((auth.role() = 'authenticated'::text));
create policy "Enable read access for all users" on public.ai_evaluation_jobs as permissive for select to public using (true);
create policy "Enable update for authenticated users only" on public.ai_evaluation_jobs as permissive for update to public using ((auth.role() = 'authenticated'::text));
create policy approved_branch_proposals_read_all on public.approved_branch_proposals as permissive for select to public using (true);
create policy approved_branch_proposals_read_anon on public.approved_branch_proposals as permissive for select to anon, authenticated using (true);
create policy anon_rw_ml_eval on public.ml_evaluation_outcomes as permissive for all to anon using (true) with check (true);
