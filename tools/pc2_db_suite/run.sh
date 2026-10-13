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
create table public.ml_brain_snapshots (id bigserial primary key, poll_ts timestamptz not null, session_date date, context_json jsonb, poll_summary_json jsonb, recommendation_id text);
create table public.ml_generated_candidates (id bigserial primary key, snapshot_poll_ts timestamptz, session_date date, recommendation_id text,
  quality_tag text, decision_source text, candidate_id text, lane text, index_key text, strategy_type text, trade_mode text, rank integer,
  watchlist_rank integer, was_surfaced boolean, width integer, expiry text, execution_gate text, entry_action text, app_version text, brain_version text);
create table public.ml_pc2_authority_decisions (id bigserial primary key, poll_ts timestamptz not null, decision_index integer not null, decision_json jsonb not null);
create table public.ml_option_chain_snapshots (poll_ts timestamptz not null, index_key text not null, strike numeric not null, option_type text not null,
  primary key (poll_ts, index_key, strike, option_type));
grant select, insert on public.ml_pc2_authority_decisions to anon, authenticated;
grant usage, select on sequence public.ml_pc2_authority_decisions_id_seq to anon, authenticated;
SQL
psql_db -f supabase/migrations/20261012101537_ml_brain_snapshots_poll_ts_idempotency.sql >/dev/null 2>&1
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

echo "== b503 R6: capture provenance through the real exporter and Pc2ParityAudit (IST day 2026-10-05)"
prov() { # key ordinal slot_hm delivery attempt [origin]
  printf '{"schema":"capture_provenance_v1","capture_key":"2026-10-05|%s","session_date":"2026-10-05","slot_ordinal":%s,"delivery":"%s","attempt_id":"%s","slot_scheduled_ist":"2026-10-05T%s:00+0530","origin_poll_ts":"2026-10-05T%s:00+0530"}' "$1" "$1" "$3" "$4" "$2" "$2"
}
snap() { # poll_ts provenance-json-or-null
  psql_db -c "insert into public.ml_brain_snapshots (poll_ts, session_date, context_json, poll_summary_json) values ('$1', '2026-10-05', '{}', $( [[ $2 == null ]] && echo null || echo "jsonb_build_object('capture_provenance', '$2'::jsonb)" ))" >/dev/null
}
psql_db -c "insert into public.ml_option_chain_snapshots values ('2026-10-05 09:15:04+05:30','NIFTY',1,'CE'), ('2026-10-05 09:20:03+05:30','NIFTY',1,'CE'), ('2026-10-05 09:25:05+05:30','NIFTY',1,'CE'), ('2026-10-05 09:30:02+05:30','NIFTY',1,'CE')" >/dev/null
snap '2026-10-05 09:15:20+05:30' "$(prov 1 09:15 ON_TIME a1)"
snap '2026-10-05 09:21:40+05:30' "$(prov 2 09:20 LATE a2)"     # completed 97 s after its chain poll
snap '2026-10-05 09:31:10+05:30' "$(prov 3 09:25 LATE a3)"     # rolled over past the NEXT slot's poll
snap '2026-10-05 09:30:30+05:30' null                          # pre-b503 row: legacy 60 s rule
r6_cat() { python3 -c "import json,sys; r=json.load(open('$1/pc2_parity_report.json')); c=r['categories']; print(' '.join(sorted(k for k,v in c.items() if v.get('fail',0)>0)))"; }
tools/pc2_parity_export.sh 2026-10-05 "$WORK/r6ok" >/dev/null; audit "$WORK/r6ok" || true
r6_fails="$(r6_cat "$WORK/r6ok")"
check "R6 exporter carries capture_provenance" "grep -q capture_provenance_v1 $WORK/r6ok/snapshots.jsonl"
check "R6 >60 s completion and next-slot rollover are matched to their origin polls; legacy row by the 60 s rule (fails: '$r6_fails')" \
  "! grep -qE 'MISSING_SNAPSHOT_FOR_POLL|SNAPSHOT_WITHOUT_INVENTORY|AMBIGUOUS_INVENTORY_MATCH|INVALID_CAPTURE_PROVENANCE|DUPLICATE_ORIGIN_CLAIM|MISSING_INVENTORY_FOR_ORIGIN|SESSION_MISMATCH' <<<'$r6_fails'"
