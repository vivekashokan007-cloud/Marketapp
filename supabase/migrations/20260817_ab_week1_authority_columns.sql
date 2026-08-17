-- Keep the live Build 3 A/B payload aligned with the additive audit schema.

alter table if exists public.ab_week1_decisions
  add column if not exists effective_gate_reason text,
  add column if not exists a8_shadow_evidence_reason text,
  add column if not exists a8_gate_mode text,
  add column if not exists n_ev_below_floor_released_to_ranking integer;
