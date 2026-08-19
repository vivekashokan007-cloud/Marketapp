alter table if exists public.ml_generated_candidates
    add column if not exists app_version text,
    add column if not exists brain_version text;

comment on column public.ml_generated_candidates.app_version is
    'Android/PWA app version that produced this candidate evidence row.';

comment on column public.ml_generated_candidates.brain_version is
    'Python brain version that produced this candidate evidence row.';
