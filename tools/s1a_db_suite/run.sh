#!/usr/bin/env bash
# b511 S1a client-DELETE containment suite against a SCRATCH PostgreSQL 17 (never production).
#   PGHOST=... PGPORT=... PGUSER=<superuser> S1A_SCRATCH_OK=1 [S1A_POSTGREST_BIN=...] tools/s1a_db_suite/run.sh
set -euo pipefail
[[ "${S1A_SCRATCH_OK:-}" == "1" ]] || { echo "refusing: set S1A_SCRATCH_OK=1 to confirm a scratch server" >&2; exit 2; }
cd "$(git rev-parse --show-toplevel)"
exec python3 tools/s1a_db_suite/suite.py