# Without provenance the same population is NOT recognised (proves the matcher, not a loosened window).
psql_db -c "create table r6keep as select * from public.ml_brain_snapshots where session_date = '2026-10-05'; update public.ml_brain_snapshots set poll_summary_json = null where session_date = '2026-10-05'" >/dev/null
tools/pc2_parity_export.sh 2026-10-05 "$WORK/r6noprov" >/dev/null; audit "$WORK/r6noprov" || true
check "R6 the same late rows without provenance stay MISSING/unmatched" "grep -qE 'MISSING_SNAPSHOT_FOR_POLL|SNAPSHOT_WITHOUT_INVENTORY|AMBIGUOUS' <<<'$(r6_cat "$WORK/r6noprov")'"
psql_db -c "delete from public.ml_brain_snapshots where session_date = '2026-10-05'; insert into public.ml_brain_snapshots select * from r6keep" >/dev/null
# Duplicate claim to one origin.
snap '2026-10-05 09:22:50+05:30' "$(prov 2 09:20 LATE a2bis)"
tools/pc2_parity_export.sh 2026-10-05 "$WORK/r6dup" >/dev/null; audit "$WORK/r6dup" || true
check "R6 two snapshots claiming one origin poll fail DUPLICATE_ORIGIN_CLAIM" "grep -q DUPLICATE_ORIGIN_CLAIM <<<'$(r6_cat "$WORK/r6dup")'"
psql_db -c "delete from public.ml_brain_snapshots where poll_ts = '2026-10-05 09:22:50+05:30'" >/dev/null
# Corrupt / inconsistent provenance.
psql_db -c "update public.ml_brain_snapshots set poll_summary_json = jsonb_set(poll_summary_json, '{capture_provenance,slot_scheduled_ist}', '\"2026-10-05T09:40:00+0530\"') where poll_ts = '2026-10-05 09:21:40+05:30'" >/dev/null
tools/pc2_parity_export.sh 2026-10-05 "$WORK/r6bad" >/dev/null; audit "$WORK/r6bad" || true
check "R6 inconsistent provenance fails INVALID_CAPTURE_PROVENANCE (never re-bucketed)" "grep -q INVALID_CAPTURE_PROVENANCE <<<'$(r6_cat "$WORK/r6bad")'"
psql_db -c "update public.ml_brain_snapshots set poll_summary_json = jsonb_build_object('capture_provenance', '\"garbage\"'::jsonb) where poll_ts = '2026-10-05 09:21:40+05:30'" >/dev/null
tools/pc2_parity_export.sh 2026-10-05 "$WORK/r6corrupt" >/dev/null; audit "$WORK/r6corrupt" || true
check "R6 corrupt (non-object) provenance fails INVALID_CAPTURE_PROVENANCE" "grep -q INVALID_CAPTURE_PROVENANCE <<<'$(r6_cat "$WORK/r6corrupt")'"
psql_db -c "delete from public.ml_brain_snapshots where session_date = '2026-10-05'; insert into public.ml_brain_snapshots select * from r6keep" >/dev/null
# Midnight: a 2026-10-05 capture stored after midnight is still exported for 2026-10-05, and its 8.6 h
# completion is outside the 6 h capture window, so it fails visibly instead of being silently counted.
snap '2026-10-06 00:01:00+05:30' "$(prov 4 09:30 LATE a4)"
tools/pc2_parity_export.sh 2026-10-05 "$WORK/r6mid" >/dev/null; audit "$WORK/r6mid" || true
check "R6 midnight: the late origin-day row is exported for its origin day" "grep -q '\"2026-10-05|4\"' $WORK/r6mid/snapshots.jsonl"
check "R6 midnight: completion outside the capture window fails INVALID_CAPTURE_PROVENANCE" "grep -q INVALID_CAPTURE_PROVENANCE <<<'$(r6_cat "$WORK/r6mid")'"
tools/pc2_parity_export.sh 2026-10-06 "$WORK/r6next" >/dev/null; audit "$WORK/r6next" || true
check "R6 midnight: the next day's audit does not count it (other-session/invalid, not SNAPSHOT_WITHOUT_INVENTORY)" "! grep -q SNAPSHOT_WITHOUT_INVENTORY <<<'$(r6_cat "$WORK/r6next")'"
psql_db -c "drop table r6keep" >/dev/null

