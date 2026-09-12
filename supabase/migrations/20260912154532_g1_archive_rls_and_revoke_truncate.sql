-- G1 Phase 1a (applied remotely 2026-09-12 as g1_archive_rls_and_revoke_truncate).
-- Checked in so the repo matches production. Statements are idempotent.
-- Archive: RLS on, no anon/authenticated policies (deny). Truncate revoked on
-- the first critical-table set. See 20260912162000 for remaining grant cleanup.

ALTER TABLE public.ml_recommendation_outcomes_archive ENABLE ROW LEVEL SECURITY;

REVOKE TRUNCATE ON TABLE
    public.app_config,
    public.ml_brain_snapshots,
    public.ml_context_percentile_history,
    public.ml_daily_accuracy,
    public.ml_evaluation_outcomes,
    public.ml_models,
    public.ml_performance,
    public.ml_recommendation_outcomes,
    public.ml_recommendation_outcomes_archive,
    public.ml_rejected_candidate_outcomes,
    public.trades,
    public.trades_v2
FROM anon, authenticated;
