-- BUILD 3 / WEEK-1 paired old-vs-new decision logger.
-- Manual Checkpoint C step: run in Supabase SQL editor after v2.5.0/b331 is shipped.

create table if not exists public.ab_week1_decisions (
  id bigserial primary key,
  snapshot_poll_ts text not null,
  schema_version integer,
  session_date date,
  poll_number integer,
  experiment_name text not null,
  app_version text,
  brain_version text,
  teacher_config_version text,
  teacher_first_active boolean not null default false,
  old_pick_candidate_id text,
  new_pick_candidate_id text,
  old_pick_lane text,
  new_pick_lane text,
  old_pick_rank_key_json jsonb,
  new_pick_rank_key_json jsonb,
  old_pick_rank_in_new integer,
  new_pick_rank_in_old integer,
  picks_differ boolean,
  old_actor_verdict text,
  new_actor_verdict text,
  gate_verdict text,
  gate_reason text,
  a8_gate_reason text,
  lane_gate_reason text,
  n_candidates_original integer,
  n_candidates_pre_gate integer,
  n_candidates_after_a8 integer,
  n_candidates_after_lane_gate integer,
  n_ev_negative integer,
  n_ev_below_floor integer,
  n_bnf_removed_by_calm_lane_gate integer,
  n_nf_survivors_after_a8 integer,
  vix double precision,
  range_sigma double precision,
  regime_type text,
  bias text,
  old_would_have_taken boolean,
  old_pick jsonb,
  new_pick jsonb,
  old_top3 jsonb,
  new_top3 jsonb,
  thresholds jsonb,
  local_saved_at text,
  inserted_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  raw_payload jsonb
);

create unique index if not exists ab_week1_decisions_poll_experiment_uidx
  on public.ab_week1_decisions (snapshot_poll_ts, experiment_name);

create index if not exists ab_week1_decisions_session_idx
  on public.ab_week1_decisions (session_date);

create index if not exists ab_week1_decisions_gate_idx
  on public.ab_week1_decisions (gate_reason);

create or replace function public.ab_week1_decisions_set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists ab_week1_decisions_updated_at on public.ab_week1_decisions;
create trigger ab_week1_decisions_updated_at
before update on public.ab_week1_decisions
for each row execute function public.ab_week1_decisions_set_updated_at();