echo "== b503 C2: a delayed but valid origin (09:52 in slot 09:50) through the real exporter and audit (2026-10-08)"
psql_db -c "insert into public.ml_option_chain_snapshots values ('2026-10-08 09:52:31+05:30','NIFTY',1,'CE')" >/dev/null
psql_db -c "insert into public.ml_brain_snapshots (poll_ts, session_date, context_json, poll_summary_json) values ('2026-10-08 09:54:00+05:30', '2026-10-08', '{}', jsonb_build_object('capture_provenance', '{\"schema\":\"capture_provenance_v1\",\"capture_key\":\"2026-10-08|8\",\"session_date\":\"2026-10-08\",\"slot_ordinal\":8,\"delivery\":\"LATE\",\"attempt_id\":\"d8\",\"slot_scheduled_ist\":\"2026-10-08T09:50:00+0530\",\"origin_poll_ts\":\"2026-10-08T09:52:00+0530\"}'::jsonb))" >/dev/null
tools/pc2_parity_export.sh 2026-10-08 "$WORK/c2" >/dev/null; audit "$WORK/c2" || true
c2_fails="$(r6_cat "$WORK/c2")"
check "C2 origin 09:52 + chain 09:52:31 (+151 s) is matched (fails: '$c2_fails')" "! grep -qE 'MISSING_INVENTORY_FOR_ORIGIN|MISSING_SNAPSHOT_FOR_POLL|SNAPSHOT_WITHOUT_INVENTORY|AMBIGUOUS' <<<'$c2_fails'"
psql_db -c "update public.ml_option_chain_snapshots set poll_ts = '2026-10-08 09:50:04+05:30' where poll_ts = '2026-10-08 09:52:31+05:30'" >/dev/null
tools/pc2_parity_export.sh 2026-10-08 "$WORK/c2bad" >/dev/null; audit "$WORK/c2bad" || true
check "C2 inventory unrelated to the recorded origin still fails" "grep -q MISSING_INVENTORY_FOR_ORIGIN <<<'$(r6_cat "$WORK/c2bad")'"

echo "== b503 C3: generated sampled-cohort audit + readiness through the real exporter (fixtures from the real capture engine, 2026-10-07)"
B503_COHORT_FIXTURE_OUT="$WORK/cohort.sql" $JVM_TEST com.marketradar.app.B503CohortFixtureTest >"$WORK/cohortfx.log" 2>&1 \
  || { cat "$WORK/cohortfx.log"; echo "FAIL  cohort fixture generation"; failures=$((failures+1)); }
psql_db -f "$WORK/cohort.sql" >/dev/null
psql_db -c "insert into public.ml_option_chain_snapshots values ('2026-10-07 09:15:04+05:30','NIFTY',1,'CE'), ('2026-10-07 09:20:03+05:30','NIFTY',1,'CE'), ('2026-10-07 09:25:05+05:30','NIFTY',1,'CE'), ('2026-10-07 09:30:02+05:30','NIFTY',1,'CE')" >/dev/null
readiness() { PC2_AUDIT_DIR="$1" $JVM_TEST com.marketradar.app.CaptureReadinessRunTest >"$1.rlog" 2>&1 || true; }
cohort_fails() { python3 -c "import json; r=json.load(open('$1/capture_cohort_report.json')); print(' '.join(sorted(k for k,v in r['categories'].items() if v['fail']>0)) or 'NONE')"; }
cohort_run() { tools/pc2_parity_export.sh 2026-10-07 "$WORK/$1" >/dev/null; readiness "$WORK/$1"; cohort_fails "$WORK/$1"; }
c3=$(cohort_run c3ok)
c3j="$WORK/c3ok/capture_cohort_report.json"; c3r="$WORK/c3ok/capture_readiness_report.json"
check "C3 exporter writes generated.jsonl declared in the manifest" "python3 -c \"import json; m=json.load(open('$WORK/c3ok/manifest.json')); assert m['files']['generated.jsonl']['rows']==9\""
check "C3 all channels intact: sampled cohort verified, 4 cohorts incl. 1 legitimate empty (fails: $c3)" \
  "[[ '$c3' == NONE ]] && python3 -c \"import json; r=json.load(open('$c3j')); assert r['sampled_cohort_verified'] and r['verified_cohorts']==4 and r['empty_cohorts']==1 and not r['full_population_verified']\""
