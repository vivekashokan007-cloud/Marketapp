-- G2 FRICTION INTO LIVE PAPER P&L.
-- Additive only. Keeps actual_pnl/canonical_won gross and adds net fields.

alter table if exists public.trades_v2
  add column if not exists friction_cost numeric,
  add column if not exists friction_breakdown_json jsonb,
  add column if not exists net_pnl numeric,
  add column if not exists net_won boolean,
  add column if not exists friction_version text;

create index if not exists trades_v2_friction_version_idx
  on public.trades_v2 (friction_version);

create index if not exists trades_v2_net_won_idx
  on public.trades_v2 (net_won);
