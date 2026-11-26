-- Extend the active option evidence table; preserve its existing rows and keys.
alter table public.ml_option_chain_snapshots
  add column if not exists instrument_key text,
  add column if not exists market_data_json jsonb,
  add column if not exists option_greeks_json jsonb,
  add column if not exists source_received_at timestamptz;
