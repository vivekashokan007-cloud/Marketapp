-- Recommendation outcome rows use the same teacher diagnostic contract as
-- evaluation outcomes. Keep this table in parity so post-close retry upserts
-- can be idempotent and fully verified.

alter table if exists public.ml_recommendation_outcomes
    add column if not exists managed_pnl double precision,
    add column if not exists managed_gross_pnl double precision,
    add column if not exists friction_cost double precision,
    add column if not exists exit_reason text,
    add column if not exists exit_step integer,
    add column if not exists exit_ts timestamptz,
    add column if not exists r_multiple double precision,
    add column if not exists captured_pct double precision,
    add column if not exists peak_pnl double precision,
    add column if not exists trough_pnl double precision,
    add column if not exists max_capture_pct double precision,
    add column if not exists near_target_pct double precision,
    add column if not exists target_gap_pnl double precision,
    add column if not exists time_to_peak_step integer,
    add column if not exists target_was_reached boolean,
    add column if not exists is_success boolean,
    add column if not exists risk_at_entry double precision,
    add column if not exists regime_bucket text,
    add column if not exists label_version text,
    add column if not exists teacher_config_version text,
    add column if not exists tp_threshold double precision,
    add column if not exists sl_threshold double precision,
    add column if not exists break_even_win_rate_pct double precision,
    add column if not exists price_integrity text,
    add column if not exists h2_price_integrity_reason text,
    add column if not exists h2_later_value_points numeric,
    add column if not exists h2_entry_basis_points numeric,
    add column if not exists h2_bound_width_points numeric,
    add column if not exists h2_formula text,
    add column if not exists created_at timestamptz not null default now();

comment on column public.ml_recommendation_outcomes.peak_pnl is
    'Maximum net teacher P&L observed along the sampled managed-exit path before exit.';
comment on column public.ml_recommendation_outcomes.trough_pnl is
    'Minimum net teacher P&L observed along the sampled managed-exit path before exit.';
comment on column public.ml_recommendation_outcomes.max_capture_pct is
    'Peak net P&L divided by strategy maxProfit, for target-too-high diagnostics.';
comment on column public.ml_recommendation_outcomes.near_target_pct is
    'Peak net P&L divided by teacher TP threshold; values below 1.0 never reached target.';
comment on column public.ml_recommendation_outcomes.target_gap_pnl is
    'Teacher TP threshold minus peak net P&L. Positive means target was missed.';
comment on column public.ml_recommendation_outcomes.time_to_peak_step is
    'Path step where peak net P&L occurred.';
comment on column public.ml_recommendation_outcomes.target_was_reached is
    'True when the sampled path reached the teacher TP threshold.';

do $$
begin
    if to_regclass('public.ml_recommendation_outcomes') is not null then
        -- Keep the newest row per app upsert key before enforcing idempotency.
        delete from public.ml_recommendation_outcomes dst
         using (
            select ctid
              from (
                select ctid,
                       row_number() over (
                           partition by snapshot_id, candidate_id, role
                           order by created_at desc nulls last, ctid desc
                       ) as rn
                  from public.ml_recommendation_outcomes
            ) ranked
             where rn > 1
         ) dup
         where dst.ctid = dup.ctid;

        create unique index if not exists uq_ml_recommendation_outcomes_snapshot_candidate_role
            on public.ml_recommendation_outcomes (snapshot_id, candidate_id, role);

        create index if not exists idx_ml_reco_outcomes_session_role
            on public.ml_recommendation_outcomes (session_date, role);
    end if;
end $$;

notify pgrst, 'reload schema';
