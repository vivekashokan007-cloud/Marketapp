# Evaluator containment scratch DB suite

Never production. Builds disposable PostgreSQL 17 databases that mirror the live
evaluator / ai_* shape, measures effective access for anon / authenticated /
anonymous-auth / service_role (SQL + PostgREST), applies the proposed migration,
re-measures, and re-applies for idempotence. Runs under both pre- and post-
30-Oct-2026 default-grant profiles.

```sh
PGHOST=127.0.0.1 PGPORT=55440 PGUSER=supabase_admin \
EVALUATOR_CONTAINMENT_SCRATCH_OK=1 \
EVALUATOR_CONTAINMENT_POSTGREST_BIN=/path/to/postgrest \
tools/evaluator_containment_db_suite/run.sh
```