check "C3 readiness keeps C3 and source-cap gates CLOSED; full population not verified" \
  "python3 -c \"import json; r=json.load(open('$c3r')); assert r['generated_sampled_cohort_verified'] and r['c3_gate']=='CLOSED' and r['source_cap_gate']=='CLOSED' and r['full_population_verified'] is False\""
check "C3 readiness: capture cohort NOT complete while PC2 parity is not certified" "python3 -c \"import json; r=json.load(open('$c3r')); assert r['capture_cohort_complete'] is False and r['pc2_parity_certified'] is False\""
c3case() { # label, setup SQL, expected category, undo SQL
  psql_db -c "$2" >/dev/null; local got; got=$(cohort_run "c3_$5"); psql_db -c "$4" >/dev/null
  check "C3 $1 -> $3 (got: $got)" "grep -qw '$3' <<<'$got'"
}
psql_db -c "create table g_keep as select * from public.ml_generated_candidates; create table s_keep as select * from public.ml_brain_snapshots where session_date = '2026-10-07'" >/dev/null
RESTORE="truncate public.ml_generated_candidates; insert into public.ml_generated_candidates select * from g_keep; delete from public.ml_brain_snapshots where session_date = '2026-10-07'; insert into public.ml_brain_snapshots select * from s_keep"
c3case "generated upload absent (slot 1)" "delete from public.ml_generated_candidates where snapshot_poll_ts = '2026-10-07 09:15:00+05:30'" GENERATED_COHORT_ABSENT "$RESTORE" absent
c3case "one row missing (slot 2, NF only)" "delete from public.ml_generated_candidates where id = (select min(id) from public.ml_generated_candidates where snapshot_poll_ts = '2026-10-07 09:20:00+05:30')" COHORT_ROWS_MISSING "$RESTORE" missing
c3case "changed identity" "update public.ml_generated_candidates set candidate_id = 'NF-99' where candidate_id = 'NF-0' and snapshot_poll_ts = '2026-10-07 09:15:00+05:30'" COHORT_EXTRA_ROWS "$RESTORE" ident
c3case "changed content" "update public.ml_generated_candidates set strategy_type = 'IRON_FLY' where candidate_id = 'BNF-1' and snapshot_poll_ts = '2026-10-07 09:15:00+05:30'" COHORT_CONTENT_CONFLICT "$RESTORE" content
c3case "wrong index (NF/BNF mixture)" "update public.ml_generated_candidates set index_key = 'BNF' where candidate_id = 'NF-1' and snapshot_poll_ts = '2026-10-07 09:15:00+05:30'" COHORT_WRONG_INDEX "$RESTORE" index
c3case "extra row" "insert into public.ml_generated_candidates (snapshot_poll_ts, session_date, recommendation_id, candidate_id, lane, index_key, rank) select snapshot_poll_ts, session_date, recommendation_id, 'NF-77', lane, index_key, 77 from g_keep where candidate_id = 'NF-0' and snapshot_poll_ts = '2026-10-07 09:15:00+05:30'" COHORT_EXTRA_ROWS "$RESTORE" extra
c3case "orphan rows (no snapshot claims them)" "insert into public.ml_generated_candidates (snapshot_poll_ts, session_date, recommendation_id, candidate_id, lane, index_key, rank) values ('2026-10-07 09:45:00+05:30', '2026-10-07', 'rec-x', 'NF-0', 'NF_intraday', 'NF', 1)" ORPHAN_GENERATED_ROWS "$RESTORE" orphan
c3case "rows of a legitimate empty cohort (slot 4)" "insert into public.ml_generated_candidates (snapshot_poll_ts, session_date, recommendation_id, candidate_id, lane, index_key, rank) values ('2026-10-07 09:30:00+05:30', '2026-10-07', 'rec-y', 'NF-0', 'NF_intraday', 'NF', 1)" COHORT_EXTRA_ROWS "$RESTORE" emptyextra
c3case "lineage: rows of another recommendation" "update public.ml_generated_candidates set recommendation_id = 'rec-other' where snapshot_poll_ts = '2026-10-07 09:25:00+05:30'" COHORT_LINEAGE_MISMATCH "$RESTORE" lineage
c3case "wrong manifest version" "update public.ml_brain_snapshots set poll_summary_json = jsonb_set(poll_summary_json, '{generated_cohort_manifest,schema}', '\"generated_cohort_manifest_v0\"') where poll_ts = '2026-10-07 09:15:20+05:30'" COHORT_WRONG_VERSION "$RESTORE" version
c3case "empty expectation without an original reason" "update public.ml_brain_snapshots set poll_summary_json = jsonb_set(poll_summary_json, '{generated_cohort_manifest,reason}', '\"\"') where poll_ts = '2026-10-07 09:30:30+05:30'" INVALID_COHORT_MANIFEST "$RESTORE" noreason
c3case "b503 snapshot without a manifest" "update public.ml_brain_snapshots set poll_summary_json = poll_summary_json - 'generated_cohort_manifest' where poll_ts = '2026-10-07 09:22:40+05:30'" MISSING_COHORT_MANIFEST "$RESTORE" nomanifest
c3case "pre-b503 snapshot is unverifiable, not certified" "insert into public.ml_brain_snapshots (poll_ts, session_date, context_json) values ('2026-10-07 09:35:20+05:30', '2026-10-07', '{}')" COHORT_UNVERIFIABLE_PRE_B503 "$RESTORE" pre
c3case "generation failed at capture" "update public.ml_brain_snapshots set poll_summary_json = jsonb_set(poll_summary_json, '{generated_cohort_manifest,status}', '\"BUILD_FAILED\"') where poll_ts = '2026-10-07 09:30:30+05:30'" GENERATED_BUILD_FAILED "$RESTORE" failed
check "C3 late (slot 2, +160 s) and rollover (slot 3, completed after slot 4 poll) lineage verified in the intact run" \
  "python3 -c \"import json; r=json.load(open('$c3j')); assert not any(f['severity']=='FAIL' for f in r['findings'])\""
