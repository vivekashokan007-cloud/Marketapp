-- BUILD 3 / D4-D7 A/B decision logger schema repair.
-- Additive only. Fixes PostgREST PGRST204 on live ab_week1_decisions upsert.

alter table if exists public.ab_week1_decisions
  add column if not exists a8_ev_floor_mult double precision;

create index if not exists ab_week1_decisions_a8_ev_floor_mult_idx
  on public.ab_week1_decisions (session_date, a8_ev_floor_mult);
