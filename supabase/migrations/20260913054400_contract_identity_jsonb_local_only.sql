-- LOCAL-ONLY DRAFT MIGRATION — NOT APPLIED TO PRODUCTION
-- Gate: Lot/DTE/NF–BNF identity (2026-09-13)
-- Branch: work/g8-g10-integrity-20260913
-- Ruling: HANDOFF_chatgpt_ruling_lot_dte_20260913.md D2 option B
--
-- STATUS: NOT APPLIED TO PRODUCTION. Publishing paused.
-- Do NOT run against prod Supabase. Do NOT use apply_migration MCP.
-- File exists so writers/tests can target the intended additive schema locally.
--
-- Additive nullable jsonb only. No rewrites, no backfills, no drops.

-- Primary evaluation outcomes
ALTER TABLE public.ml_evaluation_outcomes
  ADD COLUMN IF NOT EXISTS contract_identity jsonb NULL;

COMMENT ON COLUMN public.ml_evaluation_outcomes.contract_identity IS
  'Canonical contract identity payload (lot/DTE/index/expiry/legs). Nullable for legacy rows. LOCAL migration draft — not applied to production as of 2026-09-13.';

-- Recommendation outcomes (secondary path)
ALTER TABLE public.ml_recommendation_outcomes
  ADD COLUMN IF NOT EXISTS contract_identity jsonb NULL;

COMMENT ON COLUMN public.ml_recommendation_outcomes.contract_identity IS
  'Canonical contract identity payload (lot/DTE/index/expiry/legs). Nullable for legacy rows. LOCAL migration draft — not applied to production as of 2026-09-13.';

-- Rejected path keeps identity inside existing outcome_json / evaluation_lineage
-- (table: ml_rejected_candidate_outcomes). No new column required for rejected.

-- OPTIONAL local index for filtering (do not create on prod under pause):
-- CREATE INDEX IF NOT EXISTS ml_evaluation_outcomes_contract_identity_gin
--   ON public.ml_evaluation_outcomes USING gin (contract_identity);
