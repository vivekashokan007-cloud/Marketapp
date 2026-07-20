-- P&L engine provenance quarantine for historical paper trades.
-- Additive only: never rewrites actual_pnl, canonical_won, premiums, or labels.

alter table if exists public.trades_v2
  add column if not exists pnl_engine text,
  add column if not exists structure_incomplete boolean,
  add column if not exists pnl_reconciles boolean,
  add column if not exists pnl_engine_reason text,
  add column if not exists implied_multiplier numeric,
  add column if not exists recon_error numeric,
  add column if not exists pnl_engine_classified_at timestamptz;

create index if not exists trades_v2_pnl_engine_idx
  on public.trades_v2 (pnl_engine);

create index if not exists trades_v2_structure_incomplete_idx
  on public.trades_v2 (structure_incomplete);

create index if not exists trades_v2_pnl_reconciles_idx
  on public.trades_v2 (pnl_reconciles);
