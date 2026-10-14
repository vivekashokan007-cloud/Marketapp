#!/usr/bin/env bash
# Implied-EV v1.2 migration suite against a SCRATCH PostgreSQL (never production).
#   PGHOST=... PGPORT=... PGUSER=... EV_SCRATCH_OK=1 tools/ev_db_suite/run.sh
set -euo pipefail
[[ "${EV_SCRATCH_OK:-}" == "1" ]] || { echo "refusing: set EV_SCRATCH_OK=1 to confirm a scratch server" >&2; exit 2; }
cd "$(git rev-parse --show-toplevel)"
exec python3 tools/ev_db_suite/suite.py
