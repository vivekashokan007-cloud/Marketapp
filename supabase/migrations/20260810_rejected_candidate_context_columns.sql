-- Keeps rejected-candidate research rows aligned with the app payload.
-- This is additive only; existing rows and constraints are unchanged.

alter table if exists public.ml_rejected_candidate_outcomes
    add column if not exists iv_richness double precision,
    add column if not exists width double precision,
    add column if not exists prob_profit double precision;

comment on column public.ml_rejected_candidate_outcomes.iv_richness is
    'Candidate IV-richness context captured at ranking/evaluation time.';
comment on column public.ml_rejected_candidate_outcomes.width is
    'Strategy width used for rejected-candidate diagnostics.';
comment on column public.ml_rejected_candidate_outcomes.prob_profit is
    'Candidate probability-of-profit context captured at ranking/evaluation time.';

notify pgrst, 'reload schema';
