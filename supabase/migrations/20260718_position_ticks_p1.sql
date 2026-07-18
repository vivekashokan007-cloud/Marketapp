-- POSITION TRACKING P1: capture-only tick loop storage.
-- Additive only. Does not alter ranking, teacher labels, alerts, or scan outputs.

create table if not exists public.position_ticks (
  id bigserial primary key,
  trade_id text not null,
  session_date date not null,
  tick_ts timestamptz not null,
  source text not null,
  auth_source text,
  index_key text,
  strategy_type text,
  status text,
  leg_count integer,
  valuation_quality text,
  mark_basis text,
  executable_mark numeric,
  mid_mark numeric,
  ltp_mark numeric,
  current_pnl numeric,
  current_pnl_r numeric,
  running_mae numeric,
  running_mfe numeric,
  policy_action text,
  policy_reason text,
  policy_trace_json jsonb,
  legs_json jsonb,
  created_at timestamptz not null default now()
);

create index if not exists position_ticks_trade_ts_idx
  on public.position_ticks (trade_id, tick_ts desc);

create index if not exists position_ticks_session_idx
  on public.position_ticks (session_date, index_key, strategy_type);

create index if not exists position_ticks_policy_idx
  on public.position_ticks (session_date, policy_action, valuation_quality);

alter table public.position_ticks enable row level security;

do $$
begin
  if not exists (
    select 1
    from pg_policies
    where schemaname = 'public'
      and tablename = 'position_ticks'
      and policyname = 'position_ticks_insert_anon'
  ) then
    create policy position_ticks_insert_anon
      on public.position_ticks
      for insert
      to anon, authenticated
      with check (true);
  end if;
end $$;

do $$
begin
  if not exists (
    select 1
    from pg_policies
    where schemaname = 'public'
      and tablename = 'position_ticks'
      and policyname = 'position_ticks_select_anon'
  ) then
    create policy position_ticks_select_anon
      on public.position_ticks
      for select
      to anon, authenticated
      using (true);
  end if;
end $$;

alter table if exists public.trades_v2
  add column if not exists close_trace_json jsonb;
