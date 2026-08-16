-- Complete the v2.5.87 PC2 telemetry identity and explanation contract.

alter table public.ml_pc2_authority_decisions
    add column if not exists authority_state_reason text not null
        default 'legacy_reason_unavailable',
    add column if not exists authority_diagnostics_version text not null
        default 'pc2_authority_diagnostics_v1';

alter table public.ml_pc2_authority_decisions
    drop constraint if exists ml_pc2_authority_decisions_diagnostics_version_check;

alter table public.ml_pc2_authority_decisions
    add constraint ml_pc2_authority_decisions_diagnostics_version_check check (
        length(btrim(authority_diagnostics_version)) > 0
    );

drop index if exists public.ml_pc2_authority_decisions_poll_index_uidx;

create unique index ml_pc2_authority_decisions_poll_index_uidx
    on public.ml_pc2_authority_decisions (
        poll_ts,
        policy_version,
        authority_diagnostics_version,
        decision_index
    );

create index if not exists ml_pc2_authority_decisions_diagnostics_session_idx
    on public.ml_pc2_authority_decisions (
        authority_diagnostics_version,
        session_date,
        authority_state
    );