psql_db -c "drop table g_keep; drop table s_keep" >/dev/null

echo "== b503 capture events, legacy replay and collector designation"
psql_db -f supabase/migrations/20261009120000_paper_decision_menu_evidence.sql >/dev/null 2>&1
check "capture-event migration applies" "psql_db -v ON_ERROR_STOP=1 -f supabase/migrations/20261013120000_capture_events.sql >/dev/null 2>&1"
check "capture-event migration re-applies idempotently" "psql_db -v ON_ERROR_STOP=1 -f supabase/migrations/20261013120000_capture_events.sql >/dev/null 2>&1"
psql -X -d "$DB" -f tools/pc2_db_suite/b503_capture.sql >"$WORK/b503.out" 2>&1 || true
grep -E "^(PASS|FAIL)|ERROR" "$WORK/b503.out" || true
b503_fail=$(grep -cE "^FAIL|ERROR" "$WORK/b503.out" || true)
b503_pass=$(grep -c "^PASS" "$WORK/b503.out" || true)
check "b503 capture suite: $b503_pass passed, $b503_fail failed" "[[ $b503_fail -eq 0 && $b503_pass -ge 32 ]]"
# R4 concurrency at the boundary: 12 parallel sessions race for the last slot; exactly one may insert.
psql_db -q <<'SQL' >/dev/null
insert into pc2_private.ingest_devices (device_key_hash, label, active)
  values (encode(sha256(convert_to(repeat('d',64),'UTF8')),'hex'), 'b503 D', true);
