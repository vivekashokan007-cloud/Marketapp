-- P1 rejected-candidate research outcomes.
-- Additive only: rejected gate-menu outcomes are kept out of the production
-- teacher-label table whose role constraint is intentionally primary/secondary.

create table if not exists public.ml_rejected_candidate_outcomes (
    id text primary key,
    snapshot_id text not null,
    session_date date not null,
    poll_ts timestamptz,
    candidate_id text not null,
    lane text,
    index_key text,
    trade_mode text,
    strategy_type text,
    role text not null default 'rejected' check (role = 'rejected'),
    sim_pnl_h2 double precision,
    outcome_h2 boolean,
    canonical_won boolean,
    managed_pnl double precision,
    managed_gross_pnl double precision,
    friction_cost double precision,
    exit_reason text,
    exit_step integer,
    exit_ts timestamptz,
    path_points_count integer,
    r_multiple double precision,
    captured_pct double precision,
    is_success boolean,
    risk_at_entry double precision,
    regime_bucket text,
    label_version text,
    teacher_config_version text,
    tp_threshold double precision,
    sl_threshold double precision,
    break_even_win_rate_pct double precision,
    price_integrity text,
    h2_price_integrity_reason text,
    premium_edge double precision,
    credit_width_ratio double precision,
    sigma_otm double precision,
    rejection_stage text,
    rejection_reason text,
    gate_name text,
    gate_field text,
    observed_value double precision,
    threshold_value double precision,
    margin double precision,
    margin_pct double precision,
    rejected_rank_in_snapshot integer,
    rejected_eval_rank integer,
    rejected_eval_cap integer,
    rejected_eval_source text,
    stage_sample_fraction double precision,
    stage_total_rejected integer,
    stage_normalizable integer,
    stage_skipped_not_evaluable integer,
    rejected_eval_selection jsonb,
    source_record_type text,
    outcome_json jsonb not null default '{}'::jsonb,
    app_version text,
    created_at timestamptz not null default now()
);

create index if not exists idx_ml_rejected_candidate_outcomes_session_date
    on public.ml_rejected_candidate_outcomes (session_date);

create index if not exists idx_ml_rejected_candidate_outcomes_stage
    on public.ml_rejected_candidate_outcomes (session_date, rejection_stage);

create index if not exists idx_ml_rejected_candidate_outcomes_strategy
    on public.ml_rejected_candidate_outcomes (session_date, strategy_type);

create index if not exists idx_ml_rejected_candidate_outcomes_snapshot
    on public.ml_rejected_candidate_outcomes (snapshot_id);

grant select, insert, update on public.ml_rejected_candidate_outcomes to anon, authenticated;
