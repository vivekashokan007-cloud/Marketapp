-- G1 Phase 1a remaining hardening. Safe for the current anon APK/PWA.
-- Does NOT revoke INSERT/SELECT/UPDATE the recording path still needs.
-- Does NOT add ownership policies yet (no owner columns; Auth cutover is Phase 1b+).

-- Archive: strip leftover client grants. RLS is already on with no anon/auth
-- policies, so row access was already denied; this removes table-level leftovers.
-- service_role keeps grants and BYPASSRLS for admin/evaluator tools.
REVOKE ALL ON TABLE public.ml_recommendation_outcomes_archive FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON TABLE public.ml_recommendation_outcomes_archive TO service_role;

ALTER TABLE public.ml_recommendation_outcomes_archive ENABLE ROW LEVEL SECURITY;

-- TRUNCATE is a table-level privilege (not a REST DELETE). APK/PWA never need it.
REVOKE TRUNCATE ON ALL TABLES IN SCHEMA public FROM anon, authenticated;

-- APK SupabaseClient and PWA DB adapter never issue table DELETE.
-- Revoke DELETE from anon only; keep authenticated DELETE until Auth cutover.
REVOKE DELETE ON ALL TABLES IN SCHEMA public FROM anon;

-- Future tables created by postgres should not inherit TRUNCATE or anon DELETE.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE TRUNCATE ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE DELETE ON TABLES FROM anon;