insert into capture_private.writer_ledger (device_key_hash, ist_day, events)
  values (encode(sha256(convert_to(repeat('d',64),'UTF8')),'hex'), (now() at time zone 'Asia/Kolkata')::date, 4999);
SQL
for i in $(seq 1 12); do
  ( psql -X -q -t -A -d "$DB" -c "set role anon; select public.capture_event_record(repeat('d',64), public.b503_ev('2026-10-13|$i','race$i','CAPTURE_STUCK'))->>'status'" >"$WORK/race.$i" 2>&1 || true ) &
done
wait
race_ins=$(cat "$WORK"/race.* | grep -c '^INSERTED$' || true)
race_cap=$(cat "$WORK"/race.* | grep -c 'CAPTURE_EVENT_DAILY_CAP' || true)
check "R4 concurrent boundary: exactly one of 12 inserts at 4999 ($race_ins inserted, $race_cap PT429)" "[[ $race_ins -eq 1 && $race_cap -eq 11 ]]"
race_state=$(psql_db -t -A -c "select l.events || '/' || (select count(*) from public.ml_capture_events e where e.device_key_hash = l.device_key_hash) from capture_private.writer_ledger l where l.device_key_hash = encode(sha256(convert_to(repeat('d',64),'UTF8')),'hex')")
check "R4 concurrent boundary: ledger ends at exactly 5000 with 1 stored row ($race_state)" "[[ '$race_state' == 5000/1 ]]"
# R4 total persisted bound: fill one device's day to the byte cap with maximum-size events and measure every
# representation (heap + toast + indexes of events, conflicts, ledger) against the 8 MiB input allowance.
psql_db -q -t -A <<'SQL' >"$WORK/r4_bound.txt"
insert into pc2_private.ingest_devices (device_key_hash, label, active)
  values (encode(sha256(convert_to(repeat('e',64),'UTF8')),'hex'), 'b503 E', true);
create temp table base as select pg_total_relation_size('public.ml_capture_events') ev, pg_total_relation_size('capture_private.conflicts') cf,
  pg_total_relation_size('capture_private.writer_ledger') lg;
do $$ declare i int := 0; r jsonb; begin
  loop
    i := i + 1;
    begin
      r := public.capture_event_record(repeat('e',64), public.b503_ev('2026-10-13|' || i, 'fill' || i, 'CAPTURE_STUCK')
           || jsonb_build_object('detail', jsonb_build_object('pad', (select string_agg(md5(i || '-' || g), '') from generate_series(1, 231) g))));
    exception when others then exit; end;
    exit when i > 6000;
  end loop; end $$;
do $$ declare i int := 0; begin
  loop i := i + 1;
    begin perform public.capture_event_record(repeat('e',64), public.b503_ev('2026-10-13|' || (i % 900 + 1), 'fill' || (i % 900 + 1), 'CAPTURE_STUCK', 'c' || i));
    exception when others then exit; end;
    exit when i > 2000; end loop; end $$;
select l.events || ' ' || l.conflicts || ' ' || l.bytes || ' ' ||
  (pg_total_relation_size('public.ml_capture_events') - b.ev) || ' ' || (pg_total_relation_size('capture_private.conflicts') - b.cf) || ' ' ||
  (pg_total_relation_size('capture_private.writer_ledger') - b.lg)
  from capture_private.writer_ledger l, base b where l.device_key_hash = encode(sha256(convert_to(repeat('e',64),'UTF8')),'hex');
