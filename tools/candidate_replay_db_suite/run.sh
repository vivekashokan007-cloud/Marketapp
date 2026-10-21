#!/usr/bin/env bash
# Candidate replay (ml_generated_candidates upsert vs live RLS) against a SCRATCH PostgreSQL 17 + PostgREST (never production).
#   PGHOST=... PGPORT=... PGUSER=<superuser> CANDIDATE_SCRATCH_OK=1 CANDIDATE_POSTGREST_BIN=... tools/candidate_replay_db_suite/run.sh [--write]
set -euo pipefail
[[ "${CANDIDATE_SCRATCH_OK:-}" == "1" ]] || { echo "refusing: set CANDIDATE_SCRATCH_OK=1 to confirm a scratch server" >&2; exit 2; }
cd "$(git rev-parse --show-toplevel)"
exec python3 tools/candidate_replay_db_suite/suite.py "$@"
