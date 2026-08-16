-- v2.5.87 PC2 authority contract. Preserve historical v2.5.86 states while
-- normalizing the three-state release contract used by new decisions.

alter table public.ml_pc2_authority_decisions
    add column if not exists constant text,
    add column if not exists context_variable text,
    add column if not exists slice_key text,
    add column if not exists authority_kind text,
    add column if not exists stability_bar double precision,
    add column if not exists stability_pass boolean,
    add column if not exists diversity_pass boolean,
    add column if not exists diversity_status text,
    add column if not exists censor_guard_status text,
    add column if not exists censor_guard_flags jsonb,
    add column if not exists population_provenance_verified boolean,
    add column if not exists population_provenance_scope text,
    add column if not exists population_provenance_version text,
    add column if not exists neutrality_tick double precision,
    add column if not exists neutrality_delta double precision,
    add column if not exists neutrality_pass boolean,
    add column if not exists hard_threshold double precision,
    add column if not exists percentile_threshold double precision,
    add column if not exists observed_value double precision,
    add column if not exists hard_passed boolean,
    add column if not exists percentile_passed boolean,
    add column if not exists counterfactual_behavior_differs boolean,
    add column if not exists promotion_id text,
    add column if not exists promotion_valid_until date;

alter table public.ml_pc2_authority_decisions
    drop constraint if exists ml_pc2_authority_decisions_authority_state_check;

alter table public.ml_pc2_authority_decisions
    add constraint ml_pc2_authority_decisions_authority_state_check check (
        authority_state in (
            'SHADOW',
            'BEHAVIOR_NEUTRAL',
            'BEHAVIOR_CHANGING_PAPER',
            'FALLBACK',
            'ACTIVE_NEUTRAL',
            'ACTIVE_CHANGED'
        )
    );

create index if not exists ml_pc2_authority_decisions_constant_slice_idx
    on public.ml_pc2_authority_decisions (constant, slice_key, session_date);

create index if not exists ml_pc2_authority_decisions_state_session_idx
    on public.ml_pc2_authority_decisions (authority_state, session_date);

grant select, insert, update on table public.ml_pc2_authority_decisions
    to anon, authenticated;
