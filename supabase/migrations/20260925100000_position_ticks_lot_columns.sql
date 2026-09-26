-- b487 started sending these four fields in every position_ticks row.
-- numeric accepts Kotlin's serialized Double values (for example 30.0).
-- This is additive; the existing table grants and RLS policies remain in force.
alter table public.position_ticks
  add column if not exists quantity_units numeric,
  add column if not exists contract_lot_size numeric,
  add column if not exists number_of_lots numeric,
  add column if not exists lot_authoritative boolean;

notify pgrst, 'reload schema';
