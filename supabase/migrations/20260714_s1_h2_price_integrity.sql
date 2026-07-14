alter table if exists public.ml_evaluation_outcomes
    add column if not exists price_integrity text,
    add column if not exists h2_price_integrity_reason text,
    add column if not exists h2_later_value_points numeric,
    add column if not exists h2_entry_basis_points numeric,
    add column if not exists h2_bound_width_points numeric,
    add column if not exists h2_formula text;

alter table if exists public.ml_recommendation_outcomes
    add column if not exists price_integrity text,
    add column if not exists h2_price_integrity_reason text,
    add column if not exists h2_later_value_points numeric,
    add column if not exists h2_entry_basis_points numeric,
    add column if not exists h2_bound_width_points numeric,
    add column if not exists h2_formula text;

update public.ml_evaluation_outcomes
   set price_integrity = 'LEGACY_PRE_S1',
       h2_price_integrity_reason = 'COMPUTED_WITH_BROKEN_DEBIT_RULER'
 where price_integrity is null;

update public.ml_recommendation_outcomes
   set price_integrity = 'LEGACY_PRE_S1',
       h2_price_integrity_reason = 'COMPUTED_WITH_BROKEN_DEBIT_RULER'
 where price_integrity is null;

do $$
begin
    if to_regclass('public.ml_evaluation_outcomes') is not null then
        create index if not exists idx_ml_eval_outcomes_price_integrity
            on public.ml_evaluation_outcomes (session_date, price_integrity, h2_price_integrity_reason);
    end if;
    if to_regclass('public.ml_recommendation_outcomes') is not null then
        create index if not exists idx_ml_reco_outcomes_price_integrity
            on public.ml_recommendation_outcomes (session_date, price_integrity, h2_price_integrity_reason);
    end if;
end $$;
