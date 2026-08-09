alter table if exists public.ml_evaluation_outcomes
    add column if not exists peak_pnl double precision,
    add column if not exists trough_pnl double precision,
    add column if not exists max_capture_pct double precision,
    add column if not exists near_target_pct double precision,
    add column if not exists target_gap_pnl double precision,
    add column if not exists time_to_peak_step integer,
    add column if not exists target_was_reached boolean;

alter table if exists public.ml_rejected_candidate_outcomes
    add column if not exists peak_pnl double precision,
    add column if not exists trough_pnl double precision,
    add column if not exists max_capture_pct double precision,
    add column if not exists near_target_pct double precision,
    add column if not exists target_gap_pnl double precision,
    add column if not exists time_to_peak_step integer,
    add column if not exists target_was_reached boolean;

comment on column public.ml_evaluation_outcomes.peak_pnl is
    'Maximum net teacher P&L observed along the sampled managed-exit path before exit.';
comment on column public.ml_evaluation_outcomes.trough_pnl is
    'Minimum net teacher P&L observed along the sampled managed-exit path before exit.';
comment on column public.ml_evaluation_outcomes.max_capture_pct is
    'Peak net P&L divided by strategy maxProfit, for target-too-high diagnostics.';
comment on column public.ml_evaluation_outcomes.near_target_pct is
    'Peak net P&L divided by teacher TP threshold; values below 1.0 never reached target.';
comment on column public.ml_evaluation_outcomes.target_gap_pnl is
    'Teacher TP threshold minus peak net P&L. Positive means target was missed.';
comment on column public.ml_evaluation_outcomes.time_to_peak_step is
    'Path step where peak net P&L occurred.';
comment on column public.ml_evaluation_outcomes.target_was_reached is
    'True when the sampled path reached the teacher TP threshold.';

comment on column public.ml_rejected_candidate_outcomes.peak_pnl is
    'Maximum net teacher P&L observed along the sampled managed-exit path before exit.';
comment on column public.ml_rejected_candidate_outcomes.trough_pnl is
    'Minimum net teacher P&L observed along the sampled managed-exit path before exit.';
comment on column public.ml_rejected_candidate_outcomes.max_capture_pct is
    'Peak net P&L divided by strategy maxProfit, for target-too-high diagnostics.';
comment on column public.ml_rejected_candidate_outcomes.near_target_pct is
    'Peak net P&L divided by teacher TP threshold; values below 1.0 never reached target.';
comment on column public.ml_rejected_candidate_outcomes.target_gap_pnl is
    'Teacher TP threshold minus peak net P&L. Positive means target was missed.';
comment on column public.ml_rejected_candidate_outcomes.time_to_peak_step is
    'Path step where peak net P&L occurred.';
comment on column public.ml_rejected_candidate_outcomes.target_was_reached is
    'True when the sampled path reached the teacher TP threshold.';