SQL
read -r r4_ev r4_cf r4_bytes r4_evsz r4_cfsz r4_lgsz < <(grep -E '^[0-9]+ ' "$WORK/r4_bound.txt" | tail -1)
r4_total=$(( r4_evsz + r4_cfsz + r4_lgsz ))
echo "INFO  R4 bound: events=$r4_ev conflicts=$r4_cf charged_bytes=$r4_bytes persisted_total=$r4_total (events $r4_evsz, conflicts $r4_cfsz, ledger $r4_lgsz)"
check "R4 the cap stops a filling device at <= 8 MiB charged and <= 5000 rows" "[[ $r4_bytes -le 8388608 && $(( r4_ev + r4_cf )) -le 5000 && $r4_cf -le 500 ]]"
check "R4 measured persisted total <= 24 MiB/device/day (documented bound)" "[[ $r4_total -le 25165824 ]]"
# Second shape: many small incompressible events, so the 5000-row cap (not the byte cap) binds.
psql_db -q -t -A <<'SQL' >"$WORK/r4_bound2.txt"
insert into pc2_private.ingest_devices (device_key_hash, label, active)
  values (encode(sha256(convert_to(repeat('f',64),'UTF8')),'hex'), 'b503 F', true);
create temp table base as select pg_total_relation_size('public.ml_capture_events') ev;
do $$ declare i int := 0; begin
  loop i := i + 1;
    begin perform public.capture_event_record(repeat('f',64), public.b503_ev('2026-10-14|' || i, 'small' || i, 'CAPTURE_STUCK')
           || jsonb_build_object('detail', jsonb_build_object('pad', (select string_agg(md5(i || ':' || g), '') from generate_series(1, 30) g))));
    exception when others then exit; end;
    exit when i > 6000; end loop; end $$;
select l.events || ' ' || l.bytes || ' ' || (pg_total_relation_size('public.ml_capture_events') - b.ev)
  from capture_private.writer_ledger l, base b where l.device_key_hash = encode(sha256(convert_to(repeat('f',64),'UTF8')),'hex');
SQL
read -r r4s_ev r4s_bytes r4s_sz < <(grep -E '^[0-9]+ ' "$WORK/r4_bound2.txt" | tail -1)
echo "INFO  R4 bound (row-cap shape): events=$r4s_ev charged_bytes=$r4s_bytes persisted_events=$r4s_sz"
check "R4 row-cap shape stops at 5000 events and <= 24 MiB persisted" "[[ $r4s_ev -eq 5000 && $r4s_sz -le 25165824 ]]"
HA="$(python3 -c 'import hashlib; print(hashlib.sha256(b"a"*64).hexdigest())')"
HB="$(python3 -c 'import hashlib; print(hashlib.sha256(b"b"*64).hexdigest())')"
psql_db -c "update pc2_private.ingest_devices set active = true where label = 'b503 B'" >/dev/null
# psql exits non-zero on a refusal; capture first so pipefail does not mask the matched message.
designate() { psql -X -q -d "$DB" -v hash="$1" -v label="$2" -f tools/pdm_collector/designate.sql 2>&1 || true; }
revoke() { psql -X -q -d "$DB" -v hash="$1" -f tools/pdm_collector/revoke.sql 2>&1 || true; }
check "designation inserts the first collector" "designate $HA 'Vivek phone' | grep -q 'DESIGNATED hash'"
check "re-running for the same hash is the ALREADY_DESIGNATED no-op" "designate $HA 'Vivek phone' | grep -q ALREADY_DESIGNATED"
check "a second device is refused (ANOTHER_COLLECTOR_ACTIVE)" "designate $HB 'other' | grep -q ANOTHER_COLLECTOR_ACTIVE"
check "a malformed hash is refused" "designate nothex 'x' | grep -q HASH_MALFORMED"
check "exactly one active collector after all attempts" "[[ \$(psql -X -At -d $DB -c 'select count(*) from pdm_private.collectors where active') == 1 ]]"
check "revocation touches exactly the one row" "revoke $HA | grep -q REVOKED"
check "revoking again is refused" "revoke $HA | grep -q NOT_AN_ACTIVE_COLLECTOR"
check "a revoked collector is not silently re-activated" "designate $HA 'Vivek phone' | grep -q PREVIOUSLY_REVOKED"
check "read-only checks run" "psql -X -q -d $DB -v day=2026-10-14 -f tools/pdm_collector/checks.sql >/dev/null 2>&1"

echo "== $failures failure(s)"
exit $(( failures > 0 ? 1 : 0 ))
