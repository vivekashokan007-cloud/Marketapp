#!/usr/bin/env bash
# DB-1 PostgreSQL suite and end-to-end parity audit, against a SCRATCH server.
#
# Never point this at production: it creates and drops its own database, and it
# deliberately corrupts rows inside that scratch database to prove the audit
# catches them. It refuses to run unless PC2_SCRATCH_OK=1.
#
#   PGHOST=... PGPORT=... PGUSER=... PGPASSWORD=... PC2_SCRATCH_OK=1 tools/pc2_db_suite/run.sh
#
# PC2_JVM_TEST runs one JUnit class with the environment passed through; it
# defaults to the Gradle unit-test task (as in CI).
set -euo pipefail
[[ "${PC2_SCRATCH_OK:-}" == "1" ]] || { echo "refusing: set PC2_SCRATCH_OK=1 to confirm a scratch server" >&2; exit 2; }
cd "$(git rev-parse --show-toplevel)"
JVM_TEST="${PC2_JVM_TEST:-./gradlew --console=plain :app:testDebugUnitTest --rerun --tests}"
DB="pc2_suite_$$"
WORK="$(mktemp -d)"
KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
psql_admin() { psql -X -q -v ON_ERROR_STOP=1 -d postgres "$@"; }
psql_db()    { psql -X -q -v ON_ERROR_STOP=1 -d "$DB" "$@"; }
cleanup() { psql_admin -c "drop database if exists $DB" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT
failures=0
check() { if eval "$2"; then echo "PASS  $1"; else echo "FAIL  $1"; failures=$((failures + 1)); fi; }

echo "== scratch database $DB"
psql_admin -c "create database $DB" >/dev/null
psql_db <<'SQL'
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
end $$;
-- Minimal stand-ins for the legacy tables the audit reads; the migration must not touch them.
create table public.ml_brain_snapshots (id bigserial primary key, poll_ts timestamptz not null, session_date date, context_json jsonb);
create table public.ml_pc2_authority_decisions (id bigserial primary key, poll_ts timestamptz not null, decision_index integer not null, decision_json jsonb not null);
create table public.ml_option_chain_snapshots (poll_ts timestamptz not null, index_key text not null, strike numeric not null, option_type text not null,
  primary key (poll_ts, index_key, strike, option_type));
grant select, insert on public.ml_pc2_authority_decisions to anon, authenticated;
grant usage, select on sequence public.ml_pc2_authority_decisions_id_seq to anon, authenticated;
SQL
psql_db -f supabase/migrations/20260929100000_pc2_compact_dual_write.sql >/dev/null 2>&1
psql_db -f supabase/migrations/20260929100000_pc2_compact_dual_write.sql >/dev/null 2>&1
echo "PASS  migration applies, and re-applies idempotently"

echo "== fixtures from the current Kotlin builder"
PC2_FIXTURE_OUT="$WORK/fixtures" PC2_FIXTURE_KEY="$KEY" $JVM_TEST com.marketradar.app.Pc2DbFixtureTest >"$WORK/fixture.log" 2>&1 \
  || { cat "$WORK/fixture.log"; echo "FAIL  fixture generation"; exit 1; }
check "fixtures written" "[[ -s $WORK/fixtures/ingest_vectors.sql && -s $WORK/fixtures/session_owner.sql ]]"

echo "== migration suite"
psql -X -d "$DB" -v key="$KEY" -v vectors="$WORK/fixtures/ingest_vectors.sql" -f tools/pc2_db_suite/suite.sql >"$WORK/suite.out" 2>&1 || true
grep -E "^(PASS|FAIL)|ERROR" "$WORK/suite.out" || true
suite_fail=$(grep -cE "^FAIL|ERROR" "$WORK/suite.out" || true)
suite_pass=$(grep -c "^PASS" "$WORK/suite.out" || true)
vectors_ok=$(grep -c "inserted=true" "$WORK/suite.out" || true)
check "suite: $suite_pass passed, $suite_fail failed" "[[ $suite_fail -eq 0 && $suite_pass -ge 50 ]]"
check "all 11 Kotlin-built vectors ingested as anon ($vectors_ok)" "[[ $vectors_ok -eq 11 ]]"

echo "== end-to-end session, ingested as anon"
psql_db -f "$WORK/fixtures/session_owner.sql" >/dev/null
inserted=$( { echo "set role anon;"; cat "$WORK/fixtures/session_anon.sql"; } | psql -X -q -At -v ON_ERROR_STOP=1 -d "$DB" | grep -c '^true$' || true)
check "12 session batches ingested as anon ($inserted)" "[[ $inserted -eq 12 ]]"

export PC2_DB_URL="dbname=$DB"
audit() { # $1 = export dir; returns the JVM test's exit code
  PC2_AUDIT_DIR="$1" $JVM_TEST com.marketradar.app.Pc2ParityAuditRunTest >"$1.log" 2>&1
}
category() { python3 -c "import json,sys; r=json.load(open('$1/pc2_parity_report.json')); sys.exit(0 if r['categories'].get('$2',{}).get('fail',0)>0 else 1)"; }
field() { python3 -c "import json; print(json.load(open('$1/pc2_parity_report.json'))['$2'])"; }

tools/pc2_parity_export.sh 2026-10-01 "$WORK/ok" >/dev/null
if audit "$WORK/ok"; then echo "PASS  complete session certifies"; else echo "FAIL  complete session did not certify"; cat "$WORK/ok/pc2_parity_report.json" || true; failures=$((failures+1)); fi
check "certified 12 of 12 PC2 polls, 13 inventory polls" "[[ \$(field $WORK/ok certified_polls) == 12 && \$(field $WORK/ok expected_pc2_polls) == 12 && \$(field $WORK/ok inventory_polls) == 13 ]]"

expect_fail() { # $1 label, $2 dir, $3 expected category (or '-' for input faults)
  if audit "$2"; then echo "FAIL  $1: audit certified"; failures=$((failures+1)); return; fi
  if [[ "$3" == "-" ]] || category "$2" "$3"; then echo "PASS  $1 (not certified${3:+, $3})"; else echo "FAIL  $1: expected $3"; failures=$((failures+1)); fi
}

cp -r "$WORK/ok" "$WORK/nomanifest"; rm "$WORK/nomanifest/manifest.json"
expect_fail "A1 missing manifest" "$WORK/nomanifest" INPUT_INCOMPLETE
cp -r "$WORK/ok" "$WORK/truncated"; sed -i '$d' "$WORK/truncated/legacy.jsonl"
expect_fail "A1 partially exported file" "$WORK/truncated" INPUT_INCOMPLETE
cp -r "$WORK/ok" "$WORK/nofile"; rm "$WORK/nofile/snapshots.jsonl"
expect_fail "A1 missing data file" "$WORK/nofile" INPUT_INCOMPLETE
tools/pc2_parity_export.sh 2026-10-02 "$WORK/emptyday" >/dev/null
expect_fail "A1 a day with no data" "$WORK/emptyday" EMPTY_INVENTORY

psql_db -c "create table keep as select * from public.ml_brain_snapshots where poll_ts = '2026-10-01 09:40:00+05:30'; delete from public.ml_brain_snapshots where poll_ts = '2026-10-01 09:40:00+05:30';"
tools/pc2_parity_export.sh 2026-10-01 "$WORK/missingpoll" >/dev/null
expect_fail "A1 one expected poll missing its snapshot" "$WORK/missingpoll" MISSING_SNAPSHOT_FOR_POLL
psql_db -c "insert into public.ml_brain_snapshots select * from keep; drop table keep;"

psql_db <<'SQL'
create table keep as select * from public.ml_brain_snapshots where poll_ts = '2026-10-01 09:25:00+05:30';
update public.ml_brain_snapshots s
   set context_json = jsonb_set(s.context_json, '{snapshot_pc2_authority_compact_ref}',
       (select context_json -> 'snapshot_pc2_authority_compact_ref' from public.ml_brain_snapshots
         where poll_ts = '2026-10-01 09:20:00+05:30'))
 where s.poll_ts = '2026-10-01 09:25:00+05:30';
SQL
tools/pc2_parity_export.sh 2026-10-01 "$WORK/copiedref" >/dev/null
expect_fail "A2 a reference copied onto the next poll" "$WORK/copiedref" REFERENCE_POLL_MISMATCH
psql_db -c "delete from public.ml_brain_snapshots where poll_ts = '2026-10-01 09:25:00+05:30'; insert into public.ml_brain_snapshots select * from keep; drop table keep;"

tools/pc2_parity_export.sh 2026-10-01 "$WORK/restored" >/dev/null
if audit "$WORK/restored"; then echo "PASS  restored session certifies again"; else echo "FAIL  restored session"; failures=$((failures+1)); fi

psql_db <<'SQL'
alter table public.ml_pc2_decision_batches drop constraint ml_pc2_decision_batches_poll_time_valid;
alter table public.ml_pc2_decision_batches drop constraint ml_pc2_decision_batches_identity_bound;
update public.ml_pc2_decision_batches set poll_ts_text = '2026-99-99Tgarbage'
 where poll_ts_text = '2026-10-01T09:30:00+0530';
SQL
tools/pc2_parity_export.sh 2026-10-01 "$WORK/malformed" >/dev/null && echo "PASS  the export survives a malformed stored timestamp" || { echo "FAIL  export aborted"; failures=$((failures+1)); }
expect_fail "A3 malformed stored timestamp is surfaced, not omitted" "$WORK/malformed" MALFORMED_TIMESTAMP_ROWS

echo "== $failures failure(s)"
exit $(( failures > 0 ? 1 : 0 ))
