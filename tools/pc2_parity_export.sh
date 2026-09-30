#!/usr/bin/env bash
# DB-1 parity export: READ ONLY. Produces the three JSON-Lines files consumed
# by Pc2ParityAudit for one explicit IST day, [D 00:00+05:30, D+1 00:00+05:30).
#
#   PC2_DB_URL='postgresql://...' tools/pc2_parity_export.sh 2026-10-01 [out_dir]
#   PC2_AUDIT_DIR=out_dir ./gradlew :app:testDebugUnitTest \
#       --tests 'com.marketradar.app.Pc2ParityAuditRunTest'
#
# The session is forced read-only (default_transaction_read_only=on), so no
# statement in this file can write even if edited by mistake.
set -euo pipefail

day="${1:?usage: pc2_parity_export.sh YYYY-MM-DD [out_dir]}"
out="${2:-pc2_parity_${day}}"
[[ "$day" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "date must be YYYY-MM-DD" >&2; exit 2; }
: "${PC2_DB_URL:?set PC2_DB_URL to a connection string}"
mkdir -p "$out"
export PGOPTIONS='-c default_transaction_read_only=on'
PSQL=(psql "$PC2_DB_URL" -X -q -At -v ON_ERROR_STOP=1 -v day="$day")

"${PSQL[@]}" -f - > "$out/snapshots.jsonl" <<'SQL'
select pg_catalog.json_build_object(
         'poll_ts_utc', to_char(poll_ts at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
         'session_date', session_date::text,
         'compact_ref', context_json -> 'snapshot_pc2_authority_compact_ref',
         'decisions', context_json -> 'snapshot_pc2_authority_decisions',
         'policy', context_json -> 'snapshot_pc2_authority_policy',
         'brain_version', context_json -> 'snapshot_brain_version')::text
  from public.ml_brain_snapshots
 where poll_ts >= (:'day' || ' 00:00:00+05:30')::timestamptz
   and poll_ts <  (:'day' || ' 00:00:00+05:30')::timestamptz + interval '1 day'
 order by poll_ts;
SQL

"${PSQL[@]}" -f - > "$out/batches.jsonl" <<'SQL'
select pg_catalog.row_to_json(b)::text
  from (select batch_id, session_date_text, poll_ts_text, brain_version, policy_hash,
               policy_version, authority_diagnostics_version, grouping_schema_version,
               digest_algorithm, decision_count, distinct_decision_count,
               decision_digest, grouped_digest, grouped_canonical, complete
          from public.ml_pc2_decision_batches
         where session_date_text = :'day'
            or (poll_ts_text::timestamptz >= (:'day' || ' 00:00:00+05:30')::timestamptz
                and poll_ts_text::timestamptz < (:'day' || ' 00:00:00+05:30')::timestamptz + interval '1 day')
         order by poll_ts_text, batch_id) b;
SQL

"${PSQL[@]}" -f - > "$out/legacy.jsonl" <<'SQL'
select pg_catalog.json_build_object(
         'poll_ts_utc', to_char(poll_ts at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
         'decision_index', decision_index,
         'decision_json', decision_json)::text
  from public.ml_pc2_authority_decisions
 where poll_ts >= (:'day' || ' 00:00:00+05:30')::timestamptz
   and poll_ts <  (:'day' || ' 00:00:00+05:30')::timestamptz + interval '1 day'
 order by poll_ts, decision_index;
SQL

echo "exported $(wc -l < "$out/snapshots.jsonl") snapshots, $(wc -l < "$out/batches.jsonl") batches, $(wc -l < "$out/legacy.jsonl") legacy rows to $out"
