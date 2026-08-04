-- C.3 context percentile history.
-- Additive derived table for controlled live/backfill writes. The app continues
-- to store compact percentile telemetry in ml_brain_snapshots.context_json; this
-- table is for batch-safe point-in-time percentile history and audits.

create table if not exists public.ml_context_percentile_history (
    id text primary key,
    session_date date not null,
    poll_ts timestamptz,
    snapshot_id text,
    index_key text,
    lane text,
    trade_mode text,
    variable_name text not null,
    variable_group text,
    value double precision,
    pct_30 double precision,
    pct_60 double precision,
    support_count integer not null default 0,
    support_count_30 integer,
    support_count_60 integer,
    history_window_end timestamptz,
    history_source text not null check (history_source in ('live', 'backfill')),
    pre_t_clean boolean not null default false,
    schema_version text not null default 'context_percentiles_v1',
    recording_version text not null default 'c3_percentile_recording_v1',
    source_table text,
    source_quality text,
    extra_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create unique index if not exists idx_ml_context_percentile_history_unique
    on public.ml_context_percentile_history (
        session_date,
        coalesce(poll_ts, 'epoch'::timestamptz),
        coalesce(index_key, ''),
        coalesce(lane, ''),
        variable_name,
        history_source
    );

create index if not exists idx_ml_context_percentile_history_session
    on public.ml_context_percentile_history (session_date);

create index if not exists idx_ml_context_percentile_history_variable
    on public.ml_context_percentile_history (variable_name, session_date);

create index if not exists idx_ml_context_percentile_history_source
    on public.ml_context_percentile_history (history_source, pre_t_clean, session_date);

grant select, insert, update on public.ml_context_percentile_history to anon, authenticated;
