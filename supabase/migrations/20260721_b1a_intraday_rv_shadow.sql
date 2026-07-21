-- Phase 1 / B1a: shadow-only intraday realized-volatility capture.
-- Additive only. Does not affect live ranking, notifications, sandbox, or teacher labels.

alter table if exists public.ml_brain_snapshots
    add column if not exists b1a_intraday_rv_json jsonb,
    add column if not exists b1a_rv_status text,
    add column if not exists b1a_bnf_rv_to_iv_daily_ratio double precision,
    add column if not exists b1a_nf_rv_to_iv_daily_ratio double precision;

alter table if exists public.ml_poll_sequences
    add column if not exists b1a_intraday_rv_json jsonb,
    add column if not exists b1a_rv_status text,
    add column if not exists b1a_bnf_rv_to_iv_daily_ratio double precision,
    add column if not exists b1a_nf_rv_to_iv_daily_ratio double precision;

do $$
begin
    if to_regclass('public.ml_brain_snapshots') is not null and exists (
        select 1
          from information_schema.columns
         where table_schema = 'public'
           and table_name = 'ml_brain_snapshots'
           and column_name = 'session_date'
    ) then
        create index if not exists ml_brain_snapshots_b1a_rv_session_idx
            on public.ml_brain_snapshots (session_date, b1a_rv_status);
    end if;

    if to_regclass('public.ml_poll_sequences') is not null and exists (
        select 1
          from information_schema.columns
         where table_schema = 'public'
           and table_name = 'ml_poll_sequences'
           and column_name = 'session_date'
    ) then
        create index if not exists ml_poll_sequences_b1a_rv_session_idx
            on public.ml_poll_sequences (session_date, b1a_rv_status);
    end if;
end $$;
