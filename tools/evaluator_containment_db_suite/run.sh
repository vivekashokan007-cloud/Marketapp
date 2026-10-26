#!/usr/bin/env bash
# CDX-GROK-CONTAINMENT-PREP-1 scratch suite. Never production.
#   PGHOST=... PGPORT=... PGUSER=supabase_admin EVALUATOR_CONTAINMENT_SCRATCH_OK=1 \
#   [EVALUATOR_CONTAINMENT_POSTGREST_BIN=/path/to/postgrest] tools/evaluator_containment_db_suite/run.sh
set -euo pipefail
[[ "${EVALUATOR_CONTAINMENT_SCRATCH_OK:-}" == "1" ]] || { echo "refusing: set EVALUATOR_CONTAINMENT_SCRATCH_OK=1" >&2; exit 2; }
cd "$(git rev-parse --show-toplevel)"
exec python3 tools/evaluator_containment_db_suite/suite.py
